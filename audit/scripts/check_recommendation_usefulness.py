"""Measure delivered customer outcomes, separating archives and fresh trials.

Counts saved artifacts and runs, not independent customers or market outcomes.
No missing facts are filled in and no readiness thresholds are loosened.
"""
import argparse
import collections
import json
import statistics
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
REPORT_NAMES={'first_search.json','continued_search.json','next_search.json','search_report.json'}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def gap_group(item):
    source=item.get('source','')
    if source.startswith('organization:'):
        return 'organization.'+source.rsplit(':',1)[-1]
    if source:return source
    reason=item.get('reason','unspecified')
    if reason=='missing_cost_input':return 'cost.'+item.get('field','unspecified')
    if reason=='missing_scale':return 'scale.'+item.get('metric','unspecified')
    return reason


def inspect_report(path,report):
    candidates=report['ranked_candidates'];rows=[]
    for c in candidates:
        confirmation=c.get('confirmation',{});assessment=c.get('floor_assessment') or {}
        components=c.get('scoring_components',{})
        widths={k:part['contribution_upper']-part['contribution_lower'] for k,part in components.items()
                if isinstance(part.get('contribution_upper'),(int,float)) and isinstance(part.get('contribution_lower'),(int,float))}
        rows.append(dict(id=c['property_data'].get('id'),source=c['property_data'].get('source'),
            external_id=c['property_data'].get('external_id'),rent=c['property_data'].get('price_month'),
            floor=c['property_data'].get('floor'),score_lower=c.get('unrounded_total_score'),score_upper=c.get('score_upper_bound'),
            score_coverage=c.get('score_coverage'),uncertainty_contributions=widths,
            confirmation_status=confirmation.get('status','absent'),admission_status=assessment.get('status','absent'),
            confirmation_reason_codes=sorted({r.get('code','unspecified') for r in confirmation.get('reasons',[])}),
            mandatory_unknown=[r.get('client_quote') for r in confirmation.get('checks',{}).get('client_requirements',{}).get('items',[]) if r.get('result')=='unknown'],
            known_cards=(c.get('competition_report') or {}).get('total_cards_found'),
            inventory_status=(c.get('competition_report') or {}).get('collection_status'),
            missing_scales=[k for k,p in components.items() if p.get('scale_usable') is False],
            unknown_cost_despite_known_rent=(isinstance(c['property_data'].get('price_month'),(int,float))
                and c['property_data']['price_month']>0 and
                components.get('premises_cost_month',{}).get('scale_usable') is False),
            profile_weights={k:p.get('weight') for k,p in components.items()}))
    gaps=sorted({gap_group(i) for i in report.get('missing_inputs',[])})
    # Reported leader stability is kept separate from source/admission status.
    strict=bool(rows and report.get('selection_status')=='preliminary_leader' and report.get('comparison_complete') is True
                and rows[0]['confirmation_status']=='source_conditions_checked')
    return dict(file=str(path.relative_to(ROOT)),run=str(path.parent.relative_to(ROOT)),artifact=path.stem,
        city=report.get('city'),business=report.get('target_business'),concept=(report.get('ranking_profile') or {}).get('concept'),
        center=[report.get('center_lat'),report.get('center_lng')],radius=report.get('search_radius_m'),
        constraints={k:report.get(k) for k in ('client_min_area_sqm','client_max_area_sqm','client_max_price_month')},
        profile_status=(report.get('ranking_profile') or {}).get('status'),selection_status=report.get('selection_status'),
        profile_analysis_status=(report.get('ranking_profile') or {}).get('analysis_status'),
        profile_contract_error=(report.get('ranking_profile') or {}).get('metadata',{}).get('contract_error'),
        requirement_coverage=report.get('requirement_coverage',[]),
        comparison_complete=report.get('comparison_complete'),confirmation_status=report.get('confirmation_status'),
        data_complete=report.get('data_complete'),recommendation=report.get('recommendation',{}),
        numerical_matches=report.get('numerical_matching_count'),evaluated_count=report.get('evaluated_count'),
        ranked_count=len(rows),pending_count=len(report.get('floor_verification_candidates',[])),
        rejected_count=len(report.get('floor_rejected_candidates',[]))+len(report.get('confirmation_rejected_candidates',[])),
        seconds=(report.get('total_search_time_ms') or 0)/1000,stage_timings_ms=report.get('stage_timings_ms'),
        strict_recommendation=strict,missing_groups=gaps,missing_item_count=len(report.get('missing_inputs',[])),
        analysis_errors=report.get('analysis_errors',[]),candidates=rows,
        input_sensitive_ids=report.get('input_sensitive_challenger_ids',[]),
        weight_sensitive_ids=report.get('weight_sensitive_challenger_ids',[]),
        model_sensitive_ids=report.get('model_sensitive_challenger_ids',[]))


