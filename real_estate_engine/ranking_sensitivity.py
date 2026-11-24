"""Explain ranking changes caused by unfixed scenario assumptions.

One parameter is varied at a time. This is a diagnostic stress test, not a
confidence interval or a fit to business outcomes. Client choices stay fixed.
"""
import copy
import math
import time

from .market_context import evaluate_market_context,validate_market_context
from .ranking_policy import bind_reference_scales

LABELS = {
    'competition_scale': 'влияние аудитории на конкурентное давление',
    'outside_pressure': 'влияние внешних альтернатив и отказа от покупки',
    'references.residents': 'опорный уровень числа жителей',
    'references.workers': 'опорный уровень числа работников',
    'references.site_audience': 'опорный уровень внутренней аудитории',
    'references.synergy': 'опорный уровень синергии окружения',
    'references.company_revenue': 'опорный уровень выручки юрлиц конкурентов',
    'access.reference_minutes': 'опорное время доступности',
}


def candidate_id(candidate):
    prop=candidate.property_data
    return prop['id'] if prop.get('id') is not None else str(prop.get('source',''))+':'+str(prop.get('external_id',''))


def _parameters(config,provenance):
    fixed=set(provenance.get('parameter_paths',[]))
    # Previously saved profiles preserve the exact source marker assigned by
    # apply_market_references, even when per-field provenance was not serialized.
    marker=(provenance.get('kind','')+': '+provenance.get('source',''))
    supplied=provenance.get('kind') in ('client_reference','validated_benchmark')
    items=[(name,config[name]) for name in ('competition_scale','outside_pressure')]
    items.extend(('references.'+name,item) for name,item in sorted(config['references'].items()))
    if config['access']['kind']!='none':
        items.append(('access.reference_minutes',dict(value=config['access']['reference_minutes'],reason='')))
    for path,item in items:
        if supplied and item.get('reason')==marker:fixed.add(path)
    return [(p,v['value']) for p,v in items if p not in fixed],sorted(fixed)


def _change(config,path,value):
    changed=copy.deepcopy(config)
    if path.startswith('references.'):
        changed['references'][path.split('.')[1]]['value']=value
    elif path=='access.reference_minutes':changed['access']['reference_minutes']=value
    else:changed[path]['value']=value
    return changed


def model_assumption_sensitivity(profile,candidates,*,factors=(.5,2.0)):
    started=time.perf_counter()
    if (not isinstance(factors,(tuple,list)) or not factors or
            any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in factors)):
        raise ValueError('Sensitivity factors must be finite positive scenario multipliers')
    result=dict(status='not_applicable',factors=list(factors),tested_scenarios=0,expected_scenarios=0,
        parameters=[],fixed_parameters=[],effects=[],challenger_ids=[],score_envelopes=[],errors=[],
        scope='one_unfixed_parameter_at_a_time',interpretation='Scenario sensitivity, not market confidence bounds',
        weights_changed=False)
    if profile is None or len(candidates)<2:return result
    try:
        profile=bind_reference_scales(profile)
        criterion=next((c for c in profile.criteria if c.metric=='market_fit_index'),None)
        config=profile.metadata.get('market_context')
        if criterion is None or config is None:return result
        config=validate_market_context(config,profile.concept)
        parameters,fixed=_parameters(config,profile.metadata.get('market_reference_provenance',{}))
        result['parameters']=[p for p,_ in parameters];result['fixed_parameters']=fixed
        result['expected_scenarios']=len(parameters)*len(factors)
        identifiers=[candidate_id(c) for c in candidates]
        if len(set(identifiers))!=len(identifiers):raise ValueError('Ambiguous candidate identities')
        baseline=[c.unrounded_total_score for c in candidates]
        upper=[c.score_upper_bound for c in candidates]
        if any(type(v) not in (int,float) or not math.isfinite(v) for v in baseline+upper):
            raise ValueError('Missing baseline bounds')
        highest=max(baseline)
        leaders={i for i,v in enumerate(baseline) if math.isclose(v,highest,abs_tol=1e-8,rel_tol=1e-10)}
        result['baseline_leader_ids']=[identifiers[i] for i in sorted(leaders)]
        lows=list(baseline);highs=list(upper);challengers=set()
        for path,base in parameters:
            for factor in factors:
                try:
                    scenario_value=base*factor
                    if not math.isfinite(scenario_value) or scenario_value<=0:
                        raise ValueError('Nonfinite sensitivity scenario')
                    # Validate the supplied profile once. The wire-contract cap
                    # bounds supplied scales, not diagnostic hypotheses; a
                    # valid 1e15 reference can be stress-tested at 2e15.
                    config_variant=_change(config,path,scenario_value)
                    values=[];uppers=[]
                    for candidate,low,high in zip(candidates,baseline,upper):
                        component=candidate.scoring_components['market_fit_index']
                        interval,_=evaluate_market_context(config_variant,candidate.property_data,candidate.competition_report)
                        values.append(max(0,min(100,low-component['contribution_lower']+criterion.weight*interval.low)))
                        uppers.append(max(0,min(100,high-component['contribution_upper']+criterion.weight*interval.high)))
                    result['tested_scenarios']+=1
                    lows=[min(a,b) for a,b in zip(lows,values)];highs=[max(a,b) for a,b in zip(highs,uppers)]
                    threshold=max(values[i] for i in leaders)
                    rivals={i for i,v in enumerate(values) if i not in leaders and v>=threshold-1e-8}
                    if rivals:
                        challengers.update(rivals)
                        top=max(values)
                        result['effects'].append(dict(parameter=path,label=LABELS[path],factor=factor,
                            baseline_value=base,scenario_value=base*factor,
                            leader_ids=[identifiers[i] for i,v in enumerate(values) if math.isclose(v,top,abs_tol=1e-8,rel_tol=1e-10)],
                            challenger_ids=[identifiers[i] for i in sorted(rivals)],
                            compared_scores=[dict(property_id=identifiers[i],baseline_lower=baseline[i],scenario_lower=values[i])
                                for i in sorted(leaders|rivals)]))
                except Exception as exc:
                    result['errors'].append(dict(parameter=path,factor=factor,error_type=type(exc).__name__))
        result['challenger_ids']=[identifiers[i] for i in sorted(challengers)]
        result['score_envelopes']=[dict(property_id=i,lower=lo,upper=hi) for i,lo,hi in zip(identifiers,lows,highs)]
        result['status']=('incomplete' if result['errors'] else 'model_sensitive' if challengers else
            'baseline_tie' if len(leaders)>1 else 'no_unfixed_assumptions' if not parameters else 'no_rank_change_in_tested_scenarios')
    except Exception as exc:
        result['status']='incomplete';result['errors'].append(dict(error_type=type(exc).__name__))
    result['elapsed_ms']=round((time.perf_counter()-started)*1000,2)
    return result
