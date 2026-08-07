from __future__ import annotations

import argparse
from concurrent.futures import CancelledError, ThreadPoolExecutor
from contextlib import contextmanager
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from vps_monitor.audit import (
    audit_envelope,
    build_file_manifest,
    product_quality_gate as audited_product_quality_gate,
    verify_file_manifest,
)
from vps_monitor.contracts import (
    ContractError,
    OUTCOMES,
    build_envelope,
    validate_envelope,
    validate_live_envelope,
)
from vps_monitor.state import merge_history_events, write_state_directory

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - requests fallback still works locally
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "providers.yaml"
SITE_DIR = ROOT / "site"
STATE_DIR = ROOT / "state"
WEB_DIR = ROOT / "web"

LIVE_EVIDENCE_FIELDS = frozenset(
    {
        "task_id",
        "provider",
        "http_status",
        "final_url",
        "method",
        "outcome",
        "block_reason",
        "attempts",
        "latency_ms",
    }
)
LIVE_EVIDENCE_METHODS = ("requests", "browser", "circuit", "other")
LIVE_EVIDENCE_REASON_RE = re.compile(r"^[a-z0-9_.:-]{1,80}$")
LIVE_EVIDENCE_REASON_CODES = frozenset(
    {
        "request_failed",
        "http_status",
        "connection_error",
        "browser_error",
        "challenge_detected",
        "url_domain_mismatch",
        "provider_circuit_open",
        "no_exact_same_card_offer",
        "multiple_matching_offers",
        "currency_or_period_conflict",
        "detail_unverified",
        "sold_out",
        "playwright_not_installed",
        "offline_smoke_no_network",
        "unclassified",
    }
)


@dataclass(frozen=True)
class PlanTarget:
    id: str
    provider: str
    plan_name: str
    plan_tokens: tuple[str, ...]
    url: str
    region: str
    provider_claimed_routes: tuple[str, ...]
    expected_domains: tuple[str, ...] = ()
    priority: int = 100
    expected_currencies: tuple[str, ...] = ()
    expected_billing_periods: tuple[str, ...] = ()
    lifecycle: str = "active"
    reliability: float = 6.0
    oversell: str = "medium"
    reliability_note: str = ""
    specs: tuple = ()


@dataclass(frozen=True)
class Offer:
    plan_name: str
    amount: float
    currency: str
    billing_period: str
    monthly_amount: float
    availability: str
    price_raw: str
    specs: str
    provider_claimed_routes: tuple[str, ...]
    parsed_route_evidence: tuple[str, ...]
    measured_routes: None = None
    offer_id: str = ""
    product_url: str = ""


@dataclass(frozen=True)
class ParseResult:
    outcome: str
    offer: Offer | None = None
    block_reason: str | None = None


@dataclass(frozen=True)
class HTTPFetch:
    markup: str
    outcome: str
    http_status: int | None
    final_url: str | None
    method: str
    block_reason: str | None
    attempts: int
    latency_ms: int


@dataclass(frozen=True)
class TargetResult:
    target: PlanTarget
    outcome: str
    offer: Offer | None
    http_status: int | None
    final_url: str | None
    method: str
    block_reason: str | None
    attempts: int
    latency_ms: int
    checked_at: str


class RequestLimiter:
    def __init__(self, global_limit: int = 4, per_host_limit: int = 1):
        self._global = threading.BoundedSemaphore(global_limit)
        self._per_host_limit = per_host_limit
        self._hosts: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _acquire(semaphore: threading.BoundedSemaphore, cancelled: threading.Event | None) -> None:
        while True:
            if cancelled is not None and cancelled.is_set():
                raise CancelledError()
            if semaphore.acquire(timeout=0.05):
                return

    @contextmanager
    def slot(self, url: str, cancelled: threading.Event | None = None):
        host = (urlparse(url).hostname or "").lower()
        with self._lock:
            host_slot = self._hosts.setdefault(host, threading.BoundedSemaphore(self._per_host_limit))
        global_acquired = False
        host_acquired = False
        try:
            self._acquire(self._global, cancelled)
            global_acquired = True
            self._acquire(host_slot, cancelled)
            host_acquired = True
            yield
        finally:
            if host_acquired:
                host_slot.release()
            if global_acquired:
                self._global.release()


class BrowserLimiter:
    def __init__(self, global_limit: int = 1):
        self._slot = threading.BoundedSemaphore(global_limit)

    @contextmanager
    def slot(self):
        self._slot.acquire()
        try:
            yield
        finally:
            self._slot.release()


class ProviderCircuitBreaker:
    def __init__(self, threshold: int = 2):
        self.threshold = threshold
        self._state: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()

    def allow(self, provider: str) -> bool:
        with self._lock:
            return self._state.get(provider, ("", 0))[1] < self.threshold

    def record(self, provider: str, reason: str | None) -> None:
        with self._lock:
            if not reason:
                self._state.pop(provider, None)
                return
            old_reason, count = self._state.get(provider, ("", 0))
            self._state[provider] = (
                reason,
                count + 1 if old_reason == reason else 1,
            )


