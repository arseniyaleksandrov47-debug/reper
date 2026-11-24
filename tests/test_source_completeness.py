"""Data loss, independent source failures and conservative partial-market bounds."""
import asyncio
import copy
import json
import random
import unittest
from dataclasses import asdict
from datetime import datetime
from types import SimpleNamespace

from real_estate_engine import ClassifiedCompetitor,CompetitionLevel
from real_estate_engine.business_model import premises_cost
from real_estate_engine.collectors import AvitoCollector,CianCollector,DomclickCollector,InvestMoscowCollector,YandexRealtyCollector
from real_estate_engine.competitors import NvidiaCompetitorAnalyzer,MapCardsResult,YandexMapsRadiusCollector,merge_map_card
from real_estate_engine.competitor_classification import ConceptCompetitorClassifier
from real_estate_engine.ai_contract import AITextResponse
from real_estate_engine.market_context import evaluate_market_context,validate_market_context
from real_estate_engine.population import BuildingFetchResult,population_counts_available
from real_estate_engine.ranking_policy import metric_value
from test_business_model import fact
from test_collector_floors import collector_with_response
from test_engine_regressions import LAT,LNG,business_card,candidate,population_analyzer,memory_storage
from test_market_context import environment,market_config,CONCEPT,peer
from test_remaining_problems import building


class SourceRetentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_five_collectors_preserve_full_received_fields_through_sqlite(self):
        common=dict(description='Полное описание '+('x'*800),unmodeledFinancialTerms=dict(currency='RUB',amount=12345),
                    accessRules=dict(text='Объявленные ограничения доступа'))
        ci=dict(common,id='1',geo=dict(coordinates=dict(lat=LAT,lng=LNG)),totalArea=50,
                bargainTerms=dict(priceRur=100000,utilitiesPrice=12345),floorNumber=1)
        ya=dict(common,offerId='2',location=dict(point=dict(latitude=LAT,longitude=LNG)),area=dict(value=50),price=dict(value=100000),floorsOffered=[1])
        dm=dict(common,id='3',location=dict(lat=LAT,lon=LNG),objectInfo=dict(area=50,floor=1),price=100000)
        av=dict(common,id='4',coords=dict(lat=LAT,lng=LNG),title='Помещение 50 м²',priceDetailed=dict(value=100000),floor=1)
        inv=dict(common,id='5',name='Помещение в аренду',coords=[LAT,LNG],objectArea=50,startPrice=1200000,floor=1,
            typeCode='nsi:tender_type_portal:12',objectTypeCode='nsi:41:30011569',requestEndDate='2099-01-01T12:00:00Z',
            address='Москва, Тестовая улица, 1')
        cases=[(CianCollector,ci,dict(data=dict(offersSerialized=[ci])),None),
            (YandexRealtyCollector,ya,None,'<script>window.INITIAL_STATE = '+json.dumps(dict(search=dict(offers=dict(entities=[ya]))))+';</script>'),
            (DomclickCollector,dm,dict(result=dict(items=[dm])),None),
            (AvitoCollector,av,None,'<script>'+json.dumps(dict(loaderData=dict(data=dict(catalog=dict(items=[av]))),padding='x'*100000))+'</script>'),
            (InvestMoscowCollector,inv,dict(entities=[dict(tenders=[inv])]),None)]
        store,conn=memory_storage()
        try:
            for cls,item,payload,text in cases:
                with self.subTest(source=cls.__name__):
                    offers=await collector_with_response(cls,payload,text).collect(1)
                    self.assertEqual(len(offers),1);offer=offers[0]
                    self.assertEqual(offer.raw_attributes['source_payload'],item)
                    self.assertIn(item['description'],offer.description)
                    datetime.fromisoformat(offer.raw_attributes['source_fetched_at'])
                    store.upsert_property(offer)
                    # Query by identity: all examples share a spatial dedup key.
                    with store._get_connection() as c:
                        raw=c.execute('SELECT raw_json FROM properties WHERE source=? AND external_id=?',
                            (offer.source.value,offer.external_id)).fetchone()[0]
                    self.assertEqual(json.loads(raw)['source_payload'],item)
        finally:conn.close()

    async def test_unknown_organization_keeps_full_card_and_is_never_unrelated(self):
        cards=[business_card(org_id=str(i),features=['Длинный дополнительный признак'],websites=['https://example.test/']) for i in range(3)]
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer);analyzer.model='fixture'
        async def maps(**kwargs):return MapCardsResult(cards,status='ok')
        analyzer.map_collector=SimpleNamespace(collect_in_circle=maps)
        analyzer.population_analyzer=population_analyzer()
        def infer(prompt,tokens,**kwargs):
            data=json.loads(prompt.split('\nINPUT_JSON\n',1)[1]);rows=[]
            for o in data['organizations']:
                unknown=o['org_id']=='1'
                rows.append(dict(idx=o['idx'],same_purchase_offer='unknown' if unknown else 'no',
                    need_relation='unknown' if unknown else 'unrelated',relationship_basis='unknown' if unknown else 'unrelated',
                    audience_relation='overlap',scenario_access='compatible',evidence_fields=['categories'],offer_evidence_ids=[],reason='Контроль'))
            return AITextResponse(json.dumps(dict(categories=rows,summary='Контроль')),'fixture',{})
        analyzer._call_nvidia_sync=infer
        report=await analyzer.analyze_circle(LAT,LNG,400,'Тест',business_concept='Точная концепция',enrich_fns=False)
        self.assertEqual(report.collection_status,'ok');self.assertEqual(report.data_status,'partial')
        self.assertEqual([c.org_id for c in report.unclassified_businesses],['1'])
        self.assertEqual(report.unclassified_businesses[0].websites,['https://example.test/'])
        self.assertEqual(len(report.unrelated_businesses),2)
        self.assertEqual(sum(len(getattr(report,k)) for k in ('unrelated_businesses','unclassified_businesses')),report.total_cards_found)
        json.dumps(asdict(report),ensure_ascii=False,allow_nan=False)

    async def test_empty_maps_do_not_prevent_independent_population_analysis(self):
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer);analyzer.model='fixture'
        async def maps(**kwargs):return MapCardsResult(status='unavailable',warnings=['Controlled outage'])
        analyzer.map_collector=SimpleNamespace(collect_in_circle=maps)
        pop=population_analyzer();pop._fetch_2gis_buildings_sync=lambda *args:BuildingFetchResult([building()],coverage_complete=True)
        analyzer.population_analyzer=pop
        report=await analyzer.analyze_circle(LAT,LNG,400,'Тест',enrich_fns=False,business_concept='Приём по записи.')
        self.assertEqual(report.collection_status,'unavailable');self.assertEqual(report.total_cards_found,0)
        self.assertGreater(report.population.estimated_residents_count,0)
        self.assertTrue(population_counts_available(report.population))

    async def test_cards_without_collection_status_remain_unverified(self):
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer);analyzer.model='fixture'
        async def maps(**kwargs):return [business_card()]
        analyzer.map_collector=SimpleNamespace(collect_in_circle=maps)
        analyzer.population_analyzer=population_analyzer()
        report=await analyzer.analyze_circle(LAT,LNG,400,'Тест',enrich_fns=False,business_concept='Приём по записи.')
        self.assertEqual(report.total_cards_found,1)
        self.assertEqual(report.collection_status,'unknown')
        self.assertIn('Полнота сбора карточек организаций не подтверждена',report.data_warnings)


