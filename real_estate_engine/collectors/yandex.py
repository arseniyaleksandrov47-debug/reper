import asyncio
import logging
import json
import re
import time
from typing import List, Optional
from urllib.parse import urljoin
from ..floor_policy import extract_floor_details
from .base import BaseCollector, extract_specs_from_text, preserve_source_fields
from ..models import CanonicalProperty, PlatformSource
from ..rate_limiter import AdaptiveRateLimiter
from ..cities import get_city

logger = logging.getLogger(__name__)


class YandexRealtyCollector(BaseCollector):
    def __init__(self, proxy: Optional[str] = None, rate_limiter: Optional[AdaptiveRateLimiter] = None, *, city=None):
        self.city = get_city(city)
        super().__init__(name="YandexRealty", domain="realty.ya.ru", proxy=proxy, rate_limiter=rate_limiter)
        self.web_url = f"https://realty.ya.ru/{self.city.realty_slug}/snyat/kommercheskaya-nedvizhimost/"

    async def collect(self, limit: int = 50) -> List[CanonicalProperty]:
        self.begin_collection(limit)
        if not self.source_access_ready():return []
        properties: List[CanonicalProperty] = []
        seen_ids = set()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://yandex.ru/",
        }
        loop = asyncio.get_event_loop()
        max_pages = max(1, (limit + 19) // 20)

        for page in range(1,max_pages+1):
            if len(properties) >= limit:
                break
            await self.pace(min_delay=0.8, max_delay=1.6)
            params = {"page": page} if page > 1 else {}
            try:
                t0_net = time.perf_counter()
                resp = await loop.run_in_executor(
                    None, lambda p=params: self.session.get(self.web_url, params=p, headers=headers, timeout=15)
                )
                net_time_ms = (time.perf_counter() - t0_net) * 1000.0
                self.record_status(resp.status_code)
                if resp.status_code != 200:
                    self.record_access_issue(resp)
                    logger.warning(f"[Yandex] HTTP {resp.status_code}")
                    break

                m = re.search(r"window\.INITIAL_STATE\s*=\s*", resp.text)
                if not m:
                    if not self.record_access_issue(resp):
                        self.record_collection_error("Не найдено состояние поиска; возможна смена формата страницы")
                    break

                t0_parse = time.perf_counter()
                data,_ = json.JSONDecoder().raw_decode(resp.text[m.end():].lstrip())
                if "entities" not in (data.get("search", {}).get("offers") or {}):
                    if not self.record_access_issue(resp):
                        self.record_collection_error("Не распознана структура списка объявлений")
                    break
                self.record_parsed_page()
                parse_time_ms = (time.perf_counter() - t0_parse) * 1000.0
                offers_entities = data.get("search", {}).get("offers", {}).get("entities", [])
                if not offers_entities:
                    break

                for item in offers_entities:
                    if len(properties) >= limit:
                        break
                    try:
                        ext_id = str(item.get("offerId") or item.get("id") or "")
                        if not ext_id or ext_id in seen_ids:
                            continue
                        loc = item.get("location", {})
                        pt = loc.get("point", {}) or loc.get("expectedMetro", {})
                        lat, lng = float(pt.get("latitude", 0.0)), float(pt.get("longitude", 0.0))
                        if lat == 0.0 or lng == 0.0:
                            continue
                        area = float(item.get("area", {}).get("value", 0.0))
                        if area <= 0:
                            continue

                        price_info = item.get("price", {})
                        if (price_info.get("currency", "RUR") not in ("RUR", "RUB")
                                or price_info.get("unit") not in (None, "WHOLE_OFFER", "SQUARE_METER")
                                or price_info.get("period") not in (None, "PER_MONTH", "PER_YEAR")):
                            raise ValueError("Unsupported currency or price units")
                        price_month = float(price_info.get("value", 0.0))
                        if price_info.get("unit") == "SQUARE_METER":
                            price_month *= area
                        if price_info.get("period") == "PER_YEAR":
                            price_month /= 12.0
                        if price_month <= 0:
                            continue

                        price_sqm_year = (price_month * 12.0) / area
                        address = loc.get("geocoderAddress", "") or loc.get("unifiedAddress", self.city.name)
                        desc = item.get("description", "") or ""
                        specs = extract_specs_from_text(desc)
                        floors = item.get("floorsOffered") or []
                        floor_details = extract_floor_details(offered_floors=floors, description=desc)
                        imgs = item.get("fullImages", []) or item.get("appMiddleImages", [])
                        photos = [("https:" + i if i.startswith("//") else i) for i in imgs[:5] if isinstance(i, str)]

                        seen_ids.add(ext_id)
                        properties.append(
                            CanonicalProperty(
                                source=PlatformSource.YANDEX,
                                external_id=ext_id,
                                title=item.get("title") or f"Коммерческое помещение {area} м² ({address.split(',')[-1].strip() if address else ''})",
                                address=address,
                                lat=lat,
                                lng=lng,
                                price_month=round(price_month, 2),
                                price_sqm_year=round(price_sqm_year, 2),
                                area_sqm=area,
                                floor=floor_details["floor"],
                                has_separate_entrance=bool(specs["has_separate_entrance"]),
                                power_kw=specs["power_kw"],
                                photos=photos,
                                url=urljoin(self.web_url,item.get("url") or f"/offer/{ext_id}/"),
                                description=desc,
                                raw_attributes=preserve_source_fields(item,floor_details),
                            )
                        )
                    except Exception as parse_err:
                        self.record_parse_error()
                        logger.debug(f"[Yandex] Entity parse error: {parse_err}")
                logger.info(f"[Yandex] Page {page}: Net={net_time_ms:.1f}ms | JSON={parse_time_ms:.2f}ms | Total={len(properties)}")
            except Exception as e:
                self.record_collection_error(f"Сбор недоступен ({type(e).__name__})")
                logger.error(f"[Yandex] Fetch error: {e}")
                break

        return properties
