"""Regional source identity, units, dates and shared network transport."""
import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from real_estate_engine.cities import SAINT_PETERSBURG
from real_estate_engine.collectors.base import _DirectInterfaceBridge
from real_estate_engine.collectors.domclick import DomclickCollector
from real_estate_engine.collectors.municipal import rental_lot
from real_estate_engine.environment_cache import EnvironmentSourceCache
from real_estate_engine.metro_data import MetroFlowDataset
from real_estate_engine.population import ResidentialPopulationAnalyzer, BuildingFetchResult
from real_estate_engine.population_data import BuildingPopulationDataset
from real_estate_engine.property_sources import PublicPropertySources, domclick_catalogue_unit, municipal_unit
from real_estate_engine.spb_metro_source import parse_annual_report
from real_estate_engine.competitors import NvidiaCompetitorAnalyzer, MapCardsResult
from test_engine_regressions import business_card
import test_fns_acceptance as finance_tests

INN = finance_tests.INN


def lot():
    now = datetime.now(timezone.utc)
    return dict(id='123456_1', subjectRFCode='78', typeTransaction='rent', category={'code': '11'},
        lotStatus='APPLICATIONS_SUBMISSION', isAnnulled=False, isStopped=False, currencyCode='643',
        biddStartTime=(now-timedelta(days=1)).isoformat(), biddEndTime=(now+timedelta(days=1)).isoformat(),
        characteristics=[dict(code='totalAreaRealty', characteristicValue=50)], priceMin=1200000,
        attributes=[dict(code='DA_priceMinFor_rent', value=dict(name='Арендный платеж за год'))],
        point=dict(lat=59.93, lon=30.31), estateAddress='Санкт-Петербург, Тестовая улица, 5',
        lotDescription='Этаж 1. Начальная ставка.', lotName='Нежилое помещение')


def offer(identifier='123456_1'):
    return dict(external_id=identifier, lat=59.93, lng=30.31,
        address='Санкт-Петербург, Тестовая улица, 5', area_sqm=50,
        url='https://spb.domclick.ru/card/rent__free_purpose__'+identifier)


