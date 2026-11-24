from floor_ai_fixtures import ranking_transport_fixture
import copy
import json
import math
import random
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

from real_estate_engine.accessibility import PedestrianNetwork
from real_estate_engine.business_model import analyze_business_model, concept_digest, attach_business_inputs, assess_mandatory_requirements
from real_estate_engine.ranking_policy import evaluate_profile, bind_reference_scales
from real_estate_engine.ai_contract import AIContractError
from real_estate_engine import OptimalPropertyRecommender
from test_concept_ranking import profile, criterion, CONCEPT
from test_engine_regressions import candidate, competition_report, business_card, memory_storage, property_offer, LAT, LNG
from floor_ai_fixtures import FixtureFloorAnalyzer


def fact(low, high=None):
    return dict(low=low, high=low if high is None else high, source='Синтетический контролируемый пример', kind='assumption')


def endpoint(node, verified=True):
    return dict(node=node, verified=verified, source='Синтетическая привязка к пешеходной сети')


def network_data(complete=True):
    def edge(a, b, minutes, allowed=True):
        return {'from': a, 'to': b, 'minutes': minutes, 'walk_allowed': allowed, 'bidirectional': True}
    return dict(source='Синтетический граф, не реальная карта', as_of='2026-11-04', coverage_complete=complete,
                nodes=['home', 'junction', 'entry', 'metro', 'rival', 'far', 'island'],
                edges=[edge('home', 'junction', 2), edge('junction', 'metro', 2), edge('junction', 'entry', 1),
                       edge('entry', 'metro', 3), edge('home', 'rival', 4), edge('home', 'far', 12), edge('home', 'island', .1, False)])


def model_data(concept=CONCEPT, rivals=0):
    return dict(version=1, concept_sha256=concept_digest(concept), audience_partition='exclusive', partition_source='Контролируемое разбиение',
                distance_decay_per_minute=fact(0), target_attractiveness=fact(1), max_walk_minutes=fact(10),
                target_entry=endpoint('entry'), competitor_inventory=dict(coverage_complete=True, source='Синтетический полный перечень'),
                competitors=[dict(id=f'r{i}', attractiveness=fact(1), entry=endpoint('rival')) for i in range(rivals)],
                cohorts=[dict(id='local', origin=endpoint('home'), people=fact(1000), open_hours_overlap=fact(1),
                              purchase_opportunities_per_person_day=fact(1), conversion_after_choice=fact(1), metro_exit=endpoint('metro'),
                              outside_options={key: fact(1) for key in ('other_area', 'online', 'no_purchase')})],
                margin_per_order=fact(200), operating_days_month=fact(30), other_fixed_cost_month=fact(30000))


def evidence(model=None):
    return dict(market_model=model if model is not None else model_data(), pedestrian_network=network_data(),
                premises_cost=dict(operating_month=fact(10000), fitout_total=fact(120000), amortization_months=fact(12)))


def modeled_property(i=1, data=None, **changes):
    return candidate(i, raw_attributes=dict(business_evidence=data if data is not None else evidence()), **changes)


def analyze(prop=None, report=None, stress=0, concept=CONCEPT):
    return analyze_business_model(prop or modeled_property(), report or competition_report(), concept, stress=stress)


