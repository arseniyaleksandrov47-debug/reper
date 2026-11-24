"""Automatic acquisition of mapped entrances and pedestrian topology.

Only shared OSM node IDs create connections: proximity never crosses a fence,
railway or street. Building entrances remain distinct from a verified unit door.
"""
import asyncio
import copy
import hashlib
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime,timezone
import requests
from .source_cache import SourceCache
from .storage import haversine_distance
from .accessibility import PedestrianNetwork
from .walk_barriers import BarrierIndex,_level


def _tags(element):return {t.get('k'):t.get('v') for t in element.findall('tag')}


def _public(tags):
    if tags.get('foot:conditional') or tags.get('access:conditional') or tags.get('opening_hours') or tags.get('locked:conditional'):
        return False
    foot=tags.get('foot')
    if foot in ('no','private','use_sidepath'):return False
    if foot in ('yes','designated','permissive'):return True
    if tags.get('access') in ('no','private','delivery','customers','permit','destination'):
        return False
    return not (tags.get('access:conditional') or tags.get('opening_hours'))


def _walk_way(tags):
    if not _public(tags) or tags.get('area')=='yes' or tags.get('construction') or tags.get('proposed'):return False
    if tags.get('motorroad')=='yes' or tags.get('barrier') in ('fence','wall','hedge','retaining_wall','city_wall'):return False
    highway=tags.get('highway')
    if highway in ('footway','pedestrian','path','steps','living_street','residential','service','unclassified','track'):
        return True
    # Never use the centreline of a major road as a substitute for separately
    # mapped sidewalks, crossings and access to the correct side of the road.
    return False


def _blocked_node(tags):
    if not _public(tags) or tags.get('locked')=='yes':return True
    if tags.get('barrier') and tags.get('barrier') not in ('bollard','cycle_barrier','kerb','entrance'):
        return tags.get('foot') not in ('yes','designated','permissive')
    return False


def _street(value):
    text=str(value).lower().replace('ё','е')
    for short,long in [('пр-т','проспект'),('просп.','проспект'),('ул.','улица'),('пл.','площадь'),('наб.','набережная')]:text=text.replace(short,long)
    return ' '.join(re.findall(r'[а-яa-z0-9]+',text))


def _house(value):
    text=str(value).lower().replace('ё','е')
    text=re.sub(r'корпус|корп\.?','к',text)
    text=re.sub(r'строение|стр\.?|ст(?=\s*\d)','с',text)
    return re.sub(r'[^а-яa-z0-9/]','',text)


def _address_match(tags,address):
    street=tags.get('addr:street') or tags.get('addr:place')
    house=tags.get('addr:housenumber')
    if not street or not house:return False
    needle=_street(street)
    # Accept both 'улица Большая Академическая' and 'Большая Академическая улица'.
    tokens=needle.split()
    pieces=re.split(r'[,;]| / ',str(address))
    for i,piece in enumerate(pieces):
        normalized=_street(piece)
        if not all(re.search(r'(?<!\w)'+re.escape(t)+r'(?!\w)',normalized) for t in tokens):continue
        for candidate in pieces[i:i+2]:
            match=re.search(r'\d+[а-яa-z]?(?:\s*(?:/\s*\d+|к(?:орп(?:ус)?)?\.?\s*\d+|с(?:тр(?:оение)?)?\.?\s*\d+|ст\s*\d+))*',candidate,re.I)
            if match and _house(match.group(0))==_house(house):return True
    return False


