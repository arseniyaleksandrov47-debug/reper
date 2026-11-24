"""New live client scenarios with stage timings and a preserved comparison inventory."""
import argparse
import asyncio
import hashlib
import json
import logging
import sqlite3
import sys
import time
from contextvars import ContextVar
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'audit/scripts'))
from two_integrated_runs import TracedAnalyzer, event, save
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine import RealEstateAggregator, OptimalPropertyRecommender, CanonicalProperty, PlatformSource
from real_estate_engine.property_sources import cian_unit

INFERENCE_TASK = ContextVar('validation_inference_task', default='unknown')
CASES = [
    dict(key='walk_in_print', business='Пункт печати и копирования документов',
         lat=55.774441, lng=37.670968, location='Нижняя Красносельская улица, 40/12к6',
         min_area=40, max_area=80, rent=150000,
         concept=('Пункт печати и копирования документов. Клиенты свободно заходят с улицы с 09:00 до 20:00. '
                  'Нужен разрешённый проход любых клиентов непосредственно в помещение в эти часы. '
                  'Главный приоритет — доступная пешеходная аудитория с учётом маршрутов и конкурентов. '
                  'Затем важны расходы на аренду и эксплуатацию. '
                  'Обязательно наличие санузла внутри арендуемого блока. Желательна остеклённая входная дверь. '
                  'Ремонт и его смету определяю самостоятельно. Ищу 40–80 м², аренда до 150000 рублей в месяц.')),
    dict(key='appointment_tailor', business='Ателье ремонта одежды по записи',
         lat=55.774441, lng=37.670968, location='Нижняя Красносельская улица, 40/12к6',
         min_area=40, max_area=80, rent=150000,
         concept=('Ателье ремонта одежды по предварительной записи. Клиентов нахожу через интернет и рекомендации. '
                  'Принимаю с 10:00 до 19:00, пропуск или регистрация на ресепшене допустимы. '
                  'Этаж не ограничиваю при подходящем пути посетителей. Случайный поток прохожих не нужен. '
                  'Главный приоритет — низкие расходы на аренду и эксплуатацию. '
                  'В рабочей комнате обязательно естественное освещение. Внутри блока нужна раковина с водой. '
                  'Желательно место для примерочной. Обустройство и ремонт планирую сам. '
                  'Ищу 40–80 м², аренда до 150000 рублей в месяц.')),
    dict(key='delivery_accessories', business='Микросклад интернет-магазина аксессуаров',
         lat=55.708, lng=37.622, location='метро Тульская',
         min_area=60, max_area=120, rent=180000,
         concept=('Микросклад интернет-магазина аксессуаров. Продаём онлайн и отправляем заказы курьерами. '
                  'Покупатели в помещение не приходят. Сотрудники и курьеры переносят небольшие коробки вручную '
                  'с 09:00 до 18:00, тяжёлых грузов нет. Пропуска допустимы. Этаж не ограничиваю. '
                  'Случайный поток прохожих не нужен. Главный приоритет — низкие расходы на аренду и эксплуатацию. '
                  'Нужен дверной проём шириной не менее 90 см. Желательно наличие стеллажей. '
                  'Ремонт и обустройство рассчитываю самостоятельно. Ищу 60–120 м², аренда до 180000 рублей в месяц.')),
]


def task_name(prompt):
    try:
        data = json.loads(prompt.split('INPUT_JSON\n')[-1])
    except (ValueError, TypeError):
        return 'text'
    if data.get('task'):
        return data['task']
    if 'client_fragments' in data:
        return 'extract_physical_requirements'
    if 'images' in data:
        return 'visual_requirements'
    if 'ranking_objective' in data:
        return 'ranking_profile'
    return 'structured_text'


