"""Point 2: exact entity, executed HTTP fallback and finance-to-score path."""
import asyncio
import copy
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from real_estate_engine.fns import FnsBrowserRevenueCollector
from real_estate_engine.legal_entity import valid_inn,extract_company_inns,exact_registry_house
from test_engine_regressions import business_card

INN='7734443270'


def fns_payload(inn=INN,amount=1000):
    return dict(item=dict(inn=inn,id=123,shortName='АО ВКУСВИЛЛ',bfo=dict(period='2025',gainSum=amount)),
                details=dict(current2110=amount,previous2110=900,current2400=-5))


class LegalIdentityTests(unittest.TestCase):
    def test_same_house_but_different_corpus_is_not_a_match(self):
        self.assertTrue(exact_registry_house('Москва, Тестовая улица, 5 ст3',dict(house='5',block='3',block_type_full='строение')))
        self.assertFalse(exact_registry_house('Москва, Тестовая улица, 5 ст3',dict(house='5',block='3',block_type_full='корпус')))
        self.assertFalse(exact_registry_house('Москва, Тестовая улица, 5 ст3',dict(house='5')))
    def test_checksum_is_required_and_only_labelled_company_inn_is_extracted(self):
        self.assertTrue(valid_inn(INN));self.assertFalse(valid_inn('7734443271'))
        body='<p>Продавец АО «ВКУСВИЛЛ», ИНН 7734443270</p>'
        records=extract_company_inns(body,'https://example.test/contacts','ВкусВилл')
        self.assertEqual(records[0]['inn'],INN)
        self.assertFalse(extract_company_inns(body,'https://example.test','Другой магазин'))
        self.assertFalse(extract_company_inns('Телефон '+INN,'https://example.test','ВкусВилл'))

    def test_parser_checks_requested_entity_and_thousand_ruble_units(self):
        profile=FnsBrowserRevenueCollector._parse_fns_payload(INN,fns_payload(),'verified_probe')
        self.assertEqual(profile.revenue_rub,1000000)
        self.assertEqual(profile.net_profit_rub,-5000)
        self.assertEqual(profile.reporting_year,2025)
        self.assertIsNone(FnsBrowserRevenueCollector._parse_fns_payload(INN,fns_payload('7707354624'),'wrong'))
        self.assertIsNone(FnsBrowserRevenueCollector._parse_fns_payload(INN,fns_payload(amount=float('nan')),'bad'))

    def test_malformed_source_finance_never_raises_or_becomes_zero(self):
        for value in ('not-a-number',{},[],True,float('inf')):
            with self.subTest(value=value):
                self.assertIsNone(FnsBrowserRevenueCollector._parse_fns_payload(INN,fns_payload(amount=value),'bad'))
        for payload in (None,[],{'item':[]},{'item':{'inn':INN,'bfo':[]}}):
            self.assertIsNone(FnsBrowserRevenueCollector._parse_fns_payload(INN,payload,'bad'))

    def test_financial_details_keep_their_own_year(self):
        payload=fns_payload();payload['details_year']=2024
        profile=FnsBrowserRevenueCollector._parse_fns_payload(INN,payload,'probe')
        self.assertEqual(profile.reporting_year,2024)
        payload['details'].pop('current2110')
        self.assertIsNone(FnsBrowserRevenueCollector._parse_fns_payload(INN,payload,'probe').revenue_rub)
        payload['details_year']=2999
        self.assertIsNone(FnsBrowserRevenueCollector._parse_fns_payload(INN,payload,'bad'))


class FinanceAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_fallback_actually_executes_and_returns_data(self):
        calls=[]
        def get(url,**kwargs):
            calls.append(url)
            if 'advanced-search' in url:data=dict(content=[dict(inn='7707354624',id=999),fns_payload()['item']])
            elif url.endswith('/bfo'):data=[dict(id=1,period='2024'),dict(id=42,period='2025'),dict(id=2,period='2022')]
            else:data=[dict(financialResult=fns_payload()['details'])]
            return SimpleNamespace(status_code=200,content=json.dumps(data).encode())
        collector=FnsBrowserRevenueCollector.__new__(FnsBrowserRevenueCollector);collector.proxy=None
        with patch('real_estate_engine.fns.curl_requests.Session',return_value=SimpleNamespace(get=get)):
            values=await collector._fetch_inns_fallback_http([INN,INN])
        self.assertEqual(len(calls),3)
        self.assertEqual(values[INN]['item']['inn'],INN)
        self.assertEqual(values[INN]['details']['current2110'],1000)
        self.assertEqual(values[INN]['details_year'],2025)
        self.assertTrue(any('/42/details' in c for c in calls))
        self.assertFalse(any('/999/' in c for c in calls))

    def collector(self,candidates,website):
        collector=FnsBrowserRevenueCollector.__new__(FnsBrowserRevenueCollector)
        collector._inn_cache={};collector._inn_cache_expiry={};collector._cache_clock=time.monotonic
        collector.match_diagnostics={}
        collector.dadata=SimpleNamespace(resolve_candidates_for_card=lambda *args:candidates)
        async def resolve(*args):return website
        collector.website_resolver=SimpleNamespace(resolve=resolve)
        async def http(inns):return {inn:fns_payload(inn) for inn in inns}
        async def browser(inns):raise AssertionError('Successful HTTP should not start a browser')
        collector._fetch_inns_fallback_http=http;collector._fetch_inns_via_browser_cdp=browser
        return collector

    async def test_address_okved_and_brand_hints_are_not_accepted_for_positive_revenue(self):
        collector=self.collector([(INN,'dadata_address_okved'),('7707354624','dadata_brand_dict')],[])
        card=business_card(fns_inn=INN,fns_revenue_rub=99999999)
        self.assertEqual(await collector.enrich_cards([card]),0)
        self.assertIsNone(card.fns_revenue_rub)
        self.assertEqual(card.fns_match_status,'unverified')

    async def test_verified_website_company_is_queried_and_attached_with_provenance(self):
        evidence=[dict(inn=INN,source_url='https://example.test/contacts',scope='company',quote='АО Вкусвилл ИНН '+INN)]
        collector=self.collector([],evidence);card=business_card(title='ВкусВилл')
        self.assertEqual(await collector.enrich_cards([card]),1)
        self.assertEqual(card.fns_inn,INN);self.assertEqual(card.fns_match_status,'matched_company')
        self.assertEqual(card.fns_match_evidence,evidence)
        self.assertEqual(card.fns_revenue_rub,1000000)
        self.assertIn('query='+INN,card.fns_profile.source_url)

    async def test_two_possible_exact_entities_remain_ambiguous(self):
        collector=self.collector([(INN,'dadata_name_address'),('7707354624','dadata_name_address')],[])
        card=business_card();self.assertEqual(await collector.enrich_cards([card]),0)
        self.assertEqual(card.fns_match_status,'ambiguous')

    async def test_temporary_failure_recovers_after_negative_cache_expiry(self):
        collector=self.collector([(INN,'dadata_name_address')],[]);calls=[]
        async def http(inns):
            calls.append(list(inns));return {} if len(calls)==1 else {INN:fns_payload()}
        async def browser(inns):return {}
        collector._fetch_inns_fallback_http=http;collector._fetch_inns_via_browser_cdp=browser
        card=business_card()
        collector._cache_clock=lambda:100
        self.assertEqual(await collector.enrich_cards([card]),0)
        collector._cache_clock=lambda:130
        self.assertEqual(await collector.enrich_cards([card]),0)
        self.assertEqual(len(calls),1)
        collector._cache_clock=lambda:161
        self.assertEqual(await collector.enrich_cards([card]),1)
        self.assertEqual(len(calls),2)
        self.assertEqual(collector.match_diagnostics[card.org_id]['finance_status'],'available')

    async def test_success_cache_expires_and_changed_entity_does_not_keep_old_finance(self):
        collector=self.collector([(INN,'dadata_name_address')],[]);calls=[]
        async def http(inns):calls.append(list(inns));return {i:fns_payload(i,len(calls)*1000) for i in inns}
        collector._fetch_inns_fallback_http=http;card=business_card()
        for now in (100,200,86501):
            collector._cache_clock=lambda:now
            await collector.enrich_cards([card])
        self.assertEqual(len(calls),2);self.assertEqual(card.fns_revenue_rub,2000000)
        collector.dadata.resolve_candidates_for_card=lambda *args:[]
        self.assertEqual(await collector.enrich_cards([card]),0);self.assertIsNone(card.fns_inn)

    async def test_one_entity_resolution_failure_does_not_erase_other_cards(self):
        collector=self.collector([],[])
        async def resolve(title,*args):
            if title=='broken':raise RuntimeError('Controlled website outage')
            return [dict(inn=INN,source_url='https://example.test/legal',scope='company')]
        def dadata(title,*args):
            if title=='broken':raise RuntimeError('Controlled registry outage')
            return []
        collector.website_resolver.resolve=resolve;collector.dadata.resolve_candidates_for_card=dadata
        bad=business_card(org_id='bad',title='broken',fns_inn=INN,fns_revenue_rub=1)
        good=business_card(org_id='good',title='ВкусВилл')
        self.assertEqual(await collector.enrich_cards([bad,good]),1)
        self.assertIsNone(bad.fns_revenue_rub);self.assertEqual(good.fns_inn,INN)

    async def test_chain_outlets_skip_fns_resolution_cleanly(self):
        called=[]
        collector=self.collector([(INN,'dadata_name_address')],[])
        async def resolve(*args):called.append('web');return []
        def dadata(*args):called.append('dadata');return []
        collector.website_resolver.resolve=resolve
        collector.dadata.resolve_candidates_for_card=dadata
        chain=business_card(org_id='chain1',title='Шоколадница',is_chain=True)
        self.assertEqual(await collector.enrich_cards([chain]),0)
        self.assertEqual(called,[])
        self.assertEqual(chain.fns_diagnostics['scope'],'chain_excluded')
        self.assertEqual(chain.fns_diagnostics['finance_status'],'not_applicable_chain')

    async def test_website_resolver_checks_about_link_and_fallback_subpage(self):
        from real_estate_engine.legal_entity import WebsiteLegalResolver
        seen=[]
        def get_about(url,**kwargs):
            seen.append(url)
            if url.endswith('/o-kompanii'):
                return SimpleNamespace(status_code=200,content='<p>ООО «Вкусвилл», ИНН 7734443270</p>'.encode())
            return SimpleNamespace(status_code=200,content='<html><a href="/o-kompanii">О нас</a></html>'.encode())
        res=await WebsiteLegalResolver(SimpleNamespace(get=get_about)).resolve('ВкусВилл',['https://site1.example.test/'])
        self.assertEqual([r['inn'] for r in res],[INN])
        self.assertIn('https://site1.example.test/o-kompanii',seen)
        seen_fb=[]
        def get_fallback(url,**kwargs):
            seen_fb.append(url)
            if url.endswith('/rekvizity'):
                return SimpleNamespace(status_code=200,content='<p>АО «Вкусвилл» ИНН 7734443270</p>'.encode())
            return SimpleNamespace(status_code=200,content='<html><div>SPA shell</div></html>'.encode())
        res_fb=await WebsiteLegalResolver(SimpleNamespace(get=get_fallback)).resolve('ВкусВилл',['https://site2.example.test/'])
        self.assertEqual([r['inn'] for r in res_fb],[INN])
        self.assertIn('https://site2.example.test/rekvizity',seen_fb)


