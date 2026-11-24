"""Reproduce contract weaknesses without live calls or production-file writes."""
import asyncio
import copy
import hashlib
import json
import logging
import sys
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from real_estate_engine import NvidiaCompetitorAnalyzer
from real_estate_engine.floor_policy import BusinessFloorProfileResolver,_json_object,extract_floor_details,extract_floor_evidence
from floor_ai_fixtures import RecordingFloorInference,default_response,assessment_response
from real_estate_engine.competitors import MapCardsResult
from test_engine_regressions import business_card,LAT,LNG

OUT=ROOT/'audit/ai_contract_2026-11-03'
if '--after-fix' in sys.argv:
    OUT=ROOT/'audit/ai_contract_fix_2026-11-03/local_probes'
OUT.mkdir(parents=True,exist_ok=True)


async def main():
    working=ROOT/'data/moscow_commercial.db'
    before=hashlib.sha256(working.read_bytes()).hexdigest()
    prop=dict(id=1,title='Контрольное помещение',floor=1,
              description='Первый этаж, вход только по трём ступеням. Пандуса и подъёмника нет. Посетителям на колясках пройти невозможно.')
    resolver=BusinessFloorProfileResolver()
    profile=await resolver.resolve('Консультационный центр','Обязателен непрерывный путь от улицы до двери без ступеней для посетителей на инвалидных колясках')
    original=dict(candidate_id='c0',status='eligible',reason='Этаж известен',missing_facts=[],checks=[dict(
        requirement_id='floor_known',description='Этаж известен',critical=True,origin='inferred',result='met',evidence_ids=['f0'])])
    variants=[]
    def test(name,change):
        response=copy.deepcopy(original);change(response)
        try:
            assessment=resolver._validate_assessment(response,prop,profile)
            record=dict(case=name,status=assessment.status,reason=assessment.reason,missing_facts=assessment.missing_facts)
        except Exception as exc:record=dict(case=name,error_type=type(exc).__name__)
        variants.append(record)
    test('floor_only_omits_explicit_stepfree_requirement',lambda d:None)
    test('missing_facts_omitted',lambda d:d.pop('missing_facts'))
    test('missing_facts_null',lambda d:d.update(missing_facts=None))
    test('missing_facts_string_containing_real_gap',lambda d:d.update(missing_facts='Не подтверждён путь без ступеней'))
    test('missing_facts_object_containing_real_gap',lambda d:d.update(missing_facts={'critical':'Не подтверждён путь без ступеней'}))
    test('missing_facts_array_containing_real_gap',lambda d:d.update(missing_facts=['Не подтверждён путь без ступеней']))
    test('checks_missing',lambda d:d.pop('checks'))
    test('checks_wrong_type',lambda d:d.update(checks='met'))
    test('critical_wrong_type',lambda d:d['checks'][0].update(critical=1))
    test('invented_evidence_id',lambda d:d['checks'][0].update(evidence_ids=['fake']))
    test('invalid_origin',lambda d:d['checks'][0].update(origin='law'))
    test('invented_explicit_requirement',lambda d:d['checks'][0].update(origin='explicit',description='Клиент явно разрешил любой первый этаж'))
    test('missing_status_with_critical_failure',lambda d:(d.pop('status'),d['checks'][0].update(result='not_met')))
    test('unknown_status_with_critical_failure',lambda d:(d.update(status='INVALID'),d['checks'][0].update(result='not_met')))
    test('status_disagrees_with_checks',lambda d:d.update(status='rejected'))
    test('missing_reason',lambda d:d.pop('reason'))
    test('unknown_check_result',lambda d:d['checks'][0].update(result='maybe'))
    parser=[]
    for name,raw in [
        ('surrounding_non_json_text','model commentary '+json.dumps(original)+' trailing commentary'),
        ('duplicate_keys','{"status":"needs_verification","status":"eligible"}'),
        ('nonstandard_nan','{"fit_score":NaN}'),
        ('nonstandard_infinity','{"fit_score":Infinity}'),
        ('wrong_envelope','{"unrelated":true}'),
        ('truncated_json','{"assessments":['),
    ]:
        try:parser.append(dict(case=name,accepted=True,keys=list(_json_object(raw))))
        except Exception as exc:parser.append(dict(case=name,accepted=False,error_type=type(exc).__name__))

    # The two model responses satisfy every currently checked field, yet omit
    # the explicit access requirement. This demonstrates a semantic contract gap.
    def frozen_floor_only(payload):
        return {'assessments':[assessment_response({**p,'requirements':[]}) for p in payload['candidates']]}
    model=RecordingFloorInference(frozen_floor_only,frozen_floor_only)
    engine=BusinessFloorProfileResolver(model)
    decision=(await engine.assess_many([prop],profile))['1']
    two_model_case=dict(kind='controlled adversarial model outputs, not a new live response',
        property=prop,profile=asdict(profile),assessment=asdict(decision),calls=len(model.calls),
        explicit_contradiction_still_approved=decision.status=='eligible')

    # A malformed candidate result can be cached as if it were a valid uncertain answer.
    def missing_reason(payload):
        result=default_response(payload)
        for entry in result['assessments']:entry.pop('reason',None)
        return result
    cache_model=RecordingFloorInference(missing_reason)
    cached_resolver=BusinessFloorProfileResolver(cache_model)
    first=await cached_resolver.assess_many([prop],profile)
    calls_before=len(cache_model.calls)
    cache_model.handler=default_response
    second=await cached_resolver.assess_many([prop],profile)
    malformed_cache=dict(first_status=first['1'].status,second_status=second['1'].status,
        calls_before=calls_before,calls_after=len(cache_model.calls),
        malformed_response_cached=len(cache_model.calls)==calls_before)

    transport=[]
    for finish in ('stop','length','content_filter'):
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.model=analyzer.floor_model='controlled-model'
        analyzer.endpoint='https://example.test/inference'
        body={'choices':[{'finish_reason':finish,'message':{'content':json.dumps({'assessments':[original]})}}],
              'usage':{'completion_tokens':4500}}
        analyzer.http_session=SimpleNamespace(post=lambda *a,**k:SimpleNamespace(status_code=200,content=json.dumps(body).encode(),text='controlled response'))
        try:
            response=analyzer._call_floor_structured_sync('test', 1200, {'type':'object'})
            raw,used=response
            transport.append(dict(finish_reason=finish,accepted=True,returned_metadata=bool(getattr(response,'metadata',{}))))
        except Exception as exc:transport.append(dict(finish_reason=finish,accepted=False,error_type=type(exc).__name__))

    trace_summaries={}
    expanded=ROOT/'audit/expanded_test_2026-11-03'
    for prefix in ('','after/'):
        counts=Counter();models=Counter()
        for path in (expanded/prefix/'trace').glob('*.json'):
            data=json.loads(path.read_text(encoding='utf-8'))
            counts[data['task']]+=1
            if data.get('error_type'):counts[data['error_type']]+=1
            if data.get('model'):models[data['model']]+=1
        trace_summaries[prefix or 'before']=dict(counts=dict(counts),models=dict(models))

    competitor_results=[]
    class Maps:
        async def collect_in_circle(self,**kwargs):
            return MapCardsResult([business_card(org_id='one',categories=['Кофейня'])],status='ok')
    class Fns:
        async def enrich_cards(self,cards):return 0
    class Population:
        async def analyze_circle(self,**kwargs):return None
    good={'categories':[{'idx':1,'level':'direct','reason':'тот же товар'}],'summary':'Контроль'}
    conflict={'categories':[{'idx':1,'level':'direct','reason':'тот же товар'},
                            {'idx':1,'level':'unrelated','reason':'не связан'}],'summary':'Контроль'}
    competitor_variants=[('valid_direct',json.dumps(good)),('duplicate_idx_conflict',json.dumps(conflict)),
        ('bool_idx',json.dumps({'categories':[{'idx':True,'level':'direct'}]})),
        ('fractional_idx',json.dumps({'categories':[{'idx':1.8,'level':'direct'}]})),
        ('truncated_but_all_categories_extracted','{"categories":[{"idx":1,"level":"direct","reason":"тот же товар"}],"summary":"незаконченный'),
        ('missing_reason',json.dumps({'categories':[{'idx':1,'level':'direct'}]}))]
    for name,raw in competitor_variants:
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.model='controlled-model';analyzer.map_collector=Maps();analyzer.fns_collector=Fns();analyzer.population_analyzer=Population()
        async def classify(*args,**kwargs):
            return json.loads(raw),'controlled-model',{}
        analyzer._concept_classifier=SimpleNamespace(classify=classify)
        with patch('real_estate_engine.competitors.FootTrafficEstimator.estimate_for_circle',return_value=None):
            report=await analyzer.analyze_circle(LAT,LNG,400,'Кофейня',business_concept='Клиенты приходят за кофе с улицы.')
        competitor_results.append(dict(case=name,data_status=report.data_status,direct=len(report.direct_competitors),
            unrelated=len(report.unrelated_businesses),warnings=report.data_warnings))
    input_cases=[]
    for name,floor,text in [
        ('reception_on_another_floor',2,'Помещение на втором этаже. Ресепшн на первом этаже.'),
        ('building_height_is_not_offered_level',1,'Торговый центр имеет 2 этажа. Помещение на первом этаже.'),
        ('genuine_ad_floor_conflict',1,'Помещение расположено в подвале.')]:
        metadata=extract_floor_details(floor,description=text)
        evidence=extract_floor_evidence(dict(floor=metadata['floor'],description=text[:500],raw_attributes=metadata))
        input_cases.append(dict(case=name,declared_floor=floor,description=text,metadata=metadata,evidence=asdict(evidence)))
    data=dict(validation_mutations=variants,json_parser=parser,two_model_omission=two_model_case,
        malformed_cache=malformed_cache,transport=transport,saved_trace_statistics=trace_summaries,competitor_contract=competitor_results,input_floor_scope=input_cases,
        working_database_unchanged=before==hashlib.sha256(working.read_bytes()).hexdigest())
    (OUT/'results.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in data.items() if k not in ('two_model_omission','validation_mutations')},ensure_ascii=False,indent=2))
    print(json.dumps(dict(two_model_omission_approved=two_model_case['explicit_contradiction_still_approved'],
        validator_outcomes={r['case']:r.get('status',r.get('error_type')) for r in variants}),ensure_ascii=False,indent=2))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
