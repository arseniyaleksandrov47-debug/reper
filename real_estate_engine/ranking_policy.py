"""One concept-derived, validated scoring configuration shared by a search.

The model selects measured/proxy features and priorities. It never receives
candidate advertisements, supplies listing facts, or emits executable code.
Raw-unit normalization is bound in code to the common reference snapshot.
"""
import asyncio
import copy
import hashlib
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from functools import partial

from .ai_contract import AIContractError, AIResponseError, AITransportError, AI_DEADLINE, strict_json_object
from .business_model import Interval, analyze_business_model
from .ranking_scales import reference_for
from .scenario_contract import scenario_schema,scenario_prompt,normalize_scenario_response,compile_priority_order
from .market_context import (
    market_context_schema,bind_market_references,validate_market_context,evaluate_market_context,
    apply_default_market_scales,default_market_reference_provenance)
from .market_context import audience_usage_schema,validate_audience_usage,compile_audience_usage,normalize_audience_annotations


# These are measurement capabilities, not business categories. Features in the
# same family share a signal and cannot receive separate additive rewards.
METRICS = {
    'market_fit_index': ('Адаптивная пригодность окружения: аудитория, прямые/косвенные альтернативы, синергия, отзывы и финансовая устойчивость юрлиц; сценарный индекс, не прогноз заказов', 'demand_result', 'index'),
    'passing_traffic_index': ('Проходящий поток: модельный индекс 0–100; не измеренные покупатели', 'external_arrival', 'index'),
    'metro_distance_m': ('Расстояние до метро по прямой; не пеший маршрут', 'external_arrival', 'm'),
    'stop_distance_m': ('Расстояние до остановки по прямой; не пеший маршрут', 'external_arrival', 'm'),
    'residents_count': ('Оценка жителей в круге анализа; не подтверждённый спрос', 'local_market', 'people'),
    'office_workers_count': ('Оценка работников в окружении; не аудитория внутри данного здания', 'local_market', 'people'),
    'competitors_per_1000_people': ('Прямые конкуренты на 1000 модельных жителей и работников; прокси предложения/аудитории. При нуле конкурентов спрос неизвестен', 'local_market', 'ratio'),
    'rent_month': ('Аренда в месяц, без неуказанных расходов на эксплуатацию и ремонт', 'occupancy_cost', 'rub/month'),
    'rent_sqm_year': ('Аренда за квадратный метр в год', 'occupancy_cost', 'rub/sqm/year'),
    'area_sqm': ('Площадь помещения', 'physical_fit', 'sqm'),
    'site_audience_daily': ('Подтверждённая внутренняя аудитория именно этой площадки за день; отсутствует без отдельного источника', 'internal_audience', 'people/day'),
    'walk_time_minutes': ('Подтверждённое время пешего маршрута до входа; неизвестно без маршрута', 'external_arrival', 'minutes'),
    'delivery_time_minutes': ('Подтверждённое время доставки для заданной зоны; неизвестно без маршрута', 'delivery_access', 'minutes'),
    'accessible_audience_daily': ('Доступная аудитория с учётом входа, маршрутов, часов работы и непересекающихся групп; сценарный диапазон, не покупатели', 'demand_result', 'people/day'),
    'orders_daily': ('Возможные заказы: доступная аудитория, сценарная конверсия и выбор среди конкурентов/внешних альтернатив; требует исходных диапазонов', 'demand_result', 'orders/day'),
    'premises_cost_month': ('Аренда + эксплуатация; подготовка учитывается только при предоставленных клиентом данных, иначе остаётся вне расчёта', 'occupancy_cost', 'rub/month'),
    'net_contribution_month': ('Сценарный результат Q × вклад заказа × дни − постоянные расходы; не прогноз прибыли', 'business_result', 'rub/month'),
}

# A metric derived from another metric's inputs must not be rewarded again.
# Audience, travel and competition are stages of one pipeline. Independent
# additive proxies cannot establish that the underlying people are disjoint.
MODEL_METRICS = {'accessible_audience_daily', 'orders_daily', 'premises_cost_month', 'net_contribution_month'}
SIGNAL_INPUTS = {name: {'audience_access_choice'} for name in METRICS}
for _name in ('rent_month', 'rent_sqm_year', 'premises_cost_month'):
    SIGNAL_INPUTS[_name] = {'premises_cost'}
SIGNAL_INPUTS['area_sqm'] = {'physical_fit'}
SIGNAL_INPUTS['rent_sqm_year'] = {'premises_cost', 'physical_fit'}
SIGNAL_INPUTS['net_contribution_month'] = {'audience_access_choice', 'premises_cost'}

# Native generation only chooses a suitability profile. The signed business
# result remains a diagnostic/manual objective until the underlying inputs are
# calibrated. Three wire slots prevent the model from emitting each stage of
# the same demand pipeline as an independent criterion.
PROFILE_GROUPS = {
    'audience_access_choice': ['market_fit_index'],
    'occupancy_cost': ['rent_month', 'premises_cost_month'],
    'physical_fit': ['area_sqm'],
}


@dataclass(frozen=True)
class RankingCriterion:
    metric: str
    weight: float
    preference: str
    low: float
    high: float
    concept_quote: str
    reason: str
    assumption: str


