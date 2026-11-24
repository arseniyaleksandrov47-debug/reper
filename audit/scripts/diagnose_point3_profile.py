"""Diagnose one real profile request; this does not count as the 5x3 matrix.

The previously recorded, exact-concept interpreter answer is replayed solely
to isolate the generator. Native schema and production validation stay intact.
Only public/business inputs are recorded, never HTTP authorization headers.
"""
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from run_acceptance_point3 import CASES
from real_estate_engine import NvidiaCompetitorAnalyzer
from real_estate_engine.ai_contract import AITextResponse,strict_json_object
from real_estate_engine.ranking_policy import RankingProfileResolver


async def main():
    out=ROOT/'audit/acceptance_point3/ai_diagnostic';out.mkdir(exist_ok=True)
    prior=ROOT/'audit/acceptance_point3/run_191357_791788'
    files=list(prior.glob('ai_street_*.json'))
    recorded=next(json.loads(p.read_text('utf-8')) for p in files
                  if 'interpretations' in json.loads(json.loads(p.read_text('utf-8'))['text']))
    old=json.loads((ROOT/'audit/acceptance_point3/run_190750_767195/street_report.json').read_text('utf-8'))
    constraints=old['ranking_profile']['metadata']['client_constraints']
    case=CASES[0];assert old['ranking_profile']['concept']==case['concept']
    analyzer=NvidiaCompetitorAnalyzer();captured={}
    analyzer._call_scenario_contract_structured_sync=lambda *a:AITextResponse(recorded['text'],recorded['model'],recorded['metadata'])
    def capture(prompt,max_tokens,schema):
        captured.update(prompt=prompt,max_tokens=max_tokens,schema=schema)
        raise RuntimeError('Diagnostic capture only')
    analyzer._call_ranking_profile_structured_sync=capture
    await RankingProfileResolver(analyzer).resolve(case['business'],case['concept'],constraints)
    assert captured
    (out/'generator_input.json').write_text(json.dumps(captured,ensure_ascii=False,indent=2),encoding='utf-8')
    payload=dict(model=analyzer.floor_model,messages=[
        dict(role='system',content='Ты гео-маркетинговый аналитик коммерческой недвижимости. Отвечай СТРОГО валидным JSON без markdown-оберток и без лишнего текста.'),
        dict(role='user',content=captured['prompt'])],temperature=0,max_tokens=captured['max_tokens'],stream=True,
        chat_template_kwargs=dict(enable_thinking=False),
        response_format=dict(type='json_schema',json_schema=dict(name='floor_assessment_v4',strict=True,schema=captured['schema'])))
    analyzer.floor_request_pacer.wait_http_slot(10)
    start=time.perf_counter();events=[];text=[];finish=None;usage=None;done=False;error=None
    def event(**data):
        row=dict(seconds=round(time.perf_counter()-start,3),**data);events.append(row)
        (out/'stream_events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf-8')
        if data.get('type')!='chunk':print(json.dumps(row,ensure_ascii=False),flush=True)
    try:
        with analyzer.http_session.post(analyzer.endpoint,json=payload,stream=True,timeout=(5,20)) as response:
            event(type='headers',status=response.status_code)
            response.raise_for_status()
            for line in response.iter_lines(chunk_size=1):
                if time.perf_counter()-start>90:raise TimeoutError('Diagnostic 90 second deadline')
                if not line or not line.startswith(b'data:'):continue
                data=line[5:].strip()
                if data==b'[DONE]':done=True;break
                data=json.loads(data);usage=data.get('usage') or usage
                for choice in data.get('choices',[]):
                    delta=choice.get('delta') or {};piece=delta.get('content') or ''
                    if piece:text.append(piece)
                    if choice.get('finish_reason'):finish=choice['finish_reason']
                    event(type='chunk',chars=len(piece),reasoning_chars=len(delta.get('reasoning_content') or ''),finish=choice.get('finish_reason'))
            event(type='finished',done=done,finish=finish,chars=sum(map(len,text)),usage=usage)
    except Exception as exc:
        error=type(exc).__name__;event(type='error',error=error)
    answer=''.join(text)
    (out/'stream_answer.txt').write_text(answer,encoding='utf-8')
    valid=False
    if done and finish=='stop':
        try:strict_json_object(answer);valid=True
        except ValueError:pass
    result=dict(seconds=round(time.perf_counter()-start,3),done=done,finish=finish,valid_json=valid,error=error,
                first_content_seconds=next((e['seconds'] for e in events if e.get('chars')),None),chunks=len(events),chars=len(answer))
    (out/'stream_summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);asyncio.run(main())
