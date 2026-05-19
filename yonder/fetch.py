"""HTTP fetch helper for subscription / rules URLs.

Bounded by size (1 MiB) and time (30s default). Streaming-read so an
oversize response is detected and dropped without buffering the whole body
in RAM — important on a router with constrained memory.
"""

from __future__ import annotations

import httpx

USER_AGENT = "yonder/0.3-py"

# Subscription bodies are short (a few KB typical). 1 MiB is a generous cap
# that still protects the daemon from a misconfigured source URL streaming
# megabytes at us. Reuse for rules fetches.
MAX_BODY_BYTES = 1 << 20

# Most subscription providers respond within a second. 30s tolerates slow
# upstreams without letting a hung connection block the apply pipeline.
DEFAULT_TIMEOUT_S = 30.0


class FetchError(Exception):
    """Raised for any fetch-time failure (network, HTTP non-2xx, oversize)."""


async def fetch_url(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int = MAX_BODY_BYTES,
) -> bytes:
    """GET url; return bytes; raise FetchError on any problem.

    Reads up to max_bytes+1 to detect overflow in a single pass without
    buffering the whole stream first.
    """
    try:
        async with client.stream("GET", url, headers={"User-Agent": USER_AGENT}) as resp:
            if not (200 <= resp.status_code < 300):
                raise FetchError(f"HTTP {resp.status_code}")
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError(f"response too large (>{max_bytes // 1024} KB limit)")
            return b"".join(chunks)
    except FetchError:
        raise
    except httpx.HTTPError as exc:
        raise FetchError(str(exc)) from exc
