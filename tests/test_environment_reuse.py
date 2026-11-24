"""Public source reuse must preserve freshness, geometry and concept isolation."""
import asyncio
import json
import tempfile
import threading
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from real_estate_engine.ai_contract import AITextResponse
from real_estate_engine.competitors import NvidiaCompetitorAnalyzer,YandexMapsRadiusCollector
from real_estate_engine.environment_cache import EnvironmentSourceCache
from real_estate_engine.population import BuildingFetchResult
from test_engine_regressions import LAT,LNG,population_analyzer
from test_remaining_problems import building


class SnapshotTests(unittest.TestCase):
    def test_expiry_force_refresh_and_mutation_do_not_reset_source_age(self):
        clock=[100.0];cache=EnvironmentSourceCache(None,clock=lambda:clock[0]);calls=[]
        def fetch():calls.append(1);return dict(value=[len(calls)])
        first=cache.get_or_fetch('k',fetch,ttl=5);first.value['value'].append('mutation')
        clock[0]=104;second=cache.get_or_fetch('k',fetch,ttl=5)
        self.assertEqual(second.value,{'value':[1]});self.assertEqual(second.fetched_at,100)
        self.assertEqual(second.expires_at,105);self.assertEqual(second.cache_status,'memory')
        clock[0]=105;expired=cache.get_or_fetch('k',fetch,ttl=5)
        self.assertEqual(expired.value,{'value':[2]});self.assertEqual(expired.fetched_at,105)
        forced=cache.get_or_fetch('k',fetch,ttl=5,force_refresh=True)
        self.assertEqual(forced.value,{'value':[3]});self.assertEqual(forced.cache_status,'fresh')

    def test_persistent_snapshot_keeps_timestamp_and_honors_shorter_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'source.sqlite3'
            one=EnvironmentSourceCache(path).get_or_fetch('k',lambda:[1],ttl=60)
            two=EnvironmentSourceCache(path).get_or_fetch('k',lambda:self.fail('Unexpected fetch'),ttl=10)
            self.assertEqual(two.cache_status,'disk');self.assertEqual(two.fetched_at,one.fetched_at)
            self.assertEqual(two.expires_at,one.fetched_at+10)
            three=EnvironmentSourceCache(path,clock=lambda:one.fetched_at+11).get_or_fetch('k',lambda:[2],ttl=10)
            self.assertEqual(three.value,[2]);self.assertEqual(three.cache_status,'fresh')

    def test_partial_empty_and_failed_sources_remain_distinct(self):
        cache=EnvironmentSourceCache(None);calls=[]
        def partial():calls.append(1);return dict(complete=False,items=[1])
        for i in range(2):cache.get_or_fetch('partial',partial,ttl=30,cacheable=lambda v:v['complete'])
        self.assertEqual(len(calls),2)
        empty=cache.get_or_fetch('empty',lambda:[],ttl=30)
        again=cache.get_or_fetch('empty',lambda:self.fail('Empty successful source must be reusable'),ttl=30)
        self.assertEqual(empty.value,again.value);self.assertEqual(again.cache_status,'memory')
        with self.assertRaises(RuntimeError):cache.get_or_fetch('broken',lambda:(_ for _ in ()).throw(RuntimeError()),ttl=30)
        self.assertEqual(cache.get_or_fetch('broken',lambda:[3],ttl=30).value,[3])
        self.assertFalse(cache._flights)

    def test_broken_disk_and_unserializable_optional_snapshot_preserve_fresh_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'broken.sqlite3';path.write_bytes(b'not a SQLite database')
            cache=EnvironmentSourceCache(path)
            self.assertEqual(cache.get_or_fetch('k',lambda:[1],ttl=30).value,[1])
            # A malformed value must not poison the cache or erase other valid facts.
            value=cache.get_or_fetch('nan',lambda:dict(valid=[1],unknown=float('nan')),ttl=30)
            self.assertEqual(value.value['valid'],[1]);self.assertNotIn('nan',cache._memory)

    def test_concurrent_requests_coalesce_without_blocking_another_key(self):
        cache=EnvironmentSourceCache(None);started=threading.Event();release=threading.Event();calls=[]
        def slow():
            calls.append(1);started.set()
            if not release.wait(3):raise TimeoutError('test gate')
            return [1]
        with ThreadPoolExecutor(max_workers=4) as pool:
            first=pool.submit(cache.get_or_fetch,'a',slow,ttl=30)
            self.assertTrue(started.wait(2))
            second=pool.submit(cache.get_or_fetch,'a',slow,ttl=30)
            other=pool.submit(cache.get_or_fetch,'b',lambda:[2],ttl=30)
            try:self.assertEqual(other.result(2).value,[2])
            finally:release.set()
            a,b=first.result(2),second.result(2)
        self.assertEqual(len(calls),1);self.assertEqual(a.value,b.value)
        self.assertIn(b.cache_status,('shared','memory'))

    def test_memory_is_bounded(self):
        cache=EnvironmentSourceCache(None,max_entries=2)
        for i in range(4):cache.get_or_fetch(str(i),lambda:[1],ttl=30)
        self.assertEqual(len(cache._memory),2)


