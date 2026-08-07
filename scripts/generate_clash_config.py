"""Build a minimal mihomo config from the subscription formats used by the crawlers."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
import yaml


SUBSCRIPTION_USER_AGENT = os.getenv(
    "PROXY_SUBSCRIPTION_USER_AGENT", "mihomo/1.19.13"
)


def redact_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        return "<invalid-url>"
    return f"{parsed.scheme}://{parsed.netloc}/***"


def decode_base64_text(value: str) -> str:
    normalized = value.strip()
    normalized += "=" * (-len(normalized) % 4)
    return base64.b64decode(normalized).decode("utf-8")


class QuotedYamlString(str):
    """Force YAML to preserve numeric-looking Reality short IDs as strings."""


class QuotedSafeDumper(yaml.SafeDumper):
    pass


def _represent_quoted_yaml_string(
    dumper: yaml.SafeDumper, value: QuotedYamlString
) -> yaml.ScalarNode:
    return dumper.represent_scalar(
        "tag:yaml.org,2002:str", str(value), style="'"
    )


QuotedSafeDumper.add_representer(
    QuotedYamlString, _represent_quoted_yaml_string
)


class ClashConfigGenerator:
    def __init__(self, config_path: str = "/tmp/mihomo/config.yaml"):
        self.config_path = config_path

    def fetch_subscription(self, url: str, indirection_depth: int = 0) -> str:
        if indirection_depth >= 3:
            print("subscription indirection limit reached")
            return ""
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(
                url,
                headers={
                    "User-Agent": SUBSCRIPTION_USER_AGENT,
                    "Accept": "text/plain,application/yaml,application/json,*/*",
                },
                timeout=30,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"subscription fetch failed: {redact_url(url)} {type(exc).__name__}")
            return ""
        content = response.text.strip()
        print(
            f"subscription fetched: {redact_url(url)} "
            f"HTTP {response.status_code} bytes={len(content)}"
        )
        if content.startswith(("http://", "https://")):
            return self.fetch_subscription(content, indirection_depth + 1)
        if content.startswith(
            ("proxies:", "port:", "mixed-port:", "vmess://", "vless://", "trojan://", "ss://")
        ):
            return content
        try:
            return decode_base64_text(content)
        except (ValueError, UnicodeDecodeError, base64.binascii.Error):
            return content

    @staticmethod
    def _first(params: dict[str, list[str]], *names: str, default: Any = None) -> Any:
        for name in names:
            values = params.get(name)
            if values and values[0] != "":
                return values[0]
        return default

    @staticmethod
    def _valid_reality_short_id(value: str) -> bool:
        normalized = value.strip()
        return (
            len(normalized) % 2 == 0
            and len(normalized) <= 16
            and bool(re.fullmatch(r"[0-9a-fA-F]+", normalized))
        )

    def parse_vmess(self, link: str) -> dict[str, Any] | None:
        try:
            data = json.loads(decode_base64_text(link[8:]))
            proxy: dict[str, Any] = {
                "name": data.get("ps") or "vmess",
                "type": "vmess",
                "server": data.get("add"),
                "port": int(data.get("port", 443)),
                "uuid": data.get("id"),
                "alterId": int(data.get("aid", 0)),
                "cipher": data.get("scy", "auto"),
                "udp": True,
            }
            network = data.get("net", "tcp")
            if network == "ws":
                proxy["network"] = "ws"
                proxy["ws-opts"] = {
                    "path": data.get("path", "/"),
                    "headers": {"Host": data.get("host") or data.get("add")},
                }
            elif network == "grpc":
                proxy["network"] = "grpc"
                proxy["grpc-opts"] = {
                    "grpc-service-name": data.get("path", "")
                }
            if data.get("tls") == "tls":
                proxy["tls"] = True
                proxy["servername"] = (
                    data.get("sni") or data.get("host") or data.get("add")
                )
            return proxy
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"vmess node ignored: {type(exc).__name__}")
            return None

    def parse_vless(self, link: str) -> dict[str, Any] | None:
        try:
            parsed = urlparse(link)
            params = parse_qs(parsed.query, keep_blank_values=True)
            proxy: dict[str, Any] = {
                "name": unquote(parsed.fragment) or "vless",
                "type": "vless",
                "server": parsed.hostname,
                "port": parsed.port,
                "uuid": parsed.username,
                "udp": True,
            }
            flow = self._first(params, "flow")
            if flow:
                proxy["flow"] = flow
            security = self._first(params, "security")
            encryption = self._first(params, "encryption")
            if encryption:
                proxy["encryption"] = encryption
            elif security == "reality":
                proxy["encryption"] = "none"
            if security in {"tls", "reality"}:
                proxy["tls"] = True
                servername = self._first(params, "sni", "host")
                if servername:
                    proxy["servername"] = servername
            fingerprint = self._first(params, "fp", "client-fingerprint")
            if fingerprint:
                proxy["client-fingerprint"] = fingerprint
            if security == "reality":
                reality: dict[str, str] = {}
                public_key = self._first(params, "pbk", "public-key")
                if public_key:
                    reality["public-key"] = public_key
                short_id = self._first(params, "sid", "short-id")
                if short_id and self._valid_reality_short_id(short_id):
                    reality["short-id"] = short_id.strip()
                spider = self._first(params, "spx", "spider-x", "spiderx")
                if spider:
                    reality["spider-x"] = spider
                if reality:
                    proxy["reality-opts"] = reality
            network = self._first(params, "type", default="tcp")
            if network == "ws":
                proxy["network"] = "ws"
                proxy["ws-opts"] = {
                    "path": self._first(params, "path", default="/"),
                    "headers": {
                        "Host": self._first(
                            params, "host", default=parsed.hostname
                        )
                    },
                }
            elif network == "grpc":
                proxy["network"] = "grpc"
                proxy["grpc-opts"] = {
                    "grpc-service-name": self._first(
                        params, "serviceName", default=""
                    )
                }
            return proxy
        except (ValueError, TypeError) as exc:
            print(f"vless node ignored: {type(exc).__name__}")
            return None

    def parse_trojan(self, link: str) -> dict[str, Any] | None:
        try:
            parsed = urlparse(link)
            params = parse_qs(parsed.query)
            proxy: dict[str, Any] = {
                "name": unquote(parsed.fragment) or "trojan",
                "type": "trojan",
                "server": parsed.hostname,
                "port": parsed.port,
                "password": parsed.username,
                "udp": True,
            }
            sni = self._first(params, "sni")
            if sni:
                proxy["sni"] = sni
            network = self._first(params, "type", default="tcp")
            if network == "ws":
                proxy["network"] = "ws"
                proxy["ws-opts"] = {
                    "path": self._first(params, "path", default="/")
                }
            elif network == "grpc":
                proxy["network"] = "grpc"
                proxy["grpc-opts"] = {
                    "grpc-service-name": self._first(
                        params, "serviceName", default=""
                    )
                }
            return proxy
        except (ValueError, TypeError) as exc:
            print(f"trojan node ignored: {type(exc).__name__}")
            return None

    def parse_ss(self, link: str) -> dict[str, Any] | None:
        try:
            payload, _, fragment = link[5:].partition("#")
            name = unquote(fragment) or "ss"
            if "@" in payload:
                userinfo, server_info = payload.rsplit("@", 1)
                try:
                    decoded = decode_base64_text(userinfo)
                except (ValueError, UnicodeDecodeError, base64.binascii.Error):
                    decoded = unquote(userinfo)
                server, port = server_info.rsplit(":", 1)
            else:
                decoded_payload = decode_base64_text(payload)
                decoded, server_info = decoded_payload.rsplit("@", 1)
                server, port = server_info.rsplit(":", 1)
            cipher, password = decoded.split(":", 1)
            return {
                "name": name,
                "type": "ss",
                "server": server.strip("[]"),
                "port": int(port),
                "cipher": cipher,
                "password": password,
                "udp": True,
            }
        except (ValueError, TypeError, UnicodeDecodeError, base64.binascii.Error) as exc:
            print(f"ss node ignored: {type(exc).__name__}")
            return None

    def parse_ssr(self, link: str) -> dict[str, Any] | None:
        try:
            decoded = decode_base64_text(link[6:])
            server, port, protocol, cipher, obfs, remainder = decoded.split(
                ":", 5
            )
            encoded_password, _, query = remainder.partition("/?")
            params = parse_qs(query)
            encoded_name = self._first(params, "remarks")
            return {
                "name": (
                    decode_base64_text(encoded_name)
                    if encoded_name
                    else "ssr"
                ),
                "type": "ssr",
                "server": server,
                "port": int(port),
                "cipher": cipher,
                "password": decode_base64_text(encoded_password),
                "protocol": protocol,
                "obfs": obfs,
                "udp": True,
            }
        except (ValueError, TypeError, UnicodeDecodeError, base64.binascii.Error) as exc:
            print(f"ssr node ignored: {type(exc).__name__}")
            return None

    def parse_hysteria(self, link: str) -> dict[str, Any] | None:
        try:
            parsed = urlparse(link)
            params = parse_qs(parsed.query)
            return {
                "name": unquote(parsed.fragment) or "hysteria",
                "type": "hysteria",
                "server": parsed.hostname,
                "port": parsed.port,
                "password": parsed.username
                or self._first(params, "auth", default=""),
                "obfs": self._first(params, "obfs"),
                "alpn": self._first(params, "alpn", default="h3"),
                "protocol": self._first(params, "protocol", default="udp"),
                "up": self._first(params, "up", default="20 Mbps"),
                "down": self._first(params, "down", default="100 Mbps"),
                "sni": self._first(
                    params, "sni", default=parsed.hostname
                ),
                "skip-cert-verify": self._first(
                    params, "insecure", default="0"
                )
                == "1",
            }
        except (ValueError, TypeError) as exc:
            print(f"hysteria node ignored: {type(exc).__name__}")
            return None

    def parse_hysteria2(self, link: str) -> dict[str, Any] | None:
        try:
            parsed = urlparse(link)
            params = parse_qs(parsed.query)
            proxy: dict[str, Any] = {
                "name": unquote(parsed.fragment) or "hysteria2",
                "type": "hysteria2",
                "server": parsed.hostname,
                "port": parsed.port,
                "password": parsed.username
                or self._first(params, "auth", default=""),
            }
            sni = self._first(params, "sni")
            if sni:
                proxy["sni"] = sni
            obfs = self._first(params, "obfs")
            if obfs:
                proxy["obfs"] = obfs
                proxy["obfs-password"] = self._first(
                    params, "obfs-password", default=""
                )
            return proxy
        except (ValueError, TypeError) as exc:
            print(f"hysteria2 node ignored: {type(exc).__name__}")
            return None

    def parse_tuic(self, link: str) -> dict[str, Any] | None:
        try:
            parsed = urlparse(link)
            params = parse_qs(parsed.query)
            return {
                "name": unquote(parsed.fragment) or "tuic",
                "type": "tuic",
                "server": parsed.hostname,
                "port": parsed.port,
                "uuid": parsed.username,
                "password": parsed.password,
                "alpn": ["h3"],
                "congestion-controller": self._first(
                    params,
                    "congestion_control",
                    "congestion-controller",
                    default="cubic",
                ),
                "sni": self._first(
                    params, "sni", default=parsed.hostname
                ),
                "disable-sni": self._first(
                    params, "disable_sni", default="0"
                )
                == "1",
                "reduce-rtt": self._first(
                    params, "reduce_rtt", default="1"
                )
                == "1",
                "udp-relay-mode": self._first(
                    params, "udp_relay_mode", default="native"
                ),
            }
        except (ValueError, TypeError) as exc:
            print(f"tuic node ignored: {type(exc).__name__}")
            return None

    def parse_wireguard(self, link: str) -> dict[str, Any] | None:
        try:
            parsed = urlparse(link)
            params = parse_qs(parsed.query)
            return {
                "name": unquote(parsed.fragment) or "wireguard",
                "type": "wireguard",
                "server": parsed.hostname,
                "port": parsed.port,
                "private-key": parsed.username,
                "ip": self._first(params, "ip", default=""),
                "ipv6": self._first(params, "ipv6", default=""),
                "public-key": self._first(
                    params, "publickey", "public-key", default=""
                ),
                "dns": self._first(params, "dns", default=""),
                "mtu": int(self._first(params, "mtu", default="1280")),
                "udp": True,
            }
        except (ValueError, TypeError) as exc:
            print(f"wireguard node ignored: {type(exc).__name__}")
            return None

    def parse_link(self, link: str) -> dict[str, Any] | None:
        normalized = link.strip()
        if normalized.startswith("vmess://"):
            return self.parse_vmess(normalized)
        if normalized.startswith("vless://"):
            return self.parse_vless(normalized)
        if normalized.startswith("trojan://"):
            return self.parse_trojan(normalized)
        if normalized.startswith("ss://"):
            return self.parse_ss(normalized)
        if normalized.startswith("ssr://"):
            return self.parse_ssr(normalized)
        if normalized.startswith("hysteria://"):
            return self.parse_hysteria(normalized)
        if normalized.startswith(("hysteria2://", "hy2://")):
            return self.parse_hysteria2(normalized)
        if normalized.startswith("tuic://"):
            return self.parse_tuic(normalized)
        if normalized.startswith("wireguard://"):
            return self.parse_wireguard(normalized)
        return None

    def parse_subscription(
        self, url: str, exclude_keywords: list[str] | None = None
    ) -> list[dict[str, Any]]:
        content = self.fetch_subscription(url)
        if not content:
            return []
        excluded = [value.lower() for value in (exclude_keywords or [])]
        proxies: list[dict[str, Any]] = []
        if content.lstrip().startswith(
            ("proxies:", "port:", "mixed-port:", "socks-port:")
        ):
            try:
                parsed = yaml.safe_load(content)
                raw_proxies = parsed.get("proxies", []) if isinstance(parsed, dict) else []
            except yaml.YAMLError as exc:
                print(f"subscription YAML ignored: {type(exc).__name__}")
                raw_proxies = []
            proxies.extend(
                value for value in raw_proxies if isinstance(value, dict)
            )
        else:
            for line in content.splitlines():
                proxy = self.parse_link(line)
                if proxy:
                    proxies.append(proxy)
        filtered = [
            proxy
            for proxy in proxies
            if not any(
                keyword in str(proxy.get("name", "")).lower()
                for keyword in excluded
            )
        ]
        print(f"subscription nodes parsed: {len(filtered)}")
        return filtered

    def _sanitize(self, value: Any) -> Any:
        if isinstance(value, dict):
            cleaned = {
                key: self._sanitize(item)
                for key, item in value.items()
                if item is not None
            }
            reality = cleaned.get("reality-opts")
            if isinstance(reality, dict) and "short-id" in reality:
                short_id = str(reality["short-id"])
                if self._valid_reality_short_id(short_id):
                    reality["short-id"] = QuotedYamlString(short_id)
                else:
                    reality.pop("short-id")
            return cleaned
        if isinstance(value, list):
            return [self._sanitize(item) for item in value]
        return value

    def generate_config_from_proxies(
        self,
        proxies: list[dict[str, Any]],
        mixed_port: int = 7890,
        socks_port: int = 7891,
        external_controller: str = "127.0.0.1:9090",
        health_check_url: str = "https://www.baidu.com/",
    ) -> str:
        cleaned = [self._sanitize(proxy) for proxy in proxies]
        names = [str(proxy["name"]) for proxy in cleaned]
        config = {
            "mixed-port": mixed_port,
            "socks-port": socks_port,
            "allow-lan": False,
            "bind-address": "127.0.0.1",
            "mode": "rule",
            "log-level": "info",
            "ipv6": False,
            "external-controller": external_controller,
            "proxies": cleaned,
            "proxy-groups": [
                {
                    "name": "PROXY",
                    "type": "select",
                    "proxies": ["BALANCE", *names],
                },
                {
                    "name": "BALANCE",
                    "type": "load-balance",
                    "proxies": names,
                    "url": health_check_url,
                    "interval": 60,
                    "strategy": "round-robin",
                    "health-check": {
                        "enable": True,
                        "url": health_check_url,
                        "interval": 60,
                    },
                },
            ],
            # Source domains must not bypass the proxy merely because they resolve
            # to CN addresses. Only local traffic is direct.
            "rules": ["GEOIP,LAN,DIRECT", "MATCH,PROXY"],
        }
        header = (
            "# Generated crawler proxy configuration\n"
            f"# generated_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
            f"# nodes={len(cleaned)}\n"
        )
        return header + yaml.dump(
            config,
            Dumper=QuotedSafeDumper,
            allow_unicode=True,
            sort_keys=False,
            width=4096,
        )

    def save_config(self, config: str, path: str | None = None) -> None:
        target = Path(path or self.config_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(config, encoding="utf-8")
        print(f"proxy config saved: {target}")
