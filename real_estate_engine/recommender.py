import asyncio
import copy
import logging
import math
import re
import statistics
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import orjson
from curl_cffi import requests as curl_requests

from .collectors.base import _DirectInterfaceBridge
from .competitors import NvidiaCompetitorAnalyzer, RadiusCompetitionReport
from .storage import PropertyStorage, haversine_distance
from .floor_policy import (
    BusinessFloorProfile, BusinessFloorProfileResolver, FloorAssessment,
    FloorCandidateReview, _floor_precheck, _unverified, assess_floor, floor_candidate_key,
)
from .ranking_policy import RankingProfile, RankingProfileResolver, METRICS, evaluate_profile, bind_reference_scales, explain_components
from .business_model import attach_business_inputs, validate_business_inputs, assess_mandatory_requirements
from .requirement_coverage import assess_requirement_coverage
from .scenario_contract import access_requirement_quotes
from .confirmation_flow import with_confirmation
from .cities import get_city, city_in_text

logger = logging.getLogger("PropertyRecommender")


def require_business_concept(business_concept, business_format):
    concept = business_format if business_concept is None else business_concept
    if not isinstance(concept, str) or not concept.strip():
        raise ValueError('Концепция бизнеса должна быть непустым текстом')
    return concept

# Справочник районов, округов и деловых кластеров Москвы:
# ключ -> (lat, lng, radius_m, каноническое название, ключевые слова в адресах)
MOSCOW_DISTRICTS_AND_ZONES: Dict[str, Tuple[float, float, float, str, List[str]]] = {
    "цао": (55.7558, 37.6173, 4500.0, "ЦАО (Центральный округ)", ["тверск", "арбат", "преснен", "басман", "таган", "хамовник", "замосквореч", "якиманк", "мещанск", "красносельск"]),
    "центр": (55.7558, 37.6173, 3500.0, "Центр Москвы (Садовое кольцо)", ["тверск", "арбат", "воздвиженк", "никитск", "покровк", "маросейк", "пятницк", "ордынк", "охотный"]),
    "тверской": (55.7655, 37.6045, 2200.0, "Тверской район (ЦАО)", ["тверск", "дмитровк", "петро", "лесная", "новослободск", "белорусск", "охотный", "гнездников", "глинищев"]),
    "арбат": (55.7505, 37.5915, 1700.0, "Район Арбат / Новый Арбат (ЦАО)", ["арбат", "воздвиженк", "знаменк", "молчановк", "смоленск", "новинск", "никитск"]),
    "хамовники": (55.7310, 37.5820, 2500.0, "Район Хамовники (ЦАО)", ["хамовни", "остоженк", "пречистенк", "льва толстого", "фрунзенск", "пироговск", "плющих", "зубовск", "волхонк"]),
    "пресненский": (55.7545, 37.5540, 2600.0, "Пресненский район / Сити (ЦАО)", ["преснен", "1905", "краснопреснен", "баррикадн", "бронн", "грузинск", "звенигородск", "мантулинск"]),
    "москва-сити": (55.7475, 37.5385, 1300.0, "Деловой центр «Москва-Сити»", ["пресненская наб", "тестовская", "сити", "выставочн"]),
    "сити": (55.7475, 37.5385, 1300.0, "Деловой центр «Москва-Сити»", ["пресненская наб", "тестовская", "сити"]),
    "патриаршие": (55.7635, 37.5925, 1000.0, "Патриаршие пруды (ЦАО)", ["бронн", "спиридоновк", "козихин", "патриарш", "ермолаевск"]),
    "замоскворечье": (55.7335, 37.6280, 1900.0, "Район Замоскворечье (ЦАО)", ["пятницк", "новокузнецк", "ордынк", "павелецк", "щипок", "люсиновск", "татарск", "садовническ"]),
    "якиманка": (55.7315, 37.6115, 1900.0, "Район Якиманка (ЦАО)", ["якиманк", "полянк", "донская", "шаболовк", "кадашевск", "болотн"]),
    "басманный": (55.7640, 37.6580, 2500.0, "Басманный район (ЦАО)", ["басман", "покровк", "маросейк", "солянк", "земляной", "бауманск", "курск", "чистопруд"]),
    "таганский": (55.7410, 37.6580, 2300.0, "Таганский район (ЦАО)", ["таганск", "гончарн", "николоямск", "рогожск", "воронцовск", "марксистск"]),
    "мещанский": (55.7785, 37.6265, 2100.0, "Мещанский район (ЦАО)", ["проспект мира", "сретенк", "цветной", "трубн", "сухаревск", "олимпийск", "щепкин"]),
    "красносельский": (55.7745, 37.6485, 2000.0, "Красносельский район (ЦАО)", ["каланчевск", "красносельск", "маши порываевой", "сахарова", "комсомольск", "мясницк"]),
    "даниловский": (55.7090, 37.6275, 2500.0, "Даниловский район (ЮАО)", ["автозаводск", "тульск", "холодильн", "дербеневск", "павелецкая наб", "симоновск"]),
    "донской": (55.7160, 37.6040, 2100.0, "Донской район (ЮАО)", ["донская", "вавилова", "орджоникидзе", "ленинский", "загородн"]),
    "академический": (55.6880, 37.5680, 2300.0, "Академический район (ЮЗАО)", ["дмитрия ульянова", "профсоюзн", "кедрова", "кржижановск", "вавилова"]),
    "гагаринский": (55.6940, 37.5450, 2400.0, "Гагаринский район / Университет (ЮЗАО)", ["вернадского", "ломоносовск", "вавилова", "университетск"]),
    "раменки": (55.7020, 37.5080, 2800.0, "Район Раменки (ЗАО)", ["мичуринск", "вернадского", "мосфильмовск", "ломоносовск", "раменк"]),
    "дорогомилово": (55.7410, 37.5340, 2200.0, "Район Дорогомилово / Кутузовский (ЗАО)", ["кутузовск", "киевск", "береговой", "дорогомиловск"]),
    "фили": (55.7430, 37.4880, 2500.0, "Фили-Давыдково / Филёвский парк (ЗАО)", ["олеко дундича", "барклая", "багратионовск", "кастанаевск", "филевск"]),
    "кунцево": (55.7300, 37.4450, 3000.0, "Район Кунцево / Можайский (ЗАО)", ["рублевск", "верейск", "молодогвардейск", "ярцевск"]),
    "крылатское": (55.7630, 37.4080, 2200.0, "Район Крылатское (ЗАО)", ["осенний", "крылатск", "рублевск"]),
    "сокол": (55.8050, 37.5150, 2200.0, "Район Сокол / Войковский (САО)", ["ленинградск", "волоколамск", "алабяна", "балтийск"]),
    "аэропорт": (55.7980, 37.5420, 2200.0, "Район Аэропорт / Савёловский (САО)", ["ленинградск", "мишина", "масловка", "планетн"]),
    "марьина роща": (55.7960, 37.6020, 2200.0, "Район Марьина Роща / Бутырский (СВАО)", ["двинцев", "сущевск", "шереметьевск", "стрелецк", "складочн"]),
    "останкинский": (55.8180, 37.6260, 2500.0, "Останкинский / Алексеевский / ВДНХ (СВАО)", ["проспект мира", "годовикова", "звездный", "королева"]),
    "вднх": (55.8210, 37.6320, 2400.0, "Район ВДНХ / Алексеевский (СВАО)", ["проспект мира", "годовикова", "звездный", "космонавтов"]),
    "южнопортовый": (55.7180, 37.6760, 2400.0, "Южнопортовый / Дубровка (ЮВАО)", ["шарикоподшипниковск", "машиностроения", "южнопортов", "велозаводск"]),
    "сао": (55.8000, 37.5400, 4500.0, "САО (Северный округ)", ["ленинградск", "мишина", "волоколамск", "дмитровск"]),
    "свао": (55.8150, 37.6280, 4500.0, "СВАО (Северо-Восточный округ)", ["проспект мира", "двинцев", "годовикова", "звездный"]),
    "зао": (55.7300, 37.4700, 5000.0, "ЗАО (Западный округ)", ["кутузовск", "рублевск", "вернадского", "верейск", "осенний", "дундича"]),
    "юзао": (55.6750, 37.5450, 4500.0, "ЮЗАО (Юго-Западный округ)", ["профсоюзн", "вернадского", "бутлерова", "дмитрия ульянова"]),
    "юао": (55.7000, 37.6200, 4500.0, "ЮАО (Южный округ)", ["донская", "холодильн", "щипок", "автозаводск"]),
    "ювао": (55.7150, 37.6850, 4500.0, "ЮВАО (Юго-Восточный округ)", ["шарикоподшипниковск", "волгоградск", "рязанск"]),
    "москва": (55.7558, 37.6173, 25000.0, "Вся Москва (все районы и округа)", []),
    "вся москва": (55.7558, 37.6173, 25000.0, "Вся Москва (все районы и округа)", []),
    "любой": (55.7558, 37.6173, 25000.0, "Вся Москва (все районы и округа)", []),
}

