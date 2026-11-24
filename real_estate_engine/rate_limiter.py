import asyncio
import time
import random
import logging
from typing import Dict

logger = logging.getLogger("RateLimiter")

class AdaptiveRateLimiter:
    """
    Intelligent rate limiter with domain isolation, randomized jitter,
    and exponential backoff on HTTP 429/403 to prevent bans.
    """
    def __init__(self, default_min_delay: float = 0.8, default_max_delay: float = 2.0):
        self.default_min = default_min_delay
        self.default_max = default_max_delay
        self._last_request_time: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._domain_locks: Dict[str, asyncio.Lock] = {}
        self._backoff_until: Dict[str, float] = {}

    def _get_lock(self, domain: str) -> asyncio.Lock:
        if domain not in self._domain_locks:
            self._domain_locks[domain] = asyncio.Lock()
        return self._domain_locks[domain]

    async def wait_before_request(self, domain: str, min_delay: float = None, max_delay: float = None):
        """Ensures human-like, randomized pacing before hitting a specific platform."""
        min_d = min_delay if min_delay is not None else self.default_min
        max_d = max_delay if max_delay is not None else self.default_max

        lock = self._get_lock(domain)
        async with lock:
            now = time.monotonic()

            # 1. Check if domain is under exponential backoff cooldown
            if domain in self._backoff_until:
                cooldown_remaining = self._backoff_until[domain] - now
                if cooldown_remaining > 0:
                    logger.warning(f"[{domain}] Pacing cooldown active: waiting {cooldown_remaining:.2f}s...")
                    await asyncio.sleep(cooldown_remaining)
                del self._backoff_until[domain]

            # 2. Enforce jittered delay between consecutive requests to the same domain
            last_time = self._last_request_time.get(domain, 0.0)
            elapsed = time.monotonic() - last_time
            jittered_delay = random.uniform(min_d, max_d)

            if elapsed < jittered_delay:
                sleep_needed = jittered_delay - elapsed
                logger.debug(f"[{domain}] Pacing: sleep {sleep_needed:.2f}s (jitter target {jittered_delay:.2f}s)")
                await asyncio.sleep(sleep_needed)

            self._last_request_time[domain] = time.monotonic()

    def record_status(self, domain: str, status_code: int):
        """Notifies limiter of response status to dynamically adjust backoff."""
        if status_code in (429, 403):
            # Backoff 15-30s on rate-limit / captcha trigger
            backoff_sec = random.uniform(15.0, 30.0)
            self._backoff_until[domain] = time.monotonic() + backoff_sec
            logger.warning(f"[{domain}] Received HTTP {status_code}! Triggering {backoff_sec:.1f}s backoff.")
