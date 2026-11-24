"""Large local audit. Network APIs are not called; old observations stay labelled."""
import asyncio
import copy
import hashlib
import io
import json
import logging
import random
import sys
import time
import unittest
from collections import Counter
from dataclasses import asdict
from datetime import datetime,timedelta,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'));sys.path.insert(0,str(ROOT/'audit/scripts'))
import expanded_offline_test as existing
import check_ai_contract as contract_audit
from real_estate_engine.floor_policy import BusinessFloorProfileResolver,floor_facts,floor_evidence_catalog
from real_estate_engine.ai_contract import normalize_floor_response,AIContractError
from floor_ai_fixtures import assessment_response,RecordingFloorInference,default_response

BASE=ROOT/'audit/big_run_2026-11-03'
MOSCOW=timezone(timedelta(hours=3))
OUT=None

def save(name,value):
    p=OUT/name;p.parent.mkdir(exist_ok=True,parents=True)
    p.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')

def sources():
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'real_estate_engine').rglob('*.py'))}

def event(name,**values):
    record=dict(event=name,time_moscow=datetime.now(MOSCOW).isoformat(),**values)
    with (OUT/'events.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(record,ensure_ascii=False)+'\n')
    print(json.dumps(record,ensure_ascii=False),flush=True)

async def contract_mutations():
    rng=random.Random(2026110306)
    resolver=BusinessFloorProfileResolver()
    failures=[];counts=Counter();valid=0
    for index in range(1000):
        floor=rng.choice([-2,-1,0,1,2,7,18,42])
        prop=dict(id=index,floor=floor,title='Тестовое помещение',description=
            f'Этаж {floor}. Клиентам по записи вход разрешён. Из входной зоны до двери ровный открытый путь, имеется лифт до этажа.')
        context=f'Клиенты приходят по записи; необходимо сохранить условие клиента номер {index}'
        profile=await resolver.resolve(f'Произвольная услуга {index}',context)
        candidate=dict(candidate_id='c0',facts=floor_facts(prop),requirements=resolver.required_checks(profile))
        response=assessment_response(candidate)
        def missing_check(d):
            required={r['requirement_id'] for r in candidate['requirements']}
            removable=[i for i,c in enumerate(d['checks']) if c['requirement_id'] in required]
            d['checks'].pop(rng.choice(removable))
        def floor_only(d):
            for c in d['checks']:
                if c['requirement_id'] in ('audience_access','route_to_unit'):c['evidence']=[{'field':'floor','value':floor}]
        mutations=[
            ('missing_condition',missing_check),
            ('missing_facts_string',lambda d:d.update(missing_facts='Неизвестен доступ')),
            ('missing_facts_null',lambda d:d.update(missing_facts=None)),
            ('missing_facts_object',lambda d:d.update(missing_facts={'gap':'доступ'})),
            ('missing_reason',lambda d:d.pop('reason')),
            ('empty_reason',lambda d:d.update(reason=' ')),
            ('invalid_status',lambda d:d.update(status='APPROVED')),
            ('duplicate_condition',lambda d:d['checks'].append(copy.deepcopy(d['checks'][0]))),
            ('invalid_critical',lambda d:d['checks'][1].update(critical=1)),
            ('condition_origin_changed',lambda d:next(c for c in d['checks'] if c['requirement_id']=='request_0').update(origin='inferred')),
            ('request_quote_changed',lambda d:next(c for c in d['checks'] if c['requirement_id']=='request_0').update(request_quote='Другой запрос')),
            ('description_changed',lambda d:next(c for c in d['checks'] if c['requirement_id']=='request_0').update(description='Можно без записи')),
            ('unsupported_floor_proof',floor_only),
            ('invented_evidence',lambda d:next(c for c in d['checks'] if c['requirement_id']=='audience_access').update(evidence=[{'field':'description','quote':'Беспрепятственный вход всем с улицы'}])),
            ('missing_evidence',lambda d:next(c for c in d['checks'] if c['requirement_id']=='route_to_unit').update(evidence=[])),
            ('invalid_result',lambda d:d['checks'][1].update(result='probably')),
            ('waive_positive_constraint',lambda d:next(c for c in d['checks'] if c['requirement_id']=='request_0').update(result='not_required',critical=False,evidence=[])),
            ('extra_field',lambda d:d.update(approved=True)),
        ]
        try:
            decision=resolver._validate_assessment(response,prop,profile)
            valid+=decision.status=='eligible'
            if decision.status!='eligible':failures.append(dict(case=index,kind='valid_control_rejected',status=decision.status))
        except Exception as e:failures.append(dict(case=index,kind='valid_control_exception',error_type=type(e).__name__))
        for label,mutate in mutations:
            changed=copy.deepcopy(response);mutate(changed);counts[label]+=1
            try:
                decision=resolver._validate_assessment(changed,prop,profile)
                if decision.status=='eligible':failures.append(dict(case=index,kind=label,status='eligible'))
            except (ValueError,TypeError,KeyError):pass
        if index and index%250==0:event('contract_progress',cases=index,failures=len(failures))
    record=dict(valid_controls=valid,valid_controls_expected=1000,mutations=sum(counts.values()),by_type=dict(counts),failures=failures,
        interpretation='adversarial wire responses with fixed fixtures; not new AI inference or semantic accuracy')
    save('contract_mutations.json',record);event('contract_mutations_completed',valid=valid,mutations=record['mutations'],failures=len(failures))
    return record

async def replay_saved_ai():
    base=ROOT/'audit/ai_contract_fix_2026-11-03'
    original=json.loads((base/'final/live_cases.json').read_text(encoding='utf-8'))
    snapshot=json.loads((base/'final/final_source_hashes.json').read_text(encoding='utf-8'))
    hashes_match=all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==value for name,value in snapshot.items())
    resolver=BusinessFloorProfileResolver();entries=[];bad=[]
    for record in original:
        profile=await resolver.resolve(record['business'],record['context'])
        for prop in record['offers']:
            saved=record['assessments'][str(prop['id'])]
            if not saved['checks']:continue
            response=dict(candidate_id='c0',status=saved['status'],reason=saved['model_reason'] or saved['reason'],
                checks=saved['checks'],missing_facts=saved['missing_facts'])
            # Only revalidate structurally valid archived conclusions. The source
            # values are frozen, not refreshed and not newly judged by a model.
            try:
                verdict=resolver._validate_assessment(response,prop,profile)
                entries.append(dict(case=record['case'],id=prop['id'],saved_status=saved['status'],revalidated_status=verdict.status,
                    analysis_status=verdict.analysis_status))
                if saved['status']!='eligible' and verdict.status=='eligible':bad.append(entries[-1])
            except Exception as exc:entries.append(dict(case=record['case'],id=prop['id'],error_type=type(exc).__name__))
    # Test persisted public observations against mutations without introducing
    # fresh semantic decisions. They are previous model outputs, not ground truth.
    real=ROOT/'audit/expanded_test_2026-11-03/fresh_sample.json'
    sample=json.loads(real.read_text(encoding='utf-8'))
    record=dict(kind='offline revalidation of previous results, not repeated live AI inference',saved_code_matches_current=hashes_match,
        archived_control_checks=36,previous_passed=34,previous_false_deferrals=['studio_upper','studio_basement'],
        available_public_sample=len(sample),revalidated=entries,new_approvals=bad)
    save('saved_ai_replay.json',record);event('saved_ai_replayed',records=len(entries),new_approvals=len(bad),same_code=hashes_match)
    return record

