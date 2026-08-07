#!/usr/bin/env python3
"""Fetch VPS promo intel from host-review / AFF RSS feeds (P3-5).

Sources (verified reachable, full-text RSS, 2026-08-07):
  - LowEndBox:  https://lowendbox.com/feed/
  - LowEndTalk: https://lowendtalk.com/discussions/feed.rss

Only *article-level* intel is captured (title/link/date/source). Affiliate
parameters are NOT processed or published; links are the canonical article
URLs. This channel is an intelligence feed only — it never enters the
product gate (same-card offer verification) and cannot fabricate offers.

Output: site/data/deals.json (schema below), also published via --site-dir.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
SITE_DIR = ROOT / "site"

# Canonical feed URLs (verified 200, full-text, no auth).
FEEDS = {
    "lowendbox": {
        "url": "https://lowendbox.com/feed/",
        "label": "LowEndBox",
        "timeout": 30,
    },
    "lowendtalk": {
        "url": "https://lowendtalk.com/discussions/feed.rss",
        "label": "LowEndTalk",
        "timeout": 30,
    },
}

# Strong keywords: alone are enough to pass (explicit VPS/hosting/promo intent).
STRONG_KEYWORDS = (
    "vps", "hosting", "host", "promo", "promotion", "coupon", "discount",
    "kvm", "vds", "cn2", "9929", "cmi", "cmmin2", "gia", "ssd", "nvme",
    "vcore", "带宽", "优惠", "特价", "促销", "低至", "月付", "年付", "sale",
)
# Weak keywords: only pass when combined with a strong keyword.
WEAK_KEYWORDS = ("deal", "off", "cloud", "server", "ram", "cpu")
# VPS-only: exclude dedicated server / web-hosting / domain noise.
EXCLUDE_KEYWORDS = (
    "dedicated server", "dedicated servers", "bare metal", "bare-metal",
    "web hosting", "shared hosting", "domain", "reseller hosting",
    "wordpress hosting", "ssl", "email hosting", "cdn", "dns",
)
# Buyer-request / discussion patterns (not deals).
REQUEST_PATTERNS = (
    "looking for", "lookin for", "recommend", "suggest", "any good",
    "which one", "is there", "advice", "question", "opinion", "compare",
    "how to", "what's", "whats", "status", "down and", "is down", "attack",
    "outage", "back up", "backup", "giveaway", "hardware", "rant",
    "review of", "reviews?", "thread", "cpanel", "escape", "suspended",
    "yabs", "reselling", "request", "potato", "worst", "avoid", "flaw",
    "vulnerability", "exploit", "geo-locates", "homepage?", "dedi",
    "tribblix", "self-hosting", "written by ai", "america turns",
    "good with your hands", "colocation pricing",
)
# Explicit offer markers: if present, the entry passes regardless of other
# request-pattern heuristics (e.g. "2GB EPYC VPS $4/mo", "-50% OFF").
# Deliberately narrow: price/discount/CPU-model signals only, not bare
# words like "vps" or "kvm" (those live in STRONG_KEYWORDS and stay
# subject to request-pattern exclusions).
OFFER_PATTERNS = (
    r"\$\d+(\.\d+)?\s*/?\s*(mo|month|yr|year|quarter|半|月|年)",
    r"\d+%?\s*off",
    r"-?\d{1,2}%",
    r"epyc", r"ryzen", r"xeon",
    r"flat price", r"lifetime", r"黑五", r"双十一", r"圣诞",
)

FEED_NAMESPACES = {
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return (node.text or "").strip()


def keyword_match(title: str, summary: str) -> bool:
    lowered = f"{title} {summary}".casefold()
    if any(word in lowered for word in EXCLUDE_KEYWORDS):
        return False
    # Explicit offer markers override request heuristics.
    if any(re.search(pattern, lowered) for pattern in OFFER_PATTERNS):
        return True
    if any(pattern in lowered for pattern in REQUEST_PATTERNS):
        return False
    # Question-mark titles are almost always buyer requests, not offers.
    if "?" in title:
        return False
    has_strong = any(word in lowered for word in STRONG_KEYWORDS)
    if has_strong:
        return True
    # Weak-only: require at least two weak hits (e.g. "server deals").
    weak_hits = [word for word in WEAK_KEYWORDS if word in lowered]
    return len(weak_hits) >= 2


def fetch_feed(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_feed(source_key: str, raw: str, *, now: str) -> list[dict[str, Any]]:
    root = ET.fromstring(raw)
    items: list[dict[str, Any]] = []
    for node in root.iter():
        if _local_name(node.tag) != "item":
            continue
        title = ""
        link = ""
        pub = ""
        summary = ""
        for child in node:
            name = _local_name(child.tag)
            if name == "title":
                title = _text(child)
            elif name == "link":
                link = _text(child)
            elif name == "pubdate":
                pub = _text(child)
            elif name == "description":
                summary = _text(child)
            elif name == "encoded" and "content" in child.tag:
                summary = _text(child)
        if not title:
            continue
        if not keyword_match(title, summary):
            continue
        items.append(
            {
                "source": source_key,
                "source_label": FEEDS[source_key]["label"],
                "title": re.sub(r"\s+", " ", title).strip(),
                "link": link,
                "published": pub,
                "fetched_at": now,
            }
        )
    return items


def build_deals(*, site_dir: Path) -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    entries: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for source_key, feed in FEEDS.items():
        try:
            raw = fetch_feed(feed["url"], timeout=feed["timeout"])
            parsed = parse_feed(source_key, raw, now=now)
            entries.extend(parsed)
            print(f"[deals] {source_key}: {len(parsed)} entries")
        except Exception as exc:
            errors[source_key] = f"{type(exc).__name__}: {exc}"
            print(f"[deals] {source_key} failed: {errors[source_key]}", file=sys.stderr)
    entries.sort(key=lambda row: row.get("published") or "", reverse=True)
    deals = {
        "schema_version": 1,
        "fetched_at": now,
        "sources": list(FEEDS.keys()),
        "entry_count": len(entries),
        "errors": errors,
        "entries": entries[:100],
    }
    data_dir = site_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "deals.json").write_text(
        json.dumps(deals, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return deals


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch VPS promo intel from review/AFF RSS feeds")
    parser.add_argument("--site-dir", type=Path, default=SITE_DIR)
    args = parser.parse_args()
    deals = build_deals(site_dir=args.site_dir)
    print(json.dumps({"entry_count": deals["entry_count"], "errors": deals["errors"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
