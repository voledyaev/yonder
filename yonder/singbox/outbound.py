"""VLESS server → sing-box outbound.

The sing-box analogue of `yonder.xray.build_outbound`. Maps a parsed
`Server` (with its vless:// query params) to a sing-box `vless` outbound.
Supports Reality (most common today) and plain TLS, over tcp / ws / grpc.

sing-box differs from xray in shape:
  * top-level `server` / `server_port` / `uuid` / `flow` (no `vnext` nesting)
  * TLS is a `tls` block with nested `utls` (uTLS fingerprint) and `reality`
  * ws/grpc go in a `transport` block
"""

from __future__ import annotations

from typing import Any

from yonder.vless import Server


def outbound_tag(subscription_id: str, server_id: str) -> str:
    """Stable, globally-unique tag for one server's outbound.

    Composite (subscription + server) because two subscriptions can hold the
    same host:port; the selector and the Clash API switch by this tag, so it
    must be unique across the whole config. This is also the value yonder
    PUTs to `/proxies/<selector>` to switch servers.
    """
    return f"{subscription_id}/{server_id}"


def build_vless_outbound(srv: Server, tag: str) -> dict[str, Any]:
    """Build a sing-box `vless` outbound for `srv` under `tag`."""
    p = srv.params

    out: dict[str, Any] = {
        "type": "vless",
        "tag": tag,
        "server": srv.host,
        "server_port": srv.port,
        "uuid": srv.uuid,
    }
    if flow := p.get("flow"):
        out["flow"] = flow

    security = p.get("security") or "none"
    if security == "reality":
        out["tls"] = {
            "enabled": True,
            "server_name": p.get("sni", ""),
            "utls": {"enabled": True, "fingerprint": p.get("fp") or "chrome"},
            "reality": {
                "enabled": True,
                "public_key": p.get("pbk", ""),
                "short_id": p.get("sid", ""),
            },
        }
    elif security == "tls":
        out["tls"] = {
            "enabled": True,
            "server_name": p.get("sni") or srv.host,
            "utls": {"enabled": True, "fingerprint": p.get("fp") or "chrome"},
            "alpn": ["h2", "http/1.1"],
        }

    network = p.get("type") or "tcp"
    if network == "ws":
        transport: dict[str, Any] = {"type": "ws", "path": p.get("path") or "/"}
        if host := p.get("host"):
            transport["headers"] = {"Host": host}
        out["transport"] = transport
    elif network == "grpc":
        out["transport"] = {"type": "grpc", "service_name": (p.get("path") or "").lstrip("/")}

    return out
