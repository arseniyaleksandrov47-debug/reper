"""Replay the eleven recorded inputs, then check requirement routing live.

Replay grouping uses the original observation time explicitly. No old source
is relabelled fresh for a live decision. The final unit check fetches anew.
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
import types
import typing
from dataclasses import fields,is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from two_integrated_runs import TracedAnalyzer,event,save
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine import OptimalPropertyRecommender,RadiusCompetitionReport
from real_estate_engine.floor_policy import FloorAssessment,floor_candidate_key
from real_estate_engine.ranking_policy import RankingProfile,RankingCriterion
from real_estate_engine.property_confirmation import PropertyConfirmationVerifier
from real_estate_engine.premises_requirements import physical_context_quotes
from real_estate_engine.confirmation_flow import group_verified_listings,add_admission_reason
from real_estate_engine.ai_contract import AI_DEADLINE
from real_estate_engine.storage import WORKSPACE_DB_PATH


def decode(annotation,value):
    if value is None:return None
    origin=typing.get_origin(annotation);args=typing.get_args(annotation)
    if origin in (typing.Union,types.UnionType):
        return decode(next(a for a in args if a is not type(None)),value)
    if origin in (list,tuple):
        items=[decode(args[0],v) if args else v for v in value]
        return tuple(items) if origin is tuple else items
    if origin is dict:return {k:decode(args[1],v) for k,v in value.items()}
    if is_dataclass(annotation):
        hints=typing.get_type_hints(annotation)
        return annotation(**{f.name:decode(hints.get(f.name,typing.Any),value[f.name]) for f in fields(annotation) if f.name in value})
    if isinstance(annotation,type) and issubclass(annotation,Enum):return annotation(value)
    return value


async def main():
    source=ROOT/'audit/moscow_barbershop_trial/run_20261107_113314'
    out=ROOT/'audit/selection_repairs'/datetime.now().strftime('run_%Y%m%d_%H%M%S')
    out.mkdir(parents=True);(out.parent/'LATEST.txt').write_text(str(out),encoding='utf-8')
    before=hashlib.sha256(WORKSPACE_DB_PATH.read_bytes()).hexdigest()
    request=json.loads((source/'client_request.json').read_text(encoding='utf-8'))
    prior=json.loads((source/'continued_search.json').read_text(encoding='utf-8'))
    strict=json.loads((source/'first_search.json').read_text(encoding='utf-8'))
    raw=prior['ranking_profile']
    profile=RankingProfile(**{**raw,'criteria':tuple(RankingCriterion(**c) for c in raw['criteria'])})
    inventory=json.loads((source/'fresh_matches.json').read_text(encoding='utf-8'))
    reports={}
    for path in sorted(source.glob('environment_*.json')):
        rep=decode(RadiusCompetitionReport,json.loads(path.read_text(encoding='utf-8')))
        reports.setdefault((rep.center_lat,rep.center_lng),rep)
    assessments={floor_candidate_key(c['property_data']):decode(FloorAssessment,c['assessment']) for c in strict['floor_verification_candidates']}
    pairs=[(p,reports[(p['lat'],p['lng'])]) for p in inventory]
    ranked=OptimalPropertyRecommender._score_and_rank_candidates(pairs,ranking_profile=profile,floor_assessments=assessments)
    replay_rows=[dict(id=c.property_data['id'],address=c.property_data['address'],rent=c.property_data['price_month'],
        lower=c.unrounded_total_score,upper=c.score_upper_bound,ordering_basis=c.business_model_diagnostics.get('ordering_basis')) for c in ranked]
    old=[SimpleNamespace(property_data=c['property_data'],confirmation=c['confirmation'],
        floor_assessment=decode(FloorAssessment,c['floor_assessment']),data_warnings=[]) for c in prior['ranked_candidates']]
    original_time=max(s['fetched_at'] for c in old for s in c.confirmation['sources'])+1
    with patch('real_estate_engine.confirmation_flow.time.time',return_value=original_time):
        grouped=group_verified_listings(old)
    for c in old:add_admission_reason(c)
    replay=dict(mode='recorded_inputs_not_new_live_search',count=len(pairs),ranked=replay_rows,
        previous_final_rows=len(old),grouped_rows=len(grouped),
        original_observation_clock=original_time,
        remaining_admission_reasons=[r for c in old for r in c.confirmation['reasons'] if r['code']=='admission_unknown'])
    save(out/'replay.json',replay)
    event('recorded_replay_complete',count=len(pairs),grouped_rows=len(grouped),top=replay_rows[:3])
    os.environ['NVIDIA_REQUESTS_PER_MINUTE']='40';os.environ['NVIDIA_MIN_REQUEST_INTERVAL_S']='1.6'
    analyzer=TracedAnalyzer(out);analyzer.current_case='same_client_requirements'
    budget=NetworkBudget(20);analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    verifier=PropertyConfirmationVerifier(analyzer)
    params=dict(target_business=request['business'],allowed_floors=tuple(request['allowed_floors']),
        min_area_sqm=request['min_area'],max_area_sqm=request['max_area'],max_price_month=request['max_rent'],
        physical_context_quotes=physical_context_quotes(profile))
    token=AI_DEADLINE.set(time.monotonic()+90)
    try:
        requirements=await verifier._requirements(request['concept'],params['physical_context_quotes'])
        save(out/'live_requirements.json',requirements)
        analyzer.current_case='new_first_candidate'
        verification=await asyncio.wait_for(verifier.verify(ranked[0],request['concept'],params),70)
        save(out/'live_first_candidate.json',verification)
    finally:AI_DEADLINE.reset(token)
    summary=dict(replay=dict(count=len(pairs),grouped_rows=len(grouped),new_order=replay_rows[:3]),
        live_physical_requirements=[r['client_quote'] for r in requirements],
        live_candidate=dict(address=ranked[0].property_data['address'],rent=ranked[0].property_data['price_month'],
            status=verification['status'],sources=len(verification.get('sources',[])),
            source_errors=verification.get('source_errors'),photo=verification.get('photo_analysis'),
            verification_error=verification.get('verification_error')),
        nvidia_requests=len(budget.records),http=budget.records,
        original_database_unchanged=before==hashlib.sha256(WORKSPACE_DB_PATH.read_bytes()).hexdigest())
    save(out/'summary.json',summary);event('selection_repair_check_complete',directory=str(out),**summary)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
