import asyncio
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from real_estate_engine import NvidiaCompetitorAnalyzer
from real_estate_engine.ai_contract import AI_DEADLINE,AIResponseError,AITransportError
from real_estate_engine.ai_pacing import AIRequestPacer
from real_estate_engine.property_confirmation import PropertyConfirmationVerifier,normalize_explanation,validate_checks
from real_estate_engine.property_sources import PublicPropertySources,PublicSourceError
from real_estate_engine.environment_cache import EnvironmentSourceCache
from test_property_confirmation import SemanticAnalyzer,Sources,source,prop,checks,ARCHIVE


RECORDED=ARCHIVE.parents[2]/'confirmation_flow/semantic_20261105_110452'


def response(finish='stop'):
    return SimpleNamespace(status_code=200,content=json.dumps(dict(choices=[dict(finish_reason=finish,message=dict(content='{"verdict":"supported","reason":"Достаточно"}'))])).encode())


def transport(post):
    ai=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
    ai.model='primary';ai.endpoint='https://example.test';ai.http_session=SimpleNamespace(post=post)
    ai.floor_request_pacer=AIRequestPacer(0)
    return ai


class CompletedAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_correct_office_verdict_survives_long_explanation(self):
        recorded=json.loads(next(RECORDED.glob('ai_office_use_*.json')).read_text(encoding='utf-8'))
        answer=json.loads(recorded['text']);self.assertGreater(len(answer['reason']),220)
        self.assertEqual(recorded['metadata']['finish_reason'],'stop')
        ai=SemanticAnalyzer()
        ai._call_property_proof_structured_sync=lambda *a,**kw:(recorded['text'],recorded['model'])
        verifier=PropertyConfirmationVerifier(ai,Sources([source()]))
        primary=checks(availability='unknown',access='unknown',client_requirements='unknown')
        original=json.loads((RECORDED.parent/'run_20261105_103236/staff_office.json').read_text(encoding='utf-8'))
        primary['concept_use']=original['checks']['concept_use']['primary']
        proof,model=await verifier._prove('Бизнес: Офис\nКонцепция: Офис для сотрудников. Посетители и продажи с улицы не планируются.',original['evidence'],primary)
        self.assertEqual(proof['concept_use']['verdict'],'supported')
        self.assertLessEqual(len(proof['concept_use']['reason']),220)
        self.assertEqual(proof['concept_use']['reason_full'],answer['reason'])
        self.assertEqual(model,'mistralai/mistral-large-4-1t-a52b')

    async def test_recorded_access_overclaim_remains_unsupported(self):
        recorded=json.loads(next(RECORDED.glob('ai_coffee_access_*.json')).read_text(encoding='utf-8'))
        ai=SemanticAnalyzer();ai._call_property_proof_structured_sync=lambda *a,**kw:(recorded['text'],recorded['model'])
        verifier=PropertyConfirmationVerifier(ai,Sources([]))
        primary=checks(availability='unknown',concept_use='unknown',client_requirements='unknown')
        original=json.loads((RECORDED.parent/'run_20261105_103236/walk_in.json').read_text(encoding='utf-8'))
        primary['access']=original['checks']['access']['primary']
        proof,_=await verifier._prove('Бизнес: Кофейня навынос\nКонцепция: Покупатели свободно заходят с улицы без пропуска и записи.',original['evidence'],primary)
        self.assertEqual(proof['access']['verdict'],'unsupported')

    async def test_stage_timeout_keeps_completed_independent_facts(self):
        ai=SemanticAnalyzer();verifier=PropertyConfirmationVerifier(ai,Sources([source()]))
        original=ai._call_property_proof_structured_sync
        def slow_access(prompt,*args,**kwargs):
            if json.loads(prompt.split('INPUT_JSON\n')[1]).get('check_id')=='access':time.sleep(.35)
            return original(prompt,*args,**kwargs)
        ai._call_property_proof_structured_sync=slow_access
        token=AI_DEADLINE.set(time.monotonic()+.3)
        try:
            result=await verifier.verify(SimpleNamespace(property_data=prop(),business_model_diagnostics={'cost':{'missing_inputs':[]}}),'Концепция',{})
        finally:AI_DEADLINE.reset(token)
        self.assertEqual(result['checks']['concept_use']['result'],'pass')
        self.assertEqual(result['checks']['access']['result'],'unknown')
        self.assertEqual(result['checks']['access']['independent']['error_phase'],'stage_deadline')
        self.assertEqual(result['status'],'requires_confirmation')

    def test_long_primary_explanation_does_not_erase_evidence_or_decision(self):
        original=checks();original['concept_use']['reason']='Пояснение. '*40
        normalized=validate_checks(original,{'s0:freeDate':{},'s0:destination':{}})
        self.assertEqual(normalized['concept_use']['result'],'pass')
        self.assertEqual(normalized['concept_use']['evidence_ids'],['s0:freeDate'])
        self.assertIn('reason_full',normalized['concept_use'])

    def test_explanations_remain_bounded_and_nonempty(self):
        for value in (' ',None,False,'x'*2001):
            with self.subTest(value_type=type(value)),self.assertRaises(ValueError):normalize_explanation(value)


