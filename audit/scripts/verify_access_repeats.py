"""Two fresh access checks on frozen real ads; a separate verified-cache replay.

No new ranking, environment collection, business outcomes or profit validation.
The pre-existing client interpretation is deliberately reused unchanged.
"""
import asyncio
import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT/'audit/scripts'))
from expanded_scenario_matrix import NetworkBudget
from two_integrated_runs import TracedAnalyzer, fingerprints, save, event
from archived_floor import load_floor_profile
from real_estate_engine.floor_policy import (BusinessFloorProfileResolver,
    FloorAssessment, floor_evidence_catalog, floor_facts)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ids', nargs='+', type=int, default=[63,80,81,205])
    parser.add_argument('--http-budget', type=int, default=24)
    parser.add_argument('--proof-guards', action='store_true')
    args = parser.parse_args()
    if not set(args.ids).issubset({63,80,81,205}) or not 1 <= args.http_budget <= 24:
        raise ValueError('Invalid bounded sample or budget')
    base = ROOT/'audit/access_evidence_2026-11-04'
    out = base/datetime.now().strftime('run_%H%M%S_%f')
    out.mkdir(parents=True)
    (base/'LATEST.txt').write_text(str(out), encoding='utf-8')
    source = ROOT/'audit/expanded_scenarios_2026-11-04/run_145641_923781/real_search.json'
    profile = load_floor_profile(json.loads(source.read_text(encoding='utf-8'))['floor_profile'])
    with closing(sqlite3.connect((ROOT/'data/moscow_commercial.db').as_uri()+'?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        all_rows = [dict(r) for r in conn.execute('SELECT * FROM properties WHERE id IN (63,80,81,205) ORDER BY id')]
    if [r['id'] for r in all_rows] != [63,80,81,205]: raise RuntimeError('Frozen sample changed')
    rows = [p for p in all_rows if p['id'] in args.ids]
    save(out/'sample.json', rows); save(out/'profile.json', asdict(profile))
    frozen = fingerprints()
    save(out/'manifest.json', dict(source_profile=str(source), fingerprints=frozen,
        scope=__doc__, predeclared_expectations={
            '63':'access may be supported by two separate entrances; no measured demand conclusion',
            '80':'access passage must be cited instead of marketing',
            '81':'street-retail flow is insufficient alone to prove the path/admission',
            '205':'source_conflict; no AI needed'}, http_limit=args.http_budget,
            proof_guards=args.proof_guards))
    os.environ['NVIDIA_MIN_REQUEST_INTERVAL_S'] = '4'
    budget = NetworkBudget(args.http_budget)
    analyzer = TracedAnalyzer(out)
    analyzer.http_session.post = budget.wrap(analyzer.http_session.post)
    records = []
    if args.proof_guards:
        for identifier, description, custom_profile in [
                (81, 'Описанный путь к двери помещения совместим с посещением по записи', profile),
                (80, 'Нужен путь до помещения без ступеней.', replace(profile,
                    request_clauses=('Нужен путь до помещения без ступеней.',)))]:
            prop = next(p for p in all_rows if p['id']==identifier)
            refs = [r for r in floor_evidence_catalog(floor_facts(prop)).values() if r['field']=='description']
            # Deliberately unproven met assertion: diagnostic input, never a real admission.
            check = dict(requirement_id='request_0', description=description, critical=True,
                         origin='explicit', result='met', evidence=refs)
            probe = FloorAssessment('eligible', (1,), 'Predeclared unproven assertion probe', 'audit', checks=[check])
            analyzer.current_case = f'proof_guard_{identifier}'
            resolver = BusinessFloorProfileResolver(analyzer)
            await resolver._verify_positive_proofs([(prop,'probe')], {str(identifier):probe}, custom_profile)
            verdicts = [p['verdict'] for p in probe.inference_metadata.get('proof_verifications', [])]
            record = dict(kind='isolated_proof_guard', id=identifier, expected='unknown', verdicts=verdicts,
                          passed=verdicts==['unknown'], resulting_status=probe.status)
            save(out/f'proof_guard_{identifier}.json', asdict(probe));records.append(record)
            event('proof_guard_completed', **record)
    for run in (1,2):
        analyzer.current_case = f'cold_{run}'
        resolver = BusinessFloorProfileResolver(analyzer, assessment_cache_path=out/f'cache_{run}.sqlite3')
        before = len(budget.records); started = time.perf_counter()
        decisions = await resolver.assess_many(rows, profile, timeout_s=100)
        save(out/f'cold_{run}.json', {key:asdict(value) for key,value in decisions.items()})
        record = dict(kind='fresh', run=run, seconds=round(time.perf_counter()-started,3),
            http_attempts=len(budget.records)-before,
            results={key:dict(status=a.status,analysis_status=a.analysis_status,
                              review=a.semantic_review_status,
                              proofs=[p['verdict'] for p in a.inference_metadata.get('proof_verifications',[])])
                     for key,a in decisions.items()})
        records.append(record); event('fresh_completed', **record)
        # Reopen cache in a new resolver; deferred ads must not be treated as verified.
        reusable = [p for p in rows if resolver._cacheable(decisions[str(p['id'])], profile)]
        warm = BusinessFloorProfileResolver(analyzer, assessment_cache_path=out/f'cache_{run}.sqlite3')
        before = len(budget.records); started = time.perf_counter()
        replay = await warm.assess_many(reusable, profile, timeout_s=10)
        cache_record = dict(kind='verified_cache_replay', run=run,
            seconds=round(time.perf_counter()-started,4), http_attempts=len(budget.records)-before,
            verified_candidates=len(reusable), all_disk_hits=bool(replay) and all(
                a.inference_metadata.get('assessment_cache')==dict(hit=True,source='disk') for a in replay.values()))
        save(out/f'warm_{run}.json', {key:asdict(value) for key,value in replay.items()})
        records.append(cache_record); event('warm_completed', **cache_record)
    summary = dict(records=records, http_attempts=len(budget.records),
        http_429=sum(r.get('status')==429 for r in budget.records),
        peak_requests_per_minute=max((sum(r['start']<=v['start']<r['start']+60 for v in budget.records)
                                      for r in budget.records), default=0),
        source_and_working_database_unchanged=frozen==fingerprints())
    save(out/'http_records.json', budget.records); save(out/'summary.json', summary)
    event('access_audit_completed', directory=str(out), **summary)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    logging.disable(logging.CRITICAL)
    asyncio.run(main())
