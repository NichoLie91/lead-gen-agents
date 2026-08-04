"""GitHubRateLimiter tests (spec 8.3). Uses a fake clock; no network."""
import asyncio
import json
import os
import tempfile

import pytest

from src.core.rate_limiter import GhResponse, GitHubRateLimiter, RateLimitExceeded


class FakeClock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _limiter(clock, ceiling=4000, state_file=None):
    return GitHubRateLimiter(ceiling=ceiling, state_file=state_file, clock=clock)


def test_bucket_refills_over_time():
    clock = FakeClock()
    lim = _limiter(clock)
    assert lim.remaining_budget == 4000
    clock.advance(3600)  # one full hour -> fully refilled
    asyncio.run(lim.reserve(points=100, method="CUSTOM"))  # non-standard method -> explicit points
    assert lim.remaining_budget == 3900


def test_method_point_mapping():
    clock = FakeClock()
    lim = _limiter(clock)
    asyncio.run(lim.reserve(points=1, method="GET"))    # GET = 1 point
    assert lim.remaining_budget == 3999
    asyncio.run(lim.reserve(points=1, method="POST"))   # POST = 5 points
    assert lim.remaining_budget == 3994


def test_effective_wait_for_ceiling():
    clock = FakeClock()
    lim = _limiter(clock)
    # Drain the bucket.
    for _ in range(4000):
        lim._bucket -= 1
    wait = lim.effective_wait(1, clock(), max_wait=3600)
    assert wait > 0  # must wait for refill


def test_effective_wait_raises_when_over_max():
    clock = FakeClock()
    lim = _limiter(clock)
    lim._bucket = 0
    try:
        lim.effective_wait(10, clock(), max_wait=5)
    except RateLimitExceeded as exc:
        assert "max_wait" in str(exc)
    else:
        raise AssertionError("expected RateLimitExceeded")


def test_server_headers_clamp_bucket():
    clock = FakeClock()
    lim = _limiter(clock)
    lim.update_from_headers({
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": "37",
        "x-ratelimit-reset": str(clock() + 1000),
    })
    assert lim.remaining_budget == 37  # server remaining wins over bucket


def test_server_exhausted_forces_wait():
    clock = FakeClock()
    lim = _limiter(clock)
    reset = clock() + 600
    lim.update_from_headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)})
    wait = lim.effective_wait(1, clock(), max_wait=3600)
    assert wait == 600


def test_secondary_window_eviction():
    clock = FakeClock()
    lim = _limiter(clock)
    # Push 900 points into the window.
    for _ in range(900):
        lim._record_secondary(clock(), 1)
    wait = lim.effective_wait(1, clock(), max_wait=3600)
    assert wait == 60  # oldest point ages out in 60s
    clock.advance(61)
    assert lim.effective_wait(1, clock(), max_wait=3600) == 0  # window drained


def test_retry_after_beats_backoff_and_no_retry_after_backoff():
    async def run():
        clock = FakeClock()
        lim = _limiter(clock)
        calls = []

        async def executor_429(_m, _p, _h, _b):
            calls.append("call")
            return GhResponse(status=429, headers={"retry-after": "0"})

        with pytest.raises(RateLimitExceeded):
            await lim.guarded_gh_api(executor_429, "GET", "/rate", max_retries=2)
        assert len(calls) == 3  # initial + 2 retries, then gives up

    asyncio.run(run())


def test_304_consumes_no_retries_and_no_points():
    async def run():
        clock = FakeClock()
        lim = _limiter(clock)
        calls = []

        async def executor(_m, _p, _h, _b):
            calls.append("call")
            return GhResponse(status=304, headers={"x-ratelimit-remaining": "4000"})

        resp = await lim.guarded_gh_api(executor, "GET", "/unchanged", max_retries=2)
        assert resp.status == 304
        assert len(calls) == 1
        assert lim.remaining_budget == 4000  # reserved point was refunded

    asyncio.run(run())


def test_persisted_budget_reloads():
    clock = FakeClock()
    with tempfile.TemporaryDirectory() as tmp:
        state_file = os.path.join(tmp, "github_ratelimit.json")
        lim = _limiter(clock, state_file=state_file)
        lim._bucket = 1234.0
        lim._persist()

        reloaded = _limiter(clock, state_file=state_file)
        assert reloaded.remaining_budget == 1234


def test_persist_file_is_valid_json():
    clock = FakeClock()
    with tempfile.TemporaryDirectory() as tmp:
        state_file = os.path.join(tmp, "github_ratelimit.json")
        lim = _limiter(clock, state_file=state_file)
        lim.update_from_headers({"x-ratelimit-remaining": "2500"})
        lim._persist()
        with open(state_file, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data["server"]["remaining"] == 2500