@dataclass(frozen=True)
class RankingProfile:
    status: str = 'needs_context'
    concept: str = ''
    summary: str = ''
    criteria: tuple = ()
    questions: tuple = ()
    assumptions: tuple = ()
    model: str = ''
    analysis_status: str = 'missing_concept'
    metadata: dict = field(default_factory=dict)
    visit_context_quotes: tuple = ()
    unmodeled_requirements: tuple = ()


def concept_fragments(concept):
    return {f's{i}':piece.strip() for i,piece in enumerate(re.split(r'[;\n]+|(?<=[.!?])\s+(?=[А-ЯЁA-Z])',concept)) if piece.strip()}


def explicit_area_bands(concept):
    """Read literal numeric area bands, never infer preferences from a niche."""
    pattern=r'(?<![\d−-])(\d+(?:[.,]\d+)?)\s*(?:[–—-]|до)\s*(\d+(?:[.,]\d+)?)\s*(?:м\s*[²2]|м\^2|кв\.?\s*м(?:\.|\b))'
    bands=set()
    for low,high in re.findall(pattern,concept,re.I):
        low=float(low.replace(',','.'));high=float(high.replace(',','.'))
        if math.isfinite(low) and math.isfinite(high) and 0<low<high:bands.add((low,high))
    return bands


def profile_schema(fragments):
    def obj(properties):
        return dict(type='object',properties=properties,required=list(properties),additionalProperties=False)
    string=dict(type='string',maxLength=180)
    source=dict(type='string',enum=list(fragments))
    criterion=obj(dict(metric=dict(type='string',enum=list(METRICS)),weight=dict(type='number',exclusiveMinimum=0,maximum=1),
        preference=dict(type='string',enum=['higher','lower','range']),low=dict(type='number',minimum=0),
        high=dict(type='number',minimum=0),concept_quote=source,reason=string,assumption=string))
    groups={}
    for family,names in PROFILE_GROUPS.items():
        grouped=copy.deepcopy(criterion);grouped['type']=['object','null']
        grouped['properties']['metric']['enum']=names
        if family in ('audience_access_choice','occupancy_cost'):
            grouped['properties']['preference']['enum']=['higher' if family=='audience_access_choice' else 'lower']
            grouped['properties']['low']['enum']=[0];grouped['properties']['high']['enum']=[100]
        if family=='physical_fit':
            bands=explicit_area_bands(' '.join(fragments.values()))
            if len(bands)==1:
                low,high=next(iter(bands))
                grouped['properties']['preference']['enum']=['range']
                grouped['properties']['low']['enum']=[low];grouped['properties']['high']['enum']=[high]
                grouped['properties']['concept_quote']['enum']=[k for k,v in fragments.items() if (low,high) in explicit_area_bands(v)]
        groups[family]=grouped
    criteria_schema=obj(groups)
    properties=dict(status=dict(type='string',enum=['ready','needs_context']),summary=string,
        criteria=criteria_schema,questions=dict(type='array',items=string,maxItems=4),
        assumptions=dict(type='array',items=string,maxItems=8),visit_context_quotes=dict(type='array',items=source,maxItems=6),
        unmodeled_requirements=dict(type='array',items=source,maxItems=12))
    properties['market_context']=market_context_schema(fragments)
    properties['market_context']['type']=['object','null']
    return obj(properties)


def bind_concept_references(data,fragments):
    data=copy.deepcopy(data)
    def quote(identifier):
        if not isinstance(identifier,str) or identifier not in fragments:raise AIContractError('Unknown concept fragment')
        return fragments[identifier]
    try:
        groups=data['criteria']
        if not isinstance(groups,dict) or set(groups)!=set(PROFILE_GROUPS):
            raise AIContractError('Invalid criterion groups')
        data['criteria']=[]
        for family,criterion in groups.items():
            if criterion is None:continue
            if not isinstance(criterion,dict) or criterion.get('metric') not in PROFILE_GROUPS[family]:
                raise AIContractError('Metric assigned to wrong signal family')
            data['criteria'].append(criterion)
        for criterion in data['criteria']:criterion['concept_quote']=quote(criterion['concept_quote'])
        for field in ('visit_context_quotes','unmodeled_requirements'):data[field]=[quote(k) for k in data[field]]
        if data.get('market_context') is not None:
            data['market_context']=bind_market_references(data['market_context'],fragments)
    except (KeyError,TypeError) as exc:raise AIContractError('Malformed concept references') from exc
    return data


def compile_importances(data):
    """Model importance values are relative; arithmetic belongs to the code."""
    data=copy.deepcopy(data)
    if data.get('status')!='ready':return data,{}
    criteria=data.get('criteria')
    if not isinstance(criteria,list) or not criteria:raise AIContractError('Ready profile has no criteria')
    for c in criteria:
        if not isinstance(c,dict) or type(c.get('weight')) not in (int,float) or not math.isfinite(c['weight']) or not 0<c['weight']<=1:
            raise AIContractError('Invalid relative importance')
    total=sum(c['weight'] for c in criteria)
    for c in criteria:c['weight']/=total
    # A fragment used only for cost/area/local-market scoring is not a literal
    # obligation about audience admission or a route to the offered floor.
    nonvisit=set();visit=set()
    for c in criteria:
        if c.get('metric') not in METRICS:continue
        target=nonvisit if METRICS[c['metric']][1] in ('occupancy_cost','physical_fit','local_market') else visit
        target.add(c.get('concept_quote'))
    removed=[q for q in data.get('visit_context_quotes',[]) if q in nonvisit-visit]
    data['visit_context_quotes']=[q for q in data.get('visit_context_quotes',[]) if q not in nonvisit-visit]
    return data,dict(raw_importance_sum=total,importance_normalized_by_code=True,removed_nonvisit_quotes=removed)


