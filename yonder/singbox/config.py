"""Assemble a complete sing-box config from a yonder state snapshot.

Pure: `build_config(snap) -> dict`. No I/O — `service.py` serialises and
writes it. The shape is the one validated by `sing-box check` on the router.

Server switching and on/off do NOT regenerate this config — they're live
Clash API calls against the `selector`. This config is rewritten only when
the *set* of servers, the routing rules, or the DNS upstream change.
"""

from __future__ import annotations

from typing import Any

from yonder.singbox.dns import DNS_DIRECT, build_dns
from yonder.singbox.outbound import build_vless_outbound, outbound_tag
from yonder.singbox.route import build_route
from yonder.state import Data

SELECTOR_TAG = "select"
TUN_NAME = "sing-box0"
TUN_ADDRESS = "172.19.0.1/30"
CLASH_API_ADDR = "127.0.0.1:9090"
# gvisor stack tolerates a large tun MTU; 1500 keeps router-originated TCP
# clear of fragmentation edge cases through the nested VLESS transport.
TUN_MTU = 1500


def _server_outbounds(snap: Data) -> list[tuple[str, dict[str, Any]]]:
    """(tag, outbound) for every server across all subscriptions.

    Tags are composite (subscription/server) and unique, so identical
    host:port in two subscriptions don't collide in the selector.
    """
    out: list[tuple[str, dict[str, Any]]] = []
    for sub in snap.subscriptions:
        for srv in sub.servers:
            tag = outbound_tag(sub.id, srv.id)
            out.append((tag, build_vless_outbound(srv, tag)))
    return out


def active_tag(snap: Data) -> str | None:
    """The outbound tag of the active server, or None if unset / dangling."""
    ref = snap.active_server
    if ref is None:
        return None
    for sub in snap.subscriptions:
        if sub.id != ref.subscription_id:
            continue
        if any(srv.id == ref.server_id for srv in sub.servers):
            return outbound_tag(ref.subscription_id, ref.server_id)
    return None


def selector_default(snap: Data) -> str:
    """What the selector should point at: the active server when VPN is on
    and the selection resolves, else `direct` (VPN off / no valid server)."""
    if snap.vpn_on:
        tag = active_tag(snap)
        if tag is not None:
            return tag
    return "direct"


def build_config(snap: Data) -> dict[str, Any]:
    server_obs = _server_outbounds(snap)
    server_tags = [tag for tag, _ in server_obs]

    selector = {
        "type": "selector",
        "tag": SELECTOR_TAG,
        # `direct` is a member so on/off is a pure selector switch (no
        # process restart): off → select `direct`, on → select a server.
        "outbounds": [*server_tags, "direct"],
        "default": selector_default(snap),
    }

    outbounds: list[dict[str, Any]] = [ob for _, ob in server_obs]
    outbounds.append(selector)
    outbounds.append({"type": "direct", "tag": "direct"})
    outbounds.append({"type": "block", "tag": "block"})

    route = build_route(snap.rules or None, SELECTOR_TAG)
    # Resolve outbound *server* domains over the direct resolver, breaking the
    # bootstrap loop (DoH detours through the proxy, whose server domain must
    # itself be resolved first — not through the proxy).
    route["default_domain_resolver"] = DNS_DIRECT

    return {
        "log": {"level": "warn", "timestamp": True},
        "dns": build_dns(snap.dns.doh_url, SELECTOR_TAG),
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "interface_name": TUN_NAME,
                "address": [TUN_ADDRESS],
                "mtu": TUN_MTU,
                "auto_route": True,
                "strict_route": False,
                "stack": "gvisor",
            }
        ],
        "outbounds": outbounds,
        "route": route,
        "experimental": {"clash_api": {"external_controller": CLASH_API_ADDR}},
    }
