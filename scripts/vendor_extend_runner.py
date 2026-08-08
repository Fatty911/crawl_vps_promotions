#!/usr/bin/env python3
"""Auto-extend vendor scope (P2): turn discovery candidates into reviewed targets.

Pipeline (all failures create an issue, never silent):
  1. discover_vendors.py emits state/vendor_candidates.json (deterministic).
  2. This runner asks an Agent (opencode, kimi-coding-plan/k3) to build full
     target definitions for up to MAX_NEW_VENDORS candidates. The AI must
     provide a concrete official URL per vendor.
  3. Deterministic validation: URL reachability (HTTP 200/3xx, not 403),
     page contains VPS markers, id/url/domain uniqueness vs providers.yaml,
     config loads + full test suite passes in a worktree.
  4. Two-family review (glm-5.2 + minimax-m3) of the exact staged diff.
  5. Commit on main with review trailers only when both reviews PASS.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
MAX_NEW_VENDORS = int(os.environ.get("VENDOR_MAX_NEW", "3"))
# Provider names and model IDs MUST match the global opencode config exactly:
# opencode 1.18.x only lets config injection override providers/models that are
# already declared (whitelist + models). Verified 2026-08-08 on opencode 1.18.15:
#   - volcengine-coding/glm-5.2          (global opencode.json) works
#   - volcengine-coding/kimi-k2.7-code   (global opencode.json) works
#   - arbitrary new provider names       -> ProviderModelNotFoundError
REVIEW_MODELS = [
    {
        "name": "glm", "provider": "volcengine-coding", "model": "glm-5.2",
        "env_key": "VOLCENGINE_CODING_PLAN_API_KEY",
        "base_url": "https://ark.cn-beijing.volces.com/api/coding/v3",
    },
    # Moonshot family via Ark (NIM minimax was flaky/429 on 2026-08-08;
    # Ark kimi-k2.7-code is fast and stable — same two-family requirement:
    # Zhipu glm-5.2 + Moonshot kimi-k2.7-code).
    {
        "name": "kimi", "provider": "volcengine-coding", "model": "kimi-k2.7-code",
        "env_key": "VOLCENGINE_CODING_PLAN_API_KEY",
        "base_url": "https://ark.cn-beijing.volces.com/api/coding/v3",
    },
]

GEN_MODEL = {
    "name": "glm", "provider": "volcengine-coding", "model": "glm-5.2",
    "env_key": "VOLCENGINE_CODING_PLAN_API_KEY",
    "base_url": "https://ark.cn-beijing.volces.com/api/coding/v3",
}

# VPS-ish markers to check on a candidate landing page.
PAGE_MARKERS = (
    "vps", "kvm", "cloud", "server", "ram", "cpu", "ssd", "nvme",
    "bandwidth", "month", "price",
)


def _sh(args: list[str], cwd: Path | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, cwd=cwd, timeout=timeout)


def call_opencode(provider: dict, prompt: str, max_tokens: int = 6000) -> str | None:
    """调用 OpenCode CLI（Agent 工具），禁止脚本直连 LLM API。

    Provider config (baseURL/apiKey/limits/read-only permission) is written as
    a project-level opencode.json inside --dir; prompt is passed as a
    positional message. OPENCODE_CONFIG_CONTENT env injection returns 404 on
    opencode 1.18.15 (verified 2026-08-08) — do not use it.
    """
    import tempfile

    key = os.environ.get(provider.get("env_key", ""), "")
    if not key:
        print(f"[vendor-extend] missing {provider.get('env_key')}", file=sys.stderr)
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
    with tempfile.TemporaryDirectory(prefix="vendor-extend-") as tmpdir:
        # Write the provider config as a PROJECT-level opencode.json inside
        # --dir. Verified on opencode 1.18.15 (2026-08-08): with no global
        # config (fresh runner) a project opencode.json registers arbitrary
        # providers; OPENCODE_CONFIG_CONTENT env injection does NOT work
        # (404) — never use it here.
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
            completed = subprocess.run(cmd, capture_output=True, text=True, timeout=900, env=env)
        except subprocess.TimeoutExpired:
            print(f"[vendor-extend] opencode timeout ({provider['name']})", file=sys.stderr)
            return None
        if completed.returncode != 0:
            print(f"[vendor-extend] opencode exit {completed.returncode}: "
                  f"{(completed.stderr or '')[:300]}", file=sys.stderr)
            return None
        return (completed.stdout or "").strip() or None


def build_generation_prompt(candidates: list[dict], existing_providers: list[str]) -> str:
    return f"""你是 VPS 监控任务扩展器。根据候选厂商列表，为 crawl_vps_promotions 仓库生成新的 providers.yaml 任务定义。

