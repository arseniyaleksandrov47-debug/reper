import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from real_estate_engine import NvidiaCompetitorAnalyzer
from real_estate_engine.ai_contract import AITransportError
from real_estate_engine.ai_pacing import AIRequestPacer, retry_after_seconds, shared_request_pacer
from real_estate_engine.floor_policy import BusinessFloorProfileResolver
from floor_ai_fixtures import RecordingFloorInference,default_response


PROP=dict(id=1,floor=1,title='Помещение',description='Вход посетителям с улицы разрешён.\nК двери ведёт лестница.')


class IsolatedProofTests(unittest.IsolatedAsyncioTestCase):
    async def test_different_proof_sets_do_not_see_each_others_facts(self):
        def primary(payload):
            data=default_response(payload)
            for c in data['assessments'][0]['checks']:
                if c['requirement_id']=='audience_access':c['evidence']=[dict(field='description',quote='Вход посетителям с улицы разрешён.')]
                if c['requirement_id']=='route_to_unit':c['evidence']=[dict(field='description',quote='К двери ведёт лестница.')]
            return data
        model=RecordingFloorInference(primary)
        def proof(payload):
            if payload['source_fragments']==[dict(field='description',quote='К двери ведёт лестница.')]:
                self.assertNotIn('Вход посетителям',str(payload))
                self.assertEqual([c['requirement_id'] for c in payload['claims']],['route_to_unit'])
                return dict(verdict='unknown',reason='Не установлена пригодность лестницы')
            return dict(verdict='supported',reason='Контрольный разрешённый вход')
        model.proof_handler=proof
        resolver=BusinessFloorProfileResolver(model)
        result=(await resolver.assess_many([PROP],await resolver.resolve('Бизнес')))['1']
        self.assertEqual(result.status,'needs_verification')
        self.assertEqual(len(model.proof_calls),2)

    async def test_unknown_information_is_not_evidence_of_a_final_rejection(self):
        def primary(payload):
            data=default_response(payload)
            next(c for c in data['assessments'][0]['checks'] if c['requirement_id']=='audience_access')['result']='not_met'
            return data
        model=RecordingFloorInference(primary,primary)
        def proof(payload):
            self.assertTrue(all(c['assertion']=='not_met' for c in payload['claims']))
            return dict(verdict='unknown',reason='Отсутствие сведений не доказывает запрет')
        model.proof_handler=proof
        resolver=BusinessFloorProfileResolver(model)
        result=(await resolver.assess_many([dict(id=1,floor=3,title='Помещение',description='Нет сведений о доступе посетителей.')],await resolver.resolve('Бизнес','Приходят клиенты')))['1']
        self.assertEqual(result.status,'needs_verification')

    async def test_identical_source_sets_share_one_conjunctive_proof(self):
        def primary(payload):
            result=default_response(payload)
            for row in result['assessments'][0]['checks']:
                row['evidence']=[dict(field='description',quote=PROP['description'].splitlines()[0])]
            return result
        model=RecordingFloorInference(primary,primary);resolver=BusinessFloorProfileResolver(model)
        result=(await resolver.assess_many([PROP],await resolver.resolve('Бизнес','Клиенты по записи')))['1']
        self.assertEqual(result.status,'eligible')
        self.assertEqual(len(model.proof_calls),1)
        self.assertEqual(len(model.proof_calls[0]['claims']),3)

    async def test_proof_transport_failure_does_not_approve_or_poison_cache(self):
        model=RecordingFloorInference()
        def fail(payload):raise RuntimeError('controlled proof transport failure')
        model.proof_handler=fail;resolver=BusinessFloorProfileResolver(model);profile=await resolver.resolve('Бизнес')
        result=(await resolver.assess_many([PROP],profile))['1']
        self.assertEqual(result.status,'needs_verification');self.assertFalse(resolver._assessment_cache)
        model.proof_handler=lambda p:dict(verdict='supported',reason='Контрольная проверка')
        self.assertEqual((await resolver.assess_many([PROP],profile))['1'].status,'eligible')

    async def test_malformed_proof_never_approves(self):
        model=RecordingFloorInference();model.proof_handler=lambda p:dict(verdict='yes',reason='not a valid verdict')
        resolver=BusinessFloorProfileResolver(model)
        result=(await resolver.assess_many([PROP],await resolver.resolve('Бизнес')))['1']
        self.assertEqual(result.status,'needs_verification');self.assertEqual(result.analysis_status,'schema_error')

    async def test_unknown_primary_assessment_is_not_promoted_by_a_proof(self):
        def primary(payload):
            data=default_response(payload)
            next(c for c in data['assessments'][0]['checks'] if c['requirement_id']=='audience_access').update(result='unknown',evidence=[])
            return data
        model=RecordingFloorInference(primary);resolver=BusinessFloorProfileResolver(model)
        result=(await resolver.assess_many([PROP],await resolver.resolve('Бизнес')))['1']
        self.assertEqual(result.status,'needs_verification');self.assertEqual(model.proof_calls,[])

    async def test_rate_limited_is_explicit_and_not_cached_as_missing_listing_data(self):
        class Limited(BusinessFloorProfileResolver):
            async def _infer(self,*args):raise AITransportError('HTTP 429',status_code=429)
        resolver=Limited()
        result=(await resolver.assess_many([PROP],await resolver.resolve('Бизнес')))['1']
        self.assertEqual(result.analysis_status,'rate_limited');self.assertFalse(resolver._assessment_cache)


