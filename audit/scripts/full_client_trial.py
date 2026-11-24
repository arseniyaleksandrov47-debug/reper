"""Fresh client journey: empty inventory, cold scenario, search and find-more.

No archived-listing fallback, replayed AI profile, narrowed analysis shortlist,
invented premises facts, widened client constraints or third-party contact.
Explicit --continue-unverified exercises the client's provisional-analysis option.
"""
import asyncio
import argparse
import hashlib
import json
import logging
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit/scripts'))
from two_integrated_runs import TracedAnalyzer, event, save
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine import RealEstateAggregator, OptimalPropertyRecommender, LocationResolver
from real_estate_engine.cities import get_city
from real_estate_engine.confirmation_flow import property_key


REQUEST = dict(
    business='Кофейня навынос', location='метро Курская', radius_m=2000,
    min_area=30, max_area=80, max_rent=250000, allowed_floors=[1], location_title_contains='курск',
    concept=(
        'Хочу открыть кофейню навынос у метро Курская. Продаём кофе и готовую выпечку '
        'покупателям с улицы, из соседних домов и офисов. Покупатели приходят пешком '
        'без записи с 08:00 до 20:00. Нужен первый этаж и свободный разрешённый вход '
        'любых покупателей непосредственно в помещение в эти часы. '
        'Главный приоритет — доступная пешеходная аудитория с учётом маршрутов '
        'и прямых и косвенных конкурентов. Затем важны низкие полные расходы на '
        'помещение, включая эксплуатацию и подготовку. Площадь от 30 до 80 м², '
        'аренда не более 250 000 рублей в месяц. География — 2 км от метро Курская.'
    ),
)


def resolve_location(request=None):
    request=REQUEST if request is None else request
    city = get_city(request.get('city'))
    found = LocationResolver._geocode_yandex_sync(request['location'], city=city)
    expected=request.get('location_title_contains')
    if not found or (expected and expected.casefold() not in found[2].casefold()):
        found = LocationResolver._geocode_nominatim_sync(request['location'], city=city)
        if not found or (expected and expected.casefold() not in found[2].casefold()):
            raise ValueError('Cannot verify requested station; no substitute center used')
        lat, lng, title = found
        return dict(lat=lat, lng=lng, title=title, city=city.key,
            source='OpenStreetMap Nominatim live geocoder',primary_geocoder='unavailable_or_unmatched')
    lat, lng, title = found
    if not city.contains(lat, lng):
        raise ValueError('Geocoder returned a point outside selected city envelope')
    return dict(lat=lat, lng=lng, title=title, city=city.key, source='Yandex Maps live geocoder')


def shown_properties(report):
    return [c.property_data for c in (
        list(report.ranked_candidates) + list(report.comparable_candidates)
        + list(report.floor_verification_candidates) + list(report.floor_rejected_candidates)
    )] + [c['property_data'] for c in report.confirmation_rejected_candidates]


