import asyncio
import json
import logging
import math
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import orjson
import requests
from curl_cffi import requests as curl_requests
from requests.adapters import HTTPAdapter

from .collectors.base import _DirectInterfaceBridge
from .credentials import load_nvidia_key
from .ai_contract import AIContractError, AIResponseError, AITransportError, AITextResponse, require_string
from .ai_pacing import shared_request_pacer, retry_after_seconds
from .fns import FnsBrowserRevenueCollector, FnsCompanyProfile, resolve_dadata_key
from .population import ResidentialPopulationAnalyzer, ResidentialPopulationReport
from .storage import haversine_distance
from .traffic import (
    FootTrafficEstimator,
    FootTrafficReport,
    summarize_card_histogram,
)



logger = logging.getLogger("CompetitorAnalyzer")

DEFAULT_NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
DEFAULT_NVIDIA_MODEL = "nvidia/nemotron-3.5-lightning-30b-a3b"


class CompetitionLevel(str, Enum):
    DIRECT = "direct"
    INDIRECT = "indirect"
    SYNERGY = "synergy"
    UNRELATED = "unrelated"


@dataclass
class MapBusinessCard:
    org_id: str
    title: str
    categories: List[str]
    category_classes: List[str]
    address: str
    lat: float
    lng: float
    distance_m: float
    rating: Optional[float] = None
    ratings_count: int = 0
    reviews_count: int = 0
    working_hours: str = ""
    is_chain: bool = False
    features: List[str] = field(default_factory=list)
    url: str = ""
    # Финансовые данные из ГИР БО ФНС (через DaData ИНН + браузерный запрос к bo.nalog.gov.ru)
    # В интегрированном рейтинге описывают силу альтернатив, не выручку этой точки.
    fns_profile: Optional[FnsCompanyProfile] = None
    fns_inn: Optional[str] = None
    fns_legal_name: Optional[str] = None
    fns_okved: Optional[str] = None
    fns_revenue_rub: Optional[float] = None
    fns_net_profit_rub: Optional[float] = None
    fns_reporting_year: Optional[int] = None
    # Гео-трафик и почасовая гистограмма посещаемости из Яндекс Карт
    histogram_data: Optional[Dict[str, List[float]]] = None
    nearby_metro: List[Dict[str, Any]] = field(default_factory=list)
    nearby_stops: List[Dict[str, Any]] = field(default_factory=list)
    building_entrances: List[Tuple[float, float]] = field(default_factory=list)
    traffic_profile_str: Optional[str] = None
    websites: List[str] = field(default_factory=list)
    fns_match_status: str = 'unverified'
    fns_match_evidence: List[Dict[str,Any]] = field(default_factory=list)
    fns_diagnostics: Dict[str,Any] = field(default_factory=dict)
    source_conflicts: Dict[str,List[Any]] = field(default_factory=dict)


    @property
    def primary_category(self) -> str:
        return self.categories[0] if self.categories else "Прочее"

    @property
    def fns_summary_str(self) -> str:
        if not self.fns_inn:
            return "ФНС: н/д"
        rev_str = (f"{self.fns_revenue_rub / 1_000_000:,.1f} млн ₽ ({self.fns_reporting_year} г.)"
                   if self.fns_revenue_rub is not None else "отчётность недоступна")
        profit_str = (
            f", чист. прибыль: {self.fns_net_profit_rub / 1_000_000:,.1f} млн ₽"
            if self.fns_net_profit_rub is not None
            else ""
        )
        method_tag = ""
        if self.fns_profile and self.fns_profile.match_method == "dadata_address_okved":
            method_tag = " [по адресу/ОКВЭД]"
        return f"ФНС{method_tag}: {self.fns_legal_name} (ИНН {self.fns_inn}) | Выручка: {rev_str}{profit_str}"



@dataclass
class ClassifiedCompetitor:
    card: MapBusinessCard
    level: CompetitionLevel
    reason: str


@dataclass
class RadiusCompetitionReport:
    target_business: str
    center_lat: float
    center_lng: float
    diameter_m: float
    radius_m: float
    total_cards_found: int
    direct_competitors: List[ClassifiedCompetitor]
    indirect_competitors: List[ClassifiedCompetitor]
    synergy_businesses: List[ClassifiedCompetitor]
    unrelated_businesses: List[ClassifiedCompetitor]
    category_breakdown: Dict[str, Dict[str, Any]]
    ai_summary: str
    model_used: str
    map_fetch_time_ms: float
    ai_inference_time_ms: float
    fns_enriched_count: int = 0
    fns_fetch_time_ms: float = 0.0
    foot_traffic: Optional[FootTrafficReport] = None
    population: Optional[ResidentialPopulationReport] = None
    data_status: str = "unknown"
    data_warnings: List[str] = field(default_factory=list)
    ai_response_metadata: Dict[str, Any] = field(default_factory=dict)
    collection_status: str = 'unknown'
    unclassified_businesses: List[MapBusinessCard] = field(default_factory=list)


class MapCardsResult(list):
    """List-compatible collection with per-request completeness metadata."""

    def __init__(self, cards=(), *, status: str, warnings=None, source_snapshots=None):
        super().__init__(cards)
        self.status = status
        self.warnings = list(warnings or [])
        self.source_snapshots = dict(source_snapshots or {})


def merge_map_card(target,incoming):
    """A search-specific snippet must not hide other verbatim source facts."""
    for name in ('categories','category_classes','features','websites','building_entrances','nearby_metro','nearby_stops'):
        values=getattr(target,name)
        for value in getattr(incoming,name):
            if value not in values:values.append(value)
    for name in ('title','address','working_hours'):
        first,second=getattr(target,name),getattr(incoming,name)
        if not first and second:setattr(target,name,second)
        elif first and second and first!=second:
            values=target.source_conflicts.setdefault(name,[first])
            if second not in values:values.append(second)
    flags={}
    for feature in target.features:
        match=re.fullmatch(r'(.+?)\s*:\s*(true|false)',feature,re.I)
        if match:flags.setdefault(match.group(1).strip(),set()).add(match.group(2).lower()=='true')
    for name,values in flags.items():
        if len(values)>1:target.source_conflicts['feature:'+name]=[False,True]




