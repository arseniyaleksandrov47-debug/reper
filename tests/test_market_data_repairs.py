"""Source freshness, routes, explicit references and complete source texts."""
import copy
import json
import unittest
from datetime import date
from dataclasses import asdict

from real_estate_engine.metro_data import MetroFlowDataset
from real_estate_engine.traffic import FootTrafficEstimator
from real_estate_engine.accessibility import resolve_access_route
from real_estate_engine.business_model import concept_digest,validate_business_inputs
from real_estate_engine.ranking_policy import evaluate_profile,bind_reference_scales,metric_value
from real_estate_engine.ranking_scales import validate_ranking_scales
from test_engine_regressions import candidate,business_card,LAT,LNG,competition_report
from test_market_context import market_config,integrated_profile,environment,CONCEPT
from test_business_model import network_data,endpoint
from test_collector_floors import collector_with_response
from real_estate_engine.collectors import CianCollector,DomclickCollector,YandexRealtyCollector


def metro_dataset(**changes):
    data=dict(source='Synthetic dated source; not actual Metro traffic',as_of='2026-11-01',
        bounds=[55,57,36,39],measure='entries_per_day',stations={'Тверская':100000})
    data.update(changes)
    return MetroFlowDataset(data,today=date(2026,11,4))


class MetroFreshnessTests(unittest.TestCase):
    def test_fractional_hours_apply_even_if_all_24_hour_slots_are_present(self):
        concept=CONCEPT+' Работаем с 00:30 до 24:00.'
        cfg=market_config(concept);report=environment()
        current=evaluate_profile(integrated_profile(cfg,concept=concept),candidate(),report)
        baseline=evaluate_profile(integrated_profile(),candidate(),report)
        self.assertLess(current['business_model']['market_context']['sources']['transit_flow']['value'],
                        baseline['business_model']['market_context']['sources']['transit_flow']['value'])
        self.assertAlmostEqual(current['business_model']['market_context']['sources']['transit_flow']['value'],4000*23.5/24)

    def test_undated_legacy_station_is_unknown_in_new_estimates(self):
        card=business_card(nearby_metro=[dict(name='Тверская',coordinates=[LNG,LAT])])
        ft=FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,[card])
        self.assertIsNone(ft.nearest_metros[0].station_daily_passengers)
        self.assertIsNone(ft.estimated_daily_pedestrians)
        self.assertEqual(ft.transport_data_status,'partial')
        self.assertIsNone(ft.transport_upper_daily)
        report=environment();report.foot_traffic=ft
        score=evaluate_profile(integrated_profile(),candidate(),report)
        source=score['business_model']['market_context']['sources']['transit_flow']
        self.assertFalse(source['known']);self.assertEqual(source['evidence_coverage'],0)
        self.assertGreater(source['index_upper'],source['index_lower'])

    def test_dated_source_is_exact_matched_and_keeps_provenance(self):
        ds=metro_dataset();card=business_card(nearby_metro=[dict(name='Тверская',coordinates=[LNG,LAT])])
        ft=FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,[card],metro_dataset=ds)
        self.assertEqual(ft.nearest_metros[0].station_daily_passengers,100000)
        self.assertEqual(ft.nearest_metros[0].passenger_as_of,'2026-11-01')
        self.assertEqual(ft.transport_data_status,'complete')
        self.assertIsNone(ds.lookup('Тверская площадь',LAT,LNG))
        self.assertIsNone(ds.lookup('Тверская',59.9,30.3))
        json.dumps(asdict(ft),allow_nan=False)

    def test_stale_future_ambiguous_and_bad_unit_data_are_not_used(self):
        self.assertIsNone(metro_dataset(as_of='2024-01-01').lookup('Тверская',LAT,LNG))
        for changes in [dict(as_of='2030-01-01'),dict(measure='per_quarter'),
                        dict(stations={'Тверская':1,' тверская ':2}),dict(stations={'Тверская':float('nan')})]:
            with self.subTest(changes=changes),self.assertRaises(ValueError):metro_dataset(**changes)

    def test_partial_transport_does_not_turn_unknown_station_into_zero(self):
        card=business_card(nearby_metro=[dict(name='Тверская',coordinates=[LNG,LAT])],
            nearby_stops=[dict(id='stop',name='Остановка',coordinates=[LNG,LAT])])
        ft=FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,[card])
        self.assertGreater(ft.transport_lower_daily,0)
        self.assertIsNone(ft.transport_upper_daily)
        self.assertGreater(ft.transport_source_coverage,0);self.assertLess(ft.transport_source_coverage,1)


