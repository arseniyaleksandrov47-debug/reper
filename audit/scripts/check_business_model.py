"""Small reproducible scenario audit; --live generates a current ranking profile.

All property/route/business facts below are explicitly synthetic. No property
collection or production-database writes occur. The live request contains only
the concept, constraints and supported metric registry.
"""
import argparse
import asyncio
import copy
import hashlib
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from test_business_model import model_data,evidence,modeled_property,endpoint,network_data
from test_engine_regressions import competition_report,LAT,LNG
from real_estate_engine import NvidiaCompetitorAnalyzer,RankingProfileResolver,OptimalPropertyRecommender
from real_estate_engine.business_model import analyze_business_model,concept_digest
from real_estate_engine.ranking_policy import bind_reference_scales

CONCEPT=('Кофейня навынос. Покупатели приходят пешком с улицы. Главный приоритет — возможные заказы '
         'с учётом доступной аудитории, маршрутов и конкурентов. Затем важны все расходы на помещение, '
         'включая эксплуатацию и подготовку. Площадь 30–70 м².')


def fingerprint():
    paths=[*(ROOT/'real_estate_engine').rglob('*.py'),ROOT/'run_engine.py']
    db=ROOT/'data/moscow_commercial.db'
    if db.exists():paths.append(db)
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');args=parser.parse_args()
    base=ROOT/'audit/business_model_2026-11-04';base.mkdir(exist_ok=True)
    out=base/datetime.now().strftime('run_%H%M%S_%f');out.mkdir()
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    frozen=fingerprint();rep=competition_report();records=[];pairs=[]
    for name in ('baseline','five_rivals','blocked','partial_network','missing_cost'):
        model=model_data(CONCEPT,rivals=5 if name=='five_rivals' else 0)
        data=evidence(model)
        if name in ('blocked','partial_network'):model['target_entry']=endpoint('island')
        if name=='partial_network':data['pedestrian_network']['coverage_complete']=False
        if name=='missing_cost':data['premises_cost'].pop('operating_month')
        prop=modeled_property(len(records)+1,data=data)
        metrics,details=analyze_business_model(prop,rep,CONCEPT)
        records.append(dict(case=name,metrics={k:v.as_dict() for k,v in metrics.items()},details=details))
        pairs.append((prop,rep))
    assert records[0]['metrics']['orders_daily']==dict(low=250.0,high=250.0)
    assert records[2]['metrics']['orders_daily']['high']==0
    assert records[3]['metrics']['orders_daily']==dict(low=0.0,high=250.0)
    assert records[4]['metrics']['premises_cost_month']['high'] is None
    assert records[1]['metrics']['orders_daily']['high']<records[0]['metrics']['orders_daily']['low']
    (out/'controlled_scenarios.json').write_text(json.dumps(records,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    example=evidence(model_data(CONCEPT));graph=example.pop('pedestrian_network')
    reference=dict(concept_sha256=concept_digest(CONCEPT),metrics={
        'premises_cost_month':dict(low=0,high=200000,source='Synthetic client budget, not market calibration')})
    (base/'example.synthetic.json').write_text(json.dumps(dict(properties={'cian:REPLACE_WITH_LISTING_EXTERNAL_ID':example},
        pedestrian_network=graph,ranking_scales=reference),ensure_ascii=False,indent=2),encoding='utf-8')
    summary=dict(controlled_cases=5,controlled_checks_passed=5,synthetic_facts=True,live_requested=args.live)
    if args.live:
        class TracedAnalyzer(NvidiaCompetitorAnalyzer):
            def _call_ranking_profile_structured_sync(self,prompt,max_tokens,schema):
                response=super()._call_ranking_profile_structured_sync(prompt,max_tokens,schema)
                raw,_=response
                (out/f'wire_{time.time_ns()}.json').write_text(raw,encoding='utf-8')
                return response
        analyzer=TracedAnalyzer();resolver=RankingProfileResolver(analyzer)
        started=time.perf_counter();profile=await resolver.resolve('Кофейня',CONCEPT,timeout_s=25)
        attempts=analyzer.floor_request_pacer.http_attempts
        repeat=await resolver.resolve('Кофейня',CONCEPT,timeout_s=25) if profile.status=='ready' else None
        summary.update(profile_ready=profile.status=='ready',analysis_status=profile.analysis_status,
                       seconds=round(time.perf_counter()-started,3),http_attempts=analyzer.floor_request_pacer.http_attempts,
                       http_429=analyzer.floor_request_pacer.throttle_responses,
                       cache_avoids_http=repeat is not None and attempts==analyzer.floor_request_pacer.http_attempts and profile==repeat)
        if profile.status=='ready':
            bound=bind_reference_scales(profile, ranking_scales=reference)
            ranked=OptimalPropertyRecommender._score_and_rank_candidates(pairs,ranking_profile=bound)
            summary['market_and_cost_selected']={'market_fit_index','premises_cost_month'} <= {c.metric for c in profile.criteria}
            (out/'live_profile_and_scores.json').write_text(json.dumps(dict(profile=asdict(bound),scores=[dict(id=c.property_data['id'],lower=c.total_score,
                upper=c.score_upper_bound,input_low=c.input_score_lower_bound,input_high=c.input_score_upper_bound,components=c.scoring_components) for c in ranked]),
                ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        else:
            (out/'live_profile_error.json').write_text(json.dumps(asdict(profile),ensure_ascii=False,indent=2),encoding='utf-8')
    summary['source_and_database_unchanged']=frozen==fingerprint()
    summary['interpretation']='Contract and scenario arithmetic only; not real-market measurements, calibrated demand, or profit prediction.'
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