def maps(cache,fetch):
    collector=YandexMapsRadiusCollector.__new__(YandexMapsRadiusCollector)
    collector.source_cache=cache;collector._fetch_query_sync=fetch
    collector._build_queries_for_business=lambda business,extra_queries=None:list(dict.fromkeys(['base',business]+(extra_queries or [])))
    return collector


def raw_card(identifier='a',lat=LAT):
    return dict(id=identifier,title='Предложение',coordinates=[LNG,lat],categories=[dict(name='Услуга')])


class EnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_queries_reused_but_new_niche_extra_geometry_and_radius_fetched(self):
        calls=Counter();cache=EnvironmentSourceCache(None)
        def fetch(query,lat,lng,diameter):calls[query]+=1;return [raw_card()]
        collector=maps(cache,fetch)
        one=await collector.collect_in_circle(LAT,LNG,400,'a')
        one[0].features.append('mutation');one[0].fns_inn='foreign-entity'
        two=await collector.collect_in_circle(LAT,LNG,400.0,'b',extra_queries=['extra'])
        self.assertEqual(calls,Counter(base=1,a=1,b=1,extra=1))
        self.assertFalse(two[0].features);self.assertIsNone(two[0].fns_inn)
        self.assertEqual(two.source_snapshots['base']['fetched_at'],one.source_snapshots['base']['fetched_at'])
        moved=await collector.collect_in_circle(LAT+.00001,LNG,400,'a')
        self.assertGreater(moved[0].distance_m,one[0].distance_m)
        await collector.collect_in_circle(LAT,LNG,200,'a')
        self.assertEqual(calls['base'],3)
        await collector.collect_in_circle(LAT,LNG,400,'a',force_refresh=True)
        self.assertEqual(calls['base'],4)

    async def test_failed_query_retried_while_healthy_query_stays_cached(self):
        calls=Counter()
        def fetch(query,*args):
            calls[query]+=1
            if query=='fail' and calls[query]==1:raise RuntimeError('controlled outage')
            return [raw_card()]
        collector=maps(EnvironmentSourceCache(None),fetch)
        first=await collector.collect_in_circle(LAT,LNG,400,'fail')
        second=await collector.collect_in_circle(LAT,LNG,400,'fail')
        self.assertEqual(first.status,'partial');self.assertEqual(second.status,'ok')
        self.assertEqual(calls,Counter(base=1,fail=2))

    async def test_cancelling_one_map_request_preserves_shared_source_for_other_client(self):
        gate=threading.Event();started=asyncio.Event();calls=[];loop=asyncio.get_running_loop()
        def fetch(*args):
            calls.append(1);loop.call_soon_threadsafe(started.set)
            if not gate.wait(3):raise TimeoutError('test gate')
            return [raw_card()]
        collector=maps(EnvironmentSourceCache(None),fetch)
        first=asyncio.create_task(collector.collect_in_circle(LAT,LNG,400,'base'))
        await asyncio.wait_for(started.wait(),2)
        second=asyncio.create_task(collector.collect_in_circle(LAT,LNG,400,'base'))
        try:
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):await first
        finally:gate.set()
        result=await asyncio.wait_for(second,2)
        self.assertEqual(len(result),1);self.assertEqual(len(calls),1)
        again=await collector.collect_in_circle(LAT,LNG,400,'base')
        self.assertEqual(again.source_snapshots['base']['cache_status'],'memory')

    async def test_malformed_source_is_not_reused_after_parser_failure(self):
        calls=Counter()
        def fetch(query,*args):
            calls[query]+=1
            return [dict(id='a',title='Invalid coordinates',coordinates=['bad',LAT],categories=[dict(name='Услуга')])] if calls[query]==1 else [raw_card()]
        collector=maps(EnvironmentSourceCache(None),fetch)
        first=await collector.collect_in_circle(LAT,LNG,400,'base')
        second=await collector.collect_in_circle(LAT,LNG,400,'base')
        self.assertEqual(first.status,'partial');self.assertEqual(second.status,'ok')
        self.assertEqual(calls['base'],2)

    async def test_expiry_gets_new_card_facts_without_accumulating_deleted_cards(self):
        clock=[100.0];generation=[0];cache=EnvironmentSourceCache(None,clock=lambda:clock[0])
        collector=maps(cache,lambda *args:[raw_card(str(generation[0]))])
        one=await collector.collect_in_circle(LAT,LNG,400,'base')
        clock[0]=401;generation[0]=1
        two=await collector.collect_in_circle(LAT,LNG,400,'base')
        self.assertEqual([c.org_id for c in one],['0']);self.assertEqual([c.org_id for c in two],['1'])

    async def test_population_sources_reused_across_diameters_only_when_geometry_matches(self):
        analyzer=population_analyzer();cache=EnvironmentSourceCache(None);analyzer.source_cache=cache;calls=Counter()
        def buildings(*args):calls['buildings']+=1;return BuildingFetchResult([building()],coverage_complete=True)
        def prices(*args):calls['prices']+=1;return [500000],[25]
        analyzer._fetch_2gis_buildings_sync=buildings;analyzer._fetch_yandex_realty_prices_sync=prices
        one=await analyzer.analyze_circle(LAT,LNG,400)
        two=await analyzer.analyze_circle(LAT,LNG,400)
        left,right=asdict(one),asdict(two)
        for d in (left,right):d.pop('fetch_time_ms');d.pop('source_snapshots')
        self.assertEqual(left,right);self.assertEqual(calls,Counter(buildings=1,prices=1))
        two.top_residential_buildings[0].estimated_residents=999999
        three=await analyzer.analyze_circle(LAT,LNG,200)
        self.assertNotEqual(three.estimated_residents_count,999999)
        self.assertEqual(calls,Counter(buildings=2,prices=1))
        await analyzer.analyze_circle(LAT+.00001,LNG,200)
        self.assertEqual(calls,Counter(buildings=3,prices=2))

    async def test_population_partial_retries_only_failed_part_then_force_refreshes_both(self):
        analyzer=population_analyzer();analyzer.source_cache=EnvironmentSourceCache(None);calls=Counter()
        def buildings(*args):calls['buildings']+=1;return BuildingFetchResult([building()],coverage_complete=True)
        def prices(*args):
            calls['prices']+=1
            return ([],[]) if calls['prices']==1 else ([],[25])
        analyzer._fetch_2gis_buildings_sync=buildings;analyzer._fetch_yandex_realty_prices_sync=prices
        first=await analyzer.analyze_circle(LAT,LNG);second=await analyzer.analyze_circle(LAT,LNG)
        self.assertEqual(first.data_status,'partial');self.assertEqual(second.data_status,'ok')
        third=await analyzer.analyze_circle(LAT,LNG)
        self.assertEqual(third.median_flat_price_mln_rub,25)
        self.assertIsNone(third.median_housing_price_sqm_rub)
        self.assertEqual(calls,Counter(buildings=1,prices=2))
        await analyzer.analyze_circle(LAT,LNG,force_refresh=True)
        self.assertEqual(calls,Counter(buildings=2,prices=3))

    async def test_truncated_building_snapshot_does_not_become_a_complete_cached_circle(self):
        analyzer=population_analyzer();analyzer.source_cache=EnvironmentSourceCache(None);calls=[]
        def buildings(*args):
            calls.append(1)
            return BuildingFetchResult([building()],coverage_complete=len(calls)>1,warnings=['truncated'] if len(calls)==1 else [])
        analyzer._fetch_2gis_buildings_sync=buildings
        analyzer._fetch_yandex_realty_prices_sync=lambda *args:([500000],[25])
        one=await analyzer.analyze_circle(LAT,LNG);two=await analyzer.analyze_circle(LAT,LNG)
        self.assertFalse(one.building_coverage_complete);self.assertTrue(two.building_coverage_complete)
        self.assertEqual(len(calls),2)

    async def test_reusing_source_facts_never_reuses_another_concept_decision(self):
        calls=Counter();cache=EnvironmentSourceCache(None)
        def fetch(query,*args):calls[query]+=1;return [raw_card()]
        analyzer=NvidiaCompetitorAnalyzer.__new__(NvidiaCompetitorAnalyzer)
        analyzer.map_collector=maps(cache,fetch);analyzer.model='fixture'
        analyzer.population_analyzer=population_analyzer();analyzer.population_analyzer.source_cache=cache
        concepts=[]
        def infer(prompt,tokens,**kwargs):
            data=json.loads(prompt.split('\nINPUT_JSON\n',1)[1]);concepts.append(data['business_concept'])
            direct=data['business_concept']=='concept-a'
            rows=[dict(idx=o['idx'],same_purchase_offer='yes' if direct else 'no',
                need_relation='same_purchase' if direct else 'unrelated',relationship_basis='specific_purchase' if direct else 'unrelated',
                audience_relation='overlap',scenario_access='compatible',evidence_fields=['categories'],
                offer_evidence_ids=['category:0'] if direct else [],reason='Контроль независимости концепций') for o in data['organizations']]
            return AITextResponse(json.dumps(dict(categories=rows,summary='Контроль')),'fixture',{})
        analyzer._call_nvidia_sync=infer
        a=await analyzer.analyze_circle(LAT,LNG,400,'a',business_concept='concept-a',enrich_fns=False)
        b=await analyzer.analyze_circle(LAT,LNG,400,'b',business_concept='concept-b',enrich_fns=False)
        self.assertEqual(calls['base'],1);self.assertEqual(concepts,['concept-a','concept-b'])
        self.assertEqual(len(a.direct_competitors),1);self.assertEqual(len(b.unrelated_businesses),1)
        self.assertEqual(b.ai_response_metadata['environment_sources']['maps']['base']['cache_status'],'memory')
