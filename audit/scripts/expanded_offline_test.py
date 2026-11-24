"""Independent large generated-data and metamorphic checks; no production DB writes."""
import asyncio
import itertools
import json
import logging
import math
import random
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from real_estate_engine import CanonicalProperty,PlatformSource,PropertyStorage,OptimalPropertyRecommender,haversine_distance
from test_engine_regressions import memory_storage,population_analyzer,competition_report,property_offer,LAT,LNG
from test_remaining_problems import environment,building
from floor_ai_fixtures import FixtureFloorAnalyzer
OUT=ROOT/'audit/expanded_test_2026-11-03'
OUT.mkdir(parents=True,exist_ok=True)
RNG=random.Random(2026110302)

def save(name,value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

def event(name,**values):
    print(json.dumps(dict(event=name,**values),ensure_ascii=False),flush=True)

def destination(lat,lng,meters,bearing):
    a,b,angle,course=map(math.radians,(lat,lng,meters/6371000*180/math.pi,bearing))
    c=math.asin(math.sin(a)*math.cos(angle)+math.cos(a)*math.sin(angle)*math.cos(course))
    d=b+math.atan2(math.sin(course)*math.sin(angle)*math.cos(a),math.cos(angle)-math.sin(a)*math.sin(c))
    return math.degrees(c),(math.degrees(d)+180)%360-180

def oracle_distances(lat,lng,coords):
    a,b=np.radians([lat,lng])
    c,d=np.radians(coords[:,0]),np.radians(coords[:,1])
    x=np.cos(c)*np.sin(d-b)
    y=np.cos(a)*np.sin(c)-np.sin(a)*np.cos(c)*np.cos(d-b)
    z=np.sin(a)*np.sin(c)+np.cos(a)*np.cos(c)*np.cos(d-b)
    return 6371000*np.arctan2(np.hypot(x,y),z)

def spatial_checks(property_count=12000, query_count=5000):
    store=PropertyStorage(str(OUT/'generated.db'))
    # A rerun replaces the isolated generated dataset only.
    with store._get_connection() as conn:
        conn.execute('DELETE FROM properties_rtree')
        conn.execute('DELETE FROM properties')
    centers=[(55.7558,37.6173),(59.9386,30.3141),(56.8389,60.6057)]
    props=[]
    for i in range(property_count):
        lat,lng=destination(*centers[i%3],RNG.uniform(0,90000),RNG.uniform(0,360))
        area=RNG.uniform(15,800)
        price=RNG.uniform(15000,2000000)
        props.append(CanonicalProperty(source=PlatformSource.CIAN,external_id=str(i),title='Synthetic only',
            address='Generated coordinates, not a market listing',lat=lat,lng=lng,area_sqm=area,
            price_month=price,price_sqm_year=price*12/area,floor=RNG.choice([None,-1,0,1,2,3,7,14])))
    edge_queries=[]
    # Points just inside, on and outside boundaries, including wide and wrapping circles.
    for center,radius in [(centers[0],500),(centers[1],200000),(centers[2],700000),
                          ((0,179.8),150000),((85,30),1000000),((55.7,37.6),21000000)]:
        edge_queries.append((*center,radius))
        for bearing in range(0,360,10):
            for offset in (-.1,.1):
                lat,lng=destination(*center,min(radius,20000000)+offset,bearing)
                props.append(CanonicalProperty(source=PlatformSource.CIAN,external_id='edge'+str(len(props)),
                    title='Boundary fixture',address='Synthetic only',lat=lat,lng=lng,area_sqm=50,
                    price_month=100000,price_sqm_year=24000,floor=1))
    store.upsert_batch(props)
    with store._get_connection() as conn:
        rows=[dict(r) for r in conn.execute('SELECT * FROM properties ORDER BY id')]
    coords=np.array([(r['lat'],r['lng']) for r in rows])
    ids=np.array([r['id'] for r in rows])
    area=np.array([r['area_sqm'] for r in rows]);price=np.array([r['price_month'] for r in rows]);rate=np.array([r['price_sqm_year'] for r in rows])
    failures=[];filter_failures=[];timings=[]
    queries=edge_queries+[(centers[i%3][0]+RNG.uniform(-.25,.25),centers[i%3][1]+RNG.uniform(-.4,.4),RNG.uniform(10,30000)) for i in range(query_count)]
    for index,(lat,lng,radius) in enumerate(queries):
        distances=oracle_distances(lat,lng,coords)
        expected=set(map(int,ids[distances<=radius]))
        start=time.perf_counter()
        try:
            actual={r['id'] for r in store.find_within_radius_rtree(lat,lng,radius)}
            timings.append((time.perf_counter()-start)*1000)
            if actual!=expected: failures.append(dict(case=index,center=[lat,lng],radius=radius,missing=sorted(expected-actual)[:12],extra=sorted(actual-expected)[:12]))
        except Exception as exc:
            failures.append(dict(case=index,error_type=type(exc).__name__))
        low=RNG.uniform(10,200); high=low+RNG.uniform(5,600); budget=RNG.uniform(20000,2000000); max_rate=RNG.uniform(5000,120000)
        mask=(distances<=radius)&(area>=low)&(area<=high)&(price<=budget)&(rate<=max_rate)
        expected_hash={rows[i]['dedup_hash'] for i in np.flatnonzero(mask)}
        try:
            actual_hash={r['dedup_hash'] for r in store.find_candidates(lat,lng,radius,min_area_sqm=low,max_area_sqm=high,max_price_month=budget,max_price_sqm_year=max_rate)}
            if actual_hash!=expected_hash: filter_failures.append(dict(case=index,missing=len(expected_hash-actual_hash),extra=len(actual_hash-expected_hash)))
        except Exception as exc: filter_failures.append(dict(case=index,error_type=type(exc).__name__))
        if index and index%1000==0: event('spatial_progress',queries=index,failures=len(failures),filter_failures=len(filter_failures))
    result=dict(generated_properties=len(rows),spatial_cases=len(queries),filter_cases=len(queries),
        failures=failures,filter_failures=filter_failures,median_ms=float(np.median(timings)),p95_ms=float(np.percentile(timings,95)))
    save('spatial.json',result)
    event('spatial_completed',cases=len(queries),failures=len(failures),filter_failures=len(filter_failures))
    return result

async def ranking_checks(pool_count=250):
    failures=[];comparisons=0
    for index in range(pool_count):
        store,conn=memory_storage()
        count=RNG.randint(8,36)
        props=[property_offer(str(i),lat=LAT+i*.0002,area_sqm=RNG.uniform(25,260),price_month=RNG.uniform(40000,250000),price_sqm_year=RNG.uniform(8000,70000)) for i in range(count)]
        store.upsert_batch(props)
        reports={p.lat:environment(RNG.uniform(5,95)) for p in props}
        class Analyzer(FixtureFloorAnalyzer):
            fail_lat=None
            async def analyze_circle(inner,**kw):
                if kw['center_lat']==inner.fail_lat: raise RuntimeError('injected fault')
                return reports[kw['center_lat']]
        analyzer=Analyzer();engine=OptimalPropertyRecommender(store,analyzer)
        kwargs=dict(center_lat=LAT,center_lng=LNG,search_radius_m=5000,business_format='Контрольный сценарий по записи',max_price_month=300000)
        full=await engine.find_optimal_properties('Произвольный сервис',max_candidates=count,**kwargs)
        short=await engine.find_optimal_properties('Произвольный сервис',max_candidates=1,**kwargs)
        comparisons+=3
        if full.evaluated_count!=count or short.evaluated_count!=count or full.best_candidate.property_data['id']!=short.best_candidate.property_data['id']:
            failures.append(dict(case=index,kind='display_limit_changes_selection'))
        expected={c.property_data['id']:c.unrounded_total_score for c in full.ranked_candidates}
        analyzer.fail_lat=props[index%count].lat
        partial=await engine.find_optimal_properties('Произвольный сервис',max_candidates=count,**kwargs)
        if partial.comparison_complete or len(partial.analysis_errors)!=1: failures.append(dict(case=index,kind='fault_not_visible'))
        for c in partial.ranked_candidates:
            comparisons+=1
            if c.unrounded_total_score!=expected[c.property_data['id']]: failures.append(dict(case=index,kind='fault_changes_unrelated_score'))
        # Enumerate the corners of the active profile's weight ranges.
        ranked=full.ranked_candidates;components=ranked[0].scoring_components
        oracle=[]
        for other in ranked[1:]:
            differences=[part['contribution_lower']-other.scoring_components[key]['contribution_lower']
                         for key,part in components.items()]
            if any(sum(m*d for m,d in zip(multipliers,differences))<=0
                   for multipliers in itertools.product((.9,1.1),repeat=len(differences))):
                oracle.append(other.property_data['id'])
        if oracle!=full.weight_sensitive_challenger_ids: failures.append(dict(case=index,kind='sensitivity_mismatch'))
        conn.close()
    result=dict(pools=pool_count,comparisons=comparisons,failures=failures,kind='controlled contracts; floor inference and environments are fixtures, not market evidence')
    save('ranking.json',result);event('ranking_completed',pools=pool_count,failures=len(failures))
    return result

def population_checks(scenario_count=800):
    failures=[]
    malformed=[None,[],"unknown",{"point":[]},{"point":"unknown"},{"floors":"5"},
        {"structure_info":"unknown"},{"links":{"database_entrances":"unknown"}},
        {"geometry":{"hover":["POLYGON"]}}]
    cases=[]
    for index in range(scenario_count):
        count=RNG.randint(1,1000)
        b=building(str(index),structure_info={'apartments_count':count},
            purpose_name=RNG.choice(['Жилой дом','Строящийся жилой дом','Снесённый жилой дом']))
        cases.append((index,[b],0 if b['purpose_name']!='Жилой дом' else round(count*2.18*.92)))
    for index,bad in enumerate(malformed):
        changed=bad if not isinstance(bad,dict) else {**building('bad'),**bad}
        cases.append((scenario_count+index,[building('valid'),changed],None))
    for index,buildings,expected in cases:
        try:
            report=population_analyzer(buildings).analyze_circle_sync(LAT,LNG)
            if expected is not None and report.estimated_residents_count!=expected:
                failures.append(dict(case=index,kind='incorrect_lifecycle_or_count',expected=expected,actual=report.estimated_residents_count))
        except Exception as exc: failures.append(dict(case=index,error_type=type(exc).__name__,kind='malformed_source_aborts_population'))
    result=dict(cases=len(cases),failures=failures)
    save('population.json',result);event('population_completed',cases=len(cases),failures=len(failures))
    return result

async def main():
    results=dict(spatial=spatial_checks(),ranking=await ranking_checks(),population=population_checks())
    save('offline_summary.json',dict(spatial_failures=len(results['spatial']['failures']),filter_failures=len(results['spatial']['filter_failures']),ranking_failures=len(results['ranking']['failures']),population_failures=len(results['population']['failures'])))

if __name__=='__main__':
    logging.disable(logging.CRITICAL)
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
