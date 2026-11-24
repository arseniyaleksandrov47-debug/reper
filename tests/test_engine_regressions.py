import asyncio
import contextlib
import io
import json
import sqlite3
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from real_estate_engine import (
    CanonicalProperty, ClassifiedCompetitor, CompetitionLevel, MapBusinessCard,
    MoscowLocationResolver, NvidiaCompetitorAnalyzer, OptimalPropertyRecommender,
    PropertyStorage, RadiusCompetitionReport, ResidentialPopulationAnalyzer,
    PlatformSource, YandexMapsRadiusCollector, haversine_distance,
)
from real_estate_engine.competitors import MapCardsResult
from floor_ai_fixtures import FixtureFloorAnalyzer, ranking_fixture_response
from real_estate_engine.ranking_policy import validate_profile


LAT, LNG = 55.7558, 37.6173
SEARCH_CONCEPT = 'Клиенты приходят с улицы. Важна стоимость аренды.'


def property_offer(external_id="1", **changes):
    values = dict(
        source=PlatformSource.CIAN, external_id=external_id, title="Помещение",
        address="Москва, тестовая улица, 1", lat=LAT, lng=LNG,
        area_sqm=50, price_month=100000, price_sqm_year=24000, floor=1,
    )
    values.update(changes)
    return CanonicalProperty(**values)


def candidate(i=1, **changes):
    values = dict(
        id=i, source="cian", title="Помещение", address="Москва",
        lat=LAT, lng=LNG, area_sqm=50, price_month=100000,
        price_sqm_year=24000, floor=1, distance_m=0,
    )
    values.update(changes)
    return values


def business_card(**changes):
    values = dict(
        org_id="1", title="Кофейня", categories=["Кофейня"],
        category_classes=[], address="Москва", lat=LAT, lng=LNG, distance_m=100,
    )
    values.update(changes)
    return MapBusinessCard(**values)


def competition_report(**changes):
    values = dict(
        target_business="Кофейня", center_lat=LAT, center_lng=LNG,
        diameter_m=400, radius_m=200, total_cards_found=1,
        direct_competitors=[], indirect_competitors=[], synergy_businesses=[],
        unrelated_businesses=[], category_breakdown={}, ai_summary="", model_used="test",
        map_fetch_time_ms=0, ai_inference_time_ms=0, data_status="ok",
    )
    values.update(changes)
    return RadiusCompetitionReport(**values)


def rank(prop=None, report=None):
    profile = validate_profile(ranking_fixture_response(SEARCH_CONCEPT), SEARCH_CONCEPT)
    return OptimalPropertyRecommender._score_and_rank_candidates(
        [(prop or candidate(), report or competition_report())],
        profile,
    )[0]


def population_analyzer(buildings=(), prices=((), ())):
    analyzer = ResidentialPopulationAnalyzer.__new__(ResidentialPopulationAnalyzer)
    analyzer._fetch_2gis_buildings_sync = lambda *args: list(buildings)
    analyzer._fetch_yandex_realty_prices_sync = lambda *args: prices
    return analyzer


def memory_storage():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.create_function("HAVERSINE", 4, haversine_distance)
    storage = PropertyStorage.__new__(PropertyStorage)
    storage._get_connection = lambda: conn
    storage._init_db()
    return storage, conn


