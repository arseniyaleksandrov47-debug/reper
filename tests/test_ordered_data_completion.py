import asyncio
import copy
import json
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch
import threading

from real_estate_engine import NvidiaCompetitorAnalyzer,CompetitionLevel,YandexMapsRadiusCollector
from real_estate_engine.competitors import MapCardsResult
from real_estate_engine.environment_cache import EnvironmentSourceCache
from real_estate_engine.organization_details import OrganizationDetailCompleter,exact_organization
from real_estate_engine.metro_data import MetroFlowDataset
from real_estate_engine.mos_metro_source import build_metro_dataset
from real_estate_engine.traffic import FootTrafficEstimator
from real_estate_engine.market_context import _transport_audience
from test_automatic_access import observation,metadata
from test_engine_regressions import business_card,LAT,LNG
import test_fns_acceptance as fns_cases
from test_fns_acceptance import INN
from real_estate_engine.legal_entity import WebsiteLegalResolver,canonical_business_url


def item(identifier='1',**changes):
    value=dict(id=identifier,title='Услуги',address='Москва',coordinates=[LNG,LAT],categories=[dict(name='Ремонт приборов')],
               features=[dict(id='street_entrance',value=True),dict(name='Услуга',value=[dict(name='По записи')])])
    value.update(changes);return value


def body(values):return '<script type="application/json" class="state-view">'+json.dumps(dict(items=values),ensure_ascii=False)+'</script>'


class DetailParsingTests(unittest.TestCase):
    def test_matching_brand_or_nearby_card_is_not_an_exact_id(self):
        with self.assertRaises(ValueError):exact_organization(body([item('2')]),'1')
        with self.assertRaises(ValueError):exact_organization(body([item(),item(title='Другой объект')]),'1')
        self.assertEqual(exact_organization(body([item('2'),item()]),'1')['id'],'1')

    def test_identical_nested_copy_is_one_card_not_ambiguity(self):
        value=item();self.assertEqual(exact_organization(body([value,copy.deepcopy(value)]),'1'),value)

    def test_feature_identifier_and_enum_values_are_preserved(self):
        collector=YandexMapsRadiusCollector.__new__(YandexMapsRadiusCollector)
        card=collector._parse_card(item(),LAT,LNG,200)
        self.assertIn('street_entrance: True',card.features)
        self.assertIn('Услуга: По записи',card.features)


class DetailCompletionTests(unittest.IsolatedAsyncioTestCase):
    def setup_provider(self,items,limit=8):
        calls=[];collector=YandexMapsRadiusCollector.__new__(YandexMapsRadiusCollector)
        collector.source_cache=EnvironmentSourceCache(None)
        provider=OrganizationDetailCompleter(collector,limit=limit)
        def fetch(identifier):calls.append(identifier);return copy.deepcopy(items[identifier])
        provider.fetch=fetch
        return provider,calls

    async def test_details_add_evidence_histogram_and_source_without_dropping_old_facts(self):
        provider,calls=self.setup_provider({'1':item(histogramData={'mon':[1]*24})})
        card=business_card(features=['Старый признак'])
        first=await provider.complete([card],LAT,LNG,200)
        self.assertIn('Старый признак',card.features);self.assertIn('street_entrance: True',card.features)
        self.assertEqual(card.histogram_data,{'mon':[1]*24});self.assertEqual(first['changed_ids'],['1'])
        second=await provider.complete([card],LAT,LNG,200)
        self.assertEqual(calls,['1']);self.assertEqual(second['changed_ids'],[])
        self.assertEqual(second['records']['1']['cache_status'],'memory')

    async def test_conflicting_positive_and_negative_source_flags_remain_conflicting(self):
        provider,_=self.setup_provider({'1':item(features=[dict(name='Услуга по записи',value=False)])})
        card=business_card(features=['Услуга по записи: True'])
        await provider.complete([card],LAT,LNG,200)
        self.assertIn('feature:Услуга по записи',card.source_conflicts)

    async def test_distant_same_id_or_other_id_cannot_enrich_existing_point(self):
        for detail in (item('2'),item(coordinates=[LNG+.05,LAT])):
            provider,_=self.setup_provider({'1':detail});card=business_card(features=['Исходный факт'])
            before=copy.deepcopy(card);result=await provider.complete([card],LAT,LNG,200)
            self.assertEqual(result['checked'],0);self.assertEqual(card,before)

    async def test_limit_is_explicit_and_source_failure_does_not_invent_missing_facts(self):
        provider,calls=self.setup_provider({'1':item()},limit=1)
        cards=[business_card(org_id='1'),business_card(org_id='2')]
        result=await provider.complete(cards,LAT,LNG,200)
        self.assertEqual(result['deferred_ids'],['2']);self.assertEqual(calls,['1'])
        def fail(*a):raise TimeoutError('Controlled outage')
        provider.fetch=fail;provider.collector.source_cache=EnvironmentSourceCache(None)
        original=copy.deepcopy(cards[0]);result=await provider.complete(cards[:1],LAT,LNG,200)
        self.assertEqual(result['checked'],0);self.assertEqual(cards[0],original)

    async def test_only_changed_unknown_organizations_are_reclassified_with_original_indexes(self):
        cards=[business_card(org_id='1'),business_card(org_id='2')];calls=[]
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.model='legacy';analyzer.classification_model='primary';analyzer.classification_proof_model='proof'
        async def maps(**kwargs):return MapCardsResult(cards,status='ok')
        async def population(**kwargs):return None
        analyzer.map_collector=SimpleNamespace(collect_in_circle=maps)
        analyzer.population_analyzer=SimpleNamespace(analyze_circle=population)
        async def complete(items,*args,**kwargs):
            self.assertEqual([c.org_id for c in items],['1']);items[0].features.append('Новая точная услуга')
            return dict(changed_ids=['1'],checked=1)
        analyzer.organization_detail_completer=SimpleNamespace(complete=complete)
        async def classify(self,items,*args,**kwargs):
            calls.append([c.org_id for c in items]);second=len(calls)>1
            return (dict(categories=[dict(idx=i+1,level='direct' if second else 'unknown' if c.org_id=='1' else 'unrelated',reason='Контроль') for i,c in enumerate(items)],summary='Контроль'),
                    'primary',dict(organization_decisions={c.org_id:dict(idx=i+1) for i,c in enumerate(items)},unknown_count=0 if second else 1))
        with patch('real_estate_engine.competitor_classification.ConceptCompetitorClassifier.classify',classify):
            report=await analyzer.analyze_circle(LAT,LNG,400,'Услуги',business_concept='Клиенты покупают услугу',enrich_fns=False)
        self.assertEqual(len(calls),2);self.assertEqual(calls[1],['1'])
        self.assertEqual([c.card.org_id for c in report.direct_competitors],['1'])
        self.assertEqual([c.card.org_id for c in report.unrelated_businesses],['2'])
        self.assertEqual(report.ai_response_metadata['unknown_count'],0)


