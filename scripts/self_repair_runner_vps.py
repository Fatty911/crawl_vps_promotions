#!/usr/bin/env python3
"""VPS-specific self-repair runner (P0b, opt-in).

Trust-root whitelist (NEVER auto-modified; mirrored from AGENTS.md):
  AGENTS.md, .githooks/, scripts/codex_delivery_gate.py, workflows/*.yml,
  providers.yaml, requirements.txt, vps_monitor/contracts.py,
  vps_monitor/audit.py, vps_monitor/state.py, docs/, web/, tests/.
Auto-repair may ONLY touch vps_monitor/monitor.py (ALLOWED_PATCH_PATHS);
any patch touching another path is aborted before validation.

Only `site_breakage` tasks (page reachable, same-card offer parse failed,
plan token still present) may be repaired. The runner:

  1. Re-fetches the target page through vps_monitor.verify (the single
     audited network egress) and confirms every plan token still exists;
     external_retired pages are never repaired.
  2. Asks the OpenCode Agent (kimi-coding-plan/k3, read-only) for a JSON
     {patch, reasoning, confidence} fix.
  3. Applies the patch in a throwaway worktree, validates with
     pytest + compileall + git diff --check + schema v4 (contracts API).
  4. Two review families (NIM z-ai/glm-5.2 + minimaxai/minimax-m3) review
     the exact worktree diff (429/5xx/unstructured => FAIL, never PASS);
     Reviewed-Diff-SHA256 binds the exact reviewed diff string.
  5. Commits on main with trailers only when both reviews PASS.

Safety mirrors crawl_laptops' self_repair_runner but with VPS-specific
trust-root boundaries and the validated endpoints.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

REVIEW_TRAILER_FAMILY_1 = "Review-Model-Family-1"
REVIEW_TRAILER_FAMILY_2 = "Review-Model-Family-2"
REVIEW_TRAILER_RESULT_1 = "Review-Result-1"
REVIEW_TRAILER_RESULT_2 = "Review-Result-2"
REVIEW_TRAILER_DIFF = "Reviewed-Diff-SHA256"

# Two different families, verified available on NIM (2026-08-07):
# z-ai/glm-5.2 (Zhipu) and minimaxai/minimax-m3 (MiniMax).
# Provider names/model IDs MUST match the global opencode config exactly:
# opencode 1.18.x only lets config injection override already-declared
# providers/models (verified 2026-08-08 on opencode 1.18.15). The global
# opencode.json declares nvidia models as nvidia-glm-5.2 / nvidia-minimax-m3.
REVIEW_PROVIDERS = [
    {
        "name": "nvidia",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "env_key": "NVIDIA_NIM_API_KEY",
        "model": "nvidia-glm-5.2",
    },
    {
        "name": "nvidia",
        "base_url": "https://integrate.api.nvidia.com/v1",
        "env_key": "NVIDIA_NIM_API_KEY",
        "model": "nvidia-minimax-m3",
    },
]

# Repair agent provider (fix generation only; key consumed by OpenCode CLI).
FIX_PROVIDER = {
    "name": "kimi-coding-plan",
    "base_url": "https://api.kimi.com/coding/v1",
    "env_key": "KIMI_CODINGPLAN_API_KEY",
    "model": "k3",
}

# Trust-root whitelist: ONLY this file may be changed by auto-repair.
ALLOWED_PATCH_PATHS = frozenset({"vps_monitor/monitor.py"})
MAX_DELETED_LINES = 50


def _sh(args: list[str], cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd or ROOT), capture_output=True, text=True, timeout=timeout)


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return _sh(["git", *args], cwd=cwd)


def patch_paths(patch: str) -> list[str]:
    paths = re.findall(r"^diff --git a/(\S+) b/\S+", patch, flags=re.M)
    return [p for p in paths if p]


def build_fix_prompt(task: dict, log_excerpt: str) -> str:
    return f"""你是资深 VPS 优惠监控修复工程师。Fatty911/crawl_vps_promotions 仓库中任务解析失败。

