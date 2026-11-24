"""Bounded exact-ID completion from public organization cards."""
import asyncio
import json
import re
import time
from .ai_contract import AI_DEADLINE


def exact_organization(body,org_id):
    match=re.search(r'<script[^>]*class="state-view"[^>]*>(.*?)</script>',body,re.S)
    if not match:raise ValueError('Public organization state missing')
    stack=[json.loads(match.group(1))];found=[]
    while stack:
        value=stack.pop()
        if isinstance(value,list):stack.extend(value)
        elif isinstance(value,dict):
            if str(value.get('id'))==str(org_id) and value.get('title') and ('features' in value or 'categories' in value):found.append(value)
            stack.extend(v for v in value.values() if isinstance(v,(dict,list)))
    unique={json.dumps(v,sort_keys=True,ensure_ascii=False):v for v in found}
    if len(unique)!=1:raise ValueError('No unique exact-ID organization card')
    return next(iter(unique.values()))


class OrganizationDetailCompleter:
    def __init__(self,collector,*,limit=8):
        self.collector=collector;self.limit=limit

    def fetch(self,org_id):
        if not str(org_id).isdigit():raise ValueError('Invalid organization ID')
        response=self.collector.session.get('https://yandex.ru/maps/org/'+str(org_id)+'/',headers=self.collector.headers,timeout=8)
        if response.status_code!=200 or len(response.content)>4000000:raise ValueError('Public organization source unavailable')
        return exact_organization(response.text,org_id)

    async def complete(self,cards,lat,lng,radius,*,force_refresh=False):
        from .competitors import merge_map_card,haversine_distance
        from .competitor_classification import ConceptCompetitorClassifier
        chosen=cards[:self.limit];semaphore=asyncio.Semaphore(2)
        async def one(card):
            async with semaphore:
                cache=getattr(self.collector,'source_cache',None)
                if cache is None:item=await asyncio.to_thread(self.fetch,card.org_id);metadata=dict(cache_status='disabled')
                else:
                    key=cache.key('yandex_organization_details_v1',org_id=card.org_id)
                    snap=await asyncio.to_thread(cache.get_or_fetch,key,lambda:self.fetch(card.org_id),ttl=300,force_refresh=force_refresh)
                    item=snap.value;metadata=snap.diagnostics()
                detailed=self.collector._parse_card(item,lat,lng,radius)
                if detailed is None or detailed.org_id!=card.org_id:raise ValueError('Organization outside the selected area')
                if haversine_distance(card.lat,card.lng,detailed.lat,detailed.lng)>50:raise ValueError('Organization location changed')
                return detailed,metadata
        tasks=[asyncio.create_task(one(c)) for c in chosen]
        budget=12
        if AI_DEADLINE.get() is not None:budget=min(budget,max(0,AI_DEADLINE.get()-time.monotonic()-5))
        try:
            if tasks:
                _,pending=await asyncio.wait(tasks,timeout=budget)
                for task in pending:task.cancel()
            outcomes=await asyncio.gather(*tasks,return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        changed=[];records={}
        for card,outcome in zip(chosen,outcomes):
            if isinstance(outcome,BaseException):
                records[card.org_id]=dict(status='unavailable',error_type=type(outcome).__name__);continue
            detailed,metadata=outcome;before=ConceptCompetitorClassifier.facts(card)
            merge_map_card(card,detailed)
            if detailed.histogram_data is not None:
                card.histogram_data=detailed.histogram_data;card.traffic_profile_str=detailed.traffic_profile_str
            if ConceptCompetitorClassifier.facts(card)!=before:changed.append(card.org_id)
            records[card.org_id]=dict(status='checked',source_url=detailed.url,**metadata)
        return dict(requested=len(cards),checked=sum(v['status']=='checked' for v in records.values()),
                    changed_ids=changed,deferred_ids=[c.org_id for c in cards[self.limit:]],records=records)
