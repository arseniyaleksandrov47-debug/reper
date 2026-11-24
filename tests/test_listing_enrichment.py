import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from datetime import datetime,timezone

from real_estate_engine.collectors.avito import catalogue_description
from real_estate_engine.listing_enrichment import recover_catalogue_details,merge_unit_sources,fresh_sources
from real_estate_engine.floor_policy import extract_floor_evidence,FloorAssessment,floor_candidate_key
from real_estate_engine.property_sources import PublicPropertySources,PublicSourceError
from real_estate_engine.environment_cache import EnvironmentSourceCache
import test_property_confirmation as confirmation_tests
from real_estate_engine.property_confirmation import CHECKS
from real_estate_engine.property_confirmation import confirmation_schema
from test_engine_regressions import property_offer


def item(description,identity=123):
    return dict(id=123,iva=dict(DescriptionStep=[dict(payload=dict(description=description,debug=dict(id=identity)))]))


class DetailParsingTests(unittest.TestCase):
    def test_empty_requirements_do_not_generate_an_empty_keyed_object(self):
        empty=confirmation_schema(['s0:description'],[])['properties']['client_requirements']
        self.assertNotIn('items',empty['properties'])
        nonempty=confirmation_schema(['s0:description'],[dict(id='r1')])['properties']['client_requirements']
        self.assertEqual(nonempty['properties']['items']['required'],['r1'])

    def test_own_component_is_kept_and_other_unit_is_ignored(self):
        self.assertEqual(catalogue_description(item('Доступ со двора.')),'Доступ со двора.')
        self.assertEqual(catalogue_description(item('Другой лот.',999)),'')
        data=item('Описание полностью.');data['description']='Описание'
        self.assertEqual(catalogue_description(data),'Описание полностью.')

    def test_saved_snapshot_recovers_description_and_unit_floor_without_faking_freshness(self):
        raw=dict(source_payload=item('Первый двор. 1 этаж. Вход через парадную.'),source_fetched_at=1234)
        prop=dict(source='avito',external_id='123',title='Помещение, 1 эт, 31 м2',floor=None,
            description='',raw_json=json.dumps(raw))
        changed=recover_catalogue_details(prop)
        self.assertEqual(changed['floor'],1)
        self.assertEqual(json.loads(changed['raw_json'])['source_fetched_at'],1234)
        self.assertEqual(prop['description'],'')
        raw['source_payload']=item('Две комнаты, вход со двора.')
        prop.update(title='Две комнаты',raw_json=json.dumps(raw))
        self.assertIsNone(recover_catalogue_details(prop)['floor'])

    def test_building_height_is_not_unit_floor(self):
        for title in ('7-этажный дом','В здании 7 этажей'):
            self.assertFalse(extract_floor_evidence(dict(title=title)) .floors)
        self.assertEqual(extract_floor_evidence(dict(title='Офис 1 эт.')).floors,(1,))

    def test_fresh_identity_required_and_conflicting_floors_are_preserved(self):
        sources=[dict(identity_checked=True,fetched_at=time.time(),floor='1-й этаж',facts=dict(description='Вход с улицы.'))]
        prop=dict(floor=None,description='',title='Помещение')
        changed,updates=merge_unit_sources(prop,fresh_sources(dict(sources=sources)))
        self.assertEqual(changed['floor'],1);self.assertIn('description',updates)
        for patch in (dict(identity_checked=False),dict(fetched_at=time.time()-301)):
            self.assertFalse(fresh_sources(dict(sources=[{**sources[0],**patch}])))
        changed,_=merge_unit_sources(prop,[*sources,{**sources[0],'floor':7}])
        self.assertIsNone(changed['floor'])
        self.assertEqual(extract_floor_evidence(changed).floors,(1,7))

    def test_blocked_card_keeps_received_exact_fresh_catalogue_facts_with_original_timestamp(self):
        stamp=time.time()-25
        payload=item('Вход с улицы. Первый этаж.')
        payload.update(title='Помещение 31 м2',urlPath='/city/offer_123',
            coords=dict(address_user='Санкт-Петербург, Гороховая улица, 34'),
            priceDetailed=dict(value=49900,postfix='в месяц',fullString='49 900 ₽ в месяц'))
        prop=dict(source='avito',external_id='123',url='https://www.avito.ru/city/offer_123',
            address=payload['coords']['address_user'],area_sqm=31,
            raw_json=json.dumps(dict(source_payload=payload,source_fetched_at=datetime.fromtimestamp(stamp,timezone.utc).isoformat())))
        provider=PublicPropertySources(SimpleNamespace(environment_source_cache=EnvironmentSourceCache()))
        def blocked(url):raise PublicSourceError('http_status',403)
        provider._get=blocked
        result=provider.collect(prop)
        self.assertEqual(len(result['sources']),1)
        source=result['sources'][0]
        self.assertAlmostEqual(source['fetched_at'],stamp,places=5)
        self.assertEqual(source['floor'],1);self.assertIn('Вход с улицы',source['facts']['description'])
        self.assertTrue(result['errors'])
        for changes in (dict(source_fetched_at=datetime.fromtimestamp(time.time()-301,timezone.utc).isoformat()),
                        dict(source_payload={**payload,'id':999})):
            changed={**json.loads(prop['raw_json']),**changes}
            self.assertFalse(provider.collect({**prop,'raw_json':json.dumps(changed)})['sources'])


class DetailAdmissionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=confirmation_tests.ContinuationTests.asyncSetUp

    async def test_new_source_before_admission_updates_floor_and_excludes_changed_budget(self):
        self.storage.upsert_batch([property_offer('unknown',floor=None,price_month=24000)])
        seen=[]
        class Source:
            def collect(self,prop):
                return dict(sources=[dict(identity_checked=True,fetched_at=time.time(),floor=1,price_month=180000)])
        self.ai.property_verifier=SimpleNamespace(sources=Source())
        original=self.engine.floor_profile_resolver.assess_many
        async def assess(properties,*args,**kwargs):
            seen.extend(p['external_id'] for p in properties)
            return await original(properties,*args,**kwargs)
        self.engine.floor_profile_resolver.assess_many=assess
        self.ai.property_verifier.verify=self._checked
        report=await self.engine.find_optimal_properties(**self.kw)
        self.assertNotIn('unknown',seen)
        rejected=next(c for c in report.floor_rejected_candidates if c.property_data['external_id']=='unknown')
        self.assertEqual(rejected.property_data['price_month'],180000)
        self.assertEqual(rejected.assessment.source,'current_unit_source')

    @staticmethod
    async def _checked(*args):return dict(status='source_conditions_checked',reasons=[],checks={k:dict(result='pass') for k in CHECKS})

    async def test_late_floor_updates_reassess_and_remove_stale_pending_only_after_pass(self):
        self.storage.upsert_batch([property_offer('unknown',floor=None,price_month=24000)])
        original=self.engine.floor_profile_resolver.assess_many
        seen=[]
        async def assess(properties,*args,**kwargs):
            results=await original(properties,*args,**kwargs)
            for p in properties:
                if p['external_id']=='unknown':
                    seen.append(p['floor'])
                    if p['floor']==1:results[floor_candidate_key(p)]=FloorAssessment('eligible',(1,),'Подтверждён вход на уровне улицы','test_independent')
            return results
        self.engine.floor_profile_resolver.assess_many=assess
        class Verify:
            async def verify(self,candidate,*args):
                return dict(status='source_conditions_checked',reasons=[],checks={k:dict(result='pass') for k in CHECKS},
                    sources=[dict(identity_checked=True,fetched_at=time.time(),floor=1,
                        facts=dict(description='Доступ посетителей без пропусков. Отдельный вход с улицы.'))])
        self.ai.property_verifier=Verify()
        report=await self.engine.find_optimal_properties(**{**self.kw,'max_candidates':4},include_unverified=True)
        recovered=next(c for c in report.ranked_candidates if c.property_data['external_id']=='unknown')
        self.assertEqual(seen,[None,1]);self.assertEqual(recovered.floor_assessment.status,'eligible')
        self.assertFalse(any(r['code']=='admission_unknown' for r in recovered.confirmation['reasons']))
        self.assertEqual(report.unverified_evaluated_count,0);self.assertTrue(report.comparison_complete)

    async def test_fresh_floor_outside_client_allowed_floors_is_rejected(self):
        class Verify:
            async def verify(self,candidate,*args):
                return dict(status='source_conditions_checked',reasons=[],checks={k:dict(result='pass') for k in CHECKS},
                    sources=[dict(identity_checked=True,fetched_at=time.time(),floor=7)])
        self.ai.property_verifier=Verify()
        report=await self.engine.find_optimal_properties(**self.kw,allowed_floors=(1,))
        self.assertFalse(report.ranked_candidates)
        self.assertEqual(len(report.confirmation_rejected_candidates),3)

    async def test_fresh_floor_does_not_clear_unproven_customer_access(self):
        self.storage.upsert_batch([property_offer('unknown',floor=None,price_month=24000)])
        original=self.engine.floor_profile_resolver.assess_many
        async def assess(properties,*args,**kwargs):
            results=await original(properties,*args,**kwargs)
            for p in properties:
                if p['external_id']=='unknown' and p['floor']==1:
                    results[floor_candidate_key(p)]=FloorAssessment('needs_verification',(1,),
                        'Доступ посетителей не подтверждён','test_independent',missing_facts=['Доступ посетителей'])
            return results
        self.engine.floor_profile_resolver.assess_many=assess
        class Verify:
            async def verify(self,candidate,*args):
                return dict(status='source_conditions_checked',reasons=[],checks={k:dict(result='pass') for k in CHECKS},
                    sources=[dict(identity_checked=True,fetched_at=time.time(),floor=1)])
        self.ai.property_verifier=Verify()
        report=await self.engine.find_optimal_properties(**{**self.kw,'max_candidates':4},include_unverified=True)
        recovered=next(c for c in report.ranked_candidates if c.property_data['external_id']=='unknown')
        self.assertEqual(recovered.floor_assessment.status,'needs_verification')
        self.assertEqual(recovered.confirmation['status'],'requires_confirmation')
        self.assertFalse(report.comparison_complete)

    async def test_source_failure_does_not_invent_additional_client_requirements(self):
        verifier=confirmation_tests.PropertyConfirmationVerifier(confirmation_tests.SemanticAnalyzer(),confirmation_tests.Sources([]))
        verifier._requirements=AsyncMock(return_value=[])
        candidate=SimpleNamespace(property_data=confirmation_tests.prop(),business_model_diagnostics={})
        result=await verifier.verify(candidate,'Концепция',dict(cost_scope='rent_only'))
        self.assertEqual(result['checks']['client_requirements']['result'],'not_applicable')
        self.assertEqual(result['status'],'requires_confirmation')
        self.assertTrue(any(r['code']=='availability' for r in result['reasons']))
        self.assertFalse(any(r['code']=='client_requirements' for r in result['reasons']))

    async def test_inference_failure_preserves_typed_publication_status_but_not_access(self):
        for active in (True,False):
            value=confirmation_tests.source(publication_active=active,
                facts=dict(status='Активно' if active else 'В архиве'))
            ai=confirmation_tests.SemanticAnalyzer(primary=lambda payload:TimeoutError())
            verifier=confirmation_tests.PropertyConfirmationVerifier(ai,confirmation_tests.Sources([value]))
            verifier._requirements=AsyncMock(return_value=[])
            candidate=SimpleNamespace(property_data=confirmation_tests.prop(),business_model_diagnostics={})
            result=await verifier.verify(candidate,'Концепция',dict(cost_scope='rent_only'))
            self.assertEqual(result['checks']['availability']['result'],'pass' if active else 'fail')
            self.assertEqual(result['checks']['client_requirements']['result'],'not_applicable')
            self.assertEqual(result['status'],'requires_confirmation' if active else 'not_suitable')
            self.assertTrue(any(r['code']=='access' for r in result['reasons']))


class ConfirmationStrictModeBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_strict_mode_moves_reassessed_needs_verification_candidate_and_backfills_next(self):
        import copy
        from real_estate_engine.cities import SAINT_PETERSBURG
        from real_estate_engine.confirmation_flow import with_confirmation
        from real_estate_engine.ranking_policy import RankingCriterion

        def make_cand(prop, score):
            return SimpleNamespace(
                property_data=copy.deepcopy(prop),
                competition_report=SimpleNamespace(direct_competitors=[]),
                floor_assessment=FloorAssessment('eligible', (1,), 'Подходит', 'fixture'),
                data_warnings=[],
                verdict='Предварительный вариант',
                confirmation={},
                unrounded_total_score=score,
                input_score_lower_bound=score - 2,
                input_score_upper_bound=score + 2,
                score_upper_bound=score + 2,
                score_coverage=1.0,
                scoring_components={},
                business_model_diagnostics={},
            )

        class Verifier:
            async def verify(self, candidate, concept, parameters):
                pid = candidate.property_data['id']
                if pid == '1':
                    return {
                        'status': 'requires_confirmation',
                        'label': 'Требует подтверждения условий',
                        'checks': {k: {'result': 'pass'} for k in CHECKS},
                        'reasons': [{'code': 'access', 'message': 'Проверить вход'}],
                        'sources': [{'identity_checked': True, 'fetched_at': time.time(), 'facts': {'description': 'Первый этаж, вход со двора'}}],
                    }
                return {
                    'status': 'source_conditions_checked',
                    'label': 'Условия проверены по источникам',
                    'checks': {k: {'result': 'pass'} for k in CHECKS},
                    'reasons': [],
                }

        class FakeFloorResolver:
            async def assess_many(self, props, profile):
                out = {}
                for prop in props:
                    desc = str(prop.get('description') or '').lower()
                    key = floor_candidate_key(prop)
                    out[key] = (
                        FloorAssessment('needs_verification', (1,), 'Вход со двора не подтверждён', 'fixture', missing_facts=['удобный уличный вход'])
                        if 'со двора' in desc else FloorAssessment('eligible', (1,), 'Подходит', 'fixture')
                    )
                return out

        class DummyRecommender:
            city = SAINT_PETERSBURG
            floor_profile_resolver = FakeFloorResolver()
            competitor_analyzer = SimpleNamespace(property_verifier=Verifier())

            @staticmethod
            def _score_and_rank_candidates(items, **kwargs):
                assessments = kwargs.get('floor_assessments') or {}
                ordered = sorted(items, key=lambda pair: 0 if pair[0]['id'] == '1' else 1)
                cands = []
                for prop, _ in ordered:
                    cand = make_cand(prop, 90.0 if prop['id'] == '1' else 80.0)
                    if floor_candidate_key(prop) in assessments:
                        cand.floor_assessment = assessments[floor_candidate_key(prop)]
                    cands.append(cand)
                return cands

            @staticmethod
            def _weight_sensitive_challengers(survivors):
                return []

            @with_confirmation
            async def run(self, include_unverified=False, max_candidates=1, confirmation_timeout_s=5.0, location_query='Василеостровская', single_only=False, **kwargs):
                p1 = dict(id='1', source='avito', title='Первый лот', address='Санкт-Петербург, Линия 10-я, 1', url='https://avito.ru/1', price_month=120000.0, area_sqm=55.0, floor='1', description='1 этаж')
                p2 = dict(id='2', source='avito', title='Второй лот', address='Санкт-Петербург, Линия 10-я, 2', url='https://avito.ru/2', price_month=130000.0, area_sqm=55.0, floor='1', description='Отдельный вход с улицы')
                c1 = make_cand(p1, 90.0)
                pool = [c1] if single_only else [c1, make_cand(p2, 80.0)]
                return SimpleNamespace(
                    ranked_candidates=pool[:max_candidates],
                    _evaluated_candidates=pool,
                    location_query=location_query,
                    client_min_area_sqm=40.0,
                    client_max_area_sqm=75.0,
                    client_max_price_month=180000.0,
                    client_max_price_sqm_year=None,
                    search_radius_m=500.0,
                    center_lat=59.94,
                    center_lng=30.28,
                    ranking_profile=SimpleNamespace(
                        concept='обязателен 1-й этаж, удобный уличный вход',
                        criteria=[RankingCriterion('rent_month', 1.0, 'lower_is_better', 100000.0, 180000.0, 'ставка до 180 тыс', 'r', 'a')],
                        metadata={},
                        unmodeled_requirements=(),
                        status='ready',
                    ),
                    floor_profile=object(),
                    comparable_candidates=[],
                    floor_verification_candidates=[],
                    floor_rejected_candidates=[],
                    missing_inputs=[],
                    ranking_warnings=[],
                    stage_timings_ms={},
                    total_search_time_ms=0.0,
                    _evaluation_complete=True,
                    evaluated_count=len(pool),
                    total_matching_in_db=len(pool),
                    analysis_errors=[],
                    deferred_candidate_bounds=[],
                    confirmation_rejected_candidates=[],
                    top_score_gap=None if single_only else 10.0,
                    input_sensitive_challenger_ids=[],
                    weight_sensitive_challenger_ids=[],
                    model_sensitive_challenger_ids=[],
                    best_candidate=c1,
                )

        rec = DummyRecommender()
        strict = await rec.run(include_unverified=False, max_candidates=1)
        self.assertEqual([c.property_data['id'] for c in strict.ranked_candidates], ['2'])
        self.assertEqual([r.property_data['id'] for r in strict.floor_verification_candidates], ['1'])
        self.assertEqual(strict.unverified_evaluated_count, 0)

        strict_empty = await rec.run(include_unverified=False, max_candidates=1, single_only=True)
        self.assertEqual(strict_empty.ranked_candidates, [])
        self.assertEqual([r.property_data['id'] for r in strict_empty.floor_verification_candidates], ['1'])
        self.assertEqual(strict_empty.selection_status, 'needs_verification')

        lenient = await rec.run(include_unverified=True, max_candidates=1)
        self.assertEqual([c.property_data['id'] for c in lenient.ranked_candidates], ['1'])
        self.assertEqual(lenient.floor_verification_candidates, [])
        self.assertEqual(lenient.unverified_evaluated_count, 1)


if __name__=='__main__':unittest.main()
