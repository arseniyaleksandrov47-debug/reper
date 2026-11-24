from floor_ai_fixtures import scenario_transport_fixture
"""Input-level regressions: collected analytics must change adaptive ranking."""
import copy
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace

from real_estate_engine import ClassifiedCompetitor, CompetitionLevel, OptimalPropertyRecommender
from real_estate_engine.ai_contract import AIContractError
from real_estate_engine.ranking_policy import (RankingProfileResolver,RankingCriterion,RankingProfile,
    evaluate_profile,profile_schema,concept_fragments,bind_concept_references,validate_profile)
from real_estate_engine.market_context import validate_market_context,evaluate_market_context
from real_estate_engine.market_context import validate_audience_usage,compile_audience_usage
from real_estate_engine.concept_evidence import client_clock_window
from floor_ai_fixtures import FixtureFloorAnalyzer
from test_engine_regressions import candidate,competition_report,business_card,LAT,LNG,memory_storage,property_offer


CONCEPT='Клиенты приходят с улицы и из соседних домов; важны окружение, конкуренты и низкая аренда.'


def market_config(concept=CONCEPT, **changes):
    def group(values):
        return {k:dict(weight=v,concept_quote=concept,reason='Контрольный сценарный приоритет') for k,v in values.items()}
    config=dict(audience=group(dict(transit_flow=.4,residents=.3,workers=.2,site_audience=0,synergy=.1)),
        alternatives=group(dict(direct=.8,indirect=.2)),
        strength=group(dict(presence=.2,proximity=.2,reputation=.2,company_revenue=.2,company_profit=.2)),
        references={k:dict(value=v,reason='Условная шкала теста, не рыночное измерение') for k,v in
            dict(residents=1000,workers=500,site_audience=100,synergy=2,company_revenue=10000000).items()},
        competition_scale=dict(value=2,reason='Условный масштаб индекса конкуренции'),
        outside_pressure=dict(value=1,reason='Условное давление внешних альтернатив и отказа от покупки'),
        active_hours=client_clock_window(concept)['active_hours'],access=dict(kind='none',weight=0,reference_minutes=15,
                                               concept_quote=concept,reason='Отдельный маршрут не задан'))
    config.update(changes)
    return validate_market_context(config,concept)


def peer(identifier='direct', level=CompetitionLevel.DIRECT, **changes):
    card=business_card(org_id=identifier,rating=4,ratings_count=50,reviews_count=30,
        fns_inn=identifier,fns_revenue_rub=10000000,fns_net_profit_rub=1000000,fns_reporting_year=2025,**changes)
    return ClassifiedCompetitor(card,level,'Контрольная классификация')


def environment(transit_flow=4000):
    return competition_report(total_cards_found=3,collection_status='ok',
        direct_competitors=[peer()],indirect_competitors=[peer('indirect',CompetitionLevel.INDIRECT)],
        synergy_businesses=[peer('synergy',CompetitionLevel.SYNERGY)],
        population=SimpleNamespace(data_status='ok',estimated_residents_count=1000,estimated_office_workers_count=500,
            buildings_data_available=True,building_coverage_complete=True,malformed_buildings_count=0,data_warnings=[]),
        foot_traffic=SimpleNamespace(data_status='proxy',transport_data_status='complete',
            transport_lower_daily=transit_flow,transport_upper_daily=transit_flow,transport_source_coverage=1,
            local_commercial_daily=10000,traffic_index=90,estimated_daily_pedestrians=14000,
            hourly_profile=SimpleNamespace(weekday_hourly_share=[1/24]*24)))


def integrated_profile(config=None, weights=(.8,.2),concept=CONCEPT):
    return RankingProfile(status='ready',concept=concept,summary='Контроль интеграции',
        criteria=(RankingCriterion('market_fit_index',weights[0],'higher',0,100,concept,'Окружение','Не откалибровано'),
                  RankingCriterion('rent_month',weights[1],'lower',0,200000,concept,'Расходы','Ориентир теста')),
        metadata=dict(market_context=config or market_config(concept)),visit_context_quotes=(concept,))


