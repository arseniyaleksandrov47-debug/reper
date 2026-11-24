"""Explicit client choice continues uncertain admission without inventing approval."""
import json
import io
import contextlib
import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from floor_ai_fixtures import FixtureFloorAnalyzer, RecordingFloorInference
from test_unknown_floor_context import response
from test_engine_regressions import LAT,LNG,memory_storage,property_offer,competition_report
from real_estate_engine import OptimalPropertyRecommender,RealEstateAggregator
from real_estate_engine.confirmation_flow import continue_unverified,property_key
from real_estate_engine.floor_policy import BusinessFloorProfileResolver


CONCEPT='Клиенты приходят по записи. Нужен разрешённый доступ до двери помещения.'


class OptionalAnalysisTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.storage,conn=memory_storage();self.addCleanup(conn.close)
        self.storage.upsert_batch([property_offer('one'),property_offer('two',price_month=90000,lat=LAT+.0001),
            property_offer('wrong_floor',floor=7),property_offer('expensive',price_month=500000),
            property_offer('far',lat=LAT+.1),property_offer('wrong_area',area_sqm=500)])
        model=RecordingFloorInference(handler=lambda p:response(p,unknown=True))
        self.calls=[]
        calls=self.calls
        class Analyzer(FixtureFloorAnalyzer):
            _call_floor_structured_sync=model._call_floor_structured_sync
            async def analyze_circle(self,**kwargs):
                calls.append(kwargs);return competition_report()
        self.ai=Analyzer();self.engine=OptimalPropertyRecommender(self.storage,self.ai)
        self.kw=dict(target_business='Новая услуга',business_concept=CONCEPT,
            center_lat=LAT,center_lng=LNG,search_radius_m=500,min_area_sqm=40,max_area_sqm=70,
            max_price_month=150000,allowed_floors=(1,),max_candidates=2)

    async def test_default_still_waits_for_client_choice(self):
        report=await self.engine.find_optimal_properties(**self.kw)
        self.assertEqual(self.calls,[]);self.assertIsNone(report.best_candidate)
        self.assertFalse(report.include_unverified)
        action=next(a for a in report.next_actions if a['type']=='continue_unverified')
        self.assertTrue(action['request']['include_unverified'])
        self.assertEqual(action['request']['exclude_property_keys'],None)
        self.assertEqual(len(report.floor_verification_candidates),2)
        self.assertEqual(len(report.floor_rejected_candidates),1)

    async def test_explicit_continuation_scores_same_pending_candidates(self):
        strict=await self.engine.find_optimal_properties(**self.kw)
        report=await self.engine.continue_unverified_analysis(strict)
        self.assertEqual(len(self.calls),2)
        self.assertEqual(report.evaluated_count,2);self.assertEqual(report.unverified_evaluated_count,2)
        self.assertEqual(len(report.ranked_candidates),2);self.assertEqual(report.floor_verification_candidates,[])
        self.assertTrue(report.include_unverified);self.assertFalse(report.comparison_complete)
        self.assertEqual(report.ranking_profile.criteria,strict.ranking_profile.criteria)
        self.assertEqual(report.selection_status,'needs_data')
        self.assertEqual({c.property_data['external_id'] for c in report.ranked_candidates},{'one','two'})
        self.assertTrue(all(c.floor_assessment.status=='needs_verification' for c in report.ranked_candidates))
        self.assertTrue(all(c.confirmation['status']=='requires_confirmation' for c in report.ranked_candidates))
        self.assertTrue(all(any(r['code']=='admission_unknown' for r in c.confirmation['reasons']) for c in report.ranked_candidates))

    async def test_even_positive_source_check_does_not_silently_clear_unknown_admission(self):
        class SourceChecked:
            async def verify(self,*args):return dict(status='source_conditions_checked',label='Проверено',reasons=[],checks={})
        self.ai.property_verifier=SourceChecked()
        report=await self.engine.find_optimal_properties(**self.kw,include_unverified=True)
        self.assertTrue(report.ranked_candidates)
        self.assertTrue(all(c.confirmation['status']=='requires_confirmation' for c in report.ranked_candidates))

    async def test_known_source_rejection_still_removes_candidate(self):
        class Rejected:
            async def verify(self,*args):return dict(status='not_suitable',label='Запрещено',reasons=[])
        self.ai.property_verifier=Rejected()
        report=await self.engine.find_optimal_properties(**self.kw,include_unverified=True)
        self.assertEqual(report.ranked_candidates,[]);self.assertIsNone(report.best_candidate)
        self.assertEqual(len(report.confirmation_rejected_candidates),2)

    async def test_continuation_preserves_explicit_client_exclusions(self):
        report=await self.engine.find_optimal_properties(**self.kw,exclude_property_keys=['cian:one'])
        continued=await self.engine.continue_unverified_analysis(report)
        self.assertEqual([c.property_data['external_id'] for c in continued.ranked_candidates],['two'])

    async def test_find_more_keeps_opt_in_and_does_not_repeat_scored_options(self):
        kw={**self.kw,'max_candidates':1}
        report=await self.engine.find_optimal_properties(**kw,include_unverified=True)
        first=property_key(report.best_candidate.property_data)
        self.storage.upsert_batch([property_offer('new',price_month=95000,lat=LAT+.0003)])
        second=await self.engine.find_more_options(report)
        self.assertTrue(second.include_unverified);self.assertIsNotNone(second.best_candidate)
        self.assertNotEqual(first,property_key(second.best_candidate.property_data))
        self.assertEqual(second.best_candidate.property_data['external_id'],'new')
        self.assertEqual(second.client_max_price_month,report.client_max_price_month)
        self.assertEqual(second.search_radius_m,report.search_radius_m)

    async def test_analysis_limit_does_not_duplicate_a_ranked_candidate_as_pending(self):
        report=await self.engine.find_optimal_properties(**self.kw,include_unverified=True,analysis_limit=1)
        self.assertEqual(len(report.ranked_candidates),1)
        self.assertEqual(len(report.floor_verification_candidates),1)
        self.assertNotEqual(property_key(report.ranked_candidates[0].property_data),
                            property_key(report.floor_verification_candidates[0].property_data))
        self.assertFalse(report.comparison_complete)

    async def test_serialized_action_requires_original_business_data_and_replays_choice(self):
        report=await self.engine.find_optimal_properties(**self.kw,business_data=dict(properties={}))
        saved=json.loads(json.dumps(asdict(report)))
        serialized=SimpleNamespace(next_actions=saved['next_actions'])
        with self.assertRaises(ValueError):await continue_unverified(self.engine,serialized)
        continued=await continue_unverified(self.engine,serialized,business_data=dict(properties={}))
        self.assertTrue(continued.include_unverified);self.assertEqual(continued.evaluated_count,2)

    async def test_truthy_strings_do_not_enable_client_choice(self):
        for value in ('true',1,None):
            with self.subTest(value=value),self.assertRaises(ValueError):
                await self.engine.find_optimal_properties(**self.kw,include_unverified=value)
        self.assertEqual(self.calls,[])

    async def test_aggregator_exposes_the_same_explicit_action(self):
        strict=await self.engine.find_optimal_properties(**self.kw)
        agg=RealEstateAggregator.__new__(RealEstateAggregator);agg.recommender=self.engine
        report=await agg.continue_unverified_analysis(strict)
        self.assertTrue(report.include_unverified);self.assertEqual(report.evaluated_count,2)

    async def test_cli_continuation_keeps_interactively_entered_conditions_and_choice(self):
        import run_engine
        args=SimpleNamespace(niche='Студия',area='60–120',min_area=60,max_area=120,max_price=180000,
            price_sqm=None,price_sqm_year=30000,price_sqm_period='month',location='метро Тульская',
            candidates=3,analysis_limit=None,max_listing_age_hours=24,business_format=None,
            concept='Клиенты приходят по записи.',business_evidence_file=None,allowed_floors=(1,2),
            skip_collect=True,include_unverified=False,interactive=True,exclude_property=['id:old'])
        command=run_engine.client_search_command(args,include_unverified=True,excludes=['id:old','cian:seen'])
        with patch('sys.argv',[command[1],*command[2:]]):parsed=run_engine.parse_args()
        self.assertEqual((parsed.location,parsed.min_area,parsed.max_area,parsed.max_price),
                         (args.location,args.min_area,args.max_area,args.max_price))
        self.assertEqual(parsed.concept,args.concept);self.assertEqual(parsed.allowed_floors,(1,2))
        self.assertTrue(parsed.include_unverified);self.assertFalse(parsed.interactive)
        self.assertEqual(parsed.exclude_property,['id:old','cian:seen'])

    async def cli_choice(self,answer):
        import run_engine
        strict=await self.engine.find_optimal_properties(**self.kw)
        continued=await self.engine.continue_unverified_analysis(strict)
        with patch('sys.argv',['run_engine.py','--interactive','--skip-collect','--niche','Новая услуга',
            '--concept',CONCEPT,'--min-area','40','--max-area','70','--max-price','150000','--allowed-floors','1']):
            args=run_engine.parse_args()
        aggregate=SimpleNamespace(storage=self.storage,find_optimal_properties=AsyncMock(return_value=strict),
            continue_unverified_analysis=AsyncMock(return_value=continued))
        output=io.StringIO()
        with patch.object(run_engine,'parse_args',return_value=args), \
             patch.object(run_engine,'prompt_client_interactive',return_value=args), \
             patch.object(run_engine,'RealEstateAggregator',return_value=aggregate), \
             patch('builtins.input',return_value=answer),contextlib.redirect_stdout(output):
            await run_engine.main()
        return aggregate,args,output.getvalue()

    async def test_interactive_no_keeps_original_strict_result(self):
        aggregate,args,output=await self.cli_choice('нет')
        aggregate.continue_unverified_analysis.assert_not_awaited()
        self.assertFalse(args.include_unverified)
        self.assertIn('Автоматических рекомендаций нет',output)

    async def test_interactive_yes_displays_provisional_result_and_persists_choice(self):
        aggregate,args,output=await self.cli_choice('да')
        aggregate.continue_unverified_analysis.assert_awaited_once()
        self.assertTrue(args.include_unverified)
        self.assertIn('Предварительное сравнение выполнено по выбору клиента',output)
        self.assertIn('--include-unverified',output)
        self.assertIn('обязательные условия допуска не подтверждены',output)

    async def test_full_cost_limit_injects_ranking_scale_and_roundtrips_cli(self):
        import run_engine
        from real_estate_engine.business_model import concept_digest
        from real_estate_engine.recommender import require_business_concept
        strict=await self.engine.find_optimal_properties(**self.kw)
        with patch('sys.argv',['run_engine.py','--skip-collect','--niche','Новая услуга',
            '--concept',CONCEPT,'--full-cost-limit','240000']):
            args=run_engine.parse_args()
        cmd=run_engine.client_search_command(args)
        with patch('sys.argv',[cmd[1],*cmd[2:]]):
            reparsed=run_engine.parse_args()
        self.assertEqual(reparsed.full_cost_limit,240000.0)
        aggregate=SimpleNamespace(storage=self.storage,find_optimal_properties=AsyncMock(return_value=strict),
            continue_unverified_analysis=AsyncMock(),supply_ranking_references=AsyncMock())
        with patch.object(run_engine,'parse_args',return_value=args), \
             patch.object(run_engine,'RealEstateAggregator',return_value=aggregate), \
             contextlib.redirect_stdout(io.StringIO()):
            await run_engine.main()
        passed_bdata=aggregate.find_optimal_properties.await_args.kwargs['business_data']
        expected_sha=concept_digest(require_business_concept(CONCEPT,None))
        self.assertEqual(passed_bdata['ranking_scales']['concept_sha256'],expected_sha)
        self.assertEqual(passed_bdata['ranking_scales']['metrics']['premises_cost_month']['high'],240000.0)
        with patch('sys.argv',['run_engine.py','--skip-collect','--niche','Новая услуга',
            '--concept',CONCEPT,'--full-cost-limit','0']):
            bad_args=run_engine.parse_args()
        with patch.object(run_engine,'parse_args',return_value=bad_args), \
             patch.object(run_engine,'RealEstateAggregator',return_value=aggregate), \
             self.assertRaises(ValueError):
            await run_engine.main()

    async def test_provide_references_interactive_and_cli_replay(self):
        import run_engine
        strict=await self.engine.find_optimal_properties(**self.kw)
        need_ref_report=await self.engine.find_optimal_properties(**self.kw)
        need_ref_report.ranked_candidates=[]
        need_ref_report.next_actions=[dict(type='provide_references',label='Указать ориентиры оценки',
            required_metrics=['premises_cost_month'],questions=['Максимальные полные затраты ₽/мес?'])]
        need_ref_report.confirmation_message='Укажите ориентиры для ранжирования.'
        with patch('sys.argv',['run_engine.py','--skip-collect','--niche','Новая услуга','--concept',CONCEPT]):
            cli_args=run_engine.parse_args()
        agg_cli=SimpleNamespace(storage=self.storage,find_optimal_properties=AsyncMock(return_value=need_ref_report),
            supply_ranking_references=AsyncMock(return_value=strict))
        out_cli=io.StringIO()
        with patch.object(run_engine,'parse_args',return_value=cli_args), \
             patch.object(run_engine,'RealEstateAggregator',return_value=agg_cli), \
             contextlib.redirect_stdout(out_cli):
            await run_engine.main()
        self.assertIn('--full-cost-limit',out_cli.getvalue())
        self.assertIn('<СУММА_РУБ_МЕС>',out_cli.getvalue())
        with patch('sys.argv',['run_engine.py','--interactive','--skip-collect','--niche','Новая услуга','--concept',CONCEPT]):
            int_args=run_engine.parse_args()
        agg_int=SimpleNamespace(storage=self.storage,find_optimal_properties=AsyncMock(return_value=need_ref_report),
            supply_ranking_references=AsyncMock(return_value=strict))
        out_int=io.StringIO()
        with patch.object(run_engine,'parse_args',return_value=int_args), \
             patch.object(run_engine,'prompt_client_interactive',return_value=int_args), \
             patch.object(run_engine,'RealEstateAggregator',return_value=agg_int), \
             patch('builtins.input',return_value='250 тыс'), \
             contextlib.redirect_stdout(out_int):
            await run_engine.main()
        agg_int.supply_ranking_references.assert_awaited_once()
        passed_scales=agg_int.supply_ranking_references.await_args.args[1]
        self.assertEqual(passed_scales['metrics']['premises_cost_month']['high'],250000.0)
        self.assertEqual(int_args.full_cost_limit,250000.0)


if __name__=='__main__':unittest.main()
