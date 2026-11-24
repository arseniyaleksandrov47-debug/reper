"""Cold live AI on predeclared synthetic concepts and premises facts.

These checks test interpretation and safeguards, not actual market demand.
No listing collection, production database writes or third-party contact.
"""
import asyncio
import argparse
import copy
import hashlib
import logging
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'));sys.path.insert(0,str(ROOT/'tests'))
from two_integrated_runs import TracedAnalyzer,event,save
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine import RankingProfileResolver
from real_estate_engine.floor_policy import BusinessFloorProfileResolver,floor_candidate_key
from real_estate_engine.premises_requirements import extract_requirements,physical_context_quotes
from real_estate_engine.scenario_contract import access_requirement_quotes
from real_estate_engine.ranking_policy import METRICS
from real_estate_engine.ai_contract import AI_DEADLINE
from real_estate_engine.storage import WORKSPACE_DB_PATH
from test_engine_regressions import candidate


CASES=[
    dict(key='internal_coffee',business='Кофейня для сотрудников бизнес-центра',primary='market_fit_index',
        area=(15,40),rent=120000,floor=8,expected_floor='eligible',
        physical=['Обязательна точка подключения воды внутри арендуемого блока.',
                  'Желательна точка подключения проводного интернета.'],
        concept=('Кофейня для сотрудников бизнес-центра. Покупатели уже работают на восьмом этаже и приходят в перерывах с 09:00 до 18:00. '
            'Внешние покупатели не обслуживаются. Случайный поток прохожих не нужен. Этаж не ограничиваю. '
            'Сотрудникам нужен разрешённый проход к двери помещения в часы работы. '
            'Главный приоритет — внутренняя аудитория именно этой площадки; затем важны расходы на аренду и эксплуатацию. '
            'Обязательна точка подключения воды внутри арендуемого блока. Желательна точка подключения проводного интернета. '
            'Ремонт рассчитываю сам. Предпочтительна площадь 15–40 м², аренда до 120000 рублей в месяц.'),
        description=('Предлагаемый блок на восьмом этаже БЦ. Все сотрудники этого этажа имеют разрешённый свободный проход '
            'из рабочего холла непосредственно к двери арендуемого блока с 09:00 до 18:00. Внешние посетители не допускаются. '
            'Внутри арендуемого блока есть точка подключения воды и проводного интернета.')),
    dict(key='instrument_booking',business='Мастерская электронных музыкальных инструментов',primary='premises_cost_month',
        area=(30,80),rent=150000,floor=9,expected_floor='eligible',
        physical=['Обязателен проём входа шириной не менее 1,1 м.',
                  'Желательна возможность подключения трёхфазного питания.'],
        concept=('Мастерская электронных музыкальных инструментов. Клиенты приезжают по предварительной записи с 11:00 до 20:00. '
            'Случайный поток прохожих не нужен. Этаж не ограничиваю. Пропуск допустим. '
            'Нужен разрешённый проход записанных клиентов к двери в рабочие часы. '
            'Главный приоритет — низкие расходы на аренду и эксплуатацию, затем доступная аудитория жителей. '
            'Обязателен проём входа шириной не менее 1,1 м. Желательна возможность подключения трёхфазного питания. '
            'Ремонт и обустройство рассчитываю сам. Предпочтительна площадь 30–80 м², аренда до 150000 рублей в месяц.'),
        description=('Предлагаемое помещение на девятом этаже. Записанным клиентам заранее оформляют пропуска; '
            'вход и проход до двери разрешены с 11:00 до 20:00. Пассажирский лифт довозит до девятого этажа, '
            'от лифта ведёт открытый коридор. Ширина проёма входа в арендуемое помещение 1,2 м. Есть трёхфазное питание.')),
    dict(key='courier_cargo',business='Пункт отправки комплектующих',primary='premises_cost_month',
        area=(40,120),rent=180000,floor=4,expected_floor='rejected',
        physical=['Обязателен путь для неразборного груза массой 350 кг от разгрузки до двери.',
                  'Желательна возможность крепления стеллажей к стенам.'],
        concept=('Пункт отправки комплектующих. Все заказы поступают онлайн. Покупатели в помещение не приходят. '
            'Сотрудники и курьеры работают с 08:00 до 22:00. Случайный поток прохожих не нужен. Этаж не ограничиваю. '
            'Нужен разрешённый проход сотрудников и курьеров к двери в часы работы. '
            'Главный приоритет — низкие расходы на аренду и эксплуатацию, затем доступность доставки. '
            'Обязателен путь для неразборного груза массой 350 кг от разгрузки до двери. '
            'Желательна возможность крепления стеллажей к стенам. Ремонт рассчитываю сам. '
            'Предпочтительна площадь 40–120 м², аренда до 180000 рублей в месяц.'),
        description=('Арендуемый блок на четвёртом этаже. Сотрудникам и курьерам разрешён проход от разгрузки до двери '
            'с 08:00 до 22:00. Для арендуемого блока единственный путь любого неделимого груза от разгрузки '
            'проходит через грузовой лифт грузоподъёмностью 200 кг. Нагрузка свыше 200 кг запрещена, '
            'альтернативного пути или подъёмника нет. Стеллажи разрешено крепить к стенам.')),
]


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--case',choices=[c['key'] for c in CASES])
    parser.add_argument('--cargo-variants',action='store_true')
    args=parser.parse_args();selected=[c for c in CASES if args.case is None or c['key']==args.case]
    if args.cargo_variants:
        selected=[]
        for capacity,expected in ((600,'eligible'),(300,'rejected'),(None,'needs_verification')):
            case=copy.deepcopy(next(c for c in CASES if c['key']=='courier_cargo'))
            case.update(key='cargo_420_'+str(capacity),expected_floor=expected)
            case['concept']=case['concept'].replace('350','420')
            case['physical']=[q.replace('350','420') for q in case['physical']]
            source=('Арендуемый блок на четвёртом этаже. Сотрудникам и курьерам разрешён проход от разгрузки до двери '
                'с 08:00 до 22:00. Для арендуемого блока единственный путь неделимого груза от разгрузки до двери '
                'проходит через грузовой лифт. ')
            source+=(f'В паспорте лифта предельная нагрузка {capacity} кг. Превышение {capacity} кг запрещено, '
                'альтернативного пути или подъёмника нет. ' if capacity is not None else
                'Грузоподъёмность лифта не опубликована. Альтернативный путь не описан. ')
            case['description']=source+'Стеллажи разрешено крепить к стенам.'
            selected.append(case)
    out=ROOT/'audit/diverse_scenarios'/datetime.now().strftime('run_%Y%m%d_%H%M%S')
    out.mkdir(parents=True);(out.parent/'LATEST.txt').write_text(str(out),encoding='utf-8')
    before=hashlib.sha256(WORKSPACE_DB_PATH.read_bytes()).hexdigest()
    save(out/'manifest.json',dict(scope=__doc__,cases=selected,http_budget=60))
    os.environ['NVIDIA_REQUESTS_PER_MINUTE']='40';os.environ['NVIDIA_MIN_REQUEST_INTERVAL_S']='1.6'
    analyzer=TracedAnalyzer(out);budget=NetworkBudget(60)
    analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    resolver=RankingProfileResolver(analyzer);floor_resolver=BusinessFloorProfileResolver(analyzer)
    rows=[];started=time.monotonic();requirements_cache={}
    for case in selected:
        analyzer.current_case=case['key'];tick=time.monotonic()
        constraints=dict(min_area_sqm=case['area'][0],max_area_sqm=case['area'][1],max_price_month=case['rent'])
        profile=await resolver.resolve(case['business'],case['concept'],constraints,timeout_s=90)
        save(out/(case['key']+'_profile.json'),asdict(profile))
        row=dict(case=case['key'],profile=profile.status,analysis_status=profile.analysis_status)
        if profile.status=='ready':
            token=AI_DEADLINE.set(time.monotonic()+90)
            stage='physical_extraction'
            try:
                if case['concept'] not in requirements_cache:
                    requirements_cache[case['concept']]=await extract_requirements(analyzer,case['concept'],physical_context_quotes(profile))
                requirements=copy.deepcopy(requirements_cache[case['concept']])
                save(out/(case['key']+'_requirements.json'),requirements)
                quotes={r['client_quote'] for r in requirements}
                weights={c.metric:c.weight for c in profile.criteria}
                market=profile.metadata['market_context']['audience']
                scenario=profile.metadata['scenario_contract']
                stage='floor_assessment'
                obligations,_=access_requirement_quotes(scenario,profile.criteria,{k:v[1] for k,v in METRICS.items()})
                context='; '.join(dict.fromkeys(r['quote'] for group in ('context','premises_requirements','waivers') for r in scenario[group]))
                floor_profile=await floor_resolver.resolve(case['business'],context,requirement_quotes=obligations,
                    context_quotes=tuple(r['quote'] for r in scenario['context']),waiver_quotes=tuple(r['quote'] for r in scenario['waivers']))
                prop=candidate(external_id=case['key'],title='Синтетический контроль, не реальное объявление',
                    floor=case['floor'],area_sqm=sum(case['area'])/2,price_month=case['rent']/2,description=case['description'])
                decisions=await floor_resolver.assess_many([prop],floor_profile,timeout_s=60)
                assessment=decisions[floor_candidate_key(prop)]
                save(out/(case['key']+'_floor.json'),asdict(assessment))
                row.update(weights=weights,physical_quotes=sorted(quotes),transit_weight=market['transit_flow']['weight'],
                    site_weight=market['site_audience']['weight'],floor_status=assessment.status,
                    expected_floor=case['expected_floor'],floor_reason=assessment.reason,
                    checks=dict(primary_priority=max(weights,key=weights.get)==case['primary'],
                        physical_scope=quotes==set(case['physical']),transit_waiver=market['transit_flow']['weight']==0,
                        floor_scenario=assessment.status==case['expected_floor'],
                        internal_audience=market['site_audience']['weight']>0 if case['key']=='internal_coffee' else True))
            except Exception as exc:row.update(error_type=type(exc).__name__,stage=stage,
                error_detail=str(exc) if isinstance(exc,ValueError) else None,checks={'completed':False})
            finally:AI_DEADLINE.reset(token)
        else:row['checks']={'profile_ready':False}
        row['seconds']=round(time.monotonic()-tick,2);rows.append(row)
        save(out/'progress.json',rows);event('scenario_complete',**row)
    starts=[r['start'] for r in budget.records]
    peak=max((sum(t-60<s<=t for s in starts) for t in starts),default=0)
    summary=dict(scope=__doc__,cases=rows,passed=all(all(r['checks'].values()) for r in rows),
        http_attempts=len(budget.records),peak_rpm=peak,seconds=round(time.monotonic()-started,2),
        original_database_unchanged=before==hashlib.sha256(WORKSPACE_DB_PATH.read_bytes()).hexdigest())
    save(out/'http.json',budget.records);save(out/'summary.json',summary)
    event('diverse_scenarios_complete',directory=str(out),**summary)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
