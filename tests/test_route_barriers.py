"""Mapped obstacles, uncertain crossing waits and their effect on ranking."""
import copy
import unittest
from datetime import date
from xml.sax.saxutils import quoteattr
from types import SimpleNamespace

from real_estate_engine.osm_access import parse_osm_map,assess_mapped_access
from real_estate_engine.accessibility import PedestrianNetwork,resolve_access_route
from real_estate_engine.route_audience import condition_transport_on_routes
from real_estate_engine.traffic import FootTrafficEstimator
from real_estate_engine.metro_data import MetroFlowDataset
from real_estate_engine.ranking_policy import evaluate_profile
from test_engine_regressions import LAT,LNG,business_card,candidate
from test_market_context import market_config,integrated_profile,environment,CONCEPT
from test_engine_regressions import memory_storage,property_offer
from floor_ai_fixtures import FixtureFloorAnalyzer
from real_estate_engine import OptimalPropertyRecommender


def tags(data):return ''.join('<tag k='+quoteattr(k)+' v='+quoteattr(str(v))+'/>' for k,v in data.items())


def crossing_map(kind='railway',*,crossing=None,foot_tags=None,detour=False,shared=True):
    crossings={'railway':{'railway':'rail'},'road':{'highway':'primary'},'barrier':{'barrier':'fence'},'water':{'waterway':'river'}}
    points={1:(-100,0,{'railway':'subway_entrance','name':'Тестовая'}),2:(0,0,crossing or {}),
        3:(100,0,{'entrance':'main'}),4:(0,-50,{}),5:(0,50,{}),6:(110,10,{}),7:(110,-10,{}),
        8:(-100,70,{}),9:(100,70,{})}
    xml='<osm>'
    for node,(x,y,attributes) in points.items():
        xml+=f'<node id="{node}" lat="{LAT+y/111320}" lon="{LNG+x/(111320*.562)}">{tags(attributes)}</node>'
    def way(identifier,refs,attributes):
        return f'<way id="{identifier}">'+''.join(f'<nd ref="{n}"/>' for n in refs)+tags(attributes)+'</way>'
    xml+=way(10,[1,2,3] if shared else [1,3],dict(highway='footway',**(foot_tags or {})))
    xml+=way(11,[4,2,5],crossings[kind])
    if detour:xml+=way(12,[1,8,9,3],dict(highway='footway'))
    xml+=way(20,[3,6,7,3],dict(building='yes',**{'addr:street':'Тестовая улица','addr:housenumber':'1'}))
    return (xml+'</osm>').encode()


def route_for(body):
    snapshot=parse_osm_map(body,'Synthetic barrier-map fixture',fetched_at='2026-11-05')
    evidence=assess_mapped_access(snapshot,candidate(address='Москва, Тестовая улица, 1'))
    return resolve_access_route(evidence,'walk'),evidence


def transport_traffic():
    dataset=MetroFlowDataset(dict(source='Synthetic dated metro-flow fixture',as_of='2026-11-01',
        bounds=[55,57,36,39],measure='entries_per_day',stations={'Тестовая':100000}),today=date(2026,11,5))
    return FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,
        [business_card(nearby_metro=[dict(name='Тестовая',coordinates=[LNG,LAT])])],metro_dataset=dataset)


def route_evidence(minutes=2,*,complete=True,connected=True,wait_upper=0):
    origin=dict(node='metro',verified=True,source='Synthetic mapped metro exit',kind='metro_exit',name='Тестовая')
    entry=dict(node='entry',verified=True,source='Synthetic checked unit entrance')
    edges=[dict(**{'from':'metro','to':'entry'},minutes=minutes,walk_allowed=True,bidirectional=True,
                wait_minutes_lower=0,wait_minutes_upper=wait_upper)] if connected else []
    return dict(pedestrian_network=dict(source='Synthetic pedestrian network',as_of='2026-11-05',
        coverage_complete=complete,nodes=['metro','entry'],edges=edges),
        access_routes=dict(walk=dict(origin=origin,entry=entry,scope='unit_entrance')))


