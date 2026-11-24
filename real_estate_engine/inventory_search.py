"""Bounded collection driven by the client's unchanged numeric search.

Operational depth/size limits are not market coverage or business suitability.
"""
import asyncio
import copy
import math

from .confirmation_flow import property_keys
from .cities import get_city, city_in_text


SCOPE_FIELDS=('city','location_query','center_lat','center_lng','search_radius_m','target_area_sqm',
              'min_area_sqm','max_area_sqm','max_price_month','max_price_sqm',
              'max_price_sqm_year','price_sqm_period','max_listing_age_hours','exclude_property_keys')


def request_scope(request):
    result={k:copy.deepcopy(request.get(k)) for k in SCOPE_FIELDS}
    result['city']=get_city(result['city']).key
    result['location_query']=result['location_query'] or ''
    result['price_sqm_period']=result['price_sqm_period'] or 'month'
    result['exclude_property_keys']=sorted(set(result['exclude_property_keys'] or ()))
    return result


async def collect_for_search(aggregator, request, *, initial_limit=30,
                             max_limit=600, target_matches=12, ceiling=2400):
    for value in (initial_limit,max_limit,target_matches,ceiling):
        if type(value) is not int or value<1:raise ValueError('Collection limits must be positive integers')
    if not initial_limit<=max_limit<=ceiling:raise ValueError('Invalid collection depth bounds')
    from .recommender import LocationResolver, OptimalPropertyRecommender
    city = get_city(getattr(aggregator, 'city', None))
    if request.get('city') is not None and get_city(request['city']) != city:
        raise ValueError('Город сохранённого запроса не совпадает с городом сбора')
    request = dict(request, city=city.key, location_query=request.get('location_query') or city.name)
    named_city = city_in_text(request['location_query'])
    if named_city and named_city != city:
        raise ValueError('Город запроса не совпадает с выбранным городом сбора')
    latitude,longitude=request.get('center_lat'),request.get('center_lng')
    radius=request.get('search_radius_m')
    if (latitude is None)!=(longitude is None):raise ValueError('Укажите обе координаты центра')
    if latitude is None:
        latitude,longitude,radius,_,keywords=await asyncio.to_thread(
            LocationResolver.resolve,request['location_query'],radius,aggregator.proxy,city)
    else:
        keywords=[]
        if radius is None:radius=2500.0
    if (any(type(v) not in (int,float) or not math.isfinite(v) for v in (latitude,longitude,radius))
            or not -90<latitude<90 or not -180<=longitude<=180 or radius<=0):
        raise ValueError('Некорректная география сбора')
    if not city.contains(latitude, longitude):
        raise ValueError('Центр поиска вне выбранного города сбора')
    lower,upper=request.get('min_area_sqm'),request.get('max_area_sqm')
    area=request.get('target_area_sqm')
    if area is not None:
        if type(area) not in (int,float) or not math.isfinite(area) or area<=0:raise ValueError('Invalid target area')
        if lower is None:lower=round(max(8.0,area*.65),1)
        if upper is None:upper=round(area*1.35,1)
    _,year=OptimalPropertyRecommender.normalize_price_sqm(request.get('max_price_sqm'),
        request.get('max_price_sqm_year'),request.get('price_sqm_period','month'))
    for value in (lower,upper,request.get('max_price_month'),year):
        if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or value<=0):
            raise ValueError('Некорректные числовые ограничения сбора')
    if lower is not None and upper is not None and lower>upper:raise ValueError('Invalid area bounds')
    query=dict(target_lat=latitude,target_lng=longitude,radius_meters=radius,address_keywords=keywords,
        min_area_sqm=lower,max_area_sqm=upper,max_price_month=request.get('max_price_month'),
        max_price_sqm_year=year,max_listing_age_hours=(request['max_listing_age_hours']
            if request.get('max_listing_age_hours') is not None else 24))
    exclusions=request.get('exclude_property_keys')
    if exclusions is not None and (not isinstance(exclusions,(list,tuple))
            or any(not isinstance(k,str) or not k for k in exclusions)):
        raise ValueError('Invalid excluded property keys')
    excluded=set(exclusions or ())
    def matches():
        return [p for p in aggregator.storage.find_candidates(**query)
                if not excluded.intersection(property_keys(p))]
    matches()  # Validate the storage query before any network mutation.
    rounds=[];depth=initial_limit
    while True:
        batch=await aggregator.run_pipeline(limit_per_source=depth)
        count=len(matches())
        rounds.append(dict(limit_per_source=depth,fresh_matches=count,
            batch_collected=batch['batch_collected'],batch_saved_or_updated=batch['batch_saved_or_updated'],
            source_results=copy.deepcopy(batch['source_results'])))
        unavailable=not any(v['status'] in ('ok','empty','partial') for v in batch['source_results'].values())
        if count>=target_matches or depth>=max_limit or unavailable:break
        depth=min(max_limit,depth*4)
    state=dict(city=city.key,rounds=rounds,limit_per_source=depth,ceiling=ceiling,
        next_limit=min(ceiling,depth*2) if depth<ceiling else None,
        target_matches=target_matches,fresh_numerical_matches=count,coverage_complete=False,
        stop_reason='sources_unavailable' if unavailable else 'sample_target' if count>=target_matches else 'depth_limit',
        search_query=query,request_scope=request_scope(request),excludes=sorted(excluded),
        message='Проверена ограниченная выборка объявлений. Полнота рынка не подтверждена.')
    aggregator._inventory_collection=copy.deepcopy(state)
    return dict(**batch,inventory_search=state)


def attach_collection(report,state):
    if not state:return report
    report.inventory_collection=copy.deepcopy(state)
    report.inventory_collection['remaining_numerical_matches']=report.numerical_matching_count
    message=state['message']
    if message not in report.ranking_warnings:report.ranking_warnings.append(message)
    if report.numerical_matching_count==0:
        report.confirmation_message='В просмотренной части источников новых вариантов с этими условиями нет.'
        if state.get('next_limit'):
            report.confirmation_message+=' Можно продолжить сбор без изменения требований.'
    return report
