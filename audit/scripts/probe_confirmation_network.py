"""Unauthenticated connectivity probe; never prints keys or proxy credentials."""
import concurrent.futures
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
import requests

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from real_estate_engine.collectors.base import _DirectInterfaceBridge


def main():
    bridge=_DirectInterfaceBridge.get_proxy_url()
    paths=[('environment',True,None),('system_route',False,None),('existing_source_bridge',False,bridge)]
    def probe(settings):
        name,trust_env,proxy=settings;start=time.perf_counter()
        with requests.Session() as session:
            session.trust_env=trust_env
            if proxy:session.proxies={'http':proxy,'https':proxy}
            try:
                response=session.get('https://integrate.api.nvidia.com/v1/models',timeout=(4,5))
                return dict(route=name,status=response.status_code,seconds=round(time.perf_counter()-start,2))
            except Exception as exc:return dict(route=name,error_type=type(exc).__name__,seconds=round(time.perf_counter()-start,2))
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:result=list(pool.map(probe,paths))
    dest=ROOT/'audit/records/confirmation_network_probe.json';dest.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))


if __name__=='__main__':main()
