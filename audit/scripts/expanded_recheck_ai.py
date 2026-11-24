"""Recheck exact frozen examples after fixes; preserve all baseline artifacts."""
import asyncio,hashlib,json,logging,sys,time
from collections import Counter,defaultdict
from dataclasses import asdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from real_estate_engine import NvidiaCompetitorAnalyzer,OptimalPropertyRecommender,PropertyStorage
from archived_floor import load_floor_assessment
import expanded_live_test as audit
from expanded_cases import controlled_cases,REAL_CONTEXTS
BASE=ROOT/'audit/expanded_test_2026-11-03'
OUT=BASE/'after'
audit.OUT=OUT
OUT.mkdir(exist_ok=True)

async def main():
    analyzer=NvidiaCompetitorAnalyzer()
    audit.TracedResolver.gate=asyncio.Semaphore(2)
    gate=asyncio.Semaphore(2)
    sample=json.loads((BASE/'fresh_sample.json').read_text(encoding='utf-8'))
    before=hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest()
    controlled=[];real=[];saved_resolvers={}
    async def control(case,repeat):
        async with gate:
            name,business,context,offers=case
            resolver=audit.TracedResolver(analyzer)
            profile=await resolver.resolve(business,context)
            start=time.perf_counter()
            decisions=await resolver.assess_many([{k:v for k,v in p.items() if k!='expected'} for p in offers],profile)
            checks=[]
            for p in offers:
                expected=p['expected'];actual=decisions[p['id']].status
                checks.append(dict(id=p['id'],expected=expected,actual=actual,passed=actual!='eligible' if expected=='not_eligible' else actual==expected))
            record=dict(case=name,repeat=repeat,checks=checks,assessments={k:asdict(v) for k,v in decisions.items()},seconds=round(time.perf_counter()-start,3))
            controlled.append(record);audit.save(f'controlled/{name}_{repeat}.json',record)
            audit.event('controlled_completed',case=name,repeat=repeat,passed=sum(c['passed'] for c in checks),total=len(checks),statuses={c['id']:c['actual'] for c in checks})
    async def real_job(case):
        async with gate:
            name,business,context=case
            resolver=audit.TracedResolver(analyzer)
            profile=await resolver.resolve(business,context)
            start=time.perf_counter();decisions=await resolver.assess_many(sample,profile);elapsed=time.perf_counter()-start
            start=time.perf_counter();cached=await resolver.assess_many(sample,profile)
            record=dict(case=name,count=len(sample),business=business,context=context,assessments={k:asdict(v) for k,v in decisions.items()},
                counts=dict(Counter(v.status for v in decisions.values())),seconds=round(elapsed,3),cache_ms=round((time.perf_counter()-start)*1000,3),
                cache_preserves_results=all(asdict(cached[k])==asdict(v) for k,v in decisions.items()))
            real.append(record);saved_resolvers[name]=resolver;audit.save(f'real/{name}.json',record)
            audit.event('real_completed',case=name,counts=record['counts'],seconds=record['seconds'],cache_ok=record['cache_preserves_results'])
    # Replay all old approvals through the new independent reviewer before re-inference.
    captured=[]
    by_id={str(p['id']):p for p in sample}
    for file in (BASE/'real').glob('*.json'):
        record=json.loads(file.read_text(encoding='utf-8'))
        resolver=audit.TracedResolver(analyzer)
        profile=await resolver.resolve(record['business'],record['context'])
        results={k:load_floor_assessment(a) for k,a in record['assessments'].items() if a['status']=='eligible'}
        if not results:continue
        await resolver._review_decisions([(by_id[k],'replay') for k in results],results,profile)
        for k,a in results.items():captured.append(dict(case=record['case'],id=k,status=a.status,assessment=asdict(a)))
        audit.save('replayed_approvals.json',captured)
        audit.event('old_approvals_replayed',case=record['case'],statuses={k:a.status for k,a in results.items()})
    await asyncio.gather(*(control(case,repeat) for repeat in (1,2) for case in controlled_cases()),*(real_job(case) for case in REAL_CONTEXTS))
    # Re-run two public searches using live source and financial enrichment.
    integrations=[]
    for name,business,context in REAL_CONTEXTS:
        record=next(r for r in real if r['case']==name)
        if not record['counts'].get('eligible'):continue
        engine=OptimalPropertyRecommender(PropertyStorage(str(BASE/'sample.db')),analyzer)
        engine.floor_profile_resolver=saved_resolvers[name]
        try:
            report=await asyncio.wait_for(engine.find_optimal_properties(business,business_format=context,center_lat=55.7558,center_lng=37.6173,
                search_radius_m=40000,min_area_sqm=20,max_area_sqm=350,max_price_month=600000,max_candidates=4,analysis_limit=4),timeout=180)
            audit.save(f'integration/{name}.json',asdict(report))
            entry=dict(case=name,numerical=report.numerical_matching_count,eligible=report.total_matching_in_db,evaluated=report.evaluated_count,
                complete=report.comparison_complete,errors=report.analysis_errors,winner=report.best_candidate.property_data['id'] if report.best_candidate else None,
                seconds=report.total_search_time_ms/1000)
        except Exception as exc:entry=dict(case=name,error_type=type(exc).__name__)
        integrations.append(entry);audit.event('integration_completed',**entry)
        if len(integrations)>=2:break
    by_case=defaultdict(list)
    for r in controlled:
        for c in r['checks']:by_case[c['id']].append(c['actual'])
    final=dict(controlled_evaluations=sum(len(r['checks']) for r in controlled),
        controlled_failed=[dict(case=r['case'],repeat=r['repeat'],**c) for r in controlled for c in r['checks'] if not c['passed']],
        controlled_status_flips={k:v for k,v in by_case.items() if len(set(v))>1},real_evaluations=sum(r['count'] for r in real),
        real_summaries=[{k:v for k,v in r.items() if k!='assessments'} for r in real],integration=integrations,
        replayed_old_approvals=[{k:v for k,v in r.items() if k!='assessment'} for r in captured],inference_calls=audit.TracedResolver.sequence,
        working_database_unchanged=before==hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest())
    audit.save('live_summary.json',final)
    audit.event('final_recheck_completed',control_failures=len(final['controlled_failed']),flips=len(final['controlled_status_flips']),unchanged=final['working_database_unchanged'])

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
