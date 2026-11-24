"""Recognize literal numeric search predicates already enforced by the caller.

These are units and typed search fields, not business categories. A phrase
containing any additional condition remains unhandled as a whole.
"""
import math
import re

from .storage import haversine_distance

NUMBER=r'\d+(?:[ \u00a0]\d{3})*(?:[.,]\d+)?'
AREA=r'(?:м\s*[²2]|кв\.?\s*м\.?)'


def number(value):
    return float(value.replace(' ','').replace('\u00a0','').replace(',','.'))


def finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def literal_filter_checks(quote,constraints):
    if not constraints:return []
    parts=[p.strip(' .:') for p in re.split(r'[,;](?!\d)|\s+и\s+',quote.casefold()) if p.strip(' .:')]
    checks=[]
    for part in parts:
        area=re.fullmatch(r'(?:площадь\s*)?(?:от\s*)?('+NUMBER+r')\s*(?:[–—-]|до)\s*('+NUMBER+r')\s*'+AREA,part)
        if area:
            low,high=map(number,area.groups());applied_low=constraints.get('min_area_sqm');applied_high=constraints.get('max_area_sqm')
            if not (finite(low) and finite(high) and 0<low<high and finite(applied_low) and finite(applied_high) and low<=applied_low<=applied_high<=high):return []
            checks.append(dict(field='area_sqm',lower=low,upper=high));continue
        rent=re.fullmatch(r'(?:аренда|арендная плата)\s+(?:до|не выше|не более|максимум|≤)\s*('+NUMBER+
            r')\s*(тыс\.?|тысяч\w*)?\s*(?:руб(?:лей|ля|ль|\.)?|₽)\s*(?:в\s+месяц|/\s*мес\.?)',part)
        if rent:
            high=number(rent[1])*(1000 if rent[2] else 1);applied=constraints.get('max_price_month')
            if not (finite(high) and finite(applied) and 0<applied<=high):return []
            checks.append(dict(field='price_month',lower=0,upper=high));continue
        distance=re.fullmatch(r'(?:в пределах|радиус(?:ом|е)?|не далее|не дальше)\s*('+NUMBER+
            r')\s*(км|километр\w*|м|метр\w*)\s+от\s+(.+)',part)
        if distance:
            high=number(distance[1])*(1000 if distance[2].startswith(('км','километр')) else 1)
            applied=constraints.get('search_radius_m');origin=distance[3].strip();query=str(constraints.get('location_query') or '').casefold().strip()
            origin_matches=(origin==query and query!='метро' or origin=='метро' and query.startswith('метро ') or origin in ('центра поиска','точки поиска'))
            if not (origin_matches and finite(high) and finite(applied) and 0<applied<=high and
                    finite(constraints.get('center_lat')) and finite(constraints.get('center_lng'))):return []
            checks.append(dict(field='distance_m',lower=0,upper=high));continue
        return []
    return checks


def filters_hold(checks,properties,constraints):
    if not checks or not properties:return False
    for prop in properties:
        for check in checks:
            if check['field']=='distance_m' and not all(finite(prop.get(k)) for k in ('lat','lng')):return False
            value=(haversine_distance(constraints['center_lat'],constraints['center_lng'],prop.get('lat'),prop.get('lng'))
                   if check['field']=='distance_m' else prop.get(check['field']))
            if not finite(value) or not check['lower']-1e-7<=value<=check['upper']+1e-7:return False
    return True
