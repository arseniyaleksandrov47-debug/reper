import asyncio
import logging
import time
import orjson
from typing import List, Optional
from ..floor_policy import extract_floor_details
from .base import BaseCollector, extract_specs_from_text, preserve_source_fields
from ..models import CanonicalProperty, PlatformSource
from ..rate_limiter import AdaptiveRateLimiter
from ..cities import get_city

logger = logging.getLogger(__name__)


class CianCollector(BaseCollector):
    def __init__(self, proxy: Optional[str] = None, rate_limiter: Optional[AdaptiveRateLimiter] = None, *, city=None):
        self.city = get_city(city)
        super().__init__(name="CIAN", domain="api.cian.ru", proxy=proxy, rate_limiter=rate_limiter)
        self.endpoint = "https://api.cian.ru/search-offers/v2/search-offers-desktop/"

    async def collect(self, limit: int = 50) -> List[CanonicalProperty]:
        self.begin_collection(limit)
        if not self.source_access_ready():return []
        properties: List[CanonicalProperty] = []
        seen_ids = set()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": f"https://{self.city.cian_host}/snyat-pomeshenie/",
            "Origin": "https://www.cian.ru",
        }
        loop = asyncio.get_event_loop()
        max_pages = max(1, (limit + 27) // 28)

        for page in range(1, max_pages + 1):
            if len(properties) >= limit:
                break
            await self.pace(min_delay=1.0, max_delay=2.0)
            payload = {
                "jsonQuery": {
                    "_type": "commercialrent",
                    "region": {"type": "terms", "value": [self.city.cian_region]},
                    "office_type": {"type": "terms", "value": [1, 2, 3, 5, 7]},
                    "engine_version": {"type": "term", "value": 2},
                    "page": {"type": "term", "value": page},
                }
            }
            try:
                t0 = time.perf_counter()
                resp = await loop.run_in_executor(
                    None, lambda p=payload: self.session.post(self.endpoint, json=p, headers=headers, timeout=15)
                )
                net_time_ms = (time.perf_counter() - t0) * 1000.0
                self.record_status(resp.status_code)
                if self.record_access_issue(resp):break
                if resp.status_code != 200:
                    logger.warning(f"[CIAN] HTTP {resp.status_code}")
                    break

                t0_parse = time.perf_counter()
                data = orjson.loads(resp.content)
                if "offersSerialized" not in (data.get("data") or {}):
                    self.record_collection_error("Не распознана структура списка объявлений")
                    break
                self.record_parsed_page()
                parse_time_ms = (time.perf_counter() - t0_parse) * 1000.0
                raw_offers = data.get("data", {}).get("offersSerialized", [])
                if not raw_offers:
                    break

                for item in raw_offers:
                    if len(properties) >= limit:
                        break
                    try:
                        ext_id = str(item.get("id") or "")
                        if not ext_id or ext_id in seen_ids:
                            continue
                        geo = item.get("geo", {})
                        coords = geo.get("coordinates", {})
                        lat, lng = float(coords.get("lat", 0.0)), float(coords.get("lng", 0.0))
                        if lat == 0.0 or lng == 0.0:
                            continue
                        area = float(item.get("totalArea", 0.0) or 0.0)
                        if area <= 0:
                            continue
                        terms = item.get("bargainTerms", {}) or {}
                        price_month = float(terms.get("priceRur") or terms.get("price") or 0.0)
                        if price_month <= 0:
                            continue
                        if terms.get("priceType") == "squareMeter":
                            price_month *= area
                        if terms.get("paymentPeriod") == "annual":
                            price_month /= 12.0

                        price_sqm_year = (price_month * 12.0) / area
                        address = geo.get("userInput", "") or item.get("formattedAddress", self.city.name)
                        desc = item.get("description", "") or ""
                        specs = extract_specs_from_text(desc)
                        floor = item.get("floorNumber")
                        if floor is None:
                            floor = (item.get("building") or {}).get("floorNumber")
                        floor_details = extract_floor_details(floor, title=item.get("title") or "", description=desc)
                        has_sep = "separate" in str(item.get("entranceType", "")).lower() or bool(specs["has_separate_entrance"])
                        photos = [p.get("fullUrl") for p in item.get("photos", [])[:5] if p.get("fullUrl")]

                        seen_ids.add(ext_id)
                        properties.append(
                            CanonicalProperty(
                                source=PlatformSource.CIAN,
                                external_id=ext_id,
                                title=item.get("title") or f"ПСН {area} м²",
                                address=address,
                                lat=lat,
                                lng=lng,
                                price_month=round(price_month, 2),
                                price_sqm_year=round(price_sqm_year, 2),
                                area_sqm=area,
                                floor=floor_details["floor"],
                                has_separate_entrance=has_sep,
                                power_kw=specs["power_kw"],
                                photos=photos,
                                url=item.get("fullUrl", f"https://www.cian.ru/rent/commercial/{ext_id}/"),
                                description=desc,
                                raw_attributes=preserve_source_fields(item,floor_details),
                            )
                        )
                    except Exception as parse_err:
                        self.record_parse_error()
                        logger.debug(f"[CIAN] Parse item error: {parse_err}")
                logger.info(f"[CIAN] Page {page}: Net={net_time_ms:.1f}ms | orjson={parse_time_ms:.2f}ms | Total={len(properties)}")
            except Exception as e:
                self.record_collection_error(f"Сбор недоступен ({type(e).__name__})")
                logger.error(f"[CIAN] Fetch error: {e}")
                break

        return properties
