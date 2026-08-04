"""Persistent JSON state for the system.

PII POLICY (spec section 11): the repo is PUBLIC, so only non-PII data may be
written into ``state/``. Lead PII (names, emails, phones, addresses) lives ONLY
in the private Google Sheet. Dedupe keys are stored as sha256 hashes.

Files:
    telegram_offset.json      - getUpdates offset (survives across poll jobs)
    github_ratelimit.json     - rate-limit budget (survives across jobs)
    stop_requested.json       - /stop flag consumed by a running pipeline
    pipeline_running.json     - run marker for /status + /run guards
    last_run.json             - most recent run report (metrics only)
    dedupe.json               - {"keys": [sha256(name|address), ...]}
    sheet_mirror.json         - local sheet mirror used in dry-run mode
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class StateStore:
    """Load/save JSON state files atomically, with change detection."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict | list] = {}

    def path(self, name: str) -> Path:
        if not name.endswith(".json"):
            name = f"{name}.json"
        return self.root / name

    def load(self, name: str, default: Any = None) -> Any:
        if name in self._cache:
            return self._cache[name]
        p = self.path(name)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                self._cache[name] = data
                return data
            except (json.JSONDecodeError, OSError):
                pass
        self._cache[name] = default
        return default

    def save(self, name: str, data: Any) -> None:
        self._cache[name] = data
        p = self.path(name)
        fd, tmp = tempfile.mkstemp(dir=str(self.root), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, p)  # atomic: a crashed job never corrupts state
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def save_if_changed(self, name: str, data: Any) -> bool:
        """Save only if serialized content changed; returns True when changed.

        The GitHub Agent uses the return value to decide whether to commit,
        keeping git history quiet (spec 8.1.7).
        """
        prev = self.load(name, None)
        if prev is not None and prev == data:
            return False
        self.save(name, data)
        return True

    def get(self, name: str, key: str, default: Any = None) -> Any:
        data = self.load(name, {})
        if not isinstance(data, dict):
            return default
        return data.get(key, default)

    def set(self, name: str, key: str, value: Any) -> bool:
        data = self.load(name, {})
        if not isinstance(data, dict):
            data = {}
        data[key] = value
        return self.save_if_changed(name, data)