class PacingTests(unittest.TestCase):
    def test_concurrent_throttles_share_one_slowdown_and_success_restores_baseline(self):
        pacer=AIRequestPacer(1.6)
        with patch('real_estate_engine.ai_pacing.time.monotonic',return_value=100):
            for _ in range(4):pacer.throttled(30)
        self.assertAlmostEqual(pacer.min_interval,2.4)
        self.assertEqual(pacer.throttle_responses,4)
        for _ in range(32):pacer.succeeded()
        self.assertAlmostEqual(pacer.min_interval,1.6)

    def test_hard_limit_applies_to_every_http_attempt_in_a_rolling_minute(self):
        clock=[100.0];starts=[];pacer=AIRequestPacer(0,40)
        def sleep(seconds):clock[0]+=seconds
        with patch('real_estate_engine.ai_pacing.time.monotonic',side_effect=lambda:clock[0]),patch('real_estate_engine.ai_pacing.time.sleep',side_effect=sleep):
            for _ in range(85):pacer.wait_http_slot(120);starts.append(clock[0])
        for i in range(40,len(starts)):self.assertGreaterEqual(starts[i]-starts[i-40],60)
        self.assertEqual(pacer.http_attempts,85)

    def test_wait_budget_prevents_a_late_request_during_long_cooldown(self):
        pacer=AIRequestPacer(0);pacer.throttled(600)
        with self.assertRaises(AITransportError):pacer.wait_http_slot(.01)
        self.assertEqual(pacer.http_attempts,0)

    def test_same_key_uses_one_shared_pacer_across_clients(self):
        first=shared_request_pacer('controlled-fixture-account',min_interval=0,requests_per_minute=40)
        second=shared_request_pacer('controlled-fixture-account',min_interval=0,requests_per_minute=40)
        self.assertIs(first,second)

    def test_retry_after_accepts_seconds_and_handles_invalid_values(self):
        self.assertEqual(retry_after_seconds('2.5'),2.5)
        for value in (None,'invalid','nan','inf'):self.assertEqual(retry_after_seconds(value),30)
        self.assertEqual(retry_after_seconds('-1'),0)

    def test_rate_limit_does_not_trigger_model_fallback_or_hide_http_status(self):
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.model='fixture-model';analyzer.endpoint='https://example.test';calls=[]
        def post(*args,**kwargs):
            calls.append(kwargs);return SimpleNamespace(status_code=429,headers={'Retry-After':'7'})
        analyzer.http_session=SimpleNamespace(post=post);analyzer.floor_request_pacer=AIRequestPacer(0)
        with self.assertRaises(AITransportError) as context:analyzer._call_nvidia_sync('fixture')
        self.assertEqual(context.exception.status_code,429);self.assertEqual(context.exception.retry_after,7)
        self.assertEqual(len(calls),1);self.assertEqual(analyzer.floor_request_pacer.throttle_responses,1)


class AsyncPacingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelling_pacing_wait_does_not_start_an_http_request(self):
        pacer=AIRequestPacer(10);await pacer.wait()
        task=asyncio.create_task(pacer.wait());await asyncio.sleep(0);task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(pacer.http_attempts,0)
