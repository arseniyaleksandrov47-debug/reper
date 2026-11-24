import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from real_estate_engine.ai_contract import AI_DEADLINE
from real_estate_engine.confirmation_cache import ConfirmationProofCache
from real_estate_engine.property_confirmation import PropertyConfirmationVerifier,CHECKS
from test_property_confirmation import SemanticAnalyzer,Sources,source,prop,checks
from real_estate_engine.confirmation_flow import with_confirmation,start_confirmation_prefetch
from real_estate_engine.recommender import OptimalPropertySearchReport,EvaluatedPropertyCandidate
from real_estate_engine.business_model import premises_cost,business_evidence
from test_engine_regressions import competition_report


class CachedAnalyzer(SemanticAnalyzer):
    floor_model='primary'
    property_proof_model='proof-model'
    def __init__(self):super().__init__();self.proof_calls=[];self.proofs={k:dict(verdict='supported',reason='Подтверждено') for k in CHECKS}
    def _call_property_proof_structured_sync(self,prompt,*args,**kwargs):
        payload=json.loads(prompt.split('INPUT_JSON\n')[1])
        if payload.get('task')=='ground_premises_requirements':
            return super()._call_property_proof_structured_sync(prompt,*args,**kwargs)
        self.proof_calls.append(payload)
        if 'items' in payload.get('claim',{}):
            return super()._call_property_proof_structured_sync(prompt,*args,**kwargs)
        return json.dumps(self.proofs[payload['check_id']],ensure_ascii=False),self.property_proof_model


class ProofReuseTests(unittest.IsolatedAsyncioTestCase):
    async def test_independent_conditions_preserve_all_conflicting_facts(self):
        ai=CachedAnalyzer();verifier=PropertyConfirmationVerifier(ai,Sources([source()]))
        proof,_=await verifier._prove('Концепция',{'source':dict(value='Противоречие',scope='building',source_url='https://source.test',fetched_at=time.time())},checks())
        self.assertEqual(len(ai.proof_calls),4);self.assertEqual({p['check_id'] for p in ai.proof_calls},set(CHECKS))
        self.assertEqual(ai.proof_calls[0]['evidence']['source']['value'],'Противоречие')
        self.assertTrue(all(v['verdict']=='supported' for v in proof.values()))

    async def test_repeated_current_proofs_reused_but_changed_concept_facts_model_not_reused(self):
        ai=CachedAnalyzer();verifier=PropertyConfirmationVerifier(ai,Sources([source()]))
        catalog={'s0:freeDate':dict(value='Свободно',scope='unit',source_url='https://unit.test',fetched_at=time.time())}
        for _ in range(2):proof,_=await verifier._prove('Концепция',catalog,checks())
        self.assertEqual(len(ai.proof_calls),4);self.assertTrue(proof['access']['reuse']['hit'])
        await verifier._prove('Другая концепция',catalog,checks())
        changed=copy.deepcopy(catalog);changed['s0:freeDate']['value']='Недоступно'
        await verifier._prove('Концепция',changed,checks())
        ai.property_proof_model='different-proof'
        await verifier._prove('Концепция',catalog,checks())
        self.assertEqual(len(ai.proof_calls),16)

    async def test_malformed_condition_does_not_erase_valid_conditions_and_is_not_cached(self):
        ai=CachedAnalyzer();ai.proofs['access']={'invalid':'value'}
        verifier=PropertyConfirmationVerifier(ai,Sources([source()]))
        catalog={'s0:freeDate':dict(value='Свободно',fetched_at=time.time())}
        for _ in range(2):result,_=await verifier._prove('Concept',catalog,checks())
        self.assertEqual(result['access']['verdict'],'unknown')
        self.assertEqual(result['concept_use']['verdict'],'supported');self.assertEqual(len(ai.proof_calls),5)

    async def test_unknown_primary_checks_do_not_consume_another_request(self):
        ai=CachedAnalyzer();verifier=PropertyConfirmationVerifier(ai,Sources([]))
        values=checks(**{k:'unknown' for k in CHECKS})
        proof,model=await verifier._prove('Concept',{},values)
        self.assertFalse(ai.proof_calls);self.assertIsNone(model)
        self.assertEqual(len(proof),4)

    async def test_source_expiry_and_cost_gaps_are_not_bypassed_by_warm_proof(self):
        ai=CachedAnalyzer();values=[source()];verifier=PropertyConfirmationVerifier(ai,Sources(values))
        candidate=SimpleNamespace(property_data=prop(),business_model_diagnostics={'cost':{'missing_inputs':[dict(field='fitout_total',action='Уточните подготовку')]}})
        for _ in range(2):result=await verifier.verify(candidate,'Concept',{})
        self.assertEqual(len(ai.proof_calls),5);self.assertEqual(result['status'],'requires_confirmation')
        self.assertIn('cost:fitout_total',[r['code'] for r in result['reasons']])
        values[0]['fetched_at']=time.time()-301
        result=await verifier.verify(candidate,'Concept',{})
        self.assertEqual(result['status'],'requires_confirmation')
        self.assertNotIn('availability',result['checks'])


