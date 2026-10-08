#!/usr/bin/env python3
"""Verify the public Pages payload through normal TLS against the built manifest."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import socket
import sys
import time
import urllib.parse
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin

import requests


MAX_FILE_BYTES = 25 * 1024 * 1024

# 本机代理 (Clash/mihomo TUN) 的 fake-ip 透传段: 域名经 TUN DNS 解析恒得这两段,
# 真实出口由代理解析, 不是内网目标 (AA 采集器与本脚本都曾被它误伤)
_FAKE_IP_NETS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("2001:2::/48"),
)


def _require_public_base(base_url: str) -> str:
    """验收目标必须 https，且主机不是字面量内网/元数据地址。

    base_url 来自流水线参数（可被写坏），不设防时一个指向内网/元数据地址的
    base_url 会让验收变成携内网探测能力的跳板；禁重定向（_get 的 opener）
    防止用公网首跳绕过此检查。刻意不做 DNS 解析级校验：CNB 容器的 DNS/
    透明代理架构下域名解析结果可以是平台内网网关（09-30 实证 pages.dev 因此
    被误杀、验收 155s 秒挂），解析值在此环境不构成内网判定依据；字面量
    内网 IP/主机名（实际攻击面）仍逐项拒绝。"""
    parsed = urllib.parse.urlparse(str(base_url).rstrip("/"))
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise ValueError(f"verify base_url must be https with a host: {base_url!r}")
    if host == "localhost" or host.endswith((".local", ".internal")):
        raise ValueError(f"verify base_url refuses non-public host: {host!r}")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return parsed.geturl()  # 域名：字面量检查已过，解析层交由运行环境
    if not addr.is_global:
        raise ValueError(f"verify base_url refuses non-public literal address: {host!r}")
    return parsed.geturl()


def _web_excluded_files(files: object) -> dict:
    # 2026-08-11 用户规则：web/ 前端文件允许两次 monitor 构建间漂移（pages-deploy 独立部署），
    # data/ 与核心字段必须严格一致。manifest 的 files 比较据此排除 web/ 前缀条目。
    return {
        str(name): meta
        for name, meta in (files or {}).items()
        if not str(name).startswith("web/")
    }


def compare_manifests(expected: dict, actual: dict) -> list[str]:
    mismatches = [
        field
        for field in ("schema_version", "batch_id", "source_sha", "mode", "run_id", "run_attempt")
        if expected.get(field) != actual.get(field)
    ]
    if _web_excluded_files(expected.get("files")) != _web_excluded_files(actual.get("files")):
        mismatches.append("files(web-excluded)")
    return mismatches


def verify_prices(base_url: str, expected: dict) -> None:
    # live prices.json 必须逐任务命中 expected 的 amount/period/availability；
    # currency 允许 EUR/USD 等价（Contabo 按访客区域serve等价价格）；促销变化必须 FAIL。
    live_rows = json.loads(_get(urljoin(base_url.rstrip("/") + "/", "data/prices.json")))
    live = {str(row.get("task_id")): row for row in live_rows}
    missing = [task_id for task_id in expected if task_id not in live]
    if missing:
        raise ValueError(f"missing tasks: {sorted(missing)}")
    for task_id, want in expected.items():
        row = live[task_id]
        allowed = want["currency"]
        allowed = allowed if isinstance(allowed, list) else [allowed]
        if row.get("currency") not in allowed:
            raise ValueError(
                f"{task_id}:currency={row.get('currency')}!= {allowed}"
            )
        if row.get("amount") != want["amount"]:
            raise ValueError(f"{task_id}:amount={row.get('amount')}!= {want['amount']}")
        if row.get("billing_period") != want["billing_period"]:
            raise ValueError(f"{task_id}:billing_period={row.get('billing_period')}!= {want['billing_period']}")
        if row.get("availability") != want["availability"]:
            raise ValueError(f"{task_id}:availability={row.get('availability')}!= {want['availability']}")


def _get(url: str) -> bytes:
    # 用 requests（run_footprint 出口探测同款头集）：09-30 全天留痕实证，CNB 出口对
    # 裸 urllib（HTTP/1.1、无典型头集）的请求恒返 308（CF 对非浏览器客户端的反爬
    # 挑战，UA 与 query 均非变量），requests 形态每轮 200。allow_redirects=False
    # 保留禁重定向语义：跟随会绕过 _require_public_base 的主机校验。
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 Chrome/126"},
        timeout=30,
        allow_redirects=False,
    )
    if response.status_code != 200:
        raise OSError(f"public file fetch failed: HTTP {response.status_code} {url}")
    data = response.content
    if len(data) > MAX_FILE_BYTES:
        raise OSError("public file exceeds size limit")
    return data


def verify(base_url: str, expected: dict) -> None:
    # 刻意不加 cachebust query：CF Pages 对带 query 的静态资产请求返回 308 重定向
    # （cleanUrl 行为，POSIX 留痕批 sn=cnb-acf-1k3qibak9 的 20 次进度行实证），
    # 而禁重定向钉死首跳会让 308 直接失败。pages.dev 默认域部署即时生效、
    # max-age=0 不缓存，本就不需要绕缓存。
    actual = json.loads(_get(urljoin(base_url.rstrip("/") + "/", "data/manifest.json")))
    mismatches = compare_manifests(expected, actual)
    if mismatches:
        # actual 关键字段进异常消息：进度行直接暴露线上真实 run 标识，
        # 区分「旧轮缓存/传播延迟」（actual.run_id 是旧轮）与「真不一致」（同轮仍异）
        raise ValueError(
            f"public manifest mismatch: {','.join(mismatches)}"
            f" [actual run_id={actual.get('run_id')}"
            f" source_sha={str(actual.get('source_sha'))[:12]}]"
        )
    for name, metadata in expected["files"].items():
        path = PurePosixPath(str(name))
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe manifest path: {name}")
        digest = hashlib.sha256(_get(urljoin(base_url.rstrip("/") + "/", str(path)))).hexdigest()
        if digest != metadata["sha256"]:
            raise ValueError(f"public file hash mismatch: {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--attempts", type=int, default=6)
    args = parser.parse_args()
    base_url = _require_public_base(args.base_url)
    expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    error: Exception | None = None
    for attempt in range(args.attempts):
        try:
            verify(base_url, expected)
            print(f"POST_DEPLOY_VERIFIED batch={expected['batch_id']} source_sha={expected['source_sha']}")
            return 0
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            error = exc
            # 每次重试必须有一行输出：CNB 对「连续 10 分钟无输出」的 job 强杀（09-30 sn=cnb-idr
            # 实证 600086ms 整，验收 20x30s 静默重试恰好撞线被砍，商品门禁连带 skip）。
            # 且 stdout 在管道下全缓冲——不切行缓冲的话这些进度行同样到不了平台。
            try:
                sys.stdout.reconfigure(line_buffering=True)
            except (AttributeError, ValueError):
                pass
            print(f"attempt {attempt + 1}/{args.attempts}: {type(exc).__name__}: {exc}")
            if attempt + 1 < args.attempts:
                # 09-30 实测（sn=cnb-e1a-1k3o7uvqe）：CF Pages 部署后边缘传播超过 12x10s=2 分钟，
                # 验收全程读到旧 manifest 误报 mismatch；10 分钟后复测同内容已可见。
                # 间隔放宽到 30s，12 次窗口 2 分钟 → 6 分钟。
                time.sleep(30)
    print(f"POST_DEPLOY_FAILED {type(error).__name__}: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
