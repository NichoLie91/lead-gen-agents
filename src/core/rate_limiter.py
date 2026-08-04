"""GitHub API rate limiter (spec section 8).

Design: conservative token-bucket ceiling + server-header awareness (the
``x-ratelimit-*`` headers are ground truth) + secondary-limit guard (900
pts/min, 100 concurrent) + Retry-After-first exponential backoff + persisted
budget so it survives across the many ephemeral GitHub Actions jobs.

The limiter never makes HTTP calls itself: an ``executor`` callable is injected
(the GitHub Agent supplies one that shells out to ``gh api -i``). This keeps
the class fully unit-testable with a fake clock and fake responses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# --- constants (spec 8.3) ---
CEILING_DEFAULT = 4000            # requests/hour headroom under GitHub's 5000
REFILL_RATE = CEILING_DEFAULT / 3600.0
POINTS_BY_METHOD = {
    "GET": 1, "HEAD": 1, "OPTIONS": 1,
    "PUT": 5, "POST": 5, "PATCH": 5, "DELETE": 5,
}
GIT_PUSH_POINTS = 5
SECONDARY_POINT_MAX = 900
SECONDARY_WINDOW_SEC = 60
CONCURRENCY_MAX = 100
MAX_RETRIES = 6
BACKOFF_BASE_SEC = 1.0
BACKOFF_MAX_SEC = 60.0

RATE_LIMIT_STATUSES = {429}                    # plus 403 with rate-limit headers
RESPONSE_KEYS = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")


class RateLimitExceeded(Exception):
    """Raised when a request would have to wait longer than ``max_wait``."""


@dataclass
class GhResponse:
    """Minimal response shape returned by an executor."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str = ""


Executor = Callable[[str, str, dict, dict], Awaitable[GhResponse]]


