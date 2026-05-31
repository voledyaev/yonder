"""Hermetic tests for the sing-box config generator (yonder.singbox.*).

No router, no sing-box binary — pure dict-shape assertions. The shapes here
were validated against `sing-box check` v1.13 on the actual device; these
tests lock them so refactors don't silently break the generated config.
"""

from __future__ import annotations

from yonder.singbox.config import (
    SELECTOR_TAG,
    active_tag,
    build_config,
    selector_default,
)
from yonder.singbox.dns import DNS_DIRECT, DNS_PROXY, build_dns
from yonder.singbox.outbound import build_vless_outbound, outbound_tag
from yonder.singbox.route import GEOIP_RU, GEOSITE_RU, build_route, default_route_rules
from yonder.state import ActiveServerRef, Data, DnsState, Subscription
from yonder.vless import Server


def _reality_server(host="de-dp-01.com", port=8443, uuid="u-1") -> Server:
    return Server(
        id=f"{host}:{port}",
        name="DE",
        country="DE",
        host=host,
        port=port,
        uuid=uuid,
        params={
            "flow": "xtls-rprx-vision",
            "security": "reality",
            "sni": "storage.example.com",
            "fp": "firefox",
            "pbk": "publickeybase64",
            "sid": "abcd1234",
            "type": "tcp",
        },
    )


def _data(*, vpn_on=True, active=True, rules=None) -> Data:
    srv = _reality_server()
    sub = Subscription(
        id="sub-1", label="liberty", source="https://x", fetched_at="t", servers=[srv]
    )
    ref = ActiveServerRef(subscription_id="sub-1", server_id=srv.id) if active else None
    return Data(
        subscriptions=[sub],
        active_server=ref,
        vpn_on=vpn_on,
        rules=rules or [],
        dns=DnsState(),
    )


# --- outbound ---------------------------------------------------------------


def test_reality_outbound_shape():
    srv = _reality_server()
    ob = build_vless_outbound(srv, "tagX")
    assert ob["type"] == "vless"
    assert ob["tag"] == "tagX"
    assert ob["server"] == "de-dp-01.com"
    assert ob["server_port"] == 8443
    assert ob["uuid"] == "u-1"
    assert ob["flow"] == "xtls-rprx-vision"
    assert ob["tls"]["enabled"] is True
    assert ob["tls"]["server_name"] == "storage.example.com"
    assert ob["tls"]["utls"] == {"enabled": True, "fingerprint": "firefox"}
    assert ob["tls"]["reality"] == {
        "enabled": True,
        "public_key": "publickeybase64",
        "short_id": "abcd1234",
    }
    # tcp network → no transport block
    assert "transport" not in ob


def test_flow_omitted_when_absent():
    srv = Server(
        id="h:443",
        name="x",
        country="??",
        host="h",
        port=443,
        uuid="u",
        params={"security": "reality", "pbk": "k", "sni": "s"},
    )
    assert "flow" not in build_vless_outbound(srv, "t")


def test_plain_tls_outbound():
    srv = Server(
        id="h:443",
        name="x",
        country="??",
        host="h",
        port=443,
        uuid="u",
        params={"security": "tls", "sni": "s", "fp": "chrome"},
    )
    ob = build_vless_outbound(srv, "t")
    assert ob["tls"]["enabled"] is True
    assert "reality" not in ob["tls"]
    assert ob["tls"]["alpn"] == ["h2", "http/1.1"]


def test_ws_transport():
    srv = Server(
        id="h:443",
        name="x",
        country="??",
        host="h",
        port=443,
        uuid="u",
        params={"security": "tls", "type": "ws", "path": "/ray", "host": "cdn.example.com"},
    )
    ob = build_vless_outbound(srv, "t")
    assert ob["transport"] == {
        "type": "ws",
        "path": "/ray",
        "headers": {"Host": "cdn.example.com"},
    }


def test_grpc_transport():
    srv = Server(
        id="h:443",
        name="x",
        country="??",
        host="h",
        port=443,
        uuid="u",
        params={"security": "reality", "type": "grpc", "path": "/GunService", "pbk": "k"},
    )
    ob = build_vless_outbound(srv, "t")
    assert ob["transport"] == {"type": "grpc", "service_name": "GunService"}


def test_outbound_tag_is_composite_and_unique():
    assert outbound_tag("sub-1", "h:443") == "sub-1/h:443"
    assert outbound_tag("sub-1", "h:443") != outbound_tag("sub-2", "h:443")


# --- route ------------------------------------------------------------------


def test_route_default_rules_ru_and_private_direct():
    r = build_route(None, SELECTOR_TAG)
    # sniff + resolve must come first so domain/rule_set rules can match.
    assert r["rules"][0] == {"action": "sniff"}
    assert r["rules"][1] == {"action": "resolve"}
    assert {"ip_is_private": True, "outbound": "direct"} in r["rules"]
    assert {"rule_set": [GEOIP_RU, GEOSITE_RU], "outbound": "direct"} in r["rules"]
    assert r["final"] == SELECTOR_TAG
    assert r["auto_detect_interface"] is True
    tags = {rs["tag"] for rs in r["rule_set"]}
    assert tags == {GEOIP_RU, GEOSITE_RU}


