from floor_ai_fixtures import ranking_transport_fixture
import asyncio
import copy
import contextlib
import io
import json
import unittest
from dataclasses import asdict,replace
from types import SimpleNamespace
from unittest.mock import patch

from real_estate_engine import OptimalPropertyRecommender, RankingProfileResolver, RankingCriterion, RankingProfile
from real_estate_engine.ranking_policy import validate_profile,evaluate_profile,metric_value,utility,bind_reference_scales,concept_fragments,profile_schema,bind_concept_references,compile_importances,explicit_area_bands
from real_estate_engine.ai_contract import AIContractError
from floor_ai_fixtures import FixtureFloorAnalyzer,ranking_fixture_response,scenario_transport_fixture
from test_engine_regressions import memory_storage,property_offer,candidate,competition_report,LAT,LNG
from test_remaining_problems import environment


CONCEPT='Кофейня: прохожие заходят с улицы; арендные расходы важны'


def criterion(metric,weight=1,preference='higher',low=0,high=100,quote=CONCEPT):
    return dict(metric=metric,weight=weight,preference=preference,low=low,high=high,
        concept_quote=quote,reason='Контролируемое следствие концепции',assumption='Экспертная шкала, не факт рынка')


def profile(criteria,concept=CONCEPT,visit_quotes=None):
    return validate_profile(dict(status='ready',summary='Контролируемый профиль',criteria=criteria,questions=[],
        assumptions=['Не откалибровано на результатах бизнеса'],visit_context_quotes=visit_quotes or [concept],unmodeled_requirements=[]),concept)


