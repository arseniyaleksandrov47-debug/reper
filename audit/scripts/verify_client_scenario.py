"""Read-only checks for a saved client scenario, without requiring a winner."""
import argparse
import json
import math
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def read(path):return json.loads(path.read_text(encoding='utf-8'))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--base',type=Path,required=True)
    args=parser.parse_args();base=ROOT/args.base
    out=Path((base/'LATEST.txt').read_text(encoding='utf-8').strip())
    request,result,first=(read(out/name) for name in ('client_request.json','result.json','first_search.json'))
    second=read(out/'next_search.json') if (out/'next_search.json').exists() else None
    reports=[first]+([second] if second else [])
    def all_candidates(report):
        rows=[c for key in ('ranked_candidates','floor_verification_candidates','floor_rejected_candidates') for c in report[key]]
        rows.extend(report['confirmation_rejected_candidates'])
        return rows
    def within(prop):
        lat1,lon1,lat2,lon2=map(math.radians,(first['center_lat'],first['center_lng'],prop['lat'],prop['lng']))
        a=math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
        distance=6371000*2*math.atan2(math.sqrt(a),math.sqrt(1-a))
        return (distance<=request['radius_m']+1 and request['min_area']<=prop['area_sqm']<=request['max_area']
                and 0<prop['price_month']<=request['max_rent'])
    def identity(candidate):
        p=candidate['property_data'];return str(p['source'])+':'+str(p['external_id'])
    profile=first['ranking_profile'];criteria={v['metric']:v['weight'] for v in profile['criteria']} if profile else {}
    pending=[c for r in reports for c in r['floor_verification_candidates']]
    checks=dict(
        profile_ready=bool(profile and profile['status']=='ready'),
        exact_concept_preserved=bool(profile and profile['concept']==request['concept']),
        all_numeric_matches_have_disposition=all(r['numerical_matching_count']==len(all_candidates(r)) for r in reports),
        requested_numeric_limits_preserved=all(within(c['property_data']) for r in reports for c in all_candidates(r)),
        explicit_floor_parameter_preserved=(first['floor_profile']['allowed_floors']==request.get('allowed_floors')),
        every_pending_lot_labeled=all(c['confirmation']['status']=='requires_confirmation' for c in pending),
        no_repeats=(not second or not ({identity(c) for c in all_candidates(first)} & {identity(c) for c in all_candidates(second)})),
        continuation_constraints_preserved=bool(second and all(
            first[k]==second[k] for k in ('center_lat','center_lng','search_radius_m','client_min_area_sqm',
                                          'client_max_area_sqm','client_max_price_month'))),
        independent_confirmation_not_skipped=all(c['confirmation'] for r in reports for c in r['ranked_candidates']),
        no_pending_candidate_called_best=all(r['best_candidate'] is None for r in reports if not r['ranked_candidates']),
        original_database_unchanged=result['checks']['original_database_unchanged'],
        rpm_within_limit=result['peak_rpm']<=40,
    )
    outcome=dict(numeric_matches=sum(r['numerical_matching_count'] for r in reports),
        assessed_pending=len(pending),scored=sum(r['evaluated_count'] for r in reports),
        ranked=sum(len(r['ranked_candidates']) for r in reports),environment_reports=len(result['environment_records']),
        floor_analysis_statuses=[c['assessment']['analysis_status'] for c in pending],
        continuation_collection_status=second['inventory_collection'].get('stop_reason') if second else None,
        model_criteria=criteria,
        transit_flow_weight=profile['metadata'].get('market_context',{}).get('audience',{}).get('transit_flow',{}).get('weight') if profile else None,
        active_hours=profile['metadata'].get('market_context',{}).get('active_hours') if profile else None,
        seconds=result['seconds'],http_attempts=result['http_attempts'],peak_rpm=result['peak_rpm'])
    record=dict(checks=checks,outcome=outcome)
    (out/'independent_checks.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(record,ensure_ascii=False,indent=2))
    assert all(checks.values()),'A client scenario invariant failed'


if __name__=='__main__':main()
