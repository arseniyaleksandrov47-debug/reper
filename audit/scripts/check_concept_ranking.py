"""Three live concept-profile requests, followed by controlled arithmetic checks."""
import argparse,asyncio,hashlib,json,logging,sys,time
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from real_estate_engine import NvidiaCompetitorAnalyzer,RankingProfileResolver,OptimalPropertyRecommender,MetroStationFlow
from real_estate_engine.ranking_policy import bind_reference_scales
from test_engine_regressions import candidate,LAT,LNG
from test_remaining_problems import environment


def sources():
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'real_estate_engine').rglob('*.py')}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--case',choices=['walk_in','booking','internal']);args=parser.parse_args()
    base=ROOT/'audit/concept_ranking_2026-11-04';base.mkdir(exist_ok=True)
    out=base/datetime.now(timezone(timedelta(hours=3))).strftime('run_%H%M%S_%f');out.mkdir()
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    frozen=sources();db=hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest()
    class TracedAnalyzer(NvidiaCompetitorAnalyzer):
        def _call_ranking_profile_structured_sync(self,prompt,max_tokens,schema):
            response=super()._call_ranking_profile_structured_sync(prompt,max_tokens,schema)
            raw,model=response
            (out/f'wire_{time.time_ns()}.json').write_text(raw,encoding='utf-8')
            return response
    analyzer=TracedAnalyzer();resolver=RankingProfileResolver(analyzer);records=[]
    cases=[
        ('walk_in','Кофейня','Кофейня навынос. Покупатели — прохожие, которые спонтанно заходят с пешеходной улицы. Главный критерий — проходящий поток у входа; более высокая аренда допустима ради потока. Предпочтительна площадь 30–70 м².',1),
        ('booking','Студия озвучки','Все клиенты приезжают по предварительной записи, их привожу через интернет. Случайный уличный поток не нужен. Главный критерий — низкая аренда; также важен удобный путь от метро. Площадь нужна 30–70 м².',2),
        ('internal','Кофейня','Кофейня только для сотрудников внутри бизнес-центра. Покупатели уже находятся в здании и имеют разрешённый путь к помещению. Внешний уличный поток не использую. Главный критерий — подтверждённая внутренняя аудитория именно этого БЦ, затем низкая аренда. Площадь 30–70 м².',3)]
    pairs=[]
    for identifier,rent,traffic,metro,audience,walk in [(1,180000,95,80,100,2),(2,60000,10,200,200,5),(3,100000,20,700,1200,12)]:
        evidence={name:dict(value=value,verified=True,source='Synthetic controlled example, not a real owner or route measurement')
                  for name,value in [('site_audience_daily',audience),('walk_time_minutes',walk)]}
        prop=candidate(identifier,area_sqm=50,price_month=rent,price_sqm_year=rent*12/50,raw_json=json.dumps(dict(business_evidence=evidence)))
        report=environment(traffic);report.foot_traffic.nearest_metros=[MetroStationFlow('Тестовая',metro,100000,1000)]
        report.foot_traffic.closest_stop_distance_m=metro
        report.population=SimpleNamespace(data_status='ok',data_warnings=[],estimated_residents_count=1000,
            estimated_office_workers_count=500,buildings_data_available=True,building_coverage_complete=True,
            malformed_buildings_count=0)
        pairs.append((prop,report))
    for name,business,concept,expected in cases:
        if args.case and name!=args.case:continue
        started=time.perf_counter();profile=await resolver.resolve(business,concept,dict(min_area_sqm=30,max_area_sqm=70),timeout_s=25)
        before_cache=analyzer.floor_request_pacer.http_attempts
        repeat=await resolver.resolve(business,concept,dict(min_area_sqm=30,max_area_sqm=70),timeout_s=25) if profile.analysis_status=='valid' else None
        cached=repeat is not None and before_cache==analyzer.floor_request_pacer.http_attempts and asdict(profile)==asdict(repeat)
        if profile.status=='ready':profile=bind_reference_scales(profile)
        record=dict(case=name,profile=asdict(profile),cache_avoids_http=cached,seconds=time.perf_counter()-started,
                    controlled_expected_winner=expected)
        if profile.status=='ready':
            ranked=OptimalPropertyRecommender._score_and_rank_candidates(pairs,ranking_profile=profile)
            record['controlled_winner']=ranked[0].property_data['id']
            record['controlled_expectation_met']=record['controlled_winner']==expected
            record['controlled_scores']=[dict(id=c.property_data['id'],lower=c.total_score,upper=c.score_upper_bound,
                components=c.scoring_components) for c in ranked]
        else:record['controlled_expectation_met']=False
        records.append(record);(out/f'{name}.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:v for k,v in record.items() if k not in ('profile','controlled_scores')},ensure_ascii=False),flush=True)
    summary=dict(live_profiles=len(records),profiles_ready=sum(r['profile']['status']=='ready' for r in records),
        controlled_rankings_passed=sum(r['controlled_expectation_met'] for r in records),
        cache_checks_passed=sum(r['cache_avoids_http'] for r in records),
        http_attempts=analyzer.floor_request_pacer.http_attempts,http_429=analyzer.floor_request_pacer.throttle_responses,
        source_code_unchanged=frozen==sources(),working_database_unchanged=db==hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest(),
        interpretation='Live profiles with fixed synthetic candidate facts, not a measured business-profit or real-market evaluation.')
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
