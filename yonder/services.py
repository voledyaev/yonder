"""Controls the xray daemon via the xkeen wrapper.

Abstracts the underlying service manager so the rest of the app just calls
restart() and gets a sensible result regardless of whether xkeen is present
(router) or not (local dev machine).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

# Path to the xkeen wrapper script installed by XKeen's own installer.
XKEEN_BIN = "/opt/sbin/xkeen"

# xkeen -start/-restart can take a while: it sets up iptables, may load
# kernel modules on first run, and may do a synchronous probe of the
# configured proxy server. 90s is the budget after which we assume xkeen
# has hung and kill it.
DEFAULT_TIMEOUT_S = 90.0


class XKeenService:
    """xkeen + xray service controller, async-friendly."""

    def __init__(
        self,
        bin_path: str | Path = XKEEN_BIN,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._bin = Path(bin_path)
        self._timeout = timeout_s

    def installed(self) -> bool:
        """True when the xkeen binary exists as a regular file.

        A stray symlink to a missing target is treated as "not installed".
        """
        return self._bin.is_file()

    async def start(self) -> tuple[bool, str]:
        return await self._invoke("-start")

    async def stop(self) -> tuple[bool, str]:
        return await self._invoke("-stop")

    async def restart(self) -> tuple[bool, str]:
        return await self._invoke("-restart")

    async def is_running(self) -> bool:
        """Best-effort check: is xray actually running right now?

        Uses pidof from busybox-ash; pgrep would require the procps-ng opkg.
        """
        code, _ = await _run(["pidof", "xray"], timeout_s=5.0)
        return code == 0

    async def _invoke(self, arg: str) -> tuple[bool, str]:
        if not self.installed():
            return True, "xkeen not installed; skipped (config still written)"
        code, err = await _run([str(self._bin), arg], timeout_s=self._timeout)
        if err:
            return False, err
        if code != 0:
            return False, f"xkeen exit {code}"
        return True, ""


async def _run(argv: list[str], timeout_s: float) -> tuple[int, str]:
    """Run `argv` with stdio fully discarded, return (exit_code, err_message).

    Stdio handling is load-bearing here. `xkeen -restart` forks `xray` as a
    daemon — xray inherits the parent's stdout/stderr file descriptors. If we
    pipe them, asyncio's read-until-EOF logic waits forever because the
    long-running xray daemon never closes its inherited fds. proc.wait() then
    blocks indefinitely, leaking the apply worker.

    Even more subtle: when the timeout fires, killing xkeen (our direct
    child) does NOT kill xray (already orphaned to init). So even after
    timeout the pipe stays open, the drainer task stays stuck, and the next
    call never gets to run.

    Both stdout and stderr must therefore be redirected to actual file
    descriptors via subprocess.DEVNULL (which opens /dev/null in the child).
    That breaks the inheritance chain. Diagnostic detail is lost, but
    daemon-leak protection wins.
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
