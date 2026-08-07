#!/usr/bin/env python3
"""Create or update the structured VPS diagnosis issue (P0a).

Idempotent: one issue per (run_id). If an open issue with the same run_id
already exists, only a comment is appended. No LLM, no writes besides the
issue itself via the GitHub CLI.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

LABEL = "monitor-diagnosis"
STATE_MARKER = "<!-- vps-diagnosis-state"


def gh(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def find_existing_issue(repo: str, run_id: str) -> str:
    needle = f"[vps-diagnosis] run {run_id}"
    result = gh(
        "api",
        "-X",
        "GET",
        "search/issues",
        "-f",
        f"q=repo:{repo} in:title {needle!r}",
        "-f",
        "per_page=10",
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
        if needle in (item.get("title") or ""):
            return str(item["number"])
    return ""


def build_body(report: dict, run_id: str, sha: str) -> str:
    counts = report.get("counts", {})
    lines = [
        f"**VPS monitor run {run_id} 失败诊断**（head sha `{sha}`）",
        "",
        "确定性规则分类（无 LLM）：",
        "",
        f"- ok: {counts.get('ok', 0)}",
        f"- external_block（外部风控/网络，禁止自动修复）: {counts.get('external_block', 0)}",
        f"- external_retired（配置冲突/跳转，需人工核查）: {counts.get('external_retired', 0)}",
        f"- site_breakage（疑似站点改版，可自动修复候选）: {counts.get('site_breakage', 0)}",
        f"- unknown: {counts.get('unknown', 0)}",
        "",
        "| task_id | 分类 | 证据 | 建议 |",
        "|---|---|---|---|",
    ]
    for task in report.get("tasks", []):
        ev = task.get("evidence", {})
        evidence = (
            f"{ev.get('outcome')} http={ev.get('http_status')} "
            f"reason={ev.get('block_reason') or '-'}"
        )
        lines.append(
            f"| {task['task_id']} | {task['classification']} | {evidence} | "
            f"{task.get('suggestion', '')} |"
        )
    lines.append("")
    lines.append(STATE_MARKER + f" run_id={run_id} -->")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Write VPS diagnosis issue")
    parser.add_argument("--classification", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--label", default=LABEL)
    args = parser.parse_args()

    try:
        report = json.loads(args.classification.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read classification: {exc}", file=sys.stderr)
        return 1

    body = build_body(report, args.run_id, args.sha)
    existing = find_existing_issue(args.repo, args.run_id)
    if existing:
        # Overwrite the body (not just append) so the STATE_MARKER and the
        # classification table always reflect the latest run state.
        result = gh("issue", "edit", existing, "--repo", args.repo, "--body", body)
        if result.returncode != 0:
            print(f"edit failed: {result.stderr}", file=sys.stderr)
            return 1
        print(f"updated existing issue #{existing} (body replaced)")
        return 0

    result = gh(
        "issue", "create", "--repo", args.repo,
        "--title", args.title, "--body", body,
        "--label", args.label,
    )
    if result.returncode == 0 and result.stdout.strip():
        print(f"created issue: {result.stdout.strip()}")
        return 0
    # Label may fail on missing metadata; fall back to unlabelled issue.
    result = gh(
        "issue", "create", "--repo", args.repo,
        "--title", args.title, "--body", body,
    )
    if result.returncode != 0:
        print(f"create failed: {result.stderr}", file=sys.stderr)
        return 1
    print(f"created issue (unlabelled): {result.stdout.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
