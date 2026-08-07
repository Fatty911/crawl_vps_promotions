import json
from pathlib import Path

from scripts.classify_vps_failure import build_report, classify_task, load_plan_tokens

ROOT = Path(__file__).parents[1]

# Inline fixture mirroring a real live-evidence.json (run 31138423518) with
# the hostdare 403+parse-failure trap that must be classified external_block.
REALISTIC_EVIDENCE = {
    "schema_version": 1,
    "mode": "live",
    "summary": {
        "task_count": 14,
        "provider_count": 10,
        "outcome_counts": {"success": 2, "blocked": 3, "rejected": 1, "error": 8, "out_of_stock": 0},
        "method_counts": {"requests": 8, "browser": 6, "circuit": 0, "other": 0},
    },
    "tasks": [
        {"task_id": "zorocloud-us-cnc-pro", "provider": "ZoroCloud", "http_status": None,
         "final_url": "https://zorocloud.com", "method": "requests", "outcome": "error",
         "block_reason": "connection_error", "attempts": 3, "latency_ms": 7852},
        {"task_id": "zorocloud-us-9929-pro", "provider": "ZoroCloud", "http_status": None,
         "final_url": "https://zorocloud.com", "method": "requests", "outcome": "error",
         "block_reason": "connection_error", "attempts": 3, "latency_ms": 2708},
        {"task_id": "zorocloud-jp-cn2-pro", "provider": "ZoroCloud", "http_status": None,
         "final_url": "https://zorocloud.com", "method": "requests", "outcome": "error",
         "block_reason": "connection_error", "attempts": 3, "latency_ms": 12556},
        {"task_id": "hostdare-cssd3", "provider": "HostDare", "http_status": 403,
         "final_url": "https://bill.hostdare.com", "method": "requests", "outcome": "error",
         "block_reason": "no_exact_same_card_offer", "attempts": 2, "latency_ms": 752},
        {"task_id": "hostdare-camd3", "provider": "HostDare", "http_status": 403,
         "final_url": "https://bill.hostdare.com", "method": "requests", "outcome": "error",
         "block_reason": "no_exact_same_card_offer", "attempts": 2, "latency_ms": 576},
        {"task_id": "bandwagon-osaka-40g", "provider": "BandwagonHost", "http_status": 200,
         "final_url": "https://bandwagonhost.com", "method": "requests", "outcome": "rejected",
         "block_reason": "detail_unverified", "attempts": 2, "latency_ms": 1135},
        {"task_id": "jtti-us-cn2", "provider": "Jtti", "http_status": 200,
         "final_url": "https://www.jtti.cc", "method": "browser", "outcome": "blocked",
         "block_reason": "challenge_detected", "attempts": 2, "latency_ms": 943},
        {"task_id": "jtti-jp-optimized", "provider": "Jtti", "http_status": 200,
         "final_url": "https://www.jtti.cc", "method": "browser", "outcome": "blocked",
         "block_reason": "challenge_detected", "attempts": 2, "latency_ms": 1426},
        {"task_id": "racknerd-4gb-special", "provider": "RackNerd", "http_status": 403,
         "final_url": "https://my.racknerd.com", "method": "requests", "outcome": "blocked",
         "block_reason": "http_status", "attempts": 2, "latency_ms": 124},
        {"task_id": "cloudcone-ssd-vps-4", "provider": "CloudCone", "http_status": 200,
         "final_url": "https://app.cloudcone.com", "method": "requests", "outcome": "error",
         "block_reason": "no_exact_same_card_offer", "attempts": 3, "latency_ms": 3525},
        {"task_id": "buyvm-slice4096", "provider": "BuyVM", "http_status": 200,
         "final_url": "https://buyvm.net", "method": "requests", "outcome": "error",
         "block_reason": "no_exact_same_card_offer", "attempts": 2, "latency_ms": 1631},
        {"task_id": "greencloud-budgetkvmhk2-3", "provider": "GreenCloud", "http_status": 200,
         "final_url": "https://greencloudvps.com", "method": "requests", "outcome": "success",
         "block_reason": None, "attempts": 1, "latency_ms": 496},
        {"task_id": "layerstack-r108", "provider": "LayerStack", "http_status": None,
         "final_url": "https://www.layerstack.com", "method": "browser", "outcome": "error",
         "block_reason": "browser_error", "attempts": 2, "latency_ms": 5893},
        {"task_id": "lisahost-us-9929-annual", "provider": "LisaHost", "http_status": 200,
         "final_url": "https://www.lisahost.com", "method": "requests", "outcome": "success",
         "block_reason": None, "attempts": 1, "latency_ms": 1637},
    ],
}


def _task(outcome: str = "error", status=None, reason: str | None = None) -> dict:
    return {
        "task_id": "demo-task",
        "provider": "Demo",
        "outcome": outcome,
        "http_status": status,
        "block_reason": reason,
        "attempts": 2,
        "latency_ms": 1000,
        "method": "requests",
    }


