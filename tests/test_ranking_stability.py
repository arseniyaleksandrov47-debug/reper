"""Choice consistency and honest treatment of unavailable traffic/reference data."""
import copy
import io
import contextlib
import json
import random
import unittest
from dataclasses import replace,asdict
from unittest.mock import patch,AsyncMock
from types import SimpleNamespace

from real_estate_engine import OptimalPropertyRecommender
from real_estate_engine.traffic import FootTrafficEstimator
from real_estate_engine.ranking_policy import bind_reference_scales,evaluate_profile,RankingProfileResolver
from real_estate_engine.business_model import concept_digest
from real_estate_engine.ranking_scales import validate_ranking_scales
from test_business_model import modeled_property,model_data,evidence,fact
from test_concept_ranking import profile,criterion,CONCEPT
from test_engine_regressions import competition_report,business_card,candidate,LAT,LNG,memory_storage,property_offer
from test_remaining_problems import environment
from floor_ai_fixtures import FixtureFloorAnalyzer


def anchors(**metrics):
    return dict(concept_sha256=concept_digest(CONCEPT),metrics={k:dict(low=v[0],high=v[1],source='Явный ориентир клиента для контрольного примера') for k,v in metrics.items()})


def example_pairs():
    pairs=[]
    for identifier,people,rent in [(1,400,100000),(2,360,50000),(3,0,1000000)]:
        model=model_data();model['cohorts'][0]['people']=fact(people)
        pairs.append((modeled_property(identifier,data=evidence(model),price_month=rent),competition_report()))
    return pairs


class ScaleStabilityTests(unittest.TestCase):
    def test_reported_rank_reversal_is_fixed_without_changing_other_scores(self):
        p=profile([criterion('orders_daily',.7,high=100),criterion('premises_cost_month',.3,'lower',high=120000)])
        pairs=example_pairs();rank=OptimalPropertyRecommender._score_and_rank_candidates
        first=rank(pairs[:2],ranking_profile=p)
        extra=rank(pairs,ranking_profile=p)
        scores={c.property_data['id']:c.unrounded_total_score for c in first}
        self.assertEqual(first[0].property_data['id'],2)
        self.assertEqual(extra[0].property_data['id'],2)
        self.assertAlmostEqual(scores[1],70);self.assertAlmostEqual(scores[2],75.5)
        self.assertEqual(scores,{c.property_data['id']:c.unrounded_total_score for c in extra if c.property_data['id'] in scores})

    def test_explicit_anchors_are_independent_of_outliers_duplicates_order_and_removal(self):
        p=replace(profile([criterion('orders_daily',.7),criterion('premises_cost_month',.3,'lower')]),metadata={'scale_origin':'ai_placeholders'})
        supplied=anchors(orders_daily=(0,100),premises_cost_month=(0,120000))
        pairs=example_pairs();rng=random.Random(31)
        reference=bind_reference_scales(p, ranking_scales=supplied)
        expected=[evaluate_profile(reference,*pair) for pair in pairs[:2]]
        for _ in range(50):
            sample=[*pairs,*(copy.deepcopy(pairs[2]) for i in range(rng.randint(0,12)))]
            rng.shuffle(sample)
            frozen=bind_reference_scales(p, ranking_scales=supplied)
            self.assertEqual(frozen.criteria,reference.criteria)
            for i,pair in enumerate(pairs[:2]):self.assertEqual(evaluate_profile(frozen,*pair),expected[i])
        for pair in pairs[:2]:
            alone=bind_reference_scales(p, ranking_scales=supplied)
            self.assertEqual(alone.criteria,reference.criteria)

    def test_missing_ai_scale_does_not_convert_customers_to_a_100_customer_target(self):
        p=replace(profile([criterion('orders_daily')]),metadata={'scale_origin':'ai_placeholders'})
        result=evaluate_profile(p,modeled_property(),competition_report())
        part=result['components']['orders_daily']
        self.assertEqual(part['value'],250)
        self.assertIsNone(part['utility']);self.assertEqual(part['missing_reason'],'missing_scale')
        self.assertEqual((result['lower'],result['upper'],result['coverage']),(0,100,0))

    def test_budget_anchor_is_explicit_and_is_not_used_as_total_cost_budget(self):
        p=replace(profile([criterion('rent_month',1,'lower')]),metadata={'scale_origin':'ai_placeholders','client_constraints':{'max_price_month':200000}})
        bound=bind_reference_scales(p)
        self.assertEqual(bound.criteria[0].high,200000)
        self.assertEqual(evaluate_profile(bound,candidate(),competition_report())['lower'],50)
        cost=replace(p,criteria=tuple(profile([criterion('premises_cost_month',1,'lower')]).criteria))
        self.assertFalse(bind_reference_scales(cost).metadata['reference_scales']['premises_cost_month']['usable'])

    def test_bad_and_cross_concept_scales_rejected(self):
        for change in ({'low':True},{'high':float('nan')},{'source':''},{'low':-1},{'high':0}):
            data=anchors(orders_daily=(0,100));data['metrics']['orders_daily'].update(change)
            with self.subTest(change=change),self.assertRaises(ValueError):validate_ranking_scales(data)
        data=anchors(orders_daily=(0,100));data['concept_sha256']='0'*64
        with self.assertRaises(ValueError):bind_reference_scales(profile([criterion('orders_daily')]), ranking_scales=data)

    def test_binder_does_not_read_candidate_facts_to_define_scales(self):
        class UnavailableCandidate:
            def __iter__(self):raise AssertionError('No candidate reads allowed for reference construction')
        p=profile([criterion('premises_cost_month',1,'lower',high=250000)])
        bound=bind_reference_scales(p)
        self.assertEqual(bound.criteria,p.criteria)


