"""Evidence of a company's INN from a business-linked public website.

Nearby address/OKVED and brand dictionaries are discovery hints, never proof
that an outlet belongs to the first legal entity with positive revenue.
"""
import asyncio
import html
import ipaddress
import re
import time
import copy
import weakref
from urllib.parse import urljoin,urlsplit,urlunsplit,parse_qsl,urlencode


def canonical_business_url(url):
    parts=urlsplit(url);query=parse_qsl(parts.query,keep_blank_values=True)
    # Preserve functional store/account parameters. Only tracking tags are
    # removed; signed URLs are left intact.
    if any(k.casefold() in ('signature','sig','token') for k,_ in query):return url
    query=[(k,v) for k,v in query if not k.casefold().startswith('utm_') and k.casefold() not in ('yclid','gclid','fbclid','ysclid')]
    return urlunsplit((parts.scheme,parts.netloc,parts.path or '/',urlencode(query),''))


def valid_inn(value):
    text=str(value)
    if not re.fullmatch(r'\d{10}',text):return False
    digits=list(map(int,text));weights=[2,4,10,3,5,9,4,6,8]
    return sum(a*b for a,b in zip(digits[:9],weights))%11%10==digits[9]


_STREET_TYPE_HINT_RE=re.compile(
    r'\b(?:улица|ул\.?|переулок|пер\.?|бульвар|б-р\.?|проспект|пр-?кт\.?|пр-т\.?|просп\.?|'
    r'набережная|наб\.?|площадь|пл\.?|шоссе|ш\.?|проезд|пр-д\.?|линия|лн\.?|аллея|тупик|вал|дорога)\b',
    re.I,
)
_HOUSE_CLAUSE_RE=re.compile(
    r'^(?:д(?:ом)?\.?\s*)?\d+(?!-\s*[яйе]\b)(?:\s*[/\-]\s*\d+)?'
    r'(?:\s*[а-яa-z](?=\s|$|[/\-]|к(?:орп|\d|\.)|с(?:тр|\d|\.)|ст(?:\d|\.)|лит))?'
    r'(?:\s*,?\s*(?:к(?:орп(?:ус)?)?(?:\.|\s|(?=\d))|с(?:тр(?:оение)?)?(?:\.|\s|(?=\d))|'
    r'ст(?:\.|\s|(?=\d))|лит(?:ер(?:а)?)?(?:\.|\s)?|вл(?:ад(?:ение)?)?(?:\.|\s)?)\s*[а-яa-z0-9/-]+)*$',
    re.I,
)
_BLOCK_CLAUSE_RE=re.compile(
    r'^(?:к(?:орп(?:ус)?)?(?:\.|\s|(?=\d))|с(?:тр(?:оение)?)?(?:\.|\s|(?=\d))|'
    r'ст(?:\.|\s|(?=\d))|лит(?:ер(?:а)?)?(?:\.|\s)?|вл(?:ад(?:ение)?)?(?:\.|\s)?)\s*[а-яa-z0-9/-]+$',
    re.I,
)


def _house_key(value):
    text=str(value).lower().replace('ё','е')
    text=re.sub(r'корпус|корп\.?','к',text)
    text=re.sub(r'строение|стр\.?|ст(?=\s*\d)','с',text)
    text=re.sub(r'литера|литер|лит\.?','',text)
    return re.sub(r'[^а-яa-z0-9/]','',text)


def _looks_like_house(part):
    text=str(part or '').strip().lower().replace('ё','е')
    if not text or not re.search(r'^\s*(?:д(?:ом)?\.?\s*)?\d',text):
        return False
    return bool(_HOUSE_CLAUSE_RE.match(text))


def _normalize_street_text(value):
    text=str(value or '').lower().replace('ё','е')
    text=re.sub(r'\bвасильевск\w*\s+остров\w*\b',' во ',text)
    text=re.sub(r'\bпетроградск\w*\s+сторон\w*\b',' пс ',text)
    text=re.sub(r'(?:^|[\s,])в\s*\.\s*о\.?(?=$|[\s,])',' во ',text)
    text=re.sub(r'(?:^|[\s,])п\s*\.\s*с\.?(?=$|[\s,])',' пс ',text)
    text=re.sub(r'(?:^|[\s,])лн\.?(?=$|[\s,])',' линия ',text)
    text=re.sub(
        r'(?:^|[\s,])(?:улица|ул\.?|переулок|пер\.?|бульвар|б-р\.?|проспект|пр-?кт\.?|пр-т\.?|просп\.?|'
        r'набережная|наб\.?|площадь|пл\.?|шоссе|ш\.?|проезд|пр-д\.?|аллея|тупик)(?=$|[\s,])',
        ' ',
        text,
    )
    return text


