import asyncio

import pytest
from yonder.apply import ApplyPipeline
from yonder.keenetic import DohUpstream
from yonder.state import (
    DEFAULT_DOH_URL,
    ActiveServerRef,
    Data,
    State,
    Subscription,
)
from yonder.vless import Server

# --- Fakes ------------------------------------------------------------------


class FakeRouter:
    """Stand-in for KeeneticClient — only the DoH-upstream surface."""

    def __init__(self, upstreams=None):
        self._upstreams = list(upstreams or [])
        self.calls: list[tuple[str, str]] = []

    async def list_doh_upstreams(self):
        return [DohUpstream(url=u) for u in self._upstreams]

    async def add_doh_upstream(self, url):
        self.calls.append(("add", url))
        if url not in self._upstreams:
            self._upstreams.append(url)

    async def remove_doh_upstream(self, url):
        self.calls.append(("remove", url))
        if url in self._upstreams:
            self._upstreams.remove(url)


class FakeServices:
    """Scriptable XKeenService stand-in."""

    def __init__(self):
        self.start_result = (True, "")
        self.stop_result = (True, "")
        self.restart_result = (True, "")
        self.calls: list[str] = []

    async def start(self):
        self.calls.append("start")
        return self.start_result

    async def stop(self):
        self.calls.append("stop")
        return self.stop_result

    async def restart(self):
        self.calls.append("restart")
        return self.restart_result


# --- Helpers ----------------------------------------------------------------


def make_server(id_="h:443") -> Server:
    return Server(id=id_, name=id_, country="??", host="h", port=443, uuid="u", params={})


async def state_with_active(tmp_path, vpn_on=True) -> tuple[State, str]:
    s = State(tmp_path / "state.json")
    sub_id = "sub-test"

    def mutate(d: Data):
        d.subscriptions = [
            Subscription(
                id=sub_id,
                label="L",
                source="x",
                fetched_at="t",
                servers=[make_server()],
            )
        ]
        d.active_server = ActiveServerRef(subscription_id=sub_id, server_id="h:443")
        d.vpn_on = vpn_on

    await s.update(mutate)
    return s, sub_id


async def run_one_apply(state, services, router, configs_dir) -> None:
    """Start pipeline, signal one apply, wait for completion, stop."""
    p = ApplyPipeline(state, services, router, configs_dir=configs_dir)
    await p.start()
    p.signal()
    # Wait until last_apply is recorded — bounded retry loop.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if state.snapshot().last_apply is not None:
            break
    await p.stop()


# --- ON path ----------------------------------------------------------------


async def test_on_path_enables_doh_then_restarts_xkeen(tmp_path):
    state, _ = await state_with_active(tmp_path, vpn_on=True)
    router = FakeRouter()
    services = FakeServices()

    await run_one_apply(state, services, router, tmp_path / "configs")

    # DoH applied first, then xkeen restart.
    assert ("add", DEFAULT_DOH_URL) in router.calls
    assert services.calls == ["restart"]
    snap = state.snapshot()
    assert snap.last_apply is not None
    assert snap.last_apply.ok is True
    assert snap.dns.active_url == DEFAULT_DOH_URL
    assert snap.applying is False  # cleared by the worker


async def test_on_path_writes_xray_configs(tmp_path):
    state, _ = await state_with_active(tmp_path, vpn_on=True)
    router = FakeRouter()
    services = FakeServices()
    configs = tmp_path / "configs"

    await run_one_apply(state, services, router, configs)

    assert (configs / "04_outbounds.json").is_file()
    assert (configs / "05_routing.json").is_file()


