"""Central configuration: environment variables + ``config/criteria.json``.

Every component reads settings from here. Secrets are read from the
environment (GitHub Actions secrets in production); when a secret is missing
the related component degrades gracefully (offline / dry-run mode).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv optional for CI (secrets come from Actions)
    load_dotenv = None

REPO_ROOT = Path(__file__).resolve().parents[2]

# Lead-criteria defaults (spec section 7.1)
DEFAULT_VERTICALS = ["plumber", "hvac", "cleaning", "mechanic", "dental"]
DEFAULT_METROS = [
    "Houston", "Tampa", "Phoenix", "Indianapolis", "Atlanta", "Charlotte",
    "Orlando", "Denver", "San Antonio", "Las Vegas", "Nashville", "Memphis",
]
DEFAULT_CRITERIA = {
    "verticals": DEFAULT_VERTICALS,
    "metros": DEFAULT_METROS,
    "rating_min": 4.0,
    "reviews_min": 5,
    "reviews_max": 2000,
    "raw_pool_cap": 250,
    "target_leads": 250,
    "per_vertical_cap_pct": 40,
    "hot_threshold": 90,
    "warm_threshold": 70,
    "emails_per_run_max": 50,
    "ig_dms_per_24h_max": 15,
    "query_shape": "small {vertical} business {city} no website phone email",
}


@dataclass
class Settings:
    repo_root: Path = REPO_ROOT
    # --- secrets (empty when absent -> components run offline) ---
    telegram_bot_token: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    composio_api_key: str = ""
    gh_pat: str = ""
    google_sheet_id: str = ""
    admin_telegram_ids: list[int] = field(default_factory=list)
    telegram_alert_chat_id: str = ""
    # --- knobs ---
    poll_max_wait_sec: float = 300.0
    pipeline_max_wait_sec: float = 3600.0
    github_ceiling_per_hour: int = 4000
    dry_run: bool = False
    # --- criteria ---
    criteria: dict = field(default_factory=lambda: dict(DEFAULT_CRITERIA))

    @classmethod
    def load(cls, env: dict | None = None) -> Settings:
        # Local dev: load .env (gitignored) when no explicit env mapping is given.
        if env is None and load_dotenv is not None:
            load_dotenv(REPO_ROOT / ".env", override=False)
        env = os.environ if env is None else env
        settings = cls(
            telegram_bot_token=env.get("TELEGRAM_BOT_TOKEN", ""),
            gemini_api_key=env.get("GEMINI_API_KEY", ""),
            gemini_model=env.get("GEMINI_MODEL", "gemini-2.5-flash"),
            composio_api_key=env.get("COMPOSIO_API_KEY", ""),
            gh_pat=env.get("GH_PAT", ""),
            google_sheet_id=env.get("GOOGLE_SHEET_ID", ""),
            telegram_alert_chat_id=env.get("TELEGRAM_ALERT_CHAT_ID", ""),
            poll_max_wait_sec=float(env.get("POLL_MAX_WAIT_SEC", "300")),
            pipeline_max_wait_sec=float(env.get("PIPELINE_MAX_WAIT_SEC", "3600")),
            github_ceiling_per_hour=int(env.get("GITHUB_CEILING_PER_HOUR", "4000")),
            dry_run=env.get("DRY_RUN", "") in ("1", "true", "True"),
        )
        raw_admin = env.get("ADMIN_TELEGRAM_IDS", "")
        settings.admin_telegram_ids = [int(x) for x in raw_admin.split(",") if x.strip()]
        settings.criteria = cls._load_criteria(settings.repo_root / "config" / "criteria.json")
        return settings

    @staticmethod
    def _load_criteria(path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        merged = dict(DEFAULT_CRITERIA)
        merged.update(data or {})
        return merged

    # --- criteria accessors ---
    @property
    def verticals(self) -> list[str]:
        return list(self.criteria.get("verticals", DEFAULT_VERTICALS))

    @property
    def metros(self) -> list[str]:
        return list(self.criteria.get("metros", DEFAULT_METROS))

    def crit(self, key: str, default=None):
        return self.criteria.get(key, default)

    @property
    def state_dir(self) -> Path:
        return self.repo_root / "state"
