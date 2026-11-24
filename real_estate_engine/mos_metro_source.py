"""Automatic dated station-flow acquisition from the public data.mos.ru portal.

Uses the same public metadata/catalog endpoints as the site's table view; the
separate apidata API and its account key are not required. No hidden API keys.
"""
import asyncio
import calendar
import json
import math
import time
import hashlib
from collections import defaultdict
from datetime import date,datetime
from .metro_data import MetroFlowDataset
from .source_cache import SourceCache


METADATA_URL='https://data.mos.ru/api/v2/odata/datasets/62743'
CATALOG_URL='https://data.mos.ru/api/v2/odata/catalog/get'


def _quarter(value):
    pieces=str(value).strip().split();text=pieces[0] if pieces else ''
    return {'I':1,'II':2,'III':3,'IV':4,'1':1,'2':2,'3':3,'4':4}.get(text)


def build_metro_dataset(metadata,rows,*,today=None):
    today=today or date.today()
    if not isinstance(metadata,dict) or metadata.get('generalInfo',{}).get('publicationStatusTagId')!='published':
        raise ValueError('Metro dataset is not publicly published')
    publication=metadata.get('actualData',{})
    published=datetime.strptime(publication['releaseOpenDate'],'%d.%m.%Y %H:%M:%S').date()
    if published>today:raise ValueError('Metro publication is in the future')
    valid=[];ids=set()
    for row in rows:
        if not isinstance(row,dict) or row.get('is_deleted') not in (None,0,False):continue
        identifier=row.get('global_id')
        if identifier is None or identifier in ids:raise ValueError('Missing or duplicate portal row ID')
        ids.add(identifier)
        year=row.get('Year');quarter=_quarter(row.get('Quarter'))
        if type(year) is not int or not 2000<=year<=today.year or quarter is None:raise ValueError('Invalid observation period')
        end=date(year,quarter*3,calendar.monthrange(year,quarter*3)[1])
        if end>today or end>published:continue
        incoming=row.get('IncomingPassengers');name=row.get('NameOfStation');line=row.get('Line')
        if (type(incoming) not in (int,float) or not math.isfinite(incoming) or incoming<0 or
                not isinstance(name,str) or not name.strip() or not isinstance(line,str) or not line.strip()):
            raise ValueError('Invalid station observation')
        valid.append((end,year,quarter,name,line,float(incoming),identifier))
    if not valid:raise ValueError('No completed-quarter observations')
    latest=max(v[0] for v in valid)
    year=latest.year;quarter=(latest.month+2)//3
    start=date(year,(quarter-1)*3+1,1);days=(latest-start).days+1
    latest_rows=[v for v in valid if v[0]==latest]
    groups=defaultdict(list)
    for value in latest_rows:groups[MetroFlowDataset.normalize(value[3])].append(value)
    stations={};ambiguous=[];variants={}
    for name,values in groups.items():
        # A station name shared by several lines is not silently summed: the
        # map card usually has no line identity and may refer to one entrance.
        if len(values)!=1:
            ambiguous.append(name)
            if len({v[4] for v in values})==len(values):
                variants[values[0][3]]=[dict(line=v[4],entries_per_day=v[5]/days) for v in values]
            continue
        stations[values[0][3]]=values[0][5]/days
    if not stations and not variants:raise ValueError('No usable station observations in latest quarter')
    source=f'https://data.mos.ru/opendata/62743; version={publication["versionNum"]}; release={publication["releaseNum"]}; period={year}-Q{quarter}'
    data=dict(source=source,as_of=latest.isoformat(),bounds=[55.0,57.0,36.0,39.0],
        measure='entries_per_day',stations=stations)
    if variants:data['station_variants']=variants
    diagnostics=dict(status='available',source='https://data.mos.ru/opendata/62743',
        publication_date=published.isoformat(),observation_period_start=start.isoformat(),observation_period_end=latest.isoformat(),
        days_in_period=days,release=publication['releaseNum'],version=publication['versionNum'],
        source_rows=len(rows),latest_period_rows=len(latest_rows),usable_stations=len(stations),
        ambiguous_station_names=ambiguous,bounded_stations=len(variants),measure='IncomingPassengers / calendar days in quarter',
        note='Quarterly average station entries, not measured footfall at a premises entrance')
    return data,diagnostics


