"""Use the actual completed NVIDIA profile reply for the identical live request.

The independent interpreter reply and native configuration are revalidated by
the ordinary resolver. Exact input data is checked; no preferences or property
facts are inserted. Premises/access/environment stages remain live.
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
import practical_city_trial as trial
from real_estate_engine.ai_contract import AITextResponse
from two_integrated_runs import save

origin=Path((ROOT/'audit/practical_trial/LATEST.txt').read_text('utf-8'))
prior=Path((origin/'CITY_LATEST.txt').read_text('utf-8'))
diagnostic=origin/'profile_transport_diagnostic'
request=json.loads((diagnostic/'request.json').read_text('utf-8'))
response=json.loads((diagnostic/'response_2500.json').read_text('utf-8'))
choice=response['choices'][0]
assert choice['finish_reason']=='stop'
expected=json.loads(request['messages'][1]['content'].split('\nINPUT_JSON\n',1)[1])
semantic=None
interpreter_origin=prior
provenance_path=prior/'profile_reuse_provenance.json'
if provenance_path.is_file():
    interpreter_origin=Path(json.loads(provenance_path.read_text('utf-8'))['independent_interpreter_origin'])
for path in interpreter_origin.glob('ai_*.json'):
    recorded=json.loads(path.read_text('utf-8'))
    if 'interpretations' in json.loads(recorded['text']):semantic=recorded;break
assert semantic
OriginalAnalyzer=trial.TracedAnalyzer


class ReusingAnalyzer(OriginalAnalyzer):
    def __init__(self,out):
        super().__init__(out)
        save(out/'profile_reuse_provenance.json',dict(scope=__doc__,request_origin=str(diagnostic/'request.json'),
            configuration_origin=str(diagnostic/'response_2500.json'),independent_interpreter_origin=str(interpreter_origin),
            finish_reason=choice['finish_reason'],usage=response.get('usage')))

    def _call_scenario_contract_structured_sync(self,prompt,max_tokens,schema):
        data=json.loads(prompt.split('\nINPUT_JSON\n',1)[1])
        if data['concept']!=expected['concept']:raise ValueError('Changed concept: cannot reuse interpreter')
        return AITextResponse(semantic['text'],semantic['model'],semantic['metadata'])

    def _call_ranking_profile_structured_sync(self,prompt,max_tokens,schema):
        actual=json.loads(prompt.split('\nINPUT_JSON\n',1)[1])
        if actual!=expected:raise ValueError('Changed request data: cannot reuse configuration')
        return AITextResponse(choice['message']['content'],response.get('model',self.floor_model),
            dict(finish_reason='stop',usage=response.get('usage'),audit_origin=str(diagnostic/'response_2500.json')))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL)
    trial.TracedAnalyzer=ReusingAnalyzer
    asyncio.run(trial.main())