async def test_on_path_rolls_back_doh_when_xkeen_fails(tmp_path):
    state, _ = await state_with_active(tmp_path, vpn_on=True)
    router = FakeRouter(
        [
            "https://user-had-this.example/dns-query",  # user's pre-existing
        ]
    )
    services = FakeServices()
    services.restart_result = (False, "xkeen exit 1")

    await run_one_apply(state, services, router, tmp_path / "configs")

    snap = state.snapshot()
    assert snap.last_apply.ok is False
    assert "xkeen" in snap.last_apply.msg
    # DoH should have been rolled back: state shows nothing active, router
    # has the user's original upstream back.
    assert snap.dns.active_url is None
    assert router._upstreams == ["https://user-had-this.example/dns-query"]


# --- OFF path ---------------------------------------------------------------


async def test_off_path_stops_xkeen_and_removes_doh(tmp_path):
    state, _ = await state_with_active(tmp_path, vpn_on=True)
    router = FakeRouter(
        [
            "https://user-had-this.example/dns-query",
        ]
    )
    services = FakeServices()

    # First apply: turn it ON.
    await run_one_apply(state, services, router, tmp_path / "configs")
    assert state.snapshot().dns.active_url == DEFAULT_DOH_URL

    # Flip vpn_on=false and apply again.
    await state.update(lambda d: setattr(d, "vpn_on", False))
    services.calls.clear()
    router.calls.clear()
    # Reset last_apply so the polling loop sees the next one.
    await state.update(lambda d: setattr(d, "last_apply", None))

    await run_one_apply(state, services, router, tmp_path / "configs")

    snap = state.snapshot()
    assert snap.last_apply.ok is True
    assert services.calls == ["stop"]
    # DoH removed; user's original upstream restored.
    assert snap.dns.active_url is None
    assert router._upstreams == ["https://user-had-this.example/dns-query"]


async def test_off_path_when_active_server_missing(tmp_path):
    # vpn_on=True but no active_server resolves → treated as OFF.
    s = State(tmp_path / "state.json")
    await s.update(lambda d: setattr(d, "vpn_on", True))
    router = FakeRouter()
    services = FakeServices()

    await run_one_apply(s, services, router, tmp_path / "configs")

    assert services.calls == ["stop"]
    assert s.snapshot().last_apply.ok is True


# --- Coalescing ------------------------------------------------------------


async def test_rapid_signals_coalesce(tmp_path):
    state, _ = await state_with_active(tmp_path, vpn_on=True)
    router = FakeRouter()
    services = FakeServices()

    p = ApplyPipeline(state, services, router, configs_dir=tmp_path / "configs")
    await p.start()

    # 10 signals fired before the worker can start any iteration.
    for _ in range(10):
        p.signal()

    # Wait for at least one apply to complete.
    for _ in range(100):
        await asyncio.sleep(0.01)
        if state.snapshot().last_apply is not None:
            break

    await p.stop()

    # Coalescing: 10 signals collapsed into at most 2 iterations (the one
    # that started immediately + one more if the event was re-set during
    # the first). Never 10.
    assert 1 <= len(services.calls) <= 2


# --- Failure path ----------------------------------------------------------


async def test_doh_failure_recorded_as_last_apply(tmp_path):
    state, _ = await state_with_active(tmp_path, vpn_on=True)
    router = FakeRouter()
    # Empty doh_url forces enable_doh to fail.
    await state.update(lambda d: setattr(d.dns, "doh_url", ""))
    services = FakeServices()

    await run_one_apply(state, services, router, tmp_path / "configs")

    snap = state.snapshot()
    assert snap.last_apply.ok is False
    assert "DoH" in snap.last_apply.msg
    assert snap.last_error == snap.last_apply.msg
    # xkeen never invoked when DoH gate fails.
    assert services.calls == []


async def test_successful_apply_clears_last_error(tmp_path):
    state, _ = await state_with_active(tmp_path, vpn_on=True)
    # Pre-set a stale error from a prior failed apply.
    await state.update(lambda d: setattr(d, "last_error", "stale failure"))

    router = FakeRouter()
    services = FakeServices()
    await run_one_apply(state, services, router, tmp_path / "configs")

    snap = state.snapshot()
    assert snap.last_apply.ok is True
    assert snap.last_error == ""
