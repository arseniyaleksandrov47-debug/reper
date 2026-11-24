import copy
import json
import unittest
from types import SimpleNamespace

from real_estate_engine.ai_contract import AIContractError
from real_estate_engine.profile_parameters import parameter_issues,resolve_parameters
from real_estate_engine.ranking_policy import RankingProfileResolver,RankingProfile
from real_estate_engine.market_context import validate_market_context,compile_audience_usage
from real_estate_engine.scenario_contract import compile_priority_order
from test_market_context import market_config,CONCEPT


def failed_parameters():
    config=market_config()
    config['references']['residents']['value']=0
    config['references']['site_audience']['value']=0
    config['audience']['residents']['weight']=0
    scenario=dict(audience_usage={'residents':{'usage':'use'}},priority_rules=[])
    return config,scenario


class ParameterAnalyzer:
    def __init__(self):self.bad=False;self.calls=[]
    def _call_profile_parameters_structured_sync(self,prompt,tokens,*,response_schema):
        payload=json.loads(prompt.split('INPUT_JSON\n')[1]);self.calls.append(payload)
        response={}
        for key,item in payload['parameters'].items():
            value=.1 if item['maximum']==1 else 100
            if item.get('metric'):value=.8 if item['metric']=='premises_cost_month' else .2
            response[key]=dict(value=0 if self.bad else value,reason='Условное значение численного контрольного примера')
        return json.dumps(response),'independent-parameter-model'


class ParameterTests(unittest.IsolatedAsyncioTestCase):
    async def test_warehouse_zero_parameters_are_resolved_without_changing_sources(self):
        config,scenario=failed_parameters();original=copy.deepcopy(config)
        profile=dict(status='ready',criteria=[])
        issues=parameter_issues(profile,config,scenario,{})
        self.assertEqual({'.'.join(item['path']) for item in issues},{
            'market_context.references.residents.value','market_context.references.site_audience.value',
            'market_context.audience.residents.weight'})
        analyzer=ParameterAnalyzer()
        _,updated,metadata=await resolve_parameters(analyzer,CONCEPT,profile,config,scenario,issues)
        updated=validate_market_context(updated,CONCEPT)
        updated,_=compile_audience_usage(updated,scenario['audience_usage'])
        self.assertGreater(updated['audience']['residents']['weight'],0)
        self.assertEqual(config,original)
        self.assertEqual(updated['strength'],original['strength'])
        self.assertEqual(metadata['model'],'independent-parameter-model')
        self.assertFalse(metadata['property_facts_generated'])
        self.assertEqual(len(analyzer.calls),1)

    async def test_invalid_parameter_answer_is_not_applied(self):
        config,scenario=failed_parameters();original=copy.deepcopy(config)
        analyzer=ParameterAnalyzer();analyzer.bad=True
        profile=dict(status='ready',criteria=[])
        with self.assertRaises(AIContractError):
            await resolve_parameters(analyzer,CONCEPT,profile,config,scenario,
                parameter_issues(profile,config,scenario,{}))
        self.assertEqual(config,original)

    async def test_same_model_cannot_claim_independent_parameter_resolution(self):
        config,scenario=failed_parameters();original=copy.deepcopy(config)
        profile=dict(status='ready',criteria=[])
        with self.assertRaises(AIContractError):
            await resolve_parameters(ParameterAnalyzer(),CONCEPT,profile,config,scenario,
                parameter_issues(profile,config,scenario,{}),primary_model='independent-parameter-model')
        self.assertEqual(config,original)

    async def test_equal_importances_are_reconsidered_by_model_not_epsilon(self):
        profile=dict(status='ready',criteria=[dict(metric='market_fit_index',weight=.5,reason='Тест',concept_quote=CONCEPT),
            dict(metric='premises_cost_month',weight=.5,reason='Тест',concept_quote=CONCEPT)])
        scenario=dict(priority_rules=[{'higher':'occupancy_cost','lower':'audience_access_choice'}])
        groups={'market_fit_index':'audience_access_choice','premises_cost_month':'occupancy_cost'}
        config=market_config();issues=parameter_issues(profile,config,scenario,groups)
        self.assertEqual(len(issues),2)
        updated,_,_=await resolve_parameters(ParameterAnalyzer(),CONCEPT,profile,config,scenario,issues)
        checked,_=compile_priority_order(updated,scenario,groups)
        self.assertGreater(checked['criteria'][1]['weight'],checked['criteria'][0]['weight'])
        self.assertEqual(profile['criteria'][0]['weight'],.5)


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_equal_integer_and_float_constraints_reuse_the_same_profile(self):
        resolver=RankingProfileResolver(SimpleNamespace(floor_model='primary',floor_review_model='review'))
        calls=[]
        async def generate(business,concept,requirements):
            calls.append(copy.deepcopy(requirements))
            return RankingProfile(status='ready',concept=concept)
        resolver._generate=generate
        first={'min_area_sqm':40,'max_area_sqm':80,'competition_diameter_m':400}
        await resolver.resolve('Бизнес',CONCEPT,first)
        await resolver.resolve('Бизнес',CONCEPT,{k:float(v) for k,v in first.items()})
        self.assertEqual(len(calls),1)
        await resolver.resolve('Бизнес',CONCEPT,{**first,'min_area_sqm':40.5})
        self.assertEqual(len(calls),2)
        self.assertEqual(first['min_area_sqm'],40)


if __name__=='__main__':unittest.main()
