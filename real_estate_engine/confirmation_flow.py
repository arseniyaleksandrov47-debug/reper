"""Confirmation labels and executable continuation, separate from score math."""
import asyncio
import copy
import inspect
import math
import time
import hashlib
import json
from contextvars import ContextVar
from functools import wraps
from types import SimpleNamespace

from .ai_contract import AI_DEADLINE
from .premises_requirements import physical_context_quotes

_PREFETCH=ContextVar('premises_confirmation_prefetch',default=None)


class ConfirmationPrefetch:
    def __init__(self,verifier,parameters):
        self.verifier=verifier;self.parameters=parameters;self.jobs={};self.hits=0;self.invalidated=0

    @staticmethod
    def signature(candidate):
        from .property_sources import raw_attributes
        prop=candidate.property_data
        fields={k:prop.get(k) for k in ('id','source','external_id','title','address','lat','lng','description','url','area_sqm','price_month','floor')}
        fields['source_payload']=raw_attributes(prop).get('source_payload')
        fields['photos_json']=prop.get('photos_json')
        fields['photos']=prop.get('photos')
        fields['cost']=candidate.business_model_diagnostics.get('cost')
        return hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()

    async def run(self,candidate,parameters):
        seconds=self.parameters['confirmation_timeout_s']
        deadline=min(AI_DEADLINE.get() or float('inf'),time.monotonic()+seconds)
        seconds=max(0,deadline-time.monotonic())
        token=AI_DEADLINE.set(deadline)
        try:
            return await asyncio.wait_for(self.verifier.verify(candidate,
                self.parameters.get('business_concept') or self.parameters.get('business_format'),parameters),seconds)
        finally:AI_DEADLINE.reset(token)

    def start(self,properties,max_candidates,constraints,profile=None):
        if (self.verifier is None or len(properties)>max_candidates or
                not (self.parameters.get('business_concept') or self.parameters.get('business_format'))):return
        from .business_model import premises_cost,business_evidence
        from .decision_policy import ranking_cost_scope
        from .ranking_policy import MODEL_METRICS
        model_cost=profile is None or any(c.metric in MODEL_METRICS for c in profile.criteria)
        parameters={**self.parameters,'confirmation_constraints':constraints,
                    'physical_context_quotes':physical_context_quotes(profile),'cost_scope':ranking_cost_scope(profile)}
        for prop in properties:
            try:
                candidate=SimpleNamespace(property_data=copy.deepcopy(prop),business_model_diagnostics=(
                    {'cost':premises_cost(prop,business_evidence(prop))[1]} if model_cost else {}))
                signature=self.signature(candidate)
            except (ValueError,TypeError):continue
            if property_key(prop) not in self.jobs:
                self.jobs[property_key(prop)]=(signature,asyncio.create_task(self.run(candidate,parameters)))

    async def obtain(self,candidate,parameters):
        entry=self.jobs.get(property_key(candidate.property_data))
        matches=entry and entry[0]==self.signature(candidate)
        if entry and not matches:self.invalidated+=1
        if matches:
            try:
                outcome=await entry[1]
                if isinstance(outcome,dict) and all(type(s.get('fetched_at')) in (int,float) and
                        0<=time.time()-s['fetched_at']<=300 for s in outcome.get('sources',[])):
                    self.hits+=1;return outcome
            except Exception:
                # An early timeout has already consumed its allotted budget;
                # don't repeat it in the same search and extend the wait.
                self.hits+=1
                return pending_confirmation([dict(code='verification_unavailable',message='Проверка источников не завершена; требуется подтверждение условий')])
            self.invalidated+=1
        return await self.run(candidate,parameters)

    async def close(self):
        tasks=[task for _,task in self.jobs.values()]
        for task in tasks:
            if not task.done():task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)


def start_confirmation_prefetch(properties,max_candidates,constraints,profile=None):
    current=_PREFETCH.get()
    if current is not None:current.start(properties,max_candidates,constraints,profile)


