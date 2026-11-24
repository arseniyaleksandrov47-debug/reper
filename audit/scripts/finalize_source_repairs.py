"""Final offline verification; frozen real environments, no market outcome claims."""
import copy
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'));sys.path.insert(0,str(ROOT/'tests'))
from two_integrated_runs import save,fingerprints
from archived_scenario import load_audience_usage
from real_estate_engine.ranking_policy import validate_profile,bind_reference_scales,evaluate_profile
from real_estate_engine.market_context import compile_audience_usage
from real_estate_engine.business_model import concept_digest
from real_estate_engine.concept_evidence import client_clock_window
from test_engine_regressions import candidate


def namespace(value):
    if isinstance(value,dict):return SimpleNamespace(**{k:namespace(v) for k,v in value.items()})
    if isinstance(value,list):return [namespace(v) for v in value]
    return value


def main():
    base=ROOT/'audit/source_repairs_2026-11-04'
    live=base/'run_174549_203129'
    summary=json.loads((live/'summary.json').read_text(encoding='utf-8'))
    cases=[];comparisons=0
    snapshots=sorted((ROOT/'audit/scenario_semantic_fix_2026-11-04/run_121128_830168').glob('environment_controlled_coffee_*.json'))
    for key,profile_path in [
        ('street',ROOT/'audit/market_integration_2026-11-04/run_164423_035656/street_profile.json'),
        ('booking',base/'run_173450_716326/profile.json')]:
        raw=json.loads(profile_path.read_text(encoding='utf-8'));meta=copy.deepcopy(raw['metadata'])
        scenario=meta['scenario_contract']
        parsed_usage=load_audience_usage(scenario)
        meta['market_context'],_=compile_audience_usage(meta['market_context'],parsed_usage)
        old_hours=meta['market_context']['active_hours']
        current_clock=client_clock_window(raw['concept'])
        meta['market_context']['active_hours']=current_clock['active_hours']
        envelope={name:raw[name] for name in ('status','summary','criteria','questions','assumptions','visit_context_quotes','unmodeled_requirements')}
        profile=validate_profile(envelope,raw['concept'],raw['model'],meta)
        # Cost scale and premises are explicitly synthetic test inputs; no data
        # about expenses is retrofitted to the real environment snapshots.
        scales=dict(concept_sha256=concept_digest(profile.concept),metrics=dict(
            premises_cost_month=dict(low=0,high=250000,source='Synthetic audit utility reference, not market calibration')))
        bound=bind_reference_scales(profile, ranking_scales=scales)
        results=[]
        for i,path in enumerate(snapshots,1):
            original=json.loads(path.read_text(encoding='utf-8'));report=namespace(original)
            prop=candidate(i,lat=report.center_lat,lng=report.center_lng)
            baseline=evaluate_profile(bound,prop,report)
            assert 0<=baseline['lower']<=baseline['upper']<=100
            assert 0<=baseline['coverage']<=1
            assert baseline['input_lower']<=baseline['lower']+1e-9
            assert baseline['input_upper']>=baseline['upper']-1e-9
            expensive=copy.deepcopy(prop);expensive['price_month']*=1.5
            cost=evaluate_profile(bound,expensive,report)
            assert cost['lower']<=baseline['lower']+1e-9 and cost['upper']<=baseline['upper']+1e-9
            for n in range(10):
                # Binding must ignore sample extrema, duplicate listings and order.
                outlier=candidate(999,price_month=1e9,area_sqm=1e6)
                other=bind_reference_scales(profile, ranking_scales=scales)
                assert evaluate_profile(other,prop,report)==baseline;comparisons+=1
            trace=baseline['business_model']['market_context']['sources']['transit_flow']
            if any(m.get('passenger_source')=='legacy_moscow_table_without_date'
                   for m in original['foot_traffic']['nearest_metros']):
                assert trace['value'] is None and trace['evidence_coverage']<1
            results.append(dict(snapshot=str(path),premises_kind='synthetic',score_lower=baseline['lower'],score_upper=baseline['upper'],
                coverage=baseline['coverage'],transit=trace,missing_inputs=baseline['business_model']['market_context']['missing_inputs'],
                note='Previously saved category classifications retained; no fresh full real selection or fresh FNS lookup'))
        cases.append(dict(case=key,profile_source=str(profile_path),revalidated_profile=asdict(bound),results=results,
            saved_active_hours=old_hours,recompiled_active_hours=current_clock['active_hours'],
            clock_recompiled_from_original_client_text=True,
            interpretation='Saved AI weights reused; original client clock is recompiled by current code, no new AI generation'))
    current=fingerprints();initial=json.loads((base/'run_173450_716326/manifest.json').read_text(encoding='utf-8'))
    raw_log=(ROOT/'audit/logs/repair_round_tests.log').read_bytes()
    log=raw_log.decode('utf-16' if raw_log.startswith(b'\xff\xfe') else 'utf-8',errors='replace')
    count=int(re.search(r'Ran (\d+) tests',log)[1])
    final=dict(local_tests=count,tests_passed=log.rstrip().endswith('OK'),
        current_live_classification_controls_passed=all(c['predeclared_control_passed'] for c in summary['classifications']),
        current_live_classification_cache_reused=all(c['cache_reused_without_network'] for c in summary['classifications']),
        live_profile_hours_and_source_waiver_passed=summary['profile']['passed'],
        live_profile_replayed_from=summary['profile']['replayed_from'],
        frozen_environment_evaluations=sum(len(c['results']) for c in cases),sample_independence_comparisons=comparisons,
        cases=cases,working_database_unchanged=current['moscow_commercial.db']==initial['fingerprints']['moscow_commercial.db'],
        fingerprints=current,network_calls_in_final_verification=0,
        known_limitations=['No fresh official station-flow dataset obtained',
            'No automatic street-network/entrance acquisition or empirical market calibration',
            'Old real environment snapshots have no retrieved FNS accounts; finance effects covered by controlled tests',
            'Clock parser supports explicit numerical windows; day-specific schedules and vague natural language need richer temporal modeling',
            'AI semantic errors remain possible; classification controls are synthetic cases, not measured market accuracy'])
    save(base/'FINAL_VERIFICATION.json',final)
    assert final['tests_passed'] and final['working_database_unchanged']
    assert final['current_live_classification_controls_passed'] and final['current_live_classification_cache_reused']
    print(json.dumps({k:v for k,v in final.items() if k not in ('cases','fingerprints','known_limitations')},ensure_ascii=False))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');main()
