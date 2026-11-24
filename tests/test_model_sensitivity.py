"""Customer reports must expose model assumptions without retuning preferences."""
import copy
import json
import time
import unittest
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import patch

from real_estate_engine.confirmation_flow import with_confirmation
from real_estate_engine.market_calibration import apply_market_references
from real_estate_engine.ranking_sensitivity import model_assumption_sensitivity
from real_estate_engine.recommender import OptimalPropertyRecommender, OptimalPropertySearchReport
from test_engine_regressions import candidate, LAT, LNG
from test_market_context import integrated_profile, environment


def scenario(profile=None):
    profile=profile or integrated_profile()
    a=environment(1000);a.population.estimated_residents_count=10000;a.population.estimated_office_workers_count=0
    b=environment(1000);b.population.estimated_residents_count=1000;b.population.estimated_office_workers_count=1000
    pairs=[(candidate(id=i,external_id=str(i),price_month=100000),r) for i,r in ((1,a),(2,b))]
    return profile,OptimalPropertyRecommender._score_and_rank_candidates(pairs,ranking_profile=profile)


def client_profile(*,all_parameters=False,legacy=False):
    profile=integrated_profile();config=profile.metadata['market_context']
    supplied=dict(kind='client_reference',source='Explicit test references',references=dict(residents=1000))
    if all_parameters:
        supplied.update(references={k:v['value'] for k,v in config['references'].items()},
            competition_scale=config['competition_scale']['value'],outside_pressure=config['outside_pressure']['value'])
    bound,provenance=apply_market_references(config,supplied)
    if legacy:provenance.pop('parameter_paths')
    return replace(profile,metadata={**profile.metadata,'market_context':bound,'market_reference_provenance':provenance})


class SensitivityTests(unittest.TestCase):
    def test_reports_concrete_changed_assumption_and_keeps_inputs_and_weights(self):
        profile,ranked=scenario();before=copy.deepcopy((profile,ranked))
        result=model_assumption_sensitivity(profile,ranked)
        self.assertEqual(ranked[0].property_data['id'],2)
        self.assertEqual(result['status'],'model_sensitive')
        self.assertEqual(result['challenger_ids'],[1])
        effect=next(e for e in result['effects'] if e['parameter']=='references.residents')
        self.assertEqual((effect['baseline_value'],effect['scenario_value']),(1000,2000))
        self.assertEqual(effect['leader_ids'],[1]);self.assertFalse(result['weights_changed'])
        self.assertEqual(result['tested_scenarios'],result['expected_scenarios'])
        self.assertEqual((profile,ranked),before)

    def test_client_supplied_fields_are_fixed_individually_including_old_snapshots(self):
        for legacy in (False,True):
            with self.subTest(legacy=legacy):
                profile,ranked=scenario(client_profile(legacy=legacy))
                result=model_assumption_sensitivity(profile,ranked)
                self.assertEqual(result['fixed_parameters'],['references.residents'])
                self.assertNotIn('references.residents',result['parameters'])
                self.assertIn('references.workers',result['parameters'])
                self.assertEqual(result['tested_scenarios'],12)

    def test_all_supplied_parameters_do_not_get_varied(self):
        profile,ranked=scenario(client_profile(all_parameters=True))
        result=model_assumption_sensitivity(profile,ranked)
        self.assertEqual(result['status'],'no_unfixed_assumptions')
        self.assertEqual(result['tested_scenarios'],0)
        self.assertFalse(result['effects'])

    def test_valid_reference_at_input_limit_can_be_stress_tested_beyond_wire_limit(self):
        profile=integrated_profile();config=copy.deepcopy(profile.metadata['market_context'])
        config['references']['site_audience']['value']=1e15
        profile,ranked=scenario(replace(profile,metadata={'market_context':config}))
        result=model_assumption_sensitivity(profile,ranked)
        self.assertFalse(result['errors'])
        self.assertEqual(result['tested_scenarios'],14)
        self.assertEqual(profile.metadata['market_context']['references']['site_audience']['value'],1e15)

    def test_active_access_time_is_checked_unless_explicitly_supplied(self):
        profile=integrated_profile();config=copy.deepcopy(profile.metadata['market_context'])
        config['access'].update(kind='walk',weight=.2)
        for fixed in (False,True):
            with self.subTest(fixed=fixed):
                provenance={};changed=config
                if fixed:
                    changed,provenance=apply_market_references(config,dict(kind='validated_benchmark',
                        source='Test walking benchmark',references={},access_reference_minutes=15))
                p,ranked=scenario(replace(profile,metadata={'market_context':changed,'market_reference_provenance':provenance}))
                result=model_assumption_sensitivity(p,ranked)
                self.assertEqual('access.reference_minutes' in result['parameters'],not fixed)
                self.assertEqual('access.reference_minutes' in result['fixed_parameters'],fixed)
                self.assertFalse(result['errors'])

    def test_identical_baseline_leaders_do_not_produce_false_flips(self):
        profile,ranked=scenario();same=copy.deepcopy(ranked[0]);same.property_data.update(id=3,external_id='3')
        result=model_assumption_sensitivity(profile,[ranked[0],same])
        self.assertEqual(result['status'],'baseline_tie')
        self.assertFalse(result['effects']);self.assertFalse(result['challenger_ids'])

    def test_absent_market_metric_or_single_candidate_needs_no_probe(self):
        profile,ranked=scenario()
        for p,pool in ((None,ranked),(profile,ranked[:1]),(replace(profile,criteria=profile.criteria[1:]),ranked)):
            self.assertEqual(model_assumption_sensitivity(p,pool)['status'],'not_applicable')

    def test_failed_probe_is_incomplete_and_does_not_leak_exception_message(self):
        profile,ranked=scenario()
        with patch('real_estate_engine.ranking_sensitivity.evaluate_market_context',side_effect=ValueError('private diagnostic')):
            result=model_assumption_sensitivity(profile,ranked)
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(result['tested_scenarios'],0)
        self.assertEqual(len(result['errors']),result['expected_scenarios'])
        self.assertNotIn('private diagnostic',json.dumps(result))

    def test_invalid_factors_are_rejected(self):
        profile,ranked=scenario()
        for factors in ([],[0],[-1],[True],[float('nan')],[float('inf')]):
            with self.subTest(factors=factors),self.assertRaises(ValueError):
                model_assumption_sensitivity(profile,ranked,factors=factors)


