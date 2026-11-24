"""Read public unit sources. Never interpret a whole building page as one unit."""
import html
import json
import math
import re
import time
from datetime import datetime, timezone
from functools import cached_property
from urllib.parse import urlparse, unquote, urljoin

from .ai_contract import AI_DEADLINE
from .cities import CITIES
from .collectors.access_state import SourceAccessError

CIAN_HOSTS = {'cian.ru', 'www.cian.ru'} | {city.cian_host for city in CITIES.values()}
DOMCLICK_HOSTS = {'domclick.ru', 'www.domclick.ru'} | {city.domclick_host for city in CITIES.values()}


class PublicSourceError(RuntimeError):
    def __init__(self,kind,status_code=None):
        super().__init__('Public source '+kind)
        self.kind=kind;self.status_code=status_code


def source_error(stage,exc):
    # Do not persist exception strings: proxy URLs may contain credentials.
    return dict(stage=stage,error_type=type(exc).__name__,
                kind=getattr(exc,'kind','identity_or_format' if isinstance(exc,ValueError) else 'transport'),
                status_code=getattr(exc,'status_code',None),
                **({'retry_after_s':exc.retry_after_s} if getattr(exc,'retry_after_s',None) is not None else {}))


def remaining_budget():
    remaining=(AI_DEADLINE.get() or (time.monotonic()+10))-time.monotonic()
    if remaining<=0:raise TimeoutError('Confirmation budget expired')
    return min(10,remaining)


def raw_attributes(prop):
    raw=prop.get('raw_attributes',prop.get('raw_json',{}))
    if isinstance(raw,str):
        try:raw=json.loads(raw)
        except ValueError:return {}
    return raw if isinstance(raw,dict) else {}


def plain(value):
    value=re.sub(r'<(?:script|style|svg)\b[^>]*>.*?</(?:script|style|svg)>','',value,flags=re.S|re.I)
    return re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]+>',' ',value))).strip()


def unit_photos(values,base_url):
    """Only images from the already bound unit object, never page-wide galleries."""
    from urllib.parse import urljoin
    from .premises_requirements import public_image_url
    if isinstance(values,(str,dict)):values=[values]
    if not isinstance(values,list):return []
    found=[]
    for item in values:
        if isinstance(item,dict):item=item.get('fullUrl') or item.get('url') or item.get('contentUrl')
        if not isinstance(item,str):continue
        url=urljoin(base_url,item)
        if public_image_url(url) and url not in found:found.append(url)
    return found[:3]


def _street_abbreviations(value):
    value=value.casefold().replace('ё','е')
    value=re.sub(r'\bпр-д\.?(?=\s|,|$)','проезд',value)
    value=re.sub(r'\b(?:пр-т\.?|просп\.)(?=\s|,|$)','проспект',value)
    for abbreviation, full in ((r'пер\.?', 'переулок'), (r'наб\.?', 'набережная'), (r'б-р\.?', 'бульвар')):
        value=re.sub(r'(?<![а-я])'+abbreviation+r'(?=\s|,|$)',full,value)
    return value


def address_key(value):
    value=_street_abbreviations(value)
    value=re.sub(r'^\s*(?:россия|российская федерация|russia)\s*,\s*','',value)
    value=re.sub(r'(?<![а-я])(?:строение|строения|стр\.?|ст\.?)\s*(?=\d)','с',value)
    value=re.sub(r'(?<![а-я])(?:корпус|корп\.?|к\.)\s*(?=\d)','к',value)
    value=re.sub(r'\bпл\.(?=\s|,|$)','площадь',value)
    value=re.sub(r'\bш\.(?=\s|,|$)','шоссе',value)
    value=re.sub(r'\b(?:санкт[- ]петербург|спб)\b\.?','',value)
    value=re.sub(r'\b(?:литера|лит\.)\s*', '', value)
    value=re.sub(r'\b(?:москва|на|улица|улице|ул|дом|д)\b\.?','',value)
    return re.sub(r'[^а-яa-z0-9]','',value)


def addresses_match(left,right):
    """Resolve ambiguous пр. only against the other address's explicit type."""
    if not isinstance(left,str) or not isinstance(right,str):return False
    from .cities import city_in_text
    left_city, right_city = city_in_text(left), city_in_text(right)
    if left_city and right_city and left_city != right_city:
        return False
    values=(_street_abbreviations(left),_street_abbreviations(right))
    keys=tuple(address_key(value) for value in values)
    if not all(keys):return False
    if keys[0]==keys[1]:return True
    abbreviation=r'(?<![а-я])пр\.(?=\s|,|$)'
    for index,other in ((0,1),(1,0)):
        types=re.findall(r'(?<![а-я])(?:проезд|проспект)(?![а-я])',values[other])
        if len(types)==1 and len(re.findall(abbreviation,values[index]))==1:
            expanded=re.sub(abbreviation,types[0],values[index])
            if address_key(expanded)==keys[other]:return True
    return False


def js_object(body,pattern):
    match=re.search(pattern,body)
    if not match:raise ValueError('Source object missing')
    value,_=json.JSONDecoder().raw_decode(body[match.end():].lstrip())
    if not isinstance(value,dict):raise ValueError('Source object invalid')
    return value


