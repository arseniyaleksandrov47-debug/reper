from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any
from datetime import datetime

class PlatformSource(str, Enum):
    CIAN = "cian"
    AVITO = "avito"
    YANDEX = "yandex"
    DOMCLICK = "domclick"
    INVESTMOSCOW = "investmoscow"
    MUNICIPAL = "municipal"

@dataclass
class CanonicalProperty:
    source: PlatformSource
    external_id: str
    title: str
    address: str
    lat: float
    lng: float
    price_month: float
    price_sqm_year: float
    area_sqm: float
    floor: Optional[int] = None
    floors_total: Optional[int] = None
    has_separate_entrance: bool = False
    power_kw: Optional[float] = None
    photos: List[str] = field(default_factory=list)
    url: str = ""
    phone_token: Optional[str] = None
    description: str = ""
    raw_attributes: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def deduplication_hash(self) -> str:
        """
        Creates a spatial-financial signature to identify the same property
        cross-listed across multiple platforms.
        Rounds lat/lng to ~10 meters accuracy + area within 2 sqm.
        """
        rounded_lat = round(self.lat, 4)
        rounded_lng = round(self.lng, 4)
        rounded_area = round(self.area_sqm / 2.0) * 2.0
        return f"{rounded_lat}_{rounded_lng}_{rounded_area}_{int(self.price_month / 5000) * 5000}"
