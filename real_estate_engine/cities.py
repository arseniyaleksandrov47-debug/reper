"""City geography and source identifiers, independent of business/scoring rules."""
from dataclasses import dataclass
import math
import os
import re
import sys
from pathlib import Path


def _migrate_legacy_runtime_data(legacy_root: Path, target_root: Path) -> None:
    """Copy saved DPAPI keys, SQLite DBs, and caches once from legacy GeoEstate AppData folder."""
    import shutil

    if not legacy_root.is_dir():
        return
    for folder_name in (".local-secrets", "data", ".local-cache"):
        src_dir = legacy_root / folder_name
        if not src_dir.is_dir():
            continue
        dst_dir = target_root / folder_name
        for src in src_dir.rglob("*"):
            if not src.is_file() or src.name.endswith(("-wal", "-shm")):
                continue
            dst = dst_dir / src.relative_to(src_dir)
            if dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(f".{dst.name}.tmp")
            shutil.copy2(src, tmp)
            tmp.replace(dst)


def runtime_root():
    """Return the writable data root for installed/frozen builds or repo root locally."""
    if getattr(sys, 'frozen', False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / 'installed.flag').is_file() or not os.access(exe_dir, os.W_OK):
            local_app = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local'))
            target = local_app / 'Reper'
            target.mkdir(parents=True, exist_ok=True)
            _migrate_legacy_runtime_data(local_app / 'GeoEstate', target)
            return target
        return exe_dir
    return Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class CityProfile:
    key: str
    name: str
    aliases: tuple
    center: tuple
    bounds: tuple  # Broad ingestion envelope, not an administrative polygon.
    search_radius_m: float
    cian_region: int
    cian_host: str
    domclick_address: str | None
    avito_slug: str
    realty_slug: str
    maps_id: int
    maps_slug: str
    dgis_slug: str
    municipal_source: str | None = None
    registry_regions: tuple = ()
    domclick_host: str = 'domclick.ru'
    metro_entrance_source: str | None = None

    def contains(self, lat, lng):
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in (lat, lng)):
            return False
        return self.bounds[0] <= lat <= self.bounds[1] and self.bounds[2] <= lng <= self.bounds[3]

    @property
    def maps_url(self):
        return f'https://yandex.ru/maps/{self.maps_id}/{self.maps_slug}'

    @property
    def database_path(self):
        return runtime_root() / 'data' / f'{self.key}_commercial.db'


MOSCOW = CityProfile(
    'moscow', 'Москва', ('москва', 'moscow', 'мск'), (55.7558, 37.6173),
    (55.0, 57.0, 36.0, 39.0), 25000, 1, 'www.cian.ru',
    '1d1463ae-c80f-4d19-9331-a1b68a85b553', 'moskva', 'moskva',
    213, 'moscow', 'moscow', 'investmoscow', ('77', '50'))
SAINT_PETERSBURG = CityProfile(
    'saint_petersburg', 'Санкт-Петербург',
    ('санкт-петербург', 'санкт петербург', 'петербург', 'питер', 'спб',
     'saint_petersburg', 'saint-petersburg', 'saint petersburg', 'st petersburg', 'spb'),
    (59.9391, 30.3159), (59.3, 60.4, 29.3, 31.3), 25000,
    2, 'spb.cian.ru', '7b4698a7-f8b8-424c-9195-e24f3ddb88f3',
    'sankt-peterburg', 'sankt-peterburg', 2, 'saint-petersburg', 'spb', 'gis_torgi', ('78', '47'), 'spb.domclick.ru', metro_entrance_source='osm')
CITIES = {city.key: city for city in (MOSCOW, SAINT_PETERSBURG)}


def normalize_city(value):
    return ' '.join(value.casefold().replace('ё', 'е').split())


def get_city(value=None):
    if isinstance(value, CityProfile):
        if CITIES.get(value.key) != value:
            raise ValueError('Город не зарегистрирован')
        return value
    if value is None:
        return MOSCOW
    if not isinstance(value, str):
        raise ValueError('Укажите поддерживаемый город')
    key = normalize_city(value)
    for city in CITIES.values():
        if key in (city.key, *city.aliases):
            return city
    raise ValueError(f'Город «{value}» пока не подключён')


def city_for_coordinates(lat, lng):
    return next((city for city in CITIES.values() if city.contains(lat, lng)), None)


def city_in_text(value):
    if not isinstance(value, str):
        return None
    text = normalize_city(value)
    for city in CITIES.values():
        for alias in (city.key, *city.aliases):
            if re.search(r'(?<![\w])' + re.escape(alias) + r'(?![\w])', text):
                return city
    return None