def domclick_catalogue_unit(item, metadata, prop):
    """Bind current public search facts to one unit, without inventing card access."""
    from .cities import get_city, city_for_coordinates
    expected = str(prop.get('external_id', ''))
    if str(item.get('id')) != expected:
        raise ValueError('Domclick catalogue unit ID mismatch')
    listing = urlparse(prop.get('url', ''))
    identifier = re.fullmatch(r'/card/rent__[a-z_]+__(\d+)/?', listing.path)
    if listing.hostname not in DOMCLICK_HOSTS or not identifier or identifier[1] != expected:
        raise ValueError('Domclick rental URL identity mismatch')
    city = get_city(metadata['city'])
    if (city_for_coordinates(prop.get('lat'), prop.get('lng')) != city
            or metadata['region_id'] != city.domclick_address or item.get('offerRegion') != city.domclick_address):
        raise ValueError('Domclick catalogue region mismatch')
    address = (item.get('address') or {}).get('displayName')
    if not isinstance(address, str) or not addresses_match(address, prop.get('address', '')):
        raise ValueError('Domclick catalogue address mismatch')
    info = item.get('objectInfo') or {}
    area = float(info.get('area') or 0)
    price = item.get('price')
    rent = float(price.get('value') or 0) if isinstance(price, dict) else float(price or 0)
    if (not math.isfinite(area) or area <= 0 or abs(area-float(prop['area_sqm'])) > .5
            or not math.isfinite(rent) or rent <= 0 or item.get('isSold') is not False):
        raise ValueError('Domclick catalogue unit terms or current publication missing')
    from .floor_policy import extract_floor_details
    floor = extract_floor_details(info.get('floor'), description=str(item.get('description') or ''))['floor']
    description = plain(str(item.get('description') or ''))
    facts = dict(title=plain(str(item.get('title') or f'Арендное объявление Домклик {expected}')),
        description=description, status='Арендное объявление присутствует в свежей выдаче источника',
        area_sqm=f'Площадь предлагаемого помещения: {area:g} м²',
        price_month=f'Месячная аренда по опубликованной арендной выдаче: {rent:g} рублей')
    if floor is not None:
        facts['floor'] = f'Этаж предлагаемого помещения: {floor}'
    photos = [urljoin('https://img.dmclk.ru/', v['url'])
              for v in item.get('photos', []) if isinstance(v, dict) and isinstance(v.get('url'), str)]
    return dict(url=metadata['source_url'], listing_url=prop['url'], unit_id=expected, address=address,
        area_sqm=area, price_month=rent, floor=floor, facts=facts, identity_checked=True,publication_active=True,
        photos=unit_photos(photos, prop['url']), source_kind='refreshed_public_rental_catalogue',
        source_note='Проверены опубликованные условия; отдельная карточка и подтверждение арендодателя не получены')


def municipal_unit(data, url, prop):
    from .cities import city_for_coordinates
    from .collectors.municipal import rental_lot
    city=city_for_coordinates(prop.get('lat'),prop.get('lng'))
    if city is None or str(data.get('id'))!=str(prop.get('external_id')):
        raise ValueError('Municipal lot identity mismatch')
    fresh=rental_lot(data,city,active_only=False)
    if (fresh is None or not addresses_match(fresh.address,prop.get('address',''))
            or abs(fresh.area_sqm-float(prop['area_sqm']))>.5):
        raise ValueError('Municipal unit address or area mismatch')
    active=fresh.raw_attributes['auction_active']
    facts=dict(title=fresh.title,description=fresh.description,
        status=('Принимаются заявки на арендные торги' if active else 'Заявки не принимаются; статус '+str(data.get('lotStatus'))),
        area_sqm=f'Площадь предлагаемого помещения: {fresh.area_sqm:g} м²',
        auction_start=f'Стартовая ставка: {fresh.price_month:g} рублей в месяц; окончательная аренда определяется торгами')
    if fresh.floor is not None:facts['floor']=f'Этаж: {fresh.floor}'
    if not active:facts['archive_status']='Прием заявок на данный лот не открыт'
    return dict(url=url,unit_id=fresh.external_id,address=fresh.address,area_sqm=fresh.area_sqm,
        price_month=fresh.price_month,floor=fresh.floor,facts=facts,identity_checked=True,
        photos=[],auction_start_only=True,source_kind='official_municipal_auction')


