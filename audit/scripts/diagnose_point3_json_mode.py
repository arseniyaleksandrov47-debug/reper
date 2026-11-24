"""Compare JSON-object output with the same real input and full local contract.

Diagnostic only: a recorded exact-concept interpretation isolates the generator.
"""
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from real_estate_engine import NvidiaCompetitorAnalyzer
from real_estate_engine.ai_contract import AITextResponse
from real_estate_engine.ranking_policy import RankingProfileResolver
from run_acceptance_point3 import CASES
from dataclasses import asdict


async def main():
    out=ROOT/'audit/acceptance_point3/ai_diagnostic'
    prior=ROOT/'audit/acceptance_point3/run_191357_791788'
    recorded=next(json.loads(p.read_text('utf-8')) for p in prior.glob('ai_street_*.json')
                  if 'interpretations' in json.loads(json.loads(p.read_text('utf-8'))['text']))
    old=json.loads((ROOT/'audit/acceptance_point3/run_190750_767195/street_report.json').read_text('utf-8'))
    analyzer=NvidiaCompetitorAnalyzer()
    analyzer._call_scenario_contract_structured_sync=lambda *a:AITextResponse(recorded['text'],recorded['model'],recorded['metadata'])
    def generate(prompt,max_tokens,schema):
        payload=dict(model=analyzer.floor_model,messages=[dict(role='system',content='Верни только JSON по указанной схеме, без markdown.'),
            dict(role='user',content=prompt+'\nOUTPUT_JSON_SCHEMA\n'+json.dumps(schema,ensure_ascii=False))],
            max_tokens=max_tokens,temperature=0,chat_template_kwargs=dict(enable_thinking=False),response_format=dict(type='json_object'))
        analyzer.floor_request_pacer.wait_http_slot(5);started=time.perf_counter()
        response=analyzer.http_session.post(analyzer.endpoint,json=payload,timeout=45)
        record=dict(seconds=round(time.perf_counter()-started,3),status=response.status_code)
        if response.status_code==200:
            data=response.json();record['usage']=data.get('usage');record['finish']=data['choices'][0]['finish_reason']
            (out/'json_mode_raw.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
            (out/'json_mode_transport.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
            print(record,flush=True)
            if record['finish']!='stop':raise RuntimeError('Incomplete JSON diagnostic')
            return AITextResponse(data['choices'][0]['message']['content'],analyzer.floor_model,dict(transport='json_object',**record))
        (out/'json_mode_transport.json').write_text(json.dumps(record),encoding='utf-8')
        response.raise_for_status()
    analyzer._call_ranking_profile_structured_sync=generate
    case=CASES[0];started=time.perf_counter()
    profile=await RankingProfileResolver(analyzer).resolve(case['business'],case['concept'],old['ranking_profile']['metadata']['client_constraints'])
    (out/'json_mode_profile.json').write_text(json.dumps(asdict(profile),ensure_ascii=False,indent=2),encoding='utf-8')
    print(dict(status=profile.status,analysis_status=profile.analysis_status,seconds=round(time.perf_counter()-started,3),metadata=profile.metadata if profile.status!='ready' else 'validated'),flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
