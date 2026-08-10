"""Tests for the auto vendor-discovery chain (P2)."""

import json
from pathlib import Path

import yaml

from scripts.discover_vendors import (
    STOPWORDS,
    _brand_from_slug,
    extract_vendor_candidates,
    load_existing_providers,
)

ROOT = Path(__file__).parents[1]


SAMPLE_DEALS = [
    {"source": "lowendtalk", "title": "linveo.com - Intel KVM VPS with 4GB RAM from $2.85/mo",
     "link": "https://lowendtalk.com/discussion/213583/linveo-com-intel-kvm-vps-with-4"},
    {"source": "lowendtalk", "title": "★ RoboVPS® offers: Full h/w KVM VPS, AMD Ryzen 9 9950X",
     "link": "https://lowendtalk.com/discussion/219830/robovps-offers-full-h-w-kvm"},
    {"source": "lowendtalk", "title": "TNAHosting - SSD KVM VPS - Starting at $14.40/year",
     "link": "https://lowendtalk.com/discussion/219944/tnahosting-ssd-kvm-vps-starti"},
    {"source": "lowendtalk", "title": "In urgent need of a Storage VPS",
     "link": "https://lowendtalk.com/discussion/219927/looking-for-a-vps-provider-wi"},
    {"source": "lowendbox", "title": "END OF AN ERA: Luxvps Closes Its Special Offer",
     "link": "https://lowendbox.com/blog/end-of-an-era-luxvps-closes-its-special-offer"},
    {"source": "lowendtalk", "title": "Why Oracle's Trouble in Wisconsin Is a Big Deal",
     "link": "https://lowendbox.com/blog/why-oracles-trouble-in-wisconsin-is-a-big-deal"},
    {"source": "lowendtalk", "title": "GreenCloud | TOP 1 PROVIDER | DOUBLE PROMOTIONS",
     "link": "https://lowendtalk.com/discussion/216691/greencloud-top-1-provider"},
    {"source": "lowendtalk", "title": "Taiwan VPS Deals are BACK! SoftShellWeb - Serving",
     "link": "https://lowendtalk.com/discussion/213567/taiwan-vps-deals-are-back-softs"},
]


def test_brand_from_slug():
    assert _brand_from_slug("https://lowendtalk.com/discussion/213583/linveo-com-intel-kvm-vps-with-4") == "linveo"
    assert _brand_from_slug("https://lowendtalk.com/discussion/219830/robovps-offers-full-h-w-kvm") == "robovps"
    assert _brand_from_slug("https://lowendbox.com/blog/end-of-an-era-luxvps-closes-its-special") == "luxvps"
    assert _brand_from_slug("https://lowendtalk.com/discussion/219927/looking-for-a-vps-provider-wi") == ""
    assert _brand_from_slug("") == ""


def test_brand_from_slug_rejects_bandwidth_descriptors():
    """Bandwidth descriptors like 10gbps are not brands (the auto-extend chain
    once misnamed a provider '10Gbps' from a deal slug; verified 2026-08-09
    that 10gbps.io redirects to datapacket.com — a dedicated-server vendor)."""
    assert _brand_from_slug("https://lowendtalk.com/discussion/212014/black-friday-exclusive-amd-epyc-7713-9334-nvme-vps-from-4-month-iowa-utah-10gbps") == ""
    assert _brand_from_slug("https://lowendtalk.com/discussion/219682/from-2-25-mo-10-gbps-kvm-vps-lo") == ""
    assert _brand_from_slug("https://lowendtalk.com/discussion/219899/krypt-ion-changed-my-legacy-vps") == "krypt"


def test_extract_vendor_candidates_finds_real_vendors():
    candidates = extract_vendor_candidates(SAMPLE_DEALS)
    vendors = {c["vendor"].casefold() for c in candidates}
    assert "linveo" in vendors
    assert "robovps" in vendors
    assert "tnahosting" in vendors
    assert "luxvps" in vendors
    # Noise that must not appear.
    assert "oracle" not in vendors
    assert "urgent" not in vendors
    assert "taiwan" not in vendors


