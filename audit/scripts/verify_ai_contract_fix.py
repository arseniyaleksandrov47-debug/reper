"""Live semantic/contract verification on frozen public inputs after contract fixes."""
import asyncio,hashlib,json,logging,sys,time
from collections import Counter
from dataclasses import asdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from real_estate_engine import NvidiaCompetitorAnalyzer
from real_estate_engine.floor_policy import BusinessFloorProfileResolver
from expanded_cases import controlled_cases,REAL_CONTEXTS
from expanded_holdout import CASES
OUT=ROOT/'audit/ai_contract_fix_2026-11-03';OUT.mkdir(exist_ok=True)

def save(name,data):
    (OUT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

class TracedResolver(BusinessFloorProfileResolver):
    sequence=0
    async def _infer(self,task,instructions,data,max_tokens):
        type(self).sequence+=1;index=type(self).sequence
        start=time.perf_counter();record=dict(task=task,input=data)
        try:
            response,model=await super()._infer(task,instructions,data,max_tokens)
            record.update(response=response,model=model,metadata=getattr(response,'metadata',{}))
            return response,model
        except Exception as exc:
            record['error_type']=type(exc).__name__;raise
        finally:
            record['seconds']=round(time.perf_counter()-start,3)
            save(f'trace_{index:03}.json',record)

async def main():
    before=hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest()
    analyzer=NvidiaCompetitorAnalyzer();gate=asyncio.Semaphore(2);records=[]
    cases=controlled_cases()+CASES
    if '--smoke' in sys.argv:cases=[cases[i] for i in (0,1,3,8)]
    async def one(case):
        async with gate:
            name,business,context,offers=case
            resolver=TracedResolver(analyzer);profile=await resolver.resolve(business,context)
            start=time.perf_counter()
            decisions=await resolver.assess_many([{k:v for k,v in p.items() if k!='expected'} for p in offers],profile)
            checks=[dict(id=p['id'],expected=p['expected'],actual=decisions[p['id']].status,
                         analysis_status=decisions[p['id']].analysis_status,
                         passed=decisions[p['id']].status!='eligible' if p['expected']=='not_eligible' else decisions[p['id']].status==p['expected']) for p in offers]
            record=dict(case=name,business=business,context=context,profile=asdict(profile),offers=offers,checks=checks,
                assessments={k:asdict(v) for k,v in decisions.items()},seconds=round(time.perf_counter()-start,3))
            records.append(record);save('live_cases.json',records)
            print(json.dumps(dict(case=name,checks=checks,seconds=record['seconds']),ensure_ascii=False),flush=True)
    await asyncio.gather(*(one(case) for case in cases))
    sample=json.loads((ROOT/'audit/expanded_test_2026-11-03/fresh_sample.json').read_text(encoding='utf-8'))
    real=[]
    for name,prop_id in [('coffee',197),('office',169)]:
        _,business,context=next(c for c in REAL_CONTEXTS if c[0]==name)
        prop=next(p for p in sample if p['id']==prop_id)
        resolver=TracedResolver(analyzer);profile=await resolver.resolve(business,context)
        decision=(await resolver.assess_many([prop],profile))[str(prop_id)]
        real.append(dict(case=name,property=prop,profile=asdict(profile),assessment=asdict(decision)))
        save('previous_false_approvals.json',real)
        print(json.dumps(dict(real_case=name,id=prop_id,status=decision.status,analysis_status=decision.analysis_status),ensure_ascii=False),flush=True)
    summary=dict(control_evaluations=sum(len(r['checks']) for r in records),
        passed=sum(c['passed'] for r in records for c in r['checks']),
        failures=[dict(case=r['case'],**c) for r in records for c in r['checks'] if not c['passed']],
        previous_approval_statuses={r['case']:r['assessment']['status'] for r in real},
        working_database_unchanged=before==hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest())
    save('live_summary.json',summary);print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
