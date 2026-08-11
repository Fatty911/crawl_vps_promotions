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
        # BandwagonHost：多周期解析（2026-08-11），主价=月化最低周期（年付）
        ("BandwagonHost", "bandwagonhost.html", 499.99, "USD", "yearly", "in_stock"),
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
    # Blocked requests now fall through to the browser fallback (real
    # fingerprint + fresh node) before giving up — a blocked outcome alone
    # must not be the end of the round for that target.
    result = fetch_target(
        target,
        request_fn=lambda _: challenge,
        browser_fn=lambda _: HTTPFetch(
            fixture("challenge.html"), "blocked", 200, target.url, "browser",
            "captcha", 1, 5, browser_diag="challenge"
        ),
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
    # crawl order must follow prioritize_targets (priority, id) — not raw
    # config order, which diverges once priorities are non-monotonic
    # (2026-08-10: contabo matrix 175-196 inserted before DMIT 180).
    from vps_monitor.monitor import prioritize_targets
    assert [result.target.id for result in results] == [t.id for t in prioritize_targets(targets)]
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
        "browser_diag",
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


def test_parse_offer_buyvm_plan_card_and_span_heading():
    """BuyVM cards are div.plan.fourplan with span-split headings
    ('SLICE <span>4096</span>'). parse_offer must match them (observed
    2026-08-10: buyvm-slice4096/2048 reported no_exact_same_card_offer while
    the cards are on the page; the selector list lacked .plan and the text
    fallback required the full token in a single text node)."""
    targets = {t.id: t for t in load_targets(load_config())}
    target = targets["buyvm-slice4096"]
    markup = """<html><body>
    <div class="plan fourplan">
      <h2>SLICE <span>4096</span></h2>
      <ul>
        <li><strong>1 Core</strong> @ 3.50+ GHz Dedicated CPU Usage</li>
        <li><strong>4096 MB</strong> Memory</li>
        <li><strong>80 GB SSD</strong> Storage</li>
        <li>Unmetered Bandwidth</li>
        <li>1 IPv4 Address</li>
      </ul>
      <p>$15.00 per month</p>
      <!-- BuyVM order buttons have NO href (JS-driven checkout); the page
           itself is the product page (observed 2026-08-10). -->
      <a data-group="slice" data-plan="4096" class="orderbutton button greenbutton">ORDER THIS PACKAGE</a>
    </div>
    </body></html>"""
    result = parse_offer(markup, target)
    assert result.outcome == "success", f"expected success, got {result.outcome}: {result.block_reason}"
    assert result.offer is not None
    assert result.offer.amount == 15.00
    assert result.offer.billing_period == "monthly"
    assert result.offer.offer_id  # non-empty offer id (path fallback)
    # href-less order button falls back to the product page URL itself.
    assert result.offer.product_url == target.url


def test_blocked_request_falls_through_to_browser_and_browser_success_counts():
    """A blocked request must not end the round: the browser fallback (real
    fingerprint + rotated node) can still win the offer. Regression for the
    2026-08-10 decision that blocked/error targets deserve the browser path
    instead of being returned immediately."""
    target = load_targets(load_config())[0]
    blocked = HTTPFetch(
        fixture("challenge.html"), "blocked", 403, target.url, "requests", "http_403", 1, 5
    )
    good = HTTPFetch(fixture("zorocloud.html"), "success", 200, target.url, "browser", None, 1, 5)
    result = fetch_target(
        target,
        request_fn=lambda _: blocked,
        browser_fn=lambda _: good,
    )
    assert result.outcome == "success" and result.method == "browser"


def test_error_request_falls_through_to_browser():
    """Connection-error requests (no HTTP status) also get the browser path."""
    target = load_targets(load_config())[0]
    failed = HTTPFetch("", "error", None, target.url, "requests", "connection:ConnectionError", 3, 5)
    good = HTTPFetch(fixture("zorocloud.html"), "success", 200, target.url, "browser", None, 1, 5)
    result = fetch_target(
        target,
        request_fn=lambda _: failed,
        browser_fn=lambda _: good,
    )
    assert result.outcome == "success" and result.method == "browser"


def test_browser_fetch_retries_on_challenge_then_returns_blocked():
    """browser_fetch rotates to a second node once when the first node shows a
    challenge page; if the second is also blocked the outcome stays blocked."""
    target = load_targets(load_config())[0]
    # A challenge page that is identical on both nodes: browser_fetch must
    # call sync_playwright twice (two nodes) and still report blocked.
    calls = []

    class FakePage:
        def content(self):
            return fixture("challenge.html")

        @property
        def url(self):
            return target.url

        def on(self, *_args):
            pass

        def goto(self, *_args, **_kwargs):
            # networkidle + 30s: Cloudflare challenges complete via JS after
            # domcontentloaded; waiting for idle lets the challenge pass and
            # the real product DOM load (2026-08-10 browser-root regression).
            assert _kwargs.get("wait_until") == "networkidle"
            assert _kwargs.get("timeout") == 30_000
            return None

    class FakeBrowser:
        def new_page(self, **_kwargs):
            return FakePage()

        def close(self):
            pass

    class FakePlaywright:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @property
        def chromium(self):
            return self

        def launch(self, **_kwargs):
            calls.append(_kwargs)
            return FakeBrowser()

    import vps_monitor.monitor as monitor_mod
    original = monitor_mod.sync_playwright
    monitor_mod.sync_playwright = lambda: FakePlaywright()
    try:
        result = monitor_mod.browser_fetch(target.url)
    finally:
        monitor_mod.sync_playwright = original
    assert len(calls) == 2  # two node attempts
    assert result.outcome == "blocked"


def test_parse_offer_cloudcone_pricing_card_and_deploy_button():
    """CloudCone lists plans as div.pricing cards (not .plan/.package-card);
    the SSD VPS 2 card's CTA is 'Deploy VPS' while SSD VPS 4's is
    'Purchase Plan' — both must be recognised (observed 2026-08-10:
    tokens matched the page but _has_enabled_order_control missed 'deploy'
    and the selector list missed .pricing, yielding detail_unverified /
    no_exact_same_card_offer on live pages)."""
    markup = fixture("cloudcone_vps.html")
    targets = {t.id: t for t in load_targets(load_config())}
    for tid, expected in (
        ("cloudcone-ssd-vps-2", (3.83, "monthly")),
        ("cloudcone-ssd-vps-4", (7.66, "monthly")),
    ):
        result = parse_offer(markup, targets[tid])
        assert result.outcome == "success", (tid, result.block_reason)
        assert result.offer is not None
        assert result.offer.amount == expected[0], (tid, result.offer.amount)
        assert result.offer.billing_period == expected[1]
        assert result.offer.availability == "in_stock"
        assert result.offer.offer_id and "token-" in result.offer.offer_id
        assert result.offer.product_url.startswith("https://app.cloudcone.com/vps/")


def test_parse_offer_contabo_ct_productbox_get_started():
    """Contabo renamed plans (VPS M -> Cloud VPS 4 by vCPU) and moved to
    Astro ct-productbox cards with a 'Get Started' CTA and price text
    '€5.50 4 40 / month' (slash-space-month, not /month) — all three must
    parse (observed 2026-08-10: tokens matched but price period and
    order-control detection both failed on the live page)."""
    markup = fixture("contabo_vps.html")
    target = next(t for t in load_targets(load_config()) if t.id == "contabo-vps-m")
    result = parse_offer(markup, target)
    assert result.outcome == "success", result.block_reason
    assert result.offer is not None
    assert result.offer.amount == 4.4 and result.offer.currency == "EUR"
    assert result.offer.billing_period == "monthly"
    assert result.offer.availability == "in_stock"


def test_parse_offer_linveo_panel_configure_button():
    """Linveo moved Intel KVM pricing to /intel with panel cards and a
    'Configure | OH' CTA linking to billing.linveo.com (4GB tier
    discontinued; live tier is 8GB $5.50/mo). 'configure' keyword needed
    for order-control detection (observed 2026-08-10)."""
    markup = fixture("linveo_intel.html")
    target = next(t for t in load_targets(load_config()) if t.id == "linveo-ohio-vps")
    result = parse_offer(markup, target)
    assert result.outcome == "success", result.block_reason
    assert result.offer is not None
    assert result.offer.amount == 5.5 and result.offer.currency == "USD"
    assert result.offer.billing_period == "monthly"
    assert result.offer.availability == "in_stock"
    assert "billing.linveo.com" in (result.offer.product_url or "")


def test_parse_offer_contabo_full_matrix():
    """Contabo full product matrix must parse without JS: Cloud VPS 4-18
    (€4.40-39.20 promo prices, struck previous-price excluded) and Cloud
    VPS Plus 4-18 (EPYC NVMe, €10.80-79.20). All 12 tiers live on two
    server-rendered pages (observed 2026-08-10 — user pasted the rendered
    text of both pages and asked why the matrix can't be parsed)."""
    expected = {
        "contabo-vps-m": (4.4, "vps"),
        "contabo-vps-6": (6.0, "vps"),
        "contabo-vps-8": (11.2, "vps"),
        "contabo-vps-12": (20.0, "vps"),
        "contabo-vps-16": (29.6, "vps"),
        "contabo-vps-18": (39.2, "vps"),
        "contabo-plus-4": (10.8, "performance"),
        "contabo-plus-6": (15.2, "performance"),
        "contabo-plus-8": (28.0, "performance"),
        "contabo-plus-12": (47.2, "performance"),
        "contabo-plus-16": (63.2, "performance"),
        "contabo-plus-18": (79.2, "performance"),
    }
    fixtures = {
        "vps": fixture("contabo_vps_full.html"),
        "performance": fixture("contabo_vps_performance_full.html"),
    }
    targets = {t.id: t for t in load_targets(load_config())}
    for tid, (amount, page) in expected.items():
        target = targets[tid]
        result = parse_offer(fixtures[page], target)
        assert result.outcome == "success", (tid, result.block_reason)
        assert result.offer is not None
        assert abs(result.offer.amount - amount) < 0.01, (tid, result.offer.amount)
        assert result.offer.currency == "EUR"
        assert result.offer.billing_period == "monthly"
        assert result.offer.availability == "in_stock"


def test_bandwagon_whmcs_onclick_order_button():
    """BandwagonHost (2026-08-11): product table rows use WHMCS
    <input type="button" onclick="window.location='cart.php?a=add&pid=87'">
    order buttons with NO href. _specific_order_url must extract the JS-target
    URL so the offer carries a real per-plan order link (pid offer id).
    Also covers: full cart page (no productFilter) has all plans in <tr>
    rows; 10G CN2 GIA plan was renamed to SPECIAL 20G KVM PROMO V5."""
    from vps_monitor.monitor import load_config, load_targets, parse_offer
    body = (Path(__file__).parent / "fixtures" / "bandwagon_cart.html").read_text(encoding="utf-8")
    targets = {t.id: t for t in load_targets(load_config())}

    res = parse_offer(body, targets["bandwagon-cn2-gia-10g"])
    assert res.outcome == "success", res.block_reason
    # 多周期解析（2026-08-11）：主价 = 月化最低的支付周期（年付 $169.99/12
    # = $14.17/月 < 季付 $16.66/月），price_points 保留全部周期月化单价
    assert res.offer.amount == 169.99 and res.offer.currency == "USD"
    assert res.offer.billing_period == "yearly"
    assert res.offer.monthly_amount == 14.17
    assert res.offer.price_points == (
        ("yearly", 14.17),
        ("semiannual", 15.0),
        ("quarterly", 16.66),
    )
    assert res.offer.product_url == "https://bandwagonhost.com/cart.php?a=add&pid=87"

    res = parse_offer(body, targets["bandwagon-osaka-40g"])
    assert res.outcome == "success", res.block_reason
    # 四周期全解析：月付 $49.99/月 vs 年付 $499.99/12=$41.67/月
    assert res.offer.amount == 499.99 and res.offer.currency == "USD"
    assert res.offer.billing_period == "yearly"
    assert res.offer.monthly_amount == 41.67
    assert res.offer.price_points == (
        ("yearly", 41.67),
        ("semiannual", 45.0),
        ("quarterly", 46.66),
        ("monthly", 49.99),
    )
    assert res.offer.product_url == "https://bandwagonhost.com/cart.php?a=add&pid=134"


def test_cloudiplc_pt_row_whmcs_row_link():
    """CloudIPLC (2026-08-11): WHMCS custom theme uses column cards
    (.pt__cell) grouped in <a class="pt__row"> rows where the row link IS
    the add-to-cart link (cart.php?a=add&pid=17, no button label).
    Requires: .pt__row in card selectors, self-anchor handling in
    _specific_order_url/_has_enabled_order_control, a=add URL recognition.
    Also: [停运] (discontinued) markers must parse as out_of_stock."""
    from vps_monitor.monitor import load_config, load_targets, parse_offer
    base = Path(__file__).parent / "fixtures"
    targets = {t.id: t for t in load_targets(load_config())}

    qz = (base / "cloudiplc_qz.html").read_text(encoding="utf-8")
    res = parse_offer(qz, targets["cloudiplc-quanzhou-cn2"])
    assert res.outcome == "success", res.block_reason
    assert res.offer.amount == 1083.33 and res.offer.currency == "CNY"
    assert res.offer.billing_period == "monthly"
    assert res.offer.product_url == "https://www.cloudiplc.com/cart.php?a=add&pid=17"

    la = (base / "cloudiplc_la.html").read_text(encoding="utf-8")
    res = parse_offer(la, targets["cloudiplc-la-cn2"])
    assert res.outcome == "out_of_stock", (res.outcome, res.block_reason)


def test_hosthatch_spa_data_table_row():
    """HostHatch (2026-08-11): SPA page renders plan rows as
    <div class="data-table-row"> with <strong>NVMe 2 GB</strong> and
    "from $4.00 / month" (no per-region naming, no order links on page —
    order URL falls back to the page URL, offer id from path segment).
    Regression: 2 GB plan must parse at $4.00 monthly in_stock."""
    from vps_monitor.monitor import load_config, load_targets, parse_offer
    body = (Path(__file__).parent / "fixtures" / "hosthatch_ssd_vps.html").read_text(encoding="utf-8")
    targets = {t.id: t for t in load_targets(load_config())}
    res = parse_offer(body, targets["hosthatch-la-nvme"])
    assert res.outcome == "success", res.block_reason
    assert res.offer.amount == 4.0 and res.offer.currency == "USD"
    assert res.offer.billing_period == "monthly"
    assert res.offer.product_url == "https://hosthatch.com/ssd-vps"


def test_layerstack_pricing_usd_mth():
    """LayerStack (2026-08-11): pricing page renders US$X /mth rows in
    <tr> (US$ prefix + /mth period were both unsupported before).
    Also: R111 tier was discontinued — now tracked as R208."""
    from vps_monitor.monitor import load_config, load_targets, parse_offer
    body = (Path(__file__).parent / "fixtures" / "layerstack_pricing.html").read_text(encoding="utf-8")
    targets = {t.id: t for t in load_targets(load_config())}
    res = parse_offer(body, targets["layerstack-r108"])
    assert res.outcome == "success", res.block_reason
    assert res.offer.amount == 17.76 and res.offer.currency == "USD"
    assert res.offer.billing_period == "monthly"
    res = parse_offer(body, targets["layerstack-r208"])
    assert res.outcome == "success", res.block_reason
    assert res.offer.amount == 34.49 and res.offer.currency == "USD"


def test_spartanhost_virtfusion_zero_available():
    """SpartanHost (2026-08-11): billing domain moved from
    billing.spartantech.com (dead cert) to billing.spartanhost.net; CN2 GIA
    tier retired, now AS9929-CMIN2 SEAKVM line. Virtfusion renders "0
    Available" placeholder on every tier while Order Now is enabled — must
    parse as in_stock, not out_of_stock."""
    from vps_monitor.monitor import load_config, load_targets, parse_offer
    body = (Path(__file__).parent / "fixtures" / "spartanhost_cmin2.html").read_text(encoding="utf-8")
    targets = {t.id: t for t in load_targets(load_config())}
    res = parse_offer(body, targets["spartanhost-sea-9929-cmin2"])
    assert res.outcome == "success", res.block_reason
    assert res.offer.amount == 36.0 and res.offer.currency == "USD"
    assert res.offer.availability == "in_stock"
    assert res.offer.product_url.startswith("https://billing.spartanhost.net/")


def test_parse_period_semiannual():
    """2026-08-11：半年付周期识别（semi-annual / half-year / 半年付）。"""
    from vps_monitor.monitor import _parse_period
    for text in ("Semi-Annually $59.94", "half-yearly $55", "每半年 ¥300", "半年付 ¥290"):
        assert _parse_period(text) == "semiannual", text
    assert _parse_period("monthly $5") == "monthly"
    assert _parse_period("annually $50") == "yearly"
    assert _parse_period("quarterly $15") == "quarterly"


def test_parse_all_price_periods_keeps_min_per_cycle():
    """2026-08-11：同一套餐多支付周期单价不同——全部提取，同周期取最小，
    按周期顺序返回；无周期价格返回 None。"""
    from vps_monitor.monitor import _parse_all_price_periods
    text = (
        "Monthly $5.99 or Quarterly $16.47 (save 8%), Semi-Annually $29.94, "
        "Annually $47.88 (save 33%). Was $7.99/month, now $5.99/month."
    )
    assert _parse_all_price_periods(text) == {
        "monthly": 5.99,
        "quarterly": 16.47,
        "semiannual": 29.94,
        "yearly": 47.88,
    }
    # 同周期多价格（原价 + 促销价）取最小
    assert _parse_all_price_periods("Was $10.00/month, now $6.00/month") == {"monthly": 6.0}
    # 无周期价格
    assert _parse_all_price_periods("$9.99 only, no period words here") is None


def test_offer_price_points_status_row():
    """2026-08-11：price_points 写入 status 行；单周期回退（None）。"""
    from vps_monitor.monitor import load_config, load_targets, parse_offer
    from pathlib import Path
    body = (Path(__file__).parent / "fixtures" / "bandwagon_cart.html").read_text(encoding="utf-8")
    targets = {t.id: t for t in load_targets(load_config())}
    res = parse_offer(body, targets["bandwagon-osaka-40g"])
    assert res.offer.price_points
    # 单周期（HostHatch from $12.00 / month）不产生 price_points
    hh = (Path(__file__).parent / "fixtures" / "hosthatch_ssd_vps.html").read_text(encoding="utf-8")
    r2 = parse_offer(hh, targets["hosthatch-la-nvme"])
    assert r2.offer.price_points == ()
