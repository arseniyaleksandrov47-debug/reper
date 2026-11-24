"""Regressions from the first practical request, at native and local contracts."""
import copy
import unittest

from real_estate_engine.ai_contract import AIContractError
from real_estate_engine.market_context import AUDIENCE,audience_usage_schema,validate_audience_usage
from real_estate_engine.ranking_policy import concept_fragments,profile_schema
from real_estate_engine.scenario_contract import scenario_schema,validate_scenario
from test_scenario_contract import SOURCES,scenario


class PracticalProfileContractTests(unittest.TestCase):
    def test_walk_in_without_appointments_cannot_create_a_native_transit_waiver(self):
        sources=concept_fragments('Покупатели приходят пешком без записи. Важны прохожие и жители.')
        schema=audience_usage_schema(sources)
        # This exact semantic mistake was emitted by the live model. Native
        # decoding must not offer a decision already forbidden by local guards.
        for name in AUDIENCE:self.assertNotIn('waived',schema['properties'][name]['properties']['usage']['enum'])
        data={name:dict(usage='use',source_id='s0',reason='Контроль',waiver_quote=None) for name in AUDIENCE}
        data['transit_flow']['usage']='waived'
        with self.assertRaises(AIContractError):validate_audience_usage(data,sources,dict(waivers=[]))

    def test_actual_waiver_remains_available_only_for_its_specific_audience(self):
        sources=concept_fragments('Случайный поток прохожих не нужен. Жители и сотрудники важны.')
        schema=audience_usage_schema(sources)
        self.assertIn('waived',schema['properties']['transit_flow']['properties']['usage']['enum'])
        for name in ('residents','workers','synergy','site_audience'):
            self.assertNotIn('waived',schema['properties'][name]['properties']['usage']['enum'])

    def test_translated_priority_quote_is_forbidden_before_it_reaches_local_validation(self):
        schema=scenario_schema(SOURCES)
        wrong=SOURCES['s3'].replace('Главный','Main')
        self.assertNotIn(wrong,schema['properties']['priority_rules']['items']['properties']['quote']['enum'])
        self.assertIn(SOURCES['s3'],schema['properties']['priority_rules']['items']['properties']['quote']['enum'])
        data=scenario();data['priority_rules'][0]['quote']=wrong
        with self.assertRaises(AIContractError):validate_scenario(data,SOURCES)

    def test_source_quotes_are_not_shortened_by_explanation_length_limits(self):
        sources={'s0':'Подробное условие '+('длинная цитата '*30)}
        schema=profile_schema(sources)
        quote=schema['properties']['criteria']['properties']['physical_fit']['properties']['concept_quote']
        self.assertNotIn('maxLength',quote)
        self.assertEqual(quote['enum'],['s0'])
        self.assertIn(sources['s0'],scenario_schema(sources)['properties']['primary_priority']['properties']['quote']['enum'])
