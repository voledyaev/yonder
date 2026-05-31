"""Fail-closed firewall guard for the xkeen restart window.

When xkeen restarts (server switch, toggle-on, watchdog recovery) it flushes
its TPROXY mangle rules and rebuilds them. In that gap — typically a couple
of seconds — forwarded LAN→WAN traffic that *should* be proxied no longer
gets intercepted, so it falls through to the direct route and leaks to the
ISP (real IP exposed, RKN-blocked sites fail, traffic unencrypted).

An xray *crash* is already fail-closed: xkeen's rules stay in place, the
TPROXY redirect points at a dead socket, and packets are dropped. The leak
is specific to xkeen tearing its own rules down on restart. So we bracket
every restart with a FORWARD DROP on the WAN interface: during the gap all
forwarded internet egress is blocked instead of leaking.

When xray is up this DROP is inert — proxied packets are consumed locally by
the TPROXY redirect and never reach the FORWARD egress path, so the rule only
ever bites during the flush window.

Rules are tagged with a comment so a leftover from a SIGKILL'd daemon (whose
`finally` never ran) can be swept on the next startup.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess

logger = logging.getLogger(__name__)

COMMENT = "yonder-killswitch"


async def _ipt(args: list[str], timeout: float = 5.0) -> int:
    """Run `iptables <args>` with stdio discarded; return exit code (-1 on
    missing binary / timeout). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "iptables",
            *args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return -1
    try:
        return await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return -1


async def detect_wan() -> str | None:
    """Return the default-route egress interface (e.g. 'eth3'), or None.

    None when there's no default route (nothing to leak to anyway) or `ip`
    is unavailable (non-router host) — callers then skip the guard.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ip",
            "route",
            "show",
            "default",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None
    toks = out.decode("utf-8", "replace").split()
    if "dev" in toks:
        i = toks.index("dev")
        if i + 1 < len(toks):
            return toks[i + 1]
    return None


def _insert_args(wan: str) -> list[str]:
    # Position 1: ahead of Keenetic's `ACCEPT ESTABLISHED,RELATED` (which
    # sits near the top of FORWARD), so in-flight connections are blocked
    # too — a partial kill switch that lets established flows leak is no
    # kill switch.
    return ["-I", "FORWARD", "1", "-o", wan, "-j", "DROP", "-m", "comment", "--comment", COMMENT]


def _delete_args(wan: str) -> list[str]:
    return ["-D", "FORWARD", "-o", wan, "-j", "DROP", "-m", "comment", "--comment", COMMENT]


async def engage(wan: str) -> bool:
    """Insert the fail-closed DROP. Returns True if it went in."""
    if await _ipt(_insert_args(wan)) == 0:
        logger.info("killswitch engaged on %s", wan)
        return True
    logger.warning("killswitch engage failed on %s", wan)
    return False


async def disengage(wan: str) -> None:
    """Remove the DROP — every copy, in case more than one slipped in."""
    for _ in range(8):
        if await _ipt(_delete_args(wan)) != 0:
            break


async def sweep() -> None:
    """Best-effort cleanup of a leftover rule on daemon startup.

    Covers the SIGKILL case where `disengage`'s `finally` never ran and a
    DROP was left blocking all egress.
    """
    wan = await detect_wan()
    if wan:
        await disengage(wan)