def summarize(rows):
    candidates=[c for r in rows for c in r['candidates']]
    ranked=[r for r in rows if r['ranked_count']]
    request_keys={json.dumps([r['city'],r['concept'],r['center'],r['radius'],r['constraints']],sort_keys=True,ensure_ascii=False) for r in rows}
    gaps=collections.Counter(g for r in rows for g in r['missing_groups'])
    widths=[c['score_upper']-c['score_lower'] for c in candidates
            if isinstance(c['score_lower'],(int,float)) and isinstance(c['score_upper'],(int,float))]
    return dict(saved_reports=len(rows),saved_runs=len({r['run'] for r in rows}),distinct_request_fingerprints=len(request_keys),
        statuses=dict(collections.Counter(r['selection_status'] for r in rows)),
        reports_with_ranking=len(ranked),complete_ranked_reports=sum(r['comparison_complete'] is True for r in ranked),
        preliminary_leader_reports=sum(r['selection_status']=='preliminary_leader' and bool(r['ranked_count']) for r in rows),
        strict_recommendations=sum(r['strict_recommendation'] for r in rows),
        recommendation_statuses=dict(collections.Counter(r['recommendation'].get('status','not_recorded') for r in rows)),
        reports_with_check_priority=sum(r['recommendation'].get('primary_candidate_id') is not None for r in rows),
        candidate_confirmations=dict(collections.Counter(c['confirmation_status'] for c in candidates)),
        reports_with_supported_admission=sum(any(c['admission_status']=='eligible' for c in r['candidates']) for r in rows),
        reports_with_rejections=sum(r['rejected_count']>0 for r in rows),
        missing_report_frequency=dict(gaps.most_common()),
        median_ranked_seconds=statistics.median(r['seconds'] for r in ranked) if ranked else None,
        known_rent_but_unscaled_cost_candidates=sum(c['unknown_cost_despite_known_rent'] for c in candidates),
        known_rent_but_unscaled_cost_reports=sum(any(c['unknown_cost_despite_known_rent'] for c in r['candidates']) for r in rows),
        median_candidate_interval_width=statistics.median(widths) if widths else None)


def collect(base):
    result=[]
    for path in sorted(base.rglob('*.json')):
        if path.name not in REPORT_NAMES:continue
        raw=read(path)
        if not isinstance(raw,dict) or 'selection_status' not in raw or 'ranked_candidates' not in raw:continue
        result.append(inspect_report(path,raw))
    return result


def fresh_journeys(base):
    journeys=[]
    for path in sorted(base.glob('*/run_*/result.json')):
        reports=[inspect_report(p,read(p)) for p in (path.parent/'first_search.json',path.parent/'continued_search.json') if p.exists()]
        result=read(path)
        preparation={}
        origin=path.parent/'inventory_origin.json'
        if origin.exists():
            prepared=Path(read(origin)['path']).parent/'preparation.json'
            if prepared.exists():preparation=read(prepared)
        journeys.append(dict(file=str(path.relative_to(ROOT)),request=result['request'],seconds=result['seconds'],
            preparation=preparation,collection_and_analysis_seconds=result['seconds']+preparation.get('seconds',0),
            checks=result['checks'],http_attempts=result['http_attempts'],peak_rpm=result['peak_rpm'],
            initial=reports[0] if reports else None,final=reports[-1] if reports else None,
            provisional_continuation=len(reports)>1,
            collection=read(path.parent/'collection.json')))
    return journeys


def rent_response(rows):
    """Change only a known rent; this is a diagnostic, not a fresh offer."""
    from check_diagnostic_consistency import construct, restore_report
    from real_estate_engine.ranking_policy import RankingProfile, RankingCriterion, evaluate_profile
    results=[];errors=[]
    for row in rows:
        if not row['ranked_count']:continue
        saved=read(ROOT/row['file']);raw=dict(saved['ranking_profile'])
        raw['criteria']=tuple(construct(RankingCriterion,c) for c in raw['criteria'])
        profile=construct(RankingProfile,raw)
        for candidate in saved['ranked_candidates']:
            prop=candidate['property_data'];rent=prop.get('price_month')
            if not isinstance(rent,(int,float)) or rent<=0:continue
            try:
                environment=restore_report(candidate['competition_report'])
                before=evaluate_profile(profile,prop,environment)
                after=evaluate_profile(profile,dict(prop,price_month=rent*.7,price_sqm_year=rent*.7/prop['area_sqm']*12),environment)
                results.append(dict(file=row['file'],id=prop.get('id'),rent_before=rent,rent_after=rent*.7,
                    lower_before=before['lower'],lower_after=after['lower'],upper_before=before['upper'],upper_after=after['upper'],
                    unchanged_score=(abs(before['lower']-after['lower'])<1e-8 and abs(before['upper']-after['upper'])<1e-8)))
            except (ValueError,TypeError,KeyError,AttributeError) as exc:
                errors.append(dict(file=row['file'],id=prop.get('id'),error_type=type(exc).__name__))
    return dict(scope='Scores only: independent price tie-breaks are not tested',
        candidates=len(results),unchanged_score_count=sum(r['unchanged_score'] for r in results),results=results,errors=errors)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'audit/recommendation_usefulness/outcomes.json')
    parser.add_argument('--live-root',type=Path,default=ROOT/'audit/recommendation_usefulness')
    args=parser.parse_args();fresh_base=args.live_root.resolve()
    archives=[r for r in collect(ROOT/'audit') if not r['file'].replace('\\','/').startswith(('audit/recommendation_usefulness/','audit/usefulness_fixes/'))]
    journeys=fresh_journeys(fresh_base);finals=[j['final'] for j in journeys if j['final']]
    initials=[j['initial'] for j in journeys if j['initial']]
    result=dict(scope=__doc__,strict_definition='Ranked preliminary leader, comparison_complete=True, source_conditions_checked',
        archive_summary=summarize(archives),archive_reports=archives,
        fresh_initial_summary=summarize(initials),
        fresh_summary=summarize(finals),fresh_journeys=journeys,
        fresh_known_rent_response=rent_response(finals),
        known_rent_response=rent_response(archives+finals))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k.endswith('_summary')},ensure_ascii=False))


if __name__=='__main__':main()
