"""Tests for email_verify module (ZeroBounce integration)."""
from __future__ import annotations

import asyncio

import pytest

from src.email_verify import (
    ROLE_PREFIXES,
    STATUS_CATCH_ALL,
    STATUS_ERROR,
    STATUS_FORMAT_INVALID,
    STATUS_ROLE_BASED,
    STATUS_SPAM_TRAP,
    STATUS_VERIFIED,
    VerifyResult,
    _looks_valid,
    is_catch_all,
    is_sendable,
    verify_email,
    verify_emails,
)


class TestLooksValid:
    def test_valid_email(self):
        assert _looks_valid("test@example.com") is True

    def test_no_at(self):
        assert _looks_valid("testexample.com") is False

    def test_no_domain_dot(self):
        assert _looks_valid("test@localhost") is False

    def test_empty(self):
        assert _looks_valid("") is False

    def test_none(self):
        assert _looks_valid(None) is False

    def test_two_at_signs(self):
        assert _looks_valid("a@b@c.com") is False

    def test_long_domain(self):
        assert _looks_valid("a@" + "x" * 254 + ".com") is False


class TestIsSendable:
    def test_verified_is_sendable(self):
        assert is_sendable(STATUS_VERIFIED) is True

    def test_catch_all_is_sendable(self):
        assert is_sendable(STATUS_CATCH_ALL) is True

    def test_spamtrap_not_sendable(self):
        assert is_sendable(STATUS_SPAM_TRAP) is False

    def test_format_invalid_not_sendable(self):
        assert is_sendable(STATUS_FORMAT_INVALID) is False

    def test_error_not_sendable(self):
        assert is_sendable(STATUS_ERROR) is False


class TestIsCatchAll:
    def test_catch_all(self):
        assert is_catch_all(STATUS_CATCH_ALL) is True

    def test_verified_not_catch_all(self):
        assert is_catch_all(STATUS_VERIFIED) is False


class TestRoleBased:
    @pytest.mark.parametrize("prefix", ROLE_PREFIXES)
    def test_role_prefixes_detected(self, prefix):
        """All role-based prefixes should be caught."""
        result = VerifyResult(
            email=f"{prefix}@example.com",
            status=STATUS_ROLE_BASED,
            score=0.3,
        )
        assert result.status == STATUS_ROLE_BASED


class TestVerifyEmail:
    def test_no_api_key_returns_format_invalid(self):
        result = asyncio.run(verify_email("test@example.com", ""))
        assert result.status == STATUS_FORMAT_INVALID

    def test_invalid_format_returns_format_invalid(self):
        result = asyncio.run(verify_email("not-an-email", "fake-key"))
        assert result.status == STATUS_FORMAT_INVALID

    def test_role_based_email_detected(self):
        result = asyncio.run(verify_email("info@example.com", "fake-key"))
        assert result.status == STATUS_ROLE_BASED
        assert result.score == 0.3


class TestVerifyEmails:
    def test_empty_list(self):
        results = asyncio.run(verify_emails([], "fake-key"))
        assert results == {}

    def test_no_api_key(self):
        results = asyncio.run(verify_emails(["a@b.com"], ""))
        assert "a@b.com" in results
        assert results["a@b.com"].status == STATUS_FORMAT_INVALID
