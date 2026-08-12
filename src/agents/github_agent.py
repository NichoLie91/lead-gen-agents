"""GitHub Agent — repo/cloud ops (spec section 3, 4.5).

Responsibilities:
- commit NON-PII state back to the repo (only when git reports changes;
  git identity configured inline; pushes authenticated with GH_PAT via
  an insteadOf URL rewrite)
- trigger ``pipeline.yml`` via GitHub's REST dispatch endpoint
  (POST /repos/{owner}/{repo}/actions/workflows/pipeline.yml/dispatches with
  {"ref": <branch>, "inputs": {"mode": ...}} and Bearer GH_PAT auth), with
  graceful readable errors for 403/404/422 so the bot never crashes
- report pipeline run status for /status and the /run guard (REST)
- persist the /stop flag the pipeline checks between stages

In dry-run mode (no GH_PAT) every operation is a safe no-op so the
pipeline runs offline.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import time

import httpx

from src.core.config import Settings
from src.core.rate_limiter import GitHubRateLimiter, RateLimitExceeded
from src.core.state import StateStore

log = logging.getLogger(__name__)

STATE_FILES = [
    "telegram_offset", "github_ratelimit", "stop_requested",
    "pipeline_running", "last_run", "dedupe", "sheet_mirror",
    "approvals", "inbound_seen", "llm_usage", "gemini_keys",
]

GITHUB_API = "https://api.github.com"
PIPELINE_WORKFLOW = "pipeline.yml"
BOT_POLL_WORKFLOW = "bot-poll.yml"
DEFAULT_BRANCH = "main"
DISPATCH_TIMEOUT = 15.0

# GitHub REST endpoint used to trigger a workflow run:
#   POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches
#   body: {"ref": "main", "inputs": {...}}
# Requires a PAT with the "workflow" scope; a token with only "repo" scope
# returns HTTP 403 (the failure the bot used to crash on).


class GitHubAgent:
    def __init__(self, settings: Settings, state: StateStore, limiter: GitHubRateLimiter | None = None):
        self._settings = settings
        self._state = state
        self._limiter = limiter or GitHubRateLimiter(
            ceiling=settings.github_ceiling_per_hour,
            state_file=str(settings.state_dir / "github_ratelimit.json"),
        )
        self._enabled = bool(settings.gh_pat) and not settings.dry_run

    # ---------- state commits ----------
    def commit_state(self, names: list[str] | None = None) -> bool:
        """Commit changed state files; returns True when a push happened."""
        if not self._enabled:
            return False
        root = self._settings.repo_root
        try:
            # Only act when git actually reports changes (quiet history, spec 8.1.7).
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain", "--", "state/"],
                capture_output=True, env=self._git_env(), text=True, check=False,
            )
            if status.returncode != 0 or not status.stdout.strip():
                return False
            targets = names or STATE_FILES
            # NOTE: pathspecs MUST carry the .json suffix — git add fails with
            # exit 128 on any bare path ("pathspec did not match any files"),
            # which used to silently kill every cloud state commit.
            paths = [f"state/{t}.json" for t in targets
                     if (root / "state" / f"{t}.json").exists()]
            if not paths:
                return False
            subprocess.run(["git", "-C", str(root), "add", "--", *paths],
                           check=True, capture_output=True, env=self._git_env())
            result = subprocess.run(
                ["git", "-C", str(root), "commit", "-m",
                 f"chore(state): update {len(paths)} file(s)"],
                capture_output=True, env=self._git_env(), check=False,
            )
            if result.returncode != 0 and b"nothing to commit" not in result.stderr:
                log.warning("state commit failed: %s", result.stderr.decode(errors="replace"))
                return False
            self._limiter.reserve_git_push()  # a push is ~5 points (spec 8)
            push = subprocess.run(
                ["git", "-C", str(root), "push", "origin", "HEAD"],
                capture_output=True, env=self._git_env(), check=False,
            )
            if push.returncode != 0:
                # Classic ephemeral-job race: another actor (a concurrent
                # pipeline run, the keep-alive chain, the human) moved main
                # between our checkout and push. Rebase onto the new tip and
                # retry once — never give up and leave a stale offset that
                # would make the next poll re-process updates.
                log.warning("state push rejected, retrying with rebase: %s",
                            push.stderr.decode(errors="replace").strip().splitlines()[-1:])
                rebase = subprocess.run(
                    # autostash: a concurrent job may have left other state
                    # files modified in this tree; stash them, rebase, reapply.
                    ["git", "-C", str(root), "-c", "rebase.autoStash=true",
                     "pull", "--rebase", "origin", "main"],
                    capture_output=True, env=self._git_env(), check=False,
                )
                if rebase.returncode != 0:
                    log.warning("state rebase failed: %s",
                                rebase.stderr.decode(errors="replace")[:200])
                    return False
                push = subprocess.run(
                    ["git", "-C", str(root), "push", "origin", "HEAD"],
                    capture_output=True, env=self._git_env(), check=False,
                )
                if push.returncode != 0:
                    log.warning("state push failed after rebase: %s",
                                push.stderr.decode(errors="replace")[:200])
                    return False
            return True
        except Exception as exc:
            log.warning("commit_state failed: %s", exc)
            return False

    def _git_env(self) -> dict:
        env = {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_EDITOR": "true",   # never hang on a rebase editor prompt
            "GIT_AUTHOR_NAME": "lead-gen-agents[bot]",
            "GIT_AUTHOR_EMAIL": "lead-gen-agents[bot]@users.noreply.github.com",
            "GIT_COMMITTER_NAME": "lead-gen-agents[bot]",
            "GIT_COMMITTER_EMAIL": "lead-gen-agents[bot]@users.noreply.github.com",
        }
        if self._settings.gh_pat:
            env["GH_TOKEN"] = self._settings.gh_pat
            # Authenticate git push via https PAT through an insteadOf rewrite.
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = (
                f"url.https://x-access-token:{self._settings.gh_pat}@github.com/.insteadOf"
            )
            env["GIT_CONFIG_VALUE_0"] = "https://github.com/"
        return env

    # ---------- workflow control (REST, not the `gh` CLI) ----------
    def _repo_slug(self) -> str:
        """"owner/repo" from GITHUB_REPOSITORY or the git remote."""
        env_slug = os.environ.get("GITHUB_REPOSITORY", "").strip().strip("/")
        if env_slug and "/" in env_slug:
            return env_slug
        try:
            remote = subprocess.run(
                ["git", "-C", str(self._settings.repo_root), "remote", "get-url", "origin"],
                capture_output=True, text=True, check=False,
            ).stdout.strip()
        except Exception:
            remote = ""
        match = re.search(r"(?:github\.com[/:])([^/]+)/([^/\s]+?)(?:\.git)?$", remote)
        if match:
            return f"{match.group(1)}/{match.group(2)}"
        return ""

    def _dispatch_ref(self) -> str:
        """Branch to dispatch on: GITHUB_REF / GITHUB_REF_NAME, else main."""
        return (
            os.environ.get("GITHUB_REF", "")
            or os.environ.get("GITHUB_REF_NAME", "")
            or DEFAULT_BRANCH
        )

    def _gh_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._settings.gh_pat}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "lead-gen-agents",
        }

    async def trigger_pipeline(self, mode: str = "full") -> str:
        """Dispatch pipeline.yml via GitHub's REST dispatch endpoint.

        Returns a human-readable message the Telegram bot can send back
        verbatim — never raises, so a 403/API error cannot crash the poller.
        """
        if not self._enabled:
            return ("skipped (dry-run / GH_PAT not set) - add a personal access "
                    "token with 'repo' + 'workflow' scopes as the GH_PAT repo "
                    "secret to enable /run and cloud state commits")
        slug = self._repo_slug()
        if not slug:
            return "trigger failed: could not determine owner/repo (set GITHUB_REPOSITORY or git remote origin)"
        url = f"{GITHUB_API}/repos/{slug}/actions/workflows/{PIPELINE_WORKFLOW}/dispatches"
        payload = {"ref": self._dispatch_ref(), "inputs": {"mode": mode}}
        try:
            await self._limiter.reserve(points=5, method="POST", max_wait=30)
        except RateLimitExceeded as exc:
            return f"trigger failed: GitHub API rate budget exhausted ({exc})"
        try:
            async with httpx.AsyncClient(timeout=DISPATCH_TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=self._gh_headers())
        except Exception as exc:
            return f"trigger failed: {exc} (GitHub API unreachable?)"

        if resp.status_code == 204:
            return f"triggered pipeline mode={mode} on {slug}@{self._dispatch_ref()}"
        if resp.status_code == 403:
            return (
                "trigger failed: GitHub rejected the dispatch (403). This usually "
                "means the GH_PAT token is missing the 'workflow' scope. Regenerate "
                "the token in GitHub → Settings → Developer settings → Personal "
                "access tokens with 'repo' AND 'workflow' scopes, then update the "
                "GH_PAT repo secret."
            )
        if resp.status_code == 404:
            return (
                f"trigger failed: workflow '{PIPELINE_WORKFLOW}' not found in {slug} "
                "(404). Check the repo slug and that the workflow file exists on the branch."
            )
        if resp.status_code == 422:
            return (
                "trigger failed: GitHub rejected the dispatch payload (422). Check "
                f"that branch '{self._dispatch_ref()}' exists and inputs match the "
                "workflow_dispatch definition."
            )
        return (
            f"trigger failed: GitHub API returned {resp.status_code} — "
            f"{resp.text[:200]}"
        )

    async def bot_poll_pending(self) -> bool:
        """True when a bot-poll run is already queued or in progress.

        The keep-alive guard: only dispatch the next poll when nothing is
        pending, otherwise the cron + keep-alive combination compounds the
        queue (each run spawns a successor AND the cron fires every 5 min).
        """
        if not self._enabled:
            return False
        slug = self._repo_slug()
        if not slug:
            return False
        url = f"{GITHUB_API}/repos/{slug}/actions/workflows/{BOT_POLL_WORKFLOW}/runs"
        try:
            await self._limiter.reserve(points=1, method="GET", max_wait=15)
            async with httpx.AsyncClient(timeout=DISPATCH_TIMEOUT) as client:
                resp = await client.get(
                    url, params={"per_page": 5},
                    headers=self._gh_headers(),
                )
            # IMPORTANT: skip the run this job is EXECUTING IN. The keep-alive
            # check runs at the start of a poll, while the current run is still
            # "in_progress" — counting it made the guard always return True and
            # silently killed the keep-alive chain (the bot collapsed back to
            # the throttled * /5 cron: hourly polls instead of ~5-minute ones).
            current = os.environ.get("GITHUB_RUN_ID", "")
            for run in (resp.json().get("workflow_runs") or []):
                if str(run.get("id", "")) == str(current):
                    continue
                if run.get("status") in ("queued", "in_progress", "requested", "waiting"):
                    return True
            return False
        except Exception:
            # On any API hiccup, assume nothing is pending so the chain keeps
            # going rather than stalling (the concurrency group still guards
            # against overlaps).
            return False

    async def trigger_bot_poll(self) -> str:
        """Re-dispatch bot-poll.yml via GitHub's REST dispatch endpoint.

        Called by the poll script itself when POLL_KEEPALIVE is on: GitHub
        throttles the * /5 cron (observed gaps of 30-150 min), so each run
        queues the next one to hold a near-5-minute polling chain. The
        bot-poll concurrency group (cancel-in-progress: false) serializes any
        overlap. Never raises — a 403/API error only logs a warning.
        """
        if not self._enabled:
            return "keep-alive skipped (dry-run / GH_PAT not set)"
        slug = self._repo_slug()
        if not slug:
            return "keep-alive failed: could not determine owner/repo"
        url = f"{GITHUB_API}/repos/{slug}/actions/workflows/{BOT_POLL_WORKFLOW}/dispatches"
        payload = {"ref": self._dispatch_ref(), "inputs": {}}
        try:
            await self._limiter.reserve(points=5, method="POST", max_wait=30)
        except RateLimitExceeded as exc:
            return f"keep-alive failed: GitHub API rate budget exhausted ({exc})"
        try:
            async with httpx.AsyncClient(timeout=DISPATCH_TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=self._gh_headers())
        except Exception as exc:
            return f"keep-alive failed: {exc} (GitHub API unreachable?)"
        if resp.status_code == 204:
            return f"next bot-poll queued on {slug}@{self._dispatch_ref()}"
        if resp.status_code == 403:
            return (
                "keep-alive failed: GitHub rejected the dispatch (403). The "
                "GH_PAT token is missing the 'workflow' scope."
            )
        if resp.status_code == 404:
            return (
                f"keep-alive failed: workflow '{BOT_POLL_WORKFLOW}' not found in "
                f"{slug} (404)."
            )
        if resp.status_code == 422:
            return (
                f"keep-alive failed: GitHub rejected the dispatch payload (422) "
                f"on branch '{self._dispatch_ref()}'."
            )
        return (
            f"keep-alive failed: GitHub API returned {resp.status_code} — "
            f"{resp.text[:200]}"
        )

    async def pipeline_in_progress(self) -> bool:
        """True when a pipeline.yml run is currently in_progress (REST)."""
        if not self._enabled:
            return False
        slug = self._repo_slug()
        if not slug:
            return False
        url = f"{GITHUB_API}/repos/{slug}/actions/workflows/{PIPELINE_WORKFLOW}/runs"
        try:
            await self._limiter.reserve(points=1, method="GET", max_wait=15)
            async with httpx.AsyncClient(timeout=DISPATCH_TIMEOUT) as client:
                resp = await client.get(
                    url, params={"status": "in_progress", "per_page": 1},
                    headers=self._gh_headers(),
                )
            return bool((resp.json().get("total_count") or 0) > 0)
        except Exception:
            return False

    # ---------- stop flag ----------
    def set_stop(self) -> None:
        self._state.set("stop_requested", "stop", True)
        self._state.set("stop_requested", "ts", time.time())

    def clear_stop(self) -> None:
        self._state.save_if_changed("stop_requested", {"stop": False, "ts": time.time()})

    def stop_requested(self) -> bool:
        return bool(self._state.get("stop_requested", "stop", False))
