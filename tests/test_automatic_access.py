"""Point 1 acceptance: acquired topology, mapped entries and dated metro data."""
import asyncio
import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from real_estate_engine.osm_access import parse_osm_map,assess_mapped_access,OSMPedestrianProvider,attach_automatic_access,_address_match
from real_estate_engine.mos_metro_source import build_metro_dataset,MosMetroFlowSource
from real_estate_engine.accessibility import resolve_access_route,PedestrianNetwork
from test_engine_regressions import candidate
from test_engine_regressions import business_card,LAT,LNG,memory_storage,property_offer
from test_market_context import integrated_profile,market_config,environment,CONCEPT
from real_estate_engine import NvidiaCompetitorAnalyzer,OptimalPropertyRecommender
from real_estate_engine.competitors import MapCardsResult
from real_estate_engine.metro_data import MetroFlowDataset
from floor_ai_fixtures import FixtureFloorAnalyzer


def osm_xml(*,private=False,disconnected=False,second_entrance=False,one_way=False):
    barrier='<tag k="barrier" v="gate"/><tag k="access" v="private"/>' if private else ''
    refs='<nd ref="1"/><nd ref="2"/><nd ref="3"/>' if not disconnected else '<nd ref="1"/><nd ref="2"/>'
    second='<node id="5" lat="55.7502" lon="37.6102"><tag k="entrance" v="main"/></node>' if second_entrance else ''
    second_ref='<nd ref="5"/>' if second_entrance else ''
    direction='<tag k="oneway:foot" v="-1"/>' if one_way else ''
    return (f'<osm><node id="1" lat="55.75" lon="37.61"><tag k="railway" v="subway_entrance"/></node>'
        f'<node id="2" lat="55.7501" lon="37.61">{barrier}</node>'
        '<node id="3" lat="55.7502" lon="37.61"><tag k="entrance" v="main"/></node>'
        '<node id="4" lat="55.7502" lon="37.6101"/>'+second+
        f'<way id="10">{refs}<tag k="highway" v="footway"/>{direction}</way>'
        f'<way id="20"><nd ref="3"/><nd ref="4"/>{second_ref}<nd ref="3"/><tag k="building" v="yes"/>'
        '<tag k="addr:street" v="Тестовая улица"/><tag k="addr:housenumber" v="5 к1"/></way></osm>').encode()


def prop():return candidate(address='Москва, Тестовая улица, 5 к1',lat=55.7502,lng=37.61)


def metadata(count):
    return dict(generalInfo=dict(publicationStatusTagId='published'),
        actualData=dict(versionNum=1,releaseNum=36,releaseOpenDate='23.10.2026 00:00:38'),
        version=dict(publicationCatalogId=115426),release=dict(cntObjects=count))


def observation(i=1,name='Тестовая',year=2026,quarter='III квартал',incoming=92000,line='Тестовая линия'):
    return dict(global_id=i,is_deleted=0,NameOfStation=name,Line=line,Year=year,Quarter=quarter,
                IncomingPassengers=incoming,OutgoingPassengers=9999999)


