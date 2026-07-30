"""Deterministic structural and product gates for public VPS batches."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import urlparse

from vps_monitor.contracts import ContractError, validate_envelope

def product_quality_gate(prices: Iterable[Mapping[str, object]]) -> bool:
    """Require eight distinct, live, explicitly available same-card offers."""
    valid_task_ids: set[str] = set()
    for row in prices:
        task_id = str(row.get("task_id") or row.get("id") or "")
        parsed = urlparse(str(row.get("product_url") or row.get("url") or ""))
        amount = row.get("amount")
        if (
            task_id
            and row.get("outcome", "success") == "success"
            and row.get("mode", "live") == "live"
            and bool(row.get("offer_id"))
            and row.get("availability") == "in_stock"
            and isinstance(amount, (int, float))
            and float(amount) > 0
            and bool(row.get("currency"))
            and row.get("billing_period") in {"monthly", "quarterly", "yearly"}
            and parsed.scheme == "https"
            and bool(parsed.hostname)
        ):
            valid_task_ids.add(task_id)
    return len(valid_task_ids) >= 8


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_file_manifest(
    root: Path,
    relative_paths: Iterable[str],
    *,
    batch_id: str,
    source_sha: str,
) -> dict[str, object]:
    files = {
        path: {"sha256": _sha256(root / path), "size": (root / path).stat().st_size}
        for path in sorted(set(relative_paths))
    }
    return {
        "schema_version": 4,
        "batch_id": batch_id,
        "source_sha": source_sha,
        "files": files,
    }


def verify_file_manifest(root: Path, manifest: Mapping[str, object]) -> list[str]:
    mismatches: list[str] = []
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        return ["manifest.files"]
    for name, metadata in files.items():
        path = root / str(name)
        expected = metadata.get("sha256") if isinstance(metadata, Mapping) else None
        if not path.is_file() or expected != _sha256(path):
            mismatches.append(str(name))
    return sorted(mismatches)


def audit_envelope(
    envelope: Mapping[str, object],
    expected_task_ids: Iterable[str],
) -> dict[str, object]:
    violations: list[dict[str, object]] = []
    expected = list(expected_task_ids)
    try:
        validate_envelope(envelope, expected)
        structure_status = "pass"
    except ContractError as exc:
        structure_status = "blocked"
        violations.append({"code": "structure_contract", "detail": str(exc)})
    statuses = envelope.get("statuses")
    prices = envelope.get("prices")
    rows = statuses if isinstance(statuses, list) else []
    price_rows = prices if isinstance(prices, list) else []
    prices_by_task = {
        str(row.get("task_id") or ""): row
        for row in price_rows
        if isinstance(row, Mapping)
    }
    for row in rows:
        if not isinstance(row, Mapping) or row.get("outcome") != "success":
            continue
        price = prices_by_task.get(str(row.get("task_id") or ""))
        source = urlparse(str((price or {}).get("url") or row.get("source_url") or ""))
        product = urlparse(str((price or {}).get("product_url") or ""))
        same_domain = bool(source.hostname and product.hostname) and (
            source.hostname == product.hostname
            or source.hostname.endswith(f".{product.hostname}")
            or product.hostname.endswith(f".{source.hostname}")
        )
        if (
            not isinstance(price, Mapping)
            or product.scheme != "https"
            or not same_domain
            or not price.get("offer_id")
            or price.get("availability") != "in_stock"
            or not price.get("currency")
            or price.get("billing_period") not in {"monthly", "quarterly", "yearly"}
        ):
            violations.append(
                {"code": "success_evidence_invalid", "task_id": row.get("task_id")}
            )
            structure_status = "blocked"
    providers = sorted(
        {
            str(row.get("provider"))
            for row in rows
            if isinstance(row, Mapping) and row.get("provider")
        }
    )
    for provider in providers:
        if not any(
            isinstance(row, Mapping)
            and row.get("provider") == provider
            and row.get("outcome") == "success"
            for row in rows
        ):
            violations.append({"code": f"provider_success_zero:{provider}"})
    product_status = (
        "pass"
        if structure_status == "pass" and product_quality_gate(price_rows)
        else "blocked"
    )
    fingerprint_input = {
        "schema_version": 4,
        "source_sha": envelope.get("source_sha"),
        "codes": sorted(str(row["code"]) for row in violations),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": 4,
        "batch_id": envelope.get("batch_id"),
        "source_sha": envelope.get("source_sha"),
        "structure_status": structure_status,
        "product_status": product_status,
        "status": "pass" if structure_status == product_status == "pass" else "blocked",
        "fingerprint": fingerprint,
        "violations": violations,
    }
