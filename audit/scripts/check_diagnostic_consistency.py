"""Replay saved diagnostics and test ranking invariants, without network or AI."""
import argparse
import copy
import hashlib
import json
import math
import random
import statistics
import sys
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from real_estate_engine.competitors import MapBusinessCard,ClassifiedCompetitor,RadiusCompetitionReport
from real_estate_engine.population import ResidentialPopulationReport,ResidentialBuildingInfo
from real_estate_engine.traffic import FootTrafficReport,HourlyTrafficProfile,MetroStationFlow
from real_estate_engine.fns import FnsCompanyProfile
from real_estate_engine.ranking_policy import RankingProfile,RankingCriterion,evaluate_profile,METRICS,explain_components
from real_estate_engine.market_context import evaluate_market_context
from real_estate_engine.route_audience import condition_transport_on_routes
from real_estate_engine.business_model import business_evidence
from real_estate_engine.ranking_sensitivity import model_assumption_sensitivity
from test_market_context import environment,integrated_profile
from test_route_barriers import transport_traffic,route_evidence


def construct(cls,data):
    return cls(**{k:v for k,v in data.items() if k in {f.name for f in fields(cls)}})


def restore_report(data):
    data=copy.deepcopy(data)
    def card(raw):
        raw=dict(raw)
        if raw.get('fns_profile'):raw['fns_profile']=construct(FnsCompanyProfile,raw['fns_profile'])
        return construct(MapBusinessCard,raw)
    for group in ('direct_competitors','indirect_competitors','synergy_businesses','unrelated_businesses'):
        data[group]=[construct(ClassifiedCompetitor,dict(row,card=card(row['card']))) for row in data[group]]
    data['unclassified_businesses']=[card(row) for row in data.get('unclassified_businesses',[])]
    if data.get('population'):
        pop=data['population'];pop['top_residential_buildings']=[construct(ResidentialBuildingInfo,r) for r in pop.get('top_residential_buildings',[])]
        data['population']=construct(ResidentialPopulationReport,pop)
    if data.get('foot_traffic'):
        traffic=data['foot_traffic'];traffic['hourly_profile']=construct(HourlyTrafficProfile,traffic.get('hourly_profile') or {})
        traffic['nearest_metros']=[construct(MetroStationFlow,r) for r in traffic.get('nearest_metros',[])]
        data['foot_traffic']=construct(FootTrafficReport,traffic)
    return construct(RadiusCompetitionReport,data)


def variants(config):
    for name in ('competition_scale','outside_pressure'):
        for factor in (.5,2):
            changed=copy.deepcopy(config);changed[name]['value']*=factor
            yield name,factor,changed
    for name in config['references']:
        for factor in (.5,2):
            changed=copy.deepcopy(config);changed['references'][name]['value']*=factor
            yield 'references.'+name,factor,changed


def route_case():
    traffic=transport_traffic();traffic.transport_sources[0].update(station_passengers_lower=1000,station_passengers_upper=1000)
    other=copy.deepcopy(traffic.transport_sources[0]);other.update(source_id='metro:Другая',name='Другая',station_passengers_lower=100000,station_passengers_upper=100000)
    traffic.transport_sources.append(other);values=[]
    for minutes in (1.9,2.1):
        e=route_evidence(minutes);e['pedestrian_network']['nodes'].append('other')
        e['pedestrian_network']['edges'].append(dict(**{'from':'other','to':'entry'},minutes=2,walk_allowed=True,bidirectional=True))
        e['accessibility_diagnostics']=dict(arrivals=[dict(node='other',verified=True,source='Synthetic mapped exit',kind='metro_exit',name='Другая')])
        r=condition_transport_on_routes(traffic,e)
        values.append(dict(route_minutes=minutes,lower=r.transport_lower_daily,upper=r.transport_upper_daily))
    return dict(before=values[0],after=values[1],passed=values[1]['lower']<=values[0]['lower'] and values[1]['upper']<=values[0]['upper'])