class MosMetroFlowSource:
    def __init__(self,*,cache_path='.local-cache/public_sources.sqlite3',session=None,ttl=21600):
        self.cache=SourceCache(cache_path);self.session=session;self.ttl=ttl;self.lock=asyncio.Lock()
        self.dataset=None;self.next_check=0;self.requests=0
        self.diagnostics=dict(status='not_loaded',source='https://data.mos.ru/opendata/62743')

    def _session(self):
        if self.session is None:
            from curl_cffi import requests
            from .collectors.base import _DirectInterfaceBridge
            proxy=_DirectInterfaceBridge.get_proxy_url()
            self.session=requests.Session(impersonate='chrome120',proxies={'http':proxy,'https':proxy} if proxy else None)
            self.session.headers.update({'Accept':'application/json','Referer':'https://data.mos.ru/opendata/62743'})
        return self.session

    def _fetch(self):
        session=self._session();self.requests+=1
        response=session.get(METADATA_URL,timeout=(5,15));response.raise_for_status();metadata=response.json()
        catalog=metadata.get('version',{}).get('publicationCatalogId')
        count=metadata.get('release',{}).get('cntObjects')
        if type(catalog) is not int or type(count) is not int or not 0<count<=30000:raise ValueError('Invalid public catalog metadata')
        rows=[];ids=set();now=date.today()
        # Query only a complete quarter, with a unique stable sort. The portal's
        # default ordering has ties and produced repeated rows between pages.
        current_index=now.year*4+(now.month-1)//3
        for period in range(current_index-1,current_index-5,-1):
            year,quarter=period//4,period%4+1
            roman=('I','II','III','IV')[quarter-1]
            criteria=f"Year = {year} and Quarter = '{roman} квартал'"
            for offset in range(0,10000,1000):
                self.requests+=1
                response=session.post(CATALOG_URL,json=dict(id=catalog,limit=1000,offset=offset,
                    criteria=criteria,sorting={'global_id':'asc'}),timeout=(5,15))
                response.raise_for_status();page=response.json();batch=page.get('response')
                if page.get('status')!=0 or not isinstance(batch,list) or len(batch)>1000:
                    raise ValueError('Incomplete metro catalog page')
                for row in batch:
                    identifier=row.get('global_id')
                    if identifier is None or identifier in ids:raise ValueError('Metro catalog pagination repeated rows')
                    if row.get('Year')!=year or _quarter(row.get('Quarter'))!=quarter:
                        raise ValueError('Metro portal did not apply the requested observation-period filter')
                    ids.add(identifier)
                rows.extend(batch)
                if len(batch)<1000:break
            else:raise ValueError('Metro quarter pagination exceeds bounded limit')
            if rows:break
        data,diagnostics=build_metro_dataset(metadata,rows)
        # Check the release after paging: rows from different revisions must
        # never be presented as one coherent snapshot.
        self.requests+=1
        response=session.get(METADATA_URL,timeout=(5,15));response.raise_for_status();after=response.json()
        if after.get('actualData')!=metadata.get('actualData'):raise ValueError('Metro release changed during download')
        diagnostics['observation_rows_sha256']=hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        return dict(dataset=data,diagnostics=diagnostics,raw_snapshot=dict(metadata=metadata,rows=rows))

    async def get_dataset(self):
        async with self.lock:
            if time.monotonic()<self.next_check:return self.dataset
            key='mos_metro_portal_v2:62743'
            try:
                cached=self.cache.get(key)
                if cached:
                    payload=json.loads(cached[1]);cache_hit=True
                else:
                    payload=await asyncio.to_thread(self._fetch);cache_hit=False
                dataset=MetroFlowDataset(payload['dataset'])
                if (date.today()-dataset.as_of).days>dataset.max_age_days:raise ValueError('No fresh completed-quarter data')
                self.dataset=dataset;self.diagnostics=dict(payload['diagnostics'],cache_hit=cache_hit)
                if not cache_hit:self.cache.put(key,json.dumps(payload,ensure_ascii=False).encode(),self.ttl)
                self.next_check=time.monotonic()+self.ttl
                return self.dataset
            except Exception as exc:
                self.dataset=None;self.next_check=time.monotonic()+60
                self.diagnostics=dict(status='unavailable',source='https://data.mos.ru/opendata/62743',error_type=type(exc).__name__,
                    validation_error=str(exc)[:160] if isinstance(exc,ValueError) else None,
                    warning='Датированный пассажиропоток не получен; неизвестные станции не получают типовые числа')
                return None