def mega_unit(body,url,lot,prop):
    """Publisher-specific format, any lot; no niche or pilot-address rules."""
    anchor=unquote(urlparse(url).fragment)
    if not re.fullmatch(r'[a-z_]+_'+re.escape(lot),anchor):raise ValueError('Missing exact unit anchor')
    unit=js_object(body,r'\b'+re.escape(anchor)+r'\s*:\s*')
    building=js_object(body,r'\bvar\s+building_data\s*=\s*')
    if str(unit.get('id'))!=lot or unit.get('anchor')!=anchor:raise ValueError('Unit identity mismatch')
    if not addresses_match(building.get('locativ',''),prop.get('address','')):
        raise ValueError('Building address mismatch')
    area=float(unit.get('area',0))
    if not math.isfinite(area) or area<=0 or abs(area-float(prop['area_sqm']))>.5:raise ValueError('Unit area mismatch')
    # Building context ends at its card, before any other unit's section.
    start=body.find('id="building-spacer"')
    building_html=body[start:body.find('id="',start+21)] if start>=0 else ''
    facts={k:plain(str(v)) for k,v in unit.items() if isinstance(v,(str,int,float)) and v!=''}
    facts['building_context']=plain(building_html)[:4000]
    return dict(url=url,unit_id=lot,address=prop['address'],area_sqm=float(unit['area']),
                price_month=float(unit['totalPrice']) if unit.get('totalPrice') else None,
                floor=facts.get('floor'),facts=facts,identity_checked=True,
                photos=unit_photos(unit.get('photos',[]),url))


def jsonld_unit(body,url,prop):
    """Accept only a structured, exactly bound unit, never arbitrary page text."""
    def identity_url(value):
        if value.startswith('//'):value='https:'+value
        parsed=urlparse(value.split('#')[0].rstrip('/'))
        if parsed.netloc.casefold() in ('realty.ya.ru','realty.yandex.ru'):
            parsed=parsed._replace(netloc='realty.yandex.ru')
        return parsed.geturl()
    found=[]
    for script in re.findall(r'<script\b[^>]*type=[\"\']application/ld\+json[\"\'][^>]*>(.*?)</script>',body,re.S|re.I):
        try:data=json.loads(script)
        except ValueError:continue
        stack=[data]
        while stack:
            value=stack.pop()
            if isinstance(value,list):stack.extend(value);continue
            if not isinstance(value,dict):continue
            stack.extend(v for v in value.values() if isinstance(v,(dict,list)))
            identifiers=[value.get('url'),value.get('@id'),value.get('identifier')]
            exact=any(isinstance(v,str) and identity_url(v)==identity_url(prop.get('url','')) for v in identifiers)
            area=value.get('floorSize',{});area=area.get('value') if isinstance(area,dict) else None
            address=value.get('address',{})
            address=address.get('streetAddress') if isinstance(address,dict) else address
            if not exact or not area or not address:continue
            try:matches=abs(float(area)-float(prop['area_sqm']))<=.5
            except (ValueError,TypeError):continue
            if not matches or not addresses_match(address,prop.get('address','')):continue
            facts={k:plain(v) for k,v in value.items() if isinstance(v,str) and k in ('name','description','permittedUse','availability')}
            found.append(dict(url=url,unit_id=str(prop.get('external_id')),address=address,area_sqm=float(area),
                              price_month=None,floor=None,facts=facts,identity_checked=True,
                              photos=unit_photos(value.get('image',[]),url)))
    if len(found)!=1:raise ValueError('No unique structured unit')
    return found[0]


def avito_catalogue_snapshot(prop):
    """Reuse the exact recently received rental row; preserve its original age."""
    from .collectors.avito import catalogue_description
    from .floor_policy import extract_floor_details
    raw=raw_attributes(prop);item=raw.get('source_payload',{})
    if prop.get('source')!='avito' or not isinstance(item,dict) or str(item.get('id'))!=str(prop.get('external_id')):
        raise ValueError('Avito catalogue identity mismatch')
    stamp=datetime.fromisoformat(raw['source_fetched_at'])
    if stamp.tzinfo is None or not 0<=time.time()-stamp.timestamp()<=300:
        raise ValueError('Avito catalogue snapshot expired')
    url=urlparse(prop.get('url',''));path=urlparse(item.get('urlPath','')).path
    if (url.hostname not in ('avito.ru','www.avito.ru') or path!=url.path
            or not path.endswith('_'+str(prop.get('external_id')))):
        raise ValueError('Avito catalogue URL mismatch')
    address=(item.get('coords') or {}).get('address_user') or (item.get('geo') or {}).get('formattedAddress')
    if not isinstance(address,str) or not addresses_match(address,prop.get('address','')):
        raise ValueError('Avito catalogue address mismatch')
    title=item.get('title','');description=catalogue_description(item)
    match=re.search(r'(\d+(?:[.,]\d+)?)\s*(?:м²|м2|кв\.?\s*м)',title,re.I)
    area=float(match[1].replace(',','.')) if match else 0
    terms=item.get('priceDetailed') or {};rent=terms.get('value');period=str(terms.get('postfix','')).casefold()
    if (not math.isfinite(area) or area<=0 or abs(area-float(prop['area_sqm']))>.5
            or type(rent) not in (int,float) or not math.isfinite(rent) or rent<=0
            or '₽' not in str(terms.get('fullString',''))
            or not re.fullmatch(r'(?:за м[²2]\s+)?в (?:месяц|год)',period)):
        raise ValueError('Avito catalogue rental terms incomplete')
    rent=rent*(area if period.startswith('за ') else 1)/(12 if period.endswith('год') else 1)
    floor=extract_floor_details(item.get('floor'),title=title,description=description)['floor']
    facts=dict(title=plain(title),description=plain(description),
        status='Арендное объявление присутствует в свежей выдаче Авито',
        area_sqm=f'Площадь предлагаемого помещения: {area:g} м²',
        price_month=f'Месячная аренда предлагаемого помещения: {rent:g} рублей')
    if floor is not None:facts['floor']=f'Этаж предлагаемого помещения: {floor}'
    return dict(url=prop['url'],unit_id=str(item['id']),address=address,area_sqm=area,price_month=rent,
        floor=floor,facts=facts,identity_checked=True,fetched_at=stamp.timestamp(),publication_active=True,
        source_kind='collected_public_rental_catalogue',
        source_note='Сохранён свежий ответ выдачи; отдельная карточка и подтверждение арендодателя не получены')


