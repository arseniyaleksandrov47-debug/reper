"""Territorial mass balance, incomplete data and station/entrance identity."""
import json
import tempfile
import time
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

from real_estate_engine.territory_population import TerritoryPopulationModel, building_identity, contains_point
from real_estate_engine.population_data import BuildingPopulationDataset
from real_estate_engine.population import ResidentialPopulationAnalyzer, population_residents_interval
from real_estate_engine.metro_entrances import parse_entrances, MetroEntranceDataset, MetroEntranceSource, TableRows
from real_estate_engine.metro_data import MetroFlowDataset
from real_estate_engine.traffic import FootTrafficEstimator
from real_estate_engine.route_audience import _matches


def territory():
    return dict(name='Тестовый округ',source='https://statistics.example/population',as_of='2025-01-01',
        population=1000,inventory_source='https://buildings.example/inventory',inventory_as_of='2026-11-07',
        coverage_complete=True,geometry=dict(type='Polygon',coordinates=[[[30,59],[31,59],[31,60],[30,60],[30,59]]]),
        buildings=[dict(building_id='111111_opaque',lat=59.5,lng=30.5,apartments=100,volume=10000),
                   dict(building_id='222222_opaque',lat=59.6,lng=30.6,apartments=300,volume=30000)])


def model(data=None):
    return TerritoryPopulationModel(data or territory(),today=date(2026,11,7))


