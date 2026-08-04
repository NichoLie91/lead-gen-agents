"""Composio Agent — the tool gateway (spec sections 3, 6).

PRIMARY PATH IS THE V3 REST API. Composio deprecated the v1 backend
(HTTP 410 "upgrade to v3 APIs") and the last composio-core SDK on PyPI
(0.7.21) still validates keys against v1, so it cannot be used. We call:

    GET  {base}/connected_accounts            -> connection status
    GET  {base}/tools?toolkit_slug=X&limit=.. -> tool catalog (slug resolution)
    POST {base}/tools/execute/{tool_slug}     -> run an action

Body shape for execute (discovered from the v3 OpenAPI spec):
    {"connected_account_id": "...", "user_id": "default",
     "arguments": {action-specific params}}

When ``COMPOSIO_API_KEY`` is absent the agent reports connections as
NOT_CONFIGURED and every tool call raises ``ComposioNotConfigured``.
"""
from __future__ import annotations

import logging

import httpx

from src.core.config import Settings

log = logging.getLogger(__name__)

V3_BASE = "https://backend.composio.dev/api/v3"
EXECUTE_TIMEOUT = 90.0

# Slug resolution table (spec 6.2): purpose -> candidate action slugs.
# Verified against the live v3 catalog (2026-08): the googlesheets toolkit has
# NO add-tab action, so each tab lives in its own spreadsheet
# (GOOGLESHEETS_SHEET_FROM_JSON) and tabs are rewritten via clear-range +
# GOOGLESHEETS_BATCH_UPDATE (named-tab write). Reads use GOOGLESHEETS_BATCH_GET.
SLUG_ALIASES: dict[str, list[str]] = {
    "send_email": ["GMAIL_SEND_EMAIL", "GMAIL_SEND_MESSAGE"],
    "create_draft": ["GMAIL_CREATE_EMAIL_DRAFT", "GMAIL_CREATE_DRAFT"],
    "send_draft": ["GMAIL_SEND_DRAFT"],
    "sheet_create_named": ["GOOGLESHEETS_SHEET_FROM_JSON"],
    "sheet_clear": ["GOOGLESHEETS_CLEAR_VALUES"],
    "sheet_values_update": ["GOOGLESHEETS_BATCH_UPDATE"],
    "sheet_get_info": ["GOOGLESHEETS_GET_SPREADSHEET_INFO", "GOOGLESHEETS_GET_SHEET_NAMES"],
    "sheet_read": ["GOOGLESHEETS_BATCH_GET"],
    "maps_search": ["SERPAPI_GOOGLE_MAPS_SEARCH", "ZENSERP_ZENSERP_GOOGLE_MAPS_SEARCH"],
    "web_search": ["TAVILY_TAVILY_SEARCH", "SERPER_GOOGLE_SEARCH"],
    "fetch_url": ["COMPOSIO_SEARCH_FETCH_URL_CONTENT"],
    "ig_send_dm": ["INSTAGRAM_SEND_TEXT_MESSAGE"],
    "multi_execute": ["COMPOSIO_MULTI_EXECUTE_TOOL"],
    "manage_connections": ["COMPOSIO_MANAGE_CONNECTIONS"],
    "wait_connections": ["COMPOSIO_WAIT_FOR_CONNECTIONS"],
}

# Toolkits whose catalogs we resolve slugs against (per-toolkit fetches return
# the full tool set reliably; a single unfiltered fetch is capped).
RESOLVE_TOOLKITS = (
    "gmail", "googlesheets", "instagram", "github", "serpapi", "zenserp", "tavily",
)

# Map an action slug prefix to the Composio toolkit that owns it (v3 executes
# require the connected account of the owning toolkit).
TOOLKIT_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("GMAIL_", "gmail"),
    ("GOOGLESHEETS_", "googlesheets"),
    ("INSTAGRAM", "instagram"),
    ("GITHUB_", "github"),
    ("SERPAPI_", "serpapi"),
    ("ZENSERP_", "zenserp"),
    ("TAVILY_", "tavily"),
)

