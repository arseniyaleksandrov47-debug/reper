import asyncio
import logging
import math
import os
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import orjson
from curl_cffi import requests as curl_requests

from .collectors.base import _DirectInterfaceBridge
from .storage import haversine_distance
from .cities import city_for_coordinates
from .territory_population import building_identity

logger = logging.getLogger("PopulationAnalyzer")


def population_counts_available(report):
    """Trust building counts only when the building source confirms coverage."""
    return (
        getattr(report, "buildings_data_available", False) is True
        and getattr(report, "building_coverage_complete", False) is True
        and getattr(report, "malformed_buildings_count", None) == 0
        and getattr(report, "buildings_without_coordinates_count", 0) == 0
    )


def population_residents_interval(report):
    if not population_counts_available(report):return None
    bounds=getattr(report,'estimated_residents_range',None)
    model=getattr(report,'population_model',{}) or {}
    if bounds is not None and model.get('usable') is True and model.get('modeled') is True:
        if (len(bounds)==2 and all(type(v) in (int,float) and math.isfinite(v) for v in bounds)
                and 0<=bounds[0]<=bounds[1]):return tuple(bounds)
        return None
    value=getattr(report,'estimated_residents_count',None)
    return (value,value) if type(value) in (int,float) and math.isfinite(value) and value>=0 else None


class BuildingFetchResult(list):
    def __init__(self, items=(), *, coverage_complete=False, warnings=()):
        super().__init__(items)
        self.coverage_complete = coverage_complete
        self.warnings = list(warnings)


MOSCOW_DISTRICT_HOUSEHOLD_SIZE: Dict[str, float] = {
    "тверской": 2.05,
    "арбат": 2.05,
    "хамовники": 2.10,
    "пресненский": 2.10,
    "замоскворечье": 2.08,
    "якиманка": 2.05,
    "басманный": 2.12,
    "таганский": 2.12,
    "красносельский": 2.10,
    "мещанский": 2.10,
    "дорогомилово": 2.15,
    "раменки": 2.22,
    "филёвский парк": 2.20,
    "хорошёвский": 2.18,
    "аэропорт": 2.15,
    "сокол": 2.15,
    "беговой": 2.12,
    "савёловский": 2.18,
    "бутырский": 2.20,
    "марьина роща": 2.18,
    "алексеевский": 2.18,
    "сокольники": 2.18,
    "лефортово": 2.20,
    "южнопортовый": 2.22,
    "даниловский": 2.18,
    "донской": 2.15,
    "гагаринский": 2.18,
    "академический": 2.18,
    "черёмушки": 2.22,
    "обручевский": 2.22,
    "марьино": 2.35,
    "люблино": 2.35,
    "выхино-жулебино": 2.32,
    "отрадное": 2.28,
    "митино": 2.32,
    "строгино": 2.25,
    "крылатское": 2.25,
    "кунцево": 2.25,
}


@dataclass
class ResidentialBuildingInfo:
    building_id: str
    address: str
    purpose_name: str
    distance_m: float
    floors: int
    apartments_count: int
    is_exact_apartments: bool
    entrances_count: int
    estimated_residents: Optional[int]
    year_of_construction: Optional[int] = None
    material: str = ""
    footprint_sqm: float = 0.0
    estimated_residents_range: Optional[Tuple[float,float]] = None


@dataclass
class ResidentialPopulationReport:
    center_lat: float
    center_lng: float
    diameter_m: float
    radius_m: float
    district_name: str
    total_buildings_scanned: int
    residential_buildings_count: int
    exact_passport_buildings_count: int
    total_apartments_count: int
    total_entrances_count: int
    estimated_residents_count: Optional[int]
    avg_building_floors: float
    avg_construction_year: Optional[int]
    predominant_wall_material: str
    office_buildings_count: int
    estimated_office_workers_count: int
    realty_offers_sampled: int
    median_housing_price_sqm_rub: Optional[float]
    median_flat_price_mln_rub: Optional[float]
    top_residential_buildings: List[ResidentialBuildingInfo] = field(default_factory=list)
    fetch_time_ms: float = 0.0
    data_sources_used: List[str] = field(default_factory=list)
    buildings_data_available: bool = False
    housing_prices_data_available: bool = False
    data_status: str = "unavailable"
    data_warnings: List[str] = field(default_factory=list)
    building_coverage_complete: bool = False
    non_operational_buildings_count: int = 0
    planned_residential_buildings_count: int = 0
    planned_apartments_count: int = 0
    buildings_outside_radius_count: int = 0
    buildings_without_coordinates_count: int = 0
    malformed_buildings_count: int = 0
    source_snapshots: Dict[str, Any] = field(default_factory=dict)
    estimated_residents_range: Optional[Tuple[float,float]] = None
    population_model: Dict[str, Any] = field(default_factory=dict)



