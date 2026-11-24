"""A user-delegated practical preset through collection and the real search API.

Freshly retrieved listings and archived examples are distinguished explicitly.
No fabricated premises facts and no silent widening of geography or budget.
"""
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from pathlib import Path

import requests

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from two_integrated_runs import TracedAnalyzer,save,event
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine import RealEstateAggregator,OptimalPropertyRecommender,MoscowLocationResolver


PRESET=dict(business='Кофейня навынос',location='метро Белорусская',radius_m=2000,min_area=30,max_area=80,max_rent=250000,
    concept=('Кофейня навынос: продаём кофе покупателям с улицы, из соседних домов и офисов. '
        'Покупатели приходят пешком без записи. Нужен первый этаж и разрешённый вход покупателей к арендуемому помещению. '
        'Главный приоритет — доступная пешеходная аудитория и конкурентное окружение. '
        'Затем важны низкие полные расходы на помещение, включая эксплуатацию и подготовку. '
        'Площадь от 30 до 80 м², аренда не более 250 000 рублей в месяц.'))


def resolve():
    geo=MoscowLocationResolver._geocode_yandex_sync(PRESET['location'])
    if geo:return dict(lat=geo[0],lng=geo[1],title=geo[2],source='Yandex Maps live geocoder')
    # A single public geocoding request; no substitution of guessed coordinates.
    response=requests.get('https://nominatim.openstreetmap.org/search',params=dict(
        q='метро Белорусская Москва',format='jsonv2',addressdetails=1,limit=8),
        headers={'User-Agent':'CommercialPremisesEngine/0.1 (practical location lookup)'},timeout=(5,12))
    response.raise_for_status();rows=response.json()
    for row in rows:
        address=row.get('address',{});city=address.get('city','')
        if ('москв' in city.lower() or 'moscow' in city.lower()) and row.get('class')=='railway' and row.get('type') in ('station','subway_entrance','stop'):
            return dict(lat=float(row['lat']),lng=float(row['lon']),title=row['display_name'],
                source='OpenStreetMap Nominatim',osm_type=row.get('osm_type'),osm_id=row.get('osm_id'))
    raise ValueError('Station coordinates could not be verified; no guessed point used')


async def main():
    base=ROOT/'audit/practical_trial';out=base/datetime.now(timezone(timedelta(hours=3))).strftime('run_%Y%m%d_%H%M%S');out.mkdir(parents=True)
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    original=ROOT/'data/moscow_commercial.db';before_hash=hashlib.sha256(original.read_bytes()).hexdigest();target=out/'trial.sqlite3'
    with closing(sqlite3.connect(original.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(target)) as dst:src.backup(dst)
    os.environ['NVIDIA_MIN_REQUEST_INTERVAL_S']='2.5'
    agg=RealEstateAggregator(str(target));analyzer=TracedAnalyzer(out);analyzer.current_case='practical_trial'
    agg.competitor_analyzer=analyzer;agg.recommender=OptimalPropertyRecommender(agg.storage,analyzer)
    budget=NetworkBudget(80);analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    started=time.perf_counter();event('trial_started',preset=PRESET)
    location_task=asyncio.create_task(asyncio.to_thread(resolve))
    collection=await agg.run_pipeline(limit_per_source=30);save(out/'collection.json',collection)
    event('fresh_collection',collected=collection['batch_collected'],saved=collection['batch_saved_or_updated'],
        sources={k:dict(status=v['status'],collected=v['collected'],warnings=v['warnings']) for k,v in collection['source_results'].items()})
    location=await location_task;save(out/'preset.json',dict(PRESET,location_resolved=location));event('location',**location)
    query=dict(target_lat=location['lat'],target_lng=location['lng'],radius_meters=PRESET['radius_m'],
        min_area_sqm=PRESET['min_area'],max_area_sqm=PRESET['max_area'],max_price_month=PRESET['max_rent'])
    fresh=agg.storage.find_candidates(**query,max_listing_age_hours=24)
    all_rows=agg.storage.find_candidates(**query)
    origin='freshly_retrieved' if fresh else 'archived_examples_not_reconfirmed'
    save(out/'candidate_inventory.json',dict(origin=origin,fresh=fresh,all_candidates=all_rows))
    event('candidate_inventory',fresh=len(fresh),including_archive=len(all_rows),origin=origin)
    report=await agg.recommender.find_optimal_properties(PRESET['business'],center_lat=location['lat'],center_lng=location['lng'],
        search_radius_m=PRESET['radius_m'],min_area_sqm=PRESET['min_area'],max_area_sqm=PRESET['max_area'],
        max_price_month=PRESET['max_rent'],business_concept=PRESET['concept'],allowed_floors=(1,),
        max_listing_age_hours=24 if fresh else None,max_candidates=3,analysis_limit=8,
        ranking_timeout_s=90,floor_timeout_s=120,environment_timeout_s=240,fns_timeout_s=40)
    save(out/'search_report.json',asdict(report))
    peak=max((sum(r['start']<=v['start']<r['start']+60 for v in budget.records) for r in budget.records),default=0)
    result=dict(preset=PRESET,location=location,origin=origin,fresh_numerical_matches=len(fresh),all_numerical_matches=len(all_rows),
        seconds=round(time.perf_counter()-started,2),profile_status=report.ranking_profile.status if report.ranking_profile else None,
        selection_status=report.selection_status,numerical_matches=report.numerical_matching_count,
        eligible=report.total_matching_in_db,evaluated=report.evaluated_count,
        ranked=[dict(id=c.property_data['id'],address=c.property_data['address'],area=c.property_data['area_sqm'],
            rent=c.property_data['price_month'],url=c.property_data['url'],score_lower=c.unrounded_total_score,score_upper=c.score_upper_bound,
            known_organizations=c.competition_report.total_cards_found-len(c.competition_report.unclassified_businesses),
            unclassified_organizations=len(c.competition_report.unclassified_businesses)) for c in report.ranked_candidates],
        rejected=[dict(id=c.property_data['id'],address=c.property_data['address'],area=c.property_data['area_sqm'],rent=c.property_data['price_month'],
            floor=c.property_data['floor'],url=c.property_data['url'],reason=c.assessment.reason) for c in report.floor_rejected_candidates],
        needs_verification=[dict(id=c.property_data['id'],address=c.property_data['address'],area=c.property_data['area_sqm'],rent=c.property_data['price_month'],
            floor=c.property_data['floor'],url=c.property_data['url'],reason=c.assessment.reason,missing=c.assessment.missing_facts) for c in report.floor_verification_candidates],
        missing_inputs=report.missing_inputs,analysis_errors=report.analysis_errors,stage_timings=report.stage_timings_ms,
        http_attempts=len(budget.records),peak_rpm=peak,http=budget.records,original_database_unchanged=before_hash==hashlib.sha256(original.read_bytes()).hexdigest())
    save(out/'result.json',result);event('practical_trial_completed',directory=str(out),**{k:v for k,v in result.items() if k not in ('preset','http','missing_inputs')})


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
