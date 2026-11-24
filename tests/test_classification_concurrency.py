"""Concurrency preserves organization identity, proof, cancellation and rate limits."""
import asyncio
import hashlib
import json
import threading
import time
import unittest
from collections import Counter

from real_estate_engine.ai_contract import AITextResponse, AI_DEADLINE
from real_estate_engine.competitor_classification import ConceptCompetitorClassifier
from test_engine_regressions import business_card


class Transport:
    def __init__(self):
        self.loop=asyncio.get_running_loop()
        self.events=asyncio.Queue()
        self.lock=threading.Lock()
        self.calls=Counter();self.proofs=Counter()
        self.active=0;self.peak=0
        self.gates={};self.all_infer_gate=None

    def enter(self,stage,ids):
        with self.lock:
            counts=self.calls if stage=='infer' else self.proofs
            counts.update(ids);self.active+=1;self.peak=max(self.peak,self.active)
        self.loop.call_soon_threadsafe(self.events.put_nowait,(stage,tuple(ids)))
        try:
            gate=self.gates.get((stage,tuple(ids)))
            if stage=='infer' and self.all_infer_gate is not None:gate=self.all_infer_gate
            if gate is not None and not gate.wait(5):raise TimeoutError('Test gate was not released')
        finally:
            with self.lock:self.active-=1

    def infer(self,prompt,tokens,**kwargs):
        data=json.loads(prompt.split('\nINPUT_JSON\n',1)[1])
        self.enter('infer',[o['org_id'] for o in data['organizations']])
        return AITextResponse(json.dumps(dict(categories=[dict(idx=o['idx'],same_purchase_offer='yes',
            need_relation='same_purchase',relationship_basis='specific_purchase',audience_relation='overlap',
            scenario_access='compatible',evidence_fields=['categories'],offer_evidence_ids=['category:0'],
            reason='Исходное предложение') for o in data['organizations']],summary='Контроль')),'primary',{})

    def verify(self,prompt,tokens,**kwargs):
        claims=json.loads(prompt.split('\nINPUT_JSON\n',1)[1])['claims']
        self.enter('proof',[v['source_fragments'][0]['quote'].removeprefix('offer-') for v in claims.values()])
        return AITextResponse(json.dumps({k:dict(verdict='supported',reason='Подтверждено исходными данными')
            for k in claims}),'proof',{})

    async def started(self,stage,ids):
        # Independent batches may finish in any order; keep unmatched signals.
        expected=(stage,tuple(ids));held=[]
        try:
            while True:
                event=await asyncio.wait_for(self.events.get(),2)
                if event==expected:return
                held.append(event)
        finally:
            for event in held:self.events.put_nowait(event)


