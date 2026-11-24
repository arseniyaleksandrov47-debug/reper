"""Interval scenarios for reachable demand, customer choice and premises costs.

No fitted coefficients or measurements are supplied by this module. Inputs must
carry a source and distinguish observations, estimates and assumptions. Ratings
and company accounts deliberately have no role in the calculation.
"""
import hashlib
import copy
import json
import math
from dataclasses import dataclass

from .accessibility import PedestrianNetwork


@dataclass(frozen=True)
class Interval:
    low: float = 0.0
    high: float = None  # None is an unknown/unbounded endpoint, JSON-safe.

    @property
    def midpoint(self):
        return self.low / 2 + self.high / 2 if self.low is not None and self.high is not None else None

    def as_dict(self):
        return {'low': self.low, 'high': self.high}


def concept_digest(concept):
    return hashlib.sha256(concept.encode('utf-8')).hexdigest()


def business_evidence(prop):
    raw = prop.get('raw_attributes', prop.get('raw_json', {}))
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return {}
    data = raw.get('business_evidence', {}) if isinstance(raw, dict) else {}
    return data if isinstance(data, dict) else {}


def validate_business_inputs(data):
    if not isinstance(data, dict) or set(data) - {'properties', 'pedestrian_network', 'ranking_scales'} or not isinstance(data.get('properties'), dict):
        raise ValueError('business_data: нужны properties и необязательный pedestrian_network')
    allowed = {'market_model', 'premises_cost', 'pedestrian_network', 'mandatory_requirements', 'site_audience_daily', 'walk_time_minutes', 'delivery_time_minutes','access_routes'}
    for key, item in data['properties'].items():
        if not isinstance(key, str) or ':' not in key or not isinstance(item, dict) or set(item) - allowed:
            raise ValueError('business_data.properties: ключ source:external_id и только бизнес-данные')
        if 'pedestrian_network' in item:
            PedestrianNetwork(item['pedestrian_network'])
        if 'access_routes' in item:
            routes=item['access_routes']
            if not isinstance(routes,dict) or set(routes)-{'walk','delivery'} or any(
                not isinstance(r,dict) or not {'origin','entry'}.issubset(r) or set(r)-{'origin','entry','scope'} or
                r.get('scope','unit_entrance') not in ('unit_entrance','building_entrance') for r in routes.values()):
                raise ValueError('access_routes: нужны origin/entry для walk или delivery')
    if 'pedestrian_network' in data:
        PedestrianNetwork(data['pedestrian_network'])
    if 'ranking_scales' in data:
        from .ranking_scales import validate_ranking_scales
        validate_ranking_scales(data['ranking_scales'])


def attach_business_inputs(properties, data):
    """Overlay supplied evidence in memory; do not modify listings or the DB."""
    validate_business_inputs(data)
    result = []
    shared_network = copy.deepcopy(data.get('pedestrian_network'))
    for original in properties:
        prop = copy.deepcopy(original)
        key = f"{prop.get('source')}:{prop.get('external_id', prop.get('id'))}"
        supplied = data['properties'].get(key)
        if supplied is not None:
            raw = prop.get('raw_attributes', prop.get('raw_json', {}))
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError as exc:
                    raise ValueError('Невозможно дополнить повреждённые raw_json объявления') from exc
            if not isinstance(raw, dict):
                raise ValueError('raw_attributes объявления должен быть объектом')
            evidence = copy.deepcopy(business_evidence(prop))
            evidence.update(copy.deepcopy(supplied))
            if 'pedestrian_network' in data and 'pedestrian_network' not in supplied:
                evidence['pedestrian_network'] = shared_network
            raw['business_evidence'] = evidence
            prop['raw_attributes'] = raw
        result.append(prop)
    return result


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _fact(data, name, *, stress=0, maximum=None, positive=False, signed=False):
    fact = data.get(name) if isinstance(data, dict) else None
    if (not isinstance(fact, dict) or not isinstance(fact.get('source'), str) or not fact['source'].strip()
            or fact.get('kind') not in ('observed', 'estimate', 'assumption')):
        raise ValueError(f'{name}: нужен источник и kind=observed/estimate/assumption')
    if 'value' in fact:
        if 'low' in fact or 'high' in fact:
            raise ValueError(f'{name}: задайте value либо low/high')
        low = high = fact['value']
    else:
        low, high = fact.get('low'), fact.get('high')
    if (not _finite(low) or not _finite(high) or low > high or (not signed and low < 0)
            or (positive and low <= 0) or (maximum is not None and high > maximum)):
        raise ValueError(f'{name}: некорректный диапазон')
    # Stress the primitive inputs, not just the already aggregated score/Q.
    low, high = low - abs(low) * stress, high + abs(high) * stress
    if not signed:
        low = max(0.0, low)
    if maximum is not None:
        high = min(maximum, high)
    if positive and low <= 0:
        raise ValueError(f'{name}: нижняя граница должна оставаться положительной')
    return _checked(Interval(float(low), float(high)))