class RateTests(unittest.TestCase):
    def test_explicit_yearly_rate_below_10000(self):
        self.assertEqual(OptimalPropertyRecommender.normalize_price_sqm(max_price_sqm_year=6000), (500, 6000))

    def test_monthly_rate_above_10000(self):
        self.assertEqual(OptimalPropertyRecommender.normalize_price_sqm(12000), (12000, 144000))

    def test_explicit_period_and_precision(self):
        self.assertEqual(OptimalPropertyRecommender.normalize_price_sqm(6001, price_sqm_period="year"), (6001 / 12, 6001))

    def test_invalid_rates_rejected(self):
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OptimalPropertyRecommender.normalize_price_sqm(value)
        with self.assertRaises(ValueError):
            OptimalPropertyRecommender.normalize_price_sqm(100, 1200)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.storage, self.conn = memory_storage()
        self.addCleanup(self.conn.close)

    def test_updates_all_fields_and_moves_spatial_index(self):
        old = property_offer()
        self.storage.upsert_property(old)
        new = replace(old, title="Новое", address="Другой адрес", lat=LAT + .02,
                      area_sqm=70, floor=2, price_month=120000,
                      price_sqm_year=20571, photos=["new.jpg"], power_kw=25,
                      has_separate_entrance=True, url="https://example.org/new",
                      description="Новое описание", raw_attributes={"changed": True})
        self.storage.upsert_batch([new])
        row = self.conn.execute("SELECT * FROM properties").fetchone()
        for name in ("title", "address", "lat", "lng", "area_sqm", "floor", "price_month",
                     "price_sqm_year", "power_kw", "url", "description"):
            self.assertEqual(row[name], getattr(new, name), name)
        self.assertEqual(row["dedup_hash"], new.deduplication_hash)
        self.assertEqual(row["photos_json"], '["new.jpg"]')
        self.assertEqual(row["raw_json"], '{"changed":true}')
        self.assertEqual(row["has_separate_entrance"], 1)
        self.assertEqual(len(self.storage.find_within_radius_rtree(LAT, LNG, 100)), 0)
        self.assertEqual(len(self.storage.find_within_radius_rtree(new.lat, LNG, 100)), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0], 1)

    def test_existing_index_repaired_from_stored_coordinates(self):
        self.storage.upsert_property(property_offer())
        self.conn.execute("UPDATE properties_rtree SET min_lat=50,max_lat=50")
        self.storage._init_db()
        self.assertEqual(len(self.storage.find_within_radius_rtree(LAT, LNG, 100)), 1)

    def test_failed_batch_rolls_back_table_and_index(self):
        with self.assertRaises(TypeError):
            self.storage.upsert_batch([
                property_offer(), property_offer("2", raw_attributes={"bad": object()}),
            ])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM properties_rtree").fetchone()[0], 0)

    def test_keywords_cannot_bypass_radius_and_filters(self):
        offers = [
            property_offer("ok"),
            property_offer("far", lat=LAT + .03),
            property_offer("expensive", price_month=150001),
            property_offer("small", area_sqm=39.9),
            property_offer("large", area_sqm=70.1),
            property_offer("rate", price_sqm_year=30001),
        ]
        self.storage.upsert_batch(offers)
        rows = self.storage.find_candidates(LAT, LNG, 500, ["тестовая"], 40, 70, 150000, 30000)
        self.assertEqual([r["external_id"] for r in rows], ["ok"])

    def test_cross_platform_duplicate_only_once(self):
        self.storage.upsert_batch([property_offer(), property_offer(source=PlatformSource.AVITO)])
        self.assertEqual(len(self.storage.find_candidates(LAT, LNG, 500)), 1)

    def test_nonpositive_prices_do_not_bypass_budget_filters(self):
        self.storage.upsert_batch([
            property_offer("zero_m", lat=LAT, price_month=0, price_sqm_year=10000),
            property_offer("zero_sqm", lat=LAT+0.0001, address="Тестовая улица, 2", price_month=50000, price_sqm_year=0),
            property_offer("valid", lat=LAT+0.0002, address="Тестовая улица, 3", price_month=50000, price_sqm_year=10000),
        ])
        self.assertEqual(sorted(r["external_id"] for r in self.storage.find_candidates(LAT, LNG, 500, max_price_month=100000)), ["valid", "zero_sqm"])
        self.assertEqual([r["external_id"] for r in self.storage.find_candidates(LAT, LNG, 500, max_price_month=100000, max_price_sqm_year=20000)], ["valid"])


