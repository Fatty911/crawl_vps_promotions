#!/usr/bin/env python3
"""Start and validate a required mihomo runtime for crawler workflow steps."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

try:
    from scripts.generate_clash_config import ClashConfigGenerator, redact_url
except ModuleNotFoundError:
    from generate_clash_config import ClashConfigGenerator, redact_url


MIHOMO_API = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
GITHUB_NO_PROXY = (
    "127.0.0.1,localhost,api.github.com,github.com,"
    "results-receiver.actions.githubusercontent.com,.blob.core.windows.net"
)
PROXY_ENV_DISABLED = {
    "PROXY_ENABLED": "false",
    "PROXY_CONFIG_FILE": "",
    "HTTP_PROXY": "",
    "HTTPS_PROXY": "",
    "ALL_PROXY": "",
    "http_proxy": "",
    "https_proxy": "",
    "all_proxy": "",
    "NO_PROXY": "",
    "no_proxy": "",
}


CREDENTIAL_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9+/=_\-]{24,}")
URL_CREDENTIAL_PATTERN = re.compile(r"(://)[^/@\s]+@")


def redact_log_content(raw: str) -> str:
    """Strip credential-looking material before echoing mihomo log excerpts.

    mihomo logs can embed subscription URLs and node credentials; the proxy
    secret must never reach the workflow log.  URL userinfo and any long
    high-entropy token are masked, which keeps diagnostics useful without
    leaking secrets.
    """

    text = URL_CREDENTIAL_PATTERN.sub(r"\1***@", raw)
    return CREDENTIAL_TOKEN_PATTERN.sub("***", text)


def tail_log_for_diagnostics(log_path: Path, limit: int = 3000) -> None:
    """Print the redacted tail of the mihomo log for failure triage.

    Ported from the crawl_cars proxy setup (last 3000 characters), with
    credential redaction so the fail-closed security contract is preserved.
    """

    try:
        content = log_path.read_text(errors="replace")
    except OSError:
        return
    excerpt = content[-limit:]
    if not excerpt.strip():
        return
    print(f"=== mihomo log tail (redacted, last {limit} characters) ===")
    print(redact_log_content(excerpt))


def append_github_env(path: str, values: dict[str, str]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def mask(value: str) -> None:
    if value and os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{value}")


def split_plain_urls(raw: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[\r\n;|,]+", raw)
        if part.strip()
    ]


def parse_proxy_secret(raw: str) -> tuple[list[str], list[str]]:
    normalized = (raw or "").strip()
    if not normalized or normalized.lower() == "null":
        return [], []
    subscriptions: list[str] = []
    excluded: list[str] = []
    try:
        data: Any = json.loads(normalized)
    except json.JSONDecodeError:
        subscriptions = split_plain_urls(normalized)
    else:
        if isinstance(data, dict):
            raw_subscriptions = data.get("subscriptions") or data.get("subs") or []
            raw_excluded = data.get("exclude_keywords") or data.get("exclude") or []
            subscriptions = (
                split_plain_urls(raw_subscriptions)
                if isinstance(raw_subscriptions, str)
                else [str(value).strip() for value in raw_subscriptions if str(value).strip()]
            )
            excluded = (
                split_plain_urls(raw_excluded)
                if isinstance(raw_excluded, str)
                else [str(value).strip() for value in raw_excluded if str(value).strip()]
            )
        elif isinstance(data, list):
            subscriptions = [
                str(value).strip() for value in data if str(value).strip()
            ]
        elif isinstance(data, str):
            subscriptions = split_plain_urls(data)
    valid: list[str] = []
    seen: set[str] = set()
    for url in subscriptions:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            print("invalid proxy subscription entry ignored")
            continue
        if url in seen:
            continue
        seen.add(url)
        mask(url)
        valid.append(url)
    return valid, excluded


def choose_mihomo_asset(release: dict[str, Any]) -> str | None:
    candidates: list[tuple[int, str, str]] = []
    for asset in release.get("assets", []):
        name = str(asset.get("name", "")).lower()
        url = str(asset.get("browser_download_url", ""))
        if (
            url
            and "linux" in name
            and "amd64" in name
            and name.endswith(".gz")
            and not any(token in name for token in ("deb", "rpm", "pkg", "debug"))
        ):
            candidates.append((10 if "compatible" in name else 0, name, url))
    return sorted(candidates, reverse=True)[0][2] if candidates else None


def find_mihomo(bin_dir: Path) -> Path | None:
    existing = shutil.which("mihomo")
    if existing:
        return Path(existing)
    candidate = bin_dir / "mihomo"
    return candidate if candidate.exists() else None


def download_mihomo(bin_dir: Path) -> Path | None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "mihomo"
    try:
        request = urllib.request.Request(
            MIHOMO_API, headers={"User-Agent": "crawl-laptops-actions"}
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.loads(response.read().decode("utf-8"))
        asset_url = choose_mihomo_asset(release)
        if not asset_url:
            return None
        request = urllib.request.Request(
            asset_url, headers={"User-Agent": "crawl-laptops-actions"}
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            target.write_bytes(gzip.decompress(response.read()))
        target.chmod(
            target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        print(f"mihomo ready: {target}")
        return target
    except Exception as exc:
        print(f"mihomo download failed: {type(exc).__name__}: {exc}")
        return None


def parse_nodes(
    subscriptions: list[str], excluded: list[str]
) -> list[dict[str, Any]]:
    generator = ClashConfigGenerator()
    proxies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for url in subscriptions:
        print(f"parsing subscription: {redact_url(url)}")
        for proxy in generator.parse_subscription(url, excluded):
            key = str(proxy.get("name") or "").strip() or json.dumps(
                proxy, sort_keys=True, ensure_ascii=False
            )
            if key not in seen:
                seen.add(key)
                proxies.append(proxy)
    return proxies


def write_runtime_files(
    proxy_config: Path,
    clash_config: Path,
    subscriptions: list[str],
    excluded: list[str],
    proxies: list[dict[str, Any]],
    health_check_url: str = "https://www.baidu.com/",
) -> None:
    proxy_config.parent.mkdir(parents=True, exist_ok=True)
    proxy_config.write_text(
        json.dumps(
            {
                "subscription_count": len(subscriptions),
                "exclude_keywords": excluded,
                "node_count": len(proxies),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    generator = ClashConfigGenerator(str(clash_config))
    generator.save_config(
        generator.generate_config_from_proxies(
            proxies, health_check_url=health_check_url
        ),
        str(clash_config),
    )


def wait_for_controller(timeout: int = 30) -> bool:
    session = requests.Session()
    session.trust_env = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (
                session.get(
                    "http://127.0.0.1:9090/version", timeout=2
                ).status_code
                == 200
            ):
                return True
        except requests.RequestException:
            time.sleep(1)
    return False


def test_local_proxy(
    urls: list[str], max_retries: int = 3, retry_delay: int = 5
) -> bool:
    session = requests.Session()
    session.trust_env = False
    proxies = {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
    for url in urls:
        healthy = False
        for attempt in range(1, max_retries + 1):
            try:
                response = session.get(url, proxies=proxies, timeout=15)
                healthy = 200 <= response.status_code < 400
                print(
                    f"proxy target health: {urlparse(url).netloc} "
                    f"HTTP {response.status_code} attempt={attempt}"
                )
            except requests.RequestException as exc:
                print(
                    f"proxy target health: {urlparse(url).netloc} "
                    f"{type(exc).__name__} attempt={attempt}"
                )
            if healthy:
                break
            if attempt < max_retries:
                time.sleep(retry_delay)
        if not healthy:
            return False
    return bool(urls)


def unavailable(github_env: str, reason: str, required: bool) -> int:
    append_github_env(github_env, PROXY_ENV_DISABLED)
    if required:
        print(f"required proxy unavailable: {reason}")
        return 2
    print(f"proxy unavailable: {reason}")
    return 0


def stop_and_report(process: subprocess.Popen[bytes], log_path: Path) -> None:
    process.terminate()
    tail_log_for_diagnostics(log_path)
    print(f"mihomo stopped after proxy validation failure; log retained at {log_path}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-env", default=os.getenv("GITHUB_ENV", ""))
    parser.add_argument("--proxy-config", default="/tmp/proxies.json")
    parser.add_argument("--clash-config", default="/tmp/mihomo/config.yaml")
    parser.add_argument("--bin-dir", default="/tmp/mihomo-bin")
    parser.add_argument("--test-url", action="append", default=[])
    parser.add_argument("--require-proxy", action="store_true")
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()
    if args.clear:
        append_github_env(args.github_env, PROXY_ENV_DISABLED)
        print("crawler proxy environment cleared for GitHub transfer")
        return 0
    subscriptions, excluded = parse_proxy_secret(
        os.getenv("PROXY_SUBSCRIPTIONS", "")
    )
    if not subscriptions:
        return unavailable(
            args.github_env, "PROXY_SUBSCRIPTIONS is missing or invalid", args.require_proxy
        )
    proxies = parse_nodes(subscriptions, excluded)
    if not proxies:
        return unavailable(
            args.github_env, "subscription contained no supported nodes", args.require_proxy
        )
    proxy_config = Path(args.proxy_config)
    clash_config = Path(args.clash_config)
    health_check_url = os.environ.get(
        "HEALTH_CHECK_URL",
        f"https://{urlparse(args.test_url[0]).netloc}/"
        if args.test_url
        else "https://www.baidu.com/",
    )
    write_runtime_files(
        proxy_config, clash_config, subscriptions, excluded, proxies, health_check_url
    )
    mihomo = find_mihomo(Path(args.bin_dir)) or download_mihomo(
        Path(args.bin_dir)
    )
    if not mihomo:
        return unavailable(args.github_env, "mihomo is unavailable", args.require_proxy)
    log_path = Path("/tmp/mihomo.log")
    log_file = log_path.open("ab")
    process = subprocess.Popen(
        [str(mihomo), "-d", str(clash_config.parent), "-f", str(clash_config)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(f"mihomo started: pid={process.pid}")
    if not wait_for_controller():
        stop_and_report(process, log_path)
        return unavailable(
            args.github_env, "mihomo controller did not become ready", args.require_proxy
        )
    if not test_local_proxy(args.test_url):
        stop_and_report(process, log_path)
        return unavailable(
            args.github_env, "source-specific proxy health check failed", args.require_proxy
        )
    append_github_env(
        args.github_env,
        {
            "PROXY_ENABLED": "true",
            "PROXY_CONFIG_FILE": str(proxy_config),
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "ALL_PROXY": "socks5://127.0.0.1:7891",
            "http_proxy": "http://127.0.0.1:7890",
            "https_proxy": "http://127.0.0.1:7890",
            "all_proxy": "socks5://127.0.0.1:7891",
            "NO_PROXY": GITHUB_NO_PROXY,
            "no_proxy": GITHUB_NO_PROXY,
        },
    )
    print(
        f"required proxy enabled: nodes={len(proxies)} "
        "route=MATCH,PROXY endpoint=http://127.0.0.1:7890"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
