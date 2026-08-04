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
) -> list[dict]:
    """Enrich each lead with email + instagram; never invents data.

    Offline (no Composio key / dry-run): mocks get deterministic mock contacts
    (marked via the ``_mock`` flag only); real leads get NEEDS_ENRICHMENT flags.
    """
    out: list[dict] = []
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
            email = email or await _find_email(composio, lead)
            lead["email"] = normalize_email(email)
            instagram = instagram or await _find_instagram(composio, lead)
            lead["instagram"] = normalize_instagram(instagram)
            if lead.get("website") and not lead.get("_mock"):
                lead["_chatbot"] = await _has_chatbot(composio, lead["website"])
                lead["_agency_built"] = await _has_agency_marker(composio, lead["website"])

        lead["email_status"] = "VERIFIED" if lead.get("email") else "NEEDS_ENRICHMENT"
        lead["ig_status"] = "VERIFIED" if lead.get("instagram") else "NEEDS_VERIFICATION"
        out.append(lead)
    return out


async def _find_email(composio: ComposioAgent, lead: dict) -> str | None:
    try:
        query = f"{lead.get('name')} {lead.get('city', '')} contact email"
        results = await composio.search_web(query)
        for result in results:
            snippet = result.get("snippet") or result.get("content") or ""
            emails = extract_emails(snippet)
            if emails:
                return emails[0]
        # Fallback: fetch the contact page when a website exists.
        if lead.get("website"):
            html = await composio.fetch_url(lead["website"])
            emails = extract_emails(html)
            if emails:
                return emails[0]
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


async def _find_instagram(composio: ComposioAgent, lead: dict) -> str | None:
    try:
        query = f"{lead.get('name')} instagram"
        results = await composio.search_web(query)
        for result in results:
            snippet = result.get("snippet") or result.get("content") or ""
            for token in re.findall(r"@?instagram\.com/([A-Za-z0-9._]{1,30})", snippet):
                handle = normalize_instagram(token)
                if handle:
                    return handle
    except ComposioNotConfigured:
        pass
    return None
