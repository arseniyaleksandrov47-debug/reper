import argparse
import asyncio
import json
import math
import os
import re
import sys
from typing import Optional, Tuple

# Ensure UTF-8 output on Windows console
sys.stdout.reconfigure(encoding='utf-8')

from real_estate_engine import RealEstateAggregator, OptimalPropertyRecommender
from real_estate_engine.floor_policy import parse_floor_values
from real_estate_engine.population import population_counts_available
from real_estate_engine.cities import get_city
from real_estate_engine.ranking_policy import METRICS
from real_estate_engine.business_model import concept_digest, validate_business_inputs
from real_estate_engine.recommender import require_business_concept


def scoring_label(metric):
    return METRICS[metric][0].split(';',1)[0]


def count_label(value):
    return f'~{value:,}' if value is not None else 'не определено'


def resident_label(population):
    bounds=getattr(population,'estimated_residents_range',None)
    model=getattr(population,'population_model',{}) or {}
    if bounds is not None and model.get('usable'):
        import math
        dates=sorted({t['as_of'] for t in model.get('territories',[])})
        return f'{math.floor(bounds[0]):,}–{math.ceil(bounds[1]):,} (модель; статистика '+', '.join(dates)+')'
    return count_label(population.estimated_residents_count)


def scoring_unit(unit):
    return {'index':'индекс','m':'м','people':'чел.','ratio':'на 1000 чел.',
            'rub/month':'₽/мес','rub/sqm/year':'₽/м²/год','sqm':'м²','people/day':'чел./день','orders/day':'заказов/день','minutes':'мин'}.get(unit,unit)


def estimated_count(value):
    return 'н/д' if value is None else f'~{value:,.0f}'


def transport_bounds_text(foot_traffic):
    """Format route- or source-conditioned daily transport range."""
    if foot_traffic is None:
        return None
    low = getattr(foot_traffic, 'transport_lower_daily', None)
    if low is None:
        return None
    high = getattr(foot_traffic, 'transport_upper_daily', None)
    return f'{low:,.0f}–{high:,.0f}' if high is not None else f'от {low:,.0f}'


def foot_traffic_daily_label(foot_traffic):
    """Return pedestrian stream label or fallback transport-range label for tables."""
    if foot_traffic is None:
        return 'н/д'
    if foot_traffic.estimated_daily_pedestrians is not None:
        return estimated_count(foot_traffic.estimated_daily_pedestrians)
    bounds = transport_bounds_text(foot_traffic)
    return f'{bounds} (тр.)' if bounds is not None else 'н/д'


def metro_flow_label(station):
    count=station.station_daily_passengers
    bounds=getattr(station,'station_daily_passengers_range',None)
    distance=(f'{station.distance_m:.0f} м до нанесённого входа по прямой' if getattr(station,'location_basis','')=='mapped_entrance'
              else f'{station.distance_m:.0f} м по прямой')
    if count is None and bounds is not None:
        if getattr(station,'passenger_measure','')=='passengers_per_day_unspecified':
            return f'м. «{station.name}» ({distance}; диапазон справочного потока {bounds[0]:,.0f}–{bounds[1]:,.0f}/сут; данные от {station.passenger_as_of}; разделение входа/выхода не опубликовано)'
        return f'м. «{station.name}» ({distance}; справочный поток одной линии {bounds[0]:,.0f}–{bounds[1]:,.0f}/сут; данные от {station.passenger_as_of}; линия не установлена)'
    if count is None:return f'м. «{station.name}» ({distance}; пассажиропоток неизвестен)'
    date_label='данные от '+station.passenger_as_of if getattr(station,'passenger_as_of','') else 'дата не подтверждена'
    return (f'м. «{station.name}» ({distance}; справочный поток {count:,}/сут, '
            f'{date_label}; прокси коридора {estimated_count(station.estimated_corridor_daily)}/сут)')


def parse_allowed_floors_input(value: str) -> Tuple[int, ...]:
    pieces = value.split(",")
    if any(not parse_floor_values(p.strip()) for p in pieces):
        raise argparse.ArgumentTypeError("Укажите этажи через запятую: 1,2 или цоколь,-1")
    return parse_floor_values(pieces)


