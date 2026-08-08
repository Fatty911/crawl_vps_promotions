#!/usr/bin/env python3
"""Idempotently report vendor discovery candidates as a GitHub issue.

Creates/updates an issue titled '[vps-vendor-discovery] 新厂商候选' with the
current candidate list. A STATE_MARKER line records the run so re-runs update
the same issue instead of spamming new ones.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STATE_MARKER = "STATE_MARKER:"
ISSUE_TITLE = "[vps-vendor-discovery] 新厂商候选"


def _sh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=120)


def find_existing_issue() -> str | None:
    result = _sh(["gh", "issue", "list", "--repo", "Fatty911/crawl_vps_promotions",
                  "--state", "open", "--json", "number,title"])
    if result.returncode != 0:
        return None
    try:
        issues = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    for issue in issues:
        if issue.get("title") == ISSUE_TITLE:
            return str(issue["number"])
    return None


def build_body(payload: dict[str, Any], run_id: str) -> str:
    lines = [
        f"**厂商发现结果**（deals={payload['total_deals']}，候选={payload['candidate_count']}）",
        "",
        "| 候选厂商 | 来源 | 标题 |",
        "|---|---|---|",
    ]
    for candidate in payload["candidates"]:
        title = candidate["title"].replace("|", "\\|")[:80]
        link = candidate["link"]
        lines.append(
            f"| {candidate['vendor']} | {candidate['source']} | [{title}]({link}) |"
        )
    lines.extend(
        [
            "",
            "> 运行方式：`gh workflow run vps-vendor-discovery.yml -f auto_extend=true` "
            "对候选执行 AI 生成+URL 验证+双评审后自动合入 providers.yaml。",
            "",
            f"{STATE_MARKER} run_id={run_id} candidates={payload['candidate_count']}",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, default=ROOT / "state" / "vendor_candidates.json")
    parser.add_argument("--repo", default="Fatty911/crawl_vps_promotions")
    args = parser.parse_args()

    if not args.candidates.exists():
        print("[vendor-issue] no candidates file")
        return 0
    payload = json.loads(args.candidates.read_text(encoding="utf-8"))
    if not payload.get("candidates"):
        # Close the issue if it exists and now there are no candidates.
        existing = find_existing_issue()
        if existing:
            _sh(["gh", "issue", "close", existing, "--repo", args.repo,
                 "--comment", "本轮无新厂商候选，关闭。"])
            print(f"[vendor-issue] closed #{existing} (no candidates)")
        return 0

    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    body = build_body(payload, run_id)
    existing = find_existing_issue()
    if existing:
        result = _sh(["gh", "issue", "edit", existing, "--repo", args.repo,
                      "--body", body])
        print(f"[vendor-issue] updated #{existing}")
    else:
        result = _sh(["gh", "issue", "create", "--repo", args.repo,
                      "--title", ISSUE_TITLE, "--body", body])
        print(f"[vendor-issue] created {result.stdout.strip()}")
    if result.returncode != 0:
        print(f"[vendor-issue] gh failed: {result.stderr[:300]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