class PopulationCompletenessTests(unittest.TestCase):
    def population(self,complete=True,prices=((),())):
        analyzer=population_analyzer(prices=prices)
        analyzer._fetch_2gis_buildings_sync=lambda *args:BuildingFetchResult([building()],coverage_complete=complete)
        return analyzer.analyze_circle_sync(LAT,LNG)

    def test_known_population_is_used_even_when_only_prices_fail(self):
        pop=self.population();self.assertEqual(pop.data_status,'partial')
        report=environment();report.population=pop
        _,details=evaluate_market_context(market_config(),candidate(),report)
        self.assertTrue(details['sources']['residents']['known']);self.assertTrue(details['sources']['workers']['known'])
        self.assertEqual(metric_value('residents_count',candidate(),report),201)
        self.assertNotIn('residents',details['missing_inputs'])

    def test_incomplete_building_coverage_does_not_become_an_exact_population_count(self):
        pop=self.population(False,([500000],[25]));report=environment();report.population=pop
        _,details=evaluate_market_context(market_config(),candidate(),report)
        self.assertFalse(details['sources']['residents']['known'])
        self.assertIsNone(metric_value('residents_count',candidate(),report))

    def test_housing_prices_alone_do_not_manufacture_population(self):
        pop=population_analyzer(prices=([500000],[25])).analyze_circle_sync(LAT,LNG)
        report=environment();report.population=pop
        self.assertFalse(population_counts_available(pop))
        self.assertIsNone(metric_value('office_workers_count',candidate(),report))


