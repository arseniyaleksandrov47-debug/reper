"""Missing floor identity is relevant only through actual client conditions."""
import unittest

from floor_ai_fixtures import RecordingFloorInference, FixtureFloorAnalyzer
from test_engine_regressions import LAT, LNG, property_offer, memory_storage, competition_report
from real_estate_engine import OptimalPropertyRecommender
from real_estate_engine.floor_policy import BusinessFloorProfileResolver


ACCESS=('Записанные посетители допускаются к кабинету ежедневно с 10:00 до 21:00. '
        'Посетитель входит с улицы в отдельный вход и по коридору проходит непосредственно к двери кабинета.')
CONCEPT='Посетители приходят по записи. Этаж не ограничен, нужен разрешённый путь до двери кабинета.'


def response(payload, *, unknown=False, denied=False):
    decisions=[]
    for item in payload['candidates']:
        facts=item['facts'].get('source_context',item['facts'])
        checks=[]
        for requirement in item['requirements']:
            result='unknown' if unknown else 'not_met' if denied and requirement['requirement_id']=='audience_access' else 'met'
            check={**requirement,'critical':True,'result':result,
                   'evidence':[] if result=='unknown' else [dict(field='description',value=facts['description'])]}
            check.pop('evidence_scope',None)
            checks.append(check)
        decisions.append(dict(candidate_id=item['candidate_id'],status='needs_verification' if unknown else 'rejected' if denied else 'eligible',
            reason='Проверка допуска и пути по исходному объявлению',checks=checks,missing_facts=[]))
    return dict(assessments=decisions)


class UnknownFloorContextTests(unittest.IsolatedAsyncioTestCase):
    async def resolver(self, *, unknown=False, denied=False, context=CONCEPT, allowed=None, requirements=()):
        handler=lambda p:response(p,unknown=unknown,denied=denied)
        model=RecordingFloorInference(handler=handler,review_handler=handler)
        resolver=BusinessFloorProfileResolver(model)
        profile=await resolver.resolve('Произвольный бизнес',context,allowed_floors=allowed,
            requirement_quotes=requirements,context_quotes=(context,))
        return resolver,profile,model

    def prop(self,**changes):
        return dict(dict(id=1,floor=None,title='Кабинет',description=ACCESS),**changes)

    async def test_missing_number_with_proven_access_can_be_eligible(self):
        resolver,profile,model=await self.resolver()
        result=(await resolver.assess_many([self.prop()],profile))['1']
        self.assertEqual(result.status,'eligible')
        self.assertEqual(result.floors,())
        self.assertTrue(any('Этаж не указан' in warning for warning in result.warnings))
        self.assertTrue(model.calls);self.assertTrue(model.proof_calls)
        self.assertTrue(all(c['result']=='met' for c in result.checks))

    async def test_missing_access_stays_unknown_after_context_assessment(self):
        resolver,profile,model=await self.resolver(unknown=True)
        prop=self.prop();prop['description']='Площадь помещения указана в объявлении.'
        result=(await resolver.assess_many([prop],profile))['1']
        self.assertEqual(result.status,'needs_verification')
        self.assertEqual(result.analysis_status,'valid')
        self.assertTrue(model.calls)
        self.assertTrue(any(c['result']=='unknown' for c in result.checks))
        self.assertNotEqual(result.reason,'Этаж не указан — требуется уточнение')

    async def test_explicit_numeric_floor_filter_still_stops_unknown_without_ai(self):
        resolver,profile,model=await self.resolver(allowed=(1,))
        result=(await resolver.assess_many([self.prop()],profile))['1']
        self.assertEqual(result.status,'needs_verification')
        self.assertEqual(result.analysis_status,'insufficient_facts')
        self.assertEqual(model.calls,[])

    async def test_natural_language_first_floor_is_not_proven_by_generic_access_text(self):
        quote='Нужен первый этаж.'
        resolver,profile,model=await self.resolver(context=CONCEPT+' '+quote,requirements=(quote,))
        result=(await resolver.assess_many([self.prop()],profile))['1']
        self.assertEqual(result.status,'needs_verification')
        condition=next(c for c in result.checks if c['requirement_id']=='request_0')
        self.assertEqual(condition['result'],'unknown')
        self.assertTrue(model.calls)

    async def test_conflicting_numbers_still_require_source_clarification(self):
        resolver,profile,model=await self.resolver()
        prop=self.prop();prop.update(floor=2,description=ACCESS+' Помещение на седьмом этаже.')
        # Numeric source claims are used so this is independent of word parsing.
        prop['description']=ACCESS+' Помещение на 7 этаже.'
        result=(await resolver.assess_many([prop],profile))['1']
        self.assertEqual(result.analysis_status,'source_conflict')
        self.assertEqual(model.calls,[])

    async def test_missing_floor_does_not_hide_a_proven_access_rejection(self):
        resolver,profile,model=await self.resolver(denied=True)
        prop=self.prop();prop['description']='Допуск только сотрудникам. Посетители не допускаются к помещению.'
        result=(await resolver.assess_many([prop],profile))['1']
        self.assertEqual(result.status,'rejected')
        self.assertEqual(result.floors,())
        self.assertTrue(model.proof_calls)

    async def test_eligible_missing_number_reaches_real_recommender_environment_stage(self):
        storage,conn=memory_storage();self.addCleanup(conn.close)
        storage.upsert_batch([property_offer(floor=None,title='Кабинет',description=ACCESS)])
        resolver,_,model=await self.resolver()
        calls=[]
        class Analyzer(FixtureFloorAnalyzer):
            _call_floor_structured_sync=model._call_floor_structured_sync
            async def analyze_circle(self,**kwargs):
                calls.append(kwargs);return competition_report()
        engine=OptimalPropertyRecommender(storage,Analyzer())
        report=await engine.find_optimal_properties('Произвольный бизнес',business_format=CONCEPT,
            center_lat=LAT,center_lng=LNG,min_area_sqm=20,max_area_sqm=60,max_price_month=110000)
        self.assertEqual(len(calls),1)
        self.assertEqual(report.evaluated_count,1)
        self.assertIsNone(report.best_candidate.property_data['floor'])


if __name__=='__main__':unittest.main()
