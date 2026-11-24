import unittest
from types import SimpleNamespace
from dataclasses import replace
from real_estate_engine.requirement_coverage import assess_requirement_coverage
from real_estate_engine.floor_policy import FloorAssessment
from test_concept_ranking import profile,criterion,CONCEPT


def candidate(quote,result='met',status='eligible'):
    return SimpleNamespace(floor_assessment=FloorAssessment(status,(1,),'Контроль','structured',
        checks=[dict(requirement_id='request_0',request_quote=quote,result=result,critical=True)],analysis_status='valid',
        semantic_review_status='supported',inference_metadata={'proof_verifications':[dict(verdict='supported',claims=[dict(requirement_id='request_0')])]}))


class RequirementCoverageTests(unittest.TestCase):
    def test_only_independently_passed_exact_obligation_is_known_to_be_handled(self):
        q='Нужен первый этаж.'
        p=replace(profile([criterion('rent_month',1,'lower')]),unmodeled_requirements=(q,))
        self.assertEqual(assess_requirement_coverage(p,[candidate(q),candidate(q)])[0]['status'],'checked_before_rating')
        for candidates in ([],[candidate(q,'unknown')],[candidate(q,status='needs_verification')],[candidate('Другая цитата')]):
            self.assertEqual(assess_requirement_coverage(p,candidates)[0]['status'],'unmodeled')

    def test_overlap_does_not_hide_unmodeled_part_of_mixed_sentence(self):
        q='Предпочтительна площадь 30–70 м² и обязательна парковка.'
        p=replace(profile([criterion('area_sqm',1,'range',30,70,quote=q)],concept=q),unmodeled_requirements=(q,))
        result=assess_requirement_coverage(p,[candidate('Только первый этаж')])[0]
        self.assertEqual(result['status'],'scoring_conflict');self.assertEqual(result['metrics'],['area_sqm'])

    def test_unknown_aspect_is_retained_and_duplicate_warning_is_merged(self):
        p=replace(profile([criterion('rent_month',1,'lower')]),unmodeled_requirements=('Нужна парковка.','Нужна парковка.'))
        result=assess_requirement_coverage(p,[candidate(CONCEPT)])
        self.assertEqual(len(result),1);self.assertEqual(result[0]['status'],'unmodeled')

    def test_exact_client_waiver_needs_no_ad_proof_and_mixed_obligation_stays_unresolved(self):
        quote='Внешние посетители и случайный поток не нужны.'
        p=replace(profile([criterion('rent_month',1,'lower')]),unmodeled_requirements=(quote,),metadata={'scenario_contract':{'waivers':[dict(quote=quote)]}})
        self.assertEqual(assess_requirement_coverage(p,[])[0]['status'],'waived_by_client')
        mixed='Лифт не нужен, но нужен доступ без ступеней.'
        p=replace(p,unmodeled_requirements=(mixed,),metadata={'scenario_contract':{'waivers':[dict(quote=mixed)]}})
        self.assertEqual(assess_requirement_coverage(p,[])[0]['status'],'unmodeled')

    def test_unreviewed_or_unproved_condition_does_not_remove_warning(self):
        quote='Нужен первый этаж.';p=replace(profile([criterion('rent_month',1,'lower')]),unmodeled_requirements=(quote,))
        for mode in ('review','proof','identity'):
            item=candidate(quote)
            if mode=='review':item.floor_assessment.semantic_review_status='not_reviewed'
            elif mode=='proof':item.floor_assessment.inference_metadata['proof_verifications'][0]['verdict']='unknown'
            else:item.floor_assessment.inference_metadata['proof_verifications'][0]['claims'][0]['requirement_id']='different'
            self.assertEqual(assess_requirement_coverage(p,[item])[0]['status'],'unmodeled')
