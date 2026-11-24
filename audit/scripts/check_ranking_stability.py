"""Choice-consistency regressions and one optional live profile contract.

No listing collection, notification or production-DB writes. All market facts
and client utility anchors in this audit are explicitly synthetic.
"""
import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
from dataclasses import asdict,replace
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from real_estate_engine import NvidiaCompetitorAnalyzer,RankingProfileResolver,OptimalPropertyRecommender
from real_estate_engine.ranking_policy import bind_reference_scales,evaluate_profile
from real_estate_engine.business_model import concept_digest
from real_estate_engine.traffic import FootTrafficEstimator
from test_concept_ranking import profile,criterion,CONCEPT
from test_ranking_stability import example_pairs,anchors
from test_business_model import model_data,evidence,modeled_property
from test_engine_regressions import competition_report,LAT,LNG

LIVE_CONCEPT=('Кофейня навынос. Покупатели приходят пешком с улицы. Главный приоритет — возможные заказы '
              'с учётом доступной аудитории, маршрутов и конкурентов. Затем важны все расходы на помещение, '
              'включая эксплуатацию и подготовку. Площадь 30–70 м².')


def fingerprint():
    files=[ROOT/'run_engine.py',*(ROOT/'real_estate_engine').rglob('*.py')]
    db=ROOT/'data/moscow_commercial.db'
    if db.exists():files.append(db)
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');args=parser.parse_args()
    base=ROOT/'audit/ranking_stability_2026-11-04';base.mkdir(exist_ok=True)
    out=base/datetime.now().strftime('run_%H%M%S_%f');out.mkdir()
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    frozen=fingerprint();pairs=example_pairs()
    p=profile([criterion('orders_daily',.7,high=100),criterion('premises_cost_month',.3,'lower',high=120000)])
    rank=OptimalPropertyRecommender._score_and_rank_candidates
    records=[]
    for label,items in [('two_candidates',pairs[:2]),('with_dominated_expensive_candidate',pairs)]:
        ranked=rank(items,ranking_profile=p)
        records.append(dict(case=label,scores=[dict(id=c.property_data['id'],score=c.unrounded_total_score) for c in ranked]))
    assert records[0]['scores'][0]['id']==records[1]['scores'][0]['id']==2
    assert records[0]['scores']==[c for c in records[1]['scores'] if c['id'] in (1,2)]
    unknown=replace(p,metadata={'scale_origin':'ai_placeholders'})
    unscored=evaluate_profile(bind_reference_scales(unknown),*pairs[0])
    assert unscored['lower']==0 and unscored['upper']==100 and unscored['coverage']==0
    supplied=anchors(orders_daily=(0,100),premises_cost_month=(0,120000))
    restored=evaluate_profile(bind_reference_scales(unknown, ranking_scales=supplied),*pairs[0])
    assert restored['lower']==70 and restored['upper']==70
    empty=FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,[])
    assert empty.estimated_daily_pedestrians is None and empty.traffic_index is None
    summary=dict(counterexample_fixed=True,known_scores_preserved=True,missing_scale_not_fabricated=True,
                 explicit_scales_restored_score=True,empty_traffic_unknown=True,unknown_metro_not_substituted=True,
                 live_requested=args.live,synthetic_facts=True)
    (out/'controlled.json').write_text(json.dumps(dict(rankings=records,missing_scale=unscored,
        supplied_scale=restored,empty_traffic=asdict(empty)),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    if args.live:
        class TracedAnalyzer(NvidiaCompetitorAnalyzer):
            def _call_ranking_profile_structured_sync(self,prompt,max_tokens,schema):
                response=super()._call_ranking_profile_structured_sync(prompt,max_tokens,schema)
                raw,_=response
                (out/f'wire_{time.time_ns()}.json').write_text(raw,encoding='utf-8')
                return response
        analyzer=TracedAnalyzer();resolver=RankingProfileResolver(analyzer)
        started=time.perf_counter();live_profile=await resolver.resolve('Кофейня',LIVE_CONCEPT,timeout_s=25)
        attempts=analyzer.floor_request_pacer.http_attempts
        repeated=await resolver.resolve('Кофейня',LIVE_CONCEPT,timeout_s=25) if live_profile.status=='ready' else None
        summary.update(live_profile_ready=live_profile.status=='ready',analysis_status=live_profile.analysis_status,
            live_seconds=round(time.perf_counter()-started,3),http_attempts=analyzer.floor_request_pacer.http_attempts,
            http_429=analyzer.floor_request_pacer.throttle_responses,
            cache_avoids_http=repeated is not None and repeated==live_profile and attempts==analyzer.floor_request_pacer.http_attempts)
        prop=modeled_property(data=evidence(model_data(LIVE_CONCEPT)));rep=competition_report()
        if live_profile.status=='ready':
            reference=dict(concept_sha256=concept_digest(LIVE_CONCEPT),metrics={
                'premises_cost_month':dict(low=0,high=200000,source='Synthetic client cost target, not market calibration')})
            bound=bind_reference_scales(live_profile, ranking_scales=reference)
            result=evaluate_profile(bound,prop,rep)
            summary['demand_and_cost_selected']={'orders_daily','premises_cost_month'}<={c.metric for c in live_profile.criteria}
            summary['live_scores_finite']=0<=result['lower']<=result['upper']<=100
            (out/'live_profile_and_score.json').write_text(json.dumps(dict(profile=asdict(bound),score=result),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        else:
            (out/'live_error.json').write_text(json.dumps(asdict(live_profile),ensure_ascii=False,indent=2),encoding='utf-8')
    summary['source_and_database_unchanged']=frozen==fingerprint()
    summary['interpretation']='Scenario arithmetic and AI contract; not calibrated profit, market optimum or notification latency.'
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
