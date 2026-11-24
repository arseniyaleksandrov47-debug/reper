"""Mapped metro entrances and explicit station relations, independent of POIs.

Several entrances share a station total. No nearest-station guess attaches a
passenger count to an otherwise unbound entrance.
"""
import asyncio
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests
from .cities import get_city
from .metro_data import MetroFlowDataset
from .source_cache import SourceCache
from .storage import haversine_distance

ENDPOINT = 'https://maps.mail.ru/osm/tools/overpass/api/interpreter'
FALLBACK_ENDPOINT = 'https://overpass.private.coffee/api/interpreter'
OVERPASS_ENDPOINTS = (ENDPOINT, FALLBACK_ENDPOINT, 'https://overpass-api.de/api/interpreter')
TIMETABLE = 'https://metro.spb.ru/rejimrabotystancii.html'


class TableRows(HTMLParser):
    def __init__(self):
        super().__init__(); self.rows=[];self.row=None;self.cell=None
    def handle_starttag(self,tag,attrs):
        if tag=='tr':self.row=[]
        elif tag in ('td','th') and self.row is not None:self.cell=[]
        elif tag=='br' and self.cell is not None:self.cell.append(' ')
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ('td','th') and self.cell is not None:
            self.row.append(' '.join(''.join(self.cell).split()));self.cell=None
        elif tag=='tr' and self.row is not None:
            if self.row:self.rows.append(self.row)
            self.row=None


def _strip_entrance_suffix(name):
    return re.sub(r'\s*(?:-|№)\s*\d+\s*$', '', str(name or '').strip()).strip()


def _known_station_index(names):
    index = {}
    for raw in names:
        text = str(raw or '').strip()
        if not text:
            continue
        key = MetroFlowDataset.normalize(text)
        if key and key not in index:
            index[key] = text
    return index


def _is_numbered_variant(norm_variant, norm_base):
    if not norm_base or not norm_variant.startswith(norm_base) or len(norm_variant) <= len(norm_base):
        return False
    suffix = norm_variant[len(norm_base):]
    return suffix[0] in (' ', '-') and suffix.lstrip(' -').isdigit()


def _match_station_from_catalogue(raw_name, known_by_norm):
    raw = str(raw_name or '').strip()
    if not raw or not known_by_norm:
        return []
    norm = MetroFlowDataset.normalize(raw)
    if norm in known_by_norm:
        return [known_by_norm[norm]]
    stripped = _strip_entrance_suffix(raw)
    snorm = MetroFlowDataset.normalize(stripped)
    if snorm and snorm in known_by_norm:
        return [known_by_norm[snorm]]
    if snorm:
        family = sorted({canon for key, canon in known_by_norm.items() if _is_numbered_variant(key, snorm)})
        if len(family) >= 2:
            return family
    return []


def _resolve_group_name(entrance, known_by_norm):
    names = list(entrance.get('station_names') or [])
    own_stripped = _strip_entrance_suffix(entrance.get('name', ''))
    own_norm = MetroFlowDataset.normalize(own_stripped)
    if len(names) == 1:
        binding = entrance.get('station_binding') or (
            'stop_area_relation' if entrance.get('station_relations') else 'entrance_name_and_station_catalogue'
        )
        return names[0], binding
    if len(names) > 1 and own_norm:
        exact = [n for n in names if MetroFlowDataset.normalize(n) == own_norm]
        if len(exact) == 1:
            return exact[0], entrance.get('station_binding') or 'stop_area_relation'
        if all(_is_numbered_variant(MetroFlowDataset.normalize(n), own_norm) for n in names):
            return own_stripped, entrance.get('station_binding') or 'stop_area_relation'
    if not names and own_stripped:
        matched = _match_station_from_catalogue(entrance.get('name', ''), known_by_norm)
        if len(matched) == 1:
            return matched[0], 'entrance_name_and_station_catalogue'
        if len(matched) >= 2:
            return own_stripped, 'entrance_name_and_station_catalogue'
    return 'Вход метро ' + entrance['node'], 'unbound'


