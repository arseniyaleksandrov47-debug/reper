"""Tests for GeoEstate desktop FastAPI bridge, SPA serving, and frozen path helpers."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import desktop_api
import desktop_app
from real_estate_engine import credentials
from real_estate_engine.business_model import validate_business_inputs
from real_estate_engine.cities import runtime_root
from real_estate_engine.ranking_policy import RankingCriterion, RankingProfile


def _dummy_report() -> SimpleNamespace:
    criterion = RankingCriterion(
        metric="location_traffic",
        weight=0.55,
        preference="higher_better",
        low=100.0,
        high=5000.0,
        concept_quote="пешеходный трафик",
        reason="Высокий пешеходный поток",
        assumption=None,
    )
    rp = RankingProfile(
        status="ready",
        criteria=[criterion],
        summary="Пекарня у метро",
        assumptions=["Тестовое допущение"],
        questions=[],
        unmodeled_requirements=[],
        metadata={
            "reference_scales": {
                "location_traffic": {"source": "concept", "usable": True}
            }
        },
    )
    cand = SimpleNamespace(
        property_data={
            "id": 101,
            "source": "cian",
            "external_id": "999888",
            "title": "Стрит-ритейл 55 м²",
            "address": "СПб, Средний пр. В.О., 40",
            "lat": 59.942,
            "lng": 30.278,
            "area_sqm": 55.0,
            "price_month": 150000.0,
            "price_sqm_year": 32727.0,
            "floor": 1,
            "url": "https://cian.ru/rent/commercial/999888/",
            "description": "1 этаж, отдельный вход",
            "has_separate_entrance": True,
            "power_kw": None,
        },
        total_score=72.4,
        score_upper_bound=78.9,
        score_coverage=0.88,
        input_score_lower_bound=65.0,
        input_score_upper_bound=80.0,
        price_sqm_month=2727.0,
        verified_fns_revenue_mln_sum=18.5,
        direct_competitors_avg_rating=4.6,
        competitor_comparison_summary="Умеренная конкуренция",
        pros=["Близко к метро"],
        cons=["Высокий трафик днём"],
        verdict="Подходит после подтверждения условий собственником",
        data_warnings=["Не проведена очная проверка"],
        floor_assessment={"status": "approved", "reason": "Подтверждён 1-й этаж"},
        confirmation={"status": "requires_confirmation", "label": "Требует подтверждения"},
        scoring_components={
            "location_traffic": {
                "unit": "people/day",
                "weight": 0.55,
                "value_lower": 1800,
                "value_upper": 2400,
                "contribution_lower": 36.2,
                "contribution_upper": 41.0,
            }
        },
        business_model_diagnostics={"ranking_cost_scope": "total_monthly"},
        competition_report=SimpleNamespace(
            total_cards_found=12,
            fns_enriched_count=3,
            model_used="test-model",
            ai_summary="Спрос устойчивый",
            map_fetch_time_ms=120,
            ai_inference_time_ms=90,
            fns_fetch_time_ms=50,
            data_status="complete",
            collection_status="ok",
            data_warnings=[],
            foot_traffic=SimpleNamespace(
                estimated_daily_pedestrians=2100,
                estimated_weekday_daily=2300,
                estimated_weekend_daily=1600,
                estimated_avg_hourly_daytime=140,
                estimated_peak_hourly=260,
                traffic_index=74,
                traffic_level="Высокий",
                metro_contribution_daily=900,
                stops_contribution_daily=450,
                local_commercial_daily=750,
                unique_stops_count=4,
                closest_stop_distance_m=110,
                transport_data_status="ok",
                transport_lower_daily=1100,
                transport_upper_daily=1500,
                data_status="ok",
                data_warnings=[],
                nearest_metros=[{"station_name": "Василеостровская", "distance_m": 280}],
                hourly_profile=SimpleNamespace(
                    weekday_hourly_share=[0.0] * 24,
                    weekend_hourly_share=[0.0] * 24,
                    morning_share_pct=28.0,
                    lunch_share_pct=34.0,
                    evening_share_pct=30.0,
                    night_share_pct=8.0,
                    peak_weekday_hour=13,
                    peak_weekend_hour=15,
                    weekend_to_weekday_ratio=0.72,
                    location_rhythm_type="commuter_mixed",
                    cards_with_histograms=5,
                ),
            ),
            population=SimpleNamespace(
                district_name="Василеостровский",
                radius_m=500,
                residential_buildings_count=18,
                exact_passport_buildings_count=14,
                total_apartments_count=1420,
                total_entrances_count=42,
                estimated_residents_count=3100,
                estimated_residents_range=[2850, 3400],
                estimated_office_workers_count=600,
                office_buildings_count=4,
                avg_building_floors=5.2,
                avg_construction_year=1956,
                predominant_wall_material="Кирпич",
                median_housing_price_sqm_rub=295000,
                median_flat_price_mln_rub=14.8,
                data_status="ok",
                data_warnings=[],
                population_model={"usable": True, "territories": []},
                top_residential_buildings=[],
                status="ok",
                exact_passports_count=14,
                total_buildings_count=18,
            ),
            direct_competitors=[],
            indirect_competitors=[],
            synergy_businesses=[],
        ),
    )
    return SimpleNamespace(
        city="saint_petersburg",
        target_business="Пекарня",
        location_query="метро Василеостровская",
        resolved_location_name="м. Василеостровская",
        center_lat=59.9426,
        center_lng=30.2783,
        search_radius_m=2000.0,
        competition_diameter_m=400.0,
        total_matching_in_db=30,
        numerical_matching_count=8,
        evaluated_count=1,
        total_search_time_ms=1450.0,
        selection_status="completed",
        confirmation_status="requires_confirmation",
        confirmation_message="Требуется подтверждение условий",
        comparison_complete=True,
        data_complete=False,
        include_unverified=False,
        unverified_evaluated_count=0,
        top_score_gap=float("nan"),
        stage_timings_ms={"ranking": 120.0},
        ranking_warnings=[],
        missing_inputs=[],
        analysis_errors=[],
        recommendation="Проверить лидера #1",
        next_actions=[],
        ranking_profile=rp,
        requirement_coverage=[{"concept_quote": "1 этаж", "status": "modeled"}],
        floor_profile={"allowed_floors": [1]},
        ranked_candidates=[cand],
        floor_verification_candidates=[],
        floor_rejected_candidates=[],
        deferred_candidate_bounds=[],
        confirmation_rejected_candidates=[],
    )


class DesktopAppTests(unittest.TestCase):
    def test_runtime_root_frozen_and_local(self):
        local_root = runtime_root()
        self.assertTrue((local_root / "desktop_api.py").is_file())

        with tempfile.TemporaryDirectory() as tmp:
            exe_path = Path(tmp) / "Reper" / "Reper.exe"
            exe_path.parent.mkdir(parents=True)
            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "executable", str(exe_path)
            ):
                self.assertEqual(runtime_root(), exe_path.parent.resolve())

            meipass = Path(tmp) / "_meipass"
            (meipass / "frontend_dist").mkdir(parents=True)
            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "_MEIPASS", str(meipass), create=True
            ):
                self.assertEqual(desktop_api.frontend_dist_dir(), meipass / "frontend_dist")

            # Installed build marker directs writable runtime_root() to LOCALAPPDATA/Reper
            # and migrates any saved secrets/DBs from legacy LOCALAPPDATA/GeoEstate
            local_app = Path(tmp) / "LocalAppData"
            legacy_root = local_app / "GeoEstate"
            (legacy_root / ".local-secrets").mkdir(parents=True)
            (legacy_root / ".local-secrets" / "nvidia.dpapi").write_bytes(b"secret-blob")
            (legacy_root / "data").mkdir(parents=True)
            (legacy_root / "data" / "saint_petersburg_commercial.db").write_bytes(b"db-blob")

            (exe_path.parent / "installed.flag").write_text("installed\n", encoding="utf-8")
            with patch.object(sys, "frozen", True, create=True), patch.object(
                sys, "executable", str(exe_path)
            ), patch.dict(os.environ, {"LOCALAPPDATA": str(local_app)}):
                resolved = runtime_root()
                self.assertEqual(resolved, local_app / "Reper")
                self.assertEqual(
                    (resolved / ".local-secrets" / "nvidia.dpapi").read_bytes(),
                    b"secret-blob",
                )
                self.assertEqual(
                    (resolved / "data" / "saint_petersburg_commercial.db").read_bytes(),
                    b"db-blob",
                )

    def test_api_health_listings_and_spa(self):
        client = TestClient(desktop_api.app)
        resp = client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual([c["key"] for c in data["cities"]], ["saint_petersburg", "moscow"])
        for c in data["cities"]:
            if c["db_exists"]:
                self.assertGreater(c["stats"]["total"], 0)
                self.assertGreater(c["stats"]["unique_locations"], 0)
                for cnt in c["stats"]["by_source"].values():
                    self.assertIsInstance(cnt, int)
        self.assertNotIn("nvapi-", data["secrets"]["nvidia_masked"])

        listings_resp = client.get("/api/cities/saint_petersburg/listings?limit=5")
        self.assertEqual(listings_resp.status_code, 200)
        listings_data = listings_resp.json()
        self.assertEqual(listings_data["city"], "saint_petersburg")
        self.assertIsInstance(listings_data["items"], list)
        if data["cities"][0]["db_exists"]:
            self.assertGreater(listings_data["total_matched"], 0)
            self.assertGreater(len(listings_data["items"]), 0)
            first_item = listings_data["items"][0]
            self.assertIn("description", first_item)
            self.assertIn("photos", first_item)
            self.assertIsInstance(first_item["photos"], list)

        missing_api = client.get("/api/nonexistent-route")
        self.assertEqual(missing_api.status_code, 404)

        root_html = client.get("/")
        self.assertEqual(root_html.status_code, 200)
        self.assertIn("<div id=\"root\"", root_html.text)

    def test_serialize_and_export_report(self):
        serialized = desktop_api.serialize_report(_dummy_report(), job_id="testjob1")
        self.assertEqual(serialized["job_id"], "testjob1")
        self.assertIsNone(serialized["top_score_gap"])  # NaN sanitized cleanly
        self.assertEqual(serialized["requirement_coverage"][0]["status"], "modeled")
        self.assertEqual(len(serialized["ranked_candidates"]), 1)

        desktop_api.JOBS["testjob1"] = {
            "job_id": "testjob1",
            "status": "completed",
            "result": serialized,
        }
        try:
            client = TestClient(desktop_api.app)
            md_resp = client.get("/api/jobs/testjob1/export?format=markdown")
            self.assertEqual(md_resp.status_code, 200)
            self.assertIn("Средний пр. В.О., 40", md_resp.text)

            json_resp = client.get("/api/jobs/testjob1/export?format=json")
            self.assertEqual(json_resp.status_code, 200)
            self.assertEqual(json_resp.json()["city"], "saint_petersburg")
        finally:
            desktop_api.JOBS.pop("testjob1", None)

    def test_free_port_ephemeral(self):
        port = desktop_app._free_port()
        self.assertGreater(port, 1024)
        self.assertLess(port, 65536)

    def test_bootstrap_seed_data_preserves_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "app"
            seed = Path(tmp) / "seed"
            (seed / "reference").mkdir(parents=True)
            (seed / "reference" / "metro.json").write_text('{"v":1}', encoding="utf-8")
            (seed / "saint_petersburg_commercial.db").write_bytes(b"seed-db")

            (root / "data").mkdir(parents=True)
            (root / "data" / "saint_petersburg_commercial.db").write_bytes(b"user-edited-db")

            desktop_app._bootstrap_seed_data(root, seed_root=seed)
            self.assertEqual((root / "data" / "saint_petersburg_commercial.db").read_bytes(), b"user-edited-db")
            self.assertEqual((root / "data" / "reference" / "metro.json").read_text(encoding="utf-8"), '{"v":1}')

    def test_map_keys_and_secrets_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            ym_path = Path(tmp) / "yandex_maps.dpapi"
            dg_path = Path(tmp) / "dgis_maps.dpapi"
            with patch.object(credentials, "DEFAULT_YANDEX_MAPS_KEY_PATH", ym_path), patch.object(
                credentials, "DEFAULT_DGIS_MAPS_KEY_PATH", dg_path
            ):
                credentials.save_yandex_maps_key("12345678-aaaa-bbbb-cccc-123456789012")
                credentials.save_dgis_maps_key("ruabc1234567890abcdef")

                client = TestClient(desktop_api.app)
                health = client.get("/api/health").json()
                self.assertTrue(health["secrets"]["yandex_maps_configured"])
                self.assertTrue(health["secrets"]["dgis_maps_configured"])
                self.assertNotIn("12345678-aaaa", health["secrets"]["yandex_maps_masked"])

                map_keys = client.get("/api/map-keys")
                self.assertEqual(map_keys.status_code, 200)
                self.assertEqual(
                    map_keys.json()["yandex_maps_key"],
                    "12345678-aaaa-bbbb-cccc-123456789012",
                )
                self.assertEqual(map_keys.json()["dgis_maps_key"], "ruabc1234567890abcdef")

    def test_serialize_property_summary_photos_and_fields(self):
        summary = desktop_api._serialize_property_summary(
            {
                "id": 7,
                "source": "avito",
                "title": "Помещение",
                "address": "Невский 10",
                "photos_json": '["https://img.example/1.jpg", "ftp://ignored", "not-a-url"]',
                "description": "Витринные окна",
                "has_separate_entrance": True,
                "power_kw": 25.0,
            }
        )
        self.assertEqual(summary["photos"], ["https://img.example/1.jpg"])
        self.assertEqual(summary["power_kw"], 25.0)
        self.assertTrue(summary["has_separate_entrance"])

    def test_build_business_data_satisfies_contract(self):
        req = desktop_api.SearchRequest(full_cost_limit=250000.0)
        bdata = desktop_api._build_business_data(req)
        self.assertIsNotNone(bdata)
        validate_business_inputs(bdata)
        self.assertIsNone(desktop_api._build_business_data(desktop_api.SearchRequest(full_cost_limit=None)))


class RecommendationSerializationTests(unittest.TestCase):
    def test_dict_and_string_recommendation_serialization(self) -> None:
        from desktop_api import serialize_report

        report = _dummy_report()
        report.recommendation = {
            "status": "partial_shortlist",
            "primary_candidate_id": 980,
            "message": "Первый вариант выбран среди завершённых оценок.",
            "checks": [{"kind": "published_conditions"}],
        }
        payload = serialize_report(report)
        self.assertIsInstance(payload["recommendation"], str)
        self.assertEqual(
            payload["recommendation"],
            "Первый вариант выбран среди завершённых оценок.",
        )
        self.assertEqual(
            payload["recommendation_details"]["primary_candidate_id"],
            980,
        )

        report_empty = _dummy_report()
        report_empty.recommendation = {}
        payload_empty = serialize_report(report_empty)
        self.assertEqual(payload_empty["recommendation"], "")
        self.assertEqual(payload_empty["recommendation_details"], {})

        report_str = _dummy_report()
        report_str.recommendation = "Проверить лидера #1"
        payload_str = serialize_report(report_str)
        self.assertEqual(payload_str["recommendation"], "Проверить лидера #1")
        self.assertIsNone(payload_str["recommendation_details"])


class DesktopStabilityRegressionTests(unittest.TestCase):
    def test_nominatim_resolves_multi_segment_street_and_rejects_ambiguous(self):
        from real_estate_engine.recommender import LocationResolver

        def fake_response(rows):
            resp = SimpleNamespace(raise_for_status=lambda: None, json=lambda: rows)
            return resp

        nevsky_segments = [
            {"lat": "59.9355", "lon": "30.3250", "name": "Невский проспект", "display_name": "Невский проспект, СПб"},
            {"lat": "59.9325", "lon": "30.3480", "name": "Невский проспект", "display_name": "Невский проспект, СПб"},
            {"lat": "59.9278", "lon": "30.3650", "name": "Невский проспект", "display_name": "Невский проспект, СПб"},
        ]
        with patch("real_estate_engine.recommender.curl_requests.get", return_value=fake_response(nevsky_segments)):
            resolved = LocationResolver._geocode_nominatim_sync("Невский проспект", city="saint_petersburg")
        self.assertIsNotNone(resolved)
        self.assertAlmostEqual(resolved[0], 59.9325, places=3)

        ambiguous = [
            {"lat": "59.9500", "lon": "30.3000", "name": "Бизнес-центр Мир", "display_name": "Бизнес-центр Мир"},
            {"lat": "59.8900", "lon": "30.3800", "name": "Кафе Мир", "display_name": "Кафе Мир"},
        ]
        with patch("real_estate_engine.recommender.curl_requests.get", return_value=fake_response(ambiguous)):
            self.assertIsNone(LocationResolver._geocode_nominatim_sync("Мир", city="saint_petersburg"))

    def test_search_request_forwards_coordinates_and_job_status_snapshot(self):
        req = desktop_api.SearchRequest(
            city="saint_petersburg",
            location_query="Невский проспект, 10",
            center_lat=59.934,
            center_lng=30.326,
            search_radius_m=2000.0,
            analysis_limit=6,
            skip_collect=True,
        )
        fake_agg = SimpleNamespace(
            find_optimal_properties=AsyncMock(return_value=_dummy_report()),
        )
        job = {
            "job_id": "snapjob1",
            "type": "search",
            "city": "saint_petersburg",
            "status": "running",
            "stage": "init",
            "logs": ["[12:00:00] start"],
            "result": None,
            "error": None,
        }
        desktop_api.JOBS["snapjob1"] = job
        try:
            with patch.object(desktop_api, "RealEstateAggregator", return_value=fake_agg):
                asyncio.run(desktop_api._execute_search_job(job, req))

            kwargs = fake_agg.find_optimal_properties.await_args.kwargs
            self.assertEqual(kwargs["center_lat"], 59.934)
            self.assertEqual(kwargs["center_lng"], 30.326)
            self.assertEqual(kwargs["search_radius_m"], 2000.0)
            self.assertEqual(kwargs["analysis_limit"], 6)

            client = TestClient(desktop_api.app)
            snap = client.get("/api/jobs/snapjob1")
            self.assertEqual(snap.status_code, 200)
            body = snap.json()
            self.assertEqual(body["status"], "completed")
            self.assertIsInstance(body["logs"], list)
            self.assertEqual(desktop_api.job_status("snapjob1")["logs"] is job["logs"], False)
        finally:
            desktop_api.JOBS.pop("snapjob1", None)
            desktop_api.REPORT_OBJECTS.pop("snapjob1", None)

    def test_whole_city_query_forces_unbounded_radius_in_search(self):
        spb = desktop_api.get_city("saint_petersburg")
        self.assertTrue(desktop_api._is_whole_city_query("весь город", spb))
        self.assertTrue(desktop_api._is_whole_city_query("Санкт-Петербург", spb))
        self.assertFalse(desktop_api._is_whole_city_query("Невский проспект", spb))

        req = desktop_api.SearchRequest(
            city="saint_petersburg",
            location_query="весь город",
            center_lat=None,
            center_lng=None,
            search_radius_m=1200.0,
            skip_collect=True,
        )
        fake_agg = SimpleNamespace(
            find_optimal_properties=AsyncMock(return_value=_dummy_report()),
        )
        job = {
            "job_id": "wholecity1",
            "type": "search",
            "city": "saint_petersburg",
            "status": "running",
            "stage": "init",
            "logs": [],
            "result": None,
            "error": None,
        }
        desktop_api.JOBS["wholecity1"] = job
        try:
            with patch.object(desktop_api, "RealEstateAggregator", return_value=fake_agg):
                asyncio.run(desktop_api._execute_search_job(job, req))
            kwargs = fake_agg.find_optimal_properties.await_args.kwargs
            self.assertIsNone(kwargs["search_radius_m"])
        finally:
            desktop_api.JOBS.pop("wholecity1", None)
            desktop_api.REPORT_OBJECTS.pop("wholecity1", None)

    def test_humanize_logs_and_cancel_endpoint(self):
        self.assertIsNone(
            desktop_api._humanize_log_line("12:00:00 [INFO] app: [DirectBridge] connecting")
        )
        self.assertIsNone(
            desktop_api._humanize_log_line("12:00:00 [INFO] httpx: HTTP Request: GET https://x")
        )
        self.assertEqual(
            desktop_api._humanize_log_line(
                "12:00:00 [INFO] ym: [YandexMaps] query (R=200m): found 15 unique"
            ),
            "12:00:00 · Найдено организаций в окружении (R=200 м): 15",
        )
        self.assertEqual(
            desktop_api._humanize_log_line(
                "12:00:00 [INFO] pop: [Population] area: 14 res buildings, ~2300 residents, ~45 office"
            ),
            "12:00:00 · Жилой фонд рядом: 14 МКД, ~2300 жителей, ~45 офисных мест",
        )
        self.assertEqual(
            desktop_api._humanize_log_line(
                "12:00:00 [INFO] fns: [FnsBrowser] Enriched 2/5 business cards"
            ),
            "12:00:00 · Сопоставлено карточек ФНС: 2 из 5",
        )
        self.assertEqual(
            desktop_api._humanize_log_line(
                "12:00:00 [INFO] fp: Допуск проверен: eligible=3, verification=2, rejected=1"
            ),
            "12:00:00 · Проверен допуск лотов: проходят — 3, на уточнение — 2, отклонено — 1",
        )

        job = {
            "job_id": "cancelme1",
            "type": "search",
            "city": "saint_petersburg",
            "status": "running",
            "stage": "Поиск",
            "logs": [],
            "result": None,
            "error": None,
        }
        fake_task = SimpleNamespace(done=lambda: False, cancel=Mock())
        desktop_api.JOBS["cancelme1"] = job
        desktop_api.JOB_TASKS["cancelme1"] = fake_task
        try:
            client = TestClient(desktop_api.app)
            resp = client.post("/api/jobs/cancelme1/cancel")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["status"], "cancelled")
            self.assertTrue(fake_task.cancel.called)
            snap = client.get("/api/jobs/cancelme1").json()
            self.assertEqual(snap["status"], "cancelled")
            self.assertEqual(snap["stage"], "Подбор остановлен")
        finally:
            desktop_api.JOBS.pop("cancelme1", None)
            desktop_api.JOB_TASKS.pop("cancelme1", None)

    def test_city_listings_preview_filters_and_spatial_spread(self):
        client = TestClient(desktop_api.app)
        spb = desktop_api.get_city("saint_petersburg")
        if not spb.database_path.is_file():
            self.skipTest("SPb commercial DB not present")

        # 1. Whole-city sample should span > 15 km latitude/longitude, never clump strictly at center
        wide = client.get("/api/cities/saint_petersburg/listings?limit=80").json()
        lats = [it["lat"] for it in wide["items"] if it.get("lat") is not None]
        lngs = [it["lng"] for it in wide["items"] if it.get("lng") is not None]
        self.assertGreater(max(lats) - min(lats), 0.12)
        self.assertGreater(max(lngs) - min(lngs), 0.15)

        # 2. Numeric & floor filters reduce matched set and strictly constrain every returned item
        filtered = client.get(
            "/api/cities/saint_petersburg/listings",
            params={
                "limit": 300,
                "min_area": 40,
                "max_area": 100,
                "max_price": 150000,
                "floors": "1",
                "include_unverified": "false",
            },
        ).json()
        self.assertGreater(filtered["total_matched"], 0)
        self.assertLess(filtered["total_matched"], wide["total_matched"])
        for it in filtered["items"]:
            self.assertGreaterEqual(it["area_sqm"], 40)
            self.assertLessEqual(it["area_sqm"], 100)
            self.assertLessEqual(it["price_month"], 150000)

        # 3. Text zone & pinned circle filters narrow matches meaningfully
        nevsky = client.get(
            "/api/cities/saint_petersburg/listings",
            params={"location_query": "Невский"},
        ).json()
        self.assertLess(nevsky["total_matched"], wide["total_matched"])

        pinned = client.get(
            "/api/cities/saint_petersburg/listings",
            params={"center_lat": 59.934, "center_lng": 30.335, "radius_m": 600},
        ).json()
        self.assertLess(pinned["total_matched"], wide["total_matched"])

        # 4. Offline SPb district & preview enrichment (price_sqm_month + last_seen_at)
        sample_item = wide["items"][0]
        self.assertIn("price_sqm_month", sample_item)
        self.assertIn("last_seen_at", sample_item)
        petrogradka = client.get(
            "/api/cities/saint_petersburg/listings",
            params={"location_query": "Петроградка"},
        ).json()
        self.assertGreater(petrogradka["total_matched"], 0)
        self.assertLess(petrogradka["total_matched"], wide["total_matched"])


class EngineFreeUpgradesTests(unittest.TestCase):
    def test_spb_offline_zones_match_districts_and_guard_streets(self):
        from real_estate_engine.recommender import LocationResolver, match_city_zone

        for street in ("Невский проспект", "Московский проспект", "Невский", "Московский", "ул. Садовая, 10"):
            self.assertIsNone(match_city_zone(street, "spb"), street)

        with (
            patch.object(LocationResolver, "_geocode_yandex_sync", side_effect=AssertionError("unexpected geocode")),
            patch.object(LocationResolver, "_geocode_nominatim_sync", side_effect=AssertionError("unexpected geocode")),
        ):
            for query, expected_substr in (
                ("Петроградка", "Петроград"),
                ("Васька", "Василеостров"),
                ("Купчино", "Купчино"),
                ("Невский район", "Невский район"),
            ):
                lat, lng, rad, name, _kws = LocationResolver.resolve(query, city="spb")
                self.assertIn(expected_substr, name)
                self.assertGreater(rad, 1000.0)
                self.assertTrue(59.7 < lat < 60.2)
                self.assertTrue(30.0 < lng < 30.7)

    def test_spatial_spread_sample_centers_first_and_serializes_sqm(self):
        rows = [
            {"id": 1, "lat": 59.72, "lng": 30.40, "area_sqm": 50, "price_month": 100000, "last_seen_at": "2026-11-20T10:00:00Z"},
            {"id": 2, "lat": 60.08, "lng": 30.18, "area_sqm": 60, "price_month": 120000, "last_seen_at": "2026-11-21T10:00:00Z"},
            {"id": 3, "lat": 59.934, "lng": 30.335, "area_sqm": 40, "price_month": 100000, "last_seen_at": "2026-11-24T10:00:00Z"},
            {"id": 4, "lat": 60.01, "lng": 30.48, "area_sqm": 70, "price_month": 140000},
        ]
        sampled = desktop_api._spatial_spread_sample(rows, cap=3, center_lat=59.934, center_lng=30.335)
        self.assertEqual(len(sampled), 3)
        self.assertEqual(sampled[0]["id"], 3)

        summary = desktop_api._serialize_property_summary(rows[0])
        self.assertEqual(summary["price_sqm_month"], 2000)
        self.assertEqual(summary["last_seen_at"], "2026-11-20T10:00:00Z")

    def test_select_diverse_candidates_pushes_warehouses_behind_retail(self):
        from real_estate_engine.recommender import OptimalPropertyRecommender

        rent_crit = RankingCriterion(
            metric="rent_month",
            weight=1.0,
            preference="lower_better",
            low=50000.0,
            high=200000.0,
            concept_quote="аренда",
            reason="аренда",
            assumption=None,
        )
        cafe_profile = RankingProfile(
            status="ready",
            criteria=[rent_crit],
            summary="Кофейня",
            concept="Кофейня с собой и выпечкой",
            metadata={"reference_scales": {"rent_month": {"usable": True}}},
        )
        warehouse = {"id": 1, "source": "avito", "external_id": "w1", "title": "Складское помещение в промзоне", "lat": 59.93, "lng": 30.33, "price_month": 45000}
        retail = {"id": 2, "source": "cian", "external_id": "r1", "title": "Помещение свободного назначения, 1 этаж", "lat": 59.94, "lng": 30.34, "price_month": 120000}
        mixed = {"id": 3, "source": "cian", "external_id": "m1", "title": "Торгово-складское помещение ПСН", "lat": 59.95, "lng": 30.35, "price_month": 150000}

        cafe_pick = OptimalPropertyRecommender._select_diverse_candidates([warehouse, retail, mixed], 2, cafe_profile)
        self.assertEqual([x["id"] for x in cafe_pick], [2, 3])

        warehouse_profile = RankingProfile(
            status="ready",
            criteria=[rent_crit],
            summary="Склад",
            concept="Склад и логистика заказов",
            metadata={"reference_scales": {"rent_month": {"usable": True}}},
        )
        wh_pick = OptimalPropertyRecommender._select_diverse_candidates([warehouse, retail], 1, warehouse_profile)
        self.assertEqual(wh_pick[0]["id"], 1)

    def test_analyze_circle_runs_env_tasks_concurrently_and_uses_batch14(self):
        import json
        from real_estate_engine.ai_contract import AITextResponse
        from real_estate_engine.competitors import MapBusinessCard, MapCardsResult, NvidiaCompetitorAnalyzer

        async def _run():
            analyzer = NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
            analyzer.model = "fixture"
            map_started = asyncio.Event()
            pop_started_during_map = {"seen": False}

            async def collect(**_kwargs):
                map_started.set()
                await asyncio.sleep(0.03)
                cards = MapCardsResult(
                    [
                        MapBusinessCard(
                            org_id="1",
                            title="Кофейня 1",
                            categories=["Кофейня"],
                            category_classes=["cafe"],
                            address="Невский 10",
                            lat=59.934,
                            lng=30.335,
                            distance_m=50.0,
                        )
                    ],
                    status="ok",
                )
                return cards

            async def population(**_kwargs):
                await map_started.wait()
                pop_started_during_map["seen"] = True
                return None

            analyzer.map_collector = SimpleNamespace(collect_in_circle=collect)
            analyzer.population_analyzer = SimpleNamespace(analyze_circle=population)

            def infer_stub(prompt, max_tokens=None, **_kwargs):
                data = json.loads(prompt.split("\nINPUT_JSON\n", 1)[1])
                org = data["organizations"][0]
                return AITextResponse(
                    json.dumps(
                        {
                            "categories": [
                                {
                                    "idx": org["idx"],
                                    "need_relation": "same_purchase",
                                    "same_purchase_offer": "yes",
                                    "relationship_basis": "specific_purchase",
                                    "offer_evidence_ids": list(org["offer_evidence_catalog"])[:1],
                                    "audience_relation": "overlap",
                                    "scenario_access": "compatible",
                                    "evidence_fields": ["title", "categories"],
                                    "reason": "Кофейня",
                                }
                            ],
                            "summary": "1 прямой конкурент рядом",
                        }
                    ),
                    "fixture",
                    {},
                )

            analyzer._call_nvidia_sync = infer_stub
            report = await analyzer.analyze_circle(
                59.934, 30.335, 400.0, "Кофейня", enrich_fns=False, business_concept="Кофейня с собой"
            )
            self.assertTrue(pop_started_during_map["seen"])
            self.assertEqual(analyzer._concept_classifier.batch_size, 14)
            self.assertEqual(report.total_cards_found, 1)
            self.assertEqual(len(report.direct_competitors), 1)

            # Error in collect_in_circle must cancel background env tasks cleanly without leaking
            cancelled_flag = {"cancelled": False}

            async def failing_collect(**_kwargs):
                await asyncio.sleep(0.01)
                raise RuntimeError("boom")

            async def slow_pop(**_kwargs):
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    cancelled_flag["cancelled"] = True
                    raise

            analyzer.map_collector = SimpleNamespace(collect_in_circle=failing_collect)
            analyzer.population_analyzer = SimpleNamespace(analyze_circle=slow_pop)
            with self.assertRaises(RuntimeError):
                await analyzer.analyze_circle(
                    59.934, 30.335, 400.0, "Кофейня", enrich_fns=False, business_concept="Кофейня с собой"
                )
            self.assertTrue(cancelled_flag["cancelled"])

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
