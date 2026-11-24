"""Recover received unit evidence before admission and propagate fresh facts."""
import asyncio
import copy
import json
import math
import time

from .ai_contract import AI_DEADLINE
from .collectors.avito import catalogue_description
from .collectors.base import extract_specs_from_text
from .floor_policy import extract_floor_details,parse_floor_values,_floor_mentions
from .property_sources import raw_attributes


def _store(prop,raw):
    key='raw_attributes' if isinstance(prop.get('raw_attributes'),dict) else 'raw_json'
    prop[key]=raw if key=='raw_attributes' else json.dumps(raw,ensure_ascii=False)


def recover_catalogue_details(prop):
    prop=copy.deepcopy(prop);raw=copy.deepcopy(raw_attributes(prop));item=raw.get('source_payload',{})
    if prop.get('source')!='avito' or not isinstance(item,dict) or str(item.get('id'))!=str(prop.get('external_id')):return prop
    description=catalogue_description(item)
    if not description or prop.get('description') and not description.startswith(prop['description']):return prop
    prop['description']=description
    details=extract_floor_details(item.get('floor',prop.get('floor')),title=prop.get('title',''),description=description)
    raw.update(details);prop['floor']=details['floor']
    specs=extract_specs_from_text(description)
    if specs['has_separate_entrance']:prop['has_separate_entrance']=True
    if prop.get('power_kw') is None and specs['power_kw'] is not None:prop['power_kw']=specs['power_kw']
    raw['catalogue_detail_recovery']=dict(unit_id=str(item['id']),path='source_payload.iva.DescriptionStep',
        source_fetched_at=raw.get('source_fetched_at'))
    _store(prop,raw);return prop


def fresh_sources(bundle):
    return [s for s in bundle.get('sources',[]) if s.get('identity_checked') is True
        and type(s.get('fetched_at')) in (int,float) and math.isfinite(s['fetched_at'])
        and 0<=time.time()-s['fetched_at']<=300]


def merge_unit_sources(prop,sources):
    """A bound, current unit source may fill/change facts, never assert admission."""
    prop=copy.deepcopy(prop);raw=copy.deepcopy(raw_attributes(prop));changes={}
    if any(s.get('auction_start_only') for s in sources) and not prop.get('rent_price_lower_bound'):
        changes['rent_price_lower_bound']=dict(previous=prop.get('rent_price_lower_bound'),current=True)
        prop['rent_price_lower_bound']=True
    for field in ('area_sqm','price_month'):
        values={s[field] for s in sources if type(s.get(field)) in (int,float) and math.isfinite(s[field])
                and s[field]>0 and not (field=='price_month' and s.get('auction_start_only'))}
        if len(values)==1:
            value=next(iter(values))
            if value!=prop.get(field):changes[field]=dict(previous=prop.get(field),current=value);prop[field]=value
    floors=set()
    for source in sources:
        value=source.get('floor')
        floors.update(parse_floor_values(value) or _floor_mentions(str(value or '').casefold()))
    if len(floors)==1:
        value=next(iter(floors))
        if value!=prop.get('floor'):changes['floor']=dict(previous=prop.get('floor'),current=value);prop['floor']=value
    elif len(floors)>1:
        changes['floor']=dict(previous=prop.get('floor'),current=None,conflicting_sources=sorted(floors));prop['floor']=None
    text=[]
    for source in sources:
        facts=source.get('facts',{})
        for name in ('description','listing_parameters','access','destination','permittedUse'):
            value=facts.get(name)
            if isinstance(value,str) and value.strip() and value.strip() not in text:text.append(value.strip())
    if text:
        description='\n\n'.join(text)
        if description!=prop.get('description'):
            changes['description']=dict(previous=prop.get('description'),current=description);prop['description']=description
    if changes:
        raw.update(extract_floor_details(prop.get('floor'),offered_floors=sorted(floors) or None,
            title=prop.get('title',''),description=prop.get('description','')))
        if len(floors)>1:raw['floor_conflicting']=True
        prop['floor']=raw['floor']
        raw['verified_unit_sources']=[dict(url=s.get('url'),unit_id=s.get('unit_id'),fetched_at=s['fetched_at'],facts=s.get('facts',{})) for s in sources]
        if all(type(prop.get(k)) in (int,float) and prop[k]>0 for k in ('price_month','area_sqm')):
            prop['price_sqm_year']=prop['price_month']/prop['area_sqm']*12
        _store(prop,raw)
    return prop,changes


def numerical_violations(prop,constraints):
    result=[]
    for field,key,minimum in (('area_sqm','min_area_sqm',True),('area_sqm','max_area_sqm',False),
            ('price_month','max_price_month',False),('price_sqm_year','max_price_sqm_year',False)):
        value=prop.get(field);bound=constraints.get(key)
        if (type(value) in (int,float) and math.isfinite(value) and bound is not None
                and (value<bound if minimum else value>bound)):
            result.append(dict(field=field,actual=value,constraint=key,bound=bound))
    return result


async def enrich_missing_unit_details(properties,analyzer,*,timeout_s=20):
    source=getattr(getattr(analyzer,'property_verifier',None),'sources',None)
    if not callable(getattr(source,'collect',None)):return properties,[]
    result=list(properties);records=[];semaphore=asyncio.Semaphore(2)
    deadline=time.monotonic()+timeout_s
    async def one(index,prop):
        if prop.get('floor') is not None:return
        async with semaphore:
            left=deadline-time.monotonic()
            if left<=0:return
            token=AI_DEADLINE.set(deadline)
            try:
                bundle=await asyncio.wait_for(asyncio.to_thread(source.collect,prop),left)
                sources=fresh_sources(bundle)
                if sources:
                    result[index],changes=merge_unit_sources(prop,sources)
                    records.append(dict(property_id=prop.get('id'),status='enriched' if changes else 'unchanged',fields=list(changes)))
                else:records.append(dict(property_id=prop.get('id'),status='source_unavailable',errors=bundle.get('errors',[])))
            except Exception as exc:records.append(dict(property_id=prop.get('id'),status='unavailable',error_type=type(exc).__name__))
            finally:AI_DEADLINE.reset(token)
    await asyncio.gather(*(one(i,p) for i,p in enumerate(properties)))
    return result,records