async def run_misc():
    started=time.perf_counter();stream=io.StringIO()
    runner=unittest.TextTestRunner(stream=stream,verbosity=2)
    # Async unittest cases must own their event loops. Run the suite in a worker
    # thread instead of nesting its loops inside this orchestrator's loop.
    result=await asyncio.to_thread(runner.run,unittest.defaultTestLoader.discover(str(ROOT/'tests')))
    (OUT/'unit_tests.log').write_text(stream.getvalue(),encoding='utf-8')
    summary=dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),seconds=round(time.perf_counter()-started,3))
    save('unit_summary.json',summary);event('unit_tests_completed',**summary)
    contract_audit.OUT=OUT/'contract_probes';contract_audit.OUT.mkdir(exist_ok=True)
    # Suppress verbose legacy output; preserve its exact contents in the run.
    import contextlib
    buffer=io.StringIO()
    with contextlib.redirect_stdout(buffer):await contract_audit.main()
    (OUT/'contract_probes.log').write_text(buffer.getvalue(),encoding='utf-8')
    return summary

async def main(resume=None):
    global OUT
    BASE.mkdir(exist_ok=True)
    if resume:
        OUT=Path(resume).resolve()
        if BASE.resolve() not in OUT.parents:
            raise ValueError('Resume path must be an audit run directory')
        manifest=json.loads((OUT/'manifest.json').read_text(encoding='utf-8'))
        if manifest['sources']!=sources():
            raise ValueError('Source code changed; start a separate run instead')
    else:
        OUT=BASE/datetime.now(MOSCOW).strftime('offline_%H%M%S_%f');OUT.mkdir()
    (BASE/'LATEST_OFFLINE.txt').write_text(str(OUT),encoding='utf-8')
    before=sources();db_hash=hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest()
    if not resume:
        save('manifest.json',dict(mode='offline_only_no_network',started_moscow=datetime.now(MOSCOW).isoformat(),sources=before,
            working_database_sha256=db_hash,seed=2026110305,approval_block='live run not executed: automatic approval review usage limit'))
    event('offline_started',output=str(OUT))
    existing.OUT=OUT;existing.RNG=random.Random(2026110305)
    # Purely local generated data. Separate output directory prevents overwriting
    # previous audit databases and measurements.
    spatial=json.loads((OUT/'spatial.json').read_text(encoding='utf-8')) if resume and (OUT/'spatial.json').exists() else existing.spatial_checks(property_count=30000,query_count=15000)
    ranking=json.loads((OUT/'ranking.json').read_text(encoding='utf-8')) if resume and (OUT/'ranking.json').exists() else await existing.ranking_checks(pool_count=1000)
    population=json.loads((OUT/'population.json').read_text(encoding='utf-8')) if resume and (OUT/'population.json').exists() else existing.population_checks(scenario_count=5000)
    mutations=json.loads((OUT/'contract_mutations.json').read_text(encoding='utf-8')) if resume and (OUT/'contract_mutations.json').exists() else await contract_mutations()
    replay=json.loads((OUT/'saved_ai_replay.json').read_text(encoding='utf-8')) if resume and (OUT/'saved_ai_replay.json').exists() else await replay_saved_ai()
    units=await run_misc()
    final=dict(mode='offline_only_no_network',generated_properties=spatial['generated_properties'],spatial_cases=spatial['spatial_cases'],
        spatial_failures=len(spatial['failures']),filter_cases=spatial['filter_cases'],filter_failures=len(spatial['filter_failures']),
        ranking_pools=ranking['pools'],ranking_comparisons=ranking['comparisons'],ranking_failures=len(ranking['failures']),
        population_cases=population['cases'],population_failures=len(population['failures']),
        contract_mutations=mutations['mutations'],contract_failures=len(mutations['failures']),valid_contract_controls=mutations['valid_controls'],
        unit_tests=units['tests'],unit_failures=units['failures']+units['errors'],new_live_requests=0,
        source_code_unchanged=before==sources(),working_database_unchanged=db_hash==hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest(),
        finished_moscow=datetime.now(MOSCOW).isoformat())
    save('summary.json',final);event('offline_finished',**final)

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--resume');args=parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main(args.resume))