def avito_unit(body,url,prop):
    """Bind public detail-page state to the advertised Avito unit."""
    from .floor_policy import parse_floor_values

    expected=str(prop.get('external_id',''))
    match=re.search(r'(?:_|/)(\d+)/?$',urlparse(url).path)
    if not match or match[1]!=expected:
        raise ValueError('Avito URL identity mismatch')
    hydration=re.search(r'window\.__staticRouterHydrationData\s*=\s*JSON\.parse\(("(?:\\.|[^"\\])*")\)',body)
    if not hydration:
        return jsonld_unit(body,url,prop)
    page=json.loads(json.loads(hydration[1]))['loaderData']['catalog-or-main-or-item']['buyerItem']
    item=page['item'];params=page['paramsDto']
    if str(item.get('id'))!=expected or str(params.get('itemId'))!=expected:
        raise ValueError('Avito unit identity mismatch')
    address=item.get('address')
    if not addresses_match(address,prop.get('address','')):
        raise ValueError('Avito unit address mismatch')
    details={}
    for row in params.get('items',[]):
        label,value=row.get('title'),row.get('description')
        if isinstance(label,str) and isinstance(value,str) and value.strip():
            key=plain(label).casefold()
            value=plain(value)
            if key in details and details[key]!=value:
                raise ValueError('Conflicting Avito unit parameters')
            details[key]=value
    area_text=details.get('общая площадь','').replace(',','.')
    area_match=re.fullmatch(r'(\d+(?:\.\d+)?)\s*м[²2]',area_text)
    area=float(area_match[1]) if area_match else 0
    if not math.isfinite(area) or area<=0:
        raise ValueError('Current Avito area missing')
    terms=item.get('formattedPrice') or {}
    period=plain(str(terms.get('postfix') or terms.get('normalizedPricePostFix') or '')).casefold()
    price=terms.get('value')
    rent=None
    if (terms.get('currencySign')=='₽' and type(price) in (int,float) and math.isfinite(price) and price>0
            and re.fullmatch(r'(?:за м[²2]\s+)?в (?:месяц|год)',period)):
        rent=price*(area if period.startswith('за ') else 1)/(12 if period.endswith('год') else 1)
    floors=parse_floor_values(details.get('этаж'))
    floor=floors[0] if len(floors)==1 else None
    if type(item.get('isActive')) is not bool:
        raise ValueError('Current Avito publication status missing')
    active=item['isActive'] and not any(item.get(k) is True for k in ('isArchived','isBlocked','isClosed'))
    facts={'status':'Объявление активно на Авито' if active else 'Объявление на Авито неактивно',
           'area_sqm':f'Площадь предлагаемого помещения: {area:g} м²',
           'listing_parameters':'\n'.join(f'{key}: {value}' for key,value in details.items())}
    if not active:facts['archive_status']=facts['status']
    for name in ('title','description'):
        if isinstance(item.get(name),str) and item[name].strip():facts[name]=plain(item[name])
    if rent is not None:facts['price_month']=f'Месячная аренда предлагаемого помещения: {rent:g} рублей'
    if floor is not None:facts['floor']=f'Этаж предлагаемого помещения: {floor}'
    images=[next((image.get(key) for key in ('1280x960','640x480','636x476','432x324') if image.get(key)),None)
            for image in item.get('imageUrls',[]) if isinstance(image,dict)]
    return dict(url=url,unit_id=expected,address=address,area_sqm=area,price_month=rent,floor=floor,
                facts=facts,identity_checked=True,publication_active=active,photos=unit_photos(images,url))


