"""Live, versioned replay against the exact sample used before the proof fix."""
import argparse
import asyncio
import copy
import hashlib
import json
import logging
import shutil
import sys
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from real_estate_engine import NvidiaCompetitorAnalyzer, PropertyStorage, OptimalPropertyRecommender
from real_estate_engine.floor_policy import BusinessFloorProfileResolver
from expanded_cases import controlled_cases, REAL_CONTEXTS
from expanded_holdout import CASES

BASELINE=ROOT/'audit/big_run_2026-11-03/run_193231_893406'
BASE=ROOT/'audit/proof_fix_2026-11-03'
MOSCOW=timezone(timedelta(hours=3))
OUT=None

def save(name,value):
    p=OUT/name;p.parent.mkdir(parents=True,exist_ok=True)
    temp=p.with_name(p.name+'.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8');temp.replace(p)

def event(name,**values):
    row=dict(event=name,time=datetime.now(MOSCOW).isoformat(),**values)
    with (OUT/'events.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
    print(json.dumps(row,ensure_ascii=False),flush=True)

def hashes():
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'real_estate_engine').rglob('*.py'))}

class TracedResolver(BusinessFloorProfileResolver):
    gate=None
    sequence=0
    def __init__(self,analyzer,label,**kwargs):
        super().__init__(analyzer,**kwargs);self.label=label
    async def _infer(self,task,instructions,data,max_tokens):
        async with self.gate:
            type(self).sequence+=1;index=type(self).sequence;start=time.perf_counter()
            trace=dict(label=self.label,task=task,input=data)
            try:
                result,model=await super()._infer(task,instructions,data,max_tokens)
                trace.update(response=result,model=model,metadata=getattr(result,'metadata',{}))
                return result,model
            except BaseException as exc:
                trace['error_type']=type(exc).__name__
                message=str(exc).lower()
                import re
                diagnostic=str(exc).replace(self.analyzer.api_key,'[REDACTED]')
                trace['safe_diagnostic']=re.sub(r'nvapi-[A-Za-z0-9_-]+','[REDACTED]',diagnostic)[:500]
                trace['error_category']=('rate_limit' if '429' in message or 'rate limit' in message else
                    'timeout' if 'timed out' in message or 'timeout' in message else
                    'bad_request' if '400' in message or '422' in message else 'other')
                raise
            finally:
                trace['seconds']=round(time.perf_counter()-start,3);save(f'trace/{index:04}.json',trace)

def checks_for(offers,decisions):
    return [dict(id=p['id'],expected=p['expected'],actual=decisions[str(p['id'])].status,
                 passed=decisions[str(p['id'])].status!='eligible' if p['expected']=='not_eligible' else decisions[str(p['id'])].status==p['expected'],
                 analysis_status=decisions[str(p['id'])].analysis_status) for p in offers]