def compact(report):
    return dict(
        city=report.city,
        seconds=round(report.total_search_time_ms / 1000, 2),
        profile_status=report.ranking_profile.status if report.ranking_profile else None,
        numerical_matches=report.numerical_matching_count,
        eligible=report.total_matching_in_db, evaluated=report.evaluated_count,
        ranked=len(report.ranked_candidates), pending=len(report.floor_verification_candidates),
        rejected=len(report.floor_rejected_candidates),
        source_rejected=len(report.confirmation_rejected_candidates),
        selection_status=report.selection_status, confirmation_status=report.confirmation_status,
        comparison_complete=report.comparison_complete,
        data_complete=getattr(report,'data_complete',False),recommendation=getattr(report,'recommendation',{}),
        analysis_errors=report.analysis_errors, stage_timings_ms=report.stage_timings_ms,
        candidates=[dict(
            source=c.property_data['source'], external_id=c.property_data['external_id'],
            address=c.property_data['address'], area=c.property_data['area_sqm'],
            rent=c.property_data['price_month'], url=c.property_data['url'],
            score_low=c.unrounded_total_score, score_high=c.score_upper_bound,
            score_coverage=c.score_coverage, confirmation=c.confirmation,
        ) for c in report.ranked_candidates],
    )


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit-per-source', type=int, default=None)
    parser.add_argument('--output-base', type=Path, default=Path('audit/client_trial'))
    parser.add_argument('--resume-inventory',type=Path,default=None,
                        help='Retry the same client using a saved recent inventory, without replaying the AI profile')
    parser.add_argument('--request-file',type=Path,default=None)
    parser.add_argument('--continue-unverified',action='store_true')
    parser.add_argument('--skip-find-more',action='store_true')
    parser.add_argument('--serialize-actions',action='store_true',
        help='Replay JSON actions in a new recommender configured from the saved city')
    args = parser.parse_args()
    request=json.loads((ROOT/args.request_file).read_text(encoding='utf-8')) if args.request_file else REQUEST
    out = ROOT / args.output_base / datetime.now().strftime('run_%Y%m%d_%H%M%S')
    out.mkdir(parents=True)
    (out.parent / 'LATEST.txt').write_text(str(out), encoding='utf-8')
    save(out / 'client_request.json', request)
    original = ROOT / 'data/moscow_commercial.db'
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    started = time.perf_counter()
    event('client_started', directory=str(out), request=request, limit_per_source=args.limit_per_source)
    target=out / 'fresh_inventory.sqlite3'
    if args.resume_inventory:
        origin=(ROOT / args.resume_inventory).resolve()
        with sqlite3.connect(origin.as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(target) as dst:
            src.backup(dst)
        save(out/'inventory_origin.json',dict(path=str(origin),mode='recent_inventory_preserved_no_freshness_rewrite'))
    city = get_city(request.get('city'))
    agg = RealEstateAggregator(str(target), city=city)
    analyzer = TracedAnalyzer(out)
    analyzer.current_case = 'fresh_client'
    agg.competitor_analyzer = analyzer
    agg.recommender = OptimalPropertyRecommender(agg.storage, analyzer, city=city)
    budget = NetworkBudget(240)
    analyzer.http_session.post = budget.wrap(analyzer.http_session.post)
    location_task = asyncio.create_task(asyncio.to_thread(resolve_location,request))
    if args.resume_inventory:
        collection=json.loads((origin.parent/'collection.json').read_text(encoding='utf-8'))
        if collection.get('inventory_search'):agg._inventory_collection=collection['inventory_search']
        event('inventory_resumed',origin=str(origin),ai_profile_replayed=False)
    elif args.limit_per_source is not None:
        collection = await agg.run_pipeline(limit_per_source=args.limit_per_source)
    else:
        location = await location_task
        collection = await agg.collect_for_search(dict(location_query=request['location'],
            center_lat=location['lat'],center_lng=location['lng'],search_radius_m=request['radius_m'],
            min_area_sqm=request['min_area'],max_area_sqm=request['max_area'],max_price_month=request['max_rent'],
            max_listing_age_hours=24),target_matches=12)
    save(out / 'collection.json', collection)
    event('collection_complete', seconds=round(time.perf_counter()-started, 2),
          collected=collection['batch_collected'], saved=collection['batch_saved_or_updated'],
          quarantined=collection['batch_quarantined'], status=collection['data_status'],
          sources=collection['source_results'])
    if collection.get('inventory_search'):
        event('adaptive_collection',rounds=[{k:r[k] for k in ('limit_per_source','fresh_matches','batch_collected')}
            for r in collection['inventory_search']['rounds']],stop_reason=collection['inventory_search']['stop_reason'])
    location = await location_task
    save(out / 'location.json', location)
    event('location_verified', **location)
    inventory = agg.storage.find_candidates(
        target_lat=location['lat'], target_lng=location['lng'], radius_meters=request['radius_m'],
        min_area_sqm=request['min_area'], max_area_sqm=request['max_area'],
        max_price_month=request['max_rent'], max_listing_age_hours=24,
    )
    save(out / 'fresh_matches.json', inventory)
    event('inventory_filtered', matches=len(inventory), properties=[dict(
        address=p['address'], area=p['area_sqm'], rent=p['price_month'], floor=p['floor'],
        source=p['source'], external_id=p['external_id']) for p in inventory])
    report = await agg.find_optimal_properties(
        request['business'], location_query=request['location'],
        center_lat=location['lat'], center_lng=location['lng'],
        search_radius_m=request['radius_m'], min_area_sqm=request['min_area'],
        max_area_sqm=request['max_area'], max_price_month=request['max_rent'],
        business_concept=request['concept'], allowed_floors=(tuple(request['allowed_floors'])
            if request.get('allowed_floors') is not None else None),
        max_listing_age_hours=24, max_candidates=3,
    )
    save(out / 'first_search.json', asdict(report))
    def action_actor(current, name):
        if not args.serialize_actions:
            return current
        saved=json.loads(json.dumps(asdict(current),ensure_ascii=False,default=str))
        save(out/(name+'_serialized.json'),saved)
        restored_city=get_city(saved['city'])
        if restored_city != agg.city:
            raise ValueError('Saved city does not match the collection client')
        agg.recommender=OptimalPropertyRecommender(agg.storage,analyzer,city=restored_city)
        event('serialized_action_restored',action=name,city=restored_city.key)
        return SimpleNamespace(city=saved['city'],next_actions=saved['next_actions'],
            inventory_collection=saved['inventory_collection'])
    if args.continue_unverified and any(action.get('type')=='continue_unverified' for action in report.next_actions):
        event('client_continues_unverified',constraints_preserved=True)
        report=await agg.continue_unverified_analysis(action_actor(report,'continue_unverified'))
        save(out/'continued_search.json',asdict(report))
    first = compact(report)
    event('first_search_complete', **{k:v for k,v in first.items() if k != 'candidates'})
    second = None
    if not args.skip_find_more and any(action.get('type') == 'find_more' for action in report.next_actions):
        analyzer.current_case = 'client_find_more'
        second = await agg.find_more_options(action_actor(report,'find_more'))
        save(out / 'next_search.json', asdict(second))
        event('find_more_complete', **{k:v for k,v in compact(second).items() if k != 'candidates'})
    peak = max((sum(r['start'] <= v['start'] < r['start'] + 60 for v in budget.records)
                for r in budget.records), default=0)
    initial_keys = {property_key(p) for p in shown_properties(report)}
    next_keys = {property_key(p) for p in shown_properties(second)} if second else set()
    considered = [*shown_properties(report),
        *(candidate.property_data for candidate in getattr(report, '_evaluated_candidates', []))]
    considered_keys = {property_key(prop) for prop in considered}
    checks = dict(
        original_database_unchanged=original_hash == hashlib.sha256(original.read_bytes()).hexdigest(),
        all_numeric_matches_considered=(report.numerical_matching_count == len(inventory)
            and all(property_key(prop) in considered_keys for prop in inventory)),
        rpm_within_40=peak <= 40,
        no_repeated_options=None if second is None else not bool(initial_keys & next_keys),
        city_preserved=report.city==city.key and (second is None or second.city==city.key),
        pending_labeled=all(c.confirmation.get('label') == 'Требует подтверждения'
            for c in report.ranked_candidates if c.confirmation.get('status') == 'requires_confirmation'),
        next_constraints_preserved=None if second is None else bool((
            second.city, second.center_lat, second.center_lng, second.search_radius_m,
            second.client_min_area_sqm, second.client_max_area_sqm, second.client_max_price_month,
        ) == (
            report.city, report.center_lat, report.center_lng, report.search_radius_m,
            report.client_min_area_sqm, report.client_max_area_sqm, report.client_max_price_month,
        )),
    )
    scope = (__doc__ if not args.resume_inventory else
        'Client recheck of explicitly preserved recent inventory; no new collection or freshness rewrite. '
        'Cold AI scenario, live environment and source verification; provisional continuation only if requested.')
    result = dict(scope=scope, request=request, location=location,
        seconds=round(time.perf_counter()-started, 2), first=first,
        next=compact(second) if second else None, checks=checks,
        http_attempts=len(budget.records), peak_rpm=peak, http=budget.records,
        environment_records=analyzer.environment_records)
    save(out / 'result.json', result)
    event('client_journey_complete', directory=str(out), seconds=result['seconds'],
          checks=checks, http_attempts=len(budget.records), peak_rpm=peak)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
