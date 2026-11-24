"""Replay completed, recorded NVIDIA proof responses; no network/current approval."""
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from real_estate_engine.property_confirmation import PropertyConfirmationVerifier,CHECKS


async def main():
    base=ROOT/'audit/confirmation_flow';recorded=base/'semantic_20261105_110452'
    scenarios=[('office_use','staff_office','concept_use','supported','Бизнес: Офис\nКонцепция: Офис для сотрудников. Посетители и продажи с улицы не планируются.'),
        ('coffee_access','walk_in','access','unsupported','Бизнес: Кофейня навынос\nКонцепция: Покупатели свободно заходят с улицы без пропуска и записи.')]
    rows=[]
    for name,origin,key,expected,concept in scenarios:
        answer_path=next(recorded.glob('ai_'+name+'_*.json'));answer=json.loads(answer_path.read_text(encoding='utf-8'))
        facts_path=base/'run_20261105_103236'/(origin+'.json');facts=json.loads(facts_path.read_text(encoding='utf-8'))
        assert answer['metadata']['finish_reason']=='stop'
        primary={k:dict(result='unknown',evidence_ids=[],reason='Outside replay') for k in CHECKS}
        primary[key]=facts['checks'][key]['primary']
        assert set(primary[key]['evidence_ids'])<=set(facts['evidence'])
        ai=SimpleNamespace(_call_property_proof_structured_sync=lambda *a,**kw:(answer['text'],answer['model']))
        verifier=PropertyConfirmationVerifier(ai,sources=SimpleNamespace())
        proof,model=await verifier._prove(concept,facts['evidence'],primary)
        row=dict(case=name,verdict=proof[key]['verdict'],expected=expected,passed=proof[key]['verdict']==expected,
            explanation_chars=len(proof[key]['reason']),full_explanation_preserved='reason_full' in proof[key],
            model=model,response_file=str(answer_path),response_sha256=hashlib.sha256(answer_path.read_bytes()).hexdigest(),
            facts_file=str(facts_path),scope=__doc__)
        rows.append(row)
    result=dict(scope=__doc__,rows=rows,all_passed=all(r['passed'] for r in rows))
    (base/'recorded_proof_replay.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(all_passed=result['all_passed'],cases=[dict(case=r['case'],verdict=r['verdict']) for r in rows])))
    if not result['all_passed']:raise SystemExit(1)


if __name__=='__main__':asyncio.run(main())