def test_existing_providers_excluded():
    providers, domains = load_existing_providers(ROOT / "providers.yaml")
    assert "greencloud" in providers
    candidates = extract_vendor_candidates(SAMPLE_DEALS)
    fresh = [c for c in candidates if c["vendor"].casefold() not in providers]
    assert all(c["vendor"].casefold() != "greencloud" for c in fresh)


def test_stopwords_cover_geography():
    assert "taiwan" in STOPWORDS
    assert "los" in STOPWORDS
    assert "angeles" in STOPWORDS


def test_vendor_workflow_declares_schedule_and_auto_gate():
    import yaml

    raw = (ROOT / ".github/workflows/vps-vendor-discovery.yml").read_text(encoding="utf-8")
    # YAML 1.1 parses bare `on:` as boolean True; force string key.
    wf = yaml.safe_load(raw.replace("on:", "on_str:", 1))
    triggers = wf["on_str"]
    assert triggers["schedule"][0]["cron"] == "0 4 * * 2"
    assert "auto_extend" in triggers["workflow_dispatch"]["inputs"]
    steps = [s.get("name") for s in wf["jobs"]["discover"]["steps"]]
    assert "Discover new vendor candidates" in steps
    assert "AI-extend vendors (validated + reviewed + committed)" in steps
    assert "Report candidates as issue (discovery only)" in steps
    # The AI-extend path needs the opencode CLI on the runner.
    assert "Install OpenCode CLI" in steps
    assert "npm install --global opencode-ai@latest" in raw


def test_runner_scripts_registered_in_gate():
    src = (ROOT / "scripts/codex_delivery_gate.py").read_text(encoding="utf-8")
    for name in ("discover_vendors.py", "vendor_extend_runner.py", "write_vendor_issue.py"):
        assert name in src


def test_append_block_yaml_escapes_special_chars(tmp_path):
    """AI-generated strings with :/#/quotes must not break providers.yaml."""
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from vendor_extend_runner import build_append_block

    target = {
        "id": "tst-vps",
        "provider": "TestHost: Pro",
        "plan_name": '2GB "KVM" #1',
        "plan_tokens": ["2GB RAM", "KVM: Pro", "#1"],
        "url": "https://test.example.com/vps",
        "region": "US",
        "provider_claimed_routes": ["CN2: GIA", "BGP"],
        "reliability": 6,
        "oversell": "medium",
        "reliability_note": "口碑好: 稳定 #1",
        "specs": {"cpu": 2, "ram_gb": 4, "storage_gb": 40, "bandwidth_gbps": 1},
        "priority": 300,
    }
    block = build_append_block(target)
    # Must round-trip through YAML without error.
    doc = "target_defaults: &target_defaults\n  lifecycle: active\ntargets:\n" + block
    parsed = yaml.safe_load(doc)
    t = parsed["targets"][0]
    assert t["provider"] == "TestHost: Pro"
    assert t["plan_name"] == '2GB "KVM" #1'
    assert t["plan_tokens"] == ["2GB RAM", "KVM: Pro", "#1"]
    assert t["reliability_note"] == "口碑好: 稳定 #1"
    assert t["url"] == "https://test.example.com/vps"


def test_runner_stages_before_hashes_diff():
    """The runner must git add before git diff --cached (AGENTS.md trailer
    must bind the reviewed staged diff, never an empty index)."""
    src = (ROOT / "scripts/vendor_extend_runner.py").read_text(encoding="utf-8")
    add_pos = src.find('_sh(["git", "add", str(args.providers)], cwd=ROOT)')
    diff_pos = src.find('_sh(["git", "diff", "--cached", "--binary"], cwd=ROOT)')
    assert add_pos != -1 and diff_pos != -1
    assert add_pos < diff_pos