def test_route_user_rules_replace_default_after_baseline():
    user = [{"domain_suffix": [".ru"], "outbound": "direct"}]
    r = build_route(user, SELECTOR_TAG)
    # baseline sniff/resolve/private still present, then the user rule
    assert r["rules"][0] == {"action": "sniff"}
    assert user[0] in r["rules"]
    # default geo rule NOT auto-added when user supplies rules
    assert {"rule_set": [GEOIP_RU, GEOSITE_RU], "outbound": "direct"} not in r["rules"]


def test_default_route_rules_are_native():
    # Must not contain xray-style {"type":"field"} entries.
    for rule in default_route_rules():
        assert "type" not in rule


def test_proxy_alias_rewritten_to_selector():
    user = [
        {"domain_suffix": ["meduza.io"], "outbound": "proxy"},
        {"domain_suffix": [".ru"], "outbound": "direct"},
    ]
    r = build_route(user, SELECTOR_TAG)
    rewritten = next(rule for rule in r["rules"] if rule.get("domain_suffix") == ["meduza.io"])
    assert rewritten["outbound"] == SELECTOR_TAG  # "proxy" → selector
    passthrough = next(rule for rule in r["rules"] if rule.get("domain_suffix") == [".ru"])
    assert passthrough["outbound"] == "direct"  # untouched


# --- dns --------------------------------------------------------------------


def test_dns_two_resolvers_with_detours():
    dns = build_dns("https://cloudflare-dns.com/dns-query", SELECTOR_TAG)
    by_tag = {s["tag"]: s for s in dns["servers"]}
    assert by_tag[DNS_PROXY]["type"] == "https"
    assert by_tag[DNS_PROXY]["server"] == "cloudflare-dns.com"
    assert by_tag[DNS_PROXY]["path"] == "/dns-query"
    assert by_tag[DNS_PROXY]["detour"] == SELECTOR_TAG  # foreign DNS over proxy
    assert by_tag[DNS_DIRECT]["type"] == "udp"
    # No detour — sing-box rejects detouring to a plain direct outbound; the
    # RU resolver routes direct via geoip-ru anyway.
    assert "detour" not in by_tag[DNS_DIRECT]
    assert dns["final"] == DNS_PROXY
    # RU domains resolve via the direct resolver.
    assert {"rule_set": [GEOSITE_RU], "server": DNS_DIRECT} in dns["rules"]


def test_dns_doh_without_path():
    dns = build_dns("https://dns.example", SELECTOR_TAG)
    proxy = next(s for s in dns["servers"] if s["tag"] == DNS_PROXY)
    assert proxy["server"] == "dns.example"
    assert "path" not in proxy


# --- config assembly --------------------------------------------------------


def test_config_full_shape_and_selector_membership():
    cfg = build_config(_data())
    assert [i["type"] for i in cfg["inbounds"]] == ["tun"]
    types = [o["type"] for o in cfg["outbounds"]]
    assert types == ["vless", "selector", "direct", "block"]
    selector = next(o for o in cfg["outbounds"] if o["type"] == "selector")
    # every selector member resolves to a real outbound tag (+ direct)
    real_tags = {o["tag"] for o in cfg["outbounds"]}
    for member in selector["outbounds"]:
        assert member in real_tags
    assert "direct" in selector["outbounds"]
    assert cfg["experimental"]["clash_api"]["external_controller"]
    assert cfg["route"]["default_domain_resolver"] == DNS_DIRECT


def test_selector_default_on_points_at_active():
    snap = _data(vpn_on=True, active=True)
    assert active_tag(snap) == "sub-1/de-dp-01.com:8443"
    assert selector_default(snap) == "sub-1/de-dp-01.com:8443"
    assert build_config(snap)["outbounds"][1]["default"] == "sub-1/de-dp-01.com:8443"


def test_selector_default_off_points_at_direct():
    assert selector_default(_data(vpn_on=False, active=True)) == "direct"


def test_selector_default_direct_when_no_active():
    assert selector_default(_data(vpn_on=True, active=False)) == "direct"


def test_active_tag_none_for_dangling_selection():
    snap = _data(active=False)
    snap.active_server = ActiveServerRef(subscription_id="sub-1", server_id="ghost:1")
    assert active_tag(snap) is None
    assert selector_default(snap) == "direct"


def test_empty_state_selector_only_direct():
    cfg = build_config(Data())
    selector = next(o for o in cfg["outbounds"] if o["type"] == "selector")
    assert selector["outbounds"] == ["direct"]
    assert selector["default"] == "direct"
