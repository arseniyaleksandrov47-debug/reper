"""Check the new appointment client's actual result, including missing stages."""
import json
import math
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def load(path):return json.loads(path.read_text(encoding='utf-8'))


def main():
    out=Path((ROOT/'audit/client_booking/LATEST.txt').read_text(encoding='utf-8').strip())
    first,second,result=(load(out/name) for name in ('first_search.json','next_search.json','result.json'))
    request=load(out/'client_request.json')
    profile=first['ranking_profile']
    criteria={c['metric']:c['weight'] for c in profile['criteria']}
    def rows(report):
        return [c['property_data'] for key in ('ranked_candidates','floor_verification_candidates','floor_rejected_candidates')
                for c in report[key]]
    def within(prop):
        lat1,lon1,lat2,lon2=map(math.radians,(first['center_lat'],first['center_lng'],prop['lat'],prop['lng']))
        a=math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
        distance=6371000*2*math.atan2(math.sqrt(a),math.sqrt(1-a))
        return (distance<=request['radius_m']+1 and request['min_area']<=prop['area_sqm']<=request['max_area']
                and 0<prop['price_month']<=request['max_rent'])
    identity=lambda prop:str(prop['source'])+':'+str(prop['external_id'])
    reviews=[c for report in (first,second) for c in report['floor_verification_candidates']]
    checks=dict(
        successful_profile=profile['status']=='ready',
        exact_new_concept=profile['concept']==request['concept'],
        cost_is_highest_priority=criteria['premises_cost_month']>max(v for k,v in criteria.items() if k!='premises_cost_month'),
        waived_passing_flow_not_rewarded=profile['metadata']['market_context']['audience']['transit_flow']['weight']==0,
        no_forced_ground_floor=first['floor_profile']['allowed_floors'] is None,
        request_constraints_preserved=all(within(p) for report in (first,second) for p in rows(report)),
        every_numeric_match_accounted=all(r['numerical_matching_count']==len(rows(r)) for r in (first,second)),
        all_pending_explicitly_labeled=bool(reviews) and all(c['confirmation']['status']=='requires_confirmation' for c in reviews),
        no_false_best_candidate=first['best_candidate'] is None and second['best_candidate'] is None,
        new_collection_actually_done=second['inventory_collection']['limit_per_source']>first['inventory_collection']['limit_per_source'],
        no_repeated_options=not ({identity(p) for p in rows(first)} & {identity(p) for p in rows(second)}),
        original_database_unchanged=result['checks']['original_database_unchanged'],
        rpm_within_limit=result['peak_rpm']<=40,
    )
    outcome=dict(
        numeric_matches=sum(r['numerical_matching_count'] for r in (first,second)),
        scored=sum(r['evaluated_count'] for r in (first,second)),
        environment_reports=len(result['environment_records']),
        pending=len(reviews),all_pending_missing_floor=all(c['assessment']['analysis_status']=='insufficient_facts'
            and c['assessment']['reason']=='Этаж не указан — требуется уточнение' for c in reviews),
        selection_completed=False,
        finding='Unknown floor stops admission before contextual analysis even when client imposes no floor restriction',
    )
    record=dict(checks=checks,outcome=outcome,criteria=criteria,seconds=result['seconds'],
        http_attempts=result['http_attempts'],peak_rpm=result['peak_rpm'])
    (out/'independent_checks.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(record,ensure_ascii=False,indent=2))
    assert all(checks.values()),'A client journey invariant failed'


if __name__=='__main__':main()