def parse_osm_map(body,source,*,fetched_at=None,walking_speed_m_min=80):
    if type(walking_speed_m_min) not in (int,float) or not math.isfinite(walking_speed_m_min) or walking_speed_m_min<=0:
        raise ValueError('Invalid explicit walking speed assumption')
    if len(body)>16000000 or b'<!DOCTYPE' in body.upper():raise ValueError('Invalid OSM snapshot size or XML declaration')
    root=ET.fromstring(body)
    if root.tag!='osm':raise ValueError('Not an OSM map response')
    nodes={};ways=[];relations=[]
    for el in root:
        if el.tag=='node':
            lat,lng=float(el.get('lat')),float(el.get('lon'))
            if not -90<=lat<=90 or not -180<=lng<=180:raise ValueError('Invalid OSM coordinates')
            nodes[el.get('id')]=dict(id=el.get('id'),lat=lat,lng=lng,tags=_tags(el),timestamp=el.get('timestamp'))
        elif el.tag=='way':ways.append(dict(id=el.get('id'),nodes=[n.get('ref') for n in el.findall('nd')],tags=_tags(el),timestamp=el.get('timestamp')))
        elif el.tag=='relation':relations.append(dict(id=el.get('id'),members=[dict(m.attrib) for m in el.findall('member')],tags=_tags(el)))
    if len(nodes)>100000 or len(ways)>50000:raise ValueError('OSM snapshot is too large')
    barriers=BarrierIndex(nodes,ways)
    levels={};vertical_nodes=set()
    for way in ways:
        if not _walk_way(way['tags']):continue
        for n in way['nodes']:levels.setdefault(n,set()).add(_level(way['tags']))
        if way['tags'].get('highway')=='steps':vertical_nodes.update((way['nodes'][0],way['nodes'][-1]))
    def graph_key(way,node):
        key='osm:'+node
        separated=(way['tags'].get('bridge') not in (None,'no') or way['tags'].get('tunnel') not in (None,'no'))
        # Bridge/tunnel endpoints join their approaches. An interior shared
        # coordinate is not a connection to a ground-level crossing way.
        if (len(levels.get(node,()))>1 and separated and node not in (way['nodes'][0],way['nodes'][-1])
                and node not in vertical_nodes and nodes[node]['tags'].get('highway')!='elevator'):
            key+=':layer:'+str(_level(way['tags']))
        return key
    edges=[];graph_nodes=set();excluded=0;blocked_crossings=[];waiting_nodes=set();crossing_counts={}
    for way in ways:
        tags=way['tags']
        if not _walk_way(tags):
            excluded+=bool(tags.get('highway'));continue
        reverse=tags.get('oneway:foot')=='-1'
        one_way=tags.get('oneway:foot') in ('yes','1','-1')
        for a,b in zip(way['nodes'],way['nodes'][1:]):
            if a not in nodes or b not in nodes or _blocked_node(nodes[a]['tags']) or _blocked_node(nodes[b]['tags']):continue
            conflict=barriers.conflict(a,b,way)
            if conflict:
                crossing_counts[conflict['kind']]=crossing_counts.get(conflict['kind'],0)+1
                if len(blocked_crossings)<100:blocked_crossings.append(dict(from_node=a,to_node=b,walk_way=way['id'],**conflict))
                continue
            metres=haversine_distance(nodes[a]['lat'],nodes[a]['lng'],nodes[b]['lat'],nodes[b]['lng'])
            if metres<=0:continue
            if reverse:a,b=b,a
            graph_nodes.update((graph_key(way,a),graph_key(way,b)))
            for start,end in ((a,b),) if one_way else ((a,b),(b,a)):
                end_tags=nodes[end]['tags']
                wait_unknown=(end_tags.get('highway')=='traffic_signals' or end_tags.get('crossing')=='traffic_signals'
                    or end_tags.get('crossing:signals')=='yes' or end_tags.get('railway') in ('crossing','level_crossing'))
                if wait_unknown:waiting_nodes.add(end)
                edges.append(dict(**{'from':graph_key(way,start),'to':graph_key(way,end)},minutes=metres/walking_speed_m_min,
                    walk_allowed=True,bidirectional=False,source_way='https://www.openstreetmap.org/way/'+way['id'],
                    steps=tags.get('highway')=='steps',wait_minutes_lower=0,wait_minutes_upper=None if wait_unknown else 0))
    when=fetched_at or datetime.now(timezone.utc).isoformat()
    network=dict(source=source,as_of=when,coverage_complete=False,nodes=sorted(graph_nodes),edges=edges,
        method='OSM shared-node topology with geometric barrier validation',walking_speed_m_min=walking_speed_m_min,
        time_kind='mapped_length_with_unknown_crossing_wait' if waiting_nodes else 'estimated_from_mapped_length_not_measured_walk',
        barrier_diagnostics=dict(rejected_crossings=blocked_crossings,rejected_crossing_counts=crossing_counts,
                                 examples_truncated=sum(crossing_counts.values())>len(blocked_crossings),
                                 unknown_wait_nodes=sorted(waiting_nodes),
                                 conditional_walk_ways=sum(bool(w['tags'].get('highway')) and any(w['tags'].get(k) for k in
                                     ('opening_hours','access:conditional','foot:conditional','locked:conditional')) for w in ways)),
        attribution='© OpenStreetMap contributors, ODbL')
    # Entrances without a graph connection stay isolated; no straight line
    # from a building centre or an entrance to a nearby street is invented.
    for node in nodes.values():
        if node['tags'].get('entrance') or node['tags'].get('railway')=='subway_entrance':
            if 'osm:'+node['id'] not in network['nodes']:network['nodes'].append('osm:'+node['id'])
    if not network['nodes']:raise ValueError('No pedestrian graph or entrances in OSM response')
    return dict(network=network,nodes=nodes,ways=ways,relations=relations,excluded_walk_ways=excluded)


