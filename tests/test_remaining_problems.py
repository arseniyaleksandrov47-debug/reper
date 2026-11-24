"""Regressions for population, exhaustive ranking and source freshness."""
import asyncio
import copy
import json
import sqlite3
import statistics
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from test_engine_regressions import (
    LAT, LNG, SEARCH_CONCEPT, candidate, competition_report, memory_storage, population_analyzer, property_offer,
)
from test_collector_floors import collector_with_response
from floor_ai_fixtures import FixtureFloorAnalyzer, ranking_fixture_response
from real_estate_engine.ranking_policy import validate_profile
from real_estate_engine import OptimalPropertyRecommender, PlatformSource, PropertyStorage, RealEstateAggregator
from real_estate_engine.collectors import CianCollector, DomclickCollector, YandexRealtyCollector, AvitoCollector, InvestMoscowCollector
from real_estate_engine.collectors.base import BaseCollector
from real_estate_engine.population import BuildingFetchResult
from real_estate_engine.population import ResidentialPopulationAnalyzer
from real_estate_engine.cities import MOSCOW


def building(i="1", **changes):
    result = dict(id=i, name="Дом", point=dict(lat=LAT, lon=LNG), purpose_name="Жилой дом",
                  structure_info=dict(apartments_count=100), floors=dict(ground_count=5))
    result.update(changes)
    return result


def environment(traffic=30):
    foot = SimpleNamespace(estimated_daily_pedestrians=traffic * 200, estimated_peak_hourly=traffic * 20, traffic_index=traffic,
                           hourly_profile=None, nearest_metros=[])
    return competition_report(foot_traffic=foot)


