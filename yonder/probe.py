"""TCP latency probes for VLESS servers.

The goal is the same one Shadowrocket's "Test" button serves: run a quick
parallel TCP-connect to each candidate so the user can pick a server that's
both alive and reasonably close. We measure wall time of the TCP handshake
only — DNS resolution happens beforehand and is excluded from the timing
(otherwise the on-router resolver's 30-60ms per query would inflate every
measurement and make our numbers diverge from what mobile clients show).
"""

from __future__ import annotations

import asyncio
import socket
import time

from yonder.vless import Server


async def _resolve(host: str) -> str | None:
    """Return one IP for `host`, or None if the lookup fails.

    Prefers AF_INET to avoid an unreachable-IPv6 wait on dual-stack hosts —
    most VLESS endpoints are v4-only anyway, and picking v4 unconditionally
    keeps latency comparisons apples-to-apples across servers.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, family=socket.AF_INET)
    except OSError:
        return None
    return infos[0][4][0] if infos else None


async def probe_tcp(host: str, port: int, timeout: float) -> int | None:
    """Resolve `host`, then time the TCP handshake to (ip, port).

    Returns elapsed ms for the TCP portion only; None for resolve failure,
    connect timeout, refused, or any other socket-level error. The caller
    treats all of those the same way ("down").
    """
    ip = await _resolve(host)
    if ip is None:
        return None
    start = time.monotonic()
    try:
        # wait_for caps the connect; without it we'd hang on a host that
        # accepts the SYN but stalls before SYN-ACK.
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
    except (TimeoutError, OSError):
        return None
    elapsed_ms = int((time.monotonic() - start) * 1000)
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        # Some peers RST on close; we already have the timing we need.
        pass
    return elapsed_ms


async def probe_servers(servers: list[Server], *, timeout: float = 2.0) -> dict[str, int | None]:
    """Probe a batch of servers in parallel; return {server_id: ms-or-None}.

    Each probe resolves its own host inside `probe_tcp` (excluded from the
    timing). No global concurrency limit — a typical subscription is 20-50
    servers and the event loop handles that fine. If subscriptions grow
    much larger in practice we can add a Semaphore without touching call
    sites.
    """
    if not servers:
        return {}
    results = await asyncio.gather(
        *(probe_tcp(s.host, s.port, timeout) for s in servers),
        return_exceptions=False,
    )
    return {srv.id: ms for srv, ms in zip(servers, results, strict=True)}
