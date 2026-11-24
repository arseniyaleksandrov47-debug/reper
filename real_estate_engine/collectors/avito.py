import asyncio
import base64
import hashlib
import logging
import re
import threading
import time
import urllib.parse
import orjson
from typing import List, Optional
from ..floor_policy import extract_floor_details
from .base import BaseCollector, extract_specs_from_text, preserve_source_fields
from ..models import CanonicalProperty, PlatformSource
from ..rate_limiter import AdaptiveRateLimiter
from ..cities import get_city

logger = logging.getLogger(__name__)


def catalogue_description(item):
    """Read the item's own description component, not seller/similar-item text."""
    values=[]
    flat=item.get('description')
    if isinstance(flat,str) and flat.strip():values.append(flat.strip())
    iva=item.get('iva',{})
    rows=iva.get('DescriptionStep',[]) if isinstance(iva,dict) else []
    for row in rows if isinstance(rows,list) else []:
        payload=row.get('payload',{}) if isinstance(row,dict) else {}
        if not isinstance(payload,dict):continue
        identity=payload.get('debug',{});identity=identity.get('id') if isinstance(identity,dict) else None
        if identity is not None and str(identity)!=str(item.get('id')):continue
        text=payload.get('description')
        if isinstance(text,str) and text.strip() and text.strip() not in values:
            text=text.strip();values=[v for v in values if not text.startswith(v)];values.append(text)
    return '\n\n'.join(values)


