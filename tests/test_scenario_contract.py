from floor_ai_fixtures import ranking_transport_fixture, scenario_transport_fixture
import copy
import json
import unittest

from real_estate_engine.ai_contract import AIContractError
from real_estate_engine.ai_contract import normalize_floor_response,floor_response_schema
from real_estate_engine.scenario_contract import validate_scenario,normalize_scenario_response,compile_priority_order,scenario_schema,access_requirement_quotes
from real_estate_engine.ranking_policy import RankingProfileResolver,concept_fragments,PROFILE_GROUPS
from real_estate_engine.floor_policy import BusinessFloorProfileResolver
from real_estate_engine.proof_grounding import literal_floor_requirement
from real_estate_engine.business_model import concept_digest
from real_estate_engine import OptimalPropertyRecommender
from floor_ai_fixtures import FixtureFloorAnalyzer
from test_concept_ranking import criterion
from test_engine_regressions import property_offer,memory_storage,competition_report,LAT,LNG
from floor_ai_fixtures import RecordingFloorInference,assessment_response
from test_floor_semantic_review import offer


CONCEPT=('Все клиенты приезжают по предварительной записи, их привожу через интернет. '
         'Нужен разрешённый путь записанного посетителя к двери помещения в рабочие часы. '
         'Случайный поток прохожих не нужен. Главный критерий — низкие полные расходы на помещение. '
         'Предпочтительна площадь 30–100 м².')
SOURCES=concept_fragments(CONCEPT)
GROUP_FOR={metric:group for group,names in PROFILE_GROUPS.items() for metric in names}


def scenario():
    return dict(status='ready',context=[dict(source_id='s0',quote=SOURCES['s0'])],
                premises_requirements=[dict(source_id='s1',quote=SOURCES['s1'])],
                waivers=[dict(source_id='s2',quote=SOURCES['s2'])],
                priority_rules=[dict(higher='occupancy_cost',lower=group,source_id='s3',quote=SOURCES['s3'])
                                for group in ('audience_access_choice','physical_fit')],questions=[])


def scenario_response(data=None):
    if data is None:
        data=scenario()
    roles={key:dict(role='other',premises_quote=None) for key in SOURCES}
    for field,role in [('context','context'),('waivers','waiver'),('premises_requirements','premises_requirement')]:
        for item in data[field]:
            roles[item['source_id']]=dict(role=role,premises_quote=item['quote'] if role=='premises_requirement' else None)
    return dict(status=data['status'],interpretations=roles,primary_priority=None,
                priority_rules=data['priority_rules'],questions=data['questions'])


def ranking_response():
    return dict(status='ready',summary='Контроль приоритетов',criteria=[
        criterion('market_fit_index',.6,quote=SOURCES['s0']),
        criterion('premises_cost_month',.3,'lower',quote=SOURCES['s3']),
        criterion('area_sqm',.1,'range',30,100,quote=SOURCES['s4'])],
        questions=[],assumptions=[],visit_context_quotes=[SOURCES['s0'],SOURCES['s1'],SOURCES['s2']],unmodeled_requirements=[])