def request_with_budget(
    url: str,
    *,
    getter: Any = requests.get,
    sleep: Any = time.sleep,
    monotonic: Any = time.monotonic,
    total_budget: float = 45,
) -> HTTPFetch:
    started = monotonic()
    attempts = 0
    last_reason = "request_failed"
    for attempt in range(3):
        remaining = total_budget - (monotonic() - started)
        if remaining <= 0:
            break
        attempts = attempt + 1
        if remaining >= 20:
            timeout = (5, 15)
        else:
            connect_timeout = remaining / 2
            timeout = (connect_timeout, remaining - connect_timeout)
        try:
            _proxy_url = os.getenv("HTTP_PROXY") or os.getenv("http_proxy") or ""
            _proxies = {"http": _proxy_url, "https": os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or _proxy_url} if _proxy_url else None
            response = getter(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=timeout,
                proxies=_proxies,
                allow_redirects=True,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_reason = f"connection:{type(exc).__name__}"
            if attempt < 2 and monotonic() - started < total_budget:
                sleep(0.25 * (attempt + 1))
                continue
            break
        status = int(response.status_code)
        final_url = str(response.url)
        if status in {408, 429} or 500 <= status <= 599:
            last_reason = f"http_{status}"
            if attempt < 2 and monotonic() - started < total_budget:
                sleep(0.25 * (attempt + 1))
                continue
            return HTTPFetch(
                str(response.text), "error", status, final_url, "requests",
                last_reason, attempts, int((monotonic() - started) * 1000),
            )
        if status in {401, 403}:
            return HTTPFetch(
                str(response.text), "blocked", status, final_url, "requests",
                f"http_{status}", attempts, int((monotonic() - started) * 1000),
            )
        visible = BeautifulSoup(str(response.text), "html.parser").get_text(" ", strip=True)
        for word in CHALLENGE_WORDS:
            if word.casefold() in visible.casefold():
                return HTTPFetch(
                    str(response.text), "blocked", status, final_url, "requests",
                    word, attempts, int((monotonic() - started) * 1000),
                )
        return HTTPFetch(
            str(response.text), "success" if status < 400 else "error", status,
            final_url, "requests", None if status < 400 else f"http_{status}",
            attempts, int((monotonic() - started) * 1000),
        )
    return HTTPFetch(
        "", "error", None, url, "requests", last_reason, attempts,
        int((monotonic() - started) * 1000),
    )


def _is_js_shell(markup: str) -> bool:
    visible = BeautifulSoup(markup, "html.parser").get_text(" ", strip=True).casefold()
    return (
        len(visible) < 30
        or "enable javascript" in visible
        or "请启用javascript" in visible
    )


def fetch_target(
    target: PlanTarget,
    *,
    request_fn: Any,
    browser_fn: Any,
) -> TargetResult:
    checked_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    requested = request_fn(target.url)
    if not target_url_allowed(target, requested.final_url):
        return TargetResult(
            target, "rejected", None, requested.http_status, requested.final_url,
            "requests", "url_domain_mismatch", requested.attempts,
            requested.latency_ms, checked_at,
        )
    if requested.outcome == "blocked":
        return TargetResult(
            target, "blocked", None, requested.http_status, requested.final_url,
            "requests", requested.block_reason, requested.attempts,
            requested.latency_ms, checked_at,
        )
    if requested.outcome == "error":
        return TargetResult(
            target, "error", None, requested.http_status, requested.final_url,
            "requests", requested.block_reason, requested.attempts,
            requested.latency_ms, checked_at,
        )
    parsed = parse_offer(requested.markup, target)
    if parsed.outcome == "success":
        return TargetResult(
            target, "success", parsed.offer, requested.http_status,
            requested.final_url, "requests", None, requested.attempts,
            requested.latency_ms, checked_at,
        )
    rendered = browser_fn(target.url)
    if not target_url_allowed(target, rendered.final_url):
        return TargetResult(
            target, "rejected", None, rendered.http_status, rendered.final_url,
            "browser", "url_domain_mismatch",
            requested.attempts + rendered.attempts,
            requested.latency_ms + rendered.latency_ms, checked_at,
        )
    rendered_parse = parse_offer(rendered.markup, target)
    if rendered.outcome == "success" and rendered_parse.outcome == "success":
        return TargetResult(
            target, "success", rendered_parse.offer, rendered.http_status,
            rendered.final_url, "browser", None,
            requested.attempts + rendered.attempts,
            requested.latency_ms + rendered.latency_ms, checked_at,
        )
    if rendered_parse.outcome in {"rejected", "out_of_stock"}:
        outcome = rendered_parse.outcome
    else:
        outcome = "blocked" if rendered.outcome == "blocked" or rendered_parse.outcome == "blocked" else "error"
    return TargetResult(
        target, outcome, None, rendered.http_status, rendered.final_url,
        "browser", rendered.block_reason or rendered_parse.block_reason or "no_exact_same_card_offer",
        requested.attempts + rendered.attempts,
        requested.latency_ms + rendered.latency_ms, checked_at,
    )


def crawl_targets(
    targets: list[PlanTarget],
    *,
    request_fn: Any = request_with_budget,
    browser_fn: Any,
) -> list[TargetResult]:
    request_limiter = RequestLimiter(global_limit=4, per_host_limit=1)
    browser_limiter = BrowserLimiter(global_limit=1)
    breaker = ProviderCircuitBreaker(threshold=2)

    def run(target: PlanTarget) -> TargetResult:
        if not breaker.allow(target.provider):
            return TargetResult(
                target, "blocked", None, None, target.url, "circuit",
                "provider_circuit_open", 0, 0,
                dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            )
        def limited_request(url: str) -> HTTPFetch:
            with request_limiter.slot(url):
                return request_fn(url)

        def limited_browser(url: str) -> HTTPFetch:
            with browser_limiter.slot():
                return browser_fn(url)

        try:
            result = fetch_target(
                target,
                request_fn=limited_request,
                browser_fn=limited_browser,
            )
            breaker.record(
                target.provider,
                result.block_reason if result.outcome == "blocked" else None,
            )
            return result
        except Exception as exc:
            return TargetResult(
                target, "error", None, None, target.url, "requests",
                f"exception:{type(exc).__name__}", 1, 0,
                dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            )

    scheduled = prioritize_targets(targets)
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(run, scheduled))


def prioritize_targets(targets: list[PlanTarget]) -> list[PlanTarget]:
    return sorted(targets, key=lambda target: (target.priority, target.id))


def validate_round(
    results: list[TargetResult],
    targets: list[PlanTarget],
) -> dict[str, int]:
    expected = [target.id for target in targets]
    actual = [result.target.id for result in results]
    if len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise ValueError("round must contain each target exactly once")
    counts = {"success": 0, "blocked": 0, "rejected": 0, "error": 0, "out_of_stock": 0}
    for result in results:
        if result.outcome not in counts:
            raise ValueError(f"invalid outcome: {result.outcome}")
        if result.outcome != "success" and result.offer is not None:
            raise ValueError("non-success results must not contain an offer")
        if result.outcome == "success" and result.offer is None:
            raise ValueError("success result requires a same-card offer")
        if result.outcome == "success" and (
            not result.offer.offer_id
            or not result.offer.product_url
            or not target_url_allowed(result.target, result.offer.product_url)
            or result.offer.availability != "in_stock"
            or result.offer.currency not in result.target.expected_currencies
            or result.offer.billing_period not in result.target.expected_billing_periods
        ):
            raise ValueError("success requires a specific in-stock configured offer identity")
        counts[result.outcome] += 1
    return {
        "attempted": len(results),
        **counts,
        "providers": len({target.provider for target in targets}),
    }


def product_quality_gate(prices: list[dict[str, Any]]) -> bool:
    return audited_product_quality_gate(prices)


def _safe_evidence_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlparse(str(value))
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if scheme not in {"http", "https"} or not hostname:
        return None
    hostname = hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"{scheme}://{hostname}"


