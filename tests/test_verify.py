"""Tests for vps_monitor.verify (plan-token re-fetch gate)."""

import pytest

from vps_monitor.verify import verify_plan_tokens


class _FakeResponse:
    def __init__(self, text: str):
        self.text = text


def test_all_tokens_present_confirms(monkeypatch):
    import requests

    def fake_get(url, timeout, headers, allow_redirects, proxies=None):
        assert url == "https://example.com/slice"
        assert timeout == 30
        assert "Chrome" in headers["User-Agent"]
        assert proxies is None  # no proxy env in test
        return _FakeResponse("<html>SLICE 4096 plan 4096 MB SSD</html>")

    monkeypatch.setattr(requests, "get", fake_get)
    ok, missing = verify_plan_tokens("https://example.com/slice", ["SLICE 4096", "4096 MB"])
    assert ok is True
    assert missing == []


def test_verify_matches_visible_text_not_raw_html(monkeypatch):
    """Store pages split tokens across tags (e.g. 'SLICE <span>4096</span>').
    verify must match against rendered visible text, not raw HTML — raw
    substring checks falsely report retired (observed 2026-08-09 on BuyVM)."""
    import requests

    raw_html = (
        "<html><body>"
        "<h2>SLICE <span>4096</span></h2>"
        "<li><strong>4096 MB</strong> Memory</li>"
        "<li><strong>80 GB SSD</strong> Storage</li>"
        "</body></html>"
    )

    def fake_get(url, timeout, headers, allow_redirects, proxies=None):
        class R:
            text = raw_html
        return R()

    monkeypatch.setattr(requests, "get", fake_get)
    ok, missing = verify_plan_tokens("https://example.com/slice", ["SLICE 4096", "4096 MB", "80 GB SSD"])
    assert ok is True
    assert missing == []


def test_verify_browser_fallback_confirms_js_rendered_store(monkeypatch):
    """A Cloudflare/JS-rendered store returns a challenge page to requests;
    verify must fall back to a browser render before declaring retired
    (observed 2026-08-09: BuyVM behind CF was wrongly marked external_retired
    even after the proxy fix, because requests cannot execute JS)."""
    import requests

    class R:
        text = "<html><title>Just a moment...</title><body>Checking your browser before accessing.</body></html>"

    def fake_get(url, timeout, headers, allow_redirects, proxies=None):
        return R()

    monkeypatch.setattr(requests, "get", fake_get)

    from vps_monitor import verify as verify_mod

    real_browser_render = verify_mod._browser_render
    monkeypatch.setattr(
        verify_mod, "_browser_render",
        # Real _browser_render returns _visible_text() (already casefolded).
        lambda url, timeout=30000: "slice 4096 4096 mb memory 80 gb ssd storage",
    )
    ok, missing = verify_mod.verify_plan_tokens(
        "https://buyvm.net/kvm-dedicated-server-slices",
        ["SLICE 4096", "4096 MB", "80 GB SSD"],
    )
    assert ok is True
    assert missing == []
    monkeypatch.setattr(verify_mod, "_browser_render", real_browser_render)


def test_verify_browser_fallback_reports_unconfirmed_when_both_fail(monkeypatch):
    """If requests gets a challenge page AND the browser render fails, the
    result must be NOT confirmed (safe: never repair on unconfirmed data)."""
    import requests

    class R:
        text = "<html><title>Just a moment...</title></html>"

    monkeypatch.setattr(
        requests, "get",
        lambda url, timeout, headers, allow_redirects, proxies=None: R(),
    )
    from vps_monitor import verify as verify_mod

    monkeypatch.setattr(verify_mod, "_browser_render", lambda url, timeout=30000: None)
    ok, missing = verify_mod.verify_plan_tokens("https://example.com/x", ["TOKEN-A"])
    assert ok is False
    assert missing == ["TOKEN-A"]


def test_verify_uses_mihomo_proxy_env(monkeypatch):
    """verify must honour HTTP_PROXY (same channel as the monitor's requests
    path). Without it, verify fetches from the raw runner IP and gets
    anti-bot pages — misclassifying healthy tasks as retired (observed
    2026-08-09: BuyVM SLICE tokens present but verify said NOT confirmed)."""
    import requests

    captured = {}

    def fake_get(url, timeout, headers, allow_redirects, proxies=None):
        captured["proxies"] = proxies
        return _FakeResponse("<html>SLICE 4096 plan 4096 MB SSD</html>")

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(requests, "get", fake_get)
    ok, _ = verify_plan_tokens("https://example.com/slice", ["SLICE 4096"])
    assert ok is True
    assert captured["proxies"] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


def test_missing_token_fails(monkeypatch):
    import requests

    monkeypatch.setattr(
        requests, "get",
        lambda url, timeout, headers, allow_redirects, proxies=None: _FakeResponse("<html>nothing here</html>"),
    )
    ok, missing = verify_plan_tokens("https://example.com/x", ["SLICE 4096"])
    assert ok is False
    assert missing == ["SLICE 4096"]


def test_fetch_failure_is_not_confirmed(monkeypatch):
    import requests

    def boom(url, timeout, headers, allow_redirects, proxies=None):
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
