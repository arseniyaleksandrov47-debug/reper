"""Live three-concept environment check at one frozen real listing.

Fresh maps/population/AI, existing validated concept preferences, source-bound
unknowns and complete card retention. This is not a new whole-search matrix or
an observed-profit/availability test. The original listing DB is read-only.
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from two_integrated_runs import TracedAnalyzer,save,event
from expanded_scenario_matrix import NetworkBudget
from run_acceptance_point3 import CASES
from real_estate_engine.ai_contract import AI_DEADLINE
from real_estate_engine.osm_access import attach_automatic_access
from real_estate_engine.ranking_policy import validate_profile,evaluate_profile
from real_estate_engine.data_completeness import market_input_action


async def main():
    base=ROOT/'audit/source_completeness';out=base/datetime.now().strftime('run_%H%M%S_%f');out.mkdir(parents=True)
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    previous=ROOT/'audit/acceptance_point3/run_233424_560929'
    prop=next(p for p in json.loads((previous/'five_real_ads.json').read_text('utf-8')) if p['id']==80)
    original_hash=hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest()
    os.environ['NVIDIA_MIN_REQUEST_INTERVAL_S']='2.5'
    analyzer=TracedAnalyzer(out);analyzer.current_case='source_completeness'
    budget=NetworkBudget(45);analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    automatic=await analyzer.pedestrian_provider.enrich(prop);prop=attach_automatic_access(prop,automatic)
    save(out/'property.json',prop)
    checks=[];records=[]
    async def one(case):
        start=time.perf_counter();token=AI_DEADLINE.set(time.monotonic()+110)
        try:
            report=await analyzer.analyze_circle(center_lat=prop['lat'],center_lng=prop['lng'],diameter_m=400,
                target_business=case['business'],business_concept=case['concept'],enrich_fns=False)
        finally:AI_DEADLINE.reset(token)
        save(out/(case['key']+'_environment.json'),asdict(report))
        saved=json.loads((previous/(case['key']+'_profile.json')).read_text('utf-8'))
        profile=validate_profile({k:saved[k] for k in ('status','summary','criteria','questions','assumptions','visit_context_quotes','unmodeled_requirements')},
            case['concept'],saved['model'],saved['metadata'])
        score=evaluate_profile(profile,prop,report);save(out/(case['key']+'_score.json'),score)
        groups=('direct_competitors','indirect_competitors','synergy_businesses','unrelated_businesses')
        known=[v.card for k in groups for v in getattr(report,k)]
        identifiers=[c.org_id for c in known+report.unclassified_businesses]
        def check(name,passed):checks.append(dict(case=case['key'],check=name,passed=bool(passed)))
        check('all_found_cards_retained',len(identifiers)==len(set(identifiers))==report.total_cards_found)
        check('unknown_cards_not_unrelated',not ({c.org_id for c in report.unclassified_businesses}&{c.card.org_id for c in report.unrelated_businesses}))
        check('unknown_inventory_matches_classifier',len(report.unclassified_businesses)==report.ai_response_metadata.get('unknown_count'))
        check('independent_population_kept',report.population is not None)
        for group in groups[:3]:
            for classified in getattr(report,group):
                decision=report.ai_response_metadata['organization_decisions'][classified.card.org_id]
                check('accepted_relation_has_independent_proof',decision.get('offer_proof',{}).get('verdict')=='supported')
        check('score_interval_bounded',0<=score['lower']<=score['upper']<=100)
        cost_gaps=score['business_model']['cost']['missing_inputs']
        check('unavailable_expenses_not_zero',{'operating_month','fitout_total','amortization_months'}<={g['field'] for g in cost_gaps})
        gaps=score['business_model']['market_context']['missing_inputs']
        request=dict(property_id=prop['id'],source=prop['source'],external_id=prop['external_id'],url=prop['url'],
            cost_inputs=cost_gaps,market_inputs=[dict(field=g,action=market_input_action(g)) for g in gaps],
            unresolved_scales=[k for k,v in profile.metadata.get('reference_scales',{}).items() if not v.get('usable')],
            note='Actual measurements/quotes or explicitly sourced client assumptions are needed; blanks are not zero.')
        save(out/(case['key']+'_data_request.json'),request)
        record=dict(case=case['key'],seconds=round(time.perf_counter()-start,3),cards=report.total_cards_found,known=len(known),
            unknown=len(report.unclassified_businesses),collection_status=report.collection_status,
            score_lower=score['lower'],score_upper=score['upper'],coverage=score['coverage'],
            full_cost_gaps=[g['field'] for g in cost_gaps],market_gaps=gaps)
        records.append(record);event('completeness_case',**record)
    await asyncio.gather(*(one(case) for case in CASES))
    peak=max((sum(r['start']<=v['start']<r['start']+60 for v in budget.records) for r in budget.records),default=0)
    checks.append(dict(check='hard_rpm_respected',passed=peak<=40))
    checks.append(dict(check='original_database_unchanged',passed=original_hash==hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest()))
    result=dict(scope=__doc__,records=records,checks=checks,all_checks_passed=all(c['passed'] for c in checks),
        nvidia_http_attempts=len(budget.records),peak_rpm=peak,http=budget.records,
        profile_origin=str(previous.relative_to(ROOT)),fns='Not re-fetched in this bounded source/AI audit; financial gaps remain explicit.')
    save(out/'result.json',result);event('source_completeness_finished',directory=str(out),passed=result['all_checks_passed'],checks=len(checks),http=len(budget.records),peak_rpm=peak)
    if not result['all_checks_passed']:raise SystemExit(1)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
