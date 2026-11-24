"""Bounded live listing-source diagnostics, with no cookies/tokens persisted."""
import asyncio
import hashlib
import html
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from real_estate_engine.collectors import AvitoCollector,YandexRealtyCollector
from real_estate_engine.storage import WORKSPACE_DB_PATH


def diagnose(response,params):
    body=response.text;lower=body.lower();part=urlparse(str(response.url))
    title=re.search(r'<title[^>]*>(.*?)</title>',body,re.S|re.I)
    h1=re.search(r'<h1\b[^>]*>(.*?)</h1>',body,re.S|re.I)
    initial=re.search(r'window\.INITIAL_STATE\s*=\s*',body)
    old_regex=re.search(r'window\.INITIAL_STATE\s*=\s*(\{.*?\});\s*(?:window\.|</script>|$)',body,re.S)
    markers=[word for word in ('showcaptcha','smartcaptcha','confirm you are human','доступ ограничен',
        'доступ временно ограничен','подтвердите, что вы не робот','доступ с вашего ip','too many requests') if word in lower]
    record=dict(http=response.status_code,host=part.hostname,path=part.path,page=params or {},bytes=len(response.content),
        title=html.unescape(title[1])[:200] if title else None,
        heading=html.unescape(re.sub('<[^>]+>',' ',h1[1]))[:250] if h1 else None,
        initial_state_marker=bool(initial),old_yandex_regex=bool(old_regex),avito_catalog_marker='"loaderData"' in body,
        block_markers=markers,retry_after=response.headers.get('Retry-After'))
    if initial:
        try:
            state,_=json.JSONDecoder().raw_decode(body[initial.end():].lstrip())
            record['decoded_state_keys']=list(state)[:12] if isinstance(state,dict) else type(state).__name__
            if isinstance(state,dict):
                rows=state.get('search',{}).get('offers',{}).get('entities',[])
                record['offers_count']=len(rows) if isinstance(rows,list) else None
                if isinstance(rows,list):
                    identities=[str(v.get('offerId') or v.get('id')) for v in rows if isinstance(v,dict)]
                    record['page_fingerprint']=hashlib.sha256(json.dumps(identities).encode()).hexdigest()
        except ValueError:record['state_decode_error']=True
    if not initial and '"loaderData"' not in body:
        visible=re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>',' ',body,flags=re.S|re.I)
        visible=html.unescape(re.sub('<[^>]+>',' ',visible))
        visible=re.sub(r'\b[A-Za-z0-9_=-]{30,}\b','[redacted]',visible)
        record['visible_message']=re.sub(r'\s+',' ',visible).strip()[:1200]
    return record


async def main():
    out=ROOT/'audit/source_availability'/datetime.now().strftime('run_%Y%m%d_%H%M%S')
    out.mkdir(parents=True);(out.parent/'LATEST.txt').write_text(str(out),encoding='utf-8')
    before=hashlib.sha256(WORKSPACE_DB_PATH.read_bytes()).hexdigest();results={}
    async def run(cls):
        collector=cls();records=[];get=collector.session.get
        def recorded_get(url,*args,**kwargs):
            response=get(url,*args,**kwargs)
            record=diagnose(response,kwargs.get('params'))
            record['pow_challenge_cookie_present']=bool(collector.session.cookies.get('pow_challenge'))
            records.append(record)
            (out/(collector.name+'_responses.json')).write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(dict(source=collector.name,**{k:record[k] for k in ('http','page','bytes','title','initial_state_marker','old_yandex_regex','block_markers')}),ensure_ascii=False),flush=True)
            return response
        collector.session.get=recorded_get;start=time.monotonic()
        offers=await collector.collect(120)
        results[collector.name]=dict(summary=collector.collection_summary(len(offers)),seconds=round(time.monotonic()-start,2),responses=records)
        if getattr(collector,'access_issue',None):
            previous=len(records)
            repeated=await collector.collect(1)
            results[collector.name]['cooldown_control']=dict(additional_requests=len(records)-previous,
                collected=len(repeated),summary=collector.collection_summary(len(repeated)))
        collector.session.close()
    await asyncio.gather(run(AvitoCollector),run(YandexRealtyCollector))
    result=dict(sources=results,original_database_unchanged=before==hashlib.sha256(WORKSPACE_DB_PATH.read_bytes()).hexdigest())
    (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('RESULT_DIRECTORY '+str(out),flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
