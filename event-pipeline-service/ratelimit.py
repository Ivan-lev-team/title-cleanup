"""
Shared pacing + safe HTTP helpers for the enrichment clients.

Extracted 2026-09-21 after an audit found that only prospeo_search had any
pacing at all. debounce_client, leadmagic_client and seamless_client were being
driven by a 6-worker pool with nothing bounding them -- the same setup that,
on Prospeo, turned 30 calls into 20 x HTTP 429 and made a parallel run SLOWER
than a serial one because every 429 triggered an exponential-backoff sleep.

Also fixes two things every client got wrong independently:

  retry_after_seconds()  Retry-After is allowed to be an HTTP-date, not just a
                         number (RFC 7231). int("Wed, 21 Oct 2026 07:28:00 GMT")
                         raises ValueError inside the retry loop -- a guaranteed
                         crash somewhere across ~100k calls.
  safe_json()            resp.json() on an HTML error page (Cloudflare, proxy)
                         raises JSONDecodeError. Most clients guard it;
                         leadmagic_client had five unguarded calls, any one of
                         which would kill an unattended run.
"""
import email.utils
import threading
import time


class RateLimiter:
    """Token bucket over a sliding window. Thread-safe, shared across workers.

    Bounds the ISSUE RATE, not concurrency -- callers can still have several
    sockets open. That is fine; the limits these APIs publish are per-second
    and per-minute request counts.
    """

    def __init__(self, per_second=None, per_minute=None, name=""):
        self.per_second = per_second
        self.per_minute = per_minute
        self.name = name
        self._lock = threading.Lock()
        self._hits = []

    def acquire(self):
        while True:
            with self._lock:
                now = time.time()
                self._hits[:] = [t for t in self._hits if now - t < 60.0]
                in_sec = sum(1 for t in self._hits if now - t < 1.0)
                ok_sec = self.per_second is None or in_sec < self.per_second
                ok_min = self.per_minute is None or len(self._hits) < self.per_minute
                if ok_sec and ok_min:
                    self._hits.append(now)
                    return
                if self.per_minute is not None and len(self._hits) >= self.per_minute:
                    wait = 60.0 - (now - self._hits[0]) + 0.05
                else:
                    oldest = min(t for t in self._hits if now - t < 1.0)
                    wait = 1.0 - (now - oldest) + 0.02
            time.sleep(max(wait, 0.01))


def retry_after_seconds(resp, default=2):
    """Parse Retry-After whether it is a delta-seconds or an HTTP-date."""
    if resp is None:
        return default
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return default
    try:
        return max(int(float(raw)), 0)
    except (TypeError, ValueError):
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt is not None:
            delta = dt.timestamp() - time.time()
            return max(int(delta), 0)
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def safe_json(resp):
    """resp.json() that returns {} instead of raising on a non-JSON body."""
    if resp is None:
        return {}
    try:
        return resp.json() if resp.content else {}
    except ValueError:
        return {}


# Published limits, one limiter per provider, shared process-wide.
#   Prospeo  : x-second-rate-limit 5 | x-minute-rate-limit 180   (measured)
#   Seamless : 60/min per endpoint, ORG-WIDE (per their docs)
#   LeadMagic/DeBounce: undocumented; these are deliberately conservative.
PROSPEO = RateLimiter(per_second=5, per_minute=180, name="prospeo")
SEAMLESS = RateLimiter(per_second=2, per_minute=55, name="seamless")
# Published on every LeadMagic response: RateLimit-Limit 300/min,
# X-Concurrency-Limit 3000, X-RateLimit-Limit-Daily 100000. Measured latency is
# ~8s per role-finder call, so throughput is latency-bound, not rate-bound --
# the fix is concurrency up to the published ceiling, not a slower limiter.
LEADMAGIC = RateLimiter(per_second=8, per_minute=300, name="leadmagic")
# DeBounce publishes no rate headers. ~1.2s per call; keep a sane ceiling.
DEBOUNCE = RateLimiter(per_second=8, per_minute=400, name="debounce")
