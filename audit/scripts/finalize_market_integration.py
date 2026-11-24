"""Validate saved live profiles and input effects with final code, without network."""
import copy
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'));sys.path.insert(0,str(ROOT/'tests'))
from two_integrated_runs import save,fingerprints
from check_market_integration import namespace
from archived_scenario import load_audience_usage
from test_market_context import environment
from test_engine_regressions import candidate
from real_estate_engine.ranking_policy import validate_profile,evaluate_profile,bind_reference_scales
from real_estate_engine.market_context import compile_audience_usage
from real_estate_engine.business_model import concept_digest


def main():
    base=ROOT/'audit/market_integration_2026-11-04'
    sources=dict(street=base/'run_164423_035656',booking=base/'run_163741_868345')
    cases=[]
    for key,directory in sources.items():
        raw=json.loads((directory/f'{key}_profile.json').read_text(encoding='utf-8'))
        metadata=copy.deepcopy(raw['metadata']);scenario=metadata['scenario_contract']
        usage=load_audience_usage(scenario)
        metadata['market_context'],_=compile_audience_usage(metadata['market_context'],usage)
        envelope={k:raw[k] for k in ('status','summary','criteria','questions','assumptions','visit_context_quotes','unmodeled_requirements')}
        profile=validate_profile(envelope,raw['concept'],raw['model'],metadata)
        scales=dict(concept_sha256=concept_digest(profile.concept),metrics=dict(
            premises_cost_month=dict(low=0,high=250000,source='Synthetic audit client utility reference')))
        bound=bind_reference_scales(profile, ranking_scales=scales)
        prop=candidate(raw_attributes=dict(business_evidence=dict(walk_time_minutes=dict(
            value=10,verified=True,source='Synthetic route, not measured market data'))))
        report=environment();baseline=evaluate_profile(bound,prop,report)
        effects=[]
        for name,attr,amount in [('revenue','fns_revenue_rub',100000000),('profit','fns_net_profit_rub',5000000)]:
            changed=copy.deepcopy(report);setattr(changed.direct_competitors[0].card,attr,amount)
            value=evaluate_profile(bound,prop,changed)
            effects.append(dict(input=name,synthetic=True,delta_lower=value['lower']-baseline['lower'],
                                changes_final_score=abs(value['lower']-baseline['lower'])>1e-12))
        cfg=profile.metadata['market_context'];source_weights={k:v['weight'] for k,v in cfg['audience'].items()}
        records=json.loads((directory/f'{key}_frozen_environments.json').read_text(encoding='utf-8'))
        frozen=[]
        for record in records:
            data=json.loads(Path(record['snapshot']).read_text(encoding='utf-8'))
            score=evaluate_profile(bound,prop,namespace(data))
            frozen.append(dict(snapshot=record['snapshot'],lower=score['lower'],upper=score['upper'],coverage=score['coverage'],
                actual_account_rows=sum(c['card'].get('fns_revenue_rub') is not None for group in
                    ('direct_competitors','indirect_competitors','synergy_businesses') for c in data[group])))
        ready=dict(case=key,source_directory=str(directory),profile_revalidated=True,
            weights={c.metric:c.weight for c in profile.criteria},audience_weights=source_weights,
            fns_weights={k:v['weight'] for k,v in cfg['strength'].items() if k.startswith('company_')},
            controlled_financial_effects=effects,frozen_environment_replays=frozen)
        assert all(e['changes_final_score'] for e in effects)
        assert source_weights['transit_flow']==0 if key=='booking' else source_weights['transit_flow']>0 and source_weights['workers']>0
        cases.append(ready)
    audits=[]
    for directory in sorted(base.glob('run_*')):
        if (directory/'summary.json').is_file():
            data=json.loads((directory/'summary.json').read_text(encoding='utf-8'))
            audits.append(dict(directory=str(directory),http_attempts=data['http_attempts'],http_429=data['http_429'],
                               source_and_database_unchanged=data['sources_and_working_database_unchanged']))
    raw_log=(ROOT/'audit/logs/market_integration_tests.log').read_bytes()
    tests=raw_log.decode('utf-16' if raw_log.startswith(b'\xff\xfe') else 'utf-8',errors='replace')
    current=fingerprints()
    database_unchanged=all(json.loads((Path(a['directory'])/'manifest.json').read_text(encoding='utf-8'))[
        'fingerprints']['moscow_commercial.db']==current['moscow_commercial.db'] for a in audits)
    result=dict(cases=cases,audits=audits,http_attempts=sum(a['http_attempts'] for a in audits),
        http_429=sum(a['http_429'] for a in audits),local_tests=316,tests_passed='Ran 316 tests' in tests and tests.rstrip().endswith('OK'),
        final_fingerprints=current,working_database_unchanged=database_unchanged,network_calls_in_finalization=0,
        limitation='FNS input effects are synthetic controls. Frozen real environments contain no retrieved accounts; '
                   'this is suitability integration and intent compliance, not market calibration or proof of optimal premises.')
    save(base/'FINAL_VERIFICATION.json',result)
    assert result['tests_passed'] and database_unchanged
    print(json.dumps(dict(local_tests=316,tests_passed=True,cases=[c['case'] for c in cases],
        all_financial_effects_verified=True,http_attempts=result['http_attempts'],http_429=result['http_429']),ensure_ascii=False))


if __name__=='__main__':sys.stdout.reconfigure(encoding='utf-8');main()
