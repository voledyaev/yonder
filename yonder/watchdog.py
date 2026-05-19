"""Keeps the proxy alive when state.vpn_on is true.

Runs as a background coroutine inside the daemon. Every `interval` it:

1. reads vpn_on; if false, sleeps one tick
2. checks if xray is running; if alive, sleeps one tick
3. else calls services.restart() — XKeen re-establishes iptables tproxy
   and re-launches xray with the current config on disk

Failure mode is important: while vpn_on is true but xray is dead, XKeen's
iptables rules stay in place. Client traffic gets REDIRECT'd to port 1181
(xray's tproxy) and finds nothing listening → connection refused. So during
recovery, traffic fails closed — no leak to a direct route. That's the
kill-switch property of the design.

The watchdog does NOT go through the apply pipeline: a recovery restart
doesn't need to re-write xray configs or re-toggle DoH, since the only thing
that changed is that xray went down. Direct `services.restart()` is faster
and avoids interfering with an in-flight apply.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from yonder.services import XKeenService
from yonder.state import State

logger = logging.getLogger(__name__)


class WatchdogDeps(Protocol):
    """Minimal surface the watchdog needs.

    Protocol-typed so tests can pass any duck-compatible object and prod
    code wires StateServicesDeps below.
    """

    def vpn_on(self) -> bool: ...
    def applying(self) -> bool: ...
    async def is_running(self) -> bool: ...
    async def restart(self) -> tuple[bool, str]: ...


class StateServicesDeps:
    """Production WatchdogDeps backed by State + XKeenService."""

    def __init__(self, state: State, services: XKeenService):
        self._state = state
        self._services = services

    def vpn_on(self) -> bool:
        return self._state.snapshot().vpn_on

    def applying(self) -> bool:
        return self._state.snapshot().applying

    async def is_running(self) -> bool:
        return await self._services.is_running()

    async def restart(self) -> tuple[bool, str]:
        return await self._services.restart()


class Watchdog:
    """Background process supervisor for xray.

    Construct with a WatchdogDeps implementation, start with `await start()`,
    stop with `await stop()`.
    """

    def __init__(
        self,
        deps: WatchdogDeps,
        *,
        interval_s: float = 30.0,
        backoff_max_s: float = 300.0,
    ):
        self._deps = deps
        self._interval = interval_s
        self._backoff_max = backoff_max_s
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="yonder-watchdog")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _loop(self) -> None:
        logger.info("watchdog started (interval=%ss)", self._interval)
        failures = 0
        try:
            while not self._stop_event.is_set():
                failures = await self._tick(failures)
                sleep_s = self._sleep_for(failures)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_s)
                    break  # stop_event set
                except TimeoutError:
                    pass  # normal tick interval elapsed
        finally:
            logger.info("watchdog exited")

    async def _tick(self, failures: int) -> int:
        """One supervision pass. Returns the updated failure counter."""
        try:
            # Defer if the apply pipeline currently owns the world. Without
            # this we have a race: tick reads vpn_on=True, then the user
            # toggles off, apply stops xkeen → xray dies, and we'd then
            # restart xray after the user said "off". Apply pipeline always
            # sets applying=True synchronously before its work and clears
            # it at the end, so checking here naturally serialises.
            if self._deps.applying():
                logger.debug("apply in flight; skipping tick")
                return failures
            if not self._deps.vpn_on():
                return 0
            if await self._deps.is_running():
                return 0
            ok, msg = await self._deps.restart()
            if ok:
                logger.warning("xray was down; restart OK: %s", msg)
                return 0
            logger.error("xray was down; restart FAILED: %s", msg)
            return failures + 1
        except Exception:
            # Swallow exceptions from deps so a buggy dependency can't kill
            # the watchdog. Counter advances so backoff kicks in.
            logger.exception("watchdog tick errored")
            return failures + 1

    def _sleep_for(self, failures: int) -> float:
        """Exponential backoff when restarts keep failing, capped."""
        if failures == 0:
            return self._interval
        return min(self._interval * (2**failures), self._backoff_max)
