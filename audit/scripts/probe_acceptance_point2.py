"""Read-only discovery for acceptance point 2, with credential-safe HTTP logs."""
import asyncio
import json
import logging
import sys
from pathlib import Path
from datetime import datetime
from dataclasses import asdict

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from real_estate_engine.fns import FnsBrowserRevenueCollector


async def main():
    base=ROOT/'audit/acceptance_point2';out=base/datetime.now().strftime('probe_%H%M%S_%f');out.mkdir(parents=True)
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    source=ROOT/'audit/scenario_semantic_fix_2026-11-04/run_121128_830168/environment_controlled_coffee_1793783578660311900.json'
    old=json.loads(source.read_text(encoding='utf-8'))
    cards=[x['card'] for group in ('direct_competitors','indirect_competitors') for x in old[group]
        if x['card']['title'] in ('Правда кофе','Рыбторг')]
    collector=FnsBrowserRevenueCollector();records=[];suggestions=[]
    original=collector.dadata.session.post
    def post(url,**kwargs):
        query=json.loads(kwargs.get('data') or b'{}').get('query')
        try:
            r=original(url,**kwargs);records.append(dict(service='dadata',query=query,status=r.status_code,bytes=len(r.content)))
            if r.status_code==200:suggestions.append(dict(query=query,response=r.json()))
            return r
        finally:
            (out/'http_records.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    collector.dadata.session.post=post
    resolved=[]
    for card in cards:
        candidates=await asyncio.to_thread(collector.dadata.resolve_candidates_for_card,card['title'],card['categories'],card['address'])
        resolved.append(dict(card=card,candidates=candidates));print(json.dumps(dict(title=card['title'],candidates=candidates),ensure_ascii=False),flush=True)
    (out/'candidates.json').write_text(json.dumps(resolved,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'dadata_responses.json').write_text(json.dumps(suggestions,ensure_ascii=False,indent=2),encoding='utf-8')
    inns=list(dict.fromkeys(inn for r in resolved for inn,method in r['candidates']))[:4]
    raw=await collector._fetch_inns_fallback_http(inns)
    (out/'fns_raw.json').write_text(json.dumps(raw,ensure_ascii=False,indent=2),encoding='utf-8')
    profiles={inn:collector._parse_fns_payload(inn,payload,'candidate_probe') for inn,payload in raw.items()}
    result=dict(profiles={inn:asdict(p) if p else None for inn,p in profiles.items()},
        candidate_inns=inns,note='Candidates are not confirmed outlet operators; no financial match is accepted by this discovery probe')
    (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(directory=str(out),candidate_count=len(inns),fns_payloads=len(raw),profiles=result['profiles']),ensure_ascii=False),flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