class ContractTests(unittest.TestCase):
    def test_unscored_physical_preference_is_not_a_mandatory_access_gate(self):
        preference='Желательна возможность подключения трёхфазного питания.'
        mandatory='Обязателен проём входа шириной не менее 1,1 м.'
        mixed='Желательна большая площадь, но обязателен подъезд к двери.'
        scenario=dict(premises_requirements=[dict(quote=q) for q in (preference,mandatory,mixed)])
        kept,excluded=access_requirement_quotes(scenario,[],{})
        self.assertEqual(kept,(mandatory,mixed))
        self.assertEqual(excluded,[preference])
    def test_soft_route_preference_is_not_an_access_obligation_but_mixed_must_is_kept(self):
        soft='Затем важна удобная пешеходная доступность.'
        mixed='Предпочтительна площадь 30–70 м², но обязательна парковка.'
        data=dict(premises_requirements=[dict(quote=soft),dict(quote=mixed),dict(quote='Нужен путь к двери.')])
        from real_estate_engine import RankingCriterion
        criteria=[RankingCriterion('walk_time_minutes',.5,'lower',0,20,soft,'Тест',''),
                  RankingCriterion('area_sqm',.5,'range',30,70,mixed,'Тест','')]
        kept,excluded=access_requirement_quotes(data,criteria,dict(walk_time_minutes='external_arrival',area_sqm='physical_fit'))
        self.assertEqual(kept,(mixed,'Нужен путь к двери.'));self.assertEqual(excluded,[soft])

    def test_word_floor_number_is_not_an_unproved_access_condition(self):
        for quote,value in [('Нужен первый этаж.',1),('На втором этаже',2),('Только 7 этаж',7),('Нужен седьмой этаж',7)]:
            self.assertEqual(literal_floor_requirement(quote),value)
        for quote in ('Первый этаж и вход без ступеней','Этаж не нужен','На первом этаже есть охрана'):
            self.assertIsNone(literal_floor_requirement(quote))
    def test_keyed_roles_have_one_slot_per_fragment_and_primary_expands_to_all_groups(self):
        wire=dict(status='ready',interpretations={k:dict(role='context',premises_quote=None) for k in SOURCES},
                  primary_priority=dict(group='occupancy_cost',source_id='s3',quote=SOURCES['s3']),priority_rules=[],questions=[])
        wire['interpretations']['s1']=dict(role='premises_requirement',premises_quote=SOURCES['s1'])
        wire['interpretations']['s2']=dict(role='waiver',premises_quote=None)
        contract=normalize_scenario_response(wire,SOURCES)
        self.assertEqual({(r['higher'],r['lower']) for r in contract['priority_rules']},
                         {('occupancy_cost','audience_access_choice'),('occupancy_cost','physical_fit')})
        self.assertEqual([x['quote'] for x in contract['premises_requirements']],[SOURCES['s1']])
        self.assertEqual(set(scenario_schema(SOURCES)['properties']['interpretations']['properties']),set(SOURCES))
        bad=copy.deepcopy(wire);bad['interpretations']['s0']['premises_quote']=SOURCES['s0']
        with self.assertRaises(AIContractError):normalize_scenario_response(bad,SOURCES)
        bad=copy.deepcopy(wire);del bad['interpretations']['s0']
        with self.assertRaises(AIContractError):normalize_scenario_response(bad,SOURCES)

    def test_context_and_property_obligations_are_separate_exact_source_spans(self):
        result=validate_scenario(scenario(),SOURCES)
        self.assertEqual(result['context'][0]['quote'],SOURCES['s0'])
        self.assertEqual(result['premises_requirements'][0]['quote'],SOURCES['s1'])
        for field,changes in [('context',dict(quote='Придуманная цитата')),('context',dict(source_id='missing'))]:
            data=scenario();data[field][0].update(changes)
            with self.assertRaises(AIContractError):validate_scenario(data,SOURCES)
        data=scenario();data['premises_requirements'].append(data['context'][0])
        with self.assertRaises(AIContractError):validate_scenario(data,SOURCES)

    def test_mixed_sentence_can_supply_distinct_substrings_without_paraphrasing(self):
        sources=dict(s0='Клиенты по записи, но вход должен быть доступен без пропусков.')
        data=dict(status='ready',context=[dict(source_id='s0',quote='Клиенты по записи')],
                  premises_requirements=[dict(source_id='s0',quote='вход должен быть доступен без пропусков')],
                  waivers=[],priority_rules=[],questions=[])
        self.assertEqual(len(validate_scenario(data,sources)['premises_requirements']),1)

    def test_cycle_or_self_priority_is_rejected(self):
        for rule in [dict(higher='occupancy_cost',lower='occupancy_cost'),dict(higher='physical_fit',lower='occupancy_cost')]:
            data=scenario();data['priority_rules'].append(dict(**rule,source_id='s3',quote=SOURCES['s3']))
            with self.assertRaises(AIContractError):validate_scenario(data,SOURCES)

    def test_literal_waiver_misclassified_as_obligation_is_removed_without_removing_mixed_requirement(self):
        data=scenario();waiver=data['waivers'].pop();data['premises_requirements'].append(waiver)
        contract=validate_scenario(data,SOURCES)
        self.assertNotIn(waiver,contract['premises_requirements']);self.assertIn(waiver,contract['waivers'])
        mixed='Лифт не нужен, но нужен проход без ступеней.'
        data=dict(status='ready',context=[],premises_requirements=[dict(source_id='s0',quote=mixed)],waivers=[],priority_rules=[],questions=[])
        self.assertEqual(validate_scenario(data,{'s0':mixed})['premises_requirements'][0]['quote'],mixed)

    def test_generated_missing_facts_cannot_invent_lift_or_an_unrequested_check(self):
        reqs=[dict(requirement_id='audience_access',description='Допуск посетителей',origin='inferred')]
        candidate=dict(candidate_id='c0',requirements=reqs,evidence_catalog={})
        schema=floor_response_schema([candidate])
        self.assertNotIn('missing_facts',schema['properties']['assessments']['properties']['c0']['properties'])
        for missing,result in [(['Отсутствует лифт'],'unknown'),(['audience_access'],'met')]:
            wire=dict(assessments={'c0':dict(reason='Тест',missing_facts=missing,checks={'audience_access':dict(result=result,evidence_ids=[])})})
            with self.assertRaises(AIContractError):normalize_floor_response(wire,{'c0':reqs})
        wire=dict(assessments={'c0':dict(reason='Тест',checks={'audience_access':dict(result='unknown',evidence_ids=[])})})
        normalized=normalize_floor_response(wire,{'c0':reqs})
        self.assertEqual(normalized['assessments'][0]['missing_facts'],['Допуск посетителей'])

    def test_explicit_cost_priority_reassigns_existing_magnitudes_without_magic_percentages(self):
        data=ranking_response();before=sorted(c['weight'] for c in data['criteria'])
        result,metadata=compile_priority_order(data,validate_scenario(scenario(),SOURCES),GROUP_FOR)
        by_metric={c['metric']:c['weight'] for c in result['criteria']}
        self.assertEqual(by_metric,dict(market_fit_index=.3,premises_cost_month=.6,area_sqm=.1))
        self.assertEqual(sorted(by_metric.values()),before)
        self.assertTrue(metadata['priority_order_adjusted'])
        self.assertEqual(data['criteria'][0]['weight'],.6)

    def test_no_declared_priority_preserves_importances_and_caller_order(self):
        contract=scenario();contract['priority_rules']=[]
        result,metadata=compile_priority_order(ranking_response(),contract,GROUP_FOR)
        self.assertEqual(result,ranking_response());self.assertFalse(metadata['priority_order_adjusted'])

    def test_omitted_primary_or_equal_strict_priority_does_not_silently_pass(self):
        for kind in ('omitted','equal'):
            data=ranking_response()
            if kind=='omitted':data['criteria'].pop(1)
            else:
                for c in data['criteria']:c['weight']=1/3
            with self.subTest(kind=kind),self.assertRaises(AIContractError):compile_priority_order(data,scenario(),GROUP_FOR)


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_literal_quote_retry_names_the_field_and_preserves_source_text(self):
        calls=[]
        original=SOURCES['s1']
        invalid=original.replace('путь','маршрут')
        class Analyzer(FixtureFloorAnalyzer):
            @staticmethod
            @scenario_transport_fixture
            def _call_scenario_contract_structured_sync(prompt,max_tokens,schema):
                calls.append(prompt)
                data=scenario_response()
                if len(calls)==1:
                    data['interpretations']['s1']['premises_quote']=invalid
                return json.dumps(data,ensure_ascii=False),'interpreter'
        result=await RankingProfileResolver(Analyzer()).resolve('Студия',CONCEPT)
        self.assertEqual(result.status,'ready')
        self.assertEqual(len(calls),2)
        self.assertIn('interpretations.s1.premises_quote',calls[1])
        self.assertIn(original,calls[1])
        self.assertIn(invalid,calls[1])
        self.assertEqual(result.metadata['scenario_contract']['premises_requirements'][0]['quote'],original)

    async def test_interpreter_retries_instead_of_accepting_an_internal_scenario_as_a_reply(self):
        calls=[]
        class Analyzer(FixtureFloorAnalyzer):
            @staticmethod
            @scenario_transport_fixture
            def _call_scenario_contract_structured_sync(prompt,max_tokens,schema):
                calls.append(prompt)
                current,model=FixtureFloorAnalyzer._call_scenario_contract_structured_sync(prompt,max_tokens,schema)
                if len(calls)==1:
                    old=dict(scenario(),audience_usage=json.loads(current)['audience_usage'])
                    return json.dumps(old,ensure_ascii=False),model
                return json.dumps(scenario_response(),ensure_ascii=False),model
        result=await RankingProfileResolver(Analyzer()).resolve('Студия',CONCEPT)
        self.assertEqual(result.status,'ready')
        self.assertEqual(len(calls),2)
        self.assertEqual(result.metadata['scenario_repair_errors'],['Incomplete keyed scenario interpretation'])

    async def test_cyclic_interpreter_answer_is_repaired_once_without_weakening_validation(self):
        calls=[]
        class Analyzer(FixtureFloorAnalyzer):
            @staticmethod
            @scenario_transport_fixture
            def _call_scenario_contract_structured_sync(prompt,max_tokens,schema):
                calls.append(prompt);data=scenario()
                if len(calls)==1:
                    data['priority_rules'].append(dict(higher='physical_fit',lower='occupancy_cost',source_id='s3',quote=SOURCES['s3']))
                return json.dumps(scenario_response(data),ensure_ascii=False),'interpreter'
            @staticmethod
            @ranking_transport_fixture
            def _call_ranking_profile_structured_sync(prompt,max_tokens):return json.dumps(ranking_response(),ensure_ascii=False),'rank'
        result=await RankingProfileResolver(Analyzer()).resolve('Студия',CONCEPT)
        self.assertEqual(result.status,'ready');self.assertEqual(len(calls),2)
        self.assertIn('Conflicting scenario priorities',calls[1])
        self.assertEqual(result.metadata['scenario_repair_errors'],['Conflicting scenario priorities'])

    async def test_persistent_interpreter_error_stops_after_two_attempts_and_is_not_cached(self):
        calls=[]
        class Analyzer(FixtureFloorAnalyzer):
            @staticmethod
            @scenario_transport_fixture
            def _call_scenario_contract_structured_sync(prompt,max_tokens,schema):
                calls.append(prompt);return '{}','interpreter'
            @staticmethod
            @ranking_transport_fixture
            def _call_ranking_profile_structured_sync(*args):raise AssertionError('Invalid scenario must not reach weights')
        resolver=RankingProfileResolver(Analyzer());result=await resolver.resolve('Студия',CONCEPT)
        self.assertEqual(result.analysis_status,'schema_error');self.assertEqual(len(calls),2)
        self.assertFalse(resolver._cache)

    async def test_profile_contract_retry_uses_only_current_metrics(self):
        prompts=[]
        class Analyzer(FixtureFloorAnalyzer):
            @staticmethod
            @scenario_transport_fixture
            def _call_scenario_contract_structured_sync(*args):
                return json.dumps(scenario_response(),ensure_ascii=False),'interpreter'
            @staticmethod
            @ranking_transport_fixture
            def _call_ranking_profile_structured_sync(prompt,max_tokens):
                prompts.append(prompt)
                if len(prompts)==1:return '{}','rank'
                return json.dumps(ranking_response(),ensure_ascii=False),'rank'
        result=await RankingProfileResolver(Analyzer()).resolve('Студия',CONCEPT)
        self.assertEqual(result.status,'ready')
        self.assertEqual(len(prompts),2)
        self.assertIn('Исправь нарушение предыдущего ответа',prompts[1])
        for prompt in prompts:
            self.assertNotIn('orders_daily',prompt)
            payload=json.loads(prompt.split('\nINPUT_JSON\n',1)[1])
            self.assertEqual(set(payload['signal_inputs']),set(GROUP_FOR))

    async def test_profile_transport_retry_and_interpreter_model_cache_invalidation(self):
        from real_estate_engine.ai_contract import AITransportError
        calls=[]
        class Analyzer(FixtureFloorAnalyzer):
            floor_review_model='interpreter-a'
            @staticmethod
            @scenario_transport_fixture
            def _call_scenario_contract_structured_sync(*args):return json.dumps(scenario_response(),ensure_ascii=False),'interpreter'
            @staticmethod
            @ranking_transport_fixture
            def _call_ranking_profile_structured_sync(prompt,max_tokens):
                calls.append(prompt)
                if len(calls)==1:raise AITransportError('Controlled timeout',retryable=True)
                return json.dumps(ranking_response(),ensure_ascii=False),'rank'
        analyzer=Analyzer();resolver=RankingProfileResolver(analyzer)
        result=await resolver.resolve('Студия',CONCEPT)
        self.assertEqual(result.status,'ready');self.assertEqual(len(calls),2)
        self.assertEqual(result.metadata['profile_retry_errors'],['Controlled timeout'])
        await resolver.resolve('Студия',CONCEPT);self.assertEqual(len(calls),2)
        analyzer.floor_review_model='interpreter-b'
        await resolver.resolve('Студия',CONCEPT);self.assertEqual(len(calls),3)

    async def test_citation_repair_requires_fresh_independent_and_isolated_confirmation(self):
        def primary(payload):
            candidate=payload['candidates'][0];answer=assessment_response(candidate)
            check=next(c for c in answer['checks'] if c['requirement_id']=='audience_access')
            check['evidence']=[dict(field='title',quote=candidate['facts']['title'])]
            route=next(c for c in answer['checks'] if c['requirement_id']=='route_to_unit')
            route['evidence']=[dict(field='description',quote=candidate['facts']['description'])]
            return dict(assessments=[answer])
        inference=RecordingFloorInference(handler=primary)
        inference.proof_handler=lambda p:dict(verdict='supported' if any(r['field']=='description' for r in p['source_fragments']) else 'unknown',reason='Контроль выбранного фрагмента')
        resolver=BusinessFloorProfileResolver(inference)
        context='Обычные посетители приходят по записи.'
        p=await resolver.resolve('Консультации',context,requirement_quotes=(),context_quotes=(context,))
        prop=offer();prop['title']='Светлый кабинет, вход посетителей'
        result=(await resolver.assess_many([prop],p))['1']
        self.assertEqual(result.status,'eligible')
        self.assertTrue(result.inference_metadata['proof_reference_repair_attempted'])
        self.assertEqual(result.inference_metadata['proof_reference_repair_conditions'],['audience_access'])
        self.assertTrue(all(p['verdict']=='supported' for p in result.inference_metadata['proof_verifications']))
        # No supported alternative means no repair and no automatic approval.
        inference2=RecordingFloorInference(handler=primary)
        inference2.proof_handler=lambda p:dict(verdict='unknown',reason='Ни один фрагмент не доказывает условия')
        resolver2=BusinessFloorProfileResolver(inference2)
        pending=(await resolver2.assess_many([prop],p))['1']
        self.assertEqual(pending.status,'needs_verification')
        self.assertNotIn('proof_reference_repair_attempted',pending.inference_metadata)

    async def test_waived_traffic_is_not_sent_to_isolated_prover_as_a_property_condition(self):
        inference=RecordingFloorInference()
        resolver=BusinessFloorProfileResolver(inference)
        context='Клиенты приходят по записи.';waiver='Случайный поток прохожих не нужен.'
        p=await resolver.resolve('Консультации',context+' '+waiver,requirement_quotes=(),context_quotes=(context,),waiver_quotes=(waiver,))
        await resolver.assess_many([offer()],p)
        self.assertTrue(inference.proof_calls)
        for payload in inference.proof_calls:
            self.assertNotIn(waiver,json.dumps(payload['scenario'],ensure_ascii=False))
            self.assertIn(context,payload['scenario']['business_format'])
            self.assertEqual(payload['scenario']['property_requirements'],[])

    async def test_interpreter_sees_no_weights_or_properties_and_cache_covers_both_calls(self):
        calls=[]
        class Analyzer(FixtureFloorAnalyzer):
            @staticmethod
            @scenario_transport_fixture
            def _call_scenario_contract_structured_sync(prompt,max_tokens,schema):
                payload=json.loads(prompt.split('\nINPUT_JSON\n')[1]);calls.append(('interpret',payload))
                return json.dumps(scenario_response(),ensure_ascii=False),'independent-interpreter'
            @staticmethod
            @ranking_transport_fixture
            def _call_ranking_profile_structured_sync(prompt,max_tokens):
                payload=json.loads(prompt.split('\nINPUT_JSON\n')[1]);calls.append(('rank',payload))
                return json.dumps(ranking_response(),ensure_ascii=False),'ranking-generator'
        resolver=RankingProfileResolver(Analyzer())
        first=await resolver.resolve('Студия',CONCEPT);second=await resolver.resolve('Студия',CONCEPT)
        self.assertEqual(first.status,'ready');self.assertEqual(first,second);self.assertEqual(len(calls),2)
        self.assertNotIn('criteria',calls[0][1]);self.assertNotIn('properties',calls[0][1])
        self.assertEqual(calls[1][1]['interpreted_scenario']['premises_requirements'][0]['quote'],SOURCES['s1'])
        self.assertTrue(first.metadata['priority_order_adjusted'])
        weights={c.metric:c.weight for c in first.criteria}
        self.assertGreater(weights['premises_cost_month'],weights['market_fit_index'])

    async def test_public_search_never_demands_an_ad_prove_client_marketing_or_booking(self):
        class Analyzer(FixtureFloorAnalyzer):
            @staticmethod
            @scenario_transport_fixture
            def _call_scenario_contract_structured_sync(prompt,max_tokens,schema):return json.dumps(scenario_response(),ensure_ascii=False),'independent-interpreter'
            @staticmethod
            @ranking_transport_fixture
            def _call_ranking_profile_structured_sync(prompt,max_tokens):return json.dumps(ranking_response(),ensure_ascii=False),'ranking-generator'
            async def analyze_circle(self,**kw):return competition_report()
        store,conn=memory_storage();self.addCleanup(conn.close);store.upsert_property(property_offer('one'))
        engine=OptimalPropertyRecommender(store,Analyzer())
        references=dict(properties={},ranking_scales=dict(concept_sha256=concept_digest(CONCEPT),
            metrics={'premises_cost_month':dict(low=0,high=200000,source='Controlled client reference')}))
        result=await engine.find_optimal_properties('Студия',business_concept=CONCEPT,center_lat=LAT,center_lng=LNG,search_radius_m=1000,business_data=references)
        self.assertEqual(result.floor_profile.request_clauses,(SOURCES['s1'],))
        self.assertIn(SOURCES['s0'],result.floor_profile.business_format)
        self.assertEqual(result.floor_profile.scenario_context_quotes,(SOURCES['s0'],))
        self.assertEqual(result.floor_profile.waived_requirement_quotes,(SOURCES['s2'],))
        checks=BusinessFloorProfileResolver.required_checks(result.floor_profile)
        self.assertEqual([c['description'] for c in checks if c['origin']=='explicit'],[SOURCES['s1']])
        self.assertEqual(result.evaluated_count,1)

    async def test_scoring_preferences_misclassified_by_interpreter_do_not_become_access_obligations(self):
        class Analyzer(FixtureFloorAnalyzer):
            @staticmethod
            @scenario_transport_fixture
            def _call_scenario_contract_structured_sync(prompt,max_tokens,schema):
                data=scenario()
                data['premises_requirements'].extend(dict(source_id=k,quote=SOURCES[k]) for k in ('s3','s4'))
                return json.dumps(scenario_response(data),ensure_ascii=False),'interpreter'
            @staticmethod
            @ranking_transport_fixture
            def _call_ranking_profile_structured_sync(prompt,max_tokens):return json.dumps(ranking_response(),ensure_ascii=False),'rank'
            async def analyze_circle(self,**kw):return competition_report()
        store,conn=memory_storage();self.addCleanup(conn.close);store.upsert_property(property_offer('one'))
        references=dict(properties={},ranking_scales=dict(concept_sha256=concept_digest(CONCEPT),
            metrics={'premises_cost_month':dict(low=0,high=200000,source='Controlled client reference')}))
        report=await OptimalPropertyRecommender(store,Analyzer()).find_optimal_properties('Студия',business_concept=CONCEPT,center_lat=LAT,center_lng=LNG,search_radius_m=1000,business_data=references)
        self.assertEqual(report.floor_profile.request_clauses,(SOURCES['s1'],))
        self.assertEqual(len(report.ranking_profile.metadata['excluded_scoring_quotes_from_access']),2)

    async def test_uninterpretable_scenario_stops_before_listing_assessment(self):
        class Analyzer(FixtureFloorAnalyzer):
            @staticmethod
            @scenario_transport_fixture
            def _call_scenario_contract_structured_sync(prompt,max_tokens,schema):
                response=scenario();response['status']='needs_context';response['questions']=['Какой из противоречивых приоритетов важнее?']
                return json.dumps(scenario_response(response),ensure_ascii=False),'interpreter'
            @staticmethod
            @ranking_transport_fixture
            def _call_ranking_profile_structured_sync(prompt,max_tokens):raise AssertionError('Must not rank an ambiguous scenario')
        result=await RankingProfileResolver(Analyzer()).resolve('Студия',CONCEPT)
        self.assertEqual(result.status,'needs_context');self.assertEqual(result.analysis_status,'needs_context')


