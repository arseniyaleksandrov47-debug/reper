import asyncio
import copy
import time
import unittest
from collections import deque
from types import SimpleNamespace

from real_estate_engine import NvidiaCompetitorAnalyzer
from real_estate_engine.ai_contract import AI_DEADLINE
from real_estate_engine.ai_pacing import AIRequestPacer
from real_estate_engine.floor_policy import BusinessFloorProfileResolver
from real_estate_engine.market_context import (
    DEFAULT_UTILITY_SCALES,
    STRENGTH,
    apply_default_market_scales,
    evaluate_market_context,
    market_context_schema,
    validate_market_context,
)
from real_estate_engine.ranking_policy import RankingProfileResolver, bind_reference_scales
from real_estate_engine.business_model import concept_digest
from floor_ai_fixtures import FixtureFloorAnalyzer, ranking_fixture_response, scenario_transport_fixture
from real_estate_engine.ai_contract import AIContractError
from floor_ai_fixtures import RecordingFloorInference
from test_adaptive_floor_policy import prop
from test_confirmation_cause_fixes import transport,response
from test_market_context import market_config,CONCEPT,candidate,environment


class FloorDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_floor_worker_receives_the_earlier_caller_deadline(self):
        model = RecordingFloorInference()
        original = model._call_floor_structured_sync
        deadlines = []

        def record_deadline(*args, **kwargs):
            deadlines.append(AI_DEADLINE.get())
            return original(*args, **kwargs)

        model._call_floor_structured_sync = record_deadline
        resolver = BusinessFloorProfileResolver(model)
        profile = await resolver.resolve('Произвольная деятельность', 'Клиенты по записи')
        deadline = time.monotonic() + 2
        token = AI_DEADLINE.set(deadline)
        try:
            await resolver.assess_many([prop(1, id=1)], profile, timeout_s=5)
            self.assertEqual(AI_DEADLINE.get(), deadline)
        finally:
            AI_DEADLINE.reset(token)
        self.assertTrue(deadlines)
        self.assertTrue(all(value == deadline for value in deadlines))

    async def test_rate_queue_cannot_start_a_request_after_floor_budget(self):
        starts = []
        model = NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        model.model = model.floor_model = 'test-primary'
        model.floor_review_model = 'test-review'
        model.endpoint = 'https://example.test'
        model.assessment_cache_path = None
        model.http_session = SimpleNamespace(post=lambda *args, **kwargs: (
            starts.append(time.monotonic()) or SimpleNamespace(status_code=200, content=b'{}')))
        model.floor_request_pacer = AIRequestPacer(0, 1)
        model.floor_request_pacer._http_starts = deque([time.monotonic() - 59.9])
        resolver = BusinessFloorProfileResolver(model)
        profile = await resolver.resolve('Произвольная деятельность', 'Клиенты по записи')
        await resolver.assess_many([prop(1, id=1)], profile, timeout_s=.03)
        await asyncio.sleep(.15)
        self.assertEqual(starts, [])