def validate_profile(data,concept,model='',metadata=None):
    if not isinstance(data,dict) or set(data)!={'status','summary','criteria','questions','assumptions','visit_context_quotes','unmodeled_requirements'}:
        raise AIContractError('Invalid ranking profile envelope')
    if data['status'] not in ('ready','needs_context') or not isinstance(data['summary'],str) or not data['summary'].strip():
        raise AIContractError('Invalid profile status or summary')
    for name in ('questions','assumptions','visit_context_quotes','unmodeled_requirements'):
        if not isinstance(data[name],list) or not all(isinstance(x,str) and x.strip() for x in data[name]):
            raise AIContractError('Invalid profile text list')
    if any(q not in concept for q in data['visit_context_quotes']+data['unmodeled_requirements']):raise AIContractError('Invented scenario quote')
    if not isinstance(data['criteria'],list) or len(data['criteria'])>8:
        raise AIContractError('Invalid profile criteria')
    criteria=[];families=set();inputs=set()
    for c in data['criteria']:
        if not isinstance(c,dict) or set(c)!={f for f in RankingCriterion.__dataclass_fields__}:
            raise AIContractError('Invalid criterion fields')
        if not isinstance(c['metric'],str) or c['metric'] not in METRICS or c['preference'] not in ('higher','lower','range'):
            raise AIContractError('Unknown metric or preference')
        if any(type(c[k]) not in (int,float) or not math.isfinite(c[k]) for k in ('weight','low','high')):
            raise AIContractError('Non-finite criterion parameter')
        if not 0<c['weight']<=1 or not c['low']<c['high'] or (c['low']<0 and c['metric']!='net_contribution_month'):
            raise AIContractError('Invalid weight or utility anchors')
        if (metadata and metadata.get('market_integration') and metadata.get('scale_origin')=='ai_placeholders'
                and c['preference']!='range' and (c['low']!=0 or c['high']!=100)):
            raise AIContractError('Native higher/lower anchors must be 0..100 placeholders; use range for an explicit preferred area band')
        if c['metric']=='passing_traffic_index' and c['high']>100:raise AIContractError('Traffic index scale exceeds its domain')
        if c['metric']=='market_fit_index' and (c['preference']!='higher' or c['low']!=0 or c['high']!=100):
            raise AIContractError('Integrated market suitability has a code-owned 0..100 higher-is-better domain')
        if c['preference']=='range' and c['metric']!='area_sqm':
            raise AIContractError('Range utility is only supported for area')
        if any(not isinstance(c[k],str) for k in ('concept_quote','reason','assumption')):
            raise AIContractError('Invalid criterion explanation')
        if not c['reason'].strip() or not c['concept_quote'].strip() or c['concept_quote'] not in concept:
            raise AIContractError('Criterion not grounded in client concept')
        family=METRICS[c['metric']][1]
        if family in families:raise AIContractError('Repeated signal family')
        if inputs & SIGNAL_INPUTS[c['metric']]:raise AIContractError('Repeated underlying inputs: audience/access/choice and cost must each enter once')
        inputs.update(SIGNAL_INPUTS[c['metric']])
        families.add(family);criteria.append(RankingCriterion(**c))
    if data['status']=='ready':
        if metadata and metadata.get('market_integration') and metadata.get('scale_origin')=='ai_placeholders':
            bands=explicit_area_bands(concept)
            if len(bands)==1:
                low,high=next(iter(bands));area=next((c for c in criteria if c.metric=='area_sqm'),None)
                if area is None or area.preference!='range' or area.low!=low or area.high!=high:
                    raise AIContractError('Explicit client area band must be preserved as range with its literal bounds')
        if not criteria:raise AIContractError('Ready profile has no criteria')
        if not data['visit_context_quotes']:raise AIContractError('Ready profile has no visit scenario')
        if data['questions']:raise AIContractError('Ready profile still asks context questions')
        if abs(sum(c.weight for c in criteria)-1)>1e-6:
            raise AIContractError('Compiled weights must sum to one')
        if any(c.metric=='market_fit_index' for c in criteria):
            if not metadata or not metadata.get('market_context'):
                raise AIContractError('Integrated market index requires a complete scenario configuration')
            metadata={**metadata,'market_context':validate_market_context(metadata['market_context'],concept)}
    elif criteria or not data['questions']:
        raise AIContractError('Ambiguous concept needs questions and no ranking')
    return RankingProfile(data['status'],concept,data['summary'],tuple(criteria),tuple(data['questions']),
        tuple(data['assumptions']),model,'valid',metadata or {},tuple(data['visit_context_quotes']),tuple(data['unmodeled_requirements']))


