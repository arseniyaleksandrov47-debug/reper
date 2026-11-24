import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .storage import haversine_distance

logger = logging.getLogger("FootTrafficEstimator")

DAYS_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
WEEKENDS = ["saturday", "sunday"]
# Existing exposure assumptions retained as a scenario envelope, not station ranks.
METRO_EXPOSURE_SHARE_RANGE = (.06, .22)

@dataclass
class MetroStationFlow:

    name: str
    distance_m: float
    station_daily_passengers: Optional[int]
    estimated_corridor_daily: Optional[int]
    passenger_source: str = ''
    data_status: str = 'unknown'
    passenger_as_of: str = ''
    station_daily_passengers_range: Optional[Tuple[float,float]] = None
    passenger_measure: str = ''
    mapped_entrance_count: int = 0
    location_basis: str = 'station_point'


@dataclass
class HourlyTrafficProfile:
    weekday_hourly_share: List[float] = field(default_factory=lambda: [1.0 / 24.0] * 24)
    weekend_hourly_share: List[float] = field(default_factory=lambda: [1.0 / 24.0] * 24)
    day_of_week_share: Dict[str, float] = field(default_factory=dict)
    morning_share_pct: float = 0.0   # 08:00 - 11:59
    lunch_share_pct: float = 0.0     # 12:00 - 15:59
    evening_share_pct: float = 0.0   # 16:00 - 20:59
    night_share_pct: float = 0.0     # 21:00 - 07:59
    peak_weekday_hour: int = 13
    peak_weekend_hour: int = 15
    weekend_to_weekday_ratio: float = 1.0
    location_rhythm_type: str = "Сбалансированный городской трафик"
    cards_with_histograms: int = 0

    def format_hourly_sparkline(self, is_weekend: bool = False, start_hour: int = 8, end_hour: int = 22) -> str:
        """Возвращает визуальную гистограмму активности по часам (например 08:00–22:00)."""
        bars = "▁▂▃▄▅▆▇█"
        arr = self.weekend_hourly_share if is_weekend else self.weekday_hourly_share
        sub = arr[start_hour : end_hour + 1]
        max_v = max(sub) if sub else 0.0
        if max_v <= 0:
            return "—"
        chars: List[str] = []
        for v in sub:
            idx = int(round((v / max_v) * (len(bars) - 1)))
            idx = max(0, min(len(bars) - 1, idx))
            chars.append(bars[idx])
        return f"{start_hour:02d}:00 [{''.join(chars)}] {end_hour:02d}:00"


@dataclass
class FootTrafficReport:
    estimated_daily_pedestrians: Optional[int]
    estimated_weekday_daily: Optional[int]
    estimated_weekend_daily: Optional[int]
    estimated_avg_hourly_daytime: Optional[int]
    estimated_peak_hourly: Optional[int]
    traffic_index: Optional[float]
    traffic_level: str
    metro_contribution_daily: int
    stops_contribution_daily: int
    local_commercial_daily: int
    nearest_metros: List[MetroStationFlow] = field(default_factory=list)
    unique_stops_count: int = 0
    closest_stop_distance_m: Optional[float] = None
    total_building_entrances: int = 0
    hourly_profile: HourlyTrafficProfile = field(default_factory=HourlyTrafficProfile)
    data_status: str = 'proxy'
    data_warnings: List[str] = field(default_factory=list)
    transport_data_status: str = 'unavailable'
    transport_lower_daily: Optional[float] = None
    transport_upper_daily: Optional[float] = None
    transport_source_coverage: float = 0.0
    transport_sources: List[Dict[str,Any]] = field(default_factory=list)
    transport_routing_required: bool = False
    transport_route_diagnostics: Dict[str,Any] = field(default_factory=dict)

    def hourly_people_for_hour(self, hour: int, is_weekend: bool = False) -> Optional[int]:
        hour = max(0, min(23, hour))
        daily = self.estimated_weekend_daily if is_weekend else self.estimated_weekday_daily
        if daily is None:return None
        shares = (
            self.hourly_profile.weekend_hourly_share
            if is_weekend
            else self.hourly_profile.weekday_hourly_share
        )
        return int(round(daily * shares[hour]))


