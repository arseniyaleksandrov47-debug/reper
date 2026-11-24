"""Dated, partial station observations from the operator's annual report."""
import asyncio
import hashlib
import io
import json
import re
import time
from datetime import date
from urllib.parse import urljoin, urlsplit
from .cities import SAINT_PETERSBURG
from .metro_data import MetroFlowDataset
from .source_cache import SourceCache

INDEX_URL = 'https://metro.spb.ru/otchetmetro.html'


def parse_annual_report(body, source_url, *, today=None):
    from pypdf import PdfReader
    today = today or date.today()
    if not body.startswith(b'%PDF') or len(body) > 60000000:
        raise ValueError('Invalid metro annual report')
    if urlsplit(source_url).hostname != 'metro.spb.ru':
        raise ValueError('Unexpected metro report publisher')
    reader = PdfReader(io.BytesIO(body))
    observations = []; year = None; page_number = None
    for index, page in enumerate(reader.pages[:12]):
        text = page.extract_text() or ''
        match = re.search(r'ГОДОВОЙ\s+ОТЧЕТ\s+(20\d{2})', text, re.I)
        if match:
            if year is not None and year != int(match[1]):
                raise ValueError('Conflicting report years')
            year = int(match[1])
        heading = re.search(r'Самые\s+пассажиронапряженные\s+станции', text, re.I)
        if not heading:
            continue
        table = text[heading.start():]
        if not re.search(r'Среднесуточный\s+пассажиропоток,\s*чел', table):
            raise ValueError('Unknown station-flow table units')
        rows = re.findall(r'«([^»\n]+)»\s+(\d[\d \u00a0]*)(?=\n|$)', table)
        observations.extend((name.strip(), int(re.sub(r'\s', '', number))) for name, number in rows)
        page_number = index + 1
        break
    if year is None or date(year, 12, 31) > today or not 1 <= len(observations) <= 100:
        raise ValueError('No completed-year station observations')
    if len({MetroFlowDataset.normalize(name) for name, _ in observations}) != len(observations):
        raise ValueError('Duplicate station observations')
    data = dict(source=source_url+f'#page={page_number}', as_of=date(year,12,31).isoformat(),
        bounds=list(SAINT_PETERSBURG.bounds), measure='passengers_per_day_unspecified', stations=dict(observations))
    return dict(dataset=data, diagnostics=dict(status='partial', source=source_url, pdf_page=page_number,
        observation_year=year, stations=len(observations), coverage_complete=False,
        source_sha256=hashlib.sha256(body).hexdigest(),
        measure='Среднесуточный пассажиропоток, чел.; разделение входа/выхода в таблице не указано',
        note='Only published stations are used; this is not footfall at the premises entrance'))


class SpbMetroFlowSource:
    def __init__(self, *, cache_path='.local-cache/public_sources.sqlite3', session=None, ttl=21600):
        self.cache=SourceCache(cache_path); self.session=session; self.ttl=ttl
        self.lock=asyncio.Lock(); self.next_check=0; self.dataset=None
        self.diagnostics=dict(status='not_loaded',source=INDEX_URL)

    def _fetch(self):
        if self.session is None:
            from curl_cffi import requests
            self.session=requests.Session(impersonate='chrome120',verify=False)
        response=self.session.get(INDEX_URL,timeout=(5,12)); response.raise_for_status()
        candidates=[]
        for link in re.findall(r'href=["\x27]([^"\x27]+\.pdf)["\x27]',response.text,re.I):
            match=re.search(r'(20\d{2})',link)
            url=urljoin(INDEX_URL,link)
            if match and int(match[1]) < date.today().year and urlsplit(url).hostname=='metro.spb.ru':
                candidates.append((int(match[1]),url))
        if not candidates:
            raise ValueError('Operator annual report link unavailable')
        year,url=max(candidates)
        response=self.session.get(url,timeout=(5,30)); response.raise_for_status()
        parsed=parse_annual_report(response.content,url)
        if parsed['diagnostics']['observation_year']!=year:
            raise ValueError('Report year does not match the publication link')
        return parsed

    async def get_dataset(self):
        async with self.lock:
            if time.monotonic()<self.next_check:
                return self.dataset
            try:
                key='spb_operator_annual_metro_v1'
                cached=self.cache.get(key)
                payload=json.loads(cached[1]) if cached else await asyncio.to_thread(self._fetch)
                dataset=MetroFlowDataset(payload['dataset'])
                if (date.today()-dataset.as_of).days>dataset.max_age_days:
                    raise ValueError('Metro annual observations expired')
                self.dataset=dataset; self.diagnostics=dict(payload['diagnostics'],cache_hit=bool(cached))
                if not cached:
                    self.cache.put(key,json.dumps(payload,ensure_ascii=False).encode(),self.ttl)
                self.next_check=time.monotonic()+self.ttl
            except Exception as exc:
                self.dataset=None; self.next_check=time.monotonic()+60
                self.diagnostics=dict(status='unavailable',source=INDEX_URL,error_type=type(exc).__name__)
            return self.dataset