class PedestrianTests(unittest.TestCase):
    def test_routes_isochrones_and_detour_use_edges(self):
        net=PedestrianNetwork(network_data())
        self.assertEqual(net.travel(endpoint('home'),endpoint('entry')).minutes,3)
        self.assertEqual(net.detour(endpoint('home'),endpoint('entry'),endpoint('metro'))['extra_minutes'],2)
        area=net.isochrone(endpoint('entry'),3,towards_entry=True)
        self.assertIn('home',area['nodes']);self.assertNotIn('far',area['nodes'])
        self.assertEqual(net.travel(endpoint('home'),endpoint('island')).status,'unreachable')

    def test_partial_network_and_unchecked_entrance_are_unknown(self):
        net=PedestrianNetwork(network_data(False))
        self.assertEqual(net.travel(endpoint('home'),endpoint('island')).status,'unknown')
        self.assertEqual(net.travel(endpoint('home'),endpoint('entry',False)).status,'unknown')
        self.assertEqual(net.travel(endpoint('missing'),endpoint('entry')).status,'unknown')
        self.assertIsNone(net.detour(endpoint('home'),endpoint('island'),endpoint('metro'))['extra_minutes'])

    def test_directed_entrance_and_reverse_isochrone(self):
        data=network_data();data['edges']=[dict(**{'from':'home','to':'entry'},minutes=2,walk_allowed=True,bidirectional=False)]
        net=PedestrianNetwork(data)
        self.assertEqual(net.travel(endpoint('entry'),endpoint('home')).status,'unreachable')
        self.assertIn('home',net.isochrone(endpoint('entry'),3,towards_entry=True)['nodes'])
        self.assertNotIn('home',net.isochrone(endpoint('entry'),3)['nodes'])

    def test_malformed_graphs_are_rejected(self):
        for change in ('duration','permission','endpoint','source','duplicate'):
            data=network_data()
            if change=='duration':data['edges'][0]['minutes']=float('nan')
            if change=='permission':data['edges'][0]['walk_allowed']='yes'
            if change=='endpoint':data['edges'][0]['to']='missing'
            if change=='source':data['source']=''
            if change=='duplicate':data['nodes'].append('home')
            with self.subTest(change=change),self.assertRaises(ValueError):PedestrianNetwork(data)


