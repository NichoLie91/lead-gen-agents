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

import asyncio
import logging
import random

import httpx

from src.core.config import Settings

log = logging.getLogger(__name__)

V3_BASE = "https://backend.composio.dev/api/v3"
EXECUTE_TIMEOUT = 90.0

# Retry/backoff on throttles (spec 7.x): Composio surfaces Google API quota
# errors as HTTP 429 / 5xx, transport failures, or a ``successful: false`` body
# whose message mentions quota/rate limits. We sleep 2-5s first, then back off
# exponentially (2, 4, 8, 16s + jitter) up to a 60s ceiling.
RETRY_MAX_ATTEMPTS = 5       # 1 fast try + up to 4 retries
RETRY_BASE_SEC = 2.0
RETRY_MAX_SEC = 60.0

# Substrings that mark a failure as transient (safe to retry). Semantic errors
# like "Sheet Pipeline not found" are NOT retried - retrying would just burn
# quota. HTTP 429/5xx are handled at the response level; here we only match
# body-level throttle phrases (no bare digits, which would false-positive on
# semantic errors that happen to contain numbers).
RETRYABLE_HINTS = (
    "quota", "rate limit", "rate_limit", "resource_exhausted", "too many",
    "temporarily unavailable", "server error", "internal error", "timeout",
    "deadline exceeded", "connection reset", "connection refused",
)

# Slug resolution table (spec 6.2): purpose -> candidate action slugs.
# Verified against the live v3 catalog (2026-08): the googlesheets toolkit has
# NO add-tab action, so each tab lives in its own spreadsheet
# (GOOGLESHEETS_SHEET_FROM_JSON) and tabs are rewritten via clear-range +
# GOOGLESHEETS_BATCH_UPDATE (named-tab write). Reads use GOOGLESHEETS_BATCH_GET.
SLUG_ALIASES: dict[str, list[str]] = {
    "send_email": ["GMAIL_SEND_EMAIL", "GMAIL_SEND_MESSAGE"],
    "create_draft": ["GMAIL_CREATE_EMAIL_DRAFT", "GMAIL_CREATE_DRAFT"],
    "send_draft": ["GMAIL_SEND_DRAFT"],
    "mail_list_threads": ["GMAIL_LIST_THREADS"],
    "mail_fetch_thread": ["GMAIL_FETCH_MESSAGE_BY_THREAD_ID"],
    "mail_fetch_message": ["GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"],
    "mail_reply_thread": ["GMAIL_REPLY_TO_THREAD"],
    "sheet_create_named": ["GOOGLESHEETS_SHEET_FROM_JSON"],
    "sheet_clear": ["GOOGLESHEETS_CLEAR_VALUES"],
    "sheet_values_update": ["GOOGLESHEETS_BATCH_UPDATE"],
    "sheet_get_info": ["GOOGLESHEETS_GET_SPREADSHEET_INFO", "GOOGLESHEETS_GET_SHEET_NAMES"],
    "sheet_read": ["GOOGLESHEETS_BATCH_GET"],
    "maps_search": ["GOOGLE_MAPS_TEXT_SEARCH", "SERPAPI_GOOGLE_MAPS_SEARCH",
                     "ZENSERP_ZENSERP_GOOGLE_MAPS_SEARCH"],
    "web_search": ["TAVILY_TAVILY_SEARCH", "SERPAPI_SEARCH",
                     "SERPAPI_GOOGLE_LIGHT_SEARCH", "SERPER_GOOGLE_SEARCH"],
    "fetch_url": ["COMPOSIO_SEARCH_FETCH_URL_CONTENT"],
    "ig_send_dm": ["INSTAGRAM_SEND_TEXT_MESSAGE"],
    "ig_account_info": ["INSTAGRAM_GET_ACCOUNT_INFO", "INSTAGRAM_GET_PROFILE_INFO",
                         "INSTAGRAM_GET_USER_DETAILS"],
    "multi_execute": ["COMPOSIO_MULTI_EXECUTE_TOOL"],
    "manage_connections": ["COMPOSIO_MANAGE_CONNECTIONS"],
    "wait_connections": ["COMPOSIO_WAIT_FOR_CONNECTIONS"],
}

# Toolkits whose catalogs we resolve slugs against (per-toolkit fetches return
# the full tool set reliably; a single unfiltered fetch is capped).
RESOLVE_TOOLKITS = (
    "gmail", "googlesheets", "instagram", "github", "google_maps", "serpapi", "zenserp", "tavily",
)

