"""Scenario suitability index using all collected market-analysis blocks.

This is an explicitly uncalibrated utility model, never an order/profit forecast.
AI supplies preferences and utility reference levels, never property facts.
Overlapping audience proxies are blended; accounts/reviews describe alternatives.
"""
import copy
import math
import re
from collections import Counter
from .ai_contract import AIContractError
from .business_model import Interval, business_evidence
from .traffic import FootTrafficEstimator
from .concept_evidence import waiver_targets,client_clock_window
from .accessibility import resolve_access_route
from .population import population_counts_available


AUDIENCE = ('transit_flow', 'residents', 'workers', 'site_audience', 'synergy')
STRENGTH = ('presence', 'proximity', 'reputation', 'company_revenue', 'company_profit')
REFERENCES = ('residents', 'workers', 'site_audience', 'synergy', 'company_revenue')


def audience_usage_schema(source_ids):
    item=dict(type='object',properties=dict(
        usage=dict(type='string',enum=['use','waived','not_applicable']),
        source_id=dict(type='string',enum=list(source_ids)),reason=dict(type='string',minLength=1,maxLength=160),
        waiver_quote=dict(type=['string','null'])),
        required=['usage','source_id','reason','waiver_quote'],additionalProperties=False)
    properties={k:copy.deepcopy(item) for k in AUDIENCE}
    if isinstance(source_ids,dict):
        # Match the existing local guard before inference: no model may waive
        # a source without a literal waiver applying to that audience.
        allowed=set().union(*(waiver_targets(text) for text in source_ids.values()))
        for key,entry in properties.items():
            if key not in allowed:entry['properties']['usage']['enum']=['use','not_applicable']
    return dict(type='object',properties=properties,
                required=list(AUDIENCE),additionalProperties=False)


def normalize_audience_annotations(data):
    """Discard unused annotations without changing any applicability decision.

    Waiver evidence remains mandatory and fully validated for waived sources.
    A quote attached to an active source cannot remove that source's weight.
    """
    data=copy.deepcopy(data);ignored=[]
    if isinstance(data,dict):
        for key,item in data.items():
            if (isinstance(item,dict) and item.get('usage') in ('use','not_applicable')
                    and isinstance(item.get('waiver_quote'),str)):
                item['waiver_quote']=None;ignored.append('audience_usage.'+key+'.waiver_quote')
    return data,ignored


def validate_audience_usage(data, sources, scenario):
    from .proof_grounding import explicit_waiver
    if not isinstance(data,dict) or set(data)!=set(AUDIENCE):raise AIContractError('Missing independent audience applicability')
    result=copy.deepcopy(data)
    for key,item in result.items():
        if (not isinstance(item,dict) or set(item)!={'usage','source_id','reason','waiver_quote'} or
                item['usage'] not in ('use','waived','not_applicable') or
                not isinstance(item['source_id'],str) or item['source_id'] not in sources or
                not isinstance(item['reason'],str) or not item['reason'].strip()):
            raise AIContractError('Ungrounded audience applicability')
        quote=sources[item['source_id']]
        if item['usage']=='waived':
            waiver=item['waiver_quote']
            if (not isinstance(waiver,str) or not waiver.strip() or waiver not in quote or
                    key not in waiver_targets(waiver) or not any(
                        w['source_id']==item['source_id'] and waiver in w['quote'] for w in scenario['waivers'])):
                raise AIContractError('Audience waiver is not grounded for this specific source: '+key)
        elif item['waiver_quote'] is not None:
            raise AIContractError('Only a waived source may carry waiver evidence')
        if item['usage']=='not_applicable' and explicit_waiver(quote) and key not in waiver_targets(quote):
            raise AIContractError('A waiver of another audience source cannot disable this source: '+key)
        # Silence about a source is not an exclusion. An optional internal
        # audience needs affirmative context; general catchment sources don't.
        if (item['usage']=='not_applicable' and key!='site_audience' and
                not explicit_waiver(quote) and not re.search(r'\bтолько\b|\bисключительн\w*',quote,re.I)):
            item['usage']='use'
            item['reason']='Источник не исключён концепцией; отсутствие упоминания не снимает его значимость. '+item['reason']
        item['concept_quote']=quote
    return result