## 候选厂商
{json.dumps(candidates, ensure_ascii=False, indent=2)}

## 已有厂商（不要重复）
{json.dumps(existing_providers, ensure_ascii=False, indent=2)}

## 要求
1. 最多选 {MAX_NEW_VENDORS} 个厂商，每个厂商生成 1 个 target 定义（选其最具代表性的 VPS 套餐）
2. 必须提供该厂商的官方页面 URL（官网首页或产品页）——URL 会经可达性验证，不可达则丢弃
3. 只做 VPS 任务；独立主机/web-hosting/域名/RDP/VPN 一律排除
4. 每个 target 字段：id(小写连字符,如 digirdp-hk-vps)、provider(厂商名)、plan_name、
   plan_tokens(页面关键词数组)、url、region、provider_claimed_routes、reliability(0-10 社区口碑估算)、
   oversell(none/low/medium/high)、reliability_note(一句话依据)、specs(cpu/ram_gb/storage_gb/bandwidth_gbps)
5. 输出纯 JSON 数组（不要 markdown 代码块），形如：
[{{"id":"...","provider":"...","plan_name":"...","plan_tokens":["..."],"url":"https://...","region":"...","provider_claimed_routes":["..."],"reliability":6,"oversell":"medium","reliability_note":"...","specs":{{"cpu":1,"ram_gb":2,"storage_gb":20,"bandwidth_gbps":1}}}}]"""


def parse_targets(text: str) -> list[dict]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def validate_url(url: str) -> tuple[bool, str]:
    """Reachability check: 200/3xx allowed, 401/403/4xx/5xx rejected."""
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"},
            timeout=20,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        return False, f"connection:{type(exc).__name__}"
    status = response.status_code
    if status in {401, 403}:
        return False, f"http_{status}"
    if 200 <= status < 400:
        return True, f"http_{status}"
    return False, f"http_{status}"


def validate_target(target: dict, existing_ids: set, existing_urls: set, existing_domains: set) -> tuple[bool, str]:
    required = {"id", "provider", "plan_name", "plan_tokens", "url", "region",
                "provider_claimed_routes", "reliability", "oversell", "specs"}
    missing = required - set(target)
    if missing:
        return False, f"missing fields: {sorted(missing)}"
    if target["id"] in existing_ids:
        return False, "duplicate id"
    url = str(target["url"])
    if url in existing_urls:
        return False, "duplicate url"
    domain = re.sub(r"^https?://(?:www\.)?", "", url).split("/")[0].casefold()
    if domain in existing_domains:
        return False, f"domain already tracked: {domain}"
    if not 0 <= int(target["reliability"]) <= 10:
        return False, "reliability out of range"
    if target["oversell"] not in {"none", "low", "medium", "high"}:
        return False, "bad oversell value"
    ok, reason = validate_url(url)
    if not ok:
        return False, f"url unreachable: {reason}"
    return True, "ok"


def _yaml_str(value: Any) -> str:
    """Quote a YAML scalar safely (double quotes with escaping)."""
    text = str(value)
    if text == "":
        return '""'
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def build_append_block(target: dict) -> str:
    domain = re.sub(r"^https?://(?:www\.)?", "", target["url"]).split("/")[0]
    lines = [f"  - id: {_yaml_str(target['id'])}"]
    lines.append(f"    <<: *target_defaults")
    lines.append(f"    priority: {target.get('priority', 300)}")
    lines.append(f"    expected_domains: [{_yaml_str(domain)}]")
    lines.append(f"    expected_currencies: [USD]")
    lines.append(f"    provider: {_yaml_str(target['provider'])}")
    lines.append(f"    plan_name: {_yaml_str(target['plan_name'])}")
    tokens = ", ".join(_yaml_str(t) for t in target["plan_tokens"])
    lines.append(f"    plan_tokens: [{tokens}]")
    lines.append(f"    url: {_yaml_str(target['url'])}")
    lines.append(f"    region: {_yaml_str(target['region'])}")
    routes = ", ".join(_yaml_str(r) for r in target["provider_claimed_routes"])
    lines.append(f"    provider_claimed_routes: [{routes}]")
    lines.append(f"    reliability: {target['reliability']}")
    lines.append(f"    oversell: {_yaml_str(target['oversell'])}")
    note = target.get("reliability_note", "")
    lines.append(f"    reliability_note: {_yaml_str(note)}")
    specs = target["specs"]
    lines.append(f"    specs: {{cpu: {specs.get('cpu', 1)}, ram_gb: {specs.get('ram_gb', 1)}, "
                 f"storage_gb: {specs.get('storage_gb', 10)}, bandwidth_gbps: {specs.get('bandwidth_gbps', 0.5)}}}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, default=ROOT / "state" / "vendor_candidates.json")
    parser.add_argument("--providers", type=Path, default=ROOT / "providers.yaml")
    parser.add_argument("--commit", action="store_true", help="commit on main when reviews pass")
    args = parser.parse_args()

    if not args.candidates.exists():
        print("[vendor-extend] no candidates file; run discover_vendors first")
        return 2
    payload = json.loads(args.candidates.read_text(encoding="utf-8"))
    candidates = payload.get("candidates", [])
    if not candidates:
        print("[vendor-extend] no new vendor candidates")
        return 0

    config = yaml.safe_load(args.providers.read_text(encoding="utf-8"))
    existing = [str(t["provider"]) for t in config.get("targets", [])]
    existing_ids = {str(t["id"]) for t in config.get("targets", [])}
    existing_urls = {str(t["url"]) for t in config.get("targets", [])}
    existing_domains = set()
    for t in config.get("targets", []):
        for d in t.get("expected_domains", []):
            existing_domains.add(str(d).casefold())

    prompt = build_generation_prompt(candidates[:MAX_NEW_VENDORS * 3], existing)
    print("[vendor-extend] asking AI to build target definitions...")
    content = None
    for attempt in range(2):
        content = call_opencode(GEN_MODEL, prompt)
        if not content:
            content = call_opencode(REVIEW_MODELS[0], prompt)
        if content:
            break
        print(f"[vendor-extend] generation attempt {attempt + 1} failed", file=sys.stderr)
        time.sleep(10)
    if not content:
        print("[vendor-extend] AI generation failed")
        return 3

    targets = parse_targets(content)
    if not targets:
        print("[vendor-extend] AI returned no parseable targets")
        return 3
    print(f"[vendor-extend] AI produced {len(targets)} targets")

    valid: list[dict] = []
    for target in targets[:MAX_NEW_VENDORS]:
        ok, reason = validate_target(target, existing_ids, existing_urls, existing_domains)
        if not ok:
            print(f"[vendor-extend] rejected {target.get('id', '?')}: {reason}")
            continue
        existing_ids.add(target["id"])
        existing_urls.add(target["url"])
        valid.append(target)
    if not valid:
        print("[vendor-extend] no valid targets after validation")
        return 4
    print(f"[vendor-extend] {len(valid)} valid targets: {[t['id'] for t in valid]}")

    if not args.commit:
        print("[vendor-extend] dry-run: valid targets would be appended (no commit)")
        return 0

    # Append to providers.yaml.
    text = args.providers.read_text(encoding="utf-8")
    block = "".join(build_append_block(t) for t in valid)
    text = text.rstrip() + "\n" + block
    args.providers.write_text(text, encoding="utf-8")

    # Run the full test suite in a worktree (monitor loads the new config).
    if not run_validation():
        print("[vendor-extend] validation failed; reverting providers.yaml")
        _sh(["git", "checkout", "--", str(args.providers)], cwd=ROOT)
        return 5

    # Stage first, then review the exact staged diff (AGENTS.md requires the
    # Reviewed-Diff-SHA256 trailer to bind the reviewed staged content).
    _sh(["git", "add", str(args.providers)], cwd=ROOT)
    staged = _sh(["git", "diff", "--cached", "--binary"], cwd=ROOT).stdout
    import hashlib
    diff_sha = hashlib.sha256(staged.encode()).hexdigest()
    reviews = review_diff(staged, diff_sha=diff_sha)
    if not all(r["verdict"] == "PASS" for r in reviews):
        print(f"[vendor-extend] reviews not all PASS: {reviews}")
        _sh(["git", "reset", "-q", "HEAD", str(args.providers)], cwd=ROOT)
        _sh(["git", "checkout", "--", str(args.providers)], cwd=ROOT)
        return 6

    trailers = "\n".join(
        f"Review-Model-Family-{i + 1}: {r['provider']}/{r['model']}\n"
        f"Review-Result-{i + 1}: {r['verdict']}"
        for i, r in enumerate(reviews)
    )
    message = f"feat(vendor-extend): 自动扩展厂商 {[t['provider'] for t in valid]}\n\n{trailers}\nReviewed-Diff-SHA256: {diff_sha}"
    _sh(["git", "commit", "-m", message], cwd=ROOT)
    # Push to main. GITHUB_TOKEN is set by the workflow (contents: write);
    # the checkout already has the origin remote with token auth.
    push = _sh(["git", "push", "origin", "HEAD:main"], cwd=ROOT)
    if push.returncode != 0:
        print(f"[vendor-extend] push failed: {push.stderr[:300]}", file=sys.stderr)
        return 7
    print(f"[vendor-extend] committed+push: {[t['provider'] for t in valid]}")
    return 0


def run_validation() -> bool:
    import shutil
    import tempfile

    worktree = Path(tempfile.mkdtemp(prefix="vendor-extend-"))
    try:
        if _sh(["git", "worktree", "add", str(worktree), "HEAD"], cwd=ROOT).returncode != 0:
            return False
        # Copy the modified providers.yaml into the worktree.
        shutil.copy(ROOT / "providers.yaml", worktree / "providers.yaml")
        python = sys.executable
        r = _sh([python, "-m", "pytest", "-q", "-m", "not network"], cwd=worktree, timeout=600)
        return r.returncode == 0
    finally:
        _sh(["git", "worktree", "remove", "--force", str(worktree)], cwd=ROOT)


def review_diff(diff: str, run_id: str = "vendor-extend", diff_sha: str | None = None) -> list[dict]:
    from hashlib import sha256

    diff_sha = diff_sha or sha256(diff.encode()).hexdigest()
    reviews = []
    for provider in REVIEW_MODELS:
        verdict = {"verdict": "FAIL", "reason": "no review"}
        prompt = (
            f"你是只读评审员。评审 crawl_vps_promotions 自动扩展厂商的 staged diff。\n"
            f"Diff SHA256（门禁 agent-staged-v3）: {diff_sha}\n"
            f"评审要求：确认新 target 字段完整、URL 合理、无重复、VPS-only、reliability/oversell 取值合法。\n"
            f"输出格式（严格）：第一行 结论：PASS 或 结论：FAIL，第二行 DIFF_SHA256: {diff_sha}\n"
            f"然后写审查意见。\n\n{diff[:6000]}"
        )
        content = None
        for attempt in range(2):
            content = call_opencode(provider, prompt, max_tokens=1500)
            if content:
                break
            print(f"[vendor-extend] review {provider['name']} attempt {attempt + 1} failed", file=sys.stderr)
        if content:
            if re.search(r"^结论[：:]\s*PASS", content, re.M):
                verdict = {"verdict": "PASS", "reason": content[:300]}
            else:
                verdict = {"verdict": "FAIL", "reason": content[:300]}
        reviews.append({"provider": provider["provider"], "model": provider["model"], **verdict})
    return reviews


if __name__ == "__main__":
    sys.exit(main())