class ProfiledAnalyzer(TracedAnalyzer):
    def __init__(self, out):
        super().__init__(out)
        self.timings = []
        self.http_timings = []
        self.budget = NetworkBudget(180)
        original_post = self.http_session.post

        def measured_post(url, **kwargs):
            started = time.monotonic()
            record = dict(case=self.current_case, task=INFERENCE_TASK.get(),
                          model=kwargs.get('json', {}).get('model'))
            try:
                response = original_post(url, **kwargs)
                record['status'] = response.status_code
                if response.status_code == 200:
                    body = response.json()
                    record['usage'] = body.get('usage')
                    record['finish_reason'] = (body.get('choices') or [{}])[0].get('finish_reason')
                return response
            except Exception as exc:
                record['error_type'] = type(exc).__name__
                raise
            finally:
                record['seconds'] = round(time.monotonic() - started, 4)
                self.http_timings.append(record)

        self.http_session.post = self.budget.wrap(measured_post)
        for obj, method, label in (
            (self.map_collector, 'collect_in_circle', 'map_cards'),
            (self.population_analyzer, 'analyze_circle', 'population'),
            (self.pedestrian_provider, 'enrich', 'pedestrian_network'),
        ):
            original = getattr(obj, method)

            async def measured(*args, _original=original, _label=label, **kwargs):
                case = self.current_case
                started = time.monotonic()
                try:
                    return await _original(*args, **kwargs)
                finally:
                    self.timings.append(dict(case=case, stage=_label,
                                             seconds=round(time.monotonic() - started, 4)))
            setattr(obj, method, measured)

    def _call_nvidia_sync(self, prompt, *args, **kwargs):
        task = task_name(prompt)
        token = INFERENCE_TASK.set(task)
        started = time.monotonic()
        case = self.current_case
        try:
            return super()._call_nvidia_sync(prompt, *args, **kwargs)
        finally:
            self.timings.append(dict(case=case, stage='ai:' + task,
                                     seconds=round(time.monotonic() - started, 4)))
            INFERENCE_TASK.reset(token)


