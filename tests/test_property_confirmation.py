from floor_ai_fixtures import ranking_transport_fixture
import asyncio
import copy
import json
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from real_estate_engine.confirmation_flow import property_key, find_more
from real_estate_engine.environment_cache import EnvironmentSourceCache
from real_estate_engine.property_confirmation import PropertyConfirmationVerifier, CHECKS, validate_checks
from real_estate_engine.property_sources import PublicPropertySources, mega_unit, jsonld_unit, yandex_unit
from real_estate_engine import OptimalPropertyRecommender,PlatformSource
from test_engine_regressions import memory_storage, property_offer, competition_report, LAT, LNG
from floor_ai_fixtures import FixtureFloorAnalyzer, ranking_fixture_response


ARCHIVE=Path(__file__).resolve().parent/'fixtures/audit/practical_trial/run_20261105_003117/property_confirmation'
LOT_URL='https://www.mega-realty.ru/ulitsa_zemlyanoj_val_34as2_b28454.htm#arenda_pomescheniya_245807'


def prop(**changes):
    result=dict(id=1042,source='domclick',external_id='2081945577',lat=LAT,lng=LNG,
        address='Москва, улица Земляной Вал, 34а ст2',area_sqm=46,price_month=114617,floor=1,
        description='Лот №245807. Офис, магазин (кроме продуктов), салон.',url='https://domclick.ru/card/rent__free_purpose__2081945577')
    result.update(changes);return result


def source(**changes):
    result=dict(identity_checked=True,fetched_at=time.time(),url=LOT_URL,area_sqm=46,price_month=114617,floor='1-й этаж',
                facts=dict(freeDate='помещение свободно',destination='офис, магазин (кроме продуктов), салон',
                    access='Круглосуточный доступ. На охраняемой территории.'))
    result.update(changes);return result


def checks(**changes):
    result={k:dict(result='pass',evidence_ids=['s0:freeDate'],reason='Источник подтверждает условие') for k in CHECKS}
    for key,value in changes.items():result[key]=dict(result=value,evidence_ids=[] if value in ('unknown','not_applicable') else ['s0:destination'],reason='Нужно уточнение' if value=='unknown' else 'Подтверждено источником')
    return result


class Sources:
    def __init__(self,values):self.values=values
    def collect(self,p):return dict(sources=self.values,failures=[])