class ReuseTests(unittest.IsolatedAsyncioTestCase):
    def test_reference_order_does_not_change_a_proof_but_identity_and_claim_do(self):
        catalog={'a':dict(value='Факт',source_url='https://unit.test',fetched_at=1000)}
        key=lambda c,p:ConfirmationProofCache.key('Концепция',c,'access',p,'primary','proof')
        first=key(catalog,dict(result='pass',evidence_ids=['a','b']))
        self.assertEqual(first,key(catalog,dict(result='pass',evidence_ids=['b','a'])))
        changed=copy.deepcopy(catalog);changed['a']['source_url']='https://other-unit.test'
        self.assertNotEqual(first,key(changed,dict(result='pass',evidence_ids=['a','b'])))
        self.assertNotEqual(first,key(catalog,dict(result='fail',evidence_ids=['a','b'])))

    async def test_concurrent_identical_requests_share_work_and_return_independent_copies(self):
        cache=ConfirmationProofCache();calls=[]
        async def compute():
            calls.append(1);await asyncio.sleep(.02)
            return dict(verdict='supported',reason='Достаточно'),'m'
        first,second=await asyncio.gather(*(cache.get_or_compute('key',compute,model='m',valid_until=time.time()+100) for _ in range(2)))
        self.assertEqual(len(calls),1);first[0]['reason']='changed'
        self.assertEqual(second[0]['reason'],'Достаточно')

    async def test_cancelled_one_waiter_does_not_cancel_other_waiter(self):
        cache=ConfirmationProofCache();entered=asyncio.Event();release=asyncio.Event();calls=[]
        async def compute():
            calls.append(1);entered.set();await release.wait();return dict(verdict='supported',reason='OK'),'m'
        a=asyncio.create_task(cache.get_or_compute('key',compute,model='m',valid_until=time.time()+100))
        await entered.wait()
        b=asyncio.create_task(cache.get_or_compute('key',compute,model='m',valid_until=time.time()+100));await asyncio.sleep(0)
        a.cancel();await asyncio.gather(a,return_exceptions=True);release.set()
        value=await b;self.assertEqual(value[0]['verdict'],'supported');self.assertEqual(len(calls),1)

    async def test_all_waiters_cancelled_cancels_job_without_storing_answer(self):
        cache=ConfirmationProofCache();entered=asyncio.Event();cancelled=asyncio.Event()
        async def compute():
            entered.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        task=asyncio.create_task(cache.get_or_compute('key',compute,model='m',valid_until=time.time()+100))
        await entered.wait();task.cancel();await asyncio.gather(task,return_exceptions=True);await cancelled.wait()
        self.assertFalse(cache.memory);self.assertFalse(cache.flights)

    async def test_persisted_complete_proof_reused_but_expired_or_incomplete_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            file=str(Path(tmp)/'proofs.db');calls=[]
            async def compute():calls.append(1);return dict(verdict='supported',reason='OK'),'m'
            await ConfirmationProofCache(file).get_or_compute('key',compute,model='m',valid_until=time.time()+100)
            result=await ConfirmationProofCache(file).get_or_compute('key',compute,model='m',valid_until=time.time()+100)
            self.assertEqual(len(calls),1);self.assertEqual(result[0]['reuse']['source'],'disk')
            cache=ConfirmationProofCache();cache.memory['key']=(time.time()-1,(dict(verdict='supported',reason='stale'),'m'))
            await cache.get_or_compute('key',compute,model='m',valid_until=time.time()+100);self.assertEqual(len(calls),2)

    async def test_semantic_unknown_and_transport_error_are_never_cached(self):
        cache=ConfirmationProofCache();calls=[];now=time.time()
        async def ambiguous():calls.append(1);return dict(verdict='unknown',reason='Нет данных'),'m'
        await cache.get_or_compute('key',ambiguous,model='m',valid_until=now+300)
        self.assertNotIn('key',cache.memory)
        async def failed():calls.append(1);return dict(verdict='unknown',reason='Нет ответа',error_type='TimeoutError'),'m'
        await cache.get_or_compute('failed',failed,model='m',valid_until=now+300)
        self.assertNotIn('failed',cache.memory)


