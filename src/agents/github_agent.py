"""GitHub Agent — repo/cloud ops (spec section 3, 4.5).

Responsibilities:
- commit NON-PII state back to the repo (only when git reports changes;
  git identity configured inline; pushes authenticated with GH_PAT via
  an insteadOf URL rewrite)
- trigger ``pipeline.yml`` via ``gh workflow run`` (modes: full / outreach-email / ...)
- report pipeline run status for /status and the /run guard
- persist the /stop flag the pipeline checks between stages

In dry-run mode (no GH_PAT) every operation is a safe no-op so the
pipeline runs offline.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time

from src.core.config import Settings
from src.core.rate_limiter import GitHubRateLimiter
from src.core.state import StateStore

log = logging.getLogger(__name__)

STATE_FILES = [
    "telegram_offset", "github_ratelimit", "stop_requested",
    "pipeline_running", "last_run", "dedupe", "sheet_mirror",
]


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
            paths = [f"state/{t}" for t in targets if (root / "state" / f"{t}.json").exists()]
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
                log.warning("state push failed: %s", push.stderr.decode(errors="replace"))
                return False
            return True
        except Exception as exc:
            log.warning("commit_state failed: %s", exc)
            return False

    def _git_env(self) -> dict:
        env = {
            "GIT_TERMINAL_PROMPT": "0",
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

    # ---------- workflow control ----------
    async def trigger_pipeline(self, mode: str = "full") -> str:
        if not self._enabled:
            return "skipped (dry-run / GH_PAT not set)"
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "workflow", "run", "pipeline.yml", "-f", f"mode={mode}",
                env={**os.environ, "GH_TOKEN": self._settings.gh_pat},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                return f"trigger failed: {stderr.decode(errors='replace').strip()}"
            return f"triggered pipeline mode={mode}"
        except Exception as exc:
            return f"trigger failed: {exc}"

    async def pipeline_in_progress(self) -> bool:
        if not self._enabled:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "run", "list", "--workflow", "pipeline.yml",
                "--status", "in_progress", "--limit", "1",
                env={**os.environ, "GH_TOKEN": self._settings.gh_pat},
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            return bool(out.decode(errors="replace").strip())
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
