"""Live source-only cold/warm/restart checks. No NVIDIA calls or ranking replay."""
import asyncio
import hashlib
import json
import logging
import sys
import threading
import time
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from real_estate_engine.competitors import YandexMapsRadiusCollector
from real_estate_engine.population import ResidentialPopulationAnalyzer
from real_estate_engine.environment_cache import EnvironmentSourceCache
from run_acceptance_point3 import CASES


def save(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


async def main():
    base=ROOT/'audit/environment_reuse';out=base/datetime.now().strftime('run_%H%M%S_%f');out.mkdir(parents=True)
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    frozen=ROOT/'audit/acceptance_point3/run_233424_560929/five_real_ads.json'
    listings=json.loads(frozen.read_text('utf-8'))
    original_hash=hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest()
    calls=[];lock=threading.Lock();records=[]
    def instrument(session,kind):
        original=session.get
        def get(*args,**kwargs):
            record=dict(kind=kind,start=time.perf_counter())
            with lock:
                if len(calls)>=250:raise RuntimeError('Source audit HTTP budget exhausted')
                calls.append(record)
            try:
                response=original(*args,**kwargs);record['status']=response.status_code;return response
            except Exception as exc:record['error_type']=type(exc).__name__;raise
            finally:record['seconds']=round(time.perf_counter()-record['start'],3)
        session.get=get
    def collectors():
        cache=EnvironmentSourceCache(out/'source_snapshots.sqlite3')
        maps=YandexMapsRadiusCollector(source_cache=cache)
        population=ResidentialPopulationAnalyzer(source_cache=cache)
        instrument(maps.session,'maps');instrument(population.session,'buildings');instrument(population.realty_session,'housing_prices')
        return maps,population
    maps,population=collectors();slots=asyncio.Semaphore(2)
    for round_name,cases in [('cold',CASES),('warm',CASES),('restart',CASES[:1])]:
        if round_name=='restart':maps,population=collectors()
        for case in cases:
            before=len(calls);started=time.perf_counter()
            async def one(prop):
                async with slots:
                    cards,pop=await asyncio.gather(maps.collect_in_circle(prop['lat'],prop['lng'],400,case['business']),
                        population.analyze_circle(prop['lat'],prop['lng'],400))
                card_facts=[asdict(c) for c in cards];pop_facts=asdict(pop)
                pop_facts.pop('source_snapshots');pop_facts.pop('fetch_time_ms')
                facts=dict(cards=card_facts,population=pop_facts)
                stamp=hashlib.sha256(json.dumps(facts,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
                return dict(id=prop['id'],cards=len(cards),maps_status=cards.status,population_status=pop.data_status,
                    facts_sha256=stamp,maps_sources=cards.source_snapshots,population_sources=pop.source_snapshots)
            rows=await asyncio.gather(*(one(p) for p in listings))
            record=dict(round=round_name,case=case['key'],seconds=round(time.perf_counter()-started,3),
                http_attempts=len(calls)-before,source_http_counts=dict(Counter(c['kind'] for c in calls[before:])),listings=rows)
            records.append(record);save(out/'progress.json',dict(records=records,http=calls))
            print(json.dumps({k:v for k,v in record.items() if k!='listings'}),flush=True)
    checks=[]
    cold={r['case']:r for r in records if r['round']=='cold'}
    for r in records:
        if r['round']=='cold':continue
        for row in r['listings']:
            prior=next(v for v in cold[r['case']]['listings'] if v['id']==row['id'])
            for kind in ('maps_sources','population_sources'):
                for query,source in row[kind].items():
                    previous=prior[kind].get(query,{})
                    if previous.get('expires_at',0)>time.time():
                        checks.append(dict(check='reused_source_keeps_original_timestamp',round=r['round'],case=r['case'],id=row['id'],source=query,
                            passed=source.get('cache_status') in ('memory','disk','shared') and source.get('fetched_at')==previous.get('fetched_at')))
            # Complete successful source sets must yield exactly the same facts.
            same_snapshots=all(source.get('cache_status') in ('memory','disk','shared') and
                source.get('fetched_at')==prior[kind].get(query,{}).get('fetched_at')
                for kind in ('maps_sources','population_sources') for query,source in row[kind].items())
            if same_snapshots and prior['maps_status']=='ok' and prior['population_status']=='ok' and row['maps_status']=='ok' and row['population_status']=='ok':
                checks.append(dict(check='complete_environment_facts_unchanged',round=r['round'],case=r['case'],id=row['id'],
                    passed=prior['facts_sha256']==row['facts_sha256']))
    checks.append(dict(check='original_database_unchanged',passed=original_hash==hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest()))
    result=dict(scope=__doc__,recorded_ads=str(frozen.relative_to(ROOT)),nvidia_http_attempts=0,http_attempts=len(calls),
        records=records,checks=checks,all_checks_passed=all(c['passed'] for c in checks),http=calls,
        caveat='Source facts only. Failed/partial sources are retried; concept-specific AI decisions and full search time are not measured here.')
    save(out/'result.json',result)
    print(json.dumps(dict(directory=str(out),all_checks_passed=result['all_checks_passed'],checks=len(checks),http_attempts=len(calls))),flush=True)
    if not result['all_checks_passed']:raise SystemExit(1)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
