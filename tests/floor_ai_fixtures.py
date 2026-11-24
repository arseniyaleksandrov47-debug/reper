"""Controlled model responses for contract tests, not production policy."""
import json
from functools import wraps


def ranking_transport_fixture(function):
    """Put controlled arithmetic profiles on the provider's current wire."""
    @wraps(function)
    def infer(*args, schema):
        raw,model=function(*args)
        try:
            answer=json.loads(raw)
        except (ValueError,TypeError):
            return raw,model
        if not isinstance(answer,dict) or not isinstance(answer.get('criteria'),list):
            return raw,model
        from real_estate_engine.ranking_policy import concept_fragments,explicit_area_bands
        from test_market_context import market_config
        prompt=args[-2]
        payload=json.loads(prompt.split('\nINPUT_JSON\n',1)[1])
        concept=payload['concept']
        fragments=concept_fragments(concept)
        first=next(iter(fragments))
        def source_id(quote):
            return next((key for key,value in fragments.items() if quote in value),first)
        groups=dict(audience_access_choice=None,occupancy_cost=None,physical_fit=None)
        for criterion in answer['criteria']:
            metric=criterion['metric']
            group=('physical_fit' if metric=='area_sqm' else 'occupancy_cost'
                   if metric in ('premises_cost_month','rent_month','rent_sqm_year') else 'audience_access_choice')
            row={**criterion,'concept_quote':source_id(criterion['concept_quote'])}
            if group!='physical_fit':
                row.update(metric=metric if group=='occupancy_cost' and metric in ('rent_month','premises_cost_month') else
                    'premises_cost_month' if group=='occupancy_cost' else 'market_fit_index',
                    preference='lower' if group=='occupancy_cost' else 'higher',low=0,high=100)
            groups[group]=row
        config=None
        if answer['status']=='ready':
            if groups['audience_access_choice'] is None:
                template=next(iter(answer['criteria']))
                groups['audience_access_choice']={**template,'metric':'market_fit_index','weight':.001,
                    'preference':'higher','low':0,'high':100,'concept_quote':first}
            bands=explicit_area_bands(concept)
            if len(bands)==1 and groups['physical_fit'] is None:
                low,high=next(iter(bands))
                template=next(iter(answer['criteria']))
                groups['physical_fit']={**template,'metric':'area_sqm','weight':.001,'preference':'range',
                    'low':low,'high':high,'concept_quote':next(key for key,value in fragments.items()
                        if (low,high) in explicit_area_bands(value))}
            config=market_config(concept)
            config.pop('active_hours')
            usage=payload['interpreted_scenario']['audience_usage']
            for key,row in config['audience'].items():
                row['weight']=1 if usage[key]['usage']=='use' else 0
            for group in ('audience','alternatives','strength'):
                for row in config[group].values():row['concept_quote']=first
            config['access']['concept_quote']=first
        answer.update(criteria=groups,market_context=config,
            visit_context_quotes=[source_id(q) for q in answer['visit_context_quotes']],
            unmodeled_requirements=[source_id(q) for q in answer['unmodeled_requirements']])
        return json.dumps(answer,ensure_ascii=False),model
    return infer


def scenario_transport_fixture(function):
    """Supply explicit audience applicability in controlled scenario replies."""
    @wraps(function)
    def infer(*args):
        raw,model=function(*args)
        try:answer=json.loads(raw)
        except (ValueError,TypeError):return raw,model
        if not isinstance(answer,dict) or 'interpretations' not in answer or 'audience_usage' in answer:return raw,model
        payload=json.loads(args[-3].split('\nINPUT_JSON\n',1)[1])
        sources=payload['sources'];first=next(iter(sources))
        waiver=next((key for key,item in answer['interpretations'].items()
                     if item['role']=='waiver' and 'поток' in sources[key].lower()),None)
        answer['audience_usage']={key:dict(usage='not_applicable' if key=='site_audience' else
            'waived' if key=='transit_flow' and waiver else 'use',
            source_id=waiver if key=='transit_flow' and waiver else first,
            reason='Controlled applicability fixture',
            waiver_quote=sources[waiver] if key=='transit_flow' and waiver else None)
            for key in ('transit_flow','residents','workers','site_audience','synergy')}
        return json.dumps(answer,ensure_ascii=False),model
    return infer


def ranking_fixture_response(concept):
    """Fixed arithmetic fixture; this does not model business semantics."""
    def criterion(metric,weight,preference,low,high):
        return dict(metric=metric,weight=weight,preference=preference,low=low,high=high,
            concept_quote=concept,reason='Controlled scoring fixture',assumption='Controlled utility scale')
    return dict(status='ready',summary='Controlled concept profile',
        criteria=[criterion('passing_traffic_index',.75,'higher',0,100),criterion('rent_month',.25,'lower',0,250000)],
        questions=[],assumptions=['Fixture values, not inferred market facts'],visit_context_quotes=[concept],unmodeled_requirements=[])


def profile_response(**changes):
    result = {"ready": True, "label": "Тестовый сценарий", "visit_scenario": "Сценарий из теста",
        "requirements": [{"id": "known_level", "description": "Предлагаемый этаж известен",
                          "critical": True, "origin": "inferred"}],
        "assumptions": [], "questions": [], "reason": "Контролируемый ответ модели"}
    result.update(changes)
    return result