class MarketCompletenessTests(unittest.TestCase):
    def test_old_report_status_does_not_confirm_organization_coverage(self):
        report=environment()
        self.assertIsNotNone(metric_value('competitors_per_1000_people',candidate(),report))
        report.collection_status='unknown'
        _,details=evaluate_market_context(market_config(),candidate(),report)
        self.assertFalse(details['organization_inventory']['collection_complete'])
        self.assertFalse(details['sources']['synergy']['known'])
        self.assertEqual(details['choice_factor']['low'],0)
        self.assertLess(details['choice_factor']['high'],1)
        self.assertIn('organization_inventory',details['missing_inputs'])
        self.assertIsNone(metric_value('competitors_per_1000_people',candidate(),report))

    def test_partial_bounds_include_shared_entity_and_conflicting_accounts(self):
        for conflicting in (False,True):
            partial=environment();partial.collection_status='ok';partial.data_status='partial';partial.total_cards_found=4
            card=copy.deepcopy(partial.direct_competitors[0].card);card.org_id='unclassified'
            if conflicting:card.fns_revenue_rub*=3
            partial.unclassified_businesses=[card]
            bounds,_=evaluate_market_context(market_config(),candidate(),partial)
            for relation,attribute in [(CompetitionLevel.DIRECT,'direct_competitors'),(CompetitionLevel.INDIRECT,'indirect_competitors'),
                                       (CompetitionLevel.SYNERGY,'synergy_businesses'),(CompetitionLevel.UNRELATED,'unrelated_businesses')]:
                full=copy.deepcopy(partial);full.data_status='ok';full.unclassified_businesses=[]
                getattr(full,attribute).append(ClassifiedCompetitor(card,relation,'Контроль общей отчётности'))
                resolved,_=evaluate_market_context(market_config(),candidate(),full)
                with self.subTest(conflicting=conflicting,relation=relation):
                    self.assertGreaterEqual(resolved.low,bounds.low-1e-9);self.assertLessEqual(resolved.high,bounds.high+1e-9)

    def test_known_alternatives_continue_to_limit_choice_with_unknown_cards(self):
        report=environment();report.collection_status='ok';report.data_status='partial';report.total_cards_found=4
        report.unclassified_businesses=[business_card(org_id='unknown')]
        interval,details=evaluate_market_context(market_config(),candidate(),report)
        self.assertGreater(details['choice_factor']['low'],0)
        self.assertLess(details['choice_factor']['high'],1)
        self.assertEqual(details['organization_inventory']['unclassified_count'],1)
        self.assertIn('organization_classification',details['missing_inputs'])
        self.assertGreater(details['coverage'],0)

    def test_failed_collection_keeps_known_competitor_but_unbounded_other_supply(self):
        report=environment();report.collection_status='partial';report.data_status='partial'
        _,details=evaluate_market_context(market_config(),candidate(),report)
        self.assertEqual(details['choice_factor']['low'],0)
        self.assertLess(details['choice_factor']['high'],1)
        self.assertIn('organization_inventory',details['missing_inputs'])
        self.assertGreater(details['sources']['synergy']['index_lower'],0)

    def test_partial_bounds_contain_every_possible_assignment_without_niche_constants(self):
        rng=random.Random(8417)
        for i in range(15):
            config=market_config()
            for group in ('strength','audience','alternatives'):
                for item in config[group].values():item['weight']=rng.uniform(.01,1)
            config=validate_market_context(config,CONCEPT)
            for stress in (0,.1):
                partial=environment();partial.collection_status='ok';partial.data_status='partial';partial.total_cards_found=4
                card=peer('unknown').card;partial.unclassified_businesses=[card]
                bounds,_=evaluate_market_context(config,candidate(),partial,stress=stress)
                for relation,attribute in [(CompetitionLevel.DIRECT,'direct_competitors'),(CompetitionLevel.INDIRECT,'indirect_competitors'),
                                           (CompetitionLevel.SYNERGY,'synergy_businesses'),(CompetitionLevel.UNRELATED,'unrelated_businesses')]:
                    complete=copy.deepcopy(partial);complete.data_status='ok';complete.unclassified_businesses=[]
                    getattr(complete,attribute).append(ClassifiedCompetitor(card,relation,'Контроль полного исхода'))
                    resolved,_=evaluate_market_context(config,candidate(),complete,stress=stress)
                    with self.subTest(iteration=i,stress=stress,relation=relation):
                        self.assertGreaterEqual(resolved.low,bounds.low-1e-9);self.assertLessEqual(resolved.high,bounds.high+1e-9)


class CardMergeTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_query_keeps_secondary_offer_and_legal_website(self):
        collector=YandexMapsRadiusCollector.__new__(YandexMapsRadiusCollector)
        collector._build_queries_for_business=lambda *args,**kwargs:['one','two']
        def fetch(query,*args):
            return [dict(id='a',title='Организация',coordinates=[LNG,LAT],categories=[dict(name='Услуга')],
                features=[] if query=='one' else [dict(name='Дополнительное предложение',value=True)],
                urls=[] if query=='one' else ['https://example.test/legal'])]
        collector._fetch_query_sync=fetch
        cards=await collector.collect_in_circle(LAT,LNG,400,'Тест')
        self.assertEqual(len(cards),1);self.assertEqual(cards[0].websites,['https://example.test/legal'])
        self.assertIn('Дополнительное предложение: True',cards[0].features)

    async def test_conflicting_offer_is_not_positive_evidence_and_verifier_sees_conflict(self):
        card=business_card(org_id='a',features=['Предложение: True'])
        merge_map_card(card,business_card(org_id='a',features=['Предложение: False']))
        facts=ConceptCompetitorClassifier.facts(card)
        self.assertIsNone(facts['feature_flags']['Предложение'])
        self.assertFalse(any(v['quote']=='Предложение: True' for v in facts['offer_evidence_catalog'].values()))
        calls=[]
        def infer(prompt,tokens,**kwargs):
            return AITextResponse(json.dumps(dict(categories=[dict(idx=1,same_purchase_offer='yes',need_relation='same_purchase',
                relationship_basis='specific_purchase',audience_relation='overlap',scenario_access='compatible',evidence_fields=['categories'],
                offer_evidence_ids=['category:0'],reason='Контроль')],summary='Контроль')),'primary',{})
        def verify(prompt,tokens,**kwargs):
            data=json.loads(prompt.split('\nINPUT_JSON\n',1)[1]);calls.append(data)
            return AITextResponse(json.dumps({'1':dict(verdict='unknown',reason='Противоречивые исходные данные')}),'proof',{})
        classifier=ConceptCompetitorClassifier()
        result=await classifier.classify([card],'Тест',CONCEPT,'primary',infer,verify=verify,verifier_model='proof')
        self.assertEqual(result[0]['categories'][0]['level'],'unknown');self.assertFalse(classifier.cache)
        claim=calls[0]['claims']['1'];self.assertEqual(claim['source_conflicts'],{'feature:Предложение':[False,True]})
        self.assertNotIn('title',claim);self.assertNotIn('address',claim)


class CostCompletenessTests(unittest.TestCase):
    def test_missing_preparation_is_out_of_scope_not_a_free_estimate(self):
        value,details=premises_cost(candidate(),{})
        self.assertEqual({g['field'] for g in details['missing_inputs']},{'operating_month'})
        self.assertEqual(details['preparation_scope'],'tenant_managed')
        self.assertNotIn('fitout_month',details['parts'])
        self.assertEqual(set(details['excluded_components']),{'fitout_total','amortization_months'})
        self.assertIsNone(value.high);self.assertEqual(value.low,100000)

    def test_explicit_zero_fitout_needs_no_invented_amortization_period(self):
        value,details=premises_cost(candidate(),dict(premises_cost=dict(operating_month=fact(10000),fitout_total=fact(0))))
        self.assertEqual(value.low,110000);self.assertEqual(value.high,110000);self.assertFalse(details['missing_inputs'])

    def test_missing_or_unattributed_zero_is_not_treated_as_free_preparation(self):
        for fitout in ({'value':0},None):
            _,details=premises_cost(candidate(),dict(premises_cost=dict(operating_month=fact(10000),fitout_total=fitout)))
            self.assertIsNone(details['parts']['fitout_month']['high'])
            self.assertIn('fitout_total',{g['field'] for g in details['missing_inputs']})

    def test_full_sourced_cost_changes_with_supplied_amounts(self):
        evidence=dict(premises_cost=dict(operating_month=fact(10000),fitout_total=fact(120000),amortization_months=fact(12)))
        value,details=premises_cost(candidate(),evidence)
        self.assertEqual(value.low,120000);self.assertFalse(details['missing_inputs'])
        evidence['premises_cost']['operating_month']=fact(20000)
        self.assertEqual(premises_cost(candidate(),evidence)[0].low,130000)

    def test_unrepresentable_fitout_cost_remains_a_gap_without_crashing_rating(self):
        evidence=dict(premises_cost=dict(operating_month=fact(0),fitout_total=fact(1e308),amortization_months=fact(1e-308)))
        value,details=premises_cost(candidate(),evidence)
        self.assertIsNone(value.high);self.assertIn('fitout_month',{g['field'] for g in details['missing_inputs']})
