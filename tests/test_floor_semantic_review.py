"""A real source sentence can still fail to support a model's conclusion."""
import copy
import unittest

from real_estate_engine.floor_policy import BusinessFloorProfileResolver
from floor_ai_fixtures import RecordingFloorInference, default_response, supported_review_response


def offer(i=1):
    return {"id":i,"floor":2,"title":"Офис 75 м²", "description":
        "Сдаётся под офис блок на втором этаже. 10 минут пешком от метро. Есть охрана."}


def unsupported_access(payload):
    response = default_response(payload)
    for decision in response["assessments"]:
        decision["reason"] = "Если покупатели уже внутри, они смогут прийти"
        next(c for c in decision['checks'] if c['requirement_id']=='audience_access').update(
            result='met',evidence=[{"field":"description","quote":payload['candidates'][0]['facts'].get('source_context',payload['candidates'][0]['facts'])['description']}])
    return response


class SemanticReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_proven_mandatory_violation_rejects_despite_unrelated_disagreement(self):
        def primary(payload):
            answer=default_response(payload)
            next(c for c in answer['assessments'][0]['checks'] if c['requirement_id']=='route_to_unit')['result']='not_met'
            return answer
        def reviewer(payload):
            answer=supported_review_response(payload)
            for check in answer['assessments'][0]['checks']:
                if check['requirement_id'] in ('audience_access','route_to_unit'):check['result']='not_met'
            return answer
        model=RecordingFloorInference(primary,reviewer);resolver=BusinessFloorProfileResolver(model)
        result=(await resolver.assess_many([offer()],await resolver.resolve('Бизнес','Посетители приходят по записи')))['1']
        self.assertEqual(result.status,'rejected')
        self.assertEqual(result.inference_metadata['agreed_rejection_ids'],['route_to_unit'])
        self.assertEqual({c['requirement_id'] for p in model.proof_calls for c in p['claims']},{'route_to_unit'})

    async def test_different_unshared_violations_do_not_establish_rejection_consensus(self):
        def primary(payload):
            answer=default_response(payload);answer['assessments'][0]['checks'][0]['result']='not_met';return answer
        def reviewer(payload):
            answer=supported_review_response(payload)
            next(c for c in answer['assessments'][0]['checks'] if c['requirement_id']=='route_to_unit')['result']='not_met'
            return answer
        model=RecordingFloorInference(primary,reviewer);resolver=BusinessFloorProfileResolver(model)
        result=(await resolver.assess_many([offer()],await resolver.resolve('Бизнес','Посетители приходят по записи')))['1']
        self.assertEqual(result.status,'needs_verification');self.assertFalse(model.proof_calls)

    async def test_unconfirmed_shared_violation_cannot_be_rescued_by_an_unshared_one(self):
        def primary(payload):
            answer=default_response(payload)
            for c in answer['assessments'][0]['checks']:
                if c['requirement_id'] in ('audience_access','route_to_unit'):c['result']='not_met'
            return answer
        def reviewer(payload):
            answer=supported_review_response(payload)
            next(c for c in answer['assessments'][0]['checks'] if c['requirement_id']=='route_to_unit')['result']='not_met'
            return answer
        model=RecordingFloorInference(primary,reviewer)
        model.proof_handler=lambda payload:dict(verdict='unknown' if any(c['requirement_id']=='route_to_unit' for c in payload['claims']) else 'supported',reason='Контроль доказательства')
        resolver=BusinessFloorProfileResolver(model)
        result=(await resolver.assess_many([offer()],await resolver.resolve('Бизнес','Посетители приходят по записи')))['1']
        self.assertEqual(result.status,'needs_verification')
        self.assertEqual({c['requirement_id'] for p in model.proof_calls for c in p['claims']},{'route_to_unit'})

    async def test_independent_reviewer_cannot_see_first_decision(self):
        model=RecordingFloorInference()
        resolver=BusinessFloorProfileResolver(model)
        await resolver.assess_many([offer()],await resolver.resolve("Бизнес"))
        reviews=[c for c in model.calls if c['task']=='review_floor_decisions']
        self.assertTrue(reviews)
        for candidate in reviews[0]['candidates']:
            self.assertNotIn('assessment',candidate)
            self.assertNotIn('reason',candidate)
            self.assertEqual(set(candidate),{'candidate_id','facts','evidence_catalog','requirements'})

    async def test_independent_disagreement_cannot_replace_rejection_with_approval(self):
        def reject(payload):
            result=default_response(payload)
            result['assessments'][0]['checks'][0]['result']='not_met'
            return result
        resolver=BusinessFloorProfileResolver(RecordingFloorInference(reject))
        result=(await resolver.assess_many([offer()],await resolver.resolve('Бизнес','Посещение по записи')))['1']
        self.assertEqual(result.status,'needs_verification')
        self.assertEqual(result.independent_assessment['status'],'eligible')

    async def test_invalid_json_retried_once_without_using_partial_answer(self):
        model=RecordingFloorInference()
        original=model._call_floor_structured_sync
        count=0
        def infer(prompt,max_tokens,**kwargs):
            nonlocal count
            count+=1
            if count==1: return '{"assessments":[', 'fixture'
            return original(prompt,max_tokens,**kwargs)
        model._call_floor_structured_sync=infer
        resolver=BusinessFloorProfileResolver(model)
        result=(await resolver.assess_many([offer()],await resolver.resolve('Бизнес')))['1']
        self.assertEqual(result.status,'eligible')
        self.assertEqual(count,4)

    async def test_niche_name_alone_cannot_make_inferred_rejection_final(self):
        def reject(payload):
            result = default_response(payload)
            result["assessments"][0]["checks"][0]["result"] = "not_met"
            result["assessments"][0]["status"] = "rejected"
            return result
        resolver = BusinessFloorProfileResolver(RecordingFloorInference(reject))
        result = (await resolver.assess_many([offer()],await resolver.resolve("Произвольная ниша")))["1"]
        self.assertEqual(result.status,"needs_verification")
        self.assertIn("сценарий посещения",result.reason)

    async def test_refuted_access_conclusion_cannot_approve(self):
        def reviewer(payload):
            result = supported_review_response(payload)
            result["assessments"][0]["status"] = "needs_verification"
            result["assessments"][0]["checks"][0].update(result='unknown',evidence=[])
            result["assessments"][0]["reason"] = "Близость метро не подтверждает доступ покупателей на этаж"
            return result
        model = RecordingFloorInference(unsupported_access, reviewer)
        resolver = BusinessFloorProfileResolver(model)
        profile = await resolver.resolve("Кофейня")
        result = (await resolver.assess_many([offer()],profile))["1"]
        self.assertEqual(result.status,"needs_verification")
        self.assertEqual(result.semantic_review_status,"unsupported")
        self.assertIn("не подтверждает",result.reason)

    async def test_unsupported_rejection_is_also_deferred(self):
        def reject(payload):
            result = unsupported_access(payload)
            result["assessments"][0]["checks"][0]["result"] = "not_met"
            return result
        def reviewer(payload):
            result = supported_review_response(payload)
            result["assessments"][0]["status"] = "needs_verification"
            result["assessments"][0]["checks"][0].update(result='unknown',evidence=[])
            return result
        resolver = BusinessFloorProfileResolver(RecordingFloorInference(reject,reviewer))
        result = (await resolver.assess_many([offer()],await resolver.resolve("Бизнес")))["1"]
        self.assertEqual(result.status,"needs_verification")

    async def test_review_unavailable_does_not_approve_or_cache_transport_failure(self):
        def failure(payload):
            raise RuntimeError("fixture")
        model = RecordingFloorInference(review_handler=failure)
        resolver = BusinessFloorProfileResolver(model)
        profile = await resolver.resolve("Бизнес")
        result = (await resolver.assess_many([offer()],profile))["1"]
        self.assertEqual(result.semantic_review_status,"unavailable")
        self.assertEqual(result.status,"needs_verification")
        model.review_handler = supported_review_response
        retried = (await resolver.assess_many([offer()],profile))["1"]
        self.assertEqual(retried.status,"eligible")
        self.assertEqual(retried.semantic_review_status,"supported")

    async def test_missing_or_duplicate_reviews_do_not_approve(self):
        for mode in ("missing","duplicate","checks_missing"):
            def reviewer(payload):
                result = supported_review_response(payload)
                if mode=="missing": result["assessments"] = []
                elif mode=="duplicate": result["assessments"] *= 2
                else: result["assessments"][0]["checks"] = []
                return result
            resolver = BusinessFloorProfileResolver(RecordingFloorInference(review_handler=reviewer))
            with self.subTest(mode=mode):
                result = (await resolver.assess_many([offer()],await resolver.resolve("Бизнес")))["1"]
                self.assertEqual(result.status,"needs_verification")

    async def test_partial_review_preserves_other_candidate(self):
        def reviewer(payload):
            result = supported_review_response(payload)
            if payload['candidates'][0]['facts']['floor']==3:result['assessments']=[]
            return result
        resolver = BusinessFloorProfileResolver(RecordingFloorInference(review_handler=reviewer),batch_size=2)
        second=offer(2);second.update(floor=3,description=second['description'].replace('втором','третьем'))
        results = await resolver.assess_many([offer(1),second],await resolver.resolve("Бизнес"))
        self.assertEqual(results["1"].status,"eligible")
        self.assertEqual(results["2"].status,"needs_verification")

    async def test_ambiguous_context_or_missing_critical_fact_prevents_approval(self):
        for field in ('audience_access','route_to_unit'):
            def reviewer(payload):
                result = supported_review_response(payload)
                next(c for c in result['assessments'][0]['checks'] if c['requirement_id']==field).update(result='unknown',evidence=[])
                return result
            resolver = BusinessFloorProfileResolver(RecordingFloorInference(review_handler=reviewer))
            result = (await resolver.assess_many([offer()],await resolver.resolve("Бизнес")))["1"]
            self.assertEqual(result.status,"needs_verification")

    async def test_non_visitor_delivery_scenario_instructions_prevent_false_audience_not_met(self):
        prompts = []
        base = RecordingFloorInference()
        orig = base._call_floor_structured_sync
        def capture(prompt, max_tokens, **kwargs):
            prompts.append(prompt)
            return orig(prompt, max_tokens, **kwargs)
        base._call_floor_structured_sync = capture
        resolver = BusinessFloorProfileResolver(base)
        profile = await resolver.resolve(
            "Дарк-китчен на доставку",
            "Без приёма посетителей; вход с улицы не нужен, курьеры забирают заказы",
            context_quotes=("Без приёма посетителей",),
            waiver_quotes=("вход с улицы не нужен",),
            requirement_quotes=("курьеры забирают заказы",),
        )
        prop = {
            "id": 63,
            "floor": 1,
            "title": "Помещение 80 м², отдельный вход с улицы",
            "description": "Свободный вход с улицы 24/7, первый этаж, без пропускной системы.",
        }
        result = (await resolver.assess_many([prop], profile))["63"]
        self.assertEqual(result.status, "eligible")
        review_prompts = [p for p in prompts if '"task": "review_floor_decisions"' in p]
        self.assertTrue(review_prompts)
        self.assertIn("не ставь not_met", review_prompts[0])
        self.assertIn("met, а НЕ not_met", prompts[0])


if __name__ == "__main__":
    unittest.main()