def compile_audience_usage(config, usage):
    config=copy.deepcopy(config);before={k:v['weight'] for k,v in config['audience'].items()}
    for key,item in usage.items():
        if item['usage']=='use' and config['audience'][key]['weight']<=0:
            raise AIContractError('Applicable audience source omitted: '+key)
        if item['usage']!='use':
            config['audience'][key]=dict(weight=0,concept_quote=item['concept_quote'],reason=item['reason'])
    total=sum(v['weight'] for v in config['audience'].values())
    if total<=0:raise AIContractError('No applicable audience sources')
    for item in config['audience'].values():item['weight']/=total
    return config,dict(audience_weights_before=before,audience_applicability_enforced=True)


# Empirical Moscow/SPb search-circle calibration baselines (50th/75th percentiles across 195 archived runs
# and B2C HoReCa/service LLC revenue scales); NOT runtime AI guesses and NOT calibrated business outcome targets.
DEFAULT_UTILITY_SCALES = dict(
    references=dict(
        residents=1500,
        workers=1500,
        site_audience=500,
        synergy=3,
        company_revenue=50_000_000,
    ),
    competition_scale=4,
    outside_pressure=1,
    access_reference_minutes=dict(none=0, walk=15, delivery=30),
)


def access_reference_minutes_for(kind):
    """Return the deterministic time-scale constant associated with an access kind."""
    defaults = DEFAULT_UTILITY_SCALES['access_reference_minutes']
    if kind not in defaults:
        raise AIContractError('Unknown market access kind')
    return defaults[kind]


def default_market_reference_provenance():
    """Return explicit provenance metadata for code-owned default market utility scales."""
    return dict(
        kind='system_default_scale',
        source='default_market_scales',
        note='Code-owned deterministic utility scales; not calibrated against business outcomes',
    )


def apply_default_market_scales(config):
    """Populate deterministic utility scales so LLM output never invents numeric reference constants."""
    if not isinstance(config, dict) or not isinstance(config.get('access'), dict):
        raise AIContractError('Missing or malformed market context')
    updated = copy.deepcopy(config)
    updated['references'] = {
        key: dict(value=value, reason=f'Базовый ориентир модели для «{key}» (не измерение рынка)')
        for key, value in DEFAULT_UTILITY_SCALES['references'].items()
    }
    updated['competition_scale'] = dict(
        value=DEFAULT_UTILITY_SCALES['competition_scale'],
        reason='Базовый масштаб конкурентного давления модели (не калиброван)',
    )
    updated['outside_pressure'] = dict(
        value=DEFAULT_UTILITY_SCALES['outside_pressure'],
        reason='Базовый коэффициент внешнего выбора модели (не калиброван)',
    )
    updated['access']['reference_minutes'] = access_reference_minutes_for(updated['access'].get('kind'))
    return updated


def market_context_schema(source_ids):
    def obj(properties):
        return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)
    string = dict(type='string', minLength=1,maxLength=160)
    preference = obj(dict(weight=dict(type='number', minimum=0, maximum=1),
        concept_quote=dict(type='string', enum=list(source_ids)), reason=string))
    required_preference=copy.deepcopy(preference)
    required_preference['properties']['weight']=dict(type='number',exclusiveMinimum=0,maximum=1)
    return obj(dict(audience=obj({k:copy.deepcopy(preference) for k in AUDIENCE}),
        alternatives=obj({k:copy.deepcopy(required_preference) for k in ('direct','indirect')}),
        strength=obj({k:copy.deepcopy(required_preference) for k in STRENGTH}),
        access=obj(dict(kind=dict(type='string', enum=['none','walk','delivery']),
            weight=dict(type='number', minimum=0, maximum=1),
            concept_quote=dict(type='string', enum=list(source_ids)), reason=string))))


