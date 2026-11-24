"""Bounded live integration audit. Run from the project root."""
import asyncio
import copy
import hashlib
import json
import logging
import pathlib
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from real_estate_engine import RealEstateAggregator, PropertyStorage, OptimalPropertyRecommender

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "audit" / "large_test_2026-11-03"
OUT.mkdir(parents=True, exist_ok=True)
RESULT = {"started_moscow": datetime.now(timezone(timedelta(hours=3))).isoformat(), "http": []}


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def event(event_name, **value):
    print(json.dumps({"event": event_name, **value}, ensure_ascii=False), flush=True)


class RecordedSession:
    def __init__(self, session, name):
        self.session, self.name = session, name

    def __getattr__(self, name):
        return getattr(self.session, name)

    def _request(self, method, url, *args, **kwargs):
        start = time.perf_counter()
        parsed = urlsplit(url)
        record = {"source": self.name, "method": method, "host": parsed.hostname, "path": parsed.path}
        try:
            response = getattr(self.session, method)(url, *args, **kwargs)
            record["status"] = response.status_code
            record["bytes"] = len(response.content)
            return response
        except Exception as error:
            record["error_type"] = type(error).__name__
            raise
        finally:
            record["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 1)
            RESULT["http"].append(record)

    def get(self, url, *args, **kwargs):
        return self._request("get", url, *args, **kwargs)

    def post(self, url, *args, **kwargs):
        return self._request("post", url, *args, **kwargs)


class ProgressAnalyzer:
    def __init__(self, analyzer):
        self.analyzer = analyzer
        self.fns_collector = analyzer.fns_collector
        self.reports = {}

    async def analyze_circle(self, **kwargs):
        started = time.perf_counter()
        key = f"{kwargs['center_lat']:.7f},{kwargs['center_lng']:.7f}"
        if key in self.reports:
            event("location_replayed", key=key)
            return copy.deepcopy(self.reports[key])
        try:
            report = await self.analyzer.analyze_circle(**kwargs)
            self.reports[key] = report
            save("partial_live_reports.json", {k: asdict(v) for k, v in self.reports.items()})
            event("location_completed", key=key, status=report.data_status,
                  cards=report.total_cards_found, model=report.model_used,
                  elapsed_s=round(time.perf_counter() - started, 2),
                  population=report.population.data_status if report.population else "unavailable")
            return report
        except Exception as error:
            event("location_failed", error_type=type(error).__name__, elapsed_s=round(time.perf_counter() - started, 2))
            raise


async def main():
    original = ROOT / "data/moscow_commercial.db"
    RESULT["original_database_sha256"] = hashlib.sha256(original.read_bytes()).hexdigest()
    agg = RealEstateAggregator(str(OUT / f"fresh_ingestion_{datetime.now().strftime('%H%M%S')}.db"))
    for collector in agg.collectors:
        collector.session = RecordedSession(collector.session, collector.name)

    async def collect_one(collector):
        start = time.perf_counter()
        try:
            properties = await asyncio.wait_for(collector.collect(limit=30), timeout=75)
            entry = {"name": collector.name, "collected": len(properties),
                     "elapsed_s": round(time.perf_counter() - start, 2),
                     "geo_outliers": sum(not (55 <= p.lat <= 57 and 36 <= p.lng <= 39) for p in properties),
                     "invalid_prices_or_area": sum(p.price_month <= 0 or p.area_sqm <= 0 for p in properties)}
            agg.storage.upsert_batch(properties)
            save(f"offers_{collector.name}.json", [asdict(p) for p in properties])
        except Exception as error:
            entry = {"name": collector.name, "error_type": type(error).__name__, "collected": 0}
        event("collector_completed", **entry)
        return entry

    if "--skip-collect" in sys.argv:
        previous = json.loads((OUT / "live_results.json").read_text(encoding="utf-8"))
        for key in ("collectors", "fresh_database", "http"):
            RESULT[key] = previous[key]
        RESULT["reused_ingestion"] = True
        save("initial_network_failure.json", previous.get("narrow_search", {}))
    else:
        RESULT["collectors"] = await asyncio.gather(*(collect_one(c) for c in agg.collectors))
        RESULT["fresh_database"] = agg.storage.get_statistics()
    save("live_results.json", RESULT)
    event("ingestion_completed", stats=RESULT["fresh_database"])

    snapshot = PropertyStorage(str(OUT / "snapshot.db"))
    proxy = ProgressAnalyzer(agg.competitor_analyzer)
    engine = OptimalPropertyRecommender(snapshot, proxy)
    base = dict(target_business="Кофейня и пекарня", center_lat=55.7558, center_lng=37.6173,
                business_concept="Гости заходят с улицы за кофе и выпечкой. Важен поток у входа, затем аренда.",
                search_radius_m=5000, min_area_sqm=40, max_area_sqm=70,
                max_price_month=150000, max_candidates=5)
    start = time.perf_counter()
    try:
        report = await asyncio.wait_for(engine.find_optimal_properties(**base), timeout=180)
        save("live_narrow_search.json", asdict(report))
        RESULT["narrow_search"] = {"elapsed_s": round(time.perf_counter() - start, 2),
            "matching": report.total_matching_in_db, "evaluated": report.evaluated_count,
            "best_id": report.best_candidate.property_data["id"] if report.best_candidate else None,
            "errors": report.analysis_errors,
            "usable_competition_reports": sum(c.competition_report.data_status == "ok" for c in report.ranked_candidates)}
        event("narrow_search_completed", **RESULT["narrow_search"])
    except Exception as error:
        RESULT["narrow_search"] = {"error_type": type(error).__name__}
        event("narrow_search_failed", **RESULT["narrow_search"])
    save("live_results.json", RESULT)

    # Stop live ranking if the probe cannot obtain usable competitive data.
    if RESULT["narrow_search"].get("usable_competition_reports", 0):
        expanded = dict(base, min_area_sqm=40, max_area_sqm=140, max_price_month=250000)
        pool = snapshot.find_candidates(55.7558, 37.6173, 5000, min_area_sqm=40, max_area_sqm=140, max_price_month=250000)
        save("exhaustive_pool.json", pool)
        if len(pool) <= 15:
            start = time.perf_counter()
            try:
                full = await asyncio.wait_for(engine.find_optimal_properties(**dict(expanded, max_candidates=max(1, len(pool)))), timeout=360)
                save("live_exhaustive_search.json", asdict(full))
                RESULT["exhaustive_search"] = {"matching": len(pool), "evaluated": full.evaluated_count,
                    "elapsed_s": round(time.perf_counter() - start, 2),
                    "best_id": full.best_candidate.property_data["id"] if full.best_candidate else None,
                    "errors": full.analysis_errors}
                event("exhaustive_search_completed", **RESULT["exhaustive_search"])
                # Freeze all live observations for a fair shortlist comparison.
                class FrozenFns:
                    async def enrich_cards(self, cards):
                        return sum(bool(c.fns_inn) for c in cards)
                class FrozenAnalyzer:
                    fns_collector = FrozenFns()
                    async def analyze_circle(self, **kwargs):
                        key = f"{kwargs['center_lat']:.7f},{kwargs['center_lng']:.7f}"
                        if key not in proxy.reports:
                            raise RuntimeError("No live observation for this candidate")
                        return copy.deepcopy(proxy.reports[key])
                frozen_engine = OptimalPropertyRecommender(snapshot, FrozenAnalyzer())
                short = await frozen_engine.find_optimal_properties(**dict(expanded, max_candidates=5))
                save("live_default_shortlist_replay.json", asdict(short))
                RESULT["optimality_comparison"] = {
                    "method": "Same live observations; financial and environmental responses replayed for shortlist",
                    "all_pool_size": len(pool), "evaluated_all": full.evaluated_count,
                    "default_evaluated": short.evaluated_count,
                    "default_best_id": short.best_candidate.property_data["id"] if short.best_candidate else None,
                    "exhaustive_best_id": full.best_candidate.property_data["id"] if full.best_candidate else None,
                    "same_winner": bool(short.best_candidate and full.best_candidate and
                                        short.best_candidate.property_data["id"] == full.best_candidate.property_data["id"]),
                }
                event("optimality_compared", **RESULT["optimality_comparison"])
            except Exception as error:
                RESULT["exhaustive_search"] = {"error_type": type(error).__name__}
        else:
            RESULT["exhaustive_search"] = {"skipped": "Pool exceeds bounded live test budget", "matching": len(pool)}
    else:
        RESULT["exhaustive_search"] = {"skipped": "Live probe returned no usable competitive report"}
    RESULT["working_database_unchanged"] = hashlib.sha256(original.read_bytes()).hexdigest() == RESULT["original_database_sha256"]
    RESULT["finished_moscow"] = datetime.now(timezone(timedelta(hours=3))).isoformat()
    save("live_results.json", RESULT)
    event("live_audit_finished", unchanged=RESULT["working_database_unchanged"])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.getLogger().setLevel(logging.WARNING)
    asyncio.run(main())