def _checked(value):
    if any(v is not None and not _finite(v) for v in (value.low, value.high)):
        raise ValueError('Диапазон выходит за пределы числового расчёта')
    return value


def _add(*values):
    return _checked(Interval(sum(x.low for x in values), None if any(x.high is None for x in values) else sum(x.high for x in values)))


def _multiply(*values):
    # All uses here are nonnegative. Signed margin is handled separately.
    low = math.prod(x.low for x in values)
    if any(x.high == 0 for x in values):
        return Interval(0.0, 0.0)
    return _checked(Interval(low, None if any(x.high is None for x in values) else math.prod(x.high for x in values)))


def premises_cost(prop, evidence, stress=0):
    data = evidence.get('premises_cost', {})
    warnings = [];missing=[]
    rent = prop.get('price_month')
    if not _finite(rent) or rent <= 0:
        rent_range = Interval()
        warnings.append('Аренда неизвестна')
        missing.append(dict(field='rent_month',unit='rub/month',action='Уточните месячную аренду и источник ставки'))
    else:
        try:
            rent_range = _checked(Interval(rent * (1 - stress), None if rent_is_lower_bound(prop) else rent * (1 + stress)))
        except ValueError as exc:
            rent_range = Interval()
            warnings.append(str(exc))
    parts = {'rent_month': rent_range}
    try:
        parts['operating_month'] = _fact(data, 'operating_month', stress=stress)
    except ValueError as exc:
        parts['operating_month'] = Interval()
        warnings.append(str(exc))
        missing.append(dict(field='operating_month',unit='rub/month',action='Укажите все дополнительные ежемесячные расходы на помещение с источником и диапазоном'))
    # Preparation belongs to the tenant unless they supplied it explicitly.
    # Omitting this component is a declared scope, not a claim of free work.
    include_fitout=any(k in data for k in ('fitout_total','amortization_months'))
    if not include_fitout:
        try:total=_add(*parts.values())
        except ValueError as exc:
            total=Interval();warnings.append(str(exc))
        warnings.append('Ремонт и обустройство оценивает арендатор; затраты на подготовку не включены в этот расчёт')
        return total,dict(parts={k:v.as_dict() for k,v in parts.items()},total_month=total.as_dict(),
            warnings=warnings,missing_inputs=missing,cost_scope='rent_and_operating',
            preparation_scope='tenant_managed',excluded_components=['fitout_total','amortization_months'])
    fitout=months=None
    try:
        fitout = _fact(data, 'fitout_total', stress=stress)
    except ValueError as exc:
        warnings.append(str(exc))
        missing.append(dict(field='fitout_total',unit='rub',action='Укажите затраты на подготовку помещения; ноль допустим только при явном подтверждении'))
    if fitout is not None and fitout.high==0:
        parts['fitout_month']=Interval(0,0)
    else:
        try:months = _fact(data, 'amortization_months', stress=stress, positive=True)
        except ValueError as exc:
            warnings.append(str(exc))
            missing.append(dict(field='amortization_months',unit='months',action='Задайте срок списания затрат на подготовку как явное допущение клиента'))
        parts['fitout_month']=Interval()
        if fitout is not None and months is not None:
            try:parts['fitout_month']=_checked(Interval(fitout.low/months.high,fitout.high/months.low))
            except (ValueError,OverflowError,ZeroDivisionError) as exc:
                warnings.append(str(exc))
                missing.append(dict(field='fitout_month',unit='rub/month',action='Проверьте диапазоны затрат и срока: приведённая сумма выходит за пределы расчёта'))
    try:
        total = _add(*parts.values())
    except ValueError as exc:
        total = Interval()
        warnings.append(str(exc))
    return total, {'parts': {k: v.as_dict() for k, v in parts.items()}, 'total_month': total.as_dict(), 'warnings': warnings,'missing_inputs':missing,
                   'cost_scope':'rent_operating_and_supplied_preparation','preparation_scope':'client_supplied','excluded_components':[]}


