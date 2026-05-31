"""Tests for the sing-box rules validator/normaliser (parse_singbox_rules)."""

from __future__ import annotations

import json

import pytest
from yonder.rules import RulesParseError, parse_singbox_rules


def test_route_wrapped_shape():
    rules = parse_singbox_rules(
        b'{"route": {"rules": [{"domain_suffix": [".ru"], "outbound": "direct"}]}}'
    )
    assert rules == [{"domain_suffix": [".ru"], "outbound": "direct"}]


def test_rules_wrapped_shape():
    rules = parse_singbox_rules(b'{"rules": [{"rule_set": ["geoip-ru"], "outbound": "direct"}]}')
    assert len(rules) == 1


def test_bare_array_shape():
    rules = parse_singbox_rules(b'[{"domain_suffix": ["meduza.io"], "outbound": "proxy"}]')
    assert rules[0]["outbound"] == "proxy"


def test_comments_are_stripped_recursively():
    raw = json.dumps(
        {
            "_comment": "top",
            "rules": [
                {
                    "_comment": "rule note",
                    "domain_suffix": ["x.com"],
                    "outbound": "direct",
                }
            ],
        }
    )
    rules = parse_singbox_rules(raw)
    assert rules == [{"domain_suffix": ["x.com"], "outbound": "direct"}]
    assert "_comment" not in rules[0]


def test_proxy_outbound_allowed_as_alias():
    rules = parse_singbox_rules(b'[{"domain_suffix": ["bbc.com"], "outbound": "proxy"}]')
    assert rules[0]["outbound"] == "proxy"


def test_action_rule_without_outbound_ok():
    rules = parse_singbox_rules(b'[{"action": "sniff"}]')
    assert rules == [{"action": "sniff"}]


def test_invalid_action_rejected():
    with pytest.raises(RulesParseError, match="not a known sing-box action"):
        parse_singbox_rules(b'[{"action": "teleport"}]')


def test_missing_outbound_rejected():
    with pytest.raises(RulesParseError, match="outbound is missing"):
        parse_singbox_rules(b'[{"domain_suffix": ["x.com"]}]')


def test_unknown_outbound_rejected():
    with pytest.raises(RulesParseError, match="must be one of"):
        parse_singbox_rules(b'[{"domain_suffix": ["x.com"], "outbound": "wormhole"}]')


def test_rule_without_matcher_rejected():
    with pytest.raises(RulesParseError, match="no matcher"):
        parse_singbox_rules(b'[{"outbound": "direct"}]')


def test_xray_rule_rejected_with_pointer():
    with pytest.raises(RulesParseError, match="looks like an xray rule"):
        parse_singbox_rules(b'[{"type": "field", "outboundTag": "direct", "ip": ["10.0.0.0/8"]}]')


def test_empty_rules_rejected():
    with pytest.raises(RulesParseError, match="empty"):
        parse_singbox_rules(b'{"rules": []}')


def test_invalid_json_rejected():
    with pytest.raises(RulesParseError, match="not valid JSON"):
        parse_singbox_rules(b"{not json")


def test_real_routing_config_validates():
    """The shipped singbox-routing-russia.json must parse cleanly."""
    from pathlib import Path

    cfg = Path(
        "/Users/v.o.ledyaev/Projects/vpn-configs/singbox-routing-russia/singbox-routing-russia.json"
    )
    if not cfg.exists():
        pytest.skip("routing config not present in this checkout")
    rules = parse_singbox_rules(cfg.read_bytes())
    assert len(rules) == 5
    assert all("_comment" not in r for r in rules)
    # RKN-blocked rule routes to the proxy alias
    assert any(r.get("outbound") == "proxy" for r in rules)
