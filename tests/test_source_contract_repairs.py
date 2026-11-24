"""Regressions for misapplied waivers, invented hours and concept classification."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace

from real_estate_engine.ai_contract import AIContractError,AITextResponse
from real_estate_engine.concept_evidence import client_clock_window,waiver_targets
from real_estate_engine.market_context import validate_audience_usage,validate_market_context
from real_estate_engine.competitor_classification import ConceptCompetitorClassifier
from real_estate_engine.competitors import NvidiaCompetitorAnalyzer,MapCardsResult
from test_market_context import market_config,CONCEPT,integrated_profile,environment
from test_engine_regressions import business_card,LAT,LNG
from real_estate_engine.ranking_policy import evaluate_profile
from test_engine_regressions import candidate


class ClientEvidenceTests(unittest.TestCase):
    def usage(self,key,quote):
        data={k:dict(usage='use',source_id='s0',reason='Применимый источник',waiver_quote=None) for k in market_config()['audience']}
        data[key]=dict(usage='waived',source_id='s1',reason='Проверка границ отказа',waiver_quote=quote)
        return validate_audience_usage(data,dict(s0=CONCEPT,s1=quote),dict(waivers=[dict(source_id='s1',quote=quote)]))

    def test_flow_waiver_cannot_disable_workers_residents_or_synergy(self):
        quote='Случайный поток прохожих не нужен.'
        self.assertEqual(self.usage('transit_flow',quote)['transit_flow']['usage'],'waived')
        for key in ('workers','residents','synergy'):
            with self.subTest(key=key),self.assertRaises(AIContractError):self.usage(key,quote)

    def test_worker_waiver_cannot_disable_passers_by(self):
        quote='Сотрудники соседних офисов не нужны.'
        self.assertEqual(self.usage('workers',quote)['workers']['usage'],'waived')
        with self.assertRaises(AIContractError):self.usage('transit_flow',quote)

    def test_mixed_clause_does_not_transfer_negative_predicate_to_positive_subject(self):
        self.assertEqual(waiver_targets('Жители важны, случайные прохожие не нужны.'),{'transit_flow'})

    def test_unrecognized_waiver_subject_is_not_assigned_to_any_source(self):
        with self.assertRaises(AIContractError):self.usage('workers','Нечто непонятное не нужно.')

    def test_hours_not_in_concept_cannot_be_invented(self):
        config=market_config();config['active_hours']=[3,4]
        with self.assertRaises(AIContractError):validate_market_context(config,CONCEPT)
        with self.assertRaises(AIContractError):evaluate_profile(integrated_profile(config),candidate(),environment())

    def test_explicit_daytime_and_overnight_clock_ranges(self):
        self.assertEqual(client_clock_window('Работаем с 8 до 20.')['active_hours'],list(range(8,20)))
        self.assertEqual(client_clock_window('Открыто 22:00–06:00.')['active_hours'],[0,1,2,3,4,5,22,23])

    def test_partial_hours_and_breaks_are_preserved(self):
        window=client_clock_window('Работаем 08:30–20:30; перерыв 12:00–13:00.')
        self.assertEqual(window['hour_coverage'][8],.5)
        self.assertEqual(window['hour_coverage'][20],.5)
        self.assertEqual(window['hour_coverage'][12],0)

    def test_price_area_and_age_ranges_are_not_hours(self):
        for concept in ('Бюджет от 8 до 20 тыс. рублей.','Площадь 8–20 м².','Клиенты от 18 до 24 лет.'):
            window=client_clock_window(concept)
            self.assertEqual(window['active_hours'],list(range(24)))
            self.assertFalse(window['specified'])

    def test_order_counts_durations_and_calendar_dates_are_not_clock_hours(self):
        for concept in ('Работаем с загрузкой 8–20 заказов в день.','Принимаем 8–12 консультаций в день.',
                        'Работаем 8–20 часов в неделю.','Работаем 6–11 ноября.','Работаем 08.11–20.11.'):
            with self.subTest(concept=concept):
                result=client_clock_window(concept)
                self.assertEqual(result['active_hours'],list(range(24)))
                self.assertFalse(result['specified'])


class OrganizationClassificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls=[]
        self.cards=[business_card(org_id='a',title='Кофе навынос'),business_card(org_id='b',title='Кофе внутри офиса')]
    def infer(self,prompt,tokens,**kwargs):
        data=json.loads(prompt.split('\nINPUT_JSON\n',1)[1]);self.calls.append(data)
        return AITextResponse(json.dumps(dict(categories=[dict(idx=o['idx'],need_relation='same_purchase' if o['org_id']=='a' else 'substitute',
            same_purchase_offer='yes' if o['org_id']=='a' else 'no',relationship_basis='specific_purchase',
            offer_evidence_ids=list(o['offer_evidence_catalog'])[:1],
            audience_relation='overlap',scenario_access='compatible',evidence_fields=['title','categories'],
            reason='Контроль разных форматов') for o in data['organizations']],summary='Контроль')),'fixture',{})

    async def test_same_category_can_have_different_classes_and_full_concept_is_sent(self):
        classifier=ConceptCompetitorClassifier();result,_,metadata=await classifier.classify(self.cards,'Кофейня',CONCEPT,'fixture',self.infer)
        self.assertEqual(self.calls[0]['business_concept'],CONCEPT)
        self.assertEqual([e['level'] for e in result['categories']],['direct','indirect'])
        self.assertEqual(metadata['classification_scope'],'organizations')
        self.assertEqual(len(self.calls),1)

    async def test_overlapping_concurrent_searches_reuse_semantics_but_keep_live_distances(self):
        classifier=ConceptCompetitorClassifier();other=copy.deepcopy(self.cards)
        other[0].distance_m=10;other[0].rating=5;other[0].fns_revenue_rub=99999999
        results=await asyncio.gather(*(classifier.classify(c,'Кофейня',CONCEPT,'fixture',self.infer) for c in (self.cards,other)))
        self.assertEqual(len(self.calls),1)
        self.assertEqual(results[1][2]['cache_hits'],0)
        self.assertEqual(results[1][2]['inflight_hits'],2)
        self.assertEqual(other[0].distance_m,10)

    async def test_concept_or_organization_change_invalidates_cache(self):
        classifier=ConceptCompetitorClassifier()
        await classifier.classify(self.cards,'Кофейня',CONCEPT,'fixture',self.infer)
        await classifier.classify(self.cards,'Кофейня',CONCEPT+' Только сотрудники.','fixture',self.infer)
        self.cards[0].working_hours='08:00–20:00'
        await classifier.classify(self.cards,'Кофейня',CONCEPT,'fixture',self.infer)
        self.assertEqual(len(self.calls),3)
        self.assertEqual(len(self.calls[-1]['organizations']),1)

    async def test_cache_binds_decisions_to_current_ids_after_reordering(self):
        classifier=ConceptCompetitorClassifier()
        await classifier.classify(self.cards,'Кофейня',CONCEPT,'fixture',self.infer)
        result,_,metadata=await classifier.classify(list(reversed(self.cards)),'Кофейня',CONCEPT,'fixture',self.infer)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(result['categories'][0]['level'],'indirect')
        self.assertEqual(metadata['organization_decisions']['b']['idx'],1)

    async def test_large_neighborhood_is_batched_without_losing_ids(self):
        classifier=ConceptCompetitorClassifier()
        cards=[business_card(org_id=str(i)) for i in range(70)]
        result,_,meta=await classifier.classify(cards,'Кофейня',CONCEPT,'fixture',self.infer)
        self.assertEqual(len(self.calls),9);self.assertEqual(meta['batches'],9)
        self.assertTrue(all(len(c['organizations'])<=8 for c in self.calls))
        self.assertEqual([e['idx'] for e in result['categories']],list(range(1,71)))

    async def test_transport_failure_does_not_erase_already_cached_classes(self):
        classifier=ConceptCompetitorClassifier()
        await classifier.classify(self.cards[:1],'Кофейня',CONCEPT,'fixture',self.infer)
        def broken(*args,**kwargs):raise RuntimeError('Controlled service failure')
        result,_,meta=await classifier.classify(self.cards,'Кофейня',CONCEPT,'fixture',broken)
        self.assertEqual(result['categories'][0]['level'],'direct')
        self.assertEqual(result['categories'][1]['level'],'unknown')
        self.assertEqual(meta['cache_hits'],1)

    async def test_truncated_api_response_gets_one_bounded_repair(self):
        from real_estate_engine.ai_contract import AIResponseError
        attempts=[]
        def truncated(prompt,tokens,**kwargs):
            attempts.append(tokens)
            if len(attempts)==1:raise AIResponseError('Incomplete API response: length')
            return self.infer(prompt,tokens,**kwargs)
        result,_,meta=await ConceptCompetitorClassifier().classify(self.cards,'Кофейня',CONCEPT,'fixture',truncated)
        self.assertEqual(len(attempts),2);self.assertTrue(all(t>=2200 for t in attempts))
        self.assertEqual([e['level'] for e in result['categories']],['direct','indirect'])
        self.assertEqual(meta['unknown_count'],0)

    async def test_duplicate_answer_cannot_cache_a_class_and_mask_another_org(self):
        def duplicate(prompt,tokens,**kw):
            answer,_=self.infer(prompt,tokens,**kw);data=json.loads(answer)
            data['categories'][1]=dict(data['categories'][0])
            return AITextResponse(json.dumps(data),'fixture',{})
        classifier=ConceptCompetitorClassifier()
        result,_,meta=await classifier.classify(self.cards,'Кофейня',CONCEPT,'fixture',duplicate)
        self.assertEqual([r['level'] for r in result['categories']],['unknown','unknown'])
        self.assertEqual(classifier.cache,{})

    async def test_bad_json_is_repaired_once_without_partial_classification(self):
        attempts=[]
        def malformed_first(prompt,tokens,**kw):
            attempts.append(1)
            if len(attempts)==1:return AITextResponse('{"categories":[','fixture',{})
            return self.infer(prompt,tokens,**kw)
        classifier=ConceptCompetitorClassifier()
        result,_,meta=await classifier.classify(self.cards,'Кофейня',CONCEPT,'fixture',malformed_first)
        self.assertEqual(len(attempts),2)
        self.assertEqual(meta['unknown_count'],0)

    async def test_analyzer_unknown_class_is_reported_as_partial(self):
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.model='fixture'
        def broken(*args,**kw):raise RuntimeError('Controlled transport failure')
        analyzer._call_nvidia_sync=broken
        async def collect(**kw):return MapCardsResult(self.cards,status='ok')
        async def population(**kw):return None
        analyzer.map_collector=SimpleNamespace(collect_in_circle=collect)
        analyzer.population_analyzer=SimpleNamespace(analyze_circle=population)
        report=await analyzer.analyze_circle(LAT,LNG,400,'Кофейня',enrich_fns=False,business_concept=CONCEPT)
        self.assertEqual(report.data_status,'partial');self.assertFalse(report.unrelated_businesses)
        self.assertEqual(report.ai_response_metadata['unknown_count'],2)

    async def test_unknown_is_not_cached_as_unrelated(self):
        def unknown(prompt,tokens,**kw):
            data=json.loads(prompt.split('\nINPUT_JSON\n',1)[1]);self.calls.append(data)
            return AITextResponse(json.dumps(dict(categories=[dict(idx=o['idx'],need_relation='unknown',audience_relation='unknown',
                scenario_access='unknown',same_purchase_offer='unknown',relationship_basis='unknown',offer_evidence_ids=[],
                evidence_fields=[],reason='Мало сведений')
                for o in data['organizations']],summary='Неизвестно')),'fixture',{})
        classifier=ConceptCompetitorClassifier()
        for _ in range(2):
            result,_,metadata=await classifier.classify(self.cards,'Кофейня',CONCEPT,'fixture',unknown)
            self.assertEqual(metadata['unknown_count'],2)
        self.assertEqual(len(self.calls),2)

    def test_same_purchase_for_same_accessible_audience_cannot_be_synergy(self):
        self.assertEqual(ConceptCompetitorClassifier.level(dict(need_relation='same_purchase',
            same_purchase_offer='yes',offer_evidence_ids=['feature:1'],relationship_basis='specific_purchase',
            audience_relation='overlap',scenario_access='compatible')),'direct')
        self.assertEqual(ConceptCompetitorClassifier.level(dict(need_relation='complementary',
            same_purchase_offer='no',offer_evidence_ids=['category:0'],relationship_basis='specific_purchase',
            audience_relation='overlap',scenario_access='compatible')),'synergy')

    def test_same_offer_owns_relation_even_when_primary_model_calls_it_synergy(self):
        result=ConceptCompetitorClassifier.level(dict(need_relation='complementary',same_purchase_offer='yes',
            offer_evidence_ids=['feature:0'],relationship_basis='general_overlap',audience_relation='overlap',scenario_access='compatible'))
        self.assertEqual(result,'direct')

    def test_general_wellbeing_or_colocation_cannot_establish_synergy_or_substitution(self):
        for relation in ('complementary','substitute'):
            result=ConceptCompetitorClassifier.level(dict(need_relation=relation,same_purchase_offer='no',
                offer_evidence_ids=['category:0'],relationship_basis='general_overlap',audience_relation='overlap',scenario_access='compatible'))
            self.assertEqual(result,'unknown')

    def test_negative_map_feature_is_not_affirmative_offer_evidence(self):
        card=business_card(features=['Дополнительная услуга: False','Другая услуга: True'])
        facts=ConceptCompetitorClassifier.facts(card)
        self.assertFalse(facts['feature_flags']['Дополнительная услуга'])
        self.assertTrue(facts['feature_flags']['Другая услуга'])
        self.assertFalse(any('False' in r['quote'] for r in facts['offer_evidence_catalog'].values()))

    async def test_invented_product_evidence_cannot_authorize_or_enter_cache(self):
        def invented(prompt,tokens,**kw):
            response=self.infer(prompt,tokens,**kw);raw,model=response;data=json.loads(raw)
            for item in data['categories']:item['offer_evidence_ids']=['made-up-product']
            return AITextResponse(json.dumps(data),model,{})
        classifier=ConceptCompetitorClassifier();result,_,meta=await classifier.classify(self.cards,'Кофейня',CONCEPT,'fixture',invented)
        self.assertEqual(meta['unknown_count'],2);self.assertFalse(classifier.cache)

    async def test_independent_offer_proof_is_blind_and_rejection_cannot_enter_cache(self):
        proof_calls=[]
        def verify(prompt,tokens,**kwargs):
            data=json.loads(prompt.split('\nINPUT_JSON\n',1)[1]);proof_calls.append(data)
            answer={key:dict(verdict='unsupported' if key=='1' else 'supported',
                reason='Проверка исходного предложения')
                for key,value in data['claims'].items()}
            return AITextResponse(json.dumps(answer),'independent-proof',{})
        classifier=ConceptCompetitorClassifier()
        result,_,meta=await classifier.classify(self.cards,'Бизнес',CONCEPT,'primary',self.infer,verify=verify,verifier_model='proof')
        self.assertEqual([e['level'] for e in result['categories']],['unknown','indirect'])
        self.assertEqual(len(classifier.cache),1)
        for claim in proof_calls[0]['claims'].values():
            self.assertEqual(set(claim),{'claim','source_fragments'})
            self.assertTrue(all(v['field'] in ('categories','features') for v in claim['source_fragments']))
        self.assertEqual(meta['organization_decisions']['a']['offer_proof']['verdict'],'unsupported')

    async def test_proof_outage_and_forged_proof_evidence_do_not_authorize_or_poison_cache(self):
        for forged in (False,True):
            def verify(prompt,tokens,**kwargs):
                if not forged:raise RuntimeError('Controlled proof outage')
                claims=json.loads(prompt.split('\nINPUT_JSON\n',1)[1])['claims']
                return AITextResponse(json.dumps({key:dict(verdict='supported',evidence_ids=['foreign-evidence'],reason='Invalid proof') for key in claims}),'proof',{})
            classifier=ConceptCompetitorClassifier()
            result,_,meta=await classifier.classify(self.cards,'Бизнес',CONCEPT,'primary',self.infer,verify=verify,verifier_model='proof')
            self.assertEqual(meta['unknown_count'],2);self.assertFalse(classifier.cache)

    async def test_changing_proof_model_invalidates_the_semantic_cache(self):
        def verify(prompt,tokens,**kwargs):
            claims=json.loads(prompt.split('\nINPUT_JSON\n',1)[1])['claims']
            return AITextResponse(json.dumps({key:dict(verdict='supported',reason='Контроль') for key,value in claims.items()}),'proof',{})
        classifier=ConceptCompetitorClassifier()
        for model in ('proof-a','proof-a','proof-b'):
            await classifier.classify(self.cards,'Бизнес',CONCEPT,'primary',self.infer,verify=verify,verifier_model=model)
        self.assertEqual(len(self.calls),2)

    async def test_complete_supported_proof_does_not_become_unknown_due_to_explanation_length(self):
        def verify(prompt,tokens,**kwargs):
            claims=json.loads(prompt.split('\nINPUT_JSON\n',1)[1])['claims']
            return AITextResponse(json.dumps({key:dict(verdict='supported',reason='Подтверждено исходными признаками. '*12) for key in claims}),'proof',{})
        classifier=ConceptCompetitorClassifier()
        result,_,metadata=await classifier.classify(self.cards,'Бизнес',CONCEPT,'primary',self.infer,verify=verify,verifier_model='proof')
        self.assertEqual([e['level'] for e in result['categories']],['direct','indirect'])
        self.assertEqual(metadata['unknown_count'],0)

    async def test_expired_environment_keeps_cached_classes_without_new_calls(self):
        import time
        from real_estate_engine.ai_contract import AI_DEADLINE
        classifier=ConceptCompetitorClassifier()
        await classifier.classify(self.cards[:1],'Бизнес',CONCEPT,'primary',self.infer)
        token=AI_DEADLINE.set(time.monotonic()-1)
        try:result,_,meta=await classifier.classify(self.cards,'Бизнес',CONCEPT,'primary',self.infer)
        finally:AI_DEADLINE.reset(token)
        self.assertEqual(len(self.calls),1)
        self.assertEqual([e['level'] for e in result['categories']],['direct','unknown'])
        self.assertEqual(meta['cache_hits'],1)

    async def test_expired_classification_deadline_does_not_discard_collected_environment(self):
        import time
        from real_estate_engine.ai_contract import AI_DEADLINE
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer);analyzer.model='fixture'
        analyzer._call_nvidia_sync=lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('No late AI request'))
        async def collect(**kw):return MapCardsResult(self.cards,status='ok')
        async def population(**kw):return None
        analyzer.map_collector=SimpleNamespace(collect_in_circle=collect)
        analyzer.population_analyzer=SimpleNamespace(analyze_circle=population)
        token=AI_DEADLINE.set(time.monotonic()-1)
        try:report=await analyzer.analyze_circle(LAT,LNG,400,'Бизнес',enrich_fns=False,business_concept=CONCEPT)
        finally:AI_DEADLINE.reset(token)
        self.assertEqual(report.total_cards_found,2);self.assertEqual(report.data_status,'partial')
        self.assertEqual(report.ai_response_metadata['unknown_count'],2)
        self.assertFalse(report.direct_competitors)

    async def test_analyzer_preserves_per_org_classification(self):
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.model='fixture';analyzer._call_nvidia_sync=self.infer
        async def collect(**kw):return MapCardsResult(self.cards,status='ok')
        async def population(**kw):return None
        analyzer.map_collector=SimpleNamespace(collect_in_circle=collect)
        analyzer.population_analyzer=SimpleNamespace(analyze_circle=population)
        report=await analyzer.analyze_circle(LAT,LNG,400,'Кофейня',enrich_fns=False,business_concept=CONCEPT)
        self.assertEqual(len(report.direct_competitors),1);self.assertEqual(len(report.indirect_competitors),1)
        self.assertEqual(report.ai_response_metadata['classification_scope'],'organizations')
        self.assertEqual(report.data_status,'ok')


if __name__=='__main__':unittest.main()