class RankingProfileResolver:
    def __init__(self,analyzer,*,cache_ttl=3600,max_cached=64):
        self.analyzer=analyzer;self.cache_ttl=cache_ttl;self.max_cached=max_cached
        self._cache=OrderedDict();self._lock=asyncio.Lock()

    async def resolve(self,business,concept,requirements=None,timeout_s=90):
        if type(timeout_s) not in (int,float) or not math.isfinite(timeout_s) or timeout_s<=0:
            raise ValueError('Profile timeout must be positive and finite')
        if not isinstance(concept,str) or not concept.strip():
            return RankingProfile(questions=('Опишите концепцию: кто покупатели, как они приходят и что для вас важнее при выборе помещения?',))
        deadline=time.monotonic()+timeout_s
        cache_requirements=copy.deepcopy(requirements or {})
        for name,value in cache_requirements.items():
            if type(value) is float and math.isfinite(value) and value.is_integer():cache_requirements[name]=int(value)
        key=hashlib.sha256(json.dumps(dict(revision=23,business=business,concept=concept,requirements=cache_requirements,
            model=getattr(self.analyzer,'ranking_model',getattr(self.analyzer,'floor_model','configured')),
            interpreter_model=getattr(self.analyzer,'floor_review_model','configured')),sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        async def under_lock():
            async with self._lock:
                cached=self._cache.get(key)
                if cached and cached[0]>time.monotonic():
                    self._cache.move_to_end(key);return copy.deepcopy(cached[1])
                token=AI_DEADLINE.set(deadline)
                try:profile=await self._generate(business,concept,requirements or {})
                finally:AI_DEADLINE.reset(token)
                if profile.status=='ready':
                    self._cache[key]=(time.monotonic()+self.cache_ttl,copy.deepcopy(profile));self._cache.move_to_end(key)
                    while len(self._cache)>self.max_cached:self._cache.popitem(last=False)
                return profile
        try:return await asyncio.wait_for(under_lock(),timeout_s)
        except Exception as exc:
            status='rate_limited' if isinstance(exc,AITransportError) and exc.status_code==429 else 'schema_error' if isinstance(exc,AIContractError) else 'unavailable'
            return RankingProfile(concept=concept,summary='Профиль оценки концепции не подтверждён',
                questions=('Повторите формирование профиля оценки: AI не вернул пригодную конфигурацию.',),analysis_status=status,
                metadata={'contract_error':str(exc)} if isinstance(exc,AIContractError) else {'error_type':type(exc).__name__})

    async def _generate(self,business,concept,requirements):
        structured=getattr(self.analyzer,'_call_ranking_profile_structured_sync',None)
        if not callable(structured):raise RuntimeError('Structured concept ranking inference unavailable')
        fragments=concept_fragments(concept)
        sources=dict(fragments)
        explicit_context=requirements.get('explicit_visit_context')
        if explicit_context and explicit_context!=concept:
            sources.update({'visit_'+k:v for k,v in concept_fragments(explicit_context).items()})
        semantic_infer=getattr(self.analyzer,'_call_scenario_contract_structured_sync',None)
        if not callable(semantic_infer):raise RuntimeError('Independent scenario interpretation unavailable')
        pacer=getattr(self.analyzer,'floor_request_pacer',None)
        if pacer:await pacer.wait()
        semantic_schema=scenario_schema(sources);semantic_prompt=scenario_prompt(business,concept,sources)
        semantic_schema['properties']['audience_usage']=audience_usage_schema(sources)
        semantic_schema['required'].append('audience_usage')
        extra=('Дополнительно audience_usage определяет применимость источников аудитории из концепции, без весов и объявлений: '
               'transit_flow — случайный поток от транспорта; residents — местные жители; workers — работники района; '
               'site_audience — уже находящиеся ВНУТРИ конкретной площадки; synergy — соседние бизнесы, полезные концепции. '
               'usage=waived ТОЛЬКО при прямом отказе клиента (ссылка на фрагмент waiver); use для потенциально значимого источника; '
               'not_applicable при несовместимости источника со способом посещения. Для внешних посетителей site_audience=not_applicable; '
               'прямое «случайный поток не нужен» означает transit_flow=waived. '
               'Отсутствие упоминания работников, жителей или синергии НЕ является исключением: ставь use. '
               'not_applicable для этих источников допустим только при явно закрытой/исключительной аудитории, с прямой цитатой ограничения. '
               'Не обнуляй местных жителей/работников только из-за записи: они могут быть целевой аудиторией. '
               'Каждому решению нужны source_id исходного текста и reason. '
               'При waived заполни waiver_quote точной цитатой, снимающей ИМЕННО ЭТОТ источник. '
               'Отказ от прохожих не снимает работников или жителей. Для остальных usage waiver_quote=null. ')
        semantic_prompt=extra+semantic_prompt
        semantic_errors=[]
        for attempt in range(2):
            try:
                if attempt and pacer:await pacer.wait()
                semantic_response=await asyncio.to_thread(semantic_infer,semantic_prompt,2600,semantic_schema)
                semantic_raw,semantic_model=semantic_response
                semantic_data=strict_json_object(semantic_raw)
                audience_usage,ignored_annotations=normalize_audience_annotations(semantic_data.pop('audience_usage',None))
                scenario=normalize_scenario_response(semantic_data,sources)
                scenario['audience_usage']=validate_audience_usage(audience_usage,sources,scenario)
                break
            except (AIContractError,AIResponseError) as exc:
                if attempt:raise
                semantic_errors.append(str(exc))
                semantic_prompt=('Исправь нарушение контракта предыдущего ответа: '+str(exc)+'. '
                    'Заново разбери исходный текст. Приоритеты должны быть непротиворечивыми и следовать только явным сравнениям клиента.\n'+semantic_prompt)
            except AITransportError as exc:
                if attempt or not exc.retryable or exc.status_code==429:raise
                semantic_errors.append(str(exc))
        scenario_meta=dict(scenario_contract=scenario,scenario_semantic_status='independently_interpreted',
                           scenario_model=semantic_model,scenario_metadata=getattr(semantic_response,'metadata',{}),
                           scenario_repair_errors=semantic_errors)
        if ignored_annotations:scenario_meta['ignored_unused_annotations']=ignored_annotations
        from .request_filters import literal_filter_checks
        filter_quotes=dict.fromkeys([*sources.values(),*[item['quote'] for item in scenario['premises_requirements']]])
        scenario['applied_search_filters']=[dict(quote=quote,checks=checks) for quote in filter_quotes
            if (checks:=literal_filter_checks(quote,requirements))]
        if scenario['status']!='ready':
            return RankingProfile(concept=concept,summary='Нужно уточнить смысл концепции',questions=tuple(scenario['questions']),
                analysis_status='needs_context',metadata=scenario_meta)
        infer=partial(structured,schema=profile_schema(fragments))
        wire_metrics={name:group for group,names in PROFILE_GROUPS.items() for name in names}
        data=dict(business=business,concept=concept,client_constraints=requirements,ranking_objective='adaptive_suitability',
            interpreted_scenario=scenario,
            concept_fragments=fragments,
            signal_inputs={name:sorted(SIGNAL_INPUTS[name]) for name in wire_metrics},
            available_metrics={k:dict(description=METRICS[k][0],family=group,unit=METRICS[k][2]) for k,group in wire_metrics.items()})
        instruction=('Составь адаптивный рейтинг пригодности по концепции клиента. Фактов объявлений здесь нет: не придумывай их. '
            'Отвечай JSON заданной схемы. Для всех concept_quote, visit_context_quotes, unmodeled_requirements возвращай ТОЛЬКО ID из concept_fragments. '
            'interpreted_scenario — независимый разбор. Соблюдай явные priority_rules, не выводи приоритеты из названия ниши. '
            'audience_usage определяет применимость источников: use требует положительный вес; waived/not_applicable — нулевой. '
            'Канал рекламы и запись являются контекстом бизнеса, а не требованиями к объявлению. '
            'criteria в этой схеме — ОБЪЕКТ с тремя ключами: audience_access_choice (один показатель общего спроса/доступности), occupancy_cost (один стоимости), physical_fit (площадь). '
            'Каждая группа либо null (не используется), либо один объект критерия. '
            'При needs_context все группы и market_context null. '
            'В этой версии для готового профиля audience_access_choice ОБЯЗАТЕЛЬНО содержит market_fit_index, '
            'а market_context задаёт адаптивную внутреннюю формулу использования ВСЕХ собранных блоков. '
            'Полный прогноз заказов остаётся отдельной диагностикой. '
            'Верхние weight — положительные относительные важности до 1, код нормирует их. Главный критерий должен иметь наибольшую важность. '
            'market_fit_index: higher, low=0 high=100. Для аренды/полных расходов: lower, low=0 high=100 как технические заполнители, '
            'код берёт отдельный ориентир клиента; для area_sqm: range с желаемыми low/high в м². '
            'В occupancy_cost строго различай одну аренду и полные расходы по смыслу запроса (название бизнеса не определяет объём расходов): '
            'выбери premises_cost_month, если клиент просит учитывать полные, общие или совокупные ежемесячные расходы/затраты на помещение '
            '(даже если эксплуатация и подготовка не перечислены поимённо и даже если в client_constraints передан фильтр max_price_month — фильтр аренды не отменяет запрос полных расходов); '
            'выбери rent_month, только если клиент сравнивает именно арендную плату / ставку аренды без требования полных расходов на содержание. '
            'Ремонт и обустройство по умолчанию рассчитывает арендатор, подготовка включается только при предоставленных клиентом данных. '
            'Нельзя добавлять эксплуатацию к критерию одной аренды или подменять полные расходы одной арендой. '
            'reason объясняет важность, assumption отмечает допущение; concept_quote ссылается на исходный смысл. '
            'Обязательные требования к этажу/входу проверяются до рейтинга. В visit_context_quotes только контекст аудитории, '
            'способ посещения и фактические требования доступа; стоимость и желаемая площадь туда не входят. '
            'В unmodeled_requirements отметь значимые требования без показателя, например парковку или видимость фасада. '
            'Не включай в них работу, которую клиент явно оставил себе. Числовые ограничения, уже переданные '
            'в client_constraints, учитываются фильтрами; отмечай лишь дополнительные неучтённые свойства. '
            'Если сценарий понятен, ready и questions пуст. Не спрашивай факты конкретного лота и не выдумывай точную прибыль. '
            'В market_context.audience распредели относительные важности transit_flow (только метро/остановки), residents, workers, site_audience и synergy. '
            'Пересекающиеся прокси аудитории код смешивает, а не складывает людей. Для внутренней аудитории можно обнулить внешние источники с обоснованием из концепции; '
            'для записи без случайных прохожих audience.transit_flow.weight может быть нулевым. '
            'При отсутствии внутреннего сценария audience.site_audience.weight=0. '
            'Остальные собранные источники получают положительную важность, пусть небольшую, если клиент прямо не снял их значимость. '
            'alternatives.direct и indirect ОБЯЗАТЕЛЬНО положительны: их отношение следует из отличий концепции от альтернатив. '
            'strength.presence, proximity, reputation, company_revenue и company_profit ОБЯЗАТЕЛЬНО положительны. '
            'ФНС и отзывы описывают силу найденных альтернатив, а не доказывают спрос: нет отдельного бонуса за богатое окружение. '
            'Финансы синергетических организаций влияют на источник synergy. Финансы одного ИНН учитываются один раз. '
            'Числовые опорные шкалы (references, competition_scale, outside_pressure, access.reference_minutes) '
            'и часы работы active_hours подставляются детерминированным кодом автоматически; не возвращай и не выдумывай их. '
            'access.kind none/walk/delivery: отдельный подтверждённый маршрут учитывается при значимости по концепции; '
            'для none допустим weight=0, для walk/delivery weight строго положителен. '
            'access.weight (0..1) — доля не-транзитной аудитории (жители, работники, синергия), обнуляемая длинным или неподтверждённым маршрутом; '
            'ставь умеренный вес по реальной чувствительности (обычно 0.15–0.5), а ~1.0 — лишь при полной изоляции без единственного маршрута. '
            'Все concept_quote внутри market_context — ID исходных фрагментов. '
            'Верхние веса market_fit_index, occupancy_cost и physical_fit по-прежнему следуют приоритетам клиента; нет универсальных процентов. '
            'Объяснения пиши кратко: reason и assumption до 80 символов, summary до 120 символов. '
            'Сохрани все поля схемы. ')
        instruction+=(
            'В market_context.alternatives и strength каждый weight строго больше нуля, включая company_revenue и company_profit. '
            'Важность может быть малой. Финансы характеризуют альтернативы; код учитывает связь организаций с одним ИНН. '
            'Неприменимость источника аудитории задаётся только через audience.*.weight=0. '
            'Используй краткую числовую запись без длинных дробей.')
        feedback='';generation_retry_errors=[];parameters_attempted=False;parameter_metadata={}
        for attempt in range(2):
            pacer=getattr(self.analyzer,'floor_request_pacer',None)
            if pacer:await pacer.wait()
            prompt=instruction+feedback+'\nINPUT_JSON\n'+json.dumps(data,ensure_ascii=False)
            try:
                response=await asyncio.to_thread(infer,prompt,2800)
                raw,model=response
                if str(model)==str(semantic_model):
                    raise AIContractError('Independent scenario interpretation requires a different model')
                parsed=strict_json_object(raw)
                parsed=bind_concept_references(parsed,fragments)
                market_config=parsed.pop('market_context',None);usage_meta={}
                if parsed.get('status')=='ready':
                    if not any(c.get('metric')=='market_fit_index' for c in parsed['criteria']):
                        raise AIContractError('Every collected market-analysis block must enter the integrated score')
                    market_config=apply_default_market_scales(market_config)
                    if not parameters_attempted and getattr(self.analyzer,'_call_profile_parameters_structured_sync',None):
                        from .profile_parameters import parameter_issues,resolve_parameters
                        issues=parameter_issues(parsed,market_config,scenario,wire_metrics)
                        if issues:
                            parameters_attempted=True
                            parsed,market_config,parameter_metadata=await resolve_parameters(
                                self.analyzer,concept,parsed,market_config,scenario,issues,primary_model=model)
                    market_config=validate_market_context(market_config,concept)
                    if scenario and scenario.get('audience_usage'):
                        market_config,usage_meta=compile_audience_usage(market_config,scenario['audience_usage'])
                parsed,compiled=compile_importances(parsed)
                priority_meta={}
                if scenario:
                    parsed,priority_meta=compile_priority_order(parsed,scenario,wire_metrics)
                return validate_profile(parsed,concept,model,{**getattr(response,'metadata',{}),**compiled,
                    **scenario_meta,**priority_meta,
                    'profile_retry_errors':generation_retry_errors,
                    **({'parameter_resolution':parameter_metadata} if parameter_metadata else {}),
                    **({'market_context':market_config,'market_integration':'all_collected_blocks_v1',
                        'market_reference_provenance':default_market_reference_provenance(),**usage_meta} if market_config else {}),
                    'scale_origin':'ai_placeholders','client_constraints':copy.deepcopy(requirements)})
            except (AIContractError,AIResponseError) as exc:
                if attempt:raise
                generation_retry_errors.append(str(exc))
                feedback='\nИсправь нарушение предыдущего ответа: '+str(exc)+'. Используй показатели из available_metrics и ID из concept_fragments.'
            except AITransportError as exc:
                # One retry within the caller's shared deadline; never replace
                # weights or silently switch the independently chosen model.
                if attempt or not exc.retryable or exc.status_code==429:raise
                generation_retry_errors.append(str(exc))


def utility(value,criterion):
    if criterion.preference=='range':
        distance=max(criterion.low-value,0,value-criterion.high)
        return max(0,100*(1-distance/(criterion.high-criterion.low)))
    if value<=criterion.low:score=0
    elif value>=criterion.high:score=100
    else:
        scale=max(abs(criterion.low),abs(criterion.high))
        score=100*(value/scale-criterion.low/scale)/(criterion.high/scale-criterion.low/scale)
    return score if criterion.preference=='higher' else 100-score


def _number(value):
    return float(value) if type(value) in (int,float) and math.isfinite(value) and value>=0 else None


def metric_value(metric,prop,report,concept=''):
    if metric in MODEL_METRICS:
        metrics,_=analyze_business_model(prop,report,concept)
        return metrics.get(metric,Interval()).midpoint
    ft=getattr(report,'foot_traffic',None);pop=getattr(report,'population',None)
    if metric in ('rent_month','rent_sqm_year','area_sqm'):
        value=_number(prop.get({'rent_month':'price_month','rent_sqm_year':'price_sqm_year','area_sqm':'area_sqm'}[metric]))
        return value if value is not None and value>0 else None
    if metric=='passing_traffic_index':return _number(ft.traffic_index) if ft and report.total_cards_found>0 else None
    if metric=='metro_distance_m':return _number(ft.nearest_metros[0].distance_m) if ft and ft.nearest_metros else None
    if metric=='stop_distance_m':return _number(ft.closest_stop_distance_m) if ft else None
    if metric in ('residents_count','office_workers_count','competitors_per_1000_people'):
        from .population import population_counts_available
        if not population_counts_available(pop):return None
        if metric=='residents_count':return _number(pop.estimated_residents_count)
        if metric=='office_workers_count':return _number(pop.estimated_office_workers_count)
        residents=_number(pop.estimated_residents_count);workers=_number(pop.estimated_office_workers_count)
        if (getattr(report,'collection_status',None)!='ok' or report.data_status!='ok'
                or not report.total_cards_found or not report.direct_competitors
                or residents is None or workers is None or residents+workers<=0):
            return None
        return len(report.direct_competitors)*1000/(residents+workers)
    if metric in ('site_audience_daily','walk_time_minutes','delivery_time_minutes'):
        raw=prop.get('raw_attributes',prop.get('raw_json',{}))
        if isinstance(raw,str):
            try:raw=json.loads(raw)
            except (ValueError,TypeError):return None
        if not isinstance(raw,dict):return None
        evidence=raw.get('business_evidence',{})
        if metric in ('walk_time_minutes','delivery_time_minutes') and isinstance(evidence,dict):
            from .accessibility import resolve_access_route
            route=resolve_access_route(evidence,'walk' if metric=='walk_time_minutes' else 'delivery')
            return route['minutes'] if route['status']=='reachable' else None
        fact=evidence.get(metric,{}) if isinstance(evidence,dict) else {}
        if not isinstance(fact,dict) or fact.get('verified') is not True or not isinstance(fact.get('source'),str) or not fact['source'].strip():return None
        return _number(fact.get('value'))
    return None


def _model_estimates(prop,report,concept,stress,context):
    key=(id(prop),id(report),concept,stress)
    if key not in context:
        context[key]=analyze_business_model(prop,report,concept,stress=stress,network_cache=context.setdefault('networks',{}))
    return context[key]


def evaluate_profile(profile,prop,report,input_error_fraction=.2,*,evaluation_context=None):
    if profile.status!='ready':raise ValueError('A validated ready ranking profile is required')
    if profile.metadata.get('scale_origin')=='ai_placeholders' and not profile.metadata.get('scales_bound_to_search'):
        profile=bind_reference_scales(profile)
    if type(input_error_fraction) not in (int,float) or not math.isfinite(input_error_fraction) or not 0<=input_error_fraction<1:
        raise ValueError('Input error fraction must be finite and in [0, 1)')
    model_metrics={};stress_metrics={};model_details={}
    context={} if evaluation_context is None else evaluation_context
    if any(c.metric in MODEL_METRICS for c in profile.criteria):
        model_metrics,model_details=_model_estimates(prop,report,profile.concept,0,context)
        stress_metrics,_=_model_estimates(prop,report,profile.concept,input_error_fraction,context)
    market_interval=market_stress=None
    if any(c.metric=='market_fit_index' for c in profile.criteria):
        config=profile.metadata.get('market_context')
        if not config:raise ValueError('Missing integrated market configuration')
        config=validate_market_context(config,profile.concept)
        market_interval,market_details=evaluate_market_context(config,prop,report)
        market_stress,_=evaluate_market_context(config,prop,report,stress=input_error_fraction)
        model_details={**model_details,'market_context':market_details,
                       'warnings':list(dict.fromkeys([*model_details.get('warnings',[]),*market_details['warnings']]))}
    components={};lower=upper=input_lower=input_upper=coverage=0.0
    for criterion in profile.criteria:
        metric_coverage=1.0
        if criterion.metric=='market_fit_index':
            interval,stressed=market_interval,market_stress
            metric_coverage=model_details['market_context']['coverage']
        elif criterion.metric in MODEL_METRICS:
            interval=model_metrics.get(criterion.metric,Interval(None,None))
            stressed=stress_metrics.get(criterion.metric,Interval(None,None))
        elif criterion.metric=='residents_count':
            from .population import population_residents_interval
            bounds=population_residents_interval(getattr(report,'population',None))
            interval=Interval(*bounds) if bounds is not None else Interval(None,None)
            stressed=Interval(max(0,interval.low*(1-input_error_fraction)),interval.high*(1+input_error_fraction)) if bounds is not None else interval
        elif criterion.metric in ('rent_month','rent_sqm_year'):
            from .business_model import rent_is_lower_bound
            scalar=metric_value(criterion.metric,prop,report);lower_bound=rent_is_lower_bound(prop)
            interval=Interval(scalar,None if lower_bound else scalar)
            stressed=Interval(scalar*(1-input_error_fraction),None if lower_bound else scalar*(1+input_error_fraction)) if scalar is not None else interval
            if lower_bound:
                model_details['warnings']=[*model_details.get('warnings',[]),'Опубликована стартовая ставка торгов; окончательная аренда неизвестна и учитывается диапазоном']
        else:
            scalar=metric_value(criterion.metric,prop,report)
            interval=Interval(scalar,scalar)
            stressed=Interval(scalar*(1-input_error_fraction),scalar*(1+input_error_fraction)) if scalar is not None else interval
        value=interval.midpoint if metric_coverage>0 else None
        scale=profile.metadata.get('reference_scales',{}).get(criterion.metric,{})
        scale_usable=scale.get('usable',True)
        score=utility(value,criterion) if value is not None and scale_usable else None
        lo,hi=_utility_interval(interval,criterion) if scale_usable else (0,100)
        ilo,ihi=_utility_interval(stressed,criterion) if scale_usable else (0,100)
        lo*=criterion.weight;hi*=criterion.weight;ilo*=criterion.weight;ihi*=criterion.weight
        if value is not None and scale_usable:coverage+=criterion.weight*metric_coverage
        components[criterion.metric]=dict(value=value,unit=METRICS[criterion.metric][2],utility=score,weight=criterion.weight,
            value_lower=interval.low,value_upper=interval.high,
            signal_inputs=sorted(SIGNAL_INPUTS[criterion.metric]),
            scale_usable=scale_usable,scale_source=scale.get('source','caller_profile'),
            evidence_coverage=metric_coverage,
            missing_reason=('missing_scale' if not scale_usable else 'missing_value' if value is None else ''),
            contribution_lower=lo,contribution_upper=hi,reason=criterion.reason,concept_quote=criterion.concept_quote)
        lower+=lo;upper+=hi;input_lower+=ilo;input_upper+=ihi
    return dict(lower=max(0,min(100,lower)),upper=max(0,min(100,upper)),coverage=max(0,min(1,coverage)),
                input_lower=max(0,min(100,input_lower)),input_upper=max(0,min(100,input_upper)),components=components,business_model=model_details)


def explain_components(components):
    """Use the whole utility interval to distinguish strengths from uncertainty."""
    result=dict(pros=[],cons=[],uncertain=[])
    for metric,part in components.items():
        if part['value'] is None or part['utility'] is None or part['weight']<=0:continue
        low,high=part['value_lower'],part['value_upper']
        value_text=(f'{low:.2f}' if low==high else
            f"{low if low is not None else 'неизвестно'}–{high if high is not None else 'неизвестно'}")
        text=f"{METRICS[metric][0]}: {value_text}; вклад {part['contribution_lower']:.1f}–{part['contribution_upper']:.1f} из {part['weight']*100:.1f}"
        utility_low=part['contribution_lower']/part['weight'];utility_high=part['contribution_upper']/part['weight']
        if utility_low>=50:result['pros'].append(text)
        elif utility_high<50:result['cons'].append(text)
        else:result['uncertain'].append('Неоднозначный вклад критерия: '+text)
    return result


def _utility_interval(interval,criterion):
    if criterion.preference=='higher':
        return (utility(interval.low,criterion) if interval.low is not None else 0,
                utility(interval.high,criterion) if interval.high is not None else 100)
    if criterion.preference=='lower':
        return (utility(interval.high,criterion) if interval.high is not None else 0,
                utility(interval.low,criterion) if interval.low is not None else 100)
    ends=[utility(v,criterion) for v in (interval.low,interval.high) if v is not None]
    lower=min(ends) if len(ends)==2 else 0
    crosses=(interval.low is None or interval.low<=criterion.high) and (interval.high is None or interval.high>=criterion.low)
    return lower,100 if crosses else max(ends,default=100)


def bind_reference_scales(profile, *, ranking_scales=None):
    """Bind utility scales from the profile and the client's references."""
    if profile.metadata.get('scales_bound_to_search'):return profile
    metadata=dict(profile.metadata)
    if ranking_scales and ranking_scales.get('market_context'):
        from .market_calibration import apply_market_references
        from .ranking_scales import validate_ranking_scales
        from .business_model import concept_digest
        validate_ranking_scales(ranking_scales)
        if ranking_scales['concept_sha256']!=concept_digest(profile.concept):raise ValueError('Market references belong to another concept')
        if not metadata.get('market_context') or not any(c.metric=='market_fit_index' for c in profile.criteria):
            raise ValueError('Market references require the integrated market model')
        config,provenance=apply_market_references(metadata['market_context'],ranking_scales['market_context'])
        metadata['market_context']=validate_market_context(config,profile.concept)
        metadata['market_reference_provenance']=provenance
    elif metadata.get('market_context'):
        metadata.setdefault('market_reference_provenance',default_market_reference_provenance())
    profile=replace(profile,metadata=metadata)
    criteria=[];reference={}
    for criterion in profile.criteria:
        scale=reference_for(criterion,profile,ranking_scales)
        reference[criterion.metric]=scale
        criteria.append(replace(criterion,low=scale['low'],high=scale['high']) if scale['usable'] else criterion)
    return replace(profile,criteria=tuple(criteria),metadata={**profile.metadata,'scales_bound_to_search':True,
        'reference_scales':reference,'scale_policy':'candidate_independent_v1'})