def _street_tokens(value):
    return set(re.findall(r'[а-яa-z0-9]+(?:-[а-яa-z0-9]+)*',_normalize_street_text(value)))


def streets_match(street_name,addr_val,addr_data=None):
    """Compare parsed street names across word-order differences and SPb/type abbreviations."""
    if not street_name:
        return False
    raw=str(street_name).lower().replace('ё','е')
    norm_val=str(addr_val or '').lower().replace('ё','е')
    if raw in norm_val:
        return True
    req=_street_tokens(street_name)
    if not req:
        return False
    target=(addr_data or {}).get('street_with_type') or (addr_data or {}).get('street') or addr_val
    cand=_street_tokens(target)
    return req<=cand


def extract_street_and_house(address):
    """Parse (street_part, cleaned_street, house_full, house_num) including numbered streets and comma-split blocks."""
    if not address:
        return None,None,None,None
    from .cities import city_in_text

    parts=[p.strip() for p in str(address).split(',') if p.strip()]
    street_part=None
    house_parts=[]
    for p in parts:
        p_low=p.lower().replace('ё','е')
        has_street_word=bool(_STREET_TYPE_HINT_RE.search(p_low))
        if not house_parts and street_part is None:
            if (p_low in ('россия','зеленоград')
                    or re.fullmatch(r'\d{6}',p_low)
                    or re.search(r'\b(?:область|обл\.|край|республика)\b',p_low)
                    or (not has_street_word and (city_in_text(p_low) or re.search(r'\b(?:район|р-н|округ)\b',p_low)))):
                continue
            if not _looks_like_house(p):
                street_part=p
                continue
        if street_part is not None and not house_parts and _looks_like_house(p):
            house_parts.append(p)
        elif house_parts and _BLOCK_CLAUSE_RE.match(p_low):
            house_parts.append(p)
        elif house_parts:
            break

    if not street_part:
        return None,None,None,None
    cleaned_street=re.sub(
        r'(?:^|\s)(?:улица|ул\.?|переулок|пер\.?|бульвар|б-р\.?|проспект|пр-?кт\.?|пр-т\.?|просп\.?|'
        r'набережная|наб\.?|площадь|пл\.?|шоссе|ш\.?|проезд|пр-д\.?|аллея|тупик)(?=\s|$)',
        ' ',
        street_part,
        flags=re.IGNORECASE,
    )
    cleaned_street=re.sub(r'\s+',' ',cleaned_street).strip(' .,') or None
    house_full=', '.join(house_parts) if house_parts else None
    house_num=None
    if house_parts:
        clean_first=re.sub(r'^д(?:ом)?\.?\s*','',house_parts[0].strip(),flags=re.I)
        m=re.match(r'^(\d+)',clean_first)
        if m:
            house_num=m.group(1)
    return street_part,cleaned_street,house_full,house_num


def exact_registry_house(card_address,address_data):
    """Do not match corpus/structure neighbours using just the leading digit."""
    _,_,target,_=extract_street_and_house(card_address)
    if target is None:
        # Fallback if a caller passes a house-only or unusual comma layout with city+house.
        parts=[p.strip() for p in str(card_address).split(',')]
        target=next((p for p in parts[1:] if _looks_like_house(p)),None)
    if target is None:return False
    target=re.sub(r'^д(?:ом)?\.?\s*','',target,flags=re.I)
    house=str(address_data.get('house') or '')
    block=str(address_data.get('block') or '')
    block_type=str(address_data.get('block_type_full') or address_data.get('block_type') or '')
    registry=house+(f' {block_type} {block}' if block else '')
    if not registry:return False
    target_key=_house_key(target)
    if _house_key(registry)==target_key:return True
    if block and block_type.lower().startswith('лит') and block.strip().lower() in ('а','a'):
        return bool(house) and _house_key(house)==target_key
    trimmed_block=re.sub(r'(?:\s*,)?\s*(?:литера|литер|лит\.?)\s*[аa]$','',block.strip(),flags=re.I)
    if trimmed_block and trimmed_block!=block.strip():
        return _house_key(f'{house} {block_type} {trimmed_block}')==target_key
    return False


def _text(body):
    body=re.sub(r'<(?:script|style)\b[^>]*>.*?</(?:script|style)>',' ',body,flags=re.I|re.S)
    return ' '.join(html.unescape(re.sub(r'<[^>]+>',' ',body)).split())


