"""Dated building-level resident observations; missing buildings stay unknown."""
import hashlib
import json
import math
from datetime import date
from pathlib import Path

from .storage import haversine_distance
from .territory_population import building_identity


class BuildingPopulationDataset:
    def __init__(self, data, *, today=None):
        if not isinstance(data, dict) or set(data) != {'source', 'as_of', 'measure', 'buildings'}:
            raise ValueError('Invalid population dataset envelope')
        self.today = today
        self.as_of = date.fromisoformat(data['as_of'])
        if not 0 <= ((today or date.today()) - self.as_of).days <= 366:
            raise ValueError('Population observations must be current and dated')
        if not isinstance(data['source'], str) or not data['source'].strip() or data['measure'] != 'residents':
            raise ValueError('Population source and resident units required')
        if not isinstance(data['buildings'], list):
            raise ValueError('Population buildings must be a list')
        self.source = data['source']; self.buildings = {}
        for record in data['buildings']:
            if not isinstance(record, dict) or set(record) != {'building_id', 'lat', 'lng', 'residents'}:
                raise ValueError('Invalid building population observation')
            identifier = record['building_id']
            if not isinstance(identifier, str) or not identifier.strip():
                raise ValueError('Invalid building identity')
            identifier = building_identity(identifier)
            if identifier in self.buildings:
                raise ValueError('Ambiguous building identity')
            lat, lng, residents = record['lat'], record['lng'], record['residents']
            if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (lat, lng))
                    or not -90 <= lat <= 90 or not -180 <= lng <= 180
                    or type(residents) is not int or residents < 0):
                raise ValueError('Invalid population point or resident count')
            self.buildings[identifier] = record

    @classmethod
    def from_file(cls, path):
        body = Path(path).read_bytes()
        if len(body) > 16000000:
            raise ValueError('Population import exceeds size limit')
        dataset = cls(json.loads(body.decode('utf-8-sig')))
        dataset.sha256 = hashlib.sha256(body).hexdigest()
        return dataset

    def lookup(self, building_id, lat, lng):
        if not 0 <= ((self.today or date.today()) - self.as_of).days <= 366:
            return None
        record = self.buildings.get(building_identity(building_id))
        if record is None or haversine_distance(lat, lng, record['lat'], record['lng']) > 30:
            return None
        return record['residents']
