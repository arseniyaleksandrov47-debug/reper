"""Live real-listing check after deferring inferred rejections without visitor context."""
import asyncio
import hashlib
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'audit/scripts'))
from check_adaptive_floor_policy import ReadOnlyStorage
from real_estate_engine import NvidiaCompetitorAnalyzer
from real_estate_engine.floor_policy import BusinessFloorProfileResolver
OUT=ROOT/'audit/remaining_problems_2026-11-03'

async def main():
    path=ROOT/'data/moscow_commercial.db'
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    offers=ReadOnlyStorage().find_candidates(55.7558,37.6173,5000,min_area_sqm=40,max_area_sqm=140,max_price_month=250000)
    resolver=BusinessFloorProfileResolver(NvidiaCompetitorAnalyzer())
    profile=await resolver.resolve('Кофейня и пекарня')
    start=time.perf_counter()
    decisions=await resolver.assess_many(offers,profile)
    elapsed=time.perf_counter()-start
    start=time.perf_counter()
    repeated=await resolver.assess_many(offers,profile)
    result=dict(profile=asdict(profile),offers=offers,assessments={k:asdict(a) for k,a in decisions.items()},
        counts={s:sum(a.status==s for a in decisions.values()) for s in ('eligible','rejected','needs_verification')},
        elapsed_seconds=round(elapsed,3),cached_ms=round((time.perf_counter()-start)*1000,3),
        cache_preserves_results=all(asdict(a)==asdict(repeated[k]) for k,a in decisions.items()),
        working_database_unchanged=before==hashlib.sha256(path.read_bytes()).hexdigest())
    (OUT/'final_real_candidates.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ['counts','elapsed_seconds','cached_ms','cache_preserves_results','working_database_unchanged']},ensure_ascii=False))
    if result['counts']['rejected'] or not result['working_database_unchanged'] or not result['cache_preserves_results']:
        raise SystemExit(1)

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
