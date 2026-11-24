"""Replay fixed primary/reviewer responses; obtain one fresh isolated proof.

This is a targeted rejection-regression check, not a fresh full search. Source
quotes, candidate facts and client profile are reused without alteration.
"""
import argparse,asyncio,json,logging,os,sys,time
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from real_estate_engine.ai_contract import AITextResponse
from archived_floor import load_floor_profile
from real_estate_engine.floor_policy import BusinessFloorProfileResolver,floor_candidate_key
from expanded_scenario_matrix import NetworkBudget
from two_integrated_runs import TracedAnalyzer,fingerprints,save,event


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('source_directory');args=parser.parse_args()
    source=Path(args.source_directory).resolve()
    if not source.is_relative_to((ROOT/'audit').resolve()):raise ValueError('Source must be inside audit')
    saved=json.loads((source/'cargo_weak_lift_report.json').read_text(encoding='utf-8'))
    review=saved['floor_verification_candidates'][0]
    prop=review['property_data'];profile=load_floor_profile(saved['floor_profile'])
    answers=[]
    for path in sorted(source.glob('ai_cargo_weak_lift_*.json')):
        response=json.loads(path.read_text(encoding='utf-8'))
        if 'assessments' in json.loads(response['text']):answers.append((path,response))
    primary=next(item for item in answers if 'nemotron' in item[1]['model'])
    independent=next(item for item in answers if 'poolside' in item[1]['model'])
    base=ROOT/'audit/expanded_scenarios_2026-11-04'
    out=base/datetime.now(timezone(timedelta(hours=3))).strftime('rejection_%H%M%S_%f');out.mkdir()
    os.environ['NVIDIA_MIN_REQUEST_INTERVAL_S']='4';frozen=fingerprints();budget=NetworkBudget(2)
    class ReplayAnalyzer(TracedAnalyzer):
        def __init__(self):
            super().__init__(out);self.current_case='cargo_proof';self.http_session.post=budget.wrap(self.http_session.post);self.replayed=[]
        def _call_floor_structured_sync(self,prompt,max_tokens,schema,review=False):
            task=json.loads(prompt.split('\nINPUT_JSON\n',1)[1])['task']
            if task in ('assess_floor_suitability','review_floor_decisions'):
                path,answer=independent if task=='review_floor_decisions' else primary
                self.replayed.append(dict(task=task,source=str(path)))
                return AITextResponse(answer['text'],answer['model'],dict(audit_replayed_from=str(path)))
            return super()._call_floor_structured_sync(prompt,max_tokens,schema,review=review)
    analyzer=ReplayAnalyzer();resolver=BusinessFloorProfileResolver(analyzer)
    started=time.perf_counter();result=(await resolver.assess_many([prop],profile,timeout_s=60))[floor_candidate_key(prop)]
    record=dict(expected='rejected',actual=result.status,exact=result.status=='rejected',seconds=round(time.perf_counter()-started,2),
                primary_and_review_replayed=analyzer.replayed,agreed_rejection_ids=result.inference_metadata.get('agreed_rejection_ids',[]),
                fresh_proof_groups=[dict(verdict=p['verdict'],claims=[c['requirement_id'] for c in p['claims']]) for p in result.inference_metadata.get('proof_verifications',[])],
                http_attempts=len(budget.records),http_429=sum(r.get('status')==429 for r in budget.records),
                source_and_working_database_unchanged=frozen==fingerprints(),source_directory=str(source))
    save(out/'assessment.json',asdict(result));save(out/'summary.json',record);save(out/'http_records.json',budget.records)
    (base/'REJECTION_LATEST.txt').write_text(str(out),encoding='utf-8');event('rejection_replay_completed',**record)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
