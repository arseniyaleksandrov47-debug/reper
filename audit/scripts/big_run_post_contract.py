"""Repeatable larger audit of one frozen code version; no production DB writes."""
import argparse
import asyncio
import copy
import hashlib
import json
import logging
import random
import sqlite3
import sys
import time
from collections import Counter,defaultdict
from dataclasses import asdict
from datetime import datetime,timedelta,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from real_estate_engine import RealEstateAggregator,PropertyStorage,OptimalPropertyRecommender
from real_estate_engine.floor_policy import BusinessFloorProfileResolver
from expanded_cases import controlled_cases,REAL_CONTEXTS
from expanded_holdout import CASES

BASE=ROOT/'audit/big_run_2026-11-03'
MOSCOW=timezone(timedelta(hours=3))
OUT=None

def digest_sources():
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT/'real_estate_engine').rglob('*.py'))}

def save(name,value):
    path=OUT/name;path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

def event(name,**values):
    record=dict(event=name,time_moscow=datetime.now(MOSCOW).isoformat(),**values)
    with (OUT/'events.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
    print(json.dumps(record,ensure_ascii=False),flush=True)

class TracedResolver(BusinessFloorProfileResolver):
    gate=None
    sequence=0
    def __init__(self,analyzer,label,**kwargs):
        super().__init__(analyzer,**kwargs);self.label=label
    async def _infer(self,task,instructions,data,max_tokens):
        async with self.gate:
            type(self).sequence+=1;index=type(self).sequence
            record=dict(label=self.label,task=task,input=data);started=time.perf_counter()
            try:
                result,model=await super()._infer(task,instructions,data,max_tokens)
                record.update(response=result,model=model,metadata=getattr(result,'metadata',{}))
                return result,model
            except Exception as exc:
                record['error_type']=type(exc).__name__;raise
            finally:
                record['seconds']=round(time.perf_counter()-started,3)
                save(f'trace/{index:04}.json',record)

def checks_for(offers,decisions):
    checks=[]
    for offer in offers:
        actual=decisions[str(offer['id'])].status;expected=offer.get('expected','eligible')
        passed=actual!='eligible' if expected=='not_eligible' else actual==expected
        checks.append(dict(id=offer['id'],expected=expected,actual=actual,passed=passed,
                           analysis_status=decisions[str(offer['id'])].analysis_status))
    return checks

async def main():
    global OUT
    parser=argparse.ArgumentParser();parser.add_argument('--offline-only',action='store_true');args=parser.parse_args()
    if args.offline_only:
        from big_run_offline import main as offline_main
        await offline_main()
        return
    BASE.mkdir(exist_ok=True)
    OUT=BASE/datetime.now(MOSCOW).strftime('run_%H%M%S_%f');OUT.mkdir()
    (BASE/'LATEST.txt').write_text(str(OUT),encoding='utf-8')
    before_sources=digest_sources();original=ROOT/'data/moscow_commercial.db';before_db=hashlib.sha256(original.read_bytes()).hexdigest()
    save('manifest.json',dict(started_moscow=datetime.now(MOSCOW).isoformat(),sources=before_sources,working_database_sha256=before_db,
        controls_repeats=3,fresh_sample_target=60,real_contexts=6,
        planned_control_evaluations=108,planned_studio_evaluations=24,planned_real_evaluations=360))
    event('started',output=str(OUT))
    agg=RealEstateAggregator(str(OUT/'fresh.db'))
    summary=await asyncio.wait_for(agg.run_pipeline(150),timeout=180)
    save('collection.json',summary)
    event('collection_completed',collected=summary['batch_collected'],accepted=summary['batch_saved_or_updated'],quarantined=summary['batch_quarantined'],
          sources={k:v['status'] for k,v in summary['source_results'].items()})
    pool=agg.storage.find_candidates(55.7558,37.6173,40000,min_area_sqm=20,max_area_sqm=350,max_price_month=600000)
    groups=defaultdict(list);rng=random.Random(2026110304)
    for prop in pool:groups[prop['source']].append(prop)
    for props in groups.values():rng.shuffle(props)
    sample=[]
    while len(sample)<60 and any(groups.values()):
        for source in sorted(groups):
            if groups[source] and len(sample)<60:sample.append(groups[source].pop())
    save('fresh_sample.json',sample)
    event('sample_selected',count=len(sample),sources=dict(Counter(p['source'] for p in sample)),floors=dict(Counter(str(p.get('floor')) for p in sample)))
    subset=PropertyStorage(str(OUT/'sample.db'))
    with agg.storage._get_connection() as source,subset._get_connection() as dest:source.backup(dest)
    with subset._get_connection() as conn:
        conn.execute('DELETE FROM properties WHERE id NOT IN ('+','.join('?' for _ in sample)+')',[p['id'] for p in sample])
    TracedResolver.gate=asyncio.Semaphore(2);work_gate=asyncio.Semaphore(2)
    control_records=[];studio_records=[];real_records=[];resolvers={}
    async def control_job(case,repeat):
        async with work_gate:
            name,business,context,offers=case;label=f'control_{name}_{repeat}'
            resolver=TracedResolver(agg.competitor_analyzer,label)
            profile=await resolver.resolve(business,context);start=time.perf_counter()
            decisions=await resolver.assess_many([{k:v for k,v in p.items() if k!='expected'} for p in offers],profile)
            checks=checks_for(offers,decisions)
            record=dict(case=name,repeat=repeat,profile=asdict(profile),offers=offers,checks=checks,assessments={k:asdict(v) for k,v in decisions.items()},seconds=round(time.perf_counter()-start,3))
            control_records.append(record);save(f'controlled/{name}_{repeat}.json',record)
            event('control_completed',case=name,repeat=repeat,passed=sum(c['passed'] for c in checks),total=len(checks),
                  statuses={c['id']:c['actual'] for c in checks})
    await asyncio.gather(*(control_job(case,repeat) for repeat in (1,2,3) for case in controlled_cases()+CASES))
    # Wording/order/batch variants isolate the two previously lost studio cases.
    studio_case=next(c for c in controlled_cases() if c[0]=='studio')
    contexts=[studio_case[2],
        'Клиенты приходят по предварительной записи; нужен разрешённый путь посетителя до двери помещения',
        'Посетители приезжают целенаправленно по записи; случайный уличный поток не требуется; оценивается только этаж и путь к помещению']
    async def studio_job(context_index,batch_size,repeat):
        async with work_gate:
            offers=copy.deepcopy(studio_case[3][:2])
            if repeat==2:offers.reverse()
            label=f'studio_{context_index}_batch{batch_size}_r{repeat}'
            resolver=TracedResolver(agg.competitor_analyzer,label,batch_size=batch_size)
            profile=await resolver.resolve(studio_case[1],contexts[context_index]);start=time.perf_counter()
            decisions=await resolver.assess_many([{k:v for k,v in p.items() if k!='expected'} for p in offers],profile)
            checks=checks_for(offers,decisions)
            record=dict(label=label,context_index=context_index,batch_size=batch_size,repeat=repeat,profile=asdict(profile),checks=checks,
                assessments={k:asdict(v) for k,v in decisions.items()},seconds=round(time.perf_counter()-start,3))
            studio_records.append(record);save(f'studio/{label}.json',record)
            event('studio_completed',variant=label,passed=sum(c['passed'] for c in checks),total=len(checks))
    await asyncio.gather(*(studio_job(c,b,r) for c in range(3) for b in (1,4) for r in (1,2)))
    contexts=REAL_CONTEXTS+[('studio','Студия озвучки аудиокниг','Клиенты приезжают по предварительной записи; нужен разрешённый путь посетителя до помещения; уличный поток не нужен')]
    async def real_job(case):
        async with work_gate:
            name,business,context=case;label=f'real_{name}'
            resolver=TracedResolver(agg.competitor_analyzer,label)
            profile=await resolver.resolve(business,context);start=time.perf_counter()
            decisions=await resolver.assess_many(sample,profile);elapsed=time.perf_counter()-start
            start=time.perf_counter();cached=await resolver.assess_many(sample,profile);cache_ms=(time.perf_counter()-start)*1000
            deltas=[dict(id=k,before=v.status,after=cached[k].status,before_analysis=v.analysis_status,after_analysis=cached[k].analysis_status)
                    for k,v in decisions.items() if asdict(v)!=asdict(cached[k])]
            record=dict(case=name,profile=asdict(profile),count=len(sample),counts=dict(Counter(a.status for a in decisions.values())),
                analysis_states=dict(Counter(a.analysis_status for a in decisions.values())),seconds=round(elapsed,3),cache_ms=round(cache_ms,3),
                cache_preserves_results=not deltas,cache_deltas=deltas,assessments={k:asdict(v) for k,v in decisions.items()},
                repeated_assessments={k:asdict(v) for k,v in cached.items()})
            real_records.append(record);resolvers[name]=resolver;save(f'real/{name}.json',record)
            event('real_completed',case=name,counts=record['counts'],seconds=record['seconds'],cache_ok=record['cache_preserves_results'])
    await asyncio.gather(*(real_job(c) for c in contexts))
    integrations=[]
    for name,business,context in contexts:
        record=next(r for r in real_records if r['case']==name)
        if not record['counts'].get('eligible'):continue
        engine=OptimalPropertyRecommender(subset,agg.competitor_analyzer);engine.floor_profile_resolver=resolvers[name]
        try:
            report=await asyncio.wait_for(engine.find_optimal_properties(business,business_format=context,center_lat=55.7558,center_lng=37.6173,
                search_radius_m=40000,min_area_sqm=20,max_area_sqm=350,max_price_month=600000,max_candidates=6,analysis_limit=6),timeout=200)
            save(f'integration/{name}.json',asdict(report))
            entry=dict(case=name,numerical=report.numerical_matching_count,eligible=report.total_matching_in_db,evaluated=report.evaluated_count,
                complete=report.comparison_complete,errors=report.analysis_errors,warnings=report.ranking_warnings,
                winner=report.best_candidate.property_data['id'] if report.best_candidate else None,seconds=report.total_search_time_ms/1000)
        except Exception as exc:entry=dict(case=name,error_type=type(exc).__name__)
        integrations.append(entry);event('integration_completed',**entry)
        if len(integrations)>=3:break
    all_checks=[c for r in control_records for c in r['checks']]
    studio_checks=[c for r in studio_records for c in r['checks']]
    by_id=defaultdict(list)
    for r in sorted(control_records,key=lambda r:r['repeat']):
        for c in r['checks']:by_id[c['id']].append(c['actual'])
    final=dict(control_evaluations=len(all_checks),control_passed=sum(c['passed'] for c in all_checks),
        control_unsafe_approvals=[dict(case=r['case'],repeat=r['repeat'],**c) for r in control_records for c in r['checks'] if c['expected']!='eligible' and c['actual']=='eligible'],
        control_failures=[dict(case=r['case'],repeat=r['repeat'],**c) for r in control_records for c in r['checks'] if not c['passed']],
        control_status_variation={k:v for k,v in by_id.items() if len(set(v))>1},studio_evaluations=len(studio_checks),studio_passed=sum(c['passed'] for c in studio_checks),
        real_evaluations=sum(r['count'] for r in real_records),real_summaries=[{k:v for k,v in r.items() if k not in ('assessments','repeated_assessments')} for r in real_records],
        integrations=integrations,inference_calls=TracedResolver.sequence,source_code_unchanged=before_sources==digest_sources(),
        working_database_unchanged=before_db==hashlib.sha256(original.read_bytes()).hexdigest(),finished_moscow=datetime.now(MOSCOW).isoformat())
    save('live_summary.json',final);event('finished',controls=f"{final['control_passed']}/{final['control_evaluations']}",
        unsafe=len(final['control_unsafe_approvals']),studio=f"{final['studio_passed']}/{final['studio_evaluations']}",
        real=final['real_evaluations'],unchanged=final['working_database_unchanged'],code_unchanged=final['source_code_unchanged'])

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
