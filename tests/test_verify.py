"""Tests for vps_monitor.verify (plan-token re-fetch gate)."""

import pytest

from vps_monitor.verify import verify_plan_tokens


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


def test_all_tokens_present_confirms(monkeypatch):
    import requests

    def fake_get(url, timeout, headers, allow_redirects):
        assert url == "https://example.com/slice"
        assert timeout == 30
        assert "Chrome" in headers["User-Agent"]
        return _FakeResponse("<html>SLICE 4096 plan 4096 MB SSD</html>")

    monkeypatch.setattr(requests, "get", fake_get)
    ok, missing = verify_plan_tokens("https://example.com/slice", ["SLICE 4096", "4096 MB"])
    assert ok is True
    assert missing == []


def test_missing_token_fails(monkeypatch):
    import requests

    monkeypatch.setattr(
        requests, "get",
        lambda url, timeout, headers, allow_redirects: _FakeResponse("<html>nothing here</html>"),
    )
    ok, missing = verify_plan_tokens("https://example.com/x", ["SLICE 4096"])
    assert ok is False
    assert missing == ["SLICE 4096"]


def test_fetch_failure_is_not_confirmed(monkeypatch):
    import requests

    def boom(url, timeout, headers, allow_redirects):
        raise RuntimeError("network down")

    monkeypatch.setattr(requests, "get", boom)
    ok, missing = verify_plan_tokens("https://example.com/x", ["TOKEN-A"])
    assert ok is False
    assert missing == ["TOKEN-A"]


def test_empty_inputs_are_not_confirmed():
    ok, missing = verify_plan_tokens("", ["X"])
    assert ok is False
    assert missing == ["X"]
    ok, missing = verify_plan_tokens("https://example.com", [])
    assert ok is False
    assert missing == []
