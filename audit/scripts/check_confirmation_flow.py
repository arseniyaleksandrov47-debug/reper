"""Bounded integration check: recorded real inventory, live analysis and sources.

The identical client's previously completed AI profile is revalidated and reused;
premises admission, environment, sources and proof requests use ordinary services.
Only an audit DB copy is opened for writes. No third-party messages are sent.
"""
import asyncio
import hashlib
import json
import logging
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from practical_profile_reuse_trial import ReusingAnalyzer, origin, prior
from practical_trial import PRESET
from expanded_scenario_matrix import NetworkBudget
from two_integrated_runs import save,event,TracedAnalyzer
from real_estate_engine import PropertyStorage,OptimalPropertyRecommender
from real_estate_engine.ai_contract import AI_DEADLINE
from real_estate_engine.confirmation_flow import property_key

SCENARIOS=[('walk_in',PRESET['business'],PRESET['concept']),
    ('staff_office','Офис','Офис для сотрудников. Посетители и продажи с улицы не планируются. Нужен доступ сотрудников к арендуемому помещению.'),
    ('appointment','Консультации','Офис консультаций. Все посетители приходят только по предварительной записи. Нужен разрешённый проход записанных посетителей к двери помещения.')]


async def verification_only():
    """Repeat the changed stage; don't repeat unchanged environment acquisition."""
    out=ROOT/'audit/confirmation_flow'/datetime.now().strftime('verification_%Y%m%d_%H%M%S')
    out.mkdir(parents=True);(out.parent/'VERIFICATION_LATEST.txt').write_text(str(out),encoding='utf-8')
    old=json.loads((prior/'search_report.json').read_text(encoding='utf-8'))
    candidate=SimpleNamespace(**{k:old['best_candidate'][k] for k in ('property_data','business_model_diagnostics')})
    analyzer=TracedAnalyzer(out);budget=NetworkBudget(20)
    analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    rows=[]
    for name,business,concept in SCENARIOS:
        analyzer.current_case=name;token=AI_DEADLINE.set(time.monotonic()+45);start=time.perf_counter()
        try:result=await asyncio.wait_for(analyzer.property_verifier.verify(candidate,concept,dict(target_business=business,max_price_month=250000)),45)
        except asyncio.TimeoutError:result=dict(status='requires_confirmation',checks={},reasons=[dict(code='timeout')])
        finally:AI_DEADLINE.reset(token)
        save(out/(name+'.json'),result)
        row=dict(scenario=name,status=result['status'],checks={k:v['result'] for k,v in result.get('checks',{}).items()},
                 reason_codes=[r['code'] for r in result['reasons']],seconds=round(time.perf_counter()-start,2))
        rows.append(row);event('scenario',**row)
    peak=max((sum(r['start']<=v['start']<r['start']+60 for v in budget.records) for r in budget.records),default=0)
    checks=dict(walk_in_not_automatically_approved=rows[0]['status']=='requires_confirmation',
                explicit_office_use_supported=rows[1]['checks'].get('concept_use')=='pass',
                source_publication_checked=all(r['checks'].get('availability')=='pass' for r in rows),
                rpm_within_40=peak<=40)
    save(out/'result.json',dict(scope='Live repeat of confirmation only after scope and boundary fixes; actual costs remain unknown',
                              rows=rows,checks=checks,peak_rpm=peak,http=budget.records))
    event('verification_completed',checks=checks,peak_rpm=peak)
    if not all(checks.values()):raise SystemExit(2)


