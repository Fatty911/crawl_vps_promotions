from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
import yaml
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - requests fallback still works locally
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "providers.yaml"
SITE_DIR = ROOT / "site"
DATA_DIR = SITE_DIR / "data"

PRICE_PATTERNS = [
    re.compile(r"(?:¥|￥|RMB\s*)(\d+(?:\.\d+)?)", re.I),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|CNY)", re.I),
    re.compile(r"\$\s*(\d+(?:\.\d+)?)", re.I),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:USD|美元)", re.I),
    re.compile(r"(\d+(?:\.\d+)?)\s*(?:CAD|加元)", re.I),
]

RATES_TO_CNY = {
    "CNY": 1.0,
    "USD": 7.25,
    "CAD": 5.30,
}

KEYWORDS = {
    "stock": ["available", "有货", "立即购买", "order now", "buy now", "库存"],
    "sold_out": ["out of stock", "缺货", "售罄", "0 available", "不可购买"],
    "cpu": ["2c", "2 c", "2核", "2 核", "2 vcpu", "2vcpu"],
    "memory": ["4g", "4 gb", "4gb", "4G", "4 GB"],
    "disk": ["30g", "40g", "50g", "60g", "80g", "100g", "30 gb", "40 gb", "50 gb"],
    "bandwidth": ["100m", "100 mbps", "300m", "500m", "1gbps", "10gbps"],
    "route": ["cn2", "gia", "9929", "cmin2", "as58807", "as4809", "as9929", "软银", "softbank"],
}


@dataclass
class FetchResult:
    text: str
    used_browser: bool
    error: str | None = None


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def fetch_with_requests(url: str, timeout: int) -> FetchResult:
    headers = {"User-Agent": "Mozilla/5.0 vps-deal-monitor/1.0"}
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return FetchResult(" ".join(soup.get_text(" ").split()), used_browser=False)


def fetch_with_browser(url: str, timeout: int) -> FetchResult:
    if sync_playwright is None:
        return FetchResult("", used_browser=False, error="playwright not installed")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(locale="zh-CN")
        page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
        text = page.locator("body").inner_text(timeout=timeout * 1000)
        browser.close()
        return FetchResult(" ".join(text.split()), used_browser=True)


def fetch_page(url: str, timeout: int = 35) -> FetchResult:
    try:
        first = fetch_with_requests(url, timeout)
        if len(first.text) >= 800 and not looks_js_shell(first.text):
            return first
    except Exception as exc:
        first = FetchResult("", used_browser=False, error=f"requests: {exc}")
    try:
        rendered = fetch_with_browser(url, timeout)
        if rendered.text:
            return rendered
        return FetchResult(first.text, used_browser=False, error=rendered.error or first.error)
    except Exception as exc:
        return FetchResult(first.text, used_browser=False, error=f"browser: {exc}; {first.error or ''}".strip())


def looks_js_shell(text: str) -> bool:
    lowered = text.lower()
    return len(text) < 800 or "enable javascript" in lowered or "请启用javascript" in lowered


def extract_price_cny(text: str) -> tuple[float | None, str | None]:
    for pattern in PRICE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = match.group(0)
        value = float(match.group(1))
        currency = "CNY"
        if "$" in raw or "USD" in raw.upper() or "美元" in raw:
            currency = "USD"
        elif "CAD" in raw.upper() or "加元" in raw:
            currency = "CAD"
        return round(value * RATES_TO_CNY[currency], 2), raw
    return None, None


def contains_any(text: str, words: list[str]) -> bool:
    lowered = text.lower()
    return any(word.lower() in lowered for word in words)


def score(provider: dict[str, Any], text: str, price_cny: float | None) -> int:
    score_value = 0
    for key, words in KEYWORDS.items():
        if key == "sold_out":
            continue
        if contains_any(text, words):
            score_value += {"stock": 25, "cpu": 15, "memory": 15, "disk": 10, "bandwidth": 10, "route": 20}[key]
    if contains_any(text, KEYWORDS["sold_out"]):
        score_value -= 35
    if price_cny is not None:
        if price_cny <= 100:
            score_value += 25
        elif price_cny <= 130:
            score_value += 8
        else:
            score_value -= min(30, int((price_cny - 100) / 10))
    if any(contains_any(text, [plan]) for plan in provider.get("target_plans", [])):
        score_value += 10
    return max(score_value, 0)


