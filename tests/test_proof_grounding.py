import asyncio
import copy
import unittest
from dataclasses import asdict

from real_estate_engine.floor_policy import BusinessFloorProfileResolver, floor_facts, floor_evidence_catalog
from real_estate_engine.proof_grounding import measurement_dimensions, source_fragments
from floor_ai_fixtures import RecordingFloorInference, assessment_response, default_response, supported_review_response
from floor_ai_fixtures import FixtureFloorAnalyzer
from real_estate_engine import OptimalPropertyRecommender, ClassifiedCompetitor, CompetitionLevel
from real_estate_engine.ai_contract import floor_response_schema
from test_engine_regressions import memory_storage, property_offer, competition_report, business_card, LAT, LNG


class ProofGroundingTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_explicit_visitor_condition_rejects_floor_only_proof(self):
        resolver=BusinessFloorProfileResolver()
        prop=dict(id=359,floor=1,title='Помещение',description='Отдельный вход с улицы, доступ 24/7.')
        for context in ('Клиенты приезжают по записи','Посетители приходят без записи'):
            profile=await resolver.resolve('Произвольный бизнес',context)
            item=dict(candidate_id='359',facts=floor_facts(prop),requirements=resolver.required_checks(profile))
            response=assessment_response(item)
            next(c for c in response['checks'] if c['requirement_id']=='request_0')['evidence']=[{'field':'offered_floors','value':[1]}]
            decision=resolver._validate_assessment(response,prop,profile)
            self.assertEqual(decision.status,'needs_verification')
            self.assertIn('request_0',decision.contract_issues)

    async def test_undocumented_physical_quantity_is_unknown_and_not_cached_as_verified(self):
        model=RecordingFloorInference()
        resolver=BusinessFloorProfileResolver(model)
        profile=await resolver.resolve('Неизвестная ниша','Паллеты массой 1000 кг доставляются до помещения')
        prop=dict(id=1,floor=2,title='Склад',description='Дебаркадер и грузовые лифты на каждый этаж.')
        first=(await resolver.assess_many([prop],profile))['1']
        self.assertEqual(first.status,'needs_verification')
        self.assertEqual(first.analysis_status,'unsupported_evidence')
        self.assertIn('request_0',first.contract_issues)
        calls=len(model.calls)
        self.assertEqual(asdict(first),asdict((await resolver.assess_many([prop],profile))['1']))
        self.assertGreater(len(model.calls),calls)
        self.assertFalse(resolver._assessment_cache)

    async def test_documented_capacity_does_not_require_exact_same_number_or_unit(self):
        for text in ('Грузовой лифт 2000 кг, паллета проходит до помещения.',
                     'Грузовой лифт грузоподъёмностью 2 тонны, паллета проходит до помещения.'):
            def primary(payload):
                response=default_response(payload)
                for c in response['assessments'][0]['checks']:
                    if c['requirement_id']=='request_0':c['evidence']=[{'field':'description','quote':text}]
                return response
            resolver=BusinessFloorProfileResolver(RecordingFloorInference(primary))
            profile=await resolver.resolve('Новая ниша','Паллеты массой 1000 кг доставляются до помещения')
            result=(await resolver.assess_many([dict(id=1,floor=2,title='Склад',description=text)],profile))['1']
            self.assertEqual(result.status,'eligible')

    async def test_explicit_impossibility_can_reject_without_a_numeric_capacity(self):
        def handler(payload):
            response=default_response(payload)
            next(c for c in response['assessments'][0]['checks'] if c['requirement_id']=='request_0')['result']='not_met'
            return response
        resolver=BusinessFloorProfileResolver(RecordingFloorInference(handler,handler))
        profile=await resolver.resolve('Новая ниша','Паллеты массой 1000 кг доставляются до помещения')
        result=(await resolver.assess_many([dict(id=1,floor=2,title='Склад',description='Единственный путь — лестница. Провоз паллет запрещён.')],profile))['1']
        self.assertEqual(result.status,'rejected')

    async def test_reviewer_cannot_borrow_another_conditions_evidence(self):
        text='Клиентам вход разрешён. До помещения ведёт коридор.'
        def primary(payload):
            response=default_response(payload)
            for c in response['assessments'][0]['checks']:
                if c['requirement_id']=='audience_access':c['evidence']=[{'field':'description','quote':'Клиентам вход разрешён.'}]
                if c['requirement_id']=='route_to_unit':c['evidence']=[{'field':'description','quote':'До помещения ведёт коридор.'}]
            return response
        def review(payload):
            response=supported_review_response(payload)
            for c in response['assessments'][0]['checks']:
                if c['requirement_id']=='audience_access':c['evidence']=[{'field':'description','quote':'До помещения ведёт коридор.'}]
            return response
        resolver=BusinessFloorProfileResolver(RecordingFloorInference(primary,review))
        result=(await resolver.assess_many([dict(id=1,floor=1,title='Помещение',description=text)],await resolver.resolve('Бизнес')))['1']
        self.assertEqual(result.status,'needs_verification')
        self.assertEqual(result.analysis_status,'unsupported_evidence')

    async def test_reviewer_receives_only_cited_fragments_without_first_reason(self):
        model=RecordingFloorInference();resolver=BusinessFloorProfileResolver(model)
        prop=dict(id=1,floor=1,title='Помещение',description='Вход с улицы.\nСЕКРЕТНОЕ НЕДОКАЗАННОЕ ДОПУЩЕНИЕ')
        def primary(payload):
            response=default_response(payload)
            for c in response['assessments'][0]['checks']:
                if c['requirement_id'] in ('audience_access','route_to_unit'):
                    c['evidence']=[{'field':'description','quote':'Вход с улицы.'}]
            return response
        model.handler=primary
        await resolver.assess_many([prop],await resolver.resolve('Бизнес'))
        review=next(p for p in model.calls if p['task']=='review_floor_decisions')
        candidate=review['candidates'][0]
        self.assertNotIn('assessment',candidate)
        self.assertNotIn('reason',candidate)
        self.assertNotIn('НЕДОКАЗАННОЕ',str(candidate['facts']['source_fragments']))
        self.assertNotIn('НЕДОКАЗАННОЕ',str(candidate['evidence_catalog']))
        self.assertIn('НЕДОКАЗАННОЕ',str(candidate['facts']['source_context']))
        self.assertTrue(all('evidence_scope' in r for r in candidate['requirements']))

    async def test_real_floor_only_request_can_use_numeric_floor_but_cannot_lie_about_it(self):
        resolver=BusinessFloorProfileResolver();profile=await resolver.resolve('Бизнес','Только 7-й этаж')
        prop=dict(id=1,floor=7,title='Вход с улицы')
        item=dict(candidate_id='1',facts=floor_facts(prop),requirements=resolver.required_checks(profile))
        response=assessment_response(item)
        next(c for c in response['checks'] if c['requirement_id']=='request_0')['evidence']=[dict(field='floor',value=7)]
        self.assertEqual(resolver._validate_assessment(response,prop,profile).status,'eligible')
        other_profile=await resolver.resolve('Бизнес','Только 2-й этаж')
        changed=copy.deepcopy(response)
        explicit=next(c for c in changed['checks'] if c['requirement_id']=='request_0')
        explicit.update(description='Только 2-й этаж',request_quote='Только 2-й этаж')
        self.assertEqual(resolver._validate_assessment(changed,prop,other_profile).status,'needs_verification')

    async def test_generation_cannot_waive_positive_or_mixed_client_conditions(self):
        resolver=BusinessFloorProfileResolver()
        profile=await resolver.resolve('Бизнес','Клиенты по записи; уличный поток не нужен; вход с улицы не нужен, нужен коридор')
        prop=dict(floor=7,title='Помещение',description='Клиентам разрешён вход по пропуску, лифт и коридор до двери.')
        candidate=dict(candidate_id='c0',facts=floor_facts(prop),evidence_catalog=floor_evidence_catalog(floor_facts(prop)),requirements=resolver.required_checks(profile))
        checks=floor_response_schema([candidate])['properties']['assessments']['properties']['c0']['properties']['checks']['properties']
        self.assertNotIn('not_required',checks['request_0']['properties']['result']['enum'])
        self.assertEqual(checks['request_1']['properties']['result']['enum'],['not_required'])
        self.assertEqual(checks['request_1']['properties']['evidence_ids']['maxItems'],0)
        self.assertNotIn('not_required',checks['request_2']['properties']['result']['enum'])

    async def test_budget_keeps_completed_results_and_never_leaks_unreviewed_approval(self):
        class DelayedResolver(BusinessFloorProfileResolver):
            async def _infer(self,task,instructions,data,max_tokens):
                if task=='review_floor_decisions' and data['candidates'][0]['facts']['floor']==2:
                    await asyncio.Event().wait()
                return await super()._infer(task,instructions,data,max_tokens)
        resolver=DelayedResolver(RecordingFloorInference())
        profile=await resolver.resolve('Бизнес')
        props=[dict(id=n,floor=n,title='Вход с улицы') for n in (1,2)]
        result=await resolver.assess_many(props,profile,timeout_s=.1)
        self.assertEqual(result['1'].status,'eligible')
        self.assertEqual(result['2'].status,'needs_verification')
        self.assertEqual(result['2'].analysis_status,'budget_exceeded')

    async def test_budget_keeps_a_finished_candidate_inside_an_unfinished_batch(self):
        class DelayedResolver(BusinessFloorProfileResolver):
            async def _verify_positive_proofs(self,items,results,profile):
                failures=set()
                for prop,key in items:
                    if prop['floor']==2:await asyncio.Event().wait()
                    failures.update(await super()._verify_positive_proofs([(prop,key)],results,profile))
                return failures
        resolver=DelayedResolver(RecordingFloorInference(),batch_size=4)
        profile=await resolver.resolve('Бизнес')
        props=[dict(id=n,floor=n,title='Вход с улицы') for n in (1,2)]
        result=await resolver.assess_many(props,profile,timeout_s=.1)
        self.assertEqual(result['1'].status,'eligible')
        self.assertEqual(result['2'].analysis_status,'budget_exceeded')
        self.assertEqual(len(resolver._assessment_cache),1)


