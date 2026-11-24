"""Independent numerical expectations and adversarial data combinations."""
import copy
import itertools
import json
import random
import unittest
from dataclasses import replace

from real_estate_engine import OptimalPropertyRecommender
from real_estate_engine.collectors.yandex import YandexRealtyCollector
from real_estate_engine.confirmation_flow import group_verified_listings,property_key
from real_estate_engine.ranking_policy import evaluate_profile
from test_business_model import evidence,fact,model_data,modeled_property
from test_collector_floors import collector_with_response
from test_concept_ranking import profile,criterion
from test_engine_regressions import candidate,competition_report,memory_storage,property_offer,LAT,LNG
from floor_ai_fixtures import FixtureFloorAnalyzer
from real_estate_engine.floor_policy import BusinessFloorProfileResolver,BusinessFloorProfile
from real_estate_engine.ai_contract import validate_floor_rejection,AIContractError
from test_selection_repairs import listing


class AdversarialGroupingTests(unittest.TestCase):
    def test_physical_measurement_cannot_cite_a_floor_number(self):
        quote='Обязателен проём входа шириной не менее 1,1 м.'
        policy=BusinessFloorProfile(business_format=quote,request_clauses=(quote,))
        prop=candidate(floor=9,title='Контрольный объект',description='Ширина проёма входа в арендуемое помещение 1,2 м.')
        payload=BusinessFloorProfileResolver()._candidate_payload(prop,policy,'c0')
        requirement=next(r for r in payload['requirements'] if r['requirement_id']=='request_0')
        fields={payload['evidence_catalog'][key]['field'] for key in requirement['evidence_scope']}
        self.assertNotIn('offered_floors',fields)
        self.assertNotIn('floor',fields)
        self.assertIn('description',fields)

    def test_regrouping_in_different_orders_never_makes_a_self_alias(self):
        ads=[listing('avito'),listing('cian'),listing('yandex')]
        for permutation in itertools.permutations(range(3)):
            grouped=group_verified_listings([ads[i] for i in permutation])
            self.assertEqual(len(grouped),1)
            primary=grouped[0].property_data
            aliases=primary['listing_variants']
            keys=[property_key(v) for v in aliases]
            self.assertNotIn(property_key(primary),keys)
            self.assertEqual(len(set(keys)),2)

    def test_changed_current_terms_do_not_merge_by_old_equal_terms(self):
        for field,new in (('price_month',150000),('area_sqm',80)):
            with self.subTest(field=field):
                first=listing('avito');second=listing('cian')
                for ad in (first,second):
                    ad.confirmation['sources'][0].update(area_sqm=61.7,price_month=123400)
                second.confirmation['sources'][0][field]=new
                self.assertEqual(len(group_verified_listings([first,second])),2)

    def test_floor_values_cover_basement_ground_upper_and_unknown(self):
        for left,right in itertools.product((-1,0,1,7,None),repeat=2):
            with self.subTest(left=left,right=right):
                actual=len(group_verified_listings([listing('a',left),listing('b',right)]))
                self.assertEqual(actual,1 if left is not None and left==right else 2)


class NumericalDiversityTests(unittest.TestCase):
    def test_rejection_proof_names_violated_conditions_without_inverting_polarity(self):
        claims=[dict(requirement_id='arbitrary_requirement')]
        proof=validate_floor_rejection(dict(violated_requirement_ids=['arbitrary_requirement'],reason='Опубликован предел ниже требуемого'),claims)
        self.assertEqual(proof['verdict'],'supported')
        self.assertEqual(validate_floor_rejection(dict(violated_requirement_ids=[],reason='Нет прямого доказательства'),claims)['verdict'],'unknown')
        with self.assertRaises(AIContractError):validate_floor_rejection(dict(violated_requirement_ids=['invented'],reason='Недопустимо'),claims)

    def test_two_hundred_seeded_pools_preserve_known_cost_order_and_scores(self):
        rng=random.Random(20261107)
        policy=replace(profile([criterion('premises_cost_month',1,'lower')]),metadata={'scale_origin':'ai_placeholders'})
        for case in range(200):
            rents=rng.sample(range(20000,300001,1000),rng.randint(2,8))
            pairs=[(candidate(i+1,price_month=rent,source=chr(122-i),external_id=str(i)),competition_report())
                for i,rent in enumerate(rents)]
            rng.shuffle(pairs)
            ranked=OptimalPropertyRecommender._score_and_rank_candidates(copy.deepcopy(pairs),ranking_profile=policy)
            self.assertEqual([c.property_data['price_month'] for c in ranked],sorted(rents),case)
            expected={c.property_data['id']:(c.unrounded_total_score,c.score_upper_bound) for c in ranked}
            pairs.append((candidate(999,price_month=1e9,source='a',external_id='outlier'),competition_report()))
            again=OptimalPropertyRecommender._score_and_rank_candidates(pairs,ranking_profile=policy)
            self.assertEqual({c.property_data['id']:(c.unrounded_total_score,c.score_upper_bound)
                for c in again if c.property_data['id']!=999},expected)
            self.assertEqual(again[-1].property_data['id'],999)
            for item in again:
                self.assertGreaterEqual(item.unrounded_total_score,0)
                self.assertLessEqual(item.score_upper_bound,100)
                self.assertIsNone(item.business_model_diagnostics['cost']['total_month']['high'])

    def test_changing_scenario_priorities_changes_winner(self):
        pairs=[]
        for identity,people,rent in ((1,1200,100000),(2,400,50000)):
            data=model_data();data['cohorts'][0]['people']=fact(people)
            pairs.append((modeled_property(identity,data=evidence(data),price_month=rent),competition_report()))
        for demand_weight,expected in ((.8,1),(.1,2)):
            policy=profile([criterion('orders_daily',demand_weight,high=400),
                criterion('premises_cost_month',1-demand_weight,'lower',high=200000)])
            ranked=OptimalPropertyRecommender._score_and_rank_candidates(copy.deepcopy(pairs),ranking_profile=policy)
            self.assertEqual(ranked[0].property_data['id'],expected)

    def test_unknown_supplements_are_not_confirmed_free_cost(self):
        policy=profile([criterion('premises_cost_month',1,'lower',high=200000)])
        known=modeled_property(1,data={'premises_cost':{'operating_month':fact(10000)}},price_month=80000)
        unknown=modeled_property(2,data={},price_month=20000)
        ranked=OptimalPropertyRecommender._score_and_rank_candidates([(unknown,competition_report()),(known,competition_report())],ranking_profile=policy)
        self.assertEqual(ranked[0].property_data['id'],1)
        self.assertEqual(ranked[0].business_model_diagnostics['cost']['total_month']['high'],90000)
        self.assertIsNone(ranked[1].business_model_diagnostics['cost']['total_month']['high'])
        self.assertEqual(ranked[1].unrounded_total_score,0)

    def test_larger_operating_intervals_never_improve_cost_score(self):
        policy=profile([criterion('premises_cost_month',1,'lower',high=300000)])
        for rent in (30000,100000,250000):
            for low,high in ((0,0),(1000,10000),(10000,70000)):
                first=modeled_property(data={'premises_cost':{'operating_month':fact(low,high)}},price_month=rent)
                second=modeled_property(data={'premises_cost':{'operating_month':fact(low+15000,high+15000)}},price_month=rent)
                a=evaluate_profile(policy,first,competition_report());b=evaluate_profile(policy,second,competition_report())
                self.assertLessEqual(b['lower'],a['lower'])
                self.assertLessEqual(b['upper'],a['upper'])


class CurrencyDiversityTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_ruble_area_and_annual_units_convert_correctly(self):
        for currency,unit,period,value,expected in (
            ('RUR','SQUARE_METER','PER_YEAR',12000,50000),
            ('RUB','WHOLE_OFFER','PER_MONTH',100000,100000),
            ('RUR','WHOLE_OFFER','PER_YEAR',600000,50000),
        ):
            with self.subTest(currency=currency,unit=unit,period=period):
                item=dict(offerId='1',location={'point':{'latitude':55.75,'longitude':37.61}},
                    area={'value':50},price={'value':value,'currency':currency,'unit':unit,'period':period})
                body='<script>window.INITIAL_STATE = '+json.dumps({'search':{'offers':{'entities':[item]}}})+';</script>'
                rows=await collector_with_response(YandexRealtyCollector,text=body).collect(1)
                self.assertEqual(rows[0].price_month,expected)

    async def test_explicit_foreign_prices_are_not_ruble_rents(self):
        for currency in ('USD','EUR'):
            with self.subTest(currency=currency):
                item=dict(offerId='1',location={'point':{'latitude':55.75,'longitude':37.61}},
                    area={'value':50},price={'value':1000,'currency':currency,'unit':'WHOLE_OFFER','period':'PER_MONTH'})
                body='<script>window.INITIAL_STATE = '+json.dumps({'search':{'offers':{'entities':[item]}}})+';</script>'
                collector=collector_with_response(YandexRealtyCollector,text=body)
                rows=await collector.collect(1)
                self.assertEqual(rows,[])
                self.assertNotEqual(collector.collection_summary(len(rows))['status'],'empty')


class PublicSearchDiversityTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_leader_ties_and_disallowed_floors_through_public_search(self):
        for label,rents,floors,expected in (
            ('known_leader',(40000,130000),(1,2),'preliminary_leader'),
            ('equal_options',(80000,80000),(1,2),'comparable'),
            ('wrong_floor',(40000,130000),(7,1),'preliminary_leader'),
        ):
            with self.subTest(case=label):
                store,conn=memory_storage()
                try:
                    for i,(rent,floor) in enumerate(zip(rents,floors)):
                        store.upsert_property(property_offer(str(i),lat=LAT+i*.001,price_month=rent,floor=floor))
                    class Verify:
                        async def verify(self,*args):return dict(status='source_conditions_checked',reasons=[])
                    class Analyzer(FixtureFloorAnalyzer):
                        property_verifier=Verify()
                        async def analyze_circle(self,**kwargs):return competition_report()
                    engine=OptimalPropertyRecommender(store,Analyzer())
                    async def resolve(*args,**kwargs):return profile([criterion('rent_month',1,'lower',high=200000)])
                    engine.ranking_profile_resolver.resolve=resolve
                    report=await engine.find_optimal_properties('Контрольная деятельность',business_concept=profile([criterion('rent_month')]).concept,
                        center_lat=LAT,center_lng=LNG,search_radius_m=1000,allowed_floors=(1,2),max_candidates=2)
                    self.assertEqual(report.selection_status,expected)
                    self.assertTrue(report.comparison_complete)
                    self.assertEqual(report.confirmation_status,'source_conditions_checked')
                    self.assertEqual(len(report.floor_rejected_candidates),int(label=='wrong_floor'))
                    self.assertEqual(report.analysis_errors,[])
                finally:conn.close()
