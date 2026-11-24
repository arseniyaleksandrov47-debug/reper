"""Assert the three repaired behaviors against the completed live journey."""
import json
import math
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def load(path):return json.loads(path.read_text(encoding='utf-8'))


def main():
    out=Path((ROOT/'audit/client_fixes/LATEST.txt').read_text(encoding='utf-8').strip())
    first,next_report,result,independent=(load(out/name) for name in (
        'first_search.json','next_search.json','result.json','independent_checks.json'))
    def properties(report):
        return [c['property_data'] for k in ('ranked_candidates','floor_verification_candidates','floor_rejected_candidates')
                for c in report[k]]+[c['property_data'] for c in report['confirmation_rejected_candidates']]
    def within(prop):
        lat,lon=first['center_lat'],first['center_lng']
        p1,p2=math.radians(lat),math.radians(prop['lat'])
        delta_lat,delta_lon=p2-p1,math.radians(prop['lng']-lon)
        a=math.sin(delta_lat/2)**2+math.cos(p1)*math.cos(p2)*math.sin(delta_lon/2)**2
        distance=6371000*2*math.atan2(math.sqrt(a),math.sqrt(1-a))
        return (distance<=first['search_radius_m']+1 and
            first['client_min_area_sqm']<=prop['area_sqm']<=first['client_max_area_sqm'] and
            0<prop['price_month']<=first['client_max_price_month'])
    def identity(prop):return str(prop['source'])+':'+str(prop['external_id'])
    rounds=first['inventory_collection']['rounds']
    all_reviews=[c['assessment'] for report in (first,next_report)
                 for k in ('floor_verification_candidates','floor_rejected_candidates') for c in report[k]]
    checks=dict(
        journey_mechanisms=all(result['checks'].values()),
        independent_numeric_checks=all(independent['checks'].values()),
        first_empty_sample_automatically_expanded=rounds[0]['fresh_matches']==0 and len(rounds)>1
            and rounds[-1]['fresh_matches']>0,
        fresh_cian_source_now_processed=any(c['property_data']['source']=='cian'
            and c['confirmation'].get('processing_status')=='complete'
            and c['confirmation'].get('completed_conditions')==4
            and not c['confirmation'].get('source_errors') for c in first['ranked_candidates']),
        no_floor_wire_contract_errors=not independent['floor_wire_errors'],
        no_floor_inference_failures=all(a['analysis_status'] in ('valid','insufficient_facts') for a in all_reviews),
        find_more_actually_collected=next_report['inventory_collection']['limit_per_source']
            >first['inventory_collection']['limit_per_source'],
        find_more_returned_a_new_option=bool(next_report['ranked_candidates']),
        no_repeated_published_options=not ({identity(p) for p in properties(first)} &
                                          {identity(p) for p in properties(next_report)}),
        all_phases_preserve_numeric_filters=all(within(p) for r in (first,next_report) for p in properties(r)),
        uncertain_sources_still_not_approved=all(c['confirmation']['status']=='requires_confirmation'
            for r in (first,next_report) for c in r['ranked_candidates']),
        all_source_checks_processed=all(c['confirmation'].get('processing_status')=='complete'
            and c['confirmation'].get('completed_conditions')==4
            for r in (first,next_report) for c in r['ranked_candidates']),
        rate_limit_respected=result['peak_rpm']<=40,
    )
    record=dict(checks=checks,main_properties=len(properties(first)),
        next_properties=len(properties(next_report)),http_attempts=result['http_attempts'],peak_rpm=result['peak_rpm'])
    (out/'fix_verification.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(record,ensure_ascii=False,indent=2))
    assert all(checks.values()),'A live repair check failed'


if __name__=='__main__':main()
