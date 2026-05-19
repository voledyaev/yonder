"""HTTP client for Keenetic's REST Core Interface (RCI).

Auth is a Keenetic-specific challenge-response over JSON: GET /auth returns
401 with realm + challenge headers; we compute SHA256(challenge +
MD5(login:realm:password)) and POST it to /auth, which sets a session
cookie. Subsequent /rci/* calls carry the cookie. Sessions expire after
~5 minutes of idle — we re-auth lazily on the first 401 after expiry.

Writes go through /rci/parse with CLI-style commands (e.g. "dns-proxy https
upstream URL"). The CLI mirror is simpler and more debuggable than the
tree-write JSON shape; reads use /rci/<path> which returns clean JSON.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import httpx
from pydantic import BaseModel


class KeeneticError(Exception):
    """Base class for Keenetic-RCI errors."""


class KeeneticAuthError(KeeneticError):
    """Raised when authentication fails (bad credentials, missing realm header)."""


class KeeneticCommandError(KeeneticError):
    """Raised when an /rci/parse command returned status="error"."""

    def __init__(self, command: str, code: str, ident: str, message: str):
        super().__init__(f"{ident} [{code}]: {message}")
        self.command = command
        self.code = code
        self.ident = ident
        self.message = message


class DohUpstream(BaseModel):
    """One entry in `dns-proxy https upstream` (a DoH endpoint).

    `format` is the over-the-wire encoding: dnsm (RFC 8484 DNS message,
    Keenetic default), json, or jsonm. We always use the default; the field
    is recorded for round-trip preservation when restoring user-set upstreams.
    """

    url: str
    format: str = "dnsm"


# Keenetic returns this ident in the status block whenever a `no <cmd>` is
# issued against an upstream that's not configured. We treat it as a no-op,
# not an error, because disable_doh() is allowed to run when nothing is set.
_NO_SUCH_UPSTREAM = "no such DNS-over-HTTPS server"


class KeeneticClient:
    """Async HTTP client for Keenetic RCI.

    Designed for use as an async context manager when the daemon owns the
    underlying httpx.AsyncClient, or with an externally-provided client (for
    testing or shared connection pooling).
    """

    def __init__(
        self,
        host: str = "http://192.168.1.1",
        login: str = "admin",
        password: str = "",
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ):
        self._host = host.rstrip("/")
        self._login = login
        self._password = password
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._authed = False

    async def __aenter__(self) -> KeeneticClient:
        await self._ensure_client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(cookies=httpx.Cookies(), timeout=self._timeout)
        return self._client

    async def _auth(self) -> None:
        """Run the challenge-response handshake; cookie ends up in client jar."""
        client = await self._ensure_client()
        # Step 1: solicit a challenge. /auth always returns 401 on GET and
        # sets the session cookie that subsequent steps reuse.
        r1 = await client.get(f"{self._host}/auth")
        realm = r1.headers.get("X-NDM-Realm")
        challenge = r1.headers.get("X-NDM-Challenge")
        if not realm or not challenge:
            raise KeeneticAuthError(
                f"missing realm/challenge in /auth response (HTTP {r1.status_code})"
            )
        # Step 2: compute response token.
        md5_pw = hashlib.md5(f"{self._login}:{realm}:{self._password}".encode()).hexdigest()
        token = hashlib.sha256((challenge + md5_pw).encode()).hexdigest()
        # Step 3: submit. 200 means the session cookie is now authorized.
        r2 = await client.post(
            f"{self._host}/auth",
            json={"login": self._login, "password": token},
        )
        if r2.status_code != 200:
            raise KeeneticAuthError(f"login failed: HTTP {r2.status_code} {r2.text[:120]}")
        self._authed = True

    async def _request(
        self,
        method: str,
        path: str,
        json_payload: Any = None,
    ) -> Any:
        """Authenticated request to /rci/<path>. Re-auths once on session
        expiry (401 mid-session). Raises for any other non-2xx."""
        async with self._lock:
            client = await self._ensure_client()
            if not self._authed:
                await self._auth()
            url = f"{self._host}{path}"
            resp = await client.request(method, url, json=json_payload)
            if resp.status_code == 401:
                self._authed = False
                await self._auth()
                resp = await client.request(method, url, json=json_payload)
            resp.raise_for_status()
            return resp.json() if resp.content else None

    async def get(self, path: str) -> Any:
        """Read a configuration tree path. Returns parsed JSON."""
        return await self._request("GET", path)

    async def parse(self, command: str) -> dict[str, Any]:
        """Execute a single CLI command via /rci/parse.

        Raises KeeneticCommandError if the response status block contains
        an "error" entry.
        """
        result = await self._request("POST", "/rci/parse", {"parse": command})
        _raise_on_error_status(command, result)
        return result

    # DoH-upstream helpers -------------------------------------------------

    async def list_doh_upstreams(self) -> list[DohUpstream]:
        items = await self.get("/rci/dns-proxy/https/upstream") or []
        return [DohUpstream(**item) for item in items]

    async def add_doh_upstream(self, url: str) -> None:
        """Register a DoH upstream. Idempotent: re-adding the same URL is a no-op."""
        await self.parse(f"dns-proxy https upstream {url}")

    async def remove_doh_upstream(self, url: str) -> None:
        """Unregister a DoH upstream. Idempotent: removing one that doesn't
        exist is silently treated as success."""
        try:
            await self.parse(f"no dns-proxy https upstream {url}")
        except KeeneticCommandError as exc:
            if _NO_SUCH_UPSTREAM in exc.message:
                return
            raise


def _raise_on_error_status(command: str, result: Any) -> None:
    """Inspect a /rci/parse response for error entries.

    Keenetic always returns HTTP 200, even for invalid commands. The actual
    success/failure is in `result["status"]` (top-level) or nested under
    the command's path. We walk the response looking for any status entry
    with status="error" and raise the first one we find.
    """
    if not isinstance(result, dict):
        return
    for entry in _walk_status_entries(result):
        if entry.get("status") == "error":
            raise KeeneticCommandError(
                command,
                code=str(entry.get("code", "")),
                ident=str(entry.get("ident", "")),
                message=str(entry.get("message", "")),
            )


def _walk_status_entries(obj: Any) -> list[dict[str, Any]]:
    """Recursively find all status-entry dicts in a Keenetic RCI response.

    Status blocks may appear at the root or nested under the command path
    (e.g. {"https": {"upstream": {"status": [...]}}}). We collect every list
    found at a "status" key and flatten the dict children.
    """
    out: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "status" and isinstance(value, list):
                out.extend(v for v in value if isinstance(v, dict))
            else:
                out.extend(_walk_status_entries(value))
    return out
