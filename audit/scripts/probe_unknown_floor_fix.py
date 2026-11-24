"""Live AI boundary controls. All premises here are explicitly synthetic.

These controls do not add a recommendation or manufactured facts to a client.
"""
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from two_integrated_runs import TracedAnalyzer,event,save
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine.floor_policy import BusinessFloorProfileResolver


async def main():
    out=ROOT/'audit/unknown_floor_fix'/datetime.now().strftime('controls_%Y%m%d_%H%M%S')
    out.mkdir(parents=True)
    analyzer=TracedAnalyzer(out);analyzer.current_case='synthetic_floor_boundary'
    budget=NetworkBudget(30);analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    resolver=BusinessFloorProfileResolver(analyzer)
    context=('Посетители приходят по предварительной записи. Нужен разрешённый допуск записанных посетителей '
        'и понятный путь непосредственно к двери арендуемого кабинета с 10:00 до 21:00. '
        'Ограничений по этажам нет.')
    profile=await resolver.resolve('Контроль доступа для посетителей по записи',context,
        requirement_quotes=(),context_quotes=(context,))
    access=('СИНТЕТИЧЕСКОЕ ОБЪЯВЛЕНИЕ ДЛЯ ПРОВЕРКИ, НЕ РЕАЛЬНЫЙ ЛОТ. '
        'К арендуемому кабинету допускаются посетители по предварительной записи ежедневно с 10:00 до 21:00. '
        'Посетитель входит с улицы через вход для посетителей, затем по обозначенному коридору проходит '
        'непосредственно к двери арендуемого кабинета. Других этапов и ограничений прохода нет. '
        'Клиент проходит этот путь самостоятельно, без согласования с сотрудником. '
        'Точный номер этажа не опубликован.')
    items=[dict(id=1,source='synthetic',external_id='access_proven',floor=None,title='Контроль: допуск и путь описаны',description=access),
           dict(id=2,source='synthetic',external_id='access_unknown',floor=None,title='Контроль: данных доступа нет',
                description='СИНТЕТИЧЕСКОЕ ОБЪЯВЛЕНИЕ ДЛЯ ПРОВЕРКИ. Сдаётся кабинет 35 м². Данных о допуске и пути нет.')]
    started=time.perf_counter()
    assessed=await resolver.assess_many(items,profile,timeout_s=90)
    explicit=await resolver.resolve('Контроль явного этажа',context,allowed_floors=(1,),
        requirement_quotes=(),context_quotes=(context,))
    constrained=await resolver.assess_many([items[0]],explicit,timeout_s=90)
    records=[dict(property_data=p,assessment=asdict(assessed[str(p['id'])])) for p in items]
    checks=dict(
        unknown_number_with_proven_route_is_eligible=assessed['1'].status=='eligible',
        eligible_has_independent_proof=assessed['1'].semantic_review_status=='supported'
            and bool(assessed['1'].inference_metadata.get('proof_verifications')),
        missing_access_not_approved=assessed['2'].status=='needs_verification',
        explicit_floor_constraint_kept=constrained['1'].analysis_status=='insufficient_facts'
            and constrained['1'].status=='needs_verification',
    )
    peak=max((sum(r['start']<=v['start']<r['start']+60 for v in budget.records) for r in budget.records),default=0)
    checks['rpm_within_40']=peak<=40
    result=dict(scope=__doc__,profile=asdict(profile),records=records,explicit=asdict(constrained['1']),checks=checks,
        seconds=round(time.perf_counter()-started,2),http_attempts=len(budget.records),peak_rpm=peak,http=budget.records)
    save(out/'result.json',result)
    (out.parent/'CONTROLS_LATEST.txt').write_text(str(out),encoding='utf-8')
    event('synthetic_controls_complete',directory=str(out),checks=checks,seconds=result['seconds'],
        statuses=[c['assessment']['status'] for c in records])


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
