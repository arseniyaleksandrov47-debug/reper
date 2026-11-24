"""Regressions for repeated ads and alphabetical ties in the live client run."""
import copy
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace

from real_estate_engine import OptimalPropertyRecommender
from real_estate_engine.confirmation_flow import group_verified_listings,add_admission_reason,property_keys
from test_concept_ranking import profile,criterion
from test_engine_regressions import candidate,competition_report
from test_business_model import fact,modeled_property


def listing(source='cian',floor=1,**changes):
    prop=dict(source=source,external_id=source,url='https://'+source+'.example/unit',
        address='Москва, Тестовый проезд, 9А',area_sqm=61.7,price_month=123400,
        floor=floor,dedup_hash='same_geometry_and_terms')
    prop.update(changes)
    return SimpleNamespace(property_data=prop,data_warnings=[],confirmation=dict(
        status='requires_confirmation',sources=[dict(identity_checked=True,fetched_at=time.time(),floor=floor)]))


class ListingGroupingTests(unittest.TestCase):
    def test_unknown_inventory_floor_can_join_after_exact_unit_confirmation(self):
        a=listing('avito');a.property_data['floor']=None
        b=listing('cian')
        result=group_verified_listings([a,b])
        self.assertEqual(len(result),1)
        self.assertEqual(result[0].property_data['listing_variants'][0]['url'],b.property_data['url'])
        self.assertIn('cian:cian',property_keys(result[0].property_data))
        group_verified_listings([a,b])
        self.assertEqual(len(a.property_data['listing_variants']),1)

    def test_other_floors_addresses_terms_and_unverified_sources_stay_separate(self):
        for change in ('floor','address','area','price','stale','unbound','unknown'):
            with self.subTest(change=change):
                a=listing('avito');b=listing('cian')
                if change=='floor':b.confirmation['sources'][0]['floor']=2
                if change=='address':b.property_data['address']='Москва, Другой проезд, 9А'
                if change=='area':b.property_data['area_sqm']=62
                if change=='price':b.property_data['price_month']=124000
                if change=='stale':b.confirmation['sources'][0]['fetched_at']=time.time()-301
                if change=='unbound':b.confirmation['sources'][0]['identity_checked']=False
                if change=='unknown':b.confirmation['sources'][0]['floor']=None
                self.assertEqual(len(group_verified_listings([a,b])),2)

    def test_admission_does_not_repeat_an_already_pending_physical_quote(self):
        quote='Нужен произвольный физический параметр.'
        c=listing();c.floor_assessment=SimpleNamespace(status='needs_verification',reason='Уточнить условие',missing_facts=[quote])
        c.confirmation.update(reasons=[dict(code='client_requirements',message='Уточнить свойства')],
            checks={'client_requirements':{'items':[dict(client_quote=quote,result='unknown')]}})
        add_admission_reason(c);add_admission_reason(c)
        self.assertEqual([r['code'] for r in c.confirmation['reasons']],['client_requirements'])
        self.assertEqual(len(c.data_warnings),1)


class KnownCostOrderingTests(unittest.TestCase):
    def test_bounded_cost_range_remains_a_provisional_lower_bound(self):
        p=replace(profile([criterion('premises_cost_month',1,'lower')]),metadata={'scale_origin':'ai_placeholders'})
        prop=modeled_property(data={'premises_cost':{'operating_month':fact(10000,20000)}})
        ranked=OptimalPropertyRecommender._score_and_rank_candidates([(prop,competition_report())],ranking_profile=p)
        self.assertTrue(ranked[0].business_model_diagnostics['ordering_basis']['provisional'])

    def test_equal_unscored_costs_use_known_minimum_not_portal_alphabet(self):
        p=replace(profile([criterion('premises_cost_month',1,'lower')]),metadata={'scale_origin':'ai_placeholders'})
        cheap=candidate(id=2,price_month=30000,source='zz',external_id='cheap')
        expensive=candidate(id=1,price_month=90000,source='aa',external_id='expensive')
        for pairs in ([(expensive,competition_report()),(cheap,competition_report())],
                      [(cheap,competition_report()),(expensive,competition_report())]):
            ranked=OptimalPropertyRecommender._score_and_rank_candidates(copy.deepcopy(pairs),ranking_profile=p)
            self.assertEqual(ranked[0].property_data['id'],2)
            self.assertEqual(ranked[0].business_model_diagnostics['ordering_basis']['value'],30000)
            self.assertTrue(ranked[0].business_model_diagnostics['ordering_basis']['provisional'])
            self.assertIsNone(ranked[0].business_model_diagnostics['cost']['total_month']['high'])
            self.assertEqual(ranked[0].unrounded_total_score,ranked[1].unrounded_total_score)