class AvitoCollector(BaseCollector):
    def __init__(self, proxy: Optional[str] = None, rate_limiter: Optional[AdaptiveRateLimiter] = None, *, city=None):
        self.city = get_city(city)
        super().__init__(
            name="Avito",
            domain="www.avito.ru",
            proxy=proxy,
            rate_limiter=rate_limiter,
            impersonate="edge101",
            verify=False,
        )
        self.endpoint = f"https://www.avito.ru/{self.city.avito_slug}/kommercheskaya_nedvizhimost/sdam-ASgBAgICAUSwCNRW"
        self._request_lock = threading.Lock()

    @staticmethod
    def _remaining(deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Avito request budget expired")
        return remaining

    def _fetch_page_sync(self, url, *, params=None, headers=None, timeout=15):
        deadline = time.monotonic() + timeout
        if not self._request_lock.acquire(timeout=timeout):
            raise TimeoutError("Avito session is busy")
        try:
            response = self.session.get(url, params=params, headers=headers, timeout=self._remaining(deadline))
            if response.status_code == 439 and self._solve_pow_sync(deadline):
                response = self.session.get(url, params=params, headers=headers, timeout=self._remaining(deadline))
            return response
        finally:
            self._request_lock.release()

    def _solve_pow_sync(self, deadline) -> bool:
        try:
            raw_cookie = self.session.cookies.get("pow_challenge", "")
            if not raw_cookie:
                return False
            challenge = urllib.parse.unquote(raw_cookie)
            rg = self.session.post("https://www.avito.ru/web/3/firewallPow/get", json={"challenge": challenge},
                                   timeout=self._remaining(deadline))
            if rg.status_code != 200:
                return False
            jwt_tok = orjson.loads(rg.content).get("success", {}).get("result", {}).get("challenge_jwt")
            if not jwt_tok:
                return False

            b64 = jwt_tok.split(".")[1].replace("-", "+").replace("_", "/")
            b64 += "=" * ((4 - len(b64) % 4) % 4)
            payload = orjson.loads(base64.b64decode(b64))
            pid, compl = payload["id"], int(payload["compl"])
            if not 0 <= compl <= 6:
                return False
            prefix = "0" * compl
            nonce = 0
            while not hashlib.sha256(f"{pid}:{nonce}".encode()).hexdigest().startswith(prefix):
                if nonce % 256 == 0:
                    self._remaining(deadline)
                nonce += 1

            rv = self.session.post("https://www.avito.ru/web/3/firewallPow/verify", json={"challenge": jwt_tok, "nonce": nonce},
                                   timeout=self._remaining(deadline))
            if rv.status_code == 200 and orjson.loads(rv.content).get("success", {}).get("result", {}).get("verified"):
                self.session.cookies.set("pow_solved", "1", domain="www.avito.ru")
                logger.info(f"[Avito] Solved firewallPow (compl={compl}, nonce={nonce})")
                return True
        except Exception as e:
            logger.debug(f"[Avito] firewallPow solve error: {e}")
        return False

    async def collect(self, limit: int = 50) -> List[CanonicalProperty]:
        self.begin_collection(limit)
        if not self.source_access_ready():return []
        properties: List[CanonicalProperty] = []
        seen_ids = set()
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        }
        loop = asyncio.get_event_loop()
        max_pages = max(1, (limit + 49) // 50)

        for page in range(1, max_pages + 1):
            if len(properties) >= limit:
                break
            await self.pace(min_delay=1.2, max_delay=2.5)
            params = {"p": page} if page > 1 else {}

            try:
                t0 = time.perf_counter()

                def _fetch(p=params):
                    return self._fetch_page_sync(self.endpoint, params=p, headers=headers)

                resp = await loop.run_in_executor(None, _fetch)
                net_time_ms = (time.perf_counter() - t0) * 1000.0
                self.record_status(resp.status_code)

                if resp.status_code != 200:
                    self.record_access_issue(resp)
                    logger.warning(f"[Avito] HTTP {resp.status_code}")
                    break

                t0_parse = time.perf_counter()
                scripts = re.findall(r"<script[^>]*>(.*?)</script>", resp.text, re.DOTALL)
                raw_items = []
                recognized = False
                for sc in scripts:
                    if len(sc) > 100000 and '"loaderData"' in sc[:400]:
                        data = orjson.loads(sc.strip().encode("utf-8"))
                        recognized = "items" in (data.get("loaderData", {}).get("data", {}).get("catalog") or {})
                        raw_items = data.get("loaderData", {}).get("data", {}).get("catalog", {}).get("items", [])
                        break
                parse_time_ms = (time.perf_counter() - t0_parse) * 1000.0
                if not recognized:
                    if not self.record_access_issue(resp):
                        self.record_collection_error("Не распознана структура списка объявлений; возможна смена формата")
                    break
                self.record_parsed_page()

                if not raw_items:
                    break

                for item in raw_items:
                    if len(properties) >= limit:
                        break
                    try:
                        ext_id = str(item.get("id") or "")
                        if not ext_id or ext_id == "None" or ext_id in seen_ids:
                            continue
                        coords = item.get("coords", {}) or {}
                        lat = float(coords.get("lat") or 0.0)
                        lng = float(coords.get("lng") or 0.0)
                        if lat == 0.0 or lng == 0.0:
                            continue

                        title = item.get("title") or "Помещение на Авито"
                        desc = catalogue_description(item)
                        m_area = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:м²|м2|кв\.?\s*м)", f"{title} {desc}", re.I)
                        area = float(m_area.group(1).replace(",", ".")) if m_area else 0.0
                        if area <= 0:
                            continue

                        price_info = item.get("priceDetailed", {}) or {}
                        price_month = float(price_info.get("value") or 0.0)
                        if price_month <= 0:
                            continue
                        postfix = str(price_info.get("postfix") or "").lower()
                        if "за м²" in postfix or "за м2" in postfix:
                            price_month *= area
                        if "в год" in postfix:
                            price_month /= 12.0

                        price_sqm_year = (price_month * 12.0) / area
                        geo = item.get("geo", {}) or {}
                        address = coords.get("address_user") or geo.get("formattedAddress") or self.city.name
                        specs = extract_specs_from_text(f"{title} {desc}")
                        floor_details = extract_floor_details(item.get("floor"), title=title, description=desc)

                        photos = []
                        for img in (item.get("images") or [])[:5]:
                            if isinstance(img, dict):
                                u = img.get("636x476") or img.get("432x324") or next(iter(img.values()), None)
                                if isinstance(u, str):
                                    photos.append(u)

                        url_path = item.get("urlPath") or f"/{ext_id}"
                        url = f"https://www.avito.ru{url_path.split('?')[0]}"

                        seen_ids.add(ext_id)
                        properties.append(
                            CanonicalProperty(
                                source=PlatformSource.AVITO,
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
                                photos=photos,
                                url=url,
                                description=desc,
                                raw_attributes=preserve_source_fields(item,floor_details),
                            )
                        )
                    except Exception as parse_err:
                        self.record_parse_error()
                        logger.debug(f"[Avito] Item parse error: {parse_err}")
                logger.info(f"[Avito] Page {page}: Net={net_time_ms:.1f}ms | orjson={parse_time_ms:.2f}ms | Total={len(properties)}")
            except Exception as e:
                self.record_collection_error(f"Сбор недоступен ({type(e).__name__})")
                logger.error(f"[Avito] Fetch error: {e}")
                break

        return properties
