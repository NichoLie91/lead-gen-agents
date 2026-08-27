"""Email verification via SMTP probe + MX lookup — no API key needed.

Verifies every email found during enrichment before it is marked VERIFIED.
Catches bounces, catch-all domains, spam traps, and role-based addresses.

How it works (same as InboxValid MCP server, but built-in):
1. Format check — reject obvious garbage
2. MX record lookup — does the domain accept mail?
3. SMTP probe — connect to the mail server and ask if the address exists
4. Catch-all detection — try a random address to see if the domain accepts everything
5. Disposable domain detection — maintain a list of known burner domains
6. Role-based prefix detection — info@, sales@, admin@, etc.

Status mapping:
    VERIFIED     — MX exists, SMTP accepted the address (safe to send)
    CATCH_ALL    — domain accepts any address (risky, may bounce)
    DISPOSABLE   — known burner domain (never send)
    ROLE_BASED   — info@, sales@, etc. (low engagement)
    NO_MX_RECORD — domain has no mail servers (will bounce)
    SMTP_REJECTED — mail server rejected the address (will bounce)
    FORMAT_INVALID — obviously invalid email format
    ERROR        — network/DNS failure, skip verification
"""
from __future__ import annotations

import asyncio
import logging
import random
import smtplib
import socket
import string
from dataclasses import dataclass

import dns.resolver
import httpx

log = logging.getLogger(__name__)

VERIFY_TIMEOUT = 10.0

# Pipeline statuses
STATUS_VERIFIED = "VERIFIED"
STATUS_CATCH_ALL = "CATCH_ALL"
STATUS_DISPOSABLE = "DISPOSABLE"
STATUS_ROLE_BASED = "ROLE_BASED"
STATUS_NO_MX = "NO_MX_RECORD"
STATUS_SMTP_REJECTED = "SMTP_REJECTED"
STATUS_FORMAT_INVALID = "FORMAT_INVALID"
STATUS_ERROR = "ERROR"

# Legacy aliases (used by tests and pipeline)
STATUS_SPAM_TRAP = "SPAM_TRAP"
STATUS_INACTIVE = "INACTIVE"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_ABUSE = "ABUSE"

# Role-based prefixes (info@, sales@, admin@, etc.)
ROLE_PREFIXES = (
    "info", "sales", "admin", "support", "contact", "hello", "help",
    "office", "team", "billing", "accounts", "marketing", "hr",
    "press", "media", "abuse", "postmaster", "webmaster", "noreply",
    "no-reply", "donotreply", "jobs", "careers",
)

# Known disposable / burner email domains (top 50+)
DISPOSABLE_DOMAINS: set[str] = {
    "guerrillamail.com", "guerrillamail.de", "guerrillamail.net",
    "tempmail.com", "throwaway.email", "temp-mail.org",
    "fakeinbox.com", "sharklasers.com", "guerrillamailblock.com",
    "grr.la", "dispostable.com", "yopmail.com", "yopmail.fr",
    "mailinator.com", "guerrillamail.info", "10minutemail.com", "10minutemail.co.uk", "maildrop.cc",
    "discard.email", "discardmail.com", "discardmail.de",
    "tempail.com", "temp-mail.io", "tempmailo.com",
    "mohmal.com", "burnermail.io", "getnada.com",
    "emailondeck.com", "33mail.com", "mytemp.email",
    "tempr.email", "tempinbox.com", "tempinbox.co.uk",
    "mailexpire.com", "mailforspam.com", "spambox.us",
    "spamgourmet.com", "spamherelots.com", "spamhereplease.com",
    "spamhole.com", "spamify.com", "spaminator.de",
    "spamkill.info", "spaml.com", "spaml.de",
    "fakemailz.com", "fakenamegenerator.com",
    "harakirimail.com", "jetable.org", "jnxjn.com",
    "mailcatch.com", "mailnull.com", "mailshell.com",
    "mailsiphon.com", "mailslurp.com", "mailtome.de",
    "mailtothis.com", "mailtrash.net", "mailtv.net",
    "mailtv.tv", "veryreallyshort.com", "veryshortmail.com",
    "wegwerfmail.de", "wegwerfmail.net", "wegwerfmail.org",
}

# SMTP codes that mean "address rejected"
SMTP_REJECT_CODES = {"550", "551", "552", "553", "554"}

# Hunter.io API endpoints
HUNTER_VERIFY_URL = "https://api.hunter.io/v2/email-verifier"
HUNTER_DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"
HUNTER_TIMEOUT = 15.0


@dataclass
class VerifyResult:
    """Result of a single email verification."""
    email: str
    status: str  # One of the STATUS_* constants
    score: float  # 0.0 - 1.0 confidence
    sub_status: str = ""
    mx_found: bool = False
    smtp_check: bool = False


