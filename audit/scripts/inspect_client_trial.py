"""Independent numeric checks and read-only diagnosis of a saved client run."""
import json
import argparse
import math
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from archived_floor import load_floor_profile
from real_estate_engine.floor_policy import BusinessFloorProfileResolver
from real_estate_engine.ai_contract import normalize_floor_response
from real_estate_engine.property_sources import PublicPropertySources


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',type=Path,default=Path('audit/client_trial'))
    args=parser.parse_args()
    out = Path((ROOT / args.base / 'LATEST.txt').read_text(encoding='utf-8').strip())
    report = json.loads((out / 'first_search.json').read_text(encoding='utf-8'))
    result = json.loads((out / 'result.json').read_text(encoding='utf-8'))
    matches = json.loads((out / 'fresh_matches.json').read_text(encoding='utf-8'))
    request = result['request']
    profile = load_floor_profile(report['floor_profile'])
    resolver = BusinessFloorProfileResolver()
    conditions = resolver.required_checks(profile)
    wire_errors = []
    for path in sorted(out.glob('ai_*.json')):
        raw = json.loads(json.loads(path.read_text(encoding='utf-8'))['text'])
        if not isinstance(raw, dict) or 'assessments' not in raw:
            continue
        assessments = raw['assessments']
        try:
            normalize_floor_response(raw, {key: conditions for key in assessments})
        except Exception as exc:
            wire_errors.append(dict(file=path.name, error_type=type(exc).__name__, message=str(exc)))
    distances = []
    for prop in matches:
        lat1, lon1, lat2, lon2 = map(math.radians, (
            result['location']['lat'], result['location']['lng'], prop['lat'], prop['lng']))
        a = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
        distances.append(6371000 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))
    checks = dict(
        complete_numeric_disposition=len(matches) == len(report['floor_rejected_candidates'])
            + len(report['floor_verification_candidates']) + report['evaluated_count'],
        actual_environment_analysis=report['evaluated_count'] > 0 and bool(result['environment_records']),
        all_within_requested_radius=all(d <= request['radius_m']+1 for d in distances),
        all_within_requested_area=all(request['min_area'] <= p['area_sqm'] <= request['max_area'] for p in matches),
        all_within_requested_rent=all(0 < p['price_month'] <= request['max_rent'] for p in matches),
        ranked_floor_admission=all(c['floor_assessment']['status'] == 'eligible' for c in report['ranked_candidates']),
        interval_bounds_valid=all(0 <= c['unrounded_total_score'] <= c['score_upper_bound'] <= 100
            for c in report['ranked_candidates']),
        no_unique_leader_claim=report['selection_status'] == 'needs_data' and not report['comparison_complete'],
        unconfirmed_not_approved=all(c['confirmation']['status'] == 'requires_confirmation'
            for c in report['ranked_candidates']),
    )
    source_probe = None
    candidate = next((c for c in report['ranked_candidates'] if c['property_data']['source'] == 'cian'), None)
    if candidate:
        source = PublicPropertySources.__new__(PublicPropertySources)
        source.analyzer = SimpleNamespace(map_collector=SimpleNamespace(proxy=None))
        try:
            body, final = source._get(candidate['property_data']['url'])
            objects = []
            for script in re.findall(r'<script\b[^>]*type=[\"\']application/ld\+json[\"\'][^>]*>(.*?)</script>', body, re.S|re.I):
                try:
                    stack = [json.loads(script)]
                except ValueError:
                    continue
                while stack:
                    value = stack.pop()
                    if isinstance(value, list):
                        stack.extend(value)
                    elif isinstance(value, dict):
                        stack.extend(v for v in value.values() if isinstance(v, (dict,list)))
                        if '@type' in value or 'floorSize' in value:
                            objects.append({k:value[k] for k in ('@type','url','@id','floorSize') if k in value})
            source_probe = dict(fetched=True, final_url=final, jsonld_objects=objects,
                                exact_source_parser_status=candidate['confirmation'].get('processing_status'))
        except Exception as exc:
            source_probe = dict(fetched=False, error_type=type(exc).__name__)
    findings = dict(checks=checks, floor_wire_errors=wire_errors, source_probe=source_probe,
                    max_distance_m=round(max(distances, default=0),2))
    (out / 'independent_checks.json').write_text(json.dumps(findings, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(findings, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
