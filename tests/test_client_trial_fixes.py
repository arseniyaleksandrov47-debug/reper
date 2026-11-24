"""Failures exposed by the fresh client journey, including unsafe boundaries."""
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from real_estate_engine import RealEstateAggregator
from real_estate_engine.ai_contract import AIContractError, floor_response_schema, normalize_floor_response
from real_estate_engine.inventory_search import attach_collection, request_scope
from real_estate_engine.property_sources import cian_unit
from real_estate_engine.ranking_policy import RankingProfile, RankingProfileResolver
from test_engine_regressions import LAT, LNG, memory_storage, property_offer


class DerivedMissingFactsTests(unittest.TestCase):
    def test_native_schema_cannot_generate_a_contradictory_missing_list(self):
        conditions=[dict(requirement_id='access',description='Вход посетителей',origin='inferred'),
                    dict(requirement_id='floor',description='Первый этаж',origin='explicit')]
        schema=floor_response_schema([dict(candidate_id='c0',requirements=conditions,evidence_catalog={})])
        fields=schema['properties']['assessments']['properties']['c0']['properties']
        self.assertEqual(set(fields),{'reason','checks'})
        for first,second,missing,status in [('unknown','met',['Вход посетителей'],'needs_verification'),
                                          ('met','met',[],'eligible'),
                                          ('not_met','unknown',['Первый этаж'],'rejected')]:
            wire=dict(assessments=dict(c0=dict(reason='Оценка',checks={
                'access':dict(result=first,evidence_ids=[]),'floor':dict(result=second,evidence_ids=[])})))
            result=normalize_floor_response(wire,{'c0':conditions})['assessments'][0]
            self.assertEqual((result['missing_facts'],result['status']),(missing,status))

    def test_extra_fields_and_invented_conditions_are_rejected(self):
        conditions=[dict(requirement_id='access',description='Вход посетителей',origin='inferred')]
        for missing in (['access'],['Придуманный лифт']):
            wire=dict(assessments=dict(c0=dict(reason='Оценка',missing_facts=missing,
                checks=dict(access=dict(result='met',evidence_ids=[])))))
            with self.assertRaises(AIContractError):normalize_floor_response(wire,{'c0':conditions})
        wire['assessments']['c0'].pop('missing_facts')
        wire['assessments']['c0']['checks']['invented']=dict(result='met',evidence_ids=[])
        with self.assertRaises(AIContractError):normalize_floor_response(wire,{'c0':conditions})