class FinalReportTests(unittest.IsolatedAsyncioTestCase):
    def engine(self,verifier=None):
        class Engine:
            competitor_analyzer=SimpleNamespace(property_verifier=verifier)
            _score_and_rank_candidates=staticmethod(OptimalPropertyRecommender._score_and_rank_candidates)
            _weight_sensitive_challengers=staticmethod(OptimalPropertyRecommender._weight_sensitive_challengers)

            @with_confirmation
            async def find(self,*,max_candidates=1,confirmation_timeout_s=2,business_concept='Test',business_data=None):
                return self.report
        return Engine()

    def report(self):
        profile,pool=scenario()
        for c in pool:
            # Isolate model sensitivity from independent input/weight guards.
            c.input_score_lower_bound=c.input_score_upper_bound=c.unrounded_total_score
            c.floor_assessment=None
        report=OptimalPropertySearchReport(target_business='Test',location_query='',resolved_location_name='',
            center_lat=LAT,center_lng=LNG,search_radius_m=200,competition_diameter_m=400,
            total_matching_in_db=2,evaluated_count=2,ranked_candidates=pool[:1],best_candidate=pool[0],
            total_search_time_ms=0,ranking_profile=profile,comparison_complete=True,selection_status='preliminary_leader')
        report._evaluated_candidates=pool
        return report

    async def test_report_promotes_model_rival_outside_display_limit_and_serializes_reason(self):
        engine=self.engine();engine.report=self.report()
        engine._weight_sensitive_challengers=lambda candidates:[]
        result=await engine.find()
        self.assertEqual(result.selection_status,'comparable')
        self.assertEqual(len(result.ranked_candidates),1)
        self.assertEqual([c.property_data['id'] for c in result.comparable_candidates],[2,1])
        self.assertEqual(result.model_sensitive_challenger_ids,[1])
        self.assertEqual(result.comparable_candidates[1].confirmation['status'],'requires_confirmation')
        saved=asdict(result)
        self.assertEqual(json.loads(json.dumps(saved['model_sensitivity']))['challenger_ids'],[1])
        self.assertIn('model_sensitivity',result.stage_timings_ms)
        self.assertTrue(any('опорный уровень числа жителей' in w for w in result.ranking_warnings))

    async def test_source_rejection_removes_stale_model_and_weight_alternatives(self):
        class Reject:
            async def verify(self,c,*args):
                return dict(status='not_suitable' if c.property_data['id']==2 else 'source_conditions_checked',reasons=[])
        engine=self.engine(Reject());engine.report=self.report()
        engine.report.ranking_warnings.append('Лидер может измениться при изменении весов оценки на ±10%; текущий порядок неустойчив')
        result=await engine.find()
        self.assertEqual(result.best_candidate.property_data['id'],1)
        self.assertEqual(result.model_sensitivity['status'],'not_applicable')
        self.assertFalse(result.comparable_candidates)
        self.assertFalse(any('±10%' in w for w in result.ranking_warnings))
        self.assertEqual(result.selection_status,'preliminary_leader')

    async def test_repriced_source_is_used_by_probe_after_reranking(self):
        class Reprice:
            async def verify(self,c,*args):
                return dict(status='source_conditions_checked',reasons=[],sources=[dict(identity_checked=True,
                    fetched_at=time.time(),price_month=190000 if c.property_data['id']==2 else 10000)])
        engine=self.engine(Reprice());engine.report=self.report()
        result=await engine.find()
        self.assertEqual(result.best_candidate.property_data['id'],1)
        self.assertEqual(result.model_sensitivity['baseline_leader_ids'],[1])
        self.assertFalse(result.model_sensitive_challenger_ids)

    async def test_failed_probe_cannot_leave_a_preliminary_leader_status(self):
        engine=self.engine();engine.report=self.report()
        with patch('real_estate_engine.ranking_sensitivity.evaluate_market_context',side_effect=ValueError('failure')):
            result=await engine.find()
        self.assertEqual(result.selection_status,'needs_data')
        self.assertFalse(result.comparison_complete)
        self.assertTrue(any(row['reason']=='model_sensitivity_unavailable' for row in result.missing_inputs))

    async def test_weight_only_alternative_is_retained_when_input_ranges_do_not_overlap(self):
        engine=self.engine();engine.report=self.report()
        # No market criterion: only the weight guard supplies an alternative.
        engine.report.ranking_profile=replace(engine.report.ranking_profile,criteria=engine.report.ranking_profile.criteria[1:])
        engine._weight_sensitive_challengers=lambda candidates:[1]
        result=await engine.find()
        self.assertFalse(result.input_sensitive_challenger_ids)
        self.assertFalse(result.model_sensitive_challenger_ids)
        self.assertEqual([c.property_data['id'] for c in result.comparable_candidates],[2,1])


if __name__=='__main__':unittest.main()
