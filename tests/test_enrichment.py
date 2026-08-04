"""Enrichment validation tests (spec 7.3 hard rules)."""
from src.enrichment import extract_emails, normalize_email, normalize_instagram


def test_placeholder_email_rejected():
    assert normalize_email("[email protected]") is None


def test_invalid_email_rejected():
    assert normalize_email("not-an-email") is None
    assert normalize_email("user@") is None
    assert normalize_email("@domain.com") is None


def test_blocklisted_domain_rejected():
    for domain in ("example.com", "facebook.com", "linkedin.com",
                   "wixpress.com", "chamberofcommerce.com", "yelp.com"):
        assert normalize_email(f"owner@{domain}") is None


def test_valid_email_normalized():
    assert normalize_email("  Owner.Name@ACME-Plumbing.COM ") == "owner.name@acme-plumbing.com"


def test_extract_emails_unique_and_filtered():
    text = ("Contact us at owner@acmeplumb.com or sales@acmeplumb.com. "
            "Also [email protected] and nope@example.com.")
    emails = extract_emails(text)
    assert emails == ["owner@acmeplumb.com", "sales@acmeplumb.com"]


def test_extract_emails_empty():
    assert extract_emails(None) == []
    assert extract_emails("no emails here") == []


def test_instagram_normalization():
    assert normalize_instagram("@acmeplumb") == "acmeplumb"
    assert normalize_instagram("acme.plumb_1") == "acme.plumb_1"
    assert normalize_instagram("bad handle!") is None
    assert normalize_instagram(None) is None