def test_repair_fix_prompt_embeds_monitor_source():
    """The fix agent runs deny-tools; without the monitor.py source in the
    prompt it can only hallucinate line numbers (observed 2026-08-10 run
    31360180962: glm-5.2 honestly refused with confidence=0.0)."""
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "scripts"))
    from self_repair_runner_vps import _monitor_source_excerpt, build_fix_prompt

    src = _monitor_source_excerpt()
    assert "def _matches_target" in src
    assert "def parse_offer" in src
    # browser_fetch is the boundary; the excerpt stops before it but includes
    # all parsing helpers (matches/price/period/availability/offer/json-ld).
    assert "def _json_ld_offers" in src
    # Narrow excerpt: embedding the full 60K file exhausted glm-5.2's
    # 16000-token output budget (step_finish reason=length, no text part;
    # observed 2026-08-10 run 31374243994).
    assert len(src) < 30000
    # Page text restored to 8000 chars: thinking-disabled glm-5.2 no longer
    # burns output on reasoning, and the model needs the full price/period
    # context to raise confidence (it complained the 1500-char excerpt was
    # truncated before prices; run 31384060650).
    assert "page_text[:8000]" in (ROOT / "scripts/self_repair_runner_vps.py").read_text(encoding="utf-8")
    prompt = build_fix_prompt(
        {"task_id": "buyvm-slice4096", "plan_tokens": ["SLICE 4096"], "target_url": "https://buyvm.net/x"},
        "",
        page_text="SLICE 4096 4096 MB Memory 80 GB SSD Storage",
    )
    assert "monitor.py 当前完整源码" in prompt
    assert "def _matches_target" in prompt
    assert "实际页面可见文本" in prompt
    assert "SLICE 4096 4096 MB" in prompt
    # providers.yaml task config (expected_domains etc.) must be embedded —
    # the model asked for it to judge _specific_order_url domain checks
    # (observed 2026-08-10 run 31388059937).
    assert "任务配置" in prompt
    assert "expected_domains" in prompt
    assert "buyvm.net" in prompt
    # The deny-tools prefix must not forbid reasoning over the embedded source.
    runner_src = (ROOT / "scripts/self_repair_runner_vps.py").read_text(encoding="utf-8")
    assert "Do not call tools or modify files" not in runner_src


def test_parse_fix_response_extracts_prose_wrapped_json():
    """The fix agent wraps its JSON with prose (observed 2026-08-10 run
    31366780525: reasoning=unparseable with real content). The parser must
    extract the first { ... } span before json.loads."""
    import sys as _sys

    _sys.path.insert(0, str(ROOT / "scripts"))
    from self_repair_runner_vps import parse_fix_response

    wrapped = (
        "我分析了代码，以下是补丁：\n"
        '```json\n{"patch": "--- a/vps_monitor/monitor.py\\n+++ b/vps_monitor/monitor.py\\n", '
        '"reasoning": "fix selector", "confidence": 0.85}\n```\n'
        "希望对你有帮助。"
    )
    parsed = parse_fix_response(wrapped)
    assert parsed["confidence"] == 0.85
    assert "monitor.py" in parsed["patch"]
    assert parsed["reasoning"] == "fix selector"
    # Plain JSON still parses.
    assert parse_fix_response('{"patch": "x", "confidence": 0.5}')["confidence"] == 0.5
    # Garbage stays unparseable (safe default).
    assert parse_fix_response("not json at all")["confidence"] == 0.0
    # The fix prompt must forbid tool use (a tool loop with 18 calls
    # produced no final answer; observed 2026-08-10 run 31369421651).
    runner_src = (ROOT / "scripts/self_repair_runner_vps.py").read_text(encoding="utf-8")
    assert "DO NOT use any tool" in runner_src
    # All tools denied INCLUDING read: with the source embedded, tool calls
    # only burn output budget (run 31377327449: output=16000, reason=length,
    # no text part because plan-agent tool attempts were discarded).
    assert '"read": "deny"' in runner_src
    # glm-5.2 deep-think burned the whole output budget as reasoning with no
    # text event (step_finish reason=length; runs 31377327449..31382697099).
    # Thinking must be disabled so the model answers directly (verified
    # locally 2026-08-10: output dropped to 8 tokens, text emitted).
    assert '"thinking": {"type": "disabled"}' in runner_src
    # Parse failures must print the raw text for diagnosis.
    assert "parse_fix_response failed" in runner_src