class ScoreTests(unittest.TestCase):
    def test_missing_maps_do_not_imply_free_niche(self):
        result = rank(report=competition_report(total_cards_found=0, data_status="empty"))
        self.assertIsNone(result.scoring_components['passing_traffic_index']['value'])
        self.assertTrue(result.data_warnings)
        self.assertNotIn("Свободная ниша", result.competitor_comparison_summary)
        self.assertFalse(any("конкуренц" in p for p in result.pros))
        self.assertNotIn("Рекомендуется к аренде", result.verdict)

    def test_partial_maps_cannot_generate_low_competition_claim(self):
        result = rank(report=competition_report(data_status="partial"))
        self.assertFalse(any("конкурент" in p for p in result.pros))

    def test_company_revenue_preserved_and_does_not_boost_score(self):
        plain = business_card()
        enriched = business_card(fns_inn="company", fns_revenue_rub=1_000_000_000, fns_net_profit_rub=20_000_000)
        def report_for(card):
            return competition_report(direct_competitors=[ClassifiedCompetitor(card, CompetitionLevel.DIRECT, "test")])
        before, after = rank(report=report_for(plain)), rank(report=report_for(enriched))
        self.assertEqual(after.verified_fns_revenue_mln_sum, 1000)
        self.assertEqual(after.direct_competitors_fns_revenue_mln_sum, 1000)
        self.assertEqual(after.total_score, before.total_score)
        self.assertNotIn("на точку", after.competitor_comparison_summary)
        self.assertFalse(any("Подтвержденный спрос" in p for p in after.pros))

    def test_shared_company_revenue_counted_once(self):
        cards = [business_card(org_id=str(i), fns_inn="same", fns_revenue_rub=1_000_000_000) for i in (1, 2)]
        rep = competition_report(direct_competitors=[ClassifiedCompetitor(c, CompetitionLevel.DIRECT, "test") for c in cards])
        self.assertEqual(rank(report=rep).verified_fns_revenue_mln_sum, 1000)

    def test_unknown_floor_is_neutral_and_marked(self):
        first = rank(candidate(floor=1))
        unknown = rank(candidate(floor=None))
        self.assertEqual(unknown.scoring_components['rent_month'], first.scoring_components['rent_month'])
        self.assertTrue(any("Этаж" in w for w in unknown.data_warnings))


class PopulationTests(unittest.TestCase):
    def test_empty_sources_do_not_fabricate_population_or_housing_prices(self):
        result = population_analyzer().analyze_circle_sync(LAT, LNG)
        self.assertEqual(result.estimated_residents_count, 0)
        self.assertIsNone(result.median_housing_price_sqm_rub)
        self.assertIsNone(result.median_flat_price_mln_rub)
        self.assertEqual(result.data_sources_used, [])
        self.assertEqual(result.data_status, "unavailable")

    def test_building_passport_works_without_housing_prices(self):
        building = dict(id="1", name="Дом", point=dict(lat=LAT, lon=LNG),
                        purpose_name="Жилой дом", structure_info=dict(apartments_count=100),
                        floors=dict(ground_count=5))
        result = population_analyzer([building]).analyze_circle_sync(LAT, LNG)
        self.assertEqual(result.total_apartments_count, 100)
        self.assertGreater(result.estimated_residents_count, 0)
        self.assertEqual(result.data_status, "partial")
        self.assertTrue(result.buildings_data_available)
        self.assertFalse(result.housing_prices_data_available)
        self.assertFalse(any("Росстат" in s for s in result.data_sources_used))

    def test_prices_do_not_fabricate_building_population(self):
        result = population_analyzer(prices=([500000], [25])).analyze_circle_sync(LAT, LNG)
        self.assertEqual(result.estimated_residents_count, 0)
        self.assertTrue(result.housing_prices_data_available)
        self.assertEqual(result.data_status, "partial")

    def test_housing_price_medians_only_use_their_own_samples(self):
        for samples, expected in [(([500000], []), (500000, None)), (([], [25]), (None, 25))]:
            with self.subTest(samples=samples):
                result = population_analyzer(prices=samples).analyze_circle_sync(LAT, LNG)
                self.assertTrue(result.housing_prices_data_available)
                self.assertEqual((result.median_housing_price_sqm_rub, result.median_flat_price_mln_rub), expected)


class RecommenderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage, self.conn = memory_storage()
        self.addCleanup(self.conn.close)

    async def test_sparse_search_keeps_client_filters(self):
        self.storage.upsert_batch([
            property_offer("ok"), property_offer("far", lat=LAT + .03),
            property_offer("expensive", price_month=185000), property_offer("small", area_sqm=30),
        ])
        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self, **kw):
                return competition_report()
        engine = OptimalPropertyRecommender(self.storage, Analyzer())
        result = await engine.find_optimal_properties(
            "Кофейня", business_concept=SEARCH_CONCEPT, center_lat=LAT, center_lng=LNG, search_radius_m=500,
            min_area_sqm=40, max_area_sqm=70, max_price_month=150000, max_candidates=5,
        )
        self.assertEqual(result.total_matching_in_db, 1)
        self.assertEqual(result.evaluated_count, 1)
        self.assertEqual(result.best_candidate.property_data["external_id"], "ok")
        self.assertEqual(result.search_radius_m, 500)

    async def test_no_matches_keeps_request_metadata(self):
        engine = OptimalPropertyRecommender(self.storage, None)
        result = await engine.find_optimal_properties(
            "Кофейня", business_concept=SEARCH_CONCEPT, center_lat=LAT, center_lng=LNG, search_radius_m=500,
            max_price_month=150000, max_price_sqm_year=6000,
        )
        self.assertIsNone(result.best_candidate)
        self.assertEqual(result.client_max_price_month, 150000)
        self.assertEqual(result.client_max_price_sqm_year, 6000)

    async def test_analysis_limit_caps_pending_ai_floor_checks(self):
        self.storage.upsert_batch([
            property_offer(f"cap-{i}", lat=LAT + i * 0.0001)
            for i in range(15)
        ])

        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self, **kw):
                return competition_report()

        engine = OptimalPropertyRecommender(self.storage, Analyzer())
        seen_batch_sizes = []
        orig_assess_many = engine.floor_profile_resolver.assess_many

        async def tracked_assess_many(props, profile, **kw):
            seen_batch_sizes.append(len(props))
            return await orig_assess_many(props, profile, **kw)

        engine.floor_profile_resolver.assess_many = tracked_assess_many
        result = await engine.find_optimal_properties(
            "Кофейня",
            business_concept=SEARCH_CONCEPT,
            center_lat=LAT,
            center_lng=LNG,
            search_radius_m=1500,
            min_area_sqm=40,
            max_area_sqm=70,
            max_price_month=150000,
            analysis_limit=2,
            max_candidates=2,
        )
        self.assertEqual(seen_batch_sizes, [8])  # max(2*2+4, 2+6)
        self.assertLessEqual(result.evaluated_count, 2)
        self.assertEqual(len(result.floor_verification_candidates), 7)

    async def test_one_failed_candidate_preserves_success(self):
        self.storage.upsert_batch([property_offer(), property_offer("2", lat=LAT + .001)])
        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self, **kw):
                if kw["center_lat"] > LAT + .0005:
                    raise RuntimeError("simulated API failure")
                return competition_report()
        result = await OptimalPropertyRecommender(self.storage, Analyzer()).find_optimal_properties(
            "Кофейня", business_concept=SEARCH_CONCEPT, center_lat=LAT, center_lng=LNG, max_candidates=2,max_price_month=200000,
        )
        self.assertEqual(result.evaluated_count, 1)
        self.assertEqual(len(result.analysis_errors), 1)

    async def test_all_failures_are_distinguished_from_no_matches(self):
        self.storage.upsert_property(property_offer())
        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self, **kw):
                raise RuntimeError("simulated API failure")
        result = await OptimalPropertyRecommender(self.storage, Analyzer()).find_optimal_properties(
            "Кофейня", business_concept=SEARCH_CONCEPT, center_lat=LAT, center_lng=LNG,max_price_month=200000,
        )
        self.assertEqual(result.total_matching_in_db, 1)
        self.assertEqual(result.evaluated_count, 0)
        self.assertEqual(len(result.analysis_errors), 1)
        self.assertEqual(result.selection_status,'needs_data')

    async def test_waiting_ai_does_not_starve_other_premises_of_environment_acquisition(self):
        for i in range(3):self.storage.upsert_property(property_offer(str(i),lat=LAT+i*.002))
        entered=[];all_entered=asyncio.Event()
        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self,**kwargs):
                entered.append(kwargs)
                if len(entered)==3:all_entered.set()
                await asyncio.wait_for(all_entered.wait(),.2)
                return competition_report()
        result=await OptimalPropertyRecommender(self.storage,Analyzer()).find_optimal_properties(
            'Бизнес',business_concept=SEARCH_CONCEPT,center_lat=LAT,center_lng=LNG,search_radius_m=1000,max_candidates=3,environment_timeout_s=.5,max_price_month=200000)
        self.assertEqual(len(entered),3);self.assertEqual(result.evaluated_count,3)
        self.assertFalse(result.analysis_errors)

    async def test_shared_fns_failure_keeps_results(self):
        self.storage.upsert_property(property_offer())
        class Fns:
            async def enrich_cards(self, cards):
                raise RuntimeError("simulated FNS failure")
        class Analyzer(FixtureFloorAnalyzer):
            fns_collector = Fns()
            async def analyze_circle(self, **kw):
                return competition_report(direct_competitors=[ClassifiedCompetitor(business_card(), CompetitionLevel.DIRECT, "test")])
        result = await OptimalPropertyRecommender(self.storage, Analyzer()).find_optimal_properties(
            "Кофейня", business_concept=SEARCH_CONCEPT, center_lat=LAT, center_lng=LNG,max_price_month=200000,
        )
        self.assertEqual(result.evaluated_count, 1)
        self.assertTrue(any("Финансовое" in w for w in result.best_candidate.data_warnings))

    async def test_invalid_search_rejected_before_io(self):
        engine = OptimalPropertyRecommender(None, None)
        for kwargs in (dict(max_candidates=0), dict(search_radius_m=-1),
                       dict(min_area_sqm=100, max_area_sqm=50), dict(center_lat=LAT),
                       dict(center_lat=float("nan"), center_lng=LNG)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await engine.find_optimal_properties("Кофейня", business_concept=SEARCH_CONCEPT, **kwargs)

    async def test_missing_concept_is_rejected_before_search(self):
        with self.assertRaisesRegex(ValueError, 'Концепция бизнеса'):
            await OptimalPropertyRecommender(None, None).find_optimal_properties("Кофейня")

    async def test_cli_full_report_with_missing_data(self):
        import run_engine
        self.storage.upsert_property(property_offer())
        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self, **kw):
                return competition_report(total_cards_found=0, data_status="unavailable")
        engine = OptimalPropertyRecommender(self.storage, Analyzer())
        aggregator = SimpleNamespace(storage=self.storage, find_optimal_properties=engine.find_optimal_properties)
        stream = io.StringIO()
        with patch.object(run_engine, "RealEstateAggregator", return_value=aggregator), \
                patch("sys.argv", ["run_engine.py", "--skip-collect", "--concept", SEARCH_CONCEPT, "--area", "40-70", "--price-sqm-year", "30000","--price","200000"]), \
                contextlib.redirect_stdout(stream):
            await run_engine.main()
        output = stream.getvalue()
        self.assertIn("30,000 ₽/м²/год", output)
        self.assertIn("н/д", output)
        self.assertIn("Предварительная оценка", output)
        self.assertNotIn("свободная ниша", output.lower())
        self.assertNotIn("ЛУЧШИЙ ВЫБОР", output)


