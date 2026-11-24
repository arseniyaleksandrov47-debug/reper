"""Exercise a real client's serialized provisional action without replayed AI."""
import argparse
import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from two_integrated_runs import TracedAnalyzer,save,event
from full_client_trial import compact
from expanded_scenario_matrix import NetworkBudget
from real_estate_engine import RealEstateAggregator,OptimalPropertyRecommender
from real_estate_engine.cities import get_city


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--business-evidence-file',type=Path)
    args=parser.parse_args();out=args.run_dir.resolve()
    if not out.is_relative_to(ROOT/'audit'):raise ValueError('Audit output must stay inside workspace/audit')
    first=json.loads((out/'first_search.json').read_text(encoding='utf-8'))
    if not any(a['type']=='continue_unverified' for a in first['next_actions']):raise ValueError('No provisional action in this client result')
    agg=RealEstateAggregator(str(out/'fresh_inventory.sqlite3'),city=get_city(first['city']))
    analyzer=TracedAnalyzer(out);analyzer.current_case='continued_client';agg.competitor_analyzer=analyzer
    agg.recommender=OptimalPropertyRecommender(agg.storage,analyzer,city=agg.city)
    budget=NetworkBudget(120);analyzer.http_session.post=budget.wrap(analyzer.http_session.post)
    restored=SimpleNamespace(city=first['city'],next_actions=first['next_actions'],inventory_collection=first['inventory_collection'])
    business_data=json.loads(args.business_evidence_file.read_text(encoding='utf-8')) if args.business_evidence_file else None
    result=json.loads((out/'result.json').read_text(encoding='utf-8'))
    if not (out/'result_initial.json').exists():save(out/'result_initial.json',result)
    started=time.perf_counter();event('client_continues_unverified',constraints_preserved=True,
        scope='Standard serialized continuation; cold AI profile and live environment, no replayed facts/weights')
    report=await agg.continue_unverified_analysis(restored,business_data=business_data)
    attempts=result.setdefault('continuation_attempts',[])
    if (out/'continued_search.json').exists():
        previous=json.loads((out/'continued_search.json').read_text(encoding='utf-8'))
        save(out/('continuation_attempt_'+str(len(attempts))+'_prior.json'),previous)
    save(out/'continued_search.json',asdict(report))
    extra=round(time.perf_counter()-started,2);result['continued']=compact(report);result['seconds']+=extra
    attempts.append(dict(seconds=extra,status=report.selection_status,profile_status=report.ranking_profile.status if report.ranking_profile else None))
    result['continuation_seconds']=result.get('continuation_seconds',0)+extra
    result['http_attempts']+=len(budget.records);result['http']+=budget.records
    result['peak_rpm']=max((sum(r['start']<=v['start']<r['start']+60 for v in result['http']) for r in result['http']),default=0)
    result['checks']['rpm_within_40']=result['peak_rpm']<=40
    result['checks']['city_preserved']=report.city==first['city']
    save(out/'result.json',result)
    event('continuation_complete',seconds=extra,selection_status=report.selection_status,recommendation=report.recommendation,
        ranked=len(report.ranked_candidates),http_attempts=len(budget.records),peak_rpm=result['peak_rpm'])


if __name__=='__main__':
    logging.disable(logging.CRITICAL);asyncio.run(main())