def parse_entrances(payload, city, *, source=ENDPOINT, fetched_at=None, timetable_rows=(), timetable_fetched_at=None):
    city=get_city(city)
    if not isinstance(payload,dict) or payload.get('remark') or not isinstance(payload.get('elements'),list):
        raise ValueError('Incomplete or malformed OSM metro response')
    nodes={};relations=[];station_names=set()
    for item in payload['elements']:
        if item.get('type')=='relation':relations.append(item);continue
        tags=item.get('tags') or {}
        if tags.get('railway')=='station' and tags.get('station')=='subway' and isinstance(tags.get('name'),str):
            station_names.add(tags['name'])
        if item.get('type')!='node' or tags.get('railway')!='subway_entrance':continue
        lat=item.get('lat');lng=item.get('lon');identifier=item.get('id')
        if type(identifier) is not int or not city.contains(lat,lng):raise ValueError('Invalid metro entrance identity or position')
        tags=item.get('tags') or {}
        row=dict(node='osm:'+str(identifier),lat=lat,lng=lng,station_names=[],
            name=tags.get('name',''),ref=tags.get('ref',''),opening_hours=tags.get('opening_hours'),
            access=tags.get('access'),foot=tags.get('foot'),
            active=not (tags.get('access') in ('no','private') or tags.get('foot') in ('no','private')
                or tags.get('disused')=='yes' or tags.get('construction') not in (None,'no')),
            station_relations=[],station_binding='unbound',official_timetable_rows=[],source=source)
        if identifier in nodes and nodes[identifier]!=row:raise ValueError('Conflicting metro entrance copies')
        nodes[identifier]=row
    for relation in relations:
        tags=relation.get('tags') or {};name=tags.get('name')
        if tags.get('public_transport')!='stop_area' or not isinstance(name,str) or not name.strip():continue
        for member in relation.get('members',[]):
            if member.get('type')!='node' or member.get('ref') not in nodes:continue
            row=nodes[member['ref']]
            if name not in row['station_names']:row['station_names'].append(name)
            if relation['id'] not in row['station_relations']:row['station_relations'].append(relation['id'])
            row['station_binding']='stop_area_relation'
    known_names=station_names|{n for v in nodes.values() for n in v['station_names']}
    known_by_norm=_known_station_index(known_names)
    for row in nodes.values():
        # The entrance's own station name is source evidence when it matches
        # a named station in this same catalogue; proximity is never a binding.
        if not row['station_names']:
            matched=_match_station_from_catalogue(row['name'],known_by_norm)
            if matched:
                row['station_names']=matched
                row['station_binding']='entrance_name_and_station_catalogue'
        names={MetroFlowDataset.normalize(n) for n in row['station_names']}
        own_norm=MetroFlowDataset.normalize(_strip_entrance_suffix(row['name']))
        if len(names)>1 and own_norm:
            if own_norm in names:
                names={own_norm}
            elif all(_is_numbered_variant(n, own_norm) for n in names):
                names={own_norm}
        row['official_timetable_rows']=[list(r) for r in timetable_rows if r and
            any(MetroFlowDataset.normalize(r[0])==n or MetroFlowDataset.normalize(r[0]).startswith(n+' (') for n in names)]
    return dict(city=city.key,source=source,fetched_at=fetched_at or time.time(),entrances=list(nodes.values()),
        metadata=dict(status='mapped',entrances=len(nodes),active_entrances=sum(n['active'] for n in nodes.values()),
            associated_entrances=sum(bool(n['station_names']) for n in nodes.values()),
            stations=len({n for row in nodes.values() for n in row['station_names']}),
            timetable_source=TIMETABLE if timetable_rows else None,
            timetable_fetched_at=timetable_fetched_at or time.time() if timetable_rows else None,
            timetable_interpretation='raw published rows; conditional hours not automatically asserted',
            coverage_complete=False,coverage_scope='all matching OSM nodes in city bounds; map completeness not proven'))


