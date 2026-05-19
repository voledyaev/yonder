"""Tests for the TCP-probe primitive.

We bind ephemeral localhost listeners so the tests stay hermetic — no
external network. The "down" case uses port 1 (privileged, always
refused on a developer machine without root) plus a tight timeout.
"""

from __future__ import annotations

import asyncio

from yonder.probe import probe_servers, probe_tcp
from yonder.vless import Server


async def test_probe_tcp_returns_ms_for_live_listener():
    """A loopback listener completes the handshake immediately; we
    expect a non-negative int — typically single-digit milliseconds."""
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        ms = await probe_tcp("127.0.0.1", port, timeout=1.0)
    finally:
        server.close()
        await server.wait_closed()
    assert ms is not None
    assert ms >= 0


async def test_probe_tcp_returns_none_on_refused():
    # Pick a closed port: bind+release, then probe — the kernel won't
    # immediately reuse it, so the connect gets ECONNREFUSED.
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()

    ms = await probe_tcp("127.0.0.1", port, timeout=1.0)
    assert ms is None


async def test_probe_tcp_returns_none_on_timeout(monkeypatch):
    # Real "no route" hosts behave unpredictably across OSes (macOS can
    # return ENETUNREACH instantly while Linux times out); patch the
    # connect to hang so we deterministically exercise the timeout path.
    async def hang(*_a, **_kw):
        await asyncio.sleep(10)

    monkeypatch.setattr("yonder.probe.asyncio.open_connection", hang)
    # Use a valid literal so _resolve succeeds and we actually exercise
    # the connect-timeout path (rather than failing earlier in getaddrinfo).
    ms = await probe_tcp("127.0.0.1", 443, timeout=0.05)
    assert ms is None


async def test_probe_tcp_returns_none_on_resolve_failure(monkeypatch):
    # If DNS lookup fails, we never reach the connect attempt — the caller
    # should still see None and surface "down" in the UI.
    async def fail_resolve(*_a, **_kw):
        raise OSError("synthetic DNS failure")

    monkeypatch.setattr(
        "yonder.probe.asyncio.get_running_loop",
        lambda: type("L", (), {"getaddrinfo": fail_resolve})(),
    )
    ms = await probe_tcp("anything.example", 443, timeout=1.0)
    assert ms is None


async def test_probe_servers_empty_input():
    assert await probe_servers([]) == {}


async def test_probe_servers_returns_per_server_results():
    """One real listener + one closed port: result map has entries for
    both, with the live one being a number and the dead one None."""
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    live_port = server.sockets[0].getsockname()[1]

    # Bind a second socket on a different ephemeral port, then immediately
    # release it — connects to that port should now refuse cleanly.
    closer = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    dead_port = closer.sockets[0].getsockname()[1]
    closer.close()
    await closer.wait_closed()

    try:
        live = Server(
            id=f"127.0.0.1:{live_port}",
            name="live",
            country="??",
            host="127.0.0.1",
            port=live_port,
            uuid="x",
        )
        dead = Server(
            id=f"127.0.0.1:{dead_port}",
            name="dead",
            country="??",
            host="127.0.0.1",
            port=dead_port,
            uuid="x",
        )
        results = await probe_servers([live, dead], timeout=1.0)
    finally:
        server.close()
        await server.wait_closed()

    assert set(results.keys()) == {live.id, dead.id}
    assert isinstance(results[live.id], int)
    assert results[dead.id] is None
