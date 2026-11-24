"""Reusable public source facts, independent of business decisions and scores."""
import copy
import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass

from .source_cache import SourceCache


@dataclass(frozen=True)
class SourceSnapshot:
    value: object
    fetched_at: float
    expires_at: float
    cache_status: str

    def diagnostics(self):
        return dict(fetched_at=self.fetched_at,expires_at=self.expires_at,
                    age_seconds=round(max(0,time.time()-self.fetched_at),3),cache_status=self.cache_status)


class EnvironmentSourceCache:
    """One in-process fetch per exact source key; validated JSON persists on disk.

    Only the short memory/future bookkeeping holds a common lock. Network and
    SQLite I/O are outside it. Partial/failed data may be returned to current
    waiters, but is never saved for the next search. Each caller gets a copy.
    """
    def __init__(self,path='.local-cache/environment_sources.sqlite3',*,max_entries=512,clock=time.time):
        self.disk=SourceCache(path,max_entries=max_entries) if path else None
        self.max_entries=max_entries;self.clock=clock
        self._memory=OrderedDict();self._flights={};self._lock=threading.Lock()

    @staticmethod
    def key(source,**parameters):
        encoded=json.dumps(parameters,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()
        return source+':'+hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _copy(record,status):
        value,fetched,expires=record
        return SourceSnapshot(copy.deepcopy(value),fetched,expires,status)

    def get_or_fetch(self,key,fetch,*,ttl,cacheable=lambda value:True,force_refresh=False):
        if not math.isfinite(ttl) or ttl<=0:raise ValueError('Source TTL must be positive and finite')
        hit=None;flight_key=(key,bool(force_refresh))
        with self._lock:
            cached=self._memory.get(key)
            if cached and not force_refresh and min(cached[2],cached[1]+ttl)>self.clock():
                self._memory.move_to_end(key);hit=(cached[0],cached[1],min(cached[2],cached[1]+ttl))
            else:
                if cached and cached[2]<=self.clock():self._memory.pop(key,None)
                future=self._flights.get(flight_key)
                owner=future is None
                if owner:
                    future=Future();self._flights[flight_key]=future
        if hit is not None:return self._copy(hit,'memory')
        if not owner:return self._copy(future.result(),'shared')
        try:
            record=None;status='fresh'
            if self.disk is not None and not force_refresh:
                cached=self.disk.get(key)
                if cached:
                    try:
                        saved=json.loads(cached[1]);fetched=saved['fetched_at'];expires=saved['expires_at'];value=saved['value']
                        if (type(fetched) in (int,float) and type(expires) in (int,float) and
                                math.isfinite(fetched) and math.isfinite(expires) and fetched<=self.clock()<min(expires,fetched+ttl) and
                                cacheable(value)):
                            record=(value,fetched,min(expires,fetched+ttl));status='disk'
                    except (ValueError,TypeError,KeyError):pass
            if record is None:
                value=fetch();fetched=self.clock()
                # Validate before serialization/publication. A healthy source
                # is reusable; an incomplete source is retried next time.
                reusable=cacheable(value)
                record=(value,fetched,fetched+ttl if reusable else fetched)
                if reusable:
                    try:
                        body=json.dumps(dict(value=value,fetched_at=fetched,expires_at=record[2]),ensure_ascii=False,allow_nan=False).encode()
                        if len(body)>16000000:raise ValueError('Snapshot exceeds size bound')
                    except (TypeError,ValueError):record=(value,fetched,fetched)
                    else:
                        if self.disk is not None:self.disk.put(key,body,ttl)
            with self._lock:
                if record[2]>self.clock():
                    old=self._memory.get(key)
                    if old is None or old[1]<=record[1]:self._memory[key]=record
                    self._memory.move_to_end(key)
                    while len(self._memory)>self.max_entries:self._memory.popitem(last=False)
                future.set_result(record)
            return self._copy(record,status)
        except BaseException as exc:
            future.set_exception(exc)
            raise
        finally:
            with self._lock:
                if self._flights.get(flight_key) is future:self._flights.pop(flight_key)
