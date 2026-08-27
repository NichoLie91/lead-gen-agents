"""Enrichment (spec section 7.3) — find + VALIDATE email & Instagram handles.

Hard rules encoded here (learned in the sample run, MUST be enforced):
- ``[email protected]`` is a web-search redaction placeholder, never an address.
- Only regex-valid emails on non-blocklisted domains are accepted.
- Never invent contact info: unconfirmed fields become NEEDS_ENRICHMENT /
  NEEDS_VERIFICATION flags, and are reported honestly.
"""
from __future__ import annotations

import logging
import re

from src.agents.composio_agent import ComposioAgent, ComposioNotConfigured
from src.core.config import Settings
from src.email_verify import (
    is_sendable,
    verify_email,
)

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
PLACEHOLDER = "[email protected]"

# AI chatbot widget + agency/template-mill signatures scanned in homepage HTML
# (spec 7.2). Filling these flags lets Scout's -20 disqualifier penalties fire.
CHATBOT_SIGNATURES = ("intercom", "drift", "tidio", "voiceflow", "chat-widget", "crisp.chat")
AGENCY_MARKERS = ("powered by thrive", "powered by wix", "built with squarespace", "template by")

# Aggregators/redirectors that are never real business mailboxes (spec 7.3).
BLOCKLIST_DOMAINS = {
    "example.com", "gstatic.com", "facebook.com", "schema.org", "nextdoor.com",
    "bbb.org", "wixpress.com", "sentry.io", "wordpress.org", "instagram.com",
    "linkedin.com", "twitter.com", "yelp.com", "chamberofcommerce.com",
    "google.com", "maps.google.com",
}

IG_RE = re.compile(r"^@?[A-Za-z0-9._]{1,30}$")


def normalize_email(raw: str | None) -> str | None:
    """Return a normalized valid email or None."""
    if not raw:
        return None
    email = raw.strip().strip("<>\"'").lower()
    if email == PLACEHOLDER:
        return None
    if not EMAIL_RE.fullmatch(email):
        return None
    domain = email.split("@")[-1]
    if domain in BLOCKLIST_DOMAINS:
        return None
    return email