class YandexMapsRadiusCollector:
    BASE_SWEEP_QUERIES = [
        "кафе",
        "ресторан",
        "пекарня",
        "продукты",
        "магазины",
        "аптека",
        "красота",
        "услуги",
    ]

    def __init__(self, proxy: Optional[str] = None, *, source_cache=None):
        from .environment_cache import EnvironmentSourceCache
        self.source_cache=source_cache if source_cache is not None else EnvironmentSourceCache()
        self.proxy = proxy or _DirectInterfaceBridge.get_proxy_url()
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        self.session = curl_requests.Session(
            impersonate="chrome120",
            proxies=proxies,
            verify=False,
        )
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Referer": "https://yandex.ru/maps/",
        }

    @staticmethod
    def _compute_spn(lat: float, diameter_m: float) -> Tuple[float, float]:
        dlat = max(0.0015, (diameter_m * 1.15) / 111000.0)
        cos_lat = max(0.2, math.cos(math.radians(lat)))
        dlon = max(0.0020, (diameter_m * 1.15) / (111000.0 * cos_lat))
        return dlon, dlat

    @staticmethod
    def _build_queries_for_business(target_business: str, extra_queries: Optional[List[str]] = None) -> List[str]:
        queries: List[str] = []
        # Разбиваем целевой бизнес на отдельные ключевые слова (длиной >= 3 символов), чтобы Яндекс не склеивал их по И
        words = [
            w for w in re.split(r"[\s,/+-]+", target_business.strip().lower())
            if len(w) >= 3 and w not in ("или", "для", "при", "под", "дом", "зона", "точка", "спешелти")
        ]
        for w in words[:3]:
            if w not in queries:
                queries.append(w)
        if not queries and target_business.strip():
            queries.append(target_business.strip())
        if extra_queries:
            for q in extra_queries:
                if q and q.strip().lower() not in queries:
                    queries.append(q.strip().lower())
        for base_q in YandexMapsRadiusCollector.BASE_SWEEP_QUERIES:
            if base_q not in queries:
                queries.append(base_q)
        return queries


    def _fetch_query_sync(self, query: str, center_lat: float, center_lng: float, diameter_m: float) -> List[Dict[str, Any]]:
        dlon, dlat = self._compute_spn(center_lat, diameter_m)
        encoded_q = urllib.parse.quote(query)
        from .cities import city_for_coordinates
        city = city_for_coordinates(center_lat, center_lng)
        maps_url = city.maps_url if city else 'https://yandex.ru/maps'
        url = (
            f"{maps_url}/search/{encoded_q}/"
            f"?ll={center_lng:.6f}%2C{center_lat:.6f}&spn={dlon:.6f}%2C{dlat:.6f}&rspn=1&z=17"
        )
        try:
            resp = self.session.get(url, headers=self.headers, timeout=15)
            if resp.status_code != 200:
                raise RuntimeError(f"Yandex Maps HTTP {resp.status_code}")
            m = re.search(r'<script type="application/json" class="state-view">(.*?)</script>', resp.text, re.DOTALL)
            if not m:
                raise RuntimeError("Yandex Maps response has no map state")
            data = orjson.loads(m.group(1).encode("utf-8"))
            stack = data.get("stack") or []
            if not stack:
                raise RuntimeError("Yandex Maps response has no search results state")
            return stack[0].get("results", {}).get("items", []) or []
        except Exception as e:
            logger.debug(f"[YandexMaps] Error fetching '{query}': {e}")
            raise

    @staticmethod
    def _parse_card(item: Dict[str, Any], center_lat: float, center_lng: float, radius_m: float) -> Optional[MapBusinessCard]:
        if item.get("type") not in ("business", None):
            if not item.get("categories"):
                return None

        org_id = str(item.get("id") or "")
        title = str(item.get("title") or "").strip()
        if not org_id or not title:
            return None

        coords = item.get("coordinates") or item.get("displayCoordinates") or []
        if not isinstance(coords, list) or len(coords) < 2:
            return None

        lng, lat = float(coords[0]), float(coords[1])
        dist_m = haversine_distance(center_lat, center_lng, lat, lng)
        if dist_m > radius_m:
            return None

        raw_cats = item.get("categories") or []
        categories: List[str] = []
        cat_classes: List[str] = []
        for c in raw_cats:
            if isinstance(c, dict):
                c_name = c.get("name")
                c_cls = c.get("class")
                if c_name and c_name not in categories:
                    categories.append(str(c_name))
                if c_cls and c_cls not in cat_classes:
                    cat_classes.append(str(c_cls))

        if not categories:
            return None

        features: List[str] = []
        for sub in item.get("subtitleItems") or []:
            if isinstance(sub, dict) and sub.get("text"):
                features.append(str(sub["text"]))
        for feat in item.get("features") or []:
            if isinstance(feat, dict):
                fname = feat.get("name") or feat.get('id')
                fval = feat.get("value")
                if fname and isinstance(fval, (str, int, float)):
                    features.append(f"{fname}: {fval}")
                elif fname and fval is True:
                    features.append(str(fname))
                elif fname and isinstance(fval,list):
                    names=[v['name'] for v in fval if isinstance(v,dict) and isinstance(v.get('name'),str)]
                    if names:features.append(str(fname)+': '+', '.join(names))

        rating_data = item.get("ratingData") or {}
        rating_val = rating_data.get("ratingValue")
        rating = round(float(rating_val), 2) if rating_val is not None else None
        ratings_count = int(rating_data.get("ratingCount") or 0)
        reviews_count = int(rating_data.get("reviewCount") or 0)

        seoname = item.get("seoname") or "organization"
        card_url = f"https://yandex.ru/maps/org/{seoname}/{org_id}/"

        raw_hist = item.get("histogramData")
        hist_data = raw_hist if isinstance(raw_hist, dict) else None
        raw_metro = item.get("metro")
        metro_list = [m for m in raw_metro if isinstance(m, dict)] if isinstance(raw_metro, list) else []
        raw_stops = item.get("stops")
        stops_list = [s for s in raw_stops if isinstance(s, dict)] if isinstance(raw_stops, list) else []
        entrances_coords: List[Tuple[float, float]] = []
        for ent in item.get("entrances") or []:
            if isinstance(ent, dict):
                ec = ent.get("coordinates")
                if isinstance(ec, list) and len(ec) >= 2:
                    entrances_coords.append((float(ec[0]), float(ec[1])))

        return MapBusinessCard(
            org_id=org_id,
            title=title,
            categories=categories,
            category_classes=cat_classes,
            address=str(item.get("address") or item.get("fullAddress") or ""),
            lat=lat,
            lng=lng,
            distance_m=round(dist_m, 1),
            rating=rating,
            ratings_count=ratings_count,
            reviews_count=reviews_count,
            websites=[u for u in item.get('urls',[]) if isinstance(u,str)],
            working_hours=str(item.get("workingTimeText") or ""),
            is_chain=bool(item.get("chain")),
            features=features,
            url=card_url,
            histogram_data=hist_data,
            nearby_metro=metro_list,
            nearby_stops=stops_list,
            building_entrances=entrances_coords,
            traffic_profile_str=summarize_card_histogram(hist_data),
        )


    async def collect_in_circle(
        self,
        center_lat: float,
        center_lng: float,
        diameter_m: float,
        target_business: str,
        extra_queries: Optional[List[str]] = None,
        force_refresh: bool = False,
    ) -> List[MapBusinessCard]:
        semaphore=getattr(self,'_circle_semaphore',None)
        if semaphore is None:
            semaphore=asyncio.Semaphore(2);self._circle_semaphore=semaphore
        async with semaphore:
            return await self._collect_circle_sources(center_lat,center_lng,diameter_m,target_business,extra_queries,force_refresh)

    async def _collect_circle_sources(self,center_lat,center_lng,diameter_m,target_business,extra_queries=None,force_refresh=False):
        if (not all(math.isfinite(v) for v in (center_lat,center_lng,diameter_m)) or
                not -90<center_lat<90 or not -180<=center_lng<=180 or diameter_m<=0):
            raise ValueError('Некорректные координаты или диаметр окружения')
        radius_m = diameter_m / 2.0
        queries = self._build_queries_for_business(target_business, extra_queries=extra_queries)
        cache=getattr(self,'source_cache',None)
        def reusable(items):
            if not isinstance(items,list) or any(not isinstance(item,dict) for item in items):return False
            try:
                for item in items:self._parse_card(item,center_lat,center_lng,radius_m)
            except (TypeError,ValueError,AttributeError):return False
            return True
        async def fetch(query):
            if cache is None:
                value=await asyncio.to_thread(self._fetch_query_sync,query,center_lat,center_lng,diameter_m)
                return value,dict(cache_status='disabled')
            key=cache.key('yandex_map_query_v1',query=query,lat=float(center_lat),lng=float(center_lng),diameter_m=float(diameter_m))
            snapshot=await asyncio.to_thread(cache.get_or_fetch,key,
                lambda:self._fetch_query_sync(query,center_lat,center_lng,diameter_m),
                ttl=300,cacheable=reusable,force_refresh=force_refresh)
            return snapshot.value,snapshot.diagnostics()
        raw_batches = await asyncio.gather(*(fetch(query) for query in queries), return_exceptions=True)

        cards_by_id: Dict[str, MapBusinessCard] = {}
        failed_queries = 0
        parse_failures = 0
        snapshots={}
        for query,outcome in zip(queries,raw_batches):
            if isinstance(outcome,BaseException):
                failed_queries+=1;snapshots[query]=dict(cache_status='unavailable',error_type=type(outcome).__name__)
                continue
            batch,snapshots[query]=outcome
            if not isinstance(batch, list):
                failed_queries += 1
                continue
            for raw_item in batch:
                try:
                    card = self._parse_card(raw_item, center_lat, center_lng, radius_m)
                except (TypeError, ValueError, AttributeError):
                    parse_failures += 1
                    continue
                if card:
                    if card.org_id in cards_by_id:merge_map_card(cards_by_id[card.org_id],card)
                    else:cards_by_id[card.org_id] = card

        cards = sorted(cards_by_id.values(), key=lambda c: c.distance_m)
        logger.info(
            f"[YandexMaps] Circle D={diameter_m:.0f}m (R={radius_m:.0f}m) around ({center_lat:.5f}, {center_lng:.5f}): "
            f"found {len(cards)} unique organizations"
        )
        warnings = []
        if failed_queries:
            warnings.append(f"Не выполнено запросов к Картам: {failed_queries}/{len(queries)}")
        if parse_failures:
            warnings.append(f"Не удалось разобрать карточек: {parse_failures}")
        status = "ok" if cards else "empty"
        if failed_queries == len(queries):
            status = "unavailable"
        elif warnings:
            status = "partial"
        return MapCardsResult(cards, status=status, warnings=warnings,source_snapshots=snapshots)


