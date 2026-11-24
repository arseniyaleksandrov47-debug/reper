import asyncio
import copy
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from real_estate_engine.ai_contract import AI_DEADLINE
from real_estate_engine.business_model import premises_cost
from real_estate_engine.confirmation_flow import ConfirmationPrefetch
from real_estate_engine.premises_requirements import (
    physical_context_quotes,public_image_url,validate_requirements,aggregate_requirements,validate_requirement_proof)
from real_estate_engine.property_confirmation import PropertyConfirmationVerifier,validate_checks
from real_estate_engine.property_sources import addresses_match,avito_unit,cian_unit,jsonld_unit,unit_photos
from test_confirmation_cause_fixes import transport,response
from test_property_confirmation import prop,source,Sources,checks


QUOTE='Нужен санузел внутри помещения.'
OTHER='Нужен подъезд грузовика к воротам.'
PREFERENCE='Желателен вид во двор.'
CONCEPT='Произвольный формат бизнеса. '+QUOTE+' '+OTHER+' '+PREFERENCE
PHOTO='https://cdn.example.test/unit-1.jpg'


def row(key='a',quote=QUOTE,result='unknown',importance='mandatory'):
    return dict(id=key,client_quote=quote,requirement=quote,importance=importance,result=result,
        evidence_ids=[] if result=='unknown' else ['s0:destination'],reason='Контрольное условие')


class DetailedAnalyzer:
    property_assessment_model='text-primary'
    property_proof_model='visual-independent'
    def __init__(self,rows):
        self.rows=rows;self.calls=[];self.complete=True;self.photo_error=False;self.photo_supported=True;self.text_supported=True
        self.requirement_roles={}
        self.requirement_quotes={}
        self.missing_requirements=[]
    def _call_property_assessment_structured_sync(self,prompt,max_tokens,*,response_schema):
        data=json.loads(prompt.split('INPUT_JSON\n')[1]);self.calls.append(('text',data))
        result=checks()
        provided={row['id']:row for row in self.rows}
        items={row['id']:{k:provided.get(row['id'],row)[k] for k in ('result','evidence_ids','reason')}
            for row in data['existing_requirements']}
        result['client_requirements'].update(items=items)
        return json.dumps(result,ensure_ascii=False),self.property_assessment_model
    def _call_property_requirements_structured_sync(self,prompt,max_tokens,*,response_schema):
        data=json.loads(prompt.split('INPUT_JSON\n')[1]);self.calls.append(('extract',data))
        return json.dumps(dict(items=[dict(id=row['id'],requirement=row['requirement'],importance=row['importance'],
            client_source_id=next((k for k,v in data['client_fragments'].items() if row['client_quote'] in v),'unknown'))
            for row in self.rows]),ensure_ascii=False),self.property_assessment_model
    def _call_property_proof_structured_sync(self,prompt,max_tokens,*,response_schema):
        data=json.loads(prompt.split('INPUT_JSON\n')[1]);self.calls.append(('proof',data))
        if data.get('task')=='ground_premises_requirements':
            original_quotes={row['id']:row['client_quote'] for row in self.rows}
            fragments={quote:key for key,quote in data['client_fragments'].items()}
            result=dict(items=[dict(id=row['id'],role=self.requirement_roles.get(row['id'],row['importance']),
                client_source_id=fragments[self.requirement_quotes.get(row['id'],original_quotes[row['id']])],
                reason='Независимая проверка смысла исходной цитаты') for row in data['items']],
                missing_items=[dict(id=row['id'],requirement=row['requirement'],role=row['importance'],
                    client_source_id=fragments[row['client_quote']]) for row in self.missing_requirements])
            return json.dumps(result,ensure_ascii=False),self.property_proof_model
        if data.get('task')=='check_published_conditions':
            findings={}
            for claim in self.rows:
                finding=dict(finding={'pass':'confirmed_met','fail':'explicit_conflict'}.get(claim['result'],'unclear')
                    if self.text_supported else 'unclear',reason='Контрольная независимая проверка')
                if finding['finding']=='explicit_conflict':
                    ref=claim['evidence_ids'][0]
                    finding['contradiction']=dict(evidence_id=ref,quote=data['evidence'][ref]['value'],applies_to_unit=True)
                findings[claim['id']]=finding
            if not self.complete and findings:findings.pop(next(iter(findings)))
            return json.dumps(dict(findings=findings)),self.property_proof_model
        if 'items' in data['claim']:
            result=dict(items=[dict(id=i['id'],
                verdict='unknown' if i['result']=='unknown' else 'supported' if self.text_supported else 'unsupported',reason='Контрольная независимая проверка') for i in data['claim']['items']])
            if not self.complete and result['items']:result['items'].pop()
        else:result=dict(verdict='supported',reason='Контрольная независимая проверка')
        return json.dumps(result,ensure_ascii=False),self.property_proof_model
    def _call_property_visual_sync(self,prompt,max_tokens,*,response_schema,image_urls):
        data=json.loads(prompt.split('INPUT_JSON\n')[1]);self.calls.append(('vision',data,image_urls))
        if self.photo_error:raise TimeoutError('controlled timeout')
        return json.dumps(dict(observations=[dict(requirement_id=data['requirements'][0]['id'],
            observation='На контрольном фото виден унитаз.',image_ids=['p0'],
            confirms_visible_requirement=self.photo_supported)])),self.property_proof_model


class RequirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_conditions_with_repeated_provider_ids_are_preserved(self):
        ai=DetailedAnalyzer([])
        ai.missing_requirements=[row('same',QUOTE),row('same',OTHER)]
        rows=await PropertyConfirmationVerifier(ai,Sources([]))._requirements(QUOTE+' '+OTHER)
        self.assertEqual({r['client_quote'] for r in rows},{QUOTE,OTHER})
        self.assertEqual(len({r['id'] for r in rows}),2)

    async def test_repeated_provider_ids_do_not_erase_different_client_conditions(self):
        class RepeatedIds(DetailedAnalyzer):
            def _call_property_requirements_structured_sync(self,prompt,max_tokens,*,response_schema):
                raw,model=super()._call_property_requirements_structured_sync(prompt,max_tokens,response_schema=response_schema)
                value=json.loads(raw)
                for item in value['items']:item['id']='same_source_id'
                return json.dumps(value),model
            def _call_property_proof_structured_sync(self,prompt,max_tokens,*,response_schema):
                data=json.loads(prompt.split('INPUT_JSON\n')[1])
                if data.get('task')!='ground_premises_requirements':return super()._call_property_proof_structured_sync(prompt,max_tokens,response_schema=response_schema)
                return json.dumps(dict(items=[dict(id=r['id'],role='mandatory',
                    client_source_id=next(k for k,v in data['client_fragments'].items() if v==r['requirement']),
                    reason='Независимо проверенное физическое условие') for r in data['items']],missing_items=[])),self.property_proof_model
        ai=RepeatedIds([row('a',QUOTE),row('b',OTHER)])
        rows=await PropertyConfirmationVerifier(ai,Sources([]))._requirements(QUOTE+' '+OTHER)
        self.assertEqual({r['client_quote'] for r in rows},{QUOTE,OTHER})
        self.assertEqual(len({r['id'] for r in rows}),2)

    async def test_category_title_alone_cannot_prove_a_business_use_prohibition(self):
        class Review(DetailedAnalyzer):
            def _call_property_proof_structured_sync(self,prompt,max_tokens,*,response_schema):
                return json.dumps(dict(findings={'concept_use':dict(finding='explicit_conflict',
                    reason='Модель ошибочно вывела запрет из названия',contradiction=dict(
                        evidence_id='s0:title',quote='Объект категории X',applies_to_unit=True))})),self.property_proof_model
        verifier=PropertyConfirmationVerifier(Review([]),Sources([]))
        catalog={'s0:title':dict(value='Объект категории X')}
        claim=dict(result='fail',evidence_ids=['s0:title'],reason='Предполагаемая несовместимость')
        proof,_=await verifier._review_published_conditions('Произвольная деятельность',catalog,'concept_use',claim,None)
        self.assertEqual(proof['verdict'],'unknown')

    async def test_independent_owner_routes_access_away_from_physical_requirements(self):
        access='Клиенты должны иметь разрешённый доступ в часы работы.'
        ai=DetailedAnalyzer([row('access',access),row('physical',QUOTE)])
        ai.requirement_roles={'access':'access'}
        rows=await PropertyConfirmationVerifier(ai,Sources([]))._requirements(access+' '+QUOTE)
        self.assertEqual([r['client_quote'] for r in rows],[QUOTE])

    async def run_case(self,rows,photos=None,ai=None,p=None):
        ai=ai or DetailedAnalyzer(rows)
        candidate=SimpleNamespace(property_data=p or prop(),business_model_diagnostics={'cost':{'missing_inputs':[]}})
        result=await PropertyConfirmationVerifier(ai,Sources([source(photos=photos or [])])).verify(candidate,CONCEPT,{})
        return result,ai

    async def test_separate_known_and_unknown_conditions_are_preserved(self):
        result,ai=await self.run_case([row(result='pass'),row('b',OTHER)])
        rows=result['checks']['client_requirements']['items']
        self.assertEqual([r['result'] for r in rows],['pass','unknown'])
        self.assertEqual(result['status'],'requires_confirmation')
        self.assertFalse(any(c[0]=='vision' for c in ai.calls))

    async def test_missing_photo_is_not_evidence_of_absence(self):
        result,_=await self.run_case([row(),row('b',OTHER)])
        self.assertEqual(result['checks']['client_requirements']['result'],'unknown')
        self.assertNotEqual(result['status'],'not_suitable')

    async def test_explicit_failure_of_required_condition_excludes(self):
        result,_=await self.run_case([row(result='fail')])
        self.assertEqual(result['status'],'not_suitable')

    async def test_preference_does_not_become_a_mandatory_filter(self):
        result,_=await self.run_case([row(result='pass'),row('p',PREFERENCE,'fail','preferred')])
        self.assertEqual(result['status'],'source_conditions_checked')
        self.assertEqual(result['checks']['client_requirements']['items'][1]['result'],'fail')

    async def test_unmentioned_requirement_cannot_be_invented(self):
        result,_=await self.run_case([row(quote='Нужен придуманный параметр')])
        self.assertEqual(result['status'],'requires_confirmation')
        self.assertEqual(result['verification_error']['type'],'ValueError')

    async def test_incomplete_extraction_cannot_pass(self):
        ai=DetailedAnalyzer([row(result='pass')]);ai.complete=False
        result,_=await self.run_case(ai.rows,ai=ai)
        self.assertEqual(result['checks']['client_requirements']['result'],'unknown')

    async def test_no_extra_photographs_when_text_covers_requirements(self):
        result,ai=await self.run_case([row(result='pass')],[PHOTO])
        self.assertEqual(result['status'],'source_conditions_checked')
        self.assertFalse(any(c[0]=='vision' for c in ai.calls))

    async def test_independent_photo_observation_is_linked_to_requirement(self):
        result,ai=await self.run_case([row(),row('b',OTHER)],[PHOTO])
        rows=result['checks']['client_requirements']['items']
        self.assertEqual([r['result'] for r in rows],['pass','unknown'])
        calls=[c for c in ai.calls if c[0]=='vision']
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][2],[PHOTO]);self.assertEqual(rows[0]['label'],'Видно на фото объявления')
        self.assertIn('visual:0',result['evidence'])
        self.assertEqual(result['photo_analysis']['status'],'observations_ready')

    async def test_partial_visual_observation_does_not_confirm_feature(self):
        ai=DetailedAnalyzer([row()]);ai.photo_supported=False
        result,_=await self.run_case(ai.rows,[PHOTO],ai=ai)
        self.assertEqual(result['checks']['client_requirements']['items'][0]['result'],'unknown')

    async def test_rejected_text_overclaim_leads_to_photo_check(self):
        ai=DetailedAnalyzer([row(result='pass')]);ai.text_supported=False;ai.photo_supported=False
        result,_=await self.run_case(ai.rows,[PHOTO],ai=ai)
        self.assertEqual(result['checks']['client_requirements']['items'][0]['result'],'unknown')
        self.assertTrue(any(call[0]=='vision' for call in ai.calls))

    async def test_visual_timeout_keeps_completed_text_checks(self):
        ai=DetailedAnalyzer([row()]);ai.photo_error=True
        result,_=await self.run_case(ai.rows,[PHOTO],ai=ai)
        self.assertEqual(result['photo_analysis']['status'],'unavailable')
        self.assertEqual(result['checks']['concept_use']['result'],'pass')
        self.assertEqual(result['checks']['client_requirements']['result'],'unknown')

    async def test_small_remaining_budget_skips_photos(self):
        token=AI_DEADLINE.set(time.monotonic()+7)
        try:result,ai=await self.run_case([row()],[PHOTO])
        finally:AI_DEADLINE.reset(token)
        self.assertFalse(any(c[0]=='vision' for c in ai.calls))
        self.assertEqual(result['checks']['availability']['result'],'pass')

    async def test_collected_but_unbound_photos_are_not_used(self):
        result,ai=await self.run_case([row()],p=prop(photos_json=json.dumps([PHOTO])))
        self.assertFalse(any(c[0]=='vision' for c in ai.calls))
        self.assertEqual(result['status'],'requires_confirmation')

    async def test_duplicate_source_photos_do_not_duplicate_image_inputs(self):
        result,ai=await self.run_case([row()],[PHOTO,PHOTO])
        self.assertEqual(result['photo_analysis']['photo_count'],1)

    async def test_no_conditions_still_requires_completeness_review(self):
        result,ai=await self.run_case([])
        self.assertEqual(result['checks']['client_requirements']['result'],'not_applicable')
        proof=[c[1] for c in ai.calls if c[0]=='proof' and 'items' in c[1].get('claim',{})]
        self.assertEqual(len(proof),1)

    async def test_extraction_cannot_see_advertisement_and_is_reused(self):
        ai=DetailedAnalyzer([row(),row('b',OTHER)])
        verifier=PropertyConfirmationVerifier(ai,Sources([source()]))
        candidate=SimpleNamespace(property_data=prop(),business_model_diagnostics={'cost':{'missing_inputs':[]}})
        await asyncio.gather(*(verifier.verify(candidate,CONCEPT,{}) for _ in range(2)))
        extraction=[c[1] for c in ai.calls if c[0]=='extract']
        self.assertEqual(len(extraction),1)
        self.assertEqual(set(extraction[0]),{'client_fragments'})
        self.assertIn(QUOTE,extraction[0]['client_fragments'].values())
        await verifier.verify(candidate,CONCEPT,{})
        self.assertEqual(len([c for c in ai.calls if c[0]=='extract']),1)

    async def test_omitted_extracted_condition_fails_closed(self):
        ai=DetailedAnalyzer([row(),row('b',OTHER)])
        original=ai._call_property_assessment_structured_sync
        def omit(*args,**kwargs):
            text,model=original(*args,**kwargs);data=json.loads(text)
            data['client_requirements']['items']=[]
            return json.dumps(data),model
        ai._call_property_assessment_structured_sync=omit
        result,_=await self.run_case(ai.rows,ai=ai)
        self.assertEqual(result['status'],'requires_confirmation')
        self.assertEqual(result['verification_error']['type'],'ValueError')

    async def test_changed_concept_is_extracted_again(self):
        ai=DetailedAnalyzer([row()]);verifier=PropertyConfirmationVerifier(ai,Sources([]))
        first=await verifier._requirements(CONCEPT)
        first[0]['requirement']='changed'
        second=await verifier._requirements(CONCEPT)
        self.assertNotEqual(first,second)
        await verifier._requirements(CONCEPT+' Другой сценарий.')
        self.assertEqual(len([c for c in ai.calls if c[0]=='extract']),2)

    async def test_source_expiry_invalidates_individual_rows_too(self):
        clock=[1000];ai=DetailedAnalyzer([row(result='pass')])
        original=ai._call_property_proof_structured_sync
        def expire(*args,**kwargs):
            result=original(*args,**kwargs);clock[0]=1301;return result
        ai._call_property_proof_structured_sync=expire
        with patch('real_estate_engine.property_confirmation.time.time',side_effect=lambda:clock[0]):
            result,_=await self.run_case(ai.rows,ai=ai)
        self.assertEqual(result['checks']['client_requirements']['items'][0]['result'],'unknown')

    async def test_unavailable_source_preserves_extracted_client_conditions(self):
        ai=DetailedAnalyzer([row(),row('b',OTHER)])
        verifier=PropertyConfirmationVerifier(ai,Sources([]))
        candidate=SimpleNamespace(property_data=prop(),business_model_diagnostics={'cost':{'missing_inputs':[]}})
        result=await verifier.verify(candidate,CONCEPT,{})
        self.assertEqual(result['status'],'requires_confirmation')
        self.assertEqual(result['requirement_extraction']['status'],'complete')
        self.assertEqual([r['client_quote'] for r in result['checks']['client_requirements']['items']],[QUOTE,OTHER])
        self.assertTrue(all(r['result']=='unknown' for r in result['checks']['client_requirements']['items']))
        self.assertEqual(result['processing_status'],'unavailable')
        self.assertEqual(result['photo_analysis']['status'],'unavailable')
        self.assertFalse(any(c[0] in ('text','vision') for c in ai.calls))
        self.assertTrue(all(c[1].get('task')=='ground_premises_requirements' for c in ai.calls if c[0]=='proof'))

    async def test_source_collection_error_keeps_conditions_without_leaking_error_text(self):
        class BrokenSource:
            def collect(self,prop):
                raise RuntimeError('private connection details')
        ai=DetailedAnalyzer([row()])
        verifier=PropertyConfirmationVerifier(ai,BrokenSource())
        candidate=SimpleNamespace(property_data=prop(),business_model_diagnostics={'cost':{'missing_inputs':[]}})
        result=await verifier.verify(candidate,CONCEPT,{})
        self.assertEqual(len(result['checks']['client_requirements']['items']),1)
        self.assertEqual(result['status'],'requires_confirmation')
        self.assertNotIn('private connection details',json.dumps(result))

    async def test_waiver_and_permission_do_not_become_property_requirements(self):
        flow='Случайный поток прохожих не нужен.'
        permit='Пропуск допустим.'
        concept=CONCEPT+' '+flow+' '+permit
        ai=DetailedAnalyzer([row()])
        rows=await PropertyConfirmationVerifier(ai,Sources([]))._requirements(concept)
        self.assertEqual([r['id'] for r in rows],['a'])
        self.assertTrue({flow,permit}.isdisjoint(ai.calls[0][1]['client_fragments'].values()))

    async def test_independent_review_repairs_contract_and_uses_only_property_fragments(self):
        booking='Клиенты приезжают по записи.'
        floor='Этаж не ограничиваю.'
        permit='Регистрация на ресепшене допустима.'
        cost='Главный приоритет — низкая аренда.'
        concept=' '.join((booking,floor,permit,cost,QUOTE,OTHER))
        profile=SimpleNamespace(metadata={'scenario_contract':dict(
            context=[dict(source_id='s0',quote=booking)],premises_requirements=[],waivers=[])},
            criteria=[SimpleNamespace(metric='premises_cost_month',concept_quote=cost)])
        ai=DetailedAnalyzer([])
        ai.missing_requirements=[row(),row('b',OTHER)]
        original=ai._call_property_proof_structured_sync
        attempts=[]
        def review(prompt,*args,**kwargs):
            raw,model=original(prompt,*args,**kwargs)
            attempts.append(prompt)
            if len(attempts)==1:
                value=json.loads(raw)
                item=value['missing_items'][0]
                item['importance']=item.pop('role')
                return json.dumps(value),model
            return raw,model
        ai._call_property_proof_structured_sync=review
        rows=await PropertyConfirmationVerifier(ai,Sources([]))._requirements(concept,physical_context_quotes(profile))
        self.assertEqual([r['client_quote'] for r in rows],[QUOTE,OTHER])
        self.assertEqual(len(attempts),2)
        self.assertEqual(set(ai.calls[0][1]['client_fragments'].values()),{QUOTE,OTHER})

    async def test_ambiguous_client_interpretation_cannot_impose_a_hard_filter(self):
        ai=DetailedAnalyzer([row()]);ai.requirement_roles={'a':'unknown'}
        with self.assertRaises(ValueError):
            await PropertyConfirmationVerifier(ai,Sources([]))._requirements(CONCEPT)

    async def test_independent_grounding_rebinds_a_wrong_client_quote(self):
        ai=DetailedAnalyzer([{**row(quote=OTHER),'requirement':QUOTE}])
        ai.requirement_quotes={'a':QUOTE}
        rows=await PropertyConfirmationVerifier(ai,Sources([]))._requirements(CONCEPT)
        self.assertEqual(rows[0]['client_quote'],QUOTE)
        review=next(call[1] for call in ai.calls if call[0]=='proof')
        self.assertNotIn('client_quote',review['items'][0])

    async def test_empty_extraction_does_not_erase_conditions_when_source_unavailable(self):
        ai=DetailedAnalyzer([])
        ai.missing_requirements=[row(),row('b',OTHER)]
        verifier=PropertyConfirmationVerifier(ai,Sources([]))
        candidate=SimpleNamespace(property_data=prop(),business_model_diagnostics={'cost':{'missing_inputs':[]}})
        result=await verifier.verify(candidate,CONCEPT,{})
        self.assertEqual(result['status'],'requires_confirmation')
        rows=result['checks']['client_requirements']['items']
        self.assertEqual([(r['client_quote'],r['result']) for r in rows],[(QUOTE,'unknown'),(OTHER,'unknown')])


