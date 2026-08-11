#!/usr/bin/env python3
"""Verify the public Pages payload through normal TLS against the built manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin


MAX_FILE_BYTES = 25 * 1024 * 1024


def compare_manifests(expected: dict, actual: dict) -> list[str]:
    # web/ 前端文件可由独立 pages-deploy workflow 更新（2026-08-11 用户
    # 要求：前端展示改动独立部署、不被爬取/merge 阻塞），其哈希在两次
    # monitor 构建之间允许与线上不一致；data/ 与核心字段必须严格一致。
    def files_minus_web(files):
        return {k: v for k, v in (files or {}).items() if not str(k).startswith("web/")}
    fields = [
        field
        for field in ("schema_version", "batch_id", "source_sha", "mode", "run_id", "run_attempt")
        if expected.get(field) != actual.get(field)
    ]
    if files_minus_web(expected.get("files")) != files_minus_web(actual.get("files")):
        fields.append("files(web-excluded)")
    return fields


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "pages-post-deploy-verify"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise OSError("public file exceeds size limit")
    return data


def verify(base_url: str, expected: dict) -> None:
    actual = json.loads(_get(urljoin(base_url.rstrip("/") + "/", "manifest.json")))
    mismatches = compare_manifests(expected, actual)
    if mismatches:
        raise ValueError(f"public manifest mismatch: {','.join(mismatches)}")
    for name, metadata in expected["files"].items():
        if str(name).startswith("web/"):
            # 前端文件由 pages-deploy workflow 独立验证哈希（不在此处比对）
            continue
        path = PurePosixPath(str(name))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe manifest path: {name}")
        digest = hashlib.sha256(_get(urljoin(base_url.rstrip("/") + "/", str(path)))).hexdigest()
        if digest != metadata["sha256"]:
            raise ValueError(f"public file hash mismatch: {name}")


def verify_prices(base_url: str, expected_prices: dict) -> None:
    """Data-semantic gate: the live prices.json must contain every expected
    task with matching amount/period/availability (currency may be EUR or USD
    — Contabo serves equivalent prices per visitor locale, same numbers).

    Snapshot values come from scripts/expected_prices.json; when a promo
    ends the amount changes and this gate fails on purpose, forcing a human
    to refresh the snapshot (2026-08-11 user rule: deployment only counts as
    success once the deployed data matches reality)."""
    payload = json.loads(_get(urljoin(base_url.rstrip("/") + "/", "data/prices.json")))
    by_task = {str(row.get("task_id") or row.get("id") or ""): row for row in payload if isinstance(row, dict)}
    expected_tasks = {tid: spec for tid, spec in expected_prices.items() if not tid.startswith("_")}
    missing = [tid for tid in expected_tasks if tid not in by_task]
    if missing:
        raise ValueError(f"expected prices missing tasks: {','.join(sorted(missing))}")
    mismatches = []
    for tid, expected in expected_tasks.items():
        row = by_task[tid]
        amount = row.get("amount")
        if amount is None or abs(float(amount) - float(expected["amount"])) > 0.01:
            mismatches.append(f"{tid}:amount={amount}!= {expected['amount']}")
        currency = row.get("currency")
        if currency not in expected["currency"]:
            mismatches.append(f"{tid}:currency={currency}")
        if row.get("billing_period") != expected["billing_period"]:
            mismatches.append(f"{tid}:period={row.get('billing_period')}")
        if row.get("availability") != expected["availability"]:
            mismatches.append(f"{tid}:avail={row.get('availability')}")
    if mismatches:
        raise ValueError("live prices mismatch: " + "; ".join(mismatches))
    print(f"PRICES_VERIFIED tasks={len(expected_prices)}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--expected-prices", default="")
    parser.add_argument("--attempts", type=int, default=6)
    args = parser.parse_args()
    expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    expected_prices = (
        json.loads(Path(args.expected_prices).read_text(encoding="utf-8"))
        if args.expected_prices
        else None
    )
    error: Exception | None = None
    for attempt in range(args.attempts):
        try:
            verify(args.base_url, expected)
            if expected_prices is not None:
                verify_prices(args.base_url, expected_prices)
            print(f"POST_DEPLOY_VERIFIED batch={expected['batch_id']} source_sha={expected['source_sha']}")
            return 0
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            error = exc
            if attempt + 1 < args.attempts:
                time.sleep(10)
    print(f"POST_DEPLOY_FAILED {type(error).__name__}: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