def summarize_card_histogram(histogram_data: Optional[Dict[str, List[float]]]) -> Optional[str]:
    """Формирует краткую сводку почасовой посещаемости для конкретной карточки бизнеса."""
    if not histogram_data or not isinstance(histogram_data, dict):
        return None

    wd_hours = [0.0] * 24
    we_hours = [0.0] * 24
    wd_total = 0.0
    we_total = 0.0

    for d in WEEKDAYS:
        arr = histogram_data.get(d)
        if isinstance(arr, list) and len(arr) == 24:
            for h in range(24):
                val = float(arr[h] or 0.0)
                wd_hours[h] += val
                wd_total += val

    for d in WEEKENDS:
        arr = histogram_data.get(d)
        if isinstance(arr, list) and len(arr) == 24:
            for h in range(24):
                val = float(arr[h] or 0.0)
                we_hours[h] += val
                we_total += val

    if wd_total + we_total <= 0:
        return None

    combined = [wd_hours[h] + we_hours[h] for h in range(24)]
    peak_h = max(range(24), key=lambda h: combined[h])
    peak_start = max(0, peak_h - 1)
    peak_end = min(23, peak_h + 1)

    avg_wd = wd_total / 5.0 if wd_total > 0 else 0.0
    avg_we = we_total / 2.0 if we_total > 0 else 0.0
    if avg_wd > 0:
        ratio = avg_we / avg_wd
        diff_pct = int(round((ratio - 1.0) * 100))
        we_tag = f"вых: {'+' if diff_pct >= 0 else ''}{diff_pct}%"
    else:
        we_tag = "только вых."

    return f"Пик: {peak_start:02d}:00–{peak_end:02d}:00 ({we_tag})"