class SourceFragmentTests(unittest.TestCase):
    def test_decimal_measurements_and_abbreviations_are_not_broken(self):
        text='Площадь 33.5 м², проём 1.2 м. Рядом м. Окружная.'
        self.assertEqual(source_fragments(text),[text])
        catalog=floor_evidence_catalog(dict(description=text))
        self.assertEqual(list(catalog.values()),[dict(field='description',quote=text)])

    def test_quantity_dimensions_require_real_units(self):
        self.assertEqual(measurement_dimensions('Паллеты 1 000 кг'),{'mass'})
        self.assertEqual(measurement_dimensions('2 тонны'),{'mass'})
        self.assertEqual(measurement_dimensions('Проём 100 см'),{'length'})
        self.assertEqual(measurement_dimensions('Площадь 100 м², 1 этаж'),set())


class SearchBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.storage,conn=memory_storage();self.addCleanup(conn.close)
        self.storage.upsert_batch([property_offer('fast',lat=LAT),property_offer('slow',lat=LAT+.001)])
        self.kwargs=dict(center_lat=LAT,center_lng=LNG,search_radius_m=1000,business_concept='Приём по записи.',max_price_month=200000)

    async def test_environment_deadline_preserves_completed_candidate(self):
        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self,**kw):
                if kw['center_lat']>LAT:await asyncio.Event().wait()
                return competition_report()
        result=await OptimalPropertyRecommender(self.storage,Analyzer()).find_optimal_properties('Бизнес',environment_timeout_s=.05,**self.kwargs)
        self.assertEqual(result.evaluated_count,1)
        self.assertEqual(result.best_candidate.property_data['external_id'],'fast')
        self.assertFalse(result.comparison_complete)
        self.assertEqual(len(result.analysis_errors),1)
        self.assertIn('лимит времени',result.analysis_errors[0]['message'])

    async def test_finance_timeout_keeps_environment_and_marks_missing_finance(self):
        card=business_card()
        class Analyzer(FixtureFloorAnalyzer):
            fns_collector=None
            async def analyze_circle(self,**kw):
                return competition_report(synergy_businesses=[ClassifiedCompetitor(card=card,level=CompetitionLevel.SYNERGY,reason='fixture')])
            async def enrich_cards(self,cards):await asyncio.Event().wait()
        analyzer=Analyzer();analyzer.fns_collector=analyzer
        result=await OptimalPropertyRecommender(self.storage,analyzer).find_optimal_properties('Бизнес',fns_timeout_s=.05,**self.kwargs)
        self.assertEqual(result.evaluated_count,2)
        self.assertTrue(all('Финансовое обогащение недоступно' in c.competition_report.data_warnings for c in result.ranked_candidates))

    async def test_time_budgets_reject_nonfinite_nonpositive_and_unbounded_values(self):
        engine=OptimalPropertyRecommender(self.storage,FixtureFloorAnalyzer())
        for parameter in ('floor_timeout_s','environment_timeout_s','fns_timeout_s'):
            for value in (None,False,0,-1,float('inf'),float('nan')):
                with self.subTest(parameter=parameter,value=value),self.assertRaises(ValueError):
                    await engine.find_optimal_properties('Бизнес',**{parameter:value},**self.kwargs)
