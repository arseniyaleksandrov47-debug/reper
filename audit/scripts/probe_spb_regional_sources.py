"""Live regional adapters and exact-unit source checks, without AI or working DB writes."""
import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from real_estate_engine.collectors.domclick import DomclickCollector
from real_estate_engine.collectors.municipal import MunicipalRentalCollector
from real_estate_engine.environment_cache import EnvironmentSourceCache
from real_estate_engine.competitors import YandexMapsRadiusCollector
from real_estate_engine.property_sources import PublicPropertySources
from real_estate_engine.spb_metro_source import SpbMetroFlowSource


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT/'audit/spb_regional_sources/live_regional.json')
    args = parser.parse_args()
    started = time.monotonic()
    municipal = MunicipalRentalCollector(city='spb')
    domclick = DomclickCollector(city='spb')
    metro = SpbMetroFlowSource()
    lots, offers, dataset = await asyncio.gather(municipal.collect(20), domclick.collect(120), metro.get_dataset())
    source = PublicPropertySources(SimpleNamespace(environment_source_cache=EnvironmentSourceCache(None),
        map_collector=YandexMapsRadiusCollector()))
    source.bind_collectors([domclick])
    checks = []
    for prop in offers[:1] + lots[:1]:
        result = await asyncio.to_thread(source.collect, asdict(prop))
        checks.append(dict(id=prop.external_id, source=prop.source.value, result=result))
    result = dict(elapsed_s=round(time.monotonic()-started, 2), municipal=[asdict(p) for p in lots],
        municipal_health=municipal.collection_summary(len(lots)), domclick_count=len(offers),
        domclick_health=domclick.collection_summary(len(offers)), metro=metro.diagnostics,
        metro_station_count=len(dataset.stations) if dataset else None, source_checks=checks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print(json.dumps(dict(output=str(args.output), municipal=len(lots), domclick=len(offers),
        metro=metro.diagnostics, checks=[dict(id=r['id'], sources=len(r['result']['sources']),
        failures=r['result']['failures']) for r in checks]), ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
