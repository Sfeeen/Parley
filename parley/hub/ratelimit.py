"""Token-bucket rate limiting for the Hub (SPEC §12.1).

Why a token bucket rather than a fixed window: an agent that batches 64 events
once a second is well-behaved, but a fixed window would punish it for landing on
a boundary.  A bucket lets short bursts through (that is the point of ``burst``)
while holding the long-run average to the configured rate.

``Limiter`` owns the only lock in this module and it is a *leaf* lock: nothing
inside this module ever calls back out, so it can be taken while holding any
other Hub lock without risking a cycle.
"""

from __future__ import annotations

import logging
import threading
from typing import Dict, Tuple

log = logging.getLogger("parley.hub.ratelimit")

# kind -> (events per minute, burst).  SPEC §12.1 fixes the first two columns;
# the burst for blobs and enrolments is set equal to the rate because the spec
# does not grant them a larger one.
DEFAULT_LIMITS: Dict[str, Tuple[float, float]] = {
    "events": (60.0, 120.0),
    "blobs": (120.0, 120.0),
    "enroll": (10.0, 10.0),
    # Not in the spec, deliberately generous: a backstop so that a buggy client
    # hammering /v1/state cannot pin a core.  Set to 0 in `limits` to disable.
    "request": (600.0, 1200.0),
}

#: Buckets untouched for this long are discarded so a long-running Hub that has
#: seen many source addresses does not grow without bound.
_IDLE_EVICT_S = 900.0


class TokenBucket:
    """A single bucket.  Not thread-safe on its own; ``Limiter`` guards it."""

    __slots__ = ("rate_per_s", "burst", "_tokens", "_updated")

    def __init__(self, rate_per_min: float, burst: float) -> None:
        self.rate_per_s = float(rate_per_min) / 60.0
        self.burst = float(burst)
        self._tokens = float(burst)
        self._updated = -1.0

    @property
    def last_used(self) -> float:
        return self._updated

    def take(self, now: float, n: float = 1.0) -> float:
        """Consume ``n`` tokens.

        Returns ``0.0`` when the request is allowed, otherwise the number of
        seconds the caller should wait (the ``Retry-After`` value).
        """
        if self._updated < 0.0:
            self._updated = now
        elapsed = now - self._updated
        if elapsed > 0.0:
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate_per_s)
        self._updated = now

        if n <= 0.0:
            return 0.0
        if n > self.burst:
            # Impossible to ever satisfy; refuse rather than stall forever.
            return 0.0 if self.rate_per_s <= 0.0 else max(1.0, n / self.rate_per_s)
        if n <= self._tokens:
            self._tokens -= n
            return 0.0
        if self.rate_per_s <= 0.0:
            return 3600.0
        deficit = n - self._tokens
        # Round up to whole seconds: Retry-After is expressed in seconds and a
        # client that rounds down would immediately bounce again.
        wait = deficit / self.rate_per_s
        return float(max(1, int(wait + 0.999)))


class Limiter:
    """Per-key, per-kind buckets.

    ``key`` is the agent id for authenticated traffic and the source address for
    enrolment (SPEC §12.1 says enrolment is limited *per source address*, because
    before enrolment there is no agent id to limit).
    """

    def __init__(self, limits: "Dict[str, Tuple[float, float]] | None" = None) -> None:
        self._limits = dict(DEFAULT_LIMITS)
        if limits:
            self._limits.update(limits)
        self._buckets: Dict[Tuple[str, str], TokenBucket] = {}
        self._lock = threading.Lock()
        self._last_evict = 0.0

    def check(self, key: str, kind: str, now: float, n: float = 1.0) -> float:
        """0.0 when allowed, otherwise the Retry-After in seconds."""
        spec = self._limits.get(kind)
        if not spec or spec[0] <= 0.0:
            return 0.0
        slot = (kind, key)
        with self._lock:
            bucket = self._buckets.get(slot)
            if bucket is None:
                bucket = TokenBucket(spec[0], spec[1])
                self._buckets[slot] = bucket
            wait = bucket.take(now, n)
            self._evict_locked(now)
        if wait:
            log.info("rate limited key=%s kind=%s retry_after=%.0fs", key, kind, wait)
        return wait

    def forget(self, key: str) -> None:
        """Drop every bucket for a key (used when an agent is revoked)."""
        with self._lock:
            for slot in [s for s in self._buckets if s[1] == key]:
                self._buckets.pop(slot, None)

    def _evict_locked(self, now: float) -> None:
        if now - self._last_evict < 60.0:
            return
        self._last_evict = now
        stale = [s for s, b in self._buckets.items() if now - b.last_used > _IDLE_EVICT_S]
        for s in stale:
            self._buckets.pop(s, None)
