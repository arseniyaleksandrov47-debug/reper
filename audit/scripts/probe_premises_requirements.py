"""Live text/VLM calls on an archival, exactly bound offer-card and its photos.

This tests interpretation, not a current offer's availability or permission.
Source age is preserved. No database or landlord contact is involved.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from real_estate_engine import NvidiaCompetitorAnalyzer
from real_estate_engine.ai_contract import AI_DEADLINE
from real_estate_engine.property_confirmation import PropertyConfirmationVerifier
from real_estate_engine.property_sources import cian_unit


CONCEPT=('Офис для трёх сотрудников, клиентских посещений не будет. '
    'Нужна остеклённая входная дверь. Нужен санузел внутри арендуемого блока. '
    'Нужен подъезд грузовика непосредственно к входу. Ремонт и обустройство планирую сам.')


async def main():
    out=ROOT/'audit/premises_requirements';out.mkdir(exist_ok=True)
    archive=ROOT/'audit/client_fixes/sources'
    prop=json.loads((archive/'cian_expected.json').read_text(encoding='utf-8'))
    page=archive/'cian_unit.html';stamp=page.stat().st_mtime
    unit=cian_unit(page.read_text(encoding='utf-8'),prop['url'],prop)
    catalog={f's0:{key}':dict(value=value,source_url=unit['url'],fetched_at=stamp,scope='unit')
        for key,value in unit['facts'].items()}
    photos=[dict(url=url,source_url=unit['url'],fetched_at=stamp) for url in unit['photos']]
    ai=NvidiaCompetitorAnalyzer(assessment_cache_path=None,environment_cache_path=None)
    verifier=PropertyConfirmationVerifier(ai)
    result=dict(scope=__doc__,concept=CONCEPT,source_url=unit['url'],source_snapshot_at=stamp,
        source_age_seconds=time.time()-stamp,photos=[p['url'] for p in photos],checks={})
    token=AI_DEADLINE.set(time.monotonic()+120)
    started=time.monotonic()
    try:
        result['stage']='extract_requirements'
        requirements=await verifier._requirements(CONCEPT)
        result['extracted_requirements']=requirements
        result['stage']='assess_text'
        primary,model=await verifier._assess(CONCEPT,catalog,requirements)
        result.update(primary=primary,primary_model=model)
        result['checks']['client_conditions_separated']=len(primary['client_requirements']['items'])==3
        result['stage']='prove_text'
        review,review_model=await verifier._prove(CONCEPT,catalog,primary,only={'client_requirements'})
        baseline=verifier._condition_result('client_requirements',primary['client_requirements'],review['client_requirements'])
        result['text_verified_requirements']=baseline
        result['stage']='check_photos'
        visual_started=time.monotonic()
        enriched,facts,diagnostics=await verifier._photo_requirements(CONCEPT,catalog,baseline,photos,model)
        result['photo_seconds']=round(time.monotonic()-visual_started,3)
        result['photo_analysis']=diagnostics
        result['client_requirements']=enriched
        result['evidence']={**catalog,**facts};result['models']=[model,review_model,diagnostics.get('model')]
        result['checks']['actual_vlm_observations_received']=bool(facts)
        result['checks']['unknown_conditions_not_rejected']=all(
            i['result']!='fail' for i in enriched.get('items',[]) if i['id'] in {
                r['id'] for r in baseline.get('items',[]) if r['result']=='unknown'})
        result['stage']='complete'
    except Exception as exc:
        result['error']=dict(type=type(exc).__name__,phase=getattr(exc,'phase',None),status_code=getattr(exc,'status_code',None))
    finally:AI_DEADLINE.reset(token)
    result['seconds']=round(time.monotonic()-started,3)
    (out/'live_interpretation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(seconds=result['seconds'],checks=result['checks'],error=result.get('error'),
        photo_analysis=result.get('photo_analysis')),ensure_ascii=False))


if __name__=='__main__':asyncio.run(main())
