"""Cached full-territory housing inventories for municipal population scenarios."""
import hashlib
import json
import math
import os
import re
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

from curl_cffi import requests
from .cities import get_city, runtime_root
from .territory_population import TerritoryPopulationModel, building_identity, contains_point, positive
from .storage import haversine_distance

_LOCK = threading.RLock()
ROOT = runtime_root()


def _name(value):
    text = re.sub(r'муниципальный|муниципальное|образование|округ', '', str(value).casefold())
    return ''.join(re.findall(r'[а-яa-z0-9]',text.replace('ё','е')))


def housing_record(raw, analyzer):
    purpose = str(raw.get('purpose_name') or raw.get('building_name') or '').casefold()
    struct = raw.get('structure_info') or {}; apartments = positive(struct.get('apartments_count'))
    lifecycle = ' '.join(str(raw.get(k) or '') for k in ('purpose_name','building_name','construction_status','status')).casefold()
    if re.search(r'строящ|строительств|снес|снос|проектируем|рассел',lifecycle):
        return None
    if apartments is None and not any(word in purpose for word in ('жилой','общежитие','апартамент','таунхаус')):
        return None
    point = raw.get('point') or {}; lat=point.get('lat'); lng=point.get('lon')
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in (lat,lng)):
        raise ValueError('Residential inventory point missing')
    floors = positive((raw.get('floors') or {}).get('ground_count'))
    footprint = analyzer._polygon_area_sqm((raw.get('geometry') or {}).get('hover'),lat)
    return dict(building_id=building_identity(raw.get('id')),lat=lat,lng=lng,apartments=apartments,
        volume=footprint*floors if footprint and floors else None,
        address=str(raw.get('name') or raw.get('address_name') or ''))


def _segment_intersects_box(lng0, lat0, lng1, lat1, south, north, west, east):
    """Return True when segment (lng0,lat0)-(lng1,lat1) overlaps [south,north]x[west,east]."""
    dx=lng1-lng0;dy=lat1-lat0;t0,t1=0.0,1.0
    for p,q in ((-dx,lng0-west),(dx,east-lng0),(-dy,lat0-south),(dy,north-lat0)):
        if p==0:
            if q<0:return False
        else:
            r=q/p
            if p<0:t0=max(t0,r)
            else:t1=min(t1,r)
            if t0>t1:return False
    return True


def _cell_intersects_geometry(geometry, south, north, west, east):
    """Return True unless the rectangular tile [south,north]x[west,east] is disjoint from geometry."""
    for lat,lng in ((south,west),(south,east),(north,west),(north,east),((south+north)/2,(west+east)/2)):
        if contains_point(geometry,lat,lng):return True
    kind=geometry.get('type') if isinstance(geometry,dict) else None
    coords=geometry.get('coordinates') if isinstance(geometry,dict) else None
    if not coords:return True
    polygons=coords if kind=='MultiPolygon' else [coords] if kind=='Polygon' else []
    if not polygons:return True
    for poly in polygons:
        if not poly or not poly[0]:continue
        ring=poly[0]
        for a,b in zip(ring,[*ring[1:],ring[0]]):
            if _segment_intersects_box(float(a[0]),float(a[1]),float(b[0]),float(b[1]),south,north,west,east):
                return True
    return False