def rent_is_lower_bound(prop):
    from .property_sources import raw_attributes
    start=raw_attributes(prop).get('auction_start_year')
    return prop.get('rent_price_lower_bound') is True or type(start) in (int,float) and math.isfinite(start) and start>0


def _route(cohort, destination, network, entry, stress):
    # Direct route measurements must explicitly refer to a checked entrance.
    direct = cohort.get('routes', {}).get(destination) if isinstance(cohort.get('routes', {}), dict) else None
    if direct is not None:
        if not isinstance(direct, dict) or direct.get('entrance_verified') is not True:
            raise ValueError('Время маршрута требует проверенного входа')
        return _fact({'minutes': direct}, 'minutes', stress=stress), 'reachable'
    if network:
        result = network.travel(cohort.get('origin'), entry)
        if result.status == 'reachable':
            if result.minutes_upper is None:return Interval(),'unknown'
            return Interval(result.minutes_lower * (1 - stress), result.minutes_upper * (1 + stress)), result.status
        return Interval(), result.status
    return Interval(), 'unknown'


def _choice_weight(attractiveness, duration, status, decay, horizon):
    if status == 'unreachable' or (duration.low is not None and duration.low > horizon.high):
        return Interval(0.0, 0.0)
    if status == 'unknown':
        return Interval(0.0, attractiveness.high)
    low = 0.0 if duration.high > horizon.low else attractiveness.low * math.exp(-decay.high * duration.high)
    return Interval(low, attractiveness.high * math.exp(-decay.low * duration.low))


def _share(target, alternatives):
    def ratio(numerator, weights):
        if numerator == 0:
            return 0.0
        scale = max(numerator, *weights)
        scaled = numerator / scale
        return scaled / (scaled + sum(w / scale for w in weights))
    lower = 0.0 if any(a.high is None for a in alternatives) else ratio(target.low, [a.high for a in alternatives])
    upper = ratio(target.high, [a.low for a in alternatives])
    return Interval(lower, upper)


def _known_competitor_ids(report):
    return {str(c.card.org_id) for c in [*getattr(report, 'direct_competitors', []), *getattr(report, 'indirect_competitors', [])]}


def _accessible_audience(model, network, stress):
    """Keep the audience stage available when choice coefficients are absent."""
    if model.get('audience_partition') != 'exclusive' or not isinstance(model.get('partition_source'), str) or not model['partition_source'].strip():
        raise ValueError('Нужны непересекающиеся группы аудитории с источником')
    cohorts=model.get('cohorts')
    if not isinstance(cohorts,list) or not 1<=len(cohorts)<=64:
        raise ValueError('Некорректные группы аудитории')
    horizon=_fact(model,'max_walk_minutes',stress=stress,positive=True)
    identifiers=set();total=Interval(0,0);details=[]
    for cohort in cohorts:
        if not isinstance(cohort,dict) or not isinstance(cohort.get('id'),str) or not cohort['id'] or cohort['id'] in identifiers:
            raise ValueError('Нужны уникальные ID групп аудитории')
        identifiers.add(cohort['id'])
        people=_fact(cohort,'people',stress=stress)
        hours=_fact(cohort,'open_hours_overlap',stress=stress,maximum=1)
        duration,status=_route(cohort,'target',network,model.get('target_entry'),stress)
        access=Interval(0 if status!='reachable' or duration.high>horizon.low else 1,
                        0 if status=='unreachable' or duration.low>horizon.high else 1)
        audience=_multiply(people,hours,access);total=_add(total,audience)
        details.append(dict(id=cohort['id'],audience=audience.as_dict(),route_status=status))
    return total,details


