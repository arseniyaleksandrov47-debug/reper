import asyncio
import logging
import time
import math
from typing import List, Dict, Any, Optional, Tuple
from .storage import PropertyStorage
from .models import CanonicalProperty
from .rate_limiter import AdaptiveRateLimiter
from .cities import get_city, city_in_text
from .competitors import NvidiaCompetitorAnalyzer, RadiusCompetitionReport
from .recommender import (
    OptimalPropertyRecommender,
    OptimalPropertySearchReport,
    require_business_concept,
)
from .collectors import (
    CianCollector,
    AvitoCollector,
    YandexRealtyCollector,
    DomclickCollector,
    InvestMoscowCollector
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("MasterAggregator")
_CITY_BOUNDS = object()

class RealEstateAggregator:
    def __init__(self, db_path: Optional[str] = None, proxy: Optional[str] = None,
                 collection_bounds=_CITY_BOUNDS, *, city=None):
        self.city = get_city(city)
        self.collection_bounds = self.city.bounds if collection_bounds is _CITY_BOUNDS else collection_bounds
        self.storage = PropertyStorage(db_path=str(self.city.database_path) if db_path is None else db_path)
        self.proxy = proxy
        self.rate_limiter = AdaptiveRateLimiter(default_min_delay=1.0, default_max_delay=2.5)
        self.competitor_analyzer = NvidiaCompetitorAnalyzer(proxy=proxy)
        self.recommender = OptimalPropertyRecommender(
            storage=self.storage,
            competitor_analyzer=self.competitor_analyzer,
            proxy=proxy,
            city=self.city,
        )
        
        # Instantiate collectors with shared rate limiter and persistent Keep-Alive sessions
        self.collectors = [
            collector(proxy=proxy, rate_limiter=self.rate_limiter, city=self.city)
            for collector in (YandexRealtyCollector, CianCollector, DomclickCollector, AvitoCollector)
        ]
        if self.city.municipal_source == 'investmoscow':
            self.collectors.append(InvestMoscowCollector(proxy=proxy, rate_limiter=self.rate_limiter))
        elif self.city.municipal_source == 'gis_torgi':
            from .collectors.municipal import MunicipalRentalCollector
            self.collectors.append(MunicipalRentalCollector(city=self.city,proxy=proxy,rate_limiter=self.rate_limiter))

    async def analyze_competition(
        self,
        lat: float,
        lng: float,
        diameter_m: float,
        target_business: str,
        extra_queries: Optional[List[str]] = None,
        business_concept: Optional[str] = None,
    ) -> RadiusCompetitionReport:
        return await self.competitor_analyzer.analyze_circle(
            center_lat=lat,
            center_lng=lng,
            diameter_m=diameter_m,
            target_business=target_business,
            extra_queries=extra_queries,
            business_concept=business_concept,
        )

    async def run_pipeline(self, limit_per_source: int = 30) -> Dict[str, Any]:
        """
        Executes all collectors concurrently while respecting per-domain pacing,
        deduplicates offers, and indexes them in R-Tree storage in milliseconds.
        """
        if isinstance(limit_per_source, bool) or not isinstance(limit_per_source, int) or limit_per_source < 1:
            raise ValueError("limit_per_source должен быть целым числом не меньше 1")
        t0_total = time.perf_counter()
        sources=getattr(getattr(getattr(self,'competitor_analyzer',None),'property_verifier',None),'sources',None)
        bind=getattr(sources,'bind_collectors',None)
        if callable(bind):bind(self.collectors)
        logger.info(f"Starting multi-platform ingestion across {len(self.collectors)} sources with adaptive pacing...")

        tasks = [c.collect(limit=limit_per_source) for c in self.collectors]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        total_collected = 0
        all_properties: List[CanonicalProperty] = []
        quarantined = []
        source_results = {}

        for collector, result in zip(self.collectors, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                logger.error(f"Collector {collector.name} error: {result}")
                details = {"status": "unavailable", "collected": 0,
                           "warnings": [f"Сбор недоступен ({type(result).__name__})"], "coverage_complete": False}
            elif isinstance(result, list):
                total_collected += len(result)
                details = collector.collection_summary(len(result))
                rejected = []
                for prop in result:
                    reason = self._collection_quality_issue(prop)
                    if reason:
                        quarantined.append((prop, reason))
                        rejected.append({"source": prop.source.value, "external_id": prop.external_id,
                                         "lat": prop.lat, "lng": prop.lng, "reason": reason})
                    else:
                        all_properties.append(prop)
                details["accepted"] = len(result) - len(rejected)
                details["quarantined"] = rejected
                if rejected:
                    details["warnings"].append(f"Изолировано записей с ошибками исходных данных: {len(rejected)}")
                    details["status"] = "partial" if details["accepted"] else "unavailable"
            else:
                details = {"status": "unavailable", "collected": 0,
                           "warnings": ["Сборщик вернул некорректный результат"], "coverage_complete": False}
            source_results[collector.name] = details

        # Batch upsert to database with spatial R-Tree indexing
        t0_db = time.perf_counter()
        self.storage.quarantine_batch(quarantined)
        saved_count = self.storage.upsert_batch(all_properties)
        for source, details in source_results.items():
            self.storage.record_source_health(source, details)
        db_time_ms = (time.perf_counter() - t0_db) * 1000.0

        total_time_ms = (time.perf_counter() - t0_total) * 1000.0
        stats = self.storage.get_statistics()

        logger.info(f"Batch completed: {total_collected} items in {total_time_ms:.1f}ms. DB upsert+R-Tree: {db_time_ms:.2f}ms.")
        return {
            "city": self.city.key,
            "source_scope": {"municipal": self.city.municipal_source or "not_connected"},
            "batch_collected": total_collected,
            "batch_saved_or_updated": saved_count,
            "batch_quarantined": len(quarantined),
            "db_upsert_time_ms": round(db_time_ms, 2),
            "total_pipeline_time_ms": round(total_time_ms, 2),
            "database_stats": stats,
            "source_results": source_results,
            "data_status": ("ok" if source_results and all(r["status"] in ("ok", "empty") for r in source_results.values())
                            else "partial" if all_properties else "unavailable"),
        }

    def _collection_quality_issue(self, prop: CanonicalProperty) -> Optional[str]:
        try:
            finite = all(math.isfinite(v) for v in (prop.lat, prop.lng, prop.area_sqm, prop.price_month, prop.price_sqm_year))
        except (ValueError, TypeError):
            finite = False
        if not finite:
            return "Некорректные числовые данные источника"
        if not (-90 <= prop.lat <= 90 and -180 <= prop.lng <= 180) or prop.lat == 0 or prop.lng == 0:
            return "Некорректные координаты источника"
        if min(prop.area_sqm, prop.price_month, prop.price_sqm_year) <= 0:
            return "Некорректная площадь или цена источника"
        bounds = getattr(self, "collection_bounds", None)
        city = getattr(self, 'city', None)
        named_city = city_in_text(prop.address)
        if city and named_city and named_city != city:
            return "Адрес относится к другому городу; требуется проверка источника"
        if bounds:
            min_lat, max_lat, min_lng, max_lng = bounds
            if not (min_lat <= prop.lat <= max_lat and min_lng <= prop.lng <= max_lng):
                return "Координаты вне заявленной зоны сбора; требуется проверка источника"
        return None

    async def find_optimal_properties(
        self,
        target_business: str,
        location_query: Optional[str] = None,
        center_lat: Optional[float] = None,
        center_lng: Optional[float] = None,
        search_radius_m: Optional[float] = None,
        target_area_sqm: Optional[float] = None,
        min_area_sqm: Optional[float] = None,
        max_area_sqm: Optional[float] = None,
        max_price_month: Optional[float] = None,
        max_price_sqm: Optional[float] = None,
        max_price_sqm_year: Optional[float] = None,
        max_candidates: int = 5,
        competition_diameter_m: float = 400.0,
        price_sqm_period: str = "month",
        business_format: Optional[str] = None,
        allowed_floors: Optional[Tuple[int, ...]] = None,
        analysis_limit: Optional[int] = None,
        max_listing_age_hours: Optional[float] = None,
        business_concept: Optional[str] = None,
        business_data: Optional[Dict[str, Any]] = None,
        exclude_property_keys: Optional[List[str]] = None,
        confirmation_timeout_s: float = 45,
        refresh_inventory: bool = False,
        include_unverified: bool = False,
        city: Optional[str] = None,
    ) -> OptimalPropertySearchReport:
        """
        Перебирает помещения выбранного города и района по параметрам клиента
        (желаемая площадь, бюджет аренды в месяц, опциональная цена за м², ниша бизнеса),
        собирает открытые данные (Яндекс Карты, ФНС ГИР БО, 2ГИС паспорта МКД, Дептранс)
        и возвращает ТОП лучших мест со сравнением конкурентов.
        """
        from .inventory_search import attach_collection, request_scope
        require_business_concept(business_concept, business_format)
        if city is not None and get_city(city) != self.city:
            raise ValueError('Город сохранённого запроса не совпадает с городом клиента')
        location_query = location_query or self.city.name
        named_city = city_in_text(location_query)
        if named_city and named_city != self.city:
            raise ValueError('Город запроса не совпадает с выбранным городом сбора')
        if center_lat is not None and center_lng is not None and not self.city.contains(center_lat, center_lng):
            raise ValueError('Центр поиска вне выбранного города сбора')
        collection_request=dict(city=self.city.key,location_query=location_query,center_lat=center_lat,center_lng=center_lng,
                search_radius_m=search_radius_m,target_area_sqm=target_area_sqm,min_area_sqm=min_area_sqm,
                max_area_sqm=max_area_sqm,max_price_month=max_price_month,max_price_sqm=max_price_sqm,
                max_price_sqm_year=max_price_sqm_year,price_sqm_period=price_sqm_period,
                max_listing_age_hours=max_listing_age_hours,exclude_property_keys=exclude_property_keys)
        if refresh_inventory:
            await self.collect_for_search(collection_request,target_matches=max_candidates*4)
        state=getattr(self,'_inventory_collection',None)
        if state and state.get('request_scope')!=request_scope(collection_request):state=None
        if state:
            query=state['search_query']
            center_lat,center_lng,search_radius_m=query['target_lat'],query['target_lng'],query['radius_meters']
        report=await self.recommender.find_optimal_properties(
            city=self.city.key,
            target_business=target_business,
            location_query=location_query,
            center_lat=center_lat,
            center_lng=center_lng,
            search_radius_m=search_radius_m,
            target_area_sqm=target_area_sqm,
            min_area_sqm=min_area_sqm,
            max_area_sqm=max_area_sqm,
            max_price_month=max_price_month,
            max_price_sqm=max_price_sqm,
            max_price_sqm_year=max_price_sqm_year,
            max_candidates=max_candidates,
            competition_diameter_m=competition_diameter_m,
            price_sqm_period=price_sqm_period,
            business_format=business_format,
            allowed_floors=allowed_floors,
            analysis_limit=analysis_limit,
            max_listing_age_hours=max_listing_age_hours,
            business_concept=business_concept,
            business_data=business_data,
            exclude_property_keys=exclude_property_keys,
            confirmation_timeout_s=confirmation_timeout_s,
            include_unverified=include_unverified,
        )
        return attach_collection(report,state)

    async def collect_for_search(self,request,**limits):
        from .inventory_search import collect_for_search
        return await collect_for_search(self,request,**limits)

    async def continue_unverified_analysis(self,report,*,business_data=None):
        from .inventory_search import attach_collection
        result=await self.recommender.continue_unverified_analysis(report,business_data=business_data)
        return attach_collection(result,getattr(report,'inventory_collection',None))

    async def supply_ranking_references(self,report,ranking_scales,*,business_data=None):
        from .inventory_search import attach_collection
        result=await self.recommender.supply_ranking_references(report,ranking_scales,business_data=business_data)
        return attach_collection(result,getattr(report,'inventory_collection',None))

    async def find_more_options(self, report, *, business_data=None):
        """Continue unchanged constraints, collecting deeper if the sample ran out."""
        from .inventory_search import attach_collection
        next_report=await self.recommender.find_more_options(report,business_data=business_data)
        state=getattr(report,'inventory_collection',{})
        if next_report.numerical_matching_count==0 and state.get('next_limit'):
            action=next(a for a in report.next_actions if a.get('type')=='find_more')
            depth=state['next_limit']
            await self.collect_for_search(action['request'],initial_limit=depth,max_limit=depth,
                ceiling=state['ceiling'],target_matches=state['target_matches'])
            next_report=await self.recommender.find_more_options(report,business_data=business_data)
            state=self._inventory_collection
        return attach_collection(next_report,state)
