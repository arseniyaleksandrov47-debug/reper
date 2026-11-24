"""Reconcile model warnings with the actual checks performed by the engine."""
import re
from .proof_grounding import explicit_waiver,client_managed_aspect
from .request_filters import literal_filter_checks,filters_hold


def assess_requirement_coverage(profile,candidates,search_constraints=None):
    """Do not hide an unmodeled aspect just because its sentence was scored.

    Only an exact obligation independently passed by every ranked candidate is
    known to have been handled before rating. An overlap with a scoring quote
    is a model conflict, not proof that every aspect of that sentence is covered.
    """
    records=[]
    scenario=profile.metadata.get('scenario_contract',{})
    property_ids={item.get('source_id') for item in scenario.get('premises_requirements',[]) if item.get('source_id') is not None}
    context={item['quote'] for group in ('context','waivers') for item in scenario.get(group,[])
             if item.get('source_id') is not None and item['source_id'] not in property_ids}
    waived={item['quote'] for item in scenario.get('waivers',[]) if isinstance(item,dict) and isinstance(item.get('quote'),str) and explicit_waiver(item['quote'])}
    for quote in dict.fromkeys(profile.unmodeled_requirements):
        if client_managed_aspect(quote):
            records.append(dict(concept_quote=quote,status='client_managed',metrics=[]))
            continue
        filter_checks=literal_filter_checks(quote,search_constraints)
        if filter_checks and filters_hold(filter_checks,[c.property_data for c in candidates],search_constraints):
            records.append(dict(concept_quote=quote,status='checked_by_filters',metrics=[],checks=filter_checks))
            continue
        if quote in waived:
            records.append(dict(concept_quote=quote,status='waived_by_client',metrics=[]))
            continue
        # Business context is already an input to the scenario. A positive
        # property request/preference cannot disappear under a context label.
        if quote in context and not re.search(r'\b(?:нуж\w*|треб\w*|обязател\w*|необходим\w*|долж\w*|желатель\w*|предпочт\w*|важн\w*|приоритет\w*)\b',quote,re.I):
            records.append(dict(concept_quote=quote,status='scenario_context',metrics=[]))
            continue
        declared=any(item['quote']==quote or scenario.get('sources',{}).get(item.get('source_id'))==quote
                     for item in scenario.get('premises_requirements',[])
                     if item['quote'] not in profile.metadata.get('excluded_scoring_quotes_from_access',[]))
        checked=[];handled=[]
        for candidate in candidates:
            assessment=candidate.floor_assessment
            proofs=assessment.inference_metadata.get('proof_verifications',[]) if assessment else []
            passed=bool(assessment and assessment.status=='eligible' and assessment.analysis_status=='valid'
                and assessment.semantic_review_status=='supported' and proofs and all(p.get('verdict')=='supported' for p in proofs)
                and any(c.get('request_quote')==quote and c.get('critical') is True and c.get('result')=='met'
                        and any(claim.get('requirement_id')==c.get('requirement_id') for proof in proofs for claim in proof.get('claims',[]))
                        for c in assessment.checks))
            checked.append(passed)
            handled.append(passed or bool(declared and assessment and assessment.status=='needs_verification'))
        metrics=[c.metric for c in profile.criteria if c.concept_quote==quote]
        if checked and all(checked):
            records.append(dict(concept_quote=quote,status='checked_before_rating',metrics=metrics))
        elif handled and all(handled):
            records.append(dict(concept_quote=quote,status='verification_pending',metrics=metrics))
        elif metrics:
            records.append(dict(concept_quote=quote,status='scoring_conflict',metrics=metrics))
        else:
            records.append(dict(concept_quote=quote,status='unmodeled',metrics=[]))
    return records
