"""Diagnostic order follows inputs rather than rank bonuses or interval centers."""
import copy
import random
import unittest

from real_estate_engine.route_audience import condition_transport_on_routes
from real_estate_engine.ranking_policy import explain_components,evaluate_profile,RankingProfile,RankingCriterion
from real_estate_engine.recommender import OptimalPropertyRecommender
from test_route_barriers import transport_traffic,route_evidence
from test_market_context import environment,integrated_profile
from test_engine_regressions import candidate as candidate_data,competition_report


def paired_traffic(first_count=1000,second_count=100000):
    traffic=transport_traffic()
    traffic.transport_sources[0].update(station_passengers_lower=first_count,station_passengers_upper=first_count)
    other=copy.deepcopy(traffic.transport_sources[0]);other.update(source_id='metro:Другая',name='Другая',
        station_passengers_lower=second_count,station_passengers_upper=second_count)
    traffic.transport_sources.append(other)
    return traffic


def paired_routes(first_time,second_time):
    e=route_evidence(first_time);e['pedestrian_network']['nodes'].append('other')
    e['pedestrian_network']['edges'].append(dict(**{'from':'other','to':'entry'},minutes=second_time,walk_allowed=True,bidirectional=True))
    e['accessibility_diagnostics']=dict(arrivals=[dict(node='other',verified=True,source='Synthetic mapped exit',kind='metro_exit',name='Другая')])
    return e


class RouteMonotonicityTests(unittest.TestCase):
    def test_making_the_smaller_station_farther_does_not_promote_the_large_station(self):
        traffic=paired_traffic()
        before=condition_transport_on_routes(traffic,paired_routes(1.9,2))
        after=condition_transport_on_routes(traffic,paired_routes(2.1,2))
        self.assertLess(after.transport_lower_daily,before.transport_lower_daily)
        self.assertLess(after.transport_upper_daily,before.transport_upper_daily)

    def test_worse_route_never_increases_either_bound_for_many_source_sizes_and_times(self):
        rng=random.Random(761)
        for _ in range(200):
            traffic=paired_traffic(rng.uniform(100,200000),rng.uniform(100,200000))
            first=rng.uniform(.5,15);second=rng.uniform(.5,15)
            before=condition_transport_on_routes(traffic,paired_routes(first,second))
            after=condition_transport_on_routes(traffic,paired_routes(first+rng.uniform(.01,5),second))
            self.assertLessEqual(after.transport_lower_daily,before.transport_lower_daily+1e-8)
            self.assertLessEqual(after.transport_upper_daily,before.transport_upper_daily+1e-8)

    def test_new_station_does_not_redistribute_exposure_from_previously_known_stations(self):
        traffic=transport_traffic();e=paired_routes(2,3)
        before=condition_transport_on_routes(traffic,e)
        after=condition_transport_on_routes(paired_traffic(100000,1000),e)
        self.assertGreaterEqual(after.transport_lower_daily,before.transport_lower_daily)
        self.assertGreaterEqual(after.transport_upper_daily,before.transport_upper_daily)
        self.assertEqual(before.transport_route_diagnostics['sources'][0]['contribution_lower_daily'],
                         after.transport_route_diagnostics['sources'][0]['contribution_lower_daily'])


class ExplanationGroundingTests(unittest.TestCase):
    def test_interval_crossing_neutral_level_is_neither_pro_nor_con(self):
        report=environment();report.population.estimated_residents_range=(100,900)
        report.population.population_model=dict(usable=True,modeled=True)
        profile=RankingProfile(status='ready',criteria=(RankingCriterion('residents_count',1,'higher',0,1000,
            'Важны жители','Сценарный приоритет','Ориентир примера'),))
        score=evaluate_profile(profile,{},report);text=explain_components(score['components'])
        self.assertEqual(text['pros'],[]);self.assertEqual(text['cons'],[])
        self.assertEqual(len(text['uncertain']),1);self.assertIn('100–900',text['uncertain'][0])

    def test_certain_cost_advantage_and_risk_follow_whole_utility_interval(self):
        profile=RankingProfile(status='ready',criteria=(RankingCriterion('rent_month',1,'lower',0,200000,
            'Важна аренда','Приоритет клиента','Ориентир примера'),))
        cheap=evaluate_profile(profile,dict(price_month=50000),competition_report())
        costly=evaluate_profile(profile,dict(price_month=150000),competition_report())
        self.assertTrue(explain_components(cheap['components'])['pros'])
        self.assertTrue(explain_components(costly['components'])['cons'])

    def test_missing_data_cautions_are_not_presented_as_established_property_defects(self):
        profile=integrated_profile();report=environment();report.data_warnings=['Controlled missing input warning']
        scored=OptimalPropertyRecommender._score_and_rank_candidates([(candidate_data(),report)],profile)[0]
        self.assertIn('Controlled missing input warning',scored.data_warnings)
        self.assertNotIn('Controlled missing input warning',scored.cons)

    def test_pareto_superior_candidate_is_never_ranked_below_dominated_candidate(self):
        profile=integrated_profile();a=environment(20000);b=environment(10000)
        a.population.estimated_residents_count=2000;b.population.estimated_residents_count=1000
        better=candidate_data(id=1,price_month=50000);worse=candidate_data(id=2,price_month=100000)
        ranked=OptimalPropertyRecommender._score_and_rank_candidates([(worse,b),(better,a)],profile)
        self.assertEqual(ranked[0].property_data['id'],1)
        for key,part in ranked[0].scoring_components.items():
            self.assertGreaterEqual(part['contribution_lower'],ranked[1].scoring_components[key]['contribution_lower'])