def yandex_unit(body,url,prop):
    """Read only offerCard.card, excluding related offers and page galleries."""
    from .floor_policy import parse_floor_values

    expected=str(prop.get('external_id',''))
    if urlparse(url).path.rstrip('/')!='/offer/'+expected or not expected.isdigit():
        raise ValueError('Yandex URL identity mismatch')
    marker=re.search(r'window\.INITIAL_STATE\s*=\s*',body)
    if not marker:return jsonld_unit(body,url,prop)
    state,_=json.JSONDecoder().raw_decode(body[marker.end():].lstrip())
    card=state.get('offerCard',{}).get('card',{})
    if str(card.get('offerId'))!=expected:
        raise ValueError('Yandex unit identity mismatch')
    if card.get('offerType')!='RENT' or card.get('offerCategory')!='COMMERCIAL':
        raise ValueError('Yandex unit is not a commercial rental')
    location=card.get('location') or {}
    address=location.get('geocoderAddress') or location.get('unifiedAddress') or location.get('address')
    if not addresses_match(address,prop.get('address','')):
        raise ValueError('Yandex unit address mismatch')
    size=card.get('area') or {}
    area=size.get('value')
    if size.get('unit')!='SQUARE_METER' or type(area) not in (int,float) or not math.isfinite(area) or area<=0:
        raise ValueError('Current Yandex area missing')
    terms=card.get('price') or {}
    price=terms.get('value');rent=None
    if (terms.get('currency') in ('RUR','RUB') and terms.get('unit') in ('SQUARE_METER','WHOLE_OFFER')
            and terms.get('period') in ('PER_MONTH','PER_YEAR') and type(price) in (int,float)
            and math.isfinite(price) and price>0):
        rent=price*(area if terms['unit']=='SQUARE_METER' else 1)/(12 if terms['period']=='PER_YEAR' else 1)
        if not math.isfinite(rent):rent=None
    if type(card.get('active')) is not bool:
        raise ValueError('Current Yandex publication status missing')
    active=card['active'] and card.get('obsolete') is not True
    floors=parse_floor_values(card.get('floorsOffered'))
    floor=floors[0] if len(floors)==1 else None
    facts={'status':'Объявление активно на Яндекс Недвижимости' if active else 'Объявление на Яндекс Недвижимости неактивно',
           'area_sqm':f'Площадь предлагаемого помещения: {area:g} м²'}
    if not active:facts['archive_status']=facts['status']
    if rent is not None:facts['price_month']=f'Месячная аренда предлагаемого помещения: {rent:g} рублей'
    if floor is not None:facts['floor']=f'Этаж предлагаемого помещения: {floor}'
    if isinstance(card.get('description'),str):facts['description']=plain(card['description'])
    for key in ('commercial','house','building','utilitiesIncluded','taxationForm'):
        if key in card:facts[key]=json.dumps(card[key],ensure_ascii=False)[:4000]
    return dict(url=url,unit_id=expected,address=address,area_sqm=float(area),price_month=rent,floor=floor,
                facts=facts,identity_checked=True,photos=unit_photos(card.get('fullImages') or card.get('mainImages') or [],url))


def torgi_unit(body,url,prop):
    """Read the public city-auction card and keep bid terms explicit."""
    from .floor_policy import parse_floor_values

    expected=str(prop.get('external_id',''))
    if not expected.isdigit() or urlparse(url).path.rstrip('/')!=f'/tender/{expected}':
        raise ValueError('Moscow auction URL identity mismatch')
    match=re.search(r'<script\b[^>]*\bid=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>',body,re.S)
    if not match:raise ValueError('Moscow auction card data missing')
    values=json.loads(match[1])

    def dereference(index):
        if type(index) is not int or not 0<=index<len(values):
            raise ValueError('Invalid Moscow auction card reference')
        return values[index]

    def object_at(index):
        value=dereference(index)
        if not isinstance(value,dict):
            raise ValueError('Invalid Moscow auction card object')
        return value

    root=object_at(values[0][1])
    payload=object_at(dereference(root['data'])[1])
    card_fields={'tenderId','objectTypeId','headerInfo','sidebar','mapInfo','objectInfo','imageInfo'}
    cards=[value for index in payload.values() if type(index) is int and index>=0
           and isinstance(value:=dereference(index),dict) and card_fields<=set(value)
           and str(dereference(value['tenderId']))==expected]
    if len(cards)!=1:raise ValueError('Moscow auction card identity mismatch')
    card=cards[0]
    header=object_at(card['headerInfo'])
    if dereference(card['objectTypeId'])!=30011569 or dereference(header['tenderTypeName'])!='Аренда':
        raise ValueError('Moscow auction is not a commercial lease')
    area=dereference(header['objectAreaInMeters'])
    address=dereference(header['address'])
    if (type(area) not in (int,float) or not math.isfinite(area) or area<=0
            or not addresses_match(address,prop.get('address',''))):
        raise ValueError('Moscow auction unit identity or area mismatch')
    sidebar=object_at(card['sidebar'])
    location=object_at(card['mapInfo'])
    price=dereference(location['price'])
    annual_rate=re.fullmatch(r'([\d .,]+) руб\. за м[²2] в год',plain(str(dereference(sidebar['perPrice']))))
    rent=None
    if annual_rate and type(price) in (int,float) and math.isfinite(price) and price>0:
        expected_price=float(annual_rate[1].replace(' ','').replace(',','.'))*area
        if math.isclose(price,expected_price,rel_tol=.005,abs_tol=2):rent=price/12
    status=str(dereference(object_at(sidebar['tenderStatusInfo'])['statusText']))
    end=datetime.fromisoformat(dereference(sidebar['requestEndDate']).replace('Z','+00:00'))
    if end.tzinfo is None:raise ValueError('Moscow auction deadline is not dated')
    details={}
    for index in dereference(card['objectInfo']):
        row=object_at(index)
        details[str(dereference(row['label']))]=str(dereference(row['value']))
    floors=parse_floor_values(details.get('Этаж'))
    floor=floors[0] if len(floors)==1 else None
    facts={'status':status,'title':plain(str(dereference(header['title']))),
           'listing_parameters':'\n'.join(f'{key}: {value}' for key,value in details.items()),
           'auction_terms':f'Приём заявок до {end.isoformat()}. Цена торгов определяется аукционом.'}
    if end<=datetime.now(timezone.utc):facts['archive_status']='Срок подачи заявок истёк'
    if rent is not None:facts['auction_start']=f'Годовая стартовая ставка: {price:g} ₽'
    if floor is not None:facts['floor']=f'Этаж предлагаемого помещения: {floor}'
    image_info=object_at(card['imageInfo'])
    images=[dereference(row['url']) for index in dereference(image_info['attachedImages'])
            if str(dereference((row:=object_at(index))['tenderId']))==expected]
    return dict(url=url,unit_id=expected,address=address,area_sqm=area,price_month=rent,floor=floor,
                facts=facts,identity_checked=True,photos=unit_photos(images,url),auction_start_only=True)


