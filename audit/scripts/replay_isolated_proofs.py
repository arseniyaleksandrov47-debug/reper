"""New live proof checks on saved decisions. This is not a full ranking replay."""
import asyncio,copy,json,logging,sys,time
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
import retest_proof_fix as trace
from real_estate_engine import NvidiaCompetitorAnalyzer
from archived_floor import load_floor_profile

async def main():
    base=ROOT/'audit/proof_fix_2026-11-03'
    out=base/datetime.now(timezone(timedelta(hours=3))).strftime('isolated_%H%M%S_%f');out.mkdir()
    (base/'LATEST_ISOLATED.txt').write_text(str(out),encoding='utf-8')
    trace.OUT=out;trace.TracedResolver.gate=asyncio.Semaphore(2)
    analyzer=NvidiaCompetitorAnalyzer();jobs=asyncio.Semaphore(2);records=[];cases=[]
    original=ROOT/'audit/big_run_2026-11-03/run_193231_893406'
    recent=base/'full_222003_214630'
    props={str(p['id']):p for p in json.loads((original/'fresh_sample.json').read_text(encoding='utf-8'))}
    for folder,label in ((original,'original'),(recent,'v6')):
        for f in sorted((folder/'real').glob('*.json')):
            d=json.loads(f.read_text(encoding='utf-8'))
            for key,a in d['repeated_assessments'].items():
                if a['status']=='eligible':cases.append((label,d['case'],key,props[key],d['profile'] if 'profile' in d else json.loads((original/'real'/f.name).read_text(encoding='utf-8'))['profile'],a,None))
    for f in sorted((recent/'controlled').glob('*_1.json')):
        d=json.loads(f.read_text(encoding='utf-8'))
        for p in d['offers']:
            if p['expected']=='eligible':cases.append(('positive_control',d['case'],str(p['id']),p,d['profile'],d['assessments'][str(p['id'])],'eligible'))
    d=json.loads((recent/'controlled/mall_3.json').read_text(encoding='utf-8'))
    p=next(p for p in d['offers'] if p['id']=='mall_unknown')
    cases.append(('unknown_rejection','mall','mall_unknown',p,d['profile'],d['assessments']['mall_unknown'],'needs_verification'))
    async def job(index,case):
        async with jobs:
            label,name,key,prop,profile_data,prior,expected=case
            resolver=trace.TracedResolver(analyzer,f'{label}_{name}_{key}');profile=load_floor_profile(profile_data)
            payload=dict(candidate_id=key,**{k:prior[k] for k in ('status','reason','checks','missing_facts')})
            start=time.perf_counter();assessment=resolver._validate_assessment(payload,prop,profile)
            results={key:assessment}
            await resolver._verify_positive_proofs([(prop,None)],results,profile)
            result=dict(label=label,case=name,id=key,before=prior['status'],after=assessment.status,expected=expected,
                passed=assessment.status==expected if expected else None,seconds=time.perf_counter()-start,assessment=asdict(assessment))
            records.append(result);trace.save(f'decisions/{index:03}.json',result)
            trace.event('proof',label=label,case=name,id=key,before=prior['status'],after=assessment.status,expected=expected)
    trace.event('started_isolated_proofs',output=str(out),cases=len(cases))
    await asyncio.gather(*(job(i,c) for i,c in enumerate(cases)))
    summary=dict(cases=len(records),labelled=sum(r['expected'] is not None for r in records),
        labelled_passed=sum(r['passed'] is True for r in records),
        deferred=sum(r['before']=='eligible' and r['after']!='eligible' for r in records),
        http_attempts=analyzer.floor_request_pacer.http_attempts,throttle_responses=analyzer.floor_request_pacer.throttle_responses,
        failures=[{k:r[k] for k in ('label','case','id','expected','after')} for r in records if r['passed'] is False])
    trace.save('summary.json',summary);trace.event('finished_isolated_proofs',**summary)

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
