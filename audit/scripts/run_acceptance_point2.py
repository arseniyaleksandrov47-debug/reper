"""Real linked business -> published company INN -> FNS accounts -> score.

Uses a live map card and its own linked website. The scoring profile is an
explicit controlled utility configuration; no business profit is predicted.
"""
import asyncio
import copy
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'));sys.path.insert(0,str(ROOT/'tests'))
from two_integrated_runs import save,event,fingerprints
from real_estate_engine.competitors import YandexMapsRadiusCollector,ClassifiedCompetitor,CompetitionLevel
from real_estate_engine.fns import FnsBrowserRevenueCollector
from real_estate_engine.ranking_policy import evaluate_profile
from test_market_context import integrated_profile,environment
from test_engine_regressions import candidate


async def main():
    base=ROOT/'audit/acceptance_point2';out=base/datetime.now().strftime('run_%H%M%S_%f');out.mkdir(parents=True)
    (base/'LATEST.txt').write_text(str(out),encoding='utf-8')
    frozen=fingerprints();save(out/'manifest.json',dict(scope=__doc__,fingerprints=frozen))
    maps=YandexMapsRadiusCollector()
    raw=await asyncio.to_thread(maps._fetch_query_sync,'ВкусВилл',55.765064,37.59881,1000)
    cards=[maps._parse_card(r,55.765064,37.59881,1000) for r in raw]
    card=next((c for c in cards if c and 'вкусвилл' in c.title.lower().replace(' ','') and c.websites),None)
    if card is None:raise RuntimeError('No live map card with a business-linked website')
    save(out/'map_card_before.json',asdict(card));save(out/'map_raw.json',raw)
    collector=FnsBrowserRevenueCollector();http=[]
    original=collector.website_resolver.session.get
    def website_get(url,**kwargs):
        r=original(url,**kwargs)
        http.append(dict(service='business_website',url=url,status=r.status_code,bytes=len(r.content)))
        if r.status_code==200:(out/f'company_page_{len(http)}.html').write_bytes(r.content)
        save(out/'http_records.json',http)
        return r
    collector.website_resolver.session.get=website_get
    original_finance=collector._fetch_inns_fallback_http
    async def recorded_finance(inns):
        result=await original_finance(inns);save(out/'fns_raw.json',result);return result
    collector._fetch_inns_fallback_http=recorded_finance
    # Reuse the fixed quantitative environment solely to isolate the change
    # caused by the REAL account values. It is explicitly not a real full search.
    report=environment();report.direct_competitors=[ClassifiedCompetitor(card,CompetitionLevel.DIRECT,'Controlled same-purchase classification')]
    profile=integrated_profile();prop=candidate()
    before=evaluate_profile(profile,prop,report)
    started=time.perf_counter();count=await asyncio.wait_for(collector.enrich_cards([card]),timeout=60)
    after=evaluate_profile(profile,prop,report)
    save(out/'map_card_after.json',asdict(card));save(out/'match_diagnostics.json',collector.match_diagnostics)
    save(out/'score_before.json',before);save(out/'score_after.json',after)
    result=dict(card_title=card.title,card_address=card.address,websites=card.websites,enriched=count,
        match_status=card.fns_match_status,match_evidence=card.fns_match_evidence,inn=card.fns_inn,
        legal_name=card.fns_legal_name,reporting_year=card.fns_reporting_year,revenue_rub=card.fns_revenue_rub,
        net_profit_rub=card.fns_net_profit_rub,finance_source=card.fns_profile.source_url if card.fns_profile else None,
        seconds=round(time.perf_counter()-started,2),before_lower=before['lower'],before_upper=before['upper'],
        after_lower=after['lower'],after_upper=after['upper'],
        score_changed=abs(before['lower']-after['lower'])>1e-12 or abs(before['upper']-after['upper'])>1e-12,
        source_and_working_database_unchanged=frozen==fingerprints(),
        passed=count==1 and card.fns_match_status=='matched_company' and card.fns_revenue_rub is not None and
            card.fns_net_profit_rub is not None and card.fns_reporting_year is not None and
            (abs(before['lower']-after['lower'])>1e-12 or abs(before['upper']-after['upper'])>1e-12),
        limitation='Company accounts, not this outlet revenue. Controlled nonfinancial scoring inputs; no full market choice claim.')
    save(out/'summary.json',result);event('point2_completed',directory=str(out),**result)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
