"""PII-safe lead identifiers (spec 11 / Step 06).

A lead's stable ID is the sha256 of ``name|address`` (lowercased). Only this
hash is ever stored in the PUBLIC repo (dedupe, approvals); every piece of
identifiable lead data lives in the private Google Sheet, keyed by this hash.
"""
from __future__ import annotations

import hashlib


def lead_id(name: str, address: str = "") -> str:
    """Deterministic, non-reversible id for a lead (name + address)."""
    return hashlib.sha256(f"{name or ''}|{address or ''}".lower().encode()).hexdigest()
