"""Small transport diagnosis, without logging credentials or API response bodies."""
import concurrent.futures
import json
import logging
import sys
import time
import requests
from urllib.parse import urlsplit
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from real_estate_engine import NvidiaCompetitorAnalyzer

logging.disable(logging.CRITICAL)
def probe(model):
    analyzer=NvidiaCompetitorAnalyzer();start=time.perf_counter()
    try:
        response=analyzer._call_nvidia_sync('Return exactly {"ok":true}',50,model_override=model,timeout=15,
            response_schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False})
        return dict(model=model,ok=True,seconds=time.perf_counter()-start,metadata=response.metadata)
    except Exception as exc:
        message=str(exc).lower()
        return dict(model=model,ok=False,seconds=time.perf_counter()-start,error_type=type(exc).__name__,
                    category='timeout' if 'timed out' in message else 'rate_limit' if '429' in message else 'other')

if __name__=='__main__':
    print(json.dumps({'environment_proxy_hosts':[(k,urlsplit(v).hostname,urlsplit(v).port) for k,v in requests.utils.get_environ_proxies('https://integrate.api.nvidia.com').items() if k!='no']},ensure_ascii=False),flush=True)
    with concurrent.futures.ThreadPoolExecutor(2) as executor:
        results=list(executor.map(probe,['nvidia/nemotron-3.5-lightning-30b-a3b','poolside/laguna-xs-2.1']))
    p=ROOT/'audit/proof_fix_2026-11-03/transport_probe.json';p.write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(results,indent=2))