class RouteIntegrationTests(unittest.TestCase):
    def evidence(self,complete=True):return dict(pedestrian_network=network_data(complete),
        access_routes=dict(walk=dict(origin=endpoint('home'),entry=endpoint('entry'))))

    def test_network_is_used_by_the_adaptive_access_factor_and_raw_metric(self):
        evidence=self.evidence();route=resolve_access_route(evidence,'walk')
        self.assertEqual((route['status'],route['minutes']),('reachable',3))
        prop=candidate(raw_attributes=dict(business_evidence=evidence))
        self.assertEqual(metric_value('walk_time_minutes',prop,environment()),3)
        cfg=market_config();cfg['access']=dict(kind='walk',weight=1,reference_minutes=15,
            concept_quote=CONCEPT,reason='Проверка пути по сети')
        score=evaluate_profile(integrated_profile(cfg),prop,environment())
        used=score['business_model']['market_context']['sources']['walk_time_minutes']
        self.assertEqual(used['route_evidence']['method'],'pedestrian_network')

    def test_disconnected_full_network_and_partial_network_are_different(self):
        for complete,expected in [(True,'unreachable'),(False,'unknown')]:
            evidence=self.evidence(complete);evidence['access_routes']['walk']['entry']=endpoint('island')
            self.assertEqual(resolve_access_route(evidence,'walk')['status'],expected)

    def test_unverified_entrance_is_not_snapped_to_a_nearby_network_node(self):
        evidence=self.evidence();evidence['access_routes']['walk']['entry']=endpoint('entry',False)
        self.assertEqual(resolve_access_route(evidence,'walk')['status'],'unknown')

    def test_conflicting_direct_and_derived_times_are_not_silently_selected(self):
        evidence=self.evidence();evidence['walk_time_minutes']=dict(value=8,verified=True,source='Synthetic direct measurement')
        self.assertEqual(resolve_access_route(evidence,'walk')['status'],'conflict')

    def test_pedestrian_graph_does_not_invent_delivery_time(self):
        evidence=self.evidence();evidence['access_routes']['delivery']=evidence['access_routes']['walk']
        self.assertEqual(resolve_access_route(evidence,'delivery')['status'],'unknown')
        evidence['delivery_time_minutes']=dict(value=20,verified=True,source='Synthetic delivery measurement')
        self.assertEqual(resolve_access_route(evidence,'delivery')['minutes'],20)

    def test_route_requests_are_accepted_by_public_business_data_contract(self):
        validate_business_inputs(dict(properties={'cian:1':self.evidence()}))


class MarketReferenceTests(unittest.TestCase):
    def scales(self,**changes):
        data=dict(concept_sha256=concept_digest(CONCEPT),metrics={},market_context=dict(
            kind='client_reference',source='Explicit synthetic client utility reference',references=dict(company_revenue=1000000)))
        data.update(changes);return data

    def test_client_levels_replace_assumptions_without_changing_adaptive_weights(self):
        profile=integrated_profile();before=copy.deepcopy(profile.metadata['market_context'])
        bound=bind_reference_scales(profile, ranking_scales=self.scales())
        self.assertEqual(bound.metadata['market_context']['references']['company_revenue']['value'],1000000)
        self.assertEqual(bound.criteria,profile.criteria)
        self.assertEqual(before,profile.metadata['market_context'])
        self.assertEqual(bound.metadata['market_reference_provenance']['kind'],'client_reference')
        self.assertNotEqual(evaluate_profile(profile,candidate(),environment())['lower'],
                            evaluate_profile(bound,candidate(),environment())['lower'])

    def test_missing_or_cross_concept_calibration_is_not_trusted(self):
        bad=self.scales();bad['market_context']['source']=''
        with self.assertRaises(ValueError):validate_ranking_scales(bad)
        with self.assertRaises(ValueError):bind_reference_scales(integrated_profile(), ranking_scales=self.scales(concept_sha256='0'*64))


class CompleteDescriptionTests(unittest.IsolatedAsyncioTestCase):
    async def test_cian_domclick_and_yandex_keep_access_facts_after_500_characters(self):
        description='Описание помещения. '*50+' Вход посетителям запрещён, только сотрудники.'
        cian={'data':{'offersSerialized':[dict(id='1',geo={'coordinates':{'lat':LAT,'lng':LNG}},totalArea=50,
            bargainTerms={'priceRur':100000},description=description)]}}
        dom={'result':{'items':[dict(id='1',location={'lat':LAT,'lon':LNG},objectInfo={'area':50,'floor':1},price=100000,description=description)]}}
        yandex={'search':{'offers':{'entities':[dict(offerId='1',location={'point':{'latitude':LAT,'longitude':LNG}},
            area={'value':50},price={'value':100000},floorsOffered=[1],description=description)]}}}
        for cls,payload in [(CianCollector,cian),(DomclickCollector,dom),(YandexRealtyCollector,yandex)]:
            collector=collector_with_response(cls,text='<script>window.INITIAL_STATE = '+json.dumps(payload)+';</script>') if cls is YandexRealtyCollector else collector_with_response(cls,payload)
            result=await collector.collect(1)
            with self.subTest(source=cls.__name__):
                self.assertEqual(len(result),1)
                self.assertEqual(result[0].description,description)


if __name__=='__main__':unittest.main()