def assess_mapped_access(snapshot,prop,*,max_arrival_distance_m=1000):
    nodes=snapshot['nodes'];network=snapshot['network'];address=prop.get('address','')
    related=[];member_ways=set();ways_by_id={w['id']:w for w in snapshot['ways']}
    for relation in snapshot.get('relations',[]):
        if not relation['tags'].get('building') or not _address_match(relation['tags'],address):continue
        parts=[ways_by_id[m['ref']] for m in relation['members'] if m.get('type')=='way'
               and m.get('role') in ('outer','') and m.get('ref') in ways_by_id]
        if not parts:continue
        member_ways.update(p['id'] for p in parts)
        related.append(dict(id=relation['id'],nodes=list(dict.fromkeys(n for p in parts for n in p['nodes'])),
                            tags=relation['tags'],osm_type='relation'))
    buildings=[dict(w,osm_type='way') for w in snapshot['ways'] if w['id'] not in member_ways
               and w['tags'].get('building') and _address_match(w['tags'],address)]+related
    candidates=[]
    for building in buildings:
        for key in building['nodes']:
            node=nodes.get(key)
            if not node or not node['tags'].get('entrance'):continue
            tags=node['tags'];kind=tags['entrance']
            if kind in ('no','exit','emergency','staircase','service','home'):continue
            if not _public(tags) and not (tags.get('access')=='customers' and tags.get('foot') not in ('no','private') and
                    not tags.get('access:conditional') and not tags.get('opening_hours')):continue
            if tags.get('level') not in (None,'0','0.0'):continue
            candidates.append(dict(node='osm:'+key,verified=True,source='https://www.openstreetmap.org/node/'+key,
                lat=node['lat'],lng=node['lng'],tags=tags,binding_scope='building_entrance',
                building='https://www.openstreetmap.org/'+building['osm_type']+'/'+building['id']))
    candidates=list({c['node']:c for c in candidates}.values())
    arrivals=[]
    station_names={}
    for relation in snapshot.get('relations',[]):
        tags=relation['tags']
        if tags.get('public_transport')!='stop_area' or not tags.get('name'):continue
        # A named stop area must actually contain a mapped subway station.
        member_nodes=[nodes[m['ref']] for m in relation['members'] if m.get('type')=='node' and m.get('ref') in nodes]
        if not any(n['tags'].get('railway')=='station' and (n['tags'].get('station')=='subway' or n['tags'].get('subway')=='yes') for n in member_nodes):continue
        for n in member_nodes:
            if n['tags'].get('railway')=='subway_entrance':station_names.setdefault(n['id'],[]).append(tags['name'])
    lat,lng=prop.get('lat'),prop.get('lng')
    for key,node in nodes.items():
        t=node['tags']
        is_metro=t.get('railway')=='subway_entrance'
        is_stop=t.get('highway')=='bus_stop' or t.get('public_transport') in ('platform','stop_position')
        if not (is_metro or is_stop) or _blocked_node(t):continue
        distance=haversine_distance(lat,lng,node['lat'],node['lng'])
        if distance>max_arrival_distance_m:continue
        arrivals.append(dict(node='osm:'+key,verified=True,source='https://www.openstreetmap.org/node/'+key,
            lat=node['lat'],lng=node['lng'],kind='metro_exit' if is_metro else 'public_transport_stop',name=t.get('name',''),
            station_names=list(dict.fromkeys([*station_names.get(key,[]),*([t['station:name']] if t.get('station:name') else []),
                *([t['name']] if is_metro and t.get('name') else [])])),distance_m=distance))
    graph=PedestrianNetwork(network);routes=[]
    for entrance in candidates:
        for origin,route in zip(arrivals,graph.travel_many_to(arrivals,entrance)):
            routes.append(dict(origin=origin,entry=entrance,status=route.status,minutes=route.minutes,
                minutes_lower=route.minutes_lower,minutes_upper=route.minutes_upper))
    reachable=sorted((r for r in routes if r['status']=='reachable'),key=lambda r:r['minutes_lower'])
    diagnostic=dict(status='available' if reachable else 'needs_verification',building_matches=len(buildings),
        matched_buildings=[dict(osm_id=w['id'],osm_type=w['osm_type'],tags=w['tags']) for w in buildings],entrance_candidates=candidates,
        arrivals=arrivals,route_candidates=routes,source=network['source'],as_of=network['as_of'],
        network_nodes=len(network['nodes']),network_edges=len(network['edges']),
        barrier_diagnostics=network.get('barrier_diagnostics',{}),
        coverage_complete=False,unit_entrance_verified=False,
        warnings=['Картографический вход здания не устанавливает дверь арендуемого блока',
                  'Пути используют только связи OSM; отсутствие пути в неполной карте остаётся неизвестным',
                  'Время модельное: длина пути / 80 м/мин, не измеренный проход',
                  'Ожидание на светофорах и железнодорожных переходах без замера задаётся неизвестной верхней границей',
                  'Проходы с неразобранным расписанием или условным доступом не считаются постоянно открытыми'])
    if not buildings:diagnostic['missing_reason']='no_exact_address_building'
    elif not candidates:diagnostic['missing_reason']='no_mapped_public_ground_entrance'
    elif not arrivals:diagnostic['missing_reason']='no_mapped_arrival_point'
    elif not reachable:diagnostic['missing_reason']='entrances_not_connected_to_walk_network'
    # A single mapped public building entrance can be used for a separately
    # labelled approach-to-building estimate. Never claim that it is a unit door.
    evidence=dict(pedestrian_network=network,accessibility_diagnostics=diagnostic)
    if len(buildings)==1 and len(candidates)==1 and reachable:
        chosen=reachable[0]
        evidence['access_routes']=dict(walk=dict(origin=chosen['origin'],entry=chosen['entry'],scope='building_entrance'))
        diagnostic['chosen_route']=chosen
    elif reachable:diagnostic['missing_reason']='ambiguous_building_entrance'
    return evidence


