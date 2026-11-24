"""Real client chooses to rank previously unverified photo-studio listings.

Use the client's recently collected inventory without changing freshness,
premises facts, constraints or ranking weights. Profile and environment use
the normal production service; no prior AI profile is replayed.
"""
import asyncio
import hashlib
import json
import logging
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from two_integrated_runs import TracedAnalyzer,event,save
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine import RealEstateAggregator,OptimalPropertyRecommender
from real_estate_engine.confirmation_flow import property_key


async def main():
    base=ROOT/'audit/optional_analysis'
    out=base/datetime.now().strftime('run_%Y%m%d_%H%M%S');out.mkdir(parents=True)
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    source=ROOT/'audit/client_photo/run_20261105_170909'
    request=json.loads((source/'client_request.json').read_text(encoding='utf-8'))
    location=json.loads((source/'location.json').read_text(encoding='utf-8'))
    save(out/'client_request.json',request);save(out/'location.json',location)
    target=out/'inventory.sqlite3'
    with sqlite3.connect((source/'fresh_inventory.sqlite3').as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(target) as dst:src.backup(dst)
    save(out/'inventory_origin.json',dict(path=str(source/'fresh_inventory.sqlite3'),mode='recent_inventory_preserved_no_freshness_rewrite'))
    original=ROOT/'data/moscow_commercial.db';fingerprint=hashlib.sha256(original.read_bytes()).hexdigest()
    agg=RealEstateAggregator(str(target));analyzer=TracedAnalyzer(out)
    agg.competitor_analyzer=analyzer;agg.recommender=OptimalPropertyRecommender(agg.storage,analyzer)
    budget=NetworkBudget(120);analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    parameters=dict(target_business=request['business'],location_query=request['location'],
        center_lat=location['lat'],center_lng=location['lng'],search_radius_m=request['radius_m'],
        min_area_sqm=request['min_area'],max_area_sqm=request['max_area'],max_price_month=request['max_rent'],
        business_concept=request['concept'],allowed_floors=None,max_listing_age_hours=24,max_candidates=3,
        environment_timeout_s=180,fns_timeout_s=30)
    started=time.perf_counter();analyzer.current_case='strict_before_client_choice'
    strict=await agg.recommender.find_optimal_properties(**parameters)
    save(out/'strict_search.json',asdict(strict))
    if strict.ranking_profile is not None and strict.ranking_profile.status!='ready':
        event('profile_retry',status=strict.ranking_profile.status)
        analyzer.current_case='explicit_profile_retry'
        strict=await agg.recommender.find_optimal_properties(**parameters)
        save(out/'strict_retry.json',asdict(strict))
    event('strict_result',status=strict.selection_status,ranked=len(strict.ranked_candidates),pending=len(strict.floor_verification_candidates))
    choice=next((a for a in strict.next_actions if a['type']=='continue_unverified'),None)
    continued=None
    if choice:
        save(out/'client_choice.json',dict(action=choice,selected=True,
            explanation='Client explicitly asks to continue provisional comparison; unknown conditions remain unknown'))
        analyzer.current_case='after_client_choice'
        continued=await agg.continue_unverified_analysis(strict)
        save(out/'continued_search.json',asdict(continued))
        event('continued_result',status=continued.selection_status,evaluated=continued.evaluated_count,
            ranked=len(continued.ranked_candidates),unverified=continued.unverified_evaluated_count,
            confirmation=continued.confirmation_status)
    peak=max((sum(r['start']<=v['start']<r['start']+60 for v in budget.records) for r in budget.records),default=0)
    original_keys={property_key(c.property_data) for c in strict.floor_verification_candidates}
    new_keys={property_key(c.property_data) for c in continued.ranked_candidates} if continued else set()
    checks=dict(
        strict_did_not_rank_uncertain=not strict.ranked_candidates and bool(strict.floor_verification_candidates),
        explicit_action_offered=choice is not None,
        explicit_action_taken=bool(continued and continued.include_unverified),
        same_pending_listings_ranked=bool(new_keys) and new_keys.issubset(original_keys),
        environment_started=bool(analyzer.environment_records),
        score_produced=bool(continued and continued.evaluated_count),
        all_ranked_still_require_confirmation=bool(continued and continued.ranked_candidates) and all(
            c.confirmation['status']=='requires_confirmation' for c in continued.ranked_candidates),
        admission_not_rewritten=bool(continued and continued.ranked_candidates) and all(
            c.floor_assessment.status=='needs_verification' for c in continued.ranked_candidates),
        no_claim_of_complete_comparison=bool(continued and not continued.comparison_complete),
        constraints_preserved=bool(continued and all(getattr(strict,k)==getattr(continued,k) for k in (
            'center_lat','center_lng','search_radius_m','client_min_area_sqm','client_max_area_sqm','client_max_price_month'))),
        rate_limit_respected=peak<=40,
        original_database_unchanged=hashlib.sha256(original.read_bytes()).hexdigest()==fingerprint,
    )
    rows=[dict(address=c.property_data['address'],area=c.property_data['area_sqm'],rent=c.property_data['price_month'],
        floor=c.property_data['floor'],url=c.property_data['url'],score_low=c.unrounded_total_score,
        score_high=c.score_upper_bound,coverage=c.score_coverage,confirmation=c.confirmation['status'],
        environment=c.competition_report.data_status,organizations=c.competition_report.total_cards_found)
        for c in continued.ranked_candidates] if continued else []
    result=dict(scope=__doc__,checks=checks,seconds=round(time.perf_counter()-started,2),
        strict_status=strict.selection_status,continued_status=continued.selection_status if continued else None,
        candidates=rows,http_attempts=len(budget.records),peak_rpm=peak,http=budget.records,
        environment_records=analyzer.environment_records)
    save(out/'result.json',result);event('optional_analysis_complete',directory=str(out),seconds=result['seconds'],
        checks=checks,candidates=rows,http_attempts=result['http_attempts'],peak_rpm=peak)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