REQUIRED_CONNECTIONS = ("googlesheets", "gmail")
OPTIONAL_CONNECTIONS = ("instagram", "github", "serpapi", "zenserp", "tavily")


class ComposioNotConfigured(Exception):
    """Raised when a Composio tool is called without an API key configured."""


class ComposioAgent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.connected = bool(settings.composio_api_key)
        self._toolset = None
        self._slugs: dict[str, str] = {}
        self._slugs_resolved = False
        self._account_by_toolkit: dict[str, str] = {}
        if self.connected:
            self._try_init_sdk()

    # ---------- lifecycle ----------
    def _try_init_sdk(self) -> None:
        """Try the SDK (harmless if it fails); REST v3 is always the fallback."""
        try:
            from composio import ComposioToolSet  # type: ignore[import-not-found]

            self._toolset = ComposioToolSet(api_key=self.settings.composio_api_key)
            self._toolset.get_tools()  # force validation; raises ApiKeyError (410) if broken
        except Exception as exc:
            log.info("composio-core SDK unavailable (%s); using Composio v3 REST API", exc)
            self._toolset = None

    # ---------- helpers ----------
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.settings.composio_api_key}

    def _toolkit_for(self, action: str) -> str:
        upper = action.upper()
        for prefix, toolkit in TOOLKIT_BY_PREFIX:
            if upper.startswith(prefix):
                return toolkit
        return ""

    async def refresh_connections(self) -> None:
        """Build {toolkit_slug: connected_account_id} from the v3 API."""
        self._account_by_toolkit = {}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{V3_BASE}/connected_accounts", headers=self._headers()
                )
            for account in resp.json().get("items", []):
                toolkit = (account.get("toolkit") or {}).get("slug")
                if toolkit and account.get("status") == "ACTIVE":
                    self._account_by_toolkit[toolkit] = account.get("id", "")
        except Exception as exc:
            log.warning("refresh_connections failed: %s", exc)

    def slug(self, purpose: str) -> str:
        return self._slugs.get(purpose) or SLUG_ALIASES.get(purpose, [""])[0]

    async def resolve_slugs(self) -> None:
        """Resolve canonical action slugs against the live v3 catalog.

        Fetches the catalog per toolkit so every candidate list is complete,
        then keeps the first candidate that actually exists. Runs lazily once
        per process before the first tool execution.
        """
        self._slugs = {}
        try:
            available: set[str] = set()
            async with httpx.AsyncClient(timeout=60) as client:
                for toolkit in RESOLVE_TOOLKITS:
                    resp = await client.get(
                        f"{V3_BASE}/tools",
                        params={"toolkit_slug": toolkit, "limit": 1000},
                        headers=self._headers(),
                    )
                    for item in resp.json().get("items", []):
                        available.add(item.get("slug", ""))
            for purpose, candidates in SLUG_ALIASES.items():
                for cand in candidates:
                    if cand in available:
                        self._slugs[purpose] = cand
                        break
            self._slugs_resolved = True
        except Exception as exc:
            log.warning("resolve_slugs failed: %s", exc)

    # ---------- pre-flight ----------
    async def preflight(self) -> dict[str, str]:
        """Return a status map for required + optional connections."""
        statuses = {c: "NOT_CONFIGURED" for c in REQUIRED_CONNECTIONS + OPTIONAL_CONNECTIONS}
        if not self.connected:
            return statuses
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{V3_BASE}/connected_accounts", headers=self._headers()
                )
            if resp.status_code != 200:
                return {c: f"ERROR {resp.status_code}" for c in statuses}
            items = resp.json().get("items", [])
            for account in items:
                toolkit = (account.get("toolkit") or {}).get("slug", "")
                for conn in REQUIRED_CONNECTIONS + OPTIONAL_CONNECTIONS:
                    if conn in toolkit or toolkit in conn:
                        statuses[conn] = "ACTIVE" if account.get("status") == "ACTIVE" else "MISSING"
            return statuses
        except Exception as exc:
            log.warning("preflight failed: %s", exc)
            return {c: "ERROR" for c in statuses}

    # ---------- tool execution (v3 REST) ----------
    async def execute_action(self, action: str, params: dict) -> dict:
        if not self.connected:
            raise ComposioNotConfigured(f"{action} requires COMPOSIO_API_KEY")
        if self.connected and not self._slugs_resolved:
            await self.resolve_slugs()
        if not self._account_by_toolkit:
            await self.refresh_connections()

        body: dict = {"arguments": params or {}, "user_id": "default"}
        toolkit = self._toolkit_for(action)
        account_id = self._account_by_toolkit.get(toolkit, "")
        if toolkit and account_id:
            body["connected_account_id"] = account_id

        try:
            async with httpx.AsyncClient(timeout=EXECUTE_TIMEOUT) as client:
                resp = await client.post(
                    f"{V3_BASE}/tools/execute/{action}",
                    json=body, headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            log.error("Composio action %s transport error: %s", action, exc)
            return {"ok": False, "action": action, "error": str(exc)}

        if resp.status_code != 200:
            log.error("Composio action %s failed: %s", action, resp.text[:300])
            return {"ok": False, "action": action, "error": resp.text[:300]}
        payload = resp.json()
        # Composio returns HTTP 200 even when the action itself failed; the
        # failure is signalled by the body's ``successful`` flag (or an embedded
        # ``http_error``). Treat those as failures (verified against v3 API).
        if payload.get("successful") is False:
            err = payload.get("error") or payload.get("message") or ""
            log.error("Composio action %s failed: %s", action, str(err)[:300])
            return {"ok": False, "action": action, "error": str(err)[:300]}
        data = payload.get("data", payload)
        if isinstance(data, dict) and "http_error" in data:
            log.error("Composio action %s failed: %s", action, str(data)[:300])
            return {"ok": False, "action": action, "error": str(data)[:300]}
        return {"ok": True, "action": action, "data": data}

    # ---------- higher-level tools ----------
    async def search_google_maps(self, query: str, start: int = 0) -> list[dict]:
        resp = await self.execute_action(
            self.slug("maps_search"), {"q": query}
        )
        if not resp.get("ok"):
            return []
        return self._normalize_maps(resp.get("data", {}))

    @staticmethod
    def _normalize_maps(data) -> list[dict]:
        if isinstance(data, dict):
            raw = (data.get("local_results") or data.get("results")
                   or data.get("places") or data.get("response_data"))
            if isinstance(raw, dict):
                raw = raw.get("local_results") or raw.get("results")
        else:
            raw = data
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            out.append({
                "name": item.get("title") or item.get("name", ""),
                "address": item.get("formatted_address") or item.get("address", ""),
                "phone": item.get("formatted_phone_number") or item.get("phone", ""),
                "website": item.get("website") or item.get("website_url", ""),
                "rating": item.get("rating"),
                "reviews": item.get("user_ratings_total") or item.get("reviews"),
                "open_state": item.get("business_status") or item.get("open_state", ""),
            })
        return out

    async def search_web(self, query: str) -> list[dict]:
        resp = await self.execute_action(self.slug("web_search"), {"query": query})
        return resp.get("data", []) if resp.get("ok") else []

    async def fetch_url(self, url: str) -> str:
        resp = await self.execute_action(self.slug("fetch_url"), {"url": url})
        data = resp.get("data")
        return data if isinstance(data, str) else str(data)

    async def gmail_send_email(self, *, to: str, subject: str, body: str) -> dict:
        return await self.execute_action(
            self.slug("send_email"),
            {"userId": "me", "to": [to], "subject": subject, "body": body},
        )

    async def gmail_create_draft(self, *, to: str, subject: str, body: str) -> dict:
        return await self.execute_action(
            self.slug("create_draft"),
            {"userId": "me", "to": [to], "subject": subject, "body": body},
        )

    async def ig_send_dm(self, *, recipient_id: str, message: str) -> dict:
        return await self.execute_action(
            self.slug("ig_send_dm"),
            {"recipient_id": recipient_id, "text": message},
        )
