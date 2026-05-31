"""Client for sing-box's Clash API (the experimental.clash_api controller).

This is how yonder switches servers and toggles on/off at runtime: a `PUT`
against the `selector` outbound changes the active server live — no process
restart, no netfilter flush, existing connections to the old server just
drain. It's the core win of the sing-box model over xkeen's restart-everything.

The controller has no secret (we configure `external_controller` only), so
requests are unauthenticated and local-only (127.0.0.1).
"""

from __future__ import annotations

import httpx


class ClashError(RuntimeError):
    """A Clash API call failed (sing-box down, unknown proxy/selector, etc.)."""


class ClashClient:
    """Thin async wrapper over the Clash API.

    Takes an httpx.AsyncClient so tests can inject a MockTransport. `base_url`
    points at the controller (matching `CLASH_API_ADDR` in the config).
    """

    def __init__(self, client: httpx.AsyncClient, base_url: str = "http://127.0.0.1:9090"):
        self._client = client
        self._base = base_url.rstrip("/")

    async def select(self, selector: str, name: str) -> None:
        """Switch `selector` to outbound `name` (the live server switch).

        `name` goes in the body, not the URL, so composite tags with `/` and
        `:` (e.g. "sub-1/host:443") need no escaping. Expects 204.
        """
        try:
            r = await self._client.put(f"{self._base}/proxies/{selector}", json={"name": name})
        except httpx.HTTPError as exc:
            raise ClashError(f"clash select failed: {exc}") from exc
        if r.status_code != 204:
            raise ClashError(f"clash select {selector}→{name}: HTTP {r.status_code} {r.text[:200]}")

    async def current(self, selector: str) -> str:
        """Return the selector's currently-selected outbound (`now`)."""
        try:
            r = await self._client.get(f"{self._base}/proxies/{selector}")
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise ClashError(f"clash current failed: {exc}") from exc
        return r.json().get("now", "")

    async def healthy(self) -> bool:
        """True when the controller answers — used by the watchdog to detect a
        wedged sing-box (process up but API unresponsive)."""
        try:
            r = await self._client.get(f"{self._base}/version")
            return r.status_code == 200
        except httpx.HTTPError:
            return False