# Map an action slug prefix to the Composio toolkit that owns it (v3 executes
# require the connected account of the owning toolkit).
TOOLKIT_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("GMAIL_", "gmail"),
    ("GOOGLESHEETS_", "googlesheets"),
    ("INSTAGRAM", "instagram"),
    ("GITHUB_", "github"),
    ("GOOGLE_MAPS_", "google_maps"),
    ("SERPAPI_", "serpapi"),
    ("ZENSERP_", "zenserp"),
    ("TAVILY_", "tavily"),
)

REQUIRED_CONNECTIONS = ("googlesheets", "gmail")
OPTIONAL_CONNECTIONS = ("instagram", "github", "google_maps", "serpapi", "zenserp", "tavily")


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
        # Per-run URL cache: chatbot/agency checks + email enrichment fetch the
        # same lead homepage, so one Tavily extract per URL per run (quota).
        self._url_cache: dict[str, str] = {}
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
        then keeps the first candidate that actually exists. Candidates owned
        by a CONNECTED toolkit are preferred: e.g. web_search resolves to
        TAVILY_TAVILY_SEARCH only when tavily is ACTIVE, otherwise it falls
        through to SERPAPI/Serper (so a connected provider is always used over
        a catalog-listed-but-unconnected one). Runs lazily once per process
        before the first tool execution.
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
            if not self._account_by_toolkit:
                await self.refresh_connections()
            for purpose, candidates in SLUG_ALIASES.items():
                chosen = None
                fallback = None
                for cand in candidates:
                    if cand not in available:
                        continue
                    fallback = fallback or cand
                    toolkit = self._toolkit_for(cand)
                    if toolkit and toolkit in self._account_by_toolkit:
                        chosen = cand
                        break
                if chosen is None:
                    chosen = fallback
                if chosen:
                    self._slugs[purpose] = chosen
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
    async def _execute_once(self, action: str, body: dict) -> tuple[dict, bool]:
        """One raw execute attempt -> (result_dict, retryable_flag)."""
        try:
            async with httpx.AsyncClient(timeout=EXECUTE_TIMEOUT) as client:
                resp = await client.post(
                    f"{V3_BASE}/tools/execute/{action}",
                    json=body, headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            return {"ok": False, "action": action, "error": str(exc)}, True

        if resp.status_code == 429 or resp.status_code >= 500:
            return (
                {"ok": False, "action": action,
                 "error": f"HTTP {resp.status_code}: {resp.text[:200]}"},
                True,
            )
        if resp.status_code != 200:
            return (
                {"ok": False, "action": action, "error": resp.text[:300]},
                False,
            )
        payload = resp.json()
        # Composio returns HTTP 200 even when the action itself failed; the
        # failure is signalled by the body's ``successful`` flag (or an embedded
        # ``http_error``). Treat those as failures (verified against v3 API).
        if payload.get("successful") is False:
            err = str(payload.get("error") or payload.get("message") or "")[:300]
            return {"ok": False, "action": action, "error": err}, self._is_retryable(err)
        data = payload.get("data", payload)
        if isinstance(data, dict) and "http_error" in data:
            err = str(data)[:300]
            return {"ok": False, "action": action, "error": err}, self._is_retryable(err)
        return {"ok": True, "action": action, "data": data}, False

    @staticmethod
    def _is_retryable(error_text: str) -> bool:
        """True when an error smells like a throttle/server hiccup."""
        lowered = error_text.lower()
        return any(hint in lowered for hint in RETRYABLE_HINTS)

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

        for attempt in range(RETRY_MAX_ATTEMPTS):
            result, retryable = await self._execute_once(action, body)
            if result.get("ok") or not retryable or attempt == RETRY_MAX_ATTEMPTS - 1:
                if not result.get("ok") and attempt > 0:
                    log.error(
                        "Composio action %s failed after %d attempt(s): %s",
                        action, attempt + 1, result.get("error", ""),
                    )
                return result
            # Exponential backoff with jitter: 2s first, then 4, 8, 16... (cap 60s).
            delay = min(RETRY_MAX_SEC, RETRY_BASE_SEC * (2 ** attempt))
            delay += random.uniform(0, 0.5 * delay)
            log.warning(
                "Composio action %s throttled (attempt %d/%d); sleeping %.1fs: %s",
                action, attempt + 1, RETRY_MAX_ATTEMPTS, delay,
                result.get("error", ""),
            )
            await asyncio.sleep(delay)

    # ---------- higher-level tools ----------
    async def search_google_maps(self, query: str, start: int = 0) -> list[dict]:
        # The resolved action differs in its query param name: Google Places
        # (GOOGLE_MAPS_TEXT_SEARCH) uses ``textQuery``; SerpAPI/ZenSerp use ``q``.
        slug = self.slug("maps_search")
        if "GOOGLE_MAPS" in slug.upper():
            params = {"textQuery": query, "maxResultCount": 20,
                      "fieldMask": "places.id,places.displayName,places.formattedAddress,"
                                    "places.nationalPhoneNumber,places.websiteUri,"
                                    "places.rating,places.userRatingCount,places.businessStatus"}
        else:
            params = {"q": query}
        resp = await self.execute_action(slug, params)
        if not resp.get("ok"):
            return []
        return self._normalize_maps(resp.get("data", {}))

    @staticmethod
    def _normalize_maps(data) -> list[dict]:
        if isinstance(data, dict):
            raw = (data.get("local_results") or data.get("results")
                   or data.get("places") or data.get("response_data"))
            if isinstance(raw, dict):
                raw = (raw.get("local_results") or raw.get("results")
                       or raw.get("places"))
        else:
            raw = data
        if not isinstance(raw, list):
            return []
        out = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            display = item.get("displayName") or {}
            name = display.get("text") or item.get("title") or item.get("name", "")
            address = (item.get("formattedAddress") or item.get("formatted_address")
                       or item.get("address", ""))
            # Parse state from address (e.g. "123 Main St, Toronto, ON, Canada" -> "ON")
            state = ""
            if address:
                parts = [p.strip() for p in address.split(",")]
                # Canadian provinces are 2-letter codes; US states too
                for part in parts:
                    part_upper = part.upper().strip()
                    if len(part_upper) == 2 and part_upper.isalpha():
                        state = part_upper
                        break
            out.append({
                "name": name,
                "address": address,
                "phone": item.get("nationalPhoneNumber") or item.get("formatted_phone_number")
                         or item.get("phone", ""),
                "website": item.get("websiteUri") or item.get("website") or item.get("website_url", ""),
                "rating": item.get("rating"),
                "reviews": item.get("userRatingCount") or item.get("user_ratings_total")
                           or item.get("reviews"),
                "open_state": item.get("businessStatus") or item.get("business_status")
                              or item.get("open_state", ""),
                "state": state,
                "source_url": item.get("googleMapsUri") or item.get("place_id") or "",
                "source_evidence_text": f"Google Maps listing: {name} in {address[:80]}",
                "evidence_quality": "high" if (item.get("rating") and item.get("userRatingCount")) else "medium",
                "booking_signal": "unknown",
                "emergency_signal": "unknown",
            })
        return out

    async def search_web(self, query: str) -> list[dict]:
        # Tavily takes ``query``; SerpAPI takes ``q`` (catalog-verified).
        slug = self.slug("web_search")
        params = {"q": query} if "SERPAPI" in slug.upper() else {"query": query}
        resp = await self.execute_action(slug, params)
        if not resp.get("ok"):
            return []
        data = resp.get("data")
        # Composio v3 wraps Tavily output as {"response_data": {"results": [...]}};
        # SerpAPI returns {"organic_results": [...]}. Normalize all shapes to a
        # flat list of {title,url,snippet/content} items for enrichment.
        if isinstance(data, dict):
            raw = (data.get("results") or data.get("organic_results")
                   or data.get("items"))
            if not isinstance(raw, list):
                inner = data.get("response_data")
                if isinstance(inner, dict):
                    raw = (inner.get("results") or inner.get("organic_results")
                           or inner.get("items"))
            data = raw
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            if not isinstance(item, dict):
                continue
            out.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content") or item.get("snippet") or "",
                "content": item.get("content") or item.get("snippet") or "",
            })
        return out

    async def fetch_url(self, url: str) -> str:
        """Fetch webpage text as a string ('' on any failure).

        Prefers the Composio fetch tool when it resolves (it was removed from
        the v3 catalog 2026-08), otherwise falls back to a direct Tavily
        ``extract`` call using TAVILY_API_KEY — proven to return raw page
        content (emails/mailto links) for lead-website enrichment. Results are
        cached per URL for the run so the chatbot/agency checks and email
        enrichment share one extract call (Tavily quota).
        """
        cached = self._url_cache.get(url)
        if cached is not None:
            return cached
        if not self._slugs_resolved:
            await self.resolve_slugs()
        html = ""
        resolved = self._slugs.get("fetch_url")
        if resolved:
            resp = await self.execute_action(resolved, {"url": url})
            data = resp.get("data") if resp.get("ok") else None
            if isinstance(data, str) and data.strip() and data != "None":
                html = data
        if not html:
            html = await self._tavily_extract(url) if self.settings.tavily_api_key else ""
        self._url_cache[url] = html
        return html

    async def _tavily_extract(self, url: str) -> str:
        """Direct Tavily extract fallback (raw page text, includes emails).

        One retry on 429/5xx (dev-key rate limits) with a short sleep, matching
        the codebase's throttle-averse style.
        """
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        "https://api.tavily.com/extract",
                        json={"api_key": self.settings.tavily_api_key, "urls": [url]},
                    )
                if resp.status_code in (429, 500, 502, 503, 504):
                    if attempt == 0:
                        await asyncio.sleep(2.0)
                        continue
                    log.warning("Tavily extract HTTP %s for %s", resp.status_code, url)
                    return ""
                if resp.status_code != 200:
                    log.warning("Tavily extract HTTP %s for %s", resp.status_code, url)
                    return ""
                for item in resp.json().get("results", []):
                    raw = item.get("raw_content") or ""
                    if raw.strip():
                        return raw
                return ""
            except httpx.HTTPError as exc:
                if attempt == 0:
                    await asyncio.sleep(2.0)
                    continue
                log.warning("Tavily extract failed for %s: %s", url, exc)
                return ""
        return ""

    async def gmail_send_email(self, *, to: str, subject: str, body: str) -> dict:
        # Composio v3 GMAIL_SEND_EMAIL requires `recipient_email` + `body`
        # (verified against the live tool schema). The old v1 shape
        # (userId/to) was rejected with HTTP 400 "fields are missing:
        # {'recipient_email'}" — the reason every send bounced.
        return await self.execute_action(
            self.slug("send_email"),
            {"recipient_email": to, "subject": subject, "body": body},
        )

    async def gmail_create_draft(self, *, to: str, subject: str, body: str) -> dict:
        # v3 GMAIL_CREATE_EMAIL_DRAFT requires recipient_email/subject/body.
        return await self.execute_action(
            self.slug("create_draft"),
            {"recipient_email": to, "subject": subject, "body": body},
        )

    async def ig_send_dm(self, *, recipient_id: str, message: str) -> dict:
        # v3 INSTAGRAM_SEND_TEXT_MESSAGE requires a numeric PSID (IG-scoped
        # id). Enrichment only collects the @handle, and the v3 catalog has no
        # handle->ID resolver, so a non-numeric recipient can NEVER send — fail
        # fast with a readable error instead of a 400 round-trip.
        if not str(recipient_id).strip().isdigit():
            return {
                "ok": False, "action": self.slug("ig_send_dm"),
                "error": ("recipient_id must be a numeric Instagram PSID, got "
                           f"'{recipient_id}' (handle); no handle->ID resolver "
                           "in the v3 catalog"),
            }
        return await self.execute_action(
            self.slug("ig_send_dm"),
            {"recipient_id": recipient_id, "text": message},
        )

    async def ig_account_status(self) -> dict | None:
        """Return {"restricted": bool, "reason": str} for the connected
        Instagram account, or None when it can't be determined.

        Used by ANCHOR's pre-flight: if Meta has flagged/restricted the account
        the IG stage must halt. Fail-OPEN: unknown/missing tool -> None, so the
        stage never halts on a tooling gap, only on a real restriction signal.
        """
        try:
            resp = await self.execute_action(self.slug("ig_account_info"), {})
        except ComposioNotConfigured:
            return None
        if not resp.get("ok"):
            return None
        data = resp.get("data") or {}
        if not isinstance(data, dict):
            return None
        text = str(data).lower()
        restricted_words = (
            "restricted", "action_blocked", "action blocked", "flagged",
            "temporarily limited", "limited access", "disabled", "banned",
        )
        if any(w in text for w in restricted_words):
            return {"restricted": True, "reason": text[:140]}
        return {"restricted": False, "reason": ""}