def controlled_checks():
    profile=integrated_profile();prop=dict(price_month=100000,area_sqm=50)
    failures=[];count=0
    def check(name,a,b):
        nonlocal count
        count+=1
        if b['lower']+1e-9<a['lower'] or b['upper']+1e-9<a['upper']:failures.append(name)
    for residents in (100,1000,10000):
        for transit in (100,1000,10000):
            for rent in (40000,100000,180000):
                r=environment(transit);r.population.estimated_residents_count=residents
                p=dict(prop,price_month=rent);base=evaluate_profile(profile,p,r)
                check('lower rent',base,evaluate_profile(profile,dict(p,price_month=rent*.8),r))
                more=copy.deepcopy(r);more.population.estimated_residents_count*=1.2
                check('more residents',base,evaluate_profile(profile,p,more))
                more=environment(transit*1.2);more.population.estimated_residents_count=residents
                check('more transit',base,evaluate_profile(profile,p,more))
    return dict(comparisons=count,failures=failures,route=route_case())


def coefficient_checks(samples=500):
    rng=random.Random(721);profile=integrated_profile();config=profile.metadata['market_context']
    scenarios=flips=runtime_scenarios=0;unguarded=[];runtime_misses=[];runtime_errors=[];runtime_timings=[]
    for sample in range(samples):
        pairs=[];scores=[]
        for _ in range(2):
            report=environment(10**rng.uniform(2,5))
            report.population.estimated_residents_count=10**rng.uniform(2,4.5)
            report.population.estimated_office_workers_count=10**rng.uniform(1,4)
            prop=dict(price_month=rng.uniform(30000,180000),area_sqm=50)
            pairs.append((prop,report));scores.append(evaluate_profile(profile,prop,report))
        winner=max(range(2),key=lambda k:scores[k]['lower']);other=1-winner
        delta=[part['contribution_lower']-scores[other]['components'][key]['contribution_lower'] for key,part in scores[winner]['components'].items()]
        guarded=(scores[winner]['input_lower']<=scores[other]['input_upper'] or sum(delta)-.1*sum(abs(v) for v in delta)<=0)
        probe=model_assumption_sensitivity(profile,[SimpleNamespace(property_data=dict(pair[0],id=i),competition_report=pair[1],
            unrounded_total_score=s['lower'],score_upper_bound=s['upper'],scoring_components=s['components'])
            for i,(pair,s) in enumerate(zip(pairs,scores))])
        runtime_scenarios+=probe['tested_scenarios'];runtime_timings.append(probe['elapsed_ms'])
        if probe['errors']:runtime_errors.append(dict(sample=sample,errors=probe['errors']))
        detected={(e['parameter'],e['factor']) for e in probe['effects'] if other in e['challenger_ids']}
        for parameter,factor,changed in variants(config):
            values=[s['lower']-s['components']['market_fit_index']['contribution_lower']+
                .8*evaluate_market_context(changed,*pair)[0].low for pair,s in zip(pairs,scores)]
            scenarios+=1
            if values[other]>values[winner]+1e-8:
                flips+=1
                if not guarded:unguarded.append(dict(sample=sample,parameter=parameter,factor=factor))
                if (parameter,factor) not in detected:runtime_misses.append(dict(sample=sample,parameter=parameter,factor=factor))
    return dict(samples=samples,scenarios=scenarios,flips=flips,unguarded_flips=unguarded,
        runtime_scenarios=runtime_scenarios,runtime_misses=runtime_misses,runtime_errors=runtime_errors,
        runtime_median_ms=statistics.median(runtime_timings),runtime_max_ms=max(runtime_timings),
        scope='Controlled scenarios; checks warning guards and the customer-report probe, not business outcome accuracy')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'audit/diagnostic_ranking/baseline.json');args=parser.parse_args()
    original=hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest()
    reports=[];skipped=[];explanation_issues=[];current_explanation_issues=[];replay_changes=[];scenario_count=0
    constraint_violations=[];listings=set();accounting_issues=[];route_recomputed=0
    for pattern in ('*/run_*/first_search.json','*/run_*/continued_search.json','*/run_*/next_search.json'):
        for path in sorted((ROOT/'audit').glob(pattern)):
            saved=json.loads(path.read_text(encoding='utf-8'));raw=saved.get('ranking_profile') or {};candidates=saved.get('ranked_candidates') or []
            if raw.get('status')!='ready' or not candidates:continue
            try:
                raw=dict(raw);raw['criteria']=tuple(construct(RankingCriterion,c) for c in raw['criteria'])
                profile=construct(RankingProfile,raw);pairs=[];scores=[]
                for candidate in candidates:
                    pair=(candidate['property_data'],restore_report(candidate['competition_report']));pairs.append(pair)
                    if pair[1].foot_traffic is not None and pair[1].foot_traffic.transport_routing_required:
                        pair[1].foot_traffic=condition_transport_on_routes(pair[1].foot_traffic,business_evidence(pair[0]));route_recomputed+=1
                    scores.append(evaluate_profile(profile,*pair))
            except (ValueError,TypeError,KeyError,AttributeError) as exc:
                skipped.append(dict(file=str(path.relative_to(ROOT)),error_type=type(exc).__name__));continue
            ids=[str(p[0].get('id',p[0].get('external_id'))) for p in pairs]
            probe=model_assumption_sensitivity(profile,[SimpleNamespace(property_data=p,competition_report=r,
                unrounded_total_score=s['lower'],score_upper_bound=s['upper'],scoring_components=s['components'])
                for (p,r),s in zip(pairs,scores)])
            best=max(s['lower'] for s in scores);baseline={ids[i] for i,s in enumerate(scores) if math.isclose(s['lower'],best,abs_tol=1e-8)}
            changes=[];config=profile.metadata.get('market_context');criterion=next((c for c in profile.criteria if c.metric=='market_fit_index'),None)
            if config and criterion and len(pairs)>1:
                for parameter,factor,changed in variants(config):
                    altered=[s['lower']-s['components']['market_fit_index']['contribution_lower']+
                        criterion.weight*evaluate_market_context(changed,*pair)[0].low for pair,s in zip(pairs,scores)]
                    winner={ids[i] for i,v in enumerate(altered) if math.isclose(v,max(altered),abs_tol=1e-8)};scenario_count+=1
                    if not baseline & winner:changes.append(dict(parameter=parameter,factor=factor,leader_ids=sorted(winner)))
            for candidate,s in zip(candidates,scores):
                prop=candidate['property_data'];listings.add((prop.get('source'),str(prop.get('external_id') or prop.get('url'))))
                for field,limit,is_min in (('area_sqm',saved.get('client_min_area_sqm'),True),
                        ('area_sqm',saved.get('client_max_area_sqm'),False),('price_month',saved.get('client_max_price_month'),False)):
                    if limit is not None and (prop[field]<limit-1e-7 if is_min else prop[field]>limit+1e-7):
                        constraint_violations.append(dict(file=str(path.relative_to(ROOT)),id=prop['id'],field=field,actual=prop[field],limit=limit))
                allowed=(saved.get('floor_profile') or {}).get('allowed_floors')
                if allowed and type(prop.get('floor')) is int and prop['floor'] not in allowed:
                    constraint_violations.append(dict(file=str(path.relative_to(ROOT)),id=prop['id'],field='floor'))
                if candidate.get('confirmation',{}).get('status')=='not_suitable':
                    constraint_violations.append(dict(file=str(path.relative_to(ROOT)),id=prop['id'],field='rejected_still_ranked'))
                if (not 0<=s['lower']<=s['upper']<=100 or not 0<=s['coverage']<=1 or
                        not math.isclose(s['lower'],sum(p['contribution_lower'] for p in s['components'].values()),abs_tol=1e-7)):
                    accounting_issues.append(dict(file=str(path.relative_to(ROOT)),id=prop['id']))
                current=explain_components(s['components'])
                for label,sign in (('pros',1),('cons',-1)):
                    for text in candidate.get(label,[]):
                        part=next((p for key,p in s['components'].items() if text.startswith(METRICS[key][0]+':')),None)
                        if part and part['weight']>0:
                            low=part['contribution_lower']/part['weight'];high=part['contribution_upper']/part['weight']
                            if sign==1 and low<50 or sign==-1 and high>=50:
                                explanation_issues.append(dict(file=str(path.relative_to(ROOT)),id=candidate['property_data']['id'],kind=label,text=text,utility_bounds=[low,high]))
                for label in ('pros','cons'):
                    for text in current[label]:
                        part=next(p for key,p in s['components'].items() if text.startswith(METRICS[key][0]+':'))
                        low=part['contribution_lower']/part['weight'];high=part['contribution_upper']/part['weight']
                        if label=='pros' and low<50 or label=='cons' and high>=50:
                            current_explanation_issues.append(dict(file=str(path.relative_to(ROOT)),text=text))
                if not math.isclose(candidate['unrounded_total_score'],s['lower'],abs_tol=1e-6):
                    replay_changes.append(dict(file=str(path.relative_to(ROOT)),id=candidate['property_data']['id'],published=candidate['unrounded_total_score'],current=s['lower']))
            reports.append(dict(file=str(path.relative_to(ROOT)),business=saved['target_business'],candidates=len(pairs),
                published_status=saved['selection_status'],tied_leader_ids=sorted(baseline),model_parameter_flips=changes,
                runtime_model_sensitivity=probe,
                scores=[dict(id=i,lower=s['lower'],upper=s['upper'],sum_lower=sum(p['contribution_lower'] for p in s['components'].values()),coverage=s['coverage']) for i,s in zip(ids,scores)]))
    runtime_summary=dict(tested_scenarios=sum(r['runtime_model_sensitivity']['tested_scenarios'] for r in reports),
        expected_scenarios=sum(r['runtime_model_sensitivity']['expected_scenarios'] for r in reports),
        incomplete_reports=[r['file'] for r in reports if r['runtime_model_sensitivity']['status']=='incomplete'],
        sensitive_reports=[r['file'] for r in reports if r['runtime_model_sensitivity']['effects']],
        max_elapsed_ms=max((r['runtime_model_sensitivity'].get('elapsed_ms',0) for r in reports),default=0))
    result=dict(scope=__doc__,reports=reports,skipped=skipped,coefficient_scenarios=scenario_count,
        runtime_model_checks=runtime_summary,
        published_explanation_issues=explanation_issues,current_explanation_issues=current_explanation_issues,replay_changes=replay_changes,controlled=controlled_checks(),
        unique_listings=len(listings),constraint_violations=constraint_violations,accounting_issues=accounting_issues,
        route_recomputed_candidates=route_recomputed,
        controlled_coefficient_checks=coefficient_checks(),
        original_database_unchanged=original==hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest())
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(output=str(args.output),reports=len(reports),candidates=sum(r['candidates'] for r in reports),skipped=len(skipped),
        unique_listings=len(listings),route_recomputed_candidates=route_recomputed,coefficient_scenarios=scenario_count,published_explanation_issues=len(explanation_issues),current_explanation_issues=len(current_explanation_issues),
        replay_changes=len(replay_changes),constraint_violations=len(constraint_violations),accounting_issues=len(accounting_issues),
        controlled=result['controlled'],controlled_coefficient_checks=result['controlled_coefficient_checks'],
        runtime_model_checks=runtime_summary),ensure_ascii=False))


if __name__=='__main__':main()
