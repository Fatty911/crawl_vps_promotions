import hashlib

import pytest

from vps_monitor.contracts import (
    ContractError,
    build_envelope,
    validate_envelope,
    validate_live_envelope,
)


SOURCE_SHA = "d" * 40
CONFIG_SHA = hashlib.sha256(b"providers").hexdigest()


def _status(task_id: str, outcome: str = "blocked") -> dict:
    return {
        "task_id": task_id,
        "outcome": outcome,
        "attempts": 1,
        "started_at": "2026-07-30T00:00:00Z",
        "finished_at": "2026-07-30T00:00:01Z",
        "source_url": "https://provider.example/store",
        "final_url": "https://provider.example/store",
        "rejection_reason": "challenge" if outcome != "success" else None,
        "evidence_hash": "e" * 64,
        "parser_version": "vps-v4",
    }


def _envelope(mode: str = "live") -> tuple[dict, list[str]]:
    task_ids = [f"target-{index}" for index in range(14)]
    envelope = build_envelope(
        repo="crawl_vps_promotions",
        run_id="456",
        run_attempt="3",
        source_sha=SOURCE_SHA,
        config_sha256=CONFIG_SHA,
        started_at="2026-07-30T00:00:00Z",
        finished_at="2026-07-30T00:01:00Z",
        mode=mode,
        baseline_batch_id=None,
        expected_tasks=14,
        statuses=[_status(task_id) for task_id in task_ids],
        prices=[],
        evidence_sha256="f" * 64,
        audit_status="blocked",
    )
    return envelope, task_ids


def test_v4_envelope_has_batch_identity_and_exact_14_task_conservation():
    envelope, task_ids = _envelope()
    validate_envelope(envelope, task_ids)
    assert envelope["schema_version"] == 4
    assert envelope["batch_id"] == "crawl_vps_promotions:456:3"
    assert envelope["summary"]["blocked"] == 14
    assert sum(envelope["summary"][key] for key in ("success", "blocked", "rejected", "error", "out_of_stock")) == 14


def test_non_success_status_with_offer_fails_closed():
    envelope, task_ids = _envelope()
    envelope["statuses"][0]["offer"] = {"amount": 1}
    with pytest.raises(ContractError, match="non-success"):
        validate_envelope(envelope, task_ids)


def test_fixture_envelope_cannot_pass_live_validation():
    envelope, task_ids = _envelope(mode="fixture")
    validate_envelope(envelope, task_ids)
    with pytest.raises(ContractError, match="mode=live"):
        validate_live_envelope(envelope, task_ids)
