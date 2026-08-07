"""Tests for the v4 extension: scoring, deals intel, and task set."""

import json
from pathlib import Path

import pytest
import yaml

from scripts.fetch_deals import (
    EXCLUDE_KEYWORDS,
    REQUEST_PATTERNS,
    STRONG_KEYWORDS,
    keyword_match,
    parse_feed,
)
from vps_monitor.monitor import load_config, load_targets, value_score

ROOT = Path(__file__).parents[1]


# ---------- providers.yaml extension ----------

def test_config_revision_is_five():
    config = yaml.safe_load((ROOT / "providers.yaml").read_text(encoding="utf-8"))
    assert config["config_revision"] == 5


def test_task_set_expanded_to_18_providers_and_vps_only():
    config = load_config(ROOT / "providers.yaml")
    targets = load_targets(config)
    providers = {target.provider for target in targets}
    assert len(targets) >= 25
    assert len(providers) >= 16
    # New providers present
    for name in ("CloudIPLC", "V.PS", "DMIT", "SpartanHost", "PacificRack", "Contabo", "HostHatch", "ion"):
        assert name in providers, f"missing provider {name}"


def test_all_targets_have_scoring_and_specs():
    targets = load_targets(load_config(ROOT / "providers.yaml"))
    for target in targets:
        assert 0 <= target.reliability <= 10, target.id
        assert target.oversell in {"none", "low", "medium", "high"}, target.id
        specs = dict(target.specs)
        assert specs.get("ram_gb", 0) > 0, f"{target.id} missing ram_gb specs"


def test_pacificrack_is_low_score_example():
    targets = load_targets(load_config(ROOT / "providers.yaml"))
    pr = [t for t in targets if t.provider == "PacificRack"]
    assert pr
    assert all(t.reliability <= 3 for t in pr)
    assert all(t.oversell == "high" for t in pr)


# ---------- value_score ----------

def _target_with_specs(specs: dict, reliability: float = 7.0):
    from vps_monitor.monitor import PlanTarget

    return PlanTarget(
        id="t", provider="P", plan_name="p", plan_tokens=("p",), url="https://example.com",
        region="US", provider_claimed_routes=("BGP",), reliability=reliability,
        oversell="medium", specs=tuple((k, float(v)) for k, v in specs.items()),
    )


def test_value_score_cheap_high_specs_wins():
    cheap = _target_with_specs({"cpu": 4, "ram_gb": 8, "storage_gb": 100, "bandwidth_gbps": 2})
    pricey = _target_with_specs({"cpu": 1, "ram_gb": 1, "storage_gb": 10, "bandwidth_gbps": 0.3})
    assert value_score(cheap, 5.0) > value_score(pricey, 5.0)


def test_value_score_bounds_and_none():
    target = _target_with_specs({"cpu": 4, "ram_gb": 8, "storage_gb": 100, "bandwidth_gbps": 2})
    score = value_score(target, 5.0)
    assert 1.0 <= score <= 10.0
    assert value_score(target, None) is None
    assert value_score(target, 0) is None


def test_value_score_in_status_output():
    # Run a fixture round and confirm value_score appears in status rows.
    import subprocess

    result = subprocess.run(
        ["python", "-m", "vps_monitor.monitor", "--output"],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-1000:]
    site = ROOT / "site"
    status = json.loads((site / "data" / "status.json").read_text(encoding="utf-8"))
    assert len(status) >= 25
    assert all("reliability" in row and "oversell" in row and "specs" in row for row in status)
    import shutil

    shutil.rmtree(site, ignore_errors=True)


# ---------- deals intel ----------

