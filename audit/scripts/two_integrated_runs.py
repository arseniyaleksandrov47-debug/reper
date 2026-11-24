"""Two bounded public-search runs with live NVIDIA/maps/population/FNS.

Run 1: explicitly synthetic listings and business inputs, real environment.
Run 2: three frozen real listings, no fabricated business facts, real environment.
Original DB is read-only; migrations/subsetting apply only to an audit copy.
This does not measure ingestion or notification delivery latency.
"""
import asyncio
import argparse
import contextlib
import hashlib
import io
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from real_estate_engine import PropertyStorage,OptimalPropertyRecommender,NvidiaCompetitorAnalyzer,CanonicalProperty,PlatformSource
from real_estate_engine.business_model import concept_digest
from test_business_model import model_data,evidence,fact
from run_engine import print_top_comparison_table,print_detailed_location_report

COFFEE=('Кофейня навынос. Покупатели приходят пешком с улицы, вход непосредственно в помещение открыт любым посетителям. '
        'Нужен первый этаж. Главный приоритет — возможные заказы с учётом доступной аудитории, маршрутов и конкурентов. '
        'Затем важны все расходы на помещение, включая эксплуатацию и подготовку. Площадь 30–70 м².')
BOOKING=('Студия консультаций. Все клиенты приезжают по предварительной записи, их привожу через интернет. '
         'Нужен разрешённый путь записанного посетителя к двери помещения в рабочие часы. '
         'Случайный поток прохожих не нужен. Главный критерий — низкие полные расходы на помещение, '
         'включая аренду, эксплуатацию и подготовку. Предпочтительна площадь 30–100 м².')


def fingerprints():
    paths=[ROOT/'run_engine.py',*(ROOT/'real_estate_engine').rglob('*.py')]
    hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    hashes['moscow_commercial.db']=hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest()
    return hashes


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False,default=str),encoding='utf-8')


def event(name,**fields):
    print(json.dumps(dict(event=name,**fields),ensure_ascii=False),flush=True)


class TracedAnalyzer(NvidiaCompetitorAnalyzer):
    def __init__(self,out):
        super().__init__();self.out=out;self.current_case='startup';self.environment_records=[]
        def trace_http_error(response,*args,**kwargs):
            if response.status_code>=400:
                message=response.text
                if self.api_key:message=message.replace(self.api_key,'[redacted]')
                save(self.out/f'http_error_{self.current_case}_{time.time_ns()}.json',
                     dict(status=response.status_code,message=message[:2000]))
                event('http_error',case=self.current_case,status=response.status_code,message=message[:500])
        self.http_session.hooks.setdefault('response',[]).append(trace_http_error)

    def _call_nvidia_sync(self,*args,**kwargs):
        started=time.perf_counter();record=dict(case=self.current_case,model=kwargs.get('model_override',self.model))
        try:
            response=super()._call_nvidia_sync(*args,**kwargs)
            raw,model=response
            save(self.out/f'ai_{self.current_case}_{time.time_ns()}.json',dict(text=raw,model=model,metadata=getattr(response,'metadata',{})))
            record.update(status='complete',seconds=round(time.perf_counter()-started,3),model=model)
            return response
        except Exception as exc:
            record.update(status='failed',error_type=type(exc).__name__,status_code=getattr(exc,'status_code',None),seconds=round(time.perf_counter()-started,3))
            raise
        finally:event('ai_call',**record)

    async def analyze_circle(self,**kwargs):
        start=time.perf_counter();record=dict(case=self.current_case,lat=kwargs['center_lat'],lng=kwargs['center_lng'])
        try:
            report=await super().analyze_circle(**kwargs)
            record.update(status=report.data_status,cards=report.total_cards_found,
                          population=report.population.data_status if report.population else 'unavailable')
            save(self.out/f'environment_{self.current_case}_{time.time_ns()}.json',asdict(report))
            return report
        except Exception as exc:
            record.update(status='failed',error_type=type(exc).__name__);raise
        finally:
            record['seconds']=round(time.perf_counter()-start,3)
            self.environment_records.append(record);event('environment',**record)


