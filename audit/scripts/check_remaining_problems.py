"""Bounded live audit in an isolated database; never initializes the working database."""
import asyncio
import hashlib
import json
import logging
import math
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from real_estate_engine import RealEstateAggregator

OUT = ROOT / "audit" / "remaining_problems_2026-11-03"
OUT.mkdir(parents=True, exist_ok=True)


def save(name, value):
    (OUT/name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def event(name, **values):
    print(json.dumps(dict(event=name, **values), ensure_ascii=False), flush=True)


async def main():
    original = ROOT/"data/moscow_commercial.db"
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    now = datetime.now(timezone(timedelta(hours=3)))
    result = {"started_moscow": now.isoformat(), "original_database_sha256": original_hash}
    agg = RealEstateAggregator(str(OUT/f"live_{now.strftime('%H%M%S')}.db"))
    started = time.perf_counter()
    try:
        summary = await asyncio.wait_for(agg.run_pipeline(30), timeout=80)
        save("source_results.json", summary)
        result["sources"] = summary["source_results"]
        result["quarantined_count"] = summary["batch_quarantined"]
        result["collection_seconds"] = round(time.perf_counter()-started, 3)
        with agg.storage._get_connection() as conn:
            rows = [dict(r) for r in conn.execute("SELECT id,source,external_id,lat,lng,address,last_seen_at FROM properties")]
        outliers = [r for r in rows if not (55 <= r["lat"] <= 57 and 36 <= r["lng"] <= 39)]
        save("coordinate_outliers.json", outliers)
        result["coordinate_outliers_count"] = len(outliers)
        result["received_rows_with_observation_time"] = sum(bool(r["last_seen_at"]) for r in rows)
        result["received_rows_count"] = len(rows)
        event("collection_finished", statuses={k:v["status"] for k,v in result["sources"].items()}, collected=len(rows), outliers=len(outliers))
    except Exception as exc:
        result["collection_error_type"] = type(exc).__name__
        event("collection_failed", error_type=type(exc).__name__)
    save("results.json", result)

    with sqlite3.connect(original.as_uri()+"?mode=ro", uri=True) as conn:
        centers = conn.execute("SELECT id,lat,lng FROM properties WHERE id IN (387,86)").fetchall()
    conn.close()
    population_results = []
    for property_id, lat, lng in centers:
        analyzer = agg.competitor_analyzer.population_analyzer
        fetch = analyzer._fetch_2gis_buildings_sync
        captured = {}
        def record(*args):
            raw = fetch(*args)
            captured["items"] = list(raw)
            captured["coverage_complete"] = getattr(raw, "coverage_complete", False)
            captured["warnings"] = getattr(raw, "warnings", [])
            return raw
        analyzer._fetch_2gis_buildings_sync = record
        try:
            started = time.perf_counter()
            report = await asyncio.wait_for(asyncio.to_thread(analyzer.analyze_circle_sync, lat, lng, 400), timeout=50)
            save(f"population_{property_id}.json", asdict(report))
            save(f"buildings_{property_id}.json", captured)
            population_results.append(dict(property_id=property_id, status=report.data_status,
                elapsed_seconds=round(time.perf_counter()-started, 3), current_residents=report.estimated_residents_count,
                current_apartments=report.total_apartments_count, planned_apartments=report.planned_apartments_count,
                non_operational_buildings=report.non_operational_buildings_count,
                excluded_outside_circle=report.buildings_outside_radius_count,
                excluded_missing_coordinates=report.buildings_without_coordinates_count,
                coverage_complete=report.building_coverage_complete, warnings=report.data_warnings))
            event("population_finished", **population_results[-1])
        except Exception as exc:
            population_results.append(dict(property_id=property_id, error_type=type(exc).__name__))
            event("population_failed", **population_results[-1])
        finally:
            analyzer._fetch_2gis_buildings_sync = fetch
        result["population"] = population_results
        save("results.json", result)

    result["original_database_unchanged"] = hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    result["finished_moscow"] = datetime.now(timezone(timedelta(hours=3))).isoformat()
    save("results.json", result)
    event("audit_finished", database_unchanged=result["original_database_unchanged"])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