class PopulationIntegrityTests(unittest.TestCase):
    def test_truncated_2gis_pages_do_not_claim_complete_coverage(self):
        analyzer = ResidentialPopulationAnalyzer.__new__(ResidentialPopulationAnalyzer)
        analyzer._2gis_key, analyzer._key_refreshed = "fixture", True
        def get(*args, **kwargs):
            page = int(kwargs["params"]["page"])
            payload = {"meta":{"code":200}, "result":{"total":200,"items":[{"id":str(page*100+i)} for i in range(50)]}}
            return SimpleNamespace(status_code=200,content=json.dumps(payload).encode())
        analyzer.session = SimpleNamespace(get=get)
        result = analyzer._fetch_2gis_buildings_sync(LAT,LNG,200)
        self.assertEqual(len(result),150)
        self.assertFalse(result.coverage_complete)
        self.assertTrue(result.warnings)

    def test_radius_cap_is_reported_even_when_source_pages_complete(self):
        analyzer = ResidentialPopulationAnalyzer.__new__(ResidentialPopulationAnalyzer)
        analyzer._2gis_key, analyzer._key_refreshed = "fixture", True
        analyzer.session = SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status_code=200,content=b'{"result":{"total":0,"items":[]}}'))
        result = analyzer._fetch_2gis_buildings_sync(LAT,LNG,1000)
        self.assertFalse(result.coverage_complete)
        self.assertTrue(result.warnings)

    def test_inconsistent_source_total_does_not_claim_complete_coverage(self):
        analyzer = ResidentialPopulationAnalyzer.__new__(ResidentialPopulationAnalyzer)
        analyzer._2gis_key, analyzer._key_refreshed = "fixture", True
        analyzer.session = SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status_code=200,content=b'{"result":{"total":200,"items":[]}}'))
        result = analyzer._fetch_2gis_buildings_sync(LAT,LNG,200)
        self.assertFalse(result.coverage_complete)
        self.assertTrue(result.warnings)

    def test_construction_apartments_are_future_capacity_not_current_residents(self):
        result = population_analyzer([building(), building("2", purpose_name="Строящийся жилой дом")]).analyze_circle_sync(LAT, LNG)
        self.assertEqual(result.total_apartments_count, 100)
        self.assertEqual(result.estimated_residents_count, 201)
        self.assertEqual(result.residential_buildings_count, 1)
        self.assertEqual(result.planned_residential_buildings_count, 1)
        self.assertEqual(result.planned_apartments_count, 100)
        self.assertEqual(result.non_operational_buildings_count, 1)

    def test_construction_office_has_no_current_workers(self):
        result = population_analyzer([building(purpose_name="Строящийся бизнес-центр", structure_info={})]).analyze_circle_sync(LAT, LNG)
        self.assertEqual(result.estimated_office_workers_count, 0)
        self.assertFalse(result.buildings_data_available)

    def test_future_year_excluded_even_without_construction_label(self):
        result = population_analyzer([building(structure_info=dict(apartments_count=100, year_of_construction=datetime.now(timezone.utc).year+1))]).analyze_circle_sync(LAT, LNG)
        self.assertEqual(result.estimated_residents_count, 0)
        self.assertEqual(result.planned_apartments_count, 100)

    def test_demolished_building_is_not_future_housing(self):
        result = population_analyzer([building(purpose_name="Снесённый жилой дом")]).analyze_circle_sync(LAT, LNG)
        self.assertEqual(result.estimated_residents_count, 0)
        self.assertEqual(result.planned_apartments_count, 0)
        self.assertEqual(result.non_operational_buildings_count, 1)

    def test_exact_radius_excludes_building_at_220m(self):
        result = population_analyzer([building(point=dict(lat=LAT+220/111195, lon=LNG))]).analyze_circle_sync(LAT, LNG, 400)
        self.assertEqual(result.estimated_residents_count, 0)
        self.assertEqual(result.buildings_outside_radius_count, 1)

    def test_missing_and_invalid_coordinates_are_not_placed_inside_circle(self):
        for point in ({}, {"lat": "nan", "lon": LNG}, {"lat": 100, "lon": LNG}):
            with self.subTest(point=point):
                result = population_analyzer([building(point=point)]).analyze_circle_sync(LAT, LNG)
                self.assertEqual(result.estimated_residents_count, 0)
                self.assertEqual(result.buildings_without_coordinates_count, 1)

    def test_malformed_passport_numbers_do_not_abort_valid_buildings(self):
        result = population_analyzer([building(structure_info=dict(apartments_count="unknown", year_of_construction="-"), floors=dict(ground_count="unknown")), building("2")]).analyze_circle_sync(LAT, LNG)
        self.assertEqual(result.residential_buildings_count, 2)
        self.assertGreaterEqual(result.total_apartments_count, 100)

    def test_building_failure_preserves_prices(self):
        analyzer = population_analyzer(prices=([500000], [25]))
        def failure(*args):
            raise RuntimeError("fixture")
        analyzer._fetch_2gis_buildings_sync = failure
        result = analyzer.analyze_circle_sync(LAT, LNG)
        self.assertEqual(result.median_housing_price_sqm_rub, 500000)
        self.assertEqual(result.estimated_residents_count, 0)
        self.assertEqual(result.data_status, "partial")

    def test_prices_failure_preserves_buildings(self):
        analyzer = population_analyzer([building()])
        def failure(*args):
            raise RuntimeError("fixture")
        analyzer._fetch_yandex_realty_prices_sync = failure
        self.assertEqual(analyzer.analyze_circle_sync(LAT, LNG).estimated_residents_count, 201)

    def test_fetch_coverage_survives_analysis(self):
        analyzer = population_analyzer(prices=([500000], [25]))
        analyzer._fetch_2gis_buildings_sync = lambda *a: BuildingFetchResult([building()], coverage_complete=False, warnings=["truncated fixture"])
        result = analyzer.analyze_circle_sync(LAT, LNG)
        self.assertEqual(result.data_status, "partial")
        self.assertFalse(result.building_coverage_complete)
        self.assertIn("truncated fixture", result.data_warnings)

    def test_covered_building_fetch_subdivides_and_deduplicates_truncated_circle(self):
        analyzer = population_analyzer(prices=([500000], [25]))
        calls = []
        def fetch(lat, lng, radius_m):
            calls.append((round(lat, 5), round(lng, 5), radius_m))
            if len(calls) == 1:
                return BuildingFetchResult([building("b0")], coverage_complete=False,
                    warnings=["2ГИС: достигнут лимит страниц, охват зданий неполный"])
            idx = len(calls)
            return BuildingFetchResult([building("b0"), building(f"b{idx}", point=dict(lat=LAT, lon=LNG))], coverage_complete=True)
        analyzer._fetch_2gis_buildings_sync = fetch
        result = analyzer.analyze_circle_sync(LAT, LNG, 500)
        self.assertTrue(result.building_coverage_complete)
        self.assertEqual(len(calls), 8)
        self.assertEqual(result.residential_buildings_count, 8)
        self.assertEqual(result.data_status, "ok")

    def test_invalid_circle_rejected_before_requests(self):
        for diameter in (0, -1, float("inf"), float("nan")):
            with self.subTest(diameter=diameter), self.assertRaises(ValueError):
                population_analyzer().analyze_circle_sync(LAT, LNG, diameter)


class ExhaustiveRankingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage, conn = memory_storage()
        self.addCleanup(conn.close)
        self.storage.upsert_batch([property_offer(str(i), lat=LAT+i*.001, area_sqm=80 if i<6 else 250,
                                                price_month=100000+i*1000, price_sqm_year=(100000+i*1000)*12/(80 if i<6 else 250)) for i in range(1,7)])
        class Analyzer(FixtureFloorAnalyzer):
            def __init__(self):
                self.calls = []
                self.fail_lat = None
            async def analyze_circle(inner, **kw):
                inner.calls.append(kw["center_lat"])
                if kw["center_lat"] == inner.fail_lat:
                    raise RuntimeError("fixture failure")
                return environment(95) if kw["center_lat"] > LAT+.0055 else environment()
        self.analyzer = Analyzer()
        self.engine = OptimalPropertyRecommender(self.storage, self.analyzer)
        # Hold scoring constant while testing exhaustive search orchestration.
        async def resolve(*args,**kwargs):
            response=ranking_fixture_response(SEARCH_CONCEPT)
            return validate_profile(response,SEARCH_CONCEPT,metadata={'client_constraints':{'max_price_month':250000}})
        self.engine.ranking_profile_resolver.resolve=resolve
        self.kwargs = dict(center_lat=LAT, center_lng=LNG, search_radius_m=5000,
                           max_price_month=250000, business_concept=SEARCH_CONCEPT)

    async def test_default_compares_all_and_finds_winner_beyond_old_shortlist(self):
        report = await self.engine.find_optimal_properties("Кофейня", max_candidates=1, **self.kwargs)
        self.assertEqual(report.evaluated_count, 6)
        self.assertEqual(len(report.ranked_candidates), 1)
        self.assertEqual(len(self.analyzer.calls), 6)
        self.assertEqual(report.best_candidate.property_data["external_id"], "6")
        self.assertTrue(report.comparison_complete)

    async def test_result_count_cannot_change_winner_or_score(self):
        one = await self.engine.find_optimal_properties("Кофейня", max_candidates=1, **self.kwargs)
        five = await self.engine.find_optimal_properties("Кофейня", max_candidates=5, **self.kwargs)
        self.assertEqual(one.best_candidate.property_data["id"], five.best_candidate.property_data["id"])
        self.assertEqual(one.best_candidate.unrounded_total_score, five.best_candidate.unrounded_total_score)

    async def test_explicit_shortcut_is_marked_incomplete_and_keeps_scores(self):
        full = await self.engine.find_optimal_properties("Кофейня", max_candidates=6, **self.kwargs)
        short = await self.engine.find_optimal_properties("Кофейня", max_candidates=6, analysis_limit=5, **self.kwargs)
        self.assertEqual(short.evaluated_count, 5)
        self.assertFalse(short.comparison_complete)
        self.assertTrue(any("пропущен" in w for w in short.ranking_warnings))
        by_id = {c.property_data["id"]: c for c in full.ranked_candidates}
        for c in short.ranked_candidates:
            self.assertEqual(c.unrounded_total_score, by_id[c.property_data["id"]].unrounded_total_score)

    async def test_environment_failure_does_not_change_remaining_scores(self):
        full = await self.engine.find_optimal_properties("Кофейня", max_candidates=6, **self.kwargs)
        self.analyzer.fail_lat = LAT+.002
        partial = await self.engine.find_optimal_properties("Кофейня", max_candidates=6, **self.kwargs)
        self.assertFalse(partial.comparison_complete)
        by_id = {c.property_data["id"]: c for c in full.ranked_candidates}
        for c in partial.ranked_candidates:
            self.assertEqual(c.unrounded_total_score, by_id[c.property_data["id"]].unrounded_total_score)

    async def test_invalid_analysis_limits(self):
        for limit in (0, -1, 1.5, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                await self.engine.find_optimal_properties("Кофейня", analysis_limit=limit, **self.kwargs)

    def test_close_scores_sort_before_display_rounding(self):
        # A small flow advantage is visible only before display rounding.
        pairs = [(candidate(1), environment(50)), (candidate(2), environment(50.01))]
        profile = validate_profile(ranking_fixture_response(SEARCH_CONCEPT), SEARCH_CONCEPT)
        ranked = self.engine._score_and_rank_candidates(pairs, profile)
        self.assertEqual(ranked[0].total_score, ranked[1].total_score)
        self.assertEqual(ranked[0].property_data["id"], 2)
        self.assertGreater(ranked[0].unrounded_total_score, ranked[1].unrounded_total_score)

    def test_weight_sensitivity_detects_reversal_beyond_display_tie(self):
        a = SimpleNamespace(property_data={"id":1}, scoring_components={
            'audience':{'contribution_lower':40},'cost':{'contribution_lower':10}})
        b = SimpleNamespace(property_data={"id":2}, scoring_components={
            'audience':{'contribution_lower':10},'cost':{'contribution_lower':38}})
        self.assertEqual(self.engine._weight_sensitive_challengers([a,b]), [2])

    def test_dominating_candidate_is_stable_under_positive_weights(self):
        a = SimpleNamespace(property_data={"id":1}, scoring_components={
            'audience':{'contribution_lower':40},'cost':{'contribution_lower':40}})
        b = SimpleNamespace(property_data={"id":2}, scoring_components={
            'audience':{'contribution_lower':10},'cost':{'contribution_lower':10}})
        self.assertEqual(self.engine._weight_sensitive_challengers([a,b]), [])


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.storage, self.conn = memory_storage()
        self.addCleanup(self.conn.close)

    def test_upsert_records_observation_time(self):
        self.storage.upsert_property(property_offer())
        row = self.conn.execute("SELECT * FROM properties").fetchone()
        self.assertIsNotNone(row["last_seen_at"])
        self.assertGreaterEqual(datetime.fromisoformat(row["last_seen_at"]), datetime(2026,1,1))

    def test_freshness_filter_excludes_unknown_and_old(self):
        self.storage.upsert_batch([property_offer("fresh"), property_offer("old", lat=LAT+.001), property_offer("unknown", lat=LAT+.002)])
        self.conn.execute("UPDATE properties SET last_seen_at='2001-01-01 00:00:00' WHERE external_id='old'")
        self.conn.execute("UPDATE properties SET last_seen_at=NULL WHERE external_id='unknown'")
        self.conn.commit()
        rows = self.storage.find_candidates(LAT, LNG, 5000, max_listing_age_hours=24)
        self.assertEqual([r["external_id"] for r in rows], ["fresh"])
        self.assertEqual(len(self.storage.find_candidates(LAT, LNG, 5000)), 3)

    def test_latest_copy_wins_deduplication(self):
        self.storage.upsert_batch([property_offer("old"), property_offer("fresh", source=PlatformSource.AVITO)])
        self.conn.execute("UPDATE properties SET last_seen_at='2001-01-01 00:00:00' WHERE external_id='old'")
        self.conn.commit()
        self.assertEqual(self.storage.find_candidates(LAT, LNG)[0]["external_id"], "fresh")

    def test_quarantined_old_copy_is_excluded_until_source_correction(self):
        prop = property_offer()
        self.storage.upsert_property(prop)
        self.storage.quarantine_batch([(prop, "fixture inconsistent coordinates")])
        self.assertEqual(self.storage.find_candidates(LAT, LNG), [])
        self.assertEqual(self.storage.get_statistics()["quarantined_offers"], 1)
        self.storage.upsert_property(prop)
        self.assertEqual(len(self.storage.find_candidates(LAT, LNG)), 1)
        self.assertEqual(self.storage.get_statistics()["quarantined_offers"], 0)

    def test_failure_preserves_last_success_and_stored_ads(self):
        self.storage.upsert_property(property_offer())
        self.storage.record_source_health("CIAN", dict(status="ok", collected=1, warnings=[]))
        self.conn.execute("UPDATE source_health SET last_success_at='2001-01-01 00:00:00'")
        self.conn.commit()
        self.storage.record_source_health("CIAN", dict(status="unavailable", collected=0, warnings=["HTTP 403"]))
        stats = self.storage.get_statistics()
        self.assertEqual(stats["source_health"]["CIAN"]["last_success_at"], "2001-01-01 00:00:00")
        self.assertEqual(stats["total_offers"], 1)

    def test_legacy_schema_migration_does_not_invent_freshness(self):
        schema = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='properties'").fetchone()[0]
        schema = schema.replace("last_seen_at TIMESTAMP,", "")
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp)/"legacy.db")
            with sqlite3.connect(path) as conn:
                conn.execute(schema)
                conn.execute("INSERT INTO properties(source,external_id,dedup_hash,lat,lng,price_month,price_sqm_year,area_sqm) VALUES ('cian','1','hash',55.75,37.61,100000,24000,50)")
            conn.close()
            migrated = PropertyStorage(path)
            with migrated._get_connection() as conn:
                self.assertIsNone(conn.execute("SELECT last_seen_at FROM properties").fetchone()[0])


class CollectorHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_quarantined_records_do_not_count_as_usable_data(self):
        storage,conn = memory_storage()
        self.addCleanup(conn.close)
        class BadCoordinates:
            name="bad_coordinates"
            async def collect(self,limit):
                return [property_offer(lat=59.383672,lng=28.46356)]
            def collection_summary(self,count):
                return dict(status="ok",collected=count,warnings=[],coverage_complete=False)
        agg=RealEstateAggregator.__new__(RealEstateAggregator)
        agg.city=MOSCOW
        agg.storage,agg.collectors=storage,[BadCoordinates()]
        agg.collection_bounds=(55.,57.,36.,39.)
        result=await agg.run_pipeline(1)
        self.assertEqual(result["data_status"],"unavailable")
        self.assertEqual(result["batch_saved_or_updated"],0)
        self.assertEqual(result["batch_quarantined"],1)

    def test_coordinate_validation_is_configurable_and_does_not_invent_coordinates(self):
        agg = RealEstateAggregator.__new__(RealEstateAggregator)
        agg.collection_bounds = (55.,57.,36.,39.)
        prop = property_offer(lat=59.383672, lng=28.46356)
        self.assertIsNotNone(agg._collection_quality_issue(prop))
        self.assertEqual((prop.lat, prop.lng), (59.383672,28.46356))
        agg.collection_bounds = None
        self.assertIsNone(agg._collection_quality_issue(prop))

    def test_nonfinite_source_numbers_are_rejected(self):
        agg = RealEstateAggregator.__new__(RealEstateAggregator)
        for changes in (dict(lat=float("nan")), dict(price_month=float("inf")), dict(area_sqm=-1)):
            self.assertIsNotNone(agg._collection_quality_issue(property_offer(**changes)))

    async def test_denied_source_is_unavailable_not_empty(self):
        c = collector_with_response(AvitoCollector, text="denied")
        c.session.get = lambda *a, **k: SimpleNamespace(status_code=403)
        c.rate_limiter = SimpleNamespace(record_status=lambda *a: None)
        c.domain = "example.test"
        c.record_status = lambda code: BaseCollector.record_status(c, code)
        result = await c.collect(1)
        health = c.collection_summary(len(result))
        self.assertEqual(health["status"], "unavailable")
        self.assertEqual(health["http_statuses"], [403])

    async def test_valid_empty_is_distinct_from_invalid_response(self):
        for payload, expected in (({"data":{"offersSerialized":[]}}, "empty"), ({"error":"blocked"}, "unavailable")):
            c = collector_with_response(CianCollector, payload)
            result = await c.collect(1)
            self.assertEqual(c.collection_summary(len(result))["status"], expected)

    async def test_yandex_missing_state_does_not_report_empty_market(self):
        c = collector_with_response(YandexRealtyCollector, text="<html>captcha</html>")
        await c.collect(1)
        self.assertEqual(c.collection_summary(0)["status"], "unavailable")

    async def test_direct_collectors_reject_invalid_limit(self):
        for cls in (AvitoCollector, CianCollector, DomclickCollector, InvestMoscowCollector, YandexRealtyCollector):
            with self.subTest(cls=cls.__name__), self.assertRaises(ValueError):
                await collector_with_response(cls, {}).collect(0)

    async def test_pipeline_reports_partial_failure_and_keeps_good_source(self):
        storage, conn = memory_storage()
        self.addCleanup(conn.close)
        class FakeCollector:
            def __init__(self, name, fail=False):
                self.name, self.fail = name, fail
            async def collect(self, limit):
                if self.fail:
                    raise RuntimeError("fixture")
                return [property_offer()]
            def collection_summary(self, collected):
                return dict(status="ok", collected=collected, warnings=[], coverage_complete=False)
        agg = RealEstateAggregator.__new__(RealEstateAggregator)
        agg.city=MOSCOW
        agg.storage = storage
        agg.collectors = [FakeCollector("good"), FakeCollector("bad", True)]
        result = await agg.run_pipeline(1)
        self.assertEqual(result["data_status"], "partial")
        self.assertEqual(result["source_results"]["bad"]["status"], "unavailable")
        self.assertEqual(result["batch_saved_or_updated"], 1)
        self.assertFalse(result["source_results"]["good"]["coverage_complete"])


if __name__ == "__main__":
    unittest.main()