class ResidentialPopulationAnalyzer:
    DEFAULT_2GIS_WEB_KEY = "c7f1a769-c8a5-4636-b14d-d8c987808a12"
    DEFAULT_2GIS_SALT = "baf4c54e9dae"

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
        self.realty_session = curl_requests.Session(
            impersonate="chrome120",
            proxies=proxies,
            verify=False,
        )
        self._2gis_key = self.DEFAULT_2GIS_WEB_KEY
        self._key_refreshed = False
        self._thread_local = threading.local()
        self._key_lock = threading.Lock()
        from .municipal_population_source import MunicipalPopulationSource
        self.municipal_population_source = MunicipalPopulationSource(self)

    def _get_2gis_http_session(self):
        sess = getattr(self, "session", None)
        if sess is None or type(sess).__module__.split(".")[0] != "curl_cffi":
            return sess
        if threading.current_thread() is threading.main_thread():
            return sess
        local = getattr(self, "_thread_local", None)
        if local is None:
            local = threading.local()
            self._thread_local = local
        per_thread = getattr(local, "dgis_session", None)
        if per_thread is None:
            proxy = getattr(self, "proxy", None)
            proxies = {"http": proxy, "https": proxy} if proxy else None
            per_thread = curl_requests.Session(
                impersonate="chrome120",
                proxies=proxies,
                verify=False,
            )
            local.dgis_session = per_thread
        return per_thread

    @staticmethod
    def _sign_2gis_params(url_path: str, params: Dict[str, str], salt: str = "baf4c54e9dae") -> str:
        concat_vals = "".join(str(params[k]) for k in sorted(params.keys()))
        raw = f"{url_path}{concat_vals}{salt}"
        h = 5381
        for ch in raw:
            h = (h * 33 + ord(ch)) & 0xFFFFFFFF
        return str(h)

    @staticmethod
    def _polygon_area_sqm(wkt_polygon: Optional[str], ref_lat: float) -> float:
        """Return net footprint area in sq. m for WKT POLYGON or MULTIPOLYGON, subtracting holes."""
        if not isinstance(wkt_polygon, str) or not wkt_polygon:
            return 0.0
        header = re.search(r"\b(MULTI)?POLYGON\b", wkt_polygon, re.I)
        if not header:
            return 0.0
        start = wkt_polygon.find("(", header.end())
        if start < 0:
            return 0.0

        is_multi = bool(header.group(1))
        poly_depth = 2 if is_multi else 1
        ring_depth = poly_depth + 1
        polygons: List[List[str]] = []
        current_poly: Optional[List[str]] = None
        ring_start: Optional[int] = None
        depth = 0

        for idx in range(start, len(wkt_polygon)):
            ch = wkt_polygon[idx]
            if ch == "(":
                depth += 1
                if depth == poly_depth:
                    current_poly = []
                elif depth == ring_depth:
                    ring_start = idx + 1
            elif ch == ")":
                if depth == ring_depth and ring_start is not None and current_poly is not None:
                    current_poly.append(wkt_polygon[ring_start:idx])
                    ring_start = None
                elif depth == poly_depth and current_poly is not None:
                    polygons.append(current_poly)
                    current_poly = None
                depth -= 1
                if depth <= 0:
                    break

        if not polygons:
            return 0.0

        cos_lat = math.cos(math.radians(ref_lat))
        m_per_deg_lat = 111_132.0
        m_per_deg_lon = 111_319.5 * cos_lat

        def ring_area(ring_wkt: str) -> float:
            pts: List[Tuple[float, float]] = []
            for pair in ring_wkt.split(","):
                parts = pair.strip().split()
                if len(parts) >= 2:
                    try:
                        lon = float(parts[0])
                        lat = float(parts[1])
                        pts.append((lon * m_per_deg_lon, lat * m_per_deg_lat))
                    except ValueError:
                        continue
            if len(pts) < 3:
                return 0.0
            x0, y0 = pts[0]
            norm_pts = [(x - x0, y - y0) for x, y in pts]
            area2 = 0.0
            n = len(norm_pts)
            for i in range(n):
                x1, y1 = norm_pts[i]
                x2, y2 = norm_pts[(i + 1) % n]
                area2 += x1 * y2 - x2 * y1
            return abs(area2) * 0.5

        total_area = 0.0
        for rings in polygons:
            if not rings:
                continue
            outer = ring_area(rings[0])
            holes = sum(ring_area(hole) for hole in rings[1:])
            total_area += max(0.0, outer - holes)
        return round(total_area, 1)

    def _refresh_2gis_key_if_needed(self, city=None) -> None:
        if self._key_refreshed:
            return
        try:
            resp = self.session.get(
                f"https://2gis.ru/{city.dgis_slug}" if city else "https://2gis.ru/",
                cookies={"dg5_museum_accept": "true"},
                timeout=8,
            )
            if resp.status_code == 200:
                m = re.search(r'webApiKey"\s*:\s*"([a-f0-9-]{36})"', resp.text)
                if m:
                    self._2gis_key = m.group(1)
                    self._key_refreshed = True
        except Exception as e:
            logger.debug(f"[Population] 2GIS key refresh skipped: {e}")

    def _fetch_2gis_buildings_sync(
        self, center_lat: float, center_lng: float, radius_m: float
    ) -> List[Dict[str, Any]]:
        all_items: Dict[str, Dict[str, Any]] = {}
        search_radius = int(max(150, min(600, math.ceil(radius_m))))
        warnings = []
        coverage_complete = False
        if search_radius < radius_m:
            warnings.append(f"2ГИС: запрошено только {search_radius} м из радиуса {radius_m:g} м")
        fields = (
            "items.floors,"
            "items.structure_info.material,"
            "items.structure_info.year_of_construction,"
            "items.structure_info.elevators_count,"
            "items.structure_info.apartments_count,"
            "items.structure_info.project_type,"
            "items.structure_info.is_in_emergency_state,"
            "items.building_name,"
            "items.address,"
            "items.point,"
            "items.purpose_name,"
            "items.links.database_entrances,"
            "items.adm_div,"
            "items.geometry.hover"
        )

        for page in (1, 2, 3):
            params: Dict[str, str] = {
                "key": self._2gis_key,
                "type": "building",
                "point": f"{center_lng:.6f},{center_lat:.6f}",
                "radius": str(search_radius),
                "page_size": "50",
                "page": str(page),
                "fields": fields,
            }
            params["r"] = self._sign_2gis_params("/3.0/items", params, self.DEFAULT_2GIS_SALT)
            http_sess = self._get_2gis_http_session()
            try:
                resp = http_sess.get(
                    "https://catalog.api.2gis.ru/3.0/items",
                    params=params,
                    headers={"Referer": "https://2gis.ru/", "Origin": "https://2gis.ru"},
                    timeout=9,
                )
                data = orjson.loads(resp.content)
                meta_code = (data.get("meta") or {}).get("code", 200)
                if meta_code == 403 and not self._key_refreshed:
                    lock = getattr(self, "_key_lock", None)
                    if lock is not None:
                        with lock:
                            if not self._key_refreshed:
                                self._refresh_2gis_key_if_needed(city_for_coordinates(center_lat, center_lng))
                    else:
                        self._refresh_2gis_key_if_needed(city_for_coordinates(center_lat, center_lng))
                    params["key"] = self._2gis_key
                    params.pop("r", None)
                    params["r"] = self._sign_2gis_params("/3.0/items", params, self.DEFAULT_2GIS_SALT)
                    resp = http_sess.get(
                        "https://catalog.api.2gis.ru/3.0/items",
                        params=params,
                        headers={"Referer": "https://2gis.ru/", "Origin": "https://2gis.ru"},
                        timeout=9,
                    )
                    data = orjson.loads(resp.content)

                meta_code = (data.get("meta") or {}).get("code", resp.status_code)
                if resp.status_code != 200 or meta_code != 200:
                    error_type = ((data.get("meta") or {}).get("error") or {}).get("type")
                    # 2GIS signals 0 matches inside a valid circle with HTTP 200 and meta.code=404 (itemNotFound).
                    if resp.status_code == 200 and meta_code == 404 and error_type == "itemNotFound" and page == 1:
                        coverage_complete = True
                        break
                    warnings.append(f"2ГИС: ошибка ответа HTTP {resp.status_code}, код {meta_code}")
                    break

                result = data.get("result") or {}
                if "items" not in result and "total" not in result:
                    warnings.append("2ГИС: не распознана структура ответа")
                    break
                items = result.get("items") or []
                if not items:
                    coverage_complete = int(result.get("total") or 0) <= len(all_items)
                    if not coverage_complete:
                        warnings.append("2ГИС: выдача оборвалась до заявленного числа зданий")
                    break
                for item in items:
                    bid = building_identity(item.get("id"))
                    if bid:
                        all_items[bid] = item
                total = result.get("total")
                if (total is not None and len(all_items) >= int(total)) or len(items) < 50:
                    coverage_complete = total is None or len(all_items) >= int(total)
                    if not coverage_complete:
                        warnings.append("2ГИС: получено меньше зданий, чем заявил источник")
                    break
            except Exception as e:
                logger.warning(f"[Population] 2GIS buildings page {page} error: {e}")
                warnings.append(f"2ГИС: страница {page} недоступна ({type(e).__name__})")
                break

        if not coverage_complete and not warnings:
            warnings.append("2ГИС: достигнут лимит страниц, охват зданий неполный")
        return BuildingFetchResult(all_items.values(), coverage_complete=coverage_complete and search_radius >= radius_m,
                                   warnings=warnings)

    @staticmethod
    def _hex_subcircles(center_lat: float, center_lng: float, radius_m: float) -> List[Tuple[float, float, float]]:
        offset_m = 0.55 * radius_m
        sub_radius = max(150.0, math.ceil(0.595 * radius_m) + 1.0)
        cos_lat = max(0.1, math.cos(math.radians(center_lat)))
        tiles = [(center_lat, center_lng, sub_radius)]
        for k in range(6):
            angle = math.radians(k * 60.0)
            d_lat = offset_m * math.cos(angle) / 111_132.0
            d_lng = offset_m * math.sin(angle) / (111_319.5 * cos_lat)
            tiles.append((center_lat + d_lat, center_lng + d_lng, sub_radius))
        return tiles

    @staticmethod
    def _is_2gis_capacity_truncation(result: Any, radius_m: float) -> bool:
        warnings = list(getattr(result, "warnings", []))
        return radius_m > 600 or any("лимит страниц" in w or "запрошено только" in w for w in warnings)

    def _fetch_2gis_buildings_covered_sync(
        self, center_lat: float, center_lng: float, radius_m: float
    ) -> BuildingFetchResult:
        first = self._fetch_2gis_buildings_sync(center_lat, center_lng, radius_m)
        if getattr(first, "coverage_complete", False):
            return first
        if not self._is_2gis_capacity_truncation(first, radius_m):
            return first

        max_calls = 60
        calls = 1
        merged: Dict[str, Dict[str, Any]] = {}
        for item in first:
            bid = building_identity(item.get("id"))
            if bid:
                merged[bid] = item
        leaf_warnings: List[str] = []
        all_complete = True
        queue = list(reversed(self._hex_subcircles(center_lat, center_lng, radius_m)))
        can_parallel = (
            "_fetch_2gis_buildings_sync" not in getattr(self, "__dict__", {})
            and type(getattr(self, "session", None)).__module__.split(".")[0] == "curl_cffi"
        )
        while queue:
            batch_coords = []
            while queue and len(batch_coords) < (4 if can_parallel else 1):
                sub_lat, sub_lng, sub_r = queue.pop()
                if sub_r > 600.0:
                    if calls + len(queue) + len(batch_coords) + 7 > max_calls:
                        all_complete = False
                        leaf_warnings.append("2ГИС: превышен лимит тайлов для полного обхода круга")
                        continue
                    queue.extend(reversed(self._hex_subcircles(sub_lat, sub_lng, sub_r)))
                    continue
                if calls + len(batch_coords) >= max_calls:
                    all_complete = False
                    leaf_warnings.append("2ГИС: достигнут лимит подучастков, охват зданий неполный")
                    continue
                batch_coords.append((sub_lat, sub_lng, sub_r))
            if not batch_coords:
                continue
            if len(batch_coords) == 1:
                sub_lat, sub_lng, sub_r = batch_coords[0]
                sub_results = [(sub_lat, sub_lng, sub_r, self._fetch_2gis_buildings_sync(sub_lat, sub_lng, sub_r))]
                calls += 1
            else:
                with ThreadPoolExecutor(max_workers=len(batch_coords)) as sub_pool:
                    futs = [
                        (slat, slng, sr, sub_pool.submit(self._fetch_2gis_buildings_sync, slat, slng, sr))
                        for slat, slng, sr in batch_coords
                    ]
                    calls += len(futs)
                    sub_results = [(slat, slng, sr, fut.result()) for slat, slng, sr, fut in futs]
            for sub_lat, sub_lng, sub_r, sub in sub_results:
                for item in sub:
                    bid = building_identity(item.get("id"))
                    if bid:
                        merged[bid] = item
                if getattr(sub, "coverage_complete", False):
                    continue
                if self._is_2gis_capacity_truncation(sub, sub_r) and sub_r > 165.0 and calls + len(queue) + 7 <= max_calls:
                    queue.extend(reversed(self._hex_subcircles(sub_lat, sub_lng, sub_r)))
                else:
                    all_complete = False
                    leaf_warnings.extend(getattr(sub, "warnings", []) or ["2ГИС: охват подучастка неполный"])

        warnings = [] if all_complete else list(dict.fromkeys(leaf_warnings or ["2ГИС: охват зданий неполный"]))
        return BuildingFetchResult(merged.values(), coverage_complete=all_complete, warnings=warnings)

    def _fetch_yandex_realty_prices_sync(
        self, center_lat: float, center_lng: float
    ) -> Tuple[List[float], List[float]]:
        dlat = 0.0060
        dlon = 0.0105
        city = city_for_coordinates(center_lat, center_lng)
        if city is None:
            return [], []
        url = (
            f"https://realty.yandex.ru/{city.realty_slug}/kupit/kvartira/"
            f"?leftLongitude={center_lng - dlon:.5f}"
            f"&rightLongitude={center_lng + dlon:.5f}"
            f"&bottomLatitude={center_lat - dlat:.5f}"
            f"&topLatitude={center_lat + dlat:.5f}"
        )
        prices_sqm: List[float] = []
        flat_prices_mln: List[float] = []
        try:
            resp = self.realty_session.get(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": f"https://realty.yandex.ru/{city.realty_slug}/",
                },
                timeout=9,
            )
            if resp.status_code == 200:
                m = re.search(r"window\.INITIAL_STATE\s*=\s*(\{.*?\});?\s*</script>", resp.text, re.DOTALL)
                if m:
                    state = orjson.loads(m.group(1).encode("utf-8", errors="ignore"))
                    offers = ((state.get("search") or {}).get("offers") or {}).get("entities") or []
                    for off in offers:
                        if not isinstance(off, dict):
                            continue
                        price_obj = off.get("price") or {}
                        val = price_obj.get("value")
                        per_sqm = price_obj.get("valuePerPart")
                        area_val = (off.get("area") or {}).get("value")
                        if not per_sqm and val and area_val and float(area_val) > 10:
                            per_sqm = float(val) / float(area_val)
                        if per_sqm and 150_000 <= float(per_sqm) <= 6_000_000:
                            prices_sqm.append(float(per_sqm))
                        if val and 4_000_000 <= float(val) <= 2_500_000_000:
                            flat_prices_mln.append(float(val) / 1_000_000.0)
        except Exception as e:
            logger.debug(f"[Population] Yandex Realty fetch warning: {e}")

        return prices_sqm, flat_prices_mln

    def _building_snapshot(self,lat,lng,radius,force_refresh):
        cache=getattr(self,'source_cache',None)
        if cache is None:return self._fetch_2gis_buildings_covered_sync(lat,lng,radius),dict(cache_status='disabled')
        def fetch():
            result=self._fetch_2gis_buildings_covered_sync(lat,lng,radius)
            return dict(items=list(result),coverage_complete=getattr(result,'coverage_complete',False),
                        warnings=list(getattr(result,'warnings',[])))
        def reusable(value):
            return (isinstance(value,dict) and value.get('coverage_complete') is True and value.get('warnings')==[] and
                    isinstance(value.get('items'),list) and all(isinstance(item,dict) for item in value['items']))
        snapshot=cache.get_or_fetch(cache.key('2gis_buildings_v3',lat=float(lat),lng=float(lng),radius_m=float(radius)),fetch,
            ttl=3600,cacheable=reusable,force_refresh=force_refresh)
        value=snapshot.value
        return BuildingFetchResult(value['items'],coverage_complete=value['coverage_complete'],warnings=value['warnings']),snapshot.diagnostics()

    def _price_snapshot(self,lat,lng,force_refresh):
        cache=getattr(self,'source_cache',None)
        if cache is None:return self._fetch_yandex_realty_prices_sync(lat,lng),dict(cache_status='disabled')
        def reusable(value):
            return (isinstance(value,(list,tuple)) and len(value)==2 and any(value) and
                    all(isinstance(group,(list,tuple)) and all(type(n) in (int,float) and math.isfinite(n) and n>0 for n in group)
                        for group in value))
        snapshot=cache.get_or_fetch(cache.key('yandex_housing_prices_v1',lat=float(lat),lng=float(lng)),
            lambda:self._fetch_yandex_realty_prices_sync(lat,lng),ttl=3600,cacheable=reusable,force_refresh=force_refresh)
        return snapshot.value,snapshot.diagnostics()

    def analyze_circle_sync(
        self, center_lat: float, center_lng: float, diameter_m: float = 400.0, *, force_refresh: bool = False
    ) -> ResidentialPopulationReport:
        t0 = time.perf_counter()
        if (not all(math.isfinite(v) for v in (center_lat, center_lng, diameter_m))
                or not -90 < center_lat < 90 or not -180 <= center_lng <= 180 or diameter_m <= 0):
            raise ValueError("Некорректные координаты или диаметр анализа населения")
        radius_m = diameter_m / 2.0

        data_warnings = [];snapshots={}

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_bldg = pool.submit(self._building_snapshot, center_lat, center_lng, radius_m,force_refresh)
            fut_realty = pool.submit(self._price_snapshot, center_lat, center_lng,force_refresh)
            try:
                raw_buildings,snapshots['buildings'] = fut_bldg.result()
            except Exception as exc:
                snapshots['buildings']=dict(cache_status='unavailable',error_type=type(exc).__name__)
                raw_buildings = BuildingFetchResult(warnings=[f"Данные зданий недоступны ({type(exc).__name__})"])
            try:
                (prices_sqm, flat_prices_mln),snapshots['housing_prices'] = fut_realty.result()
            except Exception as exc:
                snapshots['housing_prices']=dict(cache_status='unavailable',error_type=type(exc).__name__)
                prices_sqm, flat_prices_mln = [], []
                data_warnings.append(f"Цены жилья недоступны ({type(exc).__name__})")
        data_warnings.extend(getattr(raw_buildings, "warnings", []))
        coverage_complete = getattr(raw_buildings, "coverage_complete", False)
        if raw_buildings and not coverage_complete and not getattr(raw_buildings, "warnings", []):
            data_warnings.append("Полнота охвата зданий источником не подтверждена")

        city = city_for_coordinates(center_lat, center_lng)
        city_name = city.name if city else 'Не определён'
        district_name = city_name
        household_reference_available = bool(city and city.key == 'moscow')
        resident_dataset = None
        if os.environ.get('POPULATION_DATA_FILE'):
            try:
                from .population_data import BuildingPopulationDataset
                resident_dataset = BuildingPopulationDataset.from_file(os.environ['POPULATION_DATA_FILE'])
                snapshots['population'] = dict(source=resident_dataset.source, as_of=resident_dataset.as_of.isoformat(),
                    sha256=resident_dataset.sha256, measure='residents')
            except (ValueError, TypeError, OSError, KeyError) as exc:
                snapshots['population'] = dict(status='unavailable', error_type=type(exc).__name__)
                data_warnings.append('Набор жителей по зданиям не прошёл проверку источника, даты или формата')
        residential_buildings: List[ResidentialBuildingInfo] = []
        exact_passport_cnt = 0
        total_apartments = 0
        total_entrances = 0
        floors_list: List[int] = []
        years_list: List[int] = []
        materials_counter: Dict[str, int] = {}

        office_buildings_cnt = 0
        office_workers_total = 0
        non_operational_count = planned_residential_count = planned_apartments = 0
        outside_count = missing_coords_count = 0
        malformed_count = 0

        def positive_int(value):
            try:
                numeric = float(value)
                return int(numeric) if math.isfinite(numeric) and numeric > 0 else 0
            except (TypeError, ValueError, OverflowError):
                return 0

        for b in raw_buildings:
            if not isinstance(b, dict):
                malformed_count += 1
                continue
            b = dict(b)
            malformed = False
            for name in ("point", "structure_info", "floors", "geometry", "links"):
                if b.get(name) is not None and not isinstance(b[name], dict):
                    b[name] = {}
                    malformed = True
            if b.get("adm_div") is not None and not isinstance(b["adm_div"], list):
                b["adm_div"] = []
                malformed = True
            links = dict(b.get("links") or {})
            entrances = links.get("database_entrances")
            if entrances is not None and (not isinstance(entrances, list) or not all(isinstance(e,dict) for e in entrances)):
                links["database_entrances"] = [e for e in entrances if isinstance(e,dict)] if isinstance(entrances,list) else []
                malformed = True
            b["links"] = links
            geometry = dict(b.get("geometry") or {})
            if geometry.get("hover") is not None and not isinstance(geometry["hover"],str):
                geometry["hover"] = None
                malformed = True
            b["geometry"] = geometry
            malformed_count += int(malformed)
            pt = b.get("point") or {}
            blat = pt.get("lat")
            blon = pt.get("lon")
            try:
                blat, blon = float(blat), float(blon)
                if not math.isfinite(blat) or not math.isfinite(blon) or not -90 <= blat <= 90 or not -180 <= blon <= 180:
                    raise ValueError("invalid point")
                dist = haversine_distance(center_lat, center_lng, float(blat), float(blon))
                if dist > radius_m:
                    outside_count += 1
                    continue
            except (ValueError, TypeError):
                missing_coords_count += 1
                continue

            if district_name == city_name:
                for adm in b.get("adm_div") or []:
                    if isinstance(adm, dict) and adm.get("type") == "district" and adm.get("name"):
                        district_name = str(adm["name"])
                        break

            purpose = str(b.get("purpose_name") or b.get("building_name") or "").strip()
            purpose_low = purpose.lower()
            struct = b.get("structure_info") or {}
            apts_raw = positive_int(struct.get("apartments_count"))
            floors_raw = (b.get("floors") or {}).get("ground_count")
            floors = positive_int(floors_raw)

            hover_wkt = (b.get("geometry") or {}).get("hover")
            footprint_sqm = self._polygon_area_sqm(hover_wkt, center_lat)

            entrances_list = (b.get("links") or {}).get("database_entrances") or []
            numbered_entrances = [
                e for e in entrances_list
                if isinstance(e, dict) and (e.get("entity_number") or "подъезд" in str(e.get("entity_name") or "").lower())
            ]
            entrances_cnt = len(numbered_entrances) if numbered_entrances else len(entrances_list)

            is_residential = (
                apts_raw > 0
                or "жилой" in purpose_low
                or "общежитие" in purpose_low
                or "апартамент" in purpose_low
                or "таунхаус" in purpose_low
            )

            # Building lifecycle is a fact about the source object, independent of a business niche.
            lifecycle = " ".join(str(v or "") for v in (
                purpose, b.get("building_name"), b.get("construction_status"), struct.get("status")))
            year = positive_int(struct.get("year_of_construction"))
            planned = bool(re.search(r"строящ|незаверш[её]н|проектируем|планируем|under_construction|planned",
                                     lifecycle.lower())) or year > datetime.now(timezone.utc).year
            non_operational = planned or bool(re.search(r"рассел[её]н|снес[её]н|demolished", lifecycle.lower()))
            if non_operational:
                non_operational_count += 1
                if is_residential and planned:
                    planned_residential_count += 1
                    planned_apartments += apts_raw
                continue

            is_office_or_admin = (
                not is_residential
                and any(
                    kw in purpose_low
                    for kw in (
                        "административн",
                        "бизнес-центр",
                        "офис",
                        "учебн",
                        "научн",
                        "институт",
                        "министерств",
                        "банк",
                        "торгов",
                        "многофункциональн",
                    )
                )
            )

            if is_residential:
                if floors <= 0:
                    floors = 5
                is_exact = False
                if apts_raw > 0:
                    apts = apts_raw
                    is_exact = True
                    exact_passport_cnt += 1
                else:
                    if footprint_sqm >= 120:
                        res_floors = max(1, floors - 1) if floors >= 4 else floors
                        useful_res_sqm = footprint_sqm * res_floors * 0.72
                        apts = max(4, int(round(useful_res_sqm / 58.0)))
                    elif entrances_cnt > 0:
                        res_floors = max(1, floors - 1) if floors >= 5 else floors
                        apts = max(4, entrances_cnt * res_floors * 3)
                    else:
                        apts = max(6, floors * 6)

                yr_int = year if 1750 <= year <= datetime.now(timezone.utc).year else None
                mat = str(struct.get("material") or "").strip()

                if yr_int:
                    years_list.append(yr_int)
                if mat:
                    materials_counter[mat] = materials_counter.get(mat, 0) + 1
                floors_list.append(floors)

                dist_key = district_name.lower().replace("район", "").strip()
                hh_size = MOSCOW_DISTRICT_HOUSEHOLD_SIZE.get(dist_key, 2.18) if household_reference_available else None
                occupancy_rate = 0.92
                est_residents = int(round(apts * hh_size * occupancy_rate)) if hh_size is not None else None
                if resident_dataset is not None:
                    # An explicit import takes precedence; its missing records are not filled by another model.
                    est_residents = resident_dataset.lookup(b.get('id'), blat, blon)

                total_apartments += apts
                total_entrances += max(1, entrances_cnt)

                addr_str = str(b.get("name") or b.get("address_name") or "Жилой дом")
                residential_buildings.append(
                    ResidentialBuildingInfo(
                        building_id=str(b.get("id") or ""),
                        address=addr_str,
                        purpose_name=purpose or "Жилой дом",
                        distance_m=round(dist, 1),
                        floors=floors,
                        apartments_count=apts,
                        is_exact_apartments=is_exact,
                        entrances_count=max(1, entrances_cnt),
                        estimated_residents=est_residents,
                        year_of_construction=yr_int,
                        material=mat,
                        footprint_sqm=footprint_sqm,
                    )
                )

            elif is_office_or_admin:
                office_buildings_cnt += 1
                eff_floors = max(2, floors if floors > 0 else 4)
                eff_footprint = footprint_sqm if footprint_sqm >= 100 else 450.0
                office_gfa = eff_footprint * eff_floors * 0.65
                workers = int(round(office_gfa / 15.0))
                office_workers_total += min(workers, 3500)

        municipal_estimate = None
        municipal_source = getattr(self,'municipal_population_source',None)
        if not os.environ.get('POPULATION_DATA_FILE') and municipal_source is not None and residential_buildings:
            try:
                municipal_estimate = municipal_source.estimate_circle(city,raw_buildings,
                    [b.building_id for b in residential_buildings])
                if municipal_estimate:
                    snapshots['municipal_population'] = municipal_estimate
                    if municipal_estimate['usable']:
                        from .territory_population import building_identity
                        for building in residential_buildings:
                            row=municipal_estimate['building_estimates'].get(building_identity(building.building_id))
                            if row:
                                building.estimated_residents=round(row['point']) if row['point'] is not None else None
                                building.estimated_residents_range=(row['lower'],row['upper'])
                        data_warnings.append('Жители рассчитаны по полному округу и датированной статистике; диапазон сценарный, фактическая заселённость домов не измерялась')
            except Exception as exc:
                snapshots['municipal_population']=dict(status='unavailable',error_type=type(exc).__name__)
                data_warnings.append('Муниципальная модель населения недоступна; пропуск не заменён средним коэффициентом')
        municipal_usable=bool(municipal_estimate and municipal_estimate['usable'])
        if not municipal_usable and not household_reference_available and resident_dataset is None:
            if municipal_estimate and municipal_estimate.get('territories'):
                data_warnings.append('Муниципальная статистика жителей покрывает не все округа в круге; общий итог жителей неизвестен')
            elif municipal_estimate and municipal_estimate.get('errors'):
                data_warnings.append('Муниципальная модель населения не завершила расчёт по округу; общий итог жителей неизвестен')
            else:
                data_warnings.append('Для этого города не подключены параметры заселённости домохозяйств; число жителей неизвестно. Московские коэффициенты не применялись')
        resident_counts_complete = ((household_reference_available or resident_dataset is not None)
            and all(rb.estimated_residents is not None for rb in residential_buildings))
        estimated_residents_total = sum(rb.estimated_residents for rb in residential_buildings) if resident_counts_complete else None
        residents_range=None
        if municipal_usable:
            estimated_residents_total=round(municipal_estimate['point']) if municipal_estimate['point'] is not None else None
            residents_range=(municipal_estimate['lower'],municipal_estimate['upper'])
        if resident_dataset is not None:
            matched = sum(rb.estimated_residents is not None for rb in residential_buildings)
            snapshots['population'].update(matched_buildings=matched, residential_buildings=len(residential_buildings),
                coverage_complete=bool(coverage_complete and resident_counts_complete))
            if not resident_counts_complete:
                data_warnings.append('Набор жителей покрывает не все жилые здания; общий итог жителей неизвестен')
        buildings_available = bool(residential_buildings or office_buildings_cnt)
        prices_available = bool(prices_sqm or flat_prices_mln)
        if non_operational_count:
            data_warnings.append(f"Исключено строящихся или неэксплуатируемых зданий: {non_operational_count}; будущие квартиры не считаются нынешними жителями")
        if malformed_count:
            data_warnings.append(f"Некорректный формат данных зданий: {malformed_count}; повреждённые поля пропущены, оценки неполные")
        if missing_coords_count:
            data_warnings.append(f"Здания без корректных координат исключены: {missing_coords_count}")
        if buildings_available:
            data_warnings.append("Жители и офисные сотрудники оценены по модели; фактическая заселённость и посещаемость не измерены")
        if not buildings_available:
            data_warnings.append("Нет данных о жилых и офисных зданиях; население не определено")
        if not prices_available:
            data_warnings.append("Нет выборки цен объявлений жилья")

        avg_floors = round(statistics.mean(floors_list), 1) if floors_list else 0.0
        avg_year = int(round(statistics.mean(years_list))) if years_list else None
        predominant_material = (
            max(materials_counter.items(), key=lambda x: x[1])[0]
            if materials_counter
            else "н/д"
        )

        median_sqm_rub = round(statistics.median(prices_sqm), 0) if prices_sqm else None
        median_flat_mln = round(statistics.median(flat_prices_mln), 1) if flat_prices_mln else None

        residential_buildings.sort(key=lambda x: (-(x.estimated_residents or 0), x.distance_m))

        sources_used = []
        if buildings_available:
            sources_used.append("2ГИС: данные зданий (catalog.api.2gis.ru)")
        if prices_available:
            sources_used.append("Яндекс Недвижимость: цены объявлений поблизости")
        if resident_dataset is not None:
            sources_used.append('Жители по зданиям: '+resident_dataset.source)
        if municipal_usable:
            sources_used.append('Муниципальная модель жителей: '+municipal_estimate['territories'][0]['source'])
        if buildings_available and prices_available and coverage_complete and not malformed_count and resident_counts_complete:
            data_status = "ok"
        elif buildings_available or prices_available:
            data_status = "partial"
        else:
            data_status = "unavailable"

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        logger.info(
            f"[Population] Circle D={diameter_m:.0f}m around ({center_lat:.5f}, {center_lng:.5f}): "
            f"{len(residential_buildings)} res buildings ({exact_passport_cnt} exact passport), "
            f"{total_apartments} apts, ~{estimated_residents_total} residents, "
            f"~{office_workers_total} office workers, {len(prices_sqm)} unit-price samples and "
            f"{len(flat_prices_mln)} total-price samples in {elapsed_ms}ms"
        )

        return ResidentialPopulationReport(
            center_lat=center_lat,
            center_lng=center_lng,
            diameter_m=diameter_m,
            radius_m=radius_m,
            district_name=district_name,
            total_buildings_scanned=len(raw_buildings),
            residential_buildings_count=len(residential_buildings),
            exact_passport_buildings_count=exact_passport_cnt,
            total_apartments_count=total_apartments,
            total_entrances_count=total_entrances,
            estimated_residents_count=estimated_residents_total,
            avg_building_floors=avg_floors,
            avg_construction_year=avg_year,
            predominant_wall_material=predominant_material,
            office_buildings_count=office_buildings_cnt,
            estimated_office_workers_count=office_workers_total,
            realty_offers_sampled=len(prices_sqm),
            median_housing_price_sqm_rub=median_sqm_rub,
            median_flat_price_mln_rub=median_flat_mln,
            top_residential_buildings=residential_buildings[:10],
            fetch_time_ms=elapsed_ms,
            data_sources_used=sources_used,
            buildings_data_available=buildings_available,
            housing_prices_data_available=prices_available,
            data_status=data_status,
            data_warnings=data_warnings,
            building_coverage_complete=coverage_complete,
            non_operational_buildings_count=non_operational_count,
            planned_residential_buildings_count=planned_residential_count,
            planned_apartments_count=planned_apartments,
            buildings_outside_radius_count=outside_count,
            buildings_without_coordinates_count=missing_coords_count,
            malformed_buildings_count=malformed_count,
            source_snapshots=snapshots,
            estimated_residents_range=residents_range,
            population_model=municipal_estimate or {},
        )

    async def analyze_circle(
        self, center_lat: float, center_lng: float, diameter_m: float = 400.0, *, force_refresh: bool = False
    ) -> ResidentialPopulationReport:
        return await asyncio.to_thread(self.analyze_circle_sync,center_lat,center_lng,diameter_m,force_refresh=force_refresh)