def property_key(prop):
    if prop.get('source') and prop.get('external_id') is not None:
        return str(prop['source'])+':'+str(prop['external_id'])
    return 'id:'+str(prop.get('id'))


def property_keys(prop):
    keys=[property_key(prop)]
    if prop.get('id') is not None:keys.append('id:'+str(prop['id']))
    for variant in prop.get('listing_variants',[]):
        keys.append(property_key(variant))
        if variant.get('id') is not None:keys.append('id:'+str(variant['id']))
    return list(dict.fromkeys(keys))


def pending_confirmation(reasons,**extra):
    return dict(status='requires_confirmation',label='Требует подтверждения',
                reasons=list(reasons),scope='published_conditions_for_client_concept',**extra)


def local_confirmation(candidate):
    reasons=[dict(code='source_check_unavailable',message='Проверка публикации и ограничений по источникам не выполнена')]
    cost=candidate.business_model_diagnostics.get('cost',{}) if candidate.business_model_diagnostics.get('ranking_cost_scope','total_monthly')=='total_monthly' else {}
    for gap in cost.get('missing_inputs',[]):
        reasons.append(dict(code='cost:'+gap['field'],message=gap['action']))
    return pending_confirmation(reasons)


def group_verified_listings(candidates):
    """Group matching ads only after a fresh exact-unit floor check.

    Keep alternate URLs visible: matching geometry/terms are a possible same
    space, not proof that every identically priced room in a building is one.
    """
    from .property_sources import address_key
    from .floor_policy import parse_floor_values
    groups={};result=[]
    for candidate in candidates:
        prop=candidate.property_data;floors=set();areas=set();prices=set()
        for source in candidate.confirmation.get('sources',[]):
            stamp=source.get('fetched_at')
            if (source.get('identity_checked') and type(stamp) in (int,float)
                    and 0<=time.time()-stamp<=300):
                floors.update(parse_floor_values(source.get('floor')))
                for field,values in (('area_sqm',areas),('price_month',prices)):
                    value=source.get(field)
                    if type(value) in (int,float) and math.isfinite(value) and value>0:values.add(value)
        key=None
        if prop.get('dedup_hash') and len(floors)==1 and len(areas)<=1 and len(prices)<=1:
            key=(prop['dedup_hash'],address_key(prop.get('address','')),
                 next(iter(areas),prop.get('area_sqm')),next(iter(prices),prop.get('price_month')),tuple(floors))
        if key is None or key not in groups:
            if key is not None:groups[key]=candidate
            result.append(candidate)
        else:
            primary=groups[key]
            variants=primary.property_data.setdefault('listing_variants',[])
            for item in [prop,*prop.get('listing_variants',[])]:
                if property_key(item)==property_key(primary.property_data):continue
                variant={k:item.get(k) for k in ('source','external_id','id','url','address','area_sqm','price_month')}
                if variant not in variants:variants.append(variant)
            warning='Совпадающие объявления сгруппированы; альтернативные ссылки сохранены, идентичность помещения требует проверки'
            if warning not in primary.data_warnings:primary.data_warnings.append(warning)
    return result


def add_admission_reason(candidate):
    """Keep one visible explanation when the same client condition is pending."""
    assessment=candidate.floor_assessment
    if assessment is None or assessment.status!='needs_verification' or candidate.confirmation.get('status')=='not_suitable':return
    confirmation=candidate.confirmation
    confirmation['status']='requires_confirmation';confirmation['label']='Требует подтверждения'
    reasons=confirmation.setdefault('reasons',[])
    checks=confirmation.get('checks',{})
    pending_quotes={item['client_quote'] for item in checks.get('client_requirements',{}).get('items',[]) if item['result']=='unknown'}
    missing=list(assessment.missing_facts)
    if missing and set(missing)<=pending_quotes:
        reasons[:]=[r for r in reasons if r.get('code')!='admission_unknown']
    else:
        access=next((r for r in reasons if r.get('code')=='access'),None)
        if access is not None and missing:
            access['admission_details']=dict(reason=assessment.reason,missing_facts=missing)
            reasons[:]=[r for r in reasons if r.get('code')!='admission_unknown']
        elif not any(r.get('code')=='admission_unknown' for r in reasons):
            reasons.append(dict(code='admission_unknown',message=assessment.reason,missing_facts=missing))
    warning='Предварительный рейтинг по выбору клиента; обязательные условия допуска не подтверждены'
    if warning not in candidate.data_warnings:candidate.data_warnings.append(warning)
    candidate.verdict='Предварительный вариант для проверки; обязательные условия допуска не подтверждены'