class MetroEntranceDataset:
    def __init__(self,data):
        self.city=get_city(data['city']);self.source=data['source'];self.fetched_at=data['fetched_at']
        if (not isinstance(self.source,str) or not self.source or type(self.fetched_at) not in (int,float)
                or not math.isfinite(self.fetched_at) or not 0<=time.time()-self.fetched_at<=7*86400):
            raise ValueError('Invalid or expired entrance snapshot')
        self.entrances=data['entrances'];self.diagnostics=data['metadata']
        seen=set()
        for row in self.entrances:
            if (not self.city.contains(row['lat'],row['lng']) or row['node'] in seen
                    or not row['node'].startswith('osm:') or not isinstance(row['station_names'],list)
                    or not all(isinstance(n,str) and n.strip() for n in row['station_names'])):
                raise ValueError('Invalid entrance snapshot')
            seen.add(row['node'])
        self._known_by_norm=_known_station_index(n for row in self.entrances for n in row['station_names'])

    def nearby_groups(self,lat,lng,radius_m=1200):
        groups={}
        for entrance in self.entrances:
            if not entrance['active']:continue
            distance=haversine_distance(lat,lng,entrance['lat'],entrance['lng'])
            if distance>radius_m:continue
            # Ambiguous stop areas without disambiguating vestibule names do not become several copies of one flow.
            name,binding=_resolve_group_name(entrance,self._known_by_norm)
            key=MetroFlowDataset.normalize(name)
            group=groups.setdefault(key,dict(name=name,entrances=[],distance_m=distance,
                coordinates=[entrance['lng'],entrance['lat']],station_binding=binding))
            group['entrances'].append(entrance)
            resolved_bindings={_resolve_group_name(v,self._known_by_norm)[1] for v in group['entrances']}
            if binding!='unbound' and len(resolved_bindings)>1:group['station_binding']='mixed_explicit_station_evidence'
            if distance<group['distance_m']:
                group.update(distance_m=distance,coordinates=[entrance['lng'],entrance['lat']])
        return sorted(groups.values(),key=lambda g:(g['distance_m'],g['name']))


