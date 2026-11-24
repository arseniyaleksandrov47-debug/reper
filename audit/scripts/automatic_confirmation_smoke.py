"""Bounded live check of the pilot unit, without opening the listings database."""
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from real_estate_engine import NvidiaCompetitorAnalyzer
from real_estate_engine.ai_contract import AI_DEADLINE


async def main():
    root=Path(__file__).resolve().parents[1]
    trial=root/'practical_trial/run_20261105_003117'
    latest=sorted(trial.glob('city_comparison_*/search_report.json'))[-1]
    report=json.loads(latest.read_text(encoding='utf-8-sig'))
    best=report['best_candidate']
    candidate=SimpleNamespace(property_data=best['property_data'],business_model_diagnostics=best['business_model_diagnostics'])
    analyzer=NvidiaCompetitorAnalyzer()
    concept='Кофейня навынос. Покупатели заходят с улицы без предварительной записи. Нужен первый этаж и свободный вход для покупателей. Площадь 30–80 м², аренда до 250000 рублей в месяц.'
    start=time.perf_counter();token=AI_DEADLINE.set(time.monotonic()+60)
    try:
        result=await asyncio.wait_for(analyzer.property_verifier.verify(candidate,concept,{}),60)
    finally:AI_DEADLINE.reset(token)
    output=dict(live_public_sources=True,live_independent_ai=True,property_id=candidate.property_data['id'],
                concept=concept,elapsed_s=round(time.perf_counter()-start,2),confirmation=result)
    dest=root/'records/automatic_confirmation_live.json'
    dest.write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(status=result['status'],source_count=len(result['sources']),models=result['models'],
                         reason_codes=[r['code'] for r in result['reasons']],elapsed_s=output['elapsed_s']),ensure_ascii=True))


if __name__=='__main__':asyncio.run(main())
