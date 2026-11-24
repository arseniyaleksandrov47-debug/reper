"""Refresh public unit facts and verification only, without replaying AI answers."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from real_estate_engine.ai_contract import AI_DEADLINE
from real_estate_engine.competitors import NvidiaCompetitorAnalyzer
from real_estate_engine.property_confirmation import PropertyConfirmationVerifier
from real_estate_engine.premises_requirements import physical_context_quotes


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    report=json.loads((args.run_dir/'first_search.json').read_text(encoding='utf-8'))
    request=json.loads((args.run_dir/'client_request.json').read_text(encoding='utf-8'))
    analyzer=NvidiaCompetitorAnalyzer();wire=[]
    def record(response,*unused,**kwargs):
        if response.status_code!=200:return
        data=response.json()
        for row in data.get('choices',[]):
            message=row.get('message') or {}
            wire.append(dict(model=data.get('model'),finish_reason=row.get('finish_reason'),usage=data.get('usage'),
                content_chars=len(message.get('content') or ''),reasoning_chars=len(message.get('reasoning_content') or '')))
    analyzer.http_session.hooks.setdefault('response',[]).append(record)
    verifier=PropertyConfirmationVerifier(analyzer);results=[]
    profile=SimpleNamespace(**report['ranking_profile'])
    profile.criteria=[SimpleNamespace(**c) for c in profile.criteria]
    context=physical_context_quotes(profile)
    started=time.perf_counter()
    for candidate in report['ranked_candidates']:
        token=AI_DEADLINE.set(time.monotonic()+55)
        try:
            result=await asyncio.wait_for(verifier.verify(SimpleNamespace(**candidate),request['concept'],dict(
                cost_scope='rent_only',target_business=request['business'],allowed_floors=request['allowed_floors'],
                physical_context_quotes=context,
                confirmation_constraints=dict(min_area_sqm=request['min_area'],max_area_sqm=request['max_area'],max_price_month=request['max_rent']))),55)
        except Exception as exc:result=dict(error_type=type(exc).__name__)
        finally:AI_DEADLINE.reset(token)
        results.append(dict(property_id=candidate['property_data']['id'],address=candidate['property_data']['address'],confirmation=result))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(dict(seconds=time.perf_counter()-started,results=results,wire=wire,
        http_attempts=analyzer.floor_request_pacer.http_attempts),ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(results=[dict(property_id=r['property_id'],status=r['confirmation'].get('status'),
        verification_error=r['confirmation'].get('verification_error')) for r in results],wire=wire),ensure_ascii=False))

if __name__=='__main__':asyncio.run(main())
