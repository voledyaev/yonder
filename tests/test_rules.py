import pytest
from yonder.rules import RulesParseError, parse_xray_rules


def test_full_routing_shape():
    rules = parse_xray_rules(
        b'{"routing": {"rules": [{"outboundTag": "direct", "ip": ["10.0.0.0/8"]}]}}'
    )
    assert len(rules) == 1
    # type=field auto-filled by parser
    assert rules[0]["type"] == "field"


def test_rules_only_shape():
    rules = parse_xray_rules(b'{"rules": [{"outboundTag": "proxy", "domain": ["example.com"]}]}')
    assert len(rules) == 1


def test_bare_array_shape():
    rules = parse_xray_rules(b'[{"outboundTag": "block", "ip": ["1.1.1.1"]}]')
    assert len(rules) == 1


def test_rejects_invalid_json():
    with pytest.raises(RulesParseError, match="not valid JSON"):
        parse_xray_rules(b"{ not json")


def test_rejects_unknown_top_level_key():
    with pytest.raises(RulesParseError, match="rules"):
        parse_xray_rules(b'{"foo": "bar"}')


def test_rejects_top_level_string():
    with pytest.raises(RulesParseError, match="object or array"):
        parse_xray_rules(b'"just a string"')


def test_rejects_empty_rules_array():
    with pytest.raises(RulesParseError, match="empty"):
        parse_xray_rules(b'{"rules": []}')


def test_rejects_invalid_outbound_tag():
    with pytest.raises(RulesParseError, match="outboundTag"):
        parse_xray_rules(b'[{"outboundTag": "internet", "ip": ["1.1.1.1"]}]')


def test_rejects_no_match_field():
    with pytest.raises(RulesParseError, match="match field"):
        parse_xray_rules(b'[{"outboundTag": "direct"}]')


def test_preserves_existing_type():
    rules = parse_xray_rules(b'[{"outboundTag": "direct", "ip": ["1.1.1.1"], "type": "custom"}]')
    assert rules[0]["type"] == "custom"


def test_multiple_valid_rules():
    rules = parse_xray_rules(b"""[
        {"outboundTag": "direct", "ip": ["10.0.0.0/8"]},
        {"outboundTag": "proxy", "domain": ["example.com"]},
        {"outboundTag": "block", "domain": ["ads.example.com"]}
    ]""")
    assert len(rules) == 3
    for r in rules:
        assert r["type"] == "field"


def test_rejects_rule_not_an_object():
    with pytest.raises(RulesParseError, match="not an object"):
        parse_xray_rules(b'[{"outboundTag": "direct", "ip": ["1.1.1.1"]}, "string-rule"]')


@pytest.mark.parametrize(
    "field",
    [
        "domain",
        "ip",
        "port",
        "network",
        "source",
        "user",
        "inboundTag",
        "protocol",
    ],
)
def test_all_match_fields_accepted(field):
    text = f'[{{"outboundTag": "direct", "{field}": ["x"]}}]'
    rules = parse_xray_rules(text.encode())
    assert len(rules) == 1