class MetroEntranceSource:
    def __init__(self,*,cache_path='.local-cache/public_sources.sqlite3',session=None,ttl=86400,bootstrap_dir=None):
        from .cities import runtime_root
        self.cache=SourceCache(cache_path);self.session=session or requests.Session();self.ttl=ttl
        self.bootstrap_dir=(runtime_root()/'data/reference' if bootstrap_dir is None
                            else Path(bootstrap_dir) if bootstrap_dir is not False else None)
        self.session.headers.update({'User-Agent':'CommercialPremisesEngine/0.1 (public OSM map reader; interactive local development)',
                                     'Accept':'application/json'})
        self.lock=asyncio.Lock();self.datasets={};self.next_check={};self.diagnostics={}

    def _fetch(self,city):
        s,n,w,e=city.bounds
        query=(f'[out:json][timeout:35];node({s},{w},{n},{e})["railway"="subway_entrance"]->.e;'
               'rel(bn.e)["public_transport"="stop_area"]->.r;(.e;.r;);out body;')
        payload=None;source=ENDPOINT;last_exc=None
        for source in OVERPASS_ENDPOINTS:
            try:
                response=self.session.get(source,params={'data':query},timeout=(10,45))
                response.raise_for_status()
                if len(response.content)>8000000:raise ValueError('Metro snapshot too large')
                payload=response.json()
                if not isinstance(payload,dict) or payload.get('remark') or not isinstance(payload.get('elements'),list):
                    raise ValueError('Incomplete or malformed OSM metro response')
                break
            except (requests.RequestException,ValueError) as exc:
                last_exc=exc;payload=None
        if payload is None:
            raise last_exc or RuntimeError('Metro Overpass fetch failed')
        rows=[]
        if city.key=='saint_petersburg':
            try:
                from curl_cffi import requests as curl_requests
                reply=curl_requests.get(TIMETABLE,impersonate='chrome120',verify=False,timeout=10)
                reply.raise_for_status();parser=TableRows();parser.feed(reply.text);rows=parser.rows
            except Exception:pass
        return parse_entrances(payload,city,source=source,timetable_rows=rows)

    async def get_dataset(self,city,*,force_refresh=False):
        city=get_city(city)
        async with self.lock:
            imported=os.environ.get('METRO_ENTRANCES_DATA_FILE')
            if imported:
                try:
                    path=Path(imported)
                    if path.stat().st_size>8000000:raise ValueError('Metro entrance import too large')
                    dataset=MetroEntranceDataset(json.loads(path.read_text(encoding='utf-8-sig')))
                    if dataset.city!=city:raise ValueError('Metro entrance import city mismatch')
                    self.datasets[city.key]=dataset
                    self.diagnostics[city.key]=dict(dataset.diagnostics,data_origin='provided_dataset',source=dataset.source,
                        fetched_at=datetime.fromtimestamp(dataset.fetched_at,timezone.utc).isoformat())
                    return dataset
                except (ValueError,TypeError,KeyError,OSError) as exc:
                    self.datasets.pop(city.key,None)
                    self.diagnostics[city.key]=dict(status='unavailable',data_origin='provided_dataset',error_type=type(exc).__name__)
                    return None
            if not force_refresh and time.monotonic()<self.next_check.get(city.key,0):
                current=self.datasets.get(city.key)
                if current is None or 0<=time.time()-current.fetched_at<=7*86400:return current
            fallback=None;origin='cache'
            try:
                key='metro_entrances_v1:'+city.key;cached=self.cache.get(key)
                if cached:
                    try:
                        fallback=json.loads(cached[1]);cached_dataset=MetroEntranceDataset(fallback)
                        if cached_dataset.city!=city:raise ValueError('Cached metro city mismatch')
                    except (ValueError,TypeError,KeyError):fallback=None
                path=self.bootstrap_dir/f'metro_entrances_{city.key}.json' if self.bootstrap_dir else None
                if fallback is None and path and path.is_file():
                    try:
                        fallback=json.loads(path.read_text(encoding='utf-8'));dataset=MetroEntranceDataset(fallback)
                        if dataset.city!=city:raise ValueError('Metro entrance import city mismatch')
                        origin='reference_snapshot'
                    except (ValueError,TypeError,KeyError,OSError):fallback=None;origin='cache'
                if fallback is not None and not force_refresh and time.time()-fallback['fetched_at']<self.ttl:
                    data=fallback
                else:
                    data=await asyncio.to_thread(self._fetch,city);origin='live'
                dataset=MetroEntranceDataset(data)
                if dataset.city!=city:raise ValueError('Metro catalogue city mismatch')
                self.datasets[city.key]=dataset;self.next_check[city.key]=time.monotonic()+self.ttl
                self.diagnostics[city.key]=dict(dataset.diagnostics,cache_hit=origin!='live',data_origin=origin,source=dataset.source,
                    fetched_at=datetime.fromtimestamp(dataset.fetched_at,timezone.utc).isoformat())
                self.cache.put(key,json.dumps(data,ensure_ascii=False).encode(),max(1,7*86400-(time.time()-dataset.fetched_at)))
                return dataset
            except Exception as exc:
                if fallback is not None:
                    try:
                        dataset=MetroEntranceDataset(fallback)
                        if dataset.city!=city:raise ValueError('Fallback city mismatch')
                        self.datasets[city.key]=dataset;self.next_check[city.key]=time.monotonic()+600
                        self.diagnostics[city.key]=dict(dataset.diagnostics,status='stale_snapshot',data_origin=origin,
                            source=dataset.source,fetched_at=datetime.fromtimestamp(dataset.fetched_at,timezone.utc).isoformat(),
                            refresh_error_type=type(exc).__name__)
                        return dataset
                    except (ValueError,TypeError,KeyError):pass
                self.datasets.pop(city.key,None);self.next_check[city.key]=time.monotonic()+60
                self.diagnostics[city.key]=dict(status='unavailable',error_type=type(exc).__name__)
                return None