class NvidiaCompetitorAnalyzer:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = DEFAULT_NVIDIA_MODEL,
        dadata_api_key: Optional[str] = None,
        proxy: Optional[str] = None,
        floor_model: Optional[str] = None,
        floor_review_model: Optional[str] = None,
        assessment_cache_path: Optional[str] = ".local-cache/access_assessments.sqlite3",
        metro_flow_data_file: Optional[str] = None,
        classification_model: Optional[str] = None,
        classification_proof_model: Optional[str] = None,
        classification_reasoning_budget: Optional[int] = None,
        environment_cache_path: Optional[str] = '.local-cache/environment_sources.sqlite3',
    ):
        self.api_key = api_key if api_key is not None else load_nvidia_key()
        self.model = model
        self.assessment_cache_path = assessment_cache_path
        from .osm_access import OSMPedestrianProvider
        self.pedestrian_provider=OSMPedestrianProvider()
        self.metro_flow_dataset=None;self.metro_flow_warning=None
        from .mos_metro_source import MosMetroFlowSource
        self.metro_flow_source=MosMetroFlowSource()
        from .spb_metro_source import SpbMetroFlowSource
        self.regional_metro_sources={'saint_petersburg':SpbMetroFlowSource()}
        from .metro_entrances import MetroEntranceSource
        self.metro_entrance_source=MetroEntranceSource()
        dataset_path=metro_flow_data_file or os.environ.get('METRO_FLOW_DATA_FILE')
        if dataset_path:
            from .metro_data import MetroFlowDataset
            try:self.metro_flow_dataset=MetroFlowDataset.from_file(dataset_path)
            except (OSError,ValueError,TypeError):self.metro_flow_warning='Датированный источник метро не загружен; поток метро остаётся неизвестным'
        self.floor_model = floor_model or os.environ.get("NVIDIA_FLOOR_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
        self.classification_model=classification_model or os.environ.get('NVIDIA_CLASSIFICATION_MODEL',self.floor_model)
        self.classification_proof_model=classification_proof_model or os.environ.get('NVIDIA_CLASSIFICATION_PROOF_MODEL','nvidia/nemotron-3.5-super-vl-120b-a12b')
        self.classification_reasoning_budget=(classification_reasoning_budget if classification_reasoning_budget is not None
            else int(os.environ.get('NVIDIA_CLASSIFICATION_REASONING_BUDGET','256')))
        if type(self.classification_reasoning_budget) is not int or not 0<=self.classification_reasoning_budget<=32768:
            raise ValueError('Classification reasoning budget must be an integer between 0 and 32768')
        self.floor_review_model = floor_review_model or os.environ.get("NVIDIA_FLOOR_REVIEW_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b")
        self.property_proof_model=os.environ.get('NVIDIA_PROPERTY_PROOF_MODEL','nvidia/nemotron-3.5-super-vl-120b-a12b')
        self.property_assessment_model=os.environ.get('NVIDIA_PROPERTY_ASSESSMENT_MODEL',self.floor_model)
        self.endpoint = "https://integrate.api.nvidia.com/v1/chat/completions"
        self.http_session = requests.Session()
        pool_adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32)
        self.http_session.mount("https://", pool_adapter)
        self.http_session.mount("http://", pool_adapter)
        rpm=int(os.environ.get('NVIDIA_REQUESTS_PER_MINUTE','40'))
        if rpm<1:raise ValueError('NVIDIA_REQUESTS_PER_MINUTE must be positive')
        self.floor_request_pacer=shared_request_pacer(self.api_key,
            min_interval=float(os.environ.get('NVIDIA_MIN_REQUEST_INTERVAL_S',str(60/rpm+.1))),requests_per_minute=rpm)
        self.http_session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })
        from .environment_cache import EnvironmentSourceCache
        self.environment_source_cache=EnvironmentSourceCache(environment_cache_path)
        self.map_collector = YandexMapsRadiusCollector(proxy=proxy,source_cache=self.environment_source_cache)
        from .organization_details import OrganizationDetailCompleter
        self.organization_detail_completer=OrganizationDetailCompleter(self.map_collector)
        self.fns_collector = FnsBrowserRevenueCollector(
            dadata_api_key=resolve_dadata_key(dadata_api_key),
            proxy=proxy,
        )
        self.population_analyzer = ResidentialPopulationAnalyzer(proxy=proxy,source_cache=self.environment_source_cache)
        from .property_confirmation import PropertyConfirmationVerifier
        self.property_verifier=PropertyConfirmationVerifier(self)


    def _call_floor_structured_sync(self, prompt: str, max_tokens: int, schema: dict, review: bool = False):
        return self._call_nvidia_sync(prompt, max_tokens,
            model_override=self.floor_review_model if review else self.floor_model,
            timeout=45, response_schema=schema)

    def _call_fast_property_sync(self,prompt,max_tokens,*,model,response_schema,response_format_type):
        """Two bounded attempts for idempotent fast inference, one outer budget."""
        from .ai_contract import AI_DEADLINE
        deadline=min(time.monotonic()+45,AI_DEADLINE.get() or float('inf'))
        token=AI_DEADLINE.set(deadline)
        try:
            for attempt in range(2):
                try:
                    return self._call_nvidia_sync(prompt,max_tokens,model_override=model,
                        timeout=18 if attempt else 12,response_schema=response_schema,response_format_type=response_format_type,
                        connect_timeout_s=5)
                except AITransportError as exc:
                    transient=exc.phase in ('connect','read','proxy','connection') or exc.status_code in (502,503,504)
                    if attempt or not exc.retryable or not transient or deadline-time.monotonic()<=1:raise
        finally:AI_DEADLINE.reset(token)

    def _call_property_assessment_structured_sync(self,prompt,max_tokens,*,response_schema):
        model=getattr(self,'property_assessment_model',self.floor_model)
        if 'nemotron' in model or model=='poolside/laguna-xs-2.1':
            from .ai_contract import AI_DEADLINE
            deadline=min(time.monotonic()+45,AI_DEADLINE.get() or float('inf'))
            token=AI_DEADLINE.set(deadline)
            # Nemotron models stall in NIM guided json_schema decoding on nested
            # requirement/property schemas; send json_object with embedded schema.
            initial_format='json_object' if 'nemotron' in model else 'json_schema'
            try:
                try:
                    return self._call_fast_property_sync(prompt,max_tokens,model=model,
                        response_schema=response_schema,response_format_type=initial_format)
                except AIResponseError as exc:
                    # Never accept a truncated answer. Retry its generation once
                    # without constrained decoding, then apply the same schema
                    # and grounding validation in the caller.
                    if exc.finish_reason!='length' or deadline-time.monotonic()<4:raise
                    return self._call_fast_property_sync(prompt+'\nПиши кратко: reason до 160 символов. Только поля схемы.',
                        max_tokens,model=model,response_schema=response_schema,response_format_type='json_object')
            finally:AI_DEADLINE.reset(token)
        return self._call_nvidia_sync(prompt,max_tokens,model_override=model,timeout=45,response_schema=response_schema)

    def _call_property_proof_structured_sync(self,prompt,max_tokens,*,response_schema):
        model=getattr(self,'property_proof_model','nvidia/nemotron-3.5-super-vl-120b-a12b')
        if 'nemotron' in model:
            return self._call_fast_property_sync(prompt,max(800,max_tokens),model=model,
                response_schema=response_schema,response_format_type='json_object')
        # Separate model for evidence entailment; no silent fallback to a model
        # which confused listed uses with exclusive use in the recorded audit.
        from .ai_contract import AI_DEADLINE
        deadline=min(time.monotonic()+45,AI_DEADLINE.get() or float('inf'))
        token=AI_DEADLINE.set(deadline)
        try:
            for attempt in range(2):
                try:
                    return self._call_nvidia_sync(prompt,max_tokens+1700,
                        model_override=model,
                        timeout=45,response_schema=response_schema,
                        response_format_type='json_object',reasoning_effort='low',connect_timeout_s=5)
                except AITransportError as exc:
                    # Retry once only if the connection wasn't established;
                    # never repeat an expensive read timeout or invalid answer.
                    if attempt or exc.phase not in ('connect','proxy') or deadline-time.monotonic()<=5:raise
        finally:AI_DEADLINE.reset(token)

    def _call_property_visual_sync(self,prompt,max_tokens,*,response_schema,image_urls):
        return self._call_nvidia_sync(prompt,max_tokens,
            model_override=self.property_proof_model,timeout=18,
            response_schema=response_schema,response_format_type='json_object',
            connect_timeout_s=5,image_urls=image_urls)

    def _call_property_requirements_structured_sync(self,prompt,max_tokens,*,response_schema):
        return self._call_property_assessment_structured_sync(prompt,max_tokens,response_schema=response_schema)

    def _call_profile_parameters_structured_sync(self,prompt,max_tokens,*,response_schema):
        return self._call_fast_property_sync(prompt,max_tokens,model=self.floor_review_model,
            response_schema=response_schema,response_format_type='json_schema')

    def _call_competitor_structured_sync(self,prompt,max_tokens,*,response_schema):
        model=getattr(self,'classification_model',self.model)
        budget=getattr(self,'classification_reasoning_budget',256) if 'nemotron' in model else None
        return self._call_nvidia_sync(prompt,max_tokens+(budget or 0),model_override=model,
            timeout=45,response_schema=response_schema,reasoning_budget=budget)

    def _call_competitor_proof_structured_sync(self,prompt,max_tokens,*,response_schema):
        return self._call_nvidia_sync(prompt,max_tokens,model_override=self.classification_proof_model,
            timeout=60,response_schema=response_schema,
            response_format_type='json_object',reasoning_effort=None if 'nemotron' in self.classification_proof_model else 'low')

    def _call_ranking_profile_structured_sync(self,prompt: str,max_tokens: int,schema: dict):
        return self._call_nvidia_sync(prompt,max_tokens,
            model_override=getattr(self,'ranking_model',self.floor_model),
            timeout=45,response_schema=schema,response_format_type='json_object')

    def _call_scenario_contract_structured_sync(self,prompt: str,max_tokens: int,schema: dict):
        # Interpret client intent independently of the model proposing weights.
        return self._call_nvidia_sync(prompt,max_tokens,
            model_override=self.floor_review_model,
            timeout=25,response_schema=schema)

    def _call_nvidia_sync(self, prompt: str, max_tokens: int = 900, *,
                          model_override: Optional[str] = None,
                          timeout: float = 15, response_schema: Optional[dict] = None,
                          response_format_type: str = 'json_schema',reasoning_effort: Optional[str] = None,
                          reasoning_budget: Optional[int] = None,
                          connect_timeout_s: Optional[float] = None,
                          image_urls=None) -> AITextResponse:
        if response_format_type not in ('json_schema','json_object'):raise ValueError('Unknown JSON response format')
        if reasoning_budget is not None and (type(reasoning_budget) is not int or not 0<=reasoning_budget<=32768):
            raise ValueError('Invalid reasoning budget')
        if connect_timeout_s is not None and (type(connect_timeout_s) not in (int,float) or not math.isfinite(connect_timeout_s) or connect_timeout_s<=0):
            raise ValueError('Invalid connection timeout')
        from .ai_contract import AI_DEADLINE
        deadline=min(time.monotonic()+timeout,AI_DEADLINE.get() or float('inf'))
        def remaining():
            budget=deadline-time.monotonic()
            if budget<=0:raise AITransportError('AI request budget expired',retryable=True,phase='deadline')
            return budget
        m = model_override or self.model
        try:
            payload: Dict[str, Any] = {
                "model": m,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Ты гео-маркетинговый аналитик коммерческой недвижимости. "
                            "Отвечай СТРОГО валидным JSON без markdown-оберток и без лишнего текста."
                        ),
                    },
                    {"role": "user", "content": prompt+('\nOUTPUT_JSON_SCHEMA\n'+json.dumps(response_schema,ensure_ascii=False)
                        if response_schema is not None and response_format_type=='json_object' else '')},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.1,
            }
            if image_urls:
                text_content=payload['messages'][-1]['content']
                payload['messages'][-1]['content']=[{'type':'text','text':text_content},
                    *[{'type':'image_url','image_url':{'url':url}} for url in image_urls]]
            if "nemotron" in m:
                payload["chat_template_kwargs"] = ({"enable_thinking":True,"reasoning_budget":reasoning_budget}
                    if reasoning_budget else {"enable_thinking":False})
            if reasoning_effort is not None:payload['reasoning_effort']=reasoning_effort
            if response_schema is not None:
                payload['temperature'] = 0 if response_format_type=='json_schema' else 0.1
                payload["response_format"] = ({"type":"json_schema","json_schema":{
                    "name":"floor_assessment_v4","strict":True,"schema":response_schema}}
                    if response_format_type=='json_schema' else {'type':'json_object'})

            if getattr(self,'floor_request_pacer',None):self.floor_request_pacer.wait_http_slot(remaining())
            request_budget=remaining()
            http_timeout=request_budget
            if connect_timeout_s is not None:
                from requests.adapters import TimeoutSauce
                http_timeout=TimeoutSauce(total=request_budget,connect=min(connect_timeout_s,request_budget),read=request_budget)
            resp = self.http_session.post(self.endpoint, json=payload, timeout=http_timeout)
            remaining()  # A late result cannot confirm an expired request.
            if resp.status_code == 200:
                if getattr(self,'floor_request_pacer',None):self.floor_request_pacer.succeeded()
                data = orjson.loads(resp.content)
                choices = data.get("choices")
                if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                    raise AIResponseError("Invalid API choices")
                choice = choices[0]
                finish = choice.get("finish_reason")
                if finish != "stop":
                    raise AIResponseError(f"Incomplete API response: {finish}",finish_reason=finish)
                message = choice.get("message") or {}
                if not isinstance(message, dict):
                    raise AIResponseError("Invalid API message")
                if message.get("refusal"):
                    raise AIResponseError("Model refusal")
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    return AITextResponse(content.strip(), m, {"finish_reason": finish, "usage": data.get("usage"),
                                                              "model": data.get("model", m),
                                                              "response_format":response_format_type if response_schema is not None else None,
                                                              "reasoning_effort":reasoning_effort,"reasoning_budget":reasoning_budget})
                raise AIResponseError("Empty API response")
            status=resp.status_code
            retry_after=retry_after_seconds(getattr(resp,'headers',{}).get('Retry-After')) if status==429 else 0
            if status==429 and getattr(self,'floor_request_pacer',None):
                self.floor_request_pacer.throttled(retry_after)
            raise AITransportError(f'AI service returned HTTP {status}',status_code=status,
                retryable=status in (408,429) or status>=500,retry_after=retry_after,phase='http_status')
        except requests.RequestException as exc:
            phase = ('connect' if isinstance(exc, requests.ConnectTimeout) else
                     'read' if isinstance(exc, requests.ReadTimeout) else
                     'proxy' if isinstance(exc, requests.exceptions.ProxyError) else
                     'tls' if isinstance(exc, requests.exceptions.SSLError) else 'connection')
            message = 'AI request timed out' if isinstance(exc, requests.Timeout) else 'AI connection unavailable'
            raise AITransportError(message, retryable=True, phase=phase) from exc

    async def analyze_circle(
        self,
        center_lat: float,
        center_lng: float,
        diameter_m: float,
        target_business: str,
        extra_queries: Optional[List[str]] = None,
        enrich_fns: bool = True,
        business_concept: Optional[str] = None,
        force_refresh: bool = False,
    ) -> RadiusCompetitionReport:

        if not isinstance(business_concept, str) or not business_concept.strip():
            raise ValueError('Концепция бизнеса должна быть непустым текстом')

        radius_m = diameter_m / 2.0
        from .cities import city_for_coordinates
        city = city_for_coordinates(center_lat, center_lng)
        metro_source=(getattr(self,'metro_flow_source',None) if city and city.key=='moscow' else
            getattr(self,'regional_metro_sources',{}).get(city.key) if city else None)
        env_warnings: List[str] = []

        async def _run_population() -> Optional[ResidentialPopulationReport]:
            try:
                return await self.population_analyzer.analyze_circle(
                    center_lat=center_lat,
                    center_lng=center_lng,
                    diameter_m=diameter_m,
                    **({'force_refresh':True} if force_refresh else {}),
                )
            except Exception as e:
                logger.warning(f"[CompetitorAnalyzer] Population analysis failed: {e}")
                env_warnings.append("Анализ населения недоступен")
                return None

        async def _run_metro():
            if getattr(self,'metro_flow_dataset',None) is None:
                if metro_source is None:
                    env_warnings.append('Датированный пассажиропоток этого города не подключён; поток метро неизвестен')
                    return None
                try:
                    dataset=await metro_source.get_dataset()
                    if dataset is None:
                        env_warnings.append('Датированный пассажиропоток недоступен; станциям не назначены типовые значения')
                    return dataset
                except Exception:
                    env_warnings.append('Датированный пассажиропоток недоступен; станциям не назначены типовые значения')
            return None

        def metro_metadata():
            supplied=getattr(self,'metro_flow_dataset',None)
            if supplied is not None:
                return dict(status='provided_dataset',source=getattr(supplied,'source',''),
                    as_of=str(getattr(supplied,'as_of','')),measure=getattr(supplied,'measure',''))
            return dict(metro_source.diagnostics) if metro_source else dict(status='unconfigured')

        async def _run_entrances():
            source=getattr(self,'metro_entrance_source',None)
            if source is None or city is None or not city.metro_entrance_source:return None
            try:return await source.get_dataset(city)
            except Exception:
                env_warnings.append('Справочник входов метро недоступен; неизвестные входы не заменены центрами станций')
                return None

        def entrance_metadata():
            source=getattr(self,'metro_entrance_source',None)
            return dict(getattr(source,'diagnostics',{}).get(city.key,{})) if city else {}

        pop_task = asyncio.create_task(_run_population())
        metro_task = asyncio.create_task(_run_metro())
        ent_task = asyncio.create_task(_run_entrances())
        env_tasks = (pop_task, metro_task, ent_task)
        try:
            t0_map = time.perf_counter()
            cards = await self.map_collector.collect_in_circle(
                center_lat=center_lat,
                center_lng=center_lng,
                diameter_m=diameter_m,
                target_business=target_business,
                extra_queries=extra_queries,
                **({'force_refresh':True} if force_refresh else {}),
            )
            map_time_ms = (time.perf_counter() - t0_map) * 1000.0
            data_status = getattr(cards, "status", "unknown")
            collection_status=data_status
            data_warnings = list(getattr(cards, "warnings", []))
            if collection_status == "unknown":
                data_warnings.append("Полнота сбора карточек организаций не подтверждена")

            if not cards:
                pop_report,automatic_metro,metro_entrances=await asyncio.gather(*env_tasks)
                data_warnings.extend(env_warnings)
                source_metadata=dict(maps=getattr(cards,"source_snapshots",{}),population=getattr(pop_report,"source_snapshots",{}))
                return RadiusCompetitionReport(
                    target_business=target_business,
                    center_lat=center_lat,
                    center_lng=center_lng,
                    diameter_m=diameter_m,
                    radius_m=radius_m,
                    total_cards_found=0,
                    direct_competitors=[],
                    indirect_competitors=[],
                    synergy_businesses=[],
                    unrelated_businesses=[],
                    category_breakdown={},
                    ai_summary="Карточки организаций не получены. Требуется проверка окружения.",
                    model_used=self.model,
                    map_fetch_time_ms=round(map_time_ms, 1),
                    ai_inference_time_ms=0.0,
                    data_status=data_status,
                    data_warnings=data_warnings + ["Нет карточек для оценки конкуренции"],
                    population=pop_report,
                    foot_traffic=FootTrafficEstimator.estimate_for_circle(center_lat,center_lng,radius_m,[],
                        metro_dataset=getattr(self,"metro_flow_dataset",None) or automatic_metro,metro_entrances=metro_entrances),
                    ai_response_metadata=dict(environment_sources=source_metadata,metro_data=metro_metadata(),metro_entrances=entrance_metadata()),
                    collection_status=collection_status,
                )

            cat_groups: Dict[str, List[MapBusinessCard]] = {}
            for c in cards:
                key = f"{c.primary_category}: {c.title} [{c.org_id}]"
                cat_groups.setdefault(key, []).append(c)

            cat_keys = list(cat_groups)

            environment_sources=dict(maps=getattr(cards,'source_snapshots',{}))
            ai_response_metadata = {}
            async def _run_ai() -> Tuple[Optional[dict], str, float]:
                nonlocal ai_response_metadata
                t0 = time.perf_counter()
                try:
                    from .competitor_classification import ConceptCompetitorClassifier
                    classifier=getattr(self,'_concept_classifier',None)
                    if classifier is None:
                        classifier=ConceptCompetitorClassifier(batch_size=14);self._concept_classifier=classifier
                    ordered_cards=[cat_groups[k][0] for k in cat_keys]
                    classification_model=getattr(self,'classification_model',self.model)
                    classification_identity=classification_model+'|reasoning_budget='+str(
                        getattr(self,'classification_reasoning_budget',256) if 'nemotron' in classification_model else None)
                    parsed,mdl,ai_response_metadata=await classifier.classify(ordered_cards,target_business,business_concept,
                        classification_identity,self._call_competitor_structured_sync,pacer=getattr(self,'floor_request_pacer',None),
                        verify=self._call_competitor_proof_structured_sync if getattr(self,'classification_proof_model',None) else None,
                        verifier_model=getattr(self,'classification_proof_model',None))
                    completer=getattr(self,'organization_detail_completer',None)
                    unknown=[ordered_cards[e['idx']-1] for e in parsed['categories'] if e['level']=='unknown']
                    if unknown and completer is not None:
                        completion=await completer.complete(unknown,center_lat,center_lng,radius_m,force_refresh=force_refresh)
                        changed=[c for c in unknown if c.org_id in completion['changed_ids']]
                        if changed:
                            revised,used,meta=await classifier.classify(changed,target_business,business_concept,
                                classification_identity,self._call_competitor_structured_sync,pacer=getattr(self,'floor_request_pacer',None),
                                verify=self._call_competitor_proof_structured_sync if getattr(self,'classification_proof_model',None) else None,
                                verifier_model=getattr(self,'classification_proof_model',None))
                            positions={c.org_id:i+1 for i,c in enumerate(ordered_cards)}
                            replacements={positions[changed[e['idx']-1].org_id]:e for e in revised['categories']}
                            parsed['categories']=[dict(replacements[e['idx']],idx=e['idx']) if e['idx'] in replacements else e for e in parsed['categories']]
                            for org_id,decision in meta.get('organization_decisions',{}).items():
                                ai_response_metadata['organization_decisions'][org_id]=dict(decision,idx=positions[org_id])
                            ai_response_metadata['detail_reclassification']=meta
                            ai_response_metadata['unknown_count']=sum(e['level']=='unknown' for e in parsed['categories'])
                            mdl=', '.join(sorted(set(mdl.split(', ')+used.split(', '))))
                        ai_response_metadata['organization_detail_completion']=completion
                    return parsed,mdl,(time.perf_counter()-t0)*1000
                except Exception as exc:
                    ai_response_metadata = {"error_type": type(exc).__name__}
                    data_warnings.append("AI-анализ конкуренции недоступен")
                    return None, self.model, (time.perf_counter() - t0) * 1000.0

            async def _run_fns() -> Tuple[int, float]:
                if not enrich_fns:
                    return 0, 0.0
                t0 = time.perf_counter()
                try:
                    cnt = await self.fns_collector.enrich_cards(cards)
                except Exception:
                    logger.warning("[CompetitorAnalyzer] FNS enrichment unavailable")
                    data_warnings.append("Финансовое обогащение недоступно")
                    cnt = 0
                return cnt, (time.perf_counter() - t0) * 1000.0

            (parsed, used_model, ai_time_ms), (fns_enriched_count, fns_time_ms), pop_report, automatic_metro, metro_entrances = await asyncio.gather(
                _run_ai(),
                _run_fns(),
                *env_tasks,
            )
            data_warnings.extend(env_warnings)
            if pop_report is not None:environment_sources['population']=getattr(pop_report,'source_snapshots',{})
            ai_response_metadata['environment_sources']=environment_sources
        finally:
            for t in env_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*env_tasks, return_exceptions=True)



        if (isinstance(parsed, dict) and set(parsed) == {"categories", "summary"}
                and isinstance(parsed["categories"], list) and isinstance(parsed["summary"], str)):
            ai_cats = parsed["categories"]
            ai_summary = parsed["summary"].strip()
        else:
            ai_cats, ai_summary = [], ""
            data_status = "partial"
            data_warnings.append("Ответ AI не прошёл проверку формата; классификация не подтверждена")

        idx_to_meta: Dict[int, Tuple[CompetitionLevel, str]] = {}
        invalid_ids, seen_ids = set(), set()
        unknown_reasons={}
        for entry in ai_cats:
            try:
                if not isinstance(entry, dict) or set(entry) != {"idx", "level", "reason"}:
                    raise AIContractError("Invalid category entry")
                idx = entry["idx"]
                if type(idx) is not int or idx not in range(1, len(cat_keys)+1):
                    raise AIContractError("Invalid category ID")
                if idx in seen_ids:
                    invalid_ids.add(idx)
                    data_status = "partial"
                    data_warnings.append("AI повторил ID категории; результат для неё не подтверждён")
                seen_ids.add(idx)
                if entry['level']=='unknown':
                    unknown_reasons[idx]=require_string(entry['reason'],'uncertain organization reason')
                    data_status='partial'
                    continue
                if entry["level"] not in {level.value for level in CompetitionLevel}:
                    invalid_ids.add(idx)
                    raise AIContractError("Invalid competition level")
                idx_to_meta[idx] = (CompetitionLevel(entry["level"]), require_string(entry["reason"], "category reason"))
            except (AIContractError, TypeError):
                if isinstance(entry, dict) and type(entry.get("idx")) is int:
                    invalid_ids.add(entry["idx"])
                data_status = "partial"
                data_warnings.append("AI вернул некорректную или конфликтующую категорию")
        for idx in invalid_ids:
            idx_to_meta.pop(idx, None)

        if any(idx not in idx_to_meta for idx in range(1, len(cat_keys) + 1)):
            data_status = "partial"
            data_warnings.append("AI классифицировал не все категории организаций")

        direct_list: List[ClassifiedCompetitor] = []
        indirect_list: List[ClassifiedCompetitor] = []
        synergy_list: List[ClassifiedCompetitor] = []
        unrelated_list: List[ClassifiedCompetitor] = []
        unclassified_list: List[MapBusinessCard] = []
        category_breakdown: Dict[str, Dict[str, Any]] = {}

        for idx, c_key in enumerate(cat_keys, start=1):
            known = idx in idx_to_meta
            lvl, rsn = idx_to_meta.get(idx, (CompetitionLevel.UNRELATED, unknown_reasons.get(idx,"Классификация категории не подтверждена")))
            group = cat_groups[c_key]
            category_breakdown[c_key] = {
                "level": lvl.value if known else "unknown",
                "count": len(group),
                "closest_distance_m": min(g.distance_m for g in group),
                "reason": rsn,
                "examples": [g.title for g in group[:5]],
            }
            if not known:
                unclassified_list.extend(group)
                continue
            for card in group:
                classified = ClassifiedCompetitor(card=card, level=lvl, reason=rsn)
                if lvl == CompetitionLevel.DIRECT:
                    direct_list.append(classified)
                elif lvl == CompetitionLevel.INDIRECT:
                    indirect_list.append(classified)
                elif lvl == CompetitionLevel.SYNERGY:
                    synergy_list.append(classified)
                else:
                    unrelated_list.append(classified)

        for competitors in (direct_list, indirect_list, synergy_list):
            competitors.sort(key=lambda item: item.card.distance_m)

        if not ai_summary:
            ai_summary = (
                f"В диаметре {diameter_m:.0f}м найдено {len(direct_list)} прямых и {len(indirect_list)} косвенных конкурентов, "
                f"а также {len(synergy_list)} генераторов трафика."
            )

        ai_response_metadata['metro_data']=metro_metadata()
        ai_response_metadata['metro_entrances']=entrance_metadata()
        foot_traffic = FootTrafficEstimator.estimate_for_circle(
            center_lat=center_lat,
            center_lng=center_lng,
            radius_m=radius_m,
            cards=cards,
            metro_dataset=getattr(self,'metro_flow_dataset',None) or automatic_metro,
            metro_entrances=metro_entrances,
        )
        if getattr(self,'metro_flow_warning',None):data_warnings.append(self.metro_flow_warning)
        data_warnings.extend(getattr(foot_traffic,'data_warnings',[]))

        return RadiusCompetitionReport(
            target_business=target_business,
            center_lat=center_lat,
            center_lng=center_lng,
            diameter_m=diameter_m,
            radius_m=radius_m,
            total_cards_found=len(cards),
            direct_competitors=direct_list,
            indirect_competitors=indirect_list,
            synergy_businesses=synergy_list,
            unrelated_businesses=unrelated_list,
            category_breakdown=category_breakdown,
            ai_summary=ai_summary,
            model_used=used_model,
            map_fetch_time_ms=round(map_time_ms, 1),
            ai_inference_time_ms=round(ai_time_ms, 1),
            fns_enriched_count=fns_enriched_count,
            fns_fetch_time_ms=round(fns_time_ms, 1),
            foot_traffic=foot_traffic,
            population=pop_report,
            data_status=data_status,
            data_warnings=data_warnings,
            ai_response_metadata=ai_response_metadata,
            collection_status=collection_status,
            unclassified_businesses=unclassified_list,
        )






