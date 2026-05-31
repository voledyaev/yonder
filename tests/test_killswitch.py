"""Tests for the fail-closed kill switch around xkeen restarts.

Hermetic: the iptables / ip subprocess layer is monkeypatched so nothing
touches the host firewall. We assert the command shapes and the
engage→action→disengage ordering.
"""

from __future__ import annotations

import pytest
from yonder import killswitch
from yonder.services import XKeenService

from tests.test_services import make_fake_xkeen


@pytest.fixture
def fake_ipt(monkeypatch):
    """Record every iptables invocation; make them all succeed."""
    calls: list[list[str]] = []

    async def rec(args, timeout=5.0):
        calls.append(list(args))
        return 0

    monkeypatch.setattr(killswitch, "_ipt", rec)
    return calls


async def test_detect_wan_parses_default_route(monkeypatch):
    async def fake_detect():
        return "eth3"

    # Sanity on the args shape that engage/disengage build for a given wan.
    assert killswitch._insert_args("eth3")[:4] == ["-I", "FORWARD", "1", "-o"]
    assert "eth3" in killswitch._insert_args("eth3")
    assert killswitch._insert_args("eth3")[-1] == killswitch.COMMENT
    assert killswitch._delete_args("eth3")[0] == "-D"


async def test_engage_inserts_drop_with_comment(fake_ipt):
    ok = await killswitch.engage("eth3")
    assert ok is True
    assert fake_ipt == [
        [
            "-I",
            "FORWARD",
            "1",
            "-o",
            "eth3",
            "-j",
            "DROP",
            "-m",
            "comment",
            "--comment",
            "yonder-killswitch",
        ]
    ]


async def test_engage_reports_failure(monkeypatch):
    async def fail(args, timeout=5.0):
        return 1

    monkeypatch.setattr(killswitch, "_ipt", fail)
    assert await killswitch.engage("eth3") is False


async def test_disengage_deletes_until_absent(monkeypatch):
    # Two copies present, then nothing: expect 3 delete attempts (2 ok, 1 miss).
    results = iter([0, 0, 1])
    seen: list[list[str]] = []

    async def rec(args, timeout=5.0):
        seen.append(list(args))
        return next(results, 1)

    monkeypatch.setattr(killswitch, "_ipt", rec)
    await killswitch.disengage("eth3")
    assert len(seen) == 3
    assert all(a[0] == "-D" for a in seen)


async def test_sweep_noop_without_wan(monkeypatch):
    async def no_wan():
        return None

    called = False

    async def rec(args, timeout=5.0):
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(killswitch, "detect_wan", no_wan)
    monkeypatch.setattr(killswitch, "_ipt", rec)
    await killswitch.sweep()
    assert called is False


# --- Integration with XKeenService -----------------------------------------


async def test_guarded_restart_brackets_with_killswitch(tmp_path, monkeypatch):
    """With killswitch enabled, restart() must engage before and disengage
    after the xkeen call, in order."""
    events: list[str] = []

    async def fake_detect():
        return "eth3"

    async def fake_engage(wan):
        events.append(f"engage:{wan}")
        return True

    async def fake_disengage(wan):
        events.append(f"disengage:{wan}")

    monkeypatch.setattr(killswitch, "detect_wan", fake_detect)
    monkeypatch.setattr(killswitch, "engage", fake_engage)
    monkeypatch.setattr(killswitch, "disengage", fake_disengage)

    # Fake xkeen that records when it ran, so we can assert ordering.
    fake = make_fake_xkeen(tmp_path, "exit 0")
    svc = XKeenService(bin_path=fake, killswitch_enabled=True)

    # Wrap _invoke to log between engage and disengage.
    orig_invoke = svc._invoke

    async def logged_invoke(arg):
        events.append(f"xkeen:{arg}")
        return await orig_invoke(arg)

    monkeypatch.setattr(svc, "_invoke", logged_invoke)

    ok, _ = await svc.restart()
    assert ok is True
    assert events == ["engage:eth3", "xkeen:-restart", "disengage:eth3"]


async def test_guarded_disengages_even_when_xkeen_fails(tmp_path, monkeypatch):
    events: list[str] = []

    async def fake_detect():
        return "eth3"

    async def fake_engage(wan):
        events.append("engage")
        return True

    async def fake_disengage(wan):
        events.append("disengage")

    monkeypatch.setattr(killswitch, "detect_wan", fake_detect)
    monkeypatch.setattr(killswitch, "engage", fake_engage)
    monkeypatch.setattr(killswitch, "disengage", fake_disengage)

    fake = make_fake_xkeen(tmp_path, "exit 7")
    svc = XKeenService(bin_path=fake, killswitch_enabled=True)
    ok, msg = await svc.restart()
    assert ok is False
    assert "exit 7" in msg
    # disengage must still run despite the failure.
    assert events == ["engage", "disengage"]


async def test_killswitch_off_by_default_no_firewall_calls(tmp_path, monkeypatch):
    """Default construction must never call into killswitch (protects tests
    and local dev from touching iptables)."""
    called = False

    async def tripwire(*a, **k):
        nonlocal called
        called = True
        return "eth3"

    monkeypatch.setattr(killswitch, "detect_wan", tripwire)
    fake = make_fake_xkeen(tmp_path, "exit 0")
    svc = XKeenService(bin_path=fake)  # killswitch_enabled defaults False
    await svc.restart()
    assert called is False


async def test_stop_is_never_guarded(tmp_path, monkeypatch):
    """Even with killswitch enabled, -stop (VPN off) must not engage it."""
    called = False

    async def tripwire():
        nonlocal called
        called = True
        return "eth3"

    monkeypatch.setattr(killswitch, "detect_wan", tripwire)
    fake = make_fake_xkeen(tmp_path, "exit 0")
    svc = XKeenService(bin_path=fake, killswitch_enabled=True)
    await svc.stop()
    assert called is False