class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.classifier=ConceptCompetitorClassifier()
        self.transport=Transport();self.tasks=[];self.gates=[]

    async def asyncTearDown(self):
        for gate in self.gates:gate.set()
        for task in self.tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)

    def gate(self,stage,ids):
        gate=threading.Event();self.gates.append(gate)
        self.transport.gates[stage,tuple(ids)]=gate
        return gate

    async def classify(self,ids):
        cards=[business_card(org_id=i,categories=['offer-'+i]) for i in ids]
        return await self.classifier.classify(cards,'Бизнес','Точная концепция','primary',self.transport.infer,
            verify=self.transport.verify,verifier_model='proof')

    def start(self,ids,deadline=None):
        token=AI_DEADLINE.set(deadline)
        try:task=asyncio.create_task(self.classify(ids))
        finally:AI_DEADLINE.reset(token)
        self.tasks.append(task);return task

    async def joined(self,users):
        async def check():
            while not any(f.users==users for f in self.classifier._inflight.values()):await asyncio.sleep(0)
        await asyncio.wait_for(check(),2)

    async def test_slow_proof_does_not_block_independent_or_cached_result(self):
        gate=self.gate('proof',['a']);slow=self.start(['a'])
        await self.transport.started('proof',['a'])
        fast=await asyncio.wait_for(self.start(['b']),2)
        cached=await asyncio.wait_for(self.start(['b']),2)
        self.assertFalse(slow.done())
        self.assertEqual(fast[0]['categories'][0]['level'],'direct')
        self.assertEqual(cached[2]['cache_hits'],1)
        gate.set();await slow
        self.assertEqual(self.transport.calls,Counter(a=1,b=1))
        self.assertEqual(self.transport.proofs,Counter(a=1,b=1))

    async def test_partial_overlap_has_one_request_and_proof_per_organization(self):
        gate=self.gate('infer',['a','b']);first=self.start(['a','b'])
        await self.transport.started('infer',['a','b'])
        second=self.start(['b','c']);await self.transport.started('proof',['c'])
        gate.set();one,two=await asyncio.gather(first,second)
        self.assertEqual(self.transport.calls,Counter(a=1,b=1,c=1))
        self.assertEqual(self.transport.proofs,Counter(a=1,b=1,c=1))
        self.assertEqual(two[2]['inflight_hits'],1)
        self.assertEqual({k:v['idx'] for k,v in two[2]['organization_decisions'].items()},{'b':1,'c':2})
        # Mutating one result must not poison another result or the cache.
        two[2]['organization_decisions']['b']['offer_proof']['verdict']='unsupported'
        cached=await self.classify(['c','b','a'])
        self.assertEqual(cached[2]['cache_hits'],3)
        self.assertTrue(all(v['offer_proof']['verdict']=='supported' for v in cached[2]['organization_decisions'].values()))
        self.assertEqual(one[2]['organization_decisions']['b']['idx'],2)

    async def test_chunks_run_concurrently_with_bound_and_stable_result_order(self):
        self.classifier=ConceptCompetitorClassifier(batch_size=2,max_concurrent_batches=2)
        gate=threading.Event();self.gates.append(gate);self.transport.all_infer_gate=gate
        task=self.start(list('abcdefghi'))
        await self.transport.started('infer',['a','b']);await self.transport.started('infer',['c','d'])
        self.assertEqual(sum(self.transport.calls.values()),4)
        self.assertEqual(self.transport.peak,2)
        gate.set();result,_,meta=await task
        self.assertLessEqual(self.transport.peak,2)
        self.assertEqual([e['idx'] for e in result['categories']],list(range(1,10)))
        self.assertEqual({k:v['idx'] for k,v in meta['organization_decisions'].items()},dict(zip('abcdefghi',range(1,10))))
        self.assertEqual(meta['unknown_count'],0);self.assertEqual(meta['batches'],5)

    async def test_cancel_owner_or_joiner_preserves_other_subscriber(self):
        for cancel_owner in (True,False):
            self.classifier=ConceptCompetitorClassifier();self.transport=Transport()
            gate=self.gate('infer',['a']);owner=self.start(['a'])
            await self.transport.started('infer',['a']);joiner=self.start(['a']);await self.joined(2)
            cancelled,survivor=(owner,joiner) if cancel_owner else (joiner,owner)
            cancelled.cancel()
            with self.assertRaises(asyncio.CancelledError):await cancelled
            self.assertFalse(survivor.done());gate.set()
            result=await survivor
            self.assertEqual(result[2]['unknown_count'],0)
            self.assertEqual(self.transport.calls,Counter(a=1))
            self.assertEqual(self.transport.proofs,Counter(a=1))
            self.assertFalse(self.classifier._inflight)

    async def test_all_subscribers_cancel_without_late_proof_or_cache(self):
        gate=self.gate('infer',['a']);owner=self.start(['a'])
        await self.transport.started('infer',['a']);joiner=self.start(['a']);await self.joined(2)
        owner.cancel();joiner.cancel();await asyncio.gather(owner,joiner,return_exceptions=True)
        self.assertFalse(self.classifier._inflight);self.assertFalse(self.classifier._jobs)
        self.assertFalse(self.classifier.cache)
        gate.set();await asyncio.sleep(.02)
        self.assertFalse(self.transport.proofs)
        # A subsequent search must reserve a new job, not inherit a cancelled future.
        result=await self.classify(['a'])
        self.assertEqual(result[2]['unknown_count'],0)
        self.assertEqual(self.transport.calls,Counter(a=2))

    async def test_short_waiter_deadline_does_not_cancel_longer_owner(self):
        gate=self.gate('infer',['a']);owner=self.start(['a'],time.monotonic()+4)
        await self.transport.started('infer',['a'])
        short=self.start(['a'],time.monotonic()+.05)
        result=await short
        self.assertEqual(result[2]['unknown_count'],1);self.assertFalse(owner.done())
        gate.set();completed=await owner
        self.assertEqual(completed[2]['unknown_count'],0)
        self.assertEqual(self.transport.calls,Counter(a=1));self.assertEqual(self.transport.proofs,Counter(a=1))

    async def test_expired_queued_job_makes_no_http_attempt(self):
        self.classifier=ConceptCompetitorClassifier(max_concurrent_batches=1)
        gate=self.gate('infer',['a']);active=self.start(['a'])
        await self.transport.started('infer',['a'])
        queued=self.start(['b'],time.monotonic()+.05)
        result=await queued
        self.assertEqual(result[2]['unknown_count'],1)
        gate.set();await active
        self.assertEqual(self.transport.calls,Counter(a=1))
        self.assertFalse(self.classifier._inflight)

    async def test_duplicate_cards_share_job_and_keep_both_rows(self):
        result=await self.classify(['a','a'])
        self.assertEqual([e['idx'] for e in result[0]['categories']],[1,2])
        self.assertEqual(self.transport.calls,Counter(a=1));self.assertEqual(self.transport.proofs,Counter(a=1))
        self.assertEqual(result[2]['unknown_count'],0)
        self.assertFalse(self.classifier._inflight)

    async def test_mixed_cached_and_fresh_cards_pack_into_minimal_batches(self):
        self.classifier=ConceptCompetitorClassifier(batch_size=2,max_concurrent_batches=2)
        await self.classify(['a','b','d','e'])
        self.transport.calls.clear();self.transport.proofs.clear()
        result=await self.classify(['a','b','c','d','e','f'])
        self.assertEqual([e['idx'] for e in result[0]['categories']],[1,2,3,4,5,6])
        self.assertEqual(result[2]['cache_hits'],4)
        self.assertEqual(result[2]['classified_fresh'],2)
        self.assertEqual(result[2]['batches'],1)
        self.assertEqual(self.transport.calls,Counter(c=1,f=1))
        self.assertEqual(self.transport.proofs,Counter(c=1,f=1))

    async def test_ephemeral_clock_and_rating_features_share_cache_and_facts(self):
        base=business_card(org_id='x',features=['кофе с собой: True','Капучино: от 255 ₽','Закрыто навсегда'])
        noisy=business_card(org_id='x',features=['кофе с собой: True','Капучино: от 255 ₽','Закрыто навсегда','9.8','Закрыто до 10:00','До 02:00','Открыто с 08:00'])
        self.assertEqual(ConceptCompetitorClassifier.facts(base),ConceptCompetitorClassifier.facts(noisy))
        first=await self.classifier.classify([base],'Бизнес','Точная концепция','primary',self.transport.infer,
            verify=self.transport.verify,verifier_model='proof')
        second=await self.classifier.classify([noisy],'Бизнес','Точная концепция','primary',self.transport.infer,
            verify=self.transport.verify,verifier_model='proof')
        self.assertEqual(first[2]['classified_fresh'],1)
        self.assertEqual(second[2]['cache_hits'],1)
        self.assertEqual(second[2]['classified_fresh'],0)
        self.assertEqual(self.transport.calls,Counter(x=1))
        self.assertIn('Капучино: от 255 ₽',ConceptCompetitorClassifier.facts(noisy)['features'])
        self.assertIn('Закрыто навсегда',ConceptCompetitorClassifier.facts(noisy)['features'])

    async def test_wire_payload_deduplicates_catalog_without_losing_negative_or_conflict_flags(self):
        captured=[]
        def capture(prompt,tokens,**kw):
            captured.append(json.loads(prompt.split('\nINPUT_JSON\n',1)[1]))
            return self.transport.infer(prompt,tokens,**kw)
        card=business_card(org_id='compact',categories=['Кафе'],features=['Wi-Fi: True','Доставка: False','Кофе: True','Кофе: False'])
        res,_,_=await self.classifier.classify([card],'Кофейня','Точная концепция','primary',capture,
            verify=self.transport.verify,verifier_model='proof')
        self.assertEqual(res['categories'][0]['level'],'direct')
        org=captured[0]['organizations'][0]
        self.assertNotIn('features',org);self.assertNotIn('feature_flags',org);self.assertNotIn('categories',org)
        self.assertEqual(org['offer_evidence_catalog'],{'category:0':'Кафе','feature:0':'Wi-Fi: True'})
        self.assertEqual(org['negative_features'],['Доставка'])
        self.assertEqual(org['conflicting_features'],['Кофе'])

    async def test_prompt_forbids_generic_retail_invention_and_cache_key_tracks_revision(self):
        prompts=[]
        def capture(prompt,tokens,**kw):
            prompts.append(prompt)
            return self.transport.infer(prompt,tokens,**kw)
        card=business_card(org_id='groc',title='Гастроном',categories=['Магазин продуктов'],features=['Wi-Fi: False'])
        await self.classifier.classify([card],'Кофейня','Кофейня с собой и свежей выпечкой','primary',capture,
            verify=self.transport.verify,verifier_model='proof')
        self.assertEqual(len(prompts),1)
        self.assertIn('NEVER guess unmentioned secondary products',prompts[0])
        self.assertIn('unrelated',prompts[0])
        facts=ConceptCompetitorClassifier.facts(card)
        legacy_key=hashlib.sha256(json.dumps(dict(revision=10,business='Кофейня',concept='Кофейня с собой и свежей выпечкой',
            model='primary',verifier_model='proof',verified_contract=True,facts=facts),ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        current_key=ConceptCompetitorClassifier._key(facts,'Кофейня','Кофейня с собой и свежей выпечкой','primary',True,'proof')
        self.assertNotEqual(current_key,legacy_key)
