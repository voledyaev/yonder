import base64

import pytest
from yonder.vless import (
    VlessParseError,
    detect_country,
    parse_link,
    parse_subscription,
)


@pytest.mark.parametrize(
    "fragment,expected",
    [
        # Flag emoji
        ("\U0001f1f5\U0001f1f1 Poland", "PL"),
        ("\U0001f1e9\U0001f1ea", "DE"),
        ("\U0001f1fa\U0001f1f8 USA", "US"),
        # Native-language names
        ("Польша", "PL"),
        ("Германия", "DE"),
        ("Финляндия", "FI"),
        # English names
        ("Germany", "DE"),
        ("united states", "US"),
        # Decoration stripped
        ("⚡Польша", "PL"),
        ("(Germany)", "DE"),
        # Unknown
        ("Atlantis", "??"),
        ("", "??"),
    ],
)
def test_detect_country(fragment, expected):
    assert detect_country(fragment) == expected


def test_parse_link_basic_reality():
    uri = (
        "vless://5ce044d1-6a0b-4dc5-b2c9-6eb296642a1c@example.com:8443"
        "?security=reality&type=tcp&flow=xtls-rprx-vision&sni=test.example"
        "&fp=chrome&pbk=KEY&sid=SID#%F0%9F%87%B5%F0%9F%87%B1Poland"
    )
    srv = parse_link(uri)
    assert srv.host == "example.com"
    assert srv.port == 8443
    assert srv.uuid == "5ce044d1-6a0b-4dc5-b2c9-6eb296642a1c"
    assert srv.id == "example.com:8443"
    assert srv.country == "PL"
    assert srv.params["security"] == "reality"
    assert srv.params["pbk"] == "KEY"


@pytest.mark.parametrize(
    "uri",
    [
        "vless://@example.com:8443",  # missing uuid
        "vless://uuid@:8443",  # missing host
        "vmess://uuid@example.com:8443",  # wrong scheme
    ],
)
def test_parse_link_errors(uri):
    with pytest.raises(VlessParseError):
        parse_link(uri)


def test_parse_link_default_port():
    srv = parse_link("vless://uuid-x@example.com")
    assert srv.port == 443


def test_parse_link_no_fragment_uses_id_as_name():
    srv = parse_link("vless://uuid-x@example.com:1234")
    assert srv.name == "example.com:1234"


SUBSCRIPTION_URIS = [
    "vless://aaa@host1.com:443?security=reality#%F0%9F%87%B5%F0%9F%87%B1Poland",
    "vless://bbb@host2.com:8443?security=reality#%F0%9F%87%A9%F0%9F%87%AAGermany",
]


def test_parse_subscription_plaintext():
    body = "\n".join(SUBSCRIPTION_URIS).encode()
    servers = parse_subscription(body)
    assert len(servers) == 2
    assert servers[0].country == "PL"
    assert servers[1].country == "DE"


def test_parse_subscription_base64():
    encoded = base64.b64encode("\n".join(SUBSCRIPTION_URIS).encode())
    servers = parse_subscription(encoded)
    assert len(servers) == 2


def test_parse_subscription_base64_no_padding():
    encoded = base64.b64encode("\n".join(SUBSCRIPTION_URIS).encode()).rstrip(b"=")
    servers = parse_subscription(encoded)
    assert len(servers) == 2


def test_parse_subscription_dedup_by_host_port():
    body = (SUBSCRIPTION_URIS[0] + "\n" + SUBSCRIPTION_URIS[0]).encode()
    servers = parse_subscription(body)
    assert len(servers) == 1


def test_parse_subscription_skips_malformed_lines():
    body = (
        SUBSCRIPTION_URIS[0] + "\nvless://broken\nplain comment line\n" + SUBSCRIPTION_URIS[1]
    ).encode()
    servers = parse_subscription(body)
    assert len(servers) == 2


def test_parse_subscription_invalid_body_raises():
    with pytest.raises(VlessParseError):
        parse_subscription(b"not base64 nor a vless list")


def test_parse_subscription_empty_body_raises():
    with pytest.raises(VlessParseError):
        parse_subscription(b"")
