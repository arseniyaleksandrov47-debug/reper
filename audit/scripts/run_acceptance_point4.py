"""Fixed acceptance point 4: lifecycle, changes, outages and the 40 RPM queue.

Controlled failures and virtual time; no paid API calls or fabricated live data.
All regression suites run once, with per-test results retained for review.
"""
import json
import logging
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from two_integrated_runs import save,fingerprints


class RecordedResult(unittest.TextTestResult):
    def startTest(self,test):
        self._started=time.perf_counter();super().startTest(test)
    def addSuccess(self,test):
        self.records.append(dict(test=test.id(),status='passed',seconds=round(time.perf_counter()-self._started,4)))
        super().addSuccess(test)
    def addFailure(self,test,err):
        self.records.append(dict(test=test.id(),status='failed'));super().addFailure(test,err)
    def addError(self,test,err):
        self.records.append(dict(test=test.id(),status='error'));super().addError(test,err)


def main():
    base=ROOT/'audit/acceptance_point4';out=base/datetime.now().strftime('run_%H%M%S_%f');out.mkdir(parents=True)
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    frozen=fingerprints();RecordedResult.records=[]
    logging.disable(logging.CRITICAL)
    suite=unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    with (out/'tests.log').open('w',encoding='utf-8') as log:
        result=unittest.TextTestRunner(stream=log,verbosity=2,resultclass=RecordedResult).run(suite)
    records=RecordedResult.records;save(out/'test_results.json',records)
    groups={
        'first_start_restart_changed_ad_and_cache':['test_access_evidence_cache.VerifiedCacheTests'],
        'changed_concept_and_organization':['test_source_contract_repairs.OrganizationClassificationTests'],
        'source_failure_and_recovery':['test_engine_regressions','test_remaining_problems','test_proof_grounding','test_automatic_access','test_fns_acceptance'],
        'shared_40_rpm_cancellation_and_fallbacks':['test_operating_acceptance','test_isolated_proofs_and_pacing.PacingTests','test_isolated_proofs_and_pacing.AsyncPacingTests'],
        'profile_failure_without_static_substitution':['test_concept_ranking']}
    counts={k:sum(any(r['test'].startswith(p) for p in prefixes) for r in records) for k,prefixes in groups.items()}
    summary=dict(scope=__doc__,tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        skipped=len(result.skipped),group_test_counts=counts,
        source_and_working_database_unchanged=frozen==fingerprints(),
        passed=result.wasSuccessful() and all(counts.values()) and frozen==fingerprints(),
        queue='48 concurrent logical requests: 40 HTTP starts, 8 explicitly waiting; 8 resume after virtual 60 seconds. Fallbacks share the same 40-start cap.',
        failure_testing='Controlled outages and virtual time, separate from live source checks in points 1–3.')
    save(out/'summary.json',summary);print(json.dumps(dict(directory=str(out),**summary),ensure_ascii=False))
    return 0 if summary['passed'] else 1


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');raise SystemExit(main())
