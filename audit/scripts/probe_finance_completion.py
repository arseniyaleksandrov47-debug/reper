"""Bounded live legal-entity completion on recorded public cards, no DB writes."""
import asyncio
import json
import sys
import time
from pathlib import Path
from dataclasses import asdict
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from real_estate_engine.competitors import MapBusinessCard
from real_estate_engine.fns import FnsBrowserRevenueCollector


async def main():
    path=ROOT/'audit/confirmation_flow/run_20261105_153151/warm_search.json'
    source=json.loads(path.read_text(encoding='utf-8'))['best_candidate']['competition_report']
    cards=[MapBusinessCard(**v['card']) for v in source['direct_competitors']]
    collector=FnsBrowserRevenueCollector();started=time.perf_counter();error=None
    try:count=await asyncio.wait_for(collector.enrich_cards(cards),25)
    except Exception as exc:count=0;error=type(exc).__name__
    result=dict(scope=__doc__,source=str(path),seconds=round(time.perf_counter()-started,2),enriched=count,error=error,
                cards=[dict(org_id=c.org_id,title=c.title,diagnostics=c.fns_diagnostics,revenue_rub=c.fns_revenue_rub) for c in cards])
    target=ROOT/'audit/source_completion/finance_completion.json';target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(seconds=result['seconds'],enriched=count,error=error,statuses=[dict(id=c.org_id,match=c.fns_match_status,finance=c.fns_diagnostics.get('finance_status')) for c in cards])))


if __name__=='__main__':asyncio.run(main())