## 失败任务（live-evidence）
```json
{json.dumps(task, ensure_ascii=False, indent=2)[:4000]}
```

## 失败日志摘录
```text
{log_excerpt[:8000]}
```

## 任务
分析根因，输出一个**最小、精确**的修复补丁。约束：
- 只允许修改 `vps_monitor/monitor.py`（解析逻辑），禁止触碰其它任何文件
- 禁止修改 providers.yaml、workflows、AGENTS.md、requirements、contracts、audit、state、docs、web、tests
- 只输出统一 diff（git apply 可应用），禁止直接写文件内容
- 不允许删除超过 {MAX_DELETED_LINES} 行
- 不确定的修复不要输出（宁可不修，不要引入幻觉）

## 输出格式（严格 JSON，不要 markdown 代码块）
{{"patch": "<unified diff 文本>", "reasoning": "<简述>", "confidence": 0.0-1.0}}
confidence < 0.7 时 patch 必须为空字符串。
"""


def build_review_prompt(diff: str, run_id: str, task_id: str) -> str:
    return f"""审查 Fatty911/crawl_vps_promotions 仓库的自修复补丁（run: {run_id}, task: {task_id}）。

## 补丁（统一 diff）
```diff
{diff[:12000]}
```

## 审查要点
1. 是否最小改动、只改 vps_monitor/monitor.py、不触碰信任根（AGENTS.md/.githooks/gate/workflows/providers.yaml/requirements/contracts/audit/state/docs/web/tests）
2. 是否引入新 bug 或删除过多代码
3. 修复是否与失败根因匹配（同卡 offer 解析失败）

