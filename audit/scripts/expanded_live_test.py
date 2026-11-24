"""Expanded bounded live audit. Public listing data only; no credentials in artifacts."""
import asyncio
import copy
import hashlib
import json
import logging
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from real_estate_engine import RealEstateAggregator, OptimalPropertyRecommender
from real_estate_engine.floor_policy import BusinessFloorProfileResolver
from expanded_cases import controlled_cases, REAL_CONTEXTS

OUT=ROOT/'audit/expanded_test_2026-11-03'
OUT.mkdir(parents=True,exist_ok=True)
EVENTS=[]

def save(name,value):
    path=OUT/name
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

def event(name,**values):
    record=dict(event=name,time_moscow=datetime.now(timezone(timedelta(hours=3))).isoformat(),**values)
    EVENTS.append(record)
    save('progress.json',EVENTS)
    print(json.dumps(record,ensure_ascii=False),flush=True)


class TracedResolver(BusinessFloorProfileResolver):
    sequence=0
    gate=None
    async def _infer(self,task,instructions,data,max_tokens):
        async with self.gate:
            type(self).sequence+=1
            number=type(self).sequence
            start=time.perf_counter()
            record=dict(task=task,input=data)
            try:
                response,model=await super()._infer(task,instructions,data,max_tokens)
                record.update(response=response,model=model)
                return response,model
            except Exception as exc:
                record['error_type']=type(exc).__name__
                raise
            finally:
                record['seconds']=round(time.perf_counter()-start,3)
                save(f'trace/{number:04}_{task}.json',record)


