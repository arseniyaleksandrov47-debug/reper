"""Check that archived cache entries cannot approve a lot under the current contract."""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from archived_floor import load_floor_profile
from real_estate_engine.floor_policy import BusinessFloorProfileResolver


class NoNetworkAnalyzer:
    floor_model = 'nvidia/nemotron-3.5-lightning-30b-a3b'
    floor_review_model = 'poolside/laguna-xs-2.1'
    def __init__(self): self.calls = 0
    def _call_floor_structured_sync(self,*args,**kwargs):
        self.calls += 1
        raise RuntimeError('Network forbidden in final cache validation')


async def main():
    base = ROOT/'audit/access_evidence_2026-11-04'
    out = Path((base/'LATEST.txt').read_text(encoding='utf-8'))
    profile = load_floor_profile(json.loads((out/'profile.json').read_text(encoding='utf-8')))
    rows = json.loads((out/'sample.json').read_text(encoding='utf-8'))
    validation = []
    for run in (1,2):
        analyzer = NoNetworkAnalyzer()
        resolver = BusinessFloorProfileResolver(analyzer, assessment_cache_path=out/f'cache_{run}.sqlite3')
        results = await resolver.assess_many(rows,profile)
        record = dict(run=run, attempted_inference=analyzer.calls,
            results={key:dict(status=value.status,cache=value.inference_metadata.get('assessment_cache'),
                analysis_status=value.analysis_status) for key,value in results.items()})
        record['passed'] = (analyzer.calls>0 and results['80'].status=='needs_verification' and
            not results['80'].inference_metadata.get('assessment_cache', {}).get('hit') and
            results['205'].analysis_status=='source_conflict')
        validation.append(record)
    print(json.dumps(dict(archive=str(out), checks=validation),ensure_ascii=False,indent=2))
    if not all(record['passed'] for record in validation):
        raise RuntimeError('Unexpected reuse of an archived assessment')


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    asyncio.run(main())
