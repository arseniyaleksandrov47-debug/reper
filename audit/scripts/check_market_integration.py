"""Fresh NVIDIA profiles, frozen real environments, controlled input perturbations.

No new ad collection or on-site business outcomes. Frozen accounts may be absent;
counterfactual financial values are explicitly synthetic, never market evidence.
"""
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
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'));sys.path.insert(0,str(ROOT/'tests'))
from two_integrated_runs import TracedAnalyzer,save,fingerprints,event
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine.ranking_policy import RankingProfileResolver,evaluate_profile,bind_reference_scales
from real_estate_engine import OptimalPropertyRecommender
from real_estate_engine.business_model import concept_digest
from test_market_context import environment,integrated_profile,market_config
from test_engine_regressions import candidate,LAT,LNG


CASES=[('street','Кофейня навынос',
    'Кофейня навынос. Покупатели приходят пешком с улицы и из соседних домов. '
    'Главный приоритет — подходящее окружение с доступной аудиторией и возможностью конкурировать. '
    'Нужен отдельный вход на первом этаже. Затем важна низкая аренда, предпочтительна площадь 30–70 м².'),
    ('booking','Студия консультаций',
    'Студия консультаций. Клиенты приезжают по записи, привлекаю их через интернет. '
    'Случайный поток прохожих не нужен. Главный приоритет — низкие полные расходы на помещение. '
    'Затем важны удобство пешего маршрута и конкурентное окружение. Предпочтительна площадь 30–100 м².')]


def namespace(value):
    if isinstance(value,dict):return SimpleNamespace(**{k:namespace(v) for k,v in value.items()})
    if isinstance(value,list):return [namespace(v) for v in value]
    return value


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--case',choices=('all','street','booking'),default='all')
    args=parser.parse_args()
    base=ROOT/'audit/market_integration_2026-11-04';out=base/datetime.now().strftime('run_%H%M%S_%f')
    out.mkdir(parents=True);(base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    frozen=fingerprints();save(out/'manifest.json',dict(scope=__doc__,fingerprints=frozen,http_budget=8))
    os.environ['NVIDIA_MIN_REQUEST_INTERVAL_S']='4'
    analyzer=TracedAnalyzer(out);budget=NetworkBudget(8);analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    resolver=RankingProfileResolver(analyzer);records=[]
    paths=sorted((ROOT/'audit/scenario_semantic_fix_2026-11-04/run_121128_830168').glob('environment_controlled_coffee_*.json'))
    snapshots=[(p,namespace(json.loads(p.read_text(encoding='utf-8')))) for p in paths]
    for key,business,concept in CASES:
        if args.case!='all' and key!=args.case:continue
        analyzer.current_case=key
        profile=await resolver.resolve(business,concept,{'max_price_month':200000},timeout_s=90)
        save(out/f'{key}_profile.json',asdict(profile))
        record=dict(case=key,status=profile.status,analysis_status=profile.analysis_status)
        if profile.status=='ready':
            scales=dict(concept_sha256=concept_digest(concept),metrics=dict(
                premises_cost_month=dict(low=0,high=250000,source='Explicit synthetic audit utility reference; not market calibration')))
            bound=bind_reference_scales(profile, ranking_scales=scales)
            # Real environment snapshots are replayed unchanged at their actual coordinates.
            real_rows=[]
            for i,(path,report) in enumerate(snapshots):
                prop=candidate(i+1,lat=report.center_lat,lng=report.center_lng)
                score=evaluate_profile(bound,prop,report)
                real_rows.append(dict(snapshot=str(path),lower=score['lower'],upper=score['upper'],coverage=score['coverage'],
                    market=score['business_model'].get('market_context'),
                    note='Real saved environment, synthetic premises rent/area; no fresh collection or confirmed FNS accounts'))
            save(out/f'{key}_frozen_environments.json',real_rows)
            # Controlled analytical perturbations verify real arithmetic paths.
            report=environment();prop=candidate(raw_attributes=dict(business_evidence=dict(
                walk_time_minutes=dict(value=10,verified=True,source='Synthetic audit route, not observed'),
                delivery_time_minutes=dict(value=15,verified=True,source='Synthetic audit route, not observed'),
                site_audience_daily=dict(value=200,verified=True,source='Synthetic audit audience, not observed'))))
            baseline=evaluate_profile(bound,prop,report)
            variations=[]
            for label,change in [('competitor_revenue',lambda r:setattr(r.direct_competitors[0].card,'fns_revenue_rub',100000000)),
                                  ('competitor_profit',lambda r:setattr(r.direct_competitors[0].card,'fns_net_profit_rub',5000000)),
                                  ('direct',lambda r:r.direct_competitors.append(copy.deepcopy(r.indirect_competitors[0]))),
                                  ('missing_fns',lambda r:setattr(r.direct_competitors[0].card,'fns_revenue_rub',None))]:
                changed=copy.deepcopy(report);change(changed)
                if label=='direct':
                    changed.direct_competitors[-1].card.org_id='extra_synthetic_direct'
                    changed.direct_competitors[-1].card.fns_inn='extra_synthetic_direct'
                value=evaluate_profile(bound,prop,changed)
                variations.append(dict(kind=label,synthetic=True,lower=value['lower'],upper=value['upper'],coverage=value['coverage'],
                    delta_lower=value['lower']-baseline['lower']))
            save(out/f'{key}_controlled_effects.json',dict(baseline=baseline,variations=variations))
            config=profile.metadata['market_context']
            record.update(weights={c.metric:c.weight for c in profile.criteria},
                audience_weights={k:v['weight'] for k,v in config['audience'].items()},
                strength_weights={k:v['weight'] for k,v in config['strength'].items()},
                synthetic_finance_changes_score=all(abs(v['delta_lower'])>1e-12 for v in variations[:2]),
                frozen_environment_count=len(real_rows),frozen_environment_coverage=[r['coverage'] for r in real_rows])
        records.append(record);event('market_case_completed',**record)
    summary=dict(cases=records,http_attempts=len(budget.records),http_429=sum(r.get('status')==429 for r in budget.records),
        peak_requests_per_minute=max((sum(r['start']<=v['start']<r['start']+60 for v in budget.records) for r in budget.records),default=0),
        sources_and_working_database_unchanged=frozen==fingerprints())
    save(out/'summary.json',summary);save(out/'http_records.json',budget.records)
    event('market_integration_completed',directory=str(out),**summary)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
