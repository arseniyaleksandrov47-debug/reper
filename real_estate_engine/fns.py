import asyncio
import copy
import json
import logging
import math
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import orjson
import websockets
from curl_cffi import requests as curl_requests

from .collectors.base import _DirectInterfaceBridge
from .credentials import load_dadata_key

logger = logging.getLogger("FnsEnricher")

DEFAULT_DADATA_API_KEY = (
    os.environ.get("DADATA_API_KEY")
    or "093459bf07647fc12c2a897ef85dd7ca80822c9c"
)


def resolve_dadata_key(api_key: Optional[str] = None) -> str:
    """Resolve explicit, environment, DPAPI-stored, or legacy fallback DaData key."""
    if api_key is not None and api_key != "":
        return api_key
    return load_dadata_key() or DEFAULT_DADATA_API_KEY


@dataclass
class FnsCompanyProfile:
    inn: str
    legal_name: str
    ogrn: Optional[str] = None
    okved: Optional[str] = None
    address: Optional[str] = None
    revenue_rub: Optional[float] = None
    prev_revenue_rub: Optional[float] = None
    net_profit_rub: Optional[float] = None
    reporting_year: Optional[int] = None
    fns_org_id: Optional[int] = None
    match_method: str = "dadata"
    source_url: str = 'https://bo.nalog.gov.ru/'
    financial_unit: str = 'RUB (source thousand RUB multiplied by 1000)'

    @property
    def revenue_mln_rub(self) -> Optional[float]:
        if self.revenue_rub is None:
            return None
        return round(self.revenue_rub / 1_000_000.0, 2)