# Справочник районов и деловых/торговых кластеров Санкт-Петербурга:
# ключ -> (lat, lng, radius_m, каноническое название, ключевые слова в адресах)
SPB_DISTRICTS_AND_ZONES: Dict[str, Tuple[float, float, float, str, List[str]]] = {
    "центр": (59.9343, 30.3351, 3200.0, "Центр Санкт-Петербурга", ["невск", "лиговск", "садов", "литей", "рубинштейна", "восстания", "горохов", "итальянск", "жуковск", "маяковск", "мохов", "караван", "конюшен"]),
    "центральный": (59.9343, 30.3351, 3200.0, "Центральный район (СПб)", ["невск", "лиговск", "литей", "восстания", "жуковск", "маяковск", "суворовск", "рубинштейна", "гончарн", "кирочн", "шпалерн", "некрасов", "фурштат", "чернышевск"]),
    "петроградка": (59.9657, 30.3114, 2400.0, "Петроградский район / Петроградка", ["каменноостров", "большой пр. п", "большой проспект п", "чкаловск", "кронверк", "петроград", "попова", "вяземск", "ординарн", "пушкарск", "льва толстого", "добролюбов", "блохина"]),
    "петроградке": (59.9657, 30.3114, 2400.0, "Петроградский район / Петроградка", ["каменноостров", "большой пр. п", "большой проспект п", "чкаловск", "кронверк", "петроград", "попова", "вяземск", "ординарн", "пушкарск", "льва толстого", "добролюбов", "блохина"]),
    "петроградский": (59.9657, 30.3114, 2400.0, "Петроградский район / Петроградка", ["каменноостров", "большой пр. п", "большой проспект п", "чкаловск", "кронверк", "петроград", "попова", "вяземск", "ординарн", "пушкарск", "льва толстого", "добролюбов", "блохина"]),
    "петроградская сторона": (59.9657, 30.3114, 2400.0, "Петроградский район / Петроградка", ["каменноостров", "большой пр. п", "большой проспект п", "чкаловск", "кронверк", "петроград", "попова", "вяземск", "ординарн", "пушкарск", "льва толстого", "добролюбов", "блохина"]),
    "петроградская": (59.9657, 30.3114, 2400.0, "Петроградский район / Петроградка", ["каменноостров", "большой пр. п", "большой проспект п", "чкаловск", "кронверк", "петроград", "попова", "вяземск", "ординарн", "пушкарск", "льва толстого", "добролюбов", "блохина"]),
    "васька": (59.9404, 30.2640, 2800.0, "Василеостровский район (СПб)", ["васильев", "средний пр", "малый пр", "косая линия", "кадетск", "наличн", "кораблестроител", "уральск", "железноводск", "смоленк", "гаван", "морская наб", "василеостров"]),
    "ваське": (59.9404, 30.2640, 2800.0, "Василеостровский район (СПб)", ["васильев", "средний пр", "малый пр", "косая линия", "кадетск", "наличн", "кораблестроител", "уральск", "железноводск", "смоленк", "гаван", "морская наб", "василеостров"]),
    "васильевский остров": (59.9404, 30.2640, 2800.0, "Василеостровский район (СПб)", ["васильев", "средний пр", "малый пр", "косая линия", "кадетск", "наличн", "кораблестроител", "уральск", "железноводск", "смоленк", "гаван", "морская наб", "василеостров"]),
    "васильевский": (59.9404, 30.2640, 2800.0, "Василеостровский район (СПб)", ["васильев", "средний пр", "малый пр", "косая линия", "кадетск", "наличн", "кораблестроител", "уральск", "железноводск", "смоленк", "гаван", "морская наб", "василеостров"]),
    "василеостровский": (59.9404, 30.2640, 2800.0, "Василеостровский район (СПб)", ["васильев", "средний пр", "малый пр", "косая линия", "кадетск", "наличн", "кораблестроител", "уральск", "железноводск", "смоленк", "гаван", "морская наб", "василеостров"]),
    "адмиралтейский": (59.9185, 30.3055, 2500.0, "Адмиралтейский район (СПб)", ["садов", "сенн", "горохов", "вознесенск", "московский пр", "измайловск", "лермонтовск", "рижск", "декабристов", "мойк", "фонтанк", "грибоедова", "обводн", "пряжк", "коломн", "адмиралтейск", "рузовск"]),
    "коломна": (59.9200, 30.2850, 1700.0, "Коломна (Адмиралтейский район)", ["декабристов", "пряжк", "английск", "римского-корсакова", "садов", "лермонтовск", "коломн"]),
    "приморский": (60.0020, 30.2580, 4000.0, "Приморский район (СПб)", ["комендант", "богатырск", "савушкин", "приморск", "планерн", "оптиков", "туристическ", "долгоозерн", "испытателей", "авиаконструкторов", "торфян", "мебел", "сизова", "коломяжск", "ланск", "королева", "шуваловск"]),
    "комендантский": (60.0085, 30.2585, 2400.0, "Комендантский аэродром / м. Комендантский", ["комендант", "испытателей", "богатырск", "долгоозерн", "королева", "сизова", "уточкина", "авиаконструкторов"]),
    "старая деревня": (59.9895, 30.2550, 2000.0, "Старая Деревня / м. Беговая (СПб)", ["савушкин", "торфян", "липовая", "школьн", "бегов", "мебел", "приморск"]),
    "калининский": (59.9980, 30.3980, 3800.0, "Калининский район (СПб)", ["гражданск", "просвещения", "наук", "непокоренных", "пискаревск", "кондратьевск", "металлист", "замшина", "руставели", "луначарск", "кушелев", "калининск"]),
    "гражданка": (60.0250, 30.4050, 2600.0, "Гражданка (Калининский район)", ["гражданск", "просвещения", "наук", "луначарск", "северный пр", "руставели", "суздальск", "верности"]),
    "выборгский": (60.0420, 30.3260, 4200.0, "Выборгский район (СПб)", ["энгельса", "просвещения", "озерк", "парнас", "торезов", "лесной", "сампсониевск", "кантемировск", "светлановск", "есенина", "художников", "сикейроса", "хошимина", "выборгск", "муринск", "дудина"]),
    "озерки": (60.0370, 30.3215, 2200.0, "Озерки / Проспект Просвещения (СПб)", ["энгельса", "просвещения", "озерк", "есенина", "художников", "сикейроса", "луначарск", "хошимина"]),
    "парнас": (60.0670, 30.3340, 2200.0, "Парнас / Северная Долина (СПб)", ["дудина", "заречн", "абрамова", "рубцова", "гаврилина", "меркурьева", "парнас"]),
    "фрунзенский": (59.8450, 30.3800, 3600.0, "Фрунзенский район / Купчино (СПб)", ["купчин", "бухарест", "славы", "дунайск", "белы куна", "софийск", "пражск", "турку", "димитрова", "малая балканск", "гашека", "фрунзенск", "загребск"]),
    "купчино": (59.8320, 30.3820, 3000.0, "Купчино (Фрунзенский район)", ["купчин", "бухарест", "славы", "дунайск", "балканск", "гашека", "олеко дундича", "димитрова", "загребск", "малая каштанов"]),
    "московский район": (59.8550, 30.3200, 3600.0, "Московский район (СПб)", ["московск", "ленинск", "типанова", "гагарина", "космонавт", "звездн", "пулковск", "победы", "бассейн", "варшавск", "кубинск", "оренбургск", "благодатн", "краснопутиловск", "алтайск"]),
    "московский р-н": (59.8550, 30.3200, 3600.0, "Московский район (СПб)", ["московск", "ленинск", "типанова", "гагарина", "космонавт", "звездн", "пулковск", "победы", "бассейн", "варшавск", "кубинск", "оренбургск", "благодатн", "краснопутиловск", "алтайск"]),
    "невский район": (59.8820, 30.4600, 4000.0, "Невский район (СПб)", ["обуховской обороны", "бабушкина", "седова", "елизаров", "ломоносовск", "дыбенко", "большевиков", "дальневосточн", "коллонтай", "подвойск", "тельмана", "народн", "ивановск", "рыбацк"]),
    "невский р-н": (59.8820, 30.4600, 4000.0, "Невский район (СПб)", ["обуховской обороны", "бабушкина", "седова", "елизаров", "ломоносовск", "дыбенко", "большевиков", "дальневосточн", "коллонтай", "подвойск", "тельмана", "народн", "ивановск", "рыбацк"]),
    "красногвардейский": (59.9540, 30.4400, 3600.0, "Красногвардейский район / Охта (СПб)", ["ладожск", "энергетик", "наставник", "косыгина", "индустриальн", "большеохтин", "малоохтин", "шаумяна", "новочеркасск", "заневск", "металлист", "красногвардей", "порохов"]),
    "охта": (59.9480, 30.4150, 2400.0, "Охта / Новочеркасская (СПб)", ["большеохтин", "малоохтин", "среднеохтин", "шаумяна", "новочеркасск", "заневск", "металлист"]),
    "ладожская": (59.9325, 30.4395, 2200.0, "Район м. Ладожская / Заневский (СПб)", ["ладожск", "заневск", "энергетик", "косыгина", "уткин", "гранитн", "малоохтин"]),
    "кировский": (59.8730, 30.2580, 3200.0, "Кировский район (СПб)", ["стачек", "автово", "нарвск", "ветеранов", "ленинск", "дачн", "новаторов", "жукова", "трефолева", "ополчения", "кировск", "кронштадтск", "ульянк"]),
    "автово": (59.8670, 30.2615, 2000.0, "Автово / пр. Стачек (СПб)", ["автово", "стачек", "краснопутиловск", "автовск", "зайцева", "кронштадтск"]),
    "нарвская": (59.9010, 30.2750, 2000.0, "Нарвская застава / пл. Стачек (СПб)", ["стачек", "нарвск", "перекопск", "промышленн", "трефолева", "бумажн", "калинина", "обводн"]),
    "красносельский": (59.8350, 30.1550, 4200.0, "Красносельский район (СПб)", ["ветеранов", "ленинск", "жукова", "петергофск", "красносел", "балтийск", "героев", "казакова", "гаркавого", "десантников"]),
}

