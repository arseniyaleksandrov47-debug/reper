"""Check the configured NVIDIA connection without exposing credentials."""
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from real_estate_engine import NvidiaCompetitorAnalyzer

analyzer = NvidiaCompetitorAnalyzer()
records = []
post = analyzer.http_session.post


def observed_post(url, *args, **kwargs):
    started = time.perf_counter()
    record = {"model": kwargs.get("json", {}).get("model")}
    try:
        response = post(url, *args, **kwargs)
        record["status"] = response.status_code
        if response.status_code != 200:
            record["body_preview"] = response.text[:300].replace(analyzer.api_key, "[REDACTED]")
        return response
    except Exception as error:
        record["error_type"] = type(error).__name__
        raise
    finally:
        record["elapsed_s"] = round(time.perf_counter() - started, 2)
        records.append(record)


analyzer.http_session.post = observed_post
try:
    _, model = analyzer._call_nvidia_sync('Ответь JSON объектом {"ok": true}', 32)
    result = {"success": True, "model": model, "attempts": records}
except Exception as error:
    result = {"success": False, "error_type": type(error).__name__, "attempts": records}
(ROOT / "audit/large_test_2026-11-03/nvidia_unrestricted_probe.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