class DaDataInnResolver:
    """
    Резолвер ИНН и юрлица по карточке Яндекс Карт через DaData API:
    1. Словарь крупных сетевых брендов (чье торговое имя отличается от юрлица в ЕГРЮЛ).
    2. Поиск по названию бренда (включая транслитерацию латиницы в кириллицу) с ранжированием по ОКВЭД и региону.
    3. Поиск по улице/дому из адреса карточки + целевым кодам ОКВЭД рубрики.
    """

    SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"

    KNOWN_BRAND_INNS: Dict[str, List[str]] = {
        "азбука daily": ["7705466989"],          # ООО "ГОРОДСКОЙ СУПЕРМАРКЕТ"
        "азбука вкуса": ["7705466989"],          # ООО "ГОРОДСКОЙ СУПЕРМАРКЕТ"
        "вкусвилл": ["7734443270"],              # АО "ВКУСВИЛЛ"
        "пятерочка": ["7728029110"],             # ООО "АГРОТОРГ"
        "пятёрочка": ["7728029110"],             # ООО "АГРОТОРГ"
        "перекресток": ["7728029110"],           # АО "ТД ПЕРЕКРЕСТОК"
        "перекрёсток": ["7728029110"],
        "магнит": ["2309085638"],                # АО "ТАНДЕР"
        "дикси": ["7729602673"],                 # АО "ДИКСИ ЮГ"
        "шоколадница": ["7708074140", "5009051111"],  # ООО "ГАЛЕРА" / ООО "ШОКОЛАДНИЦА"
        "кофемания": ["7703516395"],             # ООО "КОФЕМАНИЯ"
        "моремания": ["7720315359", "5047163616"],
        "osteria mario": ["7714912344", "9705101105"],
        "surf coffee": ["7731390425"],
        "stars coffee": ["7714617793"],
        "one price coffee": ["7725490934"],
        "правда кофе": ["7715966822"],
        "чистая линия": ["5047191938"],          # ООО "ЧИСТАЯ ЛИНИЯ"
        "старик хинкалыч": ["9201517469"],       # ООО "СТАРИК ХИНКАЛЫЧ"
        "вьеткафе": ["9728133669", "7707745561", "7722820799"],
        "фармленд": ["0277098652", "0273028277"],
        "шезали": ["7732095187"],
        "время красоты": ["9721069809", "7720426758"],
        "ригла": ["7724211288"],
        "горздрав": ["7724211288"],
        "36,6": ["7719252267"],
        "столички": ["7732121736"],
        "неофарм": ["7732121736"],
    }


    CATEGORY_OKVED_MAP: List[Tuple[Tuple[str, ...], List[str]]] = [
        (
            ("кофейн", "кафе", "ресторан", "бар", "паб", "бистро", "столовая", "общепит", "хинкал", "суши", "пицц"),
            ["56.10", "56.10.1", "56.30", "56.29"],
        ),
        (
            ("пекарн", "кондитер", "булочн", "хлеб"),
            ["10.71", "10.72", "56.10", "56.10.1", "47.24"],
        ),
        (
            ("аптек", "оптика", "медиц"),
            ["47.73", "47.74", "86.21", "86.22", "86.90"],
        ),
        (
            ("продукт", "супермаркет", "гастроном", "магазин", "универсам", "алкогол", "вино"),
            ["47.11", "47.25", "47.2", "47.19", "47.21", "47.24", "47.29"],
        ),
        (
            ("красот", "парикмахер", "барбер", "ногт", "маникюр", "бров", "ресниц", "косметолог", "массаж", "спа"),
            ["96.02", "96.02.1", "96.02.2", "96.04", "86.22", "86.90"],
        ),
    ]

    def __init__(self, api_key: Optional[str] = None, proxy: Optional[str] = None):
        self.api_key = resolve_dadata_key(api_key)
        self.proxy = proxy or _DirectInterfaceBridge.get_proxy_url()
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        self.session = curl_requests.Session(
            impersonate="chrome120",
            proxies=proxies,
            verify=False,
        )
        self.headers = {
            "Authorization": f"Token {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._street_cache: Dict[str, List[Dict[str, Any]]] = {}


    @staticmethod
    def _transliterate_en_to_ru(text: str) -> str:
        multi = [
            ("shch", "щ"), ("sch", "щ"), ("sh", "ш"), ("ch", "ч"),
            ("zh", "ж"), ("ts", "ц"), ("yu", "ю"), ("ya", "я"),
            ("yo", "ё"), ("kh", "х"), ("ph", "ф"), ("th", "т"),
            ("coffee", "кофе"), ("cafe", "кафе"), ("daily", "дейли"),
            ("beauty", "бьюти"), ("bar", "бар"), ("bakery", "бейкери"),
            ("mute", "мьют"), ("sip", "сип"), ("finch", "финч"),
            ("sartoria", "сартория"), ("lamberti", "ламберти"),
        ]
        res = text.lower()
        for k, v in multi:
            res = res.replace(k, v)
        single = {
            "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф",
            "g": "г", "h": "х", "i": "и", "j": "дж", "k": "к", "l": "л",
            "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р",
            "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс",
            "y": "и", "z": "з",
        }
        return "".join(single.get(ch, ch) for ch in res)

    @classmethod
    def _expected_okveds(cls, categories: List[str]) -> List[str]:
        cat_text = " ".join(categories).lower()
        matched: List[str] = []
        for keywords, okveds in cls.CATEGORY_OKVED_MAP:
            if any(k in cat_text for k in keywords):
                for okv in okveds:
                    if okv not in matched:
                        matched.append(okv)
        if not matched:
            matched = ["56.10", "56.10.1", "56.30", "47.11", "47.19", "96.02"]
        return matched

    @staticmethod
    def _parse_street_and_house(address: str) -> Tuple[Optional[str], Optional[str]]:
        from .legal_entity import extract_street_and_house
        _, cleaned_street, _, house_num = extract_street_and_house(address)
        return cleaned_street, house_num


    def _suggest_party(
        self,
        query: str,
        count: int = 10,
        locations: Optional[List[Dict[str, str]]] = None,
        okveds: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {
            "query": query,
            "count": count,
            "type": "LEGAL",
            "status": ["ACTIVE"],
        }
        if locations:
            payload["locations"] = locations
        if okveds:
            payload["okved"] = okveds
        try:
            resp = self.session.post(
                self.SUGGEST_URL,
                headers=self.headers,
                data=orjson.dumps(payload),
                timeout=8,
            )
            if resp.status_code == 200:
                data = orjson.loads(resp.content)
                return data.get("suggestions") or []
        except Exception as e:
            logger.debug(f"[DaData] Suggest error for '{query}': {e}")
        return []


    def resolve_candidates_for_card(
        self, title: str, categories: List[str], address: str
    ) -> List[Tuple[str, str]]:
        candidates: List[Tuple[str, str]] = []
        seen_inns = set()

        def add_candidate(inn_val: Optional[str], method: str) -> None:
            if inn_val and inn_val not in seen_inns:
                seen_inns.add(inn_val)
                candidates.append((inn_val, method))

        title_clean = title.strip()
        title_low = title_clean.lower()

        for brand_key, inns in self.KNOWN_BRAND_INNS.items():
            if brand_key in title_low:
                for inn_val in inns:
                    add_candidate(inn_val, "dadata_brand_dict")

        expected_okveds = self._expected_okveds(categories)
        okved_prefixes = tuple({okv.split(".")[0] for okv in expected_okveds})
        street_name, house_num = self._parse_street_and_house(address)
        from .cities import city_in_text
        city = city_in_text(address)
        locations = [{'kladr_id': region} for region in city.registry_regions] if city else None

        search_queries = [re.sub(r"[-«»\"']", " ", title_clean).strip()]
        if re.search(r"[a-zA-Z]", title_clean):
            ru_title = self._transliterate_en_to_ru(title_clean)
            ru_words = [
                w for w in re.split(r"\s+", ru_title)
                if len(w) >= 3 and w not in ("кофе", "кафе", "дейли", "бьюти", "бар", "and")
            ]
            if ru_words:
                search_queries.append(" ".join(ru_words[:2]))

        scored_suggestions: List[Tuple[int, str, str]] = []
        for q in search_queries:
            if not q or len(q) < 2:
                continue
            suggestions = self._suggest_party(
                q,
                count=10,
                locations=locations,
            )
            if not suggestions:
                suggestions = self._suggest_party(q, count=8)

            for sug in suggestions:
                s_data = sug.get("data") or {}
                inn_val = s_data.get("inn")
                if not inn_val:
                    continue
                okv = str(s_data.get("okved") or "")
                addr_obj = s_data.get("address") or {}
                addr_val = str(addr_obj.get("value") or "").lower()
                addr_data = addr_obj.get("data") or {}
                val_name = str(sug.get("value") or "").lower()

                score = 0
                if okv in expected_okveds:
                    score += 65
                elif okv.startswith(okved_prefixes):
                    score += 45
                else:
                    score -= 20

                if city and city_in_text(addr_val) == city:
                    score += 20

                from .legal_entity import exact_registry_house, streets_match
                if street_name and streets_match(street_name, addr_val, addr_data):
                    score += 55
                    if house_num and re.search(rf"\bд\.?\s*{re.escape(house_num)}\b", addr_val):
                        score += 45

                q_first = q.lower().split()[0]
                if f'"{q_first}' in val_name or f"«{q_first}" in val_name or q.lower() in val_name:
                    score += 30

                if score >= 35:
                    normalized_name=re.sub(r'[^а-яa-z0-9]','',val_name.replace('ё','е'))
                    title_words=[w for w in re.findall(r'[а-яa-z0-9]+',q.lower().replace('ё','е')) if len(w)>=3]
                    name_match=bool(title_words) and all(w in normalized_name for w in title_words)
                    address_match=bool((not city or city_in_text(addr_val) == city) and
                        street_name and house_num and streets_match(street_name, addr_val, addr_data) and
                        exact_registry_house(address,addr_data))
                    method='dadata_name_address' if name_match and address_match else 'dadata_brand_candidate'
                    scored_suggestions.append((score+(100 if method=='dadata_name_address' else 0), str(inn_val), method))

        scored_suggestions.sort(key=lambda x: x[0], reverse=True)
        for _, inn_val, method in scored_suggestions[:3]:
            add_candidate(inn_val, method)

        if street_name and len(candidates) < 4:
            cache_key = f"{city.key if city else 'unknown'}|{street_name.lower()}|{'_'.join(expected_okveds[:6])}"
            if cache_key not in self._street_cache:
                self._street_cache[cache_key] = self._suggest_party(
                    street_name,
                    count=20,
                    locations=locations,
                    okveds=expected_okveds[:8],
                )
            street_sugs = self._street_cache[cache_key]
            addr_matches: List[Tuple[int, str]] = []
            from .legal_entity import streets_match
            for sug in street_sugs:
                s_data = sug.get("data") or {}
                inn_val = s_data.get("inn")
                if not inn_val:
                    continue
                addr_obj = s_data.get("address") or {}
                addr_val = str(addr_obj.get("value") or "").lower()
                addr_data = addr_obj.get("data") or {}
                if not streets_match(street_name, addr_val, addr_data):
                    continue
                addr_score = 45
                if house_num:
                    if re.search(rf"\bд\.?\s*{re.escape(house_num)}(?:\b|[/\-])", addr_val):
                        addr_score += 65
                addr_matches.append((addr_score, str(inn_val)))

            addr_matches.sort(key=lambda x: x[0], reverse=True)
            for sc, inn_val in addr_matches[:3]:
                if sc >= 45:
                    add_candidate(inn_val, "dadata_address_okved")


        return candidates[:4]



class FnsBrowserRevenueCollector:
    BROWSER_CANDIDATES = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]

    def __init__(
        self,
        dadata_api_key: Optional[str] = None,
        proxy: Optional[str] = None,
    ):
        self.proxy = proxy or _DirectInterfaceBridge.get_proxy_url()
        self.dadata = DaDataInnResolver(api_key=resolve_dadata_key(dadata_api_key), proxy=self.proxy)
        self._inn_cache: Dict[str, Optional[FnsCompanyProfile]] = {}
        self._inn_cache_expiry: Dict[str, float] = {}
        self._cache_clock=time.monotonic
        from .legal_entity import WebsiteLegalResolver
        self.website_resolver=WebsiteLegalResolver(curl_requests.Session(impersonate='chrome120',
            proxies={'http':self.proxy,'https':self.proxy} if self.proxy else None))
        self.match_diagnostics={}

    @classmethod
    def _find_browser_executable(cls) -> Optional[str]:
        for path in cls.BROWSER_CANDIDATES:
            if os.path.exists(path):
                return path
        return shutil.which("chrome") or shutil.which("msedge")

    @staticmethod
    def _get_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return int(s.getsockname()[1])

    @staticmethod
    def _select_bfo_record(records):
        """The public API does not promise newest-first report ordering."""
        if not isinstance(records,list):return None
        def year(record):
            value=str(record.get('period','')) if isinstance(record,dict) else ''
            return int(value) if value.isdigit() and 1900<=int(value)<datetime.now().year else 0
        usable=[r for r in records if year(r) and type(r.get('id')) is int and r['id']>0]
        return max(usable,key=lambda r:(year(r),str(r.get('actualCorrectionDate') or ''),
            r.get('actualCorrectionNumber') if type(r.get('actualCorrectionNumber')) is int else 0),default=None)

    @staticmethod
    def _build_browser_fetch_script(inns: List[str]) -> str:
        inns_json = json.dumps(list(dict.fromkeys(inns)))
        return f"""
        (async () => {{
            const inns = {inns_json};
            const out = {{}};
            const fetchOne = async (inn) => {{
                try {{
                    const r = await fetch('https://bo.nalog.gov.ru/advanced-search/organizations/search?query=' + encodeURIComponent(inn) + '&page=0', {{
                        headers: {{ 'Accept': 'application/json' }}
                    }});
                    if (!r.ok) return;
                    const d = await r.json();
                    const item = (d.content || []).find(x => String(x.inn || '').replace(/<[^>]*>/g,'') === inn);
                    if (!item) return;
                    let details = null, details_year = null;
                    if (item.id && item.bfo) {{
                        try {{
                            const rb = await fetch('https://bo.nalog.gov.ru/nbo/organizations/' + item.id + '/bfo', {{ headers: {{ 'Accept': 'application/json' }} }});
                            if (rb.ok) {{
                                const bfoList = await rb.json();
                                const latest = Array.isArray(bfoList) ? bfoList.filter(x => x && Number.isInteger(x.id) && x.id>0 && /^\d{{4}}$/.test(String(x.period)) && Number(x.period)>=1900 && Number(x.period)<new Date().getFullYear()).sort((a,b) => Number(b.period)-Number(a.period) || String(b.actualCorrectionDate || '').localeCompare(String(a.actualCorrectionDate || '')) || Number(b.actualCorrectionNumber || 0)-Number(a.actualCorrectionNumber || 0))[0] : null;
                                if (latest) {{
                                    const rd = await fetch('https://bo.nalog.gov.ru/nbo/bfo/' + latest.id + '/details', {{ headers: {{ 'Accept': 'application/json' }} }});
                                    if (rd.ok) {{
                                        const detList = await rd.json();
                                        if (detList && detList[0] && detList[0].financialResult) {{ details = detList[0].financialResult; details_year=Number(latest.period); }}
                                    }}
                                }}
                            }}
                        }} catch (e) {{}}
                    }}
                    out[inn] = {{ item, details, details_year }};
                }} catch (err) {{}}
            }};
            for (let i = 0; i < inns.length; i += 6) {{
                await Promise.all(inns.slice(i, i + 6).map(fetchOne));
            }}
            return JSON.stringify(out);
        }})()
        """


    async def _fetch_inns_via_browser_cdp(self, inns: List[str]) -> Dict[str, Dict[str, Any]]:
        if not inns:
            return {}
        browser_exe = self._find_browser_executable()
        if not browser_exe:
            return await self._fetch_inns_fallback_http(inns)

        port = self._get_free_port()
        tmp_dir = tempfile.mkdtemp(prefix="fns_chrome_")
        cmd = [
            browser_exe,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--user-data-dir={tmp_dir}",
            f"--remote-debugging-port={port}",
        ]
        if self.proxy:
            cmd.append(f"--proxy-server={self.proxy}")
        cmd.append("https://bo.nalog.gov.ru/")

        proc: Optional[subprocess.Popen] = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ws_url: Optional[str] = None
            deadline = time.perf_counter() + 8.0
            while time.perf_counter() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1.0) as resp:
                        tabs = json.loads(resp.read().decode("utf-8"))
                        page_tabs = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
                        if page_tabs:
                            ws_url = page_tabs[0]["webSocketDebuggerUrl"]
                            break
                except Exception:
                    await asyncio.sleep(0.2)

            if not ws_url:
                return await self._fetch_inns_fallback_http(inns)

            async with websockets.connect(ws_url, max_size=16 * 1024 * 1024) as ws:
                js_code = self._build_browser_fetch_script(inns)
                cdp_req = {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": js_code, "awaitPromise": True, "returnByValue": True},
                }
                await ws.send(json.dumps(cdp_req))
                while True:
                    raw_msg = await asyncio.wait_for(ws.recv(), timeout=20.0)
                    msg = json.loads(raw_msg)
                    if msg.get("id") == 1:
                        val = ((msg.get("result") or {}).get("result") or {}).get("value")
                        if val:
                            return orjson.loads(val.encode("utf-8"))
                        break
        except Exception as e:
            logger.warning(f"[FnsBrowser] CDP error: {e}, using fallback")
            return await self._fetch_inns_fallback_http(inns)
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            shutil.rmtree(tmp_dir, ignore_errors=True)
        return {}

    async def _fetch_inns_fallback_http(self, inns: List[str]) -> Dict[str, Dict[str, Any]]:
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        session = curl_requests.Session(impersonate="chrome120", proxies=proxies, verify=False)
        headers = {"Accept": "application/json", "Referer": "https://bo.nalog.gov.ru/"}
        out: Dict[str, Dict[str, Any]] = {}

        def _fetch_sync(inn: str) -> None:
            try:
                r = session.get(
                    f"https://bo.nalog.gov.ru/advanced-search/organizations/search?query={inn}&page=0",
                    headers=headers,
                    timeout=10,
                )
                if r.status_code != 200:
                    return
                d = orjson.loads(r.content)
                content = d.get("content") or []
                if not content:
                    return
                item = next((x for x in content if re.sub(r'\D','',str(x.get('inn','')))==str(inn)),None)
                if item is None:return
                details = None;details_year=None
                if item.get("id") and item.get("bfo"):
                    rb = session.get(
                        f"https://bo.nalog.gov.ru/nbo/organizations/{item['id']}/bfo",
                        headers=headers,
                        timeout=8,
                    )
                    if rb.status_code == 200:
                        bfo_list = orjson.loads(rb.content)
                        latest=self._select_bfo_record(bfo_list)
                        if latest:
                            rd = session.get(
                                f"https://bo.nalog.gov.ru/nbo/bfo/{latest['id']}/details",
                                headers=headers,
                                timeout=8,
                            )
                            if rd.status_code == 200:
                                det_list = orjson.loads(rd.content)
                                if det_list and det_list[0].get("financialResult"):
                                    details = det_list[0]["financialResult"]
                                    details_year=int(latest['period'])
                out[inn] = {"item": item, "details": details, "details_year":details_year}
            except Exception:
                pass

        semaphore=asyncio.Semaphore(4)
        async def fetch(inn):
            async with semaphore:
                await asyncio.get_running_loop().run_in_executor(None,_fetch_sync,inn)
        await asyncio.gather(*(fetch(inn) for inn in dict.fromkeys(inns)))
        return out


    @staticmethod
    def _parse_fns_payload(inn: str, payload: Dict[str, Any], match_method: str) -> Optional[FnsCompanyProfile]:
        if not isinstance(payload,dict):return None
        item = payload.get("item")
        if not isinstance(item,dict) or not item:
            return None
        details = payload.get("details") or {}
        bfo = item.get("bfo") or {}
        if not isinstance(details,dict) or not isinstance(bfo,dict):return None

        raw_name = str(item.get("shortName") or item.get("fullName") or "")
        clean_name = re.sub(r"<[^>]+>", "", raw_name).strip()
        clean_inn = re.sub(r"<[^>]+>", "", str(item.get("inn") or inn)).strip()
        from .legal_entity import valid_inn
        if clean_inn!=str(inn) or not valid_inn(clean_inn):return None

        search_year=bfo.get('period')
        period_str=payload.get('details_year') if details and payload.get('details_year') is not None else search_year
        reporting_year=int(period_str) if str(period_str).isdigit() else None
        if reporting_year is None or not 1900<=reporting_year<datetime.now().year:return None
        rev_ths = details.get("current2110")
        if rev_ths is None:
            rev_ths = bfo.get("gainSum") if str(reporting_year)==str(search_year) else None

        prev_rev_ths = details.get("previous2110")
        profit_ths = details.get("current2400")
        try:
            if any(type(v) is bool for v in (rev_ths,prev_rev_ths,profit_ths)):return None
            revenue_rub = float(rev_ths) * 1000.0 if rev_ths is not None else None
            prev_revenue_rub = float(prev_rev_ths) * 1000.0 if prev_rev_ths is not None else None
            net_profit_rub = float(profit_ths) * 1000.0 if profit_ths is not None else None
            org_id=int(item['id']) if item.get('id') is not None else None
        except (ValueError,TypeError,OverflowError):return None
        if any(value is not None and not math.isfinite(value) for value in (revenue_rub,prev_revenue_rub,net_profit_rub)):
            return None
        if any(v is not None and v<0 for v in (revenue_rub,prev_revenue_rub)):return None

        addr_parts = [
            str(item.get(k))
            for k in ("region", "city", "street", "house")
            if item.get(k)
        ]
        address_str = ", ".join(addr_parts) if addr_parts else None

        return FnsCompanyProfile(
            inn=clean_inn,
            legal_name=clean_name,
            ogrn=str(item.get("ogrn")) if item.get("ogrn") else None,
            okved=str(item.get("okved2")) if item.get("okved2") else None,
            address=address_str,
            revenue_rub=revenue_rub,
            prev_revenue_rub=prev_revenue_rub,
            net_profit_rub=net_profit_rub,
            reporting_year=reporting_year,
            fns_org_id=org_id,
            match_method=match_method,
            source_url='https://bo.nalog.gov.ru/advanced-search/organizations/search?query='+clean_inn+'&page=0',
        )

    async def enrich_cards(self, cards: List[Any]) -> int:
        if not cards:
            return 0

        t0 = time.perf_counter()
        loop = asyncio.get_event_loop()

        async def _resolve_one(card: Any) -> Tuple[str, List[Tuple[str, str]]]:
            from .legal_entity import valid_inn
            from .cities import city_for_coordinates, city_in_text
            city = city_for_coordinates(getattr(card,'lat',None), getattr(card,'lng',None))
            named_city = city_in_text(card.address)
            resolution_address = (city.name+', '+card.address) if city and named_city is None else card.address
            # Clear reused cards before any operation which can fail.
            for field in ('fns_profile','fns_inn','fns_legal_name','fns_okved','fns_revenue_rub','fns_net_profit_rub','fns_reporting_year'):
                setattr(card,field,None)
            card.fns_match_evidence=[];card.fns_match_status='unverified'
            if getattr(card,'is_chain',False):
                self.match_diagnostics[card.org_id]=dict(title=card.title,address=card.address,
                    status='unverified',candidates=[],accepted=[],website_evidence=[],errors=[],
                    scope='chain_excluded',finance_status='not_applicable_chain',
                    note='Chain brand accounts aggregate multiple outlets; outlet-level FNS not applicable',
                    resolution_address=resolution_address)
                card.fns_diagnostics=copy.deepcopy(self.match_diagnostics[card.org_id])
                return card.org_id, []
            card.fns_diagnostics=dict(status='resolving',scope='company',finance_status='not_requested')
            errors=[]
            try:
                website=await self.website_resolver.resolve(card.title,getattr(card,'websites',[]))
            except Exception as exc:
                website=[]
                errors.append(dict(stage='business_website',error_type=type(exc).__name__))
                logger.warning('Business-linked website unavailable (%s)',type(exc).__name__)
            website_inns={e['inn'] for e in website if valid_inn(e['inn'])}
            try:
                cands=[] if len(website_inns)==1 else await loop.run_in_executor(
                    None,self.dadata.resolve_candidates_for_card,card.title,card.categories,resolution_address)
                if city and named_city and city != named_city:
                    cands=[]
                    website=[]; website_inns=set()
                    errors.append(dict(stage='legal_entity_resolution',error_type='CityAddressConflict'))
            except Exception as exc:
                cands=[]
                errors.append(dict(stage='legal_entity_resolution',error_type=type(exc).__name__))
                logger.warning('Legal entity resolution unavailable (%s)',type(exc).__name__)
            exact={inn for inn,method in cands if method=='dadata_name_address' and valid_inn(inn)}
            if len(website_inns)==1:
                accepted=[(next(iter(website_inns)),'business_linked_website_company')]
                card.fns_match_evidence=website;card.fns_match_status='matched_company'
            elif not website_inns and len(exact)==1:
                accepted=[(next(iter(exact)),'dadata_name_address')]
                card.fns_match_evidence=[dict(method='dadata_name_address',title=card.title,address=card.address,
                    source_url='https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party',scope='company')]
                card.fns_match_status='matched_company'
            else:
                accepted=[];card.fns_match_status='ambiguous' if website_inns or exact else 'unverified'
                card.fns_match_evidence=[]
            self.match_diagnostics[card.org_id]=dict(title=card.title,address=card.address,
                status=card.fns_match_status,candidates=cands,accepted=accepted,website_evidence=website,
                errors=errors,scope='company',finance_status='not_requested' if accepted else 'not_matched',
                note='Address/OKVED-only and brand-dictionary candidates are not financial matches')
            card.fns_diagnostics=copy.deepcopy(self.match_diagnostics[card.org_id])
            card.fns_diagnostics['resolution_address']=resolution_address
            self.match_diagnostics[card.org_id]['resolution_address']=resolution_address
            return card.org_id, accepted

        resolved_pairs = await asyncio.gather(*(_resolve_one(c) for c in cards))
        card_candidates: Dict[str, List[Tuple[str, str]]] = dict(resolved_pairs)

        all_inns_to_query: List[str] = []
        now=self._cache_clock()
        for inn in list(self._inn_cache):
            if self._inn_cache_expiry.get(inn,0)<=now:
                self._inn_cache.pop(inn,None);self._inn_cache_expiry.pop(inn,None)
        for cands in card_candidates.values():
            for inn_val, _ in cands:
                if inn_val not in self._inn_cache and inn_val not in all_inns_to_query:
                    all_inns_to_query.append(inn_val)

        if all_inns_to_query:
            raw_fns_map = (await self._fetch_inns_fallback_http(all_inns_to_query)) or {}
            missing=[inn for inn in all_inns_to_query if inn not in raw_fns_map]
            if missing:raw_fns_map.update((await self._fetch_inns_via_browser_cdp(missing)) or {})
            for inn_val in all_inns_to_query:
                payload = raw_fns_map.get(inn_val)
                if payload:
                    self._inn_cache[inn_val] = self._parse_fns_payload(inn_val, payload, "dadata")
                else:
                    self._inn_cache[inn_val] = None
                self._inn_cache_expiry[inn_val]=self._cache_clock()+(86400 if self._inn_cache[inn_val] is not None else 60)
            while len(self._inn_cache)>1024:
                oldest=min(self._inn_cache_expiry,key=self._inn_cache_expiry.get)
                self._inn_cache.pop(oldest,None);self._inn_cache_expiry.pop(oldest,None)

        enriched_count = 0
        for card in cards:
            cands = card_candidates.get(card.org_id) or []
            best_profile: Optional[FnsCompanyProfile] = None
            for inn_val, method in cands:
                prof = self._inn_cache.get(inn_val)
                if prof is not None:
                    prof_copy = FnsCompanyProfile(
                        inn=prof.inn,
                        legal_name=prof.legal_name,
                        ogrn=prof.ogrn,
                        okved=prof.okved,
                        address=prof.address,
                        revenue_rub=prof.revenue_rub,
                        prev_revenue_rub=prof.prev_revenue_rub,
                        net_profit_rub=prof.net_profit_rub,
                        reporting_year=prof.reporting_year,
                        fns_org_id=prof.fns_org_id,
                        match_method=method,
                        source_url=prof.source_url,
                        financial_unit=prof.financial_unit,
                    )
                    if prof_copy.revenue_rub and prof_copy.revenue_rub > 0:
                        best_profile = prof_copy
                        break
                    if best_profile is None:
                        best_profile = prof_copy

            if best_profile is not None:
                card.fns_profile = best_profile
                card.fns_inn = best_profile.inn
                card.fns_legal_name = best_profile.legal_name
                card.fns_okved = best_profile.okved
                card.fns_revenue_rub = best_profile.revenue_rub
                card.fns_net_profit_rub = best_profile.net_profit_rub
                card.fns_reporting_year = best_profile.reporting_year
                enriched_count += 1
            diagnostic=self.match_diagnostics.get(card.org_id)
            if diagnostic is not None:
                if diagnostic.get('scope')!='chain_excluded':
                    diagnostic['finance_status']='available' if best_profile is not None else 'unavailable' if cands else 'not_matched'
                card.fns_diagnostics=copy.deepcopy(diagnostic)

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        logger.info(
            f"[FnsBrowser] Enriched {enriched_count}/{len(cards)} business cards with FNS data "
            f"(unique INNs queried: {len(all_inns_to_query)}) in {elapsed_ms:.1f}ms"
        )
        return enriched_count

