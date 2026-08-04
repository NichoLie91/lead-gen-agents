"""Composio Agent — the tool gateway (spec sections 3, 6).

Primary path: ``composio-core`` SDK -> ``ComposioToolSet.execute_action``.
When ``COMPOSIO_API_KEY`` is absent (local dev / dry-run / pre-setup), the
agent reports connections as NOT_CONFIGURED and every tool call raises
``ComposioNotConfigured``; callers then fall back to offline behaviour.
"""
from __future__ import annotations

import logging

from src.core.config import Settings

log = logging.getLogger(__name__)

# Slug resolution table (spec 6.2): documented slug -> candidate aliases.
# At startup we resolve against the live Composio catalog when available.
SLUG_ALIASES: dict[str, list[str]] = {
    "send_email": ["GMAIL_SEND_EMAIL", "GMAIL_SEND_MESSAGE"],
    "create_draft": ["GMAIL_CREATE_EMAIL_DRAFT", "GMAIL_CREATE_DRAFT"],
    "send_draft": ["GMAIL_SEND_DRAFT"],
    "sheet_values_update": ["GOOGLESHEETS_VALUES_UPDATE", "GOOGLESHEETS_UPDATE_SPREADSHEET_VALUES"],
    "sheet_create": ["GOOGLESHEETS_CREATE_GOOGLE_SHEET1", "GOOGLESHEETS_CREATE_SPREADSHEET"],
    "sheet_add_sheet": ["GOOGLESHEETS_ADD_SHEET"],
    "sheet_batch_update": ["GOOGLESHEETS_UPDATE_VALUES_BATCH"],
    "sheet_get_info": ["GOOGLESHEETS_GET_SPREADSHEET_INFO", "GOOGLESHEETS_GET_SHEET_NAMES"],
    "sheet_read": ["GOOGLESHEETS_GET_VALUES", "GOOGLESHEETS_GET_SPREADSHEET_VALUES", "GOOGLESHEETS_GET_SHEET_VALUES"],
    "maps_search": ["COMPOSIO_SEARCH_GOOGLE_MAPS", "GOOGLEMAPS_TEXT_SEARCH"],
    "web_search": ["COMPOSIO_SEARCH_WEB", "SERPER_GOOGLE_SEARCH", "TAVILY_WEB_SEARCH"],
    "fetch_url": ["COMPOSIO_SEARCH_FETCH_URL_CONTENT"],
    "ig_send_dm": ["INSTAGRAMBUSINESS_SEND_MESSAGE", "INSTAGRAM_SEND_DIRECT_MESSAGE"],
    "multi_execute": ["COMPOSIO_MULTI_EXECUTE_TOOL"],
    "manage_connections": ["COMPOSIO_MANAGE_CONNECTIONS"],
    "wait_connections": ["COMPOSIO_WAIT_FOR_CONNECTIONS"],
}

REQUIRED_CONNECTIONS = ("googlesheets", "gmail")
OPTIONAL_CONNECTIONS = ("instagram",)


class ComposioNotConfigured(Exception):
    """Raised when a Composio tool is called without an API key configured."""


