import asyncio
import logging
import math
import time
import orjson
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urljoin
from ..floor_policy import extract_floor_details
from .base import BaseCollector, extract_specs_from_text, preserve_source_fields
from ..models import CanonicalProperty, PlatformSource
from ..rate_limiter import AdaptiveRateLimiter

logger = logging.getLogger(__name__)
PORTAL = "https://torgi.mos.ru/"
RENTAL_TYPE = "nsi:tender_type_portal:12"
NON_RESIDENTIAL = "nsi:41:30011569"


class InvestMoscowCollector(BaseCollector):
    def __init__(self, proxy: Optional[str] = None, rate_limiter: Optional[AdaptiveRateLimiter] = None):
        super().__init__(
            name="InvestMoscow",
            domain="api.torgi.mos.ru",
            proxy=proxy,
            rate_limiter=rate_limiter,
            verify=False,
        )
        self.endpoint = "https://api.torgi.mos.ru/pptorgi/tender/v2/filtered-tenders/searchtenderobjects"

    async def collect(self, limit: int = 50) -> List[CanonicalProperty]:
        self.begin_collection(limit)
        properties: List[CanonicalProperty] = []
        seen_ids = set()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": PORTAL,
            "Origin": PORTAL.rstrip("/"),
            "Accept-Language": "ru-RU,ru;q=0.9",
        }
        loop = asyncio.get_event_loop()
        page_size = min(max(20, limit), 50)

        for page in range(1, 4):
            if len(properties) >= limit:
                break
            await self.pace(min_delay=0.8, max_delay=1.6)
            payload = {
                "pageNumber": page,
                "pageSize": page_size,
                "orderBy": "Relevance",
                "orderAsc": False,
                "tenderStatus": "nsi:tender_status_tender_filter:1",
                "objectTypes": [NON_RESIDENTIAL],
                "objectKinds": [RENTAL_TYPE],
            }
            try:
                t0 = time.perf_counter()
                resp = await loop.run_in_executor(
                    None, lambda p=payload: self.session.post(self.endpoint, json=p, headers=headers, timeout=15)
                )
                net_time_ms = (time.perf_counter() - t0) * 1000.0
                self.record_status(resp.status_code)
                if resp.status_code != 200:
                    logger.warning(f"[InvestMoscow] HTTP {resp.status_code}")
                    break

                t0_parse = time.perf_counter()
                data = orjson.loads(resp.content)
                if not isinstance(data, dict) or not isinstance(data.get("entities"), list):
                    self.record_collection_error("Не распознана структура списка объявлений")
                    break
                self.record_parsed_page()
                parse_time_ms = (time.perf_counter() - t0_parse) * 1000.0
                entities = data.get("entities", [])
                if not entities:
                    break

                for ent in entities:
                    for item in ent.get("tenders", []):
                        if len(properties) >= limit:
                            break
                        try:
                            type_code = item.get("typeCode", "")
                            name_str = str(item.get("name", ""))
                            if type_code != RENTAL_TYPE or item.get("objectTypeCode") != NON_RESIDENTIAL:
                                continue
                            end = datetime.fromisoformat(item["requestEndDate"].replace("Z", "+00:00"))
                            if end.tzinfo is None:
                                raise ValueError("Auction deadline has no timezone")
                            if end <= datetime.now(timezone.utc):
                                continue
                            ext_id = str(item.get("id") or "")
                            if not ext_id or ext_id in seen_ids:
                                continue

                            coords = item.get("coords")
                            if isinstance(coords, str) and coords.startswith("["):
                                coords = orjson.loads(coords)
                            if isinstance(coords, list) and len(coords) >= 2:
                                lat, lng = float(coords[0]), float(coords[1])
                            elif isinstance(coords, dict):
                                lat = float(coords.get("lat") or coords.get("latitude") or 0.0)
                                lng = float(coords.get("lng") or coords.get("lon") or 0.0)
                            else:
                                continue
                            if lat == 0.0 or lng == 0.0:
                                continue

                            area = float(item.get("objectArea") or item.get("area") or 0.0)
                            if area <= 0:
                                continue
                            start_price_year = float(item.get("startPrice") or 0.0)
                            price_sqm_year = float(item.get("pricePerSquare") or 0.0)
                            if start_price_year <= 0 and price_sqm_year > 0:
                                start_price_year = price_sqm_year * area
                            if not math.isfinite(start_price_year) or start_price_year <= 0:
                                continue

                            price_month = start_price_year / 12.0
                            if price_sqm_year <= 0:
                                price_sqm_year = start_price_year / area

                            purposes = item.get("functionalityPurposes") or []
                            purpose_str = ", ".join(str(p) for p in purposes if p) if purposes else "Свободное"
                            address = item.get("address") or item.get("objectAddress")
                            if not isinstance(address,str) or not address.strip():
                                raise ValueError("Auction address missing")
                            title = name_str or f"Городская аренда (ДГИ): {area} м²"
                            desc = (f"Городские торги. Назначение: {purpose_str}. "
                                    f"Годовая стартовая ставка: {start_price_year:g} ₽; итог зависит от торгов.")
                            if isinstance(item.get('description'),str) and item['description'].strip():desc+='\n'+item['description']
                            specs = extract_specs_from_text(f"{title} {desc}")
                            floor_details = extract_floor_details(item.get("floor"), title=title, description=f"{desc} {address}")
                            url = urljoin(PORTAL, f"tender/{ext_id}")

                            seen_ids.add(ext_id)
                            properties.append(
                                CanonicalProperty(
                                    source=PlatformSource.INVESTMOSCOW,
                                    external_id=ext_id,
                                    title=title,
                                    address=address,
                                    lat=lat,
                                    lng=lng,
                                    price_month=round(price_month, 2),
                                    price_sqm_year=round(price_sqm_year, 2),
                                    area_sqm=area,
                                    floor=floor_details["floor"],
                                    has_separate_entrance=bool(specs["has_separate_entrance"]),
                                    power_kw=specs["power_kw"],
                                    url=url,
                                    description=desc,
                                    raw_attributes=preserve_source_fields(item,{"program": item.get("programCode"),
                                        "purposes": purpose_str, "auction_start_year": start_price_year, **floor_details}),
                                )
                            )
                        except Exception as parse_err:
                            self.record_parse_error()
                            logger.debug(f"[InvestMoscow] Item parse error: {parse_err}")
                logger.info(f"[InvestMoscow] Page {page}: Net={net_time_ms:.1f}ms | orjson={parse_time_ms:.2f}ms | Total={len(properties)}")
                if len(entities) < page_size:
                    break
            except Exception as e:
                self.record_collection_error(f"Сбор недоступен ({type(e).__name__})")
                logger.error(f"[InvestMoscow] Fetch error: {e}")
                break

        return properties
