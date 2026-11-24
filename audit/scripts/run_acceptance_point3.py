"""Exactly five real saved ads x three concepts through the public search API.

Live NVIDIA, maps, population, automatic access and company accounts. Old saved
listing text/availability is distinguished from freshly fetched surroundings.
The original DB is read-only; migrations apply only to an audit copy.
"""
import asyncio
import argparse
import json
import logging
import math
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
from two_integrated_runs import TracedAnalyzer,save,event,fingerprints
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine import PropertyStorage,OptimalPropertyRecommender
from real_estate_engine.ranking_policy import validate_profile
from real_estate_engine.ai_contract import AIContractError


CASES=[
    dict(key='street',business='Кофейня навынос',concept=(
        'Кофейня навынос. Покупатели приходят пешком с улицы и из соседних домов. '
        'Нужен первый этаж и разрешённый вход покупателей к помещению. '
        'Главный приоритет — доступная аудитория и конкурентное окружение. '
        'Затем важны низкие полные расходы на помещение. Предпочтительна площадь 30–100 м².'),allowed_floors=(1,)),
    dict(key='booking',business='Кабинет психолога',concept=(
        'Кабинет психолога: индивидуальные психологические консультации для взрослых. Клиенты приезжают по записи, привлекаю их через интернет. '
        'Случайный поток прохожих не нужен. Нужен разрешённый путь записанных посетителей к двери. '
        'Главный приоритет — низкие полные расходы на помещение. '
        'Затем важны пешеходная доступность и конкурентное окружение. Предпочтительна площадь 30–120 м².'),allowed_floors=None),
    dict(key='delivery',business='Цветочная мастерская онлайн-заказов',concept=(
        'Цветочная мастерская: собираем букеты по интернет-заказам для доставки покупателям. Покупатели не посещают помещение, заказы забирают курьеры пешком с обычными сумками. '
        'Случайный поток прохожих не нужен. Нужен разрешённый путь курьеров до двери помещения. '
        'Главный приоритет — низкие полные расходы на помещение. '
        'Затем важны доступность для курьеров и конкурентное окружение. Предпочтительна площадь 40–150 м².'),allowed_floors=None)
]


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',choices=['all','remaining']+[c['key'] for c in CASES],default='all')
    parser.add_argument('--reuse-profiles-from',type=Path,
        help='Reuse only validated unchanged-concept live profiles, with explicit provenance; all environment analysis stays live')
    args=parser.parse_args()
    cases=[c for c in CASES if args.case=='all' or c['key']==args.case or args.case=='remaining' and c['key'] in ('street','delivery')]
    base=ROOT/'audit/acceptance_point3';out=base/datetime.now().strftime('run_%H%M%S_%f');out.mkdir(parents=True)
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    frozen=fingerprints();save(out/'manifest.json',dict(scope=__doc__,fingerprints=frozen,cases=cases,
        listing_ids=[63,80,81,205,905],nvidia_http_budget=110,
        reused_profiles_from=str(args.reuse_profiles_from) if args.reuse_profiles_from else None,
        stage_budgets_seconds=dict(profile=90,floor=120,environment=240,fns=40),
        manual_source_expectations=dict(source_conflict_id=205,street_floor_rejection_id=905,
            no_guaranteed_profit_or_single_leader_with_missing_data=True)))
    target=out/'five_real_ads.sqlite3'
    with closing(sqlite3.connect((ROOT/'data/moscow_commercial.db').as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(target)) as dest:
        src.backup(dest)
        with dest:dest.execute('DELETE FROM properties WHERE id NOT IN (63,80,81,205,905)')
    store=PropertyStorage(str(target))
    with store._get_connection() as conn:rows=[dict(r) for r in conn.execute('SELECT * FROM properties ORDER BY id')]
    save(out/'five_real_ads.json',rows)
    os.environ['NVIDIA_MIN_REQUEST_INTERVAL_S']='2.5'
    analyzer=TracedAnalyzer(out);budget=NetworkBudget(110)
    wrapped=budget.wrap(analyzer.http_session.post)
    def post(*args,**kwargs):
        try:return wrapped(*args,**kwargs)
        finally:save(out/'nvidia_http_records.json',budget.records)
    analyzer.http_session.post=post
    engine=OptimalPropertyRecommender(store,analyzer);records=[]
    original_resolve=engine.ranking_profile_resolver.resolve
    async def checkpoint_profile(business,concept,requirements=None,timeout_s=90):
        profile=None
        saved=reused_profiles.get(analyzer.current_case)
        # Reuse is diagnostic provenance, never an invisible fallback.
        if saved and saved['concept']==concept and saved['metadata'].get('client_constraints')==(requirements or {}):
            try:
                profile=validate_profile({k:saved[k] for k in ('status','summary','criteria','questions','assumptions','visit_context_quotes','unmodeled_requirements')},
                    concept,saved['model'],dict(saved['metadata'],audit_live_profile_origin=str(args_reuse_dir)))
            except AIContractError as exc:event('profile_not_reused',case=analyzer.current_case,reason=str(exc))
        if profile is None:profile=await original_resolve(business,concept,requirements,timeout_s)
        save(out/(analyzer.current_case+'_profile.json'),asdict(profile))
        return profile
    reused_profiles={};args_reuse_dir=args.reuse_profiles_from
    if args_reuse_dir:
        prior_manifest=json.loads((args_reuse_dir/'manifest.json').read_text('utf-8'))
        prior_cases={c['key']:c for c in prior_manifest['cases']}
        # The affected stage is classification. Do not replay profiles if any
        # semantic/profile/scoring source used to generate them has changed.
        for file in ('ranking_policy.py','scenario_contract.py','market_context.py','concept_evidence.py'):
            key=str(Path('real_estate_engine')/file)
            if not frozen.get(key) or file!='ranking_policy.py' and prior_manifest['fingerprints'].get(key)!=frozen[key]:raise ValueError('Profile source changed: '+file)
        for case in cases:
            if prior_cases.get(case['key'])!=json.loads(json.dumps(case)):raise ValueError('Business concept or constraints changed')
            path=args_reuse_dir/(case['key']+'_profile.json')
            saved=json.loads(path.read_text('utf-8'))
            if saved['status']!='ready' or saved['concept']!=case['concept']:raise ValueError('Cannot reuse an invalid or changed concept profile')
            if saved['model']!=getattr(analyzer,'ranking_model',analyzer.floor_model):raise ValueError('Profile model changed')
            reused_profiles[case['key']]=saved
    engine.ranking_profile_resolver.resolve=checkpoint_profile
    for case in cases:
        analyzer.current_case=case['key'];start=time.perf_counter();before=len(budget.records)
        report=await engine.find_optimal_properties(case['business'],business_concept=case['concept'],
            allowed_floors=case['allowed_floors'],center_lat=55.75,center_lng=37.55,search_radius_m=20000,
            max_price_month=4000000,max_candidates=5,competition_diameter_m=400,
            ranking_timeout_s=90,floor_timeout_s=120,environment_timeout_s=240,fns_timeout_s=40)
        save(out/(case['key']+'_report.json'),asdict(report))
        evaluations=[]
        for review in report.floor_rejected_candidates+report.floor_verification_candidates:
            a=review.assessment
            evaluations.append(dict(id=review.property_data['id'],status=a.status,analysis_status=a.analysis_status,reason=a.reason))
        for candidate in report.ranked_candidates:
            a=candidate.floor_assessment
            evaluations.append(dict(id=candidate.property_data['id'],status=a.status,analysis_status=a.analysis_status,reason=a.reason))
        all_205=[e for e in evaluations if e['id']==205]
        guards=[]
        guards.append(dict(check='all_five_listings_have_a_decision',passed=sorted(e['id'] for e in evaluations)==[63,80,81,205,905]))
        if report.ranking_profile and report.ranking_profile.status=='ready':
            criteria=report.ranking_profile.criteria
            guards.append(dict(check='full_cost_not_replaced_by_rent',passed=all(c.metric!='rent_month' for c in criteria)))
            primary='market_fit_index' if case['key']=='street' else 'premises_cost_month'
            guards.append(dict(check='scenario_primary_priority',passed=any(c.metric==primary and
                all(c.weight>d.weight for d in criteria if d.metric!=primary) for c in criteria)))
            if case['key']!='street':
                guards.append(dict(check='waived_random_traffic_has_zero_weight',
                    passed=report.ranking_profile.metadata.get('market_context',{}).get('audience',{}).get('transit_flow',{}).get('weight')==0))
        guards.append(dict(check='source_floor_conflict_remains_pending',passed=bool(all_205) and all_205[0]['status']=='needs_verification'))
        if case['key']=='street':guards.append(dict(check='explicit_first_floor_excludes_fifth',passed=any(e['id']==905 and e['status']=='rejected' for e in evaluations)))
        for c in report.ranked_candidates:
            parts=c.scoring_components
            expected_lower=sum(p['contribution_lower'] for p in parts.values())
            expected_upper=sum(p['contribution_upper'] for p in parts.values())
            guards.append(dict(check='sum_of_reported_contributions',id=c.property_data['id'],
                passed=abs(expected_lower-c.unrounded_total_score)<1e-8 and abs(expected_upper-c.score_upper_bound)<1e-8))
            guards.append(dict(check='finite_bounded_score',id=c.property_data['id'],passed=math.isfinite(c.total_score) and
                0<=c.unrounded_total_score<=c.score_upper_bound<=100 and 0<=c.score_coverage<=1))
            guards.append(dict(check='admitted_requirements_not_overridden_by_rank',id=c.property_data['id'],passed=c.floor_assessment.status=='eligible'))
        guards.append(dict(check='order_uses_unrounded_lower_bound',passed=all(a.unrounded_total_score>=b.unrounded_total_score for a,b in zip(report.ranked_candidates,report.ranked_candidates[1:]))))
        guards.append(dict(check='missing_data_never_claims_definitive_leader',passed=not report.missing_inputs or report.selection_status!='preliminary_leader'))
        record=dict(case=case['key'],seconds=round(time.perf_counter()-start,2),profile_status=report.ranking_profile.status if report.ranking_profile else None,
            selection_status=report.selection_status,evaluated=report.evaluated_count,nvidia_http_attempts=len(budget.records)-before,
            floor_results=evaluations,ranked=[dict(id=c.property_data['id'],lower=c.unrounded_total_score,upper=c.score_upper_bound,
                coverage=c.score_coverage,fns_entities=c.competition_report.fns_enriched_count) for c in report.ranked_candidates],
            checks=guards,checks_passed=all(g['passed'] for g in guards),
            meaningful_end_to_end=report.evaluated_count>0,stage_timings_ms=report.stage_timings_ms)
        records.append(record);save(out/'partial_summary.json',records)
        save(out/(case['key']+'_company_matches.json'),getattr(analyzer.fns_collector,'match_diagnostics',{}))
        save(out/(case['key']+'_review_brief.json'),dict(case=case,profile=asdict(report.ranking_profile),
            floor_results=evaluations,missing_inputs=report.missing_inputs,analysis_errors=report.analysis_errors,
            candidates=[dict(id=c.property_data['id'],score=c.unrounded_total_score,upper=c.score_upper_bound,
                components=c.scoring_components,environment_status=c.competition_report.data_status,
                classification_metadata=c.competition_report.ai_response_metadata,
                organizations=[dict(level=level,title=v.card.title,org_id=v.card.org_id,categories=v.card.categories,
                    features=v.card.features,reason=v.reason,inn=v.card.fns_inn,year=v.card.fns_reporting_year)
                    for level,group in [('direct',c.competition_report.direct_competitors),('indirect',c.competition_report.indirect_competitors),
                        ('synergy',c.competition_report.synergy_businesses),('unrelated',c.competition_report.unrelated_businesses)] for v in group])
                for c in report.ranked_candidates]))
        event('matrix_case_completed',**record)
    result=dict(cases=records,nvidia_http_attempts=len(budget.records),http_429=sum(r.get('status')==429 for r in budget.records),
        peak_requests_per_minute=max((sum(r['start']<=v['start']<r['start']+60 for v in budget.records) for r in budget.records),default=0),
        source_and_working_database_unchanged=frozen==fingerprints(),
        all_cases_reached_rating=all(r['meaningful_end_to_end'] for r in records),
        all_source_and_arithmetic_checks_passed=all(r['checks_passed'] for r in records),
        note='Source-text manual review remains separate from arithmetic checks; no current availability or measured outcome guarantee')
    save(out/'summary.json',result);event('point3_matrix_completed',directory=str(out),**result)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
