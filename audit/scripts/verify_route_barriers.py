"""Recheck saved real OSM snapshots and find a genuinely connected building.

Any removed-entry comparison is an explicitly counterfactual test, not a
claim about a real closure. No listings or unit-door facts are fabricated.
"""
import asyncio
import copy
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from real_estate_engine.osm_access import parse_osm_map,assess_mapped_access,_public
from real_estate_engine.accessibility import PedestrianNetwork,resolve_access_route
from real_estate_engine.mos_metro_source import MosMetroFlowSource
from real_estate_engine.traffic import FootTrafficEstimator
from real_estate_engine.route_audience import condition_transport_on_routes


def read(path):return json.loads(path.read_text(encoding='utf-8'))
def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


async def main():
    base=ROOT/'audit/route_barriers';out=Path((base/'LATEST.txt').read_text(encoding='utf-8').strip())
    original=read(out/'result.json');summaries=[];positive=None
    metro=MosMetroFlowSource();dataset=await metro.get_dataset()
    for index,http in enumerate(original['http']):
        if http.get('status')!=200:continue
        prior=read(out/f'evidence_{index}.json')
        snapshot=parse_osm_map((out/http['file']).read_bytes(),http['url'],fetched_at=prior['pedestrian_network']['as_of'])
        diag=snapshot['network']['barrier_diagnostics']
        summaries.append(dict(source=http['url'],rejected_crossing_counts=diag['rejected_crossing_counts'],
            examples_truncated=diag['examples_truncated'],unknown_wait_nodes=len(diag['unknown_wait_nodes'])))
        if positive is not None:continue
        graph=PedestrianNetwork(snapshot['network']);arrivals=prior['accessibility_diagnostics']['arrivals']
        metros=[a for a in arrivals if a['kind']=='metro_exit' and a.get('station_names')]
        guaranteed=set()
        for origin in metros:guaranteed.update(graph.isochrone(origin,20)['guaranteed_nodes'])
        for way in sorted(snapshot['ways'],key=lambda w:w['id']):
            tags=way['tags']
            if not tags.get('building') or not tags.get('addr:street') or not tags.get('addr:housenumber'):continue
            entries=[snapshot['nodes'][n] for n in way['nodes'] if n in snapshot['nodes'] and
                snapshot['nodes'][n]['tags'].get('entrance') in ('yes','main') and _public(snapshot['nodes'][n]['tags'])]
            entries=list({v['id']:v for v in entries}.values())
            if len(entries)!=1 or 'osm:'+entries[0]['id'] not in guaranteed:continue
            entry=entries[0]
            prop=dict(address='Москва, '+tags['addr:street']+', '+tags['addr:housenumber'],lat=entry['lat'],lng=entry['lng'])
            evidence=assess_mapped_access(snapshot,prop)
            if not evidence.get('access_routes'):continue
            relevant=evidence['accessibility_diagnostics']['arrivals']
            nearby=[dict(name=name,coordinates=[v['lng'],v['lat']]) for v in relevant
                    if v['kind']=='metro_exit' for name in v.get('station_names',[])]
            card=SimpleNamespace(nearby_metro=nearby,nearby_stops=[],building_entrances=[],ratings_count=0,histogram_data=None,distance_m=0)
            traffic=FootTrafficEstimator.estimate_for_circle(prop['lat'],prop['lng'],200,[card],metro_dataset=dataset)
            routed=condition_transport_on_routes(traffic,evidence)
            if not routed.transport_lower_daily or routed.transport_lower_daily<=0:continue
            removed=copy.deepcopy(evidence)
            key=evidence['access_routes']['walk']['entry']['node']
            removed['pedestrian_network']['edges']=[e for e in removed['pedestrian_network']['edges'] if e['from']!=key and e['to']!=key]
            closed=condition_transport_on_routes(traffic,removed)
            positive=dict(property_data=prop,building_source='https://www.openstreetmap.org/way/'+way['id'],
                route=resolve_access_route(evidence,'walk'),entry_scope='building_entrance_not_unit_door',
                transport_lower_daily=routed.transport_lower_daily,transport_upper_daily=routed.transport_upper_daily,
                route_details=routed.transport_route_diagnostics,
                counterfactual=dict(description='Remove all graph edges at the mapped entry; not an actual closure report',
                    transport_lower_daily=closed.transport_lower_daily,transport_upper_daily=closed.transport_upper_daily,
                    entry_route=resolve_access_route(removed,'walk')))
            save(out/'connected_building_evidence.json',evidence)
            break
    checks=dict(two_real_snapshots_loaded=len(summaries)==2,
        actual_railway_crossings_checked=any(s['rejected_crossing_counts'].get('railway',0)>0 for s in summaries),
        actual_road_and_fence_crossings_checked=any(s['rejected_crossing_counts'].get('road',0)>0
            and s['rejected_crossing_counts'].get('barrier',0)>0 for s in summaries),
        actual_unknown_crossing_wait_not_filled=any(s['unknown_wait_nodes']>0 for s in summaries),
        actual_connected_building_route_found=positive is not None,
        connected_entry_has_positive_route_conditioned_lower_bound=bool(positive and positive['transport_lower_daily']>0),
        counterfactual_disconnection_removes_lower_reward=bool(positive and positive['counterfactual']['transport_lower_daily']==0),
        partial_disconnection_is_not_declared_impossible=bool(positive and positive['counterfactual']['entry_route']['status']=='unknown'))
    record=dict(scope=__doc__,checks=checks,snapshots=summaries,connected_building=positive,metro_source=metro.diagnostics)
    save(base/'live_verification.json',record)
    print(json.dumps(dict(checks=checks,connected_building={k:v for k,v in (positive or {}).items() if k!='route_details'},
                         snapshots=summaries),ensure_ascii=False,indent=2))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