class NativeMarketSchemaTests(unittest.TestCase):
    def test_schema_preserves_required_finance_inputs_and_optional_audience(self):
        schema = market_context_schema({'s0': 'Описание клиента'})['properties']
        for group, names in (('strength', STRENGTH), ('alternatives', ('direct', 'indirect'))):
            for name in names:
                weight = schema[group]['properties'][name]['properties']['weight']
                self.assertEqual(weight['exclusiveMinimum'], 0)
        audience_weight = schema['audience']['properties']['transit_flow']['properties']['weight']
        self.assertEqual(audience_weight['minimum'], 0)
        self.assertNotIn('exclusiveMinimum', audience_weight)

    def test_zero_utility_scale_is_rejected_with_the_exact_field(self):
        config=market_config()
        config['references']['site_audience']['value']=0
        config['references']['residents']['value']=0
        with self.assertRaises(AIContractError) as error:
            validate_market_context(config,CONCEPT)
        self.assertIn('references.site_audience',str(error.exception))
        self.assertIn('references.residents',str(error.exception))

    def test_json_output_keeps_the_full_profile_schema_in_the_request(self):
        requests=[]
        model=transport(lambda *args,**kwargs:(requests.append(kwargs) or response()))
        model.floor_model='test-primary'
        schema={'type':'object','required':['criteria'],'properties':{'criteria':{'type':'array'}}}
        model._call_ranking_profile_structured_sync('client profile',800,schema)
        payload=requests[0]['json']
        self.assertEqual(payload['response_format'],{'type':'json_object'})
        text=payload['messages'][-1]['content']
        self.assertIn('OUTPUT_JSON_SCHEMA',text)
        self.assertIn('criteria',text)

    def test_disabled_access_has_no_required_time_scale(self):
        config=market_config()
        config['access'].update(kind='none',weight=0,reference_minutes=0)
        result=validate_market_context(config,CONCEPT)
        self.assertEqual(result['access']['reference_minutes'],0)
        score,details=evaluate_market_context(result,candidate(),environment())
        self.assertLessEqual(score.low,score.high)
        self.assertEqual(details['access_factor'],{'low':1,'high':1})
        config['access'].update(kind='walk',weight=.1)
        with self.assertRaises(AIContractError):validate_market_context(config,CONCEPT)

    def test_market_context_schema_omits_ai_utility_scales(self):
        schema = market_context_schema({'s0': 'Описание клиента'})['properties']
        for removed in ('references', 'competition_scale', 'outside_pressure'):
            self.assertNotIn(removed, schema)
        self.assertNotIn('reference_minutes', schema['access']['properties'])

    def test_deterministic_default_scales_cover_all_access_kinds(self):
        base = {k: market_config()[k] for k in ('audience', 'alternatives', 'strength', 'access')}
        for kind, expected_min in (('none', 0), ('walk', 15), ('delivery', 30)):
            cfg = copy.deepcopy(base)
            cfg['access']['kind'] = kind
            cfg['access']['weight'] = 0 if kind == 'none' else 0.3
            cfg['access']['reference_minutes'] = 999
            cfg['references'] = {'residents': {'value': 123, 'reason': 'hallucinated'}}
            applied = apply_default_market_scales(cfg)
            self.assertEqual(applied['access']['reference_minutes'], expected_min)
            for key, expected_val in DEFAULT_UTILITY_SCALES['references'].items():
                self.assertEqual(applied['references'][key]['value'], expected_val)
            self.assertEqual(applied['competition_scale']['value'], DEFAULT_UTILITY_SCALES['competition_scale'])
            self.assertEqual(applied['outside_pressure']['value'], DEFAULT_UTILITY_SCALES['outside_pressure'])

    def test_resolver_populates_code_default_scales_without_ai_scale_invention(self):
        import json

        param_calls = []

        class SlimSchemaAnalyzer(FixtureFloorAnalyzer):
            @staticmethod
            @scenario_transport_fixture
            def _call_scenario_contract_structured_sync(prompt, tokens, schema):
                return FixtureFloorAnalyzer._call_scenario_contract_structured_sync(prompt, tokens, schema)

            @staticmethod
            def _call_profile_parameters_structured_sync(prompt, tokens, **kwargs):
                param_calls.append(prompt)
                raise AssertionError('Should not invoke parameter repair when code defaults provide scales')

            @staticmethod
            def _call_ranking_profile_structured_sync(prompt, tokens, schema):
                payload = json.loads(prompt.split('\nINPUT_JSON\n', 1)[1])
                first_id = next(iter(payload['concept_fragments']))
                # Build slim market_context matching updated schema (no references/competition_scale/outside_pressure/reference_minutes)
                full_cfg = market_config()
                slim_cfg = {
                    'audience': {k: dict(weight=v['weight'], concept_quote=first_id, reason='ok') for k, v in full_cfg['audience'].items()},
                    'alternatives': {k: dict(weight=v['weight'], concept_quote=first_id, reason='ok') for k, v in full_cfg['alternatives'].items()},
                    'strength': {k: dict(weight=v['weight'], concept_quote=first_id, reason='ok') for k, v in full_cfg['strength'].items()},
                    'access': dict(kind='walk', weight=0.4, concept_quote=first_id, reason='pedestrian route'),
                }
                base = ranking_fixture_response(payload['concept'])
                reply = dict(
                    status='ready',
                    summary=base['summary'],
                    criteria=dict(
                        audience_access_choice=dict(metric='market_fit_index', weight=0.7, preference='higher', low=0, high=100, concept_quote=first_id, reason='mfi', assumption='default'),
                        occupancy_cost=dict(metric='rent_month', weight=0.3, preference='lower', low=0, high=100, concept_quote=first_id, reason='rent', assumption='placeholder'),
                        physical_fit=None,
                    ),
                    questions=[],
                    assumptions=[],
                    visit_context_quotes=[first_id],
                    unmodeled_requirements=[],
                    market_context=slim_cfg,
                )
                return json.dumps(reply, ensure_ascii=False), 'slim-ranking-model'

        async def run():
            resolver = RankingProfileResolver(SlimSchemaAnalyzer())
            return await resolver.resolve('Кофейня', CONCEPT)

        profile = asyncio.run(run())
        self.assertEqual(param_calls, [])
        self.assertEqual(profile.status, 'ready')
        self.assertEqual(profile.metadata['market_reference_provenance']['kind'], 'system_default_scale')
        mc = profile.metadata['market_context']
        self.assertEqual(mc['access']['reference_minutes'], 15)
        self.assertEqual(mc['references']['residents']['value'], DEFAULT_UTILITY_SCALES['references']['residents'])
        self.assertEqual(mc['competition_scale']['value'], DEFAULT_UTILITY_SCALES['competition_scale'])
        self.assertEqual(mc['outside_pressure']['value'], DEFAULT_UTILITY_SCALES['outside_pressure'])
        bound = bind_reference_scales(
            profile,
            ranking_scales=dict(
                concept_sha256=concept_digest(CONCEPT),
                metrics={},
                market_context=dict(kind='client_reference', source='Client explicit override', references=dict(residents=2500)),
            ),
        )
        self.assertEqual(bound.metadata['market_context']['references']['residents']['value'], 2500)
        self.assertEqual(bound.metadata['market_reference_provenance']['kind'], 'client_reference')


if __name__ == '__main__':
    unittest.main()