class SpbAddressMatchingTests(unittest.TestCase):
    def test_numbered_lines_corpus_splits_and_spb_abbreviations_match_dadata_registry(self):
        from real_estate_engine.fns import DaDataInnResolver
        from real_estate_engine.legal_entity import streets_match

        self.assertEqual(
            DaDataInnResolver._parse_street_and_house('Санкт-Петербург, 12-я линия Васильевского острова, 13'),
            ('12-я линия Васильевского острова', '13'),
        )
        self.assertEqual(
            DaDataInnResolver._parse_street_and_house('8-я линия Васильевского острова, 43'),
            ('8-я линия Васильевского острова', '43'),
        )
        self.assertEqual(
            DaDataInnResolver._parse_street_and_house('Санкт-Петербург, 10-я линия Васильевского острова, 17, корп. 2'),
            ('10-я линия Васильевского острова', '17'),
        )
        self.assertEqual(
            DaDataInnResolver._parse_street_and_house('Санкт-Петербург, Средний просп. Васильевского острова, 34'),
            ('Средний Васильевского острова', '34'),
        )

        self.assertTrue(streets_match(
            '12-я линия Васильевского острова',
            'г Санкт-Петербург, линия 12-я В.О., д 13 литера а',
            {'street_with_type': 'линия 12-я В.О.'},
        ))
        self.assertTrue(streets_match(
            '8-я линия Васильевского острова',
            '199004, Г.САНКТ-ПЕТЕРБУРГ, ЛН. 8-Я В.О., Д. 43, ЛИТЕРА А',
            {'street_with_type': 'лн. 8-я В.О.'},
        ))
        self.assertFalse(streets_match(
            '8-я линия Васильевского острова',
            'г Санкт-Петербург, 8-я Красноармейская ул., д 4',
            {'street_with_type': 'ул 8-я Красноармейская'},
        ))
        self.assertTrue(streets_match(
            'Средний Васильевского острова',
            'г Санкт-Петербург, пр-кт Средний В.О., д 34',
            {'street_with_type': 'пр-кт Средний В.О.'},
        ))

        self.assertTrue(exact_registry_house(
            'Санкт-Петербург, 12-я линия Васильевского острова, 13',
            {'house': '13', 'block': 'а', 'block_type_full': 'литера'},
        ))
        self.assertTrue(exact_registry_house(
            'Санкт-Петербург, 7-я линия Васильевского острова, 40Б',
            {'house': '40', 'block': 'б', 'block_type_full': 'литера'},
        ))
        self.assertFalse(exact_registry_house(
            'Санкт-Петербург, 7-я линия Васильевского острова, 40',
            {'house': '40', 'block': 'б', 'block_type_full': 'литера'},
        ))
        self.assertTrue(exact_registry_house(
            'Санкт-Петербург, 10-я линия Васильевского острова, 17, корп. 2',
            {'house': '17', 'block': '2', 'block_type_full': 'корпус'},
        ))
        self.assertFalse(exact_registry_house(
            'Санкт-Петербург, 10-я линия Васильевского острова, 17',
            {'house': '17', 'block': '2', 'block_type_full': 'корпус'},
        ))

        resolver = DaDataInnResolver(api_key='test')
        resolver._suggest_party = lambda *a, **k: [{
            'value': 'ООО "КОНОН НЭЙЛС"',
            'data': {
                'inn': '7801650613',
                'okved': '96.02',
                'address': {
                    'value': 'г Санкт-Петербург, линия 12-я В.О., д 13 литера а, помещ. 4-Н',
                    'data': {
                        'street_with_type': 'линия 12-я В.О.',
                        'house': '13',
                        'block': 'а',
                        'block_type_full': 'литера',
                    },
                },
            },
        }]
        resolved = resolver.resolve_candidates_for_card('Конон нэйлс', ['Салон красоты'], 'Санкт-Петербург, 12-я линия Васильевского острова, 13')
        self.assertIn(('7801650613', 'dadata_name_address'), resolved)


if __name__=='__main__':unittest.main()
