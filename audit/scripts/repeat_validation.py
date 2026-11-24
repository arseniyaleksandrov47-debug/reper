"""Cold live profile checks and full client searches on a copied inventory."""
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

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from validation_speed import CASES,ProfiledAnalyzer,run_case
from two_integrated_runs import save,event
from real_estate_engine import RealEstateAggregator,OptimalPropertyRecommender,RankingProfileResolver


NEW_CASES=[
    dict(key='archive_digitization',business='Студия оцифровки семейных архивов',
        lat=55.774441,lng=37.670968,location='Нижняя Красносельская улица, 40/12к6',radius_m=700,
        min_area=40,max_area=80,rent=150000,
        concept=('Студия оцифровки семейных архивов. Клиенты находят меня через интернет и приходят по записи '
            'с 10:00 до 19:00. Регистрация на ресепшене допустима. Этаж не ограничиваю. '
            'Случайный поток прохожих не нужен. Главный приоритет — низкие расходы на аренду и эксплуатацию. '
            'Нужна отдельная рабочая комната с закрывающейся дверью. Желателен санузел внутри блока. '
            'Ремонт и обустройство рассчитываю самостоятельно. Ищу 40–80 м² в радиусе 700 м '
            'от Нижней Красносельской улицы, 40/12к6, аренда до 150000 рублей в месяц.')),
    dict(key='packing_online_orders',business='Мастерская упаковки интернет-заказов',
        lat=55.708,lng=37.622,location='метро Тульская',radius_m=2000,
        min_area=60,max_area=120,rent=180000,
        concept=('Мастерская упаковки интернет-заказов. Покупатели заказывают онлайн и в помещение не приходят. '
            'Сотрудники и курьеры переносят небольшие коробки вручную с 08:00 до 18:00. '
            'Пропуска допустимы, этаж не ограничиваю. Тяжёлых грузов нет. '
            'Случайный поток прохожих не нужен. Главный приоритет — низкие расходы на аренду и эксплуатацию. '
            'Обязателен дверной проём шириной не менее 90 см. Желательно наличие раковины с водой внутри блока. '
            'Подготовку помещения и её бюджет планирую сам. Ищу 60–120 м² в радиусе 2 км от метро Тульская, '
            'аренда до 180000 рублей в месяц.')),
]