def classify_status(text: str, error: str | None) -> str:
    if error and not text:
        return "抓取失败"
    if contains_any(text, KEYWORDS["sold_out"]):
        return "可能缺货"
    if contains_any(text, KEYWORDS["stock"]):
        return "可能有货"
    return "需人工复核"


def build_records(config: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    checked_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    for provider in config["providers"]:
        fetched = fetch_page(provider["url"])
        price_cny, price_raw = extract_price_cny(fetched.text)
        snippet = fetched.text[:600]
        records.append(
            {
                "id": provider["id"],
                "name": provider["name"],
                "url": provider["url"],
                "region": provider.get("region", ""),
                "routes": provider.get("routes", {}),
                "notes": provider.get("notes", ""),
                "status": classify_status(fetched.text, fetched.error),
                "price_cny": price_cny,
                "price_raw": price_raw,
                "score": score(provider, fetched.text, price_cny),
                "used_browser": fetched.used_browser,
                "error": fetched.error,
                "snippet": snippet,
                "checked_at": checked_at,
            }
        )
    return sorted(records, key=lambda item: (-item["score"], item["price_cny"] or 999999, item["name"]))


def render_html(records: list[dict[str, Any]]) -> str:
    generated_at = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    rows = []
    for index, item in enumerate(records, 1):
        routes = " / ".join(f"{k}: {v}" for k, v in item["routes"].items())
        price = f"¥{item['price_cny']:.2f}" if item["price_cny"] is not None else "未识别"
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td><a href='{html.escape(item['url'])}'>{html.escape(item['name'])}</a></td>"
            f"<td>{html.escape(item['region'])}</td>"
            f"<td>{html.escape(item['status'])}</td>"
            f"<td>{price}</td>"
            f"<td>{item['score']}</td>"
            f"<td>{html.escape(routes)}</td>"
            f"<td>{'是' if item['used_browser'] else '否'}</td>"
            f"<td>{html.escape(item['notes'])}</td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>北京三网优化 VPS 性价比监控</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 24px; color: #1f2937; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px; vertical-align: top; }}
    th {{ background: #f3f4f6; }}
    .hint {{ background: #fff7ed; border: 1px solid #fed7aa; padding: 12px; margin: 16px 0; }}
  </style>
</head>
<body>
  <h1>北京三网优化 VPS 性价比监控</h1>
  <p>生成时间：{generated_at}。目标：非香港、北京移动/联通/电信优化、2 核 4G + 30G 起步、约 100M 带宽、月费尽量 ¥100 内。</p>
  <div class="hint">自动抓取只能识别页面文本、价格和库存关键词；JS 页面会用 Playwright 渲染。下单前仍需晚高峰用北京三网实测 ping、mtr/traceroute、SSH 和 OpenCode 工作流。</div>
  <table>
    <thead><tr><th>排名</th><th>服务商/套餐</th><th>地区</th><th>状态</th><th>识别价格/月</th><th>分数</th><th>线路关键词</th><th>JS 渲染</th><th>备注</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <p>原始 JSON：<a href="data/results.json">data/results.json</a></p>
</body>
</html>
"""


def write_outputs(records: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.joinpath("results.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    SITE_DIR.joinpath("index.html").write_text(render_html(records), encoding="utf-8")
    SITE_DIR.joinpath("CNAME").write_text("vps.jiucai.eu.org\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor China-optimized VPS offers and build a Pages site.")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--output", action="store_true", help="write site/index.html and site/data/results.json")
    args = parser.parse_args()
    records = build_records(load_config(args.config))
    if args.output:
        write_outputs(records)
    print(json.dumps(records, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
