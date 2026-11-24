"""Fresh city collection in an isolated audit database; no AI inference."""
import argparse
import asyncio
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from real_estate_engine import RealEstateAggregator
from real_estate_engine.cities import get_city


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--city', required=True)
    parser.add_argument('--limit', type=int, default=30)
    args = parser.parse_args()
    city = get_city(args.city)
    out = ROOT / 'audit/city_sources' / datetime.now().strftime('run_%Y%m%d_%H%M%S')
    out.mkdir(parents=True)
    original = ROOT / 'data/moscow_commercial.db'
    before = hashlib.sha256(original.read_bytes()).hexdigest()
    aggregator = RealEstateAggregator(str(out / 'inventory.sqlite3'), city=city)
    result = await aggregator.run_pipeline(args.limit)
    properties = aggregator.storage.find_candidates(target_lat=city.center[0], target_lng=city.center[1],
        radius_meters=city.search_radius_m, max_listing_age_hours=24)
    result['sample'] = [{key: row.get(key) for key in ('source', 'external_id', 'address', 'lat', 'lng',
        'area_sqm', 'price_month', 'floor', 'url')} for row in properties[:20]]
    result['original_database_unchanged'] = hashlib.sha256(original.read_bytes()).hexdigest() == before
    (out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(directory=str(out), city=city.key, collected=result['batch_collected'],
        accepted=result['batch_saved_or_updated'], sources=result['source_results']), ensure_ascii=False))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