class InputTests(unittest.TestCase):
    def test_address_abbreviations_preserve_street_type_and_building_identity(self):
        for left,right,expected in (
            ('Москва, Михайловский проезд, 1с1','Москва, Михайловский пр., 1с1',True),
            ('Михайловский пр-д, 1 строение 1','Михайловский проезд, д. 1с1',True),
            ('Ленинский пр-т, 10','Ленинский проспект, 10',True),
            ('Ленинский просп., 10','Ленинский пр., 10',True),
            ('Михайловский проезд, 1с1','Михайловский проспект, 1с1',False),
            ('Михайловский пр-д, 1с1','Михайловский пр-т, 1с1',False),
            ('Михайловский проезд, 1с1','Михайловский пр., 1с2',False),
            ('Михайловский проезд, 1с1','Михайловский пр., 2с1',False),
            ('Михайловский проезд, 1с1','Михайловский пр., 1к1',False),
            ('Михайловский проезд, 1с1','Другой пр., 1с1',False),
            ('','',False),(None,'',False),
        ):
            with self.subTest(left=left,right=right):
                self.assertEqual(addresses_match(left,right),expected)
                self.assertEqual(addresses_match(right,left),expected)

    def test_avito_detail_uses_only_exact_unit_state_and_explicit_price_period(self):
        url='https://www.avito.ru/moskva/kommercheskaya_nedvizhimost/ofis_12345'
        p=dict(external_id='12345',url=url,address='Москва, Миусская площадь, 5',area_sqm=71.1)
        item=dict(id=12345,address='Москва, Миусская пл., 5',isActive=True,title='Офис',
            formattedPrice=dict(currencySign='₽',postfix='за м² в месяц',value=1000),
            imageUrls=[{'1280x960':'https://img.avito.st/exact.jpg'}])
        params=dict(itemId=12345,items=[dict(title='Общая площадь',description='71,1 м²'),
            dict(title='Этаж',description='цокольный')])
        def body():
            data={'loaderData':{'catalog-or-main-or-item':{'buyerItem':{'item':item,'paramsDto':params}}}}
            return 'window.__staticRouterHydrationData = JSON.parse('+json.dumps(json.dumps(data))+');'
        unit=avito_unit(body(),url,p)
        self.assertEqual((unit['price_month'],unit['floor']),(71100,0))
        self.assertEqual(unit['photos'],['https://img.avito.st/exact.jpg'])
        item['formattedPrice']['postfix']=''
        self.assertIsNone(avito_unit(body(),url,p)['price_month'])
        p['address']='Москва, Михайловский проезд, 1с1'
        item['address']='Москва, Михайловский пр., 1с1'
        self.assertTrue(avito_unit(body(),url,p)['identity_checked'])
        item['address']='Москва, Михайловский пр., 1с2'
        with self.assertRaises(ValueError):avito_unit(body(),url,p)
        item['address']='Москва, Михайловский пр., 1с1'
        params['itemId']=99999
        with self.assertRaises(ValueError):avito_unit(body(),url,p)

    def test_transport_sends_real_image_parts_with_no_text_fallback(self):
        seen=[];ai=transport(lambda *a,**kw:(seen.append(kw) or response()))
        ai.property_proof_model='visual-model'
        ai._call_property_visual_sync('prompt',800,response_schema={'type':'object'},image_urls=[PHOTO])
        content=seen[0]['json']['messages'][-1]['content']
        self.assertEqual(content[0]['type'],'text')
        self.assertEqual(content[1],dict(type='image_url',image_url={'url':PHOTO}))

    def test_photos_restricted_to_public_https_without_embedded_credentials(self):
        for bad in ('file:///C:/secret','http://cdn.example.test/photo','https://127.0.0.1/a',
                    'https://user:pass@cdn.example.test/a','https://localhost/a','https://host.internal/a'):
            self.assertFalse(public_image_url(bad),bad)
        self.assertTrue(public_image_url(PHOTO))

    def test_bound_jsonld_images_exclude_other_gallery(self):
        p=prop();data=dict(url=p['url'],floorSize={'value':46},address={'streetAddress':p['address']},image=[PHOTO])
        body='<img src="https://other.example.test/other.jpg"><script type="application/ld+json">'+json.dumps(data)+'</script>'
        self.assertEqual(jsonld_unit(body,p['url'],p)['photos'],[PHOTO])

    def test_cian_own_card_photos_read_from_recorded_source(self):
        root=Path(__file__).resolve().parent/'fixtures/audit/client_fixes/sources'
        p=json.loads((root/'cian_expected.json').read_text(encoding='utf-8'))
        result=cian_unit((root/'cian_unit.html').read_text(encoding='utf-8'),p['url'],p)
        self.assertGreater(len(result['photos']),0);self.assertLessEqual(len(result['photos']),3)
        self.assertTrue(all(public_image_url(v) for v in result['photos']))

    def test_unit_photo_selection_is_bounded_and_deduplicated(self):
        urls=[dict(fullUrl=PHOTO),dict(fullUrl=PHOTO),*[f'https://cdn.example.test/{i}.jpg' for i in range(8)]]
        self.assertEqual(len(unit_photos(urls,'https://example.test/unit')),3)

    def test_unknown_and_invalid_zero_preparation_are_distinct(self):
        fact=lambda value:dict(value=value,source='client',kind='estimate')
        value,details=premises_cost(dict(price_month=100000),dict(premises_cost={'operating_month':fact(10000)}))
        self.assertEqual(value.high,110000);self.assertNotIn('fitout_month',details['parts'])
        self.assertEqual(details['preparation_scope'],'tenant_managed');self.assertFalse(details['missing_inputs'])
        _,bad=premises_cost(dict(price_month=100000),dict(premises_cost={'operating_month':fact(10000),'fitout_total':{'value':0}}))
        self.assertIn('fitout_total',{i['field'] for i in bad['missing_inputs']})

    def test_photo_change_invalidates_prefetch(self):
        candidate=SimpleNamespace(property_data=prop(photos=[PHOTO]),business_model_diagnostics={})
        old=ConfirmationPrefetch.signature(candidate)
        candidate.property_data['photos']=['https://cdn.example.test/changed.jpg']
        self.assertNotEqual(old,ConfirmationPrefetch.signature(candidate))

    def test_requirement_ids_and_quotes_are_checked(self):
        for rows in ([row(),row()],[row(quote='придуманное')]):
            with self.assertRaises(ValueError):validate_requirements(rows,{},CONCEPT)

    def test_incomplete_or_duplicate_proof_is_rejected(self):
        rows=[row(),row('b',OTHER)]
        for proof in (dict(requirements_complete=True,items=[]),dict(requirements_complete=True,items=[dict(id='a',verdict='supported',reason='x')]*2)):
            with self.assertRaises(ValueError):validate_requirement_proof(proof,rows,{})

    def test_negative_physical_proof_requires_a_direct_cited_excerpt(self):
        claim=row(result='fail')
        ref=claim['evidence_ids'][0]
        catalog={ref:{'value':'В помещении санузла нет.'}}
        proof=dict(id=claim['id'],verdict='supported',reason='Проверка')
        excerpt=dict(evidence_id=ref,quote=catalog[ref]['value'],applies_to_unit=True)
        for contradiction,expected in (
            (None,'unknown'),
            ({**excerpt,'applies_to_unit':False},'unknown'),
            ({**excerpt,'quote':'В здании санузла нет.'},'unknown'),
            (excerpt,'supported'),
        ):
            with self.subTest(contradiction=contradiction):
                answer={**proof,'contradiction':contradiction}
                checked=validate_requirement_proof(dict(requirements_complete=True,items=[answer]),[claim],catalog)
                self.assertEqual(checked['items'][0]['verdict'],expected)

    def test_photo_observation_cannot_confirm_rental_availability(self):
        primary=checks();primary['availability']['evidence_ids']=['visual:0']
        with self.assertRaises(ValueError):validate_checks(primary,{'s0:freeDate':{},'visual:0':{'kind':'model_visual_observation'}})


if __name__=='__main__':unittest.main()
