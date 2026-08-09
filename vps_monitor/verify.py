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


def verify_plan_tokens(
    target_url: str,
    plan_tokens: Iterable[str],
    *,
    timeout: int = 30,
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"
    ),
) -> tuple[bool, list[str]]:
    """Re-fetch the target page and confirm every plan token is present.

    Returns (ok, missing_tokens). Any fetch/parse failure is treated as
    "cannot confirm" (ok=False) so the runner never repairs on stale
    evidence.
    """
    tokens = [str(t) for t in plan_tokens if str(t)]
    if not target_url or not tokens:
        return False, list(tokens)
    try:
        response = requests.get(
            target_url,
            timeout=timeout,
            headers={"User-Agent": user_agent},
            allow_redirects=True,
            proxies=_proxies_from_env(),
        )
        page = response.text.casefold()
    except Exception:
        return False, list(tokens)
    missing = [t for t in tokens if t.casefold() not in page]
    return not missing, missing