async def main():
    out=ROOT/'audit/confirmation_flow'/datetime.now().strftime('run_%Y%m%d_%H%M%S')
    out.mkdir(parents=True);(out.parent/'LATEST.txt').write_text(str(out),encoding='utf-8')
    original=ROOT/'data/moscow_commercial.db'
    fingerprint=lambda:hashlib.sha256(original.read_bytes()).hexdigest()
    before=fingerprint();old=json.loads((prior/'search_report.json').read_text(encoding='utf-8'))
    selected=old['ranked_candidates']+old['floor_verification_candidates']+old['floor_rejected_candidates']
    identifiers=list(dict.fromkeys(int(c['property_data']['id']) for c in selected))
    target=out/'inventory.sqlite3'
    with closing(sqlite3.connect((origin/'trial.sqlite3').as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
        with dst:dst.execute('DELETE FROM properties WHERE id NOT IN ('+','.join('?' for _ in identifiers)+')',identifiers)
    analyzer=ReusingAnalyzer(out);analyzer.current_case='integrated';budget=NetworkBudget(60)
    analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    engine=OptimalPropertyRecommender(PropertyStorage(str(target)),analyzer)
    started=time.perf_counter();event('started',scope=__doc__,properties=len(identifiers))
    report=await engine.find_optimal_properties(PRESET['business'],location_query='Москва',search_radius_m=45000,
        min_area_sqm=30,max_area_sqm=80,max_price_month=250000,business_concept=PRESET['concept'],allowed_floors=(1,),
        max_listing_age_hours=24,max_candidates=1,ranking_timeout_s=90,floor_timeout_s=120,
        environment_timeout_s=180,fns_timeout_s=40,confirmation_timeout_s=45)
    save(out/'first_search.json',asdict(report));event('first_search',seconds=round(time.perf_counter()-started,2),
        ranked=len(report.ranked_candidates),pending=len(report.floor_verification_candidates),status=report.confirmation_status)
    second=None
    if any(a.get('type')=='find_more' for a in report.next_actions):
        second=await engine.find_more_options(report);save(out/'next_search.json',asdict(second))
    warm=None
    if '--repeat' in sys.argv:
        warm_started=time.perf_counter()
        warm=await engine.find_optimal_properties(PRESET['business'],location_query='Москва',search_radius_m=45000,
            min_area_sqm=30,max_area_sqm=80,max_price_month=250000,business_concept=PRESET['concept'],allowed_floors=(1,),
            max_listing_age_hours=24,max_candidates=1,ranking_timeout_s=90,floor_timeout_s=120,
            environment_timeout_s=180,fns_timeout_s=40,confirmation_timeout_s=45)
        save(out/'warm_search.json',asdict(warm));event('warm_search',seconds=round(time.perf_counter()-warm_started,2),
            status=warm.confirmation_status,execution=warm.confirmation_execution)
    # Small live semantic comparison: same actual unit, changed customer scenario.
    candidate=SimpleNamespace(**{k:old['best_candidate'][k] for k in ('property_data','business_model_diagnostics')})
    matrix=[]
    for name,business,concept in ([] if '--integrated-only' in sys.argv else SCENARIOS):
        analyzer.current_case=name;token=AI_DEADLINE.set(time.monotonic()+45);case_started=time.perf_counter()
        try:result=await asyncio.wait_for(analyzer.property_verifier.verify(candidate,concept,dict(target_business=business)),45)
        except asyncio.TimeoutError:result=dict(status='requires_confirmation',checks={},reasons=[dict(code='timeout')])
        finally:AI_DEADLINE.reset(token)
        save(out/(name+'.json'),result)
        matrix.append(dict(scenario=name,status=result['status'],checks={k:v['result'] for k,v in result.get('checks',{}).items()},
            reason_codes=[r['code'] for r in result['reasons']],seconds=round(time.perf_counter()-case_started,2)))
        event('scenario',**matrix[-1])
    first_keys={property_key(c.property_data) for c in report.ranked_candidates+report.comparable_candidates}
    first_keys.update(property_key(c.property_data) for c in report.floor_verification_candidates+report.floor_rejected_candidates)
    second_keys={property_key(c.property_data) for c in second.ranked_candidates} if second else set()
    checks=dict(original_db_unchanged=fingerprint()==before,
        continuation_available=second is not None,continuation_disjoint=not bool(first_keys&second_keys),
        same_geography=bool(second and (second.center_lat,second.center_lng,second.search_radius_m)==(report.center_lat,report.center_lng,report.search_radius_m)),
        same_budget_and_area=bool(second and (second.client_max_price_month,second.client_min_area_sqm,second.client_max_area_sqm)==(report.client_max_price_month,report.client_min_area_sqm,report.client_max_area_sqm)),
        pending_not_claimed_confirmed=all(c.confirmation.get('label')=='Требует подтверждения' for c in report.ranked_candidates if c.confirmation.get('status')=='requires_confirmation'))
    peak=max((sum(r['start']<=v['start']<r['start']+60 for v in budget.records) for r in budget.records),default=0)
    checks['rpm_within_40']=peak<=40
    result=dict(scope=__doc__,checks=checks,seconds=round(time.perf_counter()-started,2),
        numerical_matches=report.numerical_matching_count,evaluated=report.evaluated_count,selection_status=report.selection_status,
        first_ranked=[dict(id=c.property_data['id'],address=c.property_data['address'],status=c.confirmation['status'],
                           reason_codes=[r['code'] for r in c.confirmation.get('reasons',[])]) for c in report.ranked_candidates],
        floor_pending=len(report.floor_verification_candidates),next_ranked=len(second.ranked_candidates) if second else None,
        scenarios=matrix,peak_rpm=peak,http_attempts=len(budget.records),http=budget.records)
    if warm:result['warm_search_seconds']=round(warm.total_search_time_ms/1000,2)
    save(out/'result.json',result);event('completed',checks=checks,seconds=result['seconds'],peak_rpm=peak)
    if not all(checks.values()):raise AssertionError('Integration checks failed; see result.json')


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL)
    asyncio.run(verification_only() if '--verification-only' in sys.argv else main())
