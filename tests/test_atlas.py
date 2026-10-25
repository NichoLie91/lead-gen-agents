"""Atlas discovery filter tests (spec 7.2 + job doc 1 target gates)."""
from src.agents.atlas import (
    dedupe,
    flag_in_pool_chains,
    has_any_contact,
    is_chain,
    is_closed,
    passes_target_gate,
)
from src.agents.maps_agent import _business_name


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


def test_mock_names_terminate_past_the_prefix_count():
    """criteria.json ships 24 metros but only 20 name prefixes. The generator
    used to retry the same 20 names forever and hang every dry run."""
    taken: set[str] = set()
    names = [_business_name(i, "plumber", taken) for i in range(24)]
    assert len(set(names)) == 24


def test_mock_names_cover_new_verticals():
    """criteria.json can list any vertical; offline discovery used to raise
    ValueError on anything outside _VERTICAL_TITLES ('drain cleaning')."""
    taken: set[str] = set()
    names = [_business_name(i, "drain cleaning", taken) for i in range(4)]
    assert all(n.endswith("Drain Cleaning") for n in names)
    assert len(set(names)) == 4  # unique per metro, or chain detection zeroes the pool


def test_target_gate_requires_4_4_stars():
    """Job doc 1: target businesses are 4.4-5 stars."""
    assert passes_target_gate({"rating": 4.4, "reviews": 50}) is True
    assert passes_target_gate({"rating": 4.8, "reviews": 120}) is True
    assert passes_target_gate({"rating": 4.3, "reviews": 50}) is False
    assert passes_target_gate({"rating": 4.9, "reviews": 4}) is False   # < 5 reviews
    assert passes_target_gate({"rating": 4.9, "reviews": 3000}) is False  # > 2000
    assert passes_target_gate({"rating": None, "reviews": 50}) is False  # can't confirm
    assert passes_target_gate({"rating": "4.6", "reviews": "80"}) is True  # strings ok
    assert passes_target_gate({"rating": "junk", "reviews": 50}) is False  # malformed


# ---------- discovery must fail loudly, not silently ----------

def _maps_agent_with(composio, settings):
    from src.agents.maps_agent import MapsAgent
    return MapsAgent(composio, settings)


class _DeadProvider:
    """Every Maps call fails the way a disabled toolkit does."""

    connected = True

    async def search_google_maps(self, query, start=0):
        return []


class _LiveProvider:
    connected = True

    async def search_google_maps(self, query, start=0):
        return [{"name": "Acme Plumbing", "address": "1 Main St",
                 "phone": "555-0100", "website": ""}]


def test_discovery_raises_when_every_query_fails(monkeypatch):
    """A dead provider must not look like an empty market (2026-10-24: 750/750
    Google Maps calls returned 403 and the run still reported COMPLETED)."""
    import asyncio

    import pytest

    from src.agents.maps_agent import DiscoveryUnavailable
    from src.core.config import Settings

    settings = Settings(dry_run=False)
    agent = _maps_agent_with(_DeadProvider(), settings)
    with pytest.raises(DiscoveryUnavailable):
        asyncio.run(agent.discover(verticals=["plumber"], metros=["Houston"]))


def test_discovery_returns_leads_when_provider_works():
    import asyncio

    from src.core.config import Settings

    settings = Settings(dry_run=False)
    agent = _maps_agent_with(_LiveProvider(), settings)
    pool = asyncio.run(agent.discover(verticals=["plumber"], metros=["Houston"]))
    assert len(pool) == 1
    assert pool[0]["vertical"] == "plumber"
    assert pool[0]["city"] == "Houston"