class TopologyTests(unittest.TestCase):
    def snapshot(self,**kw):return parse_osm_map(osm_xml(**kw),'Synthetic test OSM map',fetched_at='2026-11-04')

    def test_connected_address_entry_and_metro_generate_a_scoped_route(self):
        evidence=assess_mapped_access(self.snapshot(),prop())
        route=resolve_access_route(evidence,'walk')
        self.assertEqual(route['status'],'reachable')
        self.assertGreater(route['minutes'],0)
        self.assertEqual(route['scope'],'building_entrance')
        self.assertFalse(evidence['accessibility_diagnostics']['unit_entrance_verified'])

    def test_unconnected_entry_never_gets_a_line_to_nearest_street(self):
        evidence=assess_mapped_access(self.snapshot(disconnected=True),prop())
        self.assertNotIn('access_routes',evidence)
        self.assertEqual(evidence['accessibility_diagnostics']['missing_reason'],'entrances_not_connected_to_walk_network')

    def test_private_gate_and_pedestrian_direction_are_respected(self):
        for kw in [dict(private=True),dict(one_way=True)]:
            evidence=assess_mapped_access(self.snapshot(**kw),prop())
            self.assertNotIn('access_routes',evidence)
            self.assertEqual(evidence['accessibility_diagnostics']['route_candidates'][0]['status'],'unknown')

    def test_same_house_in_a_different_street_is_not_matched(self):
        wrong=prop();wrong['address']='Москва, Другая улица, 5 к1'
        result=assess_mapped_access(self.snapshot(),wrong)
        self.assertEqual(result['accessibility_diagnostics']['building_matches'],0)

    def test_corpus_structure_and_street_order_are_normalized_without_fuzzy_neighbours(self):
        self.assertTrue(_address_match({'addr:street':'улица Большая Академическая','addr:housenumber':'5 с3'},'Москва, Большая Академическая улица, 5 ст3'))
        self.assertFalse(_address_match({'addr:street':'Тестовая улица','addr:housenumber':'5 к2'},prop()['address']))

    def test_multiple_entrances_remain_explicitly_ambiguous(self):
        evidence=assess_mapped_access(self.snapshot(second_entrance=True),prop())
        self.assertEqual(len(evidence['accessibility_diagnostics']['entrance_candidates']),2)
        self.assertNotIn('access_routes',evidence)

    def test_supplied_graph_is_not_mixed_with_an_automatic_route(self):
        automatic=assess_mapped_access(self.snapshot(),prop())
        old=prop();old['raw_attributes']={'business_evidence':{'pedestrian_network':{'marker':'supplied'}}}
        enriched=attach_automatic_access(old,automatic)
        self.assertEqual(enriched['raw_attributes']['business_evidence']['pedestrian_network'],{'marker':'supplied'})
        self.assertNotIn('access_routes',enriched['raw_attributes']['business_evidence'])
        self.assertEqual(old['raw_attributes']['business_evidence'],{'pedestrian_network':{'marker':'supplied'}})

    def test_malformed_external_map_is_not_accepted(self):
        for body in [b'<html>Error</html>',b'<!DOCTYPE osm [<!ENTITY x "y">]><osm/>']:
            with self.assertRaises(ValueError):parse_osm_map(body,'test')


class MetroConversionTests(unittest.TestCase):
    def test_latest_completed_quarter_uses_actual_days_and_entries_only(self):
        rows=[observation(),observation(2,'Другая',incoming=184000),observation(3,'Тестовая',year=2025,incoming=99999999)]
        data,info=build_metro_dataset(metadata(3),rows,today=date(2026,11,4))
        self.assertEqual(data['stations']['Тестовая'],1000)
        self.assertEqual(data['as_of'],'2026-09-30');self.assertEqual(info['publication_date'],'2026-10-23')
        self.assertEqual(info['days_in_period'],92);self.assertEqual(info['measure'],'IncomingPassengers / calendar days in quarter')

    def test_leap_year_and_cross_line_name_ambiguity(self):
        rows=[observation(year=2024,quarter='I квартал',incoming=91000),
              observation(2,'Двойная',year=2024,quarter='I квартал'),
              observation(3,'Двойная',year=2024,quarter='I квартал',line='Другая линия')]
        data,info=build_metro_dataset(metadata(3),rows,today=date(2026,11,4))
        self.assertEqual(info['days_in_period'],91);self.assertEqual(data['stations']['Тестовая'],1000)
        self.assertNotIn('Двойная',data['stations']);self.assertIn('двойная',info['ambiguous_station_names'])

    def test_incomplete_or_invalid_observations_do_not_get_fabricated_flow(self):
        for row in [observation(incoming=None),observation(incoming=-1),observation(quarter='summer')]:
            with self.assertRaises(ValueError):build_metro_dataset(metadata(1),[row],today=date(2026,11,4))


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'sources.sqlite3'

    async def test_automatic_provider_failure_is_reported_as_unknown(self):
        import requests
        def broken(*args,**kwargs):raise requests.Timeout('Controlled source timeout')
        provider=OSMPedestrianProvider(session=SimpleNamespace(headers={},get=broken),cache_path=self.path)
        result=await provider.enrich(prop())
        self.assertEqual(result['accessibility_diagnostics']['status'],'unavailable')
        self.assertEqual(resolve_access_route(result,'walk')['status'],'unknown')

    async def test_map_acquisition_is_cached_and_failures_stay_unknown(self):
        calls=[]
        def get(*a,**kw):
            calls.append(1)
            return SimpleNamespace(content=osm_xml(),raise_for_status=lambda:None)
        provider=OSMPedestrianProvider(session=SimpleNamespace(headers={},get=get),cache_path=self.path)
        first=await provider.enrich(prop());second=await provider.enrich(prop())
        self.assertEqual(len(calls),1)
        self.assertFalse(first['accessibility_diagnostics']['cache_hit']);self.assertTrue(second['accessibility_diagnostics']['cache_hit'])
        self.assertIn('access_routes',second)

    async def test_catalog_is_paged_completely_and_new_instance_uses_disk_cache(self):
        now=date.today();index=now.year*4+(now.month-1)//3-1;year,quarter=index//4,index%4+1
        calls=[];rows=[observation(i+1,'Станция '+str(i),year=year,quarter=('I','II','III','IV')[quarter-1]+' квартал') for i in range(1001)]
        def get(*args,**kwargs):
            calls.append('metadata');return SimpleNamespace(raise_for_status=lambda:None,json=lambda:metadata(len(rows)))
        def post(*args,**kwargs):
            request=kwargs['json'];calls.append(request['offset'])
            self.assertEqual(request['sorting'],{'global_id':'asc'})
            page=rows[request['offset']:request['offset']+request['limit']]
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:dict(status=0,response=page))
        session=SimpleNamespace(get=get,post=post)
        source=MosMetroFlowSource(session=session,cache_path=self.path)
        dataset=await source.get_dataset()
        self.assertIsNotNone(dataset);self.assertEqual(calls,['metadata',0,1000,'metadata'])
        other=MosMetroFlowSource(session=session,cache_path=self.path)
        self.assertIsNotNone(await other.get_dataset());self.assertEqual(other.diagnostics['cache_hit'],True)
        self.assertEqual(len(calls),4)

    async def test_catalog_ignoring_period_filter_cannot_be_cached_as_complete(self):
        session=SimpleNamespace(get=lambda *a,**k:SimpleNamespace(raise_for_status=lambda:None,json=lambda:metadata(2)),
            post=lambda *a,**k:SimpleNamespace(raise_for_status=lambda:None,json=lambda:dict(status=0,response=[observation(year=2021)])))
        source=MosMetroFlowSource(session=session,cache_path=self.path)
        self.assertIsNone(await source.get_dataset());self.assertEqual(source.diagnostics['status'],'unavailable')
        self.assertFalse(self.path.exists())


class PipelineConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_analyzer_automatically_uses_the_dated_source(self):
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer);analyzer.model='fixture'
        analyzer._call_nvidia_sync=lambda *a,**k:(json.dumps(dict(categories=[dict(idx=1,level='direct',reason='Контроль')],summary='Контроль')),'fixture')
        async def maps(**kw):return MapCardsResult([business_card(nearby_metro=[dict(name='Тестовая',coordinates=[LNG,LAT])])],status='ok')
        async def population(**kw):return None
        data,_=build_metro_dataset(metadata(1),[observation()],today=date(2026,11,4))
        dataset=MetroFlowDataset(data,today=date(2026,11,4));calls=[]
        async def dated():calls.append(1);return dataset
        analyzer.map_collector=SimpleNamespace(collect_in_circle=maps)
        analyzer.population_analyzer=SimpleNamespace(analyze_circle=population)
        analyzer.metro_flow_source=SimpleNamespace(get_dataset=dated,diagnostics={'status':'available'})
        result=await analyzer.analyze_circle(LAT,LNG,400,'Контроль',enrich_fns=False,business_concept='Приём по записи.')
        self.assertEqual(calls,[1]);self.assertEqual(result.foot_traffic.nearest_metros[0].passenger_as_of,'2026-09-30')
        self.assertGreater(result.foot_traffic.metro_contribution_daily,0)

    async def test_public_search_attaches_automatic_network_before_scoring(self):
        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self,**kw):return environment()
        analyzer=Analyzer();requests=[]
        automatic=assess_mapped_access(parse_osm_map(osm_xml(),'Synthetic map',fetched_at='2026-11-04'),prop())
        async def enrich(item):requests.append(item['address']);return automatic
        analyzer.pedestrian_provider=SimpleNamespace(enrich=enrich)
        async def finance(cards):return 0
        analyzer.fns_collector=SimpleNamespace(enrich_cards=finance)
        store,conn=memory_storage();self.addCleanup(conn.close)
        store.upsert_property(property_offer(address=prop()['address'],description='Первый этаж, отдельный вход с улицы.'))
        engine=OptimalPropertyRecommender(store,analyzer)
        cfg=market_config();cfg['access']=dict(kind='walk',weight=.5,reference_minutes=15,
            concept_quote=CONCEPT,reason='Контролируемая важность маршрута')
        async def resolve(*args,**kw):return integrated_profile(cfg)
        engine.ranking_profile_resolver.resolve=resolve
        result=await engine.find_optimal_properties('Контроль',business_concept=CONCEPT,
            center_lat=LAT,center_lng=LNG,search_radius_m=500)
        self.assertEqual(len(requests),1);self.assertEqual(result.evaluated_count,1)
        trace=result.ranked_candidates[0].business_model_diagnostics['market_context']['sources']['walk_time_minutes']
        self.assertEqual(trace['route_status'],'reachable');self.assertEqual(trace['route_evidence']['scope'],'building_entrance')


if __name__=='__main__':unittest.main()