class ContractTests(unittest.TestCase):
    def test_area_band_units_are_literal_data_not_business_rules(self):
        for text,band in [('Площадь 40–150 м².',(40,150)),('Площадь от 12,5 до 48,5 кв. м.',(12.5,48.5)),('50-200 м2',(50,200))]:
            self.assertEqual(explicit_area_bands(text),{band})
        for text in ('Цена 40–150 тыс. руб. за м²','Работаем 8–20 часов','Клиенты 18–60 лет','Бюджет 40–150 тысяч'):
            self.assertEqual(explicit_area_bands(text),set())
        area=profile_schema(concept_fragments('Любой бизнес. Предпочтительна площадь 12,5–48,5 м².'))['properties']['criteria']['properties']['physical_fit']['properties']
        self.assertEqual(area['preference']['enum'],['range'])
        self.assertEqual(area['low']['enum'],[12.5]);self.assertEqual(area['high']['enum'],[48.5])
    def test_native_area_band_cannot_silently_become_higher_is_better(self):
        concept='Предпочтительна площадь 40–150 м².'
        data=dict(status='ready',summary='Контроль диапазона',criteria=[criterion('area_sqm',1,'higher',40,150,concept)],
            questions=[],assumptions=[],visit_context_quotes=[concept],unmodeled_requirements=[])
        metadata=dict(market_integration='all_collected_blocks_v1',scale_origin='ai_placeholders')
        with self.assertRaises(AIContractError):validate_profile(data,concept,metadata=metadata)
        data['criteria'][0]['preference']='range'
        result=validate_profile(data,concept,metadata=metadata)
        self.assertEqual(utility(58,result.criteria[0]),100)
        self.assertEqual(utility(287,result.criteria[0]),0)
    def test_relative_importance_is_normalized_without_changing_its_ratio(self):
        data=ranking_fixture_response(CONCEPT);data['criteria'][0]['weight']=.8;data['criteria'][1]['weight']=.4
        compiled,meta=compile_importances(data)
        result=validate_profile(compiled,CONCEPT)
        self.assertAlmostEqual(sum(c.weight for c in result.criteria),1)
        self.assertAlmostEqual(result.criteria[0].weight/result.criteria[1].weight,2)
        self.assertAlmostEqual(meta['raw_importance_sum'],1.2)
        data['criteria'][0]['weight']=True
        with self.assertRaises(AIContractError):compile_importances(data)

    def test_scoring_only_quotes_are_not_forwarded_as_floor_obligations(self):
        concept='Клиенты заходят с улицы. Важна низкая аренда. Площадь 30–70 м².'
        data=dict(status='ready',summary='Тест',criteria=[
            criterion('passing_traffic_index',.5,quote='Клиенты заходят с улицы.'),
            criterion('rent_month',.3,'lower',quote='Важна низкая аренда.'),
            criterion('area_sqm',.2,'range',30,70,quote='Площадь 30–70 м².')],
            questions=[],assumptions=[],visit_context_quotes=['Клиенты заходят с улицы.','Важна низкая аренда.','Площадь 30–70 м².'],unmodeled_requirements=[])
        compiled,meta=compile_importances(data)
        self.assertEqual(validate_profile(compiled,concept).visit_context_quotes,('Клиенты заходят с улицы.',))
        self.assertEqual(len(meta['removed_nonvisit_quotes']),2)

    def test_wire_can_only_select_source_fragment_ids(self):
        fragments=concept_fragments(CONCEPT)
        schema=profile_schema(fragments)
        group_schema=schema['properties']['criteria']['properties']['audience_access_choice']
        ids=group_schema['properties']['concept_quote']['enum']
        self.assertEqual(ids,list(fragments))
        wire=ranking_fixture_response(CONCEPT)
        for c in wire['criteria']:c['concept_quote']=ids[0]
        wire['criteria']={'audience_access_choice':{**wire['criteria'][0],'metric':'market_fit_index'},
            'occupancy_cost':{**wire['criteria'][1],'metric':'premises_cost_month'},'physical_fit':None}
        wire['visit_context_quotes']=[ids[0]]
        bound=bind_concept_references(wire,fragments)
        self.assertEqual(bound['criteria'][0]['concept_quote'],fragments[ids[0]])
        wire['criteria']['audience_access_choice']['concept_quote']='invented'
        with self.assertRaises(AIContractError):bind_concept_references(wire,fragments)

    def test_native_groups_allow_one_metric_per_signal_and_validate_its_identity(self):
        fragments=concept_fragments(CONCEPT);source_id=next(iter(fragments))
        schema=profile_schema(fragments);groups=schema['properties']['criteria']['properties']
        self.assertEqual(set(groups),{'audience_access_choice','occupancy_cost','physical_fit'})
        self.assertNotIn('net_contribution_month',groups['audience_access_choice']['properties']['metric']['enum'])
        self.assertEqual(['market_fit_index'],groups['audience_access_choice']['properties']['metric']['enum'])
        wire=dict(criteria={k:None for k in groups},visit_context_quotes=[source_id],unmodeled_requirements=[])
        wire['criteria']['occupancy_cost']={**criterion('premises_cost_month'), 'concept_quote':source_id}
        self.assertEqual(bind_concept_references(wire,fragments)['criteria'][0]['metric'],'premises_cost_month')
        wire['criteria']['audience_access_choice']=wire['criteria']['occupancy_cost']
        with self.assertRaises(AIContractError):bind_concept_references(wire,fragments)

    def test_rejects_invalid_weights_unknown_metrics_and_invented_quotes(self):
        for changes in (dict(weight=True),dict(weight=float('nan')),dict(weight=float('inf')),dict(weight=-.1),
                        dict(weight=.8),dict(metric='magic_profit'),dict(concept_quote='Нет такой фразы'),
                        dict(low=100,high=100),dict(high=1000),dict(preference='execute_code')):
            with self.subTest(changes=changes),self.assertRaises((AIContractError,TypeError)):
                profile([{**criterion('passing_traffic_index'),**changes}])

    def test_same_signal_cannot_get_a_second_reward(self):
        for metrics in [('passing_traffic_index','metro_distance_m'),('rent_month','rent_sqm_year'),('residents_count','competitors_per_1000_people')]:
            with self.subTest(metrics=metrics),self.assertRaises(AIContractError):
                profile([criterion(metrics[0],.5),criterion(metrics[1],.5)])

    def test_ambiguous_concept_has_questions_and_no_partial_formula(self):
        data=dict(status='needs_context',summary='Неясен способ посещения',criteria=[],questions=['Клиенты приходят с улицы или по записи?'],assumptions=[],visit_context_quotes=[],unmodeled_requirements=[])
        self.assertEqual(validate_profile(data,'Кофейня').status,'needs_context')
        data['criteria']=[criterion('rent_month')]
        with self.assertRaises(AIContractError):validate_profile(data,CONCEPT)

    def test_unknown_facts_are_not_zero_and_weights_are_not_redistributed(self):
        p=profile([criterion('site_audience_daily',.8,high=1000),criterion('rent_month',.2,'lower',0,200000)])
        result=evaluate_profile(p,candidate(price_month=100000),competition_report())
        self.assertEqual(result['lower'],10);self.assertEqual(result['upper'],90)
        self.assertAlmostEqual(result['coverage'],.2)
        self.assertIsNone(result['components']['site_audience_daily']['value'])

    def test_neighbourhood_workers_do_not_become_internal_audience(self):
        rep=competition_report(population=SimpleNamespace(data_status='ok',estimated_office_workers_count=10000))
        self.assertIsNone(metric_value('site_audience_daily',candidate(),rep))
        prop=candidate(raw_json=json.dumps(dict(business_evidence=dict(site_audience_daily=dict(value=600,verified=True,source='Owner-provided access count')))))
        self.assertEqual(metric_value('site_audience_daily',prop,rep),600)

    def test_zero_competitors_cannot_establish_good_market(self):
        pop=SimpleNamespace(data_status='ok',estimated_residents_count=1000,estimated_office_workers_count=1000,
            buildings_data_available=True,building_coverage_complete=True,malformed_buildings_count=0)
        self.assertIsNone(metric_value('competitors_per_1000_people',candidate(),
            competition_report(population=pop,collection_status='ok')))

    def test_route_is_unknown_until_a_route_source_exists(self):
        self.assertIsNone(metric_value('walk_time_minutes',candidate(),environment()))
        prop=candidate(raw_json=json.dumps(dict(business_evidence=dict(walk_time_minutes=dict(value=3,verified=False,source='AI guess')))))
        self.assertIsNone(metric_value('walk_time_minutes',prop,environment()))

    def test_rent_score_is_independent_of_metro_and_traffic(self):
        p=profile([criterion('rent_month',1,'lower',0,200000)])
        a=evaluate_profile(p,candidate(),environment(10))
        b=evaluate_profile(p,candidate(),environment(100))
        self.assertEqual(a['lower'],b['lower'])

    def test_model_cannot_confuse_ruble_values_with_a_0_to_100_score_scale(self):
        p=profile([criterion('rent_month',1,'lower',0,100)])
        p=replace(p,metadata={'scale_origin':'ai_placeholders'})
        first=candidate(1,price_month=100000);second=candidate(2,price_month=200000)
        bound=bind_reference_scales(p)
        self.assertFalse(bound.metadata['reference_scales']['rent_month']['usable'])
        for prop in (first,second):
            result=evaluate_profile(bound,prop,environment())
            self.assertEqual(result['lower'],0);self.assertEqual(result['upper'],100)
            self.assertEqual(result['coverage'],0)
            self.assertEqual(result['components']['rent_month']['value'],prop['price_month'])

    def test_same_objects_change_order_under_different_concepts(self):
        walk=profile([criterion('passing_traffic_index',.8),criterion('rent_month',.2,'lower',0,200000)])
        booking=profile([criterion('rent_month',1,'lower',0,200000)])
        pairs=[(candidate(1,price_month=150000),environment(100)),(candidate(2,price_month=50000),environment(10))]
        rank=OptimalPropertyRecommender._score_and_rank_candidates
        self.assertEqual(rank(pairs,ranking_profile=walk)[0].property_data['id'],1)
        self.assertEqual(rank(pairs,ranking_profile=booking)[0].property_data['id'],2)

    def test_no_centre_bonus_or_niche_rhythm_when_using_concept(self):
        p=profile([criterion('rent_month',1,'lower',0,200000)])
        rank=OptimalPropertyRecommender._score_and_rank_candidates
        pairs=[(candidate(),environment())]
        a=rank(pairs,ranking_profile=p)[0]
        remote=[(candidate(distance_m=9000),environment())]
        b=rank(remote,ranking_profile=p)[0]
        self.assertEqual(a.total_score,b.total_score)

    def test_input_sensitivity_covers_nonmonotone_area_band(self):
        p=profile([criterion('area_sqm',1,'range',50,60)])
        result=evaluate_profile(p,candidate(area_sqm=45),competition_report())
        self.assertEqual(result['input_upper'],100)
        self.assertLess(result['input_lower'],result['lower'])


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_also_bounds_waiting_for_another_profile(self):
        entered=asyncio.Event();release=asyncio.Event()
        class SlowResolver(RankingProfileResolver):
            async def _generate(self,business,concept,requirements):
                entered.set();await release.wait()
                return validate_profile(ranking_fixture_response(concept),concept)
        resolver=SlowResolver(None)
        first=asyncio.create_task(resolver.resolve('Бизнес',CONCEPT,timeout_s=1))
        await entered.wait()
        second=await asyncio.wait_for(resolver.resolve('Другой бизнес',CONCEPT+' Другая концепция',timeout_s=.01),timeout=.1)
        self.assertEqual(second.analysis_status,'unavailable')
        self.assertEqual(second.metadata['error_type'],'TimeoutError')
        release.set();await first

    async def test_one_profile_for_concurrent_searches_and_cache_copy_isolation(self):
        calls=[]
        def infer(prompt,tokens):
            data=json.loads(prompt.split('\nINPUT_JSON\n')[1]);calls.append(data)
            self.assertNotIn('candidates',data)
            return json.dumps(ranking_fixture_response(data['concept'])),'fixture'
        resolver=RankingProfileResolver(SimpleNamespace(_call_ranking_profile_structured_sync=ranking_transport_fixture(infer),
            _call_scenario_contract_structured_sync=FixtureFloorAnalyzer._call_scenario_contract_structured_sync))
        a,b=await asyncio.gather(resolver.resolve('Кофейня',CONCEPT),resolver.resolve('Кофейня',CONCEPT))
        self.assertEqual(a,b);self.assertEqual(len(calls),1)
        a.metadata['mutated']=True
        self.assertNotIn('mutated',(await resolver.resolve('Кофейня',CONCEPT)).metadata)
        await resolver.resolve('Кофейня',CONCEPT+' Только по записи')
        await resolver.resolve('Кофейня',CONCEPT,dict(max_price_month=100000))
        self.assertEqual(len(calls),3)

    async def test_failure_does_not_cache_or_silently_use_static_weights(self):
        calls=[]
        def infer(prompt,tokens):calls.append(prompt);return '{"broken":true}','fixture'
        resolver=RankingProfileResolver(SimpleNamespace(_call_ranking_profile_structured_sync=ranking_transport_fixture(infer),
            _call_scenario_contract_structured_sync=FixtureFloorAnalyzer._call_scenario_contract_structured_sync))
        result=await resolver.resolve('Кофейня',CONCEPT)
        self.assertNotEqual(result.status,'ready');self.assertEqual(resolver._cache,{})
        resolver.analyzer._call_ranking_profile_structured_sync=ranking_transport_fixture(lambda p,t:(json.dumps(ranking_fixture_response(CONCEPT)),'fixture'))
        self.assertEqual((await resolver.resolve('Кофейня',CONCEPT)).status,'ready')

    async def test_missing_concept_does_not_guess_from_niche_name(self):
        resolver=RankingProfileResolver(None)
        result=await resolver.resolve('Кофейня',None)
        self.assertEqual(result.status,'needs_context');self.assertTrue(result.questions)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store,conn=memory_storage();self.addCleanup(conn.close)
        self.store.upsert_batch([property_offer('one'),property_offer('two',lat=LAT+.001,price_month=120000)])
        self.kw=dict(center_lat=LAT,center_lng=LNG,search_radius_m=1000,business_concept=CONCEPT,max_price_month=200000)

    async def test_same_profile_and_weights_apply_to_every_candidate(self):
        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self,**kw):return environment()
        engine=OptimalPropertyRecommender(self.store,Analyzer())
        report=await engine.find_optimal_properties('Кофейня',**self.kw)
        self.assertEqual(report.ranking_profile.status,'ready');self.assertEqual(report.evaluated_count,2)
        for c in report.ranked_candidates:
            self.assertEqual(c.score_model,'concept_v2_all_data')
            self.assertEqual({k:v['weight'] for k,v in c.scoring_components.items()},{r.metric:r.weight for r in report.ranking_profile.criteria})
        self.assertTrue(report.comparison_complete)
        self.assertFalse(report.data_complete)
        self.assertEqual(report.selection_status,'comparable')
        self.assertFalse(any(item['reason']=='missing_scale' for item in report.missing_inputs))
        json.dumps(asdict(report.ranking_profile),ensure_ascii=False)
        json.dumps([c.scoring_components for c in report.ranked_candidates],ensure_ascii=False)

    async def test_invalid_profile_stops_before_floor_and_environment_requests(self):
        class Analyzer(FixtureFloorAnalyzer):
            @ranking_transport_fixture
            def _call_ranking_profile_structured_sync(self,p,t):return '{}','fixture'
            async def analyze_circle(self,**kw):raise AssertionError('Unvalidated formula reached environment analysis')
        report=await OptimalPropertyRecommender(self.store,Analyzer()).find_optimal_properties('Кофейня',**self.kw)
        self.assertIsNone(report.best_candidate);self.assertEqual(report.ranking_profile.analysis_status,'schema_error')

    async def test_concept_finance_does_not_become_a_floor_requirement(self):
        concept='Клиенты приходят по записи. На аренду хочу тратить поменьше.'
        class Analyzer(FixtureFloorAnalyzer):
            @staticmethod
            @ranking_transport_fixture
            def _call_ranking_profile_structured_sync(prompt,tokens):
                result=ranking_fixture_response(concept);result['visit_context_quotes']=['Клиенты приходят по записи.']
                return json.dumps(result),'fixture'
            async def analyze_circle(self,**kw):return environment()
        kw={**self.kw,'business_concept':concept}
        report=await OptimalPropertyRecommender(self.store,Analyzer()).find_optimal_properties('Студия',**kw)
        self.assertEqual(report.floor_profile.request_clauses,())
        self.assertIn('Клиенты приходят по записи.',report.floor_profile.business_format)

    async def test_missing_factor_retains_uncertainty_and_exposes_comparable_bounds(self):
        class Analyzer(FixtureFloorAnalyzer):
            @staticmethod
            @ranking_transport_fixture
            def _call_ranking_profile_structured_sync(prompt,tokens):
                result=ranking_fixture_response(CONCEPT);result['criteria']=[criterion('site_audience_daily')]
                return json.dumps(result),'fixture'
            async def analyze_circle(self,**kw):return environment()
        report=await OptimalPropertyRecommender(self.store,Analyzer()).find_optimal_properties('Кофейня',**self.kw)
        self.assertTrue(report.comparison_complete)
        self.assertFalse(report.data_complete)
        self.assertTrue(report.input_sensitive_challenger_ids)
        self.assertGreater(report.ranked_candidates[0].score_upper_bound,report.ranked_candidates[0].total_score)
        self.assertLess(report.ranked_candidates[0].score_coverage,1)

    async def test_explicit_unmodeled_concept_prevents_complete_comparison(self):
        concept=CONCEPT+' Нужна парковка.'
        class Analyzer(FixtureFloorAnalyzer):
            @staticmethod
            @ranking_transport_fixture
            def _call_ranking_profile_structured_sync(prompt,tokens):
                result=ranking_fixture_response(concept);result['unmodeled_requirements']=['Нужна парковка.']
                return json.dumps(result),'fixture'
            async def analyze_circle(self,**kw):return environment()
        report=await OptimalPropertyRecommender(self.store,Analyzer()).find_optimal_properties('Кофейня',**{**self.kw,'business_concept':concept})
        self.assertFalse(report.comparison_complete)
        self.assertTrue(any('Нужна парковка.' in w for w in report.ranking_warnings))

    async def test_empty_concept_is_rejected_before_storage_or_network(self):
        with self.assertRaises(ValueError):
            await OptimalPropertyRecommender(None,None).find_optimal_properties('Бизнес',business_concept=' ')


class CLIConceptTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_concept_prompts_before_collection(self):
        import run_engine
        output=io.StringIO()
        with patch('sys.argv',['run_engine.py']),patch.object(run_engine,'RealEstateAggregator',side_effect=AssertionError('Should ask for concept first')),contextlib.redirect_stdout(output):
            await run_engine.main()
        self.assertIn('--concept',output.getvalue())