class MapsTests(unittest.IsolatedAsyncioTestCase):
    def analyzer_with_ai(self, categories, fail_fns=False):
        class Maps:
            async def collect_in_circle(self, **kwargs):
                return MapCardsResult([business_card()], status="ok")
        class Fns:
            async def enrich_cards(self, cards):
                if fail_fns:
                    raise RuntimeError("simulated FNS failure")
                return 0
        class Population:
            async def analyze_circle(self, **kwargs):
                return None
        analyzer = NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.map_collector = Maps()
        analyzer.fns_collector = Fns()
        analyzer.population_analyzer = Population()
        async def classify(*args, **kwargs):
            return {"categories": categories, "summary": "Контроль"}, "test", {}
        analyzer._concept_classifier = SimpleNamespace(classify=classify)
        analyzer.model = "test"
        return analyzer

    async def test_missing_ai_categories_are_incomplete(self):
        analyzer = self.analyzer_with_ai([])
        result = await analyzer.analyze_circle(LAT, LNG, 400, "Кофейня", business_concept=SEARCH_CONCEPT)
        self.assertEqual(result.data_status, "partial")
        self.assertTrue(result.data_warnings)
        self.assertEqual(len(result.unclassified_businesses), 1)

    async def test_unknown_ai_level_is_incomplete(self):
        analyzer = self.analyzer_with_ai([dict(idx=1, level="unexpected", reason="test")])
        result = await analyzer.analyze_circle(LAT, LNG, 400, "Кофейня", business_concept=SEARCH_CONCEPT)
        self.assertEqual(result.data_status, "partial")

    async def test_direct_fns_failure_preserves_competition(self):
        analyzer = self.analyzer_with_ai([dict(idx=1, level="direct", reason="test")], fail_fns=True)
        result = await analyzer.analyze_circle(LAT, LNG, 400, "Кофейня", business_concept=SEARCH_CONCEPT)
        self.assertEqual(len(result.direct_competitors), 1)
        self.assertEqual(result.data_status, "ok")
        self.assertTrue(any("Финансовое" in w for w in result.data_warnings))

    async def test_transport_error_not_reported_as_empty_success(self):
        collector = YandexMapsRadiusCollector.__new__(YandexMapsRadiusCollector)
        collector.headers = {}
        collector.session = SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status_code=403))
        with self.assertRaises(RuntimeError):
            collector._fetch_query_sync("кофе", LAT, LNG, 400)
        result = await collector.collect_in_circle(LAT, LNG, 400, "Кофейня")
        self.assertEqual(result.status, "unavailable")
        self.assertTrue(result.warnings)

    async def test_partial_query_results_preserve_cards_and_warning(self):
        collector = YandexMapsRadiusCollector.__new__(YandexMapsRadiusCollector)
        collector._build_queries_for_business = lambda *a, **k: ["ok", "fail"]
        def fetch(q, *args):
            if q == "fail":
                raise RuntimeError("simulated HTTP failure")
            return [dict(id="1", title="Кофейня", coordinates=[LNG, LAT], categories=[dict(name="Кофейня")])]
        collector._fetch_query_sync = fetch
        result = await collector.collect_in_circle(LAT, LNG, 400, "Кофейня")
        self.assertEqual(len(result), 1)
        self.assertEqual(result.status, "partial")

    def test_map_offer_features_are_not_silently_cut_after_six_entries(self):
        raw=dict(id='1',title='Тестовая организация',coordinates=[LNG,LAT],categories=[dict(name='Услуга')],
            subtitleItems=[dict(text='Подзаголовок')],features=[dict(name=f'Признак {i}',value=True) for i in range(10)])
        card=YandexMapsRadiusCollector._parse_card(raw,LAT,LNG,200)
        self.assertEqual(len(card.features),11)
        self.assertIn('Признак 9: True',card.features)

    async def test_map_acquisition_keeps_its_own_two_circle_limit(self):
        import threading,time
        collector=YandexMapsRadiusCollector.__new__(YandexMapsRadiusCollector)
        collector._build_queries_for_business=lambda *a,**k:['one']
        state=dict(active=0,peak=0);lock=threading.Lock()
        def fetch(*args):
            with lock:state['active']+=1;state['peak']=max(state['peak'],state['active'])
            time.sleep(.01)
            with lock:state['active']-=1
            return []
        collector._fetch_query_sync=fetch
        await asyncio.gather(*(collector.collect_in_circle(LAT,LNG,400,'Бизнес') for _ in range(6)))
        self.assertEqual(state['peak'],2)

    async def test_empty_maps_analyzer_reports_insufficient_data(self):
        class Maps:
            async def collect_in_circle(self, **kwargs):
                return MapCardsResult(status="unavailable")
        analyzer = NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.map_collector = Maps()
        analyzer.model = "test"
        result = await analyzer.analyze_circle(LAT, LNG, 400, "Кофейня", business_concept=SEARCH_CONCEPT)
        self.assertEqual(result.data_status, "unavailable")
        self.assertNotIn("свободная ниша", result.ai_summary.lower())