def bind_market_references(config, fragments):
    config = copy.deepcopy(config)
    try:
        for group in ('audience','alternatives','strength'):
            for item in config[group].values(): item['concept_quote'] = fragments[item['concept_quote']]
        config['access']['concept_quote'] = fragments[config['access']['concept_quote']]
    except (KeyError,TypeError) as exc:
        raise AIContractError('Unknown market-context concept reference') from exc
    config['active_hours']=client_clock_window('\n'.join(fragments.values()))['active_hours']
    return config


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def validate_market_context(config, concept):
    required = {'audience','alternatives','strength','references','competition_scale','outside_pressure','active_hours','access'}
    if not isinstance(config,dict) or set(config)-{'hour_coverage','hours_source_quotes'}!=required:
        raise AIContractError('Incomplete market-context configuration')
    config = copy.deepcopy(config)
    for group, names in (('audience',AUDIENCE),('alternatives',('direct','indirect')),('strength',STRENGTH)):
        values = config[group]
        if not isinstance(values,dict) or set(values)!=set(names):
            raise AIContractError('Missing market-analysis block')
        for key,item in values.items():
            if (not isinstance(item,dict) or set(item)!={'weight','concept_quote','reason'} or
                    not _finite(item['weight']) or not 0<=item['weight']<=1 or
                    not isinstance(item['reason'],str) or not item['reason'].strip() or
                    not isinstance(item['concept_quote'],str) or not item['concept_quote'].strip() or item['concept_quote'] not in concept):
                raise AIContractError('Ungrounded market-factor importance')
            if group!='audience' and item['weight']==0:
                raise AIContractError('Competition, reputation and FNS must participate in the market score')
        total = sum(item['weight'] for item in values.values())
        if total<=0: raise AIContractError('Market group has no active inputs')
        for item in values.values(): item['weight']/=total
    if not isinstance(config['references'],dict) or set(config['references'])!=set(REFERENCES):
        raise AIContractError('Missing utility reference assumptions')
    reference_items=[('references.'+key,value) for key,value in config['references'].items()]
    reference_items.extend((key,config[key]) for key in ('competition_scale','outside_pressure'))
    invalid_references=[path for path,item in reference_items if (
        not isinstance(item,dict) or set(item)!={'value','reason'} or not _finite(item['value']) or
        not 0<item['value']<=1e15 or not isinstance(item['reason'],str) or not item['reason'].strip())]
    if invalid_references:
        raise AIContractError('Invalid positive utility scales '+', '.join(invalid_references)+
            ': each value must be finite and >0. These are saturation/reference scales, not measured audiences. '
            'Inactive inputs are removed by audience weights, not by zeroing their scales.')
    hours = config['active_hours']
    if (not isinstance(hours,list) or not hours or
            any(type(h) is not int or not 0<=h<=23 for h in hours) or len(hours)!=len(set(hours))):
        raise AIContractError('Invalid scenario hours')
    clock=client_clock_window(concept)
    if set(hours)!=set(clock['active_hours']):
        raise AIContractError('Clock hours must come from the client text; unspecified hours cannot be invented')
    config['active_hours']=clock['active_hours']
    config['hour_coverage']=clock['hour_coverage']
    config['hours_source_quotes']=clock['source_quotes']
    access = config['access']
    if (not isinstance(access,dict) or set(access)!={'kind','weight','reference_minutes','concept_quote','reason'} or
            access['kind'] not in ('none','walk','delivery') or not _finite(access['weight']) or
            not 0<=access['weight']<=1 or not _finite(access['reference_minutes']) or access['reference_minutes']<0 or
            (access['kind']!='none' and access['reference_minutes']==0) or
            not isinstance(access['concept_quote'],str) or not access['concept_quote'] or access['concept_quote'] not in concept or
            not isinstance(access['reason'],str) or not access['reason'].strip() or
            (access['kind']=='none') != (access['weight']==0)):
        raise AIContractError('Invalid scenario accessibility preference')
    return config


def _number(value, signed=False):
    return float(value) if _finite(value) and (signed or value>=0) else None


def _observed(value, stress=0, maximum=None, signed=False):
    value = _number(value,signed)
    if value is None: return None
    lo,hi = value-abs(value)*stress,value+abs(value)*stress
    if not signed: lo=max(0,lo)
    if maximum is not None: lo=min(maximum,lo);hi=min(maximum,hi)
    return Interval(lo,hi)


def _saturate(interval, reference, stress):
    if interval is None:return Interval(0,1)
    # References are utility assumptions: stress them along with observations.
    return Interval(interval.low/(interval.low+reference*(1+stress)),
                    interval.high/(interval.high+reference*(1-stress)))


def _blend(parts, weights):
    return Interval(sum(weights[k]*parts[k].low for k in weights),sum(weights[k]*parts[k].high for k in weights))


