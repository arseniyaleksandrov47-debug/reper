"""Live isolated proof calls on recorded facts; not current premises approval."""
import asyncio
import copy
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from two_integrated_runs import TracedAnalyzer,save,event
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine.ai_contract import AI_DEADLINE
from real_estate_engine.property_confirmation import CHECKS


async def main():
    prior=Path((ROOT/'audit/confirmation_flow/LATEST.txt').read_text(encoding='utf-8'))
    out=ROOT/'audit/confirmation_flow'/datetime.now().strftime('semantic_%Y%m%d_%H%M%S')
    out.mkdir(parents=True);(out.parent/'SEMANTIC_LATEST.txt').write_text(str(out),encoding='utf-8')
    analyzer=TracedAnalyzer(out);budget=NetworkBudget(6)
    analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    results=[]
    for name,business,concept,key in [('office_use','Офис','Офис для сотрудников. Посетители и продажи с улицы не планируются.','concept_use'),
        ('coffee_use','Кофейня навынос','Продажа кофе покупателям с улицы без записи.','concept_use'),
        ('coffee_access','Кофейня навынос','Покупатели свободно заходят с улицы без пропуска и записи.','access')]:
        source_name='staff_office' if name=='office_use' else 'walk_in'
        record=json.loads((prior/(source_name+'.json')).read_text(encoding='utf-8'))
        primary={k:dict(result='unknown',evidence_ids=[],reason='Outside this isolated probe') for k in CHECKS}
        primary[key]=copy.deepcopy(record['checks'][key]['primary'])
        timeout=45  # Same independent-proof budget as the production path.
        analyzer.current_case=name;token=AI_DEADLINE.set(time.monotonic()+timeout);start=time.perf_counter()
        try:
            proof,model=await asyncio.wait_for(analyzer.property_verifier._prove('Бизнес: '+business+'\nКонцепция: '+concept,record['evidence'],primary),timeout)
            result=dict(proof=proof[key],model=model)
        except Exception as exc:result=dict(error_type=type(exc).__name__)
        finally:AI_DEADLINE.reset(token)
        row=dict(case=name,check=key,seconds=round(time.perf_counter()-start,2),**result)
        results.append(row);save(out/(name+'.json'),dict(scope=__doc__,recorded_source=str(prior/(source_name+'.json')),result=row));event('semantic',**row)
    checks=dict(explicit_office_use_supported=results[0].get('proof',{}).get('verdict')=='supported',
                coffee_use_not_automatically_accepted=results[1].get('proof',{}).get('verdict') in ('unknown','unsupported'),
                public_customer_access_not_inferred_from_tenant_access=results[2].get('proof',{}).get('verdict') in ('unknown','unsupported'))
    save(out/'result.json',dict(scope=__doc__,rows=results,checks=checks,http=budget.records));event('completed',checks=checks)
    if not all(checks.values()):raise SystemExit(2)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
