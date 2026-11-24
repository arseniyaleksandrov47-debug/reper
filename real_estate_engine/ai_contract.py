"""Shared validation for model responses. No business-specific suitability rules."""
import json
import math
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict
from .proof_grounding import explicit_waiver

# Copied into asyncio.to_thread: a cancelled search cannot send a late request
# after waiting for a shared account slot.
AI_DEADLINE=ContextVar('ai_request_deadline',default=None)


class AIContractError(ValueError):
    pass


class AIResponseError(RuntimeError):
    def __init__(self,message,*,finish_reason=None):
        super().__init__(message)
        self.finish_reason=finish_reason


class AITransportError(RuntimeError):
    def __init__(self,message,*,status_code=None,retryable=False,retry_after=0,phase=None):
        super().__init__(message)
        self.status_code=status_code
        self.retryable=retryable
        self.retry_after=retry_after
        self.phase=phase


class ValidatedAIData(dict):
    def __init__(self, value, metadata=None):
        super().__init__(value)
        self.metadata = metadata or {}


@dataclass
class AITextResponse:
    text: str
    model: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __iter__(self):
        # Existing clients can still unpack text, model.
        yield self.text
        yield self.model


def strict_json_object(raw: str) -> dict:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise AIContractError("Duplicate JSON key")
            result[key] = value
        return result
    def constant(value):
        raise AIContractError("Non-finite JSON number")
    def number(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise AIContractError("JSON number overflow")
        return parsed
    try:
        data = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except (ValueError, TypeError) as exc:
        raise AIContractError("Invalid JSON response") from exc
    if not isinstance(data, dict):
        raise AIContractError("Expected JSON object")
    return data


def require_string(value, label):
    if not isinstance(value, str) or not value.strip():
        raise AIContractError(f"Invalid {label}")
    return value


def require_strings(value, label):
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise AIContractError(f"Invalid {label}")
    return value


def index_assessments(data, expected_ids):
    if set(data) != {"assessments"} or not isinstance(data["assessments"], list):
        raise AIContractError("Invalid assessments envelope")
    entries, duplicates = {}, set()
    for entry in data["assessments"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("candidate_id"), str):
            raise AIContractError("Invalid candidate identity")
        key = entry["candidate_id"]
        if key not in expected_ids:
            raise AIContractError("Unknown candidate identity")
        if key in entries:
            duplicates.add(key)
        entries[key] = entry
    # Missing/duplicated entries invalidate only those candidates.
    return entries, duplicates


def floor_response_schema(candidates):
    """Constrain syntax/identity at generation time; client still validates proof."""
    def obj(properties):
        return {"type":"object","properties":properties,"required":list(properties),"additionalProperties":False}
    string = {"type":"string","minLength":1}
    variants={}
    for candidate in candidates:
        checks={}
        for requirement in candidate["requirements"]:
            waived=requirement['origin']=='explicit' and explicit_waiver(requirement['description'])
            evidence_ids=requirement.get("evidence_scope",list(candidate["evidence_catalog"]))
            if waived:evidence_ids=[]
            evidence={"type":"array","items":{"type":"string","enum":evidence_ids}} if evidence_ids else {
                "type":"array","items":{"type":"string"},"maxItems":0}
            checks[requirement["requirement_id"]] = obj({
                "result":{"type":"string","enum":["not_required"] if waived else ["met","not_met","unknown"]},
                "evidence_ids":evidence
            })
        variants[candidate["candidate_id"]] = obj({
            "reason":string,
            "checks":obj(checks),
        })
    return obj({"assessments":obj(variants)})


def floor_proof_schema():
    return {'type':'object','properties':{
        'verdict':{'type':'string','enum':['supported','unknown','unsupported']},
        'reason':{'type':'string','minLength':1,'maxLength':600}},
        'required':['verdict','reason'],'additionalProperties':False}


def floor_rejection_schema(claims):
    return dict(type='object',additionalProperties=False,required=['violated_requirement_ids','reason'],properties={
        'violated_requirement_ids':dict(type='array',maxItems=len(claims),items=dict(type='string',enum=[c['requirement_id'] for c in claims])),
        'reason':dict(type='string',minLength=1,maxLength=600)})


def validate_floor_rejection(data,claims):
    if not isinstance(data,dict) or set(data)!={'violated_requirement_ids','reason'}:
        raise AIContractError('Invalid rejection proof')
    ids=data['violated_requirement_ids'];allowed={c['requirement_id'] for c in claims}
    if not isinstance(ids,list) or any(not isinstance(v,str) or v not in allowed for v in ids) or len(ids)!=len(set(ids)):
        raise AIContractError('Unbound rejection witness')
    require_string(data['reason'],'proof reason')
    if len(data['reason'])>600:raise AIContractError('Proof reason exceeds contract limit')
    return dict(**data,verdict='supported' if ids else 'unknown')


def validate_floor_proof(data):
    if (set(data)!={'verdict','reason'} or data['verdict'] not in ('supported','unknown','unsupported')):
        raise AIContractError('Invalid proof verdict')
    require_string(data['reason'],'proof reason')
    if len(data['reason'])>600:raise AIContractError('Proof reason exceeds contract limit')
    return data


def normalize_floor_response(data, requirements=None):
    """Bind model judgments to code-owned client conditions; never trust echoes."""
    if set(data) != {"assessments"}:
        raise AIContractError("Invalid response envelope")
    entries = data["assessments"]
    if not isinstance(entries,dict) or requirements is None:
        raise AIContractError("Missing request binding")
    normalized=[]
    for identifier,entry in entries.items():
        if (identifier not in requirements or not isinstance(entry,dict)
            or set(entry) != {"reason","checks"}
            or not isinstance(entry["checks"],dict)):
            raise AIContractError("Invalid keyed assessment")
        expected={r["requirement_id"]:r for r in requirements[identifier]}
        if set(entry["checks"]) != set(expected):
            raise AIContractError("Incomplete keyed criteria")
        checks=[]
        for requirement,check in entry["checks"].items():
            if not isinstance(check,dict) or set(check) != {"result","evidence_ids"}:
                raise AIContractError("Invalid keyed criterion")
            if check['result'] not in ('met','not_met','unknown','not_required'):
                raise AIContractError('Invalid criterion result')
            bound=expected[requirement]
            checks.append({"requirement_id":requirement,"description":bound["description"],"origin":bound["origin"],
                           "critical":check["result"] != "not_required",**check,
                           **({"request_quote":bound["request_quote"]} if "request_quote" in bound else {})})
        entry={**entry,'missing_facts':[expected[c['requirement_id']]['description'] for c in checks if c['result']=='unknown']}
        outcomes=[c["result"] for c in checks if c["critical"]]
        status="rejected" if "not_met" in outcomes else "needs_verification" if "unknown" in outcomes or entry["missing_facts"] else "eligible"
        normalized.append({"candidate_id":identifier,"status":status,**entry,"checks":checks})
    return {"assessments":normalized}
