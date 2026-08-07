#!/usr/bin/env python3
"""Deterministic VPS monitor failure classifier (P0a).

Reads site/data/live-evidence.json and providers.yaml, classifies each task
into one of: ok / external_block / external_retired / site_breakage / unknown.

Pure rules, no LLM, no network, no writes. The classification drives:
- structured diagnosis issue content (vps-diagnosis.yml)
- the optional P0b auto-repair gate (only site_breakage may be repaired,
  and the repair runner re-fetches the page to confirm the plan token
  still exists before generating a patch).

Trust-root boundary: this script is NOT a trust root. It never modifies any
file. It only reads evidence and prints JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

# Outcomes that mean the target was reachable but the same card could not be
# bound to an offer. These are the only repair candidates (site changed).
BREAKAGE_REASONS = frozenset(
    {
        "no_exact_same_card_offer",
        "detail_unverified",
        "browser_error",
    }
)

# HTTP statuses / block reasons that are external risk-control or network
# problems. These must NEVER trigger auto-repair.
EXTERNAL_BLOCK_STATUSES = frozenset({403, 404, 429, 500, 502, 503, 504})
EXTERNAL_BLOCK_REASONS = frozenset(
    {
        "challenge_detected",
        "connection_error",
        "request_failed",
        "http_status",
        "provider_circuit_open",
    }
)

# Config-level conflicts: the page is there but the offer does not match the
# configured currency/period, or the URL redirected off-domain. Not a code
# bug; a human should review the target config or retire the task.
EXTERNAL_RETIRED_REASONS = frozenset(
    {
        "currency_or_period_conflict",
        "url_domain_mismatch",
        "multiple_matching_offers",
    }
)

OUTCOME_OK = frozenset({"success"})
OUTCOME_OUT_OF_STOCK = frozenset({"out_of_stock"})


def classify_task(task: dict[str, Any]) -> str:
    """Deterministic classification for a single live-evidence task row."""
    outcome = str(task.get("outcome") or "")
    reason = str(task.get("block_reason") or "")
    status = task.get("http_status")
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None

    if outcome in OUTCOME_OK:
        return "ok"
    if outcome in OUTCOME_OUT_OF_STOCK:
        return "ok"  # honest observation; not a failure to fix
    if reason in EXTERNAL_BLOCK_REASONS:
        return "external_block"
    if status_int in EXTERNAL_BLOCK_STATUSES:
        return "external_block"
    if reason in EXTERNAL_RETIRED_REASONS:
        return "external_retired"
    if reason in BREAKAGE_REASONS and status_int == 200:
        return "site_breakage"
    # browser_error with non-200, or anything unexpected: do not guess.
    return "unknown"


def load_plan_tokens(config_path: Path) -> dict[str, list[str]]:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return {
        str(row["id"]): [str(token) for token in row.get("plan_tokens", [])]
        for row in config.get("targets", [])
        if isinstance(row, dict) and row.get("id")
    }


def build_report(
    evidence: dict[str, Any],
    plan_tokens: dict[str, list[str]],
) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        "ok": 0,
        "external_block": 0,
        "external_retired": 0,
        "site_breakage": 0,
        "unknown": 0,
    }
    for row in evidence.get("tasks", []):
        task_id = str(row.get("task_id") or "")
        classification = classify_task(row)
        counts[classification] = counts.get(classification, 0) + 1
        suggestion = ""
        if classification == "external_block":
            suggestion = "外部风控/网络问题，禁止自动修复；请人工核实站点可达性与反爬状态。"
        elif classification == "external_retired":
            suggestion = "页面可达但内容与配置冲突（币种/账期/跳转），请人工核查 providers.yaml 配置或考虑退休该任务。"
        elif classification == "site_breakage":
            tokens = plan_tokens.get(task_id, [])
            suggestion = (
                "页面可访问但同卡 offer 解析失败，疑似站点改版；可进入 P0b 自动修复候选"
                "（修复前将重新抓取页面确认 plan_token 存在）。"
            )
        elif classification == "unknown":
            suggestion = "无法确定失败类别，请人工查看该任务最近 evidence。"
        else:
            suggestion = "任务正常或已售罄，无需处理。"
        tasks.append(
            {
                "task_id": task_id,
                "provider": str(row.get("provider") or ""),
                "classification": classification,
                "plan_tokens": list(plan_tokens.get(task_id, [])),
                "target_url": str(row.get("final_url") or ""),
                "evidence": {
                    "outcome": str(row.get("outcome") or ""),
                    "http_status": row.get("http_status"),
                    "block_reason": str(row.get("block_reason") or ""),
                    "attempts": row.get("attempts"),
                    "latency_ms": row.get("latency_ms"),
                    "method": str(row.get("method") or ""),
                },
                "suggestion": suggestion,
            }
        )
    summary = evidence.get("summary", {})
    return {
        "schema_version": 1,
        "source": {
            "run_id": summary.get("run_id")
            or evidence.get("run_id")
            or "",
            "mode": summary.get("mode") or evidence.get("mode") or "",
        },
        "task_count": len(tasks),
        "counts": counts,
        "tasks": tasks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic VPS failure classifier")
    parser.add_argument("--evidence", type=Path, required=True, help="site/data/live-evidence.json")
    parser.add_argument("--config", type=Path, required=True, help="providers.yaml")
    parser.add_argument("--out", type=Path, help="optional JSON output path (default stdout)")
    args = parser.parse_args()

    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": f"cannot read evidence: {exc}"}, ensure_ascii=False))
        return 1
    try:
        plan_tokens = load_plan_tokens(args.config)
    except (OSError, yaml.YAMLError) as exc:
        print(json.dumps({"error": f"cannot read config: {exc}"}, ensure_ascii=False))
        return 1

    report = build_report(evidence, plan_tokens)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
