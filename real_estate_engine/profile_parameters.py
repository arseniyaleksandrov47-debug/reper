"""Resolve invalid numerical preference parameters independently of formula structure."""
import asyncio
import copy
import json
import math

from .ai_contract import AIContractError
from .market_context import AUDIENCE,STRENGTH,REFERENCES
from .scenario_contract import compile_priority_order


def parameter_issues(profile,market,scenario,metric_groups):
    if not isinstance(market,dict):
        return []
    issues=[]

    def item(group,key):
        values=market.get(group)
        return values.get(key) if isinstance(values,dict) else None

    def add(path,item,field,maximum,role):
        if not isinstance(item,dict):return
        value=item.get(field)
        if type(value) not in (int,float) or not math.isfinite(value):return
        if value>0:return
        issues.append(dict(path=path+[field],maximum=maximum,role=role,
            value=value,reason=item.get('reason',''),quote=item.get('concept_quote','')))

    for key in REFERENCES:
        add(['market_context','references',key],item('references',key),'value',1e15,'positive_utility_scale')
    for key in ('competition_scale','outside_pressure'):
        add(['market_context',key],market.get(key),'value',1e15,'positive_utility_scale')
    for group,names in (('strength',STRENGTH),('alternatives',('direct','indirect'))):
        for key in names:
            add(['market_context',group,key],item(group,key),'weight',1,'positive_relative_importance')
    usage=scenario.get('audience_usage',{}) if scenario else {}
    for key in AUDIENCE:
        if usage.get(key,{}).get('usage')=='use':
            add(['market_context','audience',key],item('audience',key),'weight',1,'required_by_independent_applicability')
    access=market.get('access',{})
    if isinstance(access,dict) and access.get('kind') in ('walk','delivery'):
        add(['market_context','access'],access,'weight',1,'positive_relative_importance')
        add(['market_context','access'],access,'reference_minutes',1e15,'positive_time_scale')
    if scenario and isinstance(profile.get('criteria'),list):
        try:compile_priority_order(profile,scenario,metric_groups)
        except AIContractError as exc:
            if str(exc)=='Equal importances violate an explicit client priority':
                for index,criterion in enumerate(profile['criteria']):
                    issues.append(dict(path=['criteria',index,'weight'],maximum=1,
                        role='strict_client_priority',value=criterion['weight'],
                        metric=criterion['metric'],reason=criterion.get('reason',''),quote=criterion.get('concept_quote','')))
    return issues


async def resolve_parameters(analyzer,concept,profile,market,scenario,issues,primary_model=None):
    """Only model-supplied assumptions may replace the invalid assumptions."""
    call=getattr(analyzer,'_call_profile_parameters_structured_sync',None)
    if not call:raise AIContractError('Numerical parameter interpretation unavailable')
    slots={f'p{i}':issue for i,issue in enumerate(issues)}
    properties={key:dict(type='object',additionalProperties=False,required=['value','reason'],properties={
        'value':dict(type='number',exclusiveMinimum=0,maximum=issue['maximum']),
        'reason':dict(type='string',minLength=1,maxLength=160)}) for key,issue in slots.items()}
    schema=dict(type='object',additionalProperties=False,required=list(slots),properties=properties)
    prompt=(
        'Уточни только перечисленные численные ПАРАМЕТРЫ ПРЕДПОЧТЕНИЙ. Объявлений и замеров здесь нет. '
        'Не выбирай применимость источников заново: independent_applicability уже проверена. '
        'use требует положительную относительную важность, даже если она мала. '
        'Масштаб x/(x+s) строго положителен, это s, а не количество фактических покупателей. '
        'У неиспользуемого источника масштаб также положителен и не влияет на результат. '
        'Не меняй структуру формулы, требования, цитаты или приоритеты. '
        'strict_client_priority требует разных положительных важностей с соблюдением higher>lower. '
        'Значения выбирай по концепции как явные условные допущения, не из справочника бизнесов. '
        'Все value строго больше нуля и не больше указанного maximum. Верни только JSON по схеме.\nINPUT_JSON\n'+
        json.dumps(dict(task='resolve_profile_parameters',concept=concept,parameters=slots,
            independent_applicability=scenario.get('audience_usage',{}) if scenario else {},
            priority_rules=scenario.get('priority_rules',[]) if scenario else {}),ensure_ascii=False))
    raw,model=await asyncio.to_thread(call,prompt,250+160*len(slots),response_schema=schema)
    if primary_model is not None and str(model)==str(primary_model):
        raise AIContractError('Independent numerical parameter model unavailable')
    response=json.loads(raw)
    if not isinstance(response,dict) or set(response)!=set(slots):raise AIContractError('Parameter response does not match requested fields')
    for key,issue in slots.items():
        item=response[key]
        if (not isinstance(item,dict) or set(item)!={'value','reason'} or type(item['value']) not in (int,float) or
                not math.isfinite(item['value']) or not 0<item['value']<=issue['maximum'] or
                not isinstance(item['reason'],str) or not item['reason'].strip() or len(item['reason'])>160):
            raise AIContractError('Invalid independently proposed numerical parameter')
    updated=copy.deepcopy(profile);updated['market_context']=copy.deepcopy(market)
    changes=[]
    for key,issue in slots.items():
        target=updated
        for part in issue['path'][:-1]:target=target[part]
        target[issue['path'][-1]]=response[key]['value']
        if 'reason' in target:target['reason']=response[key]['reason']
        changes.append(dict(path=issue['path'],before=issue['value'],after=response[key]['value'],reason=response[key]['reason']))
    updated_market=updated.pop('market_context')
    return updated,updated_market,dict(model=str(model),changes=changes,
        origin='independent_model_assumptions',property_facts_generated=False)