def parse_area_input(area_str: Optional[str]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Разбирает ввод площади от клиента:
    - '50' -> target_area=50.0, min_area=None, max_area=None (автоматический коридор ±35%)
    - '35-75' -> target_area=55.0, min_area=35.0, max_area=75.0
    """
    if not area_str:
        return None, None, None
    s = area_str.strip().replace(",", ".").replace("м2", "").replace("м²", "").strip()
    m_range = re.match(r"^(\d+(?:\.\d+)?)\s*[-–—..]+\s*(\d+(?:\.\d+)?)$", s)
    if m_range:
        a1, a2 = float(m_range.group(1)), float(m_range.group(2))
        lo, hi = min(a1, a2), max(a1, a2)
        return round((lo + hi) / 2.0, 1), lo, hi
    m_num = re.search(r"(\d+(?:\.\d+)?)", s)
    if m_num:
        val = float(m_num.group(1))
        return val, None, None
    return None, None, None


def parse_money_input(val_str: Optional[str]) -> Optional[float]:
    """
    Разбирает ввод суммы от клиента:
    поддерживает '200000', '200 000', '200тыс', '200к', '3500'.
    """
    if not val_str:
        return None
    s = val_str.strip().lower().replace(" ", "").replace("_", "").replace(",", ".")
    mult = 1.0
    if any(k in s for k in ("тыс", "т.р", "тр", "k", "к")):
        mult = 1000.0
    elif any(k in s for k in ("млн", "m", "м")):
        mult = 1_000_000.0
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    return float(m.group(1)) * mult


def _build_full_cost_scales(concept_text: str, full_cost_limit: float, existing_scales: Optional[dict] = None) -> dict:
    if isinstance(full_cost_limit, bool) or not math.isfinite(full_cost_limit) or full_cost_limit <= 0:
        raise ValueError("--full-cost-limit должен быть положительным конечным числом")
    sha = concept_digest(concept_text)
    if existing_scales is not None:
        if existing_scales.get("concept_sha256") != sha:
            raise ValueError("Шкалы оценки в --business-evidence-file относятся к другой концепции")
        scales = dict(existing_scales)
        metrics = dict(existing_scales.get("metrics") or {})
    else:
        scales = {"concept_sha256": sha}
        metrics = {}
    metrics["premises_cost_month"] = {
        "low": 0.0,
        "high": float(full_cost_limit),
        "source": "CLI --full-cost-limit",
    }
    scales["metrics"] = metrics
    return scales


def parse_args():
    parser = argparse.ArgumentParser(
        description="Подбор коммерческих помещений по концепции, площади, цене и окружению"
    )
    parser.add_argument('--city', type=lambda value: get_city(value).key, default='moscow',
                        help='Город поиска: Москва или Санкт-Петербург (также moscow, spb)')
    parser.add_argument(
        "--niche",
        type=str,
        default="Кофейня и пекарня",
        help="Целевая ниша бизнеса клиента (например: 'Кофейня и пекарня', 'Аптека', 'Барбершоп', 'ПВЗ', 'Продукты')",
    )
    parser.add_argument(
        "--area",
        type=str,
        default=None,
        help="Желаемая площадь помещения в м² (например: '50' или диапазон '35-75')",
    )
    parser.add_argument("--min-area", type=float, default=None, help="Минимальная площадь (м²)")
    parser.add_argument("--max-area", type=float, default=None, help="Максимальная площадь (м²)")
    parser.add_argument(
        "--price",
        "--max-price",
        dest="max_price",
        type=float,
        default=None,
        help="Желаемая (максимальная) цена аренды в месяц (₽/мес)",
    )
    sqm_group = parser.add_mutually_exclusive_group()
    sqm_group.add_argument(
        "--price-sqm",
        dest="price_sqm",
        type=float,
        default=None,
        help="Максимальная ставка за м²; период задаётся --price-sqm-period (по умолчанию month)",
    )
    sqm_group.add_argument("--price-sqm-year", type=float, default=None, help="Максимальная ставка в ₽/м²/год")
    parser.add_argument("--price-sqm-period", choices=("month", "year"), default="month", help="Период --price-sqm")
    parser.add_argument(
        "--location",
        type=str,
        default=None,
        help="Район, адрес или метро выбранного города; по умолчанию весь город",
    )
    parser.add_argument("--candidates", type=int, default=5, help="Сколько лучших мест включить в ТОП")
    parser.add_argument("--analysis-limit", type=int, default=None,
                        help="Ограничить глубокий анализ ради скорости; по умолчанию сравниваются все подходящие объекты")
    parser.add_argument("--max-listing-age-hours", type=float, default=None,
                        help="Сравнивать объявления, полученные из источника за последние N часов; записи без подтверждения исключаются")
    parser.add_argument("--business-format", type=str, default=None,
                        help="Свободное описание контекста: аудитория, площадка, способ посещения")
    parser.add_argument('--concept',type=str,default=None,
                        help='Концепция бизнеса для ИИ: покупатели, способ посещения, часы работы и приоритеты выбора помещения')
    parser.add_argument('--business-evidence-file',type=str,default=None,help='JSON с проверенными маршрутами и исходными диапазонами сценария; см. docs/SCORING_CONCEPT.md')
    parser.add_argument('--full-cost-limit',type=float,default=None,
        help='Верхний порог полных затрат на помещение в месяц (₽/мес) для шкалы premises_cost_month')
    parser.add_argument('--exclude-property',action='append',default=[],help='Пропустить уже показанный ключ source:external_id или id:ID; можно повторять')
    parser.add_argument('--include-unverified',action='store_true',
        help='По вашему выбору включить в предварительный рейтинг варианты с неподтверждёнными условиями; известные нарушения исключаются')
    parser.add_argument("--allowed-floors", type=parse_allowed_floors_input, default=None,
                        help="Явно допустимые этажи, например 1,2 или цоколь; доступ на верхние этажи проверяется отдельно")
    parser.add_argument("--skip-collect", action="store_true", help="Пропустить сбор новых объявлений и искать сразу по текущей БД")
    parser.add_argument("-i", "--interactive", action="store_true", help="Интерактивный ввод параметров клиента в консоли")
    args = parser.parse_args()
    args.location = args.location or get_city(args.city).name
    return args


def client_search_command(args, *, include_unverified=False, excludes=None):
    """Serialize effective inputs, including values entered interactively."""
    command=[sys.executable,os.path.abspath(__file__)]
    fields=('niche','area','min_area','max_area','max_price','price_sqm','price_sqm_year','price_sqm_period',
            'city','location','candidates','analysis_limit','max_listing_age_hours','business_format','concept',
            'business_evidence_file','full_cost_limit')
    for name in fields:
        value=getattr(args,name,None)
        if value is not None:command.extend(['--'+name.replace('_','-'),str(value)])
    if getattr(args,'allowed_floors',None) is not None:
        command.extend(['--allowed-floors',','.join(str(v) for v in args.allowed_floors)])
    if args.skip_collect:command.append('--skip-collect')
    if include_unverified or getattr(args,'include_unverified',False):command.append('--include-unverified')
    for key in dict.fromkeys(excludes if excludes is not None else getattr(args,'exclude_property',())):
        command.extend(['--exclude-property',key])
    return command


def prompt_client_interactive(args):
    print("\n" + "=" * 75)
    print("   ВВОД ПАРАМЕТРОВ КЛИЕНТА (ОТКРЫТЫЕ ДАННЫЕ КОММЕРЧЕСКОЙ НЕДВИЖИМОСТИ)")
    print("=" * 75)
    try:
        niche_in = input(f"1. В какой нише вы хотите открыть бизнес? [{args.niche}]: ").strip()
        if niche_in:
            args.niche = niche_in

        area_in = input("2. Желаемая площадь помещения, м² (например 55 или 40-70) [любая под нишу]: ").strip()
        if area_in:
            args.area = area_in

        price_in = input("3. Желаемый бюджет аренды в месяц, ₽ (например 200000) [без ограничений]: ").strip()
        if price_in:
            parsed_p = parse_money_input(price_in)
            if parsed_p:
                args.max_price = parsed_p

        sqm_in = input("4. Максимальная ставка за м² (Enter чтобы пропустить): ").strip()
        if sqm_in:
            parsed_sqm = parse_money_input(sqm_in)
            if parsed_sqm:
                args.price_sqm = parsed_sqm
                args.price_sqm_year = None
                period_in = input("   Период ставки: мес или год [мес]: ").strip().lower()
                args.price_sqm_period = "year" if period_in in ("год", "year") else "month"

        loc_in = input(f"5. Район / метро выбранного города (Enter — оставить) [{args.location}]: ").strip()
        if loc_in:
            args.location = loc_in
        concept_in=input('6. Опишите концепцию: кто покупатели, как приходят, что важно в помещении: ').strip()
        if concept_in:args.concept=concept_in
    except EOFError:
        pass
    return args


def print_detailed_location_report(selected: dict, report, target_biz: str, diameter_m: float):
    print(f"-> Помещение: [{selected['source'].upper()}] {selected['title']} ({selected['address']})")
    print(
        f"-> Площадь: {selected['area_sqm']} м² | Аренда: {selected['price_month']:,.0f} ₽/мес "
        f"({selected['price_sqm_year']:,.0f} ₽/м²/год) | URL: {selected.get('url') or 'н/д'}"
    )
    print(f"-> Координаты: ({selected['lat']:.5f}, {selected['lng']:.5f}) | Диаметр анализа: {diameter_m:.0f} м")
    print(f"-> Целевая ниша: «{target_biz}»")
    print(
        f"-> Модель NVIDIA: {report.model_used} "
        f"(Карты: {report.map_fetch_time_ms:.0f} мс | AI: {report.ai_inference_time_ms:.0f} мс | "
        f"DaData+ФНС: {report.fns_fetch_time_ms:.0f} мс, обогащено {report.fns_enriched_count}/{report.total_cards_found})"
    )
    print(f"-> Всего карточек бизнеса в диаметре {diameter_m:.0f} м: {report.total_cards_found}")
    if getattr(report, "collection_status", None) != "ok" or report.data_status != "ok" or not report.total_cards_found:
        print("-> Данные о конкурентах требуют подтверждения")
    print(f"-> Заключение AI: {report.ai_summary}")
    for warning in report.data_warnings:
        print(f"-> Требует уточнения: {warning}")


    if report.foot_traffic:
        ft = report.foot_traffic
        hp = ft.hourly_profile
        print("\n   [МОДЕЛЬНЫЙ ПРОКСИ ПОТОКА; НЕ ЗАМЕР У ВХОДА]:")
        print(
            f"   -> Индекс прокси: {ft.traffic_index if ft.traffic_index is not None else 'н/д'}/100 — {ft.traffic_level}"
        )
        print(
            f"   -> Прокси пешеходного потока: {estimated_count(ft.estimated_daily_pedestrians)} чел./сутки "
            f"(будни: {estimated_count(ft.estimated_weekday_daily)} | выходные: {estimated_count(ft.estimated_weekend_daily)})"
        )
        print(
            f"   -> Часовой прокси (09:00–21:00): {estimated_count(ft.estimated_avg_hourly_daytime)} чел./час | "
            f"Пик: {estimated_count(ft.estimated_peak_hourly)} чел./час"
        )
        print(
            f"   -> Структура генераторов потока: Метро ~{ft.metro_contribution_daily:,} чел./сут | "
            f"Остановки НОТ ({ft.unique_stops_count} шт.) ~{ft.stops_contribution_daily:,} чел./сут | "
            f"Локальный трафик точек ~{ft.local_commercial_daily:,} чел./сут"
        )
        if ft.estimated_daily_pedestrians is None and transport_bounds_text(ft) is not None:
            status = getattr(ft, 'transport_data_status', '')
            upper_note = '' if ft.transport_upper_daily is not None else ' (верхняя граница открыта)'
            prefix = ('Транспортная составляющая (маршрутная)'
                      if status in ('route_complete', 'route_partial')
                      else 'Транспортная составляющая прокси')
            print(f'   -> {prefix}: {transport_bounds_text(ft)}/сут{upper_note}; это сценарный диапазон, не замер у входа')
        if ft.nearest_metros:
            m_str = ", ".join(
                metro_flow_label(m)
                for m in ft.nearest_metros[:3]
            )
            print(f"   -> Ближайшие станции метро: {m_str}")
        print(
            f"   -> Тип ритма улицы (по {hp.cards_with_histograms} гео-гистограммам Яндекс Карт): {hp.location_rhythm_type} "
            f"(Выходные/Будни: {hp.weekend_to_weekday_ratio:.2f}x)"
        )
        print(
            f"   -> Распределение по времени суток: Утро (08–12): {hp.morning_share_pct}% | "
            f"Обед (12–16): {hp.lunch_share_pct}% | Вечер (16–21): {hp.evening_share_pct}% | "
            f"Ночь: {hp.night_share_pct}%"
        )
        print(
            f"   -> Почасовая кривая (Будни):    {hp.format_hourly_sparkline(is_weekend=False)} "
            f"(пик в {hp.peak_weekday_hour:02d}:00 — {estimated_count(ft.hourly_people_for_hour(hp.peak_weekday_hour, False))} чел./ч)"
        )
        print(
            f"   -> Почасовая кривая (Выходные): {hp.format_hourly_sparkline(is_weekend=True)} "
            f"(пик в {hp.peak_weekend_hour:02d}:00 — {estimated_count(ft.hourly_people_for_hour(hp.peak_weekend_hour, True))} чел./ч)"
        )

    if getattr(report, "population", None):
        pop = report.population
        print(
            f"\n   [НАСЕЛЕНИЕ И ЖИЛОЙ ФОНД В РАДИУСЕ {pop.radius_m:.0f} М "
            f"({', '.join(pop.data_sources_used)})]:"
        )
        print(
            f"   -> Район: {pop.district_name} | Получено зданий из источника: {pop.total_buildings_scanned}"
        )
        if pop.buildings_data_available:
            if not population_counts_available(pop):
                print("   -> Оценки по найденным зданиям. Полнота охвата требует подтверждения.")
            print(
                f"   -> Расчётные жители в радиусе {pop.radius_m:.0f} м: {resident_label(pop)} чел. | "
                f"Жилых домов: {pop.residential_buildings_count} (точное число квартир: {pop.exact_passport_buildings_count}) | "
                f"Квартир: {pop.total_apartments_count:,} | Подъездов: {pop.total_entrances_count}"
            )
            print(
                f"   -> Расчётные сотрудники офисов: ~{pop.estimated_office_workers_count:,} чел. "
                f"({pop.office_buildings_count} зданий)"
            )
        yr_str = f"{pop.avg_construction_year} г." if pop.avg_construction_year else "н/д"
        print(
            f"   -> Характеристики жилого фонда: ср. этажность {pop.avg_building_floors} эт., "
            f"ср. год постройки {yr_str}, преобл. материал стен: {pop.predominant_wall_material}"
        )
        if pop.median_housing_price_sqm_rub:
            print(f"   -> Медиана цен жилья по объявлениям поблизости: {pop.median_housing_price_sqm_rub:,.0f} ₽/м²")
        if pop.median_flat_price_mln_rub:
            print(f"   -> Медиана полной цены квартиры по объявлениям: {pop.median_flat_price_mln_rub:.1f} млн ₽")
        for warning in pop.data_warnings:
            print(f"   -> Требует уточнения: {warning}")
        if pop.top_residential_buildings:
            print("   -> Крупнейшие жилые дома в радиусе (по паспортам МКД 2ГИС):")
            for rb in pop.top_residential_buildings[:5]:
                src_tag = "паспорт МКД" if rb.is_exact_apartments else "расчет по площади"
                yr_tag = f", {rb.year_of_construction} г." if rb.year_of_construction else ""
                mat_tag = f", {rb.material}" if rb.material else ""
                print(
                    f"      * {rb.address} ({rb.distance_m:.0f} м) — {rb.floors} эт., {rb.apartments_count} кв. [{src_tag}], "
                    f"подъездов: {rb.entrances_count}, {count_label(rb.estimated_residents)} жителей{yr_tag}{mat_tag}"
                )

    print(f"\n   [ПРЯМЫЕ КОНКУРЕНТЫ ({len(report.direct_competitors)})]:")
    for comp in report.direct_competitors[:7]:
        c = comp.card
        t_str = f" | {c.traffic_profile_str}" if c.traffic_profile_str else ""
        print(
            f"   - {c.title} [{' / '.join(c.categories[:2])}] | {c.distance_m:.0f} м | "
            f"Рейтинг: {c.rating or 'н/д'} ({c.reviews_count} отз.){t_str} | {c.fns_summary_str} | Причина: {comp.reason}"
        )

    print(f"\n   [КОСВЕННЫЕ КОНКУРЕНТЫ ({len(report.indirect_competitors)})]:")
    for comp in report.indirect_competitors[:7]:
        c = comp.card
        t_str = f" | {c.traffic_profile_str}" if c.traffic_profile_str else ""
        print(
            f"   - {c.title} [{' / '.join(c.categories[:2])}] | {c.distance_m:.0f} м | "
            f"Рейтинг: {c.rating or 'н/д'} ({c.reviews_count} отз.){t_str} | {c.fns_summary_str} | Причина: {comp.reason}"
        )

    print(f"\n   [СИНЕРГИЯ / ГЕНЕРАТОРЫ ТРАФИКА ({len(report.synergy_businesses)})]:")
    for comp in report.synergy_businesses[:5]:
        c = comp.card
        t_str = f" | {c.traffic_profile_str}" if c.traffic_profile_str else ""
        print(
            f"   - {c.title} [{' / '.join(c.categories[:2])}] | {c.distance_m:.0f} м{t_str} | "
            f"{c.fns_summary_str} | Причина: {comp.reason}"
        )



def print_top_comparison_table(search_report):
    header = (
        f"{'ТОП':<4} | {'Адрес помещения':<30} | {'Площ.':<7} | {'Аренда/мес':<12} | "
        f"{'Ставка м²/мес (год)':<21} | {'Трафик/сут':<10} | {'Жители / сотрудники':<19} | {'Конкуренты':<16} | {'Балл':<6}"
    )
    separator = "=" * len(header)
    print("\n" + separator)
    print("ПРЕДВАРИТЕЛЬНОЕ СРАВНЕНИЕ ПОМЕЩЕНИЙ: АРЕНДА, ОЦЕНОЧНЫЙ ТРАФИК И ОКРУЖЕНИЕ")
    print(separator)
    print(header)
    print("-" * len(header))
    for idx, cand in enumerate(search_report.ranked_candidates, start=1):
        p = cand.property_data
        ft = cand.competition_report.foot_traffic
        pop = cand.competition_report.population
        daily_flow = foot_traffic_daily_label(ft)
        people = (f"{resident_label(pop)} / ~{pop.estimated_office_workers_count:,}"
                  if population_counts_available(pop) else "н/д")
        direct_cnt = len(cand.competition_report.direct_competitors)
        addr_short = (p.get("address") or p.get("title") or "Адрес неизвестен")[:30]
        sqm_m = cand.price_sqm_month or round(float(p["price_month"]) / max(1.0, float(p["area_sqm"])), 0)
        sqm_y_k = float(p["price_sqm_year"]) / 1000.0
        comp_str = f"{direct_cnt} шт" if cand.competition_report.data_status == "ok" else "н/д / неполные"
        print(
            f"#{idx:<3} | {addr_short:<30} | {float(p['area_sqm']):>5.0f}м² | "
            f"{float(p['price_month']):>10,.0f} ₽ | "
            f"{sqm_m:>6,.0f} ₽ ({sqm_y_k:>4.0f}к/г) | "
            f"{daily_flow:>10} | {people:>19} | {comp_str:<16} | "+
            f"{cand.total_score:.1f}–{cand.score_upper_bound:.1f}"
        )
    print(separator)


async def main():
    args = parse_args()
    if args.interactive:
        args = prompt_client_interactive(args)
    if not (args.concept or '').strip() and not (args.business_format or '').strip():
        print('Для оценки по сценарию опишите концепцию бизнеса: --concept "Кто покупатели, как приходят и что важно". Можно использовать --interactive.')
        return

    business_data=None
    if args.business_evidence_file:
        with open(args.business_evidence_file,encoding='utf-8-sig') as stream:
            business_data=json.load(stream)
        validate_business_inputs(business_data)
    full_cost_limit=getattr(args,'full_cost_limit',None)
    if full_cost_limit is not None:
        concept_text=require_business_concept(args.concept,args.business_format)
        business_data=dict(business_data or {'properties':{}})
        business_data.setdefault('properties',{})
        business_data['ranking_scales']=_build_full_cost_scales(
            concept_text,full_cost_limit,existing_scales=business_data.get('ranking_scales'))
        validate_business_inputs(business_data)

    target_area, parsed_min_area, parsed_max_area = parse_area_input(args.area)
    min_area = args.min_area if args.min_area is not None else parsed_min_area
    max_area = args.max_area if args.max_area is not None else parsed_max_area
    norm_sqm_m, norm_sqm_y = OptimalPropertyRecommender.normalize_price_sqm(
        args.price_sqm, args.price_sqm_year, args.price_sqm_period
    )

    print("=" * 85)
    city = get_city(args.city)
    print(f">>> {city.name.upper()}: ПОДБОР ПОМЕЩЕНИЙ И СРАВНЕНИЕ С КОНКУРЕНТАМИ")
    print("=" * 85)
    print("ПАРАМЕТРЫ ЗАПРОСА КЛИЕНТА:")
    print(f"  * Ниша бизнеса:             «{args.niche}»")
    if target_area:
        eff_lo = min_area if min_area is not None else round(max(8.0, target_area * 0.65), 1)
        eff_hi = max_area if max_area is not None else round(target_area * 1.35, 1)
        print(f"  * Желаемая площадь:         {target_area:.0f} м² (коридор подбора: {eff_lo:.0f}–{eff_hi:.0f} м²)")
    elif min_area or max_area:
        print(f"  * Диапазон площади:         от {min_area or 0:.0f} до {max_area or 9999:.0f} м²")
    else:
        print("  * Желаемая площадь:         Не указана; предпочтения оцениваются по концепции при её наличии")

    if args.max_price:
        print(f"  * Желаемая цена в месяц:    до {args.max_price:,.0f} ₽/мес")
    else:
        print("  * Желаемая цена в месяц:    Не ограничена")
    if full_cost_limit is not None:
        print(f"  * Ориентир полных затрат:   до {full_cost_limit:,.0f} ₽/мес (--full-cost-limit)")

    if norm_sqm_m and norm_sqm_y:
        print(f"  * Желаемая цена за 1 м²:    до {norm_sqm_m:,.0f} ₽/м²/мес ({norm_sqm_y:,.0f} ₽/м²/год)")
    else:
        print("  * Желаемая цена за 1 м²:    Не указана")

    print(f"  * География поиска:         «{args.location}»")

    aggregator = RealEstateAggregator(db_path=str(city.database_path), proxy=None, city=city)

    if not args.skip_collect:
        print("\n[1/3] Сбор свежих объявлений: " + ', '.join(c.name for c in aggregator.collectors))
        summary = await aggregator.collect_for_search(dict(location_query=args.location,
            target_area_sqm=target_area,min_area_sqm=min_area,max_area_sqm=max_area,
            max_price_month=args.max_price,max_price_sqm=args.price_sqm,max_price_sqm_year=args.price_sqm_year,
            price_sqm_period=args.price_sqm_period,max_listing_age_hours=args.max_listing_age_hours,
            exclude_property_keys=args.exclude_property),target_matches=args.candidates*4)
        search_scope=summary['inventory_search']
        print(f"   Автосбор: {len(search_scope['rounds'])} проходов, глубина до {search_scope['limit_per_source']} объявлений на площадку.")
        print('   '+search_scope['message'])
        stats = summary["database_stats"] if isinstance(summary, dict) and "database_stats" in summary else aggregator.storage.get_statistics()
        for source, result in summary.get("source_results", {}).items():
            print(f"   {source}: {result['status']}, получено {result['collected']}; {'; '.join(result['warnings'])}")
        print(
            f"-> Всего в открытой БД: {stats['total_offers']} объявлений "
            f"(уникальных физических помещений: {stats['unique_physical_spaces']})"
        )
    else:
        stats = aggregator.storage.get_statistics()
        print(
            f"\n[1/3] База коммерческих помещений, {city.name}: {stats['total_offers']} предложений "
            f"({stats['unique_physical_spaces']} уникальных объектов)"
        )

    transport_label = "ДЕПТРАНС" if city.key == "moscow" else "ОТКРЫТЫЕ ТРАНСПОРТНЫЕ И МУНИЦИПАЛЬНЫЕ ДАННЫЕ"
    print(f"\n[2/3] АНАЛИЗ ОТКРЫТЫХ ДАННЫХ (ЯНДЕКС КАРТЫ, ФНС ГИР БО, ПАСПОРТА ДОМОВ 2ГИС, {transport_label}) И ФОРМИРОВАНИЕ ТОПА МЕСТ:")
    search_report = await aggregator.find_optimal_properties(
        target_business=args.niche,
        location_query=args.location,
        target_area_sqm=target_area,
        min_area_sqm=min_area,
        max_area_sqm=max_area,
        max_price_month=args.max_price,
        max_price_sqm=args.price_sqm,
        max_price_sqm_year=args.price_sqm_year,
        price_sqm_period=args.price_sqm_period,
        max_candidates=args.candidates,
        competition_diameter_m=400.0,
        business_format=args.business_format,
        business_concept=args.concept,
        business_data=business_data,
        allowed_floors=args.allowed_floors,
        analysis_limit=args.analysis_limit,
        max_listing_age_hours=args.max_listing_age_hours,
        exclude_property_keys=args.exclude_property,
        include_unverified=args.include_unverified,
    )

    ref_action=next((a for a in search_report.next_actions if a.get('type')=='provide_references'),None)
    if ref_action is not None:
        required=list(ref_action.get('required_metrics') or [])
        print('-> Для оценки и ранжирования требуются ориентиры клиента:')
        for question in (ref_action.get('questions') or required):
            print(f'   - {question}')
        if args.interactive and 'premises_cost_month' in required:
            raw_limit=input('Максимально приемлемые полные затраты на помещение (₽/мес, Enter — пропустить): ').strip()
            parsed_limit=parse_money_input(raw_limit) if raw_limit else None
            if parsed_limit is not None and math.isfinite(parsed_limit) and parsed_limit>0:
                concept_text=require_business_concept(args.concept,args.business_format)
                existing_scales=business_data.get('ranking_scales') if isinstance(business_data,dict) else None
                ranking_scales=_build_full_cost_scales(concept_text,parsed_limit,existing_scales=existing_scales)
                search_report=await aggregator.supply_ranking_references(search_report,ranking_scales,business_data=business_data)
                args.full_cost_limit=parsed_limit
                business_data=dict(business_data or {'properties':{}})
                business_data.setdefault('properties',{})
                business_data['ranking_scales']=ranking_scales
            elif raw_limit:
                print('   Некорректное значение суммы; ориентир не задан.')
        elif 'premises_cost_month' in required and getattr(args,'full_cost_limit',None) is None:
            import subprocess
            cmd=client_search_command(args)
            cmd.extend(['--full-cost-limit','<СУММА_РУБ_МЕС>'])
            print('   Повторите запуск с верхним порогом полных затрат (₽/мес):')
            print('   '+subprocess.list2cmdline(cmd))
            if len(required)>1:
                print('   Дополнительные метрики ('+', '.join(m for m in required if m!='premises_cost_month')+') передайте через --business-evidence-file.')

    if any(a.get('type')=='continue_unverified' for a in search_report.next_actions):
        print('-> Можно продолжить предварительный анализ вариантов с неподтверждёнными условиями.')
        if args.interactive:
            choice=input('Продолжить анализ, сохранив пометки «Требует подтверждения»? [д/Н]: ').strip().casefold()
            if choice in ('д','да','y','yes'):
                search_report=await aggregator.continue_unverified_analysis(search_report,business_data=business_data)
                args.include_unverified=True
        else:
            import subprocess
            print('   '+subprocess.list2cmdline(client_search_command(args,include_unverified=True)))
    print('-> '+search_report.confirmation_message)
    for rejected in search_report.confirmation_rejected_candidates:
        print(f"   Исключено после проверки источников: {rejected['property_data'].get('address','')}")
        for reason in rejected['confirmation']['reasons']:print('      '+reason['message'])
    if any(a.get('type')=='find_more' for a in search_report.next_actions):
        import subprocess
        continuation=next(a for a in search_report.next_actions if a.get('type')=='find_more')
        print('-> Найти ещё варианты с теми же условиями:')
        print('   '+subprocess.list2cmdline(client_search_command(args,
            include_unverified=search_report.include_unverified,excludes=continuation['request']['exclude_property_keys'])))

    print(
        f"-> Определена гео-зона: {search_report.resolved_location_name} "
        f"(центр: {search_report.center_lat:.4f}, {search_report.center_lng:.4f}, радиус охвата: {search_report.search_radius_m:.0f} м)"
    )
    print(
        f"-> Прошло числовые фильтры: {search_report.numerical_matching_count} | "
        f"Включено в анализ: {search_report.total_matching_in_db} | "
        f"Глубоко проанализировано и сравнено кандидатов: {search_report.evaluated_count} "
        f"(время перебора: {search_report.total_search_time_ms / 1000.0:.1f} сек)"
    )

    if search_report.unverified_evaluated_count:
        print(f'-> По вашему выбору оценено вариантов с неподтверждёнными условиями: {search_report.unverified_evaluated_count}. Пригодность требует подтверждения.')
    for warning in search_report.ranking_warnings:
        print(f"[!] {warning}")
    decision=getattr(search_report,'recommendation',{})
    if decision:
        print('Следующий шаг: '+decision['message'])
        if decision.get('primary_candidate_id') is not None:
            print(f"   Первый вариант для проверки: #{decision['primary_candidate_id']}")
        for check in decision.get('checks',[])[:3]:
            print('   Уточнить: '+check['message'])
            for action in check.get('actions',[])[:3]:print('      '+action)
    else:
        for requirement in search_report.missing_inputs[:5]:print('   Уточнить: '+requirement['action'])
    if search_report.ranked_candidates:
        print('Сравнение найденной выборки: '+('завершено' if search_report.comparison_complete else 'не завершено')+
            '; данные оценки: '+('полные' if getattr(search_report,'data_complete',False) else 'есть неизвестные значения'))
    if search_report.stage_timings_ms:
        labels={'concept_profile':'концепция','mandatory_checks':'обязательные проверки','environment':'окружение','company_accounts':'ФНС','scoring':'рейтинг','model_sensitivity':'устойчивость допущений'}
        print('Время этапов: '+', '.join(f"{labels.get(k,k)} {v/1000:.2f} с" for k,v in search_report.stage_timings_ms.items()))
    if search_report.comparable_candidates:
        print('Сопоставимые варианты при изменении данных, весов или сценарных допущений:')
        for item in search_report.comparable_candidates:
            print(f"   #{item.property_data['id']} {item.property_data.get('address','')}: {item.input_score_lower_bound:.1f}–{item.input_score_upper_bound:.1f}/100")
    if search_report.ranking_profile:
        profile=search_report.ranking_profile
        print(f'-> Профиль оценки концепции: {profile.summary}')
        for c in profile.criteria:
            print(f'   {scoring_label(c.metric)}: {c.weight*100:.1f}% — {c.reason}')
            print(f'      Из концепции: «{c.concept_quote}»')
            preference={'higher':'больше — лучше','lower':'меньше — лучше','range':'предпочтительный диапазон'}[c.preference]
            scale=profile.metadata.get('reference_scales',{}).get(c.metric,{})
            if scale.get('usable',True):print(f"      Шкала: {c.low:g}–{c.high:g}; {preference}; источник: {scale.get('source','caller_profile')}")
            else:print('      Шкала неизвестна: нужны ориентиры клиента; вклад сохранён диапазоном')
            if c.assumption:print(f'      Предположение шкалы: {c.assumption}')
        for assumption in profile.assumptions:print(f'   Предположение: {assumption}')
        for question in profile.questions:print(f'   Требует уточнения концепции: {question}')
        coverage={a['concept_quote']:a for a in search_report.requirement_coverage}
        for missing in profile.unmodeled_requirements:
            status=coverage.get(missing,{}).get('status')
            if status=='checked_before_rating':print(f'   Проверено до рейтинга: {missing}')
            elif status=='checked_by_filters':print(f'   Проверено числовыми фильтрами: {missing}')
            elif status=='client_managed':print(f'   Оставлено клиенту: {missing}')
            elif status=='scenario_context':print(f'   Учтено как контекст бизнеса: {missing}')
            elif status=='verification_pending':print(f'   Проверяется допуском помещения; подтверждение ещё нужно: {missing}')
            elif status=='waived_by_client':print(f'   Требование снято клиентом: {missing}')
            elif status=='scoring_conflict':print(f'   Требует уточнения полнота учёта: {missing}')
            else:print(f'   Пока не учитывается формулой: {missing}')

    if search_report.floor_profile:
        profile = search_report.floor_profile
        print(f"-> Сценарий размещения: {profile.label}. {profile.explanation}")
        if profile.business_format:
            print(f"-> Контекст клиента: {profile.business_format}")
        for requirement in profile.request_clauses:
            print(f"   Обязательное условие клиента: {requirement}")
    if search_report.floor_rejected_candidates:
        print(f"-> Исключено по обязательным требованиям к помещению и доступу: {len(search_report.floor_rejected_candidates)}")
        for item in search_report.floor_rejected_candidates[:10]:
            print(f"   [{item.property_data['id']}] {item.property_data['title']}: {item.assessment.reason}")
    if search_report.floor_verification_candidates:
        print(f"-> Требуют подтверждения обязательных условий: {len(search_report.floor_verification_candidates)}")
        for item in search_report.floor_verification_candidates[:10]:
            p = item.property_data
            print(f"   [{p['id']}] {p['title']} | {p['address']} | {item.assessment.reason} | {p.get('url') or 'н/д'}")
            bound=next((b for b in getattr(search_report,'deferred_candidate_bounds',[]) if b['property_id']==p['id']),None)
            if bound and bound['dominated']:
                print(f"      Даже условная верхняя оценка не меняет первый выбор; подтверждение этого варианта для приоритета #{bound['dominated_by_id']} не требуется.")

    if not search_report.ranked_candidates:
        if search_report.ranking_profile and search_report.ranking_profile.status!='ready':
            print('\n[!] Автоматических рекомендаций нет: профиль концепции пока не сформирован. Уточните концепцию или повторите запрос профиля.')
        elif any(g.get('reason')=='missing_scale' for g in search_report.missing_inputs):
            print('\n[!] Найденные объявления сохранены. Для сравнения сначала укажите перечисленные ориентиры оценки.')
        elif search_report.analysis_errors:
            print("\n[!] Анализ подходящих помещений недоступен. Повторите запрос позже.")
        elif search_report.floor_verification_candidates:
            print("\n[!] Автоматических рекомендаций нет: требуется подтверждение перечисленных обязательных условий. По вашему выбору доступно предварительное продолжение анализа.")
        elif search_report.floor_rejected_candidates:
            print("\n[!] Найденные помещения не выполняют обязательные требования к помещению или доступу.")
        else:
            print("\n[!] По заданным фильтрам помещений не найдено. Можно изменить бюджет, площадь или территорию.")
        return

    for error in search_report.analysis_errors:
        print(f"[!] Помещение {error['property_id']}: {error['message']}")

    print_top_comparison_table(search_report)

    print("\n" + "-" * 85)
    print("ДЕТАЛЬНОЕ СРАВНЕНИЕ КАЖДОГО МЕСТА ИЗ ТОПА С КОНКУРЕНТАМИ В ЕГО РАДИУСЕ:")
    print("-" * 85)
    for idx, cand in enumerate(search_report.ranked_candidates, start=1):
        p = cand.property_data
        ft = cand.competition_report.foot_traffic
        bounds = transport_bounds_text(ft)
        if ft and ft.estimated_daily_pedestrians is None and bounds is not None:
            traffic_phrase = f"Транспортная составляющая {bounds}/сут (сценарный диапазон)"
        else:
            daily_flow = estimated_count(ft.estimated_daily_pedestrians)+'/сут' if ft else "н/д"
            peak_flow = estimated_count(ft.estimated_peak_hourly)+'/ч' if ft else "н/д"
            traffic_phrase = f"Пешеходов {daily_flow} (пик {peak_flow})"
        company_revenue = f"{cand.verified_fns_revenue_mln_sum:,.1f} млн ₽/год" if cand.verified_fns_revenue_mln_sum > 0 else "н/д"
        badge = "МЕСТО #1 СРЕДИ ПРОВЕРЕННЫХ КАНДИДАТОВ" if idx == 1 else f"ТОП #{idx}"
        if search_report.selection_status!='preliminary_leader':badge='ВАРИАНТ ДЛЯ ПРОВЕРКИ'
        if cand.confirmation.get('status')=='requires_confirmation':badge='ВАРИАНТ ДЛЯ ПРОВЕРКИ · ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ'
        sqm_m = cand.price_sqm_month or round(float(p["price_month"]) / max(1.0, float(p["area_sqm"])), 0)

        print(f'\n#{idx}. [{badge}] ОЦЕНКА ПО КОНЦЕПЦИИ: {cand.total_score:.1f}–{cand.score_upper_bound:.1f}/100 | Значения/диапазоны заданы для {cand.score_coverage*100:.0f}% веса')
        for metric,part in cand.scoring_components.items():
            low,high=part.get('value_lower'),part.get('value_upper')
            if low is None and high is None:value='н/д'
            elif low==high:value=f"{low:.2f} {scoring_unit(part['unit'])}"
            else:value=f"{low if low is not None else 'неизвестно'}–{high if high is not None else 'неизвестно'} {scoring_unit(part['unit'])}"
            print(f"    {scoring_label(metric)}: {value}; вклад {part['contribution_lower']:.1f}–{part['contribution_upper']:.1f}")
        market=cand.business_model_diagnostics.get('market_context')
        if market:
            counts=market['used_organization_counts']
            print(f"    В рейтинге окружения: прямых {counts['direct']}, косвенных {counts['indirect']}, синергия {counts['synergy']}; юрлиц ФНС {market['financial_entities']}")
            print('    Проходимость/население → аудитория; отзывы/ФНС → сила альтернатив. Параметры шкал сценарные, без калибровки на результатах бизнеса.')
        print(
            f"    Помещение: [{p['source'].upper()}] {p['title']} | Адрес: {p['address']} | Ссылка: {p.get('url') or 'н/д'}"
        )
        print('    Проверка условий: '+cand.confirmation.get('label','Требует подтверждения'))
        for reason in cand.confirmation.get('reasons',[]):print('      '+reason['message'])
        requirements=cand.confirmation.get('checks',{}).get('client_requirements',{}).get('items',[])
        for condition in requirements:
            importance='Обязательно' if condition['importance']=='mandatory' else 'Пожелание'
            print(f"      [{importance}] {condition['client_quote']} | {condition['label']}. {condition['reason']}")
            for evidence_id in condition.get('evidence_ids',[]):
                evidence=cand.confirmation.get('evidence',{}).get(evidence_id,{})
                for link in evidence.get('image_urls',[]) or ([evidence['source_url']] if evidence.get('source_url') else []):
                    print('        '+link)
            if condition['result']=='unknown':
                for evidence_id in condition.get('visual_observation_ids',[]):
                    observation=cand.confirmation.get('evidence',{}).get(evidence_id,{})
                    if observation.get('value'):print('        По фото: '+observation['value'])
        print(
            f"    Условия аренды: Площадь {p['area_sqm']} м² | Цена: {p['price_month']:,.0f} ₽/мес | "
            f"Ставка за м²: {sqm_m:,.0f} ₽/м²/мес ({p['price_sqm_year']:,.0f} ₽/м²/год)"
        )
        if cand.floor_assessment:
            source_label = {"structured": "поле объявления", "description": "описание", "unknown": "не указан"}[cand.floor_assessment.source]
            print(f"    Этажность: {cand.floor_assessment.reason} (источник: {source_label})")
        print(
            f"    Оценка трафика и синергии (D=400м): {traffic_phrase} | "
            f"Синергетических соседей: {len(cand.competition_report.synergy_businesses)} | "
            f"Выручка сопоставленных юрлиц по ФНС: {company_revenue} "
            "(может включать другие филиалы)"
        )
        pop = cand.competition_report.population
        if population_counts_available(pop):
            print(
                f"    Население и офисы (D={2 * pop.radius_m:.0f} м): {resident_label(pop)} жителей "
                f"({pop.residential_buildings_count} МКД, {pop.total_apartments_count:,} квартир) | "
                f"~{pop.estimated_office_workers_count:,} сотр. офисов"
            )

        print(f"    [Сравнение с конкурентами вокруг точки]: {cand.competitor_comparison_summary}")
        direct_list = cand.competition_report.direct_competitors
        if direct_list:
            for c_idx, comp in enumerate(direct_list[:5], start=1):
                dc = comp.card
                t_info = f" | {dc.traffic_profile_str}" if dc.traffic_profile_str else ""
                print(
                    f"       Конкурент {c_idx}: «{dc.title}» — {dc.distance_m:.0f} м от помещения | "
                    f"Рейтинг: {dc.rating or 'н/д'}★ ({dc.reviews_count} отз.){t_info} | {dc.fns_summary_str}"
                )
        if cand.pros:
            print(f"    [+] Преимущества места: {'; '.join(cand.pros)}")
        if cand.cons:
            print(f"    [-] Риски места:        {'; '.join(cand.cons)}")
        if cand.data_warnings:
            print(f"    [?] Ограничения оценки: {'; '.join(cand.data_warnings)}")
        print(f"    [=] Резюме:             {cand.verdict}")

    best = search_report.best_candidate
    if best:
        print("\n" + "=" * 85)
        label='ПРЕДВАРИТЕЛЬНОГО ЛИДЕРА' if search_report.selection_status=='preliminary_leader' else 'ПЕРВОГО ВАРИАНТА ДЛЯ ПРОВЕРКИ'
        print(f"[3/3] ПАСПОРТ {label} (КОНЦЕПЦИЯ «{args.niche.upper()}»):")
        print("=" * 85)
        print_detailed_location_report(
            selected=best.property_data,
            report=best.competition_report,
            target_biz=args.niche,
            diameter_m=search_report.competition_diameter_m,
        )

    print("\n" + "=" * 85)
    print("[OK] Подбор завершен: составлен ТОП мест по открытым данным и выполнено сравнение с конкурентами.")
    print("=" * 85)


if __name__ == "__main__":
    asyncio.run(main())