class AllocationTests(unittest.TestCase):
    def test_full_territory_balance_and_circle_uses_global_denominator(self):
        m=model();whole=m.estimate(['111111','222222']);local=m.estimate(['111111'])
        self.assertEqual((whole['lower'],whole['upper'],whole['point']),(1000,1000,1000))
        self.assertEqual((local['lower'],local['upper'],local['point']),(250,250,250))
        self.assertEqual(local['as_of'],'2025-01-01')
        self.assertTrue(local['modeled']);self.assertIn('not_confidence_interval',local['range_kind'])

    def test_incomplete_inventory_never_allocates_whole_total_to_known_houses(self):
        data=territory();data['coverage_complete']=False
        result=model(data).estimate(['111111'])
        self.assertEqual(result['lower'],0);self.assertEqual(result['upper'],250);self.assertIsNone(result['point'])
        self.assertFalse(result['inventory_complete'])

    def test_missing_building_stays_unknown_instead_of_zero(self):
        result=model().estimate(['not_in_inventory'])
        self.assertEqual((result['lower'],result['upper']),(0,1000));self.assertIsNone(result['point'])

    def test_capacity_scenarios_preserve_sensitivity_instead_of_exact_scalar(self):
        data=territory();data['buildings'][0]['volume']=30000;data['buildings'][1]['volume']=10000
        result=model(data).estimate(['111111'])
        self.assertEqual((result['lower'],result['upper']),(250,750))
        self.assertEqual(result['scenario_results']['apartment_capacity']['point'],250)

    def test_missing_capacity_learns_only_from_local_observed_pairs(self):
        data=territory();data['buildings'][0]['apartments']=None
        self.assertEqual(model(data).estimate(['111111'])['point'],250)
        data['buildings'][0]['volume']=None
        result=model(data).estimate(['111111'])
        self.assertEqual((result['lower'],result['upper']),(0,1000));self.assertIsNone(result['point'])

    def test_missing_volume_can_use_local_inverse_density_without_fixed_coefficient(self):
        data=territory();data['buildings'][0]['volume']=None
        result=model(data).estimate(['111111'])
        self.assertEqual((result['lower'],result['upper']),(250,250))

    def test_duplicate_signed_ids_invalid_geography_and_old_future_statistics_fail(self):
        data=territory();data['buildings'].append(dict(data['buildings'][0],building_id='111111_other_token'))
        with self.assertRaises(ValueError):model(data)
        for field,value in [('as_of','2027-01-01'),('as_of','2020-01-01'),('population',True),('population',-1)]:
            data=territory();data[field]=value
            with self.subTest(field=field,value=value),self.assertRaises(ValueError):model(data)
        data=territory();data['buildings'][0]['lng']=40
        with self.assertRaises(ValueError):model(data)

    def test_polygon_holes_and_multipolygons_do_not_allocate_residents_to_water(self):
        geometry=territory()['geometry'];geometry['coordinates'].append([[30.2,59.2],[30.8,59.2],[30.8,59.8],[30.2,59.8],[30.2,59.2]])
        self.assertFalse(contains_point(geometry,59.5,30.5));self.assertTrue(contains_point(geometry,59.1,30.1))
        self.assertTrue(contains_point(dict(type='MultiPolygon',coordinates=[geometry['coordinates']]),59.1,30.1))

    def test_building_import_matches_stable_id_but_rechecks_position(self):
        dataset=BuildingPopulationDataset(dict(source='test source',as_of=date.today().isoformat(),measure='residents',
            buildings=[dict(building_id='123456_first_signature',lat=59.5,lng=30.5,residents=250)]))
        self.assertEqual(dataset.lookup('123456_second_signature',59.5,30.5),250)
        self.assertIsNone(dataset.lookup('123456_second_signature',59.6,30.6))
        self.assertEqual(building_identity('ordinary_test_id'),'ordinary_test_id')

    def test_municipal_range_only_enters_score_when_local_building_coverage_is_confirmed(self):
        report=SimpleNamespace(buildings_data_available=True,building_coverage_complete=True,malformed_buildings_count=0,
            estimated_residents_count=250,estimated_residents_range=(100,500),population_model=dict(usable=True,modeled=True))
        self.assertEqual(population_residents_interval(report),(100,500))
        report.building_coverage_complete=False
        self.assertIsNone(population_residents_interval(report))
        report.building_coverage_complete=True;report.buildings_without_coordinates_count=1
        self.assertIsNone(population_residents_interval(report))

    def test_radius_rounding_covers_fractional_requested_radius(self):
        analyzer=ResidentialPopulationAnalyzer.__new__(ResidentialPopulationAnalyzer)
        analyzer._2gis_key='test';analyzer._key_refreshed=True
        reply=SimpleNamespace(status_code=200,content=json.dumps(dict(meta=dict(code=200),result=dict(items=[],total=0))).encode())
        analyzer.session=SimpleNamespace(get=Mock(return_value=reply))
        result=analyzer._fetch_2gis_buildings_sync(59.5,30.5,200.2)
        self.assertEqual(analyzer.session.get.call_args.kwargs['params']['radius'],'201')
        self.assertTrue(result.coverage_complete)

    def test_municipal_source_outage_does_not_restart_full_inventory_for_each_property(self):
        from real_estate_engine.municipal_population_source import MunicipalPopulationSource
        from real_estate_engine.cities import SAINT_PETERSBURG
        with tempfile.TemporaryDirectory() as root:
            source=MunicipalPopulationSource(None,cache_dir=root)
            source._build=Mock(side_effect=ValueError('Controlled source outage'))
            reference=dict(source='test source',as_of='2025-01-01');area=dict(name='Test',population=1000)
            for _ in range(2):
                with self.assertRaises(ValueError):source.get_model(reference,area,SAINT_PETERSBURG)
            self.assertEqual(source._build.call_count,1)

    def test_standalone_rating_preserves_population_scenario_bounds(self):
        from real_estate_engine.ranking_policy import RankingCriterion, RankingProfile, evaluate_profile
        report=SimpleNamespace(population=SimpleNamespace(buildings_data_available=True,
            building_coverage_complete=True,malformed_buildings_count=0,estimated_residents_count=250,
            estimated_residents_range=(100,500),population_model=dict(usable=True,modeled=True)))
        profile=RankingProfile(status='ready',criteria=(RankingCriterion('residents_count',1,'higher',0,1000,
            'Важны жители','Проверка диапазона','Условная шкала'),))
        result=evaluate_profile(profile,{},report)
        self.assertEqual((result['lower'],result['upper']),(10,50))
        self.assertEqual(result['components']['residents_count']['value_lower'],100)
        self.assertEqual(result['components']['residents_count']['value_upper'],500)

    def test_integrated_rating_uses_bounds_without_increasing_source_weights(self):
        from test_market_context import environment, integrated_profile
        from real_estate_engine.ranking_policy import evaluate_profile
        report=environment();profile=integrated_profile()
        report.population.population_model=dict(usable=True,modeled=True)
        report.population.estimated_residents_range=(100,5000)
        before={k:v['weight'] for k,v in profile.metadata['market_context']['audience'].items()}
        result=evaluate_profile(profile,dict(price_month=100000),report)
        source=result['business_model']['market_context']['sources']['residents']
        self.assertAlmostEqual(source['index_lower'],100/1100)
        self.assertAlmostEqual(source['index_upper'],5000/6000)
        self.assertTrue(source['population_provenance']['modeled'])
        self.assertEqual(before,{k:v['weight'] for k,v in profile.metadata['market_context']['audience'].items()})


