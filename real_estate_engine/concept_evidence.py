"""Literal client evidence for source-specific waivers and clock windows.

Vocabulary identifies measurement subjects, never niches or allowed premises.
Unrecognized exclusions remain unconfirmed rather than disabling another input.
"""
import re
from .proof_grounding import explicit_waiver


_SUBJECTS = {
    'transit_flow': r'прохож\w*|проходимост\w*|пешеходн\w*\s+(?:поток\w*|трафик\w*)|случайн\w*\s+(?:поток\w*|посетител\w*)|проходящ\w*\s+(?:поток\w*|трафик\w*)|транспортн\w*\s+поток\w*',
    'residents': r'жител\w*|населени\w*|домохозяйств\w*|аудитори\w*\s+(?:соседних|жилых)\s+дом\w*',
    'workers': r'работник\w*|сотрудник\w*|персонал\w*|офисн\w*\s+аудитори\w*',
    'site_audience': r'внутренн\w*\s+аудитори\w*|аудитори\w*\s+внутри|посетител\w*\s+внутри|резидент\w*',
    'synergy': r'синерги\w*|соседн\w*\s+(?:бизнес\w*|заведени\w*|организаци\w*)|генератор\w*\s+(?:спрос\w*|трафик\w*)',
}
_EXTERNAL = r'внешн\w*\s+(?:аудитори\w*|клиент\w*|посетител\w*|покупател\w*)'


def waiver_targets(quote):
    """Return only subjects explicitly removed in the same atomic clause."""
    targets=set()
    for clause in re.split(r'[;.!?\n]+|\s+но\s+|\s+зато\s+',str(quote),flags=re.I):
        # Avoid applying a later negation to a different subject in a mixed list
        # of independent clauses: 'жители важны, прохожие не нужны'.
        parts=re.split(r',\s*(?=[А-Яа-яЁё])',clause)
        for part in parts:
            if not explicit_waiver(part):continue
            for name,pattern in _SUBJECTS.items():
                if re.search(pattern,part,re.I):targets.add(name)
            if re.search(_EXTERNAL,part,re.I):
                targets.update(('transit_flow','residents','workers','synergy'))
    return targets


_CLOCK = re.compile(
    r'(?<![\d:])(?P<start>(?:[01]?\d|2[0-3]))(?::(?P<sm>[0-5]\d))?\s*'
    r'(?:час(?:ов|а)?\s*)?(?:[-–—]|до)\s*'
    r'(?P<end>(?:[01]?\d|2[0-4]))(?::(?P<em>[0-5]\d))?(?![\d:])',re.I)
_HOUR_CONTEXT = re.compile(r'работ\w*|открыт\w*|график\w*|режим\w*|час\w*|при[её]м\w*|принима\w*|обслужив\w*|достав\w*|визит\w*|пик\w*|спрос\w*|поток\w*',re.I)


def client_clock_window(concept):
    """Hourly coverage from explicit clock ranges; no inferred business hours.

    Partial hours retain fractional coverage. Overnight intervals wrap midnight.
    An unspecified clock window means no time restriction, never 'open 24/7'.
    """
    positives=[];negatives=[];quotes=[];full=False
    for clause in re.split(r'[;!?\n]+|(?<=[.])\s+',str(concept)):
        if re.search(r'\b24\s*/\s*7\b|круглосуточ\w*',clause,re.I):
            full=True;quotes.append(clause.strip())
        for match in _CLOCK.finditer(clause):
            # Money, area, durations and age ranges are not a clock schedule.
            suffix=clause[match.end():match.end()+18]
            if re.match(r'\s*(?:тыс\w*|руб\w*|₽|м[²2]|минут\w*|лет\b|человек\w*|заказ\w*|клиент\w*|визит\w*|покуп\w*|заявк\w*|запис\w*|единиц\w*|шт\b|раз\b|консультаци\w*|сеанс\w*)',suffix,re.I):continue
            if re.match(r'\s*(?:январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]\b|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)',suffix,re.I):continue
            if re.match(r'\s*час(?:ов|а)?\s+(?:в|за)\s+(?:день|сут\w*|недел\w*|месяц\w*)',suffix,re.I):continue
            if (re.search(r'\d\.$',clause[:match.start()]) or re.match(r'\.\d',suffix)):continue
            if ':' not in match.group(0) and not _HOUR_CONTEXT.search(clause):continue
            sh,eh=int(match['start']),int(match['end']);sm=int(match['sm'] or 0);em=int(match['em'] or 0)
            if eh==24 and em:continue
            start,end=sh*60+sm,eh*60+em
            if start==end:continue  # '8–8' is ambiguous, not automatically all day.
            intervals=[(start,end)] if start<end else [(start,1440),(0,end)]
            negative=bool(re.search(r'не\s+работ\w*|закрыт\w*|перерыв\w*',clause[:match.start()],re.I))
            (negatives if negative else positives).extend(intervals);quotes.append(clause.strip())
    minute_mask=[bool(full or not positives) for _ in range(1440)]
    for start,end in positives:
        for minute in range(start,end):minute_mask[minute]=True
    for start,end in negatives:
        for minute in range(start,end):minute_mask[minute]=False
    coverage=[sum(minute_mask[h*60:(h+1)*60])/60 for h in range(24)]
    return dict(active_hours=[h for h,c in enumerate(coverage) if c>0],hour_coverage=coverage,
                source_quotes=list(dict.fromkeys(quotes)),specified=bool(quotes))