class ChoiceAndCostTests(unittest.TestCase):
    def test_zero_rivals_leave_outside_options_and_exact_equation(self):
        metrics,details=analyze()
        self.assertEqual(metrics['accessible_audience_daily'].low,1000)
        self.assertEqual(metrics['orders_daily'].low,250)
        self.assertEqual(metrics['premises_cost_month'].low,120000)
        self.assertEqual(metrics['net_contribution_month'].low,1350000)
        self.assertEqual(details['demand']['cohorts'][0]['choice_share']['high'],.25)
        self.assertFalse(details['calibrated'])

    def test_competition_depends_on_audience_and_attractiveness(self):
        model=model_data(rivals=5)
        large=analyze(modeled_property(data=evidence(model)))[0]['orders_daily'].low
        model['cohorts'][0]['people']=fact(100)
        small=analyze(modeled_property(data=evidence(model)))[0]['orders_daily'].low
        self.assertAlmostEqual(large,1000/9);self.assertAlmostEqual(large/small,10)
        model['competitors'][0]['attractiveness']=fact(10)
        self.assertLess(analyze(modeled_property(data=evidence(model)))[0]['orders_daily'].high,small)

    def test_missing_rival_and_incomplete_inventory_cannot_be_free_market(self):
        rep=competition_report(direct_competitors=[SimpleNamespace(card=business_card(org_id='not_modeled'))])
        metrics,details=analyze(report=rep)
        self.assertEqual(metrics['orders_daily'].low,0)
        self.assertEqual(metrics['orders_daily'].high,250)
        self.assertEqual(details['demand']['missing_competitor_ids'],['not_modeled'])
        model=model_data();model['competitor_inventory']['coverage_complete']=False
        self.assertEqual(analyze(modeled_property(data=evidence(model)))[0]['orders_daily'].low,0)

    def test_ratings_legal_revenue_and_address_words_have_no_demand_effect(self):
        model=model_data(rivals=1)
        card=business_card(org_id='r0',rating=1,fns_revenue_rub=1)
        rep=competition_report(direct_competitors=[SimpleNamespace(card=card)])
        prop=modeled_property(data=evidence(model))
        before=analyze(prop,rep)[0]
        card.rating=5;card.fns_revenue_rub=1e12;card.reviews_count=100000
        prop['address']='Москва, корпус 17, строение 7'
        self.assertEqual(before,analyze(prop,rep)[0])

    def test_network_disconnection_and_unknown_route_are_different(self):
        model=model_data();model['target_entry']=endpoint('island')
        data=evidence(model)
        self.assertEqual(analyze(modeled_property(data=data))[0]['orders_daily'].high,0)
        data['pedestrian_network']['coverage_complete']=False
        interval=analyze(modeled_property(data=data))[0]['orders_daily']
        self.assertEqual(interval.low,0);self.assertEqual(interval.high,250)
        self.assertEqual(analyze(modeled_property(data=data))[0]['accessible_audience_daily'].high,1000)

    def test_horizon_hours_and_conversion_affect_orders(self):
        model=model_data();model['cohorts'][0]['open_hours_overlap']=fact(.5);model['cohorts'][0]['conversion_after_choice']=fact(.4)
        self.assertEqual(analyze(modeled_property(data=evidence(model)))[0]['orders_daily'].low,50)
        model['target_entry']=endpoint('far')
        self.assertEqual(analyze(modeled_property(data=evidence(model)))[0]['orders_daily'].high,0)

    def test_sourced_route_service_without_graph_and_entrance_requirement(self):
        data=evidence();data.pop('pedestrian_network')
        route=dict(**fact(2),entrance_verified=True)
        data['market_model']['cohorts'][0]['routes']={'target':route}
        self.assertEqual(analyze(modeled_property(data=data))[0]['orders_daily'].low,250)
        route['entrance_verified']=False
        metrics,details=analyze(modeled_property(data=data))
        self.assertNotIn('orders_daily',metrics)
        self.assertIn('проверенного входа',' '.join(details['warnings']))

    def test_unknown_costs_are_unbounded_and_net_is_not_rent_only(self):
        data=evidence();data['premises_cost'].pop('operating_month')
        metrics,details=analyze(modeled_property(data=data))
        self.assertEqual(metrics['premises_cost_month'].low,110000)
        self.assertIsNone(metrics['premises_cost_month'].high)
        self.assertIsNone(metrics['net_contribution_month'].low)
        self.assertEqual(metrics['net_contribution_month'].high,1360000)
        json.dumps(details,allow_nan=False)

    def test_audience_does_not_require_unavailable_choice_coefficients(self):
        data=evidence();data['market_model'].pop('target_attractiveness')
        metrics,_=analyze(modeled_property(data=data))
        self.assertEqual(metrics['accessible_audience_daily'].low,1000)
        self.assertNotIn('orders_daily',metrics)

    def test_numeric_extremes_do_not_produce_nan_or_division_by_zero(self):
        data=evidence();data['premises_cost']['amortization_months']=fact(5e-324)
        metrics,details=analyze(modeled_property(data=data),stress=.999)
        self.assertIsNone(metrics['premises_cost_month'].high)
        json.dumps(details,allow_nan=False)

    def test_input_failures_do_not_invent_facts(self):
        mutations=[lambda m:m.update(concept_sha256='wrong'),lambda m:m.update(audience_partition='sum'),
                   lambda m:m['cohorts'].append(copy.deepcopy(m['cohorts'][0])),
                   lambda m:m['cohorts'][0].update(people=fact(-1)),
                   lambda m:m['cohorts'][0].update(open_hours_overlap=fact(2)),
                   lambda m:m['cohorts'][0]['people'].update(source=''),
                   lambda m:m['cohorts'][0].update(outside_options={k:fact(0) for k in ('online','other_area','no_purchase')})]
        for mutate in mutations:
            data=evidence();mutate(data['market_model'])
            with self.subTest(mutation=mutate):
                metrics,details=analyze(modeled_property(data=data))
                self.assertNotIn('orders_daily',metrics);self.assertEqual(metrics['premises_cost_month'].low,120000)
                json.dumps(details,allow_nan=False)

    def test_interval_contains_sampled_input_scenarios(self):
        model=model_data(rivals=2)
        model['target_attractiveness']=fact(.7,1.4);model['distance_decay_per_minute']=fact(.05,.2)
        cohort=model['cohorts'][0];cohort['people']=fact(700,1300);cohort['open_hours_overlap']=fact(.5,1)
        cohort['conversion_after_choice']=fact(.3,.7)
        bounds=analyze(modeled_property(data=evidence(model)))[0]['orders_daily']
        rng=random.Random(19)
        for _ in range(100):
            sampled=copy.deepcopy(model)
            def sample(item):
                if isinstance(item,dict):
                    if 'low' in item and 'high' in item:
                        value=rng.uniform(item['low'],item['high']);item['low']=item['high']=value
                    else:
                        for value in item.values():sample(value)
                elif isinstance(item,list):
                    for value in item:sample(value)
            sample(sampled)
            value=analyze(modeled_property(data=evidence(sampled)))[0]['orders_daily'].low
            self.assertLessEqual(bounds.low,value+1e-9);self.assertGreaterEqual(bounds.high,value-1e-9)

    def test_stress_propagates_from_primitives_and_serializes(self):
        normal,_=analyze();stressed,details=analyze(stress=.2)
        self.assertLess(stressed['orders_daily'].low,normal['orders_daily'].low*.8)
        self.assertGreater(stressed['premises_cost_month'].high,normal['premises_cost_month'].high*1.2)
        self.assertLess(stressed['net_contribution_month'].low,normal['net_contribution_month'].low)
        self.assertGreater(stressed['net_contribution_month'].high,normal['net_contribution_month'].high)
        json.dumps(details,allow_nan=False)


