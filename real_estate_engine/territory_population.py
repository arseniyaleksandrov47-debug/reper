"""Population allocation over a whole territory, with joint scenario bounds.

These are capacity-based estimates anchored to dated statistics, not a census
of individual houses. An incomplete inventory never absorbs the whole total.
"""
import math
import re
from datetime import date


def building_identity(value):
    value = str(value or '')
    match = re.fullmatch(r'(\d{6,})_.+', value)
    return match[1] if match else value


def contains_point(geometry, lat, lng):
    def ring_contains(ring):
        inside = False
        for a, b in zip(ring, ring[1:] + ring[:1]):
            x1, y1 = a[:2]; x2, y2 = b[:2]
            if (y1 > lat) != (y2 > lat) and lng < (x2-x1)*(lat-y1)/(y2-y1)+x1:
                inside = not inside
        return inside
    polygons = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
    return any(ring_contains(p[0]) and not any(ring_contains(h) for h in p[1:]) for p in polygons)


def positive(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


class TerritoryPopulationModel:
    def __init__(self, data, *, today=None):
        self.source = data['source']; self.as_of = date.fromisoformat(data['as_of'])
        self.inventory_source = data['inventory_source']; self.inventory_as_of = data['inventory_as_of']
        self.name = data['name']; self.geometry = data['geometry']
        self.population = data['population']; self.complete = data['coverage_complete'] is True
        if (not isinstance(self.source, str) or not self.source.strip() or not self.inventory_source
                or not isinstance(self.name, str) or type(self.population) is not int or self.population < 0
                or not 0 <= ((today or date.today())-self.as_of).days <= 730
                or self.geometry.get('type') not in ('Polygon', 'MultiPolygon')):
            raise ValueError('Invalid or expired territorial population reference')
        # Geometry must be a finite, closed polygon, never an invented radius.
        polygons = [self.geometry['coordinates']] if self.geometry['type'] == 'Polygon' else self.geometry['coordinates']
        if not polygons:
            raise ValueError('Empty territory')
        for polygon in polygons:
            if not polygon:
                raise ValueError('Empty polygon')
            for ring in polygon:
                if (len(ring) < 4 or ring[0] != ring[-1] or any(len(p) != 2 or
                        any(type(v) not in (int, float) or not math.isfinite(v) for v in p)
                        or not -180 <= p[0] <= 180 or not -90 <= p[1] <= 90 for p in ring)):
                    raise ValueError('Invalid territory ring')
        self.buildings = {}
        for row in data['buildings']:
            identifier = building_identity(row['building_id'])
            if (not identifier or identifier in self.buildings or type(row['lat']) not in (int,float)
                    or type(row['lng']) not in (int,float) or not math.isfinite(row['lat']) or not math.isfinite(row['lng'])
                    or not contains_point(self.geometry,row['lat'],row['lng'])):
                raise ValueError('Invalid, duplicated or out-of-territory building')
            self.buildings[identifier] = row
        densities = [r['apartments']/r['volume'] for r in self.buildings.values()
                     if positive(r.get('apartments')) and positive(r.get('volume'))]
        self.weights = {}
        for identifier, row in self.buildings.items():
            apartments = positive(row.get('apartments')); volume = positive(row.get('volume'))
            if apartments is not None:
                bounds = (apartments, apartments)
            elif volume is not None and densities:
                bounds = (volume*min(densities), volume*max(densities))
            else:
                bounds = (0, None)
            self.weights[identifier] = bounds
        self.volume_weights = {}
        for identifier,row in self.buildings.items():
            volume=positive(row.get('volume'));apartments=positive(row.get('apartments'))
            self.volume_weights[identifier]=((volume,volume) if volume is not None else
                (apartments/max(densities),apartments/min(densities)) if apartments is not None and densities else (0,None))

    def _allocate(self, selected, weights):
        inside = [v for k,v in weights.items() if k in selected]
        outside = [v for k,v in weights.items() if k not in selected]
        low_in = sum(v[0] for v in inside); low_out = sum(v[0] for v in outside)
        high_in = sum(v[1] for v in inside) if all(v[1] is not None for v in inside) else None
        high_out = sum(v[1] for v in outside) if all(v[1] is not None for v in outside) else None
        if not self.complete:
            high_out = None
        lower = self.population*low_in/(low_in+high_out) if high_out is not None and low_in+high_out else 0
        upper = (self.population*high_in/(high_in+low_out) if high_in is not None and high_in+low_out
                 else self.population if high_in is None else 0)
        point = None
        if self.complete and high_in is not None and high_out is not None:
            denominator = low_in+high_in+low_out+high_out
            point = self.population*(low_in+high_in)/denominator if denominator else 0
        if self.complete and math.isclose(lower,upper):point=lower
        return dict(lower=lower, upper=upper, point=point)

    def estimate(self, identifiers):
        selected = {building_identity(v) for v in identifiers}
        missing = selected-set(self.buildings)
        scenarios = dict(apartment_capacity=self._allocate(selected,self.weights),
                         building_volume_proxy=self._allocate(selected,self.volume_weights))
        lower = min(s['lower'] for s in scenarios.values())
        upper = max(s['upper'] for s in scenarios.values())
        point = scenarios['apartment_capacity']['point']
        if missing:
            lower,upper,point = 0,self.population,None
        return dict(lower=lower,upper=upper,point=point,status='modeled',territory=self.name,
            source=self.source,as_of=self.as_of.isoformat(),inventory_source=self.inventory_source,
            inventory_as_of=self.inventory_as_of,inventory_complete=self.complete,
            missing_building_ids=sorted(missing),scenario_results=scenarios,
            capacity_coverage=dict(houses=len(self.buildings),apartments_known=sum(positive(r.get('apartments')) is not None for r in self.buildings.values()),
                volume_known=sum(positive(r.get('volume')) is not None for r in self.buildings.values()),
                unresolved_capacity=sum(v[1] is None for v in self.weights.values())),
            modeled=True,range_kind='capacity_allocation_scenarios_not_confidence_interval',
            assumptions=['Population total is a dated territorial anchor, not a current building census',
                'Within each scenario residents are proportional to housing capacity',
                'Missing apartment capacity uses the observed local density envelope, never a fixed city coefficient',
                'Unknown inventory capacity stays unallocated; no uniform growth forecast is applied'],
            reference_age_days=(date.today()-self.as_of).days,
            note='The range describes allocation scenarios; it does not guarantee actual occupancy or present-day population')