class MunicipalTests(unittest.TestCase):
    def test_annual_bid_converts_to_monthly_without_using_payment_frequency(self):
        data=lot(); data['attributes'].append(dict(code='paymentFrequency', value=dict(name='Ежемесячный платеж')))
        prop=rental_lot(data,'spb')
        self.assertEqual(prop.price_month,100000)
        self.assertEqual(prop.source.value,'municipal')
        proof=municipal_unit(data,'http://torgi.gov.ru/new/api/public/lotcards/'+data['id'],offer())
        self.assertTrue(proof['auction_start_only'])
        self.assertNotIn('price_month',proof['facts'])
        data['attributes'][0]['value']['name']='Арендный платеж за месяц'
        self.assertEqual(rental_lot(data,'spb').price_month,1200000)

    def test_inactive_future_stopped_wrong_region_and_sale_are_not_current_inventory(self):
        for field,value in [('lotStatus','ARCHIVE'),('isStopped',True),('isAnnulled',True),
                ('subjectRFCode','77'),('typeTransaction','sale'),
                ('biddStartTime',(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()),
                ('biddEndTime',(datetime.now(timezone.utc)-timedelta(days=1)).isoformat())]:
            with self.subTest(field=field):
                data=lot();data[field]=value
                self.assertIsNone(rental_lot(data,'spb'))
        data=lot();data['lotStatus']='ARCHIVE'
        self.assertIn('archive_status',municipal_unit(data,'http://torgi.gov.ru/',offer())['facts'])

    def test_unknown_units_currency_identity_area_and_nonfinite_values_are_rejected(self):
        for change in [dict(currencyCode='840'),dict(priceMin=float('nan')),dict(priceMin=-1),
                       dict(attributes=[]),dict(point=dict(lat=55.75,lon=37.61))]:
            with self.subTest(change=change),self.assertRaises(ValueError):rental_lot(lot()|change,'spb')
        for prop in [offer('222_1'),offer()|dict(area_sqm=80),offer()|dict(address='Москва, Тестовая улица, 5')]:
            with self.assertRaises(ValueError):municipal_unit(lot(),'http://torgi.gov.ru/',prop)


class CatalogueTests(unittest.TestCase):
    def item(self):
        return dict(id='12345', offerRegion=SAINT_PETERSBURG.domclick_address, isSold=False,
            address=dict(displayName=offer()['address']), objectInfo=dict(area=50,floor=1),
            price=100000, description='Описание именно этого блока', photos=[dict(url='/vitrina/a/photo.jpg')])

    def metadata(self):
        return dict(city='spb',region_id=SAINT_PETERSBURG.domclick_address,
            source_url='https://bff-search-web.domclick.ru/api/offers/v1?address='+SAINT_PETERSBURG.domclick_address)

    def test_catalogue_requires_exact_unit_and_city_with_current_publication(self):
        prop=offer('12345'); result=domclick_catalogue_unit(self.item(),self.metadata(),prop)
        self.assertTrue(result['identity_checked']);self.assertEqual(result['price_month'],100000)
        self.assertEqual(result['photos'],['https://img.dmclk.ru/vitrina/a/photo.jpg'])
        for changed in [dict(id='54321'),dict(offerRegion='moscow'),dict(isSold=True),dict(isSold=None),
                        dict(objectInfo=dict(area=51)),dict(price=float('nan'))]:
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                domclick_catalogue_unit(self.item()|changed,self.metadata(),prop)

    def test_refresh_is_bounded_and_never_selects_a_different_unit(self):
        collector=DomclickCollector(city='spb'); item=self.item()
        response=Mock(content=json.dumps(dict(result=dict(items=[item]))).encode(),status_code=200,
            text='json',url=self.metadata()['source_url'])
        collector.session=SimpleNamespace(get=Mock(return_value=response))
        found,metadata=collector.public_unit_snapshot('12345')
        self.assertEqual(found['id'],'12345');self.assertEqual(metadata['city'],'saint_petersburg')
        with self.assertRaises(ValueError):collector.public_unit_snapshot('99999')
        self.assertEqual(collector.session.get.call_count,2)

    def test_http_401_full_card_refreshes_catalogue_in_same_client(self):
        from real_estate_engine.property_sources import PublicSourceError
        analyzer=SimpleNamespace(environment_source_cache=EnvironmentSourceCache(':memory:'),
            map_collector=SimpleNamespace(proxy=None))
        provider=PublicPropertySources(analyzer)
        provider._get=Mock(side_effect=PublicSourceError('card',401))
        provider._domclick_catalogue=Mock(return_value=domclick_catalogue_unit(self.item(),self.metadata(),offer('12345')))
        result=provider.collect(offer('12345'))
        self.assertEqual(len(result['sources']),1)
        self.assertEqual(result['sources'][0]['unit_id'],'12345')
        self.assertIn('card_access_diagnostic',result['sources'][0])


class MetroTests(unittest.TestCase):
    def test_unspecified_operator_flow_is_an_interval_only(self):
        data=dict(source='https://metro.spb.ru/report.pdf',as_of='2025-12-31',
            bounds=list(SAINT_PETERSBURG.bounds),measure='passengers_per_day_unspecified',stations={'Площадь Восстания':81347})
        dataset=MetroFlowDataset(data,today=date(2026,11,7))
        self.assertIsNone(dataset.lookup('Площадь Восстания',59.93,30.31))
        self.assertEqual(dataset.lookup_interval('Площадь Восстания',59.93,30.31),(0,81347))
        self.assertIsNone(dataset.lookup_interval('Сенная площадь',59.93,30.31))
        self.assertIsNone(dataset.lookup_interval('Площадь Восстания',55.75,37.61))
        self.assertIsNone(MetroFlowDataset(data,today=date(2027,11,7)).lookup_interval('Площадь Восстания',59.93,30.31))

    def test_operator_pdf_requires_year_table_units_and_unique_station_names(self):
        text='ГОДОВОЙ ОТЧЕТ 2025\nСамые пассажиронапряженные станции\nСреднесуточный пассажиропоток, чел\n«Тестовая» 12345\n'
        def parse(value):
            with patch('pypdf.PdfReader',return_value=SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda:value)])):
                return parse_annual_report(b'%PDF dummy','https://metro.spb.ru/report2025.pdf',today=date(2026,11,7))
        self.assertEqual(parse(text)['dataset']['stations'],{'Тестовая':12345})
        for bad in (text.replace('2025','2027'),text.replace('Среднесуточный','Годовой'),text+'«Тестовая» 55\n'):
            with self.assertRaises(ValueError):parse(bad)