def _safe_evidence_reason(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = str(value).strip().lower()
    if not candidate:
        return None
    if candidate in LIVE_EVIDENCE_REASON_CODES:
        return candidate
    if re.fullmatch(r"http_\d{3}", candidate):
        return "http_status"
    if candidate.startswith("connection:"):
        return "connection_error"
    if candidate.startswith("browser:"):
        return "browser_error"
    if candidate in {word.casefold() for word in CHALLENGE_WORDS}:
        return "challenge_detected"
    return "unclassified"


def _safe_evidence_method(value: str | None) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in LIVE_EVIDENCE_METHODS else "other"


def _safe_evidence_outcome(value: str | None) -> str:
    candidate = str(value or "").strip()
    return candidate if candidate in OUTCOMES else "error"


def _safe_evidence_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _safe_evidence_status(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599 else None


def _evidence_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    outcome_counts = {outcome: 0 for outcome in OUTCOMES}
    method_counts = {method: 0 for method in LIVE_EVIDENCE_METHODS}
    for row in rows:
        outcome_counts[row["outcome"]] += 1
        method_counts[row["method"]] += 1
    return {
        "task_count": len(rows),
        "provider_count": len({row["provider"] for row in rows}),
        "outcome_counts": outcome_counts,
        "method_counts": method_counts,
    }


def _build_evidence_document(rows: list[dict[str, Any]], *, mode: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "mode": mode,
        "tasks": rows,
        "summary": _evidence_summary(rows),
    }


def build_live_evidence(
    results: list[TargetResult],
    *,
    mode: str,
) -> dict[str, Any]:
    rows = [
        {
            "task_id": result.target.id,
            "provider": result.target.provider,
            "http_status": _safe_evidence_status(result.http_status),
            "final_url": _safe_evidence_url(result.final_url),
            "method": _safe_evidence_method(result.method),
            "outcome": _safe_evidence_outcome(result.outcome),
            "block_reason": _safe_evidence_reason(result.block_reason),
            "attempts": _safe_evidence_int(result.attempts),
            "latency_ms": _safe_evidence_int(result.latency_ms),
        }
        for result in results
    ]
    return _build_evidence_document(rows, mode=mode)


def _evidence_from_public(public: dict[str, Any]) -> dict[str, Any]:
    statuses = public.get("status") or []
    rows = [
        {
            "task_id": str(status.get("task_id") or status.get("id") or ""),
            "provider": str(status.get("provider") or ""),
            "http_status": _safe_evidence_status(status.get("http_status")),
            "final_url": _safe_evidence_url(status.get("final_url")),
            "method": _safe_evidence_method(status.get("method")),
            "outcome": _safe_evidence_outcome(status.get("outcome")),
            "block_reason": _safe_evidence_reason(status.get("block_reason")),
            "attempts": _safe_evidence_int(status.get("attempts")),
            "latency_ms": _safe_evidence_int(status.get("latency_ms")),
        }
        for status in statuses
        if isinstance(status, dict)
    ]
    mode = str(statuses[0].get("mode") or "fixture") if statuses else "fixture"
    return _build_evidence_document(rows, mode=mode)


def validate_live_evidence(
    evidence: dict[str, Any],
    expected_task_ids: list[str],
    *,
    expected_mode: str = "live",
) -> None:
    if not isinstance(evidence, dict) or set(evidence) != {
        "schema_version",
        "mode",
        "tasks",
        "summary",
    }:
        raise ContractError("live evidence has an unexpected schema")
    if evidence.get("schema_version") != 1 or evidence.get("mode") != expected_mode:
        raise ContractError("live evidence schema or mode is invalid")
    rows = evidence.get("tasks")
    if not isinstance(rows, list) or [row.get("task_id") for row in rows if isinstance(row, dict)] != expected_task_ids:
        raise ContractError("live evidence task order is not exactly conserved")
    for row in rows:
        if not isinstance(row, dict) or set(row) != LIVE_EVIDENCE_FIELDS:
            raise ContractError("live evidence row fields are not allowlisted")
        if not isinstance(row["task_id"], str) or not row["task_id"]:
            raise ContractError("live evidence task_id is invalid")
        if not isinstance(row["provider"], str) or not row["provider"]:
            raise ContractError("live evidence provider is invalid")
        if _safe_evidence_status(row["http_status"]) != row["http_status"]:
            raise ContractError("live evidence http_status is invalid")
        if row["final_url"] is not None and _safe_evidence_url(row["final_url"]) != row["final_url"]:
            raise ContractError("live evidence final_url is not a safe origin")
        if row["method"] not in LIVE_EVIDENCE_METHODS or row["outcome"] not in OUTCOMES:
            raise ContractError("live evidence enum is invalid")
        if row["block_reason"] is not None and (
            not isinstance(row["block_reason"], str)
            or row["block_reason"] not in LIVE_EVIDENCE_REASON_CODES
        ):
            raise ContractError("live evidence block_reason is invalid")
        if _safe_evidence_int(row["attempts"]) != row["attempts"] or _safe_evidence_int(row["latency_ms"]) != row["latency_ms"]:
            raise ContractError("live evidence numeric field is invalid")
    if evidence.get("summary") != _evidence_summary(rows):
        raise ContractError("live evidence summary does not conserve observations")


def evidence_sha256(evidence: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _specs_dict(target: PlanTarget) -> dict[str, float]:
    return {key: value for key, value in target.specs}


def value_score(target: PlanTarget, monthly_amount: float | None) -> float | None:
    """Value-for-money score 1.0-10.0 (one decimal).

    specs_index weights CPU/RAM/storage/bandwidth relative to a reference
    box; value = specs_index / monthly USD, mapped to 1..10. Returns None
    when no monthly price is available (non-success outcome).
    """
    if monthly_amount is None or monthly_amount <= 0:
        return None
    specs = _specs_dict(target)
    cpu = min(float(specs.get("cpu", 1)), 4.0) / 4.0
    ram = min(float(specs.get("ram_gb", 1)), 16.0) / 16.0
    storage = min(float(specs.get("storage_gb", 10)), 200.0) / 200.0
    bandwidth = min(float(specs.get("bandwidth_gbps", 1)), 10.0) / 10.0
    index = cpu * 0.3 + ram * 0.4 + storage * 0.2 + bandwidth * 0.1
    if index <= 0:
        return None
    raw = index / monthly_amount * 100.0
    # Map: >= 5 USD/unit -> 10, 0.5 USD/unit -> ~1 (log scale, clamped)
    import math

    score = 1.0 + 9.0 * (math.log10(raw + 0.1) + 1.0) / 2.0
    return round(max(1.0, min(10.0, score)), 1)


def build_public_data(
    results: list[TargetResult],
    targets: list[PlanTarget],
    existing_history: list[dict[str, Any]] | None = None,
    *,
    mode: str = "live",
) -> dict[str, Any]:
    summary = validate_round(results, targets)
    by_id = {result.target.id: result for result in results}
    statuses: list[dict[str, Any]] = []
    prices: list[dict[str, Any]] = []
    for target in targets:
        result = by_id[target.id]
        offer = result.offer
        evidence_hash = hashlib.sha256(
            json.dumps(
                {
                    "task_id": target.id,
                    "outcome": result.outcome,
                    "http_status": result.http_status,
                    "final_url": result.final_url,
                    "method": result.method,
                    "reason": result.block_reason,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        status = {
            "task_id": target.id,
            "id": target.id,
            "provider": target.provider,
            "plan_name": target.plan_name,
            "url": target.url,
            "source_url": target.url,
            "region": target.region,
            "mode": mode,
            "outcome": result.outcome,
            "http_status": result.http_status,
            "method": result.method,
            "attempts": result.attempts,
            "latency_ms": result.latency_ms,
            "availability": offer.availability if offer else None,
            "amount": offer.amount if offer else None,
            "currency": offer.currency if offer else None,
            "billing_period": offer.billing_period if offer else None,
            "monthly_amount": offer.monthly_amount if offer else None,
            "price_raw": offer.price_raw if offer else None,
            "provider_claimed_routes": list(target.provider_claimed_routes),
            "parsed_route_evidence": list(offer.parsed_route_evidence) if offer else [],
            "measured_routes": None,
            "final_url": result.final_url,
            "product_url": offer.product_url if offer else None,
            "offer_id": offer.offer_id if offer else None,
            "block_reason": result.block_reason,
            "rejection_reason": result.block_reason,
            "checked_at": result.checked_at,
            "started_at": result.checked_at,
            "finished_at": result.checked_at,
            "evidence_hash": evidence_hash,
            "parser_version": "vps-v4",
            "reliability": target.reliability,
            "oversell": target.oversell,
            "reliability_note": target.reliability_note,
            "specs": _specs_dict(target),
            "value_score": value_score(target, offer.monthly_amount if offer else None),
        }
        statuses.append(status)
        if offer:
            prices.append(
                {
                    "task_id": target.id,
                    "id": target.id,
                    "provider": target.provider,
                    "plan_name": target.plan_name,
                    "url": target.url,
                    "region": target.region,
                    "amount": offer.amount,
                    "currency": offer.currency,
                    "billing_period": offer.billing_period,
                    "monthly_amount": offer.monthly_amount,
                    "availability": offer.availability,
                    "outcome": "success",
                    "mode": mode,
                    "offer_id": offer.offer_id,
                    "product_url": offer.product_url,
                    "price_raw": offer.price_raw,
                    "provider_claimed_routes": list(offer.provider_claimed_routes),
                    "parsed_route_evidence": list(offer.parsed_route_evidence),
                    "measured_routes": None,
                    "checked_at": result.checked_at,
                    "observed_at": result.checked_at,
                }
            )
            prices[-1]["event_id"] = hashlib.sha256(
                json.dumps(
                    [
                        target.id,
                        offer.offer_id,
                        offer.amount,
                        result.checked_at,
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
    history_rows: list[dict[str, Any]] = []
    for row in existing_history or []:
        observed_at = row.get("observed_at") or row.get("checked_at")
        event_id = row.get("event_id")
        if observed_at and event_id:
            history_rows.append(dict(row, observed_at=observed_at))
    now = max((result.checked_at for result in results), default=dt.datetime.now(dt.UTC).isoformat())
    history = merge_history_events(history_rows, prices, now=now)
    return {
        "status": statuses,
        "prices": prices,
        "price_history": history,
        "summary": summary,
        "product_gate": product_quality_gate(prices),
    }


def _render_v3_html(public: dict[str, Any]) -> str:
    alert = (
        ""
        if public["product_gate"]
        else "<div class='alert'>本轮有效同卡套餐少于 8/14</div>"
    )
    rows = []
    for row in public["status"]:
        price = (
            html.escape(str(row["price_raw"]))
            if row["outcome"] == "success"
            else "—"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(row['provider'])}</td>"
            f"<td><a href='{html.escape(row['url'])}'>{html.escape(row['plan_name'])}</a></td>"
            f"<td>{row['outcome']}</td><td>{price}</td>"
            f"<td>{html.escape(str(row['availability'] or ''))}</td>"
            f"<td>{html.escape(str(row['http_status'] or ''))}</td>"
            f"<td>{html.escape(row['method'])}</td><td>{row['attempts']}</td>"
            f"<td>{row['latency_ms']}</td><td>{html.escape(row['checked_at'])}</td>"
            "</tr>"
        )
    summary = public["summary"]
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>VPS 同卡套餐状态</title>"
        "<style>body{font-family:sans-serif;margin:24px}.alert{background:#fee2e2;padding:14px;"
        "font-weight:700}table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:6px}</style>"
        "</head><body><h1>VPS 同卡套餐状态</h1>"
        f"{alert}<p>product_gate={'pass' if public['product_gate'] else 'fail'}</p>"
        f"<p>attempted={summary['attempted']} success={summary['success']} "
        f"blocked={summary['blocked']} error={summary['error']}</p>"
        "<table><thead><tr><th>服务商</th><th>套餐</th><th>结果</th><th>原价/周期</th>"
        "<th>库存</th><th>HTTP</th><th>方法</th><th>尝试</th><th>延迟 ms</th><th>检查时间</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></body></html>"
    )


def publish_site(
    public: dict[str, Any],
    site_dir: Path = SITE_DIR,
    *,
    evidence: dict[str, Any] | None = None,
    envelope: dict[str, Any] | None = None,
    audit_report: dict[str, Any] | None = None,
    deals: dict[str, Any] | None = None,
) -> None:
    staging = site_dir.parent / f".{site_dir.name}-staging"
    if staging.exists():
        shutil.rmtree(staging)
    data_dir = staging / "data"
    data_dir.mkdir(parents=True)
    for name, key in (
        ("status.json", "status"),
        ("prices.json", "prices"),
        ("price_history.json", "price_history"),
        ("summary.json", "summary"),
    ):
        data_dir.joinpath(name).write_text(
            json.dumps(public[key], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    if deals is not None:
        data_dir.joinpath("deals.json").write_text(
            json.dumps(deals, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    evidence = evidence if evidence is not None else _evidence_from_public(public)
    expected_task_ids = [
        str(row.get("task_id") or row.get("id") or "")
        for row in public.get("status", [])
        if isinstance(row, dict)
    ]
    evidence_mode = str(public.get("status", [{}])[0].get("mode") or "fixture")
    validate_live_evidence(evidence, expected_task_ids, expected_mode=evidence_mode)
    data_dir.joinpath("live-evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if envelope is not None:
        data_dir.joinpath("batch.json").write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    report = audit_report or {
        "schema_version": 4,
        "structure_status": "blocked",
        "product_status": "blocked",
        "status": "blocked",
        "fingerprint": hashlib.sha256(b"local-unverified").hexdigest(),
        "violations": [{"code": "missing_batch_envelope"}],
    }
    staging.joinpath("audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for asset in ("index.html", "app.js", "styles.css"):
        shutil.copy2(WEB_DIR / asset, staging / asset)
    staging.joinpath("CNAME").write_text("vps.jiucai.eu.org\n", encoding="utf-8")
    relative_paths = [
        str(path.relative_to(staging)).replace("\\", "/")
        for path in staging.rglob("*")
        if path.is_file()
    ]
    metadata = envelope or {
        "batch_id": "crawl_vps_promotions:local:0",
        "source_sha": "0" * 40,
        "mode": "fixture",
        "run_id": "local",
        "run_attempt": "0",
        "config_sha256": "0" * 64,
    }
    manifest = build_file_manifest(
        staging,
        relative_paths,
        batch_id=str(metadata["batch_id"]),
        source_sha=str(metadata["source_sha"]),
    )
    manifest.update(
        {
            "mode": metadata.get("mode"),
            "run_id": metadata.get("run_id"),
            "run_attempt": metadata.get("run_attempt"),
            "config_sha256": metadata.get("config_sha256"),
            "audit_status": report.get("status"),
        }
    )
    staging.joinpath("manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    data_dir.joinpath("manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    data_dir.joinpath("audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if site_dir.exists():
        shutil.rmtree(site_dir)
    staging.replace(site_dir)


def build_batch_envelope(
    public: dict[str, Any],
    targets: list[PlanTarget],
    *,
    mode: str,
    run_id: str,
    run_attempt: str,
    source_sha: str,
    config_sha256: str,
    started_at: str,
    finished_at: str,
    baseline_batch_id: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = evidence if evidence is not None else _evidence_from_public(public)
    expected_task_ids = [target.id for target in targets]
    validate_live_evidence(evidence, expected_task_ids, expected_mode=mode)
    envelope = build_envelope(
        repo="crawl_vps_promotions",
        run_id=run_id,
        run_attempt=run_attempt,
        source_sha=source_sha,
        config_sha256=config_sha256,
        started_at=started_at,
        finished_at=finished_at,
        mode=mode,
        baseline_batch_id=baseline_batch_id,
        expected_tasks=len(targets),
        statuses=public["status"],
        prices=public["prices"],
        evidence_sha256=evidence_sha256(evidence),
        audit_status="pass" if public["product_gate"] else "blocked",
    )
    validate_envelope(envelope, [target.id for target in targets])
    return envelope


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if (
        not isinstance(config, dict)
        or not isinstance(config.get("config_revision"), int)
        or not isinstance(config.get("retired_task_ids"), list)
        or not isinstance(config.get("targets"), list)
    ):
        raise ValueError("targets list is empty or missing")
    targets = config["targets"]
    if not targets:
        raise ValueError("targets list is empty or missing")
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for index, target in enumerate(targets):
        if not isinstance(target, dict):
            raise ValueError(f"target at index {index} is not a mapping")
        for field in (
            "id",
            "provider",
            "plan_name",
            "plan_tokens",
            "url",
            "expected_domains",
            "priority",
            "expected_currencies",
            "expected_billing_periods",
            "lifecycle",
        ):
            if not target.get(field):
                raise ValueError(f"target at index {index} missing required field: {field}")
        target_id = str(target["id"])
        url = str(target["url"])
        if target_id in seen_ids:
            raise ValueError(f"duplicate target id: {target_id}")
        if url in seen_urls:
            raise ValueError(f"duplicate target url: {url}")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"target url must be http(s): {url}")
        if "aff" in (url.split("?", 1)[1].lower() if "?" in url else ""):
            raise ValueError(f"affiliate URL forbidden: {url}")
        seen_ids.add(target_id)
        seen_urls.add(url)
    return config


def load_targets(config: dict[str, Any]) -> list[PlanTarget]:
    return [
        PlanTarget(
            id=str(row["id"]),
            provider=str(row["provider"]),
            plan_name=str(row["plan_name"]),
            plan_tokens=tuple(str(token) for token in row["plan_tokens"]),
            url=str(row["url"]),
            region=str(row.get("region", "")),
            provider_claimed_routes=tuple(str(route) for route in row.get("provider_claimed_routes", [])),
            expected_domains=tuple(
                str(domain).lower()
                for domain in row["expected_domains"]
                if str(domain)
            ),
            priority=int(row["priority"]),
            expected_currencies=tuple(
                str(currency).upper() for currency in row["expected_currencies"]
            ),
            expected_billing_periods=tuple(
                str(period) for period in row["expected_billing_periods"]
            ),
            lifecycle=str(row["lifecycle"]),
            reliability=float(row.get("reliability", 6.0) or 6.0),
            oversell=str(row.get("oversell", "medium") or "medium"),
            reliability_note=str(row.get("reliability_note", "") or ""),
            specs=tuple(
                (str(key), float(value))
                for key, value in (row.get("specs") or {}).items()
                if value is not None
            ),
        )
        for row in config["targets"]
    ]


CHALLENGE_WORDS = (
    "captcha",
    "验证码",
    "安全验证",
    "access denied",
    "cloudflare ray id",
    "checking your browser",
)
ROUTE_WORDS = ("CN2 GIA", "AS9929", "CN2", "CUG", "CMIN2", "CMI", "China Unicom", "China Mobile", "BGP")


def _matches_target(text: str, target: PlanTarget) -> bool:
    normalized = re.sub(r"[\W_]+", "", text, flags=re.UNICODE).casefold()
    return all(
        re.sub(r"[\W_]+", "", token, flags=re.UNICODE).casefold() in normalized
        for token in target.plan_tokens
    )


def _parse_price(text: str) -> tuple[float, str, str] | None:
    patterns = (
        (re.compile(r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)\s*(?:USD)?", re.I), "USD"),
        (re.compile(r"([0-9]+(?:\.[0-9]{1,2})?)\s*USD", re.I), "USD"),
        (re.compile(r"(?:¥|￥|RMB\s*)([0-9]+(?:\.[0-9]{1,2})?)", re.I), "CNY"),
        (re.compile(r"([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|CNY)", re.I), "CNY"),
        (re.compile(r"€\s*([0-9]+(?:\.[0-9]{1,2})?)"), "EUR"),
        (re.compile(r"£\s*([0-9]+(?:\.[0-9]{1,2})?)"), "GBP"),
        (re.compile(r"([0-9]+(?:\.[0-9]{1,2})?)\s*CAD", re.I), "CAD"),
    )
    for pattern, currency in patterns:
        match = pattern.search(text)
        if match:
            amount = float(match.group(1))
            if amount > 0:
                return amount, currency, match.group(0)
    return None


def _parse_bound_price_period(
    text: str,
) -> tuple[float, str, str, str] | None:
    patterns = (
        (re.compile(r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)\s*(?:USD)?", re.I), "USD"),
        (re.compile(r"([0-9]+(?:\.[0-9]{1,2})?)\s*USD", re.I), "USD"),
        (re.compile(r"(?:¥|￥|RMB\s*)([0-9]+(?:\.[0-9]{1,2})?)", re.I), "CNY"),
        (re.compile(r"([0-9]+(?:\.[0-9]{1,2})?)\s*(?:元|CNY)", re.I), "CNY"),
        (re.compile(r"€\s*([0-9]+(?:\.[0-9]{1,2})?)"), "EUR"),
        (re.compile(r"£\s*([0-9]+(?:\.[0-9]{1,2})?)"), "GBP"),
        (re.compile(r"([0-9]+(?:\.[0-9]{1,2})?)\s*CAD", re.I), "CAD"),
    )
    matches: list[tuple[int, int, float, str, str]] = []
    for pattern, currency in patterns:
        for match in pattern.finditer(text):
            span = (match.start(), match.end())
            if any(start < span[1] and span[0] < end for start, end, *_ in matches):
                continue
            amount = float(match.group(1))
            if amount > 0:
                matches.append((span[0], span[1], amount, currency, match.group(0)))
    matches.sort(key=lambda item: item[0])
    for index, (_, end, amount, currency, raw) in enumerate(matches):
        next_start = matches[index + 1][0] if index + 1 < len(matches) else len(text)
        period = _parse_period(text[end:next_start])
        if period:
            return amount, currency, raw, period
    return None


def _parse_period(text: str) -> str | None:
    lowered = text.casefold()
    if any(word in lowered for word in ("quarterly", "per quarterly", "/quarter", "每季", "季度")):
        return "quarterly"
    if any(word in lowered for word in ("annually", "annual", "yearly", "per year", "/year", "每年", "年付")):
        return "yearly"
    if any(word in lowered for word in ("monthly", "per month", "/month", "/月", "每月", "月付")):
        return "monthly"
    return None


def _parse_availability(text: str) -> str | None:
    lowered = text.casefold()
    if (
        "outofstock" in lowered
        or "out of stock" in lowered
        or "sold out" in lowered
        or re.search(r"\b0\s+(?:available|in stock)", lowered)
        or "售罄" in text
        or "缺货" in text
    ):
        return "out_of_stock"
    if (
        "instock" in lowered
        or re.search(r"\b[1-9]\d*\s+(?:available|in stock)", lowered)
        or any(word in lowered for word in ("order now", "order this package", "available"))
        or "立即订购" in text
    ):
        return "in_stock"
    return None


def _offer_from_text(
    text: str,
    target: PlanTarget,
    control_available: bool = False,
    product_url: str | None = None,
) -> Offer | None:
    if not _matches_target(text, target):
        return None
    price_period = _parse_bound_price_period(text)
    availability = _parse_availability(text) or ("in_stock" if control_available else None)
    offer_id = _offer_id(target, product_url or "")
    if price_period is None or availability is None or not product_url or not offer_id:
        return None
    amount, currency, price_raw, period = price_period
    divisor = {"monthly": 1, "quarterly": 3, "yearly": 12}[period]
    routes = tuple(word for word in ROUTE_WORDS if word.casefold() in text.casefold())
    return Offer(
        plan_name=target.plan_name,
        amount=amount,
        currency=currency,
        billing_period=period,
        monthly_amount=round(amount / divisor, 2),
        availability=availability,
        price_raw=price_raw,
        specs=" ".join(text.split())[:1000],
        provider_claimed_routes=target.provider_claimed_routes,
        parsed_route_evidence=routes,
        offer_id=offer_id,
        product_url=product_url,
    )


def _has_enabled_order_control(card: Any) -> bool:
    for control in card.select("button, input[type='button'], input[type='submit'], a[href]"):
        label = f"{control.get_text(' ', strip=True)} {control.get('value', '')}".casefold()
        if (
            not control.has_attr("disabled")
            and any(word in label for word in ("order", "buy", "订购", "购买"))
        ):
            return True
    return False


def _specific_order_url(card: Any, target: PlanTarget) -> str | None:
    for anchor in card.select("a[href]"):
        label = anchor.get_text(" ", strip=True).casefold()
        if not any(word in label for word in ("order", "buy", "订购", "购买", "checkout")):
            continue
        candidate = urljoin(target.url, str(anchor.get("href", "")))
        if target_url_allowed(target, candidate) and _offer_id(target, candidate):
            return candidate
    return None


def _offer_id(target: PlanTarget, product_url: str) -> str:
    parsed = urlparse(product_url)
    query = parse_qs(parsed.query)
    for key in ("pid", "product_id", "productid", "token", "sku", "plan"):
        values = query.get(key)
        if values and re.fullmatch(r"[\w.-]{1,120}", values[0]):
            return f"{target.id}:{key}-{values[0].lower()}"
    segments = [
        segment.lower()
        for segment in parsed.path.split("/")
        if segment and segment.lower() not in {"index.php", "cart.php", "store", "order", "create"}
    ]
    if segments and re.fullmatch(r"[\w.-]{2,120}", segments[-1]):
        return f"{target.id}:path-{segments[-1]}"
    return ""


def target_url_allowed(target: PlanTarget, raw_url: str | None) -> bool:
    parsed = urlparse(str(raw_url or ""))
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and bool(host)
        and any(host == domain or host.endswith(f".{domain}") for domain in target.expected_domains)
    )


def _json_ld_offers(soup: BeautifulSoup, target: PlanTarget) -> ParseResult | None:
    candidates: list[Offer] = []
    for script in soup.select("script[type='application/ld+json']"):
        try:
            payload = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        items = payload if isinstance(payload, list) else [payload]
        flattened: list[Any] = []
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                flattened.extend(item["@graph"])
            else:
                flattened.append(item)
        for item in flattened:
            if not isinstance(item, dict) or str(item.get("@type", "")).casefold() != "product":
                continue
            text = f"{item.get('name', '')} {item.get('description', '')}"
            if not _matches_target(text, target):
                continue
            raw_offers = item.get("offers", {})
            offers = raw_offers if isinstance(raw_offers, list) else [raw_offers]
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                price_specification = offer.get("priceSpecification")
                if not isinstance(price_specification, dict):
                    continue
                duration = str(price_specification.get("billingDuration", ""))
                period = {"P1M": "monthly", "P3M": "quarterly", "P1Y": "yearly"}.get(duration.upper())
                try:
                    amount = float(offer["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                currency = str(offer.get("priceCurrency", "")).upper()
                availability = _parse_availability(str(offer.get("availability", "")))
                product_url = str(offer.get("url") or item.get("url") or "")
                offer_id = _offer_id(target, product_url)
                if (
                    period is None
                    or amount <= 0
                    or not currency
                    or availability is None
                    or not target_url_allowed(target, product_url)
                    or not offer_id
                ):
                    continue
                divisor = {"monthly": 1, "quarterly": 3, "yearly": 12}[period]
                routes = tuple(word for word in ROUTE_WORDS if word.casefold() in text.casefold())
                candidates.append(
                    Offer(
                        target.plan_name,
                        amount,
                        currency,
                        period,
                        round(amount / divisor, 2),
                        availability,
                        f"{amount:g} {currency}",
                        " ".join(text.split())[:1000],
                        target.provider_claimed_routes,
                        routes,
                        None,
                        offer_id,
                        product_url,
                    )
                )
    unique = {
        (
            offer.offer_id,
            offer.product_url,
            offer.availability,
            offer.amount,
            offer.currency,
            offer.billing_period,
        ): offer
        for offer in candidates
    }
    if len(unique) > 1:
        return ParseResult("rejected", block_reason="multiple_matching_offers")
    if not unique:
        return None
    selected = next(iter(unique.values()))
    if selected.availability == "out_of_stock":
        return ParseResult("out_of_stock", block_reason="sold_out")
    return ParseResult("success", selected)


def parse_offer(markup: str, target: PlanTarget) -> ParseResult:
    soup = BeautifulSoup(markup, "html.parser")
    visible_soup = BeautifulSoup(markup, "html.parser")
    for node in visible_soup.select(
        "script, style, noscript, [hidden], [aria-hidden='true'], "
        "[style*='display:none'], [style*='display: none'], "
        "[style*='visibility:hidden'], [style*='visibility: hidden'], "
        ".hidden, .sr-only, .visually-hidden"
    ):
        node.decompose()
    visible = visible_soup.get_text(" ", strip=True)
    lowered = visible.casefold()
    for word in CHALLENGE_WORDS:
        if word.casefold() in lowered:
            return ParseResult("blocked", block_reason=word)
    json_result = _json_ld_offers(soup, target)
    if json_result:
        if json_result.offer and (
            json_result.offer.currency not in target.expected_currencies
            or json_result.offer.billing_period not in target.expected_billing_periods
        ):
            return ParseResult("rejected", block_reason="currency_or_period_conflict")
        return json_result
    selectors = ".package-card, .product, .package, .plan-card, .product-card, tr"
    for card in visible_soup.select(selectors):
        card_text = card.get_text(" ", strip=True)
        order_url = _specific_order_url(card, target)
        offer = _offer_from_text(
            card_text,
            target,
            _has_enabled_order_control(card),
            order_url,
        )
        if offer:
            if (
                offer.currency not in target.expected_currencies
                or offer.billing_period not in target.expected_billing_periods
            ):
                return ParseResult("rejected", block_reason="currency_or_period_conflict")
            if offer.availability == "out_of_stock":
                return ParseResult("out_of_stock", block_reason="sold_out")
            return ParseResult("success", offer)
        if (
            _matches_target(card_text, target)
            and _parse_availability(card_text) == "out_of_stock"
        ):
            return ParseResult("out_of_stock", block_reason="sold_out")
        if _matches_target(card_text, target) and _parse_bound_price_period(card_text):
            return ParseResult("rejected", block_reason="detail_unverified")
    needle = target.plan_tokens[0].casefold()
    for text_node in visible_soup.find_all(string=True):
        if needle not in str(text_node).casefold():
            continue
        card = text_node.parent
        for _ in range(5):
            if card is None or card.name in {"main", "body", "html"}:
                break
            text = card.get_text(" ", strip=True)
            if len(text) <= 2500:
                offer = _offer_from_text(
                    text,
                    target,
                    _has_enabled_order_control(card),
                    _specific_order_url(card, target),
                )
                if offer:
                    if (
                        offer.currency not in target.expected_currencies
                        or offer.billing_period not in target.expected_billing_periods
                    ):
                        return ParseResult(
                            "rejected", block_reason="currency_or_period_conflict"
                        )
                    if offer.availability == "out_of_stock":
                        return ParseResult("out_of_stock", block_reason="sold_out")
                    return ParseResult("success", offer)
            card = card.parent
    return ParseResult("error", block_reason="no_exact_same_card_offer")


def browser_fetch(url: str) -> HTTPFetch:
    started = time.monotonic()
    if sync_playwright is None:
        return HTTPFetch(
            "", "error", None, url, "browser", "playwright_not_installed", 1, 0
        )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(locale="zh-CN")
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=20_000,
                )
                markup = page.content()
                final_url = page.url
                visible = BeautifulSoup(markup, "html.parser").get_text(" ", strip=True)
                block_reason = next(
                    (
                        word
                        for word in CHALLENGE_WORDS
                        if word.casefold() in visible.casefold()
                    ),
                    None,
                )
                return HTTPFetch(
                    markup,
                    "blocked" if block_reason else "success",
                    response.status if response else None,
                    final_url,
                    "browser",
                    block_reason,
                    1,
                    int((time.monotonic() - started) * 1000),
                )
            finally:
                browser.close()
    except Exception as exc:
        return HTTPFetch(
            "", "error", None, url, "browser",
            f"browser:{type(exc).__name__}", 1,
            int((time.monotonic() - started) * 1000),
        )


def _offline_results(targets: list[PlanTarget]) -> list[TargetResult]:
    checked_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    return [
        TargetResult(
            target, "blocked", None, None, target.url, "offline",
            "offline_smoke_no_network", 0, 0, checked_at,
        )
        for target in targets
    ]


def quality_gate(site_dir: Path = SITE_DIR) -> bool:
    try:
        prices = json.loads((site_dir / "data" / "prices.json").read_text(encoding="utf-8"))
        envelope = json.loads((site_dir / "data" / "batch.json").read_text(encoding="utf-8"))
        evidence = json.loads((site_dir / "data" / "live-evidence.json").read_text(encoding="utf-8"))
        manifest = json.loads((site_dir / "manifest.json").read_text(encoding="utf-8"))
        audit = json.loads((site_dir / "audit.json").read_text(encoding="utf-8"))
        expected_task_ids = [target.id for target in load_targets(load_config())]
        validate_live_evidence(evidence, expected_task_ids)
    except (OSError, json.JSONDecodeError, ContractError, TypeError, ValueError):
        return False
    return (
        isinstance(prices, list)
        and envelope.get("mode") == "live"
        and evidence_sha256(evidence) == envelope.get("evidence_sha256")
        and audit.get("structure_status") == "pass"
        and manifest.get("batch_id") == envelope.get("batch_id")
        and not verify_file_manifest(site_dir, manifest)
        and product_quality_gate(prices)
    )


def structure_gate(site_dir: Path, expected_task_ids: list[str]) -> bool:
    try:
        envelope = json.loads((site_dir / "data" / "batch.json").read_text(encoding="utf-8"))
        manifest = json.loads((site_dir / "manifest.json").read_text(encoding="utf-8"))
        audit = json.loads((site_dir / "audit.json").read_text(encoding="utf-8"))
        statuses = json.loads((site_dir / "data" / "status.json").read_text(encoding="utf-8"))
        prices = json.loads((site_dir / "data" / "prices.json").read_text(encoding="utf-8"))
        evidence = json.loads((site_dir / "data" / "live-evidence.json").read_text(encoding="utf-8"))
        validate_live_envelope(envelope, expected_task_ids)
        validate_live_evidence(evidence, expected_task_ids)
    except (OSError, json.JSONDecodeError, ContractError, TypeError, ValueError):
        return False
    return (
        statuses == envelope.get("statuses")
        and prices == envelope.get("prices")
        and evidence_sha256(evidence) == envelope.get("evidence_sha256")
        and audit.get("structure_status") == "pass"
        and manifest.get("schema_version") == 4
        and manifest.get("batch_id") == envelope.get("batch_id")
        and manifest.get("source_sha") == envelope.get("source_sha")
        and manifest.get("config_sha256") == envelope.get("config_sha256")
        and manifest.get("mode") == "live"
        and not verify_file_manifest(site_dir, manifest)
    )


def _source_sha() -> str:
    value = os.getenv("SOURCE_SHA") or os.getenv("GITHUB_SHA", "")
    if re.fullmatch(r"[0-9a-f]{40}", value):
        return value
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "0" * 40


def _read_existing_history(site_dir: Path) -> list[dict[str, Any]]:
    for path in (STATE_DIR / "history.json", site_dir / "data" / "price_history.json"):
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(rows, list):
            return rows
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Honest same-card VPS offer monitor.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--live", action="store_true", help="fetch all 14 configured targets")
    parser.add_argument("--output", action="store_true")
    parser.add_argument("--quality-gate", action="store_true")
    parser.add_argument("--structure-gate", action="store_true")
    parser.add_argument("--deals", action="store_true", help="fetch review/AFF RSS intel into site/data/deals.json")
    parser.add_argument("--site-dir", type=Path, default=SITE_DIR)
    args = parser.parse_args()
    if args.deals:
        sys.path.insert(0, str(ROOT / "scripts"))
        from fetch_deals import build_deals

        build_deals(site_dir=args.site_dir)
        return 0
    if args.quality_gate:
        passed = quality_gate(args.site_dir)
        print(json.dumps({"vps_product_quality_gate": "pass" if passed else "fail"}))
        return 0 if passed else 1
    targets = load_targets(load_config(args.config))
    if args.structure_gate:
        passed = structure_gate(args.site_dir, [target.id for target in targets])
        print(json.dumps({"structure_gate": "pass" if passed else "fail"}))
        return 0 if passed else 1
    started_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    results = (
        crawl_targets(targets, browser_fn=browser_fetch)
        if args.live
        else _offline_results(targets)
    )
    mode = "live" if args.live else "fixture"
    public = build_public_data(
        results,
        targets,
        _read_existing_history(args.site_dir),
        mode=mode,
    )
    evidence = build_live_evidence(results, mode=mode)
    finished_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    envelope = build_batch_envelope(
        public,
        targets,
        mode=mode,
        run_id=os.getenv("GITHUB_RUN_ID", "local"),
        run_attempt=os.getenv("GITHUB_RUN_ATTEMPT", "0"),
        source_sha=_source_sha(),
        config_sha256=hashlib.sha256(args.config.read_bytes()).hexdigest(),
        started_at=started_at,
        finished_at=finished_at,
        evidence=evidence,
    )
    report = audit_envelope(envelope, [target.id for target in targets])
    deals_report: dict[str, Any] | None = None
    if args.output and mode == "live":
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            from fetch_deals import build_deals

            # Write into a temp dir only for the fetch, then pass the payload
            # to publish_site so the manifest includes it atomically.
            deals_report = build_deals(site_dir=Path(args.site_dir).parent / ".deals-tmp")
        except Exception as exc:  # intel failure must not fail the round
            print(f"[deals] skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
    if args.output:
        publish_site(
            public,
            site_dir=args.site_dir,
            evidence=evidence,
            envelope=envelope,
            audit_report=report,
            deals=deals_report,
        )
        if mode == "live":
            write_state_directory(
                STATE_DIR,
                repo="crawl_vps_promotions",
                branch=os.getenv("GITHUB_REF_NAME", "main"),
                run_id=envelope["run_id"],
                config_sha256=envelope["config_sha256"],
                batch_id=envelope["batch_id"],
                completed_task_ids=[row["task_id"] for row in public["status"]],
                history=public["price_history"],
            )
    print(json.dumps(public["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
