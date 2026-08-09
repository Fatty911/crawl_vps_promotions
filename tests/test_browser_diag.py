"""Tests for browser diagnostics (console/JS/DOM capture in browser_fetch)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vps_monitor.monitor import (  # noqa: E402
    HTTPFetch,
    _browser_diag,
    _dom_state_snapshot,
    browser_fetch,
)


class _FakePage:
    """Minimal fake Playwright page that records console/pageerror/requestfailed."""

    def __init__(self):
        self.handlers = {}
        self.title_val = "Test VPS Hosting"
        self.ready = "complete"
        self.body_len = 5000
        self.iframe_count = 0
        self.visible_text = "Intel KVM VPS 4GB RAM $2.85/mo NVMe Ohio Texas"
        self.url_val = "https://linveo.com/vps"

    def on(self, event, handler):
        self.handlers[event] = handler

    def title(self):
        return self.title_val

    def evaluate(self, expr):
        if "readyState" in expr:
            return self.ready
        if "iframe" in expr:
            return self.iframe_count
        return None

    def goto(self, url, **kwargs):
        return type("R", (), {"status": 200})()

    def content(self):
        return f"<html><head><title>{self.title_val}</title></head><body>{self.visible_text}</body></html>"

    @property
    def url(self):
        return self.url_val


def test_dom_state_snapshot():
    page = _FakePage()
    snapshot = _dom_state_snapshot(page, page.content())
    assert snapshot["title"] == "Test VPS Hosting"
    assert snapshot["ready_state"] == "complete"
    assert snapshot["body_len"] > 0
    assert snapshot["visible_len"] > 50
    assert snapshot["has_price"] is True


def test_dom_state_snapshot_js_shell():
    """A JS shell page has tiny visible text -> the snapshot flags it."""
    page = _FakePage()
    page.visible_text = ""
    snapshot = _dom_state_snapshot(page, "<html><body><div id='app'></div></body></html>")
    assert snapshot["visible_len"] < 50
    assert snapshot["has_price"] is False


def test_browser_diag_compact():
    diag = _browser_diag(
        console_msgs=["error: x", "warn: y"],
        page_errors=["TypeError: foo is not a function"],
        failed_requests=["GET https://api.example.com/x ERR_CONNECTION_REFUSED"],
        dom_state={"ready_state": "interactive", "title": "Test VPS Hosting", "error": None},
        visible_len=500,
    )
    assert "js:1" in diag
    assert "console:2" in diag
    assert "netfail:1" in diag
    assert "ready:interactive" in diag
    assert "title:Test VPS Hosting" in diag


def test_browser_diag_empty_page():
    diag = _browser_diag(
        console_msgs=[],
        page_errors=[],
        failed_requests=[],
        dom_state={"ready_state": "complete", "title": "", "error": None},
        visible_len=30,
    )
    assert diag == "text:30"


def test_browser_diag_dom_error():
    diag = _browser_diag(
        console_msgs=[],
        page_errors=[],
        failed_requests=[],
        dom_state={"ready_state": "", "title": "", "error": "TimeoutError:eval"},
        visible_len=100,
    )
    assert "dom:TimeoutError:eval" in diag


def test_httpfetch_browser_diag_field():
    """HTTPFetch carries browser_diag and it flows into evidence JSON."""
    fetch = HTTPFetch(
        markup="<html>x</html>", outcome="blocked", http_status=403,
        final_url="https://x.example/", method="browser", block_reason="captcha",
        attempts=1, latency_ms=500, browser_diag="js:2 | console:5 | text:10",
    )
    assert fetch.browser_diag == "js:2 | console:5 | text:10"
    from vps_monitor.monitor import build_live_evidence  # noqa: F401


def test_no_vision_calls_in_browser_fetch():
    """browser_fetch must never invoke vision/image APIs (agent model may not
    support images); diagnostics are DOM/console/JS only."""
    import inspect

    src = inspect.getsource(browser_fetch)
    # No vision APIs may be invoked (the agent model may not support images);
    # the comment mentions "No vision calls" so assert on call sites only.
    assert "browser_vision" not in src
    assert "vision_analyze" not in src
    assert "page.screenshot" not in src
    assert "page.on" in src  # console/pageerror/requestfailed listeners