def _demand(model, network, report, stress):
    if model.get('audience_partition') != 'exclusive' or not isinstance(model.get('partition_source'), str) or not model['partition_source'].strip():
        raise ValueError('Группы аудитории должны образовывать непересекающееся разбиение с источником')
    cohorts = model.get('cohorts')
    competitors = model.get('competitors')
    if not isinstance(cohorts, list) or not 1 <= len(cohorts) <= 64 or not isinstance(competitors, list) or len(competitors) > 128:
        raise ValueError('Некорректные группы аудитории/альтернативы')
    def ids(items):
        result = [item.get('id') for item in items if isinstance(item, dict)]
        if len(result) != len(items) or any(not isinstance(x, str) or not x or x == 'target' for x in result) or len(set(result)) != len(result):
            raise ValueError('Идентификаторы групп/конкурентов должны быть уникальными строками')
        return set(result)
    ids(cohorts)
    competitor_ids = ids(competitors)
    decay = _fact(model, 'distance_decay_per_minute', stress=stress)
    target_attractiveness = _fact(model, 'target_attractiveness', stress=stress, positive=True)
    horizon = _fact(model, 'max_walk_minutes', stress=stress, positive=True)
    alternatives = [(c['id'], _fact(c, 'attractiveness', stress=stress, positive=True), c.get('entry')) for c in competitors]
    inventory = model.get('competitor_inventory', {})
    inventory_complete = (isinstance(inventory, dict) and inventory.get('coverage_complete') is True
                          and isinstance(inventory.get('source'), str) and bool(inventory['source'].strip()))
    missing_ids = _known_competitor_ids(report) - competitor_ids
    unknown_rivals = bool(missing_ids) or not inventory_complete or getattr(report, 'data_status', 'unavailable') != 'ok'
    warnings = ['Параметры выбора и конверсии требуют калибровки; это сценарий, не прогноз продаж',
                'Непересечение групп аудитории заявлено поставщиком данных; оно не устанавливается по сумме жителей и прохожих']
    if unknown_rivals:
        warnings.append('Неполные альтернативы: неизвестные конкуренты расширяют нижнюю границу доли до нуля')
    if network and not network.complete:
        warnings.append('Пешеходный граф неполный: отсутствие пути не доказывает недоступность')
    audience_total, orders_total = Interval(0, 0), Interval(0, 0)
    details = []
    for cohort in cohorts:
        people = _fact(cohort, 'people', stress=stress)
        hours = _fact(cohort, 'open_hours_overlap', stress=stress, maximum=1)
        rate = _fact(cohort, 'purchase_opportunities_per_person_day', stress=stress)
        conversion = _fact(cohort, 'conversion_after_choice', stress=stress, maximum=1)
        duration, status = _route(cohort, 'target', network, model.get('target_entry'), stress)
        target = _choice_weight(target_attractiveness, duration, status, decay, horizon)
        outside = cohort.get('outside_options')
        if not isinstance(outside, dict) or set(outside) != {'other_area', 'online', 'no_purchase'}:
            raise ValueError('Нужны внешние альтернативы: другой район, онлайн и отказ от покупки')
        weights = [_fact(outside, name, stress=stress) for name in ('other_area', 'online', 'no_purchase')]
        if sum(w.low for w in weights) <= 0:
            raise ValueError('Внешняя альтернатива должна иметь положительную нижнюю границу веса')
        route_details = {'target': {'status': status, 'minutes': duration.as_dict()}}
        for identifier, attractiveness, entry in alternatives:
            competitor_duration, competitor_status = _route(cohort, identifier, network, entry, stress)
            weights.append(_choice_weight(attractiveness, competitor_duration, competitor_status, decay, horizon))
            route_details[identifier] = {'status': competitor_status, 'minutes': competitor_duration.as_dict()}
        if unknown_rivals:
            weights.append(Interval())
        share = _share(target, weights)
        access = Interval(0 if status != 'reachable' or duration.high > horizon.low else 1,
                          0 if status == 'unreachable' or duration.low > horizon.high else 1)
        audience = _multiply(people, hours, access)
        # Accessibility already enters target choice. Multiplying orders by a
        # second continuous distance/traffic bonus would count it twice.
        orders = _multiply(people, hours, rate, conversion, share)
        audience_total, orders_total = _add(audience_total, audience), _add(orders_total, orders)
        detail = {'id': cohort['id'], 'accessible_audience': audience.as_dict(), 'choice_share': share.as_dict(),
                  'orders_daily': orders.as_dict(), 'routes': route_details}
        if network and cohort.get('metro_exit'):
            detail['metro_detour'] = network.detour(cohort.get('origin'), model.get('target_entry'), cohort['metro_exit'])
        if status == 'unknown':
            warnings.append(f"{cohort['id']}: достижимость входа неизвестна; расстояние по прямой не подставлено")
        details.append(detail)
    return audience_total, orders_total, {'cohorts': details, 'missing_competitor_ids': sorted(missing_ids),
                                         'inventory_complete': not unknown_rivals, 'warnings': warnings}


