import hashlib
import os

import httpx
import pytest
from yonder.keenetic import (
    DohUpstream,
    KeeneticAuthError,
    KeeneticClient,
    KeeneticCommandError,
)

REALM = "Keenetic Giga"
CHALLENGE = "TESTCHALLENGE1234"
LOGIN = "admin"
PASSWORD = "secret-pw"


def _expected_token(login=LOGIN, realm=REALM, password=PASSWORD, challenge=CHALLENGE) -> str:
    md5_pw = hashlib.md5(f"{login}:{realm}:{password}".encode()).hexdigest()
    return hashlib.sha256((challenge + md5_pw).encode()).hexdigest()


class MockTransport:
    """In-memory transport scriptable per-request for httpx.AsyncClient."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []
        # When True, the first /auth GET hands out a fresh challenge then
        # POST /auth returns 200; subsequent /auth GETs do the same.
        self.auth_should_succeed = True
        # Set to True to simulate one mid-session 401 (session expired); the
        # client should re-auth and retry. Resets after firing once.
        self.next_request_expired = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    def _respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/auth" and request.method == "GET":
            return httpx.Response(
                401,
                headers={
                    "X-NDM-Realm": REALM,
                    "X-NDM-Challenge": CHALLENGE,
                    "Set-Cookie": "NMBGBUSJH=SESSION1; Path=/; Max-Age=300",
                },
            )
        if path == "/auth" and request.method == "POST":
            if not self.auth_should_succeed:
                return httpx.Response(401, text="bad password")
            return httpx.Response(200, text="")
        # RCI calls: if expired flag set, return 401 once
        if self.next_request_expired:
            self.next_request_expired = False
            return httpx.Response(401, text="session expired")
        if self.responses:
            return self.responses.pop(0)
        return httpx.Response(500, text="no canned response left for " + path)


@pytest.fixture
def transport():
    return MockTransport()


@pytest.fixture
async def client(transport):
    async_client = httpx.AsyncClient(
        transport=httpx.MockTransport(transport),
        cookies=httpx.Cookies(),
    )
    kc = KeeneticClient(
        host="http://router.test", login=LOGIN, password=PASSWORD, client=async_client
    )
    yield kc
    await async_client.aclose()


# --- Auth -------------------------------------------------------------------


async def test_auth_handshake_uses_correct_token(client, transport):
    # Trigger any RCI call to force auth + read.
    transport.responses.append(httpx.Response(200, json={}))
    await client.get("/rci/")

    get_auth, post_auth, *_ = transport.requests
    assert get_auth.method == "GET" and get_auth.url.path == "/auth"
    assert post_auth.method == "POST" and post_auth.url.path == "/auth"
    import json as _json

    body = _json.loads(post_auth.content)
    assert body["login"] == LOGIN
    assert body["password"] == _expected_token()


async def test_auth_failure_raises(transport):
    transport.auth_should_succeed = False
    async_client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    kc = KeeneticClient(
        host="http://router.test", login=LOGIN, password="wrong", client=async_client
    )
    with pytest.raises(KeeneticAuthError):
        await kc.get("/rci/")
    await async_client.aclose()


async def test_auth_missing_challenge_header_raises(transport):
    # Override the auth GET to omit the headers.
    async def t(req):
        if req.url.path == "/auth" and req.method == "GET":
            return httpx.Response(401)  # no realm/challenge
        return httpx.Response(500)

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(t))
    kc = KeeneticClient(host="http://router.test", password="x", client=async_client)
    with pytest.raises(KeeneticAuthError):
        await kc.get("/rci/")
    await async_client.aclose()


async def test_session_expiry_triggers_reauth_and_retry(client, transport):
    # First request: 401 mid-session → client re-auths and replays.
    transport.responses.append(httpx.Response(200, json={"ok": True}))

    # Do the first call so we're authed.
    transport.responses.insert(0, httpx.Response(200, json={"first": True}))
    await client.get("/rci/")

    # Now simulate session expiry on the next /rci/ call.
    transport.next_request_expired = True
    result = await client.get("/rci/")
    assert result == {"ok": True}

    # Verify the path order: initial GET /auth + POST /auth, then GET /rci/
    # (first), then GET /rci/ that hit 401, then GET /auth again + POST /auth,
    # then the retry GET /rci/.
    paths = [r.url.path for r in transport.requests]
    assert paths.count("/auth") == 4  # 2 GETs + 2 POSTs across two handshakes
    assert paths.count("/rci/") == 3  # first call + expired call + retry


# --- /rci/parse -------------------------------------------------------------


async def test_parse_raises_on_error_status(client, transport):
    transport.responses.append(
        httpx.Response(
            200,
            json={
                "status": [
                    {
                        "status": "error",
                        "code": "1179668",
                        "ident": "Core::Configurator",
                        "message": '"no" is not applicable to: dns-proxy',
                    }
                ],
            },
        )
    )
    with pytest.raises(KeeneticCommandError) as exc:
        await client.parse("no dns-proxy")
    assert exc.value.code == "1179668"
    assert "Core::Configurator" in str(exc.value)


async def test_parse_walks_nested_status(client, transport):
    # Error nested under the command path, not at root.
    transport.responses.append(
        httpx.Response(
            200,
            json={
                "https": {
                    "upstream": {
                        "status": [
                            {
                                "status": "error",
                                "code": "7471107",
                                "ident": "Command::Root",
                                "message": "no input",
                            }
                        ]
                    }
                }
            },
        )
    )
    with pytest.raises(KeeneticCommandError) as exc:
        await client.parse("dns-proxy https upstream")
    assert exc.value.code == "7471107"


async def test_parse_success_passes_through(client, transport):
    transport.responses.append(
        httpx.Response(
            200,
            json={
                "prompt": "(config)",
                "status": [
                    {
                        "status": "message",
                        "code": "22610020",
                        "ident": "Dns::Secure::ManagerDoh",
                        "message": 'DNS-over-HTTPS name server "x" added.',
                    }
                ],
            },
        )
    )
    result = await client.parse("dns-proxy https upstream x")
    assert result["status"][0]["status"] == "message"


# --- DoH helpers ------------------------------------------------------------


async def test_list_doh_upstreams_empty(client, transport):
    transport.responses.append(httpx.Response(200, json=[]))
    assert await client.list_doh_upstreams() == []


async def test_list_doh_upstreams_populated(client, transport):
    transport.responses.append(
        httpx.Response(
            200,
            json=[
                {"url": "https://cloudflare-dns.com/dns-query", "format": "dnsm"},
                {"url": "https://dns.google/dns-query", "format": "dnsm"},
            ],
        )
    )
    result = await client.list_doh_upstreams()
    assert result == [
        DohUpstream(url="https://cloudflare-dns.com/dns-query"),
        DohUpstream(url="https://dns.google/dns-query"),
    ]


async def test_add_doh_upstream_sends_correct_command(client, transport):
    transport.responses.append(
        httpx.Response(
            200,
            json={
                "prompt": "(config)",
                "status": [
                    {
                        "status": "message",
                        "code": "22610020",
                        "ident": "Dns::Secure::ManagerDoh",
                        "message": "added.",
                    }
                ],
            },
        )
    )
    await client.add_doh_upstream("https://cloudflare-dns.com/dns-query")
    last = transport.requests[-1]
    import json as _json

    assert last.url.path == "/rci/parse"
    body = _json.loads(last.content)
    assert body == {"parse": "dns-proxy https upstream https://cloudflare-dns.com/dns-query"}


async def test_remove_doh_upstream_idempotent_on_missing(client, transport):
    # Keenetic returns error "no such DNS-over-HTTPS server" when removing
    # one that's not configured. We swallow that as success.
    transport.responses.append(
        httpx.Response(
            200,
            json={
                "prompt": "(config)",
                "status": [
                    {
                        "status": "error",
                        "code": "22610920",
                        "ident": "Dns::Secure::ManagerDoh",
                        "message": 'no such DNS-over-HTTPS server: "x".',
                    }
                ],
            },
        )
    )
    # Must not raise.
    await client.remove_doh_upstream("x")


async def test_remove_doh_upstream_propagates_other_errors(client, transport):
    transport.responses.append(
        httpx.Response(
            200,
            json={
                "status": [
                    {
                        "status": "error",
                        "code": "9999",
                        "ident": "Core::Foo",
                        "message": "something else broke",
                    }
                ],
            },
        )
    )
    with pytest.raises(KeeneticCommandError):
        await client.remove_doh_upstream("x")


# --- Integration test against live router (opt-in) --------------------------

INTEGRATION_HOST = os.environ.get("YONDER_TEST_ROUTER_HOST")
INTEGRATION_PW = os.environ.get("YONDER_TEST_ROUTER_PASSWORD")


@pytest.mark.skipif(
    not (INTEGRATION_HOST and INTEGRATION_PW),
    reason="set YONDER_TEST_ROUTER_HOST and YONDER_TEST_ROUTER_PASSWORD",
)
async def test_integration_full_doh_cycle():
    """End-to-end against a real router. Skipped unless env vars are set.

    Verifies: auth handshake → add DoH upstream → list shows it → remove →
    list is empty again. Leaves the router in the state it started in.
    """
    test_url = "https://cloudflare-dns.com/dns-query"
    async with KeeneticClient(host=INTEGRATION_HOST, login="admin", password=INTEGRATION_PW) as kc:
        before = await kc.list_doh_upstreams()
        # Add — assert it appears.
        await kc.add_doh_upstream(test_url)
        after_add = await kc.list_doh_upstreams()
        assert any(u.url == test_url for u in after_add), after_add
        # Remove — assert it disappears.
        await kc.remove_doh_upstream(test_url)
        after_remove = await kc.list_doh_upstreams()
        assert not any(u.url == test_url for u in after_remove), after_remove
        # Idempotent re-remove must not raise.
        await kc.remove_doh_upstream(test_url)
        # Final state matches starting state (we may have left others alone).
        assert {u.url for u in after_remove} == {u.url for u in before}
