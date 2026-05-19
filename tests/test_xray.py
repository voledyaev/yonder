import json
from pathlib import Path

import pytest
from yonder.vless import Server
from yonder.xray import (
    OUTBOUNDS_FILE,
    ROUTING_FILE,
    build_outbound,
    write_xkeen_split,
)


@pytest.fixture
def reality_server() -> Server:
    return Server(
        id="host.com:443",
        name="host.com:443",
        country="??",
        host="host.com",
        port=443,
        uuid="uuid-1234",
        params={
            "security": "reality",
            "type": "tcp",
            "flow": "xtls-rprx-vision",
            "sni": "example.com",
            "fp": "chrome",
            "pbk": "PUB",
            "sid": "SID",
        },
    )


def test_build_outbound_reality_shape(reality_server):
    ob = build_outbound(reality_server)
    assert ob["protocol"] == "vless"
    stream = ob["streamSettings"]
    assert stream["network"] == "tcp"
    assert stream["security"] == "reality"

    rs = stream["realitySettings"]
    assert rs["serverName"] == "example.com"
    assert rs["fingerprint"] == "chrome"
    assert rs["publicKey"] == "PUB"
    assert rs["shortId"] == "SID"
    assert rs["spiderX"] == ""

    user = ob["settings"]["vnext"][0]["users"][0]
    assert user["id"] == "uuid-1234"
    assert user["flow"] == "xtls-rprx-vision"
    assert user["encryption"] == "none"


def test_build_outbound_reality_spx_forwarded(reality_server):
    srv = reality_server.model_copy(
        deep=True, update={"params": {**reality_server.params, "spx": "/"}}
    )
    ob = build_outbound(srv)
    assert ob["streamSettings"]["realitySettings"]["spiderX"] == "/"


def test_build_outbound_ws_transport():
    srv = Server(
        id="ws.example.com:443",
        name="ws",
        country="??",
        host="ws.example.com",
        port=443,
        uuid="u",
        params={
            "security": "tls",
            "type": "ws",
            "path": "/wspath",
            "host": "ws.example.com",
            "sni": "ws.example.com",
        },
    )
    ob = build_outbound(srv)
    stream = ob["streamSettings"]
    assert stream["network"] == "ws"
    assert stream["security"] == "tls"
    assert stream["wsSettings"]["path"] == "/wspath"
    assert stream["wsSettings"]["headers"]["Host"] == "ws.example.com"
    assert stream["tlsSettings"]["serverName"] == "ws.example.com"


def test_build_outbound_grpc_transport():
    srv = Server(
        id="g.example.com:443",
        name="g",
        country="??",
        host="g.example.com",
        port=443,
        uuid="u",
        params={
            "security": "tls",
            "type": "grpc",
            "path": "/grpcservice",
            "sni": "g.example.com",
        },
    )
    ob = build_outbound(srv)
    assert ob["streamSettings"]["network"] == "grpc"
    # Leading slash stripped — XKeen expects bare service name for gRPC.
    assert ob["streamSettings"]["grpcSettings"]["serviceName"] == "grpcservice"


def test_write_xkeen_split_both_files(tmp_path, reality_server):
    write_xkeen_split(reality_server, None, tmp_path)
    assert (tmp_path / OUTBOUNDS_FILE).is_file()
    assert (tmp_path / ROUTING_FILE).is_file()


def _read_outbound_tags(path: Path) -> list[str]:
    doc = json.loads(path.read_text())
    return [ob["tag"] for ob in doc["outbounds"]]


def test_write_xkeen_split_outbounds_content(tmp_path, reality_server):
    write_xkeen_split(reality_server, None, tmp_path)
    assert _read_outbound_tags(tmp_path / OUTBOUNDS_FILE) == ["proxy", "direct", "block"]


def test_write_xkeen_split_no_server_omits_proxy(tmp_path):
    write_xkeen_split(None, None, tmp_path)
    assert _read_outbound_tags(tmp_path / OUTBOUNDS_FILE) == ["direct", "block"]


def test_write_xkeen_split_custom_rules(tmp_path, reality_server):
    custom = [{"type": "field", "outboundTag": "proxy", "domain": ["foo.com"]}]
    write_xkeen_split(reality_server, custom, tmp_path)
    raw = (tmp_path / ROUTING_FILE).read_text()
    doc = json.loads(raw)
    routing = doc["routing"]
    assert routing["domainStrategy"] == "AsIs"
    assert "foo.com" in raw


def test_write_xkeen_split_default_rules_include_rfc1918(tmp_path, reality_server):
    write_xkeen_split(reality_server, None, tmp_path)
    raw = (tmp_path / ROUTING_FILE).read_text()
    for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",
    ):
        assert cidr in raw, f"default rules missing {cidr}"


def test_write_xkeen_split_atomic_no_tmp_left_behind(tmp_path, reality_server):
    write_xkeen_split(reality_server, None, tmp_path)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_write_xkeen_split_empty_rules_uses_defaults(tmp_path, reality_server):
    # Go behavior: rules of zero length triggers default_rules(); we mirror it.
    write_xkeen_split(reality_server, [], tmp_path)
    raw = (tmp_path / ROUTING_FILE).read_text()
    assert "10.0.0.0/8" in raw


def test_write_xkeen_split_creates_missing_configs_dir(tmp_path, reality_server):
    target = tmp_path / "nested" / "configs"
    write_xkeen_split(reality_server, None, target)
    assert (target / OUTBOUNDS_FILE).is_file()
    assert (target / ROUTING_FILE).is_file()