async def prepare_inventory(path, analyzer, out):
    origin = ROOT / 'audit/optional_analysis/run_20261105_173002/inventory.sqlite3'
    with sqlite3.connect(origin.as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(path) as destination:
            source.backup(destination)
    agg = RealEstateAggregator(str(path))
    expected = json.loads((ROOT / 'audit/client_fixes/sources/cian_expected.json').read_text(encoding='utf-8'))
    provider = analyzer.property_verifier.sources
    try:
        body, url = await asyncio.to_thread(provider._get, expected['url'])
        unit = cian_unit(body, url, expected)
        rent = unit['price_month']
        listing = CanonicalProperty(source=PlatformSource.CIAN, external_id=unit['unit_id'],
            title=unit['facts'].get('title', expected['title']), address=unit['address'],
            lat=expected['lat'], lng=expected['lng'], area_sqm=unit['area_sqm'],
            price_month=rent, price_sqm_year=rent * 12 / unit['area_sqm'], floor=unit['floor'],
            url=url, description=unit['facts'].get('description', ''), photos=unit['photos'])
        agg.storage.upsert_batch([listing])
        save(out / 'current_cian_source.json', dict(unit=unit, fetched_at=time.time()))
    except Exception as exc:
        save(out / 'current_cian_source.json', dict(status='unavailable', error_type=type(exc).__name__))
    save(out / 'inventory_origin.json', dict(path=str(origin), freshness_preserved=True))


async def run_case(agg, analyzer, case, out):
    analyzer.current_case = case['key']
    started = time.monotonic()
    first_http = len(analyzer.budget.records)
    report = await agg.find_optimal_properties(
        target_business=case['business'], business_concept=case['concept'],
        location_query=case['location'], center_lat=case['lat'], center_lng=case['lng'],
        search_radius_m=case.get('radius_m',2000), min_area_sqm=case['min_area'], max_area_sqm=case['max_area'],
        max_price_month=case['rent'], max_listing_age_hours=24,
        include_unverified=True, max_candidates=3,
    )
    elapsed = time.monotonic() - started
    save(out / (case['key'] + '.json'), asdict(report))
    candidates = report.ranked_candidates
    checks = dict(
        profile_ready=report.ranking_profile is not None and report.ranking_profile.status=='ready',
        candidates_evaluated=report.evaluated_count>0,
        finite_score_ranges=all(0 <= c.unrounded_total_score <= c.score_upper_bound <= 100 for c in candidates),
        pending_conditions_labeled=all(c.confirmation['status'] == 'requires_confirmation'
            for c in candidates if c.confirmation.get('checks', {}).get('client_requirements', {}).get('result') == 'unknown'),
        no_confirmed_violation_ranked=all(c.confirmation['status'] != 'not_suitable' for c in candidates),
        preparation_scope_explicit=all(c.business_model_diagnostics['cost'].get('preparation_scope') == 'tenant_managed' for c in candidates),
        no_invented_client_quotes=all(row['client_quote'] in case['concept'] or row['client_quote'] in ('Бизнес: ' + case['business'])
            for c in candidates for row in c.confirmation.get('checks', {}).get('client_requirements', {}).get('items', [])),
        incomplete_is_provisional=not report.comparison_complete if any(c.confirmation['status'] == 'requires_confirmation' for c in candidates) else True,
    )
    result = dict(case=case['key'], seconds=round(elapsed, 3), stages=report.stage_timings_ms,
        profile_status=report.ranking_profile.status if report.ranking_profile else None,
        profile_analysis=report.ranking_profile.analysis_status if report.ranking_profile else None,
        numerical_matches=report.numerical_matching_count, evaluated=report.evaluated_count,
        ranked=len(candidates), selection_status=report.selection_status,
        confirmation=report.confirmation_status, confirmation_execution=report.confirmation_execution,
        http_attempts=len(analyzer.budget.records)-first_http, checks=checks,
        analysis_errors=report.analysis_errors,
        rows=[dict(id=c.property_data['id'], source=c.property_data['source'], address=c.property_data['address'],
            score_low=c.unrounded_total_score, score_high=c.score_upper_bound,
            status=c.confirmation['status'], physical=c.confirmation.get('checks', {}).get('client_requirements'),
            photos=c.confirmation.get('photo_analysis')) for c in candidates])
    event('case_complete', **{k:v for k,v in result.items() if k != 'rows'})
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('baseline', 'optimized', 'recheck'), default='baseline')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--cases', nargs='+', choices=[c['key'] for c in CASES])
    args = parser.parse_args()
    base = args.root or ROOT / 'audit/validation_speed' / datetime.now().strftime('run_%Y%m%d_%H%M%S')
    base.mkdir(parents=True, exist_ok=True)
    (ROOT / 'audit/validation_speed/LATEST.txt').write_text(str(base), encoding='utf-8')
    out = base / args.phase
    out.mkdir(exist_ok=True)
    analyzer = ProfiledAnalyzer(out)
    inventory = base / 'inventory.sqlite3'
    if not inventory.exists():
        await prepare_inventory(inventory, analyzer, base)
    original = ROOT / 'data/moscow_commercial.db'
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    agg = RealEstateAggregator(str(inventory))
    agg.competitor_analyzer = analyzer
    agg.recommender = OptimalPropertyRecommender(agg.storage, analyzer)
    cases = [c for c in CASES if not args.cases or c['key'] in args.cases]
    save(out / 'requests.json', cases)
    rows = []
    for case in cases:
        rows.append(await run_case(agg, analyzer, case, out))
        save(out / 'partial.json', rows)
    peak = max((sum(r['start'] <= s['start'] < r['start'] + 60 for s in analyzer.budget.records)
                for r in analyzer.budget.records), default=0)
    summary = dict(phase=args.phase, cases=rows, timings=analyzer.timings, http=analyzer.http_timings,
        http_attempts=len(analyzer.budget.records), peak_rpm=peak,
        original_database_unchanged=original_hash == hashlib.sha256(original.read_bytes()).hexdigest(),
        checks_passed=all(all(row['checks'].values()) for row in rows),
        rpm_respected=peak <= 40)
    save(out / 'summary.json', summary)
    event('phase_complete', phase=args.phase, directory=str(out), peak_rpm=peak,
          checks_passed=summary['checks_passed'], cases=len(rows))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
