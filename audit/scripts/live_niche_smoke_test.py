"""Small live cross-niche checks; not exhaustive optimization certificates."""
import asyncio
import json
import logging
import pathlib
import sys
import time
from dataclasses import asdict

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from real_estate_engine import RealEstateAggregator

OUT = ROOT / "audit/large_test_2026-11-03"


async def main():
    aggregator = RealEstateAggregator(str(OUT / "snapshot.db"))
    results = []
    cases = {
        "Барбершоп": "Клиенты приходят по записи. Важны аренда и удобный пеший маршрут.",
        "ПВЗ": "Жители района пешком забирают онлайн-заказы. Нужен доступ посетителей в часы работы.",
    }
    for niche, concept in cases.items():
        start = time.perf_counter()
        try:
            report = await asyncio.wait_for(aggregator.find_optimal_properties(
                target_business=niche, business_concept=concept, center_lat=55.7558, center_lng=37.6173,
                search_radius_m=8000, min_area_sqm=40, max_area_sqm=100,
                max_price_month=200000, max_candidates=2), timeout=150)
            result = {"niche": niche, "elapsed_s": round(time.perf_counter() - start, 2), "report": asdict(report)}
            print(json.dumps({"niche": niche, "elapsed_s": result["elapsed_s"], "matching": report.total_matching_in_db,
                "evaluated": report.evaluated_count, "best_id": report.best_candidate.property_data["id"] if report.best_candidate else None,
                "direct_counts": [len(c.competition_report.direct_competitors) for c in report.ranked_candidates],
                "statuses": [c.competition_report.data_status for c in report.ranked_candidates],
                "errors": report.analysis_errors}, ensure_ascii=False), flush=True)
        except Exception as error:
            result = {"niche": niche, "error_type": type(error).__name__}
            print(json.dumps(result, ensure_ascii=False), flush=True)
        results.append(result)
        (OUT / "live_niches.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.getLogger().setLevel(logging.WARNING)
    asyncio.run(main())
