import json
import subprocess
import sys
from pathlib import Path

from vps_monitor.audit import (
    audit_envelope,
    build_file_manifest,
    product_quality_gate,
    verify_file_manifest,
)
from vps_monitor.contracts import build_envelope
from vps_monitor.monitor import (
    TargetResult,
    build_batch_envelope,
    build_live_evidence,
    build_public_data,
    evidence_sha256,
    load_config,
    load_targets,
    publish_site,
    quality_gate,
    structure_gate,
)
from scripts.post_deploy_verify import compare_manifests


def _price(task_id: str, availability: str = "in_stock") -> dict:
    return {
        "task_id": task_id,
        "outcome": "success",
        "mode": "live",
        "offer_id": f"offer:{task_id}",
        "availability": availability,
        "amount": 10.0,
        "currency": "USD",
        "billing_period": "monthly",
        "product_url": f"https://provider.example/order/{task_id}",
    }


def test_product_gate_rejects_eight_successes_when_one_is_out_of_stock():
    prices = [_price(f"task-{index}") for index in range(8)]
    prices[-1]["availability"] = "out_of_stock"

    assert product_quality_gate(prices) is False


def test_product_gate_requires_eight_distinct_explicitly_in_stock_task_ids():
    prices = [_price(f"task-{index}") for index in range(8)]
    assert product_quality_gate(prices) is True

    prices[-1]["task_id"] = prices[0]["task_id"]
    assert product_quality_gate(prices) is False


def _blocked_envelope() -> tuple[dict, list[str]]:
    task_ids = [f"target-{index}" for index in range(14)]
    statuses = [
        {
            "task_id": task_id,
            "provider": f"provider-{index}",
            "outcome": "blocked",
            "attempts": 1,
            "started_at": "2026-07-30T00:00:00Z",
            "finished_at": "2026-07-30T00:00:01Z",
            "source_url": "https://provider.example/",
            "final_url": "https://provider.example/",
            "rejection_reason": "challenge",
            "evidence_hash": "1" * 64,
            "parser_version": "vps-v4",
        }
        for index, task_id in enumerate(task_ids)
    ]
    return (
        build_envelope(
            repo="crawl_vps_promotions",
            run_id="2",
            run_attempt="1",
            source_sha="2" * 40,
            config_sha256="3" * 64,
            started_at="2026-07-30T00:00:00Z",
            finished_at="2026-07-30T00:01:00Z",
            mode="live",
            baseline_batch_id=None,
            expected_tasks=14,
            statuses=statuses,
            prices=[],
            evidence_sha256="4" * 64,
            audit_status="blocked",
        ),
        task_ids,
    )


def test_audit_is_always_emitted_when_product_gate_is_blocked():
    envelope, task_ids = _blocked_envelope()
    report = audit_envelope(envelope, task_ids)
    assert report["structure_status"] == "pass"
    assert report["product_status"] == "blocked"
    assert report["fingerprint"]
    assert any(row["code"].startswith("provider_success_zero:") for row in report["violations"])


def test_cross_domain_success_offer_blocks_structure():
    envelope, task_ids = _blocked_envelope()
    row = envelope["statuses"][0]
    row.update({"outcome": "success", "rejection_reason": None})
    envelope["prices"] = [
        {
            **_price(row["task_id"]),
            "url": "https://provider.example/store",
            "product_url": "https://evil.example/order/1",
        }
    ]
    envelope["summary"].update({"success": 1, "blocked": 13})
    report = audit_envelope(envelope, task_ids)
    assert report["structure_status"] == "blocked"
    assert any(item["code"] == "success_evidence_invalid" for item in report["violations"])


def test_file_manifest_detects_payload_tampering(tmp_path):
    payload = tmp_path / "data"
    payload.mkdir()
    (payload / "prices.json").write_text(json.dumps([]), encoding="utf-8")
    manifest = build_file_manifest(tmp_path, ["data/prices.json"], batch_id="batch-2", source_sha="5" * 40)
    assert verify_file_manifest(tmp_path, manifest) == []
    (payload / "prices.json").write_text("[1]", encoding="utf-8")
    assert verify_file_manifest(tmp_path, manifest) == ["data/prices.json"]


def test_post_deploy_manifest_comparison_binds_batch_sha_and_files():
    expected = {
        "schema_version": 4,
        "batch_id": "batch-2",
        "source_sha": "d" * 40,
        "files": {"data/prices.json": {"sha256": "e" * 64, "size": 2}},
    }
    assert compare_manifests(expected, dict(expected)) == []
    actual = {**expected, "files": {}}
    assert compare_manifests(expected, actual) == ["files"]


def test_fixture_output_cannot_pass_structure_or_product_cli_gates(tmp_path):
    root = Path(__file__).parents[1]
    site = tmp_path / "site"
    built = subprocess.run(
        [
            sys.executable,
            "-m",
            "vps_monitor.monitor",
            "--output",
            "--site-dir",
            str(site),
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr
    for gate in ("--structure-gate", "--quality-gate"):
        checked = subprocess.run(
            [
                sys.executable,
                "-m",
                "vps_monitor.monitor",
                gate,
                "--site-dir",
                str(site),
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )
        assert checked.returncode == 1


def test_live_evidence_hash_is_bound_to_published_file_and_rejects_tampering(tmp_path, monkeypatch):
    targets = load_targets(load_config())
    results = [
        TargetResult(
            target=target,
            outcome="blocked",
            offer=None,
            http_status=403,
            final_url=target.url,
            method="requests",
            block_reason="http_403",
            attempts=1,
            latency_ms=10,
            checked_at="2026-07-30T00:00:00+00:00",
        )
        for target in targets
    ]
    public = build_public_data(results, targets, mode="live")
    evidence = build_live_evidence(results, mode="live")
    envelope = build_batch_envelope(
        public,
        targets,
        mode="live",
        run_id="2",
        run_attempt="1",
        source_sha="a" * 40,
        config_sha256="b" * 64,
        started_at="2026-07-30T00:00:00Z",
        finished_at="2026-07-30T00:01:00Z",
        evidence=evidence,
    )
    report = audit_envelope(envelope, [target.id for target in targets])
    publish_site(public, tmp_path, evidence=evidence, envelope=envelope, audit_report=report)
    assert structure_gate(tmp_path, [target.id for target in targets])
    monkeypatch.setattr("vps_monitor.monitor.product_quality_gate", lambda prices: True)
    assert quality_gate(tmp_path)

    tampered = json.loads((tmp_path / "data/live-evidence.json").read_text(encoding="utf-8"))
    tampered["tasks"][0]["latency_ms"] += 1
    assert evidence_sha256(tampered) != envelope["evidence_sha256"]
    (tmp_path / "data/live-evidence.json").write_text(
        json.dumps(tampered, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    assert not structure_gate(tmp_path, [target.id for target in targets])
    assert not quality_gate(tmp_path)
