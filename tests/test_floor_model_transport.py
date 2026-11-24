import json
import unittest
from types import SimpleNamespace

from real_estate_engine import NvidiaCompetitorAnalyzer


class FloorModelTransportTests(unittest.TestCase):
    def test_classification_uses_explicit_model_without_fallback(self):
        analyzer,calls=self.analyzer(200);analyzer.classification_model='classification-model'
        _,used=analyzer._call_competitor_structured_sync('test',100,response_schema={})
        self.assertEqual(used,'classification-model');self.assertEqual(len(calls),1)

    def test_classification_reasoning_has_its_own_bounded_token_budget(self):
        analyzer,calls=self.analyzer(200);analyzer.classification_model='nvidia/nemotron-fixture'
        analyzer.classification_reasoning_budget=768
        analyzer._call_competitor_structured_sync('test',2200,response_schema={})
        payload=calls[0]['json']
        self.assertEqual(payload['chat_template_kwargs'],{'enable_thinking':True,'reasoning_budget':768})
        self.assertEqual(payload['max_tokens'],2968)
        calls.clear();analyzer._call_floor_structured_sync('test', 1200, {'type':'object'})
        self.assertNotIn('reasoning_budget',calls[0]['json'].get('chat_template_kwargs',{}))

    def test_offer_proof_has_explicit_stronger_model_and_bounded_json_mode(self):
        analyzer,calls=self.analyzer(200);analyzer.classification_proof_model='proof-model'
        _,used=analyzer._call_competitor_proof_structured_sync('test',1800,response_schema={'type':'object'})
        self.assertEqual(used,'proof-model');self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['json']['reasoning_effort'],'low')
        self.assertEqual(calls[0]['json']['response_format'],{'type':'json_object'})
        self.assertEqual(calls[0]['json']['temperature'],0.1)
        self.assertIn('OUTPUT_JSON_SCHEMA',calls[0]['json']['messages'][1]['content'])
    def test_structured_competitors_do_not_silently_switch_models(self):
        analyzer,calls=self.analyzer(500)
        with self.assertRaises(RuntimeError):analyzer._call_competitor_structured_sync('test',100,response_schema={})
        self.assertEqual(len(calls),1)
        self.assertAlmostEqual(calls[0]['timeout'],45,delta=0.5)

    def test_default_two_model_config_keeps_primary_and_verifier_independent(self):
        import os
        from unittest.mock import patch
        from real_estate_engine.competitors import NvidiaCompetitorAnalyzer
        env={k:v for k,v in os.environ.items() if not k.startswith('NVIDIA_')}
        with patch.dict(os.environ,env,clear=True):
            analyzer=NvidiaCompetitorAnalyzer(api_key='test-key')
        lightning='nvidia/nemotron-3.5-lightning-30b-a3b'
        super_vl='nvidia/nemotron-3.5-super-vl-120b-a12b'
        self.assertEqual(analyzer.model,lightning)
        self.assertEqual(analyzer.floor_model,lightning)
        self.assertEqual(analyzer.classification_model,lightning)
        self.assertEqual(analyzer.property_assessment_model,lightning)
        self.assertEqual(analyzer.floor_review_model,super_vl)
        self.assertEqual(analyzer.classification_proof_model,super_vl)
        self.assertEqual(analyzer.property_proof_model,super_vl)
        self.assertNotEqual(analyzer.floor_model,analyzer.floor_review_model)
        self.assertNotEqual(analyzer.classification_model,analyzer.classification_proof_model)
        self.assertNotEqual(analyzer.property_assessment_model,analyzer.property_proof_model)

    def test_nemotron_property_assessment_starts_with_json_object(self):
        from real_estate_engine.competitors import NvidiaCompetitorAnalyzer
        calls=[]
        def post(url,**kwargs):
            from types import SimpleNamespace
            import json
            calls.append(kwargs)
            return SimpleNamespace(status_code=200,content=json.dumps(
                {'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]}).encode())
        analyzer=NvidiaCompetitorAnalyzer(api_key='test-key')
        analyzer.http_session=SimpleNamespace(post=post)
        _,used=analyzer._call_property_assessment_structured_sync('check',1600,response_schema={'type':'object'})
        self.assertEqual(used,'nvidia/nemotron-3.5-lightning-30b-a3b')
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['json']['response_format'],{'type':'json_object'})
    def analyzer(self, status):
        calls = []
        def post(url, **kwargs):
            calls.append(kwargs)
            payload = {"choices": [{"finish_reason":"stop", "message": {"content": '{"ok":true}'}}]}
            return SimpleNamespace(status_code=status, content=json.dumps(payload).encode(), text="controlled response")
        analyzer = NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.model = "legacy-model"
        analyzer.floor_model = "configured-context-model"
        analyzer.floor_review_model = "configured-review-model"
        analyzer.endpoint = "https://example.test/inference"
        analyzer.http_session = SimpleNamespace(post=post)
        return analyzer, calls

    def test_context_call_uses_configured_model_and_own_timeout(self):
        analyzer, calls = self.analyzer(200)
        _, used = analyzer._call_floor_structured_sync("test", 1200, {'type':'object'})
        self.assertEqual(used, "configured-context-model")
        self.assertEqual(calls[0]["json"]["model"], "configured-context-model")
        self.assertAlmostEqual(calls[0]["timeout"], 45, delta=0.5)

    def test_context_model_failure_does_not_silently_switch_models(self):
        analyzer, calls = self.analyzer(500)
        with self.assertRaises(RuntimeError):
            analyzer._call_floor_structured_sync("test", 1200, {'type':'object'})
        self.assertEqual(len(calls), 1)

    def test_review_uses_separate_configured_model(self):
        analyzer, calls = self.analyzer(200)
        _, used = analyzer._call_floor_structured_sync("test", 2800, {'type':'object'}, review=True)
        self.assertEqual(used, "configured-review-model")
        self.assertEqual(calls[0]["json"]["model"], "configured-review-model")
        self.assertAlmostEqual(calls[0]["timeout"], 45, delta=0.5)

    def test_review_failure_has_no_weak_model_fallback(self):
        analyzer, calls = self.analyzer(500)
        with self.assertRaises(RuntimeError):
            analyzer._call_floor_structured_sync("test", 2800, {'type':'object'}, review=True)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
