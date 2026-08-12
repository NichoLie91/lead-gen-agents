"""GitHub REST dispatch tests (spec 4.5): readable 403/error replies, no crash."""
from __future__ import annotations

import asyncio
import subprocess

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
    assert "skipped" in asyncio.run(github.trigger_bot_poll())
    assert asyncio.run(github.pipeline_in_progress()) is False


def test_trigger_bot_poll_204_reports_queued(github, monkeypatch):
    monkeypatch.setattr(
        github_agent.httpx, "AsyncClient",
        lambda **kw: FakeClient(FakeResponse(204, "")),
    )
    reply = asyncio.run(github.trigger_bot_poll())
    assert "queued" in reply
    assert "bot-poll.yml" in reply or "acme/lead-gen" in reply


def test_trigger_bot_poll_403_is_readable(github, monkeypatch):
    monkeypatch.setattr(
        github_agent.httpx, "AsyncClient",
        lambda **kw: FakeClient(FakeResponse(403, "Resource not accessible by integration")),
    )
    reply = asyncio.run(github.trigger_bot_poll())
    assert "keep-alive failed" in reply
    assert "403" in reply


def test_bot_poll_pending_true_when_run_in_progress(github, monkeypatch):
    def fake_json():
        return {"total_count": 1, "workflow_runs": [{"id": 1, "status": "in_progress"}]}

    resp = FakeResponse(200, "")
    resp.json = fake_json
    monkeypatch.setattr(github_agent.httpx, "AsyncClient", lambda **kw: FakeClient(resp))
    assert asyncio.run(github.bot_poll_pending()) is True


def test_bot_poll_pending_false_when_all_completed(github, monkeypatch):
    def fake_json():
        return {"total_count": 1, "workflow_runs": [{"id": 1, "status": "completed"}]}

    resp = FakeResponse(200, "")
    resp.json = fake_json
    monkeypatch.setattr(github_agent.httpx, "AsyncClient", lambda **kw: FakeClient(resp))
    assert asyncio.run(github.bot_poll_pending()) is False


def test_bot_poll_pending_false_on_api_error(github, monkeypatch):
    class Boom(Exception):
        pass

    async def explode(*args, **kwargs):
        raise Boom("rate limited")

    monkeypatch.setattr(github_agent.httpx, "AsyncClient", lambda **kw: FakeClient(explode))
    # On API hiccup the guard fails OPEN so the chain keeps going.
    assert asyncio.run(github.bot_poll_pending()) is False


def test_bot_poll_pending_excludes_current_run(github, monkeypatch):
    """The keep-alive guard must NOT count the run it is executing in.

    Regression: the check runs at the start of a poll while THIS run is still
    "in_progress", so counting it made bot_poll_pending() always True and the
    keep-alive chain silently never dispatched (bot collapsed to the throttled
    cron, ~hourly polls instead of ~5-minute ones).
    """
    monkeypatch.setenv("GITHUB_RUN_ID", "42")

    def fake_json():
        return {"total_count": 1, "workflow_runs": [{"id": 42, "status": "in_progress"}]}

    resp = FakeResponse(200, "")
    resp.json = fake_json
    monkeypatch.setattr(github_agent.httpx, "AsyncClient", lambda **kw: FakeClient(resp))
    assert asyncio.run(github.bot_poll_pending()) is False  # current run only

    # A DIFFERENT run in progress still blocks the dispatch.
    def fake_json2():
        return {"total_count": 2, "workflow_runs": [
            {"id": 42, "status": "in_progress"},
            {"id": 43, "status": "in_progress"},
        ]}

    resp2 = FakeResponse(200, "")
    resp2.json = fake_json2
    monkeypatch.setattr(github_agent.httpx, "AsyncClient", lambda **kw: FakeClient(resp2))
    assert asyncio.run(github.bot_poll_pending()) is True

    # A queued successor (not the current run) also blocks the dispatch.
    def fake_json3():
        return {"total_count": 2, "workflow_runs": [
            {"id": 42, "status": "in_progress"},
            {"id": 44, "status": "queued"},
        ]}

    resp3 = FakeResponse(200, "")
    resp3.json = fake_json3
    monkeypatch.setattr(github_agent.httpx, "AsyncClient", lambda **kw: FakeClient(resp3))
    assert asyncio.run(github.bot_poll_pending()) is True