async def main():
    original=ROOT/'data/moscow_commercial.db'
    before=hashlib.sha256(original.read_bytes()).hexdigest()
    save('manifest.json',dict(started_moscow=datetime.now(timezone(timedelta(hours=3))).isoformat(),
        original_database_sha256=before,controlled_repeats=2,fresh_sample_target=40,real_contexts=len(REAL_CONTEXTS)))
    agg=RealEstateAggregator(str(OUT/'fresh.db'))
    summary=await asyncio.wait_for(agg.run_pipeline(100),timeout=150)
    save('collection.json',summary)
    event('collection_completed',collected=summary['batch_collected'],accepted=summary['batch_saved_or_updated'],
          quarantined=summary['batch_quarantined'],sources={k:v['status'] for k,v in summary['source_results'].items()})
    # Keep an immutable query view of 40 balanced freshly fetched advertisements.
    all_props=agg.storage.find_candidates(55.7558,37.6173,40000,min_area_sqm=20,max_area_sqm=350,max_price_month=600000)
    by_source=defaultdict(list)
    for prop in all_props: by_source[prop['source']].append(prop)
    rng=random.Random(20261103)
    for props in by_source.values():
        props.sort(key=lambda p:(p.get('floor') is None,p.get('floor') or 0,p['id']))
        rng.shuffle(props)
    sample=[]
    while len(sample)<40 and any(by_source.values()):
        for source in sorted(by_source):
            if by_source[source] and len(sample)<40: sample.append(by_source[source].pop())
    save('fresh_sample.json',sample)
    event('sample_selected',count=len(sample),sources=dict(Counter(p['source'] for p in sample)),
          floors=dict(Counter(str(p.get('floor')) for p in sample)))
    from real_estate_engine import PropertyStorage
    subset=PropertyStorage(str(OUT/'sample.db'))
    with agg.storage._get_connection() as source,subset._get_connection() as dest:
        source.backup(dest)
    with subset._get_connection() as conn:
        placeholders=','.join('?' for _ in sample)
        conn.execute(f'DELETE FROM properties WHERE id NOT IN ({placeholders})',[p['id'] for p in sample])
    TracedResolver.gate=asyncio.Semaphore(2)
    work_gate=asyncio.Semaphore(2)
    controlled=[]
    real=[]
    resolvers={}

    async def controlled_job(case,repeat):
        async with work_gate:
            name,business,context,offers=case
            resolver=TracedResolver(agg.competitor_analyzer)
            profile=await resolver.resolve(business,context)
            props=[{k:v for k,v in p.items() if k!='expected'} for p in offers]
            start=time.perf_counter()
            decisions=await resolver.assess_many(props,profile)
            checks=[]
            for p in offers:
                actual=decisions[p['id']].status
                expected=p['expected']
                ok=(actual!='eligible') if expected=='not_eligible' else actual==expected
                checks.append(dict(id=p['id'],expected=expected,actual=actual,passed=ok))
            record=dict(case=name,repeat=repeat,business=business,context=context,offers=offers,
                checks=checks,assessments={k:asdict(v) for k,v in decisions.items()},seconds=round(time.perf_counter()-start,3))
            controlled.append(record)
            save(f'controlled/{name}_{repeat}.json',record)
            event('controlled_completed',case=name,repeat=repeat,passed=sum(c['passed'] for c in checks),total=len(checks),
                  statuses={c['id']:c['actual'] for c in checks})

    async def real_job(case):
        async with work_gate:
            name,business,context=case
            resolver=TracedResolver(agg.competitor_analyzer)
            profile=await resolver.resolve(business,context)
            start=time.perf_counter()
            decisions=await resolver.assess_many(sample,profile)
            seconds=time.perf_counter()-start
            start=time.perf_counter()
            cached=await resolver.assess_many(sample,profile)
            record=dict(case=name,business=business,context=context,count=len(sample),
                assessments={k:asdict(v) for k,v in decisions.items()},
                counts=dict(Counter(v.status for v in decisions.values())),seconds=round(seconds,3),
                cache_ms=round((time.perf_counter()-start)*1000,3),
                cache_preserves_results=all(asdict(cached[k])==asdict(v) for k,v in decisions.items()))
            real.append(record)
            resolvers[name]=resolver
            save(f'real/{name}.json',record)
            event('real_context_completed',case=name,counts=record['counts'],seconds=record['seconds'],cache_ok=record['cache_preserves_results'])

    await asyncio.gather(*(controlled_job(case,repeat) for repeat in (1,2) for case in controlled_cases()),
                         *(real_job(case) for case in REAL_CONTEXTS))
    # Exercise the public pipeline with live environment analysis in up to two contexts.
    integration=[]
    for name,business,context in REAL_CONTEXTS:
        record=next(r for r in real if r['case']==name)
        if not record['counts'].get('eligible'): continue
        engine=OptimalPropertyRecommender(subset,agg.competitor_analyzer)
        engine.floor_profile_resolver=resolvers[name]
        try:
            report=await asyncio.wait_for(engine.find_optimal_properties(business,business_format=context,
                center_lat=55.7558,center_lng=37.6173,search_radius_m=40000,min_area_sqm=20,max_area_sqm=350,
                max_price_month=600000,max_candidates=4,analysis_limit=4),timeout=180)
            save(f'integration/{name}.json',asdict(report))
            entry=dict(case=name,numerical=report.numerical_matching_count,eligible=report.total_matching_in_db,
                evaluated=report.evaluated_count,errors=report.analysis_errors,complete=report.comparison_complete,
                winner=report.best_candidate.property_data['id'] if report.best_candidate else None,
                seconds=round(report.total_search_time_ms/1000,3))
        except Exception as exc:
            entry=dict(case=name,error_type=type(exc).__name__)
        integration.append(entry)
        event('integration_completed',**entry)
        if len(integration)>=2: break
    by_case=defaultdict(list)
    for record in controlled:
        for check in record['checks']: by_case[check['id']].append(check['actual'])
    flips={key:values for key,values in by_case.items() if len(set(values))>1}
    final=dict(controlled_evaluations=sum(len(r['checks']) for r in controlled),
        controlled_failed=[dict(case=r['case'],repeat=r['repeat'],**c) for r in controlled for c in r['checks'] if not c['passed']],
        controlled_status_flips=flips,real_evaluations=sum(r['count'] for r in real),
        real_summaries=[{k:v for k,v in r.items() if k!='assessments'} for r in real],integration=integration,
        inference_calls=TracedResolver.sequence,working_database_unchanged=before==hashlib.sha256(original.read_bytes()).hexdigest(),
        finished_moscow=datetime.now(timezone(timedelta(hours=3))).isoformat())
    save('live_summary.json',final)
    event('live_finished',controlled_failures=len(final['controlled_failed']),flips=len(flips),
        real_evaluations=final['real_evaluations'],unchanged=final['working_database_unchanged'])


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