_STREET_QUERY_RE = re.compile(
    r"(?:^|[\s,])(?:улица|ул\.?|проспект|пр-т|пр\.?|переулок|пер\.?|набережная|наб\.?|шоссе|площадь|пл\.?|бульвар|б-р|аллея|линия|проезд)(?:$|[\s,.])|\b\d{1,4}\b"
)
_ZONE_STOPWORDS_RE = re.compile(r"(?:^|\s)(?:район|р-н|округ|метро|м\.)(?=\s|$)")


def match_city_zone(
    location_query: Optional[str],
    city=None,
) -> Optional[Tuple[float, float, float, str, List[str]]]:
    """Match an offline district or cluster without letting street names hijack into districts."""
    raw_norm = re.sub(r"\s+", " ", (location_query or "").strip().lower().replace("ё", "е"))
    if not raw_norm or _STREET_QUERY_RE.search(raw_norm):
        return None
    selected = get_city(city)
    zones = (
        MOSCOW_DISTRICTS_AND_ZONES
        if selected.key == "moscow"
        else (SPB_DISTRICTS_AND_ZONES if selected.key == "saint_petersburg" else {})
    )
    if not zones:
        return None

    direct = zones.get(raw_norm)
    if direct is not None:
        return direct

    q_clean = _ZONE_STOPWORDS_RE.sub(" ", raw_norm)
    q_clean = re.sub(r"\s+", " ", q_clean).strip()
    if not q_clean:
        return None

    stripped_hit = zones.get(q_clean)
    if stripped_hit is not None:
        return stripped_hit

    for key, zone in zones.items():
        key_norm = key.replace("ё", "е")
        if len(key_norm) < 4 or "район" in key_norm or "р-н" in key_norm:
            continue
        if re.search(rf"(?<![а-яa-z0-9]){re.escape(key_norm)}(?![а-яa-z0-9])", q_clean):
            return zone
    return None


@dataclass
class EvaluatedPropertyCandidate:
    property_data: Dict[str, Any]
    competition_report: RadiusCompetitionReport
    total_score: float
    verified_fns_revenue_mln_sum: float  # Revenue of matched legal entities, not local outlets.
    profitable_competitors_count: int
    price_sqm_month: float = 0.0
    direct_competitors_fns_revenue_mln_sum: float = 0.0
    direct_competitors_fns_revenue_mln_avg: float = 0.0
    direct_competitors_avg_rating: float = 0.0
    competitor_comparison_summary: str = ""
    pros: List[str] = field(default_factory=list)
    cons: List[str] = field(default_factory=list)
    verdict: str = ""
    data_warnings: List[str] = field(default_factory=list)
    floor_assessment: Optional[FloorAssessment] = None
    unrounded_total_score: float = 0.0
    score_model: str = 'explicit_criteria'
    score_upper_bound: Optional[float] = None
    score_coverage: float = 1.0
    scoring_components: Dict[str, Any] = field(default_factory=dict)
    input_score_lower_bound: Optional[float] = None
    input_score_upper_bound: Optional[float] = None
    business_model_diagnostics: Dict[str, Any] = field(default_factory=dict)
    confirmation: Dict[str,Any] = field(default_factory=dict)


@dataclass
class OptimalPropertySearchReport:
    target_business: str
    location_query: str
    resolved_location_name: str
    center_lat: float
    center_lng: float
    search_radius_m: float
    competition_diameter_m: float
    total_matching_in_db: int
    evaluated_count: int
    ranked_candidates: List[EvaluatedPropertyCandidate]
    best_candidate: Optional[EvaluatedPropertyCandidate]
    total_search_time_ms: float
    client_target_area_sqm: Optional[float] = None
    client_min_area_sqm: Optional[float] = None
    client_max_area_sqm: Optional[float] = None
    client_max_price_month: Optional[float] = None
    client_max_price_sqm_month: Optional[float] = None
    client_max_price_sqm_year: Optional[float] = None
    analysis_errors: List[Dict[str, Any]] = field(default_factory=list)
    floor_profile: Optional[BusinessFloorProfile] = None
    numerical_matching_count: int = 0
    floor_rejected_candidates: List[FloorCandidateReview] = field(default_factory=list)
    floor_verification_candidates: List[FloorCandidateReview] = field(default_factory=list)
    comparison_complete: bool = False
    data_complete: bool = False
    recommendation: Dict[str,Any] = field(default_factory=dict)
    deferred_candidate_bounds: List[Dict[str,Any]] = field(default_factory=list)
    unit_detail_enrichment: List[Dict[str,Any]] = field(default_factory=list)
    ranking_warnings: List[str] = field(default_factory=list)
    top_score_gap: Optional[float] = None
    weight_sensitive_challenger_ids: List[Any] = field(default_factory=list)
    ranking_profile: Optional[RankingProfile] = None
    input_sensitive_challenger_ids: List[Any] = field(default_factory=list)
    comparable_candidates: List[EvaluatedPropertyCandidate] = field(default_factory=list)
    model_sensitive_challenger_ids: List[Any] = field(default_factory=list)
    model_sensitivity: Dict[str,Any] = field(default_factory=dict)
    selection_status: str = 'no_candidates'
    missing_inputs: List[Dict[str, Any]] = field(default_factory=list)
    stage_timings_ms: Dict[str, float] = field(default_factory=dict)
    requirement_coverage: List[Dict[str, Any]] = field(default_factory=list)
    confirmation_status: str = 'requires_confirmation'
    confirmation_message: str = ''
    confirmation_rejected_candidates: List[Dict[str,Any]] = field(default_factory=list)
    next_actions: List[Dict[str,Any]] = field(default_factory=list)
    confirmation_execution: Dict[str,Any] = field(default_factory=dict)
    inventory_collection: Dict[str,Any] = field(default_factory=dict)
    include_unverified: bool = False
    unverified_evaluated_count: int = 0
    city: str = ''


