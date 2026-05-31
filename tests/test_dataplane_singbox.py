"""Tests for SingBoxDataPlane: structural change → reload, selection → live API.

Hermetic — a fake SingBoxService (records reloads, scriptable running state)
and a fake ClashClient (records selects). No router, no sing-box.
"""

from __future__ import annotations

import pytest
from yonder.dataplane import SingBoxDataPlane
from yonder.singbox.clash import ClashError
from yonder.state import ActiveServerRef, Data, DnsState, Subscription
from yonder.vless import Server


class FakeService:
    def __init__(self, running=False):
        self.running = running
        self.reloads = 0
        self.restart_result = (True, "")

    async def is_running(self):
        return self.running

    async def restart(self):
        self.reloads += 1
        self.running = self.restart_result[0]
        return self.restart_result


class FakeClash:
    def __init__(self):
        self.selects: list[tuple[str, str]] = []
        self.error: Exception | None = None

    async def select(self, selector, name):
        if self.error:
            raise self.error
        self.selects.append((selector, name))


def _server(host="de-dp-01.com", port=8443) -> Server:
    return Server(
        id=f"{host}:{port}",
        name="DE",
        country="DE",
        host=host,
        port=port,
        uuid="11111111-1111-1111-1111-111111111111",
        params={
            "flow": "xtls-rprx-vision",
            "security": "reality",
            "sni": "s",
            "fp": "firefox",
            "pbk": "k",
            "sid": "x",
        },
    )


def _data(servers=None, *, vpn_on=True, active=True, rules=None) -> Data:
    servers = servers if servers is not None else [_server()]
    sub = Subscription(id="sub-1", label="L", source="x", fetched_at="t", servers=servers)
    ref = (
        ActiveServerRef(subscription_id="sub-1", server_id=servers[0].id)
        if active and servers
        else None
    )
    return Data(
        subscriptions=[sub], active_server=ref, vpn_on=vpn_on, rules=rules or [], dns=DnsState()
    )


def _plane(service, clash, tmp_path):
    return SingBoxDataPlane(service, clash, config_path=tmp_path / "config.json")


async def test_first_apply_reloads_and_writes_config(tmp_path):
    svc, clash = FakeService(running=False), FakeClash()
    plane = _plane(svc, clash, tmp_path)
    ok, msg = await plane.apply(_data())
    assert ok and msg == ""
    assert svc.reloads == 1  # not running → reload (start)
    assert clash.selects == []  # no live switch on first apply
    assert (tmp_path / "config.json").is_file()


async def test_pure_selection_change_uses_live_switch(tmp_path):
    svc, clash = FakeService(running=True), FakeClash()
    plane = _plane(svc, clash, tmp_path)
    # First apply establishes the structural baseline (reload).
    await plane.apply(_data(vpn_on=True))
    svc.reloads = 0
    # Flip vpn off — same servers/rules, only the selection changes.
    ok, _ = await plane.apply(_data(vpn_on=False))
    assert ok
    assert svc.reloads == 0  # NO restart
    assert clash.selects == [("select", "direct")]  # live switch to direct


async def test_on_after_off_selects_active_server(tmp_path):
    svc, clash = FakeService(running=True), FakeClash()
    plane = _plane(svc, clash, tmp_path)
    await plane.apply(_data(vpn_on=True))  # baseline
    svc.reloads = 0
    await plane.apply(_data(vpn_on=False))  # → direct
    await plane.apply(_data(vpn_on=True))  # → back to the server
    assert svc.reloads == 0
    assert clash.selects[-1] == ("select", "sub-1/de-dp-01.com:8443")


async def test_adding_a_server_is_structural_and_reloads(tmp_path):
    svc, clash = FakeService(running=True), FakeClash()
    plane = _plane(svc, clash, tmp_path)
    await plane.apply(_data(servers=[_server()]))  # baseline
    svc.reloads = 0
    # A new server changes the selector membership → structural → reload.
    two = [_server(), _server(host="fi-01.com", port=443)]
    ok, _ = await plane.apply(_data(servers=two))
    assert ok
    assert svc.reloads == 1
    assert clash.selects == []  # reload, not a live switch


