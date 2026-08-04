"""Atlas discovery filter tests (spec 7.2)."""
from src.agents.atlas import (
    dedupe,
    flag_in_pool_chains,
    has_any_contact,
    is_chain,
    is_closed,
)


def test_chain_detection():
    assert is_chain("Roto-Rooter Plumbing")
    assert is_chain("Benjamin Franklin Plumbing")
    assert not is_chain("Houston Plumbing Co")


def test_closed_detection():
    assert is_closed("Permanently closed")
    assert is_closed("Closed · Opens 9AM")
    assert not is_closed("Open")
    assert not is_closed("")


def test_contact_rule():
    assert has_any_contact({"phone": "555"}) is True
    assert has_any_contact({"email": "x@y.com"}) is True
    assert has_any_contact({"instagram": "ig"}) is True
    assert has_any_contact({"phone": "", "email": "", "instagram": ""}) is False


def test_dedupe_by_name_and_address():
    leads = [
        {"name": "Acme", "address": "1 Main St"},
        {"name": "acme", "address": "1 MAIN ST"},      # case-insensitive dup
        {"name": "Acme", "address": "2 Main St"},      # different address: keep
    ]
    result = dedupe(leads)
    assert len(result) == 2


def test_in_pool_chain_flag():
    leads = [
        {"name": "Dental Smiles", "address": "a"},
        {"name": "Dental Smiles", "address": "b"},
        {"name": "Dental Smiles", "address": "c"},
        {"name": "Local Shop", "address": "d"},
    ]
    chains = flag_in_pool_chains(leads)
    assert "dental smiles" in chains
    assert "local shop" not in chains
