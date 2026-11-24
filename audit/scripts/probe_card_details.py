"""Inspect public exact-organization pages; no model requests or DB writes."""
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from real_estate_engine.competitors import YandexMapsRadiusCollector


async def main():
    out=ROOT/'audit/source_completion'/datetime.now().strftime('probe_%Y%m%d_%H%M%S');out.mkdir(parents=True)
    (out.parent/'LATEST.txt').write_text(str(out),encoding='utf-8')
    data=json.loads((ROOT/'audit/confirmation_flow/run_20261105_151015/first_search.json').read_text(encoding='utf-8'))
    cards=data['best_candidate']['competition_report']['unclassified_businesses']
    collector=YandexMapsRadiusCollector();rows=[]
    for card in cards:
        def fetch():return collector.session.get(card['url'],headers=collector.headers,timeout=12)
        try:
            response=await asyncio.to_thread(fetch);body=response.text
            record=dict(id=card['org_id'],status=response.status_code,size=len(response.content))
            match=re.search(r'<script[^>]*class="state-view"[^>]*>(.*?)</script>',body,re.S)
            if not match:record['error']='no_state'
            else:
                state=json.loads(match.group(1));(out/(card['org_id']+'_state.json')).write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8')
                found=[];stack=[state]
                while stack:
                    value=stack.pop()
                    if isinstance(value,list):stack.extend(value)
                    elif isinstance(value,dict):
                        if str(value.get('id'))==card['org_id'] and any(k in value for k in ('features','categories')):found.append(value)
                        stack.extend(v for v in value.values() if isinstance(v,(dict,list)))
                (out/(card['org_id']+'_items.json')).write_text(json.dumps(found,ensure_ascii=False,indent=2),encoding='utf-8')
                record['matches']=len(found);record['feature_counts']=[len(v.get('features',[])) for v in found]
            rows.append(record)
        except Exception as exc:rows.append(dict(id=card['org_id'],error_type=type(exc).__name__))
    (out/'result.json').write_text(json.dumps(rows,indent=2),encoding='utf-8');print(json.dumps(rows))


if __name__=='__main__':asyncio.run(main())
