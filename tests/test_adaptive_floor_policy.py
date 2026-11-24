import asyncio
import contextlib
import copy
import io
import json
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

from real_estate_engine import OptimalPropertyRecommender
from real_estate_engine.floor_policy import (
    BusinessFloorProfile, BusinessFloorProfileResolver, assess_floor, extract_floor_details,
    extract_floor_evidence, floor_facts, floor_evidence_catalog, parse_floor_values,
)
from test_engine_regressions import LAT, LNG, memory_storage, property_offer, competition_report
from floor_ai_fixtures import (
    FixtureFloorAnalyzer, RecordingFloorInference, assessment_response, default_response, profile_response, floor_wire_reply,
)


def prop(floor=1, description="", **extra):
    return {"floor": floor, "title": "Помещение", "description": description, **extra}


class FloorEvidenceTests(unittest.TestCase):
    def test_numerical_levels_are_facts_not_default_permissions(self):
        for value, expected in ((0, (0,)), (-1, (-1,)), ("цоколь", (0,)), ("подвал", (-1,)), ("7.0", (7,)), (250, (250,)), ("7/12", (7,))):
            with self.subTest(value=value):
                self.assertEqual(parse_floor_values(value), expected)

    def test_multiple_levels_preserved(self):
        self.assertEqual(parse_floor_values([1, "2", 0]), (0, 1, 2))
        self.assertEqual(parse_floor_values("1-3"), (1, 2, 3))

    def test_description_recovers_missing_floor(self):
        for text, expected in (("На 7-м этаже", (7,)), ("Офис на 7-м мансардном этаже", (7,)),
                               ("Этаж: цокольный", (0,)), ("На первом этаже", (1,)), ("Этаж: 7/12", (7,))):
            with self.subTest(text=text):
                self.assertEqual(extract_floor_evidence(prop(None, text)).floors, expected)

    def test_building_height_not_offered_floor(self):
        for text in ("7-этажное здание", "В доме 7 этажей", "улица Тестовая, 7", "Этажность здания: 7"):
            self.assertEqual(extract_floor_evidence(prop(None, text)).floors, ())

    def test_negated_basement_does_not_create_conflict(self):
        self.assertFalse(extract_floor_evidence(prop(1, "Первый этаж, не цоколь и не подвал")).conflicting)

    def test_conflict_and_access_survive_truncation(self):
        text = "Лифт. " + "x" * 600 + " на 7 этаже. Лифт не работает"
        metadata = extract_floor_details(1, description=text)
        self.assertFalse(metadata["has_elevator"])
        self.assertTrue(extract_floor_evidence(prop(1, text[:500], raw_json=json.dumps(metadata))).conflicting)

    def test_invalid_floor_not_rounded(self):
        for value in (True, 1.5, "семь", float("nan"), float("inf")):
            self.assertEqual(parse_floor_values(value), ())

    def test_internal_access_not_promoted_to_public_fact(self):
        facts = floor_facts(prop(7, "Сотрудники имеют свободный доступ. Внешние посетители не допускаются."))
        self.assertIsNone(facts["public_access"])

    def test_parser_hints_are_not_confirmed_access_facts(self):
        text = "Сотрудники имеют свободный доступ. " + "x" * 600 + " Внешние посетители не допускаются."
        metadata = extract_floor_details(7, description=text)
        facts = floor_facts(prop(7, text[:500], raw_attributes=metadata))
        self.assertIsNone(facts["public_access"])
        self.assertIn("Внешние посетители не допускаются", facts["access_context"])


class AdaptiveResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_business_is_forwarded_to_joint_inference(self):
        model = RecordingFloorInference()
        resolver = BusinessFloorProfileResolver(model)
        for niche in ("Кофейня", "Квантовая мастерская"):
            profile = await resolver.resolve(niche)
            self.assertEqual(profile.request_clauses, ())
            self.assertIsNone(profile.allowed_floors)
            await resolver.assess_many([prop(1, id=1)], profile)
            self.assertEqual(model.calls[-1]["profile"]["target_business"], niche)
        self.assertEqual(len(model.calls), 4)

    async def test_context_is_free_text_not_a_format_enum(self):
        model = RecordingFloorInference()
        resolver = BusinessFloorProfileResolver(model)
        context = "Временный павильон для сотрудников на закрытой территории"
        profile = await resolver.resolve("Новый бизнес", context)
        self.assertEqual(model.calls, [])
        await resolver.assess_many([prop(1, id=1)], profile)
        self.assertEqual(model.calls[0]["profile"]["business_format"], context)

    async def test_context_changes_invalidate_assessment_cache(self):
        model = RecordingFloorInference()
        resolver = BusinessFloorProfileResolver(model)
        first = await resolver.resolve("Бизнес", "Контекст A")
        await resolver.assess_many([prop(1, id=1)], first)
        await resolver.assess_many([prop(1, id=1)], first)
        self.assertEqual(len(model.calls), 2)
        second = await resolver.resolve("Бизнес", "Контекст B")
        await resolver.assess_many([prop(1, id=1)], second)
        self.assertEqual(len(model.calls), 4)

    async def test_invalid_model_response_has_no_rule_fallback(self):
        for payload in ("not-json", {"assessments": []}):
            resolver = BusinessFloorProfileResolver(RecordingFloorInference(lambda p: payload))
            profile = await resolver.resolve("Кофейня")
            result = await resolver.assess_many([prop(1, id=1)], profile)
            self.assertEqual(result["1"].status, "needs_verification")

    async def test_no_model_does_not_assume_first_floor(self):
        resolver = BusinessFloorProfileResolver()
        profile = await resolver.resolve("Кофейня")
        result = await resolver.assess_many([prop(1)], profile)
        self.assertEqual(next(iter(result.values())).status, "needs_verification")

    async def test_different_levels_without_code_threshold(self):
        resolver = BusinessFloorProfileResolver(RecordingFloorInference())
        profile = await resolver.resolve("Тестовый контекст")
        results = await resolver.assess_many([prop(n, id=i) for i, n in enumerate((0, 1, 7, 14))], profile)
        self.assertTrue(all(r.status == "eligible" for r in results.values()))

    async def test_client_constraints_enforced_before_assessment(self):
        model = RecordingFloorInference()
        resolver = BusinessFloorProfileResolver(model)
        profile = await resolver.resolve("Бизнес", allowed_floors=(2,))
        results = await resolver.assess_many([prop(7, id=1), prop(2, id=2)], profile)
        self.assertEqual(results["1"].status, "rejected")
        self.assertEqual(results["2"].status, "eligible")
        self.assertEqual(len(model.calls[-1]["candidates"]), 1)

    async def test_invalid_explicit_constraints_rejected(self):
        for floors in ((), (True,), (1.5,)):
            with self.subTest(floors=floors), self.assertRaises(ValueError):
                await BusinessFloorProfileResolver().resolve("Бизнес", allowed_floors=floors)

    async def test_empty_query_does_not_invoke_model(self):
        model = RecordingFloorInference()
        await BusinessFloorProfileResolver(model).resolve("Бизнес")
        self.assertEqual(model.calls, [])

    async def test_missing_structured_transport_does_not_call_generic_model(self):
        calls=[]
        def generic(*args):
            calls.append(args)
            raise AssertionError("unexpected fallback")
        resolver = BusinessFloorProfileResolver(SimpleNamespace(
            floor_model="context-model", _call_nvidia_sync=generic))
        profile = await resolver.resolve("Бизнес")
        result = await resolver.assess_many([prop(1, id=1)], profile)
        self.assertEqual(result["1"].status, "needs_verification")
        self.assertFalse(calls)




