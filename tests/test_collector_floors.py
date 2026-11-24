import json
import threading
import unittest
from types import SimpleNamespace

from real_estate_engine.collectors import (
    AvitoCollector, CianCollector, DomclickCollector, InvestMoscowCollector, YandexRealtyCollector,
)
from real_estate_engine.floor_policy import extract_floor_evidence
from real_estate_engine.cities import MOSCOW


def collector_with_response(cls, payload=None, text=None):
    collector = cls.__new__(cls)
    collector.city = MOSCOW
    if cls is AvitoCollector:
        collector._request_lock = threading.Lock()
    collector.endpoint = collector.web_url = "https://example.test/offers"
    raw = text if text is not None else json.dumps(payload, ensure_ascii=False)
    response = SimpleNamespace(status_code=200, text=raw, content=raw.encode("utf-8"))
    collector.session = SimpleNamespace(get=lambda *a, **k: response, post=lambda *a, **k: response)
    async def pace(**kw):
        pass
    collector.pace = pace
    collector.record_status = lambda status: None
    return collector


class CollectorFloorTests(unittest.IsolatedAsyncioTestCase):
    def test_avito_challenge_retries_the_same_public_unit_request(self):
        collector=collector_with_response(AvitoCollector)
        replies=iter([SimpleNamespace(status_code=439),SimpleNamespace(status_code=200)])
        urls=[]
        def get(url,**kwargs):
            urls.append(url)
            return next(replies)
        collector.session.get=get
        collector._solve_pow_sync=lambda deadline:True
        url='https://www.avito.ru/moskva/ofis_12345'
        self.assertEqual(collector._fetch_page_sync(url).status_code,200)
        self.assertEqual(urls,[url,url])

    async def test_cian_ground_level_zero_not_replaced_by_building_field(self):
        payload = {"data": {"offersSerialized": [{
            "id": "1", "geo": {"coordinates": {"lat": 55.75, "lng": 37.61}},
            "totalArea": 50, "bargainTerms": {"priceRur": 100000},
            "floorNumber": 0, "building": {"floorNumber": 7},
        }]}}
        result = await collector_with_response(CianCollector, payload).collect(1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].floor, 0)

    async def test_yandex_multiple_floors_remain_multiple(self):
        payload = {"search": {"offers": {"entities": [{
            "offerId": "1", "location": {"point": {"latitude": 55.75, "longitude": 37.61}},
            "area": {"value": 50}, "price": {"value": 100000}, "floorsOffered": [1, 7],
        }]}}}
        text = "<script>window.INITIAL_STATE = " + json.dumps(payload) + ";</script>"
        result = await collector_with_response(YandexRealtyCollector, text=text).collect(1)
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].floor)
        self.assertEqual(result[0].raw_attributes["offered_floors"], [1, 7])

    async def test_yandex_published_relative_links_become_absolute_https_urls(self):
        for url,expected in (('//realty.yandex.ru/offer/1','https://realty.yandex.ru/offer/1'),
                             ('/offer/1','https://realty.ya.ru/offer/1'),
                             (None,'https://realty.ya.ru/offer/1/')):
            with self.subTest(url=url):
                payload={'search':{'offers':{'entities':[dict(offerId='1',url=url,
                    location={'point':{'latitude':55.75,'longitude':37.61}},
                    area={'value':50},price={'value':100000})]}}}
                text='<script>window.INITIAL_STATE = '+json.dumps(payload)+';</script>'
                collector=collector_with_response(YandexRealtyCollector,text=text)
                collector.web_url='https://realty.ya.ru/moskva/snyat/kommercheskaya-nedvizhimost/'
                result=await collector.collect(1)
                self.assertEqual(result[0].url,expected)

    async def test_domclick_floor_out_of_total_normalized(self):
        payload = {"result": {"items": [{
            "id": "1", "location": {"lat": 55.75, "lon": 37.61},
            "objectInfo": {"area": 50, "floor": "7/12"}, "price": 100000,
        }]}}
        result = await collector_with_response(DomclickCollector, payload).collect(1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].floor, 7)

    async def test_avito_floor_captured_before_description_shortened(self):
        item = {"id": "1", "coords": {"lat": 55.75, "lng": 37.61}, "title": "Помещение 50 м²",
                "priceDetailed": {"value": 100000}, "description": "x" * 600 + " на 7-м этаже"}
        payload = {"loaderData": {"data": {"catalog": {"items": [item]}}}, "padding": "x" * 100000}
        result = await collector_with_response(AvitoCollector, text="<script>" + json.dumps(payload) + "</script>").collect(1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].floor, 7)
        self.assertEqual(result[0].description,item['description'])
        self.assertEqual(result[0].raw_attributes["floor_source"], "description")

    async def test_investmoscow_floor_recovered_from_lease_address(self):
        item = {"id": "1", "name": "Помещение в аренду", "coords": [55.75, 37.61],
                "objectArea": 50, "startPrice": 1200000, "address": "Москва, улица Тестовая, 7, цокольный этаж",
                "typeCode": "nsi:tender_type_portal:12", "objectTypeCode": "nsi:41:30011569",
                "requestEndDate": "2099-01-01T12:00:00Z"}
        payload = {"entities": [{"tenders": [item]}]}
        result = await collector_with_response(InvestMoscowCollector, payload).collect(1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].floor, 0)
        evidence = extract_floor_evidence({"floor": result[0].floor, "raw_attributes": result[0].raw_attributes})
        self.assertEqual(evidence.floors, (0,))


if __name__ == "__main__":
    unittest.main()
