"""Inspect a failing saved schema request without exposing credentials."""
import ast,json,logging,re,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from real_estate_engine import NvidiaCompetitorAnalyzer
from real_estate_engine.ai_contract import floor_response_schema
logging.disable(logging.CRITICAL)
base=ROOT/'audit/proof_fix_2026-11-03'
out=Path((base/'LATEST_FULL.txt').read_text(encoding='utf-8'))
traces=[]
for f in sorted((out/'trace').glob('*.json')):
 try:traces.append(json.loads(f.read_text(encoding='utf-8')))
 except (ValueError,UnicodeError):pass
bad=next(d for d in traces if d.get('error_category')=='other')
data=bad['input'];analyzer=NvidiaCompetitorAnalyzer();start=time.perf_counter()
tree=ast.parse((ROOT/'real_estate_engine/floor_policy.py').read_text(encoding='utf-8'))
batch=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='batch')
instructions=ast.literal_eval(next(n.value for n in batch.body[0].body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='instructions' for t in n.targets)))
try:
 response=analyzer._call_floor_structured_sync(instructions+' Return JSON keyed assessments, each with reason, checks containing result and evidence_ids, missing_facts.\nINPUT_JSON\n'+json.dumps(data,ensure_ascii=False),1300,
     floor_response_schema(data['candidates']),review=bad['task']=='review_floor_decisions')
 result=dict(ok=True,metadata=response.metadata,seconds=time.perf_counter()-start,label=bad['label'],task=bad['task'])
except Exception as exc:
 message=str(exc).replace(analyzer.api_key,'[REDACTED]')
 message=re.sub(r'nvapi-[A-Za-z0-9_-]+','[REDACTED]',message)
 result=dict(ok=False,error_type=type(exc).__name__,safe_diagnostic=message[:500],seconds=time.perf_counter()-start,label=bad['label'],task=bad['task'])
print(json.dumps(result,ensure_ascii=True,indent=2))
(base/'saved_request_probe.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