def constraints(case):
    return dict(target_area_sqm=None,min_area_sqm=case['min_area'],max_area_sqm=case['max_area'],
        max_price_month=case['rent'],max_price_sqm_year=None,competition_diameter_m=400,
        explicit_visit_context=None)


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--requests',type=Path)
    parser.add_argument('--output-base',type=Path,default=Path('audit/repeat_validation'))
    args=parser.parse_args()
    base=ROOT/args.output_base
    out=base/datetime.now().strftime('run_%Y%m%d_%H%M%S');out.mkdir(parents=True)
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    previous=Path((ROOT/'audit/validation_speed/LATEST.txt').read_text(encoding='utf-8').strip())
    inventory=out/'inventory.sqlite3'
    with sqlite3.connect((previous/'inventory.sqlite3').as_uri()+'?mode=ro',uri=True) as source:
        with sqlite3.connect(inventory) as destination:source.backup(destination)
    original=ROOT/'data/moscow_commercial.db'
    fingerprint=hashlib.sha256(original.read_bytes()).hexdigest()
    analyzer=ProfiledAnalyzer(out)
    agg=RealEstateAggregator(str(inventory));agg.competitor_analyzer=analyzer
    agg.recommender=OptimalPropertyRecommender(agg.storage,analyzer)
    if args.requests:
        plan=json.loads((ROOT/args.requests).read_text(encoding='utf-8'))
        requests=plan['profiles'];negative_cases=plan.get('negative_cases',[])
    else:
        requests=[dict(case=case,attempt=attempt) for attempt in (1,2) for case in CASES[1:]]
        requests.extend(dict(case=case,attempt=1) for case in NEW_CASES)
        negative_cases=[]
    save(out/'requests.json',requests)
    results=[]
    started=time.monotonic()
    for request in requests:
        case=request['case'];attempt=request['attempt']
        key=case['key']+'_profile_'+str(attempt);analyzer.current_case=key
        resolver=RankingProfileResolver(analyzer)
        before=time.monotonic()
        profile=await resolver.resolve(case['business'],case['concept'],constraints(case),timeout_s=90)
        save(out/(key+'.json'),asdict(profile))
        row=dict(case=case['key'],attempt=attempt,status=profile.status,analysis_status=profile.analysis_status,
            seconds=round(time.monotonic()-before,3),error=profile.metadata.get('contract_error'),
            error_type=profile.metadata.get('error_type'),
            repairs=profile.metadata.get('profile_retry_errors',[]),
            parameter_resolution=profile.metadata.get('parameter_resolution'),
            checks=dict(profile_ready=profile.status=='ready',
                context_interpreted=profile.metadata.get('scenario_semantic_status')=='independently_interpreted'))
        results.append(row);save(out/'profiles.json',results);event('profile_complete',**row)
        perform_full=request.get('full_search',case['key'] in {item['key'] for item in NEW_CASES})
        if perform_full and profile.status=='ready':
            # Reuse this resolver's freshly validated profile through the normal
            # entry point. Its original source/intent metadata stays intact.
            agg.recommender.ranking_profile_resolver=resolver
            row['full_search']=await run_case(agg,analyzer,case,out)
            row['full_search']['profile_requests_during_search']=sum(
                record['case']==case['key'] and record['task'] in ('interpret_scenario','ranking_profile')
                for record in analyzer.http_timings)
            save(out/'profiles.json',results)
    negative_results=[]
    for case in negative_cases:
        analyzer.current_case=case['key'];before=len(analyzer.budget.records)
        report=await agg.find_optimal_properties(target_business=case['business'],business_concept=case['concept'],
            location_query=case['location'],center_lat=case['lat'],center_lng=case['lng'],
            search_radius_m=case['radius_m'],min_area_sqm=case['min_area'],max_area_sqm=case['max_area'],
            max_price_month=case['rent'],max_listing_age_hours=24,include_unverified=True)
        save(out/(case['key']+'.json'),asdict(report))
        checks=dict(empty_inventory_reported=report.numerical_matching_count==0 and report.selection_status=='no_candidates',
            no_candidate_invented=not report.ranked_candidates and report.best_candidate is None,
            no_ai_for_empty_inventory=len(analyzer.budget.records)==before,
            budget_preserved=report.client_max_price_month==case['rent'])
        negative_results.append(dict(case=case['key'],checks=checks));event('negative_case_complete',**negative_results[-1])
    peak=max((sum(r['start']<=other['start']<r['start']+60 for other in analyzer.budget.records)
        for r in analyzer.budget.records),default=0)
    full=[row['full_search'] for row in results if 'full_search' in row]
    expected_full=sum(request.get('full_search',request['case']['key'] in {item['key'] for item in NEW_CASES}) for request in requests)
    checks=dict(all_cold_profiles_ready=len(results)==len(requests) and all(row['status']=='ready' for row in results),
        all_new_searches_ranked=len(full)==expected_full and all(row['evaluated']>0 and row['ranked']>0 for row in full),
        full_search_checks_passed=len(full)==expected_full and all(all(row['checks'].values()) for row in full),
        existing_profile_reused=len(full)==expected_full and all(row['profile_requests_during_search']==0 for row in full),
        negative_cases_passed=all(all(row['checks'].values()) for row in negative_results),
        rolling_rpm_within_40=peak<=40,
        original_database_unchanged=hashlib.sha256(original.read_bytes()).hexdigest()==fingerprint)
    summary=dict(scope=__doc__,seconds=round(time.monotonic()-started,3),profiles=results,negative_cases=negative_results,checks=checks,
        peak_rpm=peak,http_attempts=len(analyzer.budget.records),http=analyzer.http_timings,timings=analyzer.timings)
    save(out/'summary.json',summary)
    event('repeat_validation_complete',directory=str(out),checks=checks,peak_rpm=peak,seconds=summary['seconds'])


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
