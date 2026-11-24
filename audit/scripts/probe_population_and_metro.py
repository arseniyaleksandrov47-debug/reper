"""Live source integration and controlled rating sensitivity; no AI calls or lease recommendation."""
import argparse
import asyncio
import copy
import hashlib
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from real_estate_engine.competitors import YandexMapsRadiusCollector,RadiusCompetitionReport
from real_estate_engine.metro_entrances import MetroEntranceSource
from real_estate_engine.spb_metro_source import SpbMetroFlowSource
from real_estate_engine.population import ResidentialPopulationAnalyzer,population_residents_interval
from real_estate_engine.traffic import FootTrafficEstimator
from real_estate_engine.ranking_policy import RankingProfile,RankingCriterion,evaluate_profile


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'audit/territory_population/integration')
    parser.add_argument('--profile-report',type=Path,default=ROOT/'audit/spb_regional_client_retry/run_20261107_185559/first_search.json')
    parser.add_argument('--refresh-metro',action='store_true')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    db=ROOT/'data/moscow_commercial.db';original=hashlib.sha256(db.read_bytes()).hexdigest()
    saved=json.loads(args.profile_report.read_text(encoding='utf-8'))['ranking_profile']
    saved['criteria']=tuple(RankingCriterion(**c) for c in saved['criteria']);profile=RankingProfile(**saved)
    populations=ResidentialPopulationAnalyzer();maps=YandexMapsRadiusCollector()
    entrances=MetroEntranceSource();flows=SpbMetroFlowSource()
    entrance_data,flow_data=await asyncio.gather(entrances.get_dataset('spb',force_refresh=args.refresh_metro),flows.get_dataset())
    results=[]
    for name,lat,lng in [('Сенной округ',59.925,30.316),('Коломна',59.921,30.282)]:
        started=time.monotonic()
        pop,cards=await asyncio.gather(populations.analyze_circle(lat,lng),maps.collect_in_circle(
            center_lat=lat,center_lng=lng,diameter_m=400,target_business='кофейня'))
        report=RadiusCompetitionReport(target_business='кофейня',center_lat=lat,center_lng=lng,diameter_m=400,radius_m=200,
            total_cards_found=len(cards),direct_competitors=[],indirect_competitors=[],synergy_businesses=[],
            unrelated_businesses=[],unclassified_businesses=list(cards),category_breakdown={},ai_summary='',
            model_used='classification intentionally not run',map_fetch_time_ms=0,ai_inference_time_ms=0,
            population=pop,data_status='partial',collection_status=cards.status)
        report.foot_traffic=FootTrafficEstimator.estimate_for_circle(lat,lng,200,cards,metro_dataset=flow_data,metro_entrances=entrance_data)
        # Equal synthetic cost/area isolate source effects; this is not a real listing selection.
        prop=dict(lat=lat,lng=lng,price_month=100000,area_sqm=50,price_sqm_year=24000,raw_attributes={})
        before=copy.deepcopy(report);before.population.estimated_residents_count=None
        before.population.estimated_residents_range=None;before.population.population_model={}
        before.foot_traffic=FootTrafficEstimator.estimate_for_circle(lat,lng,200,cards,metro_dataset=flow_data)
        baseline=evaluate_profile(profile,prop,before);updated=evaluate_profile(profile,prop,report)
        trace=updated['business_model']['market_context']['sources']['residents']
        result=dict(name=name,lat=lat,lng=lng,seconds=round(time.monotonic()-started,3),
            population=asdict(pop),metro=asdict(report.foot_traffic),cards=len(cards),map_status=cards.status,
            baseline_score=baseline,updated_score=updated,
            checks=dict(model_range_enters_rating=trace['known'] and population_residents_interval(pop) is not None,
                population_is_modeled=bool(pop.population_model.get('modeled')),
                no_population_date_rewrite=all(t['as_of']=='2025-01-01' for t in pop.population_model.get('territories',[])),
                all_map_cards_left_unclassified=not report.direct_competitors and len(report.unclassified_businesses)==len(cards)))
        results.append(result)
        (args.output/(('sennoy' if name=='Сенной округ' else 'kolomna')+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(dict(name=name,seconds=result['seconds'],cards=len(cards),population=pop.estimated_residents_count,
            range=pop.estimated_residents_range,metro_stations=len(report.foot_traffic.nearest_metros),
            score_before=(baseline['lower'],baseline['upper']),score_after=(updated['lower'],updated['upper']),checks=result['checks']),ensure_ascii=False),flush=True)
    result=dict(scope=__doc__,profile_origin=str(args.profile_report),metro_source=entrances.diagnostics,
        flow_source=flows.diagnostics,checks=dict(original_database_unchanged=hashlib.sha256(db.read_bytes()).hexdigest()==original),
        points=[dict(name=r['name'],checks=r['checks'],seconds=r['seconds']) for r in results])
    (args.output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':asyncio.run(main())
