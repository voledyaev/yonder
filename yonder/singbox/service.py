"""sing-box process control + config writing — the XKeenService replacement.

sing-box is supervised by its own init script (S99singbox); yonder writes
config.json and start/stop/restarts the service. Unlike xkeen, the common
operations (server switch, on/off) never come here — they're live Clash API
calls. This service is touched only when the config *structure* changes
(servers/rules/DNS), which needs a reload (restart).

A reload briefly tears the tun down, so restart() is bracketed by the same
fail-closed kill switch used for xkeen — that flush window can't leak. The
live-switch path has no such window, so it needs no guard.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from yonder import killswitch

SINGBOX_BIN = "/opt/sbin/sing-box"
SINGBOX_INIT = "/opt/etc/init.d/S99singbox"
SINGBOX_CONFIG = "/opt/etc/sing-box/config.json"

# A reload re-reads config, may re-establish the tun, and reconnects — give it
# room before assuming it hung.
DEFAULT_TIMEOUT_S = 60.0


def write_config(cfg: dict[str, Any], path: str | Path = SINGBOX_CONFIG) -> None:
    """Atomically write the generated sing-box config to disk (tmp+rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(cfg, indent=2).encode()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_bytes(raw)
    tmp.replace(p)


class SingBoxService:
    """sing-box lifecycle via its init script, async-friendly."""

    def __init__(
        self,
        init_path: str | Path = SINGBOX_INIT,
        bin_path: str | Path = SINGBOX_BIN,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        *,
        killswitch_enabled: bool = False,
    ):
        self._init = Path(init_path)
        self._bin = Path(bin_path)
        self._timeout = timeout_s
        # Off by default so unit tests (fake init script) never touch real
        # iptables; production enables it in the lifespan.
        self._killswitch_enabled = killswitch_enabled

    def installed(self) -> bool:
        """True when the sing-box binary exists as a regular file."""
        return self._bin.is_file()

    async def start(self) -> tuple[bool, str]:
        return await self._guarded("start")

    async def stop(self) -> tuple[bool, str]:
        # Unguarded: stopping sing-box means VPN-off-direct is intended (no
        # kill switch). With the selector→direct model we rarely stop at all.
        return await self._invoke("stop")

    async def restart(self) -> tuple[bool, str]:
        return await self._guarded("restart")

    async def is_running(self) -> bool:
        code, _ = await _run(["pidof", "sing-box"], timeout_s=5.0)
        return code == 0

    async def _guarded(self, action: str) -> tuple[bool, str]:
        """Run a start/restart bracketed by the fail-closed kill switch — the
        reload window tears the tun down and would otherwise leak."""
        if not self.installed() or not self._killswitch_enabled:
            return await self._invoke(action)
        wan = await killswitch.detect_wan()
        engaged = await killswitch.engage(wan) if wan else False
        try:
            return await self._invoke(action)
        finally:
            if engaged:
                await killswitch.disengage(wan)

    async def _invoke(self, action: str) -> tuple[bool, str]:
        if not self.installed():
            return True, "sing-box not installed; skipped (config still written)"
        code, err = await _run([str(self._init), action], timeout_s=self._timeout)
        if err:
            return False, err
        if code != 0:
            return False, f"sing-box {action} exit {code}"
        return True, ""


async def _run(argv: list[str], timeout_s: float) -> tuple[int, str]:
    """Run `argv` with stdio fully discarded; return (exit_code, err_message).

    Same daemon-leak discipline as the xkeen runner: the init script forks
    sing-box, which inherits our fds — piping them would make proc.wait()
    block forever on the long-lived daemon. DEVNULL breaks the inheritance
    chain. Diagnostics are lost; leak protection wins.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        return -1, str(exc)
    try:
        code = await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, f"timed out after {timeout_s:g}s"
    return code, ""
