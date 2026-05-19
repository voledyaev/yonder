import os
import stat
from pathlib import Path

import pytest
from yonder.services import XKeenService


def make_fake_xkeen(tmp_path: Path, body: str) -> Path:
    """Write an executable shell script that yonder will invoke as `xkeen`."""
    script = tmp_path / "xkeen"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


async def test_returns_skipped_when_xkeen_missing(tmp_path):
    svc = XKeenService(bin_path=tmp_path / "does-not-exist")
    assert not svc.installed()
    for action in (svc.start, svc.stop, svc.restart):
        ok, msg = await action()
        assert ok is True
        assert "skipped" in msg.lower()


async def test_returns_ok_on_success(tmp_path):
    fake = make_fake_xkeen(tmp_path, "exit 0")
    svc = XKeenService(bin_path=fake)
    ok, msg = await svc.start()
    assert ok is True
    assert msg == ""


async def test_returns_error_on_nonzero_exit(tmp_path):
    fake = make_fake_xkeen(tmp_path, "exit 7")
    svc = XKeenService(bin_path=fake)
    ok, msg = await svc.stop()
    assert ok is False
    assert "exit 7" in msg


async def test_times_out_and_kills_hung_process(tmp_path):
    # 5s sleep against 0.3s timeout: we expect a timeout error message,
    # and crucially the call must *return* (not hang for 5s).
    fake = make_fake_xkeen(tmp_path, "sleep 5")
    svc = XKeenService(bin_path=fake, timeout_s=0.3)
    ok, msg = await svc.restart()
    assert ok is False
    assert "timed out" in msg


async def test_directory_is_not_treated_as_installed(tmp_path):
    # `/opt/sbin/xkeen` happens to be a directory on some weird systems —
    # we only consider regular files as installed.
    d = tmp_path / "xkeen"
    d.mkdir()
    svc = XKeenService(bin_path=d)
    assert not svc.installed()
    ok, msg = await svc.start()
    assert ok is True and "skipped" in msg.lower()


async def test_is_running_returns_bool():
    # We can't easily fake `pidof` without monkeypatch; just assert the call
    # returns a bool and doesn't hang. On a dev machine without xray running,
    # pidof xray exits 1 → False. On a router with xray up → True.
    svc = XKeenService()
    result = await svc.is_running()
    assert isinstance(result, bool)
