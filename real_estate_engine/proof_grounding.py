"""Source grounding primitives, independent of business names and floor policy."""
import re


def source_fragments(text):
    """Keep source lines intact, including decimal values and abbreviations."""
    return [line.strip() for line in str(text or '').splitlines() if line.strip()]


def measurement_dimensions(text):
    """Dimensions explicitly quantified in text; this does not infer capacity.

    Matching units establishes that a quantity is actually documented. Whether
    its value, relation and physical scope satisfy the request is still checked
    by the evidence reviewer. No equipment capacities are assumed.
    """
    quantity = r'(?<![\w])\d+(?:[ .,\u00a0]\d+)*\s*'
    units = {
        'mass': r'(?:кг|килограмм\w*|kg|тонн\w*|т|tons?|tonnes?)\b',
        'length': r'(?:мм|см|дм|метр\w*|м|mm|cm|meters?|metres?)\b(?!\s*[²³23])',
    }
    return {dimension for dimension, unit in units.items()
            if re.search(quantity + unit, str(text or '').lower())}


def reference_text(references):
    return '\n'.join(str(r.get('quote', r.get('value', ''))) for r in references)


def reference_key(reference):
    import json
    return json.dumps(reference, sort_keys=True, ensure_ascii=False)


def literal_floor_requirement(text):
    """Recognize an unambiguous floor-only numeric condition, not a visit scenario."""
    prefix=r'(?:(?:только|нужен|требуется|обязателен|на)\s+)*'
    number=r'(?P<floor>[+-]?\d+)(?:-?(?:й|ый|ой|ом|м|го|ого))?'
    normalized=str(text).strip().lower().replace('ё','е')
    for body in (number+r'\s+этаж(?:е|а)?',r'этаж\s*:?\s*'+number):
        match=re.fullmatch(prefix+body+r'[.!]?',normalized)
        if match:return int(match['floor'])
    # Linguistic equivalents of literal numbers, not business floor policies.
    ordinals={'перв':1,'втор':2,'трет':3,'четверт':4,'пят':5,'шест':6,'седьм':7,'восьм':8,'девят':9,'десят':10}
    for stem,value in ordinals.items():
        word=re.escape(stem)+r'(?:ый|ой|ий|ом|ем|ьем|ьего|ого)'
        for body in (word+r'\s+этаж(?:е|а)?',r'этаж\s*:?\s*'+word):
            if re.fullmatch(prefix+body+r'[.!]?',normalized):return value
    return None


def explicit_waiver(text):
    """Only a literal removal of a requirement, with no positive obligation."""
    quote=re.sub(r'\s+',' ',str(text).lower())
    negated=r'\bне\s+(?:нужен|нужна|нужно|нужны|требуется|требуются|обязателен|обязательна|обязательно|обязательны|важен|важна|важно|важны|ограничиваю|ограничиваем)\b'
    rest=re.sub(negated,'',quote)
    return rest!=quote and not re.search(r'\b(?:нужен|нужна|нужно|нужны|требуется|требуются|обязателен|обязательна|обязательно|обязательны|важен|важна|важно|важны|но|однако|если)\b',rest)


def explicit_permission(text):
    """Recognize a standalone allowance without dropping a mixed condition."""
    quote = str(text).casefold()
    return (bool(re.search(r'\b(?:допустим[аоы]?|допуска(?:ю|ем|ется|ются))\b', quote))
            and not re.search(r'\b(?:не|нуж\w*|треб\w*|обязател\w*|необходим\w*|'
                              r'желатель\w*|долж\w*|но|однако|если|при условии)\b', quote))


def client_managed_aspect(text):
    """A standalone first-person delegation, without an additional obligation."""
    quote=str(text).casefold().replace('ё','е')
    return (bool(re.search(r'\b(?:самостоятельно|сам|сама|сами|лично)\b',quote))
            and bool(re.search(r'\b(?:оцениваю|оценю|оценим|проверяю|проверю|проверим|рассчитаю|рассчитываю|'
                               r'определяю|определю|делаю|сделаю|выполняю|выполню|решаю|решу)\b',quote))
            and not re.search(r'\b(?:нуж\w*|треб\w*|обязател\w*|необходим\w*|долж\w*|но|однако|если)\b',quote))
