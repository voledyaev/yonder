"""sing-box `route` block builder.

Mirrors the intent of yonder's xray 05_routing.json, translated to sing-box:
RU destinations (and private/LAN) go `direct`; everything else falls through
to the selector (the proxy). Domain matching needs sniffing, so the first
rule sniffs TLS/HTTP to recover the destination domain from tun packets.

Geo data is local `.srs` rule-sets installed alongside sing-box (the analogue
of today's bundled geoip.dat/geosite.dat) — offline, deterministic, no
startup egress dependency.
"""

from __future__ import annotations

from typing import Any

# Where the installer drops sing-box's geo rule-sets.
GEO_DIR = "/opt/etc/sing-box"
GEOIP_RU = "geoip-ru"
GEOSITE_RU = "geosite-ru"

# In user-supplied rules, `outbound: "proxy"` is a stable alias for "send this
# through the VPN" — rewritten to the real selector tag at build time. Lets a
# routing config stay portable and readable without baking in yonder's
# internal selector tag name.
PROXY_ALIAS = "proxy"


def rule_set_defs() -> list[dict[str, Any]]:
    """Local binary rule-set declarations referenced by the route rules."""
    return [
        {
            "type": "local",
            "tag": GEOIP_RU,
            "format": "binary",
            "path": f"{GEO_DIR}/{GEOIP_RU}.srs",
        },
        {
            "type": "local",
            "tag": GEOSITE_RU,
            "format": "binary",
            "path": f"{GEO_DIR}/{GEOSITE_RU}.srs",
        },
    ]


def default_route_rules() -> list[dict[str, Any]]:
    """RU + private → direct; everything else → selector (via `final`).

    The bundled fallback when the user supplies no custom rules.
    """
    return [
        {"ip_is_private": True, "outbound": "direct"},
        {"rule_set": [GEOIP_RU, GEOSITE_RU], "outbound": "direct"},
    ]


def build_route(
    user_rules: list[dict[str, Any]] | None,
    selector_tag: str,
) -> dict[str, Any]:
    """Assemble the full `route` block.

    `user_rules` are sing-box-native route rules (validated upstream). They
    run after the baseline sniff + private-direct so a user can still force,
    say, an RKN-blocked .ru domain to the proxy. When empty, the conservative
    default (RU/private → direct) is used.

    `selector_tag` is the `final` outbound — the selector that holds all
    servers, so unmatched (foreign) traffic is proxied.
    """
    rules: list[dict[str, Any]] = [
        # Recover the destination domain from tun packets so domain/rule_set
        # matching works (tun delivers IP packets; without sniffing only
        # ip_cidr/rule_set-ip rules would match).
        {"action": "sniff"},
        # Resolve sniffed domains before IP rules evaluate (parity with
        # xray's domainStrategy="IPIfNonMatch").
        {"action": "resolve"},
        {"ip_is_private": True, "outbound": "direct"},
    ]
    chosen = user_rules if user_rules else default_route_rules()
    rules.extend(_resolve_proxy_alias(rule, selector_tag) for rule in chosen)

    return {
        "rules": rules,
        "rule_set": rule_set_defs(),
        "final": selector_tag,
        "auto_detect_interface": True,
    }


def _resolve_proxy_alias(rule: dict[str, Any], selector_tag: str) -> dict[str, Any]:
    """Rewrite `outbound: "proxy"` to the real selector tag; pass through else."""
    if rule.get("outbound") == PROXY_ALIAS:
        return {**rule, "outbound": selector_tag}
    return rule
