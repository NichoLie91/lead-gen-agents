"""Inbound reply handling (Step 06) — turn lead replies into actions.

Pure logic (no I/O): parse the sender from a Gmail header, classify the reply
intent (Gemini label wins when present, keyword fallback otherwise) and build
suggested owner responses. The pipeline stage matches the sender to the CRM,
updates status, and notifies the owner on Telegram.
"""
from __future__ import annotations

import re

KINDS = ("INTERESTED", "PRICE_OBJECTION", "STOP", "QUESTION", "OTHER")

_STOP_RE = re.compile(r"\b(?:stop|unsub\w*|opt\s?out|remove\s+me|no\s+more|not\s+interested|leave\s+me\s+alone)\b")
_PRICE_RE = re.compile(r"\b(how much|price|pricing|cost|quote|budget|too expensive|expensive|out of budget|cheap\w*)\b")
_INTEREST_RE = re.compile(r"\b(yes|interested|let'?s talk|sounds good|book\w*|schedule|more info|tell me more|what'?s next|send me)\b")


def parse_sender_email(sender: str | None) -> str | None:
    """Extract the address from a Gmail sender like 'Name <a@b.com>'."""
    if not sender:
        return None
    match = re.search(r"<([^<>]+)>", sender)
    candidate = match.group(1).strip() if match else sender.strip()
    if re.fullmatch(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", candidate):
        return candidate.lower()
    return None


def classify_reply(text: str, llm_label: str = "") -> str:
    """One of KINDS. An LLM label is trusted first; keyword fallback otherwise."""
    label = (llm_label or "").strip().upper()
    if label in KINDS:
        return label
    t = (text or "").lower()
    if _STOP_RE.search(t):
        return "STOP"
    if _PRICE_RE.search(t):
        return "PRICE_OBJECTION"
    if _INTEREST_RE.search(t):
        return "INTERESTED"
    return "QUESTION"   # anything else is worth a human look


def suggested_reply(kind: str, lead: dict | None = None) -> str:
    """A short suggested owner response per intent (human-in-the-loop)."""
    name = (lead or {}).get("Name") or "the business"
    if kind == "INTERESTED":
        return (
            f"Reply to {name}: thanks for the note — glad this resonates. "
            "Here's a quick 15-minute slot this week to scope it; I'll walk "
            "through exactly how the build would work and what it would cost. "
            "What day works best?"
        )
    if kind == "PRICE_OBJECTION":
        return (
            f"Reply to {name}: totally fair on price. Most builds pay for "
            "themselves in the first month — happy to send a simple "
            "break-even sketch based on your numbers, no commitment."
        )
    if kind == "STOP":
        return f"Reply to {name}: done — you're unsubscribed, sorry for the noise."
    return f"Reply to {name}: acknowledge + ask one specific question to move forward."