class SemanticAnalyzer:
    def __init__(self,primary=None,review=None):
        self.primary=primary or checks();self.review=review or self.primary;self.calls=[]
    def _call_property_requirements_structured_sync(self,prompt,max_tokens,*,response_schema):
        return json.dumps(dict(items=[])), 'primary'

    def _call_property_assessment_structured_sync(self,prompt,max_tokens,*,response_schema):
        self.calls.append((prompt,False))
        payload=json.loads(prompt.split('INPUT_JSON\n')[1])
        answer=self.primary(payload) if callable(self.primary) else self.primary
        if isinstance(answer,Exception):raise answer
        answer=copy.deepcopy(answer)
        answer['client_requirements']['items']={}
        return json.dumps(answer,ensure_ascii=False),'primary'

    def _call_property_proof_structured_sync(self,prompt,max_tokens,*,response_schema):
        payload=json.loads(prompt.split('INPUT_JSON\n')[1])
        model=getattr(self,'property_proof_model','independent')
        if payload.get('task')=='ground_premises_requirements':
            return json.dumps(dict(items=[],missing_items=[])),model
        self.calls.append((prompt,True))
        if payload['check_id']=='client_requirements' and 'items' in payload['claim']:
            return json.dumps(dict(items=[])),model
        answer=self.review(payload) if callable(self.review) else self.review
        if isinstance(answer,Exception):raise answer
        key=payload['check_id']
        if payload.get('task')=='check_published_conditions':
            answer=answer[key]
            finding=dict(finding={'fail':'explicit_conflict','pass':'confirmed_met'}.get(answer['result'],'unclear'),
                reason='Независимая проверка доказательств')
            if answer['result']=='fail':
                ref=answer['evidence_ids'][0]
                finding['contradiction']=dict(evidence_id=ref,quote=payload['evidence'][ref]['value'],applies_to_unit=True)
            return json.dumps(dict(findings={key:finding}),ensure_ascii=False),model
        claim=payload['claim']
        proof=dict(verdict='supported' if answer[key]['result']==claim['claimed_result'] and answer[key]['result']!='unknown' else 'unknown',reason='Независимая проверка доказательств')
        return json.dumps(proof,ensure_ascii=False),model


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_rent_cannot_cross_client_budget_inside_change_tolerance(self):
        ai=SemanticAnalyzer();verifier=PropertyConfirmationVerifier(ai,Sources([source(price_month=250050)]))
        candidate=SimpleNamespace(property_data=prop(price_month=250000),business_model_diagnostics={'cost':{'missing_inputs':[]}})
        result=await verifier.verify(candidate,'Помещение по условиям клиента',dict(max_price_month=250000))
        self.assertNotEqual(result['status'],'source_conditions_checked')

    async def test_source_expiring_during_ai_cannot_confirm_at_decision_time(self):
        ai=SemanticAnalyzer();clock=[1000]
        verifier=PropertyConfirmationVerifier(ai,Sources([source(fetched_at=1000)]))
        original=ai._call_property_proof_structured_sync
        def expiring(*args,**kwargs):
            result=original(*args,**kwargs);clock[0]=1301;return result
        ai._call_property_proof_structured_sync=expiring
        candidate=SimpleNamespace(property_data=prop(),business_model_diagnostics={'cost':{'missing_inputs':[]}})
        with patch('real_estate_engine.property_confirmation.time.time',side_effect=lambda:clock[0]):
            result=await verifier.verify(candidate,'Концепция',{})
        self.assertEqual(result['status'],'requires_confirmation')

    async def test_nonfinite_or_missing_current_rent_cannot_confirm(self):
        for value in (float('nan'),float('inf'),None,True):
            with self.subTest(value=value):
                result,_=await self.run_case(sources=[source(price_month=value)])
                self.assertEqual(result['status'],'requires_confirmation')

    async def test_blank_independent_proof_is_not_accepted(self):
        ai=SemanticAnalyzer()
        original=ai._call_property_proof_structured_sync
        def blank(prompt,*a,**kw):
            if json.loads(prompt.split('INPUT_JSON\n')[1]).get('check_id')=='access':
                return json.dumps(dict(verdict='supported',reason=' ')),'independent'
            return original(prompt,*a,**kw)
        ai._call_property_proof_structured_sync=blank
        result,_=await self.run_case(ai)
        self.assertEqual(result['status'],'requires_confirmation')
        self.assertEqual(result['checks']['access']['result'],'unknown')

    async def test_independent_conditions_do_not_erase_other_verified_facts(self):
        ai=SemanticAnalyzer();original=ai._call_property_proof_structured_sync
        def partial(prompt,*args,**kwargs):
            if json.loads(prompt.split('INPUT_JSON\n')[1]).get('check_id')=='access':raise TimeoutError('offline')
            return original(prompt,*args,**kwargs)
        ai._call_property_proof_structured_sync=partial
        result,_=await self.run_case(ai)
        self.assertEqual(result['status'],'requires_confirmation')
        self.assertEqual(result['checks']['access']['result'],'unknown')
        self.assertEqual(result['checks']['concept_use']['result'],'pass')

    async def test_fresh_area_and_unit_rent_respect_client_limits(self):
        for values,parameters in ((dict(area_sqm=46.4),dict(max_area_sqm=46.2)),
                                  (dict(area_sqm=45.6),dict(min_area_sqm=45.8)),
                                  (dict(price_month=114617),dict(max_price_sqm=2491,price_sqm_period='month'))):
            with self.subTest(parameters=parameters):
                ai=SemanticAnalyzer();verifier=PropertyConfirmationVerifier(ai,Sources([source(**values)]))
                candidate=SimpleNamespace(property_data=prop(),business_model_diagnostics={'cost':{'missing_inputs':[]}})
                result=await verifier.verify(candidate,'Концепция',parameters)
                self.assertEqual(result['status'],'not_suitable')
                self.assertTrue(any(r['code'].startswith('filter:') for r in result['reasons']))

    async def test_recorded_live_overclaims_cannot_pass_even_when_costs_known(self):
        record=json.loads((ARCHIVE.parents[2]/'records/automatic_confirmation_live.json').read_text(encoding='utf-8'))['confirmation']
        primary={k:v['primary'] for k,v in record['checks'].items()}
        proof={k:v['independent'] for k,v in record['checks'].items()}
        ai=SemanticAnalyzer(primary)
        original=ai._call_property_proof_structured_sync
        def replay(prompt,*a,**kw):
            key=json.loads(prompt.split('INPUT_JSON\n')[1]).get('check_id')
            if key in proof and key!='client_requirements':
                return json.dumps(proof[key],ensure_ascii=False),'independent'
            return original(prompt,*a,**kw)
        ai._call_property_proof_structured_sync=replay
        values=copy.deepcopy(record['sources']);values[0]['fetched_at']=time.time()
        result,_=await self.run_case(ai,sources=values)
        self.assertEqual(result['status'],'requires_confirmation')
        self.assertTrue({'concept_use','access'}<={r['code'] for r in result['reasons']})

    async def run_case(self,analyzer=None,sources=None,cost=None,p=None):
        analyzer=analyzer or SemanticAnalyzer()
        candidate=SimpleNamespace(property_data=p or prop(),business_model_diagnostics={'cost':cost if cost is not None else {'missing_inputs':[]}})
        verifier=PropertyConfirmationVerifier(analyzer,Sources([source()] if sources is None else sources))
        return await verifier.verify(candidate,'Клиенты с улицы. Нужен свободный доступ.',{}),analyzer

    async def test_agreement_checks_conditions_without_claiming_counterparty_reply(self):
        result,ai=await self.run_case()
        self.assertEqual(result['status'],'source_conditions_checked')
        self.assertFalse(result['counterparty_confirmation']);self.assertFalse(result['on_site_inspection'])
        self.assertEqual(len(ai.calls),5)
        self.assertNotIn('primary',json.loads(ai.calls[1][0].split('INPUT_JSON\n')[1]))

    async def test_ambiguous_use_or_guarded_access_requires_confirmation(self):
        result,_=await self.run_case(SemanticAnalyzer(checks(concept_use='unknown',access='unknown')))
        self.assertEqual(result['status'],'requires_confirmation')
        self.assertTrue({'concept_use','access'}<={r['code'] for r in result['reasons']})

    async def test_explicit_unsuitability_is_removed_only_on_agreement(self):
        result,_=await self.run_case(SemanticAnalyzer(checks(concept_use='fail')))
        self.assertEqual(result['status'],'not_suitable')
        result,_=await self.run_case(SemanticAnalyzer(checks(concept_use='fail'),checks(concept_use='pass')))
        self.assertEqual(result['status'],'requires_confirmation')

    async def test_negative_access_proof_checks_unit_applicability(self):
        primary=checks(access='fail')
        ref='s0:description'
        primary['access']['evidence_ids']=[ref]
        for description,quote,applies,expected in (
            ('Лот с отдельным входом. На территории пропускной режим.',
             'На территории пропускной режим.',False,'requires_confirmation'),
            ('Вход в арендуемое помещение отдельный. На территории пропускной режим.',
             'На территории пропускной режим.',True,'requires_confirmation'),
            ('Вход в арендуемое помещение отдельный. На территории пропускной режим.',
             'Вход в арендуемое помещение отдельный. На территории пропускной режим.',True,'requires_confirmation'),
            ('На территории пропускной режим.',
             'Посетителям этого лота нужен пропуск.',True,'requires_confirmation'),
            ('Посетителям этого лота нужен пропуск, в том числе через отдельный вход.',
             'Посетителям этого лота нужен пропуск, в том числе через отдельный вход.',True,'not_suitable'),
        ):
            with self.subTest(description=description):
                ai=SemanticAnalyzer(primary)
                original=ai._call_property_proof_structured_sync
                def review(prompt,*args,**kwargs):
                    raw,model=original(prompt,*args,**kwargs)
                    payload=json.loads(prompt.split('INPUT_JSON\n')[1])
                    if payload.get('check_id')!='access':
                        return raw,model
                    self.assertNotIn('claim',payload)
                    proof=json.loads(raw)
                    proof['findings']['access']['contradiction']=dict(evidence_id=ref,quote=quote,applies_to_unit=applies)
                    return json.dumps(proof),model
                ai._call_property_proof_structured_sync=review
                unit=source()
                unit['facts']['description']=description
                result,_=await self.run_case(ai,sources=[unit])
                self.assertEqual(result['status'],expected)

    async def test_same_source_different_concepts_sent_without_niche_lookup(self):
        def interpret(payload):return checks(concept_use='pass' if payload['concept']=='Приём по записи' else 'unknown')
        ai=SemanticAnalyzer(interpret)
        verifier=PropertyConfirmationVerifier(ai,Sources([source()]))
        candidate=SimpleNamespace(property_data=prop(),business_model_diagnostics={'cost':{'missing_inputs':[]}})
        first=await verifier.verify(candidate,'Приём по записи',{})
        second=await verifier.verify(candidate,'Продажи с улицы',{})
        self.assertEqual(first['status'],'source_conditions_checked');self.assertEqual(second['status'],'requires_confirmation')
        self.assertEqual([json.loads(p.split('INPUT_JSON\n')[1])['concept'] for p,_ in ai.calls],['Приём по записи']*5+['Продажи с улицы']*4)

    async def test_business_name_visit_context_and_explicit_floor_reach_verification(self):
        ai=SemanticAnalyzer();verifier=PropertyConfirmationVerifier(ai,Sources([source()]))
        candidate=SimpleNamespace(property_data=prop(),business_model_diagnostics={'cost':{'missing_inputs':[]}})
        await verifier.verify(candidate,'Предпочтительна низкая аренда',dict(target_business='Произвольная новая деятельность',business_format='По записи',allowed_floors=(1,2)))
        for prompt,_ in ai.calls:
            concept=json.loads(prompt.split('INPUT_JSON\n')[1])['concept']
            for text in ('Произвольная новая деятельность','Предпочтительна низкая аренда','По записи','(1, 2)'):self.assertIn(text,concept)

    async def test_missing_source_never_becomes_permission(self):
        result,ai=await self.run_case(sources=[])
        self.assertEqual(result['status'],'requires_confirmation');self.assertEqual(ai.calls,[])

    async def test_stale_future_unbound_sources_are_not_used(self):
        for change in ({'fetched_at':time.time()-301},{'fetched_at':time.time()+10},{'identity_checked':False}):
            with self.subTest(change=change):
                result,ai=await self.run_case(sources=[source(**change)])
                self.assertEqual(result['status'],'requires_confirmation');self.assertFalse(ai.calls)

    async def test_changed_price_or_floor_cannot_confirm_old_rating(self):
        for change in ({'price_month':300000},{'floor':'7-й этаж'}):
            result,_=await self.run_case(sources=[source(**change)])
            self.assertEqual(result['status'],'requires_confirmation')
            self.assertIn('listing_changed',[r['code'] for r in result['reasons']])

    async def test_live_card_can_reject_previously_unknown_floor(self):
        verifier=PropertyConfirmationVerifier(SemanticAnalyzer(),Sources([source(floor='цокольный')]))
        candidate=SimpleNamespace(property_data=prop(floor=None),business_model_diagnostics={'cost':{'missing_inputs':[]}})
        result=await verifier.verify(candidate,'Магазин на первом этаже',dict(allowed_floors=(1,)))
        self.assertEqual(result['status'],'not_suitable')
        self.assertIn('filter:allowed_floors',[r['code'] for r in result['reasons']])

    async def test_verified_starting_bid_does_not_confirm_final_rent(self):
        result,_=await self.run_case(sources=[source(auction_start_only=True)])
        self.assertEqual(result['status'],'requires_confirmation')
        self.assertIn('auction_price',[r['code'] for r in result['reasons']])

    async def test_missing_costs_are_returned_as_questions(self):
        result,_=await self.run_case(cost={'missing_inputs':[dict(field='operating_month',action='Уточните эксплуатацию')]})
        self.assertEqual(result['status'],'requires_confirmation')
        self.assertEqual(result['reasons'][-1]['message'],'Уточните эксплуатацию')

    async def test_model_failure_or_invented_reference_cannot_confirm(self):
        broken=checks();broken['access']['evidence_ids']=['other_unit:power']
        for answer in (RuntimeError('offline'),broken):
            result,_=await self.run_case(SemanticAnalyzer(answer))
            self.assertEqual(result['status'],'requires_confirmation')

    async def test_universal_checks_cannot_be_waived(self):
        result,_=await self.run_case(SemanticAnalyzer(checks(access='not_applicable')))
        self.assertEqual(result['status'],'requires_confirmation')