## 输出（严格 JSON）
{{"verdict": "PASS" 或 "FAIL", "reason": "<一句话理由>"}}
"""


def call_opencode(provider: dict, prompt: str, max_tokens: int = 4000) -> str | None:
    """调用 OpenCode CLI（Agent 工具），禁止脚本直连 LLM API。"""
    key = os.environ.get(provider["env_key"], "")
    if not key:
        print(f"[vps-repair] missing {provider['env_key']}", file=sys.stderr)
        return None
    base_url = provider["base_url"].rstrip("/")
    read_only = {
        "*": "deny",
        "read": "allow",
        "edit": "deny",
        "bash": "deny",
        "webfetch": "deny",
        "task": "deny",
        "question": "deny",
        "external_directory": "deny",
    }
    config = {
        "provider": {
            provider["name"]: {
                "npm": "@ai-sdk/openai-compatible",
                "name": provider["name"],
                "options": {
                    "baseURL": base_url,
                    "apiKey": f"{{env:{provider['env_key']}}}",
                },
                "models": {provider["model"]: {"limit": {"context": 131072, "output": max(1024, int(max_tokens))}}},
            }
        },
        "agent": {"plan": {"permission": read_only}},
        "permission": read_only,
    }
    env = dict(os.environ)
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    env["OPENCODE_DISABLE_TELEMETRY"] = "1"
    opencode_bin = os.environ.get("OPENCODE_BIN", "opencode")
    with tempfile.TemporaryDirectory(prefix="vps-repair-") as tmpdir:
        # Write the provider config as a PROJECT-level opencode.json inside
        # --dir (verified 2026-08-08 on opencode 1.18.15: OPENCODE_CONFIG_CONTENT
        # env injection returns 404; a project opencode.json registers the
        # provider on a fresh runner).
        (Path(tmpdir) / "opencode.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )
        # Pass the prompt as a positional message (subprocess list-args, no
        # shell quoting issues). --file is an attachment, not a message source.
        cmd = [
            opencode_bin, "run", "--pure", "--agent", "plan",
            "--model", f"{provider['name']}/{provider['model']}",
            "--format", "default",
            "--dir", tmpdir,
            "Answer the attached prompt directly. Do not call tools or modify files. "
            "Return only the requested JSON.\n\n" + prompt,
        ]
        try:
            completed = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
        except Exception as exc:
            print(f"[vps-repair] opencode failed: {type(exc).__name__} {exc}", file=sys.stderr)
            return None
        if completed.returncode != 0:
            print(f"[vps-repair] opencode exit {completed.returncode}: {(completed.stderr or '')[:300]}", file=sys.stderr)
            return None
        return (completed.stdout or "").strip() or None


def parse_fix_response(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"patch": "", "reasoning": "unparseable", "confidence": 0.0}
    try:
        confidence = float(data.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "patch": str(data.get("patch", "") or ""),
        "reasoning": str(data.get("reasoning", "")),
        "confidence": confidence,
    }


def parse_review_response(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"verdict": "FAIL", "reason": "unparseable review"}
    return {
        "verdict": str(data.get("verdict", "FAIL")).upper(),
        "reason": str(data.get("reason", "")),
    }


def count_deleted_lines(patch: str) -> int:
    deleted = 0
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if in_hunk and line.startswith("-") and not line.startswith("---"):
            deleted += 1
    return deleted


def run_validation(worktree: Path) -> tuple[bool, str]:
    """VPS validation chain: pytest -> compileall -> git diff --check -> schema v4."""
    checks = [
        (["python", "-m", "pytest", "tests/", "-q", "-x"], "pytest"),
        (["python", "-m", "compileall", "-q", "vps_monitor", "scripts"], "compileall"),
        (["git", "diff", "--check"], "diff-check"),
    ]
    for cmd, label in checks:
        result = _sh(cmd, cwd=worktree, timeout=900)
        if result.returncode != 0:
            print(f"[vps-repair] {label} failed:\n{(result.stdout + result.stderr)[-1500:]}", file=sys.stderr)
            return False, label
        print(f"[vps-repair] {label} OK")
    # Schema v4 consistency: validate a minimal-but-complete envelope built
    # exclusively through the public contracts API, so an auto-repair cannot
    # silently break the public contract. No monitor internals are imported.
    check = (
        "from vps_monitor.contracts import build_envelope, validate_envelope; "
        "statuses = [{'task_id': 't-%d' % i, 'outcome': 'error', 'attempts': 1, "
        "'started_at': '2000-01-01T00:00:00+00:00', 'finished_at': '2000-01-01T00:00:01+00:00', "
        "'source_url': 'https://example.com', 'final_url': 'https://example.com', "
        "'rejection_reason': 'offline_smoke_no_network', 'evidence_hash': '0'*64, "
        "'parser_version': 'test'} for i in range(14)]; "
        "env = build_envelope(repo='crawl_vps_promotions', run_id='validate', run_attempt='0', "
        "source_sha='0'*40, config_sha256='0'*64, started_at='2000-01-01T00:00:00+00:00', "
        "finished_at='2000-01-01T00:00:01+00:00', mode='fixture', baseline_batch_id=None, "
        "expected_tasks=14, statuses=statuses, prices=[], evidence_sha256='0'*64, audit_status='pass'); "
        "validate_envelope(env, ['t-%d' % i for i in range(14)]); "
        "assert env['schema_version'] == 4; "
        "print('schema-v4-ok')"
    )
    result = _sh(["python", "-c", check], cwd=worktree, timeout=900)
    if result.returncode != 0:
        print(f"[vps-repair] schema-v4 check failed:\n{(result.stdout + result.stderr)[-1500:]}", file=sys.stderr)
        return False, "schema-v4"
    print("[vps-repair] schema-v4 OK")
    return True, "all"


def diff_sha256(worktree: Path) -> str:
    result = _git(["diff", "HEAD"], cwd=worktree)
    return hashlib.sha256(result.stdout.encode("utf-8")).hexdigest()


def review_diff(diff: str, run_id: str, task_id: str) -> tuple[list[dict], str]:
    reviews = []
    prompt = build_review_prompt(diff, run_id, task_id)
    for provider in REVIEW_PROVIDERS:
        verdict_record = None
        # Retry up to 3 times: 429/5xx/transient failures must NOT become PASS.
        for attempt in range(1, 4):
            content = call_opencode(provider, prompt, max_tokens=1000)
            if not content:
                print(f"[vps-repair] review {provider['name']} attempt {attempt}: call failed", file=sys.stderr)
                time.sleep(20 * attempt)
                continue
            parsed = parse_review_response(content)
            verdict_record = {"provider": provider["name"], "model": provider["model"], **parsed}
            print(f"[vps-repair] review {provider['name']}/{provider['model']}: {parsed}")
            break
        if verdict_record is None:
            verdict_record = {"provider": provider["name"], "model": provider["model"],
                              "verdict": "FAIL", "reason": "review call failed after retries"}
        reviews.append(verdict_record)
    # The Reviewed-Diff-SHA256 must bind the EXACT diff string the reviewers
    # saw (the worktree diff), never ROOT's working tree (which is untouched).
    diff_sha = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    return reviews, diff_sha


def commit_with_trailers(worktree: Path, diff_sha: str, reviews: list[dict], message: str) -> bool:
    trailers = [
        f"{REVIEW_TRAILER_FAMILY_1}: {reviews[0]['provider']}/{reviews[0]['model']}",
        f"{REVIEW_TRAILER_RESULT_1}: {reviews[0]['verdict']}",
        f"{REVIEW_TRAILER_FAMILY_2}: {reviews[1]['provider']}/{reviews[1]['model']}",
        f"{REVIEW_TRAILER_RESULT_2}: {reviews[1]['verdict']}",
        f"{REVIEW_TRAILER_DIFF}: {diff_sha}",
    ]
    commit_msg = f"{message}\n\n" + "\n".join(trailers)
    if _git(["add", "-A"], cwd=worktree).returncode != 0:
        return False
    result = _git(["commit", "-m", commit_msg], cwd=worktree)
    if result.returncode != 0:
        print(f"[vps-repair] commit failed:\n{result.stderr[:1000]}", file=sys.stderr)
        return False
    return True


def push_main(worktree: Path) -> bool:
    remote = os.environ.get("REMOTE_URL", "")
    if not remote:
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        remote = f"https://x-access-token:{token}@github.com/{repo}.git"
    result = _git(["push", remote, "HEAD:main"], cwd=worktree, timeout=180)
    if result.returncode != 0:
        print(f"[vps-repair] push failed:\n{result.stderr[:1500]}", file=sys.stderr)
        return False
    return True


def redispatch(run_id: str) -> bool:
    result = _sh(["gh", "workflow", "run", "vps-monitor.yml",
                  "--repo", os.environ.get("GITHUB_REPOSITORY", "")], timeout=120)
    if result.returncode != 0:
        print(f"[vps-repair] redispatch failed: {result.stderr[:1000]}", file=sys.stderr)
        return False
    print(f"[vps-repair] redispatched vps-monitor (diagnosed run {run_id})")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="VPS auto-repair (opt-in, P0b)")
    parser.add_argument("--classification", type=Path, required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--log-excerpt", default="")
    parser.add_argument("--attempt-marker", default="")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the proposed patch and validation plan without applying/pushing")
    args = parser.parse_args()

    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        print("[vps-repair] no GH_TOKEN", file=sys.stderr)
        return 2
    if not os.environ.get("KIMI_CODINGPLAN_API_KEY"):
        print("[vps-repair] KIMI_CODINGPLAN_API_KEY missing; P0b disabled", file=sys.stderr)
        return 3
    if not os.environ.get("NVIDIA_NIM_API_KEY"):
        print("[vps-repair] NVIDIA_NIM_API_KEY missing; P0b disabled", file=sys.stderr)
        return 3

    try:
        report = json.loads(args.classification.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[vps-repair] cannot read classification: {exc}", file=sys.stderr)
        return 3
    task = next((t for t in report.get("tasks", []) if t.get("task_id") == args.task_id), None)
    if not task:
        print(f"[vps-repair] task {args.task_id} not in classification", file=sys.stderr)
        return 3
    if task.get("classification") != "site_breakage":
        print(f"[vps-repair] task {args.task_id} classified {task.get('classification')}; not repairable", file=sys.stderr)
        return 3

    # Hard gate: re-fetch the target page now and confirm every configured
    # plan token still appears in the rendered HTML (via vps_monitor.verify,
    # the single audited network egress point). If the page no longer
    # contains the product, this is external_retired, not a repair candidate.
    target_url = str(task.get("target_url") or "")
    plan_tokens = [str(t) for t in task.get("plan_tokens") or []]
    if not target_url or not plan_tokens:
        print("[vps-repair] task lacks target_url/plan_tokens; cannot confirm; skipping", file=sys.stderr)
        return 3
    try:
        from vps_monitor.verify import verify_plan_tokens
    except ImportError as exc:
        print(f"[vps-repair] cannot import vps_monitor.verify: {exc}; skipping", file=sys.stderr)
        return 3
    confirmed, missing = verify_plan_tokens(target_url, plan_tokens)
    if not confirmed:
        print(f"[vps-repair] plan tokens NOT confirmed on live page: {missing}; external_retired, skipping", file=sys.stderr)
        return 3
    print(f"[vps-repair] all plan tokens confirmed on live page ({len(plan_tokens)})")

    fix_prompt = build_fix_prompt(task, args.log_excerpt)
    content = call_opencode(FIX_PROVIDER, fix_prompt, max_tokens=4000)
    if not content:
        print("[vps-repair] fix agent returned nothing", file=sys.stderr)
        return 3
    fix = parse_fix_response(content)
    print(f"[vps-repair] confidence={fix['confidence']} reasoning={fix['reasoning'][:200]}")
    if fix["confidence"] < 0.7 or not fix["patch"].strip():
        print("[vps-repair] low confidence or empty patch; skipping", file=sys.stderr)
        return 3
    if count_deleted_lines(fix["patch"]) > MAX_DELETED_LINES:
        print("[vps-repair] deletion guard triggered", file=sys.stderr)
        return 3
    if not set(patch_paths(fix["patch"])).issubset(ALLOWED_PATCH_PATHS):
        print(f"[vps-repair] patch touches non-allowed paths: {patch_paths(fix['patch'])}", file=sys.stderr)
        return 3

    if args.dry_run:
        print("[vps-repair] DRY-RUN: would apply this patch (no write/push):")
        print(fix["patch"][:4000])
        return 0

    with tempfile.TemporaryDirectory(prefix="vps-repair-") as tmp:
        worktree = Path(tmp) / "wt"
        if _git(["worktree", "add", str(worktree), "main"]).returncode != 0:
            print("[vps-repair] worktree add failed", file=sys.stderr)
            return 2
        try:
            patch_file = worktree / "repair.patch"
            patch_file.write_text(fix["patch"], encoding="utf-8")
            if _git(["apply", "--check", "--whitespace=error-all", "repair.patch"], cwd=worktree).returncode != 0:
                print("[vps-repair] git apply --check failed", file=sys.stderr)
                return 3
            if _git(["apply", "--whitespace=error-all", "repair.patch"], cwd=worktree).returncode != 0:
                print("[vps-repair] git apply failed", file=sys.stderr)
                return 3
            ok, label = run_validation(worktree)
            if not ok:
                print(f"[vps-repair] validation failed at {label}; not committing", file=sys.stderr)
                return 3
            diff = _git(["diff", "HEAD"], cwd=worktree).stdout
            reviews, diff_sha = review_diff(diff, args.run_id, args.task_id)
            if len(reviews) < 2 or any(r["verdict"] != "PASS" for r in reviews):
                print("[vps-repair] review not passed; not committing", file=sys.stderr)
                return 3
            if not commit_with_trailers(worktree, diff_sha, reviews,
                                        f"fix: auto-repair {args.task_id} parse failure (site_breakage)"):
                return 2
            if not push_main(worktree):
                return 2
        finally:
            _git(["worktree", "remove", "--force", str(worktree)])
            _git(["worktree", "prune"])

    redispatch(args.run_id)
    print("[vps-repair] repair committed and vps-monitor redispatched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