def analyze_business_model(prop, report=None, concept='', *, stress=0, network_cache=None):
    """Pure, network-free evaluation of sourced facts supplied by a provider.

    Missing observations remain missing. The AI selects priorities, but cannot
    populate this object or invent customer counts/coefficient values.
    """
    if not _finite(stress) or not 0 <= stress < 1:
        raise ValueError('Stress fraction must be finite and in [0, 1)')
    evidence = business_evidence(prop)
    cost, cost_details = premises_cost(prop, evidence, stress)
    metrics = {'premises_cost_month': cost}
    warnings = list(cost_details['warnings'])
    details = {'cost': cost_details, 'audience': None, 'demand': None, 'net_contribution_month': None,
               'inputs': _input_records({k: v for k, v in evidence.items() if k != 'pedestrian_network'})}
    model = evidence.get('market_model')
    if model is None:
        warnings.append('Нет исходных данных сценария спроса и выбора покупателя')
    else:
        try:
            if not isinstance(model, dict) or model.get('version') != 1 or not concept or model.get('concept_sha256') != concept_digest(concept):
                raise ValueError('Сценарий спроса должен быть версии 1 и привязан к текущей концепции')
            network_data = evidence.get('pedestrian_network')
            network = None
            if network_data is not None:
                if network_cache is not None and id(network_data) in network_cache:
                    _,network = network_cache[id(network_data)]
                else:
                    network = PedestrianNetwork(network_data)
                    # Retain decoded JSON too: a freed input dictionary's ID
                    # could otherwise be reused for a different network.
                    if network_cache is not None:network_cache[id(network_data)] = (network_data,network)
            audience,audience_details=_accessible_audience(model,network,stress)
            metrics['accessible_audience_daily']=audience
            details['audience']=audience_details
            audience, orders, demand_details = _demand(model, network, report, stress)
            metrics.update(accessible_audience_daily=audience, orders_daily=orders)
            details['demand'] = demand_details
            warnings.extend(demand_details['warnings'])
            try:
                margin = _fact(model, 'margin_per_order', stress=stress, signed=True)
                days = _fact(model, 'operating_days_month', stress=stress, positive=True, maximum=31)
                fixed = _fact(model, 'other_fixed_cost_month', stress=stress)
                monthly_orders = _multiply(orders, days)
                products = [q * m for q in (monthly_orders.low, monthly_orders.high) for m in (margin.low, margin.high)]
                total_cost = _add(cost, fixed)
                net = _checked(Interval(None if total_cost.high is None else min(products) - total_cost.high, max(products) - total_cost.low))
                metrics['net_contribution_month'] = net
                details['net_contribution_month'] = net.as_dict()
            except ValueError as exc:
                warnings.append(f'Экономический результат не определён: {exc}')
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            warnings.append(f'Спрос не рассчитан: {exc}')
    details['metrics'] = {k: v.as_dict() for k, v in metrics.items()}
    details['warnings'] = list(dict.fromkeys(warnings))
    details['calibrated'] = False
    return metrics, details