def test_repair_agent_uses_fast_ark_provider():
    """The fix agent must use the fast Ark glm-5.2 endpoint (kimi k3 timed out
    at 600s on patch generation 2026-08-10 run 31357764038)."""
    src = (ROOT / "scripts/self_repair_runner_vps.py").read_text(encoding="utf-8")
    assert "volcengine-coding" in src
    assert "glm-5.2" in src
    assert "ark.cn-beijing.volces.com/api/coding/v3" in src
    # --max-turns is NOT a valid opencode run flag (prints help + exits 0,
    # silently skipping the LLM call; verified 2026-08-10 by GLM review).
    assert "--max-turns" not in src
    # glm-5.2 needs a large output budget or its reasoning exhausts the
    # 4000-token limit and the text part comes back empty (observed
    # 2026-08-10 run 31361365239: "fix agent returned nothing").
    assert "max_tokens=16000" in src
    # Diagnostic for empty extraction: event-type distribution must be logged
    # so a silent "returned nothing" is debuggable (run 31362645513 still
    # returned nothing after the 16000 fix).
    assert "event types:" in src
    assert "stdout bytes:" in src
    # The fix agent must run with --dir at the repo root (read permission
    # reaches monitor.py) with the provider config in a gitignored scratch
    # dir; a bare temp dir made the model's read tool fail -> no text part
    # (observed 2026-08-10 run 31364202598: tool_use events, no text).
    assert '"--dir", str(ROOT)' in src
    # Provider config must live at ROOT/opencode.json (opencode's project
    # config discovery only scans <--dir>/opencode.json, never a
    # subdirectory; verified 2026-08-10 by GLM review).
    assert 'cfg_path = ROOT / "opencode.json"' in src
    assert "finally:" in src  # config is cleaned up after the call
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/opencode.json" in gi
    wf = (ROOT / ".github/workflows/vps-repair.yml").read_text(encoding="utf-8")
    assert "VOLCENGINE_CODING_PLAN_API_KEY" in wf
    assert "NVIDIA_NIM_API_KEY" in wf  # review still depends on NIM


def test_repair_workflow_installs_playwright_browser():
    """vps-repair must install the Playwright chromium binary like vps-monitor;
    verify's browser-render fallback cannot start without it (observed
    2026-08-09: verify kept reporting NOT confirmed because chromium was
    missing on the runner)."""
    repair = (ROOT / ".github/workflows/vps-repair.yml").read_text(encoding="utf-8")
    assert "playwright install --with-deps chromium" in repair
    assert repair.count("playwright install --with-deps chromium") == 1


def test_repair_workflow_has_mihomo_like_monitor():
    """vps-repair must configure the same mihomo node-rotation runtime as
    vps-monitor; without it, verify_plan_tokens fetches from the raw runner
    IP, gets anti-bot pages and misclassifies healthy tasks as retired
    (observed 2026-08-09: 8 tasks wrongly confirmed external_retired)."""
    repair = (ROOT / ".github/workflows/vps-repair.yml").read_text(encoding="utf-8")
    monitor = (ROOT / ".github/workflows/vps-monitor.yml").read_text(encoding="utf-8")
    assert "Configure mihomo node-rotation runtime" in repair
    assert "setup_proxy_runtime.py" in repair
    assert "PROXY_SUBSCRIPTIONS" in repair
    assert "--test-url" in repair
    # The verify channel must honour the proxy env too.
    verify_src = (ROOT / "vps_monitor/verify.py").read_text(encoding="utf-8")
    assert "proxies=_proxies_from_env()" in verify_src
    assert "HTTP_PROXY" in verify_src
    # Consistency guard: any workflow that fetches pages needs the runtime.
    assert monitor.count("setup_proxy_runtime.py") >= 1
    assert repair.count("setup_proxy_runtime.py") >= 1


