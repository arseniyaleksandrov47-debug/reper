"""Replay a captured false approval through the live semantic reviewer."""
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from real_estate_engine import NvidiaCompetitorAnalyzer
from archived_floor import load_floor_assessment
from real_estate_engine.floor_policy import BusinessFloorProfileResolver
OUT=ROOT/"audit/remaining_problems_2026-11-03"

async def main():
    recorded=json.loads((OUT/'semantic_review_repro_input.json').read_text(encoding='utf-8'))
    resolver=BusinessFloorProfileResolver(NvidiaCompetitorAnalyzer())
    profile=await resolver.resolve(recorded['profile']['target_business'],recorded['profile'].get('business_format') or None)
    prop=recorded['property']
    before=load_floor_assessment(recorded['assessment'])
    results={str(prop['id']):before}
    await resolver._review_decisions([(prop,'replay')],results,profile)
    reviewed=results[str(prop['id'])]
    result={'kind':'live semantic review of a captured unsupported approval, not a new listing observation',
        'property_id':prop['id'],'before':recorded['assessment'],'after':asdict(reviewed),
        'unsupported_approval_deferred':reviewed.status=='needs_verification' and reviewed.semantic_review_status=='unsupported'}
    (OUT/'semantic_review_repro_result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'deferred':result['unsupported_approval_deferred'],'status':reviewed.status,
                     'review':reviewed.semantic_review_status,'reason':reviewed.semantic_review_reason},ensure_ascii=False))
    if not result['unsupported_approval_deferred']: raise SystemExit(1)

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
