"""Dated station-flow references; never silently promote legacy constants."""
import json
import math
from datetime import date
from pathlib import Path


class MetroFlowDataset:
    def __init__(self,data,*,today=None,max_age_days=366):
        required={'source','as_of','bounds','measure','stations'}
        if not isinstance(data,dict) or not required<=set(data) or set(data)-required-{'station_variants'}:raise ValueError('Invalid metro dataset envelope')
        if not isinstance(data['source'],str) or not data['source'].strip():raise ValueError('Metro data needs a source')
        if data['measure'] not in ('entries_per_day','entries_and_exits_per_day','passengers_per_day_unspecified'):raise ValueError('Declare daily station-flow units')
        self.as_of=date.fromisoformat(data['as_of']);self.today=today or date.today();self._today_override=today
        if type(max_age_days) is not int or max_age_days<1:raise ValueError('Metro reference age must be positive days')
        if self.as_of>self.today:raise ValueError('Metro reference date cannot be in the future')
        b=data['bounds']
        if (not isinstance(b,list) or len(b)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in b)
                or not -90<=b[0]<b[1]<=90 or not -180<=b[2]<b[3]<=180):raise ValueError('Invalid metro geographical bounds')
        if not isinstance(data['stations'],dict):raise ValueError('Metro stations must be a map')
        self.stations={}
        for name,value in data['stations'].items():
            key=self.normalize(name)
            if not isinstance(name,str) or not key or key in self.stations or type(value) not in (int,float) or not math.isfinite(value) or value<0:
                raise ValueError('Invalid or ambiguous station-flow value')
            self.stations[key]=value
        self.bounds=b;self.source=data['source'];self.measure=data['measure'];self.max_age_days=max_age_days
        variants=data.get('station_variants',{})
        if not isinstance(variants,dict):raise ValueError('Invalid station variants')
        self.station_variants={}
        for name,values in variants.items():
            key=self.normalize(name)
            if not isinstance(name,str) or not key or key in self.stations or key in self.station_variants or not isinstance(values,list) or len(values)<2:raise ValueError('Invalid station variants')
            lines=set();counts=[]
            for value in values:
                if not isinstance(value,dict) or set(value)!={'line','entries_per_day'}:raise ValueError('Invalid line observation')
                line=value['line'];count=value['entries_per_day']
                if (not isinstance(line,str) or not line.strip() or line in lines or type(count) not in (int,float) or not math.isfinite(count) or count<0):raise ValueError('Invalid line observation')
                lines.add(line);counts.append(count)
            self.station_variants[key]=(min(counts),max(counts))

    @staticmethod
    def normalize(name):return ' '.join(str(name).lower().replace('ё','е').split())

    @classmethod
    def from_file(cls,path):return cls(json.loads(Path(path).read_text(encoding='utf-8-sig')))

    def lookup(self,name,lat,lng):
        if self.measure=='passengers_per_day_unspecified':return None
        if ((self._today_override or date.today())-self.as_of).days>self.max_age_days:return None
        if not self.bounds[0]<=lat<=self.bounds[1] or not self.bounds[2]<=lng<=self.bounds[3]:return None
        return self.stations.get(self.normalize(name))

    def lookup_interval(self,name,lat,lng):
        value=self.lookup(name,lat,lng)
        if value is not None:return value,value
        if ((self._today_override or date.today())-self.as_of).days>self.max_age_days:return None
        if not self.bounds[0]<=lat<=self.bounds[1] or not self.bounds[2]<=lng<=self.bounds[3]:return None
        if self.measure=='passengers_per_day_unspecified':
            reported=self.stations.get(self.normalize(name))
            return (0,reported) if reported is not None else None
        return self.station_variants.get(self.normalize(name))
