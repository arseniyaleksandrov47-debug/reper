"""Customer next steps, distinct from data completeness and source checks."""
import math

from .ranking_sensitivity import candidate_id

BLOCKING_REASONS={'missing_scale','unsupported_requirement','modeling_conflict','model_sensitivity_unavailable'}


def blocking_inputs(items):
    return [i for i in items if i.get('reason') in BLOCKING_REASONS]


def ranking_cost_scope(profile):
    metrics={c.metric for c in profile.criteria} if profile else set()
    if metrics & {'premises_cost_month','net_contribution_month'}:return 'total_monthly'
    return 'rent_only' if metrics & {'rent_month','rent_sqm_year'} else 'none'


def describe_next_step(report,pool):
    profile=report.ranking_profile
    base=dict(status='no_matches',primary_candidate_id=None,candidate_ids=[],robust_within_tested_scenarios=False,
        scope='collected_and_evaluated_properties',data_complete=report.data_complete,
        comparison_complete=report.comparison_complete,confirmation_status=report.confirmation_status,
        message=report.confirmation_message,checks=[],basis=[])
    reference_gaps=[g for g in report.missing_inputs if g.get('reason')=='missing_scale']
    if reference_gaps:
        base.update(status='provide_references',message='Для сравнения нужны ориентиры клиента; анализ помещений ещё не запускался.',
                    checks=[dict(kind='client_reference',message=g['action'],metric=g['metric']) for g in reference_gaps])
        return base
    if profile is not None and profile.status!='ready':
        technical=profile.analysis_status not in ('valid','needs_context','missing_concept')
        base.update(status='retry_profile' if technical else 'clarify_request',
            message='Технический разбор запроса не завершён; повторите его с теми же условиями.' if technical else profile.summary,
            checks=[dict(kind='request',message=q) for q in profile.questions])
        return base
    if not pool:
        if report.floor_verification_candidates:
            base.update(status='verify_conditions',message='Найдены объявления с подходящими числовыми условиями; для сравнения нужно подтвердить обязательные свойства помещения.')
        elif report.analysis_errors:base.update(status='retry_analysis',message='Анализ найденных помещений не завершён; повторите недоступные этапы.')
        return base
    leader=pool[0];others=pool[1:];identifiers=[candidate_id(c) for c in pool]
    has_priority=not others or leader.unrounded_total_score>others[0].unrounded_total_score+1e-8
    base['candidate_ids']=[candidate_id(c) for c in (report.comparable_candidates or report.ranked_candidates)]
    if has_priority:base['primary_candidate_id']=identifiers[0]
    unresolved=[g for g in blocking_inputs(report.missing_inputs) if g.get('reason') in ('unsupported_requirement','modeling_conflict')]
    interval_overlap=any(c.score_upper_bound is None or leader.unrounded_total_score<=c.score_upper_bound+1e-8 for c in others)
    sensitive=bool(report.input_sensitive_challenger_ids or report.weight_sensitive_challenger_ids or report.model_sensitive_challenger_ids)
    mandatory=bool(leader.floor_assessment and leader.floor_assessment.status!='eligible')
    from .business_model import rent_is_lower_bound
    mandatory=mandatory or report.client_max_price_month is not None and rent_is_lower_bound(leader.property_data)
    checks=leader.confirmation.get('checks',{})
    mandatory=mandatory or any(checks.get(k,{}).get('result')=='unknown' for k in ('access','concept_use','client_requirements'))
    bounded_rivals=bool(report.deferred_candidate_bounds) and all(b['dominated'] for b in report.deferred_candidate_bounds)
    robust=bool((others or bounded_rivals) and report.comparison_complete and not blocking_inputs(report.missing_inputs) and
                not interval_overlap and not sensitive and has_priority and not mandatory)
    base['robust_within_tested_scenarios']=robust
    if unresolved:
        base.update(status='clarify_requirements',primary_candidate_id=None,
            message='Для выбора нужно уточнить учёт перечисленных условий клиента; известные показатели уже сравнены.')
    elif mandatory:
        base.update(status='conditional_shortlist',message='Есть предварительное сравнение. Обязательные условия первого варианта требуют подтверждения перед решением об аренде.')
    elif not report.comparison_complete:
        base.update(status='partial_shortlist',message='Первый вариант выбран среди завершённых оценок; часть подходящих объявлений или условий ещё не сравнивалась.')
    elif robust:
        base.update(status='visit_priority',message='В просмотренной выборке вариант сохраняет преимущество в выполненных проверках. Рекомендуется проверить его первым.')
    elif not others:
        base.update(status='single_option',message='В просмотренной выборке остался один вариант. Его можно проверить; сравнения с альтернативами пока нет.')
    elif has_priority:
        base.update(status='compare_options',message='Есть первый вариант по доступным данным и сопоставимые альтернативы. Неподтверждённые факты могут изменить порядок.')
    else:
        base.update(status='compare_options',message='У вариантов одинаковая нижняя оценка. Оснований назначить одному приоритет просмотра пока нет.')
    for metric,part in leader.scoring_components.items():
        if part.get('scale_usable') and part.get('value') is not None:
            advantage=part['contribution_lower']-max((c.scoring_components[metric]['contribution_lower'] for c in others),default=0)
            base['basis'].append(dict(metric=metric,value_lower=part.get('value_lower'),value_upper=part.get('value_upper'),
                contribution_lower=part['contribution_lower'],contribution_upper=part['contribution_upper'],
                contribution_advantage=advantage))
    # Keep every raw gap in missing_inputs, but present a short plan for the
    # first comparison. A grouped block is not an estimate of each gap's effect.
    properties=identifiers[:1] if has_priority else identifiers[:3]
    facts=[]
    for c in pool:
        if candidate_id(c) not in properties:continue
        reasons=c.confirmation.get('reasons',[])
        if c.floor_assessment and c.floor_assessment.status!='eligible':
            facts.extend(c.floor_assessment.missing_facts or [c.floor_assessment.reason])
        facts.extend(r['message'] for r in reasons if r.get('code') in ('admission_unknown','access','concept_use','client_requirements','availability','auction_price','source_check_unavailable','verification_unavailable','source_expired_during_check'))
    if facts:base['checks'].append(dict(kind='published_conditions',property_ids=list(properties),
        message='Подтвердить условия выбранных помещений',actions=list(dict.fromkeys(facts))))
    for gap in blocking_inputs(report.missing_inputs):
        base['checks'].append(dict(kind='request',message=gap['action']))
    grouped={}
    for gap in report.missing_inputs:
        if gap.get('reason') in BLOCKING_REASONS or gap.get('property_id') not in properties:continue
        metric=gap.get('metric')
        part=leader.scoring_components.get(metric,{})
        width=part.get('contribution_upper',0)-part.get('contribution_lower',0)
        if not math.isfinite(width) or width<=1e-8:continue
        block=grouped.setdefault(metric,dict(kind='score_inputs',metric=metric,uncertainty_points=width,
            message='Уточнить исходные данные окружения и доступности' if metric=='market_fit_index' else 'Уточнить исходные данные расходов',
            actions=[],missing_count=0,source_actions={},organization_ids=[]))
        block['missing_count']+=1
        source=gap.get('source') or gap.get('field') or gap.get('reason')
        if source.startswith('organization:'):
            identity=source.split(':')[1]
            if identity not in block['organization_ids']:block['organization_ids'].append(identity)
            source='organization_details'
            action='Уточнить недостающие сведения о конкурентах и точное сопоставление их юрлиц; подробности сохранены в данных оценки'
        else:action=gap['action']
        block['source_actions'].setdefault(source,action)
    config=profile.metadata.get('market_context',{}) if profile else {}
    priorities={key:item['weight'] for key,item in config.get('audience',{}).items()}
    priorities['walk_time_minutes']=priorities['delivery_time_minutes']=config.get('access',{}).get('weight',0)
    priorities['organization_inventory']=priorities['organization_classification']=1
    priorities['organization_details']=sum(config.get('strength',{}).get(k,{}).get('weight',0) for k in ('company_revenue','company_profit','reputation'))
    for block in grouped.values():
        block['actions']=[action for _,action in sorted(block.pop('source_actions').items(),key=lambda pair:-priorities.get(pair[0],0))]
    base['checks'].extend(sorted(grouped.values(),key=lambda g:-g['uncertainty_points']))
    return base
