"""Regression tests for useful, bounded customer decisions without invented facts."""
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

from real_estate_engine import OptimalPropertyRecommender
from real_estate_engine.business_model import concept_digest
from real_estate_engine.proof_grounding import client_managed_aspect
from real_estate_engine.ranking_policy import evaluate_profile,bind_reference_scales,RankingProfileResolver
from real_estate_engine.request_filters import literal_filter_checks,filters_hold
from real_estate_engine.requirement_coverage import assess_requirement_coverage
from real_estate_engine.scenario_contract import scenario_schema,normalize_scenario_response
from real_estate_engine.property_confirmation import PropertyConfirmationVerifier
from real_estate_engine.floor_policy import FloorAssessment
from floor_ai_fixtures import FixtureFloorAnalyzer
from test_engine_regressions import memory_storage,property_offer,candidate,LAT,LNG
from test_market_context import integrated_profile,environment,CONCEPT
from test_property_confirmation import SemanticAnalyzer,Sources,source,prop


FILTER='Площадь 20–70 м², аренда до 160000 рублей в месяц, в пределах 1500 метров от метро.'
SCOPE=dict(min_area_sqm=20,max_area_sqm=70,max_price_month=160000,search_radius_m=1500,
    center_lat=LAT,center_lng=LNG,location_query='метро Сенная площадь')


class LiteralInputTests(unittest.TestCase):
    def test_applied_numeric_conditions_are_known_but_additional_requirements_are_retained(self):
        checks=literal_filter_checks(FILTER,SCOPE)
        self.assertEqual({c['field'] for c in checks},{'area_sqm','price_month','distance_m'})
        self.assertTrue(filters_hold(checks,[candidate(area_sqm=50,price_month=100000)],SCOPE))
        for quote in (FILTER+' Нужен грузовой подъезд.',FILTER+' Обязательна парковка.',
                      FILTER.replace('от метро','пешком от метро'),FILTER.replace('160000','90000')):
            with self.subTest(quote=quote):self.assertFalse(literal_filter_checks(quote,SCOPE))
        self.assertFalse(filters_hold(checks,[candidate(price_month=170000)],SCOPE))
        self.assertFalse(filters_hold(checks,[candidate(lat=None)],SCOPE))

    def test_comma_decimals_and_thousand_units_have_the_same_literal_bounds(self):
        scope={**SCOPE,'min_area_sqm':20.5}
        quote=FILTER.replace('20–70','20,5–70').replace('160000 рублей','160 тыс. руб.')
        self.assertEqual(literal_filter_checks(quote,scope)[0]['lower'],20.5)
        self.assertEqual(literal_filter_checks(quote,scope)[1]['upper'],160000)

    def test_client_managed_scope_is_general_and_cannot_remove_mixed_obligations(self):
        for text in ('Ремонт оцениваю самостоятельно.','Звукоизоляцию проверю сам.',
                     'Нагрузку на перекрытия рассчитаю лично.'):
            self.assertTrue(client_managed_aspect(text))
        for text in ('Ремонт оцениваю сам, но нужен санузел.','Требуется звукоизоляция; проверю сам.',
                     'Самостоятельный вход обязателен.','Самостоятельно оцените состояние.'):
            self.assertFalse(client_managed_aspect(text))

    def test_coverage_separates_checked_filters_client_responsibility_and_unknown_property(self):
        profile=replace(integrated_profile(),unmodeled_requirements=(FILTER,'Звукоизоляцию проверю сам.','Обязателен грузовой подъезд.'))
        rows=[SimpleNamespace(property_data=candidate(),floor_assessment=None)]
        result=assess_requirement_coverage(profile,rows,SCOPE)
        self.assertEqual([r['status'] for r in result],['checked_by_filters','client_managed','unmodeled'])

    def test_quote_schema_only_offers_unchanged_source_spans(self):
        text='Принимаем по записи, нужен проход до двери, без пропусков.'
        schema=scenario_schema({'s0':text})
        choices=schema['properties']['interpretations']['properties']['s0']['properties']['premises_quote']['enum']
        self.assertIn('нужен проход до двери',choices)
        self.assertTrue(all(v is None or v in text for v in choices))
        self.assertNotIn('нужен проход к двери',choices)
        bad=dict(status='ready',interpretations={'s0':dict(role='premises_requirement',premises_quote='нужен проход к двери')},
            primary_priority=None,priority_rules=[],questions=[])
        with self.assertRaises(ValueError):normalize_scenario_response(bad,{'s0':text})

    def test_pending_declared_condition_is_not_a_missing_business_model(self):
        whole='Первый или второй этаж, нужен разрешённый проход посетителей.'
        part='нужен разрешённый проход посетителей.'
        scenario=dict(sources={'s0':whole},context=[dict(source_id='s0',quote=whole)],
            premises_requirements=[dict(source_id='s0',quote=part)])
        profile=replace(integrated_profile(),unmodeled_requirements=(whole,),
            metadata={**integrated_profile().metadata,'scenario_contract':scenario})
        row=SimpleNamespace(property_data=candidate(),floor_assessment=FloorAssessment('needs_verification',(),
            'Этаж неизвестен','structured',analysis_status='insufficient_facts'))
        result=assess_requirement_coverage(profile,[row])
        self.assertEqual(result[0]['status'],'verification_pending')
        self.assertEqual(row.floor_assessment.status,'needs_verification')

    def test_context_is_known_while_wrongly_labeled_property_demands_stay_unmodeled(self):
        context='Оплату принимаем через интернет.';request='Желательна звукоизоляция.'
        scenario=dict(context=[dict(source_id='s0',quote=context),dict(source_id='s1',quote=request)],premises_requirements=[])
        profile=replace(integrated_profile(),unmodeled_requirements=(context,request),
            metadata={**integrated_profile().metadata,'scenario_contract':scenario})
        row=SimpleNamespace(property_data=candidate(),floor_assessment=None)
        result=assess_requirement_coverage(profile,[row])
        self.assertEqual([r['status'] for r in result],['scenario_context','unmodeled'])


