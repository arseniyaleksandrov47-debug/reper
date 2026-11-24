"""Acceptance point 1 only: automatic mapped access and official dated metro.

Read five saved real listings; no NVIDIA, FNS or notifications. The original DB
is opened read-only. Entrance/route gaps are recorded, never filled by snapping.
"""
import asyncio
import hashlib
import json
import logging
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from two_integrated_runs import save,event,fingerprints
from real_estate_engine.osm_access import OSMPedestrianProvider
from real_estate_engine.mos_metro_source import MosMetroFlowSource
from real_estate_engine.accessibility import resolve_access_route


async def main():
    base=ROOT/'audit/acceptance_point1'
    out=base/datetime.now().strftime('run_%H%M%S_%f');out.mkdir(parents=True)
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    frozen=fingerprints();save(out/'manifest.json',dict(scope=__doc__,fingerprints=frozen,listing_ids=[63,80,81,205,905]))
    with closing(sqlite3.connect((ROOT/'data/moscow_commercial.db').as_uri()+'?mode=ro',uri=True)) as conn:
        conn.row_factory=sqlite3.Row
        rows=[dict(r) for r in conn.execute('SELECT id,source,external_id,address,lat,lng,floor FROM properties WHERE id IN (63,80,81,205,905) ORDER BY id')]
    if len(rows)!=5:raise RuntimeError('Expected saved listing sample is missing')
    save(out/'listings.json',rows)
    provider=OSMPedestrianProvider(cache_path=out/'source_cache.sqlite3',timeout=15)
    metro=MosMetroFlowSource(cache_path=out/'source_cache.sqlite3')
    async def mapped(prop):
        start=time.perf_counter();evidence=await provider.enrich(prop)
        diagnostics=evidence.get('accessibility_diagnostics',{})
        save(out/f'listing_{prop["id"]}_access.json',evidence)
        route=resolve_access_route(evidence,'walk')
        record=dict(id=prop['id'],address=prop['address'],seconds=round(time.perf_counter()-start,3),
            status=diagnostics.get('status'),reason=diagnostics.get('missing_reason'),error_type=diagnostics.get('error_type'),
            buildings=diagnostics.get('building_matches',0),entrances=len(diagnostics.get('entrance_candidates',[])),
            graph_nodes=diagnostics.get('network_nodes',0),graph_edges=diagnostics.get('network_edges',0),
            arrival_points=len(diagnostics.get('arrivals',[])),route_status=route['status'],route_minutes=route.get('minutes'),
            route_scope=route.get('scope'),unit_entrance_verified=diagnostics.get('unit_entrance_verified',False))
        event('automatic_access_completed',**record)
        return record
    started=time.perf_counter()
    values=await asyncio.gather(metro.get_dataset(),*(mapped(p) for p in rows))
    dataset=values[0];records=values[1:]
    save(out/'metro_diagnostics.json',metro.diagnostics)
    if dataset:
        save(out/'metro_dataset.json',dict(source=dataset.source,as_of=dataset.as_of.isoformat(),bounds=dataset.bounds,
            measure=dataset.measure,stations=dataset.stations))
    # Reopen the caches in new provider instances: this is reuse, not a second
    # independent live source download.
    warm_provider=OSMPedestrianProvider(cache_path=out/'source_cache.sqlite3')
    warm_metro=MosMetroFlowSource(cache_path=out/'source_cache.sqlite3')
    start=time.perf_counter();warm_dataset=await warm_metro.get_dataset()
    warm=[]
    for row in rows:
        old=next(r for r in records if r['id']==row['id'])
        if not old['graph_nodes']:continue
        result=await warm_provider.enrich(row)
        warm.append(dict(id=row['id'],cache_hit=result['accessibility_diagnostics'].get('cache_hit'),
                         route_status=resolve_access_route(result,'walk')['status']))
    record=dict(seconds=round(time.perf_counter()-started,2),listings=records,
        official_metro_available=dataset is not None,metro_diagnostics=metro.diagnostics,
        cold_osm_requests=provider.requests,cold_metro_requests=metro.requests,
        warm_osm_requests=warm_provider.requests,warm_metro_requests=warm_metro.requests,
        warm_seconds=round(time.perf_counter()-start,3),warm=warm,
        original_code_and_database_unchanged=frozen==fingerprints(),
        mapped_graphs=sum(r['graph_nodes']>0 for r in records),mapped_building_entries=sum(r['entrances'] for r in records),
        usable_approach_routes=sum(r['route_status']=='reachable' for r in records),
        note='Mapped building approaches do not prove the door or permission of the leased unit')
    save(out/'summary.json',record);event('point1_completed',directory=str(out),**record)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