class GroundingTests(unittest.IsolatedAsyncioTestCase):
    async def decision(self, change):
        def handler(payload):
            if payload["task"] in ("derive_floor_requirements", "review_floor_requirements"):
                return profile_response()
            item = assessment_response(payload["candidates"][0])
            change(item)
            return {"assessments": [item]}
        resolver = BusinessFloorProfileResolver(RecordingFloorInference(handler, review_handler=handler))
        profile = await resolver.resolve("Бизнес", "Контролируемый сценарий посещения из теста")
        return (await resolver.assess_many([prop(7, "Текст объявления", id=1)], profile))["1"]

    async def test_invented_value_cannot_approve(self):
        result = await self.decision(lambda i: i["checks"][0].update(evidence=[{"field":"floor","value":1}]))
        self.assertEqual(result.status, "needs_verification")

    async def test_invented_quote_cannot_approve(self):
        result = await self.decision(lambda i: i["checks"][0].update(evidence=[{"field":"description","quote":"Свободный вход"}]))
        self.assertEqual(result.status, "needs_verification")

    async def test_exact_quote_can_ground_check(self):
        result = await self.decision(lambda i: i["checks"][0].update(evidence=[{"field":"description","quote":"Текст объявления"}]))
        self.assertEqual(result.status, "eligible")

    async def test_unknown_fact_cannot_be_met(self):
        result = await self.decision(lambda i: i["checks"][0].update(evidence=[{"field":"public_access","value":None}]))
        self.assertEqual(result.status, "needs_verification")

    async def test_unknown_critical_check_overrides_eligible(self):
        result = await self.decision(lambda i: i["checks"][0].update(result="unknown", evidence=[]))
        self.assertEqual(result.status, "needs_verification")

    async def test_grounded_critical_failure_overrides_eligible(self):
        result = await self.decision(lambda i: i["checks"][0].update(result="not_met"))
        self.assertEqual(result.status, "rejected")

    async def test_unsubstantiated_rejection_is_uncertain(self):
        result = await self.decision(lambda i: (i.update(status="rejected"), i["checks"][0].update(result="not_met", evidence=[])))
        self.assertEqual(result.status, "needs_verification")

    async def test_missing_or_duplicate_critical_checks_are_uncertain(self):
        for change in (lambda i: i.update(checks=[]), lambda i: i["checks"].append(copy.deepcopy(i["checks"][0]))):
            self.assertEqual((await self.decision(change)).status, "needs_verification")

    async def test_old_list_answer_cannot_create_floor_bonus(self):
        model=RecordingFloorInference(lambda payload:{'assessments':[assessment_response(payload['candidates'][0])]})
        model._call_floor_structured_sync=lambda prompt,max_tokens,**kwargs:(
            json.dumps({'assessments':[{'candidate_id':'c0','fit_score':100}]}),'fixture')
        resolver=BusinessFloorProfileResolver(model)
        profile=await resolver.resolve('Бизнес','Посетители по записи.')
        result=(await resolver.assess_many([prop(1,id=1)],profile))['1']
        self.assertEqual(result.status,'needs_verification')
        self.assertEqual(result.analysis_status,'schema_error')

    async def test_catalog_reference_is_resolved_to_observed_fact(self):
        quote="Текст объявления"
        def handler(payload):
            response=default_response(payload)
            for item,row in zip(payload['candidates'],response['assessments']):
                reference=next(k for k,v in item['evidence_catalog'].items()
                    if v=={"field":"description","quote":quote})
                row['checks'][0].pop('evidence')
                row['checks'][0]['evidence_ids']=[reference]
            return response
        resolver=BusinessFloorProfileResolver(RecordingFloorInference(handler,handler))
        profile=await resolver.resolve('Бизнес','Посетители по записи.')
        result=(await resolver.assess_many([prop(7,quote,id=1)],profile))['1']
        self.assertEqual(result.status, "eligible")
        self.assertEqual(result.checks[0]["evidence"], [{"field": "description", "quote": quote}])

    async def test_nonexistent_catalog_reference_cannot_approve(self):
        result = await self.decision(lambda i: (i["checks"][0].pop("evidence"), i["checks"][0].update(evidence_ids=["invented_fact"])))
        self.assertEqual(result.status, "needs_verification")
        self.assertIn("не подтверждены", result.reason)


class BatchAndPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_cache_changes_with_ad_and_returns_copies(self):
        model = RecordingFloorInference()
        resolver = BusinessFloorProfileResolver(model)
        profile = await resolver.resolve("Бизнес")
        first = await resolver.assess_many([prop(7, "a", id=1)], profile)
        first["1"].reason = "mutated"
        repeated = await resolver.assess_many([prop(7, "a", id=1)], profile)
        self.assertNotEqual(repeated["1"].reason, "mutated")
        self.assertEqual(len(model.calls), 2)
        await resolver.assess_many([prop(7, "b", id=1)], profile)
        self.assertEqual(len(model.calls), 4)

    async def test_partial_batch_preserves_other_candidates(self):
        def handler(payload):
            if payload["task"] in ("derive_floor_requirements", "review_floor_requirements"):
                return profile_response()
            return {"assessments": [assessment_response(payload["candidates"][0])]}
        resolver = BusinessFloorProfileResolver(RecordingFloorInference(handler), batch_size=2)
        profile = await resolver.resolve("Бизнес")
        results = await resolver.assess_many([prop(1, id=1), prop(7, id=2)], profile)
        self.assertEqual(results["1"].status, "eligible")
        self.assertEqual(results["2"].status, "needs_verification")

    async def test_unknown_floor_reaches_context_but_conflicting_floor_does_not(self):
        model = RecordingFloorInference()
        resolver = BusinessFloorProfileResolver(model)
        profile = await resolver.resolve("Бизнес")
        results = await resolver.assess_many([prop(None, id=1), prop(1, "На 7 этаже", id=2)], profile)
        self.assertTrue(model.calls)
        self.assertTrue(all(c['facts']['floor'] is None for call in model.calls for c in call['candidates']))
        self.assertTrue(all(r.status == "needs_verification" for r in results.values()))

    async def test_context_decision_filters_before_environment_analysis(self):
        store, conn = memory_storage()
        self.addCleanup(conn.close)
        store.upsert_batch([property_offer("a", floor=7), property_offer("b", floor=1), property_offer("c", floor=None)])
        calls = []
        class Analyzer(FixtureFloorAnalyzer):
            @staticmethod
            def _call_floor_structured_sync(prompt, max_tokens, *, schema, review=False):
                if json.loads(prompt.split("\nINPUT_JSON\n",1)[1])['task']=='verify_floor_proof':
                    return FixtureFloorAnalyzer._call_floor_structured_sync(prompt,max_tokens,schema=schema,review=review)
                payload = json.loads(prompt.split("\nINPUT_JSON\n", 1)[1])
                result = default_response(payload)
                if payload["task"] in ("assess_floor_suitability", "review_floor_decisions"):
                    for candidate,decision in zip(payload["candidates"],result["assessments"]):
                        if candidate["facts"]["floor"] == 7:
                            decision["checks"][0]["result"] = "not_met"
                return floor_wire_reply(payload,result), "fixture"
            async def analyze_circle(self, **kwargs):
                calls.append(kwargs)
                return competition_report()
        report = await OptimalPropertyRecommender(store, Analyzer()).find_optimal_properties("Произвольная ниша", business_format="Контролируемый сценарий посещения из теста", center_lat=LAT, center_lng=LNG,max_price_month=200000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(report.best_candidate.property_data["external_id"], "b")
        self.assertEqual(len(report.floor_rejected_candidates), 1)
        self.assertEqual(len(report.floor_verification_candidates), 1)
        json.dumps(asdict(report), default=str)

    async def test_cli_free_context_and_uncertainty(self):
        import run_engine
        store, conn = memory_storage()
        self.addCleanup(conn.close)
        store.upsert_property(property_offer())
        engine = OptimalPropertyRecommender(store, None)
        aggregator = SimpleNamespace(storage=store, find_optimal_properties=engine.find_optimal_properties)
        stream = io.StringIO()
        with patch.object(run_engine, "RealEstateAggregator", return_value=aggregator), \
                patch("sys.argv", ["run_engine.py", "--skip-collect", "--niche", "Новый бизнес", "--business-format", "Свободный контекст"]), \
                contextlib.redirect_stdout(stream):
            await run_engine.main()
        self.assertIn("Автоматических рекомендаций нет", stream.getvalue())
        self.assertNotIn("МЕСТО #1", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