class InputEffectsTests(unittest.TestCase):
    def score(self, report=None,config=None):
        return evaluate_profile(integrated_profile(config),candidate(),report or environment())

    def test_each_collected_analysis_block_changes_score_in_its_role(self):
        base=self.score()['lower']
        for change, expected in [
            (lambda r:setattr(r.population,'estimated_residents_count',3000),'higher'),
            (lambda r:setattr(r.population,'estimated_office_workers_count',1500),'higher'),
            (lambda r:setattr(r.direct_competitors[0].card,'fns_revenue_rub',100000000),'lower'),
            (lambda r:setattr(r.direct_competitors[0].card,'fns_net_profit_rub',5000000),'lower'),
            (lambda r:setattr(r.direct_competitors[0].card,'rating',5),'lower'),
            (lambda r:r.direct_competitors.append(peer('extra')),'lower'),
            (lambda r:r.indirect_competitors.append(peer('extra',CompetitionLevel.INDIRECT)),'lower'),
            (lambda r:r.synergy_businesses.append(peer('extra',CompetitionLevel.SYNERGY)),'higher'),
            (lambda r:setattr(r.synergy_businesses[0].card,'fns_revenue_rub',100000000),'higher')]:
            report=environment();change(report);actual=self.score(report)['lower']
            with self.subTest(expected=expected,actual=actual):
                self.assertGreater(actual,base) if expected=='higher' else self.assertLess(actual,base)
        self.assertGreater(self.score(environment(transit_flow=9000))['lower'],base)

    def test_old_transport_totals_do_not_supply_a_point_estimate(self):
        report=environment()
        report.foot_traffic=SimpleNamespace(data_status='proxy',metro_contribution_daily=80000,
                                           stops_contribution_daily=20000)
        transit=self.score(report)['business_model']['market_context']['sources']['transit_flow']
        self.assertIsNone(transit['value'])
        self.assertFalse(transit['known'])
        self.assertEqual((transit['index_lower'],transit['index_upper'],transit['evidence_coverage']),(0,1,0))

    def test_indirect_pressure_is_weaker_when_concept_assigns_it_less_importance(self):
        direct=environment();direct.direct_competitors.append(peer('extra'))
        indirect=environment();indirect.indirect_competitors.append(peer('extra',CompetitionLevel.INDIRECT))
        self.assertLess(self.score(direct)['lower'],self.score(indirect)['lower'])

    def test_commercial_proxy_and_separate_metro_distance_do_not_get_repeated_reward(self):
        report=environment();base=self.score(report)
        report.foot_traffic.local_commercial_daily=99999999;report.foot_traffic.traffic_index=100
        report.foot_traffic.nearest_metros=[SimpleNamespace(distance_m=0)]
        self.assertEqual(self.score(report),base)

    def test_duplicate_org_and_duplicate_reactions_do_not_multiply_strength(self):
        report=environment();base=self.score(report)['lower']
        report.direct_competitors.append(copy.deepcopy(report.direct_competitors[0]))
        report.direct_competitors[0].card.reviews_count=50
        self.assertAlmostEqual(self.score(report)['lower'],base)

    def test_accounts_are_allocated_once_per_inn_instead_of_summed_per_outlet(self):
        report=environment();first=self.score(report)['business_model']['market_context']['organizations']['direct']
        duplicate=peer('another');duplicate.card.fns_inn='direct';report.direct_competitors.append(duplicate)
        details=self.score(report)['business_model']['market_context']['organizations']
        for key in ('company_revenue','company_profit'):
            original=first['inputs'][key]['index_lower']
            self.assertAlmostEqual(details['direct']['inputs'][key]['index_lower']+
                                   details['another']['inputs'][key]['index_lower'],original)

    def test_missing_fns_broadens_interval_without_becoming_zero(self):
        complete=self.score();report=environment()
        report.direct_competitors[0].card.fns_revenue_rub=None
        report.direct_competitors[0].card.fns_net_profit_rub=None
        result=self.score(report)
        self.assertLess(result['lower'],complete['lower'])
        self.assertGreater(result['upper'],complete['upper'])
        self.assertLess(result['coverage'],complete['coverage'])
        self.assertFalse(result['business_model']['market_context']['organizations']['direct']['inputs']['company_revenue']['known'])

    def test_optional_fns_for_chains_and_unverified_orgs_preserves_interval_without_missing_spam(self):
        report=environment()
        for attr in ('direct_competitors','indirect_competitors','synergy_businesses'):
            for comp in getattr(report,attr):
                comp.card.fns_inn=None;comp.card.fns_revenue_rub=None;comp.card.fns_net_profit_rub=None
                comp.card.fns_match_status='unverified'
        report.direct_competitors[0].card.is_chain=True
        result=self.score(report)
        mc=result['business_model']['market_context']
        self.assertGreater(result['upper'],result['lower'])
        self.assertAlmostEqual(mc['coverage'],1.0)
        self.assertFalse(any(m.startswith('organization:') and ':company_' in m for m in mc['missing_inputs']))
        self.assertEqual(mc['optional_fns']['applicable_orgs'],0)
        self.assertEqual(mc['optional_fns']['excluded_or_unmatched_orgs'],3)

    def test_conflicting_accounts_for_same_inn_are_unknown(self):
        report=environment();duplicate=peer('another');duplicate.card.fns_inn='direct'
        duplicate.card.fns_revenue_rub=99999999;report.direct_competitors.append(duplicate)
        details=self.score(report)['business_model']['market_context']['organizations']
        for key in ('direct','another'):
            self.assertTrue(details[key]['financial_conflict'])
            self.assertFalse(details[key]['inputs']['company_revenue']['known'])

    def test_unknown_population_is_not_a_known_zero(self):
        report=environment();report.population=None
        result=self.score(report)
        self.assertFalse(result['business_model']['market_context']['sources']['residents']['known'])
        self.assertGreater(result['upper'],result['lower'])

    def test_old_population_totals_without_coverage_are_unknown(self):
        report=environment()
        report.population=SimpleNamespace(data_status='ok',estimated_residents_count=1000,
            estimated_office_workers_count=500,data_warnings=[])
        sources=self.score(report)['business_model']['market_context']['sources']
        for name in ('residents','workers'):
            with self.subTest(source=name):
                source=sources[name]
                self.assertFalse(source['known'])
                self.assertIsNone(source['value'])
                self.assertEqual((source['index_lower'],source['index_upper']),(0,1))

    def test_without_observed_audience_no_competitors_cannot_create_demand(self):
        report=environment(transit_flow=0);report.population.estimated_residents_count=0;report.population.estimated_office_workers_count=0
        report.direct_competitors=[];report.indirect_competitors=[];report.synergy_businesses=[]
        result=self.score(report)
        self.assertEqual(result['business_model']['market_context']['audience_index']['low'],0)
        self.assertLess(result['business_model']['market_context']['choice_factor']['high'],1)

    def test_waived_passing_flow_can_have_zero_weight_without_turning_off_fns(self):
        config=market_config();config['audience']['transit_flow']['weight']=0
        config=validate_market_context(config,CONCEPT)
        base=self.score(config=config)['lower'];report=environment(transit_flow=100000)
        self.assertAlmostEqual(self.score(report,config)['lower'],base)
        report.direct_competitors[0].card.fns_revenue_rub=100000000
        self.assertLess(self.score(report,config)['lower'],base)

    def test_same_places_change_order_when_concept_priorities_change(self):
        street=environment(transit_flow=21000)
        street.population.estimated_residents_count=200
        residential=environment(transit_flow=1500)
        residential.population.estimated_residents_count=10000
        a=market_config();b=market_config()
        for key,value in dict(transit_flow=.9,residents=.05,workers=.025,site_audience=0,synergy=.025).items():a['audience'][key]['weight']=value
        for key,value in dict(transit_flow=.05,residents=.9,workers=.025,site_audience=0,synergy=.025).items():b['audience'][key]['weight']=value
        self.assertGreater(self.score(street,a)['lower'],self.score(residential,a)['lower'])
        self.assertLess(self.score(street,b)['lower'],self.score(residential,b)['lower'])

    def test_stress_changes_primitive_facts_and_explicit_reference_assumptions(self):
        result=self.score()
        self.assertLessEqual(result['input_lower'],result['lower'])
        self.assertGreaterEqual(result['input_upper'],result['upper'])
        self.assertGreater(result['input_upper']-result['input_lower'],0)

    def test_operating_hours_and_verified_route_have_one_defined_role(self):
        timed_concept=CONCEPT+' Работаем с 8 до 11.'
        config=market_config(timed_concept)
        timed=evaluate_profile(integrated_profile(config,concept=timed_concept),candidate(),environment())
        self.assertLess(timed['lower'],self.score()['lower'])
        config=market_config();config['access']=dict(kind='walk',weight=.5,reference_minutes=15,
            concept_quote=CONCEPT,reason='Контроль маршрута')
        prop=candidate(raw_attributes=dict(business_evidence=dict(walk_time_minutes=dict(
            value=5,verified=True,source='Контрольный измеренный маршрут'))))
        short=evaluate_profile(integrated_profile(config),prop,environment())
        prop['raw_attributes']['business_evidence']['walk_time_minutes']['value']=30
        long=evaluate_profile(integrated_profile(config),prop,environment())
        self.assertGreater(short['lower'],long['lower'])

    def test_higher_rent_cannot_improve_score(self):
        p=integrated_profile();cheap=evaluate_profile(p,candidate(price_month=50000),environment())
        expensive=evaluate_profile(p,candidate(price_month=150000),environment())
        self.assertGreater(cheap['lower'],expensive['lower'])

    def test_more_available_audience_mitigates_same_competition(self):
        small=environment();large=environment();large.population.estimated_residents_count=10000
        a=self.score(small)['business_model']['market_context'];b=self.score(large)['business_model']['market_context']
        self.assertGreater(b['choice_factor']['low'],a['choice_factor']['low'])

    def test_refusing_a_financial_or_competitor_block_is_a_contract_error(self):
        for group,key in [('strength','company_revenue'),('strength','company_profit'),('alternatives','indirect')]:
            config=market_config();config[group][key]['weight']=0
            with self.subTest(group=group,key=key),self.assertRaises(AIContractError):validate_market_context(config,CONCEPT)

    def test_independent_client_waiver_overrides_generator_positive_traffic_weight(self):
        concept=CONCEPT+' Случайный поток прохожих не нужен.'
        sources=dict(s0=CONCEPT,s1='Случайный поток прохожих не нужен.')
        raw={key:dict(usage='use',source_id='s0',reason='Источник применим',waiver_quote=None) for key in market_config()['audience']}
        raw['transit_flow']=dict(usage='waived',source_id='s1',reason='Прямой отказ клиента',waiver_quote=sources['s1'])
        raw['site_audience']=dict(usage='not_applicable',source_id='s0',reason='Внешние посетители',waiver_quote=None)
        usage=validate_audience_usage(raw,sources,dict(waivers=[dict(source_id='s1',quote=sources['s1'])]))
        config=market_config(concept);config['audience']['site_audience']['weight']=.1
        compiled,metadata=compile_audience_usage(config,usage)
        self.assertEqual(compiled['audience']['transit_flow']['weight'],0)
        self.assertEqual(compiled['audience']['site_audience']['weight'],0)
        self.assertGreater(compiled['audience']['residents']['weight'],0)
        self.assertTrue(metadata['audience_applicability_enforced'])
        raw['transit_flow']['waiver_quote']=None
        with self.assertRaises(AIContractError):
            validate_audience_usage(raw,sources,dict(waivers=[dict(source_id='s1',quote=sources['s1'])]))
        del raw['transit_flow']['waiver_quote']
        with self.assertRaises(AIContractError):
            validate_audience_usage(raw,sources,dict(waivers=[dict(source_id='s1',quote=sources['s1'])]))

    def test_literal_not_important_is_a_source_specific_waiver(self):
        for sentence in ('Случайный поток прохожих для меня не важен.',
                         'Пешеходная проходимость не важна.'):
            sources=dict(s0=CONCEPT,s1=sentence)
            raw={key:dict(usage='use',source_id='s0',reason='Источник применим',waiver_quote=None) for key in market_config()['audience']}
            raw['transit_flow']=dict(usage='not_applicable',source_id='s1',reason='Прямое исключение клиента',waiver_quote=None)
            raw['site_audience']=dict(usage='not_applicable',source_id='s0',reason='Нет внутренней аудитории',waiver_quote=None)
            usage=validate_audience_usage(raw,sources,dict(waivers=[dict(source_id='s1',quote=sentence)]))
            config=market_config(CONCEPT+' '+sentence);config['audience']['transit_flow']['weight']=0
            compiled,_=compile_audience_usage(config,usage)
            self.assertEqual(compiled['audience']['transit_flow']['weight'],0)
            self.assertGreater(compiled['audience']['residents']['weight'],0)

    def test_not_important_for_one_source_cannot_disable_another(self):
        sources=dict(s0=CONCEPT,s1='Прохожие не важны.')
        raw={key:dict(usage='use',source_id='s0',reason='Источник применим',waiver_quote=None) for key in market_config()['audience']}
        raw['residents']=dict(usage='not_applicable',source_id='s1',reason='Неверное исключение',waiver_quote=None)
        with self.assertRaises(AIContractError):validate_audience_usage(raw,sources,dict(waivers=[]))

    def test_a_waiver_cannot_be_invented_from_a_normal_context_quote(self):
        raw={key:dict(usage='use',source_id='s0',reason='Источник применим',waiver_quote=None) for key in market_config()['audience']}
        raw['transit_flow'].update(usage='waived',waiver_quote=CONCEPT)
        with self.assertRaises(AIContractError):validate_audience_usage(raw,{'s0':CONCEPT},dict(waivers=[]))

    def test_an_unmentioned_catchment_source_cannot_be_silently_disabled(self):
        raw={key:dict(usage='use',source_id='s0',reason='Источник применим',waiver_quote=None) for key in market_config()['audience']}
        raw['workers']=dict(usage='not_applicable',source_id='s0',reason='Работники не упоминаются',waiver_quote=None)
        parsed=validate_audience_usage(raw,{'s0':CONCEPT},dict(waivers=[]))
        self.assertEqual(parsed['workers']['usage'],'use')


class NativeProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_search_scores_accounts_after_fns_enrichment(self):
        environment_requests=[]
        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self,**kw):
                environment_requests.append(kw)
                report=environment()
                for group in (report.direct_competitors,report.indirect_competitors,report.synergy_businesses):
                    for c in group:c.card.fns_inn=None;c.card.fns_revenue_rub=None;c.card.fns_net_profit_rub=None
                return report
        analyzer=Analyzer();calls=[]
        async def enrich(cards):
            calls.append(len(cards))
            for card in cards:
                card.fns_inn=card.org_id;card.fns_revenue_rub=10000000;card.fns_net_profit_rub=1000000
                card.fns_reporting_year=2025
        analyzer.fns_collector=SimpleNamespace(enrich_cards=enrich)
        store,conn=memory_storage();self.addCleanup(conn.close)
        store.upsert_property(property_offer(description='Первый этаж, отдельный вход с улицы.'))
        engine=OptimalPropertyRecommender(store,analyzer)
        async def resolve(*args,**kwargs):
            p=integrated_profile()
            return replace(p,criteria=(p.criteria[0],replace(p.criteria[1],metric='premises_cost_month')))
        engine.ranking_profile_resolver.resolve=resolve
        def cost_fact(v):return dict(value=v,kind='observed',source='Synthetic test input')
        result=await engine.find_optimal_properties('Контроль',center_lat=LAT,center_lng=LNG,
            search_radius_m=500,business_concept=CONCEPT,business_data=dict(properties={'cian:1':dict(premises_cost=dict(
                operating_month=cost_fact(10000),fitout_total=cost_fact(0),amortization_months=cost_fact(12)))}))
        self.assertEqual(calls,[3]);self.assertEqual(result.evaluated_count,1)
        self.assertEqual(environment_requests[0]['business_concept'],CONCEPT)
        best=result.ranked_candidates[0]
        self.assertEqual(best.score_model,'concept_v2_all_data')
        self.assertAlmostEqual(best.scoring_components['premises_cost_month']['utility'],45)
        used=best.business_model_diagnostics['market_context']['organizations']['direct']['inputs']
        self.assertTrue(used['company_revenue']['known']);self.assertGreater(used['company_revenue']['weight'],0)

    async def test_native_generation_compiles_nested_priorities_and_uses_all_blocks(self):
        class Analyzer(FixtureFloorAnalyzer):
            calls=[]
            @staticmethod
            @scenario_transport_fixture
            def _call_scenario_contract_structured_sync(prompt,tokens,schema):
                text,model=FixtureFloorAnalyzer._call_scenario_contract_structured_sync(prompt,tokens,schema)
                raw=json.loads(text);payload=json.loads(prompt.split('\nINPUT_JSON\n',1)[1]);identifier=next(iter(payload['sources']))
                raw['audience_usage']={key:dict(usage='not_applicable' if key=='site_audience' else 'use',
                    source_id=identifier,reason='Контролируемая применимость источника',waiver_quote=None) for key in market_config()['audience']}
                return json.dumps(raw),model
            @classmethod
            def _call_ranking_profile_structured_sync(cls,prompt,tokens,schema):
                payload=json.loads(prompt.split('\nINPUT_JSON\n',1)[1]);cls.calls.append(payload)
                ids=list(payload['concept_fragments']);config=market_config()
                for group in ('audience','alternatives','strength'):
                    for item in config[group].values():item['concept_quote']=ids[0]
                config['access']['concept_quote']=ids[0]
                c=dict(metric='market_fit_index',weight=.8,preference='higher',low=0,high=100,
                       concept_quote=ids[0],reason='Все собранные блоки',assumption='Сценарный индекс')
                rent=dict(c,metric='premises_cost_month',weight=.2,preference='lower')
                response=dict(status='ready',summary='Интегрированный профиль',
                    criteria=dict(audience_access_choice=c,occupancy_cost=rent,physical_fit=None),
                    questions=[],assumptions=['Не откалибровано'],visit_context_quotes=[ids[0]],
                    unmodeled_requirements=[],market_context=config)
                return json.dumps(response,ensure_ascii=False),'native-fixture'
        profile=await RankingProfileResolver(Analyzer()).resolve('Пользовательский бизнес',CONCEPT)
        self.assertEqual(profile.status,'ready')
        self.assertEqual(profile.metadata['market_integration'],'all_collected_blocks_v1')
        self.assertEqual(profile.criteria[0].metric,'market_fit_index')
        ranked=OptimalPropertyRecommender._score_and_rank_candidates(
            [(candidate(),environment())],ranking_profile=profile)
        self.assertIn('market_context',ranked[0].business_model_diagnostics)
        self.assertGreater(ranked[0].business_model_diagnostics['market_context']['organizations']['direct']['inputs']['company_revenue']['weight'],0)

    def test_integrated_schema_cannot_replace_environment_with_one_proxy(self):
        schema=profile_schema(concept_fragments(CONCEPT))
        self.assertEqual(schema['properties']['criteria']['properties']['audience_access_choice']['properties']['metric']['enum'],['market_fit_index'])
        self.assertEqual(schema['properties']['criteria']['properties']['occupancy_cost']['properties']['metric']['enum'],['rent_month','premises_cost_month'])
        self.assertIn('market_context',schema['required'])

    def test_unverified_route_with_known_audience_preserves_nonzero_coverage_and_metric_value(self):
        for kind,weight in (('walk',1.0),('delivery',0.8)):
            with self.subTest(kind=kind,weight=weight):
                cfg=market_config();cfg['audience']['transit_flow']['weight']=0
                cfg['access']=dict(kind=kind,weight=weight,reference_minutes=15 if kind=='walk' else 30,
                    concept_quote=CONCEPT,reason='Контроль покрытия при неизвестном маршруте')
                profile=integrated_profile(cfg)
                missing_route=evaluate_profile(profile,candidate(),environment())
                mc=missing_route['business_model']['market_context']
                self.assertAlmostEqual(mc['access_factor']['low'],1-weight)
                self.assertAlmostEqual(mc['access_factor']['high'],1)
                expected_key='walk_time_minutes' if kind=='walk' else 'delivery_time_minutes'
                self.assertIn(expected_key,mc['missing_inputs'])
                self.assertGreater(mc['coverage'],0)
                self.assertGreater(missing_route['coverage'],0)
                self.assertIsNotNone(missing_route['components']['market_fit_index']['value'])
                verified_evidence={expected_key:dict(value=5,verified=True,source='Synthetic verified route')}
                verified=evaluate_profile(profile,candidate(raw_attributes=dict(business_evidence=verified_evidence)),environment())
                self.assertLess(mc['coverage'],verified['business_model']['market_context']['coverage'])
                # Zero measured audience still yields zero market coverage even with bounded access penalty.
                empty=environment(transit_flow=0);empty.population.estimated_residents_count=0;empty.population.estimated_office_workers_count=0
                empty.direct_competitors=[];empty.indirect_competitors=[];empty.synergy_businesses=[]
                empty.collection_status='partial';empty.population.building_coverage_complete=False
                zero_cov=evaluate_profile(profile,candidate(),empty)
                self.assertEqual(zero_cov['business_model']['market_context']['coverage'],0)

    async def test_profile_prompt_explains_access_weight_and_bumps_cache_revision(self):
        prompts=[]
        class Analyzer:
            @classmethod
            def _call_scenario_contract_structured_sync(cls,prompt,tokens,schema):
                text,model=FixtureFloorAnalyzer._call_scenario_contract_structured_sync(prompt,tokens,schema)
                raw=json.loads(text);payload=json.loads(prompt.split('\nINPUT_JSON\n',1)[1]);identifier=next(iter(payload['sources']))
                raw['audience_usage']={key:dict(usage='not_applicable' if key=='site_audience' else 'use',
                    source_id=identifier,reason='Применимо по тесту',waiver_quote=None) for key in market_config()['audience']}
                return json.dumps(raw),model
            @classmethod
            def _call_ranking_profile_structured_sync(cls,prompt,tokens,schema):
                prompts.append(prompt)
                payload=json.loads(prompt.split('\nINPUT_JSON\n',1)[1]);ids=list(payload['concept_fragments'])
                cfg=market_config()
                for group in ('audience','alternatives','strength'):
                    for item in cfg[group].values():item['concept_quote']=ids[0]
                cfg['access'].update(kind='walk',weight=0.35,concept_quote=ids[0])
                c=dict(metric='market_fit_index',weight=.8,preference='higher',low=0,high=100,
                       concept_quote=ids[0],reason='Индекс',assumption='Сценарий')
                rent=dict(c,metric='rent_month',weight=.2,preference='lower')
                return json.dumps(dict(status='ready',summary='Ок',
                    criteria=dict(audience_access_choice=c,occupancy_cost=rent,physical_fit=None),
                    questions=[],assumptions=[],visit_context_quotes=[ids[0]],
                    unmodeled_requirements=[],market_context=cfg),ensure_ascii=False),'native-fixture'
        resolver=RankingProfileResolver(Analyzer())
        profile=await resolver.resolve('Бизнес',CONCEPT)
        self.assertEqual(profile.status,'ready')
        self.assertIn('0.15–0.5',prompts[0])
        self.assertAlmostEqual(profile.metadata['market_context']['access']['weight'],0.35)


if __name__=='__main__':unittest.main()