class RankingIntegrationTests(unittest.TestCase):
    def test_transitive_signal_overlap_is_rejected(self):
        for a,b in [('orders_daily','passing_traffic_index'),('orders_daily','competitors_per_1000_people'),
                    ('orders_daily','accessible_audience_daily'),('site_audience_daily','walk_time_minutes'),
                    ('net_contribution_month','premises_cost_month'),('net_contribution_month','orders_daily')]:
            with self.subTest(a=a,b=b),self.assertRaises(AIContractError):profile([criterion(a,.5),criterion(b,.5)])
        self.assertEqual(len(profile([criterion('orders_daily',.7),criterion('premises_cost_month',.3,'lower')]).criteria),2)

    def test_score_uses_cost_and_orders_once_with_scenario_stress(self):
        p=profile([criterion('orders_daily',.7,high=500),criterion('premises_cost_month',.3,'lower',0,200000)])
        result=evaluate_profile(p,modeled_property(),competition_report())
        self.assertAlmostEqual(result['lower'],47);self.assertEqual(result['lower'],result['upper'])
        self.assertLess(result['input_lower'],result['lower']);self.assertGreater(result['input_upper'],result['upper'])
        self.assertTrue(result['business_model']['demand'])

    def test_net_result_signed_scale_and_uncertain_cost_bounds(self):
        p=profile([criterion('net_contribution_month',1,low=-200000,high=2000000)])
        prop=modeled_property();prop['raw_attributes']['business_evidence']['market_model']['margin_per_order']=fact(-200,-100)
        bound=bind_reference_scales(p)
        self.assertLess(bound.criteria[0].low,0)
        result=evaluate_profile(bound,prop,competition_report())
        self.assertLessEqual(result['lower'],result['upper']);self.assertGreaterEqual(result['input_lower'],0)
        prop['raw_attributes']['business_evidence']['premises_cost'].pop('fitout_total')
        result=evaluate_profile(p,prop,competition_report())
        self.assertEqual(result['lower'],0);self.assertIsNone(result['components']['net_contribution_month']['value_lower'])

    def test_evidence_overlay_is_in_memory_and_cannot_override_floor(self):
        prop=candidate(external_id='x',raw_json=json.dumps({'owner':'test'}))
        data=dict(properties={'cian:x':evidence()})
        updated=attach_business_inputs([prop],data)[0]
        self.assertEqual(updated['raw_attributes']['owner'],'test');self.assertNotIn('raw_attributes',prop)
        self.assertEqual(analyze(updated)[0]['orders_daily'].low,250)
        with self.assertRaises(ValueError):attach_business_inputs([prop],dict(properties={'cian:x':dict(floor=1)}))

    def test_shared_network_is_compiled_once_per_scoring_context(self):
        source=dict(properties={f'cian:{i}':{'market_model':model_data(),'premises_cost':evidence()['premises_cost']} for i in range(10)},
                    pedestrian_network=network_data())
        props=attach_business_inputs([candidate(i,external_id=str(i)) for i in range(10)],source)
        pairs=[(p,competition_report()) for p in props]
        context={};p=profile([criterion('orders_daily',.7),criterion('premises_cost_month',.3,'lower')])
        with patch('real_estate_engine.business_model.PedestrianNetwork',wraps=PedestrianNetwork) as construct:
            bound=bind_reference_scales(p)
            for prop,rep in pairs:evaluate_profile(bound,prop,rep,evaluation_context=context)
            self.assertEqual(construct.call_count,1)

    def test_json_networks_remain_isolated_in_shared_scoring_context(self):
        p=profile([criterion('orders_daily',1,high=500)]);context={}
        pairs=[]
        for i in range(20):
            data=evidence()
            if i%2:data['pedestrian_network']['edges']=[]
            prop=candidate(i,raw_json=json.dumps(dict(business_evidence=data)))
            pairs.append((prop,competition_report()))
        for prop,rep in pairs:
            result=evaluate_profile(p,prop,rep,evaluation_context=context)
            self.assertEqual(result['components']['orders_daily']['value'],0 if prop['id']%2 else 250)