async def main():
    global OUT
    parser=argparse.ArgumentParser();parser.add_argument('--mode',choices=['pilot','full'],default='pilot')
    parser.add_argument('--controls-only',action='store_true');parser.add_argument('--resume',type=Path)
    parser.add_argument('--repeat-batch-size',type=int,default=1);parser.add_argument('--real-batch-size',type=int,default=1)
    parser.add_argument('--phase-timeout',type=float);args=parser.parse_args()
    BASE.mkdir(exist_ok=True)
    OUT=args.resume or BASE/datetime.now(MOSCOW).strftime(args.mode+'_%H%M%S_%f');OUT.mkdir(exist_ok=bool(args.resume))
    (BASE/('LATEST_'+args.mode.upper()+'.txt')).write_text(str(OUT),encoding='utf-8')
    sources=hashes();db_hash=hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest()
    if args.resume:
        manifest=json.loads((OUT/'manifest.json').read_text(encoding='utf-8'))
        assert manifest['source_hashes']==sources,'Cannot resume across source versions'
        assert manifest['working_database_hash']==db_hash,'Working database changed'
        TracedResolver.sequence=max((int(p.stem) for p in (OUT/'trace').glob('*.json')),default=0)
    else:
        save('manifest.json',dict(mode=args.mode,source_hashes=sources,working_database_hash=db_hash,baseline=str(BASELINE),
            inference_parallelism=2,default_batch_size=1,requests_per_minute=40,started=datetime.now(MOSCOW).isoformat()))
    event('started',mode=args.mode,output=str(OUT))
    analyzer=NvidiaCompetitorAnalyzer();TracedResolver.gate=asyncio.Semaphore(2);jobs=asyncio.Semaphore(2)
    save('execution_plan.json',dict(control_repeats=3 if args.mode=='full' else 1,first_control_batch_size=1,
        subsequent_control_batch_size=args.repeat_batch_size,real_batch_size=args.real_batch_size,resumed=bool(args.resume),
        phase_timeout=args.phase_timeout,
        interpretation='Later control repeats vary batching deliberately; real-advertisement timing uses the explicit batch-size override. Production default remains one candidate.'))
    if args.resume:analyzer.floor_request_pacer.throttled(60)
    controls=[];studio=[];real=[];resolvers={}
    async def control_job(case,repeat):
        async with jobs:
            name,business,context,offers=case
            batch_size=1 if repeat==1 else args.repeat_batch_size
            resolver=TracedResolver(analyzer,f'control_{name}_{repeat}',batch_size=batch_size)
            if args.resume and (OUT/f'controlled/{name}_{repeat}.json').exists():
                controls.append(json.loads((OUT/f'controlled/{name}_{repeat}.json').read_text(encoding='utf-8')));return
            profile=await resolver.resolve(business,context);start=time.perf_counter()
            decisions=await resolver.assess_many([{k:v for k,v in p.items() if k!='expected'} for p in offers],profile,timeout_s=args.phase_timeout)
            record=dict(case=name,repeat=repeat,batch_size=batch_size,checks=checks_for(offers,decisions),profile=asdict(profile),offers=offers,
                assessments={k:asdict(v) for k,v in decisions.items()},seconds=time.perf_counter()-start)
            controls.append(record);save(f'controlled/{name}_{repeat}.json',record)
            event('control',case=name,repeat=repeat,passed=sum(c['passed'] for c in record['checks']),total=len(offers))
    await asyncio.gather(*(control_job(case,r) for r in range(1,4 if args.mode=='full' else 2) for case in controlled_cases()+CASES))
    if args.controls_only:
        checks=[c for r in controls for c in r['checks']]
        summary=dict(controls=len(checks),passed=sum(c['passed'] for c in checks),source_code_unchanged=sources==hashes())
        save('controls_summary.json',summary);event('finished_controls_only',**summary);return
    studio_case=next(c for c in controlled_cases() if c[0]=='studio')
    contexts=[studio_case[2],
        'Клиенты приходят по предварительной записи; нужен разрешённый путь посетителя до двери помещения',
        'Посетители приезжают целенаправленно по записи; случайный уличный поток не требуется; оценивается только этаж и путь к помещению']
    async def studio_job(context,batch,repeat):
        async with jobs:
            offers=copy.deepcopy(studio_case[3][:2]);offers=offers if repeat==1 else offers[::-1]
            label=f'studio_{context}_{batch}_{repeat}';resolver=TracedResolver(analyzer,label,batch_size=batch)
            if args.resume and (OUT/f'studio/{label}.json').exists():
                studio.append(json.loads((OUT/f'studio/{label}.json').read_text(encoding='utf-8')));return
            profile=await resolver.resolve(studio_case[1],contexts[context]);start=time.perf_counter()
            decisions=await resolver.assess_many([{k:v for k,v in p.items() if k!='expected'} for p in offers],profile,timeout_s=args.phase_timeout)
            record=dict(context=context,batch=batch,repeat=repeat,checks=checks_for(offers,decisions),assessments={k:asdict(v) for k,v in decisions.items()},seconds=time.perf_counter()-start)
            studio.append(record);save(f'studio/{label}.json',record);event('studio',label=label,passed=sum(c['passed'] for c in record['checks']))
    await asyncio.gather(*(studio_job(c,b,r) for c in range(3) for b in (1,4) for r in (1,2)))
    sample=json.loads((BASELINE/'fresh_sample.json').read_text(encoding='utf-8'))
    manual=json.loads((BASELINE/'manual_review.json').read_text(encoding='utf-8'))
    contexts=REAL_CONTEXTS+[('studio','Студия озвучки аудиокниг','Клиенты приезжают по предварительной записи; нужен разрешённый путь посетителя до помещения; уличный поток не нужен')]
    async def real_job(case):
        async with jobs:
            name,business,context=case;resolver=TracedResolver(analyzer,'real_'+name,batch_size=args.real_batch_size)
            if args.resume and (OUT/f'real/{name}.json').exists():
                real.append(json.loads((OUT/f'real/{name}.json').read_text(encoding='utf-8')));resolvers[name]=resolver;return
            offers=sample
            if args.mode=='pilot':
                ids={f['id'] for f in manual['findings'] if f['case'].removeprefix('integration_')==name}
                offers=[p for p in sample if p['id'] in ids]
                if not offers:return
            profile=await resolver.resolve(business,context);start=time.perf_counter()
            decisions=await resolver.assess_many(offers,profile,timeout_s=args.phase_timeout);seconds=time.perf_counter()-start
            start=time.perf_counter();cached=await resolver.assess_many(offers,profile,timeout_s=args.phase_timeout);repeat_ms=(time.perf_counter()-start)*1000
            record=dict(case=name,batch_size=args.real_batch_size,count=len(offers),counts=dict(Counter(a.status for a in decisions.values())),
                analysis_states=dict(Counter(a.analysis_status for a in decisions.values())),seconds=seconds,repeat_ms=repeat_ms,
                assessments={k:asdict(v) for k,v in decisions.items()},repeated_assessments={k:asdict(v) for k,v in cached.items()})
            real.append(record);resolvers[name]=resolver;save(f'real/{name}.json',record);event('real',case=name,count=len(offers),counts=record['counts'],seconds=round(seconds,2))
    await asyncio.gather(*(real_job(case) for case in contexts))
    integrations=[]
    if args.mode=='full':
        shutil.copy2(BASELINE/'sample.db',OUT/'sample.db');storage=PropertyStorage(str(OUT/'sample.db'))
        for name,business,context in contexts[:3]:
            if args.resume and (OUT/f'integration_summary/{name}.json').exists():
                integrations.append(json.loads((OUT/f'integration_summary/{name}.json').read_text(encoding='utf-8')));continue
            engine=OptimalPropertyRecommender(storage,analyzer);engine.floor_profile_resolver=resolvers[name]
            start=time.perf_counter()
            try:
                report=await asyncio.wait_for(engine.find_optimal_properties(business,business_format=context,center_lat=55.7558,center_lng=37.6173,
                    search_radius_m=40000,min_area_sqm=20,max_area_sqm=350,max_price_month=600000,max_candidates=6,analysis_limit=6),timeout=200)
                save(f'integration/{name}.json',asdict(report))
                record=dict(case=name,eligible=report.total_matching_in_db,evaluated=report.evaluated_count,complete=report.comparison_complete,
                    errors=report.analysis_errors,warnings=report.ranking_warnings,winner=report.best_candidate.property_data['id'] if report.best_candidate else None)
            except Exception as exc:record=dict(case=name,error_type=type(exc).__name__)
            record['seconds']=time.perf_counter()-start;integrations.append(record);save(f'integration_summary/{name}.json',record);event('integration',**record)
    checks=[c for r in controls for c in r['checks']];studio_checks=[c for r in studio for c in r['checks']]
    summary=dict(mode=args.mode,controls=len(checks),controls_passed=sum(c['passed'] for c in checks),
        unsafe_controls=[c for c in checks if c['expected']!='eligible' and c['actual']=='eligible'],
        control_failures=[dict(case=r['case'],repeat=r['repeat'],**c) for r in controls for c in r['checks'] if not c['passed']],
        studio=len(studio_checks),studio_passed=sum(c['passed'] for c in studio_checks),real_evaluations=sum(r['count'] for r in real),
        real_counts=dict(Counter(a['status'] for r in real for a in r['assessments'].values())),integrations=integrations,
        inference_calls=TracedResolver.sequence,source_code_unchanged=sources==hashes(),working_database_unchanged=db_hash==hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest())
    summary.update(http_attempts_current_process=analyzer.floor_request_pacer.http_attempts,
        throttle_responses_current_process=analyzer.floor_request_pacer.throttle_responses)
    save('summary.json',summary);event('finished',**summary)

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