class PresentationTests(unittest.TestCase):
    def test_unknown_location_not_silently_mapped_to_moscow_center(self):
        with patch.object(MoscowLocationResolver, "_geocode_yandex_sync", return_value=None), \
                patch.object(MoscowLocationResolver, "_geocode_nominatim_sync", return_value=None):
            with self.assertRaises(ValueError):
                MoscowLocationResolver.resolve("неизвестный-объект-xyz")

    def test_summary_table_marks_missing_data(self):
        import run_engine
        result = rank(report=competition_report(total_cards_found=0, data_status="unavailable"))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            run_engine.print_top_comparison_table(SimpleNamespace(ranked_candidates=[result]))
        self.assertIn("н/д", stream.getvalue())
        self.assertNotIn("Конк.(ФНС)", stream.getvalue())

    def test_detailed_report_marks_missing_population_and_competition(self):
        import run_engine
        population = population_analyzer(prices=([500000], [])).analyze_circle_sync(LAT, LNG)
        rep = competition_report(population=population, total_cards_found=0, data_status="unavailable")
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            run_engine.print_detailed_location_report(candidate(), rep, "Кофейня", 400)
        output = stream.getvalue()
        self.assertIn("Данные о конкурентах требуют подтверждения", output)
        self.assertNotIn("Индекс конкурентной нагрузки", output)
        self.assertIn("население не определено", output)
        self.assertNotIn("Росстат", output)
        self.assertNotIn("Модельная оценка дохода", output)
        self.assertIn("500,000 ₽/м²", output)
        self.assertNotIn("Медиана полной цены квартиры", output)


if __name__ == "__main__":
    unittest.main()
