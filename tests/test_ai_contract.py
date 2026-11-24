"""Regressions for the concrete failures captured by the AI contract audit."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from real_estate_engine import NvidiaCompetitorAnalyzer
from real_estate_engine.ai_contract import AIContractError, AIResponseError, strict_json_object, floor_response_schema, normalize_floor_response
from real_estate_engine.floor_policy import BusinessFloorProfileResolver, extract_floor_details, extract_floor_evidence
from floor_ai_fixtures import RecordingFloorInference, assessment_response, default_response
from test_engine_regressions import LAT,LNG,business_card
from real_estate_engine.competitors import MapCardsResult


PROP={"id":1,"floor":1,"title":"Помещение","description":"Свободный вход посетителям без ступеней прямо с улицы до двери помещения."}


class StrictJSONTests(unittest.TestCase):
    def test_exponent_overflow_is_rejected_at_every_depth(self):
        for raw in ('{"score":1e999}', '{"score":-1e999}', '{"nested":[1e999]}', '{"nested":{"score":1e999}}'):
            with self.subTest(raw=raw),self.assertRaises(AIContractError):strict_json_object(raw)
        self.assertEqual(strict_json_object('{"score":1e308,"text":"Infinity"}'),{'score':1e308,'text':'Infinity'})

    def test_non_json_or_ambiguous_json_rejected(self):
        for raw in ('prefix {"x":1}', '{"x":1} suffix', '{"x":1,"x":2}',
                    '{"x":NaN}', '{"x":Infinity}', '[]', '{"x":'):
            with self.subTest(raw=raw),self.assertRaises(AIContractError):strict_json_object(raw)

    def test_keyed_ids_cannot_be_overwritten_by_payload(self):
        raw={'assessments':{'c0':{'candidate_id':'c1','checks':{}}}}
        with self.assertRaises(AIContractError):normalize_floor_response(raw)
        raw={'assessments':{'c0':{'checks':{'audience_access':{'requirement_id':'other'}}}}}
        with self.assertRaises(AIContractError):normalize_floor_response(raw)

    def test_keyed_response_preserves_every_condition_identity(self):
        raw={'assessments':{'c0':{'reason':'Нет данных',
            'checks':{'audience_access':{'result':'unknown','evidence_ids':[]}}}}}
        result=normalize_floor_response(raw,{'c0':[{'requirement_id':'audience_access','description':'Допуск аудитории','origin':'inferred'}]})
        self.assertEqual(result['assessments'][0]['candidate_id'],'c0')
        self.assertEqual(result['assessments'][0]['checks'][0]['requirement_id'],'audience_access')


class FloorContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.resolver=BusinessFloorProfileResolver()
        self.profile=await self.resolver.resolve('Консультация','Доступ без ступеней; посетители приходят по записи')
        self.candidate={"candidate_id":"c0","facts":dict(floor=1,offered_floors=[1],title=PROP['title'],description=PROP['description']),
                        "requirements":self.resolver.required_checks(self.profile)}
        self.valid=assessment_response(self.candidate)

    def decision(self,response):
        return self.resolver._validate_assessment(response,PROP,self.profile)

    async def test_every_literal_fragment_is_mandatory_and_preserves_quote(self):
        self.assertEqual(self.profile.request_clauses,('Доступ без ступеней','посетители приходят по записи'))
        for identifier in ['audience_access','route_to_unit','request_0','request_1']:
            response=copy.deepcopy(self.valid)
            response['checks']=[c for c in response['checks'] if c['requirement_id']!=identifier]
            with self.subTest(identifier=identifier),self.assertRaises(AIContractError):self.decision(response)

    async def test_literal_client_condition_cannot_be_changed_or_made_optional(self):
        for change in (dict(description='Другой сценарий'),dict(request_quote='Клиент не требовал ступеней'),dict(critical=False)):
            response=copy.deepcopy(self.valid)
            next(c for c in response['checks'] if c['requirement_id']=='request_0').update(change)
            with self.subTest(change=change),self.assertRaises(AIContractError):self.decision(response)

    async def test_positive_client_constraint_cannot_be_waived(self):
        response=copy.deepcopy(self.valid)
        next(c for c in response['checks'] if c['requirement_id']=='request_0').update(result='not_required',critical=False,evidence=[])
        with self.assertRaises(AIContractError):self.decision(response)

    async def test_explicit_waiver_does_not_need_property_evidence(self):
        profile=await self.resolver.resolve('Студия','Уличный поток не нужен')
        candidate={**self.candidate,'requirements':self.resolver.required_checks(profile)}
        response=assessment_response(candidate)
        next(c for c in response['checks'] if c['requirement_id']=='request_0').update(result='not_required',critical=False,evidence=[])
        self.assertEqual(self.resolver._validate_assessment(response,PROP,profile).status,'eligible')

    async def test_numeric_floor_alone_does_not_prove_audience_or_path(self):
        for identifier in ('audience_access','route_to_unit'):
            response=copy.deepcopy(self.valid)
            next(c for c in response['checks'] if c['requirement_id']==identifier)['evidence']=[{'field':'floor','value':1}]
            with self.subTest(identifier=identifier):self.assertEqual(self.decision(response).status,'needs_verification')

    async def test_bad_missing_fact_types_never_mean_no_missing_facts(self):
        for value in (None,'Нет данных о пути',{'critical':'неизвестно'},[1]):
            response=copy.deepcopy(self.valid);response['missing_facts']=value
            with self.subTest(value=value),self.assertRaises(AIContractError):self.decision(response)
        response=copy.deepcopy(self.valid);response.pop('missing_facts')
        with self.assertRaises(AIContractError):self.decision(response)

    async def test_status_and_origin_are_strict(self):
        for value in (None,'INVALID',[],True):
            response=copy.deepcopy(self.valid);response['status']=value
            with self.subTest(value=value),self.assertRaises((AIContractError,TypeError)):self.decision(response)
        response=copy.deepcopy(self.valid)
        response['checks'][0]['origin']='explicit'
        with self.assertRaises(AIContractError):self.decision(response)

    async def test_malformed_answer_not_cached_after_service_recovers(self):
        def broken(payload):
            response=default_response(payload)
            for entry in response['assessments']:entry.pop('reason')
            return response
        model=RecordingFloorInference(broken)
        resolver=BusinessFloorProfileResolver(model)
        first=(await resolver.assess_many([PROP],self.profile))['1']
        self.assertEqual(first.analysis_status,'schema_error')
        self.assertEqual(resolver._assessment_cache,{})
        model.handler=default_response
        second=(await resolver.assess_many([PROP],self.profile))['1']
        self.assertEqual(second.status,'eligible')
        self.assertGreater(len(model.calls),1)

    async def test_old_floor_only_answers_from_both_models_cannot_approve(self):
        def old(payload):
            return {'assessments':[assessment_response({**p,'requirements':[]}) for p in payload['candidates']]}
        model=RecordingFloorInference(old,old)
        resolver=BusinessFloorProfileResolver(model)
        result=(await resolver.assess_many([PROP],self.profile))['1']
        self.assertEqual(result.status,'needs_verification')
        self.assertEqual(result.analysis_status,'schema_error')

    async def test_unknown_extra_candidate_invalidates_envelope(self):
        def extra(payload):
            data=default_response(payload)
            data['assessments'].append({**data['assessments'][0],'candidate_id':'invented'})
            return data
        resolver=BusinessFloorProfileResolver(RecordingFloorInference(extra))
        result=(await resolver.assess_many([PROP],self.profile))['1']
        self.assertEqual(result.analysis_status,'schema_error')

    async def test_disagreement_about_one_client_condition_cannot_be_hidden_by_status(self):
        profile=await self.resolver.resolve('Студия','Уличный поток не нужен')
        def primary(payload):
            response=default_response(payload)
            for a in response['assessments']:
                next(c for c in a['checks'] if c['requirement_id']=='request_0').update(result='not_required',critical=False,evidence=[])
            return response
        resolver=BusinessFloorProfileResolver(RecordingFloorInference(primary))
        result=(await resolver.assess_many([PROP],profile))['1']
        self.assertEqual(result.status,'needs_verification')
        self.assertEqual(result.semantic_review_status,'unsupported')

    async def test_repair_transport_failure_does_not_erase_another_candidate(self):
        def handler(payload):
            if len(payload['candidates'])==1:raise RuntimeError('repair unavailable')
            response=default_response(payload)
            for check in response['assessments'][1]['checks']:
                if check['requirement_id']=='audience_access':check['evidence']=[{'field':'floor','value':1}]
            return response
        resolver=BusinessFloorProfileResolver(RecordingFloorInference(handler),batch_size=2)
        result=await resolver.assess_many([PROP,{**PROP,'id':2}],self.profile)
        self.assertEqual(result['1'].status,'eligible')
        self.assertEqual(result['2'].analysis_status,'transport_error')

    async def test_generation_schema_binds_client_quotes_and_ids(self):
        candidate={**self.candidate,'evidence_catalog':{'f0':{'field':'floor','value':1}}}
        schema=floor_response_schema([candidate])
        candidates=schema['properties']['assessments']
        self.assertEqual(candidates['required'],['c0'])
        fields=candidates['properties']['c0']['properties']
        checks=fields['checks']
        ids=set(checks['required'])
        self.assertEqual(ids,{r['requirement_id'] for r in self.candidate['requirements']})
        self.assertTrue(all(c['additionalProperties'] is False for c in checks['properties'].values()))


class FloorScopeTests(unittest.TestCase):
    def test_reception_and_building_height_do_not_conflict_with_unit(self):
        for floor,description in ((2,'Помещение на втором этаже. Ресепшн на первом этаже.'),
                                  (1,'Торговый центр имеет 2 этажа. Помещение на первом этаже.')):
            raw=extract_floor_details(floor,description=description)
            self.assertFalse(raw['floor_conflicting'])
            self.assertFalse(extract_floor_evidence(dict(floor=floor,description=description[:30],raw_attributes=raw)).conflicting)

    def test_genuine_floor_conflict_keeps_both_quoted_claims(self):
        raw=extract_floor_details(1,description='Помещение расположено в подвале.')
        self.assertTrue(raw['floor_conflicting'])
        self.assertEqual(raw['offered_floors'],[1])
        self.assertTrue(any(c['floors']==[-1] and 'подвале' in c['quote'] for c in raw['floor_claims']))


class ResponseTransportTests(unittest.TestCase):
    def analyzer(self,finish):
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.model=analyzer.floor_model='fixture'
        analyzer.endpoint='https://example.test'
        body={'model':'fixture','choices':[{'finish_reason':finish,'message':{'content':'{"assessments":[]}'}}],
              'usage':{'completion_tokens':100}}
        analyzer.http_session=SimpleNamespace(post=lambda *a,**k:SimpleNamespace(status_code=200,content=json.dumps(body).encode(),text='fixture'))
        return analyzer

    def test_only_complete_response_is_accepted(self):
        for finish in ('length','content_filter',None):
            with self.subTest(finish=finish),self.assertRaises(AIResponseError):self.analyzer(finish)._call_floor_structured_sync('test', 1200, {'type':'object'})

    def test_finish_and_usage_returned_to_caller(self):
        response=self.analyzer('stop')._call_floor_structured_sync('test', 1200, {'type':'object'})
        self.assertEqual(response.metadata['finish_reason'],'stop')
        self.assertEqual(response.metadata['usage']['completion_tokens'],100)

    def test_structured_transport_does_not_drop_schema(self):
        analyzer=self.analyzer('stop');seen=[]
        previous=analyzer.http_session.post
        def post(url,**kw):
            seen.append(kw['json']);return previous(url,**kw)
        analyzer.http_session.post=post
        schema={'type':'object','properties':{},'additionalProperties':False}
        analyzer._call_floor_structured_sync('test',100,schema)
        self.assertEqual(seen[0]['response_format']['json_schema']['schema'],schema)
        self.assertTrue(seen[0]['response_format']['json_schema']['strict'])


class CompetitorContractTests(unittest.IsolatedAsyncioTestCase):
    async def analyze(self,payload):
        class Maps:
            async def collect_in_circle(self,**kwargs):return MapCardsResult([business_card()],status='ok')
        class Fns:
            async def enrich_cards(self,cards):return 0
        class Population:
            async def analyze_circle(self,**kwargs):return None
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.model='fixture';analyzer.map_collector=Maps();analyzer.fns_collector=Fns();analyzer.population_analyzer=Population()
        async def classify(*args, **kwargs):
            return payload, 'fixture', {}
        analyzer._concept_classifier=SimpleNamespace(classify=classify)
        with patch('real_estate_engine.competitors.FootTrafficEstimator.estimate_for_circle',return_value=None):
            return await analyzer.analyze_circle(LAT,LNG,400,'Кофейня',business_concept='Клиенты приходят за кофе с улицы.')

    async def test_conflicting_duplicates_are_unknown_not_unrelated(self):
        report=await self.analyze({'categories':[{'idx':1,'level':'direct','reason':'тот же товар'},
            {'idx':1,'level':'unrelated','reason':'не связан'}],'summary':'Контроль'})
        self.assertEqual(report.data_status,'partial')
        self.assertEqual(next(iter(report.category_breakdown.values()))['level'],'unknown')
        self.assertEqual(len(report.unrelated_businesses),0)

    async def test_wrong_id_types_and_missing_reasons_are_not_valid_categories(self):
        for entry in ({'idx':True,'level':'direct','reason':'test'},{'idx':1.8,'level':'direct','reason':'test'},
                      {'idx':2,'level':'direct','reason':'test'},{'idx':1,'level':'direct'}):
            with self.subTest(entry=entry):
                report=await self.analyze({'categories':[entry],'summary':'Контроль'})
                self.assertEqual(report.data_status,'partial')
                self.assertEqual(next(iter(report.category_breakdown.values()))['level'],'unknown')

    async def test_regex_recovery_cannot_report_complete_success(self):
        report=await self.analyze('{"categories":[{"idx":1,"level":"direct","reason":"конкурент"}],"summary":"unfinished')
        self.assertEqual(report.data_status,'partial')
        self.assertEqual(next(iter(report.category_breakdown.values()))['level'],'unknown')

    async def test_valid_classification_kept(self):
        report=await self.analyze({'categories':[{'idx':1,'level':'direct','reason':'тот же товар'}],'summary':'Контроль'})
        self.assertEqual(report.data_status,'ok')
        self.assertEqual(len(report.direct_competitors),1)


if __name__=='__main__':unittest.main()