class PopulationTests(unittest.TestCase):
    def dataset(self):
        return dict(source='https://provider.example/dataset',as_of=date.today().isoformat(),measure='residents',
            buildings=[dict(building_id='b1',lat=59.93,lng=30.31,residents=220)])

    def test_exact_building_and_coordinates_required_missing_is_not_zero(self):
        dataset=BuildingPopulationDataset(self.dataset())
        self.assertEqual(dataset.lookup('b1',59.93,30.31),220)
        self.assertIsNone(dataset.lookup('b1',55.75,37.61));self.assertIsNone(dataset.lookup('b2',59.93,30.31))
        for changed in [dict(as_of='2000-01-01'),dict(measure='households'),dict(source='')]:
            with self.assertRaises(ValueError):BuildingPopulationDataset(self.dataset()|changed)
        data=self.dataset();data['buildings']*=2
        with self.assertRaises(ValueError):BuildingPopulationDataset(data)

    def test_regional_import_participates_only_with_complete_building_matches(self):
        analyzer=ResidentialPopulationAnalyzer.__new__(ResidentialPopulationAnalyzer)
        building=dict(id='b1',name='Дом',purpose_name='Жилой дом',point=dict(lat=59.93,lon=30.31),
            floors=dict(ground_count=5),structure_info=dict(apartments_count=100))
        items=[building]
        analyzer._fetch_2gis_buildings_sync=lambda *args:BuildingFetchResult(items,coverage_complete=True)
        analyzer._fetch_yandex_realty_prices_sync=lambda *args:([],[])
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'population.json';path.write_text(json.dumps(self.dataset()),encoding='utf-8')
            with patch.dict('os.environ',POPULATION_DATA_FILE=str(path)):
                report=analyzer.analyze_circle_sync(59.93,30.31)
                self.assertEqual(report.estimated_residents_count,220)
                self.assertTrue(report.source_snapshots['population']['coverage_complete'])
                items.append(copy.deepcopy(building)|dict(id='b2'))
                report=analyzer.analyze_circle_sync(59.93,30.31)
                self.assertIsNone(report.estimated_residents_count)
                self.assertFalse(report.source_snapshots['population']['coverage_complete'])


class RegionalFinanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_card_address_is_qualified_without_weakening_entity_match(self):
        collector=finance_tests.FinanceAcquisitionTests().collector([(INN,'dadata_address_okved')],[])
        collector.dadata.resolve_candidates_for_card=Mock(return_value=[(INN,'dadata_address_okved')])
        card=business_card(address='Тестовая улица, 5',lat=59.93,lng=30.31)
        self.assertEqual(await collector.enrich_cards([card]),0)
        self.assertEqual(collector.dadata.resolve_candidates_for_card.call_args.args[2],'Санкт-Петербург, Тестовая улица, 5')
        self.assertEqual(card.fns_diagnostics['resolution_address'],'Санкт-Петербург, Тестовая улица, 5')

    async def test_conflicting_city_rejects_even_website_inn(self):
        collector=finance_tests.FinanceAcquisitionTests().collector([], [dict(inn=INN,source_url='https://example.test/legal')])
        card=business_card(address='Москва, Тестовая улица, 5',lat=59.93,lng=30.31)
        self.assertEqual(await collector.enrich_cards([card]),0)
        self.assertIsNone(card.fns_revenue_rub)
        self.assertIn('CityAddressConflict',str(card.fns_diagnostics['errors']))


class MetroProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_carries_selected_regional_source_even_without_map_cards(self):
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.model='test'
        analyzer.map_collector=SimpleNamespace(collect_in_circle=AsyncMock(return_value=MapCardsResult([],status='empty')))
        analyzer.population_analyzer=SimpleNamespace(analyze_circle=AsyncMock(return_value=None))
        analyzer.metro_flow_source=SimpleNamespace(diagnostics=dict(source='moscow'),
            get_dataset=AsyncMock(side_effect=AssertionError('Moscow source must not run')))
        regional=SimpleNamespace(diagnostics=dict(status='partial',source='petersburg'),get_dataset=AsyncMock(return_value=None))
        analyzer.regional_metro_sources={'saint_petersburg':regional}
        result=await analyzer.analyze_circle(59.93,30.31,400,'Бизнес',business_concept='По записи')
        self.assertEqual(result.ai_response_metadata['metro_data']['source'],'petersburg')
        regional.get_dataset.assert_awaited_once()
        analyzer.metro_flow_source.get_dataset.assert_not_called()


