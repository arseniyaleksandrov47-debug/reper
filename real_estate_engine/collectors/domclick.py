import asyncio
import logging
import time
import threading
from urllib.parse import urljoin
import orjson
from typing import List, Optional
from ..floor_policy import extract_floor_details
from .base import BaseCollector, extract_specs_from_text, preserve_source_fields
from ..models import CanonicalProperty, PlatformSource
from ..rate_limiter import AdaptiveRateLimiter
from ..cities import get_city

logger = logging.getLogger(__name__)


class DomclickCollector(BaseCollector):
    def __init__(self, proxy: Optional[str] = None, rate_limiter: Optional[AdaptiveRateLimiter] = None, *, city=None):
        self.city = get_city(city)
        super().__init__(
            name="Domclick",
            domain="bff-search-web.domclick.ru",
            proxy=proxy,
            rate_limiter=rate_limiter,
            impersonate="safari_ios",
            verify=False,
        )
        self.endpoint = "https://bff-search-web.domclick.ru/api/offers/v1"
        self._catalogue_lock = threading.Lock()

    def public_unit_snapshot(self, external_id, *, timeout=15, max_items=120):
        """Refresh a bounded public rental catalogue and return only the exact ID."""
        from .access_state import response_access_issue, SourceAccessError
        if not self.city.domclick_address or not str(external_id).isdigit():
            raise ValueError('Unconfigured catalogue or invalid listing ID')
        deadline = time.monotonic() + timeout
        with self._catalogue_lock:
            for offset in range(0, max_items, 30):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Public catalogue lookup budget expired')
                if offset:
                    time.sleep(min(.8, remaining))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Public catalogue lookup budget expired')
                response = self.session.get(self.endpoint, params=dict(address=self.city.domclick_address,
                    deal_type='rent', category='commercial', offset=offset, limit=30,
                    sort='published', sort_dir='desc'), headers={'Accept':'application/json',
                    'User-Agent':'Domclick/6.54.0 (iPhone; iOS 17.4; Scale/3.00)'}, timeout=min(8, remaining))
                issue = response_access_issue(response)
                if issue:
                    raise SourceAccessError(issue['kind'], issue['status_code'], issue['retry_after_s'])
                response.raise_for_status()
                payload = orjson.loads(response.content)
                items = payload.get('result', {}).get('items', payload.get('items'))
                if not isinstance(items, list):
                    raise ValueError('Public catalogue format unavailable')
                matches = [item for item in items if isinstance(item, dict) and str(item.get('id')) == str(external_id)]
                if len(matches) > 1:
                    raise ValueError('Catalogue contains conflicting copies of the listing')
                if matches:
                    return matches[0], dict(source_url=str(response.url), fetched_at=time.time(),
                        region_id=self.city.domclick_address, city=self.city.key, offset=offset)
                if len(items) < 30:
                    break
        raise ValueError('Exact listing not found in the refreshed catalogue sample')

    async def collect(self, limit: int = 50) -> List[CanonicalProperty]:
        self.begin_collection(limit)
        if not self.city.domclick_address:
            self.record_collection_error('Региональный идентификатор Домклик не подтверждён; источник для этого города не подключён')
            return []
        properties: List[CanonicalProperty] = []
        seen_ids = set()
        headers = {
            "User-Agent": "Domclick/6.54.0 (iPhone; iOS 17.4; Scale/3.00)",
            "Accept": "application/json",
            "Accept-Language": "ru-RU,ru;q=0.9",
        }
        loop = asyncio.get_event_loop()
        page_size = min(limit, 30)
        max_pages = max(1, (limit + page_size - 1) // page_size)

        for page in range(max_pages):
            if len(properties) >= limit:
                break
            await self.pace(min_delay=0.8, max_delay=1.6)
            params = {
                "address": self.city.domclick_address,
                "deal_type": "rent",
                "category": "commercial",
                "offset": page * page_size,
                "limit": page_size,
                "sort": "published",
                "sort_dir": "desc",
            }
            try:
                t0 = time.perf_counter()
                resp = await loop.run_in_executor(
                    None, lambda p=params: self.session.get(self.endpoint, params=p, headers=headers, timeout=15)
                )
                net_time_ms = (time.perf_counter() - t0) * 1000.0
                self.record_status(resp.status_code)
                if resp.status_code != 200:
                    logger.warning(f"[Domclick] HTTP {resp.status_code}")
                    break

                t0_parse = time.perf_counter()
                data = orjson.loads(resp.content)
                if "items" not in (data.get("result") or {}) and "items" not in data:
                    self.record_collection_error("Не распознана структура списка объявлений")
                    break
                self.record_parsed_page()
                parse_time_ms = (time.perf_counter() - t0_parse) * 1000.0
                raw_items = data.get("result", {}).get("items", []) or data.get("items", [])
                if not raw_items:
                    break

                for item in raw_items:
                    if len(properties) >= limit:
                        break
                    try:
                        ext_id = str(item.get("id") or "")
                        if not ext_id or ext_id in seen_ids:
                            continue
                        loc = item.get("location", {}) or {}
                        lat = float(loc.get("lat") or 0.0)
                        lng = float(loc.get("lon") or loc.get("lng") or 0.0)
                        if lat == 0.0 or lng == 0.0:
                            continue
                        obj_info = item.get("objectInfo", {}) or item.get("object_info", {}) or {}
                        area = float(obj_info.get("area") or item.get("area") or 0.0)
                        if area <= 0:
                            continue
                        raw_price = item.get("price")
                        price_month = float(raw_price.get("value", 0.0) if isinstance(raw_price, dict) else (raw_price or 0.0))
                        if price_month <= 0:
                            continue

                        price_sqm_year = (price_month * 12.0) / area
                        address_info = item.get("address", {}) or {}
                        address = address_info.get("displayName") or address_info.get("display_name") or self.city.name
                        desc = item.get("description", "") or ""
                        specs = extract_specs_from_text(desc)
                        floor = obj_info.get("floor")
                        floor_details = extract_floor_details(floor, description=desc)
                        photos = [
                            urljoin('https://img.dmclk.ru/', p['url'])
                            for p in (item.get("photos") or [])[:5]
                            if isinstance(p, dict) and p.get("url")
                        ]
                        offer_type = item.get("offerType") or "commercial"
                        url = item.get("path") or f"https://domclick.ru/card/rent__{offer_type}__{ext_id}"

                        seen_ids.add(ext_id)
                        properties.append(
                            CanonicalProperty(
                                source=PlatformSource.DOMCLICK,
                                external_id=ext_id,
                                title=f"Помещение ({offer_type}) {area} м²",
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
                                url=url,
                                description=desc,
                                raw_attributes=preserve_source_fields(item,floor_details),
                            )
                        )
                    except Exception as e:
                        self.record_parse_error()
                        logger.debug(f"[Domclick] Item parse error: {e}")
                logger.info(f"[Domclick] Page {page}: Net={net_time_ms:.1f}ms | orjson={parse_time_ms:.2f}ms | Total={len(properties)}")
            except Exception as e:
                self.record_collection_error(f"Сбор недоступен ({type(e).__name__})")
                logger.error(f"[Domclick] Fetch error: {e}")
                break

        return properties
