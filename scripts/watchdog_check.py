#!/usr/bin/env python3
"""VPS watchdog checks (P1).

- Stuck run detection: latest vps-monitor run in_progress whose updated_at
  has not advanced for > stuck_minutes -> cancel it + alert issue.
- Pages freshness: fetch online manifest; finished_at older than
  freshness_hours -> alert issue.
- Consecutive product_gate failures: last N runs all failure -> alert issue.

Read-only regarding the repo tree; only issues:write + actions:write
(cancel) are used. Alerts are deduped per fingerprint title.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ALERT_TITLE_PREFIX = "[vps-watchdog] "


def gh(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def open_alert(repo: str, fingerprint: str) -> str:
    result = gh(
        "api", "-X", "GET", "search/issues",
        "-f", f"q=repo:{repo} in:title {ALERT_TITLE_PREFIX!r} {fingerprint}",
        "-f", "per_page=10",
    )
    if result.returncode != 0:
        return ""
    try:
        items = json.loads(result.stdout).get("items", [])
    except json.JSONDecodeError:
        return ""
    for item in items:
        if item.get("pull_request"):
            continue
        if fingerprint in (item.get("title") or ""):
            return str(item["number"])
    return ""


def upsert_alert(repo: str, fingerprint: str, body: str) -> None:
    existing = open_alert(repo, fingerprint)
    title = f"{ALERT_TITLE_PREFIX}{fingerprint}"
    if existing:
        gh("issue", "comment", existing, "--repo", repo, "--body", body)
        print(f"alert updated: #{existing} {title}")
    else:
        result = gh("issue", "create", "--repo", repo, "--title", title, "--body", body)
        if result.returncode != 0:
            print(f"alert create failed: {result.stderr}", file=sys.stderr)
        else:
            print(f"alert created: {result.stdout.strip()} {title}")


def check_stuck(runs_path: Path, repo: str, stuck_minutes: int) -> None:
    try:
        runs = json.loads(runs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read runs: {exc}", file=sys.stderr)
        return
    now = dt.datetime.now(dt.UTC)
    for run in runs:
        if run.get("status") != "in_progress":
            continue
        updated = run.get("updated_at")
        if not updated:
            continue
        updated_dt = dt.datetime.fromisoformat(updated.replace("Z", "+00:00"))
        idle = (now - updated_dt).total_seconds() / 60
        if idle >= stuck_minutes:
            run_id = run["id"]
            # Cancel the stuck run (double condition: in_progress + stalled).
            gh("run", "cancel", str(run_id), "--repo", repo)
            body = (
                f"vps-monitor run {run_id} 已 in_progress 且 updated_at 停滞 "
                f"{idle:.0f} 分钟（阈值 {stuck_minutes}），已取消。"
                f"head_sha={run.get('head_sha', '')[:12]}"
            )
            upsert_alert(repo, f"stuck-run-{run_id}", body)
        else:
            print(f"run {run['id']} in_progress, idle {idle:.0f} min (< {stuck_minutes})")
        break  # only the latest run matters


def check_freshness(repo: str, freshness_hours: int, pages_url: str) -> None:
    try:
        with urllib.request.urlopen(pages_url, timeout=30) as resp:
            manifest = json.loads(resp.read())
    except Exception as exc:
        upsert_alert(repo, "pages-unreachable", f"线上 manifest 不可达: {exc}\nurl={pages_url}")
        return
    finished = manifest.get("finished_at") or manifest.get("started_at")
    if not finished:
        upsert_alert(repo, "pages-manifest-invalid", "线上 manifest 缺少 finished_at")
        return
    finished_dt = dt.datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
    age = (dt.datetime.now(dt.UTC) - finished_dt).total_seconds() / 3600
    if age > freshness_hours:
        upsert_alert(
            repo,
            f"pages-stale-{finished[:10]}",
            f"线上 Pages 数据已 {age:.1f} 小时未更新（阈值 {freshness_hours}h）。"
            f"batch_id={manifest.get('batch_id')} finished_at={finished}",
        )
    else:
        print(f"pages fresh: age {age:.1f}h batch={manifest.get('batch_id')}")


def check_consecutive_failures(runs_path: Path, repo: str, threshold: int) -> None:
    try:
        runs = json.loads(runs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    recent = [r for r in runs if r.get("status") == "completed"][:threshold]
    if len(recent) < threshold:
        return
    if all(r.get("conclusion") == "failure" for r in recent):
        run_ids = ",".join(str(r["id"]) for r in recent)
        upsert_alert(
            repo,
            "product-gate-3x-failure",
            f"vps-monitor 最近 {threshold} 次 run 全部 failure（{run_ids}）。"
            f"请人工核查站点或产品门配置。",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="VPS watchdog")
    parser.add_argument("--runs", type=Path)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--stuck-minutes", type=int, default=40)
    parser.add_argument("--freshness-hours", type=int, default=26)
    parser.add_argument("--consecutive-failures", type=int, default=3)
    parser.add_argument("--check-freshness", action="store_true",
                        help="only run the Pages freshness check")
    parser.add_argument(
        "--pages-url",
        default="https://fatty911.github.io/crawl_vps_promotions/manifest.json",
    )
    args = parser.parse_args()

    if args.check_freshness:
        check_freshness(args.repo, args.freshness_hours, args.pages_url)
        return 0
    if not args.runs:
        parser.error("--runs is required unless --check-freshness is set")
    check_stuck(args.runs, args.repo, args.stuck_minutes)
    check_consecutive_failures(args.runs, args.repo, args.consecutive_failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
