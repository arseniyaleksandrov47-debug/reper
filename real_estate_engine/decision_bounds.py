"""Screen deferred rivals using known property facts and full unknown ranges.

No premise is admitted by this calculation. A deferred rival may be irrelevant
to first choice even if every one of its unknown requirements later passes.
"""
from .business_model import Interval,rent_is_lower_bound
from .ranking_policy import metric_value,_utility_interval
from .ranking_sensitivity import candidate_id

PROPERTY_METRICS={'rent_month','rent_sqm_year','area_sqm'}


def property_bounds(profile,prop,stress=.2):
    parts={}
    for criterion in profile.criteria:
        scale=profile.metadata.get('reference_scales',{}).get(criterion.metric,{})
        value=metric_value(criterion.metric,prop,None) if criterion.metric in PROPERTY_METRICS else None
        if value is None or not scale.get('usable',True):low,high=0,100
        else:
            lower=value*(1-stress);upper=value*(1+stress)
            if criterion.metric in ('rent_month','rent_sqm_year') and rent_is_lower_bound(prop):upper=None
            low,high=_utility_interval(Interval(lower,upper),criterion)
        parts[criterion.metric]=(criterion.weight*low,criterion.weight*high)
    return parts


def deferred_candidate_bounds(report,pool):
    profile=report.ranking_profile
    if not profile or profile.status!='ready' or not pool or not report.floor_verification_candidates:return []
    leader=pool[0]
    if not leader.floor_assessment or leader.floor_assessment.status!='eligible':return []
    lower=property_bounds(profile,leader.property_data)
    results=[]
    for review in report.floor_verification_candidates:
        upper=property_bounds(profile,review.property_data)
        delta=[lower[key][0]-upper[key][1] for key in lower]
        raw_margin=sum(delta)-.1*sum(abs(value) for value in delta)
        denominator=(1.1 if raw_margin>0 else .9)*sum(c.weight for c in profile.criteria)
        margin=raw_margin/denominator
        results.append(dict(property_id=candidate_id(review),conditional_upper_score=sum(v[1] for v in upper.values()),
            leader_known_lower_score=sum(v[0] for v in lower.values()),
            dominated=margin>1e-8,dominance_margin_under_weight_stress=margin,
            dominated_by_id=candidate_id(leader) if margin>1e-8 else None,
            conditional_on_admission=True,input_stress_fraction=.2,weight_stress_fraction=.1,
            method='known_property_bounds_with_full_0_100_unknown_utilities',
            assumption='Current listing facts with stated stress; no assertion of admission or future terms'))
    return results