def osm_entrances():
    return dict(elements=[
        dict(type='node',id=1,lat=59.93,lon=30.31,tags=dict(railway='subway_entrance',opening_hours='06:00-00:00')),
        dict(type='node',id=2,lat=59.9305,lon=30.311,tags=dict(railway='subway_entrance')),
        dict(type='relation',id=10,tags=dict(public_transport='stop_area',name='Тестовая'),
             members=[dict(type='node',ref=1,role='entrance'),dict(type='node',ref=2,role='entrance')])])


class EntranceTests(unittest.TestCase):
    def dataset(self,data=None):return MetroEntranceDataset(parse_entrances(data or osm_entrances(),'spb'))

    def test_all_entrances_of_one_station_share_one_flow_without_business_cards(self):
        entrances=self.dataset();groups=entrances.nearby_groups(59.93,30.31)
        self.assertEqual(len(groups),1);self.assertEqual(len(groups[0]['entrances']),2)
        flow=MetroFlowDataset(dict(source='test count',as_of=date.today().isoformat(),bounds=[59,61,29,32],
            measure='entries_per_day',stations={'Тестовая':1000}))
        result=FootTrafficEstimator.estimate_for_circle(59.93,30.31,200,[],metro_dataset=flow,metro_entrances=entrances)
        self.assertEqual(len(result.nearest_metros),1)
        self.assertEqual(result.nearest_metros[0].station_daily_passengers,1000)
        self.assertEqual(len(result.transport_sources),1)
        self.assertEqual(result.transport_sources[0]['entrance_nodes'],['osm:1','osm:2'])
        self.assertEqual(result.transport_sources[0]['mapped_entrances'][0]['opening_hours'],'06:00-00:00')

    def test_unknown_entrance_never_binds_to_nearest_station(self):
        data=osm_entrances();data['elements'].append(dict(type='node',id=3,lat=59.9301,lon=30.3101,tags=dict(railway='subway_entrance')))
        entrances=self.dataset(data);self.assertEqual(len(entrances.nearby_groups(59.93,30.31)),2)
        self.assertEqual(next(v for v in entrances.entrances if v['node']=='osm:3')['station_names'],[])

    def test_explicit_entrance_name_can_bind_to_existing_station_without_proximity_guess(self):
        data=osm_entrances();data['elements'].append(dict(type='node',id=3,lat=59.9301,lon=30.3101,tags=dict(railway='subway_entrance',name='Тестовая')))
        entrances=self.dataset(data);self.assertEqual(len(entrances.nearby_groups(59.93,30.31)),1)
        self.assertEqual(len(entrances.nearby_groups(59.93,30.31)[0]['entrances']),3)
        self.assertEqual(next(v for v in entrances.entrances if v['node']=='osm:3')['station_binding'],'entrance_name_and_station_catalogue')

    def test_known_closed_entrance_is_retained_in_catalog_but_not_used_for_arrival(self):
        data=osm_entrances();data['elements'][0]['tags']['access']='private'
        entrances=self.dataset(data)
        self.assertEqual(len(entrances.entrances),2)
        self.assertEqual([r['node'] for r in entrances.nearby_groups(59.93,30.31)[0]['entrances']],['osm:2'])

    def test_station_center_is_replaced_by_the_mapped_entrance(self):
        card=SimpleNamespace(nearby_metro=[dict(name='Тестовая',coordinates=[30.31,59.93])],nearby_stops=[],building_entrances=[],ratings_count=0)
        data=osm_entrances();data['elements'][0].update(lat=59.935,lon=30.32);data['elements'][1].update(lat=59.936,lon=30.321)
        result=FootTrafficEstimator.estimate_for_circle(59.93,30.31,200,[card],metro_entrances=self.dataset(data))
        self.assertGreater(result.nearest_metros[0].distance_m,500)

    def test_route_binding_uses_exact_node_and_cannot_jump_to_another_exit_with_same_name(self):
        source=dict(kind='metro_exit',source_id='metro:Тестовая',name='Тестовая',entrance_nodes=['osm:1'])
        arrivals=[dict(kind='metro_exit',node='osm:2',name='Тестовая'),dict(kind='metro_exit',node='osm:1',name='Другое имя')]
        result,method=_matches(source,arrivals)
        self.assertEqual([r['node'] for r in result],['osm:1']);self.assertEqual(method,'exact_mapped_entrance_node')

    def test_partial_response_conflicting_nodes_and_expired_snapshot_fail(self):
        with self.assertRaises(ValueError):parse_entrances(dict(elements=[],remark='timeout'),'spb')
        data=osm_entrances();data['elements'].append(dict(data['elements'][0],lat=59.94))
        with self.assertRaises(ValueError):self.dataset(data)
        data=parse_entrances(osm_entrances(),'spb');data['fetched_at']=time.time()-8*86400
        with self.assertRaises(ValueError):MetroEntranceDataset(data)

    def test_timetable_rows_preserve_conditions_and_do_not_assert_a_schedule(self):
        parser=TableRows();parser.feed('<table><tr><td>Тестовая</td><td>05:40<br>По выходным закрыт</td></tr></table>')
        data=parse_entrances(osm_entrances(),'spb',timetable_rows=parser.rows)
        self.assertIn('По выходным закрыт',data['entrances'][0]['official_timetable_rows'][0][1])
        self.assertIn('not automatically asserted',data['metadata']['timetable_interpretation'])

    def test_transfer_stop_area_and_vestibule_suffixes_bind_without_phantom_split(self):
        data=dict(elements=[
            dict(type='node',id=10,lat=59.935,lon=30.334,tags=dict(railway='subway_entrance',name='Невский проспект')),
            dict(type='node',id=11,lat=59.934,lon=30.335,tags=dict(railway='subway_entrance',name='Гостиный двор')),
            dict(type='node',id=12,lat=59.931,lon=30.319,tags=dict(railway='subway_entrance',name='Технологический институт')),
            dict(type='node',id=13,lat=59.932,lon=30.319,tags=dict(railway='subway_entrance',name='Технологический институт')),
            dict(type='node',id=14,lat=59.840,lon=30.410,tags=dict(railway='subway_entrance',name='Дунайская')),
            dict(type='node',id=15,lat=59.841,lon=30.411,tags=dict(railway='subway_entrance',name='Дунайская-1')),
            dict(type='node',id=16,lat=59.842,lon=30.412,tags=dict(railway='subway_entrance')),
            dict(type='relation',id=100,tags=dict(public_transport='stop_area',name='Невский проспект'),
                 members=[dict(type='node',ref=10,role='entrance')]),
            dict(type='relation',id=101,tags=dict(public_transport='stop_area',name='Гостиный двор'),
                 members=[dict(type='node',ref=10,role='entrance'),dict(type='node',ref=11,role='entrance')]),
            dict(type='relation',id=102,tags=dict(public_transport='stop_area',name='Технологический институт 1'),
                 members=[dict(type='node',ref=12,role='entrance')]),
            dict(type='relation',id=103,tags=dict(public_transport='stop_area',name='Технологический институт 2'),
                 members=[dict(type='node',ref=12,role='entrance')]),
            dict(type='relation',id=104,tags=dict(public_transport='stop_area',name='Дунайская'),
                 members=[dict(type='node',ref=14,role='entrance')]),
        ])
        parsed=parse_entrances(data,'spb')
        dataset=MetroEntranceDataset(parsed)
        center_groups={g['name']:g for g in dataset.nearby_groups(59.934,30.334,radius_m=2500)}
        self.assertIn('Невский проспект',center_groups)
        self.assertIn('Гостиный двор',center_groups)
        self.assertIn('Технологический институт',center_groups)
        self.assertEqual([e['node'] for e in center_groups['Невский проспект']['entrances']],['osm:10'])
        self.assertEqual(sorted(e['node'] for e in center_groups['Технологический институт']['entrances']),['osm:12','osm:13'])
        self.assertFalse(any(name.startswith('Вход метро ') and name!='Вход метро osm:16' for name in center_groups))
        south_groups={g['name']:g for g in dataset.nearby_groups(59.841,30.411,radius_m=1200)}
        self.assertEqual(sorted(e['node'] for e in south_groups['Дунайская']['entrances']),['osm:14','osm:15'])
        self.assertIn('Вход метро osm:16',south_groups)


class EntranceFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_import_overrides_a_warm_cache_and_invalid_import_does_not_fall_back(self):
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as root:
            source=MetroEntranceSource(cache_path=root+'/cache.sqlite3',bootstrap_dir=False,
                session=SimpleNamespace(headers={},get=Mock(side_effect=AssertionError('Import does not use network'))))
            source.cache.put('metro_entrances_v1:saint_petersburg',json.dumps(parse_entrances(osm_entrances(),'spb')).encode(),86400)
            data=parse_entrances(osm_entrances(),'spb',source='explicit source');path=Path(root)/'input.json';path.write_text(json.dumps(data),encoding='utf-8')
            with patch.dict('os.environ',METRO_ENTRANCES_DATA_FILE=str(path)):
                self.assertEqual((await source.get_dataset('spb')).source,'explicit source')
                path.write_text('{}',encoding='utf-8')
                self.assertIsNone(await source.get_dataset('spb'))
            source.session.get.assert_not_called()

    async def test_analyzer_discovers_entrances_even_when_no_organizations_were_collected(self):
        from unittest.mock import AsyncMock
        from real_estate_engine.competitors import NvidiaCompetitorAnalyzer,MapCardsResult
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer);analyzer.model='test'
        analyzer.map_collector=SimpleNamespace(collect_in_circle=AsyncMock(return_value=MapCardsResult([],status='empty')))
        analyzer.population_analyzer=SimpleNamespace(analyze_circle=AsyncMock(return_value=None))
        dataset=MetroEntranceDataset(parse_entrances(osm_entrances(),'spb'))
        analyzer.metro_entrance_source=SimpleNamespace(get_dataset=AsyncMock(return_value=dataset),
            diagnostics={'saint_petersburg':dict(status='mapped')})
        report=await analyzer.analyze_circle(59.93,30.31,400,'Описание бизнеса',business_concept='Посещения по записи')
        self.assertEqual(len(report.foot_traffic.nearest_metros),1)
        self.assertEqual(report.foot_traffic.nearest_metros[0].mapped_entrance_count,2)
        self.assertEqual(report.ai_response_metadata['metro_entrances']['status'],'mapped')

    async def test_failed_source_is_cooled_down_and_does_not_become_empty_complete_catalog(self):
        with tempfile.TemporaryDirectory() as root:
            source=MetroEntranceSource(cache_path=root+'/cache.sqlite3',bootstrap_dir=False,
                session=SimpleNamespace(headers={},get=Mock(side_effect=RuntimeError('unavailable'))))
            self.assertIsNone(await source.get_dataset('spb'))
            self.assertIsNone(await source.get_dataset('spb'))
            self.assertEqual(source.session.get.call_count,1)
            self.assertEqual(source.diagnostics['saint_petersburg']['status'],'unavailable')

    async def test_recent_dated_snapshot_survives_refresh_failure_without_changing_acquisition_time(self):
        with tempfile.TemporaryDirectory() as root:
            data=parse_entrances(osm_entrances(),'spb',fetched_at=time.time()-2*86400)
            source=MetroEntranceSource(cache_path=root+'/cache.sqlite3',bootstrap_dir=False,
                session=SimpleNamespace(headers={},get=Mock(side_effect=RuntimeError('unavailable'))))
            source.cache.put('metro_entrances_v1:saint_petersburg',json.dumps(data).encode(),86400)
            result=await source.get_dataset('spb')
            self.assertEqual(result.fetched_at,data['fetched_at'])
            self.assertEqual(source.diagnostics['saint_petersburg']['status'],'stale_snapshot')
            self.assertEqual(source.diagnostics['saint_petersburg']['refresh_error_type'],'RuntimeError')
            self.assertIs(await source.get_dataset('spb'),result)
            self.assertEqual(source.session.get.call_count,1)

    async def test_expired_snapshot_is_not_revived_when_source_fails(self):
        with tempfile.TemporaryDirectory() as root:
            data=parse_entrances(osm_entrances(),'spb',fetched_at=time.time()-8*86400)
            source=MetroEntranceSource(cache_path=root+'/cache.sqlite3',bootstrap_dir=False,
                session=SimpleNamespace(headers={},get=Mock(side_effect=RuntimeError('unavailable'))))
            source.cache.put('metro_entrances_v1:saint_petersburg',json.dumps(data).encode(),86400)
            memory=MetroEntranceDataset(parse_entrances(osm_entrances(),'spb'))
            memory.fetched_at=data['fetched_at']
            source.datasets['saint_petersburg']=memory;source.next_check['saint_petersburg']=time.monotonic()+600
            self.assertIsNone(await source.get_dataset('spb'))

    async def test_expired_bootstrap_snapshot_falls_through_to_live_fetch(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            bootstrap=Path(root)/'bootstrap';bootstrap.mkdir()
            expired=parse_entrances(osm_entrances(),'spb',fetched_at=time.time()-8*86400)
            (bootstrap/'metro_entrances_saint_petersburg.json').write_text(json.dumps(expired),encoding='utf-8')
            live_resp=SimpleNamespace(raise_for_status=lambda:None,content=b'{}',json=lambda:osm_entrances())
            source=MetroEntranceSource(cache_path=root+'/cache.sqlite3',bootstrap_dir=bootstrap,
                session=SimpleNamespace(headers={},get=Mock(return_value=live_resp)))
            result=await source.get_dataset('spb')
            self.assertIsNotNone(result)
            self.assertEqual(len(result.entrances),2)
            self.assertEqual(source.diagnostics['saint_petersburg']['data_origin'],'live')
            self.assertEqual(source.session.get.call_count,1)

    async def test_overpass_mirror_fallback_handles_request_error_and_invalid_json(self):
        import requests
        with tempfile.TemporaryDirectory() as root:
            bad_json=SimpleNamespace(raise_for_status=lambda:None,content=b'<html>502</html>',json=Mock(side_effect=ValueError('bad json')))
            ok_resp=SimpleNamespace(raise_for_status=lambda:None,content=b'{}',json=lambda:osm_entrances())
            source=MetroEntranceSource(cache_path=root+'/cache.sqlite3',bootstrap_dir=False,
                session=SimpleNamespace(headers={},get=Mock(side_effect=[requests.RequestException('timeout'),bad_json,ok_resp])))
            result=await source.get_dataset('spb')
            self.assertIsNotNone(result)
            self.assertEqual(result.source,'https://overpass-api.de/api/interpreter')
            self.assertEqual(source.session.get.call_count,3)


class BridgeDeadlineTests(unittest.TestCase):
    def test_response_after_fifteen_seconds_is_not_cut_off_by_shorter_bridge_idle_timeout(self):
        from unittest.mock import patch
        from real_estate_engine.collectors.base import _DirectInterfaceBridge
        client=Mock();client.recv.return_value=b'CONNECT example.test:443 HTTP/1.1\r\nHost: example.test\r\n\r\n'
        upstream=Mock();upstream.recv.side_effect=[b'delayed response',b'']
        def select(reads,writes,errors,timeout):
            return ([upstream],[],[]) if timeout>=15 else ([],[],[])
        bridge=_DirectInterfaceBridge.__new__(_DirectInterfaceBridge);bridge.if_index=1
        with patch('real_estate_engine.collectors.base.socket.socket',return_value=upstream), \
                patch('real_estate_engine.collectors.base.socket.gethostbyname',return_value='127.0.0.1'), \
                patch('real_estate_engine.collectors.base.select.select',side_effect=select):
            bridge._handle_client(client)
        self.assertIn(b'delayed response',[call.args[0] for call in client.sendall.call_args_list])


class PresentationTests(unittest.TestCase):
    def test_cli_exposes_modeled_range_and_reference_date(self):
        from run_engine import resident_label
        label=resident_label(SimpleNamespace(estimated_residents_count=250,estimated_residents_range=(100.2,500.1),
            population_model=dict(usable=True,territories=[dict(as_of='2025-01-01')])))
        self.assertIn('100–501',label);self.assertIn('модель',label);self.assertIn('2025-01-01',label)

    def test_cli_does_not_call_unspecified_operator_flow_a_single_line_observation(self):
        from run_engine import metro_flow_label
        from real_estate_engine.traffic import MetroStationFlow
        station=MetroStationFlow('Тестовая',100,None,None,station_daily_passengers_range=(0,1000),
            passenger_measure='passengers_per_day_unspecified',location_basis='mapped_entrance')
        label=metro_flow_label(station)
        self.assertIn('до нанесённого входа',label);self.assertIn('разделение входа/выхода',label)
        self.assertNotIn('одной линии',label)

    def test_cli_exposes_route_conditioned_and_open_ended_transport_ranges(self):
        import contextlib, io
        from run_engine import foot_traffic_daily_label, print_detailed_location_report
        ft=FootTrafficEstimator.estimate_for_circle(59.93,30.31,200,[])
        ft.estimated_daily_pedestrians=None;ft.estimated_weekday_daily=None;ft.estimated_weekend_daily=None
        ft.estimated_avg_hourly_daytime=None;ft.estimated_peak_hourly=None
        ft.transport_data_status='route_complete';ft.transport_lower_daily=174;ft.transport_upper_daily=3261
        self.assertEqual(foot_traffic_daily_label(ft),'174–3,261 (тр.)')
        prop=dict(source='domclick',title='Помещение',address='Невский пр., 1',area_sqm=50,
                  price_month=100000,price_sqm_year=24000,lat=59.93,lng=30.31,url='https://example.test')
        rep=SimpleNamespace(model_used='m',map_fetch_time_ms=10,ai_inference_time_ms=10,fns_fetch_time_ms=10,
            fns_enriched_count=0,total_cards_found=0,collection_status='ok',data_status='ok',ai_summary='ok',
            data_warnings=[],foot_traffic=ft,population=None,direct_competitors=[],indirect_competitors=[],
            synergy_businesses=[])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            print_detailed_location_report(prop,rep,'Кофейня',400)
        self.assertIn('Транспортная составляющая (маршрутная): 174–3,261/сут',out.getvalue())
        ft.transport_data_status='route_partial';ft.transport_upper_daily=None
        self.assertEqual(foot_traffic_daily_label(ft),'от 174 (тр.)')
        with contextlib.redirect_stdout(io.StringIO()) as out2:
            print_detailed_location_report(prop,rep,'Кофейня',400)
        self.assertIn('Транспортная составляющая (маршрутная): от 174/сут (верхняя граница открыта)',out2.getvalue())


class PopulationSourceBugFixTests(unittest.TestCase):
    def test_multipolygon_and_polygon_holes_compute_net_footprint_area(self):
        from real_estate_engine.population import ResidentialPopulationAnalyzer
        outer="30.2800 59.9400, 30.2810 59.9400, 30.2810 59.9410, 30.2800 59.9410, 30.2800 59.9400"
        hole="30.2802 59.9402, 30.2808 59.9402, 30.2808 59.9408, 30.2802 59.9408, 30.2802 59.9402"
        wing="30.2820 59.9400, 30.2825 59.9400, 30.2825 59.9405, 30.2820 59.9405, 30.2820 59.9400"
        solid_area=ResidentialPopulationAnalyzer._polygon_area_sqm(f"POLYGON(({outer}))",59.94)
        holed_area=ResidentialPopulationAnalyzer._polygon_area_sqm(f"POLYGON(({outer}), ({hole}))",59.94)
        wing_area=ResidentialPopulationAnalyzer._polygon_area_sqm(f"POLYGON(({wing}))",59.94)
        multi_area=ResidentialPopulationAnalyzer._polygon_area_sqm(f"MULTIPOLYGON((({outer}), ({hole})), (({wing})))",59.94)
        self.assertGreater(solid_area,5000.0)
        self.assertAlmostEqual(holed_area,solid_area*0.64,delta=5.0)
        self.assertAlmostEqual(multi_area,holed_area+wing_area,delta=2.0)

    def test_2gis_item_not_found_empty_tile_and_disjoint_tile_pruning(self):
        from real_estate_engine.cities import SAINT_PETERSBURG
        from real_estate_engine.municipal_population_source import MunicipalPopulationSource
        from real_estate_engine.population import BuildingFetchResult, ResidentialPopulationAnalyzer

        analyzer=ResidentialPopulationAnalyzer.__new__(ResidentialPopulationAnalyzer)
        analyzer._2gis_key='fixture';analyzer._key_refreshed=True
        empty_404=json.dumps({"meta":{"code":404,"error":{"type":"itemNotFound","message":"Results not found"}}}).encode()
        analyzer.session=SimpleNamespace(get=lambda *a,**k:SimpleNamespace(status_code=200,content=empty_404))
        empty_res=analyzer._fetch_2gis_buildings_sync(59.94,30.28,300)
        self.assertEqual(len(empty_res),0)
        self.assertTrue(empty_res.coverage_complete)
        self.assertEqual(empty_res.warnings,[])

        poly={"type":"Polygon","coordinates":[[[30.28,59.94],[30.30,59.94],[30.30,59.95],[30.29,59.95],[30.29,59.96],[30.28,59.96],[30.28,59.94]]]}
        queried=[]
        def fake_fetch(lat,lng,radius):
            queried.append((lat,lng,radius))
            return BuildingFetchResult([],coverage_complete=True)

        analyzer._fetch_2gis_buildings_sync=fake_fetch
        with tempfile.TemporaryDirectory() as root:
            src=MunicipalPopulationSource(analyzer,cache_dir=root)
            src._boundary=lambda *a,**k:{"osm_id":123,"boundingbox":["59.94","59.96","30.28","30.30"],"geojson":poly}
            data=src._build({"source":"s","as_of":"2026-01-01"},{"name":"МО Тест","population":1000},SAINT_PETERSBURG)
        self.assertTrue(data['coverage_complete'])
        self.assertFalse(any(lat>59.955 and lng>30.295 for lat,lng,_ in queried))