def cian_unit(body,url,prop):
    """Read the offer-card's own state, excluding recommendations and widgets.

    This is JSON decoding of a known public config assignment, never JS eval.
    A tracking query is not part of a CIAN unit's numeric URL identity.
    """
    def unit_id(value):
        parsed=urlparse(value)
        if parsed.hostname not in CIAN_HOSTS:
            raise ValueError('Unexpected CIAN host')
        match=re.fullmatch(r'/rent/commercial/(\d+)/?',parsed.path)
        if not match:raise ValueError('Not a commercial rental unit URL')
        return match.group(1)
    expected=unit_id(prop.get('url',''))
    if unit_id(url)!=expected or str(prop.get('external_id'))!=expected:
        raise ValueError('CIAN URL identity mismatch')
    offers=[]
    pattern=(r"window\._cianConfig\[['\"]frontend-offer-card['\"]\]\s*=\s*"
             r"\(window\._cianConfig\[['\"]frontend-offer-card['\"]\]\s*\|\|\s*\[\]\)\.concat\(")
    for script in re.findall(r'<script\b[^>]*>(.*?)</script>',body,re.S|re.I):
        for match in re.finditer(pattern,script):
            entries,_=json.JSONDecoder().raw_decode(script[match.end():].lstrip())
            if not isinstance(entries,list):raise ValueError('Invalid CIAN config')
            for entry in entries:
                if not isinstance(entry,dict) or entry.get('key')!='defaultState':continue
                state=entry.get('value')
                if not isinstance(state,dict):raise ValueError('Invalid CIAN state')
                offer=(state.get('offerData') or {}).get('offer')
                if isinstance(offer,dict):offers.append(offer)
    if len(offers)!=1:raise ValueError('No unique CIAN offer-card state')
    offer=offers[0]
    if str(offer.get('id'))!=expected or str(offer.get('cianId',offer.get('id')))!=expected:
        raise ValueError('CIAN offer identity mismatch')
    if offer.get('dealType')!='rent' or offer.get('offerType')!='commercial':
        raise ValueError('CIAN unit is not a commercial rental')
    geo=offer.get('geo') or {}
    components=geo.get('address') or []
    if not isinstance(components,list):raise ValueError('Invalid CIAN address')
    parts=[str(v.get('fullName') or '') for v in components
           if isinstance(v,dict) and v.get('type') in ('location','street','house')]
    if not any(isinstance(v,dict) and v.get('type')=='house' for v in components):
        raise ValueError('CIAN exact building address missing')
    address=', '.join(parts)
    if not addresses_match(address,prop.get('address','')):
        raise ValueError('CIAN address mismatch')
    area=float(offer.get('totalArea') or 0)
    if not math.isfinite(area) or area<=0 or abs(area-float(prop['area_sqm']))>.5:
        raise ValueError('CIAN unit area mismatch')
    terms=offer.get('bargainTerms') or {}
    # Prefer the explicit, unrounded rental terms. Unknown units/currencies
    # never silently become monthly rubles.
    rent=None
    if terms.get('currency')=='rur' and terms.get('paymentPeriod') in ('monthly','month','annual'):
        rent=float(terms.get('price') or 0)
        if terms.get('priceType')=='squareMeter':rent*=area
        elif terms.get('priceType')!='all':rent=None
        if rent is not None and terms['paymentPeriod']=='annual':rent/=12
    if rent is None:
        value=offer.get('priceTotalPerMonthRur')
        if type(value) in (int,float):rent=float(value)
    if rent is None or not math.isfinite(rent) or rent<=0:
        raise ValueError('Current CIAN monthly rent missing')
    floor=offer.get('floorNumber')
    if floor is not None and (type(floor) is not int):raise ValueError('Invalid CIAN floor')
    facts={k:plain(v) for k in ('title','description','status')
           if isinstance((v:=offer.get(k)),str) and v.strip()}
    if not facts.get('status'):raise ValueError('Current CIAN publication status missing')
    if (offer.get('flags') or {}).get('isArchived'):
        facts['archive_status']='Объявление находится в архиве'
    facts['area_sqm']=f'Площадь предлагаемого помещения: {area:g} м²'
    facts['price_month']=f'Месячная аренда предлагаемого помещения: {rent:g} рублей'
    if floor is not None:facts['floor']=f'Этаж предлагаемого помещения: {floor}'
    return dict(url=url,unit_id=expected,address=address,area_sqm=area,
                price_month=rent,floor=floor,facts=facts,identity_checked=True,
                photos=unit_photos(offer.get('photos',[]),url))