async def refresh_confirmed_ranking(recommender,report,pool,batch):
    """Apply fresh exact-unit terms and rescore the already analyzed pool."""
    if not hasattr(report,'_evaluated_candidates') or report.ranking_profile is None:return
    from .listing_enrichment import fresh_sources,merge_unit_sources,numerical_violations
    from .floor_policy import FloorCandidateReview,floor_candidate_key
    from .business_model import assess_mandatory_requirements
    changed=False;reassess=[]
    for candidate in batch:
        confirmation=candidate.confirmation
        if confirmation.get('status')=='not_suitable':continue
        sources=fresh_sources(confirmation)
        prop=candidate.property_data
        auction_flipped=False
        if any(s.get('auction_start_only') for s in sources) and not prop.get('rent_price_lower_bound'):
            prop['rent_price_lower_bound']=True;changed=True;auction_flipped=True
        prop,updates=merge_unit_sources(prop,sources)
        if not updates and not auction_flipped:continue
        if updates:
            confirmation['ranking_updates']=updates
        candidate.property_data=prop;changed=True
        constraints=dict(min_area_sqm=report.client_min_area_sqm,max_area_sqm=report.client_max_area_sqm,
            max_price_month=report.client_max_price_month,max_price_sqm_year=report.client_max_price_sqm_year)
        violations=numerical_violations(prop,constraints) if updates else []
        if violations:
            confirmation.update(status='not_suitable',label='Не соответствует требованиям')
            confirmation.setdefault('reasons',[]).append(dict(code='current_filters',message='Актуальные условия выходят за ограничения клиента',violations=violations))
        elif (({'floor','description'} & set(updates)) or auction_flipped) and report.floor_profile is not None:
            reassess.append(candidate)
        fields=set(confirmation.get('changed_fields',[]))
        if fields and fields<=set(updates):
            confirmation['reasons']=[r for r in confirmation.get('reasons',[]) if r.get('code')!='listing_changed']
            checks=confirmation.get('checks',{})
            if confirmation.get('status')!='not_suitable' and not confirmation['reasons'] and len(checks)==4 and all(c['result'] in ('pass','not_applicable') for c in checks.values()):
                confirmation.update(status='source_conditions_checked',label='Условия проверены по источникам')
    if reassess:
        assessments=await recommender.floor_profile_resolver.assess_many([c.property_data for c in reassess],report.floor_profile)
        existing_verification={floor_candidate_key(r.property_data) for r in report.floor_verification_candidates}
        for candidate in reassess:
            assessment=assessments[floor_candidate_key(candidate.property_data)]
            mandatory=assess_mandatory_requirements(candidate.property_data,report.ranking_profile.concept)
            if mandatory['status']=='rejected':assessment.status='rejected';assessment.reason='Не выполнено обязательное требование клиента'
            elif mandatory['status']=='needs_verification' and assessment.status!='rejected':
                assessment.status='needs_verification';assessment.reason='Нужно проверить обязательное требование клиента'
                assessment.missing_facts.extend(c['concept_quote'] for c in mandatory['checks'] if c['result']=='unknown')
            from .business_model import rent_is_lower_bound
            if assessment.status!='rejected' and report.client_max_price_month is not None and rent_is_lower_bound(candidate.property_data):
                assessment.status='needs_verification';assessment.reason='Нужно подтвердить окончательную аренду в пределах бюджета'
            candidate.floor_assessment=assessment
            confirmation=candidate.confirmation
            confirmation['reasons']=[r for r in confirmation.get('reasons',[]) if r.get('code')!='admission_unknown']
            if assessment.status=='rejected':
                confirmation.update(status='not_suitable',label='Не соответствует требованиям')
                confirmation['reasons'].append(dict(code='admission_rejected',message=assessment.reason))
            elif assessment.status=='needs_verification':
                if not report.include_unverified:
                    key=floor_candidate_key(candidate.property_data)
                    if key not in existing_verification:
                        report.floor_verification_candidates.append(FloorCandidateReview(candidate.property_data,assessment))
                        existing_verification.add(key)
                    pool[:]=[c for c in pool if id(c)!=id(candidate)]
                    changed=True
                else:
                    add_admission_reason(candidate)
            elif assessment.status=='eligible':
                candidate.data_warnings=[w for w in candidate.data_warnings if w!='Предварительный рейтинг по выбору клиента; обязательные условия допуска не подтверждены']
                checks=confirmation.get('checks',{})
                if not confirmation['reasons'] and len(checks)==4 and all(c['result'] in ('pass','not_applicable') for c in checks.values()):
                    confirmation.update(status='source_conditions_checked',label='Условия проверены по источникам')
    if not changed:return
    if not pool:
        pool[:]=[];return
    assessments={floor_candidate_key(c.property_data):c.floor_assessment for c in pool}
    refreshed=recommender._score_and_rank_candidates([(c.property_data,c.competition_report) for c in pool],
        ranking_profile=report.ranking_profile,floor_profile=report.floor_profile,floor_assessments=assessments)
    originals={property_key(c.property_data):c for c in pool};ordered=[]
    for fresh in refreshed:
        original=originals[property_key(fresh.property_data)]
        warnings=original.data_warnings
        for key,value in vars(fresh).items():
            if key!='confirmation':setattr(original,key,value)
        original.data_warnings=list(dict.fromkeys([*original.data_warnings,*warnings]))
        ordered.append(original)
    pool[:]=ordered


