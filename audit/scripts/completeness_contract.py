"""Recheck the exact real cards rejected for empty evidence_fields.

Recorded map facts, fresh native NVIDIA classification/proof. No source
replacement, relaxed validation or invented expense/entrance facts.
"""
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from two_integrated_runs import TracedAnalyzer,save,event
from expanded_scenario_matrix import NetworkBudget
from run_acceptance_point3 import CASES
from real_estate_engine.competitors import MapBusinessCard
from real_estate_engine.competitor_classification import ConceptCompetitorClassifier
from real_estate_engine.ai_contract import AI_DEADLINE


async def main():
    origin=Path((ROOT/'audit/source_completeness/LATEST.txt').read_text('utf-8'))
    out=origin/'contract_recheck';out.mkdir()
    os.environ['NVIDIA_MIN_REQUEST_INTERVAL_S']='2.5'
    analyzer=TracedAnalyzer(out);analyzer.current_case='contract_recheck';budget=NetworkBudget(24)
    analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    classifier=ConceptCompetitorClassifier();records=[];checks=[]
    async def one(case):
        source=json.loads((origin/(case['key']+'_environment.json')).read_text('utf-8'))
        cards=[MapBusinessCard(**v) for v in source['unclassified_businesses']]
        assert cards
        token=AI_DEADLINE.set(time.monotonic()+110)
        try:
            result,model,meta=await classifier.classify(cards,case['business'],case['concept'],
                analyzer.classification_model+'|reasoning_budget='+str(analyzer.classification_reasoning_budget),
                analyzer._call_competitor_structured_sync,pacer=analyzer.floor_request_pacer,
                verify=analyzer._call_competitor_proof_structured_sync,verifier_model=analyzer.classification_proof_model)
        finally:AI_DEADLINE.reset(token)
        save(out/(case['key']+'_classification.json'),dict(result=result,model=model,metadata=meta))
        for entry in result['categories']:
            decision=meta['organization_decisions'].get(cards[entry['idx']-1].org_id)
            if entry['level']!='unknown':
                checks.append(dict(case=case['key'],check='accepted_row_has_source_fields',passed=bool(decision and decision['evidence_fields'])))
            if entry['level'] in ('direct','indirect','synergy'):
                checks.append(dict(case=case['key'],check='positive_requires_independent_proof',passed=decision.get('offer_proof',{}).get('verdict')=='supported'))
        checks.append(dict(case=case['key'],check='all_input_rows_returned',passed=len(result['categories'])==len(cards)))
        record=dict(case=case['key'],cards=len(cards),known_before=0,known_after=len(cards)-meta['unknown_count'],unknown_after=meta['unknown_count'])
        records.append(record);event('contract_recheck_case',**record)
    await asyncio.gather(*(one(c) for c in CASES))
    peak=max((sum(r['start']<=v['start']<r['start']+60 for v in budget.records) for r in budget.records),default=0)
    checks.append(dict(check='rpm_respected',passed=peak<=40))
    result=dict(scope=__doc__,origin=str(origin.relative_to(ROOT)),records=records,checks=checks,
        all_checks_passed=all(c['passed'] for c in checks),http_attempts=len(budget.records),peak_rpm=peak,http=budget.records)
    save(out/'result.json',result);event('contract_recheck_finished',passed=result['all_checks_passed'],records=records,http=len(budget.records),peak_rpm=peak)
    if not result['all_checks_passed']:raise SystemExit(1)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