def test_success_and_out_of_stock_are_ok():
    assert classify_task(_task(outcome="success")) == "ok"
    assert classify_task(_task(outcome="out_of_stock", status=200)) == "ok"


def test_http_risk_statuses_are_external_block_never_repairable():
    for status in (403, 404, 429, 500, 502, 503, 504):
        assert classify_task(_task(status=status)) == "external_block"


def test_connection_and_challenge_reasons_are_external_block():
    for reason in ("connection_error", "challenge_detected", "request_failed"):
        assert classify_task(_task(reason=reason)) == "external_block"


def test_403_with_breakage_reason_prioritizes_external_block():
    # hostdare was http=403 with no_exact_same_card_offer; must NOT be repairable.
    task = _task(status=403, reason="no_exact_same_card_offer")
    assert classify_task(task) == "external_block"


def test_config_conflicts_are_external_retired():
    for reason in ("currency_or_period_conflict", "url_domain_mismatch", "multiple_matching_offers"):
        assert classify_task(_task(status=200, reason=reason)) == "external_retired"


def test_200_parse_failures_are_site_breakage():
    for reason in ("no_exact_same_card_offer", "detail_unverified", "browser_error"):
        assert classify_task(_task(status=200, reason=reason)) == "site_breakage"


def test_browser_error_without_status_is_unknown():
    assert classify_task(_task(reason="browser_error")) == "unknown"


def test_classifier_makes_no_network_calls(monkeypatch):
    """C8: the classifier must never touch the network or spawn processes."""
    import socket

    def deny_connect(*args, **kwargs):
        raise AssertionError("classifier must not open sockets")

    monkeypatch.setattr(socket, "socket", deny_connect)
    import subprocess as _subprocess

    monkeypatch.setattr(_subprocess, "run", deny_connect)
    tokens = load_plan_tokens(ROOT / "providers.yaml")
    report = build_report(REALISTIC_EVIDENCE, tokens)
    assert report["task_count"] == 14


def test_classifier_source_has_no_llm_or_http_imports():
    src = (ROOT / "scripts/classify_vps_failure.py").read_text(encoding="utf-8")
    assert "import requests" not in src
    assert "urllib" not in src
    assert "opencode" not in src
    assert "subprocess" not in src


def test_repair_runner_has_no_direct_network_imports():
    """C8-mirror: the repair runner must not import network libs directly;
    all egress goes through vps_monitor.verify."""
    src = (ROOT / "scripts/self_repair_runner_vps.py").read_text(encoding="utf-8")
    assert "import requests" not in src
    assert "import urllib" not in src
    assert "urlopen" not in src
    # Network egress is centralized in vps_monitor/verify.py
    verify_src = (ROOT / "vps_monitor/verify.py").read_text(encoding="utf-8")
    assert "import requests" in verify_src


def test_repair_runner_has_dry_run_and_trust_root_docstring():
    src = (ROOT / "scripts/self_repair_runner_vps.py").read_text(encoding="utf-8")
    assert "--dry-run" in src
    assert "Trust-root whitelist" in src
    assert "ALLOWED_PATCH_PATHS" in src


def test_report_conserves_counts_and_fields():
    tokens = load_plan_tokens(ROOT / "providers.yaml")
    report = build_report(REALISTIC_EVIDENCE, tokens)
    counts = report["counts"]
    assert report["task_count"] == 14
    assert sum(counts.values()) == 14
    assert counts["ok"] == 2
    assert counts["external_block"] == 8
    assert counts["site_breakage"] == 3
    assert counts["unknown"] == 1
    for task in report["tasks"]:
        assert task["classification"] in {
            "ok", "external_block", "external_retired", "site_breakage", "unknown",
        }
        assert task["evidence"]["outcome"]
        assert task["suggestion"]


def test_report_marks_403_parse_failures_as_external_not_breakage():
    tokens = load_plan_tokens(ROOT / "providers.yaml")
    report = build_report(REALISTIC_EVIDENCE, tokens)
    hostdare = [
        t for t in report["tasks"]
        if t["task_id"] in ("hostdare-cssd3", "hostdare-camd3")
    ]
    assert hostdare
    assert all(t["classification"] == "external_block" for t in hostdare)


def test_breakage_suggestion_and_structured_tokens():
    tokens = load_plan_tokens(ROOT / "providers.yaml")
    report = build_report(REALISTIC_EVIDENCE, tokens)
    buyvm = next(t for t in report["tasks"] if t["task_id"] == "buyvm-slice4096")
    assert buyvm["classification"] == "site_breakage"
    # C5: plan tokens must travel as a structured field, not inside prose.
    assert "SLICE 4096" in buyvm["plan_tokens"]
    assert "P0b" in buyvm["suggestion"] or "修复" in buyvm["suggestion"]
