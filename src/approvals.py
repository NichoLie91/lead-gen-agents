"""Approval queue (Step 04 / spec 5.1) — Telegram human-in-the-loop.

PII-SAFE (spec 11): keys are ``lead_id`` sha256 hashes only (src/core/ident.py).
The actual draft content (subject/body/email) lives in the private Outreach
sheet, keyed by the same hash, so nothing identifiable enters this repo.

Lifecycle per draft: ``pending`` -> ``approved`` | ``rejected``.
"""
from __future__ import annotations

from src.core.state import StateStore

DECISIONS = ("pending", "approved", "rejected")
_STATE_FILE = "approvals"


class ApprovalQueue:
    def __init__(self, state: StateStore):
        self._state = state

    def _data(self) -> dict[str, str]:
        # Copy: never mutate the state store's cached object in place, or
        # save_if_changed() will see no diff and skip the write.
        data = self._state.load(_STATE_FILE, {})
        return dict(data) if isinstance(data, dict) else {}

    def register(self, lead_id: str) -> None:
        """Mark a freshly drafted lead as awaiting approval."""
        data = self._data()
        data.setdefault(lead_id, "pending")
        self._state.save_if_changed(_STATE_FILE, data)

    def decide(self, lead_id: str, decision: str) -> bool:
        """Approve/reject ONE draft (accepts 8-char prefix ids). False if no match."""
        if decision not in ("approved", "rejected"):
            return False
        matched = self._match(lead_id)
        if not matched:
            return False
        data = self._data()
        for lid in matched:
            data[lid] = decision
        self._state.save_if_changed(_STATE_FILE, data)
        return True

    def decide_all(self, decision: str) -> int:
        """Approve/reject every pending draft; returns how many changed."""
        if decision not in ("approved", "rejected"):
            return 0
        data = self._data()
        changed = sum(1 for cur in data.values() if cur == "pending")
        if not changed:
            return 0
        for lid, cur in data.items():
            if cur == "pending":
                data[lid] = decision
        self._state.save_if_changed(_STATE_FILE, data)
        return changed

    def status(self, lead_id: str) -> str:
        return self._data().get(lead_id, "pending")

    def pending(self) -> list[str]:
        return [lid for lid, d in self._data().items() if d == "pending"]

    def approved(self) -> set[str]:
        return {lid for lid, d in self._data().items() if d == "approved"}

    def rejected(self) -> set[str]:
        return {lid for lid, d in self._data().items() if d == "rejected"}

    def _match(self, lead_id_or_prefix: str) -> list[str]:
        """Full-id match, else unique prefix match (drafts are shown by 8-char id)."""
        needle = (lead_id_or_prefix or "").strip().lower()
        if not needle:
            return []
        if needle in self._data():
            return [needle]
        hits = [lid for lid in self._data() if lid.startswith(needle)]
        return hits if len(hits) == 1 else []
