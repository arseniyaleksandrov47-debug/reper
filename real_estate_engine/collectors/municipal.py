"""Public municipal rental notices, bound to current federal auction records."""
import asyncio
import html
import math
import re
from datetime import datetime, timezone
from urllib.parse import urljoin
from .base import BaseCollector, preserve_source_fields, extract_specs_from_text
from ..cities import get_city
from ..models import CanonicalProperty, PlatformSource
from ..floor_policy import extract_floor_details

INDEX = 'http://gkuim.commim.gov.spb.ru/torgi-i-novosti/'
API = 'http://torgi.gov.ru/new/api/public/lotcards/'


def rental_lot(data, city, *, now=None, active_only=True):
    city=get_city(city); now=now or datetime.now(timezone.utc)
    identifier=str(data.get('id',''))
    if not re.fullmatch(r'\d+_\d+',identifier):
        raise ValueError('Invalid federal lot identity')
    if (data.get('subjectRFCode') not in city.registry_regions[:1] or data.get('typeTransaction')!='rent'
            or (data.get('category') or {}).get('code')!='11'):
        return None
    active=(data.get('lotStatus')=='APPLICATIONS_SUBMISSION' and data.get('isAnnulled') is False
            and data.get('isStopped') is False)
    start=datetime.fromisoformat(data['biddStartTime'].replace('Z','+00:00'))
    end=datetime.fromisoformat(data['biddEndTime'].replace('Z','+00:00'))
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError('Auction dates lack timezone')
    active=active and start <= now < end
    if active_only and not active:
        return None
    if data.get('currencyCode')!='643':
        raise ValueError('Unknown auction currency')
    values=[v.get('characteristicValue') for v in data.get('characteristics',[]) if v.get('code')=='totalAreaRealty']
    if len(values)!=1:
        raise ValueError('Ambiguous municipal unit area')
    area=float(values[0]); price=float(data.get('priceMinExact') or data.get('priceMin') or 0)
    period=[(v.get('value') or {}).get('name','') for v in data.get('attributes',[])
        if str(v.get('code','')).startswith('DA_priceMinFor_') and isinstance(v.get('value'),dict)]
    if len(period)!=1:
        raise ValueError('Rental bid period missing')
    period=period[0].casefold().replace('ё','е')
    if period=='арендный платеж за год':monthly=price/12
    elif period=='арендный платеж за месяц':monthly=price
    else:raise ValueError('Unsupported rental bid period')
    point=data.get('point') or {}; lat=point.get('lat'); lng=point.get('lon')
    address=data.get('estateAddress')
    if (not city.contains(lat,lng) or not isinstance(address,str) or not address.strip()
            or any(not math.isfinite(v) or v<=0 for v in (area,monthly))):
        raise ValueError('Municipal unit geography or numeric facts invalid')
    description=str(data.get('lotDescription') or '')
    details=extract_floor_details(None,description=description)
    specs=extract_specs_from_text(description)
    description+='\nНачальная цена торгов: '+str(price)+' рублей; период: '+period+'. Итоговая аренда определяется торгами.'
    return CanonicalProperty(source=PlatformSource.MUNICIPAL,external_id=identifier,
        title=str(data.get('lotName') or 'Муниципальная аренда'),address=address,lat=lat,lng=lng,
        area_sqm=area,price_month=round(monthly,2),price_sqm_year=round(monthly*12/area,2),
        floor=details['floor'],has_separate_entrance=bool(specs['has_separate_entrance']),power_kw=specs['power_kw'],
        description=description,url=f'https://torgi.gov.ru/new/public/lots/lot/{identifier}/(lotInfo:info)',
        raw_attributes=preserve_source_fields(data,dict(**details,auction_start_year=monthly*12,
            auction_price_period=period,application_deadline=end.isoformat(),auction_active=active,city=city.key)))


class MunicipalRentalCollector(BaseCollector):
    def __init__(self,proxy=None,rate_limiter=None,*,city=None):
        self.city=get_city(city)
        super().__init__('CityAuctions','torgi.gov.ru',proxy=proxy,rate_limiter=rate_limiter,verify=False)

    def _discover(self):
        response=self.session.get(INDEX,timeout=10); response.raise_for_status()
        notices=[]
        for href,label in re.findall(r'<a[^>]+href=["\x27]([^"\x27]+)["\x27][^>]*>(.*?)</a>',response.text,re.S|re.I):
            text=html.unescape(re.sub('<[^>]*>',' ',label))
            if (re.fullmatch(r'/torgi-i-novosti/\d+/',href) and 'аренд' in text.casefold()
                    and not re.search('результат|итог',text,re.I)):
                url=urljoin(INDEX,href)
                if url not in notices:notices.append(url)
        identifiers=[]
        for url in notices[:3]:
            response=self.session.get(url,timeout=10); response.raise_for_status()
            for identifier in re.findall(r'https://torgi\.gov\.ru/new/public/lots/lot/(\d+_\d+)/',response.text):
                if identifier not in identifiers:identifiers.append(identifier)
        return identifiers, len(notices)

    async def collect(self,limit=30):
        self.begin_collection(limit); result=[]
        try:
            identifiers,notices=await asyncio.to_thread(self._discover)
            self.record_parsed_page()
            if notices>3 or len(identifiers)>min(limit,40):
                self.record_collection_error('Муниципальная выборка ограничена глубиной просмотра извещений')
            for identifier in identifiers[:min(limit,40)]:
                await self.pace(min_delay=.3,max_delay=.6)
                response=await asyncio.to_thread(self.session.get,API+identifier,timeout=10)
                self.record_status(response.status_code)
                if response.status_code!=200:
                    self.record_collection_error('Федеральная карточка муниципального лота недоступна')
                    continue
                try:
                    data=response.json()
                    if str(data.get('id'))!=identifier:raise ValueError('Federal lot ID mismatch')
                    prop=rental_lot(data,self.city)
                    if prop:result.append(prop)
                except (ValueError,TypeError,KeyError):self.record_parse_error()
        except Exception as exc:
            self.record_collection_error('Муниципальный источник недоступен: '+type(exc).__name__)
        return result