def assessment_response(item, **changes):
    name = "floor" if item["facts"]["floor"] is not None else "offered_floors"
    result = {"candidate_id": item["candidate_id"], "status": "eligible",
        "reason": "Условия подтверждены данными тестового объявления",
        "checks": [], "missing_facts": []}
    result.update(changes)
    # These are predetermined valid wire responses for orchestration tests.
    # Actual semantic quality is checked separately on hand-labelled scenarios.
    for requirement in item.get("requirements", []):
        text_field = next((f for f in ("title", "description", "access_context") if item["facts"].get(f)), name)
        check={"requirement_id":requirement["requirement_id"],"description":requirement["description"],
            "critical":True,"origin":requirement["origin"],"result":"met",
            "evidence":[{"field":text_field,"value":item["facts"][text_field]}]}
        if 'evidence_scope' in requirement:
            check['evidence']=[item['evidence_catalog'][k] for k in requirement['evidence_scope']]
            if not check['evidence']:check['result']='unknown'
        if requirement['requirement_id']=='route_to_unit' and not item['facts'].get('offered_floors'):
            check.update(result='unknown',evidence=[])
        if "request_quote" in requirement:check["request_quote"]=requirement["request_quote"]
        result["checks"].append(check)
    return result


def default_response(payload):
    if payload["task"] == "review_floor_decisions":
        return supported_review_response(payload)
    if payload["task"] in ("derive_floor_requirements", "review_floor_requirements"):
        return profile_response()
    return {"assessments": [assessment_response(p) for p in payload["candidates"]]}


def supported_review_response(payload):
    """Predetermined independent result for contract tests; not semantic evaluation."""
    return {"assessments": [assessment_response(p) for p in payload["candidates"]]}


def floor_wire_reply(payload, result):
    """Serialize controlled decisions using the same keyed contract as the provider."""
    if isinstance(result, str):
        return result
    if not isinstance(result, dict) or set(result)!={'assessments'} or not isinstance(result['assessments'], list):
        return json.dumps(result,ensure_ascii=False)
    catalogs={item['candidate_id']:item['evidence_catalog'] for item in payload['candidates']}
    assessments={}
    for entry in result['assessments']:
        if not isinstance(entry,dict) or 'candidate_id' not in entry or not isinstance(entry.get('checks'),list):
            return json.dumps(result,ensure_ascii=False)
        identity=entry['candidate_id']
        if identity in assessments:
            return json.dumps(result,ensure_ascii=False)
        catalog=catalogs.get(identity,{})
        checks={}
        for check in entry['checks']:
            identifier=check['requirement_id']
            if identifier in checks:
                return json.dumps(result,ensure_ascii=False)
            ids=check.get('evidence_ids')
            if ids is None:
                ids=[]
                for ref in check.get('evidence',[]):
                    matches=[key for key,fact in catalog.items() if fact==ref]
                    if not matches:
                        value=ref.get('value',ref.get('quote'))
                        if isinstance(value,str):
                            matches=[key for key,fact in catalog.items() if fact['field']==ref.get('field')
                                     and fact.get('quote') and fact['quote'] in value]
                    ids.extend(matches or ['unbound-fixture-reference'])
            checks[identifier]=dict(result=check['result'],evidence_ids=ids)
        assessments[identity]={'checks':checks}
        if 'reason' in entry:assessments[identity]['reason']=entry['reason']
    return json.dumps(dict(assessments=assessments),ensure_ascii=False)


def proof_wire_reply(payload,result):
    if (payload.get('proof_goal')=='find_explicit_violations' and isinstance(result,dict)
            and result.get('verdict') in ('supported','unsupported','unknown')):
        result=dict(violated_requirement_ids=[c['requirement_id'] for c in payload['claims']]
                    if result['verdict']=='supported' else [],reason=result['reason'])
    return json.dumps(result,ensure_ascii=False)


class RecordingFloorInference:
    def __init__(self, handler=default_response, review_handler=supported_review_response):
        self.handler, self.calls, self.model = handler, [], "fixture-model"
        self.review_handler = review_handler
        self.proof_calls=[]
        self.proof_handler=lambda p:dict(verdict='supported',reason='Controlled positive proof fixture')

    def _call_floor_structured_sync(self, prompt, max_tokens, *, schema, review=False):
        payload = json.loads(prompt.split("\nINPUT_JSON\n", 1)[1])
        if payload['task']=='verify_floor_proof':
            self.proof_calls.append(payload)
            return proof_wire_reply(payload,self.proof_handler(payload)),'proof-fixture-model'
        self.calls.append(payload)
        result = (self.review_handler if review else self.handler)(payload)
        return floor_wire_reply(payload,result), self.model


class FixtureFloorAnalyzer:
    @staticmethod
    @scenario_transport_fixture
    def _call_scenario_contract_structured_sync(prompt,max_tokens,schema):
        payload=json.loads(prompt.split('\nINPUT_JSON\n',1)[1])
        # Fixed orchestration fixture, not a language-understanding test.
        return json.dumps(dict(status='ready',
            interpretations={key:dict(role='context',premises_quote=None) for key in payload['sources']},
            primary_priority=None,priority_rules=[],questions=[]),ensure_ascii=False),'fixture-scenario-model'

    @staticmethod
    @ranking_transport_fixture
    def _call_ranking_profile_structured_sync(prompt,max_tokens):
        payload=json.loads(prompt.split('\nINPUT_JSON\n',1)[1])
        return json.dumps(ranking_fixture_response(payload['concept']),ensure_ascii=False),'fixture-ranking-model'
    @staticmethod
    def _call_floor_structured_sync(prompt, max_tokens, *, schema, review=False):
        payload = json.loads(prompt.split("\nINPUT_JSON\n", 1)[1])
        if payload['task']=='verify_floor_proof':
            return proof_wire_reply(payload,dict(verdict='supported',reason='Controlled positive proof fixture')),'proof-fixture-model'
        return floor_wire_reply(payload,default_response(payload)), "fixture-model"