def test_repair_workflow_skips_retired_and_backfills_slots():
    """vps-repair workflow must not let confirmed-external_retired tasks
    consume the 3 repair slots (observed 2026-08-09: bandwagon/cloudcone
    retired tasks blocked buyvm/linveo every run)."""
    wf = (ROOT / ".github/workflows/vps-repair.yml").read_text(encoding="utf-8")
    assert "repaired_count >= 3" in wf
    assert "external_retired" in wf
    assert "no slot consumed" in wf
    assert "retired_ids" in wf
    # Slot accounting: success consumes a slot, retired does not, failure does.
    assert "repaired_count += 1" in wf
    assert "retired_ids.add(task" in wf
    # The bounded loop iterates the whole candidate list, not tasks[:3].
    assert "for task in tasks:" in wf
    assert "tasks[:3]" not in wf


def test_self_repair_runner_imports_vps_monitor():
    """self_repair_runner_vps.py must add ROOT to sys.path before importing
    vps_monitor.verify (workflow runs it as `python scripts/...` from the repo
    root; without the path fix the import fails and all repairs are skipped —
    observed 2026-08-09 on run 31289510463)."""
    src = (ROOT / "scripts/self_repair_runner_vps.py").read_text(encoding="utf-8")
    assert "sys.path.insert(0, str(ROOT))" in src
    # The path insert must come before the vps_monitor import site.
    insert_pos = src.find("sys.path.insert(0, str(ROOT))")
    import_pos = src.find("from vps_monitor.verify import")
    assert insert_pos != -1 and import_pos != -1
    assert insert_pos < import_pos


def test_runner_pushes_after_commit():
    """The runner must push to main after commit (a local-only commit leaves
    the repo stale; verified 2026-08-08 when the first auto-extend run
    committed on the runner but never reached GitHub)."""
    src = (ROOT / "scripts/vendor_extend_runner.py").read_text(encoding="utf-8")
    commit_pos = src.find('_sh(["git", "commit", "-m", message], cwd=ROOT)')
    push_pos = src.find('_sh(["git", "push", "origin", "HEAD:main"], cwd=ROOT)')
    assert commit_pos != -1 and push_pos != -1
    assert commit_pos < push_pos


def test_runner_sets_git_identity_and_checks_commit():
    """GitHub Actions checkouts have no git identity; commit must set
    user.name/email first and check the commit returncode (a silent failed
    commit makes push report 'Everything up-to-date' and fakes success —
    observed 2026-08-08)."""
    src = (ROOT / "scripts/vendor_extend_runner.py").read_text(encoding="utf-8")
    assert '_sh(["git", "config", "user.name", "hermes-agent-deepseek-v4-flash"], cwd=ROOT)' in src
    assert '_sh(["git", "config", "user.email", "xuerui911@gmail.com"], cwd=ROOT)' in src
    assert 'committed = _sh(["git", "commit", "-m", message], cwd=ROOT)' in src
    assert 'if committed.returncode != 0:' in src
    assert "commit failed" in src


def test_extract_text_parts_from_ndjson():
    """--format json NDJSON must be parsed into the assistant text only."""
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from vendor_extend_runner import extract_text_parts

    ndjson = (
        '{"type":"step_start","part":{"type":"step-start"}}\n'
        '{"type":"text","part":{"type":"text","text":"结论：PASS"}}\n'
        '{"type":"text","part":{"type":"text","text":"DIFF_SHA256: abc"}}\n'
        '{"type":"step_finish","part":{"type":"step-finish"}}\n'
    )
    assert extract_text_parts(ndjson) == "结论：PASS\nDIFF_SHA256: abc"
    assert extract_text_parts("not json at all") == ""
    assert extract_text_parts("") == ""