class DirectBridgeTests(unittest.TestCase):
    def run_request(self, chunks):
        client=Mock();client.recv=Mock(side_effect=chunks)
        upstream=Mock();upstream.recv=Mock(side_effect=[b'HTTP/1.1 200 OK\r\n\r\nOK',b''])
        bridge=_DirectInterfaceBridge.__new__(_DirectInterfaceBridge);bridge.if_index=1
        with patch('real_estate_engine.collectors.base.socket.socket',return_value=upstream), \
                patch('real_estate_engine.collectors.base.socket.gethostbyname',return_value='127.0.0.1'), \
                patch('real_estate_engine.collectors.base.select.select',side_effect=[([upstream],[],[]),([upstream],[],[])]):
            bridge._handle_client(client)
        return client,upstream

    def test_http_fragmented_headers_and_post_body_forwarded_in_origin_form(self):
        client,upstream=self.run_request([b'POST http://example.test/path?q=1 HTTP/1.1\r\nHo',
            b'st: example.test\r\nProxy-Authorization: secret\r\nContent-Length: 3\r\n\r\na=b'])
        upstream.connect.assert_called_once_with(('127.0.0.1',80))
        sent=upstream.sendall.call_args.args[0]
        self.assertTrue(sent.startswith(b'POST /path?q=1 HTTP/1.1\r\n'))
        self.assertTrue(sent.endswith(b'\r\n\r\na=b'));self.assertNotIn(b'Proxy-Authorization',sent)
        self.assertIn(b'200 OK',client.sendall.call_args.args[0])

    def test_https_connect_remains_a_transparent_tunnel(self):
        client,upstream=self.run_request([b'CONNECT example.test:443 HTTP/1.1\r\nHost: example.test\r\n\r\n'])
        upstream.connect.assert_called_once_with(('127.0.0.1',443))
        self.assertIn(b'200 Connection Established',client.sendall.call_args_list[0].args[0])


class MunicipalBoundaryTests(unittest.TestCase):
    def test_spb_reference_covers_all_111_municipalities_with_unique_osm_ids_and_2gis_suffix_aliases(self):
        from real_estate_engine.municipal_population_source import MunicipalPopulationSource, _name
        src=MunicipalPopulationSource(None)
        ref=src._references(SAINT_PETERSBURG)
        self.assertEqual(len(ref['territories']),111)
        self.assertEqual(sum(t['population'] for t in ref['territories']),5652922)
        self.assertEqual(len({t['osm_id'] for t in ref['territories']}),111)
        seen={}
        for t in ref['territories']:
            for key in src._territory_names(t):
                seen.setdefault(key,[]).append(t['name'])
        self.assertEqual({k:v for k,v in seen.items() if len(v)>1},{})
        for raw_2gis,expected in [('Шушары\xa0пос.','посёлок Шушары'),('Пушкин\xa0г.','город Пушкин'),
                                  ('Муниципальный округ №72','муниципальный округ Николаевский'),
                                  ('МО № 15','муниципальный округ Суздальское')]:
            matched=[t['name'] for t in ref['territories'] if _name(raw_2gis) in src._territory_names(t)]
            self.assertEqual(matched,[expected])

    def test_boundary_prefers_explicit_osm_id_lookup_and_requests_russian_names(self):
        from real_estate_engine.municipal_population_source import MunicipalPopulationSource
        src=MunicipalPopulationSource(None)
        polygon={'type':'Polygon','coordinates':[[[30.3,59.9],[30.4,59.9],[30.4,60.0],[30.3,60.0],[30.3,59.9]]]}
        item=dict(osm_type='relation',osm_id=1198055,display_name='Дворцовый округ, Санкт-Петербург',
            lat='59.94',lon='30.32',geojson=polygon)
        src.session=SimpleNamespace(get=Mock(return_value=SimpleNamespace(raise_for_status=lambda:None,json=lambda:[item])))
        found=src._boundary(dict(name='муниципальный округ Дворцовый округ',osm_id=1198055),SAINT_PETERSBURG)
        self.assertEqual(found['osm_id'],1198055)
        call=src.session.get.call_args
        self.assertTrue(call.args[0].endswith('/lookup'))
        self.assertEqual(call.kwargs['params']['osm_ids'],'R1198055')
        self.assertEqual(call.kwargs['params']['accept-language'],'ru')
        src.session.get.reset_mock()
        found_search=src._boundary(dict(name='муниципальный округ Дворцовый округ'),SAINT_PETERSBURG)
        self.assertEqual(found_search['osm_id'],1198055)
        search_call=src.session.get.call_args
        self.assertTrue(search_call.args[0].endswith('/search'))
        self.assertEqual(search_call.kwargs['params']['accept-language'],'ru')
        self.assertEqual(search_call.kwargs['params']['limit'],10)

    def test_invalid_osm_id_in_reference_file_is_rejected(self):
        from real_estate_engine.municipal_population_source import MunicipalPopulationSource
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'bad.json'
            path.write_text(json.dumps(dict(city='saint_petersburg',source='https://example.test/stat',
                as_of=date.today().isoformat(),territories=[dict(name='Округ',population=100,osm_id=-5)])),encoding='utf-8')
            with self.assertRaises(ValueError):MunicipalPopulationSource(None,reference_file=path)._references(SAINT_PETERSBURG)
