"""Measure JSON output on the new cases, keeping the validated intent interpretation.

Only the abstract client-intent answer is replayed. Profile generation is live
and passes the complete production validators and priority compilation.
"""
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from validation_speed import CASES
from real_estate_engine import NvidiaCompetitorAnalyzer,RankingProfileResolver
from real_estate_engine.ai_contract import AI_DEADLINE,AITextResponse


async def main():
    base=Path((ROOT/'audit/validation_speed/LATEST.txt').read_text(encoding='utf-8').strip())
    out=base/'format_probe_corrected';out.mkdir(exist_ok=True)
    analyzer=NvidiaCompetitorAnalyzer(assessment_cache_path=None,environment_cache_path=None)
    results=[]
    for case in CASES:
        recorded=None
        for file in sorted((base/'baseline').glob('ai_'+case['key']+'_*.json')):
            value=json.loads(file.read_text(encoding='utf-8'))
            if 'interpretations' in json.loads(value['text']):recorded=value;break
        if recorded is None:continue
        analyzer._call_scenario_contract_structured_sync=lambda *a,**kw:AITextResponse(
            recorded['text'],recorded['model'],recorded['metadata'])
        calls=[]
        def infer(prompt,max_tokens,schema):
            started=time.monotonic()
            save_request=dict(prompt=prompt,max_tokens=max_tokens,schema=schema)
            (out/(case['key']+'_request.json')).write_text(json.dumps(save_request,ensure_ascii=False,indent=2),encoding='utf-8')
            try:
                response=analyzer._call_nvidia_sync(prompt,max_tokens,model_override=analyzer.floor_model,
                    timeout=45,response_schema=schema,response_format_type='json_object')
                (out/(case['key']+'_answer.json')).write_text(json.dumps(dict(text=response.text,metadata=response.metadata),ensure_ascii=False,indent=2),encoding='utf-8')
                return response
            finally:calls.append(round(time.monotonic()-started,3))
        analyzer._call_ranking_profile_structured_sync=infer
        resolver=RankingProfileResolver(analyzer)
        constraints=dict(target_area_sqm=None,min_area_sqm=case['min_area'],max_area_sqm=case['max_area'],
            max_price_month=case['rent'],max_price_sqm_year=None,competition_diameter_m=400,explicit_visit_context=None)
        token=AI_DEADLINE.set(time.monotonic()+90);started=time.monotonic()
        try:
            profile=await resolver._generate(case['business'],case['concept'],constraints)
            result=dict(case=case['key'],status=profile.status,seconds=round(time.monotonic()-started,3),calls=calls)
            (out/(case['key']+'_profile.json')).write_text(json.dumps(asdict(profile),ensure_ascii=False,indent=2),encoding='utf-8')
        except Exception as exc:
            result=dict(case=case['key'],error_type=type(exc).__name__,error=str(exc) if isinstance(exc,ValueError) else None,
                seconds=round(time.monotonic()-started,3),calls=calls)
        finally:AI_DEADLINE.reset(token)
        results.append(result)
        print(json.dumps(result,ensure_ascii=False),flush=True)
    (out/'result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