def with_confirmation(function):
    """Finalize conditions and refresh scores when published terms change."""
    signature=inspect.signature(function)
    async def finalize(self,*args,**kwargs):
        bound=signature.bind(self,*args,**kwargs);bound.apply_defaults()
        parameters={k:v for k,v in bound.arguments.items() if k!='self'}
        report=await function(self,*args,**kwargs)
        if 'city' in parameters:
            report.city=self.city.key
            parameters['city']=self.city.key
        if 'location_query' in parameters:
            parameters['location_query']=report.location_query
        report.include_unverified=parameters.get('include_unverified',False)
        started=time.perf_counter()
        verifier=getattr(self.competitor_analyzer,'property_verifier',None)
        jobs=[];candidates=list(report.ranked_candidates)
        display_limit=parameters.get('max_candidates',len(candidates))
        pool=getattr(report,'_evaluated_candidates',candidates)
        checked=[];batch=candidates
        if candidates and verifier is not None:
            from .decision_policy import ranking_cost_scope
            verification_parameters={**parameters,'physical_context_quotes':physical_context_quotes(report.ranking_profile),
                'cost_scope':ranking_cost_scope(report.ranking_profile),
                'confirmation_constraints':dict(
                min_area_sqm=report.client_min_area_sqm,max_area_sqm=report.client_max_area_sqm,
                max_price_month=report.client_max_price_month,max_price_sqm_year=report.client_max_price_sqm_year)}
            token=AI_DEADLINE.set(time.monotonic()+parameters['confirmation_timeout_s'])
            try:
                prefetch=_PREFETCH.get()
                while batch:
                    remaining=max(0,AI_DEADLINE.get()-time.monotonic())
                    if remaining>0:
                        jobs=[asyncio.create_task(prefetch.obtain(c,verification_parameters)) for c in batch]
                        _,unfinished=await asyncio.wait(jobs,timeout=remaining)
                        for job in unfinished:job.cancel()
                        outcomes=await asyncio.gather(*jobs,return_exceptions=True)
                    else:outcomes=[None]*len(batch)
                    for candidate,outcome in zip(batch,outcomes):
                        candidate.confirmation=(outcome if isinstance(outcome,dict) and outcome.get('status') in
                            ('requires_confirmation','source_conditions_checked','not_suitable') else
                            pending_confirmation([dict(code='verification_unavailable',message='Проверка источников не завершена; требуется подтверждение условий')]))
                    checked.extend(batch)
                    await refresh_confirmed_ranking(self,report,pool,batch)
                    desired=group_verified_listings([c for c in pool if (c.confirmation or {}).get('status')!='not_suitable'])[:display_limit]
                    seen={id(c) for c in checked}
                    batch=[c for c in desired if id(c) not in seen]
                seen={id(c) for c in checked}
                candidates=[c for c in pool if id(c) in seen]
            finally:
                AI_DEADLINE.reset(token)
                for job in jobs:
                    if not job.done():job.cancel()
                if jobs:await asyncio.gather(*jobs,return_exceptions=True)
        else:
            for candidate in candidates:candidate.confirmation=local_confirmation(candidate)
        for candidate in candidates:add_admission_reason(candidate)
        for candidate in report.comparable_candidates:
            if not candidate.confirmation:candidate.confirmation=local_confirmation(candidate)
        for review in report.floor_verification_candidates:
            review.confirmation=pending_confirmation([dict(code='admission_unknown',message=q) for q in
                (review.assessment.missing_facts or [review.assessment.reason])])
        for review in report.floor_rejected_candidates:
            review.confirmation=dict(status='not_suitable',label='Не соответствует требованиям',
                reasons=[dict(code='admission_rejected',message=review.assessment.reason)])
        rejected=[c for c in candidates if c.confirmation.get('status')=='not_suitable']
        report.confirmation_rejected_candidates=[dict(property_data=c.property_data,confirmation=c.confirmation) for c in rejected]
        rejected_ids={id(c) for c in rejected}
        report.ranked_candidates=group_verified_listings([c for c in candidates if id(c) not in rejected_ids])[:display_limit]
        report.comparable_candidates=group_verified_listings([c for c in report.comparable_candidates if id(c) not in rejected_ids])
        report.best_candidate=report.ranked_candidates[0] if report.ranked_candidates else None
        # A source rejection or grouped duplicate changes the actual rivals.
        # Do not leave score gaps/sensitivity referring to removed ads.
        if hasattr(report,'_evaluated_candidates'):
            survivors=group_verified_listings([c for c in pool if id(c) not in rejected_ids])
            report.top_score_gap=(survivors[0].unrounded_total_score-survivors[1].unrounded_total_score) if len(survivors)>1 else None
            report.input_sensitive_challenger_ids=[c.property_data['id'] for c in survivors[1:]
                if survivors[0].input_score_lower_bound<=c.input_score_upper_bound] if survivors else []
            report.weight_sensitive_challenger_ids=self._weight_sensitive_challengers(survivors)
            basis=report.best_candidate.business_model_diagnostics.get('ordering_basis') if report.best_candidate else None
            if basis and report.top_score_gap==0:
                report.ranking_warnings.append(basis['reason'])
        # Evaluate the actual surviving pool once, after all source-driven
        # repricing, rejections and duplicate grouping. Base weights stay fixed.
        from .ranking_sensitivity import model_assumption_sensitivity,candidate_id
        comparison_pool=survivors if hasattr(report,'_evaluated_candidates') else report.ranked_candidates
        surviving_ids={candidate_id(c) for c in comparison_pool}
        report.missing_inputs=[g for g in report.missing_inputs if 'property_id' not in g or g['property_id'] in surviving_ids]
        if comparison_pool and report.ranking_profile and report.ranking_profile.unmodeled_requirements:
            from .requirement_coverage import assess_requirement_coverage
            scope=dict(min_area_sqm=report.client_min_area_sqm,max_area_sqm=report.client_max_area_sqm,
                max_price_month=report.client_max_price_month,search_radius_m=report.search_radius_m,
                center_lat=report.center_lat,center_lng=report.center_lng,location_query=report.location_query)
            report.requirement_coverage=assess_requirement_coverage(report.ranking_profile,comparison_pool,scope)
            report.missing_inputs=[g for g in report.missing_inputs if g.get('reason') not in ('unsupported_requirement','modeling_conflict')]
            for aspect in report.requirement_coverage:
                if aspect['status'] in ('unmodeled','scoring_conflict'):
                    report.missing_inputs.append(dict(reason='modeling_conflict' if aspect['status']=='scoring_conflict' else 'unsupported_requirement',
                        concept_quote=aspect['concept_quote'],action='Уточнить учёт требования клиента: '+aspect['concept_quote']))
        report.model_sensitivity=model_assumption_sensitivity(report.ranking_profile,comparison_pool)
        report.model_sensitive_challenger_ids=report.model_sensitivity['challenger_ids']
        report.stage_timings_ms['model_sensitivity']=report.model_sensitivity.get('elapsed_ms',0)
        # Previous diagnostics may refer to rivals removed by source checks.
        prefixes=('Лидер может измениться при изменении исходных диапазонов',
                  'Лидер может измениться при изменении весов оценки',
                  'Диапазоны оценки кандидатов пересекаются',
                  'У лидеров совпадают отображаемые баллы',
                  'Порядок вариантов зависит от сценарных допущений:')
        report.ranking_warnings=[w for w in report.ranking_warnings if not w.startswith(prefixes)]
        if report.input_sensitive_challenger_ids:
            report.ranking_warnings.append('Лидер может измениться при изменении исходных диапазонов ±20%; это сценарная проверка, не статистическая вероятность')
        if report.weight_sensitive_challenger_ids:
            report.ranking_warnings.append('Лидер может измениться при изменении весов оценки на ±10%; текущий порядок неустойчив')
        if comparison_pool and any(comparison_pool[0].unrounded_total_score<=c.score_upper_bound for c in comparison_pool[1:]):
            report.ranking_warnings.append('Диапазоны оценки кандидатов пересекаются; единственный достоверный лидер не установлен')
        effects=report.model_sensitivity['effects']
        if effects:
            descriptions=[f"{e['label']} ({e['baseline_value']:g} → {e['scenario_value']:g}; варианты "+
                ', '.join('#'+str(v) for v in e['challenger_ids'])+')' for e in effects[:3]]
            report.ranking_warnings.append('Порядок вариантов зависит от сценарных допущений: '+
                '; '.join(descriptions)+'. Веса клиента сохранены; показаны сопоставимые варианты')
        if report.model_sensitivity['status']=='incomplete':
            report.comparison_complete=False
            report.missing_inputs.append(dict(reason='model_sensitivity_unavailable',
                action='Повторите проверку устойчивости сценарных допущений: часть проверок не выполнена'))
        comparable_ids=set(report.input_sensitive_challenger_ids+report.weight_sensitive_challenger_ids+report.model_sensitive_challenger_ids)
        model_ids=set(report.model_sensitive_challenger_ids)
        rivals=[c for c in comparison_pool[1:] if candidate_id(c) in comparable_ids]
        rivals.sort(key=lambda c:candidate_id(c) not in model_ids)
        report.comparable_candidates=([comparison_pool[0],*rivals][:5]
            if comparison_pool and comparable_ids else [])
        for candidate in report.comparable_candidates:
            if not candidate.confirmation:candidate.confirmation=local_confirmation(candidate)
        from .decision_bounds import deferred_candidate_bounds
        from .decision_policy import blocking_inputs
        report.unverified_evaluated_count=sum(c.floor_assessment is not None and c.floor_assessment.status=='needs_verification' for c in comparison_pool)
        if hasattr(report,'_evaluation_complete'):
            report.comparison_complete=bool(report._evaluation_complete and not blocking_inputs(report.missing_inputs)
                and not report.floor_verification_candidates and not report.unverified_evaluated_count)
            if not report.unverified_evaluated_count:
                report.ranking_warnings=[w for w in report.ranking_warnings if not w.startswith('По выбору клиента в предварительный рейтинг включены')]
        report.deferred_candidate_bounds=(deferred_candidate_bounds(report,comparison_pool)
            if not blocking_inputs(report.missing_inputs) else [])
        irrelevant={b['property_id'] for b in report.deferred_candidate_bounds if b['dominated']}
        if (report.deferred_candidate_bounds and len(irrelevant)==len(report.floor_verification_candidates)
                and report.evaluated_count>=report.total_matching_in_db and not report.analysis_errors
                and not blocking_inputs(report.missing_inputs)):
            report.comparison_complete=True
            report.ranking_warnings=[w for w in report.ranking_warnings if not w.startswith('Есть помещения с неуточнённым этажом или доступом; они ещё не сравнивались')]
            report.ranking_warnings.append('Неподтверждённые отложенные варианты не способны обойти первый вариант даже по верхним границам; их допуск по-прежнему не подтверждён')
        pending=bool(any(candidate_id(c) not in irrelevant for c in report.floor_verification_candidates)
            or any(c.confirmation['status']=='requires_confirmation' for c in report.ranked_candidates))
        report.confirmation_status=('requires_confirmation' if pending else 'source_conditions_checked'
            if report.ranked_candidates else 'no_candidates')
        if pending:report.confirmation_message='Есть варианты, требующие подтверждения. Можно уточнить условия или найти ещё варианты.'
        elif report.ranked_candidates:report.confirmation_message='Условия проверены по доступным источникам; рейтинг остаётся предварительной оценкой.'
        else:report.confirmation_message='Подтверждённых вариантов нет. Можно найти ещё варианты с теми же условиями.'
        reference_gaps=[g for g in report.missing_inputs if g.get('reason')=='missing_scale']
        if reference_gaps and not report.ranked_candidates:
            report.confirmation_message='Сначала укажите ориентиры оценки; найденные объявления ещё не анализировались.'
        if report.include_unverified and any(c.floor_assessment is not None and
                c.floor_assessment.status=='needs_verification' for c in report.ranked_candidates):
            report.comparison_complete=False
            report.selection_status='needs_data'
            report.confirmation_message='Предварительное сравнение выполнено по выбору клиента. Условия допуска не подтверждены; первый вариант в рейтинге требует проверки.'
        if any(c.confirmation.get('checks',{}).get('client_requirements',{}).get('result')=='unknown'
               and 'items' in c.confirmation['checks']['client_requirements'] for c in report.ranked_candidates):
            report.comparison_complete=False
            report.selection_status='needs_data'
        from .decision_policy import blocking_inputs,describe_next_step
        if report.ranked_candidates:
            if blocking_inputs(report.missing_inputs) or not report.comparison_complete:
                report.comparison_complete=False
                report.selection_status='needs_data'
            else:
                report.selection_status=('comparable' if comparable_ids else 'preliminary_leader')
        elif rejected or report.floor_verification_candidates:
            report.comparison_complete=False
            report.selection_status='needs_verification' if pending else 'no_candidates'
        report.data_complete=bool(comparison_pool) and not report.missing_inputs and not report.floor_verification_candidates and all(c.score_coverage>=1-1e-9 for c in comparison_pool)
        shown=[c.property_data for c in candidates+report.comparable_candidates]+[r.property_data for r in report.floor_verification_candidates]+[r.property_data for r in report.floor_rejected_candidates]
        excludes=list(dict.fromkeys([*(parameters.get('exclude_property_keys') or ()),*(key for p in shown for key in property_keys(p))]))
        request=copy.deepcopy({k:v for k,v in parameters.items() if k!='business_data'})
        request.update(center_lat=report.center_lat,center_lng=report.center_lng,search_radius_m=report.search_radius_m,exclude_property_keys=excludes)
        request['business_data_required']=parameters.get('business_data') is not None
        if report.ranking_profile is not None and report.ranking_profile.status!='ready':
            report.next_actions=[dict(type='retry_profile',label='Повторить анализ концепции')]
            report.confirmation_status='requires_confirmation'
            report.confirmation_message='Сначала нужно уточнить или повторить анализ концепции бизнеса.'
        elif reference_gaps and not report.ranked_candidates:
            report.next_actions=[dict(type='provide_references',label='Указать ориентиры оценки',
                required_metrics=[g['metric'] for g in reference_gaps],questions=[g['action'] for g in reference_gaps],
                request=request,preserves_constraints=True,contacts_third_parties=False)]
        else:
            report.next_actions=[dict(type='find_more',label='Найти ещё варианты',request=request,
                preserves_constraints=True,contacts_third_parties=False)]
            if report.floor_verification_candidates and not report.include_unverified:
                provisional_request=copy.deepcopy({k:v for k,v in parameters.items() if k!='business_data'})
                provisional_request.update(center_lat=report.center_lat,center_lng=report.center_lng,
                    search_radius_m=report.search_radius_m,include_unverified=True,
                    business_data_required=parameters.get('business_data') is not None)
                report.next_actions.append(dict(type='continue_unverified',label='Продолжить предварительный анализ',
                    description='Сравнить варианты с неподтверждёнными условиями. Пометки и неизвестные данные сохраняются.',
                    request=provisional_request,preserves_constraints=True,contacts_third_parties=False))
        # Keep caller-supplied graphs/facts out of a duplicated JSON action.
        # An in-process continuation can reuse them; a serialized caller must
        # explicitly pass them again, never silently lose its assumptions.
        report._continuation_business_data=copy.deepcopy(parameters.get('business_data'))
        report.recommendation=describe_next_step(report,comparison_pool)
        report.stage_timings_ms['confirmation']=round((time.perf_counter()-started)*1000,1)
        report.total_search_time_ms+=report.stage_timings_ms['confirmation']
        prefetch=_PREFETCH.get()
        report.confirmation_execution=dict(overlapped_candidates=len(prefetch.jobs),reused=prefetch.hits,invalidated=prefetch.invalidated)
        return report
    @wraps(function)
    async def wrapped(self,*args,**kwargs):
        bound=signature.bind(self,*args,**kwargs);bound.apply_defaults()
        parameters={k:v for k,v in bound.arguments.items() if k!='self'}
        prefetch=ConfirmationPrefetch(getattr(self.competitor_analyzer,'property_verifier',None),parameters)
        token=_PREFETCH.set(prefetch)
        try:return await finalize(self,*args,**kwargs)
        finally:
            _PREFETCH.reset(token)
            await prefetch.close()
    return wrapped


async def find_more(recommender,report,*,business_data=None):
    return await run_continuation(recommender,report,'find_more',business_data=business_data)


async def continue_unverified(recommender,report,*,business_data=None):
    return await run_continuation(recommender,report,'continue_unverified',business_data=business_data)


async def run_continuation(recommender,report,action_type,*,business_data=None):
    action=next((a for a in report.next_actions if a.get('type')==action_type),None)
    if action is None:raise ValueError('В этом отчёте нет выбранного действия продолжения')
    request=copy.deepcopy(action['request']);required=request.pop('business_data_required',False)
    report_city=getattr(report,'city',None)
    if report_city:
        from .cities import get_city
        if request.get('city') is not None and get_city(request['city']) != get_city(report_city):
            raise ValueError('Город действия продолжения не совпадает с городом отчёта')
        request['city']=report_city
    if business_data is None:business_data=copy.deepcopy(getattr(report,'_continuation_business_data',None))
    if required and business_data is None:raise ValueError('Для продолжения передайте исходные business_data; условия и допущения нельзя отбросить')
    request['business_data']=business_data
    return await recommender.find_optimal_properties(**request)