class MunicipalPopulationSource:
    def __init__(self, analyzer, *, reference_file=None, cache_dir='.local-cache/municipal_population', budget_s=120):
        self.analyzer=analyzer; self.reference_file=reference_file;self.cache_dir=Path(cache_dir);self.budget_s=budget_s
        self.session=requests.Session(impersonate='chrome120',verify=False)
        self.session.headers.update({'User-Agent':'CommercialPremisesEngine/0.1 (municipal housing inventory)'})
        self.next_nominatim=0
        self.retry_after={};self.partial_inventories={}

    def _references(self, city):
        path=Path(self.reference_file or os.environ.get('POPULATION_TERRITORY_DATA_FILE') or
                  ROOT/'data/reference'/f'{city.key}_population.json')
        if not path.is_file():return None
        if path.stat().st_size>1000000:raise ValueError('Population reference too large')
        data=json.loads(path.read_text(encoding='utf-8'))
        if get_city(data['city'])!=city:raise ValueError('Population reference city mismatch')
        if (not isinstance(data['source'],str) or not data['source'].strip()
                or not 0<=(date.today()-date.fromisoformat(data['as_of'])).days<=730
                or not isinstance(data['territories'],list)
                or any(not isinstance(t,dict) or type(t.get('population')) is not int or t['population']<0
                       or not isinstance(t.get('name'),str) or not t['name'].strip()
                       or ('osm_id' in t and (type(t['osm_id']) is not int or t['osm_id']<=0))
                       for t in data['territories'])):
            raise ValueError('Invalid or expired municipal reference')
        return data

    @staticmethod
    def _territory_names(territory):
        return {_name(territory['name']), _name(territory.get('query_name', '')),
                *(_name(alias) for alias in territory.get('aliases', []) if isinstance(alias, str))} - {''}

    @staticmethod
    def _matching_boundaries(items, expected_names, city, *, expected_osm_id=None):
        matches=[]
        for item in items:
            geometry=item.get('geojson') or {}
            first=item.get('display_name','').split(',')[0]
            if (item.get('osm_type')=='relation'
                    and (expected_osm_id is None or item.get('osm_id')==expected_osm_id)
                    and geometry.get('type') in ('Polygon','MultiPolygon')
                    and _name(first) in expected_names
                    and city.contains(float(item['lat']),float(item['lon']))):
                matches.append(item)
        return matches

    def _boundary(self, territory, city):
        delay=self.next_nominatim-time.monotonic()
        if delay>0:time.sleep(delay)
        self.next_nominatim=time.monotonic()+1.1
        osm_id=territory.get('osm_id')
        expected_names=self._territory_names(territory)
        if type(osm_id) is int and osm_id>0:
            response=self.session.get('https://nominatim.openstreetmap.org/lookup',params={
                'osm_ids':f'R{osm_id}','format':'jsonv2','polygon_geojson':1,'accept-language':'ru'},timeout=12)
            response.raise_for_status()
            matches=self._matching_boundaries(response.json(),expected_names,city,expected_osm_id=osm_id)
            if len(matches)!=1:raise ValueError('No unique municipal boundary')
            return matches[0]
        response=self.session.get('https://nominatim.openstreetmap.org/search',params={
            'q':territory.get('query_name',territory['name'])+', '+city.name,'format':'jsonv2',
            'polygon_geojson':1,'limit':10,'accept-language':'ru',
            'viewbox':f'{city.bounds[2]},{city.bounds[1]},{city.bounds[3]},{city.bounds[0]}','bounded':1},timeout=12)
        response.raise_for_status()
        target_names={_name(territory.get('query_name',territory['name']))}|expected_names
        matches=self._matching_boundaries(response.json(),target_names,city)
        if len(matches)!=1:raise ValueError('No unique municipal boundary')
        return matches[0]

    def _build(self, reference, territory, city):
        boundary=self._boundary(territory,city);geometry=boundary['geojson']
        south,north,west,east=map(float,boundary['boundingbox'])
        # Boundaries are source facts; grid cells only partition the acquisition.
        if not city.contains(south,west) or not city.contains(north,east):raise ValueError('Territory outside city')
        cells=[(south,north,west,east)];rows={};failures=[]; completed=0;requests_count=0
        deadline=time.monotonic()+self.budget_s
        while cells and requests_count<512 and time.monotonic()<deadline:
            s,n,w,e=cells.pop()
            if not _cell_intersects_geometry(geometry,s,n,w,e):
                continue
            lat=(s+n)/2;lng=(w+e)/2
            height=(n-s)*111320;width=(e-w)*111320*math.cos(math.radians(lat));radius=math.hypot(height,width)/2+2
            if radius>550:
                cells.extend([(s,lat,w,lng),(s,lat,lng,e),(lat,n,w,lng),(lat,n,lng,e)])
                continue
            items=self.analyzer._fetch_2gis_buildings_sync(lat,lng,radius);requests_count+=1
            if not getattr(items,'coverage_complete',False) and max(width,height)>160:
                cells.extend([(s,lat,w,lng),(s,lat,lng,e),(lat,n,w,lng),(lat,n,lng,e)])
                continue
            if not getattr(items,'coverage_complete',False):
                failures.append('Incomplete building tile');continue
            completed+=1
            for raw in items:
                try:
                    record=housing_record(raw,self.analyzer)
                    if record is None or not contains_point(geometry,record['lat'],record['lng']):continue
                    identifier=record['building_id']
                    if identifier in rows and rows[identifier]!=record:
                        # Conflicting snapshots cannot silently become two houses or a trusted denominator.
                        failures.append('Conflicting building snapshot')
                    rows[identifier]=record
                except (ValueError,TypeError,KeyError):failures.append('Malformed housing record')
        complete=not cells and not failures
        data=dict(name=territory['name'],source=reference['source'],as_of=reference['as_of'],
            population=territory['population'],geometry=geometry,
            boundary_source=f"https://www.openstreetmap.org/relation/{boundary['osm_id']}",
            inventory_source='https://catalog.api.2gis.ru/3.0/items',
            inventory_as_of=datetime.now(timezone.utc).isoformat(),coverage_complete=complete,
            buildings=list(rows.values()),diagnostics=dict(completed_tiles=completed,queried_tiles=requests_count,
                pending_tiles=len(cells),errors=sorted(set(failures)),
                coverage_scope='complete response for every covering tile; completeness of the map itself is not guaranteed'))
        TerritoryPopulationModel(data)
        return data

    def get_model(self, reference, territory, city, *, force_refresh=False):
        identity=json.dumps(dict(v=2,city=city.key,territory=territory,source=reference['source'],as_of=reference['as_of']),sort_keys=True)
        path=self.cache_dir/(hashlib.sha256(identity.encode()).hexdigest()+'.json')
        with _LOCK:
            if not force_refresh and path.is_file() and time.time()-path.stat().st_mtime<7*86400:
                try:
                    data=json.loads(path.read_text(encoding='utf-8'));return TerritoryPopulationModel(data),data
                except (ValueError,TypeError,KeyError):pass
            if not force_refresh and time.monotonic()<self.retry_after.get(identity,0):
                if identity in self.partial_inventories:
                    data=self.partial_inventories[identity];return TerritoryPopulationModel(data),data
                raise ValueError('Municipal inventory source is cooling down')
            try:data=self._build(reference,territory,city)
            except Exception:
                self.retry_after[identity]=time.monotonic()+60
                raise
            if data['coverage_complete']:
                self.retry_after.pop(identity,None);self.partial_inventories.pop(identity,None)
                path.parent.mkdir(parents=True,exist_ok=True)
                temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');temporary.replace(path)
            else:
                self.retry_after[identity]=time.monotonic()+60;self.partial_inventories[identity]=data
            return TerritoryPopulationModel(data),data

    def estimate_circle(self, city, raw_buildings, residential_ids):
        reference=self._references(city) if city else None
        if not reference:return None
        selected={building_identity(v) for v in residential_ids};groups={};unmatched=set(selected);points_by_id={}
        for raw in raw_buildings:
            identifier=building_identity(raw.get('id'))
            if identifier not in selected:continue
            point=raw.get('point') or {}
            try:points_by_id[identifier]=(float(point['lat']),float(point['lon']))
            except (TypeError,ValueError,KeyError):continue
            names={_name(v.get('name')) for v in raw.get('adm_div',[]) if isinstance(v,dict)
                   and (v.get('detailed_subtype')=='mun_okrug' or v.get('type')=='living_area')}
            matches=[t for t in reference['territories'] if names & {_name(t['name']),*map(_name,t.get('aliases',[]))}]
            if len(matches)==1:groups.setdefault(matches[0]['name'],dict(territory=matches[0],ids=set()))['ids'].add(identifier)
        results=[];errors=[];points=0;lower=upper=0;building_estimates={}
        for group in groups.values():
            try:
                model,_=self.get_model(reference,group['territory'],city)
                rejected={identifier for identifier in group['ids'] if identifier in model.buildings and
                    haversine_distance(*points_by_id[identifier],model.buildings[identifier]['lat'],model.buildings[identifier]['lng'])>30}
                bound_ids=group['ids']-rejected
                if rejected:errors.append(dict(territory=group['territory']['name'],error_type='BuildingPositionMismatch'))
                estimate=model.estimate(bound_ids);results.append(estimate)
                for identifier in bound_ids:
                    individual=model.estimate([identifier])
                    building_estimates[identifier]={k:individual[k] for k in ('point','lower','upper')}
                lower+=estimate['lower'];upper+=estimate['upper']
                points=points+estimate['point'] if points is not None and estimate['point'] is not None else None
                unmatched-=bound_ids
            except (ValueError,TypeError,KeyError,OSError,requests.RequestsError) as exc:
                errors.append(dict(territory=group['territory']['name'],error_type=type(exc).__name__))
        usable=bool(results) and not unmatched
        return dict(status='modeled' if usable else 'partial',lower=lower if usable else None,
            upper=upper if usable else None,point=points if usable else None,
            territories=results,building_estimates=building_estimates,unmatched_buildings=sorted(unmatched),errors=errors,
            modeled=True,usable=usable,range_kind='capacity_allocation_scenarios_not_confidence_interval')
