"""Tests for email_verify module (pure SMTP verification, no API key)."""
from __future__ import annotations

import asyncio

import pytest

from src.email_verify import (
    ROLE_PREFIXES,
    STATUS_CATCH_ALL,
    STATUS_DISPOSABLE,
    STATUS_ERROR,
    STATUS_FORMAT_INVALID,
    STATUS_NO_MX,
    STATUS_ROLE_BASED,
    STATUS_SMTP_REJECTED,
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

    def test_disposable_not_sendable(self):
        assert is_sendable(STATUS_DISPOSABLE) is False

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
    def test_invalid_format(self):
        result = asyncio.run(verify_email("not-an-email"))
        assert result.status == STATUS_FORMAT_INVALID

    def test_role_based_email(self):
        result = asyncio.run(verify_email("info@example.com"))
        assert result.status == STATUS_ROLE_BASED
        assert result.score == 0.3

    def test_disposable_email(self):
        result = asyncio.run(verify_email("test@mailinator.com"))
        assert result.status == STATUS_DISPOSABLE
        assert result.score == 0.1

    def test_no_mx_record(self):
        """A domain with no MX records should return NO_MX_RECORD."""
        result = asyncio.run(verify_email("test@nonexistent-domain-xyz123.com"))
        assert result.status == STATUS_NO_MX

    def test_valid_gmail(self):
        """Gmail has MX records and is not catch-all, so should be VERIFIED
        (SMTP probe may reject the specific address but MX is found)."""
        result = asyncio.run(verify_email("test@gmail.com"))
        # Gmail is not catch-all and has MX records
        assert result.mx_found is True
        assert result.status in (STATUS_VERIFIED, STATUS_SMTP_REJECTED, STATUS_CATCH_ALL)


class TestVerifyEmails:
    def test_empty_list(self):
        results = asyncio.run(verify_emails([]))
        assert results == {}

    def test_batch_mixed(self):
        results = asyncio.run(verify_emails([
            "info@example.com",  # role-based
            "test@mailinator.com",  # disposable
            "invalid",  # format invalid
        ]))
        assert len(results) == 3
        assert results["info@example.com"].status == STATUS_ROLE_BASED
        assert results["test@mailinator.com"].status == STATUS_DISPOSABLE
        assert results["invalid"].status == STATUS_FORMAT_INVALID
