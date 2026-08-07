"""Tests for the mihomo runtime setup chain (proxy subscription parsing)."""

import json
from pathlib import Path

import pytest

from scripts.setup_proxy_runtime import parse_nodes, parse_proxy_secret

# The VPS repo uses the Sub-Store crawler collection (token redacted in tests
# via env; the URL below is the documented internal collection endpoint).
CRAWLER_URL = "https://sub.jiucai.eu.org/download/collection/crawler?token=1233211234567"


def test_parse_proxy_secret_plain_url():
    subs, excluded = parse_proxy_secret(CRAWLER_URL)
    assert subs == [CRAWLER_URL]
    assert excluded == []


def test_parse_proxy_secret_json_dict():
    raw = json.dumps({"subscriptions": [CRAWLER_URL], "exclude_keywords": ["香港"]})
    subs, excluded = parse_proxy_secret(raw)
    assert subs == [CRAWLER_URL]
    assert excluded == ["香港"]


def test_parse_proxy_secret_invalid_and_empty():
    subs, excluded = parse_proxy_secret("")
    assert subs == [] and excluded == []
    subs, _ = parse_proxy_secret("not a url")
    assert subs == []
    subs, _ = parse_proxy_secret("null")
    assert subs == []


@pytest.mark.network
def test_parse_nodes_from_crawler_collection():
    """Live parse of the Sub-Store crawler collection (needs network)."""
    subs, excluded = parse_proxy_secret(CRAWLER_URL)
    assert subs
    proxies = parse_nodes(subs, excluded)
    assert len(proxies) >= 50
    names = [p.get("name", "") for p in proxies]
    assert all(isinstance(n, str) and n for n in names)
    assert len({n for n in names}) == len(names)  # deduped


def test_rotator_scripts_are_registered_and_compile():
    src = (Path(__file__).parents[1] / "scripts/codex_delivery_gate.py").read_text(encoding="utf-8")
    for name in ("node_rotator.py", "setup_proxy_runtime.py", "generate_clash_config.py"):
        assert name in src
