"""Live public OSM map acquisition; coordinates verified by Nominatim.

Real maps are inspected without adding fabricated entrances or paths. Any
unbound route remains unknown; this is not a rental recommendation.
"""
import asyncio
import hashlib
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import requests

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from real_estate_engine.osm_access import OSMPedestrianProvider
from real_estate_engine.mos_metro_source import MosMetroFlowSource
from real_estate_engine.traffic import FootTrafficEstimator
from real_estate_engine.route_audience import condition_transport_on_routes
from real_estate_engine.accessibility import resolve_access_route
from real_estate_engine.market_context import _transport_audience


def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str,allow_nan=False),encoding='utf-8')


class RecordingSession:
    def __init__(self,out):self.session=requests.Session();self.headers=self.session.headers;self.out=out;self.records=[]
    def get(self,url,**kwargs):
        started=time.perf_counter()
        try:
            response=self.session.get(url,**kwargs)
            filename='map_'+hashlib.sha256(url.encode()).hexdigest()[:16]+'.osm'
            if response.status_code==200:(self.out/filename).write_bytes(response.content)
            self.records.append(dict(url=url,status=response.status_code,bytes=len(response.content),
                                     seconds=round(time.perf_counter()-started,3),file=filename))
            return response
        except Exception as exc:
            self.records.append(dict(url=url,error_type=type(exc).__name__,seconds=round(time.perf_counter()-started,3)))
            raise


async def main():
    base=ROOT/'audit/route_barriers'
    out=base/datetime.now().strftime('live_%Y%m%d_%H%M%S');out.mkdir(parents=True)
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    session=RecordingSession(out);provider=OSMPedestrianProvider(session=session,cache_path=str(out/'sources.sqlite3'),timeout=18)
    metro=MosMetroFlowSource();dataset=await metro.get_dataset();save(out/'metro_source.json',metro.diagnostics)
    rows=[]
    for query in ('Барклая 8 Москва','Новозаводская 8 Москва'):
        try:
            response=requests.get('https://nominatim.openstreetmap.org/search',params=dict(q=query,format='jsonv2',addressdetails=1,limit=3),
                headers={'User-Agent':'CommercialPremisesEngine/0.1 (routing verification)'},timeout=(5,12))
            response.raise_for_status();places=response.json()
            place=next(p for p in places if p.get('osm_type') in ('way','relation') and
                       p.get('address',{}).get('city')=='Москва')
            address=place['display_name']
            prop=dict(address=address,lat=float(place['lat']),lng=float(place['lon']))
            # Use an address with street/house order, matching the ordinary
            # client listing form rather than the geocoder's display prose.
            components=place['address'];prop['address']='Москва, '+components['road']+', '+components['house_number']
            began=time.perf_counter();evidence=await provider.enrich(prop);save(out/('evidence_'+str(len(rows))+'.json'),evidence)
            route=resolve_access_route(evidence,'walk')
            diag=evidence.get('accessibility_diagnostics',{})
            arrivals=diag.get('arrivals',[])
            metro_rows=[]
            for origin in arrivals:
                if origin['kind']=='metro_exit':
                    for name in origin.get('station_names',[]):metro_rows.append(dict(name=name,coordinates=[origin['lng'],origin['lat']]))
            stops=[dict(id=v['node'],name=v.get('name',''),coordinates=[v['lng'],v['lat']])
                   for v in arrivals if v['kind']=='public_transport_stop']
            cards=[type('MappedTransport',(),dict(nearby_metro=metro_rows,nearby_stops=stops,
                        building_entrances=[],ratings_count=0,histogram_data=None,distance_m=0))()]
            traffic=FootTrafficEstimator.estimate_for_circle(prop['lat'],prop['lng'],200,cards,metro_dataset=dataset)
            conditioned=condition_transport_on_routes(traffic,evidence)
            bound,raw,coverage,warnings=_transport_audience(conditioned,dict(active_hours=list(range(24))),0)
            row=dict(query=query,property_data=prop,geocoding=dict(osm_type=place['osm_type'],osm_id=place['osm_id']),
                seconds=round(time.perf_counter()-began,3),map_status=diag.get('status'),route=route,
                entrances=len(diag.get('entrance_candidates',[])),arrivals=len(arrivals),
                network_nodes=diag.get('network_nodes'),network_edges=diag.get('network_edges'),
                barrier_diagnostics=diag.get('barrier_diagnostics'),transport_routes=conditioned.transport_route_diagnostics,
                audience_index=bound.as_dict() if bound else None,audience_coverage=coverage)
        except Exception as exc:row=dict(query=query,error_type=type(exc).__name__)
        rows.append(row);print(json.dumps({k:v for k,v in row.items() if k not in ('barrier_diagnostics','transport_routes')},ensure_ascii=False),flush=True)
    checks=dict(any_live_map_parsed=any(v.get('network_nodes',0) for v in rows),
        maps_do_not_claim_complete_coverage=all(not v.get('transport_routes',{}).get('graph_coverage_complete',False) for v in rows),
        unknown_routes_not_given_positive_lower_audience=all(v.get('audience_index',{}).get('low',0)==0 for v in rows
            if v.get('route',{}).get('status')=='unknown'))
    save(out/'result.json',dict(scope=__doc__,rows=rows,http=session.records,checks=checks))
    print(json.dumps(dict(checks=checks,directory=str(out)),ensure_ascii=False),flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
