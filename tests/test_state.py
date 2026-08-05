"""StateStore persistence tests — regression for the cached-dict aliasing bug
that silently dropped writes (state.set() and in-place mutations)."""
from __future__ import annotations

from src.core.state import StateStore


def test_set_persists_to_disk(tmp_path):
    root = tmp_path / "state"
    s = StateStore(root)
    assert s.set("demo", "k", "v") is True
    assert (root / "demo.json").exists()
    fresh = StateStore(root)   # new process view
    assert fresh.get("demo", "k") == "v"


def test_mutating_cached_object_still_persists(tmp_path):
    root = tmp_path / "state"
    s = StateStore(root)
    s.save("demo", {"a": 1})
    data = s.load("demo", {})
    data["b"] = 2                              # mutate the cached object
    assert s.save_if_changed("demo", data) is True   # must NOT see "no change"
    fresh = StateStore(root)
    assert fresh.load("demo") == {"a": 1, "b": 2}


def test_save_if_changed_skips_when_unchanged(tmp_path):
    root = tmp_path / "state"
    s = StateStore(root)
    assert s.save_if_changed("demo", {"a": 1}) is True
    assert s.save_if_changed("demo", {"a": 1}) is False  # identical -> no write