class MandatoryTests(unittest.TestCase):
    def test_required_route_is_checked_without_rating_bonus(self):
        data=evidence();req=dict(id='access',type='route',concept_quote=CONCEPT,origin=endpoint('home'),entry=endpoint('entry'),max_minutes=fact(5))
        data['mandatory_requirements']=[req]
        self.assertEqual(assess_mandatory_requirements(modeled_property(data=data),CONCEPT)['status'],'eligible')
        req['entry']=endpoint('island')
        self.assertEqual(assess_mandatory_requirements(modeled_property(data=data),CONCEPT)['status'],'rejected')
        data['pedestrian_network']['coverage_complete']=False
        self.assertEqual(assess_mandatory_requirements(modeled_property(data=data),CONCEPT)['status'],'needs_verification')

    def test_assumptions_cannot_satisfy_mandatory_physical_requirement(self):
        data=evidence();req=dict(id='power',type='minimum',concept_quote=CONCEPT,limit=fact(25),actual=dict(**fact(30),verified=True))
        data['mandatory_requirements']=[req]
        prop=modeled_property(data=data)
        self.assertEqual(assess_mandatory_requirements(prop,CONCEPT)['status'],'needs_verification')
        req['actual']['kind']='observed'
        self.assertEqual(assess_mandatory_requirements(prop,CONCEPT)['status'],'eligible')
        req['actual'].update(low=10,high=15)
        self.assertEqual(assess_mandatory_requirements(prop,CONCEPT)['status'],'rejected')
        req['concept_quote']='Придуманное обязательство'
        self.assertEqual(assess_mandatory_requirements(prop,CONCEPT)['status'],'needs_verification')


class SearchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_search_propagates_scenario_and_returns_comparable_options(self):
        calls=[]
        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self,**kwargs):calls.append(kwargs);return competition_report()
            @staticmethod
            @ranking_transport_fixture
            def _call_ranking_profile_structured_sync(prompt,tokens):
                from dataclasses import asdict
                p=profile([criterion('orders_daily',.7),criterion('premises_cost_month',.3,'lower')])
                return json.dumps(dict(status='ready',summary=p.summary,criteria=[asdict(c) for c in p.criteria],
                                       questions=[],assumptions=[],visit_context_quotes=[CONCEPT],unmodeled_requirements=[])),'fixture'
        storage,conn=memory_storage()
        self.addCleanup(conn.close)
        storage.upsert_property(property_offer('1'))
        storage.upsert_property(property_offer('2',lat=LAT+.001,price_month=105000))
        storage.upsert_property(property_offer('3',lat=LAT+.002))
        storage.upsert_property(property_offer('4',lat=LAT+.003))
        recommender=OptimalPropertyRecommender(storage,Analyzer())
        data=dict(properties={'cian:1':evidence(),'cian:2':evidence()},ranking_scales=dict(concept_sha256=concept_digest(CONCEPT),
            metrics={'premises_cost_month':dict(low=0,high=200000,source='Controlled client reference')}))
        for identifier,entry in [('3',endpoint('island')),('4',endpoint('entry',False))]:
            extra=evidence();extra['mandatory_requirements']=[dict(id='route',type='route',concept_quote=CONCEPT,
                origin=endpoint('home'),entry=entry,max_minutes=fact(5))]
            data['properties'][f'cian:{identifier}']=extra
        result=await recommender.find_optimal_properties('Кофейня',center_lat=LAT,center_lng=LNG,search_radius_m=1000,
                                                        business_concept=CONCEPT,business_data=data,max_candidates=1)
        self.assertEqual(result.evaluated_count,2);self.assertEqual(len(result.ranked_candidates),1)
        self.assertEqual(len(calls),2);self.assertEqual(result.numerical_matching_count,4)
        self.assertEqual(len(result.floor_rejected_candidates),1);self.assertEqual(len(result.floor_verification_candidates),1)
        self.assertEqual(len(result.comparable_candidates),2)
        self.assertEqual(result.best_candidate.business_model_diagnostics['metrics']['orders_daily']['low'],250)
        json.dumps(asdict(result),allow_nan=False,default=str)
