"""Independent checks of saved point-3 results, without calling scoring code.

Checks source copies and arithmetic, not empirical market optimality or all
language judgments. Manual semantic review is recorded separately.
"""
import argparse
import json
import math
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def close(a,b):return math.isclose(a,b,abs_tol=1e-8,rel_tol=1e-10)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--run');args=parser.parse_args()
    run=Path(args.run) if args.run else Path((ROOT/'audit/acceptance_point3/LATEST.txt').read_text('utf-8').strip())
    ads={r['id']:r for r in json.loads((run/'five_real_ads.json').read_text('utf-8'))}
    checks=[];totals=[]
    def check(name,passed,**fields):checks.append(dict(check=name,passed=bool(passed),**fields))
    for case in ('street','booking','delivery'):
        report=json.loads((run/f'{case}_report.json').read_text('utf-8'))
        check('profile_and_rating_reached',report['ranking_profile']['status']=='ready' and report['ranked_candidates'],case=case)
        profile=report['ranking_profile'];criteria={v['metric']:v for v in profile['criteria']}
        decisions={v['property_data']['id']:v['assessment']['status'] for kind in ('floor_rejected_candidates','floor_verification_candidates') for v in report[kind]}
        for candidate in report['ranked_candidates']:
            prop=candidate['property_data'];identifier=prop['id'];source=ads[identifier];components=candidate['scoring_components']
            decisions[identifier]=candidate['floor_assessment']['status']
            for field in ('area_sqm','floor','price_month','description','address','url'):
                check('source_fact_preserved',prop.get(field)==source.get(field),case=case,id=identifier,field=field)
            lower=upper=0
            for metric,part in components.items():
                c=criteria[metric];w=c['weight'];value_low=part['value_lower'];value_high=part['value_upper']
                if metric=='area_sqm':
                    width=c['high']-c['low'];distance=max(c['low']-source['area_sqm'],0,source['area_sqm']-c['high'])
                    expected_low=expected_high=max(0,100*(1-distance/width))*w
                    check('area_comes_from_saved_offer',close(part['value'],source['area_sqm']),case=case,id=identifier)
                elif metric=='market_fit_index':
                    expected_low=value_low*w;expected_high=value_high*w
                elif metric=='premises_cost_month':
                    # No audit input supplies operating or fitout costs. Rent
                    # is a known lower bound, never a complete monthly cost.
                    check('full_cost_keeps_unknown_extras',part['value'] is None and value_high is None and
                          close(value_low,source['price_month']),case=case,id=identifier)
                    expected_low=0;expected_high=100*w
                else:
                    check('no_unexpected_metric',False,case=case,id=identifier,metric=metric);continue
                check('independent_weighted_contribution',close(part['contribution_lower'],expected_low) and
                      close(part['contribution_upper'],expected_high),case=case,id=identifier,metric=metric)
                lower+=expected_low;upper+=expected_high
            check('independent_total',close(lower,candidate['unrounded_total_score']) and close(upper,candidate['score_upper_bound']),case=case,id=identifier)
            check('no_unverified_listing_admitted',candidate['floor_assessment']['status']=='eligible',case=case,id=identifier)
            environment=candidate['competition_report'];metadata=environment['ai_response_metadata']
            check('current_offer_contract',metadata['classification_rule']=='purchase_offer_evidence_v3',case=case,id=identifier)
            groups=('direct_competitors','indirect_competitors','synergy_businesses','unrelated_businesses')
            known=sum(len(environment[k]) for k in groups)
            check('unknown_inventory_is_not_lost',known+metadata['unknown_count']==environment['total_cards_found'],case=case,id=identifier)
            for item in environment['direct_competitors']+environment['indirect_competitors']+environment['synergy_businesses']:
                card=item['card'];decision=metadata['organization_decisions'][card['org_id']]
                allowed={('categories',v) for v in card['categories']}|{('features',v) for v in card['features'] if not v.lower().rstrip().endswith(': false')}
                proof=decision['offer_evidence']
                check('positive_offer_cites_this_card',proof and all((v['field'],v['quote']) in allowed for v in proof),case=case,id=identifier,org_id=card['org_id'])
                independent=decision.get('offer_proof',{})
                check('positive_purchase_has_independent_proof',independent.get('verdict')=='supported' and
                      independent.get('evidence') and all((v['field'],v['quote']) in allowed for v in independent['evidence']),
                      case=case,id=identifier,org_id=card['org_id'])
            totals.append(dict(case=case,id=identifier,lower=lower,upper=upper,known_organizations=known,
                               unknown_organizations=metadata['unknown_count'],all_organizations=environment['total_cards_found']))
        check('all_five_have_a_decision',set(decisions)==set(ads),case=case)
        check('conflicting_floor_waits',decisions.get(205)=='needs_verification',case=case)
        if case=='street':check('explicit_floor_requirement_rejects_fifth',decisions.get(905)=='rejected',case=case)
        check('no_definitive_winner_with_missing_data',report['selection_status']=='needs_data' and not report['comparison_complete'],case=case)
    result=dict(checks=len(checks),passed=all(c['passed'] for c in checks),failures=[c for c in checks if not c['passed']],
                totals=totals,semantic_review='Separate manual source review; these checks cannot certify the meaning of every AI classification.')
    (run/'independent_checks.json').write_text(json.dumps(dict(summary=result,checks=checks),ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