def _git(*args: str, cwd) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args],
                          capture_output=True, text=True, check=True).stdout


def test_commit_state_pushes_state_files(tmp_path):
    """commit_state must stage state/*.json (with the .json suffix) and push.

    Regression test: pathspecs without the .json extension made git add fail
    with exit 128, silently killing every cloud state commit.
    """
    # Working repo with an initial commit and a local bare remote to push to.
    _git("init", "-b", "main", cwd=tmp_path)
    _git("config", "user.email", "bot@example.com", cwd=tmp_path)
    _git("config", "user.name", "bot", cwd=tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "approvals.json").write_text('{"v": 1}')
    (state_dir / "last_run.json").write_text('{"status": "RUNNING"}')
    (state_dir / "gemini_keys.json").write_text('{"key1": {"failures": 1}}')
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True)
    _git("remote", "add", "origin", str(bare), cwd=tmp_path)
    _git("push", "-u", "origin", "main", cwd=tmp_path)

    # Simulate a state change the bot would commit (e.g. a new approval and a
    # freshly parked Gemini key).
    (state_dir / "approvals.json").write_text('{"v": 2}')
    (state_dir / "gemini_keys.json").write_text('{"key1": {"failures": 2}}')
    settings = Settings(repo_root=tmp_path, gh_pat="ghp_test", dry_run=False)
    github = GitHubAgent(settings, StateStore(tmp_path))

    assert github.commit_state() is True

    # The remote must now carry the updated approvals.json AND gemini_keys.json
    # (the persisted Gemini key-switch file must survive ephemeral jobs).
    _git("fetch", "origin", cwd=tmp_path)
    fetched = _git("show", "origin/main:state/approvals.json", cwd=tmp_path)
    assert fetched.strip() == '{"v": 2}'
    fetched_keys = _git("show", "origin/main:state/gemini_keys.json", cwd=tmp_path)
    assert fetched_keys.strip() == '{"key1": {"failures": 2}}'


def test_commit_state_retries_push_with_rebase(monkeypatch, tmp_path):
    """A rejected push ("fetch first" race with another job) must rebase and
    retry once — never give up and leave a stale offset."""
    import subprocess as sp

    _git("init", "-b", "main", cwd=tmp_path)
    _git("config", "user.email", "bot@example.com", cwd=tmp_path)
    _git("config", "user.name", "bot", cwd=tmp_path)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "telegram_offset.json").write_text('{"offset": 1}')
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)
    bare = tmp_path / "bare.git"
    sp.run(["git", "init", "--bare", str(bare)], check=True)
    _git("remote", "add", "origin", str(bare), cwd=tmp_path)
    _git("push", "-u", "origin", "main", cwd=tmp_path)

    (state_dir / "telegram_offset.json").write_text('{"offset": 2}')
    settings = Settings(repo_root=tmp_path, gh_pat="ghp_test", dry_run=False)
    github = GitHubAgent(settings, StateStore(tmp_path))

    calls: list[list[str]] = []
    real_run = sp.run
    rebase_seen = {"done": False}

    def fake_run(args, *a, **kw):
        calls.append(list(args))
        is_push = "push" in args and "origin" in args and "HEAD" in args
        # Fail the FIRST push ("fetch first", as if another job moved main);
        # after the pull --rebase, allow the retried push through.
        if is_push and not rebase_seen["done"]:
            return sp.CompletedProcess(args, 1,
                                       b"", b"! [rejected] HEAD -> main (fetch first)\n")
        if "pull" in args and "--rebase" in args:
            rebase_seen["done"] = True
        return real_run(args, *a, **kw)

    monkeypatch.setattr(sp, "run", fake_run)
    assert github.commit_state() is True
    assert rebase_seen["done"] is True           # a rebase was attempted
    assert sum(1 for c in calls
               if "push" in c and "origin" in c and "HEAD" in c) == 2
