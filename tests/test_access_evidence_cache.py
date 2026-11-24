"""Citation retrieval and reuse must not bypass independent proof guards."""
import copy
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from contextlib import closing
from pathlib import Path

from floor_ai_fixtures import RecordingFloorInference, default_response
from real_estate_engine.floor_policy import (
    BusinessFloorProfile, BusinessFloorProfileResolver, access_evidence_scope,
    floor_evidence_catalog, floor_facts)


PROFILE = BusinessFloorProfile(business_format='Клиенты приходят по записи', status='ready',
                               scenario_context_quotes=('Клиенты приходят по записи',))
OFFER = dict(id=80, floor=1, title='ПСН 58 м² у метро', description=(
    'Светлая локация с высоким трафиком.\n'
    'Первый этаж, отдельный вход через открытую арку со двора.'))


class SourceScopeTests(unittest.TestCase):
    def test_marketing_excluded_while_exact_access_line_preserved(self):
        catalog = floor_evidence_catalog(floor_facts(OFFER))
        refs = [catalog[k] for k in access_evidence_scope(catalog)]
        self.assertEqual(refs, [dict(field='description', quote=OFFER['description'].splitlines()[1])])

    def test_negative_rules_and_steps_are_retained(self):
        facts = floor_facts({**OFFER, 'description':
            'Посетителям нельзя. Только сотрудники.\nК двери ведут ступени.\nНизкая аренда.'})
        catalog = floor_evidence_catalog(facts)
        quotes = [catalog[k]['quote'] for k in access_evidence_scope(catalog)]
        self.assertEqual(len(quotes), 2)
        self.assertIn('Посетителям нельзя. Только сотрудники.', quotes)

    def test_unknown_wording_is_not_silently_removed(self):
        catalog = floor_evidence_catalog(floor_facts({**OFFER, 'description': 'Необычная формулировка.'}))
        self.assertEqual(set(access_evidence_scope(catalog)),
                         {k for k,v in catalog.items() if v['field'] in ('title', 'description')})

    def test_access_flag_does_not_hide_unrecognized_route_wording(self):
        catalog = floor_evidence_catalog(dict(public_access=True, description='Общий вестибюль.'))
        self.assertEqual(set(access_evidence_scope(catalog)), set(catalog))

    def test_native_schema_scope_is_also_validated_locally(self):
        model = RecordingFloorInference()
        model._call_floor_structured_sync = lambda *a, **kw: None
        resolver = BusinessFloorProfileResolver(model)
        payload = resolver._candidate_payload(OFFER, PROFILE, 'c0')
        response = default_response(dict(task='assess_floor_suitability', candidates=[payload]))['assessments'][0]
        marketing = dict(field='description', quote=OFFER['description'].splitlines()[0])
        for c in response['checks']:
            if c['requirement_id'] == 'audience_access': c['evidence'] = [marketing]
        result = resolver._validate_primary(response, OFFER, PROFILE)
        self.assertEqual(result.status, 'needs_verification')
        self.assertIn('audience_access', result.contract_issues)


class VerifiedCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'assessments.sqlite3'

    def resolver(self, model=None, **kwargs):
        return BusinessFloorProfileResolver(model or RecordingFloorInference(),
            assessment_cache_path=self.path, **kwargs)

    async def assess(self, resolver, prop=OFFER, profile=PROFILE):
        return (await resolver.assess_many([copy.deepcopy(prop)], profile))[str(prop['id'])]

    async def test_reuses_verified_decision_across_resolver_instances(self):
        first = self.resolver()
        result = await self.assess(first)
        self.assertEqual(result.status, 'eligible')
        second = self.resolver()
        cached = await self.assess(second)
        self.assertEqual(cached.status, 'eligible')
        self.assertEqual(cached.inference_metadata['assessment_cache'], dict(hit=True, source='disk'))
        self.assertFalse(second.analyzer.calls)
        self.assertFalse(second.analyzer.proof_calls)

    async def test_changed_source_scenario_or_model_requires_new_inference(self):
        await self.assess(self.resolver())
        variations = [({**OFFER, 'description': OFFER['description']+' Изменение.'}, PROFILE, 'fixture-model'),
                      (OFFER, replace(PROFILE, business_format='Иная аудитория'), 'fixture-model'),
                      (OFFER, PROFILE, 'new-model')]
        for prop, profile, model_name in variations:
            model = RecordingFloorInference(); model.model = model_name
            await self.assess(self.resolver(model), prop, profile)
            self.assertTrue(model.calls)

    async def test_unproven_decision_is_not_cached_and_can_recover(self):
        model = RecordingFloorInference()
        model.proof_handler = lambda p: dict(verdict='unknown', reason='Нет доказательства')
        resolver = self.resolver(model)
        self.assertEqual((await self.assess(resolver)).status, 'needs_verification')
        self.assertFalse(resolver._assessment_cache)
        model.proof_handler = lambda p: dict(verdict='supported', reason='Контрольный ответ')
        self.assertEqual((await self.assess(resolver)).status, 'eligible')
        self.assertEqual(len([c for c in model.calls if c['task']=='assess_floor_suitability']), 2)

    async def test_different_listing_identity_does_not_borrow_cached_confirmation(self):
        await self.assess(self.resolver(), {**OFFER, 'url':'https://example.test/original'})
        resolver = self.resolver()
        await self.assess(resolver, {**OFFER, 'url':'https://example.test/replacement'})
        self.assertTrue(resolver.analyzer.calls)

    async def test_expired_entry_is_not_reused(self):
        await self.assess(self.resolver())
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute('UPDATE assessments SET expires=0')
        resolver = self.resolver()
        await self.assess(resolver)
        self.assertTrue(resolver.analyzer.calls)

    async def test_corrupt_optional_cache_does_not_prevent_fresh_assessment(self):
        self.path.write_text('invalid database', encoding='utf-8')
        resolver = self.resolver()
        self.assertEqual((await self.assess(resolver)).status, 'eligible')
        self.assertTrue(resolver.analyzer.calls)

    async def test_payload_without_complete_proof_is_not_trusted(self):
        await self.assess(self.resolver())
        with closing(sqlite3.connect(self.path)) as conn, conn:
            key, payload = conn.execute('SELECT key,payload FROM assessments').fetchone()
            value = json.loads(payload); value['inference_metadata']['proof_verifications'] = []
            conn.execute('UPDATE assessments SET payload=? WHERE key=?', (json.dumps(value), key))
        resolver = self.resolver()
        await self.assess(resolver)
        self.assertTrue(resolver.analyzer.calls)

    async def test_source_conflict_is_not_persisted(self):
        resolver = self.resolver()
        result = await self.assess(resolver, {**OFFER, 'description': 'Цоколь, отдельный вход.'})
        self.assertEqual(result.analysis_status, 'source_conflict')
        self.assertFalse(self.path.exists())

    async def test_corrupt_cached_evidence_is_revalidated_against_source(self):
        await self.assess(self.resolver())
        with closing(sqlite3.connect(self.path)) as conn, conn:
            key, payload = conn.execute('SELECT key,payload FROM assessments').fetchone()
            value = json.loads(payload)
            value['checks'][1]['evidence'] = [{'field':'description','quote':'Invented source evidence'}]
            conn.execute('UPDATE assessments SET payload=? WHERE key=?', (json.dumps(value),key))
        resolver = self.resolver()
        await self.assess(resolver)
        self.assertTrue(resolver.analyzer.calls)


if __name__ == '__main__':
    unittest.main()
