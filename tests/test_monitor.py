from pathlib import Path
from dataclasses import replace
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from urllib.parse import urlparse

import pytest
import requests

from vps_monitor.monitor import (
    BrowserLimiter,
    HTTPFetch,
    ProviderCircuitBreaker,
    RequestLimiter,
    TargetResult,
    build_live_evidence,
    build_public_data,
    crawl_targets,
    evidence_sha256,
    fetch_target,
    load_config,
    load_targets,
    parse_offer,
    prioritize_targets,
    product_quality_gate,
    publish_site,
    request_with_budget,
    validate_live_evidence,
    validate_round,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return FIXTURES.joinpath(name).read_text(encoding="utf-8")


EXPECTED_IDS = [
    "zorocloud-us-cnc-pro",
    "zorocloud-us-9929-pro",
    "zorocloud-jp-cn2-pro",
    "hostdare-cssd3",
    "hostdare-camd3",
    "bandwagon-osaka-40g",
    "jtti-us-cn2",
    "jtti-jp-optimized",
    "racknerd-4gb-special",
    "cloudcone-ssd-vps-4",
    "buyvm-slice4096",
    "greencloud-budgetkvmhk2-3",
    "layerstack-r108",
    "lisahost-us-9929-annual",
]


def test_config_has_expanded_targets_across_providers_and_official_domains():
    targets = load_targets(load_config())
    # Task set is expanded (v4 extension); conservation is checked dynamically.
    assert len(targets) >= 25
    assert len({target.id for target in targets}) == len(targets)
    assert len({target.provider for target in targets}) >= 16
    assert len({urlparse(target.url).hostname for target in targets}) >= 15
    assert len({target.url for target in targets}) == len(targets)
    assert all(target.plan_name and target.plan_tokens for target in targets)
    assert all("aff" not in urlparse(target.url).query.lower() for target in targets)
    assert all(target.expected_domains and target.priority > 0 for target in targets)
    assert all(
        target.expected_currencies
        and target.expected_billing_periods
        and target.lifecycle == "active"
        for target in targets
    )
    assert prioritize_targets(list(reversed(targets))) == targets


def test_offer_outside_configured_currency_expectation_is_rejected():
    target = next(target for target in load_targets(load_config()) if target.provider == "BuyVM")
    unexpected = replace(target, expected_currencies=("EUR",))
    parsed = parse_offer(fixture("buyvm.html"), unexpected)
    assert parsed.outcome == "rejected"
    assert parsed.block_reason == "currency_or_period_conflict"


@pytest.mark.parametrize(
    ("provider", "fixture_name", "amount", "currency", "period", "availability"),
    [
        ("ZoroCloud", "zorocloud.html", 14.99, "USD", "monthly", "in_stock"),
        ("HostDare", "hostdare.html", 216.89, "USD", "yearly", "in_stock"),
        ("BandwagonHost", "bandwagonhost.html", 49.99, "USD", "monthly", "in_stock"),
        ("Jtti", "jtti.html", 17.63, "USD", "monthly", "in_stock"),
        ("CloudCone", "cloudcone.html", 79.99, "USD", "yearly", "in_stock"),
        ("BuyVM", "buyvm.html", 15.0, "USD", "monthly", "in_stock"),
        ("GreenCloud", "greencloud.html", 45.0, "USD", "yearly", "in_stock"),
        ("LayerStack", "layerstack.html", 28.06, "USD", "monthly", "in_stock"),
        ("LisaHost", "lisahost.html", 199.0, "CNY", "yearly", "in_stock"),
    ],
)
def test_site_fixtures_bind_plan_specs_price_period_and_inventory_to_same_card(
    provider, fixture_name, amount, currency, period, availability
):
    target = next(target for target in load_targets(load_config()) if target.provider == provider)
    parsed = parse_offer(fixture(fixture_name), target)
    assert parsed.outcome == "success"
    assert parsed.offer is not None
    assert parsed.offer.plan_name == target.plan_name
    assert parsed.offer.amount == amount
    assert parsed.offer.currency == currency
    assert parsed.offer.billing_period == period
    assert parsed.offer.availability == availability
    divisor = {"monthly": 1, "quarterly": 3, "yearly": 12}[period]
    assert parsed.offer.monthly_amount == round(amount / divisor, 2)
    assert parsed.offer.measured_routes is None


@pytest.mark.parametrize(
    ("amount", "period", "monthly"),
    [(30, "monthly", 30), (90, "quarterly", 30), (360, "yearly", 30)],
)
def test_monthly_amount_is_only_same_currency_period_arithmetic(amount, period, monthly):
    markup = fixture("buyvm.html").replace("$15.00 per month", f"${amount}.00 per {period}")
    target = next(target for target in load_targets(load_config()) if target.provider == "BuyVM")
    parsed = parse_offer(markup, target)
    assert parsed.offer.currency == "USD"
    assert parsed.offer.amount == amount
    assert parsed.offer.billing_period == period
    assert parsed.offer.monthly_amount == monthly


def test_challenge_page_is_blocked_and_browser_flag_cannot_make_it_success():
    target = load_targets(load_config())[0]
    parsed = parse_offer(fixture("challenge.html"), target)
    assert parsed.outcome == "blocked"
    assert parsed.offer is None
    assert parsed.block_reason


def test_json_ld_graph_selects_the_unique_matching_offer_and_builds_stable_offer_id():
    target = next(target for target in load_targets(load_config()) if target.provider == "CloudCone")
    parsed = parse_offer(fixture("cloudcone_graph.html"), target)
    assert parsed.outcome == "success"
    assert parsed.offer is not None
    assert parsed.offer.offer_id == "cloudcone-ssd-vps-4:token-ssd-vps-4"
    assert parsed.offer.product_url == "https://app.cloudcone.com/vps/358/create?token=ssd-vps-4"

    changed_price = fixture("cloudcone_graph.html").replace('"79.99"', '"69.99"')
    changed = parse_offer(changed_price, target)
    assert changed.offer is not None
    assert changed.offer.offer_id == parsed.offer.offer_id


def test_multiple_matching_json_ld_offers_are_rejected_as_ambiguous():
    target = next(target for target in load_targets(load_config()) if target.provider == "CloudCone")
    parsed = parse_offer(fixture("cloudcone_ambiguous_offers.html"), target)
    assert parsed.outcome == "rejected"
    assert parsed.offer is None
    assert parsed.block_reason == "multiple_matching_offers"


def test_broad_cart_card_without_specific_order_url_is_only_discovery():
    target = next(
        target for target in load_targets(load_config()) if target.provider == "BandwagonHost"
    )
    parsed = parse_offer(fixture("broad_cart.html"), target)
    assert parsed.outcome == "rejected"
    assert parsed.block_reason == "detail_unverified"


def test_hidden_price_and_malformed_json_ld_are_untrusted_input():
    target = next(target for target in load_targets(load_config()) if target.provider == "BuyVM")
    hidden = """
    <div class="product-card">
      <h2>SLICE 4096</h2><p>4096 MB 80 GB SSD</p>
      <span hidden>$1.00 per month</span><span>$15.00 per month</span>
      <a href="https://buyvm.net/order/slice-4096">Order this package</a>
    </div>
    """
    parsed = parse_offer(hidden, target)
    assert parsed.offer is not None
    assert parsed.offer.amount == 15.0

    malformed = """
    <script type="application/ld+json">
    {"@type":"Product","name":"SLICE 4096 4096 MB 80 GB SSD",
     "offers":{"price":"15","priceCurrency":"USD",
     "availability":"https://schema.org/InStock",
     "url":"https://buyvm.net/order/slice-4096",
     "priceSpecification":"not-an-object"}}
    </script>
    """
    rejected = parse_offer(malformed, target)
    assert rejected.outcome in {"error", "rejected"}


def test_sold_out_observation_is_out_of_stock_without_publishable_offer():
    target = next(target for target in load_targets(load_config()) if target.provider == "RackNerd")
    parsed = parse_offer(fixture("racknerd.html"), target)
    assert parsed.outcome == "out_of_stock"
    assert parsed.offer is None


def test_success_without_specific_offer_identity_fails_structure():
    target = next(target for target in load_targets(load_config()) if target.provider == "BuyVM")
    parsed = parse_offer(fixture("buyvm.html"), target)
    assert parsed.offer is not None
    invalid = replace(parsed.offer, offer_id="", product_url=target.url)
    result = TargetResult(
        target,
        "success",
        invalid,
        200,
        target.url,
        "requests",
        None,
        1,
        1,
        "2026-07-30T00:00:00Z",
    )
    with pytest.raises(ValueError, match="specific"):
        validate_round([result], [target])


def test_final_url_must_remain_on_target_expected_domain():
    target = load_targets(load_config())[0]
    fetched = HTTPFetch(
        fixture("zorocloud.html"),
        "success",
        200,
        "https://evil.example/redirected",
        "requests",
        None,
        1,
        5,
    )
    result = fetch_target(
        target,
        request_fn=lambda _: fetched,
        browser_fn=lambda _: pytest.fail("domain mismatch must fail before browser"),
    )
    assert result.outcome == "rejected"
    assert result.block_reason == "url_domain_mismatch"


def test_unknown_css_class_uses_nearest_bounded_card_but_never_whole_page_first_price():
    target = next(target for target in load_targets(load_config()) if target.provider == "Jtti")
    opaque_card = """
    <!-- synthetic/test-only parser fixture; never live evidence -->
    <div class="pricing-unit-opaque"><h3>U.S.ecs 标准</h3>
    <p>2核 4GB 50GB 5 Mbps CN2 独享</p><b>$17.63 /月</b>
    <a href="https://www.jtti.cc/zh/order/us-ecs-standard">立即订购</a></div>
    """
    assert parse_offer(opaque_card, target).outcome == "success"
    split_page = """
    <!-- synthetic/test-only negative fixture; never live evidence -->
    <main><div><h3>U.S.ecs 标准</h3><p>2核 4GB 50GB CN2</p></div>
    <aside><b>$1.00 /月</b><button>立即订购</button></aside></main>
    """
    assert parse_offer(split_page, target).outcome == "error"


class FakeResponse:
    def __init__(self, status=200, text="<html>ok</html>", url="https://example.test/final"):
        self.status_code = status
        self.text = text
        self.url = url


class SequenceGet:
    def __init__(self, items, advance=None):
        self.items = list(items)
        self.calls = []
        self.advance = advance

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.advance:
            self.advance(sum(kwargs["timeout"]))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_request_retries_transient_statuses_only(status):
    getter = SequenceGet([FakeResponse(status), FakeResponse(status), FakeResponse(200)])
    fetched = request_with_budget("https://example.test/plan", getter=getter, sleep=lambda _: None)
    assert fetched.attempts == 3
    assert fetched.http_status == 200
    assert getter.calls[0][1]["timeout"] == (5, 15)


def test_connection_retries_but_403_and_challenge_do_not_retry_or_open_browser():
    getter = SequenceGet([requests.ConnectionError("down"), FakeResponse(200)])
    assert request_with_budget("https://example.test", getter=getter, sleep=lambda _: None).attempts == 2

    forbidden = SequenceGet([FakeResponse(403), FakeResponse(200)])
    blocked = request_with_budget("https://example.test", getter=forbidden, sleep=lambda _: None)
    assert blocked.outcome == "blocked" and blocked.attempts == 1

    target = load_targets(load_config())[0]
    challenge = HTTPFetch(
        fixture("challenge.html"), "blocked", 200, target.url, "requests", "captcha", 1, 5
    )
    result = fetch_target(
        target,
        request_fn=lambda _: challenge,
        browser_fn=lambda _: pytest.fail("challenge must not invoke browser"),
    )
    assert result.outcome == "blocked" and result.offer is None


def test_single_url_budget_truncates_final_retry_at_45_seconds():
    now = [0.0]
    getter = SequenceGet(
        [FakeResponse(503), FakeResponse(503), FakeResponse(503)],
        advance=lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    fetched = request_with_budget(
        "https://example.test",
        getter=getter,
        sleep=lambda _: None,
        monotonic=lambda: now[0],
        total_budget=45,
    )
    assert fetched.attempts == 3
    assert sum(getter.calls[-1][1]["timeout"]) <= 5
    assert now[0] == 45


def test_request_limiter_is_global_four_per_host_one_and_releases_after_exception():
    limiter = RequestLimiter(global_limit=4, per_host_limit=1)
    lock = threading.Lock()
    active = 0
    peak = 0
    host_active = {}
    host_peak = {}

    def work(url):
        nonlocal active, peak
        host = urlparse(url).hostname
        with limiter.slot(url):
            with lock:
                active += 1
                host_active[host] = host_active.get(host, 0) + 1
                peak = max(peak, active)
                host_peak[host] = max(host_peak.get(host, 0), host_active[host])
            time.sleep(0.01)
            with lock:
                active -= 1
                host_active[host] -= 1

    urls = [f"https://{host}.test/{i}" for i in range(4) for host in "abcde"]
    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(work, urls))
    assert peak <= 4
    assert all(value == 1 for value in host_peak.values())
    with pytest.raises(RuntimeError):
        with limiter.slot("https://a.test/fail"):
            raise RuntimeError("boom")
    with limiter.slot("https://a.test/reused"):
        pass


def test_cancelled_limiter_wait_is_interruptible_and_does_not_leak_slots():
    limiter = RequestLimiter(global_limit=1, per_host_limit=1)
    cancelled = threading.Event()
    entered = threading.Event()
    with limiter.slot("https://a.test/held"):
        def waiter():
            entered.set()
            with limiter.slot("https://a.test/waiting", cancelled=cancelled):
                raise AssertionError("cancelled waiter acquired a slot")

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(waiter)
            assert entered.wait(1)
            cancelled.set()
            with pytest.raises(CancelledError):
                future.result(timeout=1)
    with limiter.slot("https://a.test/reused"):
        pass


def test_provider_circuit_is_independent_and_new_batch_starts_half_open():
    breaker = ProviderCircuitBreaker(threshold=2)
    breaker.record("ZoroCloud", "captcha")
    assert breaker.allow("ZoroCloud")
    breaker.record("ZoroCloud", "captcha")
    assert not breaker.allow("ZoroCloud")
    assert breaker.allow("HostDare")
    assert ProviderCircuitBreaker(threshold=2).allow("ZoroCloud")


def test_browser_is_single_fallback_and_only_parser_success_counts():
    target = load_targets(load_config())[0]
    shell = HTTPFetch("<html>enable javascript</html>", "success", 200, target.url, "requests", None, 1, 2)
    browser_calls = []

    def browser_fn(url):
        browser_calls.append(url)
        return HTTPFetch(fixture("zorocloud.html"), "success", 200, url, "browser", None, 1, 8)

    result = fetch_target(target, request_fn=lambda _: shell, browser_fn=browser_fn)
    assert result.outcome == "success"
    assert result.method == "browser"
    assert len(browser_calls) == 1

    bad_browser = HTTPFetch("<html><body>rendered but wrong plan $1 monthly</body></html>", "success", 200, target.url, "browser", None, 1, 8)
    result = fetch_target(target, request_fn=lambda _: shell, browser_fn=lambda _: bad_browser)
    assert result.outcome != "success" and result.offer is None


def test_browser_limiter_global_one_and_crawl_preserves_config_order():
    limiter = BrowserLimiter(global_limit=1)
    with limiter.slot():
        pass
    targets = load_targets(load_config())
    challenge = HTTPFetch(fixture("challenge.html"), "blocked", 403, "", "requests", "http_403", 1, 3)
    results = crawl_targets(
        targets,
        request_fn=lambda _: challenge,
        browser_fn=lambda _: pytest.fail("403 must not invoke browser"),
    )
    assert [result.target.id for result in results] == [target.id for target in targets]
    assert len(results) == len(targets)


def blocked_result(target):
    return TargetResult(
        target=target,
        outcome="blocked",
        offer=None,
        http_status=403,
        final_url=target.url,
        method="requests",
        block_reason="http_403",
        attempts=1,
        latency_ms=15,
        checked_at="2026-07-30T00:00:00+00:00",
    )


def success_result(target, fixture_name):
    parsed = parse_offer(fixture(fixture_name), target)
    assert parsed.offer
    return TargetResult(
        target=target,
        outcome="success",
        offer=parsed.offer,
        http_status=200,
        final_url=target.url,
        method="requests",
        block_reason=None,
        attempts=1,
        latency_ms=10,
        checked_at="2026-07-30T00:00:00+00:00",
    )


def test_round_has_14_ordered_statuses_and_complete_observability():
    targets = load_targets(load_config())
    results = [blocked_result(target) for target in targets]
    summary = validate_round(results, targets)
    assert summary == {
        "attempted": len(targets),
        "success": 0,
        "blocked": len(targets),
        "rejected": 0,
        "error": 0,
        "out_of_stock": 0,
        "providers": len({target.provider for target in targets}),
    }
    public = build_public_data(results, targets)
    assert [row["id"] for row in public["status"]] == [target.id for target in targets]
    required = {
        "http_status",
        "method",
        "attempts",
        "latency_ms",
        "availability",
        "price_raw",
        "checked_at",
    }
    assert all(required <= row.keys() for row in public["status"])
    assert all(row["price_raw"] is None and row["availability"] is None for row in public["status"])


def test_live_evidence_is_bounded_and_redacts_url_and_reason_secrets():
    target = load_targets(load_config())[0]
    result = TargetResult(
        target=target,
        outcome="blocked",
        offer=None,
        http_status=403,
        final_url="https://user:password@example.com/path?token=secret#fragment",
        method="unexpected-method",
        block_reason="https://user:password@example.com/path?token=secret",
        attempts=1,
        latency_ms=12,
        checked_at="2026-07-30T00:00:00+00:00",
    )
    evidence = build_live_evidence([result], mode="live")
    validate_live_evidence(evidence, [target.id])
    row = evidence["tasks"][0]
    assert set(row) == {
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
    assert row["final_url"] == "https://example.com"
    assert row["method"] == "other"
    assert row["block_reason"] == "unclassified"
    serialized = __import__("json").dumps(evidence, ensure_ascii=False)
    assert "password" not in serialized
    assert "secret" not in serialized


def test_live_evidence_reason_codes_and_structure_fail_closed():
    target = load_targets(load_config())[0]
    evidence = build_live_evidence([blocked_result(target)], mode="live")
    evidence["tasks"][0]["block_reason"] = "api_key_secret"
    with pytest.raises(ValueError):
        validate_live_evidence(evidence, [target.id])

    evidence = build_live_evidence([blocked_result(target)], mode="live")
    evidence["tasks"][0]["unexpected"] = True
    with pytest.raises(ValueError):
        validate_live_evidence(evidence, [target.id])

    evidence = build_live_evidence([blocked_result(target)], mode="live")
    evidence["summary"]["task_count"] = 2
    with pytest.raises(ValueError):
        validate_live_evidence(evidence, [target.id])


def test_live_evidence_preserves_configured_order_and_conserves_summary():
    targets = load_targets(load_config())
    evidence = build_live_evidence([blocked_result(target) for target in targets], mode="live")
    validate_live_evidence(evidence, [target.id for target in targets])
    assert [row["task_id"] for row in evidence["tasks"]] == [target.id for target in targets]
    assert evidence["summary"]["task_count"] == len(targets)
    assert evidence["summary"]["provider_count"] == len({target.provider for target in targets})
    assert evidence["summary"]["outcome_counts"] == {
        "success": 0,
        "blocked": len(targets),
        "rejected": 0,
        "error": 0,
        "out_of_stock": 0,
    }
    assert len(evidence_sha256(evidence)) == 64


def test_browser_usage_alone_never_counts_as_success():
    target = load_targets(load_config())[0]
    poisoned = TargetResult(
        target, "blocked", parse_offer(fixture("zorocloud.html"), target).offer,
        200, target.url, "browser", "no_exact_same_card_offer", 2, 30,
        "2026-07-30T00:00:00+00:00",
    )
    with pytest.raises(ValueError, match="non-success"):
        validate_round([poisoned], [target])


def test_all_blocked_still_publishes_14_statuses_and_empty_rebuilt_price_data(tmp_path):
    targets = load_targets(load_config())
    public = build_public_data([blocked_result(target) for target in targets], targets)
    assert public["prices"] == []
    assert public["price_history"] == []
    assert public["product_gate"] is False
    publish_site(public, tmp_path)
    assert len(__import__("json").loads((tmp_path / "data/status.json").read_text(encoding="utf-8"))) == len(targets)
    assert (tmp_path / "data/prices.json").read_text(encoding="utf-8").strip() == "[]"
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "app.js" in page
    assert "live-blocked" in (tmp_path / "app.js").read_text(encoding="utf-8")
    assert (tmp_path / "manifest.json").exists()
    assert (tmp_path / "audit.json").exists()
    evidence = __import__("json").loads((tmp_path / "data/live-evidence.json").read_text(encoding="utf-8"))
    assert len(evidence["tasks"]) == len(targets)
    assert (tmp_path / "CNAME").read_text(encoding="utf-8") == "vps.jiucai.eu.org\n"


def test_product_gate_is_false_at_seven_and_true_at_eight_successes():
    targets = load_targets(load_config())
    provider_fixtures = {
        "ZoroCloud": "zorocloud.html",
        "HostDare": "hostdare.html",
        "BandwagonHost": "bandwagonhost.html",
        "Jtti": "jtti.html",
        "CloudCone": "cloudcone.html",
        "BuyVM": "buyvm.html",
        "GreenCloud": "greencloud.html",
        "LayerStack": "layerstack.html",
    }
    success_targets = []
    seen = set()
    for target in targets:
        if target.provider in provider_fixtures and target.provider not in seen:
            seen.add(target.provider)
            success_targets.append(target)
    results = [
        success_result(target, provider_fixtures[target.provider])
        if target in success_targets[:7]
        else blocked_result(target)
        for target in targets
    ]
    public = build_public_data(results, targets)
    assert len(public["prices"]) == 7
    assert not public["product_gate"]
    assert not product_quality_gate(public["prices"])
    eighth = success_targets[7]
    results[targets.index(eighth)] = success_result(eighth, provider_fixtures[eighth.provider])
    public = build_public_data(results, targets)
    assert len(public["prices"]) == 8
    assert public["product_gate"]
    assert product_quality_gate(public["prices"])
    row = public["prices"][0]
    assert row["provider_claimed_routes"]
    assert "parsed_route_evidence" in row
    assert row["measured_routes"] is None
