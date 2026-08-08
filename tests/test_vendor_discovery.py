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
        assert '"--dir", tmpdir' in src


def test_runner_uses_global_exact_provider_model_names():
    """opencode 1.18.x config injection only overrides already-declared
    providers/models when a global config exists; on a fresh runner the
    injected config registers them. Use the exact global names so both
    environments work (verified 2026-08-08 on opencode 1.18.15)."""
    src = (ROOT / "scripts/vendor_extend_runner.py").read_text(encoding="utf-8")
    assert '"provider": "volcengine-coding", "model": "glm-5.2"' in src
    assert '"provider": "volcengine-coding", "model": "kimi-k2.7-code"' in src
    # No invented/flaky provider names may remain (NIM was 429 on 2026-08-08).
    assert "nvidia-nim" not in src
    assert "nvidia-minimax-m3" not in src
    assert "minimaxai/minimax-m3" not in src
    # Same rule for the repair runner (which shares the opencode injection path).
    repair = (ROOT / "scripts/self_repair_runner_vps.py").read_text(encoding="utf-8")
    assert '"name": "nvidia"' in repair
    assert "nvidia-glm-5.2" in repair
    assert "nvidia-minimax-m3" in repair
    assert "nvidia-nim-glm" not in repair