class LocationResolver:
    @classmethod
    def resolve(
        cls,
        location_query: Optional[str],
        custom_radius_m: Optional[float] = None,
        proxy: Optional[str] = None,
        city=None,
    ) -> Tuple[float, float, float, str, List[str]]:
        selected = get_city(city)
        named_city = city_in_text(location_query)
        if named_city and named_city != selected:
            raise ValueError('Город запроса не совпадает с выбранным городом')
        q_clean = (location_query or selected.name).strip().lower().replace("ё", "е")
        if q_clean in (
            selected.name.casefold(), *selected.aliases, 'весь город', 'любой', 'все', 'all',
            'вся москва', 'весь санкт-петербург', 'весь петербург', 'весь питер', 'весь спб',
        ):
            lat, lng = selected.center
            rad, name, kw_list = selected.search_radius_m, selected.name, []
            return (
                lat,
                lng,
                custom_radius_m if custom_radius_m is not None else rad,
                name,
                kw_list,
            )
        zone_hit = match_city_zone(location_query, selected)
        if zone_hit is not None:
            lat, lng, rad, name, kw_list = zone_hit
            return (
                lat,
                lng,
                custom_radius_m if custom_radius_m is not None else rad,
                name,
                list(kw_list),
            )

        q_clean = re.sub(r"\b(?:район|р-н|округ|метро|м\.|улица|ул\.|проспект|пр-т)\b", " ", q_clean)
        q_clean = re.sub(r"\s+", " ", q_clean).strip()

        geo = cls._geocode_yandex_sync(location_query, proxy=proxy, city=selected)
        if not geo:
            geo = cls._geocode_nominatim_sync(location_query, proxy=proxy, city=selected)
        keywords = [w for w in re.split(r"[\s,.-]+", q_clean) if len(w) >= 4]
        if geo:
            lat, lng, title = geo
            if not selected.contains(lat, lng):
                raise ValueError('Геокодер вернул точку вне выбранного города')
            return (
                lat,
                lng,
                custom_radius_m if custom_radius_m is not None else 2000.0,
                f"{title} (по запросу «{location_query}»)",
                keywords,
            )

        raise ValueError(f"Не удалось определить территорию «{location_query}». Уточните адрес или координаты.")

    @staticmethod
    def _geocode_yandex_sync(query: str, proxy: Optional[str] = None, city=None) -> Optional[Tuple[float, float, str]]:
        selected = get_city(city)
        proxy_url = proxy or _DirectInterfaceBridge.get_proxy_url()
        proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        encoded_q = urllib.parse.quote(f"{selected.name}, {query}")
        url = f"{selected.maps_url}/search/{encoded_q}/"
        try:
            with curl_requests.Session(impersonate="chrome120", proxies=proxies, verify=False) as s:
                resp = s.get(url, timeout=10)
                if resp.status_code != 200:
                    return None
                m = re.search(r'<script type="application/json" class="state-view">(.*?)</script>', resp.text, re.DOTALL)
                if not m:
                    return None
                data = orjson.loads(m.group(1).encode("utf-8"))
                stack = data.get("stack") or []
                if not stack:
                    return None
                items = stack[0].get("results", {}).get("items") or []
                if not items:
                    return None
                first = items[0]
                coords = first.get("coordinates") or first.get("displayCoordinates")
                if isinstance(coords, list) and len(coords) >= 2:
                    lat, lng = float(coords[1]), float(coords[0])
                    if selected.contains(lat, lng):
                        return lat, lng, str(first.get("title") or query)
        except Exception as e:
            logger.debug(f"[LocationResolver] Geocode fallback error for '{query}': {e}")
        return None

    @staticmethod
    def _geocode_nominatim_sync(query, proxy=None, city=None):
        selected = get_city(city)
        station = bool(re.match(r'^\s*(?:метро\b|м\.)', query, re.I))
        cleaned = re.sub(r'^\s*(?:метро\b|м\.)\s*', '', query, flags=re.I)
        min_lat, max_lat, min_lng, max_lng = selected.bounds
        def _geo_base(name):
            s = str(name or '').casefold().replace('ё', 'е')
            s = re.sub(r'\b(?:улица|ул|проспект|пр-т|просп|бульвар|б-р|переулок|пер|набережная|наб|шоссе|ш|площадь|пл|проезд|пр-д|аллея|линия)\b\.?', '', s)
            return ' '.join(re.findall(r'[а-яa-z0-9]+', s))
        try:
            proxies = {'http': proxy, 'https': proxy} if proxy else None
            response = curl_requests.get('https://nominatim.openstreetmap.org/search',
                params=dict(q=f'{cleaned}, {selected.name}', format='jsonv2', addressdetails=1,
                    extratags=1, limit=20, countrycodes='ru', bounded=1,
                    viewbox=f'{min_lng},{max_lat},{max_lng},{min_lat}'),
                headers={'User-Agent': 'CommercialPremisesEngine/0.1 (city location resolution)'},
                proxies=proxies, timeout=12)
            response.raise_for_status()
            candidates = []
            for row in response.json():
                lat, lng = float(row['lat']), float(row['lon'])
                if not selected.contains(lat, lng):
                    continue
                if station and not (row.get('category', row.get('class')) == 'railway'
                        and row.get('type') == 'station' and row.get('extratags', {}).get('station') == 'subway'):
                    continue
                candidates.append((lat, lng, str(row.get('name') or row['display_name'])))
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                q_base = _geo_base(cleaned)
                bases = [_geo_base(c[2]) for c in candidates]
                same_name = [
                    c for c, b in zip(candidates, bases)
                    if b == bases[0] or (q_base and b == q_base)
                ]
                lat0, lng0 = same_name[0][0], same_name[0][1]
                nearby = [c for c in same_name if haversine_distance(lat0, lng0, c[0], c[1]) <= 15000.0]
                if len(nearby) == len(candidates) or (
                    q_base and bases[0] == q_base and len(nearby) >= max(1, len(candidates) // 2)
                ):
                    mid_lat = sum(c[0] for c in nearby) / len(nearby)
                    mid_lng = sum(c[1] for c in nearby) / len(nearby)
                    return min(nearby, key=lambda c: haversine_distance(mid_lat, mid_lng, c[0], c[1]))
            return None
        except Exception as exc:
            logger.debug('Nominatim city geocoding unavailable: %s', type(exc).__name__)
            return None


# Preserve public imports while callers migrate to the city-aware resolver.
MoscowLocationResolver = LocationResolver


class OptimalPropertyRecommender:
    """
    Перебирает и сравнивает помещения в выбранном пользователем районе/окружении под целевую нишу:
    - Фильтрует и отбирает разнообразные объекты в районе;
    - Анализирует для каждого объекта пешеходный трафик, почасовой ритм улицы, конкуренцию (AI) и выручку ФНС;
    - Сравнивает экономику самого помещения (аренда/м², цена за 1000 пешеходов, этаж, площадь) и выдает ТОП оптимальных вариантов.
    """

    def __init__(
        self,
        storage: PropertyStorage,
        competitor_analyzer: NvidiaCompetitorAnalyzer,
        proxy: Optional[str] = None,
        *, city=None,
    ):
        self.storage = storage
        self.competitor_analyzer = competitor_analyzer
        self.proxy = proxy
        self.city = get_city(city)
        self.floor_profile_resolver = BusinessFloorProfileResolver(competitor_analyzer,
            assessment_cache_path=getattr(competitor_analyzer, 'assessment_cache_path', None))
        self.ranking_profile_resolver = RankingProfileResolver(competitor_analyzer)

    @staticmethod
    def normalize_price_sqm(
        max_price_sqm: Optional[float] = None,
        max_price_sqm_year: Optional[float] = None,
        price_sqm_period: str = "month",
    ) -> Tuple[Optional[float], Optional[float]]:
        if price_sqm_period not in ("month", "year"):
            raise ValueError("Период ставки должен быть month или year")
        if max_price_sqm is not None and max_price_sqm_year is not None:
            raise ValueError("Укажите одну ставку за м² с явным периодом")
        val = max_price_sqm_year if max_price_sqm_year is not None else max_price_sqm
        if val is None:
            return None, None
        if not math.isfinite(val) or val <= 0:
            raise ValueError("Ставка за м² должна быть положительным конечным числом")
        if max_price_sqm_year is not None or price_sqm_period == "year":
            return val / 12.0, val
        return val, val * 12.0

    @staticmethod
    def _select_diverse_candidates(
        raw_candidates: List[Dict[str, Any]],
        max_candidates: int,
        ranking_profile: RankingProfile,
    ) -> List[Dict[str, Any]]:
        if not raw_candidates:
            return []

        # Only observable property terms can be used before environment
        # collection. No niche/centre preference is reintroduced here.
        concept_l = str(getattr(ranking_profile, "concept", "") or "").lower().replace("ё", "е")
        industrial_concept_re = re.compile(
            r"\b(?:склад\w*|производ\w*|цех\w*|ангар\w*|гараж\w*|бокс\w*|автосервис\w*|сто|шиномонтаж\w*|детейлинг\w*|мойк\w*|логист\w*|фулфилмент\w*|даркстор\w*|мастерск\w*|фабрик\w*|техцентр\w*)\b"
        )
        industrial_title_re = re.compile(
            r"\b(?:склад\w*|производств\w*|ангар\w*|промзон\w*|промбаз\w*|гараж\w*|бокс|цех\w*)\b"
        )
        commercial_override_re = re.compile(
            r"\b(?:псн|свободного назначения|торгов\w*|ритейл\w*|магазин\w*|кафе|ресторан\w*|общепит\w*|салон\w*|пвз|клиник\w*|офис\w*|стрит)\b"
        )
        penalize_warehouses = not bool(industrial_concept_re.search(concept_l))

        def concept_pre_rank(item):
            industrial_tier = 0
            if penalize_warehouses:
                title_l = str(item.get("title") or "").lower().replace("ё", "е")
                if industrial_title_re.search(title_l) and not commercial_override_re.search(title_l):
                    industrial_tier = 1
            terms=[]
            from .ranking_policy import utility
            for criterion in ranking_profile.criteria:
                if not ranking_profile.metadata.get('reference_scales',{}).get(criterion.metric,{}).get('usable',True):continue
                field={'rent_month':'price_month','rent_sqm_year':'price_sqm_year','area_sqm':'area_sqm'}.get(criterion.metric)
                value=item.get(field) if field else None
                if type(value) in (int,float) and math.isfinite(value):terms.append(criterion.weight*utility(value,criterion))
            return (industrial_tier, -sum(terms), str(item.get('source','')), str(item.get('external_id',item.get('id',''))))
        sorted_items=sorted(raw_candidates,key=concept_pre_rank)

        # Пространственная диверсификация: стараемся не брать два помещения в одном и том же здании (<45 м)
        selected: List[Dict[str, Any]] = []
        deferred: List[Dict[str, Any]] = []
        for item in sorted_items:
            lat, lng = float(item["lat"]), float(item["lng"])
            too_close = any(
                haversine_distance(lat, lng, float(s["lat"]), float(s["lng"])) < 45.0
                for s in selected
            )
            if not too_close:
                selected.append(item)
            else:
                deferred.append(item)
            if len(selected) >= max_candidates:
                break

        while len(selected) < max_candidates and deferred:
            selected.append(deferred.pop(0))

        return selected

    async def find_more_options(self, report, *, business_data=None):
        from .confirmation_flow import find_more
        return await find_more(self,report,business_data=business_data)

    async def continue_unverified_analysis(self, report, *, business_data=None):
        from .confirmation_flow import continue_unverified
        return await continue_unverified(self,report,business_data=business_data)

    async def supply_ranking_references(self,report,ranking_scales,*,business_data=None):
        from .confirmation_flow import run_continuation
        action=next((a for a in report.next_actions if a.get('type')=='provide_references'),None)
        if action is None:raise ValueError('Этот отчёт не запрашивает ориентиры оценки')
        original=getattr(report,'_continuation_business_data',None)
        if business_data is None and original is None and action['request'].get('business_data_required'):
            raise ValueError('Передайте исходные business_data: сведения о требованиях и маршрутах нужно сохранить')
        data=copy.deepcopy(business_data if business_data is not None else original or {'properties':{}})
        data['ranking_scales']=copy.deepcopy(ranking_scales)
        return await run_continuation(self,report,'provide_references',business_data=data)

    @with_confirmation
    async def find_optimal_properties(
        self,
        target_business: str,
        location_query: Optional[str] = None,
        center_lat: Optional[float] = None,
        center_lng: Optional[float] = None,
        search_radius_m: Optional[float] = None,
        target_area_sqm: Optional[float] = None,
        min_area_sqm: Optional[float] = None,
        max_area_sqm: Optional[float] = None,
        max_price_month: Optional[float] = None,
        max_price_sqm: Optional[float] = None,
        max_price_sqm_year: Optional[float] = None,
        max_candidates: int = 5,
        competition_diameter_m: float = 400.0,
        price_sqm_period: str = "month",
        business_format: Optional[str] = None,
        allowed_floors: Optional[Tuple[int, ...]] = None,
        analysis_limit: Optional[int] = None,
        max_listing_age_hours: Optional[float] = None,
        floor_timeout_s: float = 120,
        environment_timeout_s: float = 240,
        fns_timeout_s: float = 40,
        business_concept: Optional[str] = None,
        ranking_timeout_s: float = 90,
        business_data: Optional[Dict[str, Any]] = None,
        exclude_property_keys: Optional[List[str]] = None,
        confirmation_timeout_s: float = 45,
        include_unverified: bool = False,
        city: Optional[str] = None,
    ) -> OptimalPropertySearchReport:
        """Compare all eligible listings; max_candidates limits displayed results only.

        analysis_limit explicitly opts into an incomplete, faster comparison.
        """
        t0 = time.perf_counter()
        stage_timings={}
        if city is not None and get_city(city) != self.city:
            raise ValueError('Город сохранённого запроса не совпадает с городом клиента')
        named_city = city_in_text(location_query)
        if named_city and named_city != self.city:
            raise ValueError('Город запроса не совпадает с городом клиента')
        if type(include_unverified) is not bool:
            raise ValueError('include_unverified должен быть логическим значением')
        concept = require_business_concept(business_concept, business_format)
        if business_data is not None:
            validate_business_inputs(business_data)
            if business_data.get('ranking_scales'):
                from .business_model import concept_digest
                if business_data['ranking_scales']['concept_sha256']!=concept_digest(concept):
                    raise ValueError('Шкалы оценки относятся к другой концепции')
        if exclude_property_keys is not None and (not isinstance(exclude_property_keys,(list,tuple)) or
                any(not isinstance(key,str) or not key for key in exclude_property_keys)):
            raise ValueError('exclude_property_keys: нужен список непустых ключей помещений')
        for budget in (floor_timeout_s,environment_timeout_s,fns_timeout_s,ranking_timeout_s,confirmation_timeout_s):
            if type(budget) not in (int,float) or not math.isfinite(budget) or budget <= 0:
                raise ValueError('Лимит времени должен быть положительным конечным числом')
        for name, value in (
            ("search_radius_m", search_radius_m), ("target_area_sqm", target_area_sqm),
            ("min_area_sqm", min_area_sqm), ("max_area_sqm", max_area_sqm),
            ("max_price_month", max_price_month), ("competition_diameter_m", competition_diameter_m),
            ("max_listing_age_hours", max_listing_age_hours),
            ("floor_timeout_s", floor_timeout_s), ("environment_timeout_s", environment_timeout_s), ("fns_timeout_s", fns_timeout_s),
        ):
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} должен быть положительным конечным числом")
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or max_candidates < 1:
            raise ValueError("max_candidates должен быть не меньше 1")
        if analysis_limit is not None and (isinstance(analysis_limit, bool) or not isinstance(analysis_limit, int) or analysis_limit < 1):
            raise ValueError("analysis_limit должен быть целым числом не меньше 1")
        if min_area_sqm is not None and max_area_sqm is not None and min_area_sqm > max_area_sqm:
            raise ValueError("Минимальная площадь превышает максимальную")
        if (center_lat is None) != (center_lng is None):
            raise ValueError("Укажите обе координаты центра")
        if center_lat is not None and (
            not math.isfinite(center_lat) or not math.isfinite(center_lng)
            or not -90 < center_lat < 90 or not -180 <= center_lng <= 180
        ):
            raise ValueError("Некорректные координаты центра")
        if center_lat is not None and not self.city.contains(center_lat, center_lng):
            raise ValueError('Центр поиска вне выбранного города клиента')

        norm_sqm_month, norm_sqm_year = self.normalize_price_sqm(
            max_price_sqm=max_price_sqm,
            max_price_sqm_year=max_price_sqm_year,
            price_sqm_period=price_sqm_period,
        )

        eff_min_area = min_area_sqm
        eff_max_area = max_area_sqm
        if target_area_sqm is not None and target_area_sqm > 0:
            if eff_min_area is None:
                eff_min_area = round(max(8.0, target_area_sqm * 0.65), 1)
            if eff_max_area is None:
                eff_max_area = round(target_area_sqm * 1.35, 1)

        location_query = location_query or self.city.name
        if center_lat is not None and center_lng is not None:
            res_lat, res_lng = center_lat, center_lng
            res_rad = search_radius_m if search_radius_m is not None else 2500.0
            res_name = f"{location_query} ({res_lat:.4f}, {res_lng:.4f})"
            keywords: List[str] = []
        else:
            loop = asyncio.get_event_loop()
            res_lat, res_lng, res_rad, res_name, keywords = await loop.run_in_executor(
                None,
                LocationResolver.resolve,
                location_query,
                search_radius_m,
                self.proxy,
                self.city,
            )

        raw_candidates = self.storage.find_candidates(
            target_lat=res_lat,
            target_lng=res_lng,
            radius_meters=res_rad,
            address_keywords=keywords,
            min_area_sqm=eff_min_area,
            max_area_sqm=eff_max_area,
            max_price_month=max_price_month,
            max_price_sqm_year=norm_sqm_year,
            max_listing_age_hours=max_listing_age_hours,
        )

        from .confirmation_flow import property_keys
        excluded=set(exclude_property_keys or ())
        raw_candidates=[p for p in raw_candidates if not excluded.intersection(property_keys(p))]
        from .listing_enrichment import recover_catalogue_details,enrich_missing_unit_details
        raw_candidates=[recover_catalogue_details(p) for p in raw_candidates]
        if business_data is not None:raw_candidates=attach_business_inputs(raw_candidates,business_data)
        numerical_matching = len(raw_candidates)
        search_constraints=dict(target_area_sqm=target_area_sqm,min_area_sqm=eff_min_area,max_area_sqm=eff_max_area,
            max_price_month=max_price_month,max_price_sqm_year=norm_sqm_year,search_radius_m=res_rad,
            center_lat=res_lat,center_lng=res_lng,location_query=location_query,
            competition_diameter_m=competition_diameter_m,explicit_visit_context=business_format)
        ranking_profile=None
        if raw_candidates:
            stage_started=time.perf_counter()
            ranking_profile=await self.ranking_profile_resolver.resolve(target_business,concept,
                search_constraints,timeout_s=ranking_timeout_s)
            stage_timings['concept_profile']=round((time.perf_counter()-stage_started)*1000,1)
            if ranking_profile.status!='ready':
                return OptimalPropertySearchReport(target_business=target_business,location_query=location_query,
                    resolved_location_name=res_name,center_lat=res_lat,center_lng=res_lng,search_radius_m=res_rad,
                    competition_diameter_m=competition_diameter_m,total_matching_in_db=0,evaluated_count=0,
                    ranked_candidates=[],best_candidate=None,total_search_time_ms=(time.perf_counter()-t0)*1000,
                    numerical_matching_count=numerical_matching,ranking_profile=ranking_profile,
                    client_target_area_sqm=target_area_sqm,client_min_area_sqm=eff_min_area,client_max_area_sqm=eff_max_area,
                    client_max_price_month=max_price_month,client_max_price_sqm_month=norm_sqm_month,client_max_price_sqm_year=norm_sqm_year,
                    ranking_warnings=[ranking_profile.summary,*ranking_profile.questions],comparison_complete=False,
                    selection_status='needs_profile',stage_timings_ms=stage_timings)
            # Scales are independent of observations, so bind them before any
            # optional shortlist too. AI placeholders must not rank that list.
            ranking_profile=bind_reference_scales(ranking_profile, ranking_scales=business_data.get('ranking_scales') if business_data else None)
            missing_references=[dict(metric=metric,reason='missing_scale',
                action=('Укажите комфортные и предельные полные ежемесячные расходы на помещение; бюджет аренды не заменяет этот ориентир'
                    if metric=='premises_cost_month' else 'Укажите желаемый и предельный ориентир: '+METRICS[metric][0]))
                for metric,scale in ranking_profile.metadata.get('reference_scales',{}).items() if not scale['usable']]
            if missing_references:
                return OptimalPropertySearchReport(target_business=target_business,location_query=location_query,
                    resolved_location_name=res_name,center_lat=res_lat,center_lng=res_lng,search_radius_m=res_rad,
                    competition_diameter_m=competition_diameter_m,total_matching_in_db=0,evaluated_count=0,
                    ranked_candidates=[],best_candidate=None,total_search_time_ms=(time.perf_counter()-t0)*1000,
                    numerical_matching_count=numerical_matching,ranking_profile=ranking_profile,
                    client_target_area_sqm=target_area_sqm,client_min_area_sqm=eff_min_area,client_max_area_sqm=eff_max_area,
                    client_max_price_month=max_price_month,client_max_price_sqm_month=norm_sqm_month,client_max_price_sqm_year=norm_sqm_year,
                    missing_inputs=missing_references,selection_status='needs_data',stage_timings_ms=stage_timings,
                    ranking_warnings=['До проверки помещений нужны ориентиры клиента для денежной или размерной шкалы оценки'])
        stage_started=time.perf_counter()
        scenario=ranking_profile.metadata.get('scenario_contract') if ranking_profile else None
        floor_context=(business_format if business_format and business_concept else '; '.join(ranking_profile.visit_context_quotes) if ranking_profile else business_format)
        floor_requirements=None
        if scenario:
            # Context informs universal access checks; only premises-specific
            # obligations need a separate proof from the advertisement.
            floor_context='; '.join(dict.fromkeys(item['quote'] for name in ('context','premises_requirements','waivers') for item in scenario[name]))
            # A criterion's cost/area preference is not an access obligation,
            # even if the role interpreter labels that exact scoring quote as
            # a property requirement. Mixed clauses retain their exact access
            # substring, rather than dropping the whole source fragment.
            floor_requirements,excluded=access_requirement_quotes(scenario,ranking_profile.criteria,{k:v[1] for k,v in METRICS.items()})
            ranking_profile.metadata['excluded_scoring_quotes_from_access']=excluded
        floor_profile = await self.floor_profile_resolver.resolve(
            target_business, floor_context, allowed_floors,
            requirement_quotes=floor_requirements,
            context_quotes=tuple(item['quote'] for item in scenario['context']) if scenario else (),
            waiver_quotes=tuple(item['quote'] for item in scenario['waivers']) if scenario else (),
        )
        floor_rejected = []
        floor_verification = []
        eligible_candidates = []
        detail_started=time.perf_counter()
        raw_candidates,detail_records=await enrich_missing_unit_details(raw_candidates,self.competitor_analyzer,
            timeout_s=min(20,confirmation_timeout_s))
        stage_timings['unit_details']=round((time.perf_counter()-detail_started)*1000,1)
        from .listing_enrichment import numerical_violations
        current_violations={floor_candidate_key(p):numerical_violations(p,search_constraints) for p in raw_candidates}
        non_violating=[p for p in raw_candidates if not current_violations[floor_candidate_key(p)]]
        logger.info("Фильтры пройдены: %d лотов (%d без числовых нарушений). Запуск проверки этажности и допуска...",
                    len(raw_candidates), len(non_violating))
        assess_targets = non_violating
        skipped_ai_props = []
        if analysis_limit is not None and floor_profile.status == "ready" and ranking_profile is not None:
            precheck_map = {floor_candidate_key(p): _floor_precheck(p, floor_profile) for p in non_violating}
            pending_ai = [p for p in non_violating if precheck_map[floor_candidate_key(p)] is None]
            ai_cap = max(analysis_limit * 2 + 4, analysis_limit + 6)
            if len(pending_ai) > ai_cap:
                chosen_ai = self._select_diverse_candidates(
                    raw_candidates=pending_ai,
                    max_candidates=ai_cap,
                    ranking_profile=ranking_profile,
                )
                chosen_keys = {floor_candidate_key(p) for p in chosen_ai}
                skipped_ai_props = [p for p in pending_ai if floor_candidate_key(p) not in chosen_keys]
                assess_targets = [
                    p for p in non_violating
                    if precheck_map[floor_candidate_key(p)] is not None or floor_candidate_key(p) in chosen_keys
                ]
                logger.info("Ограниченная выборка (analysis_limit=%d): ИИ-проверка допуска для %d из %d кандидатов",
                            analysis_limit, len(chosen_ai), len(pending_ai))
        floor_assessments = await self.floor_profile_resolver.assess_many(
            assess_targets, floor_profile, timeout_s=floor_timeout_s)
        for skipped_prop in skipped_ai_props:
            floor_assessments[floor_candidate_key(skipped_prop)] = _unverified(
                skipped_prop,
                "Контекстная проверка ещё не запускалась (ограничение анализа)",
            )
        for prop in raw_candidates:
            violations=current_violations[floor_candidate_key(prop)]
            if violations:
                assessment=FloorAssessment('rejected',(),'Актуальные условия источника выходят за числовые ограничения клиента','current_unit_source',
                    analysis_status='valid',inference_metadata={'current_filter_violations':violations})
                floor_assessments[floor_candidate_key(prop)]=assessment
            else:assessment = floor_assessments[floor_candidate_key(prop)]
            if ranking_profile:
                mandatory=assess_mandatory_requirements(prop,ranking_profile.concept)
                if mandatory['checks'] or mandatory.get('error'):
                    assessment.inference_metadata['mandatory_requirements']=mandatory
                if mandatory['status']=='rejected':
                    assessment.status='rejected';assessment.reason='Не выполнено обязательное требование клиента к помещению/маршруту'
                elif mandatory['status']=='needs_verification' and assessment.status!='rejected':
                    assessment.status='needs_verification';assessment.reason='Нужно проверить обязательное требование клиента к помещению/маршруту'
                    assessment.missing_facts.extend(c['concept_quote'] for c in mandatory['checks'] if c['result']=='unknown')
                    if mandatory.get('error'):assessment.missing_facts.append(mandatory['error'])
            from .business_model import rent_is_lower_bound
            if assessment.status!='rejected' and max_price_month is not None and rent_is_lower_bound(prop):
                assessment.status='needs_verification'
                assessment.reason='Окончательная аренда неизвестна: опубликована только нижняя граница ставки'
                assessment.missing_facts.append('Подтвердить окончательную аренду в пределах бюджета клиента')
            if assessment.status == "eligible":
                eligible_candidates.append(prop)
            elif assessment.status == "rejected":
                floor_rejected.append(FloorCandidateReview(prop, assessment))
            else:
                floor_verification.append(FloorCandidateReview(prop, assessment))
        stage_timings['mandatory_checks']=round((time.perf_counter()-stage_started)*1000,1)
        comparison_pool=eligible_candidates+([r.property_data for r in floor_verification] if include_unverified else [])
        total_matching = len(comparison_pool)
        candidates_to_eval = comparison_pool if analysis_limit is None or analysis_limit >= total_matching else self._select_diverse_candidates(
            raw_candidates=comparison_pool,
            max_candidates=analysis_limit,
            ranking_profile=ranking_profile,
        )
        logger.info("Допуск проверен: eligible=%d, verification=%d, rejected=%d. Анализ окружения для %d объектов...",
                    len(eligible_candidates), len(floor_verification), len(floor_rejected), len(candidates_to_eval))

        if not candidates_to_eval:
            return OptimalPropertySearchReport(
                target_business=target_business,
                location_query=location_query,
                resolved_location_name=res_name,
                center_lat=res_lat,
                center_lng=res_lng,
                search_radius_m=res_rad,
                competition_diameter_m=competition_diameter_m,
                total_matching_in_db=0,
                evaluated_count=0,
                ranked_candidates=[],
                best_candidate=None,
                total_search_time_ms=round((time.perf_counter() - t0) * 1000.0, 1),unit_detail_enrichment=detail_records,
                client_target_area_sqm=target_area_sqm,
                client_min_area_sqm=eff_min_area,
                client_max_area_sqm=eff_max_area,
                client_max_price_month=max_price_month,
                client_max_price_sqm_month=norm_sqm_month,
                client_max_price_sqm_year=norm_sqm_year,
                floor_profile=floor_profile,
                ranking_profile=ranking_profile,
                numerical_matching_count=numerical_matching,
                floor_rejected_candidates=floor_rejected,
                floor_verification_candidates=floor_verification,
                comparison_complete=not floor_verification,
                selection_status='needs_verification' if floor_verification else 'no_candidates',
                stage_timings_ms=stage_timings,
                ranking_warnings=(["Есть помещения с неуточнённым этажом или доступом; они ещё не сравнивались"] if floor_verification else []),
            )

        from .confirmation_flow import start_confirmation_prefetch
        start_confirmation_prefetch(candidates_to_eval,max_candidates,dict(
            min_area_sqm=eff_min_area,max_area_sqm=eff_max_area,
            max_price_month=max_price_month,max_price_sqm_year=norm_sqm_year),ranking_profile)

        # Card acquisition owns its two-circle limit. A long AI request must
        # not prevent another premise from acquiring its source data.
        from .ai_contract import AI_DEADLINE
        environment_ai_deadline=time.monotonic()+max(.001,environment_timeout_s-min(5,environment_timeout_s*.1))

        async def _eval_one(prop_dict: Dict[str, Any]) -> Tuple[Dict[str, Any], RadiusCompetitionReport]:
            token=AI_DEADLINE.set(environment_ai_deadline)
            try:
                report_task = self.competitor_analyzer.analyze_circle(
                    center_lat=float(prop_dict["lat"]),
                    center_lng=float(prop_dict["lng"]),
                    diameter_m=competition_diameter_m,
                    target_business=target_business,
                    enrich_fns=False,
                    business_concept=concept,
                )
                provider=getattr(self.competitor_analyzer,'pedestrian_provider',None)
                if provider is not None:
                    rep,automatic=await asyncio.gather(report_task,provider.enrich(prop_dict))
                    from .osm_access import attach_automatic_access
                    prop_dict=attach_automatic_access(prop_dict,automatic)
                else:rep=await report_task
                from .business_model import business_evidence
                from .route_audience import condition_transport_on_routes
                rep=copy.copy(rep)
                rep.foot_traffic=condition_transport_on_routes(rep.foot_traffic,business_evidence(prop_dict))
                return prop_dict, rep
            finally:AI_DEADLINE.reset(token)

        stage_started=time.perf_counter()
        tasks=[asyncio.create_task(_eval_one(p)) for p in candidates_to_eval]
        try:
            if tasks:
                _,unfinished=await asyncio.wait(tasks,timeout=environment_timeout_s)
                for task in unfinished:task.cancel()
                outcomes=await asyncio.gather(*tasks,return_exceptions=True)
                outcomes=[TimeoutError('Environment analysis budget exceeded') if task in unfinished else outcome
                          for task,outcome in zip(tasks,outcomes)]
            else:outcomes=[]
        finally:
            for task in tasks:
                if not task.done():task.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        stage_timings['environment']=round((time.perf_counter()-stage_started)*1000,1)
        pairs = []
        analysis_errors = []
        for prop, outcome in zip(candidates_to_eval, outcomes):
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                logger.warning("Analysis failed for property %s (%s)", prop["id"], type(outcome).__name__)
                analysis_errors.append({"property_id": prop["id"], "message": "Истёк лимит времени анализа окружения" if isinstance(outcome,TimeoutError) else "Анализ окружения недоступен"})
            else:
                pairs.append(outcome)

        # 2. Единое пакетное обогащение всех релевантных конкурентов и генераторов трафика через DaData + ФНС ГИР БО
        all_relevant_cards = []
        for _, rep in pairs:
            for comp in rep.direct_competitors + rep.indirect_competitors + rep.synergy_businesses:
                all_relevant_cards.append(comp.card)

        stage_started=time.perf_counter()
        if all_relevant_cards:
            t_fns = time.perf_counter()
            try:
                await asyncio.wait_for(self.competitor_analyzer.fns_collector.enrich_cards(all_relevant_cards),timeout=fns_timeout_s)
            except Exception:
                logger.warning("Shared FNS enrichment unavailable")
                for _, rep in pairs:
                    rep.data_warnings.append("Финансовое обогащение недоступно")
            shared_fns_ms = round((time.perf_counter() - t_fns) * 1000.0, 1)
            for _, rep in pairs:
                rep_cards = [
                    c.card for c in (rep.direct_competitors + rep.indirect_competitors + rep.synergy_businesses)
                ]
                rep.fns_enriched_count = sum(1 for c in rep_cards if c.fns_inn)
                rep.fns_fetch_time_ms = shared_fns_ms

        stage_timings['company_accounts']=round((time.perf_counter()-stage_started)*1000,1)
        stage_started=time.perf_counter()
        evaluation_context={}
        evaluated = self._score_and_rank_candidates(
            pairs=pairs,
            floor_profile=floor_profile,
            floor_assessments=floor_assessments,
            ranking_profile=ranking_profile,
            evaluation_context=evaluation_context,
        )

        stage_timings['scoring']=round((time.perf_counter()-stage_started)*1000,1)
        ranking_warnings = []
        missing_inputs=[]
        requirement_coverage=[]
        input_sensitive_ids=[]
        ranking_warnings.append('Приоритеты выведены ИИ из концепции; шкалы не зависят от других лотов. Модель не откалибрована на результатах бизнеса')
        if ranking_profile.metadata.get('market_context'):
            ranking_warnings.append('Индекс окружения включает проходимость, аудиторию, прямые/косвенные альтернативы, синергию, отзывы и ФНС. Внутренние опорные уровни — явные сценарные допущения, не измеренные рыночные ориентиры')
        for metric,scale in ranking_profile.metadata.get('reference_scales',{}).items():
            if not scale['usable']:
                missing_inputs.append(dict(metric=metric,reason='missing_scale',
                    action=('Укажите комфортные и предельные общие расходы на помещение в месяц, включая эксплуатацию; бюджет одной аренды не задаёт полный бюджет'
                        if metric=='premises_cost_month' else f'Укажите желаемое и предельное значение показателя: {METRICS[metric][0]}')))
        for item in evaluated:
            cost_metrics={'premises_cost_month','net_contribution_month'}&set(item.scoring_components)
            if cost_metrics:
                for gap in item.business_model_diagnostics.get('cost',{}).get('missing_inputs',[]):
                    missing_inputs.append(dict(property_id=item.property_data['id'],metric=sorted(cost_metrics)[0],
                        reason='missing_cost_input',**gap))
            context_missing=item.business_model_diagnostics.get('market_context',{}).get('missing_inputs',[])
            for source in context_missing:
                from .data_completeness import market_input_action
                missing_inputs.append(dict(property_id=item.property_data['id'],metric='market_fit_index',
                    reason='missing_market_input',source=source,action=market_input_action(source)))
            for metric,part in item.scoring_components.items():
                if part['value'] is None and not any(row.get('metric')==metric and
                        (row.get('property_id')==item.property_data['id'] or row.get('reason')=='missing_scale')
                        for row in missing_inputs):
                    missing_inputs.append(dict(property_id=item.property_data['id'],metric=metric,reason='missing_value',
                                              action='Уточните исходные значения/границы и источник'))
        if any(x['reason']=='missing_scale' for x in missing_inputs):
            ranking_warnings.append('Для части показателей нет ориентиров клиента: их вклад остаётся диапазоном, минимум/максимум выборки не подставляются')
        requirement_coverage=assess_requirement_coverage(ranking_profile,evaluated,search_constraints)
        for aspect in requirement_coverage:
            quote=aspect['concept_quote']
            if aspect['status']=='scoring_conflict':
                ranking_warnings.append('ИИ одновременно отнёс фрагмент к критерию и неучтённым аспектам; полнота учёта требует уточнения: '+quote)
                missing_inputs.append(dict(reason='modeling_conflict',concept_quote=quote,metrics=aspect['metrics'],
                                           action='Уточнить полноту учёта фрагмента концепции: '+quote))
            elif aspect['status']=='unmodeled':
                ranking_warnings.append('Пока не учтён аспект концепции: '+quote)
                missing_inputs.append(dict(reason='unsupported_requirement',concept_quote=quote,
                                           action='Проверить отдельно требование клиента: '+quote))
        if any(c.score_coverage<1-1e-9 for c in evaluated):
            ranking_warnings.append('Не все значимые показатели известны: указаны границы оценки, веса при пропусках не перераспределяются')
        if evaluated:
            input_sensitive_ids=[c.property_data['id'] for c in evaluated[1:]
                if evaluated[0].input_score_lower_bound<=c.input_score_upper_bound]
            if input_sensitive_ids:ranking_warnings.append('Лидер может измениться при изменении исходных диапазонов ±20%: аудитории, конверсии, маршрутов и расходов; это сценарий чувствительности, не статистическая вероятность')
            if any(evaluated[0].unrounded_total_score<=c.score_upper_bound for c in evaluated[1:]):
                ranking_warnings.append('Диапазоны оценки кандидатов пересекаются; единственный достоверный лидер не установлен')
        comparison_complete = len(evaluated) == total_matching and not floor_verification
        if any(a['status'] not in ('checked_before_rating','waived_by_client','client_managed','checked_by_filters','scenario_context','verification_pending') for a in requirement_coverage):comparison_complete=False
        if len(candidates_to_eval) < total_matching:
            ranking_warnings.append(f"Ускоренный режим: выбрано для анализа {len(candidates_to_eval)} из {total_matching}; лучший объект может быть пропущен")
        if analysis_errors:
            ranking_warnings.append("Не все помещения удалось проанализировать; итоговое сравнение неполное")
        evaluated_keys={floor_candidate_key(c.property_data) for c in evaluated}
        remaining_verification=[r for r in floor_verification if floor_candidate_key(r.property_data) not in evaluated_keys]
        unverified_evaluated=sum(c.floor_assessment is not None and c.floor_assessment.status=='needs_verification' for c in evaluated)
        if unverified_evaluated:
            ranking_warnings.append('По выбору клиента в предварительный рейтинг включены варианты с неподтверждёнными обязательными условиями; пригодность не подтверждена')
        if remaining_verification:
            ranking_warnings.append("Есть помещения с неуточнённым этажом или доступом; они ещё не сравнивались")
        if any(c.competition_report.data_status != "ok" or not c.competition_report.total_cards_found for c in evaluated):
            ranking_warnings.append('Данные окружения неполные; пропуски значимых показателей отражены в границах оценки')
        score_gap = (evaluated[0].unrounded_total_score - evaluated[1].unrounded_total_score) if len(evaluated) > 1 else None
        weight_sensitive_ids = self._weight_sensitive_challengers(evaluated)
        if weight_sensitive_ids:
            ranking_warnings.append("Лидер может измениться при изменении весов оценки на ±10%; текущий порядок неустойчив")
        elif score_gap is not None and round(evaluated[0].total_score, 1) == round(evaluated[1].total_score, 1):
            ranking_warnings.append("У лидеров совпадают отображаемые баллы; порядок рассчитан до округления")
        ranking_warnings.append("Баллы отражают эвристическую модель, а не доказанную доходность или вероятность успеха бизнеса")

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        from .decision_policy import blocking_inputs
        selection_status=('needs_data' if not evaluated and analysis_errors else 'no_candidates' if not evaluated else 'needs_data' if blocking_inputs(missing_inputs) or not comparison_complete
                          else 'comparable' if input_sensitive_ids or weight_sensitive_ids else 'preliminary_leader')
        result=OptimalPropertySearchReport(
            target_business=target_business,
            location_query=location_query,
            resolved_location_name=res_name,
            center_lat=res_lat,
            center_lng=res_lng,
            search_radius_m=res_rad,
            competition_diameter_m=competition_diameter_m,
            total_matching_in_db=total_matching,
            evaluated_count=len(evaluated),
            ranked_candidates=evaluated[:max_candidates],
            best_candidate=evaluated[0] if evaluated else None,
            total_search_time_ms=elapsed_ms,
            client_target_area_sqm=target_area_sqm,
            client_min_area_sqm=eff_min_area,
            client_max_area_sqm=eff_max_area,
            client_max_price_month=max_price_month,
            client_max_price_sqm_month=norm_sqm_month,
            client_max_price_sqm_year=norm_sqm_year,
            analysis_errors=analysis_errors,
            floor_profile=floor_profile,
            numerical_matching_count=numerical_matching,
            floor_rejected_candidates=floor_rejected,
            floor_verification_candidates=remaining_verification,
            comparison_complete=comparison_complete,
            data_complete=bool(evaluated) and not missing_inputs and all(c.score_coverage>=1-1e-9 for c in evaluated),
            ranking_warnings=ranking_warnings,
            top_score_gap=score_gap,
            weight_sensitive_challenger_ids=weight_sensitive_ids,
            ranking_profile=ranking_profile,
            input_sensitive_challenger_ids=input_sensitive_ids,
            comparable_candidates=([evaluated[0],*[c for c in evaluated[1:] if c.property_data['id'] in set(input_sensitive_ids+weight_sensitive_ids)]][:5]
                if input_sensitive_ids or weight_sensitive_ids else []),
            selection_status=selection_status,missing_inputs=missing_inputs,stage_timings_ms=stage_timings,
            requirement_coverage=requirement_coverage,
            unit_detail_enrichment=detail_records,
            include_unverified=include_unverified,unverified_evaluated_count=unverified_evaluated,
        )
        # Keep already analyzed alternatives for source rejection/backfill.
        # This in-process pool is deliberately absent from serialized UI data.
        result._evaluated_candidates=evaluated
        result._evaluation_complete=len(evaluated)==total_matching and not analysis_errors
        return result

    @staticmethod
    def _weight_sensitive_challengers(ranked, fraction=0.1):
        """Exact sign test over the box of independent weight perturbations.

        This is a model sensitivity check, not a statistical confidence interval.
        Normalizing positive weights does not change the sign of a score gap.
        """
        if len(ranked) < 2:
            return []
        sensitive = []
        for candidate in ranked[1:]:
            contributions=[part['contribution_lower']-candidate.scoring_components[key]['contribution_lower']
                           for key,part in ranked[0].scoring_components.items()]
            if sum(contributions) - fraction*sum(abs(v) for v in contributions) <= 0:
                sensitive.append(candidate.property_data.get("id"))
        return sensitive

    @staticmethod
    def _score_and_rank_candidates(pairs, ranking_profile, floor_profile=None, floor_assessments=None, evaluation_context=None):
        context={} if evaluation_context is None else evaluation_context
        profile=bind_reference_scales(ranking_profile)
        floor_assessments=floor_assessments or {}
        results=[]
        for prop,rep in pairs:
            score=evaluate_profile(profile,prop,rep,evaluation_context=context)
            floor=floor_assessments.get(floor_candidate_key(prop)) or assess_floor(prop,floor_profile or BusinessFloorProfile())
            warnings=list(dict.fromkeys([*rep.data_warnings,*floor.warnings,
                'Показатели окружения модельные; реальный спрос и прибыльность не измерены',
                *score['business_model'].get('warnings',[]),
                *([f"Недостающие данные или шкалы: {', '.join(k for k,v in score['components'].items() if v['missing_reason'])}"] if score['coverage']<1-1e-9 else [])]))
            if rep.population:warnings.extend(rep.population.data_warnings)
            if prop.get('last_seen_at'):warnings.append(f"Объявление получено {prop['last_seen_at']} UTC; текущую доступность нужно уточнить")
            else:warnings.append('Время последнего получения объявления неизвестно')
            explanations=explain_components(score['components'])
            pros=explanations['pros'];cons=explanations['cons'];warnings.extend(explanations['uncertain'])
            market=score['business_model'].get('market_context',{})
            from .decision_policy import ranking_cost_scope
            score['business_model']['ranking_cost_scope']=ranking_cost_scope(ranking_profile)
            rents=float(prop.get('price_month') or 0);area=float(prop.get('area_sqm') or 1)
            cards=[c.card for c in rep.direct_competitors+rep.indirect_competitors];seen=set();revenue=0;profitable=0
            for card in cards:
                if card.fns_inn and card.fns_inn not in seen:
                    seen.add(card.fns_inn);revenue+=max(0,card.fns_revenue_rub or 0);profitable+=bool(card.fns_net_profit_rub and card.fns_net_profit_rub>0)
            direct_revenue=[];direct_inns=set();ratings=[]
            for comp in rep.direct_competitors:
                card=comp.card
                if card.rating and card.rating>0:ratings.append(card.rating)
                if card.fns_inn and card.fns_inn not in direct_inns:
                    direct_inns.add(card.fns_inn)
                    if card.fns_revenue_rub and card.fns_revenue_rub>0:direct_revenue.append(card.fns_revenue_rub/1e6)
            results.append(EvaluatedPropertyCandidate(property_data=prop,competition_report=rep,
                total_score=round(score['lower'],1),unrounded_total_score=score['lower'],score_upper_bound=score['upper'],
                score_model='concept_v2_all_data' if market else 'explicit_criteria',score_coverage=score['coverage'],scoring_components=score['components'],
                input_score_lower_bound=score['input_lower'],input_score_upper_bound=score['input_upper'],
                business_model_diagnostics=score['business_model'],
                verified_fns_revenue_mln_sum=revenue/1e6,profitable_competitors_count=profitable,price_sqm_month=rents/max(1,area),
                direct_competitors_fns_revenue_mln_sum=round(sum(direct_revenue),1),
                direct_competitors_fns_revenue_mln_avg=round(statistics.mean(direct_revenue),1) if direct_revenue else 0,
                direct_competitors_avg_rating=round(statistics.mean(ratings),2) if ratings else 0,
                competitor_comparison_summary=(
                    f'Найдено прямых конкурентов: {len(rep.direct_competitors)}, '
                    f'косвенных: {len(rep.indirect_competitors)}, '
                    f'синергия: {len(rep.synergy_businesses)}'
                    if len(rep.direct_competitors) > 0
                    else 'Прямых конкурентов рядом не найдено; отсутствие конкурентов не подтверждает спрос'
                ),
                pros=pros,cons=cons,verdict='Предварительная оценка по концепции; требуется проверка исходных данных',
                data_warnings=list(dict.fromkeys(warnings)),floor_assessment=floor))
        # Equal conservative bounds are not a reason to prefer a portal's name.
        # Use the known monetary lower bound only when cost is the uniquely
        # highest priority. Unknown supplements stay unknown, never become zero.
        highest=max((criterion.weight for criterion in ranking_profile.criteria),default=0)
        primary=[criterion for criterion in ranking_profile.criteria if criterion.weight==highest]
        cost_first=len(primary)==1 and primary[0].metric in ('premises_cost_month','rent_month','rent_sqm_year') and primary[0].preference=='lower'
        def order(candidate):
            tie=math.inf
            if cost_first:
                metric=primary[0].metric
                component=candidate.scoring_components[metric]
                value=component.get('value_lower')
                if type(value) in (int,float) and math.isfinite(value):
                    tie=value
                    candidate.business_model_diagnostics['ordering_basis']=dict(
                        metric=metric,value=value,kind='known_cost_lower_bound',
                        provisional=component.get('value_upper') is None or component['value_upper']!=value,
                        reason='При равных нижних баллах показан меньший известный минимум расходов; неизвестные доплаты не учтены как ноль')
            return (-candidate.unrounded_total_score,tie,str(candidate.property_data.get('source','')),str(candidate.property_data.get('external_id',candidate.property_data.get('id',''))))
        results.sort(key=order)
        return results