def control_storage(out):
    store=PropertyStorage(str(out/'controlled.db'))
    items=[]
    for identifier,lat,lng,rent,floor in [('control_a',55.765064,37.59881,180000,1),
                                         ('control_b',55.737877,37.522049,90000,1),
                                         ('control_seventh',55.7558,37.6173,40000,7)]:
        description=('СИНТЕТИЧЕСКОЕ ОБЪЯВЛЕНИЕ ДЛЯ АУДИТА. Помещение на первом этаже. '
                     'Отдельный вход с тротуара непосредственно в помещение. '
                     'Любые посетители с улицы свободно проходят через этот вход к двери помещения в рабочие часы.') if floor==1 else (
                     'СИНТЕТИЧЕСКОЕ ОБЪЯВЛЕНИЕ ДЛЯ АУДИТА. Помещение на седьмом этаже. Доступ только сотрудникам по пропускам.')
        items.append(CanonicalProperty(source=PlatformSource.CIAN,external_id=identifier,title=f'Синтетический контроль {identifier}',
            address=f'Синтетическая точка {identifier}; координаты выбраны для живого запроса окружения Москвы',lat=lat,lng=lng,
            area_sqm=50,price_month=rent,price_sqm_year=rent*12/50,floor=floor,has_separate_entrance=floor==1,
            description=description,url='https://example.invalid/synthetic-audit'))
    store.upsert_batch(items)
    data=dict(properties={},ranking_scales=dict(concept_sha256=concept_digest(COFFEE),metrics={
        'premises_cost_month':dict(low=0,high=250000,source='Synthetic audit client cost ceiling; not market calibration')}))
    for item in items:
        model=model_data(COFFEE);model['cohorts'][0]['people']=fact(1400 if item.external_id=='control_a' else 600)
        # The live competitor inventory is deliberately not declared complete.
        # Real found alternatives not parameterized here must widen uncertainty.
        model['competitor_inventory']['coverage_complete']=False
        data['properties'][f'{item.source.value}:{item.external_id}']=evidence(model)
    save(out/'controlled_inputs.json',dict(listings=[asdict(x) for x in items],business_data=data))
    return store,data