def _transport_audience(ft,config,stress):
    """Keep unknown station flow unbounded and apply the clock window once."""
    if ft is None:return None,None,0,[]
    if getattr(ft,'transport_routing_required',False) and not getattr(ft,'transport_route_diagnostics',{}).get('applied'):
        return Interval(0,1),None,0,['Транспортная близость по прямой не использована как доступная аудитория: маршрут до входа не проверен']
    warnings=[]
    status=getattr(ft,'transport_data_status',None)
    if status=='unavailable':return None,None,0,warnings
    if status not in ('complete','bounded','partial','route_complete','route_partial'):
        return None,None,0,['В отчёте нет диапазона транспортной аудитории в действующем формате']
    lower=_number(getattr(ft,'transport_lower_daily',None))
    upper=_number(getattr(ft,'transport_upper_daily',None))
    coverage=_number(getattr(ft,'transport_source_coverage',None)) or 0
    if lower is None or upper is not None and upper<lower:return None,None,0,warnings
    overlap=config.get('hour_coverage',[1 if h in config['active_hours'] else 0 for h in range(24)])
    fraction=1.0
    if any(v!=1 for v in overlap):
        shares=getattr(getattr(ft,'hourly_profile',None),'weekday_hourly_share',None)
        if (not isinstance(shares,(list,tuple)) or len(shares)!=24 or
                any(_number(v) is None for v in shares) or sum(shares)<=0):
            return None,None,0,warnings
        fraction=sum(shares[h]*overlap[h] for h in range(24))/sum(shares)
    raw=lower*fraction if upper is not None and lower==upper else None
    flow_index=Interval(FootTrafficEstimator.index_for_daily(lower*fraction*(1-stress))/100,
        FootTrafficEstimator.index_for_daily(upper*fraction*(1+stress))/100 if upper is not None else 1)
    return flow_index,raw,min(1,coverage),warnings


