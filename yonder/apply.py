"""Single-worker apply pipeline.

One coroutine consumes apply signals and runs the active data plane's `apply`.
The pipeline owns the *machinery* (signal coalescing, the applying flag,
recording the result); the injected `DataPlane` owns *how* an apply happens
(xkeen restart vs. sing-box reload/live-switch). See `yonder.dataplane`.

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

from yonder.dataplane import DataPlane
from yonder.state import ApplyResult, Data, State, now_iso

logger = logging.getLogger(__name__)


class ApplyPipeline:
    """Owns the apply worker coroutine and its signaling Event.

    Construct once at daemon startup with a `DataPlane`, call `await start()`
    to spawn the worker, and `await stop()` for shutdown. Handlers call
    `signal()` (sync, never blocks) to request an apply.
    """

    def __init__(self, state: State, data_plane: DataPlane):
        self._state = state
        self._data_plane = data_plane
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
        return await self._data_plane.apply(self._state.snapshot())

    async def _record_result(self, ok: bool, msg: str) -> None:
        result = ApplyResult(at=now_iso(), ok=ok, msg=msg)

        def mutate(d: Data) -> None:
            d.applying = False
            d.last_apply = result
            d.last_error = "" if ok else msg

        await self._state.update(mutate)
        logger.info("apply: ok=%s msg=%r", ok, msg)
