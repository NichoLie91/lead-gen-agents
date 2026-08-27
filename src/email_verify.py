"""Email verification via ZeroBounce API.

Verifies every email found during enrichment before it is marked VERIFIED.
Catches bounces, catch-all domains, spam traps, and role-based addresses.

ZeroBounce API docs: https://www.zerobounce.net/docs
Free tier: 100 verifications/month. Rate limit: 1 request/second.

Status mapping (ZeroBounce -> our pipeline):
    valid       -> VERIFIED (safe to send)
    catch-all   -> CATCH_ALL (risky, may bounce)
    spamtrap    -> SPAM_TRAP (never send)
    abuse       -> ABUSE (known complainer)
    inactive    -> INACTIVE (mx found but mailbox not confirmed)
    unknown     -> UNKNOWN (cannot determine, treat as risky)
    disposable  -> DISPOSABLE (burner domain)
   hotmail.com  -> ROLE_BASED (info@, sales@, etc. — low engagement)
    error       -> ERROR (API failure, skip verification)
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

ZEROBOUNCE_API = "https://api.zerobounce.net/v2/validate"
VERIFY_TIMEOUT = 15.0

# Our pipeline statuses mapped from ZeroBounce results
STATUS_VERIFIED = "VERIFIED"
STATUS_CATCH_ALL = "CATCH_ALL"
STATUS_SPAM_TRAP = "SPAM_TRAP"
STATUS_ABUSE = "ABUSE"
STATUS_INACTIVE = "INACTIVE"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_DISPOSABLE = "DISPOSABLE"
STATUS_ROLE_BASED = "ROLE_BASED"
STATUS_FORMAT_INVALID = "FORMAT_INVALID"
STATUS_ERROR = "ERROR"

# Role-based prefixes (info@, sales@, admin@, etc.)
ROLE_PREFIXES = (
    "info", "sales", "admin", "support", "contact", "hello", "help",
    "office", "team", "billing", "accounts", "marketing", "hr",
    "press", "media", "abuse", "postmaster", "webmaster", "noreply",
    "no-reply", "donotreply", "jobs", "careers",
)


@dataclass
class VerifyResult:
    """Result of a single email verification."""
    email: str
    status: str  # One of the STATUS_* constants
    score: float  # 0.0 - 1.0 confidence
    sub_status: str = ""  # ZeroBounce sub-status (e.g., "low_quality")
    free_email: bool = False
    smtp_check: bool = False
    mx_found: bool = False


async def verify_email(email: str, api_key: str) -> VerifyResult:
    """Verify a single email address via ZeroBounce API.

    Returns a VerifyResult with status, score, and metadata.
    On API failure, returns STATUS_ERROR so the caller can skip gracefully.
    """
    if not api_key:
        return VerifyResult(email=email, status=STATUS_FORMAT_INVALID, score=0.0)

    # Quick format check before hitting the API
    if not _looks_valid(email):
        return VerifyResult(email=email, status=STATUS_FORMAT_INVALID, score=0.0)

    # Role-based prefix check (info@, sales@, etc.)
    prefix = email.split("@")[0].lower()
    if prefix in ROLE_PREFIXES:
        return VerifyResult(
            email=email, status=STATUS_ROLE_BASED, score=0.3,
            sub_status="role_based",
        )

    try:
        async with httpx.AsyncClient(timeout=VERIFY_TIMEOUT) as client:
            resp = await client.get(
                ZEROBOUNCE_API,
                params={"api_key": api_key, "email": email, "ip_address": ""},
            )
        if resp.status_code != 200:
            log.warning("ZeroBounce HTTP %s for %s", resp.status_code, email)
            return VerifyResult(email=email, status=STATUS_ERROR, score=0.0)

        data = resp.json()
        zb_status = (data.get("status") or "unknown").lower()
        score = float(data.get("score") or 0.0)

        # Map ZeroBounce status to our pipeline status
        status_map = {
            "valid": STATUS_VERIFIED,
            "catch-all": STATUS_CATCH_ALL,
            "spamtrap": STATUS_SPAM_TRAP,
            "abuse": STATUS_ABUSE,
            "inactive": STATUS_INACTIVE,
            "unknown": STATUS_UNKNOWN,
            "disposable": STATUS_DISPOSABLE,
        }
        status = status_map.get(zb_status, STATUS_UNKNOWN)

        return VerifyResult(
            email=email,
            status=status,
            score=score,
            sub_status=data.get("sub_status") or "",
            free_email=data.get("free_email") is True,
            smtp_check=data.get("smtp_check") is True,
            mx_found=data.get("mx_found") is True,
        )

    except httpx.HTTPError as exc:
        log.warning("ZeroBounce request failed for %s: %s", email, exc)
        return VerifyResult(email=email, status=STATUS_ERROR, score=0.0)
    except Exception as exc:
        log.warning("ZeroBounce verification error for %s: %s", email, exc)
        return VerifyResult(email=email, status=STATUS_ERROR, score=0.0)


async def verify_emails(
    emails: list[str], api_key: str, *, max_concurrent: int = 5
) -> dict[str, VerifyResult]:
    """Verify a batch of emails with concurrency control.

    Returns {email: VerifyResult} for all inputs.
    ZeroBounce rate limit: 1 request/second on free tier.
    """
    if not api_key or not emails:
        return {e: VerifyResult(email=e, status=STATUS_FORMAT_INVALID, score=0.0)
                for e in emails}

    sem = asyncio.Semaphore(max_concurrent)
    results: dict[str, VerifyResult] = {}

    async def _verify_one(email: str) -> None:
        async with sem:
            result = await verify_email(email, api_key)
            results[email] = result
            # Rate limit: 1 req/sec on free tier
            await asyncio.sleep(1.0)

    await asyncio.gather(*(_verify_one(e) for e in emails))
    return results


def is_sendable(status: str) -> bool:
    """True when an email with this verification status is safe to send."""
    return status in (STATUS_VERIFIED, STATUS_CATCH_ALL)


def is_catch_all(status: str) -> bool:
    """True when the email is a catch-all domain (risky but not blocked)."""
    return status == STATUS_CATCH_ALL


def _looks_valid(email: str) -> bool:
    """Quick format check — not a replacement for ZeroBounce, just prevents
    wasting API calls on obviously invalid addresses."""
    if not email or "@" not in email:
        return False
    parts = email.split("@")
    if len(parts) != 2:
        return False
    local, domain = parts
    if not local or not domain:
        return False
    return "." in domain and len(domain) <= 253
