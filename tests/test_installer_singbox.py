"""Hermetic tests for the sing-box installer steps.

A fake EntwareShell records `run` commands and `upload_bytes` payloads and
replays scripted results, so these exercise the step logic (idempotency
checks, arch mapping, credential-free scrub) without a router.
"""

from __future__ import annotations

import json

import pytest
from installer import steps


class FakeShell:
    def __init__(self, responder=None):
        # responder(cmd) -> (rc, out, err); default: success, empty output.
        self._responder = responder or (lambda cmd: (0, "", ""))
        self.commands: list[str] = []
        self.uploads: list[tuple[str, bytes, int]] = []

    async def run(self, cmd, check=False, timeout=0.0):
        self.commands.append(cmd)
        rc, out, err = self._responder(cmd)
        return rc, out, err

    async def upload_bytes(self, data, path, mode=0o644):
        self.uploads.append((path, data, mode))


# --- install_singbox --------------------------------------------------------


async def test_install_singbox_skips_when_version_matches():
    def respond(cmd):
        if "version" in cmd:
            return (0, f"sing-box version {steps.SINGBOX_VERSION}\n", "")
        return (0, "", "")

    shell = FakeShell(respond)
    await steps.install_singbox(shell, "aarch64")
    # No download attempted (only the version probe ran).
    assert not any("curl" in c for c in shell.commands)


async def test_install_singbox_unknown_arch_fails():
    shell = FakeShell(lambda cmd: (1, "", ""))  # version probe "not installed"
    with pytest.raises(SystemExit):
        await steps.install_singbox(shell, "sparc64")


async def test_install_singbox_uses_musl_arch_url():
    seen = {}

    def respond(cmd):
        if "version" in cmd and "curl" not in cmd:
            # first probe: not installed; final probe: installed
            if seen.get("downloaded"):
                return (0, f"sing-box version {steps.SINGBOX_VERSION}\n", "")
            return (1, "", "")
        if "curl" in cmd:
            seen["downloaded"] = True
            seen["cmd"] = cmd
        return (0, "", "")

    shell = FakeShell(respond)
    await steps.install_singbox(shell, "aarch64")
    assert "linux-arm64-musl" in seen["cmd"]
    assert steps.SINGBOX_VERSION in seen["cmd"]


# --- install_geo_rulesets ---------------------------------------------------


async def test_geo_rulesets_skip_when_present():
    shell = FakeShell(lambda cmd: (0, "", ""))  # test -s … succeeds
    await steps.install_geo_rulesets(shell)
    assert not any("curl" in c for c in shell.commands)


async def test_geo_rulesets_download_when_absent():
    def respond(cmd):
        if cmd.startswith("test -s") or " && test -s" in cmd:
            return (1, "", "")  # not present
        return (0, "", "")

    shell = FakeShell(respond)
    await steps.install_geo_rulesets(shell)
    curls = [c for c in shell.commands if "curl" in c]
    assert any("geoip-ru.srs" in c for c in curls)
    assert any("geosite-ru.srs" in c for c in curls)


# --- scrub_singbox_config ---------------------------------------------------


async def test_scrub_writes_credential_free_config():
    shell = FakeShell(lambda cmd: (0, "", ""))  # test -f config → present
    await steps.scrub_singbox_config(shell)
    assert len(shell.uploads) == 1
    path, data, mode = shell.uploads[0]
    assert path == steps.SINGBOX_CONFIG
    assert mode == 0o600
    cfg = json.loads(data)
    # No vless outbounds → no UUIDs/keys; selector points only at direct.
    assert not any(o.get("type") == "vless" for o in cfg["outbounds"])
    selector = next(o for o in cfg["outbounds"] if o["type"] == "selector")
    assert selector["outbounds"] == ["direct"]
    assert b"uuid" not in data.lower()


async def test_scrub_skips_when_no_config():
    shell = FakeShell(lambda cmd: (1, "", ""))  # test -f config → absent
    await steps.scrub_singbox_config(shell)
    assert shell.uploads == []


# --- stop_singbox -----------------------------------------------------------


async def test_stop_singbox_noop_when_init_absent():
    shell = FakeShell(lambda cmd: (1, "", ""))  # test -x init → absent
    await steps.stop_singbox(shell)
    assert not any("stop" in c for c in shell.commands)


async def test_stop_singbox_runs_when_present():
    def respond(cmd):
        return (0, "", "")  # test -x init → present

    shell = FakeShell(respond)
    await steps.stop_singbox(shell)
    assert any("S99singbox stop" in c for c in shell.commands)
