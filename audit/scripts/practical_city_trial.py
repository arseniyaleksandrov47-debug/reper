"""A clearly separated wider demo using freshly collected first-floor offers.

Original Belorusskaya search had no fresh numerical matches. This comparison
uses Moscow-wide geography and only offers with an explicitly recorded first
floor; unknown floors are deferred, not rejected or quietly deemed unsuitable.
"""
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from practical_trial import PRESET
from two_integrated_runs import TracedAnalyzer,save,event
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine import PropertyStorage,OptimalPropertyRecommender


async def main():
    origin=Path((ROOT/'audit/practical_trial/LATEST.txt').read_text('utf-8'));out=origin/('city_comparison_'+datetime.now().strftime('%H%M%S'));out.mkdir()
    (origin/'CITY_LATEST.txt').write_text(str(out),encoding='utf-8')
    target=out/'explicit_first_floor.sqlite3'
    with closing(sqlite3.connect((origin/'trial.sqlite3').as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        dst.row_factory=sqlite3.Row
        numeric=[dict(r) for r in dst.execute("SELECT * FROM properties WHERE last_seen_at>=datetime('now','-24 hours') AND area_sqm BETWEEN 30 AND 80 AND price_month<=250000")]
        with dst:
            dst.execute("DELETE FROM properties WHERE last_seen_at IS NULL OR last_seen_at<datetime('now','-24 hours') OR area_sqm<30 OR area_sqm>80 OR price_month>250000 OR floor IS NULL OR floor!=1")
    save(out/'candidate_selection.json',dict(scope=__doc__,considered=[r for r in numeric if r['floor']==1],
        deferred_unknown_floor=[r for r in numeric if r['floor'] is None],known_other_floor=[r for r in numeric if r['floor'] not in (None,1)]))
    store=PropertyStorage(str(target));os.environ['NVIDIA_MIN_REQUEST_INTERVAL_S']='2.5'
    analyzer=TracedAnalyzer(out);analyzer.current_case='city_comparison';budget=NetworkBudget(70)
    analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    engine=OptimalPropertyRecommender(store,analyzer)
    original=engine.ranking_profile_resolver.resolve
    async def retry_profile(*args,**kwargs):
        result=await original(*args,**kwargs)
        if result.status!='ready' and result.metadata.get('contract_error'):
            save(out/'rejected_profile.json',asdict(result));event('profile_retry',error=result.metadata['contract_error'])
            result=await original(*args,**kwargs)
        return result
    engine.ranking_profile_resolver.resolve=retry_profile
    started=time.perf_counter();event('city_comparison_started',known_first_floor=sum(r['floor']==1 for r in numeric),scope=__doc__)
    report=await engine.find_optimal_properties(PRESET['business'],location_query='Москва',search_radius_m=45000,
        min_area_sqm=30,max_area_sqm=80,max_price_month=250000,business_concept=PRESET['concept'],allowed_floors=(1,),
        max_listing_age_hours=24,max_candidates=3,ranking_timeout_s=90,floor_timeout_s=120,environment_timeout_s=240,fns_timeout_s=40)
    save(out/'search_report.json',asdict(report))
    peak=max((sum(r['start']<=v['start']<r['start']+60 for v in budget.records) for r in budget.records),default=0)
    result=dict(scope=__doc__,seconds=round(time.perf_counter()-started,2),profile_status=report.ranking_profile.status if report.ranking_profile else None,
        selection_status=report.selection_status,numerical_matches=report.numerical_matching_count,evaluated=report.evaluated_count,
        ranked=[dict(id=c.property_data['id'],address=c.property_data['address'],area=c.property_data['area_sqm'],rent=c.property_data['price_month'],
            url=c.property_data['url'],score_lower=c.unrounded_total_score,score_upper=c.score_upper_bound,coverage=c.score_coverage,
            organizations=c.competition_report.total_cards_found,unclassified=len(c.competition_report.unclassified_businesses)) for c in report.ranked_candidates],
        rejected=[dict(id=c.property_data['id'],address=c.property_data['address'],area=c.property_data['area_sqm'],rent=c.property_data['price_month'],
            url=c.property_data['url'],reason=c.assessment.reason) for c in report.floor_rejected_candidates],
        needs_verification=[dict(id=c.property_data['id'],address=c.property_data['address'],area=c.property_data['area_sqm'],rent=c.property_data['price_month'],
            url=c.property_data['url'],reason=c.assessment.reason,missing=c.assessment.missing_facts) for c in report.floor_verification_candidates],
        profile_questions=list(report.ranking_profile.questions) if report.ranking_profile else [],missing_inputs=report.missing_inputs,
        analysis_errors=report.analysis_errors,stage_timings=report.stage_timings_ms,http_attempts=len(budget.records),peak_rpm=peak,http=budget.records)
    save(out/'result.json',result);event('city_comparison_completed',**{k:v for k,v in result.items() if k not in ('scope','http','missing_inputs')})


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