class PublicPropertySources:
    def __init__(self,analyzer):
        self.analyzer=analyzer
        self.cache=analyzer.environment_source_cache

    def bind_collectors(self,collectors):
        from .collectors.avito import AvitoCollector
        from .collectors.domclick import DomclickCollector
        for collector in collectors:
            if isinstance(collector,AvitoCollector):self.avito_collector=collector
            if isinstance(collector,DomclickCollector):self.domclick_collector=collector

    def _domclick_catalogue(self, prop):
        from .cities import city_for_coordinates
        from .collectors.domclick import DomclickCollector
        city = city_for_coordinates(prop.get('lat'), prop.get('lng'))
        if city is None:
            raise ValueError('Listing city unavailable')
        collector = getattr(self, 'domclick_collector', None)
        if collector is None or collector.city != city:
            collector = DomclickCollector(city=city, proxy=getattr(self.analyzer.map_collector,'proxy',None))
            self.domclick_collector = collector
        item, metadata = collector.public_unit_snapshot(prop['external_id'], timeout=min(15,remaining_budget()))
        return domclick_catalogue_unit(item, metadata, prop)

    @cached_property
    def avito_collector(self):
        from .collectors.avito import AvitoCollector
        return AvitoCollector(proxy=getattr(self.analyzer.map_collector,'proxy',None))

    def _snapshot(self,key,fetch):
        snap=self.cache.get_or_fetch(key,fetch,ttl=300)
        # Avoid spending AI calls on facts that will expire during this stage.
        deadline=AI_DEADLINE.get()
        required=min(45,max(0,deadline-time.monotonic())) if deadline else 15
        if snap.expires_at-time.time()<required+1:
            snap=self.cache.get_or_fetch(key,fetch,ttl=300,force_refresh=True)
        return snap

    def _get(self,url):
        # Only supported publisher hosts or the already collected platform URL.
        # No contact forms, browser actions or authenticated endpoints.
        from curl_cffi import requests
        from .collectors.base import _DirectInterfaceBridge
        proxy=getattr(self.analyzer.map_collector,'proxy',None) or _DirectInterfaceBridge.get_proxy_url()
        try:
            if urlparse(url).hostname in ('www.avito.ru','avito.ru'):
                from .collectors.access_state import SourceAccessError
                if not self.avito_collector.source_access_ready():
                    issue=self.avito_collector.access_issue
                    raise SourceAccessError(issue['kind'],issue['status_code'],issue['retry_after_s'])
                response=self.avito_collector._fetch_page_sync(url,timeout=remaining_budget())
            else:
                with requests.Session(impersonate='chrome120',proxy=proxy,verify=False) as session:
                    response=session.get(url,timeout=remaining_budget(),allow_redirects=True,max_redirects=4)
        except (requests.exceptions.RequestException,TimeoutError) as exc:
            kind={28:'timeout',5:'proxy_dns',6:'dns',7:'connect',35:'tls',60:'tls'}.get(getattr(exc,'code',None),
                'timeout' if isinstance(exc,TimeoutError) else 'transport')
            raise PublicSourceError(kind) from exc
        from .collectors.access_state import response_access_issue,SourceAccessError
        has_data=bool(re.search(r'window\.(?:INITIAL_STATE|__staticRouterHydrationData)\s*=',response.text))
        issue=response_access_issue(response,has_data=has_data)
        if issue and urlparse(url).hostname in ('www.avito.ru','avito.ru'):
            self.avito_collector.record_access_issue(response,has_data=has_data)
        if issue:raise SourceAccessError(issue['kind'],issue['status_code'],issue['retry_after_s'])
        if response.status_code!=200:raise PublicSourceError('http_status',response.status_code)
        if len(response.content)>4000000:raise PublicSourceError('response_too_large')
        return response.text,str(response.url)

    def _publisher_sites(self,prop):
        payload=raw_attributes(prop).get('source_payload',{})
        seller=payload.get('seller',{}) if isinstance(payload,dict) else {}
        company=seller.get('company',{}) if isinstance(seller,dict) else {}
        name=company.get('displayName') or company.get('name')
        sites=[company[k] for k in ('website','site_url','homepage') if isinstance(company.get(k),str)]
        if sites or not name:return sites
        key=self.cache.key('property_publisher_lookup_v1',name=name,lat=prop.get('lat'),lng=prop.get('lng'))
        def discover():
            remaining_budget()
            items=self.analyzer.map_collector._fetch_query_sync(name,prop['lat'],prop['lng'],10000)
            names=lambda v:re.sub(r'\W','',str(v).casefold())
            return list(dict.fromkeys(u for item in items if names(item.get('title'))==names(name)
                for u in item.get('urls',[]) if isinstance(u,str)))
        return self.cache.get_or_fetch(key,discover,ttl=3600,cacheable=bool).value

    def collect(self,prop):
        failures=[];sources=[];errors=[]
        try:sites=self._publisher_sites(prop)
        except Exception as exc:
            sites=[];failures.append('publisher_lookup_unavailable');errors.append(source_error('publisher_lookup',exc))
        lots=set(re.findall(r'\b(?:лот|lot)\s*[№#:\-]?\s*(\d{3,12})\b',prop.get('description',''),re.I))
        hosts={urlparse(s if '://' in s else 'https://'+s).hostname for s in sites}
        # Exact publisher-name discovery + public lot redirect + address/area
        # validation bind this independent primary source to the advertised unit.
        if hosts and hosts<={'mega-realty.ru','www.mega-realty.ru'} and len(lots)==1:
            lot=next(iter(lots));url='https://www.mega-realty.ru/square_redirect.htm?id='+lot
            def fetch_mega():
                body,final=self._get(url)
                if urlparse(final).hostname not in hosts|{'mega-realty.ru','www.mega-realty.ru'}:raise ValueError('Unexpected publisher')
                return mega_unit(body,final,lot,prop)
            try:
                key=self.cache.key('property_unit_mega_v1',lot=lot,address=address_key(prop.get('address','')),area=prop.get('area_sqm'))
                snap=self._snapshot(key,fetch_mega)
                sources.append(dict(**snap.value,**snap.diagnostics()))
            except Exception as exc:
                failures.append('publisher_unit_unavailable_or_identity_mismatch');errors.append(source_error('publisher_unit',exc))
        # A publisher success is enough; avoid a redundant, often blocked portal GET.
        if not sources:
            remaining_budget()
            url=prop.get('url','')
            if url.startswith('//'):url='https:'+url
            host=urlparse(url).hostname or ''
            if host in ('investmoscow.ru','www.investmoscow.ru') and prop.get('source')=='investmoscow':
                identifier=str(prop.get('external_id',''))
                if identifier.isdigit():
                    host='torgi.mos.ru';url=f'https://{host}/tender/{identifier}'
            if host in CIAN_HOSTS | DOMCLICK_HOSTS | {'avito.ru','www.avito.ru','realty.yandex.ru','realty.ya.ru','investmoscow.ru','www.investmoscow.ru','torgi.mos.ru','torgi.gov.ru'}:
                def fetch_portal():
                    if host=='torgi.gov.ru':
                        match=re.fullmatch(r'/new/public/lots/lot/(\d+_\d+)/\(lotInfo:info\)',urlparse(url).path)
                        if not match or match[1]!=str(prop.get('external_id')):
                            raise ValueError('Federal auction URL mismatch')
                        from .collectors.municipal import API
                        body,final=self._get(API+match[1])
                        if urlparse(final).hostname!='torgi.gov.ru':raise ValueError('Unexpected federal auction host')
                        return municipal_unit(json.loads(body),final,prop)
                    try:
                        body,final=self._get(url)
                    except (PublicSourceError, SourceAccessError, ValueError) as exc:
                        if host not in DOMCLICK_HOSTS:
                            raise
                        unit = self._domclick_catalogue(prop)
                        unit['card_access_diagnostic'] = source_error('portal_card', exc)
                        return unit
                    cian_hosts=CIAN_HOSTS;avito_hosts={'avito.ru','www.avito.ru'}
                    yandex_hosts={'realty.ya.ru','realty.yandex.ru'}
                    allowed=(cian_hosts if host in cian_hosts else DOMCLICK_HOSTS if host in DOMCLICK_HOSTS
                             else avito_hosts if host in avito_hosts else yandex_hosts if host in yandex_hosts else {host})
                    if urlparse(final).hostname not in allowed:raise ValueError('Unexpected portal redirect')
                    if host in cian_hosts:return cian_unit(body,final,prop)
                    if host in avito_hosts:return avito_unit(body,final,prop)
                    if host in yandex_hosts:return yandex_unit(body,final,prop)
                    if host=='torgi.mos.ru':return torgi_unit(body,final,prop)
                    return jsonld_unit(body,final,prop)
                try:
                    key=self.cache.key('property_unit_portal_v7',url=url,address=address_key(prop.get('address','')),area=prop.get('area_sqm'))
                    snap=self._snapshot(key,fetch_portal)
                    sources.append(dict(**snap.value,**snap.diagnostics()))
                except Exception as exc:
                    failures.append('portal_unit_unavailable_or_unsupported');errors.append(source_error('portal_unit',exc))
            else:failures.append('source_format_unsupported')
        if not sources and prop.get('source')=='avito':
            try:sources.append(avito_catalogue_snapshot(prop))
            except (ValueError,KeyError,TypeError):pass
        return dict(sources=sources,failures=failures,errors=errors)