class ProofTransportTests(unittest.TestCase):
    def test_truncated_primary_answer_retries_unconstrained_once_within_same_deadline(self):
        seen=[]
        def post(*args,**kwargs):
            seen.append(kwargs)
            return response('length' if len(seen)==1 else 'stop')
        ai=transport(post);ai.floor_model=ai.property_assessment_model='poolside/laguna-xs-2.1'
        ai._call_property_assessment_structured_sync('check',2400,response_schema={'type':'object'})
        self.assertEqual(len(seen),2)
        self.assertEqual([r['json']['response_format']['type'] for r in seen],['json_schema','json_object'])
        self.assertEqual(seen[0]['json']['model'],seen[1]['json']['model'])
        self.assertIsNone(AI_DEADLINE.get())

    def test_model_reasoning_tokens_and_deadline_are_explicit(self):
        seen=[];ai=transport(lambda *a,**kw:(seen.append(kw) or response()))
        _,model=ai._call_property_proof_structured_sync('proof',500,response_schema={'type':'object'})
        payload=seen[0]['json']
        self.assertEqual(model,'nvidia/nemotron-3.5-super-vl-120b-a12b')
        self.assertEqual(payload['max_tokens'],800)
        self.assertNotIn('reasoning_effort',payload)
        self.assertEqual(payload['chat_template_kwargs'],{'enable_thinking':False})
        self.assertEqual(payload['response_format'],{'type':'json_object'})
        self.assertLessEqual(seen[0]['timeout'].connect_timeout,5)
        self.assertLessEqual(seen[0]['timeout'].total,45)
        self.assertEqual(ai.floor_request_pacer.http_attempts,1)

    def test_connect_failure_retries_same_model_once_and_counts_both_requests(self):
        seen=[]
        def post(*args,**kwargs):
            seen.append(kwargs['json']['model'])
            if len(seen)==1:raise requests.ConnectTimeout('Controlled failure')
            return response()
        ai=transport(post);ai._call_property_proof_structured_sync('proof',500,response_schema={})
        self.assertEqual(seen,['nvidia/nemotron-3.5-super-vl-120b-a12b']*2)
        self.assertEqual(ai.floor_request_pacer.http_attempts,2)

    def test_fast_read_timeout_has_one_retry_but_truncated_answer_is_not_retried(self):
        for error,finish in ((requests.ReadTimeout('Slow provider'),None),(None,'length')):
            seen=[]
            def post(*a,**kw):
                seen.append(kw)
                if error:raise error
                return response(finish)
            ai=transport(post)
            with self.assertRaises((AITransportError,AIResponseError)):ai._call_property_proof_structured_sync('proof',500,response_schema={})
            self.assertEqual(len(seen),2 if error else 1)

    def test_explicit_slow_model_read_timeout_is_not_repeated(self):
        seen=[]
        def post(*a,**kw):
            seen.append(kw);raise requests.ReadTimeout('Slow provider')
        ai=transport(post);ai.property_proof_model='mistralai/mistral-large-4-1t-a52b'
        with self.assertRaises(AITransportError):ai._call_property_proof_structured_sync('proof',500,response_schema={})
        self.assertEqual(len(seen),1)

    def test_connect_read_proxy_and_http_errors_keep_different_phases(self):
        for exception,phase in ((requests.ConnectTimeout('x'),'connect'),(requests.ReadTimeout('x'),'read'),
                                (requests.exceptions.ProxyError('x'),'proxy')):
            def post(*a,**kw):raise exception
            with self.assertRaises(AITransportError) as ctx:
                transport(post)._call_nvidia_sync('test')
            self.assertEqual(ctx.exception.phase,phase)
        ai=transport(lambda *a,**k:SimpleNamespace(status_code=503,headers={}))
        with self.assertRaises(AITransportError) as ctx:ai._call_nvidia_sync('test')
        self.assertEqual(ctx.exception.phase,'http_status');self.assertEqual(ctx.exception.status_code,503)

    def test_exhausted_deadline_does_not_send_request(self):
        seen=[];ai=transport(lambda *a,**kw:(seen.append(kw) or response()))
        token=AI_DEADLINE.set(time.monotonic()-1)
        try:
            with self.assertRaises(AITransportError) as ctx:ai._call_property_proof_structured_sync('proof',500,response_schema={})
        finally:AI_DEADLINE.reset(token)
        self.assertEqual(ctx.exception.phase,'deadline');self.assertFalse(seen)


class SourceDiagnosticsTests(unittest.TestCase):
    def test_network_failure_is_distinguished_from_identity_mismatch_without_secrets(self):
        maps=SimpleNamespace(_fetch_query_sync=lambda *a:[])
        for exc,kind in ((PublicSourceError('timeout'),'timeout'),(PublicSourceError('http_status',503),'http_status'),
                         (ValueError('secret proxy URL must not be saved'),'identity_or_format')):
            provider=PublicPropertySources(SimpleNamespace(map_collector=maps,environment_source_cache=EnvironmentSourceCache(None)))
            provider._publisher_sites=lambda p:['https://mega-realty.ru']
            def fail(*a):raise exc
            provider._get=fail
            result=provider.collect(prop())
            self.assertFalse(result['sources'])
            self.assertEqual(result['errors'][0]['kind'],kind)
            self.assertNotIn('secret',json.dumps(result))


if __name__=='__main__':unittest.main()
