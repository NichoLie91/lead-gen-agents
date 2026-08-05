"""ComposioAgent retry logic tests: which failures are retryable (spec 7.x)."""
from __future__ import annotations

from src.agents.composio_agent import ComposioAgent


def test_quota_exceeded_is_retryable():
    # The exact Google Sheets throttle the pipeline used to crash on.
    assert ComposioAgent._is_retryable(
        "Quota exceeded for Read requests per minute"
    ) is True


def test_rate_limit_and_server_errors_are_retryable():
    assert ComposioAgent._is_retryable("HTTP 429 Too Many Requests") is True
    assert ComposioAgent._is_retryable("RESOURCE_EXHAUSTED: quota") is True
    assert ComposioAgent._is_retryable("upstream request timeout") is True
    assert ComposioAgent._is_retryable("connection reset by peer") is True


def test_semantic_errors_are_not_retryable():
    # Retrying these would just burn quota — the fix must NOT loop on them.
    assert ComposioAgent._is_retryable("Sheet Pipeline not found") is False
    assert ComposioAgent._is_retryable("Invalid argument: bad spreadsheet_id") is False
    assert ComposioAgent._is_retryable("") is False
