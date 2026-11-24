"""Live semantic checks of floor suitability with no niche-to-floor policy."""
import asyncio
import hashlib
import json
import pathlib
import sqlite3
import sys
import time
from dataclasses import asdict

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from real_estate_engine import NvidiaCompetitorAnalyzer, PropertyStorage, haversine_distance
from real_estate_engine.floor_policy import BusinessFloorProfileResolver

OUT = ROOT / "audit/adaptive_floor_policy_2026-11-03"
OUT.mkdir(parents=True, exist_ok=True)


class ReadOnlyStorage(PropertyStorage):
    def __init__(self):
        self.db_path = "file:" + str(ROOT / "data/moscow_commercial.db").replace("\\", "/") + "?mode=ro"

    def _get_connection(self):
        c = sqlite3.connect(self.db_path, uri=True)
        c.row_factory = sqlite3.Row
        c.create_function("HAVERSINE", 4, haversine_distance)
        return c


async def main():
    original = ROOT / "data/moscow_commercial.db"
    before = hashlib.sha256(original.read_bytes()).hexdigest()
    resolver = BusinessFloorProfileResolver(NvidiaCompetitorAnalyzer())
    observations = []
    street = dict(id="street", floor=1, title="Помещение с входом с улицы", description=
        "Помещение на первом этаже. Отдельный вход прямо с пешеходной улицы, свободный доступ посетителей "
        "без пропусков. Витрина и вывеска на фасаде. Люди с улицы могут сразу подойти к торговой зоне.")
    office = dict(id="office7", floor=7, title="Изолированный офис", description=
        "Изолированный офис на седьмом этаже. Вход только для сотрудников. Посетители с улицы не допускаются. "
        "Пассажирский лифт. На этаже только закрытые офисные кабинеты, общедоступной торговой зоны нет.")
    internal = dict(id="internal7", floor=7, title="Зона действующей кофейни для сотрудников БЦ", description=
        "На 7 этаже бизнес-центра находится отдельная торговая зона действующей кофейни. В общем холле "
        "ежедневно работают и проходят 600 сотрудников этого этажа, все они имеют свободный доступ к кофейне. "
        "Есть вывеска и место выдачи заказов. Пассажирский лифт останавливается у холла. Внешние посетители не допускаются.")
    studio = dict(id="studio7", floor=7, title="Студия для записи аудиокниг", description=
        "Студия на 7 этаже. Клиенты приходят по предварительной записи, получают пропуск заранее. "
        "Посетителям по записи доступ разрешён. Пассажирский лифт до этажа. Внутри есть указатели к студии.")
    cases = [
        ("Кофейня", "Продажи прохожим с пешеходной улицы, клиент должен иметь возможность зайти спонтанно", [street, office],
         {"street": "eligible", "office7": "not_eligible"}),
        ("Кофейня для сотрудников бизнес-центра", "Продажи только сотрудникам, уже находящимся на этаже; уличная аудитория не нужна", [internal],
         {"internal7": "eligible"}),
        ("Студия озвучки аудиокниг по записи", "Клиенты приезжают целенаправленно по записи; важен путь до студии, случайный поток не нужен", [studio],
         {"studio7": "eligible"}),
    ]
    if "--studio-only" in sys.argv:
        cases = cases[-1:]
    for business, context, offers, expectations in cases:
        started = time.perf_counter()
        profile = await resolver.resolve(business, context)
        decisions = await resolver.assess_many(offers, profile)
        outcomes = {key: d.status for key, d in decisions.items()}
        checks = {key: (outcomes.get(key) != "eligible" if expected == "not_eligible" else outcomes.get(key) == expected)
                  for key, expected in expectations.items()}
        entry = {"business": business, "context": context, "kind": "controlled semantic example, not a market listing",
                 "profile": asdict(profile), "offers": offers, "assessments": {k: asdict(v) for k, v in decisions.items()},
                 "expectations_met": checks, "elapsed_s": round(time.perf_counter() - started, 2)}
        observations.append(entry)
        print(json.dumps({"business": business, "profile_status": profile.status, "outcomes": outcomes,
                          "expectations_met": checks, "elapsed_s": entry["elapsed_s"]}, ensure_ascii=False), flush=True)
        (OUT / "semantic_cases.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2), encoding="utf-8")
    offers = ReadOnlyStorage().find_candidates(55.7558, 37.6173, 5000, min_area_sqm=40, max_area_sqm=140, max_price_month=250000)
    started = time.perf_counter()
    profile = await resolver.resolve("Кофейня и пекарня")
    decisions = await resolver.assess_many(offers, profile)
    real = {"profile": asdict(profile), "offers": offers, "assessments": {k: asdict(v) for k, v in decisions.items()},
            "elapsed_s": round(time.perf_counter() - started, 2), "counts": {s: sum(d.status == s for d in decisions.values())
            for s in ("eligible", "rejected", "needs_verification")}}
    (OUT / "real_candidates.json").write_text(json.dumps(real, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"real_candidates": len(offers), "profile_status": profile.status, "counts": real["counts"], "elapsed_s": real["elapsed_s"]}, ensure_ascii=False), flush=True)
    started = time.perf_counter()
    repeated = await resolver.assess_many(offers, profile)
    cached_ms = (time.perf_counter() - started) * 1000
    summary = {"controlled_expectations_met": all(all(x["expectations_met"].values()) for x in observations),
        "real_counts": real["counts"], "real_cached_ms": round(cached_ms, 3),
        "cache_preserves_results": all(asdict(repeated[k]) == asdict(v) for k, v in decisions.items()),
        "working_database_unchanged": before == hashlib.sha256(original.read_bytes()).hexdigest()}
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
