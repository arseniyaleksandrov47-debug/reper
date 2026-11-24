"""Reproduce an observed contract gap offline against saved live decisions.

This is a diagnostic: an eligible result reproduces the defect, not a passing
correctness test. No model requests or production database writes are made.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from archived_floor import load_floor_profile
from real_estate_engine.floor_policy import (
    BusinessFloorProfileResolver,
)


def main():
    base = ROOT / 'audit/big_run_2026-11-03'
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    out = args.run or Path((base / 'LATEST.txt').read_text(encoding='utf-8'))
    observations = []
    for case in ('salon', 'pickup'):
        report = json.loads((out / f'integration/{case}.json').read_text(encoding='utf-8'))
        candidate = next(c for c in report['ranked_candidates'] if c['property_data']['id'] == 359)
        assessment = candidate['floor_assessment']
        request = next(c for c in assessment['checks'] if c['requirement_id'] == 'request_0')
        assert request['evidence'] and all(
            r['field'] in ('floor', 'offered_floors') for r in request['evidence']
        ), 'Saved fixture no longer contains the observed floor-only proof'
        payload = {
            'candidate_id': '359',
            **{k: assessment[k] for k in ('status', 'reason', 'checks', 'missing_facts')},
        }
        result = BusinessFloorProfileResolver()._validate_assessment(
            payload, candidate['property_data'], load_floor_profile(report['floor_profile'])
        )
        observations.append({
            'case': case,
            'property_id': 359,
            'condition': request['description'],
            'evidence': request['evidence'],
            'observed_status': result.status,
            'observed_analysis_status': result.analysis_status,
            'defect_reproduced': result.status == 'eligible',
            'explanation': 'A visitor condition is treated as proven by the floor number alone.',
        })
    result = {'network_used': False, 'observations': observations}
    (args.output or out / 'floor_only_reproduction_current.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
