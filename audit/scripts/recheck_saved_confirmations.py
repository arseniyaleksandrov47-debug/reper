"""Recheck published conditions of saved ranked candidates, without DB writes."""
import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'audit/scripts'))
from expanded_scenario_matrix import NetworkBudget
from two_integrated_runs import TracedAnalyzer,save,event
from real_estate_engine.ai_contract import AI_DEADLINE
from real_estate_engine.premises_requirements import physical_context_quotes
from real_estate_engine.property_confirmation import PropertyConfirmationVerifier
from real_estate_engine.storage import WORKSPACE_DB_PATH


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('report',type=Path)
    parser.add_argument('--limit',type=int,default=2)
    args=parser.parse_args()
    report=json.loads(args.report.read_text(encoding='utf-8'))
    out=ROOT/'audit/moscow_source_repairs'/datetime.now().strftime('confirmation_recheck_%Y%m%d_%H%M%S')
    out.mkdir(parents=True)
    before=hashlib.sha256(WORKSPACE_DB_PATH.read_bytes()).hexdigest()
    os.environ['NVIDIA_REQUESTS_PER_MINUTE']='40'
    os.environ['NVIDIA_MIN_REQUEST_INTERVAL_S']='1.6'
    analyzer=TracedAnalyzer(out)
    budget=NetworkBudget(24)
    analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    verifier=PropertyConfirmationVerifier(analyzer)
    profile=SimpleNamespace(**{**report['ranking_profile'],'criteria':[
        SimpleNamespace(**row) for row in report['ranking_profile']['criteria']]})
    params=dict(target_business=report['target_business'],
        min_area_sqm=report['client_min_area_sqm'],max_area_sqm=report['client_max_area_sqm'],
        max_price_month=report['client_max_price_month'],
        physical_context_quotes=physical_context_quotes(profile))
    rows=[]
    started=time.monotonic()
    for original in report['ranked_candidates'][:max(1,min(args.limit,4))]:
        candidate=SimpleNamespace(**original)
        identifier=str(candidate.property_data['external_id'])
        analyzer.current_case=identifier
        tick=time.monotonic()
        token=AI_DEADLINE.set(tick+90)
        try:
            result=await asyncio.wait_for(verifier.verify(candidate,profile.concept,params),90)
        except Exception as exc:
            result=dict(status='requires_confirmation',verification_error=type(exc).__name__)
        finally:
            AI_DEADLINE.reset(token)
        save(out/f'{identifier}.json',result)
        conditions=result.get('checks',{}).get('client_requirements',{})
        row=dict(external_id=identifier,address=candidate.property_data['address'],
            status=result['status'],sources=len(result.get('sources',[])),
            source_errors=result.get('source_errors'),
            photo=result.get('photo_analysis',{}).get('status'),
            extraction=result.get('requirement_extraction'),
            requirements=[dict(quote=item['client_quote'],result=item['result']) for item in conditions.get('items',[])],
            reasons=result.get('reasons'),verification_error=result.get('verification_error'),
            seconds=round(time.monotonic()-tick,2))
        rows.append(row)
        event('candidate_rechecked',**row)
    records=budget.records
    summary=dict(input_report=str(args.report.resolve()),candidates=rows,
        nvidia_requests=len(records),peak_rpm=max((sum(0<=entry['start']-other['start']<60
            for other in records) for entry in records),default=0),
        http_status=dict(Counter(row.get('status',row.get('error_type','unknown')) for row in records)),
        original_database_unchanged=before==hashlib.sha256(WORKSPACE_DB_PATH.read_bytes()).hexdigest(),
        seconds=round(time.monotonic()-started,2))
    save(out/'summary.json',summary)
    save(out/'http_records.json',records)
    event('confirmation_recheck_completed',directory=str(out),**summary)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