def _random_local(length: int = 12) -> str:
    """Generate a random local part for catch-all detection."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


async def _check_mx(domain: str) -> list[str]:
    """Look up MX records for a domain. Returns list of mail server hostnames."""
    try:
        loop = asyncio.get_event_loop()
        records = await loop.run_in_executor(
            None, lambda: dns.resolver.resolve(domain, "MX")
        )
        return sorted(
            [(r.preference, str(r.exchange).rstrip(".")) for r in records],
            key=lambda x: x[0],
        )
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN,
            dns.resolver.NoNameservers, dns.resolver.Timeout,
            dns.exception.DNSException, socket.gaierror, OSError):
        return []


async def _smtp_probe(mx_host: str, email: str, sender: str = "verify@localhost") -> tuple[bool, str]:
    """SMTP VRFY/RCPT TO probe: connect to MX and check if address is accepted.

    Returns (accepted: bool, response: str).
    """
    try:
        loop = asyncio.get_event_loop()

        def _probe() -> tuple[bool, str]:
            try:
                with smtplib.SMTP(mx_host, 25, timeout=VERIFY_TIMEOUT) as smtp:
                    smtp.ehlo("verify.local")
                    # Some servers require STARTTLS
                    try:
                        smtp.starttls()
                        smtp.ehlo("verify.local")
                    except smtplib.SMTPNotSupportedError:
                        pass
                    # Try MAIL FROM
                    smtp.mail(sender)
                    # RCPT TO — this is the real check
                    code, msg = smtp.rcpt(email)
                    smtp.quit()
                    if code in SMTP_REJECT_CODES:
                        return False, f"{code} {msg.decode(errors='replace')}"
                    return True, f"{code} {msg.decode(errors='replace')}"
            except smtplib.SMTPServerDisconnected:
                return False, "server disconnected"
            except smtplib.SMTPResponseException as e:
                return False, f"{e.smtp_code} {e.smtp_error.decode(errors='replace')}"
            except (TimeoutError, OSError) as e:
                return False, str(e)

        return await loop.run_in_executor(None, _probe)

    except Exception as exc:
        return False, str(exc)


async def _is_catch_all(mx_host: str, domain: str) -> bool:
    """Probe a random address to detect catch-all domains.

    If the MX accepts a clearly-fake address, the domain is catch-all.
    """
    fake = f"{_random_local()}@{domain}"
    accepted, _ = await _smtp_probe(mx_host, fake)
    return accepted


async def _hunter_verify(email: str, api_key: str) -> VerifyResult | None:
    """Verify an email via Hunter.io Email Verifier API.

    Returns VerifyResult on success, None on API failure (caller falls back to SMTP).
    Hunter statuses: valid, invalid, accept_all, disposable, webmail, unknown
    """
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=HUNTER_TIMEOUT) as client:
            resp = await client.get(
                HUNTER_VERIFY_URL,
                params={"email": email, "api_key": api_key},
            )
        if resp.status_code != 200:
            log.warning("Hunter verify HTTP %s for %s", resp.status_code, email)
            return None
        data = resp.json().get("data", {})
        zb_status = (data.get("status") or "unknown").lower()
        score = float(data.get("score") or 0.0)
        # Map Hunter status to our pipeline status
        status_map = {
            "valid": STATUS_VERIFIED,
            "accept_all": STATUS_CATCH_ALL,
            "disposable": STATUS_DISPOSABLE,
            "webmail": STATUS_VERIFIED,  # webmail = real mailbox
            "invalid": STATUS_SMTP_REJECTED,
            "unknown": STATUS_CATCH_ALL,  # treat unknown as catch-all
        }
        status = status_map.get(zb_status, STATUS_CATCH_ALL)
        return VerifyResult(
            email=email, status=status, score=score,
            sub_status=f"hunter:{zb_status}",
            mx_found=True, smtp_check=True,
        )
    except Exception as exc:
        log.warning("Hunter verify failed for %s: %s", email, exc)
        return None


async def verify_email(email: str, api_key: str = "") -> VerifyResult:
    """Verify a single email address.

    Priority: Hunter.io API > SMTP probe + MX checks.
    When Hunter API key is set, uses Hunter first (faster, more accurate).
    Falls back to SMTP probe on API failure.
    """
    # 1. Format check
    if not _looks_valid(email):
        return VerifyResult(email=email, status=STATUS_FORMAT_INVALID, score=0.0)

    local, domain = email.split("@", 1)
    domain = domain.lower()

    # 2. Role-based prefix check
    if local.lower() in ROLE_PREFIXES:
        return VerifyResult(
            email=email, status=STATUS_ROLE_BASED, score=0.3,
            sub_status="role_based",
        )

    # 3. Disposable domain check
    if domain in DISPOSABLE_DOMAINS:
        return VerifyResult(
            email=email, status=STATUS_DISPOSABLE, score=0.1,
            sub_status="disposable",
        )

    # 4. Hunter.io verification (when API key is set)
    if api_key:
        hunter_result = await _hunter_verify(email, api_key)
        if hunter_result:
            return hunter_result
        # Hunter failed — fall through to SMTP probe

    # 5. MX record lookup
    mx_records = await _check_mx(domain)
    if not mx_records:
        return VerifyResult(
            email=email, status=STATUS_NO_MX, score=0.0,
            sub_status="no_mx",
        )

    mx_host = mx_records[0][1]
    primary_mx = mx_host

    # 6. Catch-all detection (probe a random address first)
    catch_all = await _is_catch_all(primary_mx, domain)

    # 7. SMTP probe for the actual address
    accepted, response = await _smtp_probe(primary_mx, email)

    if not accepted:
        return VerifyResult(
            email=email, status=STATUS_SMTP_REJECTED, score=0.0,
            sub_status=response[:100],
            mx_found=True, smtp_check=True,
        )

    if catch_all:
        return VerifyResult(
            email=email, status=STATUS_CATCH_ALL, score=0.5,
            sub_status="catch_all_domain",
            mx_found=True, smtp_check=True,
        )

    return VerifyResult(
        email=email, status=STATUS_VERIFIED, score=0.95,
        mx_found=True, smtp_check=True,
    )


async def verify_emails(
    emails: list[str], api_key: str = "", *, max_concurrent: int = 5
) -> dict[str, VerifyResult]:
    """Verify a batch of emails with concurrency control.

    Returns {email: VerifyResult} for all inputs.
    """
    if not emails:
        return {}

    sem = asyncio.Semaphore(max_concurrent)
    results: dict[str, VerifyResult] = {}

    async def _verify_one(email: str) -> None:
        async with sem:
            result = await verify_email(email, api_key)
            results[email] = result

    await asyncio.gather(*(_verify_one(e) for e in emails))
    return results


def is_sendable(status: str) -> bool:
    """True when an email with this verification status is safe to send."""
    return status in (STATUS_VERIFIED, STATUS_CATCH_ALL)


async def hunter_domain_search(domain: str, api_key: str) -> list[str]:
    """Find email addresses for a domain via Hunter.io Domain Search API.

    Returns list of discovered email addresses (most likely first).
    Free tier: 25 searches/month. Each search returns up to 10 emails.
    """
    if not api_key or not domain:
        return []
    try:
        async with httpx.AsyncClient(timeout=HUNTER_TIMEOUT) as client:
            resp = await client.get(
                HUNTER_DOMAIN_SEARCH_URL,
                params={"domain": domain, "api_key": api_key, "limit": 10},
            )
        if resp.status_code != 200:
            log.warning("Hunter domain search HTTP %s for %s", resp.status_code, domain)
            return []
        data = resp.json().get("data", {})
        emails = []
        for entry in data.get("emails", []):
            addr = entry.get("value", "")
            status = (entry.get("verification") or {}).get("status", "")
            # Only include emails that Hunter considers valid or accept_all
            if addr and status in ("valid", "accept_all", "", "unknown"):
                emails.append(addr)
        return emails
    except Exception as exc:
        log.warning("Hunter domain search failed for %s: %s", domain, exc)
        return []


async def hunter_email_finder(domain: str, first_name: str, last_name: str,
                              api_key: str) -> str | None:
    """Find a specific person's email via Hunter.io Email Finder API.

    Returns the most likely email address, or None.
    Free tier: 25 finders/month.
    """
    if not api_key or not domain or not (first_name or last_name):
        return None
    try:
        async with httpx.AsyncClient(timeout=HUNTER_TIMEOUT) as client:
            resp = await client.get(
                "https://api.hunter.io/v2/email-finder",
                params={
                    "domain": domain,
                    "first_name": first_name,
                    "last_name": last_name,
                    "api_key": api_key,
                },
            )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", {})
        email = data.get("email", "")
        confidence = data.get("confidence", 0)
        # Only return if confidence > 50
        if email and confidence > 50:
            return email
    except Exception as exc:
        log.warning("Hunter email finder failed for %s %s: %s", first_name, last_name, exc)
    return None


def is_catch_all(status: str) -> bool:
    """True when the email is a catch-all domain (risky but not blocked)."""
    return status == STATUS_CATCH_ALL


def _looks_valid(email: str) -> bool:
    """Quick format check — not a replacement for SMTP probe."""
    if not email or "@" not in email:
        return False
    parts = email.split("@")
    if len(parts) != 2:
        return False
    local, domain = parts
    if not local or not domain:
        return False
    return "." in domain and len(domain) <= 253
