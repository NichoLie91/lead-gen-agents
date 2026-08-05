"""Approval queue tests (Step 04): PII-safe draft approval lifecycle."""
from __future__ import annotations

from src.approvals import ApprovalQueue
from src.core.state import StateStore


def make(tmp_path) -> ApprovalQueue:
    return ApprovalQueue(StateStore(tmp_path / "state"))


def test_register_sets_pending(tmp_path):
    q = make(tmp_path)
    q.register("abc123")
    assert q.status("abc123") == "pending"
    assert q.pending() == ["abc123"]


def test_decide_single_and_prefix_match(tmp_path):
    q = make(tmp_path)
    q.register("abcdef1234567890")
    assert q.decide("abcdef12", "approved") is True   # 8-char prefix
    assert q.status("abcdef1234567890") == "approved"
    assert q.approved() == {"abcdef1234567890"}
    assert q.pending() == []


def test_decide_unknown_returns_false(tmp_path):
    q = make(tmp_path)
    assert q.decide("does-not-exist", "approved") is False


def test_ambiguous_prefix_returns_false(tmp_path):
    q = make(tmp_path)
    q.register("aaaaaaaa11")
    q.register("aaaaaaaa22")
    assert q.decide("aaaaaaaa", "approved") is False  # ambiguous -> no-op


def test_decide_all_approves_every_pending(tmp_path):
    q = make(tmp_path)
    q.register("aaa")
    q.register("bbb")
    assert q.decide_all("approved") == 2
    assert q.approved() == {"aaa", "bbb"}
    assert q.pending() == []
    assert q.decide_all("approved") == 0  # nothing left pending


def test_rejections_tracked_separately(tmp_path):
    q = make(tmp_path)
    q.register("aaa")
    q.register("bbb")
    q.decide("aaa", "rejected")
    assert q.rejected() == {"aaa"}
    assert q.pending() == ["bbb"]
    assert q.approved() == set()


def test_unknown_status_defaults_to_pending(tmp_path):
    q = make(tmp_path)
    assert q.status("never-registered") == "pending"


def test_decisions_persist_across_instances(tmp_path):
    """Bot jobs are ephemeral: job #1 writes, job #2 must read the same file."""
    q1 = make(tmp_path)
    q1.register("abcdef1234567890")
    assert q1.decide("abcdef1234567890", "approved") is True
    # A fresh queue over the same state dir (fresh process) sees the decision.
    q2 = make(tmp_path)
    assert q2.status("abcdef1234567890") == "approved"
    assert q2.approved() == {"abcdef1234567890"}
    assert q2.pending() == []