class CostScopeTests(unittest.TestCase):
    def test_known_rent_changes_the_score_without_operating_costs(self):
        profile=replace(integrated_profile(),metadata={**integrated_profile().metadata,'scale_origin':'ai_placeholders',
            'client_constraints':dict(max_price_month=160000)})
        profile=bind_reference_scales(profile)
        before=evaluate_profile(profile,candidate(price_month=100000),environment())
        after=evaluate_profile(profile,candidate(price_month=70000),environment())
        self.assertGreater(after['lower'],before['lower'])
        self.assertGreater(after['upper'],before['upper'])
        self.assertNotIn('cost',after['business_model'])

    def test_auction_start_is_a_lower_bound_and_cannot_look_like_a_cheap_fixed_rent(self):
        profile=integrated_profile();report=environment()
        fixed=candidate(price_month=30000);auction={**fixed,'raw_json':json.dumps(dict(auction_start_year=360000))}
        a=evaluate_profile(profile,fixed,report);b=evaluate_profile(profile,auction,report)
        cost=b['components']['rent_month']
        self.assertIsNone(cost['value_upper']);self.assertIsNone(cost['value'])
        self.assertEqual(cost['contribution_lower'],0)
        self.assertLess(b['lower'],a['lower']);self.assertEqual(b['upper'],a['upper'])


class CustomerFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage,conn=memory_storage();self.addCleanup(conn.close)
        self.storage.upsert_batch([property_offer('cheap',price_month=20000),property_offer('expensive',lat=LAT+.001,price_month=180000)])

    def engine(self,profile,*,forbid_analysis=False):
        calls=[]
        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self,**kwargs):
                if forbid_analysis:raise AssertionError('Missing client references must stop before source work')
                calls.append(kwargs)
                result=environment();result.collection_status='partial';return result
        engine=OptimalPropertyRecommender(self.storage,Analyzer())
        async def resolve(*args,**kwargs):return profile
        engine.ranking_profile_resolver.resolve=resolve
        return engine,calls

    def kwargs(self):
        return dict(center_lat=LAT,center_lng=LNG,search_radius_m=1000,business_concept=CONCEPT,
                    max_price_month=200000,allowed_floors=(1,))

    async def test_missing_facts_do_not_veto_a_leader_that_dominates_their_whole_ranges(self):
        profile=integrated_profile(weights=(.05,.95));engine,calls=self.engine(profile)
        result=await engine.find_optimal_properties('Произвольные услуги',**self.kwargs())
        self.assertEqual(len(calls),2);self.assertTrue(result.missing_inputs)
        self.assertTrue(result.comparison_complete);self.assertFalse(result.data_complete)
        self.assertEqual(result.selection_status,'preliminary_leader')
        self.assertEqual(result.best_candidate.property_data['external_id'],'cheap')
        self.assertTrue(result.recommendation['robust_within_tested_scenarios'])
        self.assertEqual(result.recommendation['status'],'visit_priority')

    async def test_overlapping_unknowns_show_alternatives_instead_of_a_verified_winner(self):
        profile=integrated_profile(weights=(.99,.01));engine,_=self.engine(profile)
        result=await engine.find_optimal_properties('Произвольные услуги',**self.kwargs())
        self.assertEqual(result.selection_status,'comparable');self.assertFalse(result.data_complete)
        self.assertFalse(result.recommendation['robust_within_tested_scenarios'])
        self.assertEqual(len(result.comparable_candidates),2)
        self.assertEqual(result.recommendation['status'],'compare_options')

    async def test_full_cost_reference_is_requested_before_floor_or_environment_and_can_be_supplied(self):
        profile=integrated_profile()
        profile=replace(profile,criteria=(profile.criteria[0],replace(profile.criteria[1],metric='premises_cost_month')),
            metadata={**profile.metadata,'scale_origin':'ai_placeholders','client_constraints':dict(max_price_month=200000)})
        engine,calls=self.engine(profile,forbid_analysis=True)
        first=await engine.find_optimal_properties('Произвольные услуги',**self.kwargs())
        self.assertEqual(first.numerical_matching_count,2);self.assertEqual(first.evaluated_count,0)
        self.assertNotIn('mandatory_checks',first.stage_timings_ms);self.assertFalse(calls)
        self.assertEqual(first.recommendation['status'],'provide_references')
        self.assertEqual(first.next_actions[0]['type'],'provide_references')
        self.assertFalse(first.next_actions[0]['request']['exclude_property_keys'])
        saved=json.loads(json.dumps(first.next_actions))
        self.assertEqual(saved[0]['required_metrics'],['premises_cost_month'])
        # The real continuation reuses the same offers/filters with new client anchors.
        engine.competitor_analyzer.analyze_circle=lambda **kw: self.async_environment()
        refs=dict(concept_sha256=concept_digest(CONCEPT),metrics={'premises_cost_month':dict(low=0,high=250000,source='Explicit test client budget')})
        second=await engine.supply_ranking_references(first,refs)
        self.assertEqual(second.evaluated_count,2)
        self.assertEqual(second.client_max_price_month,200000)
        self.assertFalse(any(g['reason']=='missing_scale' for g in second.missing_inputs))
        self.assertTrue(any(g['reason']=='missing_cost_input' for g in second.missing_inputs))

    async def async_environment(self):return environment()

    async def test_client_unmodeled_mandatory_condition_still_blocks_complete_comparison(self):
        quote='Обязателен подъёмник с платформой нужного размера.'
        concept=CONCEPT+' '+quote;profile=integrated_profile(concept=concept)
        scenario=dict(context=[dict(source_id='s0',quote=CONCEPT)],premises_requirements=[],waivers=[],priority_rules=[])
        engine,_=self.engine(replace(profile,unmodeled_requirements=(quote,),metadata={**profile.metadata,'scenario_contract':scenario}))
        result=await engine.find_optimal_properties('Произвольные услуги',**{**self.kwargs(),'business_concept':concept})
        self.assertFalse(result.comparison_complete);self.assertEqual(result.selection_status,'needs_data')
        self.assertTrue(any(g.get('concept_quote')==quote for g in result.missing_inputs))
        self.assertFalse(result.recommendation['robust_within_tested_scenarios'])
        self.assertIsNone(result.recommendation['primary_candidate_id'])
        self.assertEqual(result.recommendation['status'],'clarify_requirements')

    async def test_rent_only_source_check_does_not_demand_unknown_operating_costs(self):
        verifier=PropertyConfirmationVerifier(SemanticAnalyzer(),Sources([source()]))
        c=SimpleNamespace(property_data=prop(),business_model_diagnostics={})
        rent=await verifier.verify(c,'Приём по записи.',dict(cost_scope='rent_only'))
        total=await verifier.verify(c,'Приём по записи.',dict(cost_scope='total_monthly'))
        self.assertNotIn('cost_unassessed',[r['code'] for r in rent['reasons']])
        self.assertIn('cost_unassessed',[r['code'] for r in total['reasons']])

    async def test_equal_scores_do_not_create_a_priority_from_source_or_id_order(self):
        self.storage.upsert_batch([property_offer('expensive',lat=LAT+.001,price_month=20000)])
        engine,_=self.engine(integrated_profile())
        result=await engine.find_optimal_properties('Произвольные услуги',**self.kwargs())
        self.assertIsNone(result.recommendation['primary_candidate_id'])
        self.assertEqual(result.recommendation['status'],'compare_options')

    async def test_unused_quote_cannot_break_or_disable_an_active_audience_source(self):
        class Analyzer(FixtureFloorAnalyzer):
            @staticmethod
            def _call_scenario_contract_structured_sync(prompt,max_tokens,schema):
                raw,model=FixtureFloorAnalyzer._call_scenario_contract_structured_sync(prompt,max_tokens,schema)
                data=json.loads(raw);sources=json.loads(prompt.split('\nINPUT_JSON\n')[1])['sources']
                data['audience_usage']['residents']['waiver_quote']=next(iter(sources.values()))
                return json.dumps(data),model
        result=await RankingProfileResolver(Analyzer()).resolve('Произвольные услуги',CONCEPT,dict(max_price_month=200000))
        self.assertEqual(result.status,'ready')
        self.assertIn('audience_usage.residents.waiver_quote',result.metadata['ignored_unused_annotations'])
        self.assertEqual(result.metadata['scenario_contract']['audience_usage']['residents']['usage'],'use')
        self.assertGreater(result.metadata['market_context']['audience']['residents']['weight'],0)

    async def test_unbounded_final_rent_is_pending_under_a_hard_budget_and_stays_bounded_in_optional_ranking(self):
        self.storage.upsert_batch([property_offer('cheap',price_month=20000,raw_attributes={'auction_start_year':240000})])
        engine,_=self.engine(integrated_profile())
        first=await engine.find_optimal_properties('Произвольные услуги',**self.kwargs())
        self.assertEqual([c.property_data['external_id'] for c in first.ranked_candidates],['expensive'])
        self.assertEqual(len(first.floor_verification_candidates),1)
        self.assertEqual(first.floor_verification_candidates[0].property_data['external_id'],'cheap')
        self.assertTrue(any(a['type']=='continue_unverified' for a in first.next_actions))
        second=await engine.continue_unverified_analysis(first)
        auction=next(c for c in second.ranked_candidates if c.property_data['external_id']=='cheap')
        self.assertEqual(auction.floor_assessment.status,'needs_verification')
        self.assertIsNone(auction.scoring_components['rent_month']['value_upper'])
        self.assertFalse(second.comparison_complete)

    async def test_irrelevant_unknown_rival_does_not_veto_a_known_leader(self):
        self.storage.upsert_batch([property_offer('expensive',lat=LAT+.001,price_month=180000,floor=None)])
        engine,calls=self.engine(integrated_profile(weights=(.05,.95)))
        result=await engine.find_optimal_properties('Произвольные услуги',**self.kwargs())
        self.assertEqual(len(calls),1);self.assertEqual(len(result.floor_verification_candidates),1)
        self.assertEqual(result.floor_verification_candidates[0].assessment.status,'needs_verification')
        self.assertTrue(result.deferred_candidate_bounds[0]['dominated'])
        self.assertTrue(result.comparison_complete);self.assertFalse(result.data_complete)
        self.assertEqual(result.selection_status,'preliminary_leader')
        self.assertEqual(result.recommendation['status'],'visit_priority')

    async def test_a_potentially_better_unknown_rival_still_prevents_a_complete_choice(self):
        self.storage.upsert_batch([property_offer('expensive',lat=LAT+.001,price_month=180000,floor=None)])
        engine,_=self.engine(integrated_profile(weights=(.8,.2)))
        result=await engine.find_optimal_properties('Произвольные услуги',**self.kwargs())
        self.assertFalse(result.deferred_candidate_bounds[0]['dominated'])
        self.assertFalse(result.comparison_complete)
        self.assertFalse(result.recommendation['robust_within_tested_scenarios'])


if __name__=='__main__':unittest.main()