class OSMPedestrianProvider:
    def __init__(self,*,cache_path='.local-cache/public_sources.sqlite3',session=None,radius_m=650,timeout=18):
        self.cache=SourceCache(cache_path);self.session=session or requests.Session()
        self.radius_m=radius_m;self.timeout=timeout;self.lock=asyncio.Lock();self.last_start=0;self.requests=0
        self.session.headers.update({'User-Agent':'CommercialPremisesEngine/0.1 (public OSM map reader)'})

    async def enrich(self,prop):
        try:
            lat=float(prop['lat']);lng=float(prop['lng'])
            if not math.isfinite(lat) or not math.isfinite(lng) or not -85<lat<85 or not -180<lng<180:raise ValueError('Invalid coordinates')
            lat_cell=round(lat,3);lng_cell=round(lng,3)
            margin=self.radius_m+100;dy=margin/111320;dx=dy/math.cos(math.radians(lat_cell))
            bbox=f'{lng_cell-dx:.7f},{lat_cell-dy:.7f},{lng_cell+dx:.7f},{lat_cell+dy:.7f}'
            url='https://www.openstreetmap.org/api/0.6/map?bbox='+bbox
            key='osm_map_v1:'+hashlib.sha256(url.encode()).hexdigest()
            async with self.lock:
                cached=self.cache.get(key)
                if cached:
                    fetched,body=cached;cache_hit=True
                else:
                    delay=1-(time.monotonic()-self.last_start)
                    if delay>0:await asyncio.sleep(delay)
                    self.last_start=time.monotonic();self.requests+=1
                    response=await asyncio.to_thread(self.session.get,url,timeout=(5,self.timeout))
                    response.raise_for_status();body=response.content
                    if len(body)>16000000:raise ValueError('OSM response exceeds size bound')
                    fetched=time.time();cache_hit=False
                    # Validate before caching: an HTML error page is never a snapshot.
                    parse_osm_map(body,url,fetched_at=datetime.fromtimestamp(fetched,timezone.utc).isoformat())
                    self.cache.put(key,body,3600)
            snapshot=parse_osm_map(body,url,fetched_at=datetime.fromtimestamp(fetched,timezone.utc).isoformat())
            evidence=assess_mapped_access(snapshot,prop)
            evidence['accessibility_diagnostics'].update(cache_hit=cache_hit,provider='OpenStreetMap API 0.6',bbox=bbox)
            return evidence
        except (requests.RequestException,ValueError,TypeError,KeyError,ET.ParseError) as exc:
            return dict(accessibility_diagnostics=dict(status='unavailable',error_type=type(exc).__name__,
                source='https://www.openstreetmap.org/api/0.6/map',warnings=['Не удалось получить или связать пешеходную сеть; маршрут остаётся неизвестным']))


def attach_automatic_access(prop,automatic):
    """An in-memory enrichment; supplied verified evidence owns its graph."""
    result=copy.deepcopy(prop)
    raw=result.get('raw_attributes',result.get('raw_json',{}))
    if isinstance(raw,str):raw=json.loads(raw or '{}')
    if not isinstance(raw,dict):raise ValueError('Invalid listing metadata')
    existing=raw.get('business_evidence',{})
    if not isinstance(existing,dict):raise ValueError('Invalid business evidence')
    extra=copy.deepcopy(automatic)
    if any(k in existing for k in ('pedestrian_network','access_routes','walk_time_minutes')):
        extra.pop('pedestrian_network',None);extra.pop('access_routes',None)
        if 'accessibility_diagnostics' in extra:extra['accessibility_diagnostics']['manual_route_precedence']=True
    raw['business_evidence']={**extra,**existing}
    result['raw_attributes']=raw
    return result
