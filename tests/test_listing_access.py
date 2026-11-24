"""Access challenges must not masquerade as empty inventory or format changes."""
import json
import unittest
from datetime import datetime,timedelta,timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import patch

from real_estate_engine.collectors import AvitoCollector,YandexRealtyCollector
from real_estate_engine.collectors.access_state import response_access_issue,SourceAccessError
from real_estate_engine.environment_cache import EnvironmentSourceCache
from real_estate_engine.property_sources import PublicPropertySources
from real_estate_engine.aggregator import RealEstateAggregator
from test_collector_floors import collector_with_response

CAPTCHA='<html><title>Are you not a robot?</title><body>Yandex SmartCaptcha: requests sent from your device are automated.</body></html>'


def response(text=CAPTCHA,status=200,headers=None):
    return SimpleNamespace(text=text,content=text.encode(),status_code=status,headers=headers or {},url='https://realty.ya.ru/offers')


def provider():
    return PublicPropertySources(SimpleNamespace(environment_source_cache=EnvironmentSourceCache(None),
        map_collector=SimpleNamespace(proxy='http://127.0.0.1:1')))


class AccessClassificationTests(unittest.TestCase):
    def test_captcha_is_identified_even_with_http_200(self):
        issue=response_access_issue(response())
        self.assertEqual(issue['kind'],'captcha')
        self.assertEqual(issue['status_code'],200)
        self.assertTrue(issue['requires_human_verification'])

    def test_widget_on_valid_page_is_not_a_captcha_challenge(self):
        self.assertIsNone(response_access_issue(response(CAPTCHA),has_data=True))
        self.assertIsNone(response_access_issue(response('<title>Помещение</title><script>smartcaptcha captcha-container</script>')))

    def test_forbidden_rate_limit_and_security_check_are_separate(self):
        for code,expected in ((403,'forbidden'),(429,'rate_limited'),(439,'security_challenge')):
            self.assertEqual(response_access_issue(response('',code))['kind'],expected)
        self.assertIsNone(response_access_issue(response('<title>Новый формат</title>',200)))

    def test_retry_after_seconds_and_date_are_used(self):
        self.assertEqual(response_access_issue(response('',429,{'Retry-After':'120'}))['retry_after_s'],120)
        date=format_datetime(datetime.now(timezone.utc)+timedelta(seconds=120),usegmt=True)
        seconds=response_access_issue(response('',429,{'Retry-After':date}))['retry_after_s']
        self.assertGreater(seconds,110);self.assertLessEqual(seconds,120)

    def test_detail_page_reports_captcha_instead_of_identity_error(self):
        p=provider()
        with patch('curl_cffi.requests.Session') as session:
            session.return_value.__enter__.return_value.get.return_value=response()
            with self.assertRaises(SourceAccessError) as caught:p._get('https://realty.ya.ru/offer/123/')
        self.assertEqual(caught.exception.kind,'captcha')


class CollectionAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_based_pagination_does_not_download_first_page_twice(self):
        collector=collector_with_response(YandexRealtyCollector,text='')
        requested=[]
        def get(*args,**kwargs):
            params=kwargs.get('params') or {};requested.append(params)
            page=params.get('page',1)
            item=dict(offerId=str(page),location={'point':{'latitude':55.75,'longitude':37.61}},
                area={'value':50},price={'value':100000})
            return response('<script>window.INITIAL_STATE = '+json.dumps({'search':{'offers':{'entities':[item]}}})+';</script>')
        collector.session.get=get
        rows=await collector.collect(40)
        self.assertEqual([r.external_id for r in rows],['1','2'])
        self.assertEqual(requested,[{},dict(page=2)])

    async def test_captcha_pauses_repeated_collection_without_new_requests(self):
        collector=collector_with_response(YandexRealtyCollector,text=CAPTCHA)
        calls=[]
        def get(*args,**kwargs):calls.append(1);return response()
        collector.session.get=get
        self.assertEqual(await collector.collect(20),[])
        first=collector.collection_summary(0)
        self.assertEqual(first['access_issue']['kind'],'captcha')
        self.assertEqual(first['status'],'unavailable')
        self.assertEqual(await collector.collect(20),[])
        self.assertEqual(len(calls),1)
        self.assertTrue(collector.collection_summary(0)['access_issue']['cooldown'])
        collector._access_pause_until=0
        collector.session.get=lambda *a,**k:response('<script>window.INITIAL_STATE = {"search":{"offers":{"entities":[]}}};</script>')
        self.assertEqual(await collector.collect(20),[])
        self.assertEqual(collector.collection_summary(0)['status'],'empty')
        self.assertNotIn('access_issue',collector.collection_summary(0))

    async def test_captcha_after_a_valid_page_keeps_collected_offers(self):
        item=dict(offerId='1',location={'point':{'latitude':55.75,'longitude':37.61}},area={'value':50},price={'value':100000})
        body='<script>window.INITIAL_STATE = '+json.dumps({'search':{'offers':{'entities':[item]}}})+';</script>'
        replies=iter((response(body),response()))
        collector=collector_with_response(YandexRealtyCollector,text=body)
        collector.session.get=lambda *a,**k:next(replies)
        rows=await collector.collect(40)
        self.assertEqual(len(rows),1)
        health=collector.collection_summary(len(rows))
        self.assertEqual(health['status'],'partial')
        self.assertEqual(health['access_issue']['kind'],'captcha')

    async def test_valid_state_followed_by_other_javascript_is_still_read(self):
        body='<script>window.INITIAL_STATE = {"search":{"offers":{"entities":[]}}};const next=1;</script>'
        collector=collector_with_response(YandexRealtyCollector,text=body)
        await collector.collect(20)
        self.assertEqual(collector.collection_summary(0)['status'],'empty')

    async def test_pipeline_binds_the_existing_avito_session(self):
        body='<script>'+json.dumps({'loaderData':{'data':{'catalog':{'items':[]}}},'padding':'x'*100000})+'</script>'
        collector=collector_with_response(AvitoCollector,text=body);collector.name='Avito'
        p=provider();agg=RealEstateAggregator.__new__(RealEstateAggregator)
        from real_estate_engine.cities import MOSCOW
        agg.city=MOSCOW
        agg.collectors=[collector];agg.competitor_analyzer=SimpleNamespace(property_verifier=SimpleNamespace(sources=p))
        agg.storage=SimpleNamespace(quarantine_batch=lambda rows:None,upsert_batch=lambda rows:0,
            record_source_health=lambda *args:None,get_statistics=lambda:{})
        await agg.run_pipeline(1)
        self.assertIs(p.avito_collector,collector)
        collector.session.get=lambda *a,**k:response('<title>Карточка</title>')
        p._get('https://www.avito.ru/moskva/ofis_12345')
        self.assertIs(p.avito_collector.session,collector.session)