class MetroIntervalTests(unittest.TestCase):
    def dataset(self):
        data,info=build_metro_dataset(metadata(2),[observation(name='Узел',incoming=92000,line='Линия А'),
            observation(2,'Узел',incoming=184000,line='Линия Б')],today=date(2026,11,4))
        return MetroFlowDataset(data,today=date(2026,11,4)),data,info

    def test_multi_line_name_preserves_bounds_without_sum_or_point_estimate(self):
        dataset,data,info=self.dataset()
        self.assertIsNone(dataset.lookup('Узел',LAT,LNG))
        self.assertEqual(dataset.lookup_interval('Узел',LAT,LNG),(1000,2000))
        self.assertEqual(data['stations'],{});self.assertEqual(info['bounded_stations'],1)

    def test_duplicate_rows_for_same_line_do_not_form_an_interval(self):
        with self.assertRaises(ValueError):build_metro_dataset(metadata(2),[observation(),observation(2)],today=date(2026,11,4))

    def test_stale_or_other_city_variant_is_not_a_reference(self):
        dataset,data,_=self.dataset()
        self.assertIsNone(dataset.lookup_interval('Узел',60,30))
        stale=MetroFlowDataset(data,today=date(2028,1,1))
        self.assertIsNone(stale.lookup_interval('Узел',LAT,LNG))

    def test_invalid_variant_numbers_lines_or_name_conflicts_are_rejected(self):
        _,original,_=self.dataset()
        cases=[]
        for value in (float('nan'),float('inf'),-1,True):
            data=copy.deepcopy(original);data['station_variants']['Узел'][0]['entries_per_day']=value;cases.append(data)
        data=copy.deepcopy(original);data['station_variants']['Узел'][1]['line']='Линия А';cases.append(data)
        data=copy.deepcopy(original);data['stations']['Узел']=1000;cases.append(data)
        for data in cases:
            with self.assertRaises(ValueError):MetroFlowDataset(data,today=date(2026,11,4))

    def test_known_interval_participates_in_market_audience_without_fake_pedestrian_count(self):
        dataset,_,_=self.dataset()
        card=business_card(nearby_metro=[dict(name='Узел',coordinates=[LNG,LAT])])
        traffic=FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,[card],metro_dataset=dataset)
        self.assertIsNone(traffic.estimated_daily_pedestrians)
        self.assertEqual(traffic.transport_data_status,'bounded')
        self.assertGreater(traffic.transport_lower_daily,0);self.assertGreater(traffic.transport_upper_daily,traffic.transport_lower_daily)
        from real_estate_engine.route_audience import condition_transport_on_routes
        from test_route_barriers import route_evidence
        name=traffic.nearest_metros[0].name
        evidence=route_evidence()
        evidence['access_routes']['walk']['origin']['name']=name
        traffic=condition_transport_on_routes(traffic,evidence)
        bounds,raw,coverage,_=_transport_audience(traffic,dict(active_hours=list(range(24))),0)
        self.assertIsNone(raw);self.assertGreater(bounds.high,bounds.low);self.assertEqual(coverage,1)

    def test_truly_unknown_other_station_keeps_upper_bound_open(self):
        dataset,_,_=self.dataset()
        card=business_card(nearby_metro=[dict(name='Узел',coordinates=[LNG,LAT]),dict(name='Нет данных',coordinates=[LNG+.001,LAT])])
        traffic=FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,[card],metro_dataset=dataset)
        self.assertEqual(traffic.transport_data_status,'partial')
        self.assertIsNone(traffic.transport_upper_daily);self.assertGreater(traffic.transport_lower_daily,0)


class FinanceGapTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_website_does_not_block_a_different_company(self):
        entered=threading.Event();release=threading.Event()
        def get(url,**kwargs):
            if 'slow' in url:entered.set();release.wait(2)
            return SimpleNamespace(status_code=200,content='<p>АО ВКУСВИЛЛ, ИНН 7734443270</p>'.encode('utf-8'))
        resolver=WebsiteLegalResolver(SimpleNamespace(get=get))
        slow=asyncio.create_task(resolver.resolve('Медленный сайт',['https://slow.example.test/']))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            fast=await asyncio.wait_for(resolver.resolve('ВкусВилл',['https://fast.example.test/']),.4)
            self.assertEqual(fast[0]['inn'],INN);self.assertFalse(slow.done())
        finally:release.set();await slow

    async def test_tracking_duplicates_reuse_but_functional_store_parameters_do_not(self):
        calls=[]
        def get(url,**kwargs):
            calls.append(url);return SimpleNamespace(status_code=200,content=b'No legal details')
        resolver=WebsiteLegalResolver(SimpleNamespace(get=get))
        await resolver.resolve('Магазин',['https://shop.example.test/?store=1&utm_source=maps&yclid=123'])
        await resolver.resolve('Магазин',['https://shop.example.test/?store=1&utm_source=other&yclid=456'])
        self.assertEqual(len(calls),1)
        await resolver.resolve('Магазин',['https://shop.example.test/?store=2'])
        self.assertEqual(len(calls),2)
        self.assertEqual(canonical_business_url('https://shop.example.test/?sig=abc&utm_source=x'),'https://shop.example.test/?sig=abc&utm_source=x')

    async def test_duplicate_same_company_requests_share_one_fetch(self):
        entered=threading.Event();release=threading.Event();calls=[]
        def get(url,**kwargs):
            calls.append(url);entered.set();release.wait(2)
            return SimpleNamespace(status_code=200,content='<p>АО ВКУСВИЛЛ, ИНН 7734443270</p>'.encode('utf-8'))
        resolver=WebsiteLegalResolver(SimpleNamespace(get=get))
        first=asyncio.create_task(resolver.resolve('ВкусВилл',['https://shop.example.test/']))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            second=asyncio.create_task(resolver.resolve('ВкусВилл',['https://shop.example.test/']));await asyncio.sleep(0)
            release.set();a,b=await asyncio.gather(first,second)
            self.assertEqual(len(calls),1);a[0]['inn']='changed';self.assertEqual(b[0]['inn'],INN)
        finally:release.set();await asyncio.gather(first,return_exceptions=True)

    async def test_no_legal_match_is_distinguished_from_unavailable_company_reporting(self):
        helper=fns_cases.FinanceAcquisitionTests()
        collector=helper.collector([],[]);card=business_card()
        await collector.enrich_cards([card])
        self.assertEqual(card.fns_diagnostics['finance_status'],'not_matched')
        self.assertEqual(card.fns_diagnostics['scope'],'company')
        collector=helper.collector([(INN,'dadata_name_address')],[])
        async def unavailable(*args):return {}
        collector._fetch_inns_fallback_http=collector._fetch_inns_via_browser_cdp=unavailable
        await collector.enrich_cards([card])
        self.assertEqual(card.fns_diagnostics['finance_status'],'unavailable')
        self.assertEqual(card.fns_diagnostics['status'],'matched_company')
        self.assertIsNone(card.fns_revenue_rub)

    async def test_resolution_errors_are_typed_without_serializing_exception_strings(self):
        collector=fns_cases.FinanceAcquisitionTests().collector([],[])
        async def failed(*args):raise RuntimeError('secret credential must not be saved')
        collector.website_resolver.resolve=failed
        card=business_card();await collector.enrich_cards([card])
        self.assertEqual(card.fns_diagnostics['errors'],[dict(stage='business_website',error_type='RuntimeError')])
        self.assertNotIn('secret',json.dumps(card.fns_diagnostics))

    def test_financially_unknown_is_not_displayed_as_zero(self):
        unknown=business_card(fns_inn=INN,fns_revenue_rub=None)
        zero=business_card(fns_inn=INN,fns_revenue_rub=0,fns_reporting_year=2025)
        self.assertIn('недоступна',unknown.fns_summary_str)
        self.assertNotIn('нулевая/скрыта',unknown.fns_summary_str)
        self.assertIn('0.0 млн',zero.fns_summary_str)


if __name__=='__main__':unittest.main()