class GeometricCrossingTests(unittest.TestCase):
    def test_unmarked_railway_crossing_is_not_a_path_even_with_shared_node(self):
        route,evidence=route_for(crossing_map())
        self.assertEqual(route['status'],'unknown')
        self.assertTrue(evidence['accessibility_diagnostics']['barrier_diagnostics']['rejected_crossings'])

    def test_unmarked_road_fence_and_river_crossings_are_not_paths(self):
        for kind in ('road','barrier','water'):
            with self.subTest(kind=kind):self.assertEqual(route_for(crossing_map(kind))[0]['status'],'unknown')

    def test_intersecting_lines_without_shared_crossing_never_get_connected(self):
        route,_=route_for(crossing_map(crossing={'railway':'crossing'},shared=False))
        self.assertEqual(route['status'],'unknown')

    def test_explicit_rail_crossing_is_reachable_but_wait_is_not_invented(self):
        route,_=route_for(crossing_map(crossing={'railway':'crossing'}))
        self.assertEqual(route['status'],'reachable');self.assertIsNone(route['minutes'])
        self.assertGreater(route['minutes_lower'],0);self.assertIsNone(route['minutes_upper'])

    def test_explicit_road_crossing_is_used_and_traffic_signal_wait_is_bounded(self):
        route,_=route_for(crossing_map('road',crossing={'highway':'crossing','crossing':'traffic_signals'}))
        self.assertEqual(route['status'],'reachable');self.assertIsNone(route['minutes_upper'])
        self.assertIsNone(route['minutes'])

    def test_forbidden_road_crossing_stays_unknown(self):
        route,_=route_for(crossing_map('road',crossing={'highway':'crossing','crossing':'no'}))
        self.assertEqual(route['status'],'unknown')

    def test_mapped_bridge_or_tunnel_can_cross_a_barrier_at_another_level(self):
        for attributes in ({'bridge':'yes','layer':'1'},{'tunnel':'yes','layer':'-1'}):
            for kind in ('railway','road','barrier','water'):
                with self.subTest(attributes=attributes,kind=kind):
                    route,_=route_for(crossing_map(kind,foot_tags=attributes))
                    self.assertEqual(route['status'],'reachable');self.assertGreater(route['minutes'],0)

    def test_bridge_tag_without_a_different_declared_level_does_not_override_crossing(self):
        route,_=route_for(crossing_map(foot_tags={'bridge':'yes','layer':'0'}))
        self.assertEqual(route['status'],'unknown')

    def test_accessible_gate_is_used_but_locked_or_conditional_gates_are_not(self):
        for changes,expected in (({},'reachable'),({'locked':'yes'},'unknown'),
                                 ({'opening_hours':'Mo-Fr 09:00-18:00'},'unknown'),
                                 ({'foot:conditional':'no @ (22:00-06:00)'},'unknown')):
            route,_=route_for(crossing_map('barrier',crossing=dict(barrier='gate',foot='yes',**changes)))
            self.assertEqual(route['status'],expected)

    def test_alley_detour_is_found_instead_of_crossing_railway(self):
        direct,_=route_for(crossing_map(foot_tags={'bridge':'yes','layer':'1'}))
        detour,evidence=route_for(crossing_map(detour=True))
        self.assertEqual(detour['status'],'reachable');self.assertGreater(detour['minutes'],direct['minutes'])
        self.assertTrue(evidence['accessibility_diagnostics']['barrier_diagnostics']['rejected_crossings'])

    def test_shared_interior_bridge_node_cannot_jump_to_ground_level_path(self):
        body=crossing_map('road',foot_tags={'bridge':'yes','layer':'1'})
        body=body.replace(tags({'highway':'primary'}).encode(),tags({'highway':'footway'}).encode())
        snapshot=parse_osm_map(body,'Synthetic grade-separated walkways')
        graph=PedestrianNetwork(snapshot['network'])
        point=lambda n:dict(node='osm:'+str(n),verified=True,source='Synthetic mapped point')
        self.assertEqual(graph.travel(point(1),point(3)).status,'reachable')
        self.assertEqual(graph.travel(point(1),point(4)).status,'unknown')

    def test_named_subway_stop_area_binds_exit_without_nearest_station_guess(self):
        body=crossing_map(foot_tags={'bridge':'yes','layer':'1'})
        body=body.replace(tags({'railway':'subway_entrance','name':'Тестовая'}).encode(),tags({'railway':'subway_entrance'}).encode())
        extra=(f'<node id="99" lat="{LAT}" lon="{LNG}">'+tags({'railway':'station','station':'subway'})+'</node>'
               '<relation id="77"><member type="node" ref="99" role="stop"/><member type="node" ref="1" role="entrance"/>'
               +tags({'public_transport':'stop_area','name':'Тестовая'})+'</relation>')
        body=body.replace(b'</osm>',extra.encode()+b'</osm>')
        _,evidence=route_for(body)
        self.assertIn('Тестовая',evidence['accessibility_diagnostics']['arrivals'][0]['station_names'])

    def test_multipolygon_building_address_is_bound_to_its_outer_entrance(self):
        body=crossing_map(foot_tags={'bridge':'yes','layer':'1'})
        old=tags(dict(building='yes',**{'addr:street':'Тестовая улица','addr:housenumber':'1'})).encode()
        body=body.replace(old,b'')
        relation=('<relation id="77"><member type="way" ref="20" role="outer"/>'+tags(
            dict(type='multipolygon',building='yes',**{'addr:street':'Тестовая улица','addr:housenumber':'1'}))+'</relation>')
        body=body.replace(b'</osm>',relation.encode()+b'</osm>')
        route,evidence=route_for(body)
        self.assertEqual(route['status'],'reachable')
        self.assertEqual(evidence['accessibility_diagnostics']['matched_buildings'][0]['osm_type'],'relation')
        self.assertIn('/relation/',evidence['access_routes']['walk']['entry']['building'])


