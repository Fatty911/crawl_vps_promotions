"""Plan-token verification for P0b auto-repair (non trust-root module).

The self-repair runner must NOT issue network calls directly; this module is
the single place where the live page is re-fetched and checked. Keeping it
here (instead of inside the runner) lets tests and the delivery gate treat
network egress as a deliberate, audited capability.
"""

from __future__ import annotations

from typing import Iterable

import requests


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
        )
        page = response.text.casefold()
    except Exception:
        return False, list(tokens)
    missing = [t for t in tokens if t.casefold() not in page]
    return not missing, missing