class TrafficIntegrityTests(unittest.TestCase):
    def test_empty_environment_is_unknown_not_450_or_zero_observed_pedestrians(self):
        ft=FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,[])
        self.assertIsNone(ft.estimated_daily_pedestrians);self.assertIsNone(ft.traffic_index)
        self.assertEqual(ft.data_status,'unavailable');self.assertIsNone(ft.hourly_people_for_hour(12))
        json.dumps(asdict(ft),allow_nan=False)

    def test_single_card_has_no_minimum_450_or_minimum_index_8(self):
        ft=FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,[business_card()])
        self.assertGreater(ft.estimated_daily_pedestrians,0);self.assertLess(ft.estimated_daily_pedestrians,450)
        self.assertLess(ft.traffic_index,8)
        self.assertNotIn('линия',ft.traffic_level.lower());self.assertNotIn('двор',ft.traffic_level.lower())

    def test_unknown_and_partial_station_names_do_not_receive_other_station_flow(self):
        card=business_card(nearby_metro=[dict(name='Новая станция',coordinates=[LNG,LAT])])
        ft=FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,[card])
        self.assertIsNone(ft.nearest_metros[0].station_daily_passengers)
        self.assertIsNone(ft.nearest_metros[0].estimated_corridor_daily)
        self.assertEqual(ft.metro_contribution_daily,0);self.assertTrue(any('неизвестен' in w for w in ft.data_warnings))

    def test_moscow_station_name_does_not_import_moscow_flow_to_petersburg_or_ekb(self):
        for lat,lng in [(59.9343,30.3351),(56.8389,60.6057)]:
            card=business_card(nearby_metro=[dict(name='Пушкинская',coordinates=[lng,lat])])
            ft=FootTrafficEstimator.estimate_for_circle(lat,lng,200,[card])
            self.assertIsNone(ft.nearest_metros[0].station_daily_passengers)

    def test_unknown_traffic_can_be_scored_and_printed_without_crash(self):
        import run_engine
        ft=FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,[])
        rep=competition_report(foot_traffic=ft)
        for ranking in (profile([criterion('passing_traffic_index')]),profile([criterion('rent_month',1,'lower',high=200000)])):
            ranked=OptimalPropertyRecommender._score_and_rank_candidates([(candidate(),rep)],ranking_profile=ranking)
            self.assertEqual(len(ranked),1)
        with contextlib.redirect_stdout(io.StringIO()) as output:run_engine.print_detailed_location_report(candidate(),rep,'Тест',400)
        self.assertIn('н/д',output.getvalue())

    def test_missing_malformed_station_coordinates_are_not_guessed_from_other_card(self):
        for coordinates in (None,['bad',LAT],[float('nan'),LAT],[LNG,91],[True,LAT]):
            card=business_card(nearby_metro=[dict(name='Тверская',distanceValue=0,coordinates=coordinates)],
                               nearby_stops=[dict(id='stop',distanceValue=0,coordinates=coordinates)],
                               building_entrances=[['bad',LAT]])
            ft=FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,[card])
            self.assertEqual(ft.nearest_metros,[]);self.assertIsNone(ft.closest_stop_distance_m)
            self.assertEqual(ft.metro_contribution_daily,0);self.assertEqual(ft.stops_contribution_daily,0)
            self.assertTrue(any('координат' in message for message in ft.data_warnings))
            json.dumps(asdict(ft),allow_nan=False)


class SearchStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_reference_stops_work_and_supplied_reference_reuses_matching_inventory(self):
        class Analyzer(FixtureFloorAnalyzer):
            async def analyze_circle(self,**kw):return environment()
        store,conn=memory_storage();self.addCleanup(conn.close)
        store.upsert_batch([property_offer('a'),property_offer('b',lat=LAT+.001,price_month=120000)])
        recommender=OptimalPropertyRecommender(store,Analyzer())
        # The profile is held fixed to test the effect of supplied scale data.
        async def resolve(*args,**kwargs):
            from floor_ai_fixtures import ranking_fixture_response
            from real_estate_engine.ranking_policy import validate_profile
            return validate_profile(ranking_fixture_response(CONCEPT),CONCEPT,metadata={'scale_origin':'ai_placeholders'})
        recommender.ranking_profile_resolver.resolve=resolve
        kwargs=dict(center_lat=LAT,center_lng=LNG,search_radius_m=1000,business_concept=CONCEPT)
        missing=await recommender.find_optimal_properties('Тест',**kwargs)
        self.assertEqual(missing.selection_status,'needs_data')
        self.assertTrue(any(i['reason']=='missing_scale' for i in missing.missing_inputs))
        supplied=await recommender.find_optimal_properties('Тест',**kwargs,business_data=dict(properties={},ranking_scales=anchors(rent_month=(0,200000))))
        self.assertFalse(any(i['reason']=='missing_scale' for i in supplied.missing_inputs))
        self.assertEqual(missing.evaluated_count,0);self.assertEqual(supplied.evaluated_count,2)
        self.assertEqual(supplied.numerical_matching_count,missing.numerical_matching_count)
        for stage in ('concept_profile','mandatory_checks','environment','company_accounts','scoring'):
            self.assertGreaterEqual(supplied.stage_timings_ms[stage],0)
        self.assertTrue(supplied.comparison_complete)
        self.assertFalse(missing.comparison_complete)
        # Cover the actual product report, including its final passport and
        # unknown traffic values, with the real report returned by search.
        import run_engine
        for cand in missing.ranked_candidates:
            cand.competition_report.foot_traffic=FootTrafficEstimator.estimate_for_circle(LAT,LNG,200,[])
        stub=SimpleNamespace(storage=store,find_optimal_properties=AsyncMock(return_value=missing))
        with patch('sys.argv',['run_engine.py','--skip-collect','--concept',CONCEPT]),patch.object(run_engine,'RealEstateAggregator',return_value=stub),contextlib.redirect_stdout(io.StringIO()) as output:
            await run_engine.main()
        self.assertIn('Найденные объявления сохранены',output.getvalue())
        self.assertNotIn('ПОЛНЫЙ ПАСПОРТ ЛУЧШЕГО',output.getvalue())

    async def test_cross_concept_reference_fails_before_io(self):
        data=dict(properties={},ranking_scales=anchors(orders_daily=(0,100)))
        with self.assertRaises(ValueError):
            await OptimalPropertyRecommender(None,None).find_optimal_properties('Тест',business_concept='Другая концепция',business_data=data)


if __name__=='__main__':unittest.main()
