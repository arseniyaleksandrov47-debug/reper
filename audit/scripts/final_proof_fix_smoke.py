"""One bounded live scenario for the final source version; no market collection."""
import asyncio,hashlib,json,logging,sys,time
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
import retest_proof_fix as audit
from real_estate_engine import NvidiaCompetitorAnalyzer

async def main():
    out=ROOT/'audit/proof_fix_2026-11-03'/datetime.now(timezone(timedelta(hours=3))).strftime('final_%H%M%S_%f');out.mkdir()
    (out.parent/'LATEST_FINAL.txt').write_text(str(out),encoding='utf-8');audit.OUT=out
    audit.TracedResolver.gate=asyncio.Semaphore(2)
    before=audit.hashes();db_hash=hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest()
    analyzer=NvidiaCompetitorAnalyzer();resolver=audit.TracedResolver(analyzer,'final_smoke',batch_size=4)
    profile=await resolver.resolve('Кофейня','Посетители заходят с улицы без пропуска; нужен разрешённый путь до двери помещения')
    offers=[
        dict(id='street',floor=1,title='Контрольное помещение',description='Первый этаж. Отдельный вход с улицы прямо к двери помещения. Свободный вход всем посетителям без пропуска.',expected='eligible'),
        dict(id='forbidden',floor=7,title='Контрольное помещение',description='Седьмой этаж. Допускаются только сотрудники. Вход внешним посетителям строго запрещён, пропуска им не выдаются. Есть лифт.',expected='not_eligible'),
        dict(id='unknown',floor=2,title='Контрольное помещение',description='Второй этаж, рядом метро. О входе посетителей и маршруте к помещению данных нет.',expected='needs_verification'),
        dict(id='second_street',floor=1,title='Контрольное помещение',description='На первом этаже нежилое помещение. Вход с пешеходной улицы без ступеней, дверь ведёт непосредственно в помещение. Доступ разрешён любым посетителям без пропуска.',expected='eligible')]
    start=time.perf_counter();decisions=await resolver.assess_many([{k:v for k,v in p.items() if k!='expected'} for p in offers],profile,timeout_s=60)
    checks=audit.checks_for(offers,decisions)
    result=dict(scope='One live, hand-labelled four-candidate scenario; not a market ranking test.',checks=checks,
        passed=sum(c['passed'] for c in checks),total=len(checks),seconds=time.perf_counter()-start,
        assessments={k:asdict(v) for k,v in decisions.items()},source_code_unchanged=before==audit.hashes(),
        working_database_unchanged=db_hash==hashlib.sha256((ROOT/'data/moscow_commercial.db').read_bytes()).hexdigest(),
        http_attempts=analyzer.floor_request_pacer.http_attempts,http_429=analyzer.floor_request_pacer.throttle_responses)
    audit.save('result.json',result);audit.save('source_hashes.json',before)
    print(json.dumps({k:v for k,v in result.items() if k!='assessments'},ensure_ascii=False,indent=2),flush=True)

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