class WaitingBoundsTests(unittest.TestCase):
    def test_known_delay_enters_time_exactly_once(self):
        evidence=route_evidence(2,wait_upper=3)
        route=resolve_access_route(evidence,'walk')
        self.assertEqual((route['minutes_lower'],route['minutes_upper']),(2,5))
        self.assertIsNone(route['minutes'])

    def test_unknown_delay_does_not_make_a_guaranteed_three_minute_isochrone(self):
        evidence=route_evidence(2,wait_upper=None)
        network=PedestrianNetwork(evidence['pedestrian_network'])
        area=network.isochrone(evidence['access_routes']['walk']['origin'],3)
        self.assertIn('entry',area['nodes']);self.assertNotIn('entry',area['guaranteed_nodes'])

    def test_verified_measurement_may_resolve_wait_within_bounds_but_not_conflict(self):
        evidence=route_evidence(2,wait_upper=None)
        evidence['walk_time_minutes']=dict(value=4,verified=True,source='Synthetic measured approach')
        self.assertEqual(resolve_access_route(evidence,'walk')['minutes'],4)
        evidence['walk_time_minutes']['value']=1
        self.assertEqual(resolve_access_route(evidence,'walk')['status'],'conflict')

    def test_many_origin_routes_preserve_direction_and_wait_bounds(self):
        evidence=route_evidence(2,complete=False,wait_upper=None)
        evidence['pedestrian_network']['nodes'].append('isolated')
        evidence['pedestrian_network']['edges'][0]['bidirectional']=False
        network=PedestrianNetwork(evidence['pedestrian_network'])
        endpoint=lambda n:dict(node=n,verified=True,source='Synthetic graph point')
        routes=network.travel_many_to([endpoint('metro'),endpoint('isolated'),endpoint('missing')],endpoint('entry'))
        self.assertEqual([r.status for r in routes],['reachable','unknown','unknown'])
        self.assertEqual(routes[0].minutes_lower,2);self.assertIsNone(routes[0].minutes_upper)
        self.assertEqual(network.travel_many_to([endpoint('entry')],endpoint('metro'))[0].status,'unknown')