def real_storage(out):
    original=ROOT/'data/moscow_commercial.db';target=out/'real_subset.db'
    with sqlite3.connect(original.as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(target) as dest:
        src.backup(dest)
        dest.execute('DELETE FROM properties WHERE id NOT IN (80,81,205)')
    store=PropertyStorage(str(target))
    with store._get_connection() as conn:
        rows=[dict(r) for r in conn.execute('SELECT * FROM properties ORDER BY id')]
    if len(rows)!=3:raise RuntimeError('The expected frozen three-listing sample is unavailable')
    save(out/'real_sample.json',rows)
    return store


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',choices=('all','controlled_coffee','real_booking'),default='all')
    parser.add_argument('--output-dir',default='audit/integrated_two_runs_2026-11-04')
    parser.add_argument('--min-request-interval',type=float,default=None)
    args=parser.parse_args()
    if args.min_request_interval is not None:
        if not 0<args.min_request_interval<60:raise ValueError('Request interval must be in (0,60)')
        os.environ['NVIDIA_MIN_REQUEST_INTERVAL_S']=str(args.min_request_interval)
    base=(ROOT/args.output_dir).resolve()
    if not base.is_relative_to((ROOT/'audit').resolve()):raise ValueError('Audit output must remain inside workspace/audit')
    base.mkdir(exist_ok=True)
    out=base/datetime.now(timezone(timedelta(hours=3))).strftime('run_%H%M%S_%f');out.mkdir()
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    frozen=fingerprints();controlled,data=control_storage(out);real=real_storage(out)
    analyzer=TracedAnalyzer(out);records=[]
    save(out/'manifest.json',dict(started=datetime.now(timezone.utc).isoformat(),fingerprints=frozen,
        scope='Two small bounded searches; no fresh listing collection or notification delivery. Controlled inputs are synthetic, real listings are a legacy snapshot.'))
    cases=[('controlled_coffee',controlled,'Кофейня',COFFEE,data,(1,)),
           ('real_booking',real,'Студия консультаций',BOOKING,None,None)]
    for label,store,business,concept,business_data,allowed in cases:
        if args.case!='all' and label!=args.case:continue
        analyzer.current_case=label;engine=OptimalPropertyRecommender(store,analyzer)
        start=time.perf_counter();attempts=analyzer.floor_request_pacer.http_attempts
        event('case_started',case=label,listing_origin='synthetic' if business_data else 'saved_real_listings')
        try:
            report=await asyncio.wait_for(engine.find_optimal_properties(business,center_lat=55.7558,center_lng=37.6173,
                search_radius_m=18000,min_area_sqm=20,max_area_sqm=130,max_price_month=320000,
                business_concept=concept,business_data=business_data,allowed_floors=allowed,max_candidates=3,
                ranking_timeout_s=25,floor_timeout_s=90,environment_timeout_s=45,fns_timeout_s=10),timeout=180)
            save(out/f'{label}_report.json',asdict(report))
            with contextlib.redirect_stdout(io.StringIO()) as rendered:
                print_top_comparison_table(report)
                print('\nСтатус выбора:',report.selection_status)
                for warning in report.ranking_warnings:print('[!]',warning)
                for missing in report.missing_inputs:print('Уточнить:',missing)
                for cand in report.ranked_candidates:
                    print('\nПаспорт варианта для проверки',cand.property_data['id'])
                    print_detailed_location_report(cand.property_data,cand.competition_report,business,400)
            (out/f'{label}_report.txt').write_text(rendered.getvalue(),encoding='utf-8')
            record=dict(case=label,status='completed',seconds=round(time.perf_counter()-start,3),
                profile_status=report.ranking_profile.status if report.ranking_profile else None,
                numerical_matches=report.numerical_matching_count,eligible=report.total_matching_in_db,evaluated=report.evaluated_count,
                rejected=len(report.floor_rejected_candidates),needs_verification=len(report.floor_verification_candidates),
                selection_status=report.selection_status,comparison_complete=report.comparison_complete,errors=report.analysis_errors,
                stage_timings_ms=report.stage_timings_ms,missing_inputs=report.missing_inputs,
                factors=[dict(metric=c.metric,weight=c.weight) for c in report.ranking_profile.criteria] if report.ranking_profile else [],
                scenario_contract=report.ranking_profile.metadata.get('scenario_contract') if report.ranking_profile else None,
                priority_order_adjusted=report.ranking_profile.metadata.get('priority_order_adjusted') if report.ranking_profile else None,
                floor_request_clauses=list(report.floor_profile.request_clauses) if report.floor_profile else [],
                ranked=[dict(id=c.property_data['id'],source=c.property_data['source'],external_id=c.property_data.get('external_id'),
                    address=c.property_data['address'],lower=c.total_score,upper=c.score_upper_bound,coverage=c.score_coverage,
                    cards=c.competition_report.total_cards_found,environment=c.competition_report.data_status) for c in report.ranked_candidates])
            record['sanity_checks']=dict(rating_reached=bool(report.ranked_candidates),
                scores_bounded=all(0<=c.unrounded_total_score<=c.score_upper_bound<=100+1e-8 for c in report.ranked_candidates) if report.ranked_candidates else None,
                no_unverified_in_rating=all(c.floor_assessment.status=='eligible' for c in report.ranked_candidates) if report.ranked_candidates else None,
                seventh_not_ranked=all(c.property_data.get('external_id')!='control_seventh' for c in report.ranked_candidates) if business_data else None,
                original_candidate_count=report.numerical_matching_count==3)
        except Exception as exc:
            record=dict(case=label,status='failed',error_type=type(exc).__name__,seconds=round(time.perf_counter()-start,3))
        record['http_attempts']=analyzer.floor_request_pacer.http_attempts-attempts
        records.append(record);save(out/f'{label}_summary.json',record);event('case_completed',**record)
        save(out/'progress.json',records)
    summary=dict(runs=records,http_attempts=analyzer.floor_request_pacer.http_attempts,http_429=analyzer.floor_request_pacer.throttle_responses,
                 environment_requests=analyzer.environment_records,source_and_working_database_unchanged=frozen==fingerprints())
    save(out/'summary.json',summary)
    event('finished',output=str(out),runs_completed=sum(r['status']=='completed' for r in records),http_attempts=summary['http_attempts'],
          http_429=summary['http_429'],unchanged=summary['source_and_working_database_unchanged'])


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
