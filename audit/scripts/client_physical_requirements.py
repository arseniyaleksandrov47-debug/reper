"""Full public search on a preserved, recent real inventory and a new client concept.

The client selects provisional comparison. Listing freshness is preserved.
AI, environment and confirmation use the production implementations.
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

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit/scripts'))
from two_integrated_runs import TracedAnalyzer, event, save
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine import RealEstateAggregator, OptimalPropertyRecommender


CONCEPT = (
    'Мастерская реставрации книг по записи. Клиентов нахожу через интернет и рекомендации. '
    'Записанные клиенты и курьеры приносят небольшие коробки вручную с 10:00 до 19:00. '
    'Пропуск или регистрация на ресепшене допустимы. Этаж не ограничиваю. '
    'Случайный поток прохожих не нужен. Главный приоритет — низкие расходы на аренду и эксплуатацию. '
    'Естественное освещение непосредственно в рабочей комнате обязательно. '
    'Внутри арендуемого блока нужна раковина с подводом воды. '
    'Желательна непрозрачная входная дверь. '
    'Ремонт, обустройство и их бюджет определю самостоятельно. '
    'Ищу 60–120 м² в радиусе 2 км от метро Тульская, аренда до 180000 рублей в месяц.'
)


async def main():
    base = ROOT / 'audit/premises_requirements'
    out = base / datetime.now().strftime('client_%Y%m%d_%H%M%S')
    out.mkdir(parents=True)
    (base / 'CLIENT_LATEST.txt').write_text(str(out), encoding='utf-8')
    origin = ROOT / 'audit/optional_analysis/run_20261105_173002'
    location = json.loads((origin / 'location.json').read_text(encoding='utf-8'))
    inventory = out / 'inventory.sqlite3'
    with sqlite3.connect((origin / 'inventory.sqlite3').as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(inventory) as dst:
            src.backup(dst)
    original = ROOT / 'data/moscow_commercial.db'
    fingerprint = hashlib.sha256(original.read_bytes()).hexdigest()
    agg = RealEstateAggregator(str(inventory))
    analyzer = TracedAnalyzer(out)
    analyzer.current_case = 'book_restoration_client'
    budget = NetworkBudget(65)
    analyzer.http_session.post = budget.wrap(analyzer.http_session.post)
    agg.competitor_analyzer = analyzer
    agg.recommender = OptimalPropertyRecommender(agg.storage, analyzer)
    parameters = dict(
        target_business='Мастерская реставрации книг по записи',
        business_concept=CONCEPT, location_query='метро Тульская',
        center_lat=location['lat'], center_lng=location['lng'],
        search_radius_m=2000, min_area_sqm=60, max_area_sqm=120,
        max_price_month=180000, allowed_floors=None, include_unverified=True,
        max_listing_age_hours=24, max_candidates=3,
    )
    save(out / 'request.json', dict(**parameters, inventory_origin=str(origin / 'inventory.sqlite3')))
    event('client_started', directory=str(out))
    started = time.monotonic()
    report = await agg.find_optimal_properties(**parameters)
    save(out / 'search.json', asdict(report))
    candidates = list(report.ranked_candidates)
    peak = max((sum(r['start'] <= other['start'] < r['start'] + 60 for other in budget.records)
                for r in budget.records), default=0)
    rows = []
    for candidate in candidates:
        confirmation = candidate.confirmation
        rows.append(dict(
            address=candidate.property_data['address'],
            source=candidate.property_data['source'],
            url=candidate.property_data.get('url'),
            score_low=candidate.unrounded_total_score,
            score_high=candidate.score_upper_bound,
            confirmation=confirmation.get('status'),
            requirement_check=confirmation.get('checks', {}).get('client_requirements'),
            photo_analysis=confirmation.get('photo_analysis'),
            cost=candidate.business_model_diagnostics.get('cost'),
        ))
    checks = dict(
        original_database_unchanged=fingerprint == hashlib.sha256(original.read_bytes()).hexdigest(),
        rpm_within_40=peak <= 40,
        score_produced=bool(candidates),
        physical_requirements_present=bool(rows) and all(row['requirement_check'] is not None for row in rows),
        only_requested_physical_conditions=bool(rows) and all(
            row['requirement_check'] is not None and
            all(any(quote in item['client_quote'] and item['importance']==importance
                    for item in row['requirement_check'].get('items',[]))
                for quote,importance in (
                    ('Естественное освещение непосредственно в рабочей комнате обязательно','mandatory'),
                    ('Внутри арендуемого блока нужна раковина с подводом воды','mandatory'),
                    ('Желательна непрозрачная входная дверь','preferred'))) and
            all(item['client_quote'] in CONCEPT and 'прохожих' not in item['client_quote']
                and 'Пропуск' not in item['client_quote']
                for item in row['requirement_check']['items']) for row in rows),
        preparation_managed_by_tenant=bool(rows) and all(
            row['cost'].get('preparation_scope') == 'tenant_managed' for row in rows),
        missing_preparation_does_not_block=all(
            not any(reason['code'] in ('cost:fitout_total', 'cost:amortization_months')
                    for reason in candidate.confirmation.get('reasons', [])) for candidate in candidates),
        unresolved_conditions_marked_provisional=all(
            candidate.confirmation['status'] == 'requires_confirmation'
            for candidate in candidates
            if candidate.confirmation.get('checks', {}).get('client_requirements', {}).get('result') == 'unknown'),
    )
    result = dict(
        scope=__doc__, seconds=round(time.monotonic() - started, 3),
        numerical_matches=report.numerical_matching_count,
        evaluated=report.evaluated_count, selection_status=report.selection_status,
        comparison_complete=report.comparison_complete,
        profile_status=report.ranking_profile.status if report.ranking_profile else None,
        analysis_errors=report.analysis_errors,
        candidates=rows, checks=checks, peak_rpm=peak,
        http_attempts=len(budget.records), http=budget.records,
    )
    save(out / 'result.json', result)
    event('client_complete', directory=str(out), seconds=result['seconds'], checks=checks,
          evaluated=report.evaluated_count, profile_status=result['profile_status'], peak_rpm=peak)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
