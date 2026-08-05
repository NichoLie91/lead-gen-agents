"""GitHub REST dispatch tests (spec 4.5): readable 403/error replies, no crash."""
from __future__ import annotations

import asyncio

import pytest

from src.agents import github_agent
from src.agents.github_agent import GitHubAgent
from src.core.config import Settings
from src.core.state import StateStore


class FakeResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}


class FakeClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        if asyncio.iscoroutinefunction(self._response):
            return await self._response()
        return self._response

    async def get(self, *args, **kwargs):
        if asyncio.iscoroutinefunction(self._response):
            return await self._response()
        return self._response


class FakeLimiter:
    """Stand-in for GitHubRateLimiter: no persistence, never blocks."""

    async def reserve(self, points: int = 1, method: str = "GET", max_wait: float = 60.0):
        return None


@pytest.fixture
def github(monkeypatch, tmp_path) -> GitHubAgent:
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/lead-gen")
    monkeypatch.setenv("GITHUB_REF", "main")
    settings = Settings(repo_root=tmp_path, gh_pat="ghp_test", dry_run=False)
    return GitHubAgent(settings, StateStore(tmp_path), limiter=FakeLimiter())


def test_dispatch_403_is_readable(github, monkeypatch):
    monkeypatch.setattr(
        github_agent.httpx, "AsyncClient",
        lambda **kw: FakeClient(FakeResponse(403, "Resource not accessible by integration")),
    )
    reply = asyncio.run(github.trigger_pipeline("full"))
    assert "403" in reply
    assert "workflow" in reply  # points the operator at the missing scope


def test_dispatch_404_is_readable(github, monkeypatch):
    monkeypatch.setattr(
        github_agent.httpx, "AsyncClient",
        lambda **kw: FakeClient(FakeResponse(404, "Not Found")),
    )
    reply = asyncio.run(github.trigger_pipeline("full"))
    assert "404" in reply
    assert "pipeline.yml" in reply


def test_dispatch_204_reports_success(github, monkeypatch):
    monkeypatch.setattr(
        github_agent.httpx, "AsyncClient",
        lambda **kw: FakeClient(FakeResponse(204, "")),
    )
    reply = asyncio.run(github.trigger_pipeline("outreach-email"))
    assert "triggered" in reply
    assert "outreach-email" in reply


def test_dispatch_transport_error_is_readable(github, monkeypatch):
    class Boom(Exception):
        pass

    async def explode(*args, **kwargs):
        raise Boom("connection refused")

    monkeypatch.setattr(github_agent.httpx, "AsyncClient", lambda **kw: FakeClient(explode))
    reply = asyncio.run(github.trigger_pipeline("full"))
    assert "trigger failed" in reply


def test_pipeline_in_progress_uses_total_count(github, monkeypatch):
    def fake_json():
        return {"total_count": 1, "workflow_runs": [{"id": 1}]}

    resp = FakeResponse(200, "")
    resp.json = fake_json
    monkeypatch.setattr(github_agent.httpx, "AsyncClient", lambda **kw: FakeClient(resp))
    assert asyncio.run(github.pipeline_in_progress()) is True


def test_disabled_agent_is_noop(tmp_path):
    settings = Settings(repo_root=tmp_path, gh_pat="", dry_run=False)
    github = GitHubAgent(settings, StateStore(tmp_path))
    assert "skipped" in asyncio.run(github.trigger_pipeline("full"))
    assert asyncio.run(github.pipeline_in_progress()) is False
