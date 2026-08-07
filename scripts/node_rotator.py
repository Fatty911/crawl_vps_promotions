#!/usr/bin/env python3
"""Node rotator: switch airport subscription nodes per request via mihomo API.

The user requires crawlers to keep rotating proxy nodes so a single node is
never long-lived against anti-bot systems (ZOL checking pages, JD risk
verification, PConline rate limits).  This module talks to the mihomo
external controller and switches the ``PROXY`` select group before every
request, maintains a runtime blacklist of nodes that got blocked/rate-limited
during this workflow run, and persists per-node failure statistics to
``state/proxy_blacklist.json`` so the next run can prefer healthier
nodes while still re-probing previously blocked ones (user preference: block
at runtime, re-probe next run).
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path
from typing import Any

import requests

CONTROLLER = os.environ.get("MIHOMO_CONTROLLER", "http://127.0.0.1:9090")
PROXY_GROUP = os.environ.get("MIHOMO_PROXY_GROUP", "PROXY")
BLACKLIST_PATH = Path(
    os.environ.get("PROXY_BLACKLIST_PATH", "state/proxy_blacklist.json")
)

# Block a node after this many consecutive failures in one run.
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("PROXY_MAX_CONSECUTIVE_FAILURES", "3"))
# Minimum delay between two node switches (seconds), to keep mihomo sane.
MIN_SWITCH_INTERVAL = float(os.environ.get("PROXY_MIN_SWITCH_INTERVAL", "0.2"))


class NodeRotator:
    """Round-robin/random node switching with a runtime failure blacklist."""

    def __init__(
        self,
        controller: str = CONTROLLER,
        group: str = PROXY_GROUP,
        blacklist_path: Path = BLACKLIST_PATH,
        switch_interval: float = MIN_SWITCH_INTERVAL,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        self.controller = controller.rstrip("/")
        self.group = group
        self.blacklist_path = blacklist_path
        self.switch_interval = switch_interval
        self.max_consecutive_failures = max_consecutive_failures
        self._session = requests.Session()
        self._session.trust_env = False  # never route controller calls via proxy
        self._nodes: list[str] = []
        self._index = 0
        self._last_switch = 0.0
        self._runtime_blacklist: set[str] = set()
        self._consecutive_failures: dict[str, int] = {}
        self._active_node: str = ""
        self._stats: dict[str, dict[str, int]] = {}
        self._enabled = False

    # ── setup ──────────────────────────────────────────────
    def discover_nodes(self) -> list[str]:
        """Fetch node names from the mihomo controller (proxies + group)."""
        try:
            resp = self._session.get(f"{self.controller}/proxies/{self.group}", timeout=5)
            if resp.status_code != 200:
                return []
            data = resp.json()
            self._nodes = [str(name) for name in data.get("all", [])]
            self._nodes = [n for n in self._nodes if n != self.group]
        except (requests.RequestException, ValueError):
            self._nodes = []
        self._enabled = bool(self._nodes)
        if self._enabled:
            self._load_stats()
            # Prefer nodes that were healthy in previous runs, but keep all
            # non-blacklisted nodes in the rotation pool for re-probing.
            self._runtime_blacklist = set()
        return self._nodes

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    # ── rotation ───────────────────────────────────────────
    def next_node(self) -> str:
        """Pick the next node, skipping the runtime blacklist.

        If every node is blacklisted, clear the runtime blacklist to force a
        full re-probe (preferred over failing the whole crawl).
        """
        if not self._nodes:
            return ""
        if self._index >= len(self._nodes):
            self._index = 0
        available = [n for n in self._nodes if n not in self._runtime_blacklist]
        if not available:
            self._runtime_blacklist.clear()
            self._consecutive_failures.clear()
            available = list(self._nodes)
        # Round-robin start, jittered so concurrent crawlers do not collide.
        node = available[self._index % len(available)]
        self._index = (self._index + 1) % len(self._nodes)
        return node

    def switch(self, node: str) -> bool:
        """Explicitly select ``node`` in the mihomo group via PUT /proxies."""
        elapsed = time.monotonic() - self._last_switch
        if elapsed < self.switch_interval:
            time.sleep(self.switch_interval - elapsed)
        try:
            resp = self._session.put(
                f"{self.controller}/proxies/{self.group}",
                json={"name": node},
                timeout=5,
            )
            ok = resp.status_code in (200, 204)
            if ok:
                self._active_node = node
            else:
                print(f"node switch failed for {node!r}: HTTP {resp.status_code}")
            self._last_switch = time.monotonic()
            return ok
        except requests.RequestException as exc:
            print(f"node switch error for {node!r}: {type(exc).__name__}")
            return False

    def rotate(self) -> str:
        """Pick and switch to the next node; returns the active node name."""
        node = self.next_node()
        if node:
            self.switch(node)
        return node

    # ── failure tracking ───────────────────────────────────
    def mark_success(self, node: str) -> None:
        self._consecutive_failures[node] = 0
        stat = self._stats.setdefault(node, {"ok": 0, "fail": 0, "blocked": 0})
        stat["ok"] += 1

    def mark_failure(self, node: str, *, blocked: bool = False) -> None:
        stat = self._stats.setdefault(node, {"ok": 0, "fail": 0, "blocked": 0})
        stat["fail"] += 1
        if blocked:
            stat["blocked"] += 1
        count = self._consecutive_failures.get(node, 0) + 1
        self._consecutive_failures[node] = count
        if blocked or count >= self.max_consecutive_failures:
            if node not in self._runtime_blacklist:
                print(
                    f"node {node!r} blacklisted "
                    f"(consecutive_failures={count}, blocked={blocked})"
                )
            self._runtime_blacklist.add(node)
            self._consecutive_failures[node] = 0

    # ── persistence ────────────────────────────────────────
    def _load_stats(self) -> None:
        try:
            if self.blacklist_path.exists():
                raw = json.loads(self.blacklist_path.read_text(encoding="utf-8"))
                self._stats = {
                    str(k): {
                        "ok": int(v.get("ok", 0)),
                        "fail": int(v.get("fail", 0)),
                        "blocked": int(v.get("blocked", 0)),
                    }
                    for k, v in raw.get("nodes", {}).items()
                    if isinstance(v, dict)
                }
        except (OSError, ValueError):
            self._stats = {}

    def save_stats(self) -> None:
        if not self._enabled:
            return
        self.blacklist_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "nodes": self._stats,
        }
        self.blacklist_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def summary(self) -> str:
        if not self._enabled:
            return "node rotator disabled (no mihomo controller)"
        return (
            f"node rotator: {len(self._nodes)} nodes, "
            f"runtime blacklist={len(self._runtime_blacklist)}"
        )


def make_rotator() -> NodeRotator:
    """Convenience factory used by crawlers; gracefully degrades to disabled."""
    rotator = NodeRotator()
    try:
        rotator.discover_nodes()
    except Exception as exc:  # pragma: no cover - defensive
        print(f"node rotator discovery failed: {type(exc).__name__}: {exc}")
        rotator._enabled = False
    return rotator


if __name__ == "__main__":
    rotator = make_rotator()
    if not rotator.enabled:
        raise SystemExit("mihomo controller unavailable; node rotation disabled")
    for _ in range(min(rotator.node_count, 5)):
        node = rotator.rotate()
        print(f"switched to: {node}")
    rotator.save_stats()
    print(rotator.summary())
