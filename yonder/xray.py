"""Builds Xray configuration files from the parsed VLESS server and rules.

We intentionally never manage Xray's DNS module (02_dns.json). Earlier
attempts to route DoH through xray — even with the DoH HTTPS endpoint pinned
to the `direct` outbound to break the obvious deadlock — left the router
unresponsive ~3 minutes after every boot. Xray's DNS module appears to
accumulate state on this hardware that the kernel can't keep up with. DNS
bypass for poisoned domains is handled at the router layer (yonder toggles
Keenetic's DoH-upstream around xkeen start/stop).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from yonder.vless import Server

# XKeen runs xray with `-confdir /opt/etc/xray/configs/` — every .json in
# that directory is merged. The default XKeen install ships six files
# (01_log..06_policy). We only own 04_outbounds and 05_routing; we leave
# 01_log/02_dns/03_inbounds/06_policy at XKeen's tested defaults — that way
# we don't have to fight XKeen's tproxy/iptables setup.
XKEEN_CONFIGS_DIR = "/opt/etc/xray/configs"
OUTBOUNDS_FILE = "04_outbounds.json"
ROUTING_FILE = "05_routing.json"


def build_outbound(srv: Server) -> dict[str, Any]:
    """Build the `proxy` outbound for a VLESS server.

    Supports VLESS over Reality (most common today) and plain TLS as a
    fallback. WS/gRPC transports are wired through if `type` indicates so.
    """
    p = srv.params

    user: dict[str, Any] = {"id": srv.uuid, "encryption": "none"}
    if flow := p.get("flow"):
        user["flow"] = flow

    network = p.get("type") or "tcp"
    security = p.get("security") or "none"
    stream: dict[str, Any] = {"network": network, "security": security}

    if security == "reality":
        stream["realitySettings"] = {
            "serverName": p.get("sni", ""),
            "fingerprint": p.get("fp") or "chrome",
            "publicKey": p.get("pbk", ""),
            "shortId": p.get("sid", ""),
            # Some providers set `spx` in the link to control the post-
            # handshake disguise GET that Reality issues against the SNI
            # host. Honor it; default to no extra step.
            "spiderX": p.get("spx", ""),
        }
    elif security == "tls":
        stream["tlsSettings"] = {
            "serverName": p.get("sni") or p.get("host", ""),
            "fingerprint": p.get("fp") or "chrome",
            "alpn": ["h2", "http/1.1"],
        }

    if network == "ws":
        ws_headers = {"Host": p["host"]} if p.get("host") else {}
        stream["wsSettings"] = {
            "path": p.get("path") or "/",
            "headers": ws_headers,
        }
    elif network == "grpc":
        stream["grpcSettings"] = {"serviceName": (p.get("path") or "").lstrip("/")}

    return {
        "tag": "proxy",
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": srv.host,
                    "port": srv.port,
                    "users": [user],
                }
            ]
        },
        "streamSettings": stream,
    }


def default_rules() -> list[dict[str, Any]]:
    """Conservative bundled fallback: "everything through the VPN, only
    RFC-1918 / loopback / link-local / multicast goes direct".

    Uses raw CIDRs (not `geoip:private`) so we don't depend on geoip.dat.
    """
    return [
        {
            "type": "field",
            "outboundTag": "direct",
            "ip": [
                "10.0.0.0/8",
                "172.16.0.0/12",
                "192.168.0.0/16",
                "127.0.0.0/8",
                "169.254.0.0/16",
                "100.64.0.0/10",
                "224.0.0.0/4",
                "::1/128",
                "fc00::/7",
                "fe80::/10",
                "ff00::/8",
            ],
        }
    ]


def write_xkeen_split(
    srv: Server | None,
    rules: list[dict[str, Any]] | None,
    configs_dir: str | Path = XKEEN_CONFIGS_DIR,
) -> None:
    """Write the two files yonder owns (04_outbounds, 05_routing).

    If srv is None, the proxy outbound is omitted and traffic falls through
    to direct — defensive fallback; the daemon should also stop xkeen when
    vpn_on is false.

    rules may be None or empty — default_rules() is used in that case.
    """
    outbounds: list[dict[str, Any]] = []
    if srv is not None:
        outbounds.append(build_outbound(srv))
    outbounds.extend(
        [
            {
                "tag": "direct",
                "protocol": "freedom",
                "streamSettings": {"sockopt": {"mark": 255}},
            },
            {"tag": "block", "protocol": "blackhole"},
        ]
    )

    if not rules:
        rules = default_rules()

    base = Path(configs_dir)
    _atomic_write_json(base / OUTBOUNDS_FILE, {"outbounds": outbounds})
    _atomic_write_json(
        base / ROUTING_FILE,
        {"routing": {"domainStrategy": "AsIs", "rules": rules}},
    )


def _atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(obj, indent=2).encode()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(raw)
    tmp.replace(path)