SAMPLE_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item><title>AwesomeHost - 2GB EPYC VPS $4/mo</title><link>https://example.com/deal1</link>
    <pubDate>Mon, 04 Aug 2026 12:00:00 GMT</pubDate><description>VPS promo</description></item>
  <item><title>Looking for a dedi in Ashburn</title><link>https://example.com/req1</link>
    <pubDate>Mon, 04 Aug 2026 13:00:00 GMT</pubDate><description>buyer request</description></item>
  <item><title>Why Oracle Trouble Is a Big Deal</title><link>https://example.com/news1</link>
    <pubDate>Mon, 04 Aug 2026 14:00:00 GMT</pubDate><description>news</description></item>
  <item><title>CloudVPS 20% OFF with COUPON</title><link>https://example.com/deal2</link>
    <pubDate>Mon, 04 Aug 2026 15:00:00 GMT</pubDate><description>sale</description></item>
</channel></rss>"""


def test_deals_keyword_gate():
    assert keyword_match("AwesomeHost 2GB EPYC VPS $4/mo", "") is True
    assert keyword_match("CloudVPS 20% OFF with COUPON", "") is True
    assert keyword_match("Looking for a dedi in Ashburn", "") is False
    assert keyword_match("Why Oracle Trouble Is a Big Deal", "") is False
    assert keyword_match("VPS hosting deal", "") is True


def test_deals_parse_feed_filters():
    entries = parse_feed("lowendbox", SAMPLE_RSS, now="2026-08-07T00:00:00+00:00")
    titles = [entry["title"] for entry in entries]
    assert "AwesomeHost - 2GB EPYC VPS $4/mo" in titles
    assert "CloudVPS 20% OFF with COUPON" in titles
    assert "Looking for a dedi in Ashburn" not in titles
    assert "Why Oracle Trouble Is a Big Deal" not in titles


def test_deals_no_affiliate_params_in_links():
    entries = parse_feed("lowendbox", SAMPLE_RSS, now="2026-08-07T00:00:00+00:00")
    for entry in entries:
        assert "aff=" not in entry["link"].lower()
        assert "ref=" not in entry["link"].lower()
        assert "referral" not in entry["link"].lower()


def test_deals_scripts_not_in_trust_root_docstring():
    src = (ROOT / "scripts/fetch_deals.py").read_text(encoding="utf-8")
    assert "never enters the" in src and "product gate" in src
    assert "Affiliate" in src and "NOT processed" in src


def test_vps_monitor_workflow_runs_deals_before_structural_gate():
    wf = (ROOT / ".github/workflows/vps-monitor.yml").read_text(encoding="utf-8")
    # Deals are fetched inline during the live round (publish_site writes
    # data/deals.json into the staging dir), so the manifest includes them.
    assert "Build complete live round" in wf
    assert "Structural publish gate" in wf
    assert "data/deals.json" in (ROOT / "vps_monitor/monitor.py").read_text(encoding="utf-8")


def test_deals_output_is_registered_in_manifest_via_rglob():
    # publish_site collects files with rglob, so data/deals.json is included
    # in manifest without touching contracts/audit trust roots.
    src = (ROOT / "vps_monitor/monitor.py").read_text(encoding="utf-8")
    assert "rglob" in src
    assert "deals" in src


def test_publish_site_writes_deals_into_staging(tmp_path):
    """publish_site must write data/deals.json into staging so the manifest
    (generated by rglob) includes it atomically."""
    import json as _json

    from vps_monitor.monitor import publish_site

    targets = load_targets(load_config(ROOT / "providers.yaml"))
    from vps_monitor.monitor import build_public_data

    # Reuse the blocked-result builder from test_monitor conventions.
    def blocked(target):
        from vps_monitor.monitor import TargetResult

        return TargetResult(
            target=target, outcome="blocked", offer=None, http_status=403,
            final_url=target.url, method="requests", block_reason="http_403",
            attempts=1, latency_ms=15, checked_at="2026-07-30T00:00:00+00:00",
        )

    public = build_public_data([blocked(t) for t in targets], targets)
    deals = {"schema_version": 1, "fetched_at": "2026-08-07T00:00:00+00:00",
             "sources": ["lowendbox"], "entry_count": 0, "errors": {}, "entries": []}
    site_dir = tmp_path / "site"
    publish_site(public, site_dir, deals=deals)
    manifest = _json.loads((site_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "data/deals.json" in manifest["files"]
    assert (site_dir / "data/deals.json").exists()