def evaluated():
    p=prop()
    return EvaluatedPropertyCandidate(p,competition_report(),50,0,0,
        business_model_diagnostics={'cost':premises_cost(p,business_evidence(p))[1]})


def report_for(candidate):
    return OptimalPropertySearchReport('Новая деятельность','Москва','Москва',55.7,37.6,1000,400,1,1,[candidate],candidate,0)


class OverlapTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_completes_during_environment_without_a_second_request(self):
        started=asyncio.Event();release=asyncio.Event();finished=asyncio.Event();calls=[]
        class Verifier:
            async def verify(self,*args):
                calls.append(1);started.set();await release.wait();finished.set()
                return dict(status='requires_confirmation',label='Требует подтверждения',reasons=[],sources=[])
        class Engine:
            competitor_analyzer=SimpleNamespace(property_verifier=Verifier())
            @with_confirmation
            async def search(self,target_business,business_concept='Концепция',confirmation_timeout_s=.5):
                candidate=evaluated();start_confirmation_prefetch([candidate.property_data],1,{})
                # This barrier can be reached only if verification starts
                # before the original search has returned its ranking.
                await asyncio.wait_for(started.wait(),.3);release.set();await finished.wait()
                return report_for(candidate)
        report=await Engine().search('Новая деятельность')
        self.assertEqual(len(calls),1);self.assertEqual(report.confirmation_execution['reused'],1)

    async def test_changed_premises_facts_are_verified_again(self):
        for field in ('price_month','cost'):
            calls=[];finished=asyncio.Event()
            class Verifier:
                async def verify(self,*args):
                    calls.append(1);finished.set()
                    return dict(status='requires_confirmation',label='Требует подтверждения',reasons=[],sources=[])
            class Engine:
                competitor_analyzer=SimpleNamespace(property_verifier=Verifier())
                @with_confirmation
                async def search(self,target_business,business_concept='Концепция',confirmation_timeout_s=.5):
                    candidate=evaluated();start_confirmation_prefetch([candidate.property_data],1,{})
                    await finished.wait()
                    if field=='cost':candidate.business_model_diagnostics['cost']['missing_inputs']=[]
                    else:candidate.property_data[field]+=100
                    return report_for(candidate)
            result=await Engine().search('Новая деятельность');self.assertEqual(len(calls),2)
            self.assertEqual(result.confirmation_execution['invalidated'],1)

    async def test_large_pool_is_not_speculatively_sent_to_ai(self):
        calls=[]
        class Verifier:
            async def verify(self,*args):
                calls.append(1);return dict(status='requires_confirmation',label='Требует подтверждения',reasons=[],sources=[])
        class Engine:
            competitor_analyzer=SimpleNamespace(property_verifier=Verifier())
            @with_confirmation
            async def search(self,target_business,business_concept='Концепция',confirmation_timeout_s=.5):
                candidate=evaluated();start_confirmation_prefetch([candidate.property_data]*6,1,{})
                await asyncio.sleep(0);self.before=len(calls)
                return report_for(candidate)
        engine=Engine();result=await engine.search('Новая деятельность')
        self.assertEqual(engine.before,0);self.assertEqual(len(calls),1)
        self.assertEqual(result.confirmation_execution['overlapped_candidates'],0)

    async def test_original_search_failure_cancels_early_verification(self):
        started=asyncio.Event();cancelled=asyncio.Event()
        class Verifier:
            async def verify(self,*args):
                started.set()
                try:await asyncio.Event().wait()
                finally:cancelled.set()
        class Engine:
            competitor_analyzer=SimpleNamespace(property_verifier=Verifier())
            @with_confirmation
            async def search(self,target_business,business_concept='Концепция',confirmation_timeout_s=.5):
                start_confirmation_prefetch([evaluated().property_data],1,{})
                await started.wait();raise RuntimeError('Controlled failure')
        with self.assertRaises(RuntimeError):await Engine().search('Новая деятельность')
        self.assertTrue(cancelled.is_set())


if __name__=='__main__':unittest.main()