class ObligationClausePreservationTests(unittest.TestCase):
    def test_mandatory_clause_combined_with_area_or_budget_keeps_access_and_physical_requirements(self):
        from types import SimpleNamespace
        from real_estate_engine.scenario_contract import access_requirement_quotes, preference_role
        from real_estate_engine.premises_requirements import physical_context_quotes

        clause = 'обязателен 1-й этаж, удобный уличный вход без пропускной системы, площадь 40-75 м², ставка до 180 тыс'
        self.assertEqual(preference_role(clause), 'obligation')
        scenario = {
            'premises_requirements': [{'source_id': 's0', 'quote': clause}],
            'applied_search_filters': [],
        }
        criteria = [
            SimpleNamespace(metric='area_sqm', concept_quote=clause),
            SimpleNamespace(metric='premises_cost_month', concept_quote=clause),
        ]
        metric_families = {'area_sqm': 'physical_fit', 'premises_cost_month': 'occupancy_cost'}
        kept, excluded = access_requirement_quotes(scenario, criteria, metric_families)
        self.assertEqual(kept, (clause,))
        self.assertEqual(excluded, [])

        profile = SimpleNamespace(
            criteria=criteria,
            metadata={'scenario_contract': scenario},
        )
        self.assertNotIn(clause, physical_context_quotes(profile))


if __name__=='__main__':unittest.main()
