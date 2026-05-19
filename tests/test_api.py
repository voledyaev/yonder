"""Tests for the FastAPI surface.

Uses httpx.AsyncClient + ASGITransport to drive the app in-process — no
sockets, no network. Subscription/rules fetches go through a MockTransport
that maps fake source URLs to canned responses.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from yonder.api import create_app
from yonder.state import DEFAULT_DOH_URL, Data, State

SAMPLE_VLESS_BODY = (
    "vless://uuid1@host1.com:443?security=reality&type=tcp"
    "#%F0%9F%87%B5%F0%9F%87%B1Poland\n"
    "vless://uuid2@host2.com:443?security=reality&type=tcp"
    "#%F0%9F%87%A9%F0%9F%87%AAGermany\n"
)


class FakePipeline:
    def __init__(self):
        self.signals = 0

    def signal(self) -> None:
        self.signals += 1


class FakeRouteMap:
    """Maps (method, url) → httpx.Response, used by httpx.MockTransport.

    Each entry can be a single response (returned every time) or a callable
    that produces a response per request — handy for "first call returns X,
    second call returns Y" tests.
    """

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.requests: list[httpx.Request] = []

    def add(self, url: str, response_or_factory) -> None:
        self.routes[url] = response_or_factory

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        entry = self.routes.get(str(request.url))
        if entry is None:
            return httpx.Response(404, text=f"no route for {request.url}")
        if callable(entry):
            return entry(request)
        return entry


@pytest.fixture
async def setup(tmp_path):
    state = State(tmp_path / "state.json")
    pipeline = FakePipeline()
    routes = FakeRouteMap()
    fetcher = httpx.AsyncClient(transport=httpx.MockTransport(routes.handle))
    app = create_app(state, pipeline, fetcher, xray_configs_dir=tmp_path / "configs")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, state, pipeline, routes
    await fetcher.aclose()


# --- Meta -------------------------------------------------------------------


async def test_get_state_returns_defaults(setup):
    client, *_ = setup
    r = await client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert body["subscriptions"] == []
    assert body["vpn_on"] is False
    assert body["active_server"] is None
    assert body["dns"]["doh_url"] == DEFAULT_DOH_URL


async def test_health(setup):
    client, *_ = setup
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


async def test_unknown_api_returns_404(setup):
    client, *_ = setup
    r = await client.get("/api/nope")
    assert r.status_code == 404


# --- Static -----------------------------------------------------------------


async def test_index_served_at_root(setup):
    client, *_ = setup
    r = await client.get("/")
    assert r.status_code == 200
    assert "<!DOCTYPE html>" in r.text or "<html" in r.text


# --- Subscriptions ----------------------------------------------------------


async def test_add_subscription_happy_path(setup):
    client, state, pipeline, routes = setup
    routes.add("http://provider.test/sub", httpx.Response(200, text=SAMPLE_VLESS_BODY))
    r = await client.post(
        "/api/subscriptions", json={"label": "Test", "source": "http://provider.test/sub"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["subscriptions"]) == 1
    sub = body["subscriptions"][0]
    assert sub["label"] == "Test"
    assert len(sub["servers"]) == 2
    # No apply triggered — new sub doesn't change runtime.
    assert pipeline.signals == 0


async def test_add_subscription_inline_vless_skips_fetch(setup):
    client, state, pipeline, routes = setup
    inline = "vless://abc@host.example:8443?security=reality&type=tcp#test"
    r = await client.post("/api/subscriptions", json={"label": "Inline", "source": inline})
    assert r.status_code == 200, r.text
    sub = r.json()["subscriptions"][0]
    assert sub["source"] == inline
    assert sub["servers"][0]["host"] == "host.example"
    assert routes.requests == []  # no fetch was made


async def test_add_subscription_label_derived_from_url(setup):
    client, state, _, routes = setup
    routes.add("http://provider.test/sub", httpx.Response(200, text=SAMPLE_VLESS_BODY))
    r = await client.post("/api/subscriptions", json={"source": "http://provider.test/sub"})
    assert r.status_code == 200
    assert r.json()["subscriptions"][0]["label"] == "provider.test"


async def test_add_subscription_label_derived_from_vless_host(setup):
    client, *_ = setup
    inline = "vless://abc@host.example:8443?security=reality&type=tcp#x"
    r = await client.post("/api/subscriptions", json={"source": inline})
    assert r.status_code == 200
    assert r.json()["subscriptions"][0]["label"] == "host.example"


async def test_add_subscription_rejects_bad_scheme(setup):
    client, *_ = setup
    r = await client.post(
        "/api/subscriptions", json={"label": "X", "source": "ftp://example.com/x"}
    )
    assert r.status_code == 400


async def test_add_subscription_fetch_failure_returns_502(setup):
    client, state, _, routes = setup
    # No route registered → 404 from MockTransport → FetchError → 502.
    r = await client.post(
        "/api/subscriptions", json={"label": "X", "source": "http://nowhere.test/x"}
    )
    assert r.status_code == 502


async def test_add_subscription_unparseable_body_returns_400(setup):
    client, state, _, routes = setup
    routes.add("http://provider.test/sub", httpx.Response(200, text="not a vless list"))
    r = await client.post(
        "/api/subscriptions", json={"label": "X", "source": "http://provider.test/sub"}
    )
    assert r.status_code == 400


async def test_delete_subscription_clears_active_when_affected(setup):
    client, state, pipeline, routes = setup
    routes.add("http://p.test/x", httpx.Response(200, text=SAMPLE_VLESS_BODY))
    add = await client.post("/api/subscriptions", json={"label": "X", "source": "http://p.test/x"})
    sub = add.json()["subscriptions"][0]
    sub_id = sub["id"]
    srv_id = sub["servers"][0]["id"]
    await client.post("/api/server", json={"subscription_id": sub_id, "server_id": srv_id})
    await client.post("/api/toggle", json={"on": True})
    pipeline.signals = 0

    r = await client.delete(f"/api/subscriptions/{sub_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["subscriptions"] == []
    assert body["active_server"] is None
    assert body["vpn_on"] is False
    assert pipeline.signals == 1  # apply triggered (affected active server)


async def test_delete_unknown_subscription_returns_404(setup):
    client, *_ = setup
    r = await client.delete("/api/subscriptions/ghost-id")
    assert r.status_code == 404


async def test_refresh_subscription_replaces_servers(setup):
    client, state, _, routes = setup
    calls = 0
    body1 = "vless://u1@host1.com:443?security=reality#%F0%9F%87%B5%F0%9F%87%B1Poland\n"
    body2 = body1 + ("vless://u2@host2.com:443?security=reality#%F0%9F%87%A9%F0%9F%87%AAGermany\n")

    def respond(req):
        nonlocal calls
        i = calls
        calls += 1
        return httpx.Response(200, text=(body1, body2)[min(i, 1)])

    routes.add("http://p.test/x", respond)
    add = await client.post("/api/subscriptions", json={"label": "X", "source": "http://p.test/x"})
    sub_id = add.json()["subscriptions"][0]["id"]
    assert len(add.json()["subscriptions"][0]["servers"]) == 1

    r = await client.post(f"/api/subscriptions/{sub_id}/refresh")
    assert r.status_code == 200
    assert len(r.json()["subscriptions"][0]["servers"]) == 2


async def test_patch_subscription_renames(setup):
    client, *_, routes = setup
    routes.add("http://p.test/x", httpx.Response(200, text=SAMPLE_VLESS_BODY))
    add = await client.post(
        "/api/subscriptions", json={"label": "Old", "source": "http://p.test/x"}
    )
    sub_id = add.json()["subscriptions"][0]["id"]
    r = await client.patch(f"/api/subscriptions/{sub_id}", json={"label": "New"})
    assert r.status_code == 200
    assert r.json()["subscriptions"][0]["label"] == "New"


# --- /api/server -----------------------------------------------------------


async def test_server_select_invalid_rejected(setup):
    client, *_ = setup
    r = await client.post("/api/server", json={"subscription_id": "ghost", "server_id": "h:443"})
    assert r.status_code == 400


async def test_server_select_valid_sets_active(setup):
    client, *_, routes = setup
    routes.add("http://p.test/x", httpx.Response(200, text=SAMPLE_VLESS_BODY))
    add = await client.post("/api/subscriptions", json={"label": "X", "source": "http://p.test/x"})
    sub = add.json()["subscriptions"][0]
    r = await client.post(
        "/api/server", json={"subscription_id": sub["id"], "server_id": sub["servers"][0]["id"]}
    )
    assert r.status_code == 200
    a = r.json()["active_server"]
    assert a["subscription_id"] == sub["id"]
    assert a["server_id"] == sub["servers"][0]["id"]


async def test_server_select_nulls_deselect(setup):
    client, state, _, routes = setup
    routes.add("http://p.test/x", httpx.Response(200, text=SAMPLE_VLESS_BODY))
    add = await client.post("/api/subscriptions", json={"label": "X", "source": "http://p.test/x"})
    sub = add.json()["subscriptions"][0]
    await client.post(
        "/api/server", json={"subscription_id": sub["id"], "server_id": sub["servers"][0]["id"]}
    )
    r = await client.post("/api/server", json={"subscription_id": None, "server_id": None})
    assert r.status_code == 200
    assert r.json()["active_server"] is None


# --- /api/toggle -----------------------------------------------------------


async def test_toggle_on_without_active_rejected(setup):
    client, *_ = setup
    r = await client.post("/api/toggle", json={"on": True})
    assert r.status_code == 400
    assert "no active server" in r.json()["error"]


async def test_toggle_off_without_active_succeeds(setup):
    client, *_ = setup
    r = await client.post("/api/toggle", json={"on": False})
    assert r.status_code == 200
    assert r.json()["vpn_on"] is False


async def test_toggle_on_with_active_succeeds(setup):
    client, state, pipeline, routes = setup
    routes.add("http://p.test/x", httpx.Response(200, text=SAMPLE_VLESS_BODY))
    add = await client.post("/api/subscriptions", json={"label": "X", "source": "http://p.test/x"})
    sub = add.json()["subscriptions"][0]
    await client.post(
        "/api/server", json={"subscription_id": sub["id"], "server_id": sub["servers"][0]["id"]}
    )
    pipeline.signals = 0
    r = await client.post("/api/toggle", json={"on": True})
    assert r.status_code == 200
    assert r.json()["vpn_on"] is True
    assert pipeline.signals == 1


# --- /api/dns/config (new) -------------------------------------------------


async def test_dns_config_updates_doh_url_and_signals(setup):
    client, state, pipeline, _ = setup
    new_url = "https://dns.google/dns-query"
    r = await client.post("/api/dns/config", json={"doh_url": new_url})
    assert r.status_code == 200
    body = r.json()
    assert body["dns"]["doh_url"] == new_url
    assert pipeline.signals == 1


async def test_dns_config_rejects_non_https(setup):
    client, *_ = setup
    r = await client.post("/api/dns/config", json={"doh_url": "http://insecure.example/dns-query"})
    assert r.status_code == 400
    assert "https" in r.json()["error"].lower()


async def test_dns_config_rejects_empty(setup):
    client, *_ = setup
    r = await client.post("/api/dns/config", json={"doh_url": ""})
    assert r.status_code == 400


async def test_dns_config_default_preserved_on_startup(setup):
    client, *_ = setup
    r = await client.get("/api/state")
    assert r.json()["dns"]["doh_url"] == DEFAULT_DOH_URL


# --- /api/rules-url --------------------------------------------------------


async def test_rules_url_set_and_clear(setup):
    client, state, pipeline, routes = setup
    routes.add(
        "http://rules.test/rules.json",
        httpx.Response(
            200,
            json={
                "rules": [
                    {"outboundTag": "direct", "ip": ["10.0.0.0/8"]},
                ]
            },
        ),
    )
    r = await client.post("/api/rules-url", json={"url": "http://rules.test/rules.json"})
    assert r.status_code == 200
    body = r.json()
    assert body["rules_url"] == "http://rules.test/rules.json"
    assert len(body["rules"]) == 1

    # Clear with empty URL.
    r = await client.post("/api/rules-url", json={"url": ""})
    assert r.status_code == 200
    assert r.json()["rules_url"] == ""
    assert r.json()["rules"] == []


async def test_rules_refresh_requires_existing_url(setup):
    client, *_ = setup
    r = await client.post("/api/rules/refresh")
    assert r.status_code == 400


# --- applying flag --------------------------------------------------------


async def test_applying_flag_set_synchronously_on_toggle(setup):
    client, state, _, routes = setup
    routes.add("http://p.test/x", httpx.Response(200, text=SAMPLE_VLESS_BODY))
    add = await client.post("/api/subscriptions", json={"label": "X", "source": "http://p.test/x"})
    sub = add.json()["subscriptions"][0]
    await client.post(
        "/api/server", json={"subscription_id": sub["id"], "server_id": sub["servers"][0]["id"]}
    )
    r = await client.post("/api/toggle", json={"on": True})
    # The handler sets applying=True before responding; the UI's next /state
    # poll thus shows it true. No real apply pipeline running here, so it
    # stays true.
    assert r.json()["applying"] is True
