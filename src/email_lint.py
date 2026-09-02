"""Cold-email linter — hard enforcement of COLD_EMAIL_PLAYBOOK.md.

Every outbound email passes through :func:`lint_email` BEFORE any Gmail call
(guardrail #0). Non-empty violations = BLOCKED, counted as ``blocked`` — the
email is never sent. Do not weaken these checks: if a rule conflicts with
"send more emails", the playbook wins.

Call order is fixed and matters:

    final = append_footer(raw_body, business_name, criteria)
    violations = lint_email(subject, final, criteria)
    if violations: BLOCK  # never send
    send(final)

``lint_email`` is footer-aware: structure rules (words, sentences, opener,
question marks, links, banned phrases) apply to the COPY only; the CAN-SPAM
footer is validated separately (opt-out line + physical address present).

The linter is deliberately conservative:
- Empty/missing ``sender.physical_address`` blocks 100% of sends (CAN-SPAM
  liability: up to $53,088 per email without a postal address in the footer).
- A body without the opt-out footer blocks (CAN-SPAM opt-out rule) — the
  inbound agent honors "stop" replies same-run.
"""

from __future__ import annotations

import re

# --- Banned phrases (playbook §3) -------------------------------------------
# AI tells + spam triggers. Matched case-insensitively as substrings on the
# lowercased subject+copy.
AI_TELLS = (
    "delve", "testament", "beacon", "tapestry", "furthermore", "moreover",
    "plethora", "synergy", "in today's world", "it is important to note",
    "at the end of the day", "i hope this email finds you well", "i came across",
    "i don't sell a generic chatbot", "custom ai", "revolutionary",
    "game-changer", "cutting-edge", "seamless", "leverage",
)
SPAM_TRIGGERS = (
    "free", "guarantee", "risk-free", "buy now", "special promotion",
    "increase revenue", "urgent", "save money", "click here", "best price",
    "act now", "limited time", "100%", "no obligation", "winner", "cash",
    "discount", "apply now", "call now", "don't delete", "double your",
)

# First-person openers the playbook bans for the first sentence (SMYKM: the
# email is about THEM, not us). Matched only at the very start of the body.
FIRST_PERSON_OPENER = re.compile(r"^\s*(?:I|We|I'm|I am|We're|We are|My|Our)\b")

# Calendar/meeting asks are banned; interest CTA only (playbook §2).
CTA_BANS = (
    "15 minutes", "15 mins", "15 min", "20 minutes", "30 minutes",
    "book a call", "book a time", "calendar", "schedule a call",
    "schedule a meeting", "calendly", "cal.com", "book here",
    "grab time", "pick a time",
)

FOOTER_MARKER = 'reply "stop" to opt out'


def _split_footer(body: str) -> tuple[str, str]:
    """Split a final email into (copy, footer). Footer = signature + opt-out
    + address appended by :func:`append_footer`. Copy is what the rules count."""
    idx = body.lower().find(FOOTER_MARKER)
    if idx == -1:
        return body, ""
    sig = body.rfind("\n— ", 0, idx)
    if sig != -1:
        return body[:sig], body[sig:]
    return body[:idx], body[idx:]


def lint_email(subject: str, body: str, criteria: dict) -> list[str]:
    """Return a list of playbook violations. Empty list = safe to send.

    ``criteria`` is the merged criteria dict (Settings.criteria) — it must
    contain a non-empty ``sender.physical_address`` or every email blocks.
    """
    violations: list[str] = []
    subject = str(subject or "")
    body = str(body or "")

    # --- CAN-SPAM gates (playbook §4) ---------------------------------------
    sender = (criteria.get("sender") or {}) if isinstance(criteria, dict) else {}
    address = str(sender.get("physical_address") or "").strip()
    copy, footer = _split_footer(body)
    if not footer:
        violations.append(
            "CAN-SPAM: footer missing - run append_footer() before lint/send "
            "(opt-out line required)")
    if not address:
        violations.append(
            "CAN-SPAM: sender.physical_address is empty in config/criteria.json "
            "- ALL sends blocked until a street address or PO Box is set "
            "($53,088/email liability)")
    elif footer:
        # First token of the address must survive into the footer (catches a
        # footer built with a different/empty address).
        first_token = address.split(",")[0].split()[0].lower()
        if first_token not in footer.lower():
            violations.append(
                "CAN-SPAM: footer address does not match sender.physical_address")

    # --- Subject rules (playbook §2) ----------------------------------------
    words = subject.split()
    if not words:
        violations.append("Subject: empty")
    if len(words) > 8:
        violations.append(f"Subject: {len(words)} words (max 8, observation-based)")
    subject_l = subject.lower()
    if subject_l.startswith(("re:", "fwd:")):
        violations.append("Subject: Re:/Fwd: thread hijack trick banned")
    if subject.isupper() and len(words) > 1:
        violations.append("Subject: ALL CAPS clickbait banned")

    # --- Copy structure rules (playbook §2) ---------------------------------
    plain = copy.replace("\u00a0", " ")
    combined_l = (subject + "\n" + copy).lower()
    if re.search(r"<\s*(a|img|table|div|p)\b", copy, re.IGNORECASE):
        violations.append("Body: HTML tags found - plain text only")
    if re.search(r"https?://|www\.", plain):
        violations.append("Body: links banned on first touch (deliverability)")
    if re.search(r"\b\S+\.(?:com|net|org|io|co)\b", plain):
        violations.append("Body: bare domain links banned on first touch")
    if re.search(r"\battach\w*", combined_l):
        violations.append("Body: attachments referenced/attached - never on first touch")

    word_count = len(re.findall(r"\b[\w'-]+\b", plain))
    if word_count > 90:
        violations.append(f"Body: {word_count} words (max 90)")
    sentences = [s for s in re.split(r"[.!?]+\s", re.sub(r"\s+", " ", plain).strip()) if s.strip()]
    if len(sentences) > 5:
        violations.append(f"Body: {len(sentences)} sentences (max 5)")
    first_line = plain.strip().split("\n", 1)[0]
    if FIRST_PERSON_OPENER.match(first_line):
        violations.append("Body: opens with I/we - SMYKM rule: it is about THEM, not us")
    q = copy.count("?")
    if q > 1:
        violations.append(f"Body: {q} question marks (max 1 - two asks = zero asks)")

    for phrase in CTA_BANS:
        if phrase in combined_l:
            violations.append(f"CTA: '{phrase}' banned - interest CTA only, reply-first")
            break

    # --- Banned phrases (playbook §3) ---------------------------------------
    for phrase in AI_TELLS:
        if phrase in combined_l:
            violations.append(f"Banned AI tell: '{phrase}'")
    for phrase in SPAM_TRIGGERS:
        if phrase in combined_l:
            violations.append(f"Spam trigger: '{phrase}'")

    return violations


def build_footer(business_name: str, criteria: dict) -> str:
    """CAN-SPAM compliant footer per playbook §4."""
    sender = (criteria.get("sender") or {}) if isinstance(criteria, dict) else {}
    address = str(sender.get("physical_address") or "").strip()
    name = str(business_name or "the team").strip()
    return (
        f"\n\n— {name}\n"
        "You're receiving this one-time note. Reply \"stop\" to opt out - honored immediately.\n"
        f"{address}"
    )


def append_footer(body: str, business_name: str, criteria: dict) -> str:
    """Append the CAN-SPAM footer if it is not already present."""
    if FOOTER_MARKER in body.lower():
        return body  # footer already present (idempotent)
    return body + build_footer(business_name, criteria)