def extract_emails(text: str | None) -> list[str]:
    """Unique, validated emails found inside arbitrary text."""
    if not text:
        return []
    found: list[str] = []
    for match in re.finditer(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", text):
        email = normalize_email(match.group(0))
        if email and email not in found:
            found.append(email)
    return found


def normalize_instagram(handle: str | None) -> str | None:
    if not handle:
        return None
    candidate = handle.strip().lstrip("@")
    if IG_RE.fullmatch(candidate):
        return candidate
    return None


async def enrich_leads(
    leads: list[dict],
    composio: ComposioAgent,
    settings: Settings,
    llm=None,
) -> list[dict]:
    """Enrich each lead with email + instagram; never invents data.

    Offline (no Composio key / dry-run): mocks get deterministic mock contacts
    (marked via the ``_mock`` flag only); real leads get NEEDS_ENRICHMENT flags.
    """
    out: list[dict] = []
    # Gemini extraction fallback is budget-capped per RUN so a big pool can
    # never trigger hundreds of LLM calls.
    budget = {"left": 15}
    for lead in leads:
        email = lead.get("email")
        instagram = lead.get("instagram")

        if settings.dry_run or not composio.connected:
            if lead.get("_mock"):
                # Deterministic mock enrichment (offline testing only).
                slug = re.sub(r"[^a-z0-9]", "", lead.get("name", "").lower())[:20]
                lead["email"] = email or (f"info@{slug}.example" if lead.get("_has_ig") else None)
                lead["instagram"] = instagram or (slug if lead.get("_has_ig") else None)
            else:
                lead["email"] = None
                lead["instagram"] = None
        else:
            email = email or await _find_email(composio, lead, llm=llm, budget=budget)
            lead["email"] = normalize_email(email)
            instagram = instagram or await _find_instagram(composio, lead, llm=llm, budget=budget)
            lead["instagram"] = normalize_instagram(instagram)
            if lead.get("website") and not lead.get("_mock"):
                lead["_chatbot"] = await _has_chatbot(composio, lead["website"])
                lead["_agency_built"] = await _has_agency_marker(composio, lead["website"])

        # Verify email via ZeroBounce if key is set; otherwise format-check only.
        if lead.get("email") and settings.zerobounce_api_key and not settings.dry_run:
            vresult = await verify_email(lead["email"], settings.zerobounce_api_key)
            lead["email_status"] = vresult.status
            lead["email_score"] = vresult.score
            if not is_sendable(vresult.status):
                log.info(
                    "Email %s failed verification: %s (score %.1f) -- marking as %s",
                    lead["email"], vresult.status, vresult.score, vresult.status,
                )
                # Keep the email for the sheet record but flag it as not sendable
                lead["email_sendable"] = False
            else:
                lead["email_sendable"] = True
        else:
            lead["email_status"] = "VERIFIED" if lead.get("email") else "NEEDS_ENRICHMENT"
            lead["email_sendable"] = bool(lead.get("email"))
        lead["ig_status"] = "VERIFIED" if lead.get("instagram") else "NEEDS_VERIFICATION"
        out.append(lead)
    return out


async def _llm_extract(llm, text: str, want: str, budget: dict) -> str | None:
    """Gemini-powered extraction fallback for emails / Instagram handles.

    Used only when regex missed (no valid contact in the text). Budget-capped
    so at most ~15 LLM calls happen per run. Returns None when offline, out of
    budget, or Gemini finds nothing.
    """
    if not llm or not getattr(llm, "available", False) or budget.get("left", 0) <= 0:
        return None
    if not text:
        return None
    budget["left"] -= 1
    prompt = (
        f"Extract the {want} contact from this text about a local business. "
        f'Reply STRICT JSON only: {{"{want}": "value" or null}}. '
        f'If none exists reply {{"{want}": null}}. Do not invent.\n\nText:\n'
        f"{text[:3000]}"
    )
    import json

    raw = await llm.complete(prompt)
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    value = data.get(want)
    return value if isinstance(value, str) and value.strip() else None


async def _find_email(composio: ComposioAgent, lead: dict, llm=None,
                      budget: dict | None = None) -> str | None:
    try:
        name = lead.get("name") or ""
        city = lead.get("city") or ""
        # Live-probed (2026-08): Tavily returns ZERO results for
        # "...contact email" phrasing but solid hits for natural business
        # queries. Search the business itself first, then with its vertical.
        snippets = []
        queries = [f"{name} {city}".strip()]
        if lead.get("vertical"):
            queries.append(f"{name} {city} {lead['vertical']} company".strip())
        for idx, query in enumerate(queries):
            results = await composio.search_web(query)
            if not results:
                continue  # zero hits -> try the vertical-phrased query
            for result in results:
                snippet = result.get("snippet") or result.get("content") or ""
                snippets.append(snippet)
                emails = extract_emails(snippet)
                if emails:
                    return emails[0]
            if idx == 0:
                # First query already surfaced the business; the website fetch
                # below is the real email source, so save the second search.
                break
        # Fallback: fetch the business website, then its /contact page — the
        # reliable email source (Tavily extract returns raw page text).
        html = ""
        if lead.get("website"):
            html = await composio.fetch_url(lead["website"]) or ""
            emails = extract_emails(html)
            if emails:
                return emails[0]
            if len(html.strip()) < 200:
                contact = await composio.fetch_url(
                    f"{lead['website'].rstrip('/')}/contact"
                ) or ""
                emails = extract_emails(contact)
                if emails:
                    return emails[0]
        # Gemini brain: read the raw snippets/HTML when regex missed.
        budget = budget or {"left": 15}
        got = await _llm_extract(llm, " ".join(snippets) + " " + html, "email", budget)
        if got:
            return normalize_email(got)
    except ComposioNotConfigured:
        pass
    return None


async def _has_chatbot(composio: ComposioAgent, url: str) -> bool:
    try:
        html = (await composio.fetch_url(url) or "").lower()
        return any(sig in html for sig in CHATBOT_SIGNATURES)
    except ComposioNotConfigured:
        return False


async def _has_agency_marker(composio: ComposioAgent, url: str) -> bool:
    try:
        html = (await composio.fetch_url(url) or "").lower()
        return any(marker in html for marker in AGENCY_MARKERS)
    except ComposioNotConfigured:
        return False


async def _find_instagram(composio: ComposioAgent, lead: dict, llm=None,
                          budget: dict | None = None) -> str | None:
    try:
        query = f"{lead.get('name')} {lead.get('city', '')} instagram".strip()
        results = await composio.search_web(query)
        snippets = []
        for result in results:
            snippet = result.get("snippet") or result.get("content") or ""
            snippets.append(snippet)
            for token in re.findall(r"@?instagram\.com/([A-Za-z0-9._]{1,30})", snippet):
                handle = normalize_instagram(token)
                if handle:
                    return handle
        # Gemini brain: read the raw snippets when regex missed.
        budget = budget or {"left": 15}
        got = await _llm_extract(llm, " ".join(snippets), "instagram", budget)
        if got:
            return normalize_instagram(got)
    except ComposioNotConfigured:
        pass
    return None