async def test_rules_change_is_structural_and_reloads(tmp_path):
    svc, clash = FakeService(running=True), FakeClash()
    plane = _plane(svc, clash, tmp_path)
    await plane.apply(_data())  # baseline (default rules)
    svc.reloads = 0
    ok, _ = await plane.apply(_data(rules=[{"domain_suffix": ["x.com"], "outbound": "direct"}]))
    assert ok
    assert svc.reloads == 1


async def test_clash_failure_falls_back_to_reload(tmp_path):
    svc, clash = FakeService(running=True), FakeClash()
    plane = _plane(svc, clash, tmp_path)
    await plane.apply(_data(vpn_on=True))  # baseline
    svc.reloads = 0
    clash.error = ClashError("controller down")
    ok, msg = await plane.apply(_data(vpn_on=False))  # would be a live switch
    assert ok  # fell back to reload
    assert svc.reloads == 1


async def test_not_running_forces_reload_even_without_structural_change(tmp_path):
    svc, clash = FakeService(running=True), FakeClash()
    plane = _plane(svc, clash, tmp_path)
    await plane.apply(_data())  # baseline, now running
    svc.reloads = 0
    svc.running = False  # sing-box died
    ok, _ = await plane.apply(_data())  # same config, but not running
    assert ok
    assert svc.reloads == 1  # reload to bring it back
    assert clash.selects == []


async def test_reload_failure_reported_and_forces_next_reload(tmp_path):
    svc, clash = FakeService(running=False), FakeClash()
    svc.restart_result = (False, "config error")
    plane = _plane(svc, clash, tmp_path)
    ok, msg = await plane.apply(_data())
    assert not ok
    assert "sing-box" in msg
    # next apply must reload again (don't trust the half-applied run)
    svc.restart_result = (True, "")
    svc.running = True
    ok2, _ = await plane.apply(_data())
    assert ok2
    assert svc.reloads == 2


async def test_watchdog_deps_wedged_when_clash_unhealthy(tmp_path):
    from yonder.dataplane import SingBoxWatchdogDeps
    from yonder.state import State

    state = State(tmp_path / "state.json")

    class Svc:
        async def is_running(self):
            return True

        async def restart(self):
            return (True, "")

    class HealthyClash:
        async def healthy(self):
            return True

    class WedgedClash:
        async def healthy(self):
            return False

    assert await SingBoxWatchdogDeps(state, Svc(), HealthyClash()).is_running() is True
    # process up but Clash unresponsive → treated as down (triggers recovery)
    assert await SingBoxWatchdogDeps(state, Svc(), WedgedClash()).is_running() is False


async def test_watchdog_deps_down_when_process_dead(tmp_path):
    from yonder.dataplane import SingBoxWatchdogDeps
    from yonder.state import State

    class DeadSvc:
        async def is_running(self):
            return False

        async def restart(self):
            return (True, "")

    class Clash:
        async def healthy(self):
            raise AssertionError("should not be checked when process is dead")

    deps = SingBoxWatchdogDeps(State(tmp_path / "s.json"), DeadSvc(), Clash())
    assert await deps.is_running() is False  # short-circuits before clash


def test_parse_rules_uses_singbox_parser():
    from yonder.rules import RulesParseError

    plane = SingBoxDataPlane(FakeService(), FakeClash(), config_path="/tmp/x")
    rules = plane.parse_rules(b'[{"domain_suffix": [".ru"], "outbound": "direct"}]')
    assert rules == [{"domain_suffix": [".ru"], "outbound": "direct"}]
    with pytest.raises(RulesParseError):
        plane.parse_rules(b'[{"type": "field", "outboundTag": "direct", "ip": ["10.0.0.0/8"]}]')
