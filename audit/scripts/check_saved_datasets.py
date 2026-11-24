"""Read-only spatial/filter oracle against saved real-listing databases."""
import hashlib,json,logging,math,random,sqlite3,sys,time
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'audit/scripts'))
from real_estate_engine import PropertyStorage,haversine_distance
from real_estate_engine.storage import ClosingConnection
from expanded_offline_test import oracle_distances

class ReadOnlyStorage(PropertyStorage):
    def __init__(self,path):self.db_path=path.resolve().as_uri()+'?mode=ro'
    def _get_connection(self):
        conn=sqlite3.connect(self.db_path,uri=True,factory=ClosingConnection)
        conn.row_factory=sqlite3.Row;conn.create_function('HAVERSINE',4,haversine_distance)
        return conn

def main():
    out=Path((ROOT/'audit/big_run_2026-11-03/LATEST_OFFLINE.txt').read_text(encoding='utf-8'))
    rng=random.Random(2026110307);records=[]
    paths=[ROOT/'data/moscow_commercial.db',ROOT/'audit/ai_contract_fix_2026-11-03/fresh_sources.db']
    for path in paths:
        before=hashlib.sha256(path.read_bytes()).hexdigest();store=ReadOnlyStorage(path)
        with store._get_connection() as conn:
            rows=[dict(r) for r in conn.execute('SELECT * FROM properties')]
            exists=conn.execute("SELECT 1 FROM sqlite_master WHERE name='quarantined_properties'").fetchone()
            excluded={(r['source'],r['external_id']) for r in conn.execute('SELECT * FROM quarantined_properties')} if exists else set()
        rows=[r for r in rows if (r['source'],r['external_id']) not in excluded]
        coordinates=np.array([(r['lat'],r['lng']) for r in rows]);failures=[];elapsed=[]
        for index in range(2000):
            anchor=rng.choice(rows)
            lat,lng=anchor['lat']+rng.uniform(-.1,.1),anchor['lng']+rng.uniform(-.15,.15)
            radius=rng.uniform(25,30000)
            distances=oracle_distances(lat,lng,coordinates)
            expected={r['id'] for i,r in enumerate(rows) if distances[i]<=radius}
            started=time.perf_counter();found=store.find_within_radius_rtree(lat,lng,radius);elapsed.append((time.perf_counter()-started)*1000)
            actual={r['id'] for r in found}
            if actual!=expected:failures.append(dict(case=index,kind='spatial',missing=list(expected-actual),extra=list(actual-expected)))
            low,high,budget,rate=rng.uniform(5,200),rng.uniform(201,1000),rng.uniform(30000,3000000),rng.uniform(5000,100000)
            expected={r['dedup_hash'] for i,r in enumerate(rows) if distances[i]<=radius and low<=r['area_sqm']<=high and r['price_month']<=budget and r['price_sqm_year']<=rate}
            actual={r['dedup_hash'] for r in store.find_candidates(lat,lng,radius,min_area_sqm=low,max_area_sqm=high,max_price_month=budget,max_price_sqm_year=rate)}
            if actual!=expected:failures.append(dict(case=index,kind='filters',missing=len(expected-actual),extra=len(actual-expected)))
        record=dict(database=str(path.relative_to(ROOT)),rows=len(rows),spatial_cases=2000,filter_cases=2000,failures=failures,
            median_ms=float(np.median(elapsed)),p95_ms=float(np.percentile(elapsed,95)),unchanged=before==hashlib.sha256(path.read_bytes()).hexdigest())
        records.append(record);print(json.dumps({k:v if k!='failures' else len(v) for k,v in record.items()},ensure_ascii=False),flush=True)
    (out/'saved_data_checks.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    if any(r['failures'] or not r['unchanged'] for r in records):raise SystemExit(1)

if __name__=='__main__':sys.stdout.reconfigure(encoding='utf-8');logging.disable(logging.CRITICAL);main()
