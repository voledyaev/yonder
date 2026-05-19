"""Single-worker apply pipeline.

One coroutine consumes apply signals and walks the on/off state machine:

    ON  = write xray configs → enable DoH → xkeen restart
    OFF = xkeen stop → disable DoH

Handlers do not call apply steps directly. They mutate state + call
`pipeline.signal()`, which is non-blocking. Multiple rapid clicks coalesce
into one extra worker iteration (the worker re-reads `state.snapshot()` on
each pass, so the final intent always wins).

`data.applying` is the UI-visible "we're churning" flag. Handlers set it to
True *before* responding to the user (so the very next poll shows it true);
the worker clears it after each iteration. If a signal arrives during an
iteration, the next iteration starts immediately and the handler will have
set applying=True again — so the UI sees the flag stay continuously on for
the duration of back-to-back applies.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from yonder.doh import disable_doh, enable_doh
from yonder.keenetic import KeeneticClient
from yonder.services import XKeenService
from yonder.state import ApplyResult, Data, State, now_iso
from yonder.xray import XKEEN_CONFIGS_DIR, write_xkeen_split

logger = logging.getLogger(__name__)


class ApplyPipeline:
    """Owns the apply worker coroutine and its signaling Event.

    Construct once at daemon startup, call `await start()` to spawn the
    worker, and `await stop()` for shutdown. Handlers call `signal()`
    (sync, never blocks) to request an apply.
    """

    def __init__(
        self,
        state: State,
        services: XKeenService,
        keenetic: KeeneticClient,
        *,
        configs_dir: str | Path = XKEEN_CONFIGS_DIR,
    ):
        self._state = state
        self._services = services
        self._keenetic = keenetic
        self._configs_dir = configs_dir
        self._signal_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="yonder-apply-loop")

    async def stop(self) -> None:
        self._stop_event.set()
        self._signal_event.set()  # wake the loop so it observes stop_event
        if self._task is not None:
            await self._task
            self._task = None

    def signal(self) -> None:
        """Non-blocking request for an apply. Coalesces with pending signals."""
        self._signal_event.set()

    async def _loop(self) -> None:
        logger.info("apply loop started")
        try:
            while not self._stop_event.is_set():
                await self._signal_event.wait()
                # Clear BEFORE doing work, so any signal arriving during the
                # iteration triggers another pass — same coalescing semantics
                # as Go's buffered-1 channel.
                self._signal_event.clear()
                if self._stop_event.is_set():
                    break
                ok, msg = await self._apply_once()
                await self._record_result(ok, msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("apply loop crashed; restarting")
        finally:
            logger.info("apply loop exited")

    async def _apply_once(self) -> tuple[bool, str]:
        snap = self._state.snapshot()

        # Always write xray configs from current state — cheap, keeps the
        # files in sync with what UI shows even when VPN is off.
        active = _resolve_active_server(snap)
        try:
            write_xkeen_split(active, snap.rules or None, self._configs_dir)
        except Exception as exc:
            return False, f"write config failed: {exc}"

        want_on = snap.vpn_on and active is not None
        if want_on:
            return await self._apply_on()
        return await self._apply_off()

    async def _apply_on(self) -> tuple[bool, str]:
        # DoH must be in place BEFORE xray starts so xray's first lookups
        # go through encrypted DNS, not the ISP resolver.
        ok_doh, msg_doh = await enable_doh(self._state, self._keenetic)
        if not ok_doh:
            return False, f"DoH: {msg_doh}"

        ok_svc, msg_svc = await self._services.restart()
        if not ok_svc:
            # xkeen failed — roll DoH back so the router doesn't end up with
            # our DNS upstream but no working tunnel.
            try:
                await disable_doh(self._state, self._keenetic)
            except Exception:
                logger.exception("DoH rollback after xkeen failure also failed")
            return False, f"xkeen: {msg_svc}"

        return True, ""

    async def _apply_off(self) -> tuple[bool, str]:
        # Stop xkeen first (so traffic is no longer relying on the proxy),
        # then unwind DoH back to whatever the user had before.
        ok_svc, msg_svc = await self._services.stop()
        if not ok_svc:
            return False, f"xkeen: {msg_svc}"

        ok_doh, msg_doh = await disable_doh(self._state, self._keenetic)
        if not ok_doh:
            return False, f"DoH: {msg_doh}"

        return True, ""

    async def _record_result(self, ok: bool, msg: str) -> None:
        result = ApplyResult(at=now_iso(), ok=ok, msg=msg)

        def mutate(d: Data) -> None:
            d.applying = False
            d.last_apply = result
            d.last_error = "" if ok else msg

        await self._state.update(mutate)
        logger.info("apply: ok=%s msg=%r", ok, msg)


def _resolve_active_server(snap: Data):
    """Pull the active Server out of snap.subscriptions (or None)."""
    ref = snap.active_server
    if ref is None:
        return None
    for sub in snap.subscriptions:
        if sub.id != ref.subscription_id:
            continue
        for srv in sub.servers:
            if srv.id == ref.server_id:
                return srv
    return None
