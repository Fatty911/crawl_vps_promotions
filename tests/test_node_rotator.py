"""Unit tests for scripts/node_rotator.py (mihomo node rotation core)."""

import json
from pathlib import Path

import pytest

from scripts.node_rotator import NodeRotator


class _FakeSession:
    """Minimal fake requests.Session for the controller API."""

    def __init__(self):
        self.put_calls = []
        self.get_calls = []
        self._nodes = ["n1", "n2", "n3"]
        self._select = "n1"
        self._fail_put = False

    def get(self, url, timeout=5):
        self.get_calls.append(url)
        import types

        resp = types.SimpleNamespace(
            status_code=200,
            json=lambda: {"all": list(self._nodes)},
        )
        return resp

    def put(self, url, json=None, timeout=5):
        self.put_calls.append((url, json))
        import types

        if self._fail_put:
            return types.SimpleNamespace(status_code=500)
        if json and json.get("name"):
            self._select = json["name"]
        return types.SimpleNamespace(status_code=204)


def _make_rotator(tmp_path: Path, fail_put: bool = False) -> NodeRotator:
    session = _FakeSession()
    session._fail_put = fail_put
    rotator = NodeRotator(
        controller="http://127.0.0.1:9090",
        group="PROXY",
        blacklist_path=tmp_path / "proxy_blacklist.json",
        switch_interval=0.0,
    )
    rotator._session = session
    rotator.discover_nodes()
    return rotator


def test_discover_nodes_and_properties(tmp_path):
    rotator = _make_rotator(tmp_path)
    assert rotator.enabled is True
    assert rotator.node_count == 3
    assert rotator._nodes == ["n1", "n2", "n3"]


def test_rotate_round_robin_and_sets_active(tmp_path):
    rotator = _make_rotator(tmp_path)
    seen = {rotator.rotate() for _ in range(4)}
    assert seen <= {"n1", "n2", "n3"}
    assert rotator._active_node in {"n1", "n2", "n3"}
    assert len(rotator._session.put_calls) == 4


def test_mark_failure_blacklists_after_threshold(tmp_path):
    rotator = _make_rotator(tmp_path)
    rotator._active_node = "n1"
    rotator.mark_failure("n1")
    assert "n1" not in rotator._runtime_blacklist  # below threshold (3)
    rotator.mark_failure("n1")
    assert "n1" not in rotator._runtime_blacklist
    rotator.mark_failure("n1")
    assert "n1" in rotator._runtime_blacklist


def test_blocked_blacklists_immediately(tmp_path):
    rotator = _make_rotator(tmp_path)
    rotator.mark_failure("n2", blocked=True)
    assert "n2" in rotator._runtime_blacklist


def test_all_blacklisted_clears_for_reprobe(tmp_path):
    rotator = _make_rotator(tmp_path)
    for node in ("n1", "n2", "n3"):
        rotator.mark_failure(node, blocked=True)
    assert rotator._runtime_blacklist == {"n1", "n2", "n3"}
    # next_node must clear the blacklist and return a node instead of failing
    node = rotator.next_node()
    assert node in {"n1", "n2", "n3"}
    assert rotator._runtime_blacklist == set()


def test_save_stats_roundtrip(tmp_path):
    rotator = _make_rotator(tmp_path)
    rotator.mark_failure("n1", blocked=True)
    rotator.mark_success("n2")
    rotator.save_stats()
    assert rotator.blacklist_path.exists()
    data = json.loads(rotator.blacklist_path.read_text(encoding="utf-8"))
    nodes = data["nodes"]
    assert nodes["n1"]["fail"] >= 1 and nodes["n1"]["blocked"] >= 1
    assert nodes["n2"]["ok"] >= 1
    # Reload into a fresh rotator and confirm stats survive.
    rotator2 = _make_rotator(tmp_path)
    assert rotator2._stats.get("n1", {}).get("blocked", 0) >= 1
    assert rotator2._stats.get("n2", {}).get("ok", 0) >= 1


def test_switch_failure_keeps_previous_active(tmp_path):
    rotator = _make_rotator(tmp_path, fail_put=True)
    rotator._active_node = "n1"
    ok = rotator.switch("n2")
    assert ok is False
    assert rotator._active_node == "n1"  # unchanged on failure