def evaluate_market_context(config, prop, report, *, stress=0):
    """Return bounded index, weighted evidence coverage and complete input trace."""
    if not _finite(stress) or not 0<=stress<1:raise ValueError('Invalid market stress')
    trace={};missing=[];warnings=[
        'Индекс пригодности окружения не откалиброван на результатах бизнеса и не прогнозирует заказы/прибыль',
        'Опорные уровни являются явными параметрами полезности сценария, а не измерениями спроса',
        'Выручка и прибыль ФНС относятся к юрлицу; их влияние — гипотеза о финансовой устойчивости альтернатив',
        'Конкуренция оценена среди найденных организаций; внерайонные/онлайн-альтернативы представлены отдельным допущением']
    automatic_access=business_evidence(prop).get('accessibility_diagnostics',{})
    if isinstance(automatic_access,dict):warnings.extend(automatic_access.get('warnings',[]))
    def value(name, raw, interval, role, **extra):
        known = interval is not None
        if not known:missing.append(name)
        trace[name]=dict(value=raw,known=known,role=role,
                         index_lower=interval.low if interval else 0,index_upper=interval.high if interval else 1,**extra)
        return interval or Interval(0,1)
    status_ok = getattr(report,'data_status',None)=='ok'
    # One org can occupy only one class; duplicated cards cannot multiply pressure.
    groups={k:[] for k in ('direct','indirect','synergy')};seen=set()
    for group,attr in (('direct','direct_competitors'),('indirect','indirect_competitors'),('synergy','synergy_businesses')):
        for item in getattr(report,attr,[]) or []:
            card=item.card;identity=str(card.org_id)
            if identity in seen:
                warnings.append('Повторная карточка организации исключена из расчёта: '+identity);continue
            seen.add(identity);groups[group].append(card)
    all_cards=[c for cards in groups.values() for c in cards]
    classified_ids=seen|{str(c.card.org_id) for c in getattr(report,'unrelated_businesses',[]) or []}
    unclassified_ids={str(c.org_id) for c in getattr(report,'unclassified_businesses',[]) or []}-classified_ids
    found=_number(getattr(report,'total_cards_found',None)) or 0
    unknown_count=max(len(unclassified_ids),int(max(0,found-len(classified_ids))))
    collection_status=getattr(report,'collection_status','unknown')
    inventory_complete=collection_status=='ok'
    classification_complete=inventory_complete and unknown_count==0 and status_ok
    if unknown_count:missing.append('organization_classification')
    if not inventory_complete:missing.append('organization_inventory')
    per_inn=Counter(str(c.fns_inn) for c in all_cards if getattr(c,'fns_inn',None))
    # Do not choose a favourable set of accounts when the same INN conflicts.
    accounts={};conflicted=set()
    financial_sources={str(card.org_id):card for card in all_cards}
    for card in getattr(report,'unclassified_businesses',[]) or []:financial_sources.setdefault(str(card.org_id),card)
    for item in getattr(report,'unrelated_businesses',[]) or []:financial_sources.setdefault(str(item.card.org_id),item.card)
    for card in financial_sources.values():
        inn=getattr(card,'fns_inn',None)
        if not inn:continue
        row=(card.fns_revenue_rub,card.fns_net_profit_rub,card.fns_reporting_year)
        if str(inn) in accounts and accounts[str(inn)]!=row:conflicted.add(str(inn))
        accounts[str(inn)]=row
    financial_details={}
    weights={k:v['weight'] for k,v in config['strength'].items()}
    strength_by_org={};strength_coverage=[];fns_applicable_count=0;fns_matched_count=0
    for card in all_cards:
        features={};known={};raw={};inn=getattr(card,'fns_inn',None)
        features['presence']=Interval(1,1);known['presence']=True;raw['presence']=1
        distance=_observed(card.distance_m,stress)
        radius=_number(getattr(report,'radius_m',None))
        features['proximity']=(Interval(max(0,1-distance.high/radius),max(0,1-distance.low/radius))
                               if distance and radius else Interval(0,1))
        known['proximity']=bool(distance and radius);raw['proximity']=card.distance_m
        rating=_observed(card.rating,stress,maximum=5)
        # ratings_count and reviews_count describe overlapping reactions: use max.
        reactions=max(_number(card.ratings_count) or 0,_number(card.reviews_count) or 0)
        reaction_range=_observed(reactions,stress)
        features['reputation']=(Interval(rating.low/5*reaction_range.low/(1+reaction_range.low),
                                         rating.high/5*reaction_range.high/(1+reaction_range.high)) if rating else Interval(0,1))
        known['reputation']=rating is not None;raw['reputation']=dict(rating=card.rating,reactions=reactions)
        revenue=_observed(card.fns_revenue_rub,stress) if inn and str(inn) not in conflicted else None
        profit=_observed(card.fns_net_profit_rub,stress,signed=True) if revenue and revenue.low>0 else None
        features['company_revenue']=_saturate(revenue,config['references']['company_revenue']['value'],stress)
        known['company_revenue']=revenue is not None;raw['company_revenue']=card.fns_revenue_rub
        if profit:
            ratios=[p/r for p in (profit.low,profit.high) for r in (revenue.low,revenue.high)]
            features['company_profit']=Interval(.5+math.atan(min(ratios))/math.pi,.5+math.atan(max(ratios))/math.pi)
        else:features['company_profit']=Interval(0,1)
        known['company_profit']=profit is not None;raw['company_profit']=card.fns_net_profit_rub
        # Option A: company FNS accounts are optional in coverage & missing_inputs
        # for chains and third-party orgs without a verified standalone LLC INN.
        company_applicable=(not getattr(card,'is_chain',False) and
            (bool(inn) or getattr(card,'fns_match_status',None)=='matched_company'))
        if company_applicable:fns_applicable_count+=1
        if inn and known['company_revenue']:fns_matched_count+=1
        missing.extend('organization:'+str(card.org_id)+':'+k for k in weights
            if not known[k] and (not k.startswith('company_') or bool(inn and str(inn) in conflicted)))
        # Accounts enter once per legal entity, not once per outlet of that entity.
        share=per_inn.get(str(inn),1)
        # Unclassified outlets may share this entity. Keep every possible
        # allocation instead of fixing its share before their roles are known.
        adjusted={k:Interval(v.low/(share+unknown_count) if inventory_complete else 0,v.high/share)
                  if k.startswith('company_') else v for k,v in features.items()}
        strength_by_org[str(card.org_id)]=_blend(adjusted,weights)
        raw_coverage=sum(weights[k] for k in weights if known[k])
        applicable_weight=sum(weights[k] for k in weights if not k.startswith('company_') or company_applicable)
        coverage=min(1.0,raw_coverage/applicable_weight) if applicable_weight>0 else 1.0
        strength_coverage.append(coverage)
        financial_details[str(card.org_id)]=dict(inn=inn,reporting_year=card.fns_reporting_year,
            legal_entity_allocation=share,financial_conflict=bool(inn and str(inn) in conflicted),
            fns_applicable=company_applicable,raw_strength_coverage=raw_coverage,
            inputs={k:dict(value=raw[k],known=known[k],weight=weights[k],
                           index_lower=adjusted[k].low,index_upper=adjusted[k].high) for k in weights},
            strength=strength_by_org[str(card.org_id)].as_dict(),coverage=coverage)
    ft=getattr(report,'foot_traffic',None);population=getattr(report,'population',None)
    audience={};audience_weights={k:v['weight'] for k,v in config['audience'].items()}
    flow_index,physical_flow,source_coverage,flow_warnings=_transport_audience(ft,config,stress)
    warnings.extend(flow_warnings)
    audience['transit_flow']=value('transit_flow',physical_flow,flow_index,'audience',
        excluded_inputs=['local_commercial_daily','separate_metro_bonus'],
        source='foot_traffic.metro_contribution_daily+stops_contribution_daily',active_hours=config['active_hours'])
    trace['transit_flow']['route_conditioning']=copy.deepcopy(getattr(ft,'transport_route_diagnostics',{}))
    trace['transit_flow']['evidence_coverage']=source_coverage if flow_index else 0
    trace['transit_flow']['known']=bool(flow_index and source_coverage>0)
    if source_coverage<1:missing.append('transit_flow')
    for key,attr in (('residents','estimated_residents_count'),('workers','estimated_office_workers_count')):
        number=getattr(population,attr,None) if population_counts_available(population) else None
        interval=_observed(number,stress)
        if key=='residents':
            from .population import population_residents_interval
            bounds=population_residents_interval(population)
            if bounds is not None:interval=Interval(max(0,bounds[0]*(1-stress)),bounds[1]*(1+stress))
        audience[key]=value(key,number,_saturate(interval,config['references'][key]['value'],stress) if interval else None,
                            'audience',source='population.'+attr,utility_reference=config['references'][key],
                            population_provenance=copy.deepcopy(getattr(population,'population_model',{})) if key=='residents' else {})
    fact=business_evidence(prop).get('site_audience_daily',{})
    number=fact.get('value') if isinstance(fact,dict) and fact.get('verified') is True and fact.get('source') else None
    interval=_observed(number,stress)
    audience['site_audience']=value('site_audience',number,
        _saturate(interval,config['references']['site_audience']['value'],stress) if interval else None,
        'audience',source=fact.get('source') if isinstance(fact,dict) else None)
    synergy_cards=groups['synergy']
    known_synergy=Interval(sum(strength_by_org[str(c.org_id)].low for c in synergy_cards),
                          sum(strength_by_org[str(c.org_id)].high for c in synergy_cards))
    synergy_strength=Interval(known_synergy.low,known_synergy.high+unknown_count)
    synergy_index=_saturate(synergy_strength,config['references']['synergy']['value'],stress)
    if not inventory_complete:synergy_index=Interval(synergy_index.low,1)
    audience['synergy']=value('synergy',len(synergy_cards),
        synergy_index,'audience',source='classified_synergy_organizations',unclassified_count=unknown_count)
    trace['synergy']['known']=classification_complete
    trace['synergy']['evidence_coverage']=1 if classification_complete else 0
    if not classification_complete:missing.append('synergy')
    audience_range=_blend(audience,audience_weights)
    audience_coverage=sum(audience_weights[k]*trace[k].get('evidence_coverage',1) for k in AUDIENCE if trace[k]['known'])
    # Financial/reputation gaps in attraction sources remain gaps in the index.
    if audience_weights['synergy'] and synergy_cards and classification_complete:
        synergy_cov=sum(financial_details[str(c.org_id)]['coverage'] for c in synergy_cards)/len(synergy_cards)
        audience_coverage-=audience_weights['synergy']*(1-synergy_cov)
    alternatives=config['alternatives'];pressure_lo=pressure_hi=0
    for group in ('direct','indirect'):
        weight=alternatives[group]['weight']
        pressure_lo+=weight*sum(strength_by_org[str(c.org_id)].low for c in groups[group])
        pressure_hi+=weight*sum(strength_by_org[str(c.org_id)].high for c in groups[group])
    # Every unknown found organization can occupy at most one alternative
    # class and has normalized strength <= 1. No niche coefficient is added.
    pressure_hi+=max(alternatives[k]['weight'] for k in ('direct','indirect'))*unknown_count
    scale=config['competition_scale']['value'];outside=config['outside_pressure']['value']
    choice=Interval(1/(1+outside*(1+stress)+pressure_hi/(1+scale*(1-stress)*audience_range.low)) if inventory_complete else 0,
                    1/(1+outside*(1-stress)+pressure_lo/(1+scale*(1+stress)*audience_range.high)))
    competitor_cards=groups['direct']+groups['indirect']
    possible_alternatives=len(competitor_cards)+unknown_count
    choice_coverage=((sum(financial_details[str(c.org_id)]['coverage'] for c in competitor_cards)/possible_alternatives
                      if possible_alternatives else weights['presence']) if inventory_complete else 0)
    if not competitor_cards:missing.append('observed_competitive_alternatives')
    access=config['access'];access_range=Interval(1,1);access_coverage=1
    if access['kind']!='none':
        key='walk_time_minutes' if access['kind']=='walk' else 'delivery_time_minutes'
        route=resolve_access_route(business_evidence(prop),access['kind'])
        number=route.get('minutes') if route['status']=='reachable' else None
        lower=route.get('minutes_lower',number);upper=route.get('minutes_upper',number)
        interval=(Interval(lower*(1-stress),upper*(1+stress)) if lower is not None and upper is not None else _observed(number,stress))
        bounded_wait=(Interval(lower*(1-stress)/(lower*(1-stress)+access['reference_minutes']*(1+stress)),1)
                      if lower is not None and upper is None else None)
        desirability=(Interval(1,1) if route['status']=='unreachable' else
                      _saturate(interval,access['reference_minutes'],stress) if interval else bounded_wait or Interval(0,1))
        access_range=Interval(1-access['weight']*desirability.high,1-access['weight']*desirability.low)
        # Missing route leaves demand magnitude known; treat unverified route as 1 unknown modifier of weight w.
        access_coverage=1 if interval or route['status']=='unreachable' else 1/(1+access['weight'])
        value(key,number,interval,'access',source=route.get('source'),route_status=route['status'],route_evidence=route)
        if route.get('scope') in ('building_entrance','building_entrance_interval'):warnings.append('Время подхода рассчитано по отмеченным входам здания; дверь конкретного арендуемого блока проверяется отдельно')
        if route['status']=='unreachable':missing.remove(key)
    route_conditioned=bool(getattr(ft,'transport_route_diagnostics',{}).get('applied')) and access['kind']=='walk'
    transit_weight=audience_weights['transit_flow'] if route_conditioned else 0
    transit=audience['transit_flow']
    # Transit exposure already includes this walk to the entry. Apply the
    # general access factor only to other audience components, not twice.
    accessible=Interval(transit_weight*transit.low+max(0,audience_range.low-transit_weight*transit.low)*access_range.low,
                        transit_weight*transit.high+max(0,audience_range.high-transit_weight*transit.high)*access_range.high)
    known_transit=transit_weight*trace['transit_flow'].get('evidence_coverage',0) if trace['transit_flow']['known'] else 0
    final_coverage=known_transit+max(0,audience_coverage-known_transit)*access_coverage
    final=Interval(100*accessible.low*choice.low,100*accessible.high*choice.high)
    details=dict(status='scenario_index',model='integrated_market_v2_route_audience',sources=trace,organizations=financial_details,
        organization_inventory=dict(collection_status=collection_status,classified_count=len(classified_ids),
            unclassified_count=unknown_count,collection_complete=inventory_complete,classification_complete=classification_complete),
        audience_index=Interval(100*audience_range.low,100*audience_range.high).as_dict(),
        accessible_audience_index=Interval(100*accessible.low,100*accessible.high).as_dict(),
        transit_route_accounted_once=route_conditioned,
        choice_factor=choice.as_dict(),access_factor=access_range.as_dict(),
        coverage=max(0,min(1,final_coverage*choice_coverage)),
        missing_inputs=sorted(set(k for k in missing if k not in AUDIENCE or audience_weights[k]>0)),
        used_organization_counts={k:len(v) for k,v in groups.items()},financial_entities=len(accounts),
        optional_fns=dict(total_relevant_orgs=len(all_cards),matched_orgs=fns_matched_count,
            applicable_orgs=fns_applicable_count,excluded_or_unmatched_orgs=len(all_cards)-fns_applicable_count,
            mode='optional_for_inapplicable_or_unmatched_entities'),
        deduplicated_organizations=len(seen),warnings=list(dict.fromkeys(warnings)),
        utility_references=copy.deepcopy(config['references']),
        reference_kind='scenario_utility_assumption_not_market_measurement')
    if automatic_access:details['automatic_access']=automatic_access
    return final,details