class FootTrafficEstimator:
    @staticmethod
    def index_for_daily(estimated_daily):
        """Shared utility convention; does not turn a proxy into a measurement."""
        if estimated_daily >= 15000:
            return min(100.0,85.0+(estimated_daily-15000)/1500.0)
        if estimated_daily >= 6000:
            return 65.0+(estimated_daily-6000)/450.0
        if estimated_daily >= 2500:
            return 42.0+23.0*(estimated_daily-2500)/3500.0
        return max(0.0,estimated_daily/2500.0*42.0)

    @staticmethod
    def _coordinates(value):
        if not isinstance(value,(list,tuple)) or len(value)<2 or any(type(v) is bool for v in value[:2]):return None
        try:lng,lat=float(value[0]),float(value[1])
        except (TypeError,ValueError,OverflowError):return None
        return (lng,lat) if math.isfinite(lat) and math.isfinite(lng) and -90<=lat<=90 and -180<=lng<=180 else None

    @classmethod
    def estimate_for_circle(
        cls,
        center_lat: float,
        center_lng: float,
        radius_m: float,
        cards: List[Any],
        *, metro_dataset=None, metro_entrances=None,
    ) -> FootTrafficReport:
        metro_by_name: Dict[str, float] = {}
        stops_by_id: Dict[str, float] = {}
        metro_points={};metro_labels={};stop_points={}
        unique_entrances: set = set()
        warnings=['Поток — некалиброванный прокси по окружению, не замер у входа; линии улицы и видимость не установлены']

        for card in cards:
            for m in getattr(card, "nearby_metro", None) or []:
                if not isinstance(m, dict):
                    continue
                m_name = str(m.get("name") or "").strip()
                if not m_name:
                    continue
                coords = cls._coordinates(m.get("coordinates"))
                if coords is None:
                    warnings.append(f'Нет корректных координат станции «{m_name}»; расстояние от другой организации не подставлено')
                    continue
                dist = haversine_distance(center_lat, center_lng, coords[1], coords[0])
                station_key=' '.join(m_name.casefold().replace('ё','е').split())
                if station_key not in metro_by_name or dist < metro_by_name[station_key]:
                    metro_by_name[station_key] = dist
                    metro_points[station_key]=coords;metro_labels[station_key]=m_name

            for st in getattr(card, "nearby_stops", None) or []:
                if not isinstance(st, dict):
                    continue
                st_id = str(st.get("id") or st.get("name") or "")
                if not st_id:
                    continue
                coords = cls._coordinates(st.get("coordinates"))
                if coords is None:
                    warnings.append('Нет корректных координат остановки; её расстояние до помещения неизвестно')
                    continue
                dist = haversine_distance(center_lat, center_lng, coords[1], coords[0])
                if dist <= max(400.0, radius_m * 1.8):
                    if st_id not in stops_by_id or dist < stops_by_id[st_id]:
                        stops_by_id[st_id] = dist
                        stop_points[st_id]=dict(name=str(st.get('name') or ''),coordinates=coords)

            for ent in getattr(card, "building_entrances", None) or []:
                coords=cls._coordinates(ent)
                if coords is not None:
                    key = (round(coords[0], 4), round(coords[1], 4))
                    unique_entrances.add(key)

        entrance_groups={}
        if metro_entrances is not None:
            for group in metro_entrances.nearby_groups(center_lat,center_lng):
                key=' '.join(group['name'].casefold().replace('ё','е').split())
                metro_by_name[key]=group['distance_m'];metro_points[key]=group['coordinates'];metro_labels[key]=group['name']
                entrance_groups[key]=group
            warnings.append('Расстояния метро привязаны к нанесённым входам; полнота карты и условные часы работы не доказаны')
        metro_flows: List[MetroStationFlow] = []
        metro_daily_total = 0.0
        missing_metro=False;known_metro=0;bounded_lower=0;bounded_upper=0;has_bounded=False
        sorted_metros = sorted(metro_by_name.items(), key=lambda x: x[1])
        transport_sources=[]
        for station_key, dist_m in sorted_metros:
            m_name=metro_labels[station_key]
            # The stored reference is Moscow-specific. A same-named station
            # elsewhere must not acquire Moscow's passenger count.
            daily_pass = metro_dataset.lookup(m_name,center_lat,center_lng) if metro_dataset is not None else None
            lookup_range=getattr(metro_dataset,'lookup_interval',None)
            passenger_range=lookup_range(m_name,center_lat,center_lng) if daily_pass is None and lookup_range else None
            corridor_share = sum(METRO_EXPOSURE_SHARE_RANGE)/2
            point=metro_points[station_key]
            transport_sources.append(dict(kind='metro_exit',source_id='metro:'+m_name,name=m_name,
                lat=point[1],lng=point[0],base_lower_daily=.85*corridor_share*(daily_pass if daily_pass is not None else passenger_range[0] if passenger_range else 0),
                base_upper_daily=.85*corridor_share*(daily_pass if daily_pass is not None else passenger_range[1]) if daily_pass is not None or passenger_range else None,
                source=metro_dataset.source if daily_pass is not None or passenger_range else '',
                as_of=metro_dataset.as_of.isoformat() if daily_pass is not None or passenger_range else '',
                station_passengers_lower=daily_pass if daily_pass is not None else passenger_range[0] if passenger_range else 0,
                station_passengers_upper=daily_pass if daily_pass is not None else passenger_range[1] if passenger_range else None,
                exposure_share_range=list(METRO_EXPOSURE_SHARE_RANGE),
                exposure_share_assumption=corridor_share,decay_minutes=310/80,grace_minutes=30/80))
            group=entrance_groups.get(station_key)
            if group:
                transport_sources[-1].update(entrance_nodes=[v['node'] for v in group['entrances']],
                    mapped_entrances=group['entrances'],station_binding=group['station_binding'],
                    entrance_source=metro_entrances.source,
                    flow_allocation='one station total shared by all mapped entrances; never summed per entrance')
            decay = math.exp(-max(0.0, dist_m - 30.0) / 310.0)
            corridor_flow = int(round(daily_pass * corridor_share * decay)) if daily_pass is not None else None
            if corridor_flow is not None:
                metro_daily_total += corridor_flow;known_metro+=1
            elif passenger_range is not None:
                bounded_lower+=math.floor(passenger_range[0]*corridor_share*decay)
                bounded_upper+=math.ceil(passenger_range[1]*corridor_share*decay)
                has_bounded=True;known_metro+=1
                if getattr(metro_dataset,'measure',None)=='passengers_per_day_unspecified':
                    warnings.append(f'Для станции «{m_name}» опубликован среднесуточный пассажиропоток без разделения входа и выхода: вклад учитывается диапазоном от нуля до опубликованного значения')
                else:
                    warnings.append(f'Станция «{m_name}» относится к нескольким линиям: использован диапазон одной линии, пассажиропотоки не суммировались')
            else:
                missing_metro=True
                warnings.append(f'Пассажиропоток станции «{m_name}» неизвестен; недатированная таблица и типовое число не подставлены')
            metro_flows.append(
                MetroStationFlow(
                    name=m_name,
                    distance_m=round(dist_m, 0),
                    station_daily_passengers=daily_pass,
                    estimated_corridor_daily=corridor_flow,
                    passenger_source=metro_dataset.source if daily_pass is not None or passenger_range is not None else '',
                    passenger_as_of=metro_dataset.as_of.isoformat() if daily_pass is not None or passenger_range is not None else '',
                    data_status='dated_reference' if daily_pass is not None else 'dated_range' if passenger_range is not None else 'unknown',
                    station_daily_passengers_range=passenger_range,
                    passenger_measure=getattr(metro_dataset,'measure','') if daily_pass is not None or passenger_range is not None else '',
                    mapped_entrance_count=len(group['entrances']) if group else 0,
                    location_basis='mapped_entrance' if group else 'station_point',
                )
            )

        stops_daily_total = 0.0
        for st_id, dist_m in stops_by_id.items():
            stop_decay = math.exp(-max(0.0, dist_m - 15.0) / 160.0)
            stops_daily_total += 950.0 * stop_decay
            point=stop_points[st_id]
            transport_sources.append(dict(kind='public_transport_stop',source_id='stop:'+str(st_id),name=point['name'],
                lat=point['coordinates'][1],lng=point['coordinates'][0],base_lower_daily=950*.85,base_upper_daily=950*.85,
                source='mapped stop with explicit uncalibrated 950/day scenario assumption',as_of='',
                decay_minutes=160/80,grace_minutes=15/80))

        closest_stop_m = min(stops_by_id.values()) if stops_by_id else None

        total_ratings = sum(int(getattr(c, "ratings_count", 0) or 0) for c in cards)
        cards_count = len(cards)
        entrances_count = max(len(unique_entrances), cards_count)

        area_norm = max(0.5, (radius_m / 200.0) ** 2)
        reviews_daily_proxy = ((total_ratings * 165.0) / 900.0) / math.sqrt(area_norm)
        orgs_daily_proxy = (cards_count * 135.0 + entrances_count * 38.0) / math.sqrt(area_norm)
        local_commercial_daily = int(round(min(18000.0, reviews_daily_proxy + orgs_daily_proxy)))

        raw_daily = (metro_daily_total + stops_daily_total + local_commercial_daily) * 0.85
        estimated_daily = max(0, int(round(raw_daily)))
        available=raw_daily>0 and not missing_metro and not has_bounded
        if not available:warnings.append('Нет достаточных сигналов даже для прокси потока; ноль не означает отсутствие прохожих')

        hourly_profile = cls._build_hourly_profile(cards, radius_m)
        wd_share_sum = sum(hourly_profile.day_of_week_share.get(d, 1.0 / 7.0) for d in WEEKDAYS)
        we_share_sum = sum(hourly_profile.day_of_week_share.get(d, 1.0 / 7.0) for d in WEEKENDS)
        weekly_total_pedestrians = estimated_daily * 7.0
        estimated_weekday_daily = int(round((weekly_total_pedestrians * wd_share_sum) / 5.0))
        estimated_weekend_daily = int(round((weekly_total_pedestrians * we_share_sum) / 2.0))

        daytime_share = sum(hourly_profile.weekday_hourly_share[9:21])
        estimated_avg_hourly_daytime = int(round((estimated_weekday_daily * daytime_share) / 12.0))

        peak_wd_flow = max(estimated_weekday_daily * s for s in hourly_profile.weekday_hourly_share)
        peak_we_flow = max(estimated_weekend_daily * s for s in hourly_profile.weekend_hourly_share)
        estimated_peak_hourly = int(round(max(peak_wd_flow, peak_we_flow)))

        traffic_index = round(cls.index_for_daily(estimated_daily),1)

        if traffic_index >= 85:
            traffic_level = "Сверхвысокое значение прокси потока"
        elif traffic_index >= 68:
            traffic_level = "Высокий городской трафик (Активный коридор)"
        elif traffic_index >= 48:
            traffic_level = "Средний устойчивый трафик"
        elif traffic_index >= 28:
            traffic_level = "Умеренное значение прокси потока"
        else:
            traffic_level = "Низкое значение прокси потока" if available else "Поток неизвестен"
        if missing_metro:traffic_level='Поток неизвестен: нет датированных данных части транспортных источников'
        elif has_bounded:traffic_level='Прокси потока задан диапазоном по датированным источникам'

        return FootTrafficReport(
            estimated_daily_pedestrians=estimated_daily if available else None,
            estimated_weekday_daily=estimated_weekday_daily if available else None,
            estimated_weekend_daily=estimated_weekend_daily if available else None,
            estimated_avg_hourly_daytime=estimated_avg_hourly_daytime if available else None,
            estimated_peak_hourly=estimated_peak_hourly if available else None,
            traffic_index=traffic_index if available else None,
            traffic_level=traffic_level,
            metro_contribution_daily=int(round(metro_daily_total * 0.85)),
            stops_contribution_daily=int(round(stops_daily_total * 0.85)),
            local_commercial_daily=int(round(local_commercial_daily * 0.85)),
            nearest_metros=metro_flows,
            unique_stops_count=len(stops_by_id),
            closest_stop_distance_m=round(closest_stop_m, 0) if closest_stop_m is not None else None,
            total_building_entrances=entrances_count,
            hourly_profile=hourly_profile,
            data_status='partial' if missing_metro else 'bounded_proxy' if has_bounded else 'proxy' if available else 'unavailable',
            data_warnings=warnings,
            transport_data_status='partial' if missing_metro else 'bounded' if has_bounded else 'complete' if known_metro or stops_by_id else 'unavailable',
            transport_lower_daily=(metro_daily_total+bounded_lower+stops_daily_total)*.85,
            transport_upper_daily=None if missing_metro else (metro_daily_total+bounded_upper+stops_daily_total)*.85,
            transport_source_coverage=(known_metro+len(stops_by_id))/(len(sorted_metros)+len(stops_by_id)) if sorted_metros or stops_by_id else 0,
            transport_sources=transport_sources,transport_routing_required=True,
            transport_route_diagnostics=dict(applied=False,method='preliminary_geodesic_not_accessible_audience'),
        )

    @classmethod
    def _build_hourly_profile(cls, cards: List[Any], radius_m: float) -> HourlyTrafficProfile:
        wd_hours = [0.0] * 24
        we_hours = [0.0] * 24
        day_totals = {d: 0.0 for d in DAYS_ORDER}
        cards_with_hist = 0

        for card in cards:
            hist = getattr(card, "histogram_data", None)
            if not hist or not isinstance(hist, dict):
                continue

            dist = float(getattr(card, "distance_m", 100.0) or 100.0)
            ratings = int(getattr(card, "ratings_count", 0) or 0)
            weight = math.exp(-dist / max(120.0, radius_m)) * (1.0 + 0.35 * math.log1p(ratings))

            valid_card = False
            for d in DAYS_ORDER:
                arr = hist.get(d)
                if not isinstance(arr, list) or len(arr) != 24:
                    continue
                valid_card = True
                for h in range(24):
                    v = max(0.0, float(arr[h] or 0.0)) * weight
                    day_totals[d] += v
                    if d in WEEKDAYS:
                        wd_hours[h] += v
                    else:
                        we_hours[h] += v

            if valid_card:
                cards_with_hist += 1

        if cards_with_hist == 0 or (sum(wd_hours) + sum(we_hours) <= 0):
            default_wd = [
                0.004, 0.002, 0.001, 0.001, 0.002, 0.005,
                0.015, 0.038, 0.068, 0.072, 0.062, 0.065,
                0.082, 0.088, 0.076, 0.068, 0.070, 0.080,
                0.084, 0.062, 0.032, 0.014, 0.006, 0.003,
            ]
            default_we = [
                0.006, 0.004, 0.002, 0.001, 0.001, 0.003,
                0.008, 0.016, 0.032, 0.052, 0.068, 0.082,
                0.092, 0.098, 0.096, 0.092, 0.088, 0.082,
                0.074, 0.054, 0.028, 0.012, 0.006, 0.003,
            ]
            return HourlyTrafficProfile(
                weekday_hourly_share=default_wd,
                weekend_hourly_share=default_we,
                day_of_week_share={d: 1.0 / 7.0 for d in DAYS_ORDER},
                morning_share_pct=24.0,
                lunch_share_pct=31.4,
                evening_share_pct=36.4,
                night_share_pct=8.2,
                peak_weekday_hour=13,
                peak_weekend_hour=13,
                weekend_to_weekday_ratio=1.0,
                location_rhythm_type="Стандартный городской профиль (оценка)",
                cards_with_histograms=0,
            )

        wd_sum = sum(wd_hours) or 1.0
        we_sum = sum(we_hours) or 1.0
        total_week_sum = sum(day_totals.values()) or 1.0

        wd_norm = [v / wd_sum for v in wd_hours]
        we_norm = [v / we_sum for v in we_hours]
        day_shares = {d: (day_totals[d] / total_week_sum) for d in DAYS_ORDER}

        all_hours = [wd_hours[h] + we_hours[h] for h in range(24)]
        all_sum = sum(all_hours) or 1.0
        morning_pct = round(sum(all_hours[8:12]) / all_sum * 100.0, 1)
        lunch_pct = round(sum(all_hours[12:16]) / all_sum * 100.0, 1)
        evening_pct = round(sum(all_hours[16:21]) / all_sum * 100.0, 1)
        night_pct = round(max(0.0, 100.0 - morning_pct - lunch_pct - evening_pct), 1)

        peak_wd_h = max(range(24), key=lambda h: wd_norm[h])
        peak_we_h = max(range(24), key=lambda h: we_norm[h])

        avg_wd_day = wd_sum / 5.0
        avg_we_day = we_sum / 2.0
        we_to_wd_ratio = round(avg_we_day / avg_wd_day, 2) if avg_wd_day > 0 else 1.0

        if we_to_wd_ratio >= 1.15:
            rhythm_type = "Туристический / Торгово-досуговый (рост трафика в выходные)"
        elif we_to_wd_ratio <= 0.78:
            rhythm_type = "Деловой / Офисный кластер (основной поток в будни)"
        elif evening_pct >= 42.0:
            rhythm_type = "Вечерний / Ресторанно-прогулочный коридор"
        elif morning_pct + lunch_pct >= 62.0:
            rhythm_type = "Дневной деловой и сервисный трафик"
        else:
            rhythm_type = "Сбалансированный городской трафик (7 дней в неделю)"

        return HourlyTrafficProfile(
            weekday_hourly_share=wd_norm,
            weekend_hourly_share=we_norm,
            day_of_week_share=day_shares,
            morning_share_pct=morning_pct,
            lunch_share_pct=lunch_pct,
            evening_share_pct=evening_pct,
            night_share_pct=night_pct,
            peak_weekday_hour=peak_wd_h,
            peak_weekend_hour=peak_we_h,
            weekend_to_weekday_ratio=we_to_wd_ratio,
            location_rhythm_type=rhythm_type,
            cards_with_histograms=cards_with_hist,
        )