class SourceTests(unittest.TestCase):
    def test_yandex_card_uses_exact_unit_with_explicit_terms_and_own_images(self):
        p=prop(url='https://realty.yandex.ru/offer/12345/',source='yandex',external_id='12345')
        card=dict(offerId='12345',offerType='RENT',offerCategory='COMMERCIAL',active=True,
            location={'geocoderAddress':p['address']},area={'value':46,'unit':'SQUARE_METER'},
            floorsOffered=[0],price={'currency':'RUR','value':12000,'unit':'SQUARE_METER','period':'PER_YEAR'},
            fullImages=['//avatars.mds.yandex.net/get-realty-offers/unit/large'])
        def body():
            return 'window.INITIAL_STATE = '+json.dumps({'offerCard':{'card':card,
                'relatedOffersData':{'card':{'offerId':'other','description':'Соседнее помещение'}}}})+';'
        unit=yandex_unit(body(),p['url'],p)
        self.assertEqual((unit['price_month'],unit['floor']),(46000,0))
        self.assertEqual(unit['photos'],['https://avatars.mds.yandex.net/get-realty-offers/unit/large'])
        self.assertNotIn('Соседнее',json.dumps(unit,ensure_ascii=False))
        for field,value in (('currency','USD'),('unit','UNKNOWN'),('period',None)):
            with self.subTest(field=field):
                prior=card['price'][field];card['price'][field]=value
                self.assertIsNone(yandex_unit(body(),p['url'],p)['price_month'])
                card['price'][field]=prior
        card['floorsOffered']=[1,7]
        self.assertIsNone(yandex_unit(body(),p['url'],p)['floor'])
        card['active']=False
        self.assertIn('archive_status',yandex_unit(body(),p['url'],p)['facts'])
        card['offerId']='other'
        with self.assertRaises(ValueError):yandex_unit(body(),p['url'],p)
        card['offerId']='12345';card['location']['geocoderAddress']='Другой адрес, 1'
        with self.assertRaises(ValueError):yandex_unit(body(),p['url'],p)
        with self.assertRaises(ValueError):yandex_unit(body(),p['url'].replace('12345','99999'),p)

    def test_yandex_collector_domain_and_canonical_card_remain_bound_to_exact_unit(self):
        p=prop(url='//realty.ya.ru/offer/12345/',source='yandex',external_id='12345')
        provider=PublicPropertySources(SimpleNamespace(environment_source_cache=EnvironmentSourceCache(None)))
        provider._publisher_sites=lambda p:[]
        canonical='https://realty.yandex.ru/offer/12345/'
        data=dict(url=canonical,floorSize={'value':p['area_sqm']},address={'streetAddress':p['address']})
        body=lambda:'<script type="application/ld+json">'+json.dumps(data)+'</script>'
        requested=[]
        provider._get=lambda url:(requested.append(url) or body(),canonical)
        self.assertEqual(len(provider.collect(p)['sources']),1)
        self.assertEqual(requested,['https://realty.ya.ru/offer/12345/'])
        data['url']='https://realty.yandex.ru/offer/99999/'
        with self.assertRaises(ValueError):jsonld_unit(body(),canonical,p)
        data['url']='https://unrelated.example/offer/12345/'
        with self.assertRaises(ValueError):jsonld_unit(body(),canonical,p)
        data['url']='https://user:pass@realty.yandex.ru/offer/12345/'
        with self.assertRaises(ValueError):jsonld_unit(body(),canonical,p)

    def test_nearly_expired_source_is_refreshed_before_spending_ai_calls(self):
        clock=[1000];calls=[];cache=EnvironmentSourceCache(None,clock=lambda:clock[0])
        provider=PublicPropertySources(SimpleNamespace(environment_source_cache=cache))
        def fetch():calls.append(1);return dict(sequence=len(calls))
        with patch('real_estate_engine.property_sources.time.time',side_effect=lambda:clock[0]):
            first=provider._snapshot('unit',fetch);clock[0]=1290
            second=provider._snapshot('unit',fetch)
        self.assertEqual(len(calls),2);self.assertEqual(second.fetched_at,1290)
        self.assertNotEqual(first.value,second.value)

    def test_recorded_page_is_scoped_to_unit_and_building(self):
        body=(ARCHIVE/'lot_245807.html').read_text(encoding='utf-8')
        unit=mega_unit(body,LOT_URL,'245807',prop())
        self.assertEqual(unit['price_month'],114617)
        self.assertIn('охраняемой территории',unit['facts']['building_context'])
        self.assertNotIn('340',json.dumps(unit,ensure_ascii=False))
        self.assertNotIn('175458',json.dumps(unit,ensure_ascii=False))
        self.assertNotIn('сдано',unit['facts']['freeDate'])

    def test_wrong_unit_address_area_or_anchor_rejected(self):
        body=(ARCHIVE/'lot_245807.html').read_text(encoding='utf-8')
        for url,lot,p in [(LOT_URL,'175458',prop()),(LOT_URL,'245807',prop(area_sqm=90)),
                          (LOT_URL,'245807',prop(address='Москва, Другая улица, 34'))]:
            with self.subTest(lot=lot,p=p),self.assertRaises(ValueError):mega_unit(body,url,lot,p)

    def test_jsonld_requires_unique_exact_unit_identity(self):
        p=prop();data=dict(url=p['url'],floorSize={'value':46},address={'streetAddress':p['address']},description='Описание этого лота')
        body=lambda d:'<script type="application/ld+json">'+json.dumps(d)+'</script>'
        self.assertTrue(jsonld_unit(body(data),p['url'],p)['identity_checked'])
        for wrong in ({**data,'url':'https://domclick.ru/other'},{**data,'floorSize':{'value':90}},[data,data]):
            with self.assertRaises(ValueError):jsonld_unit(body(wrong),p['url'],p)

    def test_publisher_discovery_and_source_cache_do_not_mix_lots(self):
        calls=[];body=(ARCHIVE/'lot_245807.html').read_text(encoding='utf-8')
        maps=SimpleNamespace(_fetch_query_sync=lambda *args:[dict(title='Университет Мегаполис',urls=['https://www.mega-realty.ru'])])
        provider=PublicPropertySources(SimpleNamespace(environment_source_cache=EnvironmentSourceCache(None),map_collector=maps))
        provider._get=lambda url:(calls.append(url) or body,LOT_URL)
        p=prop(raw_attributes={'source_payload':{'seller':{'company':{'displayName':'УНИВЕРСИТЕТ МЕГАПОЛИС'}}}})
        first=provider.collect(p);second=provider.collect(p)
        self.assertEqual(len(first['sources']),1);self.assertEqual(len(calls),1)
        self.assertEqual(second['sources'][0]['fetched_at'],first['sources'][0]['fetched_at'])
        self.assertEqual(second['sources'][0]['cache_status'],'memory')
        wrong=provider.collect(prop(**{**p,'area_sqm':90}))
        self.assertFalse(wrong['sources'])


class ContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_effective_filter_limits_reach_verifier(self):
        captured=[]
        class Capture:
            async def verify(self,candidate,concept,parameters):
                captured.append(parameters['confirmation_constraints'])
                return dict(status='requires_confirmation',label='Требует подтверждения',reasons=[])
        self.ai.property_verifier=Capture()
        values={**self.kw,'min_area_sqm':None,'max_area_sqm':None,'target_area_sqm':50,'max_price_sqm':2500}
        result=await self.engine.find_optimal_properties(**values)
        self.assertEqual(captured,[dict(min_area_sqm=32.5,max_area_sqm=67.5,max_price_month=150000,max_price_sqm_year=30000)])
        self.assertNotIn('confirmation_constraints',result.next_actions[0]['request'])

    async def asyncSetUp(self):
        self.storage,self.conn=memory_storage();self.addCleanup(self.conn.close)
        self.storage.upsert_batch([property_offer('one',price_month=25000),property_offer('two',lat=LAT+.0001,price_month=60000),
                                  property_offer('three',lat=LAT+.0002,price_month=145000),
                                  property_offer('too_expensive',price_month=500000),property_offer('far',lat=LAT+.1)])
        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self,**kwargs):return competition_report()
            @staticmethod
            @ranking_transport_fixture
            def _call_ranking_profile_structured_sync(prompt,max_tokens):
                data=json.loads(prompt.split('\nINPUT_JSON\n',1)[1])
                response=ranking_fixture_response(data['concept'])
                response['criteria']=[{**response['criteria'][1],'weight':1,'concept_quote':'Важна аренда.'}]
                response['visit_context_quotes']=['Клиенты приходят по записи.']
                return json.dumps(response,ensure_ascii=False),'fixture-ranking-model'
        self.ai=Analyzer();self.engine=OptimalPropertyRecommender(self.storage,self.ai)
        # A fixed cost profile isolates continuation and source-verification behavior.
        async def resolve(*args,**kwargs):
            from real_estate_engine.ranking_policy import validate_profile
            concept='Клиенты приходят по записи. Важна аренда.'
            response=ranking_fixture_response(concept)
            response['criteria']=[{**response['criteria'][1],'weight':1,'concept_quote':'Важна аренда.'}]
            response['visit_context_quotes']=['Клиенты приходят по записи.']
            return validate_profile(response,concept)
        self.engine.ranking_profile_resolver.resolve=resolve
        self.kw=dict(target_business='Услуги',center_lat=LAT,center_lng=LNG,search_radius_m=500,
            business_concept='Клиенты приходят по записи. Важна аренда.',
                     min_area_sqm=40,max_area_sqm=70,max_price_month=150000,max_candidates=1)

    async def test_actual_search_then_find_more_preserves_filters_and_excludes_seen(self):
        first=await self.engine.find_optimal_properties(**self.kw)
        self.assertEqual(first.best_candidate.confirmation['label'],'Требует подтверждения')
        self.assertEqual(first.next_actions[0]['label'],'Найти ещё варианты')
        second=await self.engine.find_more_options(first)
        self.assertNotEqual(property_key(first.best_candidate.property_data),property_key(second.best_candidate.property_data))
        third=await self.engine.find_more_options(second)
        self.assertEqual(len({r.best_candidate.property_data['id'] for r in (first,second,third)}),3)
        empty=await self.engine.find_more_options(third)
        self.assertIsNone(empty.best_candidate);self.assertEqual(empty.search_radius_m,500)
        self.assertEqual(empty.client_max_price_month,150000)
        self.assertEqual(empty.client_min_area_sqm,40);self.assertEqual(empty.client_max_area_sqm,70)
        self.assertTrue(empty.next_actions[0]['preserves_constraints'])
        self.assertFalse(empty.next_actions[0]['contacts_third_parties'])

    async def test_serialized_action_is_executable_and_does_not_contain_hidden_data(self):
        first=await self.engine.find_optimal_properties(**self.kw)
        saved=json.loads(json.dumps(asdict(first)))
        request=saved['next_actions'][0]['request'];self.assertFalse(request.pop('business_data_required'))
        second=await self.engine.find_optimal_properties(**request)
        self.assertNotEqual(first.best_candidate.property_data['id'],second.best_candidate.property_data['id'])

    async def test_timeouts_do_not_lose_candidate_or_pretend_confirmation(self):
        class Slow:
            async def verify(self,*args):await asyncio.sleep(1)
        self.ai.property_verifier=Slow()
        result=await self.engine.find_optimal_properties(**self.kw,confirmation_timeout_s=.01)
        self.assertIsNotNone(result.best_candidate)
        self.assertEqual(result.best_candidate.confirmation['status'],'requires_confirmation')

    async def test_explicitly_unsuitable_removed_from_recommendations(self):
        class Rejected:
            async def verify(self,*args):return dict(status='not_suitable',label='Не подходит',reasons=[dict(code='concept_use',message='Запрещено источником')])
        self.ai.property_verifier=Rejected()
        first=await self.engine.find_optimal_properties(**self.kw)
        self.assertIsNone(first.best_candidate);self.assertEqual(len(first.confirmation_rejected_candidates),3)
        second=await self.engine.find_more_options(first)
        self.assertFalse(second.confirmation_rejected_candidates)

    async def test_rejected_top_candidate_is_replaced_from_already_analyzed_pool(self):
        calls=[]
        class Verify:
            async def verify(self,candidate,*args):
                calls.append(candidate.property_data['external_id'])
                if len(calls)==1:return dict(status='not_suitable',reasons=[])
                return dict(status='source_conditions_checked',reasons=[])
        self.ai.property_verifier=Verify()
        result=await self.engine.find_optimal_properties(**self.kw)
        self.assertEqual(len(result.ranked_candidates),1)
        self.assertEqual(len(result.confirmation_rejected_candidates),1)
        self.assertEqual(len(calls),2)
        self.assertNotEqual(result.best_candidate.property_data['external_id'],calls[0])
        self.assertTrue(result.comparison_complete)
        self.assertEqual(result.selection_status,'preliminary_leader')

    async def test_verified_duplicate_is_grouped_and_top_is_refilled(self):
        self.storage.upsert_batch([property_offer('copy',source=PlatformSource.AVITO,price_month=25000,floor=None)])
        calls=[]
        class Verify:
            async def verify(self,candidate,*args):
                calls.append(candidate.property_data['external_id'])
                return dict(status='requires_confirmation',reasons=[],sources=[dict(
                    identity_checked=True,fetched_at=time.time(),floor=1)])
        self.ai.property_verifier=Verify()
        result=await self.engine.find_optimal_properties(**{**self.kw,'max_candidates':2},include_unverified=True)
        self.assertEqual(len(result.ranked_candidates),2)
        self.assertEqual(len(calls),3)
        self.assertEqual([c.property_data['price_month'] for c in result.ranked_candidates],[25000,60000])
        self.assertEqual(len(result.ranked_candidates[0].property_data['listing_variants']),1)

    async def test_changed_current_rent_reranks_and_checks_the_new_leader(self):
        for limit in (1,3):
            with self.subTest(limit=limit):
                calls=[]
                class Verify:
                    async def verify(self,candidate,*args):
                        p=candidate.property_data;calls.append(p['external_id'])
                        rent=125000 if p['external_id']=='one' else p['price_month']
                        changed=rent!=p['price_month']
                        return dict(status='requires_confirmation' if changed else 'source_conditions_checked',
                            reasons=[dict(code='listing_changed',message='Изменилась аренда')] if changed else [],
                            sources=[dict(identity_checked=True,fetched_at=time.time(),floor=1,
                                area_sqm=p['area_sqm'],price_month=rent)])
                self.ai.property_verifier=Verify()
                result=await self.engine.find_optimal_properties(**{**self.kw,'max_candidates':limit})
                self.assertEqual(result.best_candidate.property_data['external_id'],'two')
                self.assertIn('two',calls)
                if limit==3:
                    self.assertEqual([c.property_data['price_month'] for c in result.ranked_candidates],[60000,125000,145000])
                else:self.assertEqual(calls,['one','two'])

    async def test_expired_current_rent_cannot_replace_collected_rent(self):
        class Verify:
            async def verify(self,candidate,*args):
                return dict(status='requires_confirmation',reasons=[],sources=[dict(
                    identity_checked=True,fetched_at=time.time()-301,floor=1,
                    area_sqm=50,price_month=500)])
        self.ai.property_verifier=Verify()
        result=await self.engine.find_optimal_properties(**self.kw)
        self.assertEqual(result.best_candidate.property_data['price_month'],25000)

    async def test_rescored_numerical_change_does_not_remain_a_false_confirmation_gap(self):
        class Verify:
            async def verify(self,candidate,*args):
                p=candidate.property_data
                return dict(status='requires_confirmation',changed_fields=['price_month'],
                    reasons=[dict(code='listing_changed',message='Изменилась цена')],
                    checks={key:dict(result='pass') for key in CHECKS},sources=[dict(identity_checked=True,
                        fetched_at=time.time(),floor=1,area_sqm=p['area_sqm'],price_month=20000)])
        self.ai.property_verifier=Verify()
        result=await self.engine.find_optimal_properties(**self.kw)
        best=result.best_candidate
        self.assertEqual(best.property_data['price_month'],20000)
        self.assertEqual(best.property_data['price_sqm_year'],4800)
        self.assertEqual(best.scoring_components['rent_month']['value'],20000)
        self.assertEqual(best.confirmation['ranking_updates']['price_month'],dict(previous=25000,current=20000))
        self.assertEqual(result.confirmation_status,'source_conditions_checked')
        self.assertEqual(best.confirmation['reasons'],[])

    async def test_changed_area_updates_area_score_and_square_meter_rate(self):
        from test_concept_ranking import profile,criterion
        concept='Клиенты приходят по записи. Предпочтительна большая площадь.'
        async def resolve(*args,**kwargs):
            return profile([criterion('area_sqm',1,'higher',0,100,quote=concept)],concept=concept,
                visit_quotes=['Клиенты приходят по записи.'])
        self.engine.ranking_profile_resolver.resolve=resolve
        class Verify:
            async def verify(self,candidate,*args):
                p=candidate.property_data
                return dict(status='requires_confirmation',reasons=[],sources=[dict(identity_checked=True,
                    fetched_at=time.time(),floor=1,area_sqm=40 if p['external_id']=='one' else p['area_sqm'],
                    price_month=p['price_month'])])
        self.ai.property_verifier=Verify()
        result=await self.engine.find_optimal_properties(**{**self.kw,'max_candidates':3,'business_concept':concept})
        self.assertNotEqual(result.best_candidate.property_data['external_id'],'one')
        changed=next(c for c in result.ranked_candidates if c.property_data['external_id']=='one')
        self.assertEqual(changed.property_data['area_sqm'],40)
        self.assertEqual(changed.scoring_components['area_sqm']['value'],40)
        self.assertEqual(changed.property_data['price_sqm_year'],7500)

    async def test_multiple_current_price_changes_follow_new_leaders_without_repeating_checks(self):
        calls=[];rents={'one':149000,'two':148000,'three':147000}
        class Verify:
            async def verify(self,candidate,*args):
                p=candidate.property_data;calls.append(p['external_id'])
                return dict(status='requires_confirmation',changed_fields=['price_month'],
                    reasons=[dict(code='listing_changed',message='Новая цена')],
                    checks={key:dict(result='pass') for key in CHECKS},sources=[dict(
                        identity_checked=True,fetched_at=time.time(),floor=1,area_sqm=50,
                        price_month=rents[p['external_id']])])
        self.ai.property_verifier=Verify()
        result=await self.engine.find_optimal_properties(**self.kw)
        self.assertEqual(calls,['one','two','three'])
        self.assertEqual(result.best_candidate.property_data['external_id'],'three')
        self.assertEqual(result.best_candidate.property_data['price_month'],147000)

    async def test_invalid_continuation_arguments_rejected(self):
        for change in ({'exclude_property_keys':'id:1'},{'exclude_property_keys':[None]},{'confirmation_timeout_s':0},{'confirmation_timeout_s':float('nan')}):
            with self.subTest(change=change),self.assertRaises(ValueError):await self.engine.find_optimal_properties(**self.kw,**change)

    async def test_business_data_is_preserved_or_explicitly_required_after_serialization(self):
        first=await self.engine.find_optimal_properties(**self.kw,business_data={'properties':{}})
        saved=json.loads(json.dumps(asdict(first)))
        with self.assertRaises(ValueError):await find_more(self.engine,SimpleNamespace(next_actions=saved['next_actions']))
        await self.engine.find_more_options(first)


if __name__=='__main__':unittest.main()
