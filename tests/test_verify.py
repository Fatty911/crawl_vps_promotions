"""Tests for vps_monitor.verify (plan-token re-fetch gate)."""

import pathlib

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


def test_verify_browser_retries_with_fresh_node(monkeypatch):
    """The real _browser_render loops up to 3 attempts, rotating nodes; a
    challenge page on attempt 1 must be retried on a fresh node and succeed
    on attempt 2 (observed 2026-08-09: BuyVM SLICE failed via the first
    mihomo node)."""
    import requests

    from vps_monitor import verify as verify_mod

    class R:
        text = "<html><title>Just a moment...</title><body>Checking your browser before accessing.</body></html>"

    monkeypatch.setattr(
        requests, "get",
        lambda url, timeout, headers, allow_redirects, proxies=None: R(),
    )

    renders = {"count": 0}
    rotates = {"count": 0}

    class FakePage:
        def goto(self, url, **kw):
            pass

        def content(self):
            renders["count"] += 1
            if renders["count"] == 1:
                return "<html><title>Just a moment...</title></html>"
            return "<html><h2>SLICE 4096</h2><p>4096 MB Memory 80 GB SSD Storage</p></html>"

    class FakeBrowser:
        def new_page(self, **kw):
            return FakePage()

        def close(self):
            pass

    class FakePlaywright:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        @property
        def chromium(self):
            return self

        def launch(self, headless=True, **kw):
            return FakeBrowser()

    class FakeRotator:
        def __init__(self, *a, **k):
            pass

        def discover_nodes(self):
            self._enabled = True

        @property
        def enabled(self):
            return True

        def rotate(self):
            rotates["count"] += 1

    monkeypatch.setattr(verify_mod, "_sync_playwright", FakePlaywright)
    monkeypatch.setattr(verify_mod, "_get_rotator", lambda: FakeRotator())

    ok, missing = verify_mod.verify_plan_tokens(
        "https://buyvm.net/kvm-dedicated-server-slices",
        ["SLICE 4096", "4096 MB", "80 GB SSD"],
    )
    assert ok is True
    assert missing == []
    assert renders["count"] == 2  # challenge first, then success
    assert rotates["count"] >= 1  # a fresh node was picked between retries


def test_verify_browser_rotation_logic_present(monkeypatch):
    """The real _browser_render must rotate nodes between retries."""
    from vps_monitor import verify as verify_mod

    src = pathlib.Path(verify_mod.__file__).read_text(encoding="utf-8")
    assert "rotator.rotate()" in src
    assert "for _attempt in range(3)" in src
    assert "_looks_like_challenge" in src


def test_verify_include_page_returns_visible_text(monkeypatch):
    """include_page=True returns the fetched page's visible text so the fix
    agent can diagnose from the real page (observed 2026-08-10 run
    31379078505: model wanted the actual BuyVM HTML)."""
    import requests

    from vps_monitor import verify as verify_mod

    class R:
        text = "<html><h2>SLICE <span>4096</span></h2><p>4096 MB Memory 80 GB SSD</p></html>"

    monkeypatch.setattr(
        requests, "get",
        lambda url, timeout, headers, allow_redirects, proxies=None: R(),
    )
    ok, missing, page = verify_mod.verify_plan_tokens(
        "https://buyvm.net/kvm-dedicated-server-slices#slice4096",
        ["SLICE 4096", "4096 MB", "80 GB SSD"],
        include_page=True,
    )
    assert ok is True
    assert missing == []
    assert "slice 4096" in page
    assert "80 gb ssd" in page
    # Default (include_page=False) keeps the 2-tuple contract.
    ok2, missing2 = verify_mod.verify_plan_tokens(
        "https://buyvm.net/kvm-dedicated-server-slices#slice4096",
        ["SLICE 4096", "4096 MB", "80 GB SSD"],
    )
    assert ok2 is True and missing2 == []


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
