"""Verify the actual missing-floor repair separately from finding a suitable lot."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def read(path):return json.loads(path.read_text(encoding='utf-8'))


def main():
    base=ROOT/'audit/unknown_floor_fix'
    out=Path((base/'LATEST.txt').read_text(encoding='utf-8').strip())
    control_path=Path((base/'CONTROLS_LATEST.txt').read_text(encoding='utf-8').strip())
    first,result,controls=(read(p) for p in (out/'first_search.json',out/'result.json',control_path/'result.json'))
    reviews=first['floor_verification_candidates']
    checks=dict(
        original_database_unchanged=result['checks']['original_database_unchanged'],
        original_client_constraints_preserved=result['checks']['next_constraints_preserved'],
        fresh_profile_ready=first['ranking_profile']['status']=='ready',
        no_forced_floor_limit=first['floor_profile']['allowed_floors'] is None,
        actual_unknown_floor_candidates_present=bool(reviews) and all(c['property_data']['floor'] is None for c in reviews),
        all_actual_candidates_contextually_assessed=bool(reviews) and all(
            c['assessment']['inference_metadata'].get('primary',{}).get('wire_format')=='keyed_v5'
            and c['assessment']['analysis_status']=='valid' for c in reviews),
        old_unconditional_floor_stop_absent=all(c['assessment']['reason']!='Этаж не указан — требуется уточнение' for c in reviews),
        no_automatic_approval_of_missing_access=bool(reviews) and all(c['confirmation']['status']=='requires_confirmation' for c in reviews),
        proven_access_can_pass_without_floor_number=controls['checks']['unknown_number_with_proven_route_is_eligible'],
        passing_control_independently_proven=controls['checks']['eligible_has_independent_proof'],
        missing_access_control_remains_pending=controls['checks']['missing_access_not_approved'],
        explicit_first_floor_filter_preserved=controls['checks']['explicit_floor_constraint_kept'],
        rate_limits_respected=result['peak_rpm']<=40 and controls['peak_rpm']<=40,
    )
    outcome=dict(contextually_assessed=len(reviews),actual_ranked=len(first['ranked_candidates']),
        actual_environment_reports=len(result['environment_records']),
        actual_confirmed=sum(c['confirmation']['status']=='source_conditions_checked' for c in first['ranked_candidates']),
        controls_scope='explicitly synthetic premises; not client recommendations',
        seconds=result['seconds'],http_attempts=result['http_attempts'],peak_rpm=result['peak_rpm'])
    record=dict(checks=checks,outcome=outcome,client_run=str(out),control_run=str(control_path))
    (base/'verification.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(record,ensure_ascii=False,indent=2))
    assert all(checks.values()),'Missing-floor repair verification failed'


if __name__=='__main__':main()
