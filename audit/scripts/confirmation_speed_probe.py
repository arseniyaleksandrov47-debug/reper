"""Live proof-model comparison on fixed evidence, not current lease approval."""
import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from two_integrated_runs import TracedAnalyzer,save,event
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine.ai_contract import AI_DEADLINE
from real_estate_engine.property_confirmation import CHECKS


async def main():
    out=ROOT/'audit/confirmation_speed'/datetime.now().strftime('probe_%Y%m%d_%H%M%S')
    out.mkdir(parents=True);(out.parent/'PROBE_LATEST.txt').write_text(str(out),encoding='utf-8')
    prior=ROOT/'audit/confirmation_flow/run_20261105_112537'
    source=json.loads((prior/'staff_office.json').read_text(encoding='utf-8'))
    facts=source['evidence'];available='s0:freeDate';use='s0:destination';access='s0:description'
    cases=[
        ('office_use','Офис для сотрудников. Посетители и продажи с улицы не планируются.','concept_use','pass',[use],{'supported'}),
        ('coffee_use','Продажа кофе навынос покупателям с улицы.','concept_use','pass',[use],{'unknown','unsupported'}),
        ('public_access','Любой покупатель свободно проходит с улицы без записи и пропуска.','access','pass',[access],{'unknown','unsupported'}),
        ('tenant_access','Помещение для работы сотрудников. Нужен доступ арендатора к помещению.','access','pass',[access],{'supported'}),
        ('published_free','Аренда помещения для работы.','availability','pass',[available],{'supported'}),
        ('alternative_uses','Мастерская ремонта приборов без посетителей.','concept_use','pass',['s0:destination'],{'supported'}),
        ('explicit_ban','Мастерская ремонта приборов.','concept_use','pass',['s0:destination'],{'unsupported'}),
        ('conflicting_access','Нужен свободный проход покупателей без пропуска.','access','pass',['s0:description'],{'unsupported','unknown'}),
    ]
    analyzer=TracedAnalyzer(out);budget=NetworkBudget(12);analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    model=sys.argv[1] if len(sys.argv)>1 else 'mistralai/mistral-large-2-instruct'
    if len(sys.argv)>2:cases=cases[:int(sys.argv[2])]
    def proof_call(prompt,max_tokens,*,response_schema):
        return analyzer._call_nvidia_sync(prompt,800,model_override=model,timeout=25,
            response_schema=response_schema,response_format_type='json_object',connect_timeout_s=5,
            reasoning_effort=sys.argv[3] if len(sys.argv)>3 else None)
    analyzer._call_property_proof_structured_sync=proof_call
    rows=[]
    for name,concept,key,result,refs,expected in cases:
        catalog=json.loads(json.dumps(facts))
        if name=='alternative_uses':catalog[use]['value']='Допускается офис, мастерская ремонта приборов или склад.'
        if name=='explicit_ban':catalog[use]['value']='Работа мастерской ремонта приборов запрещена; допускается только офис.'
        if name=='conflicting_access':catalog['s0:building_context']['value']='Для любых посетителей обязателен пропуск и согласование охраны.'
        claims={k:dict(result='unknown',reason='Outside this case',evidence_ids=[]) for k in CHECKS}
        claims[key]=dict(result=result,evidence_ids=refs,reason='Controlled claim')
        analyzer.current_case=name;started=time.perf_counter();token=AI_DEADLINE.set(time.monotonic()+25)
        try:proof,used=await analyzer.property_verifier._prove(concept,catalog,claims)
        finally:AI_DEADLINE.reset(token)
        row=dict(case=name,verdict=proof[key]['verdict'],proof=proof[key],model=used,seconds=round(time.perf_counter()-started,2))
        row['passed']=used==model and row['verdict'] in expected
        rows.append(row);event('case',**row)
        if proof[key].get('status_code') in (400,403,404):break
    result=dict(scope=__doc__,model=model,rows=rows,all_passed=all(r['passed'] for r in rows),http=budget.records)
    save(out/'result.json',result);event('completed',all_passed=result['all_passed'],http_attempts=len(budget.records))
    if not result['all_passed']:raise SystemExit(1)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
