"""Bounded live contract check; synthetic cards, no new market collection."""
import asyncio
import argparse
import copy
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'));sys.path.insert(0,str(ROOT/'tests'))
from two_integrated_runs import TracedAnalyzer,save,event,fingerprints
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine.ranking_policy import RankingProfileResolver,validate_profile
from real_estate_engine.competitor_classification import ConceptCompetitorClassifier
from test_engine_regressions import business_card


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--reuse-profile');args=parser.parse_args()
    base=ROOT/'audit/source_repairs_2026-11-04'
    out=base/datetime.now().strftime('run_%H%M%S_%f');out.mkdir(parents=True)
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    frozen=fingerprints();save(out/'manifest.json',dict(scope=__doc__,fingerprints=frozen,http_budget=8,
        reused_profile=args.reuse_profile,predeclared_controls=dict(street=dict(synthetic_public=['direct'],synthetic_internal=['unrelated']),
            internal=dict(synthetic_internal=['direct'],synthetic_public=['unrelated','indirect']))))
    os.environ['NVIDIA_MIN_REQUEST_INTERVAL_S']='4'
    analyzer=TracedAnalyzer(out);budget=NetworkBudget(8);wrapped=budget.wrap(analyzer.http_session.post)
    def recorded_post(*args,**kwargs):
        try:return wrapped(*args,**kwargs)
        finally:save(out/'http_records.json',budget.records)
    analyzer.http_session.post=recorded_post
    concept=('Студия консультаций. Клиенты приезжают по записи, привлекаю их через интернет. '
        'Случайный поток прохожих не нужен. Работаем с 8:30 до 20:30. '
        'Главный приоритет — низкие полные расходы на помещение. Затем важно конкурентное окружение. '
        'Предпочтительна площадь 30–100 м².')
    analyzer.current_case='booking_clock_and_waiver'
    if args.reuse_profile:
        path=Path(args.reuse_profile).resolve()
        if not path.is_relative_to((ROOT/'audit').resolve()):raise ValueError('Profile must come from an audit artifact')
        raw=json.loads(path.read_text(encoding='utf-8'))
        if raw['concept']!=concept:raise ValueError('Reused profile has a different concept')
        profile=validate_profile({k:raw[k] for k in ('status','summary','criteria','questions','assumptions','visit_context_quotes','unmodeled_requirements')},
            concept,raw['model'],raw['metadata'])
    else:profile=await RankingProfileResolver(analyzer).resolve('Студия консультаций',concept,{'max_price_month':200000},timeout_s=90)
    save(out/'profile.json',asdict(profile))
    record=dict(status=profile.status,analysis_status=profile.analysis_status,replayed_from=args.reuse_profile)
    if profile.status=='ready':
        config=profile.metadata['market_context']
        record.update(flow_weight=config['audience']['transit_flow']['weight'],
            workers_weight=config['audience']['workers']['weight'],residents_weight=config['audience']['residents']['weight'],
            hour_coverage=config['hour_coverage'],hours_source_quotes=config['hours_source_quotes'])
        record['passed']=(record['flow_weight']==0 and record['workers_weight']>0 and record['residents_weight']>0 and
                          config['hour_coverage'][8]==.5 and config['hour_coverage'][20]==.5 and
                          sum(config['hour_coverage'])==12)
    else:record['passed']=False
    event('profile_completed',**record)
    cards=[business_card(org_id='synthetic_public',title='Синтетический контроль: кофе навынос',address='Тестовая улица, 1',
        features=['Отдельный вход с улицы, любой прохожий может купить кофе навынос']),
        business_card(org_id='synthetic_internal',title='Синтетический контроль: кофе сотрудникам',address='Внутри БЦ Контрольный, Тестовая улица, 2',
        features=['Доступ только сотрудникам БЦ Контрольный, внешние посетители не допускаются'])]
    save(out/'synthetic_cards.json',[asdict(c) for c in cards]);classifier=ConceptCompetitorClassifier();classifications=[]
    for key,text in [('street','Кофейня навынос. Покупатели — любые случайные прохожие с улицы, покупают быстро у уличного входа.'),
                     ('internal','Кофейня внутри БЦ Контрольный по адресу Тестовая улица, 2. Покупатели — только сотрудники, уже находящиеся внутри, покупают в перерыве без выхода из здания.')]:
        analyzer.current_case='classify_'+key;before=len(budget.records)
        parsed,model,metadata=await classifier.classify(cards,'Кофейня',text,analyzer.model,analyzer._call_competitor_structured_sync,
            pacer=analyzer.floor_request_pacer)
        cold_calls=len(budget.records)-before
        changed=copy.deepcopy(cards);changed[0].distance_m=10;changed[0].rating=5
        before=len(budget.records)
        warm,_,warm_meta=await classifier.classify(changed,'Кофейня',text,analyzer.model,analyzer._call_competitor_structured_sync,
            pacer=analyzer.floor_request_pacer)
        item=dict(case=key,concept=text,classification=parsed,model=model,metadata=metadata,
            cold_http_attempts=cold_calls,warm_http_attempts=len(budget.records)-before,warm_metadata=warm_meta,
            all_organizations_classified=metadata['unknown_count']==0,
            cache_reused_without_network=len(budget.records)==before and warm_meta['cache_hits']==len(cards))
        levels={cards[e['idx']-1].org_id:e['level'] for e in parsed['categories']}
        expected=dict(synthetic_public=['direct'],synthetic_internal=['unrelated']) if key=='street' else dict(
            synthetic_internal=['direct'],synthetic_public=['unrelated','indirect'])
        item['predeclared_control_passed']=all(levels.get(org) in allowed for org,allowed in expected.items())
        classifications.append(item);save(out/f'{key}_classification.json',item)
        event('classification_completed',case=key,cold_http_attempts=cold_calls,
            levels=[r['level'] for r in parsed['categories']],cache_reused=item['cache_reused_without_network'],
            control_passed=item['predeclared_control_passed'])
    summary=dict(profile=record,classifications=classifications,http_attempts=len(budget.records),
        http_429=sum(r.get('status')==429 for r in budget.records),sources_and_working_database_unchanged=frozen==fingerprints())
    save(out/'summary.json',summary);save(out/'http_records.json',budget.records)
    event('repair_checks_completed',directory=str(out),profile_passed=record['passed'],
        http_attempts=len(budget.records),http_429=summary['http_429'],unchanged=summary['sources_and_working_database_unchanged'])


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
