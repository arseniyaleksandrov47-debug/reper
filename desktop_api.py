"""Local FastAPI bridge exposing real_estate_engine workflows for the desktop GUI."""
import asyncio
import dataclasses
from datetime import date, datetime
from enum import Enum
import json
import logging
import math
from pathlib import Path
import re
import sys
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from real_estate_engine import RealEstateAggregator
from real_estate_engine.business_model import concept_digest
from real_estate_engine.cities import get_city, runtime_root
from real_estate_engine.credentials import (
    load_dadata_key,
    load_dgis_maps_key,
    load_nvidia_key,
    load_yandex_maps_key,
    save_dadata_key,
    save_dgis_maps_key,
    save_nvidia_key,
    save_yandex_maps_key,
)
from real_estate_engine.floor_policy import extract_floor_evidence, parse_floor_values
from real_estate_engine.population import population_counts_available
from real_estate_engine.ranking_policy import METRICS
from real_estate_engine.recommender import LocationResolver, match_city_zone
from real_estate_engine.storage import PropertyStorage

logger = logging.getLogger("ReperDesktopAPI")

app = FastAPI(title="Reper Desktop API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

JOBS: Dict[str, Dict[str, Any]] = {}
JOB_TASKS: Dict[str, asyncio.Task] = {}
REPORT_OBJECTS: Dict[str, Any] = {}
JOB_LOGS_LIMIT = 150
_JOBS_LOCK = threading.Lock()


def _is_whole_city_query(query: Optional[str], city_obj: Any) -> bool:
    q = (query or "").strip().lower().replace("ё", "е")
    aliases = {
        "",
        "весь город",
        "любой",
        "все",
        "all",
        city_obj.name.casefold().replace("ё", "е"),
        *(str(a).casefold().replace("ё", "е") for a in getattr(city_obj, "aliases", ())),
        f"весь {city_obj.name.casefold()}".replace("ё", "е"),
        "вся москва",
        "весь санкт-петербург",
        "весь петербург",
        "весь питер",
        "весь спб",
    }
    return q in aliases


def frontend_dist_dir() -> Path:
    """Locate built frontend bundle in PyInstaller _MEIPASS or local workspace."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        bundled = Path(sys._MEIPASS) / "frontend_dist"
        if bundled.is_dir():
            return bundled
    return runtime_root() / "frontend" / "dist"


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return "***"
    return f"{key[:5]}...{key[-4:]}"


def _parse_photos(raw_photos: Any) -> List[str]:
    """Extract clean HTTP(S) photo URLs from stored JSON string or list."""
    parsed = raw_photos
    if isinstance(raw_photos, str):
        try:
            parsed = json.loads(raw_photos)
        except Exception:
            return []
    if not isinstance(parsed, list):
        return []
    urls: List[str] = []
    for item in parsed:
        if isinstance(item, str) and item.startswith(("http://", "https://")) and item not in urls:
            urls.append(item)
            if len(urls) >= 5:
                break
    return urls


def _serialize_property_summary(prop: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize common listing fields for map previews, candidates, and deferred lots."""
    price_month_val = prop.get("price_month")
    area_val = prop.get("area_sqm")
    price_sqm_year_val = prop.get("price_sqm_year")
    price_sqm_month = prop.get("price_sqm_month")
    if price_sqm_month is None:
        if (
            type(price_month_val) in (int, float)
            and type(area_val) in (int, float)
            and area_val > 0
            and math.isfinite(price_month_val)
            and math.isfinite(area_val)
        ):
            price_sqm_month = round(float(price_month_val) / float(area_val))
        elif type(price_sqm_year_val) in (int, float) and math.isfinite(price_sqm_year_val) and price_sqm_year_val > 0:
            price_sqm_month = round(float(price_sqm_year_val) / 12.0)

    return {
        "id": prop.get("id"),
        "source": prop.get("source"),
        "external_id": prop.get("external_id"),
        "title": prop.get("title"),
        "address": prop.get("address"),
        "lat": prop.get("lat"),
        "lng": prop.get("lng"),
        "area_sqm": area_val,
        "price_month": price_month_val,
        "price_sqm_year": price_sqm_year_val,
        "price_sqm_month": price_sqm_month,
        "last_seen_at": prop.get("last_seen_at"),
        "floor": prop.get("floor"),
        "url": prop.get("url"),
        "description": prop.get("description"),
        "has_separate_entrance": prop.get("has_separate_entrance"),
        "power_kw": prop.get("power_kw"),
        "photos": _parse_photos(prop.get("photos_json") or prop.get("photos")),
    }


def _clean_json(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if dataclasses.is_dataclass(value):
        return _clean_json(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _clean_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_clean_json(v) for v in value]
    if hasattr(value, "__dict__"):
        return _clean_json(vars(value))
    return str(value)


def _metric_label(metric: str) -> str:
    row = METRICS.get(metric)
    return row[0].split(";", 1)[0] if row else metric


def _metric_unit(unit: Optional[str]) -> str:
    return {
        "index": "индекс",
        "m": "м",
        "people": "чел.",
        "ratio": "на 1000 чел.",
        "rub/month": "₽/мес",
        "rub/sqm/year": "₽/м²/год",
        "sqm": "м²",
        "people/day": "чел./день",
        "orders/day": "заказов/день",
        "minutes": "мин",
    }.get(unit or "", unit or "")


def _infer_stage(message: str) -> Optional[str]:
    if "Starting multi-platform ingestion" in message or "MasterAggregator" in message:
        return "Сбор объявлений с площадок"
    if "FloorPolicy" in message or "assess_many" in message or "Ограниченная выборка" in message or "Фильтры пройдены" in message:
        return "Проверка обязательных требований допуска"
    if "Анализ окружения для" in message or "YandexMaps" in message or "CompetitorAnalyzer" in message:
        return "Анализ конкурентов и окружения"
    if "PopulationAnalyzer" in message or "Population" in message:
        return "Расчёт жилого фонда и населения"
    if "FnsEnricher" in message:
        return "Поиск юрлиц и отчётности ФНС"
    if "PropertyConfirmation" in message or "confirmation" in message.lower():
        return "Перепроверка актуальности карточек"
    return None


def _humanize_log_line(raw: str) -> Optional[str]:
    """Translate internal English engine log lines into clean Russian progress events."""
    import re as _re

    if "[DirectBridge]" in raw or "HTTP Request:" in raw:
        return None
    ts = ""
    m_ts = _re.match(r"^(\d{2}:\d{2}:\d{2})\s+\[[A-Z]+\]\s+[^:]+:\s*(.*)$", raw)
    if m_ts:
        ts, body = m_ts.group(1), m_ts.group(2).strip()
    else:
        body = raw.strip()

    m_ym = _re.search(r"\[YandexMaps\].*?\(R=(\d+)m\).*?found\s+(\d+)\s+unique", body)
    if m_ym:
        body = f"Найдено организаций в окружении (R={m_ym.group(1)} м): {m_ym.group(2)}"
    else:
        m_pop = _re.search(
            r"\[Population\].*?(\d+)\s+res buildings.*?~(\d+|None)\s+residents,\s*~(\d+|None)\s+office",
            body,
        )
        if m_pop:
            res_txt = m_pop.group(2) if m_pop.group(2) != "None" else "н/д"
            off_txt = m_pop.group(3) if m_pop.group(3) != "None" else "0"
            body = f"Жилой фонд рядом: {m_pop.group(1)} МКД, ~{res_txt} жителей, ~{off_txt} офисных мест"
        else:
            m_fns = _re.search(r"\[FnsBrowser\]\s+Enriched\s+(\d+)/(\d+)\s+business cards", body)
            if m_fns:
                body = f"Сопоставлено карточек ФНС: {m_fns.group(1)} из {m_fns.group(2)}"
            elif "[FloorPolicy]" in body:
                body = body.replace("[FloorPolicy]", "").strip()
            else:
                m_prop = _re.search(r"Допуск проверен:\s*eligible=(\d+),\s*verification=(\d+),\s*rejected=(\d+)(.*)", body)
                if m_prop:
                    body = (
                        f"Проверен допуск лотов: проходят — {m_prop.group(1)}, "
                        f"на уточнение — {m_prop.group(2)}, отклонено — {m_prop.group(3)}{m_prop.group(4)}"
                    )
                elif "collectors." in raw or "Page " in body:
                    return None
    return f"{ts} · {body}" if ts else body


class _JobLogHandler(logging.Handler):
    def __init__(self, job: Dict[str, Any]):
        super().__init__()
        self.job = job
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            raw_msg = self.format(record)
            stage = _infer_stage(raw_msg)
            msg = _humanize_log_line(raw_msg)
            with _JOBS_LOCK:
                if self.job.get("status") == "cancelled":
                    return
                if stage:
                    self.job["stage"] = stage
                if msg:
                    logs = self.job["logs"]
                    logs.append(msg)
                    if len(logs) > JOB_LOGS_LIMIT:
                        del logs[: len(logs) - JOB_LOGS_LIMIT]
        except Exception:
            pass


def _city_stats(city_key: str) -> Dict[str, Any]:
    city = get_city(city_key)
    db_path = city.database_path
    stats: Dict[str, Any] = {"total": 0, "unique_locations": 0, "by_source": {}}
    if db_path.is_file():
        storage = PropertyStorage(db_path=str(db_path))
        try:
            raw = storage.get_statistics()
            raw_sources = raw.get("by_source", {})
            by_source = {
                str(src): int(val["count"] if isinstance(val, dict) else (val or 0))
                for src, val in raw_sources.items()
            }
            stats = {
                "total": int(raw.get("total_offers") or sum(by_source.values())),
                "unique_locations": int(raw.get("unique_physical_spaces") or 0),
                "by_source": by_source,
            }
        except Exception:
            pass
    return {
        "key": city.key,
        "name": city.name,
        "center": list(city.center),
        "default_radius_m": city.search_radius_m,
        "municipal_source": city.municipal_source,
        "db_exists": db_path.is_file(),
        "stats": stats,
    }


def _serialize_competitor(comp: Any) -> Dict[str, Any]:
    c = comp.card
    return _clean_json(
        {
            "level": getattr(comp.level, "value", str(comp.level)),
            "reason": comp.reason,
            "org_id": c.org_id,
            "title": c.title,
            "categories": c.categories,
            "address": c.address,
            "lat": c.lat,
            "lng": c.lng,
            "distance_m": round(float(c.distance_m), 1),
            "rating": c.rating,
            "ratings_count": c.ratings_count,
            "reviews_count": c.reviews_count,
            "working_hours": c.working_hours,
            "is_chain": c.is_chain,
            "url": c.url,
            "websites": c.websites,
            "traffic_profile_str": c.traffic_profile_str,
            "fns_match_status": c.fns_match_status,
            "fns_inn": c.fns_inn,
            "fns_legal_name": c.fns_legal_name,
            "fns_okved": c.fns_okved,
            "fns_revenue_rub": c.fns_revenue_rub,
            "fns_net_profit_rub": c.fns_net_profit_rub,
            "fns_reporting_year": c.fns_reporting_year,
        }
    )


def _serialize_candidate(cand: Any, rank: int) -> Dict[str, Any]:
    prop = cand.property_data
    rep = cand.competition_report
    ft = getattr(rep, "foot_traffic", None) if rep else None
    pop = getattr(rep, "population", None) if rep else None

    scoring_rows = []
    for metric, part in (cand.scoring_components or {}).items():
        scoring_rows.append(
            {
                "metric": metric,
                "label": _metric_label(metric),
                "unit": _metric_unit(part.get("unit")),
                "weight": part.get("weight"),
                "value_lower": part.get("value_lower"),
                "value_upper": part.get("value_upper"),
                "contribution_lower": part.get("contribution_lower"),
                "contribution_upper": part.get("contribution_upper"),
            }
        )

    ft_data = None
    if ft is not None:
        hp = getattr(ft, "hourly_profile", None)
        ft_data = _clean_json(
            {
                "estimated_daily_pedestrians": ft.estimated_daily_pedestrians,
                "estimated_weekday_daily": ft.estimated_weekday_daily,
                "estimated_weekend_daily": ft.estimated_weekend_daily,
                "estimated_avg_hourly_daytime": ft.estimated_avg_hourly_daytime,
                "estimated_peak_hourly": ft.estimated_peak_hourly,
                "traffic_index": ft.traffic_index,
                "traffic_level": ft.traffic_level,
                "metro_contribution_daily": ft.metro_contribution_daily,
                "stops_contribution_daily": ft.stops_contribution_daily,
                "local_commercial_daily": ft.local_commercial_daily,
                "unique_stops_count": ft.unique_stops_count,
                "closest_stop_distance_m": ft.closest_stop_distance_m,
                "transport_data_status": ft.transport_data_status,
                "transport_lower_daily": ft.transport_lower_daily,
                "transport_upper_daily": ft.transport_upper_daily,
                "data_status": ft.data_status,
                "data_warnings": ft.data_warnings,
                "nearest_metros": ft.nearest_metros,
                "hourly_profile": {
                    "weekday_hourly_share": hp.weekday_hourly_share if hp else [],
                    "weekend_hourly_share": hp.weekend_hourly_share if hp else [],
                    "morning_share_pct": hp.morning_share_pct if hp else 0.0,
                    "lunch_share_pct": hp.lunch_share_pct if hp else 0.0,
                    "evening_share_pct": hp.evening_share_pct if hp else 0.0,
                    "night_share_pct": hp.night_share_pct if hp else 0.0,
                    "peak_weekday_hour": hp.peak_weekday_hour if hp else 13,
                    "peak_weekend_hour": hp.peak_weekend_hour if hp else 15,
                    "weekend_to_weekday_ratio": hp.weekend_to_weekday_ratio if hp else 1.0,
                    "location_rhythm_type": hp.location_rhythm_type if hp else "",
                    "cards_with_histograms": hp.cards_with_histograms if hp else 0,
                },
            }
        )

    pop_data = None
    if pop is not None:
        pop_data = _clean_json(
            {
                "available": population_counts_available(pop),
                "district_name": pop.district_name,
                "radius_m": pop.radius_m,
                "residential_buildings_count": pop.residential_buildings_count,
                "exact_passport_buildings_count": pop.exact_passport_buildings_count,
                "total_apartments_count": pop.total_apartments_count,
                "total_entrances_count": pop.total_entrances_count,
                "estimated_residents_count": pop.estimated_residents_count,
                "estimated_residents_range": pop.estimated_residents_range,
                "estimated_office_workers_count": pop.estimated_office_workers_count,
                "office_buildings_count": pop.office_buildings_count,
                "avg_building_floors": pop.avg_building_floors,
                "avg_construction_year": pop.avg_construction_year,
                "predominant_wall_material": pop.predominant_wall_material,
                "median_housing_price_sqm_rub": pop.median_housing_price_sqm_rub,
                "median_flat_price_mln_rub": pop.median_flat_price_mln_rub,
                "data_status": pop.data_status,
                "data_warnings": pop.data_warnings,
                "population_model": {
                    "usable": (pop.population_model or {}).get("usable", False),
                    "territories": (pop.population_model or {}).get("territories", []),
                },
                "top_residential_buildings": pop.top_residential_buildings,
            }
        )

    comp_data = None
    if rep is not None:
        comp_data = _clean_json(
            {
                "total_cards_found": rep.total_cards_found,
                "fns_enriched_count": rep.fns_enriched_count,
                "model_used": rep.model_used,
                "ai_summary": rep.ai_summary,
                "map_fetch_time_ms": rep.map_fetch_time_ms,
                "ai_inference_time_ms": rep.ai_inference_time_ms,
                "fns_fetch_time_ms": rep.fns_fetch_time_ms,
                "data_status": rep.data_status,
                "collection_status": getattr(rep, "collection_status", "unknown"),
                "data_warnings": rep.data_warnings,
                "foot_traffic": ft_data,
                "population": pop_data,
                "direct_competitors": [_serialize_competitor(x) for x in rep.direct_competitors],
                "indirect_competitors": [_serialize_competitor(x) for x in rep.indirect_competitors[:12]],
                "synergy_businesses": [_serialize_competitor(x) for x in rep.synergy_businesses[:12]],
            }
        )

    return _clean_json(
        {
            "rank": rank,
            "property_data": _serialize_property_summary(prop),
            "total_score": cand.total_score,
            "score_upper_bound": cand.score_upper_bound,
            "score_coverage": cand.score_coverage,
            "input_score_lower_bound": cand.input_score_lower_bound,
            "input_score_upper_bound": cand.input_score_upper_bound,
            "price_sqm_month": cand.price_sqm_month,
            "verified_fns_revenue_mln_sum": cand.verified_fns_revenue_mln_sum,
            "direct_competitors_avg_rating": cand.direct_competitors_avg_rating,
            "competitor_comparison_summary": cand.competitor_comparison_summary,
            "pros": cand.pros,
            "cons": cand.cons,
            "verdict": cand.verdict,
            "data_warnings": cand.data_warnings,
            "floor_assessment": cand.floor_assessment,
            "confirmation": cand.confirmation,
            "scoring_components": scoring_rows,
            "business_model_diagnostics": cand.business_model_diagnostics,
            "competition_report": comp_data,
        }
    )


def serialize_report(report: Any, job_id: Optional[str] = None) -> Dict[str, Any]:
    rp = report.ranking_profile
    profile_data = None
    if rp is not None:
        cov_map = {a["concept_quote"]: a for a in getattr(report, "requirement_coverage", [])}
        criteria_rows = []
        for c in rp.criteria:
            scale = rp.metadata.get("reference_scales", {}).get(c.metric, {})
            criteria_rows.append(
                {
                    "metric": c.metric,
                    "label": _metric_label(c.metric),
                    "weight": c.weight,
                    "preference": c.preference,
                    "low": c.low,
                    "high": c.high,
                    "concept_quote": c.concept_quote,
                    "reason": c.reason,
                    "assumption": c.assumption,
                    "scale_source": scale.get("source", "caller_profile"),
                    "usable": scale.get("usable", True),
                }
            )
        profile_data = _clean_json(
            {
                "status": rp.status,
                "label": getattr(rp, "summary", None) or getattr(rp, "label", ""),
                "criteria": criteria_rows,
                "assumptions": rp.assumptions,
                "questions": rp.questions,
                "unmodeled_requirements": [
                    {"quote": q, "status": cov_map.get(q, {}).get("status", "unmodeled")}
                    for q in rp.unmodeled_requirements
                ],
            }
        )

    bounds_by_id = {b["property_id"]: b for b in getattr(report, "deferred_candidate_bounds", [])}
    deferred = []
    for item in report.floor_verification_candidates:
        p = item.property_data
        deferred.append(
            _clean_json(
                {
                    **_serialize_property_summary(p),
                    "reason": item.assessment.reason,
                    "missing_facts": item.assessment.missing_facts,
                    "bound": bounds_by_id.get(p.get("id")),
                }
            )
        )

    rejected = []
    for item in report.floor_rejected_candidates:
        p = item.property_data
        rejected.append(
            _clean_json(
                {
                    **_serialize_property_summary(p),
                    "reason": item.assessment.reason,
                }
            )
        )
    for item in getattr(report, "confirmation_rejected_candidates", []):
        p = item.get("property_data") or {}
        conf = item.get("confirmation") or {}
        msg = "; ".join(r.get("message", "") for r in conf.get("reasons", []) if r.get("message"))
        rejected.append(
            _clean_json(
                {
                    **_serialize_property_summary(p),
                    "reason": msg or conf.get("label", "Не соответствует актуальным условиям"),
                }
            )
        )

    return _clean_json(
        {
            "job_id": job_id,
            "city": report.city,
            "target_business": report.target_business,
            "location_query": report.location_query,
            "resolved_location_name": report.resolved_location_name,
            "center_lat": report.center_lat,
            "center_lng": report.center_lng,
            "search_radius_m": report.search_radius_m,
            "competition_diameter_m": report.competition_diameter_m,
            "total_matching_in_db": report.total_matching_in_db,
            "numerical_matching_count": report.numerical_matching_count,
            "evaluated_count": report.evaluated_count,
            "total_search_time_ms": report.total_search_time_ms,
            "selection_status": report.selection_status,
            "confirmation_status": report.confirmation_status,
            "confirmation_message": report.confirmation_message,
            "comparison_complete": report.comparison_complete,
            "data_complete": report.data_complete,
            "include_unverified": report.include_unverified,
            "unverified_evaluated_count": report.unverified_evaluated_count,
            "top_score_gap": report.top_score_gap,
            "stage_timings_ms": report.stage_timings_ms,
            "ranking_warnings": report.ranking_warnings,
            "missing_inputs": report.missing_inputs,
            "analysis_errors": report.analysis_errors,
            "recommendation": (
                str(report.recommendation.get("message") or "")
                if isinstance(report.recommendation, dict)
                else str(report.recommendation or "")
            ),
            "recommendation_details": (
                report.recommendation if isinstance(report.recommendation, dict) else None
            ),
            "next_actions": report.next_actions,
            "ranking_profile": profile_data,
            "requirement_coverage": getattr(report, "requirement_coverage", []),
            "floor_profile": report.floor_profile,
            "ranked_candidates": [
                _serialize_candidate(cand, i) for i, cand in enumerate(report.ranked_candidates, 1)
            ],
            "floor_verification_candidates": deferred,
            "floor_rejected_candidates": rejected,
        }
    )


class SearchRequest(BaseModel):
    city: str = "saint_petersburg"
    target_business: str = "Пекарня и кондитерская"
    location_query: Optional[str] = "метро Василеостровская"
    center_lat: Optional[float] = None
    center_lng: Optional[float] = None
    search_radius_m: Optional[float] = None
    min_area_sqm: Optional[float] = 40.0
    max_area_sqm: Optional[float] = 75.0
    target_area_sqm: Optional[float] = None
    max_price_month: Optional[float] = 180000.0
    max_price_sqm: Optional[float] = None
    price_sqm_period: str = "month"
    full_cost_limit: Optional[float] = 240000.0
    allowed_floors: Optional[List[int]] = Field(default_factory=lambda: [1])
    business_concept: str = (
        "Ремесленная пекарня-кондитерская районного формата с продажей свежего хлеба, "
        "выпечки и кофе с собой: основная аудитория — жители ближайших кварталов и пешеходы "
        "на маршруте к метро; обязателен 1-й этаж, удобный уличный вход для покупателей без "
        "пропускной системы, площадь 40-75 м², аренда до 180 000 ₽/мес"
    )
    max_candidates: int = 5
    analysis_limit: Optional[int] = None
    include_unverified: bool = False
    skip_collect: bool = True
    exclude_property_keys: List[str] = Field(default_factory=list)


class SecretsRequest(BaseModel):
    nvidia_key: Optional[str] = None
    dadata_key: Optional[str] = None
    yandex_maps_key: Optional[str] = None
    dgis_maps_key: Optional[str] = None


class CollectRequest(BaseModel):
    city: str = "saint_petersburg"
    limit_per_source: int = 120


class ContinuationRequest(BaseModel):
    action: str  # continue_unverified | find_more | supply_references
    extra_excludes: List[str] = Field(default_factory=list)
    full_cost_limit: Optional[float] = None


def _safe_load_key(loader) -> str:
    try:
        return loader()
    except Exception:
        return ""


@app.get("/api/health")
def health():
    nvidia = _safe_load_key(load_nvidia_key)
    dadata = _safe_load_key(load_dadata_key)
    yandex_maps = _safe_load_key(load_yandex_maps_key)
    dgis_maps = _safe_load_key(load_dgis_maps_key)
    return {
        "status": "ok",
        "runtime_root": str(runtime_root()),
        "cities": [_city_stats(k) for k in ("saint_petersburg", "moscow")],
        "secrets": {
            "nvidia_configured": bool(nvidia),
            "nvidia_masked": _mask_key(nvidia),
            "dadata_configured": bool(dadata),
            "dadata_masked": _mask_key(dadata),
            "yandex_maps_configured": bool(yandex_maps),
            "yandex_maps_masked": _mask_key(yandex_maps),
            "dgis_maps_configured": bool(dgis_maps),
            "dgis_maps_masked": _mask_key(dgis_maps),
        },
    }


@app.get("/api/map-keys")
def get_map_keys(request: Request):
    """Expose client-side map SDK keys strictly to loopback desktop webview callers."""
    host = request.client.host if request.client else ""
    if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(status_code=403, detail="Map keys are only accessible locally")
    return {
        "yandex_maps_key": _safe_load_key(load_yandex_maps_key),
        "dgis_maps_key": _safe_load_key(load_dgis_maps_key),
    }


@app.post("/api/secrets")
def update_secrets(req: SecretsRequest):
    updates = [
        (req.nvidia_key, save_nvidia_key),
        (req.dadata_key, save_dadata_key),
        (req.yandex_maps_key, save_yandex_maps_key),
        (req.dgis_maps_key, save_dgis_maps_key),
    ]
    for raw, saver in updates:
        if raw and raw.strip():
            try:
                saver(raw.strip())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
    return health()["secrets"]


_PREVIEW_GEO_CACHE: Dict[str, Optional[Tuple[float, float]]] = {}


def _spatial_spread_sample(
    rows: List[Dict[str, Any]],
    cap: int,
    *,
    center_lat: Optional[float] = None,
    center_lng: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Select an evenly distributed spatial sample, spiraling outward from center_lat/lng."""
    if cap <= 0 or len(rows) <= cap:
        return rows[: max(0, cap)]

    with_coords: List[Dict[str, Any]] = []
    without_coords: List[Dict[str, Any]] = []
    for row in rows:
        lat = row.get("lat")
        lng = row.get("lng")
        if (
            lat is not None
            and lng is not None
            and math.isfinite(float(lat))
            and math.isfinite(float(lng))
        ):
            with_coords.append(row)
        else:
            without_coords.append(row)

    if len(with_coords) <= cap:
        return (with_coords + without_coords)[:cap]

    lats = [float(r["lat"]) for r in with_coords]
    lngs = [float(r["lng"]) for r in with_coords]
    min_lat, max_lat = min(lats), max(lats)
    min_lng, max_lng = min(lngs), max(lngs)
    lat_span = max(1e-6, max_lat - min_lat)
    lng_span = max(1e-6, max_lng - min_lng)

    grid_n = 24
    buckets: Dict[Tuple[int, int], List[Dict[str, Any]]] = {}
    for row in with_coords:
        gy = min(grid_n - 1, int(((float(row["lat"]) - min_lat) / lat_span) * grid_n))
        gx = min(grid_n - 1, int(((float(row["lng"]) - min_lng) / lng_span) * grid_n))
        buckets.setdefault((gy, gx), []).append(row)

    if (
        center_lat is not None
        and center_lng is not None
        and math.isfinite(float(center_lat))
        and math.isfinite(float(center_lng))
    ):
        cy = ((float(center_lat) - min_lat) / lat_span) * grid_n
        cx = ((float(center_lng) - min_lng) / lng_span) * grid_n
        ordered_keys = sorted(
            buckets.keys(),
            key=lambda key: (((key[0] + 0.5) - cy) ** 2 + ((key[1] + 0.5) - cx) ** 2, key),
        )
    else:
        ordered_keys = sorted(buckets.keys())
    sampled: List[Dict[str, Any]] = []
    pass_idx = 0
    while len(sampled) < cap:
        added_in_pass = False
        for key in ordered_keys:
            cell = buckets[key]
            if pass_idx < len(cell):
                sampled.append(cell[pass_idx])
                added_in_pass = True
                if len(sampled) >= cap:
                    break
        if not added_in_pass:
            break
        pass_idx += 1
    return sampled


def _query_tokens(raw: str) -> List[str]:
    cleaned = raw.strip().lower().replace("ё", "е")
    for stop in (
        "район",
        "округ",
        "метро",
        "около",
        "возле",
        "рядом",
        "улица",
        "проспект",
        "бульвар",
        "переулок",
        "набережная",
        "ул",
        "пр-т",
        "пр",
        "пер",
        "наб",
        "р-н",
        "м",
        "у",
    ):
        cleaned = re.sub(rf"(?<!\w){re.escape(stop)}\.?(?!\w)", " ", cleaned)
    tokens: List[str] = []
    for w in re.split(r"[\s,.;:()\"«»-]+", cleaned):
        if len(w) < 3:
            continue
        stem = w[:-2] if len(w) >= 7 else (w[:-1] if len(w) >= 5 else w)
        tokens.append(stem)
    return tokens


@app.get("/api/cities/{city_key}/listings")
def city_listings_preview(
    city_key: str,
    limit: int = 600,
    min_area: Optional[float] = None,
    max_area: Optional[float] = None,
    max_price: Optional[float] = None,
    floors: Optional[str] = None,
    include_unverified: bool = True,
    location_query: Optional[str] = None,
    center_lat: Optional[float] = None,
    center_lng: Optional[float] = None,
    radius_m: Optional[float] = None,
    q: Optional[str] = None,
):
    city = get_city(city_key)
    if not city.database_path.is_file():
        return {"city": city.key, "total_matched": 0, "items": []}
    storage = PropertyStorage(db_path=str(city.database_path))

    has_pin = center_lat is not None and center_lng is not None
    loc_text = (location_query or "").strip()
    is_whole_city = not loc_text or _is_whole_city_query(loc_text, city)

    t_lat, t_lng = city.center
    eff_radius = city.search_radius_m
    zone_keywords: List[str] = []

    if has_pin:
        t_lat, t_lng = float(center_lat), float(center_lng)
        eff_radius = float(radius_m or 1200.0)
    elif not is_whole_city:
        zone_hit = match_city_zone(loc_text, city)
        if zone_hit is not None:
            z_lat, z_lng, z_rad, _zname, kw_list = zone_hit
            t_lat, t_lng = z_lat, z_lng
            eff_radius = float(radius_m or z_rad)
            zone_keywords = list(kw_list)

    rows = storage.find_candidates(
        target_lat=t_lat,
        target_lng=t_lng,
        radius_meters=eff_radius,
        min_area_sqm=min_area,
        max_area_sqm=max_area,
        max_price_month=max_price,
    )

    # Narrow by text zone when user entered a street, district, or metro query (without coordinates)
    if not has_pin and not is_whole_city and not zone_keywords:
        tokens = _query_tokens(loc_text)
        matched = (
            [
                r
                for r in rows
                if any(
                    tok in f"{r.get('address', '')} {r.get('title', '')}".lower().replace("ё", "е")
                    for tok in tokens
                )
            ]
            if tokens
            else []
        )
        if matched:
            rows = matched
        else:
            cache_key = f"{city.key}:{loc_text.lower()}"
            if cache_key not in _PREVIEW_GEO_CACHE:
                try:
                    geo = LocationResolver._geocode_nominatim_sync(loc_text, city=city)
                except Exception:
                    geo = None
                _PREVIEW_GEO_CACHE[cache_key] = (geo[0], geo[1]) if geo else None
                if len(_PREVIEW_GEO_CACHE) > 64:
                    _PREVIEW_GEO_CACHE.pop(next(iter(_PREVIEW_GEO_CACHE)))
            coords = _PREVIEW_GEO_CACHE.get(cache_key)
            if coords:
                rows = storage.find_candidates(
                    target_lat=coords[0],
                    target_lng=coords[1],
                    radius_meters=float(radius_m or 2000.0),
                    min_area_sqm=min_area,
                    max_area_sqm=max_area,
                    max_price_month=max_price,
                )
            else:
                rows = []

    if floors is not None and floors.strip():
        allowed_list = parse_floor_values([x.strip() for x in floors.split(",") if x.strip()])
        if allowed_list:
            allowed_set = set(allowed_list)
            floor_filtered: List[Dict[str, Any]] = []
            for r in rows:
                ev = extract_floor_evidence(r)
                if ev.floors is not None:
                    if any(f in allowed_set for f in ev.floors):
                        floor_filtered.append(r)
                elif include_unverified:
                    floor_filtered.append(r)
            rows = floor_filtered

    if q:
        needle = q.strip().lower().replace("ё", "е")
        rows = [
            r
            for r in rows
            if needle in str(r.get("address", "")).lower().replace("ё", "е")
            or needle in str(r.get("title", "")).lower().replace("ё", "е")
        ]

    total_matched = len(rows)
    cap = max(1, min(limit, 1000))
    sampled = _spatial_spread_sample(rows, cap, center_lat=t_lat, center_lng=t_lng)
    compact = [_serialize_property_summary(r) for r in sampled]
    return {"city": city.key, "total_matched": total_matched, "items": _clean_json(compact)}


def _attach_handler(job: Dict[str, Any]) -> _JobLogHandler:
    handler = _JobLogHandler(job)
    logging.getLogger().addHandler(handler)
    return handler


@app.post("/api/collect")
async def start_collect(req: CollectRequest):
    city = get_city(req.city)
    job_id = str(uuid.uuid4())[:10]
    job = {
        "job_id": job_id,
        "type": "collect",
        "city": city.key,
        "status": "running",
        "stage": f"Сбор объявлений ({city.name})",
        "logs": [],
        "result": None,
        "error": None,
    }
    JOBS[job_id] = job

    async def runner():
        handler = _attach_handler(job)
        try:
            agg = RealEstateAggregator(city=city.key)
            res = await agg.run_pipeline(limit_per_source=max(10, min(req.limit_per_source, 480)))
            job["result"] = _clean_json(res)
            job["stage"] = "Сбор завершён"
            job["status"] = "completed"
        except Exception as exc:
            logger.exception("Collection job failed")
            job["status"] = "failed"
            job["error"] = str(exc)
        finally:
            logging.getLogger().removeHandler(handler)

    asyncio.create_task(runner())
    return {"job_id": job_id}


def _build_business_data(req: SearchRequest) -> Optional[Dict[str, Any]]:
    if req.full_cost_limit is None:
        return None
    sha = concept_digest(req.business_concept)
    return {
        "properties": {},
        "ranking_scales": {
            "concept_sha256": sha,
            "metrics": {
                "premises_cost_month": {
                    "low": 0.0,
                    "high": float(req.full_cost_limit),
                    "source": "Desktop --full-cost-limit",
                }
            },
        },
    }


async def _execute_search_job(job: Dict[str, Any], req: SearchRequest):
    handler = _attach_handler(job)
    try:
        city = get_city(req.city)
        agg = RealEstateAggregator(city=city.key)
        if not req.skip_collect:
            job["stage"] = f"Сбор свежих объявлений ({city.name})"
            await agg.run_pipeline(limit_per_source=120)

        target_area = req.target_area_sqm
        if target_area is None and req.min_area_sqm is not None and req.max_area_sqm is not None:
            target_area = round((req.min_area_sqm + req.max_area_sqm) / 2.0, 1)

        floors = tuple(req.allowed_floors) if req.allowed_floors else None
        if floors:
            parsed = parse_floor_values([str(f) for f in floors])
            floors = tuple(parsed) if parsed else floors

        effective_radius_m = req.search_radius_m
        if req.center_lat is None and _is_whole_city_query(req.location_query, city):
            effective_radius_m = None

        job["stage"] = "Анализ концепции и фильтрация"
        report = await agg.find_optimal_properties(
            target_business=req.target_business,
            location_query=req.location_query or city.name,
            center_lat=req.center_lat,
            center_lng=req.center_lng,
            search_radius_m=effective_radius_m,
            target_area_sqm=target_area,
            min_area_sqm=req.min_area_sqm,
            max_area_sqm=req.max_area_sqm,
            max_price_month=req.max_price_month,
            max_price_sqm=req.max_price_sqm,
            price_sqm_period=req.price_sqm_period,
            max_candidates=max(1, min(req.max_candidates, 15)),
            allowed_floors=floors,
            analysis_limit=req.analysis_limit,
            business_concept=req.business_concept,
            business_data=_build_business_data(req),
            exclude_property_keys=req.exclude_property_keys or None,
            include_unverified=req.include_unverified,
            city=city.key,
        )
        with _JOBS_LOCK:
            if job.get("status") == "cancelled":
                return
            REPORT_OBJECTS[job["job_id"]] = {"report": report, "request": req}
            job["result"] = serialize_report(report, job_id=job["job_id"])
            job["stage"] = "Подбор завершён"
            job["status"] = "completed"
    except asyncio.CancelledError:
        with _JOBS_LOCK:
            job["status"] = "cancelled"
            job["stage"] = "Подбор остановлен"
            job["error"] = None
            if not job["logs"] or "Расчёт остановлен" not in job["logs"][-1]:
                job["logs"].append("Расчёт остановлен пользователем.")
    except Exception as exc:
        logger.exception("Search job failed")
        job["status"] = "failed"
        job["error"] = str(exc)
    finally:
        JOB_TASKS.pop(job["job_id"], None)
        logging.getLogger().removeHandler(handler)


@app.post("/api/search")
async def start_search(req: SearchRequest):
    job_id = str(uuid.uuid4())[:10]
    job = {
        "job_id": job_id,
        "type": "search",
        "city": get_city(req.city).key,
        "status": "running",
        "stage": "Инициализация поиска",
        "logs": [],
        "result": None,
        "error": None,
    }
    JOBS[job_id] = job
    JOB_TASKS[job_id] = asyncio.create_task(_execute_search_job(job, req))
    return {"job_id": job_id}


@app.post("/api/jobs/{job_id}/continue")
async def continue_job(job_id: str, body: ContinuationRequest):
    saved = REPORT_OBJECTS.get(job_id)
    if not saved:
        raise HTTPException(status_code=404, detail="Исходный отчёт не найден в памяти сессии")
    prev_report = saved["report"]
    prev_req: SearchRequest = saved["request"]

    new_id = str(uuid.uuid4())[:10]
    job = {
        "job_id": new_id,
        "type": "search",
        "city": prev_report.city,
        "status": "running",
        "stage": "Пересчёт выборки",
        "logs": [],
        "result": None,
        "error": None,
    }
    JOBS[new_id] = job

    async def runner():
        handler = _attach_handler(job)
        try:
            agg = RealEstateAggregator(city=prev_report.city)
            bdata = _build_business_data(prev_req)
            if body.action == "continue_unverified":
                report = await agg.continue_unverified_analysis(prev_report, business_data=bdata)
                req_copy = prev_req.model_copy(update={"include_unverified": True})
            elif body.action == "supply_references" and body.full_cost_limit is not None:
                req_copy = prev_req.model_copy(update={"full_cost_limit": body.full_cost_limit})
                bdata_next = _build_business_data(req_copy)
                assert bdata_next is not None
                report = await agg.supply_ranking_references(
                    prev_report,
                    bdata_next["ranking_scales"],
                    business_data=bdata_next,
                )
            elif body.action == "find_more":
                report = await agg.find_more_options(prev_report, business_data=bdata)
                req_copy = prev_req
            else:
                raise ValueError(f"Неизвестное действие продолжения: {body.action}")

            with _JOBS_LOCK:
                if job.get("status") == "cancelled":
                    return
                REPORT_OBJECTS[new_id] = {"report": report, "request": req_copy}
                job["result"] = serialize_report(report, job_id=new_id)
                job["stage"] = "Готово"
                job["status"] = "completed"
        except asyncio.CancelledError:
            with _JOBS_LOCK:
                job["status"] = "cancelled"
                job["stage"] = "Подбор остановлен"
                job["error"] = None
                job["logs"].append("Расчёт остановлен пользователем.")
        except Exception as exc:
            logger.exception("Continuation job failed")
            job["status"] = "failed"
            job["error"] = str(exc)
        finally:
            JOB_TASKS.pop(new_id, None)
            logging.getLogger().removeHandler(handler)

    if body.action == "find_more" and body.extra_excludes:
        merged = list(dict.fromkeys([*prev_req.exclude_property_keys, *body.extra_excludes]))
        req_copy = prev_req.model_copy(update={"exclude_property_keys": merged, "skip_collect": True})
        JOB_TASKS[new_id] = asyncio.create_task(_execute_search_job(job, req_copy))
    else:
        JOB_TASKS[new_id] = asyncio.create_task(runner())
    return {"job_id": new_id}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str):
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Задача не найдена")
        if job.get("status") != "running":
            return {"job_id": job_id, "status": job.get("status")}
        job["status"] = "cancelled"
        job["stage"] = "Подбор остановлен"
        job["error"] = None
        job["logs"].append("Расчёт остановлен пользователем.")
    task = JOB_TASKS.get(job_id)
    if task is not None and not task.done():
        task.cancel()
    return {"job_id": job_id, "status": "cancelled"}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with _JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Задача не найдена")
        return {**job, "logs": list(job.get("logs") or [])}


@app.get("/api/jobs/{job_id}/export")
def export_report(job_id: str, format: str = "markdown"):
    job = JOBS.get(job_id)
    if not job or not job.get("result"):
        raise HTTPException(status_code=404, detail="Результаты подбора ещё не готовы")
    res = job["result"]
    if format == "json":
        return JSONResponse(content=res)

    lines = [
        f"# Отчёт Reper: {res.get('target_business')} ({res.get('resolved_location_name')})",
        f"- Город: `{res.get('city')}` | Статус отбора: `{res.get('selection_status')}`",
        f"- Прошло числовые фильтры: {res.get('numerical_matching_count')} | Сравнено: {res.get('evaluated_count')}",
        "",
        "## ТОП кандидатов",
    ]
    for c in res.get("ranked_candidates", []):
        p = c["property_data"]
        lines.append(
            f"### #{c['rank']}. {p.get('title')} — {p.get('address')}\n"
            f"- Оценка: **{c['total_score']:.1f}–{c['score_upper_bound']:.1f} / 100** (покрытие {c['score_coverage']*100:.0f}%)\n"
            f"- Площадь: {p.get('area_sqm')} м² | Аренда: {p.get('price_month')} ₽/мес | Этаж: {p.get('floor')}\n"
            f"- Статус: {(c.get('confirmation') or {}).get('label', 'Требует подтверждения')}\n"
            f"- Ссылка: {p.get('url') or 'н/д'}\n"
        )
    return PlainTextResponse("\n".join(lines), media_type="text/markdown; charset=utf-8")


_dist = frontend_dist_dir()
if _dist.is_dir():
    assets_dir = _dist / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API endpoint not found")
        candidate = (_dist / full_path).resolve()
        if full_path and candidate.is_file() and _dist.resolve() in candidate.parents:
            return FileResponse(str(candidate))
        return FileResponse(
            str(_dist / "index.html"),
            headers={"Cache-Control": "no-store"},
        )