class RouteAudienceTests(unittest.TestCase):
    def test_longer_mapped_route_reduces_audience_with_same_geodesic_distance(self):
        original=transport_traffic()
        near=condition_transport_on_routes(original,route_evidence(2))
        detour=condition_transport_on_routes(original,route_evidence(12))
        self.assertGreater(near.transport_lower_daily,detour.transport_lower_daily)
        self.assertGreater(near.transport_upper_daily,detour.transport_upper_daily)
        self.assertFalse(original.transport_route_diagnostics['applied'])
        self.assertIsNone(near.estimated_daily_pedestrians)

    def test_missing_map_does_not_reward_nearby_station(self):
        traffic=condition_transport_on_routes(transport_traffic(),{})
        self.assertEqual(traffic.transport_lower_daily,0)
        self.assertGreater(traffic.transport_upper_daily,0)
        self.assertEqual(traffic.transport_source_coverage,0)

    def test_complete_disconnection_and_partial_map_remain_distinct(self):
        for complete,status in ((True,'unreachable'),(False,'unknown')):
            traffic=condition_transport_on_routes(transport_traffic(),route_evidence(complete=complete,connected=False))
            self.assertEqual(traffic.transport_route_diagnostics['sources'][0]['route_status'],status)
            self.assertEqual(traffic.transport_lower_daily,0)
            self.assertEqual(traffic.transport_upper_daily==0,complete)

    def test_signal_wait_unknown_retains_zero_lower_audience_and_upper_bound(self):
        traffic=condition_transport_on_routes(transport_traffic(),route_evidence(2,wait_upper=None))
        self.assertEqual(traffic.transport_lower_daily,0)
        self.assertGreater(traffic.transport_upper_daily,0)
        self.assertEqual(traffic.transport_source_coverage,0)

    def test_other_station_cannot_supply_this_stations_flow(self):
        evidence=route_evidence();evidence['access_routes']['walk']['origin']['name']='Другая станция'
        traffic=condition_transport_on_routes(transport_traffic(),evidence)
        self.assertEqual(traffic.transport_lower_daily,0)
        self.assertEqual(traffic.transport_route_diagnostics['sources'][0]['matched_origin_nodes'],[])

    def test_exits_of_one_station_do_not_multiply_passenger_flow(self):
        evidence=route_evidence();origin=copy.deepcopy(evidence['access_routes']['walk']['origin'])
        evidence['pedestrian_network']['nodes'].append('metro2')
        evidence['pedestrian_network']['edges'].append(dict(**{'from':'metro2','to':'entry'},
            minutes=2,walk_allowed=True,bidirectional=True))
        origin['node']='metro2';evidence['accessibility_diagnostics']=dict(arrivals=[origin])
        one=condition_transport_on_routes(transport_traffic(),route_evidence())
        two=condition_transport_on_routes(transport_traffic(),evidence)
        self.assertEqual(one.transport_lower_daily,two.transport_lower_daily)

    def score(self,minutes,*,access=True,transit=True):
        evidence=route_evidence(minutes)
        report=environment();report.foot_traffic=condition_transport_on_routes(transport_traffic(),evidence)
        config=market_config()
        for key,item in config['audience'].items():item['weight']=int(key==('transit_flow' if transit else 'residents'))
        config['access']=dict(kind='walk' if access else 'none',weight=1 if access else 0,
            reference_minutes=15,concept_quote=CONCEPT,reason='Synthetic route importance')
        prop=candidate(raw_attributes=dict(business_evidence=evidence))
        return evaluate_profile(integrated_profile(config),prop,report)

    def test_mapped_detour_changes_final_rating_not_only_a_route_diagnostic(self):
        self.assertGreater(self.score(2)['lower'],self.score(12)['lower'])

    def test_same_transit_route_does_not_receive_two_access_penalties(self):
        once=self.score(12,access=False);also=self.score(12,access=True)
        self.assertAlmostEqual(once['lower'],also['lower'])
        self.assertTrue(also['business_model']['market_context']['transit_route_accounted_once'])

    def test_access_still_matters_for_non_transit_audience(self):
        self.assertGreater(self.score(2,transit=False)['lower'],self.score(12,transit=False)['lower'])

    def test_unconditioned_estimator_cannot_reward_station_proximity_in_score(self):
        from real_estate_engine.market_context import _transport_audience
        interval,raw,coverage,_=_transport_audience(transport_traffic(),dict(active_hours=list(range(24))),0)
        self.assertEqual((interval.low,interval.high,raw,coverage),(0,1,None,0))

    def test_stop_needs_unique_name_and_coordinate_binding(self):
        ft=FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,[business_card(nearby_stops=[
            dict(id='stop',name='Остановка',coordinates=[LNG,LAT])])])
        evidence=route_evidence();origin=evidence['access_routes']['walk']['origin']
        origin.update(kind='public_transport_stop',name='Остановка',lat=LAT,lng=LNG)
        known=condition_transport_on_routes(ft,evidence)
        self.assertGreater(known.transport_lower_daily,0)
        other=copy.deepcopy(origin);other['node']='second'
        evidence['accessibility_diagnostics']=dict(arrivals=[other])
        ambiguous=condition_transport_on_routes(ft,evidence)
        self.assertEqual(ambiguous.transport_lower_daily,0)

    def test_verified_crossing_time_resolves_audience_wait_uncertainty(self):
        evidence=route_evidence(2,wait_upper=None)
        evidence['walk_time_minutes']=dict(value=4,verified=True,source='Synthetic measured trip to checked entry')
        traffic=condition_transport_on_routes(transport_traffic(),evidence)
        self.assertGreater(traffic.transport_lower_daily,0)
        source=traffic.transport_route_diagnostics['sources'][0]
        self.assertEqual(source['minutes_lower'],source['minutes_upper'])
        # A measured crossing resolves travel time, not the unmeasured share
        # of station passengers exposed to this particular premises.
        self.assertLess(traffic.transport_lower_daily,traffic.transport_upper_daily)

    def test_conflicting_route_measurement_does_not_reward_the_faster_graph(self):
        evidence=route_evidence(2)
        evidence['walk_time_minutes']=dict(value=4,verified=True,source='Synthetic conflicting route measurement')
        traffic=condition_transport_on_routes(transport_traffic(),evidence)
        self.assertEqual(traffic.transport_lower_daily,0)
        self.assertEqual(traffic.transport_route_diagnostics['sources'][0]['route_measurement_status'],'conflict')

    def test_station_priority_cannot_change_when_only_geodesic_order_changes(self):
        traffic=transport_traffic()
        other=copy.deepcopy(traffic.transport_sources[0]);other.update(source_id='metro:Другая',name='Другая',
            station_passengers_lower=50000,station_passengers_upper=50000)
        traffic.transport_sources.append(other)
        evidence=route_evidence(4)
        origin=dict(node='other',verified=True,source='Synthetic named exit',kind='metro_exit',name='Другая')
        evidence['pedestrian_network']['nodes'].append('other')
        evidence['pedestrian_network']['edges'].append(dict(**{'from':'other','to':'entry'},minutes=2,
            walk_allowed=True,bidirectional=True))
        evidence['accessibility_diagnostics']=dict(arrivals=[origin])
        first=condition_transport_on_routes(traffic,evidence)
        traffic.transport_sources.reverse()
        second=condition_transport_on_routes(traffic,evidence)
        self.assertAlmostEqual(first.transport_lower_daily,second.transport_lower_daily)
        shares={r['name']:r['exposure_assumption']['station_share_lower'] for r in first.transport_route_diagnostics['sources']}
        self.assertEqual(shares['Другая'],shares['Тестовая'])

    def test_same_station_with_different_case_is_not_counted_twice(self):
        traffic=FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,[business_card(nearby_metro=[
            dict(name='Тестовая',coordinates=[LNG,LAT]),dict(name='  тестовая ',coordinates=[LNG,LAT])])])
        self.assertEqual(len(traffic.transport_sources),1)
        self.assertEqual(len(traffic.nearest_metros),1)

    def test_ambiguous_multi_entrance_building_keeps_access_routes_omitted_but_bounds_walk_and_transit(self):
        from test_automatic_access import prop
        body=(f'<osm>'
              f'<node id="1" lat="{LAT}" lon="{LNG}"><tag k="railway" v="subway_entrance"/><tag k="name" v="Тестовая"/></node>'
              f'<node id="3" lat="{LAT+100/111320}" lon="{LNG}"><tag k="entrance" v="main"/></node>'
              f'<node id="5" lat="{LAT+220/111320}" lon="{LNG}"><tag k="entrance" v="yes"/></node>'
              f'<node id="4" lat="{LAT+220/111320}" lon="{LNG+10/111320}"/>'
              f'<way id="10"><nd ref="1"/><nd ref="3"/><nd ref="5"/><tag k="highway" v="footway"/></way>'
              f'<way id="20"><nd ref="3"/><nd ref="4"/><nd ref="5"/><nd ref="3"/><tag k="building" v="yes"/>'
              f'<tag k="addr:street" v="Тестовая улица"/><tag k="addr:housenumber" v="5 к1"/></way></osm>').encode()
        snapshot=parse_osm_map(body,'Synthetic multi-entrance OSM fixture',fetched_at='2026-11-05')
        evidence=assess_mapped_access(snapshot,prop())
        self.assertNotIn('access_routes',evidence)
        route=resolve_access_route(evidence,'walk')
        self.assertEqual(route['status'],'reachable')
        self.assertEqual(route['scope'],'building_entrance_interval')
        self.assertIsNone(route['minutes'])
        self.assertLess(route['minutes_lower'],route['minutes_upper'])
        conditioned=condition_transport_on_routes(transport_traffic(),evidence)
        self.assertGreater(conditioned.transport_lower_daily,0)
        self.assertGreater(conditioned.transport_upper_daily,conditioned.transport_lower_daily)
        self.assertEqual(conditioned.transport_route_diagnostics['entry_scope'],'building_entrance_interval')