def test_runner_uses_json_format_not_default():
    """--format default pollutes stdout with ANSI/banner lines that break
    JSON parsing (observed 2026-08-08: stdout=413 but no parseable targets).
    Both runners must use --format json + NDJSON text extraction."""
    for script in ("vendor_extend_runner.py", "self_repair_runner_vps.py"):
        src = (ROOT / f"scripts/{script}").read_text(encoding="utf-8")
        assert '"--format", "json"' in src
        assert '"--format", "default"' not in src
    assert "extract_text_parts" in (ROOT / "scripts/vendor_extend_runner.py").read_text(encoding="utf-8")


def test_review_fallthrough_requires_two_families():
    """A flaky family must not block the pipeline: review_diff falls through
    to the next family, and main() requires >=2 PASS families (each from a
    different model family) before committing."""
    src = (ROOT / "scripts/vendor_extend_runner.py").read_text(encoding="utf-8")
    # review_diff records only PASS reviews.
    assert "reviews.append({" in src
    assert "trying next family" in src
    # main() gates on two families, not all-reviewers-pass.
    assert "if len(reviews) < 2:" in src
    assert "fewer than two PASS families" in src
    # The fallback family (NIM minimax) is declared after Ark kimi.
    kimi_pos = src.find('"model": "kimi-k2.7-code"')
    minimax_pos = src.find('"model": "nvidia-minimax-m3"')
    assert kimi_pos != -1 and minimax_pos != -1
    assert kimi_pos < minimax_pos


def test_runner_uses_project_config_file_not_env():
    """OPENCODE_CONFIG_CONTENT env injection returns 404 on opencode 1.18.15
    (verified 2026-08-08); the working mechanism is a project-level
    opencode.json written inside --dir. Both runners must use the file."""
    for script in ("vendor_extend_runner.py", "self_repair_runner_vps.py"):
        src = (ROOT / f"scripts/{script}").read_text(encoding="utf-8")
        # The env var may appear in comments (explaining why it is not used),
        # but never as an actual assignment.
        assert 'env["OPENCODE_CONFIG_CONTENT"]' not in src
        assert "OPENCODE_CONFIG_CONTENT = json.dumps" not in src
        assert '"opencode.json"' in src
        if script == "vendor_extend_runner.py":
            assert '"--dir", tmpdir' in src
        else:
            # self_repair_runner runs with --dir at the repo root so the
            # model's read tool reaches monitor.py (see 2026-08-10 run
            # 31364202598: tool_use events, no text with a bare temp dir).
            assert '"--dir", str(ROOT)' in src


def test_runner_uses_global_exact_provider_model_names():
    """opencode 1.18.x config injection only overrides already-declared
    providers/models when a global config exists; on a fresh runner the
    injected config registers them. Use the exact global names so both
    environments work (verified 2026-08-08 on opencode 1.18.15)."""
    src = (ROOT / "scripts/vendor_extend_runner.py").read_text(encoding="utf-8")
    assert '"provider": "volcengine-coding", "model": "glm-5.2"' in src
    assert '"provider": "volcengine-coding", "model": "kimi-k2.7-code"' in src
    # No invented provider names may remain (NIM is only a declared fallback).
    assert "nvidia-nim" not in src
    assert "minimaxai/minimax-m3" not in src
    # Same rule for the repair runner (which shares the opencode injection path).
    repair = (ROOT / "scripts/self_repair_runner_vps.py").read_text(encoding="utf-8")
    assert '"name": "nvidia"' in repair
    assert "nvidia-glm-5.2" in repair
    assert "nvidia-minimax-m3" in repair
    assert "nvidia-nim-glm" not in repair