class CianUnitTests(unittest.TestCase):
    def setUp(self):
        self.prop=dict(external_id='123456',url='https://www.cian.ru/rent/commercial/123456/?mlSearchSessionGuid=session',
            address='Россия, Москва, Тестовая улица, 3к2',area_sqm=60)
        self.offer=dict(id=123456,cianId=123456,dealType='rent',offerType='commercial',status='published',
            totalArea='60',floorNumber=1,description='Отдельный вход. На территории пропускной режим.',
            geo=dict(address=[dict(type='location',fullName='Москва'),dict(type='street',fullName='Тестовая улица'),
                              dict(type='house',fullName='3к2')]),
            bargainTerms=dict(currency='rur',price=24000,priceType='squareMeter',paymentPeriod='annual'))

    def body(self,offer=None,extra=None):
        state=dict(offerData=dict(offer=offer or self.offer),similarOffers=dict(offers=[extra or self.offer]))
        entries=json.dumps([dict(key='defaultState',value=state)],ensure_ascii=False)
        return "<script>window._cianConfig['frontend-offer-card'] = (window._cianConfig['frontend-offer-card'] || []).concat("+entries+");</script>"

    def test_tracking_query_does_not_change_unit_and_annual_area_price_is_normalized(self):
        unit=cian_unit(self.body(),self.prop['url'].split('?')[0],self.prop)
        self.assertEqual((unit['unit_id'],unit['area_sqm'],unit['floor'],unit['price_month']),('123456',60,1,120000))
        self.assertIn('пропускной режим',unit['facts']['description'])
        self.assertNotIn('similarOffers',unit['facts'])

    def test_other_unit_or_partial_address_cannot_supply_identity(self):
        for changes in ({'id':999999},{'cianId':999999},{'totalArea':'90'},
                        {'geo':{'address':[dict(type='location',fullName='Москва')]}},
                        {'dealType':'sale'}, {'status':None}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                cian_unit(self.body({**self.offer,**changes}),self.prop['url'],self.prop)
        with self.assertRaises(ValueError):
            cian_unit(self.body(),self.prop['url'].replace('/123456/','/999999/'),self.prop)

    def test_similar_offer_and_conflicting_main_state_are_not_used(self):
        wrong={**self.offer,'id':999999,'cianId':999999}
        with self.assertRaises(ValueError):cian_unit(self.body(wrong,extra=self.offer),self.prop['url'],self.prop)
        with self.assertRaises(ValueError):cian_unit(self.body()+self.body(wrong),self.prop['url'],self.prop)

    def test_unknown_currency_or_price_period_never_means_monthly_rubles(self):
        for changes in ({'currency':'usd'},{'paymentPeriod':'unknown'},{'price':float('inf')},{'price':-10}):
            offer={**self.offer,'bargainTerms':{**self.offer['bargainTerms'],**changes}}
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                cian_unit(self.body(offer),self.prop['url'],self.prop)

    def test_recorded_live_cian_format_is_read_with_exact_identity(self):
        archive=Path(__file__).resolve().parent/'fixtures/audit/client_fixes/sources'
        prop=json.loads((archive/'cian_expected.json').read_text(encoding='utf-8'))
        unit=cian_unit((archive/'cian_unit.html').read_text(encoding='utf-8'),prop['url'],prop)
        self.assertEqual(unit['unit_id'],prop['external_id'])
        self.assertAlmostEqual(unit['price_month'],prop['price_month'],places=1)


class AdaptiveInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        storage,conn=memory_storage();self.addCleanup(conn.close)
        self.agg=RealEstateAggregator.__new__(RealEstateAggregator)
        from real_estate_engine.cities import MOSCOW
        self.agg.city=MOSCOW
        self.agg.storage=storage;self.agg.proxy=None;self.agg.collection_bounds=None
        self.request=dict(location_query='Клиентская точка',center_lat=LAT,center_lng=LNG,
            search_radius_m=500,min_area_sqm=30,max_area_sqm=80,max_price_month=150000,max_listing_age_hours=24)
        self.depths=[]
        depths=self.depths
        class Collector:
            name='fixture'
            async def collect(self,limit):
                depths.append(limit)
                items=[property_offer('far',lat=LAT+.1),property_offer('expensive',price_month=300000)]
                if limit>=120:items.append(property_offer('eligible'))
                return items
            def collection_summary(self,count):
                return dict(status='ok',collected=count,warnings=[],coverage_complete=False)
        self.agg.collectors=[Collector()]

    async def test_first_empty_sample_expands_without_relaxing_client_filters(self):
        result=await self.agg.collect_for_search(self.request,target_matches=1)
        state=result['inventory_search']
        self.assertEqual(self.depths,[30,120])
        self.assertEqual(state['fresh_numerical_matches'],1)
        self.assertEqual(state['stop_reason'],'sample_target')
        self.assertFalse(state['coverage_complete'])
        self.assertEqual(state['request_scope'],request_scope(self.request))

    async def test_depth_limit_is_explicit_and_excluded_options_do_not_fill_target(self):
        request={**self.request,'exclude_property_keys':['cian:eligible']}
        result=await self.agg.collect_for_search(request,max_limit=120,ceiling=120,target_matches=1)
        state=result['inventory_search']
        self.assertEqual(state['fresh_numerical_matches'],0)
        self.assertEqual(state['stop_reason'],'depth_limit')
        self.assertIsNone(state['next_limit'])
        report=SimpleNamespace(numerical_matching_count=0,ranking_warnings=[])
        attach_collection(report,state)
        self.assertIn('просмотренной части',report.confirmation_message)

    async def test_find_more_collects_new_pages_and_passes_same_request(self):
        await self.agg.collect_for_search(self.request,initial_limit=60,max_limit=60,ceiling=120,target_matches=1)
        request={**self.request,'exclude_property_keys':['cian:seen']}
        previous=SimpleNamespace(inventory_collection=self.agg._inventory_collection,
            next_actions=[dict(type='find_more',request=request)])
        empty=SimpleNamespace(numerical_matching_count=0,ranking_warnings=[])
        filled=SimpleNamespace(numerical_matching_count=1,ranking_warnings=[])
        # The continuation is executed again only after real new collection.
        self.agg.recommender=SimpleNamespace(find_more_options=AsyncMock(side_effect=[empty,filled]))
        result=await self.agg.find_more_options(previous)
        self.assertIs(result,filled)
        self.assertEqual(self.depths,[60,120])
        self.assertEqual(self.agg._inventory_collection['fresh_numerical_matches'],1)
        self.assertEqual(self.agg._inventory_collection['excludes'],['cian:seen'])
        self.assertEqual(self.agg.recommender.find_more_options.await_count,2)

    async def test_unavailable_sources_do_not_trigger_successive_empty_market_scans(self):
        async def fail(limit):raise RuntimeError('fixture source unavailable')
        self.agg.collectors[0].collect=fail
        state=(await self.agg.collect_for_search(self.request))['inventory_search']
        self.assertEqual(len(state['rounds']),1)
        self.assertEqual(state['stop_reason'],'sources_unavailable')
        self.assertFalse(state['coverage_complete'])

    async def test_invalid_limits_are_rejected_before_collecting(self):
        for changes in ({'min_area_sqm':90,'max_area_sqm':30},{'max_price_month':-1},
                        {'max_listing_age_hours':0},{'exclude_property_keys':'invalid'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                await self.agg.collect_for_search({**self.request,**changes})
        self.assertEqual(self.depths,[])

    async def test_another_search_does_not_inherit_previous_collection_or_center(self):
        await self.agg.collect_for_search(self.request,initial_limit=30,max_limit=30,target_matches=1)
        report=SimpleNamespace(numerical_matching_count=0,ranking_warnings=[])
        self.agg.recommender=SimpleNamespace(find_optimal_properties=AsyncMock(return_value=report))
        result=await self.agg.find_optimal_properties('Любая концепция',location_query='Другая точка',business_concept='Приём по записи.',
            center_lat=LAT+.01,center_lng=LNG,search_radius_m=300)
        self.assertIs(result,report)
        self.assertFalse(hasattr(result,'inventory_collection'))
        self.assertEqual(self.agg.recommender.find_optimal_properties.call_args.kwargs['center_lat'],LAT+.01)

    async def test_explicit_profile_retry_does_not_reuse_an_unconfirmed_generation(self):
        resolver=RankingProfileResolver(None)
        resolver._generate=AsyncMock(side_effect=[RankingProfile(analysis_status='unavailable'),RankingProfile(status='ready')])
        first=await resolver.resolve('Услуга','Описание нового бизнеса')
        second=await resolver.resolve('Услуга','Описание нового бизнеса')
        third=await resolver.resolve('Услуга','Описание нового бизнеса')
        self.assertNotEqual(first.status,'ready')
        self.assertEqual(second.status,'ready');self.assertEqual(third.status,'ready')
        self.assertEqual(resolver._generate.await_count,2)


if __name__=='__main__':unittest.main()