class PipelineRouteAudienceTests(unittest.IsolatedAsyncioTestCase):
    async def run_search(self,missing_short_route=False):
        storage,conn=memory_storage();self.addCleanup(conn.close)
        storage.upsert_batch([property_offer('short',lat=LAT,description='Первый этаж, отдельный вход с улицы.'),
                             property_offer('detour',lat=LAT+.0001,description='Первый этаж, отдельный вход с улицы.')])
        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self,**kw):
                report=environment();report.foot_traffic=transport_traffic();return report
        analyzer=Analyzer()
        async def enrich(prop):
            if prop['external_id']=='short' and missing_short_route:return {}
            return route_evidence(2 if prop['external_id']=='short' else 12)
        async def finance(cards):return 0
        analyzer.pedestrian_provider=SimpleNamespace(enrich=enrich)
        analyzer.fns_collector=SimpleNamespace(enrich_cards=finance)
        engine=OptimalPropertyRecommender(storage,analyzer)
        config=market_config()
        for key,item in config['audience'].items():item['weight']=int(key=='transit_flow')
        async def profile(*args,**kwargs):return integrated_profile(config)
        engine.ranking_profile_resolver.resolve=profile
        return await engine.find_optimal_properties('Тестовый клиент',business_concept=CONCEPT,
            center_lat=LAT,center_lng=LNG,search_radius_m=500,min_area_sqm=30,max_area_sqm=80,
            max_price_month=150000,max_candidates=2)

    async def test_real_pipeline_conditions_transport_before_final_score(self):
        report=await self.run_search()
        self.assertEqual(report.evaluated_count,2)
        self.assertEqual(report.best_candidate.property_data['external_id'],'short')
        a,b=report.ranked_candidates
        self.assertGreater(a.unrounded_total_score,b.unrounded_total_score)
        self.assertTrue(all(c.competition_report.foot_traffic.transport_route_diagnostics['applied'] for c in report.ranked_candidates))

    async def test_missing_map_cannot_beat_a_known_detour_on_guaranteed_score(self):
        report=await self.run_search(missing_short_route=True)
        self.assertEqual(report.best_candidate.property_data['external_id'],'detour')
        short=next(c for c in report.ranked_candidates if c.property_data['external_id']=='short')
        self.assertEqual(short.competition_report.foot_traffic.transport_lower_daily,0)


if __name__=='__main__':unittest.main()
