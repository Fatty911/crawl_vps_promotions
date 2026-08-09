"""Plan-token verification for P0b auto-repair (non trust-root module).

The self-repair runner must NOT issue network calls directly; this module is
the single place where the live page is re-fetched and checked. Keeping it
here (instead of inside the runner) lets tests and the delivery gate treat
network egress as a deliberate, audited capability.
"""

from __future__ import annotations

import os
from typing import Iterable

import requests
from bs4 import BeautifulSoup

# Challenge pages (Cloudflare "Just a moment...", captchas) that requests can
# fetch but whose visible text never contains the real product tokens. When
# these appear, the verify falls back to a headless-browser render (same
# capability as the monitor's browser_fetch) before declaring "retired".
_CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "captcha",
    "验证码",
    "安全验证",
    "access denied",
    "cloudflare ray id",
)

# Lazy playwright import (heavy); None when unavailable.
try:
    from playwright.sync_api import sync_playwright as _sync_playwright
except Exception:  # pragma: no cover - env without playwright
    _sync_playwright = None


def _proxies_from_env() -> dict[str, str] | None:
    """Honour the mihomo proxy env (same channel as the monitor's requests
    path). Without it, verify would fetch from the raw runner IP and get
    anti-bot challenge pages — misclassifying healthy tasks as retired
    (observed 2026-08-09: BuyVM SLICE tokens were present but verify said
    NOT confirmed)."""
    proxy_url = os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or ""
    if not proxy_url:
        return None
    https_proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or proxy_url
    return {"http": proxy_url, "https": https_proxy}


def _visible_text(markup: str) -> str:
    return BeautifulSoup(markup, "html.parser").get_text(" ", strip=True).casefold()


def _looks_like_challenge(markup: str) -> bool:
    lowered = markup.casefold()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def _browser_render(target_url: str, *, timeout: int = 30000) -> str | None:
    """Render the page with a headless browser (same capability as the
    monitor's browser_fetch) so JS-rendered / Cloudflare-protected stores can
    be verified. Returns visible text, or None on any failure."""
    if _sync_playwright is None:
        return None
    proxy_url = os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or ""
    launch_kwargs: dict[str, object] = {}
    if proxy_url and "127.0.0.1" in proxy_url:
        launch_kwargs["proxy"] = {"server": proxy_url}
    try:
        with _sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, **launch_kwargs)
            try:
                page = browser.new_page(locale="zh-CN")
                page.goto(target_url, wait_until="domcontentloaded", timeout=timeout)
                markup = page.content()
                return _visible_text(markup)
            finally:
                browser.close()
    except Exception:
        return None


def verify_plan_tokens(
    target_url: str,
    plan_tokens: Iterable[str],
    *,
    timeout: int = 30,
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"
    ),
    browser_timeout: int = 30000,
) -> tuple[bool, list[str]]:
    """Re-fetch the target page and confirm every plan token is present.

    Strategy (each level mirrors the monitor's own fetch chain):
      1. plain requests (with proxy env) -> visible-text match;
      2. if the page looks like a challenge (Cloudflare/captcha) OR tokens
         are still missing, render with a headless browser and re-match;
      3. any failure at every level -> "cannot confirm" (ok=False) so the
         runner never repairs on stale evidence.

    Returns (ok, missing_tokens).
    """
    tokens = [str(t) for t in plan_tokens if str(t)]
    if not target_url or not tokens:
        return False, list(tokens)

    page: str | None = None
    try:
        response = requests.get(
            target_url,
            timeout=timeout,
            headers={"User-Agent": user_agent},
            allow_redirects=True,
            proxies=_proxies_from_env(),
        )
        page = _visible_text(response.text)
    except Exception:
        page = None

    missing = [t for t in tokens if t.casefold() not in (page or "")]
    if not missing:
        return True, []

    # Requests-level check was inconclusive (challenge page or JS-rendered
    # store). Fall back to a real browser render before declaring retired.
    rendered = _browser_render(target_url, timeout=browser_timeout)
    if rendered:
        missing = [t for t in tokens if t.casefold() not in rendered]
        if not missing:
            return True, []

    # If the requests fetch was a challenge page but the browser render also
    # failed to confirm, we cannot distinguish "retired" from "blocked":
    # report NOT confirmed (safe: never repair on unconfirmed evidence).
    return False, missing