def _input_records(data, path=''):
    records = []
    if isinstance(data, dict):
        if 'kind' in data and 'source' in data:
            records.append({'path': path, **{k: data[k] for k in ('low', 'high', 'value', 'kind', 'source') if k in data}})
        else:
            for key, item in data.items():
                if key != 'pedestrian_network':records.extend(_input_records(item, f'{path}.{key}' if path else key))
    elif isinstance(data, list):
        for i, item in enumerate(data):records.extend(_input_records(item, f'{path}[{i}]'))
    return records


def assess_mandatory_requirements(prop, concept):
    """Check explicitly supplied client obligations before rating, never bonus them.

    This adapter does not infer new obligations. Unsupported/unchecked evidence
    blocks recommendation until verification; an assumption cannot prove a must.
    """
    evidence = business_evidence(prop)
    requirements = evidence.get('mandatory_requirements', [])
    checks = []
    try:
        if not isinstance(requirements, list) or len(requirements) > 32:
            raise ValueError('Некорректный перечень обязательных требований')
        identifiers = set()
        network = None
        for req in requirements:
            if (not isinstance(req, dict) or not isinstance(req.get('id'), str) or not req['id']
                    or req['id'] in identifiers or not isinstance(req.get('concept_quote'), str)
                    or not req['concept_quote'].strip() or req['concept_quote'] not in concept):
                raise ValueError('Обязательное требование должно иметь уникальный ID и точную цитату клиента')
            identifiers.add(req['id'])
            kind = req.get('type')
            outcome = 'unknown'
            if kind == 'route':
                limit = _fact(req, 'max_minutes', positive=True)
                if network is None and evidence.get('pedestrian_network') is not None:
                    network = PedestrianNetwork(evidence['pedestrian_network'])
                duration, status = _route(req, 'target', network, req.get('entry'), 0)
                direct = req.get('routes', {}).get('target')
                if direct is not None and (direct.get('kind') != 'observed' or direct.get('verified') is not True):
                    status = 'unknown'
                if status == 'unreachable' or (status == 'reachable' and duration.low > limit.high):outcome = 'not_met'
                elif status == 'reachable' and duration.high <= limit.low:outcome = 'met'
            elif kind in ('minimum', 'maximum', 'equals'):
                actual = req.get('actual', {})
                verified = (isinstance(actual, dict) and actual.get('verified') is True and actual.get('kind') == 'observed'
                            and isinstance(actual.get('source'), str) and bool(actual['source'].strip()))
                if kind == 'equals':
                    if type(req.get('expected')) is not bool:raise ValueError('equals требует логическое expected')
                    if verified and type(actual.get('value')) is bool:outcome = 'met' if actual['value'] == req['expected'] else 'not_met'
                else:
                    limit = _fact(req, 'limit')
                    if verified:
                        value = _fact(req, 'actual')
                        if kind == 'minimum':
                            if value.low >= limit.high:outcome = 'met'
                            elif value.high < limit.low:outcome = 'not_met'
                        else:
                            if value.high <= limit.low:outcome = 'met'
                            elif value.low > limit.high:outcome = 'not_met'
            else:
                raise ValueError('Неподдерживаемое обязательное требование')
            checks.append({'id': req['id'], 'concept_quote': req['concept_quote'], 'result': outcome})
        status = ('rejected' if any(c['result'] == 'not_met' for c in checks) else
                  'needs_verification' if any(c['result'] == 'unknown' for c in checks) else 'eligible')
        return {'status': status, 'checks': checks}
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        return {'status': 'needs_verification', 'checks': checks, 'error': str(exc)}