class GitHubRateLimiter:
    def __init__(
        self,
        ceiling: int = CEILING_DEFAULT,
        state_file: str | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._ceiling = ceiling
        self._clock = clock
        self._sem = asyncio.Semaphore(CONCURRENCY_MAX)
        self._lock = asyncio.Lock()
        self._window: deque[tuple[float, int]] = deque()   # (ts, points) last 60s
        self._etags: dict[str, str] = {}

        self._state_file = state_file
        self._bucket = float(ceiling)
        self._last_refill = self._clock()
        self._server_limit: int | None = None
        self._server_remaining: int | None = None
        self._server_reset: float | None = None
        self._server_seen = False
        if state_file:
            self._load()

    # ---------- persistence ----------
    def _load(self) -> None:
        try:
            with open(self._state_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._bucket = float(data.get("bucket_tokens", self._ceiling))
            self._last_refill = float(data.get("last_refill_ts", self._clock()))
            srv = data.get("server", {})
            self._server_limit = srv.get("limit")
            self._server_remaining = srv.get("remaining")
            self._server_reset = srv.get("reset")
            self._server_seen = bool(srv.get("seen", False))
            self._window = deque((float(ts), int(pts)) for ts, pts in data.get("secondary", []))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    def _persist(self) -> None:
        if not self._state_file:
            return
        payload = {
            "version": 1,
            "updated_at": self._clock(),
            "ceiling_per_hour": self._ceiling,
            "bucket_tokens": round(self._bucket, 4),
            "last_refill_ts": self._last_refill,
            "server": {
                "limit": self._server_limit,
                "remaining": self._server_remaining,
                "reset": self._server_reset,
                "seen": self._server_seen,
            },
            "secondary": [[ts, pts] for ts, pts in self._window],
        }
        try:
            import os
            import tempfile

            path = self._state_file
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("rate limiter persist failed: %s", exc)

    # ---------- token bucket ----------
    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        self._bucket = min(float(self._ceiling), self._bucket + elapsed * REFILL_RATE)
        self._last_refill = now

    def _wait_for_ceiling(self, points: int, now: float) -> float:
        needed = points - self._bucket
        return needed / REFILL_RATE if needed > 0 else 0.0

    # ---------- server headers (ground truth) ----------
    def update_from_headers(self, headers: Mapping[str, str]) -> None:
        h = {k.lower(): v for k, v in headers.items()}
        for key in RESPONSE_KEYS:
            if key in h:
                try:
                    value = float(h[key])
                except (TypeError, ValueError):
                    continue
                if key == "x-ratelimit-limit":
                    self._server_limit = int(value)
                elif key == "x-ratelimit-remaining":
                    self._server_remaining = int(value)
                else:
                    self._server_reset = value
        self._server_seen = True
        if self._server_remaining is not None:
            self._bucket = min(self._bucket, float(self._server_remaining))

    # ---------- secondary limits ----------
    def _record_secondary(self, now: float, points: int) -> None:
        self._window.append((now, points))
        while self._window and self._window[0][0] <= now - SECONDARY_WINDOW_SEC:
            self._window.popleft()

    def _secondary_wait(self, now: float, points: int) -> float:
        recent = [p for p in self._window if p[0] > now - SECONDARY_WINDOW_SEC]
        used = sum(p for _, p in recent) + points
        if used <= SECONDARY_POINT_MAX or not recent:
            return 0.0
        oldest = min(ts for ts, _ in recent)
        return oldest + SECONDARY_WINDOW_SEC - now

    def effective_wait(self, points: int, now: float, max_wait: float) -> float:
        """Seconds to sleep before a request of ``points`` is safe."""
        w1 = self._wait_for_ceiling(points, now)
        w2 = 0.0
        if (
            self._server_seen
            and self._server_remaining is not None
            and (self._server_remaining <= 0 or self._server_remaining < points)
        ):
            w2 = max(0.0, (self._server_reset or now) - now)
        w3 = self._secondary_wait(now, points)
        wait = max(w1, w2, w3)
        if wait > max_wait:
            raise RateLimitExceeded(f"needed to wait {wait:.0f}s (> max_wait {max_wait:.0f}s)")
        return wait

    # ---------- public API ----------
    async def reserve(self, points: int = 1, method: str = "GET", max_wait: float = 3600.0) -> None:
        """Block until a request of ``points`` is safe, then consume the budget.

        Does NOT manage the concurrency semaphore: callers that issue actual
        HTTP requests use ``guarded_gh_api`` which owns acquire/release.
        """
        points = POINTS_BY_METHOD.get(method.upper(), points)
        async with self._lock:
            now = self._clock()
            self._refill(now)
            wait = self.effective_wait(points, now, max_wait)
        if wait > 0:
            await asyncio.sleep(wait)
        async with self._lock:
            now = self._clock()
            self._refill(now)
            self._bucket -= points
            self._record_secondary(now, points)
            self._persist()

    def release(self) -> None:
        try:
            self._sem.release()
        except ValueError:
            pass

    def _refund(self, points: int) -> None:
        """Give back reserved points (e.g. a 304 that GitHub did not count)."""
        self._bucket = min(float(self._ceiling), self._bucket + points)
        if self._window:  # best-effort: drop the newest entry from the window
            self._window.pop()
        self._persist()

    async def guarded_gh_api(
        self,
        executor: Executor,
        method: str,
        path: str,
        headers: dict | None = None,
        body: dict | None = None,
        max_retries: int = MAX_RETRIES,
        max_wait: float = 3600.0,
    ) -> GhResponse:
        """The single choke point every GitHub API call goes through."""
        for attempt in range(max_retries + 1):
            hdrs = dict(headers or {})
            if method in ("GET", "HEAD") and path in self._etags:
                hdrs["If-None-Match"] = self._etags[path]
            await self.reserve(points=1, method=method, max_wait=max_wait)
            await self._sem.acquire()  # concurrency cap: 100 (spec 8.3)
            try:
                resp = await executor(method, path, hdrs, body or {})
            finally:
                self.release()
            self.update_from_headers(resp.headers)
            self._persist()

            if resp.status == 304:  # Not Modified: GitHub does not count these (spec 8.3)
                self._refund(1)
                return resp
            if resp.status in RATE_LIMIT_STATUSES or (
                resp.status == 403 and self._is_rate_limited(resp)
            ):
                retry_after = resp.headers.get("retry-after")
                await self._backoff(attempt, retry_after)
                continue
            if resp.status >= 500:
                await self._backoff(attempt)
                continue
            etag = resp.headers.get("etag")
            if etag:
                self._etags[path] = etag
            return resp
        raise RateLimitExceeded(f"{method} {path} after {max_retries} retries")

    def reserve_git_push(self, max_wait: float = 3600.0) -> None:
        """Reserve points for a git push synchronously (spec 8.3, note 3)."""
        # Git pushes happen via subprocess; reserve before pushing.
        self._bucket -= GIT_PUSH_POINTS
        self._record_secondary(self._clock(), GIT_PUSH_POINTS)
        self._persist()

    # ---------- internals ----------
    @staticmethod
    def _is_rate_limited(resp: GhResponse) -> bool:
        h = {k.lower(): v for k, v in resp.headers.items()}
        return "x-ratelimit-remaining" in h or "retry-after" in h

    async def _backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                await asyncio.sleep(float(retry_after))
                return
            except (TypeError, ValueError):
                pass
        delay = min(BACKOFF_MAX_SEC, BACKOFF_BASE_SEC * (2 ** attempt))
        await asyncio.sleep(delay + random.uniform(0, 0.5 * delay))

    # ---------- introspection ----------
    @property
    def remaining_budget(self) -> int:
        return max(0, int(self._bucket))
