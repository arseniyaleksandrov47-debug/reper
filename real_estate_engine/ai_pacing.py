"""Cancellable shared pacing for one AI account, including provider cooldowns."""
import asyncio
import hashlib
import math
import threading
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

_PACERS={}
_PACERS_LOCK=threading.Lock()


def shared_request_pacer(api_key,*,min_interval,requests_per_minute):
    if type(min_interval) not in (int,float) or not math.isfinite(min_interval) or min_interval<0:
        raise ValueError('AI request interval must be nonnegative and finite')
    if type(requests_per_minute) is not int or requests_per_minute<1:
        raise ValueError('AI request count must be a positive integer')
    identity=hashlib.sha256(api_key.encode()).digest()
    with _PACERS_LOCK:
        if identity not in _PACERS:
            _PACERS[identity]=AIRequestPacer(min_interval,requests_per_minute)
        pacer=_PACERS[identity]
        with pacer._state:
            pacer.min_interval=max(pacer.min_interval,min_interval)
            pacer.requests_per_minute=min(pacer.requests_per_minute,requests_per_minute)
        return pacer


def retry_after_seconds(value, default=30.0):
    try:
        seconds=float(value)
    except (TypeError,ValueError):
        try:seconds=(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds()
        except (TypeError,ValueError,OverflowError):return default
    return max(0.0,seconds) if math.isfinite(seconds) else default


class AIRequestPacer:
    def __init__(self,min_interval=1.6,requests_per_minute=40):
        if type(min_interval) not in (int,float) or not math.isfinite(min_interval) or min_interval<0:
            raise ValueError('AI request interval must be nonnegative and finite')
        self.min_interval=min_interval
        self._baseline_interval=min_interval
        if type(requests_per_minute) is not int or requests_per_minute<1:
            raise ValueError('AI request count must be a positive integer')
        self.requests_per_minute=requests_per_minute
        self._http_starts=deque()
        self._last_start=None
        self._not_before=0.0
        self._state=threading.Lock()
        self.http_attempts=0
        self.throttle_responses=0
        self._last_throttle=None
        self._success_streak=0

    async def wait(self):
        while True:
            with self._state:
                interval_due=0 if self._last_start is None else self._last_start+self.min_interval
                delay=max(interval_due,self._not_before)-time.monotonic()
                if delay<=0:
                    self._last_start=time.monotonic()
                    return
            await asyncio.sleep(delay)

    def wait_http_slot(self,timeout):
        """Hard rolling-window ceiling also covers synchronous calls and fallbacks."""
        from .ai_contract import AITransportError
        deadline=time.monotonic()+timeout
        while True:
            with self._state:
                now=time.monotonic()
                while self._http_starts and self._http_starts[0]<=now-60:self._http_starts.popleft()
                due=self._not_before
                if len(self._http_starts)>=self.requests_per_minute:due=max(due,self._http_starts[0]+60)
                delay=due-now
                if delay<=0:
                    self._http_starts.append(now)
                    self.http_attempts+=1
                    return
            remaining=deadline-time.monotonic()
            if remaining<=0 or delay>remaining:
                raise AITransportError('AI rate-limit wait exceeds request budget',status_code=429,retryable=True,retry_after=max(0,delay),phase='rate_limit_queue')
            time.sleep(delay)

    def throttled(self,retry_after):
        with self._state:
            now=time.monotonic()
            self._not_before=max(self._not_before,now+retry_after)
            # Concurrent in-flight refusals belong to one cooldown episode.
            if self._last_throttle is None or now-self._last_throttle>=30:
                self.min_interval=min(10.0,max(2.0,self.min_interval*1.5))
                self._last_throttle=now
            self.throttle_responses+=1
            self._success_streak=0

    def succeeded(self):
        with self._state:
            self._success_streak+=1
            if self._success_streak>=8:
                self.min_interval=max(self._baseline_interval,self.min_interval*.8)
                self._success_streak=0