class ComposioAgent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.connected = bool(settings.composio_api_key)
        self._toolset = None
        self._slugs: dict[str, str] = {}
        if self.connected:
            self._try_init_toolset()

    # ---------- lifecycle ----------
    def _try_init_toolset(self) -> None:
        try:
            from composio import ComposioToolSet  # type: ignore[import-not-found]

            self._toolset = ComposioToolSet(api_key=self.settings.composio_api_key)
            self._resolve_slugs()
        except ImportError:
            log.warning("composio-core not installed; Composio Agent runs offline")
            self.connected = False
        except Exception as exc:  # bad key etc.
            log.warning("Composio init failed: %s", exc)
            self.connected = False

    def _resolve_slugs(self) -> None:
        """Resolve canonical action slugs against the live catalog (spec 6.1)."""
        try:
            tools = self._toolset.get_tools()  # type: ignore[attr-defined]
            available = {getattr(t, "name", str(t)) for t in tools}
        except Exception:
            available = set()
        for purpose, candidates in SLUG_ALIASES.items():
            for cand in candidates:
                if cand in available:
                    self._slugs[purpose] = cand
                    break

    def slug(self, purpose: str) -> str:
        resolved = self._slugs.get(purpose)
        if resolved:
            return resolved
        candidates = SLUG_ALIASES.get(purpose, [])
        return candidates[0] if candidates else purpose

    # ---------- pre-flight ----------
    async def preflight(self) -> dict[str, str]:
        """Return a status map for required + optional connections.

        Uses the SDK when available, otherwise falls back to the Composio
        REST API (no SDK required).
        """
        statuses = {c: "NOT_CONFIGURED" for c in REQUIRED_CONNECTIONS + OPTIONAL_CONNECTIONS}
        if not self.connected or self._toolset is None:
            if self.connected:  # key present but SDK missing -> REST fallback
                return await self._rest_preflight()
            return statuses
        try:
            connections = await self._toolset.get_connected_accounts()  # type: ignore[attr-defined]
            connected = {str(getattr(c, "appUniqueId", None) or getattr(c, "app", "")).lower()
                         for c in connections}
            for conn in REQUIRED_CONNECTIONS + OPTIONAL_CONNECTIONS:
                matched = any(conn in app or app in conn for app in connected)
                statuses[conn] = "ACTIVE" if matched else "MISSING"
        except Exception as exc:
            log.warning("preflight query failed (trying REST): %s", exc)
            return await self._rest_preflight()
        return statuses

    async def _rest_preflight(self) -> dict[str, str]:
        import httpx

        url = "https://backend.composio.dev/api/v1/connected_accounts"
        statuses = {c: "ERROR" for c in REQUIRED_CONNECTIONS + OPTIONAL_CONNECTIONS}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    url, params={"user_ids": "default"},
                    headers={"x-api-key": self.settings.composio_api_key},
                )
            if resp.status_code != 200:
                return {c: f"ERROR {resp.status_code}" for c in statuses}
            payload = resp.json()
            accounts = payload if isinstance(payload, list) else payload.get("items", [])
            connected = {str(a.get("appUniqueId", "")).lower() for a in accounts}
            for conn in REQUIRED_CONNECTIONS + OPTIONAL_CONNECTIONS:
                matched = any(conn in app or app in conn for app in connected)
                statuses[conn] = "ACTIVE" if matched else "MISSING"
        except Exception as exc:
            log.warning("REST preflight failed: %s", exc)
        return statuses

    # ---------- tool execution ----------
    async def execute_action(self, action: str, params: dict) -> dict:
        if not self.connected or self._toolset is None:
            raise ComposioNotConfigured(f"{action} requires COMPOSIO_API_KEY")
        try:
            result = await self._toolset.execute_action(action=action, params=params)  # type: ignore[attr-defined]
            return {"ok": True, "action": action, "data": result}
        except Exception as exc:
            log.error("Composio action %s failed: %s", action, exc)
            return {"ok": False, "action": action, "error": str(exc)}

    async def search_google_maps(self, query: str, start: int = 0) -> list[dict]:
        """Google Maps search; returns normalized result dicts."""
        resp = await self.execute_action(
            self.slug("maps_search"),
            {"query": query, "start": start},
        )
        if not resp.get("ok"):
            return []
        return self._normalize_maps(resp.get("data", {}))

    @staticmethod
    def _normalize_maps(data) -> list[dict]:
        raw = data.get("results") if isinstance(data, dict) else data
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            out.append({
                "name": item.get("name", ""),
                "address": item.get("formatted_address") or item.get("address", ""),
                "phone": item.get("formatted_phone_number") or item.get("phone", ""),
                "website": item.get("website") or item.get("website_url", ""),
                "rating": item.get("rating"),
                "reviews": item.get("user_ratings_total") or item.get("reviews"),
                "open_state": item.get("business_status", ""),
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
            {"recipient_id": recipient_id, "message": message},
        )