def public_url(url):
    p=urlsplit(str(url))
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or p.hostname=='localhost':return False
    try:
        if not ipaddress.ip_address(p.hostname).is_global:return False
    except ValueError:pass
    return True


def extract_company_inns(body,url,title):
    """Read labelled INNs near a legal entity name; no arbitrary 10-digit IDs."""
    text=_text(body);results=[]
    title_tokens=[t for t in re.findall(r'[а-яa-z0-9]+',title.lower().replace('ё','е')) if len(t)>=3]
    for match in re.finditer(r'\bИНН\s*[:№]?\s*(\d{10})(?!\d)',text,re.I):
        inn=match[1]
        if not valid_inn(inn):continue
        before=text[max(0,match.start()-350):match.start()]
        legal=list(re.finditer(r'\b(?:ООО|АО|ПАО|ОАО|ЗАО|ОБЩЕСТВО|Акционерн\w*|Обществ\w*)\b',before,re.I))
        if not legal:continue
        legal_text=before[legal[-1].start():]
        normalized=re.sub(r'[^а-яa-z0-9]','',legal_text.lower().replace('ё','е'))
        # A product manufacturer or unrelated payment/privacy processor does
        # not become the business operator simply by appearing on its website.
        if not title_tokens or not any(t in normalized for t in title_tokens):continue
        results.append(dict(inn=inn,source_url=url,quote=text[max(0,match.start()-200):match.end()+80],
            method='business_linked_website_company',scope='company',
            note='Published business-linked company; accounts can include other branches'))
    return list({r['inn']:r for r in results}.values())


class WebsiteLegalResolver:
    def __init__(self,session,*,ttl=86400):
        self.session=session;self.ttl=ttl;self.cache={};self.locks=weakref.WeakValueDictionary();self.slots=asyncio.Semaphore(4)

    async def resolve(self,title,urls):
        urls=list(dict.fromkeys(canonical_business_url(u) for u in urls if public_url(u)))[:3]
        if not urls:return []
        key=(title,tuple(sorted(urls)));cached=self.cache.get(key)
        if cached and cached[0]>time.monotonic():return copy.deepcopy(cached[1])
        lock=self.locks.setdefault(key,asyncio.Lock())
        async with lock,self.slots:
            cached=self.cache.get(key)
            if cached and cached[0]>time.monotonic():return copy.deepcopy(cached[1])
            found=[];visited=set()
            queue=list(urls);deadline=time.monotonic()+12
            while queue and len(visited)<4 and deadline>time.monotonic():
                url=queue.pop(0)
                if url in visited or not public_url(url):continue
                visited.add(url)
                try:
                    remaining=max(.001,deadline-time.monotonic())
                    response=await asyncio.wait_for(asyncio.to_thread(self.session.get,url,timeout=min(6,remaining)),remaining)
                    if response.status_code!=200 or len(response.content)>2500000:continue
                    body=response.content.decode('utf-8',errors='replace')
                    found.extend(extract_company_inns(body,url,title))
                    origin=urlsplit(url).hostname
                    matched_subpage=False
                    for link in re.finditer(r'<a\b[^>]*href=["\x27]([^"\x27]+)["\x27][^>]*>(.*?)</a>',body,re.I|re.S):
                        target=urljoin(url,html.unescape(link[1]));label=_text(link[2])
                        if urlsplit(target).hostname==origin and re.search(r'contact|контакт|реквизит|legal|ofert|оферт|company|компани|about|о[\s_-]?нас|о[\s_-]?компан|o-?nas|o-?kompan|kompan|polit|policy|prav|info|инф|doc|документ|sveden|сведен',target+' '+label,re.I):
                            matched_subpage=True
                            if target not in visited and target not in queue:queue.append(target)
                    if not found and not matched_subpage and re.search(r'<(?:html|body|div|nav|footer|a)\b',body,re.I):
                        for path in ('/contacts','/rekvizity','/oferta'):
                            candidate_url=canonical_business_url(urljoin(url,path))
                            if urlsplit(candidate_url).hostname==origin and candidate_url not in visited and candidate_url not in queue:
                                queue.append(candidate_url)
                except Exception:continue
                if found:break
            found=list({r['inn']:r for r in found}.values())
            self.cache[key]=(time.monotonic()+(self.ttl if len(found)==1 else 60),found)
            if len(self.cache)>256:self.cache.pop(next(iter(self.cache)))
            return copy.deepcopy(found)
