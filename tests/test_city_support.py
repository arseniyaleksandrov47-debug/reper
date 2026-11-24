"""City isolation and honest handling of regional data gaps."""
import json
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from real_estate_engine import RealEstateAggregator, LocationResolver, NvidiaCompetitorAnalyzer, OptimalPropertyRecommender
from real_estate_engine.cities import MOSCOW, SAINT_PETERSBURG, get_city
from real_estate_engine.collectors import CianCollector, DomclickCollector, AvitoCollector, YandexRealtyCollector
from real_estate_engine.competitors import MapCardsResult, YandexMapsRadiusCollector
from real_estate_engine.fns import DaDataInnResolver
from real_estate_engine.population import BuildingFetchResult, ResidentialPopulationAnalyzer
from real_estate_engine.property_sources import addresses_match, PublicPropertySources, cian_unit
from real_estate_engine.environment_cache import EnvironmentSourceCache
from real_estate_engine.ranking_policy import metric_value
from test_collector_floors import collector_with_response
from test_engine_regressions import property_offer, memory_storage, competition_report


class CityLocationTests(unittest.TestCase):
    def test_aliases_and_city_specific_databases(self):
        for value in ('spb', 'СПб', 'Санкт-Петербург', 'Питер'):
            self.assertEqual(get_city(value), SAINT_PETERSBURG)
        self.assertNotEqual(MOSCOW.database_path, SAINT_PETERSBURG.database_path)
        with self.assertRaises(ValueError):
            get_city('неподключённый город')
        for lat in (None, '59.9', float('nan'), True):
            self.assertFalse(SAINT_PETERSBURG.contains(lat, 30.3))

    def test_whole_city_and_wrong_city_do_not_resolve_to_moscow(self):
        result = LocationResolver.resolve(None, city='spb')
        self.assertEqual(result[:2], SAINT_PETERSBURG.center)
        with self.assertRaises(ValueError):
            LocationResolver.resolve('Москва, Арбат', city='spb')

    def test_geocoder_uses_city_and_discards_outside_coordinates(self):
        state = {'stack': [{'results': {'items': [{'title': 'Петроградская', 'coordinates': [30.3128, 59.9649]}]}}]}
        reply = SimpleNamespace(status_code=200, text='<script type="application/json" class="state-view">'+json.dumps(state)+'</script>')
        with patch('real_estate_engine.recommender.curl_requests.Session') as session:
            get = session.return_value.__enter__.return_value.get
            get.return_value = reply
            result = LocationResolver._geocode_yandex_sync('метро Петроградская', city='spb')
            self.assertEqual(result[:2], (59.9649, 30.3128))
            self.assertIn('/maps/2/saint-petersburg/', get.call_args.args[0])
            state['stack'][0]['results']['items'][0]['coordinates'] = [37.61, 55.75]
            reply.text = '<script type="application/json" class="state-view">'+json.dumps(state)+'</script>'
            self.assertIsNone(LocationResolver._geocode_yandex_sync('метро Петроградская', city='spb'))

    def test_geocoder_failure_does_not_substitute_a_city_center(self):
        with patch.object(LocationResolver, '_geocode_yandex_sync', return_value=None), \
                patch.object(LocationResolver, '_geocode_nominatim_sync', return_value=None):
            with self.assertRaises(ValueError):
                LocationResolver.resolve('метро несуществующее', city='spb')

    def test_address_proof_preserves_city_and_litera(self):
        self.assertTrue(addresses_match('Санкт-Петербург, Ленина улица, 5 литера А', 'СПб, ул. Ленина, 5А'))
        self.assertFalse(addresses_match('Москва, ул. Ленина, 5', 'Санкт-Петербург, ул. Ленина, 5'))
        self.assertFalse(addresses_match('СПб, ул. Ленина, 5 литера А', 'СПб, ул. Ленина, 5 литера Б'))
        for abbreviation, full in [('пер.', 'переулок'), ('наб.', 'набережная'), ('б-р', 'бульвар')]:
            self.assertTrue(addresses_match(f'СПб, Тестовый {abbreviation}, 5', f'Санкт-Петербург, Тестовый {full}, 5'))
        self.assertFalse(addresses_match('СПб, Тестовый пер., 5', 'СПб, Тестовый проспект, 5'))

    def test_recorded_spb_avito_card_is_bound_with_address_abbreviation(self):
        from pathlib import Path
        from real_estate_engine.property_sources import avito_unit
        root = Path(__file__).resolve().parent/'fixtures/audit/spb_after_card_fix/run_20261107_171419'
        offers = json.loads((root / 'fresh_matches.json').read_text(encoding='utf-8'))
        prop = next(value for value in offers if value['source'] == 'avito')
        body = (root / 'source_diagnostics/avito_card.html').read_text(encoding='utf-8')
        unit = avito_unit(body, prop['url'], prop)
        self.assertEqual(unit['unit_id'], prop['external_id'])
        self.assertTrue(unit['identity_checked'])
        self.assertEqual(unit['floor'], 3)

    def test_cli_preserves_city_when_serializing_continuation(self):
        import run_engine
        with patch('sys.argv', ['run_engine.py', '--city', 'spb']):
            args = run_engine.parse_args()
        self.assertEqual(args.location, 'Санкт-Петербург')
        command = run_engine.client_search_command(args, include_unverified=True)
        self.assertEqual(command[command.index('--city')+1], 'saint_petersburg')
        self.assertIn('--include-unverified', command)

    def test_security_page_http_200_is_access_failure_not_card_format_failure(self):
        from real_estate_engine.collectors.access_state import response_access_issue
        body = '<html><script src="https://servicepipe.tech/static/checkjs/abc.js"></script></html>'
        issue = response_access_issue(SimpleNamespace(status_code=200, text=body))
        self.assertEqual(issue['kind'], 'security_challenge')
        self.assertEqual(issue['status_code'], 200)
        self.assertIsNone(response_access_issue(SimpleNamespace(status_code=200, text=body), has_data=True))

    def test_regional_cian_card_reaches_exact_unit_verification(self):
        prop = dict(source='cian', external_id='123456', url='https://spb.cian.ru/rent/commercial/123456/',
                    address='Санкт-Петербург, Тестовая улица, 3к2', area_sqm=60)
        offer = dict(id=123456, cianId=123456, dealType='rent', offerType='commercial', status='published',
            totalArea=60, floorNumber=2, description='Объявление',
            geo=dict(address=[dict(type='location', fullName='Санкт-Петербург'),
                dict(type='street', fullName='Тестовая улица'), dict(type='house', fullName='3к2')]),
            bargainTerms=dict(currency='rur', price=70000, priceType='all', paymentPeriod='monthly'))
        entries = json.dumps([dict(key='defaultState', value=dict(offerData=dict(offer=offer)))])
        body = "<script>window._cianConfig['frontend-offer-card'] = (window._cianConfig['frontend-offer-card'] || []).concat("+entries+");</script>"
        provider = PublicPropertySources(SimpleNamespace(environment_source_cache=EnvironmentSourceCache(None)))
        provider._publisher_sites = lambda *args: []
        provider._get = lambda url: (body, url)
        result = provider.collect(prop)
        self.assertEqual(result['sources'][0]['unit_id'], '123456')
        self.assertEqual(result['sources'][0]['price_month'], 70000)
        self.assertTrue(result['sources'][0]['identity_checked'])
        with self.assertRaises(ValueError):
            cian_unit(body, 'https://spb.cian.ru/rent/commercial/999999/', prop)
        with self.assertRaises(ValueError):
            cian_unit(body, 'https://spb.cian.ru.evil.test/rent/commercial/123456/', prop)


class CityCollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cian_receives_regional_id(self):
        collector = collector_with_response(CianCollector, {'data': {'offersSerialized': []}})
        collector.city = SAINT_PETERSBURG
        collector.session.post = Mock(return_value=collector.session.post())
        await collector.collect(1)
        self.assertEqual(collector.session.post.call_args.kwargs['json']['jsonQuery']['region']['value'], [2])

    async def test_unconfigured_domclick_region_is_unavailable_without_http(self):
        collector = collector_with_response(DomclickCollector, {'result': {'items': []}})
        collector.city = SimpleNamespace(domclick_address=None)
        collector.session.get = Mock(side_effect=AssertionError('Do not query an unverified region identifier'))
        self.assertEqual(await collector.collect(1), [])
        self.assertEqual(collector.collection_summary(0)['status'], 'unavailable')
        collector.session.get.assert_not_called()

    async def test_domclick_receives_confirmed_petersburg_region(self):
        collector = collector_with_response(DomclickCollector, {'result': {'items': []}})
        collector.city = SAINT_PETERSBURG
        collector.session.get = Mock(return_value=collector.session.get())
        await collector.collect(1)
        self.assertEqual(collector.session.get.call_args.kwargs['params']['address'],
                         '7b4698a7-f8b8-424c-9195-e24f3ddb88f3')

    async def test_city_scope_rejects_moscow_inventory_and_requests_before_network(self):
        storage, conn = memory_storage()
        self.addCleanup(conn.close)
        aggregator = RealEstateAggregator.__new__(RealEstateAggregator)
        aggregator.city = SAINT_PETERSBURG
        aggregator.collection_bounds = SAINT_PETERSBURG.bounds
        aggregator.storage = storage
        aggregator.proxy = None
        aggregator.run_pipeline = AsyncMock()
        self.assertIsNotNone(aggregator._collection_quality_issue(property_offer()))
        for request in ({'location_query': 'Москва'}, {'center_lat': 55.75, 'center_lng': 37.61},
                        {'city': 'moscow', 'center_lat': 59.96, 'center_lng': 30.31}):
            with self.assertRaises(ValueError):
                await aggregator.collect_for_search(request)
        aggregator.run_pipeline.assert_not_called()

    def test_catalog_urls_and_municipal_source_follow_profile(self):
        with patch('real_estate_engine.aggregator.PropertyStorage'), \
                patch('real_estate_engine.aggregator.NvidiaCompetitorAnalyzer'), \
                patch('real_estate_engine.aggregator.OptimalPropertyRecommender'):
            aggregator = RealEstateAggregator(':memory:', city='spb')
        self.assertEqual(len(aggregator.collectors), 5)
        self.assertFalse(any(c.name == 'InvestMoscow' for c in aggregator.collectors))
        self.assertTrue(all(c.city == SAINT_PETERSBURG for c in aggregator.collectors))
        self.assertIn('/sankt-peterburg/', next(c for c in aggregator.collectors if isinstance(c, AvitoCollector)).endpoint)
        self.assertIn('/sankt-peterburg/', next(c for c in aggregator.collectors if isinstance(c, YandexRealtyCollector)).web_url)


class CityEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    def test_housing_price_request_has_petersburg_url_and_referrer(self):
        analyzer = ResidentialPopulationAnalyzer.__new__(ResidentialPopulationAnalyzer)
        analyzer.realty_session = SimpleNamespace(get=Mock(return_value=SimpleNamespace(status_code=200, text='')))
        self.assertEqual(analyzer._fetch_yandex_realty_prices_sync(59.96, 30.31), ([], []))
        call = analyzer.realty_session.get.call_args
        self.assertIn('/sankt-peterburg/', call.args[0])
        self.assertEqual(call.kwargs['headers']['Referer'], 'https://realty.yandex.ru/sankt-peterburg/')

    async def test_petersburg_does_not_fetch_moscow_passenger_data(self):
        analyzer = NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.model = 'test'
        analyzer.map_collector = SimpleNamespace(collect_in_circle=AsyncMock(return_value=MapCardsResult([], status='empty')))
        analyzer.population_analyzer = SimpleNamespace(analyze_circle=AsyncMock(return_value=None))
        analyzer.metro_flow_source = SimpleNamespace(get_dataset=AsyncMock(side_effect=AssertionError('Moscow source')))
        result = await analyzer.analyze_circle(59.96, 30.31, 400, 'Концепция', business_concept='Клиенты по записи')
        analyzer.metro_flow_source.get_dataset.assert_not_called()
        self.assertTrue(any('этого города' in w for w in result.data_warnings))

    def test_petersburg_apartments_are_preserved_without_moscow_resident_coefficients(self):
        analyzer = ResidentialPopulationAnalyzer.__new__(ResidentialPopulationAnalyzer)
        building = dict(id='1', name='Дом', purpose_name='Жилой дом', point=dict(lat=59.9649, lon=30.3128),
            floors=dict(ground_count=5), structure_info=dict(apartments_count=100), adm_div=[])
        analyzer._fetch_2gis_buildings_sync = lambda *args: BuildingFetchResult([building], coverage_complete=True)
        analyzer._fetch_yandex_realty_prices_sync = lambda *args: ([], [])
        result = analyzer.analyze_circle_sync(59.9649, 30.3128)
        self.assertEqual(result.total_apartments_count, 100)
        self.assertIsNone(result.estimated_residents_count)
        self.assertIsNone(result.top_residential_buildings[0].estimated_residents)
        self.assertIsNone(metric_value('residents_count', {}, SimpleNamespace(population=result, foot_traffic=None)))
        self.assertTrue(any('Московские коэффициенты не применялись' in w for w in result.data_warnings))

    def test_maps_query_uses_city_from_coordinates(self):
        collector = YandexMapsRadiusCollector.__new__(YandexMapsRadiusCollector)
        collector.headers = {}
        collector.session = SimpleNamespace(get=Mock(return_value=SimpleNamespace(status_code=503)))
        with self.assertRaises(RuntimeError):
            collector._fetch_query_sync('услуги', 59.96, 30.31, 400)
        self.assertIn('/maps/2/saint-petersburg/', collector.session.get.call_args.args[0])

    def test_fns_discovery_uses_regional_filter_and_separates_street_cache(self):
        resolver = DaDataInnResolver.__new__(DaDataInnResolver)
        resolver._street_cache = {}
        resolver._suggest_party = Mock(return_value=[])
        resolver.resolve_candidates_for_card('Мастерская', ['Ремонт'], 'Санкт-Петербург, ул. Ленина, 5')
        regional = [call.kwargs['locations'] for call in resolver._suggest_party.call_args_list if call.kwargs.get('locations')]
        self.assertTrue(regional)
        self.assertTrue(all(value == [{'kladr_id': '78'}, {'kladr_id': '47'}] for value in regional))
        self.assertTrue(any(key.startswith('saint_petersburg|') for key in resolver._street_cache))
        self.assertEqual(resolver._parse_street_and_house('Санкт-Петербург, ул. Ленина, 5'), ('Ленина', '5'))


class CityContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from floor_ai_fixtures import FixtureFloorAnalyzer, RecordingFloorInference
        from test_unknown_floor_context import response
        self.storage, conn = memory_storage()
        self.addCleanup(conn.close)
        self.storage.upsert_batch([property_offer('spb-one', address='Санкт-Петербург, Тестовая улица, 1',
            lat=59.96, lng=30.31, price_month=70000)])
        model = RecordingFloorInference(handler=lambda payload: response(payload, unknown=True))
        self.calls = []
        calls = self.calls
        class Analyzer(FixtureFloorAnalyzer):
            _call_floor_structured_sync = model._call_floor_structured_sync
            async def analyze_circle(self, **kwargs):
                calls.append(kwargs)
                return competition_report(center_lat=kwargs['center_lat'], center_lng=kwargs['center_lng'])
        self.analyzer = Analyzer()
        self.engine = OptimalPropertyRecommender(self.storage, self.analyzer, city='spb')
        self.kwargs = dict(target_business='Новая услуга', location_query='Клиентская точка',
            center_lat=59.96, center_lng=30.31, search_radius_m=500, min_area_sqm=30, max_area_sqm=70,
            max_price_month=100000, max_candidates=1,
            business_concept='Клиенты приходят по записи. Нужен разрешённый доступ до двери помещения.')

    def restored(self, report):
        saved = json.loads(json.dumps(asdict(report)))
        return SimpleNamespace(city=saved['city'], next_actions=saved['next_actions'])

    async def test_serialized_continuation_restores_city_in_new_client(self):
        strict = await self.engine.find_optimal_properties(**self.kwargs)
        saved = self.restored(strict)
        self.assertEqual(saved.city, 'saint_petersburg')
        self.assertTrue(all(a['request']['city'] == saved.city for a in saved.next_actions))
        restored_engine = OptimalPropertyRecommender(self.storage, self.analyzer, city=saved.city)
        result = await restored_engine.continue_unverified_analysis(saved)
        self.assertEqual(result.city, saved.city)
        self.assertEqual(result.evaluated_count, 1)
        self.assertTrue(all(SAINT_PETERSBURG.contains(v['center_lat'], v['center_lng']) for v in self.calls))
        self.assertEqual(result.client_max_price_month, 100000)
        self.assertEqual(result.search_radius_m, 500)

    async def test_serialized_find_more_preserves_city_and_exclusions(self):
        first = await self.engine.find_optimal_properties(**self.kwargs, include_unverified=True)
        self.storage.upsert_batch([property_offer('spb-two', address='Санкт-Петербург, Тестовая улица, 2',
            lat=59.9601, lng=30.31, price_month=60000)])
        saved = self.restored(first)
        restored_engine = OptimalPropertyRecommender(self.storage, self.analyzer, city=saved.city)
        second = await restored_engine.find_more_options(saved)
        self.assertEqual(second.city, 'saint_petersburg')
        self.assertEqual([c.property_data['external_id'] for c in second.ranked_candidates], ['spb-two'])
        self.assertEqual(second.client_max_price_month, first.client_max_price_month)
        self.assertEqual(second.search_radius_m, first.search_radius_m)

    async def test_wrong_city_client_cannot_replay_petersburg_action(self):
        report = await self.engine.find_optimal_properties(**self.kwargs)
        saved = self.restored(report)
        wrong_engine = OptimalPropertyRecommender(self.storage, self.analyzer, city='moscow')
        with self.assertRaises(ValueError):
            await wrong_engine.continue_unverified_analysis(saved)
        self.assertEqual(self.calls, [])
        action = next(a for a in saved.next_actions if a['type'] == 'continue_unverified')
        action['request']['city'] = 'moscow'
        with self.assertRaises(ValueError):
            await self.engine.continue_unverified_analysis(saved)
        self.assertEqual(self.calls, [])

    async def test_direct_search_rejects_mismatched_city_before_analysis(self):
        for kwargs in (dict(self.kwargs, city='moscow'), dict(self.kwargs, center_lat=55.75, center_lng=37.61),
                       dict(self.kwargs, location_query='Москва, улица Тестовая')):
            with self.assertRaises(ValueError):
                await self.engine.find_optimal_properties(**kwargs)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
