"""Persistent state of the yonder daemon.

Single JSON file under the daemon's data directory. Atomic writes via
write-temp-then-rename so a crash mid-write cannot corrupt saved state.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from yonder.vless import Server

# v2 introduced multi-subscription support (each subscription becomes its own
# card in the UI). v1 had a single subscription_url + flat servers list; we
# do not migrate — a v1 file silently falls back to v2 defaults and the user
# re-enters their subscriptions through the UI.
SCHEMA_VERSION = 2


class Subscription(BaseModel):
    """One source of VLESS servers.

    Source may be an HTTP(S) URL (which yonder fetches), or a literal
    `vless://...` URI (parsed inline; refresh is a no-op).
    """

    id: str
    label: str
    source: str
    fetched_at: str
    servers: list[Server] = Field(default_factory=list)


class ActiveServerRef(BaseModel):
    """Pointer to one server inside one subscription.

    Composite key because two subscriptions can in principle contain the same
    host:port — making this composite also means deleting a subscription
    cleanly resets the active server when the active selection was inside it.
    """

    subscription_id: str
    server_id: str


class ApplyResult(BaseModel):
    """Outcome of the most recent xkeen apply cycle.

    Persisted so the UI can show a non-transient status — earlier we wiped
    `last_error` on the next successful apply, which made transient failures
    invisible if the user kept clicking.
    """

    at: str
    ok: bool
    msg: str


DEFAULT_DOH_URL = "https://cloudflare-dns.com/dns-query"


class PingResult(BaseModel):
    """Latest TCP-probe result for a single server.

    `ms` is None when the probe timed out or the connect failed — the UI
    renders that as "down". `at` lets the UI show when the measurement
    was taken (results don't auto-expire; they stay until re-tested).
    """

    ms: int | None
    at: str


class DnsState(BaseModel):
    """DoH-upstream state on the Keenetic router.

    `doh_url` is what the user wants (editable from the UI).
    `active_url` is what yonderd has actually pushed to the router right now
    (None when no DoH is currently applied by us — either VPN is off, or we
    haven't reached the first apply yet). Tracking the applied URL separately
    matters when the user edits `doh_url` while VPN is on: on disable we must
    remove the URL we actually pushed, not the new one in settings.

    `previous_upstreams` snapshots whatever DoH endpoints the user already had
    configured before yonderd touched `dns-proxy`, so disable_doh can restore
    them instead of leaving the router DoH-less.
    """

    doh_url: str = DEFAULT_DOH_URL
    active_url: str | None = None
    previous_upstreams: list[str] = Field(default_factory=list)


class Data(BaseModel):
    """On-disk schema. Also what snapshot() returns to callers."""

    version: int = SCHEMA_VERSION
    subscriptions: list[Subscription] = Field(default_factory=list)
    active_server: ActiveServerRef | None = None
    vpn_on: bool = False
    rules_url: str = ""
    rules_fetched_at: str = ""
    rules: list[dict[str, Any]] = Field(default_factory=list)
    rules_warnings: list[str] = Field(default_factory=list)
    rules_skipped_count: int = 0
    last_error: str = ""
    last_apply: ApplyResult | None = None
    applying: bool = False
    dns: DnsState = Field(default_factory=DnsState)
    # Keyed by server.id (host:port). Stale entries (servers that no longer
    # exist in any subscription) are harmless — the UI ignores keys it can't
    # match to a current server tile.
    pings: dict[str, PingResult] = Field(default_factory=dict)


class State:
    """Async-safe wrapper around the JSON file.

    Reads return deep copies and take no lock — callers may freely mutate
    the returned object without affecting stored state. Writes go through
    `update()` which serializes via an asyncio.Lock and persists atomically.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._data = self._load()

    def _load(self) -> Data:
        """Load from disk, falling back to defaults on missing/corrupt/v1 file."""
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return Data()
        try:
            loaded = Data.model_validate_json(raw)
        except ValidationError:
            # Corrupt file or older schema: keep defaults, don't crash. The
            # next save will rewrite cleanly. We do not migrate v1 — its
            # subscription_url/servers shape would surface as "empty
            # subscriptions but vpn_on=true", so a full reset is safer.
            return Data()
        if loaded.version != SCHEMA_VERSION:
            return Data()
        return loaded

    def snapshot(self) -> Data:
        """Return a deep-copied snapshot safe to mutate."""
        return self._data.model_copy(deep=True)

    def active_server(self) -> Server | None:
        """Resolve the active selection to a concrete Server, or None if
        nothing is selected or the selection points at a server that no
        longer exists (deleted subscription, refreshed-away server, etc).
        """
        ref = self._data.active_server
        if ref is None:
            return None
        for sub in self._data.subscriptions:
            if sub.id != ref.subscription_id:
                continue
            for srv in sub.servers:
                if srv.id == ref.server_id:
                    return srv.model_copy(deep=True)
            return None
        return None

    def has_subscription(self, subscription_id: str) -> bool:
        return any(sub.id == subscription_id for sub in self._data.subscriptions)

    def has_server(self, subscription_id: str, server_id: str) -> bool:
        for sub in self._data.subscriptions:
            if sub.id != subscription_id:
                continue
            return any(srv.id == server_id for srv in sub.servers)
        return False

    async def update(self, fn: Callable[[Data], None]) -> Data:
        """Apply `fn` under the lock, persist, and return a snapshot.

        Use this for atomic read-modify-write of multiple fields. `fn`
        mutates the passed-in Data in place.
        """
        async with self._lock:
            fn(self._data)
            self._save_locked()
            return self._data.model_copy(deep=True)

    async def add_subscription(self, label: str, source: str, servers: list[Server]) -> Data:
        """Append a new subscription with a freshly-generated ID.

        Labels and sources are not deduplicated — users may want multiple
        cards for the same source under different labels.
        """
        sub = Subscription(
            id=_new_subscription_id(),
            label=label,
            source=source,
            fetched_at=now_iso(),
            servers=list(servers) if servers else [],
        )
        return await self.update(lambda d: d.subscriptions.append(sub))

    async def delete_subscription(self, subscription_id: str) -> Data:
        """Remove a subscription by ID.

        If the active server was inside it, the active selection is cleared
        and vpn_on is forced off (so the apply worker stops xkeen rather
        than running it without a target).
        """

        def mutate(d: Data) -> None:
            d.subscriptions = [s for s in d.subscriptions if s.id != subscription_id]
            if d.active_server and d.active_server.subscription_id == subscription_id:
                d.active_server = None
                d.vpn_on = False

        return await self.update(mutate)

    async def replace_subscription_servers(
        self, subscription_id: str, servers: list[Server]
    ) -> Data:
        """Update a subscription's server list (after a refresh or source edit).

        If the active server was inside this subscription and is no longer in
        the new server list, the active selection is cleared and vpn_on is
        forced off.
        """
        new_servers = list(servers) if servers else []

        def mutate(d: Data) -> None:
            for sub in d.subscriptions:
                if sub.id != subscription_id:
                    continue
                sub.servers = new_servers
                sub.fetched_at = now_iso()
                break
            if d.active_server is None or d.active_server.subscription_id != subscription_id:
                return
            still_present = any(srv.id == d.active_server.server_id for srv in new_servers)
            if not still_present:
                d.active_server = None
                d.vpn_on = False

        return await self.update(mutate)

    async def merge_pings(self, results: dict[str, int | None]) -> Data:
        """Record ping results, overwriting any prior entries for the same IDs.

        Other entries are left untouched — a per-subscription test should not
        wipe results from other subscriptions that were tested earlier.
        """
        at = now_iso()

        def mutate(d: Data) -> None:
            for server_id, ms in results.items():
                d.pings[server_id] = PingResult(ms=ms, at=at)

        return await self.update(mutate)

    async def rename_subscription(self, subscription_id: str, label: str) -> Data:
        def mutate(d: Data) -> None:
            for sub in d.subscriptions:
                if sub.id == subscription_id:
                    sub.label = label
                    return

        return await self.update(mutate)

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        raw = self._data.model_dump_json(indent=2).encode()
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_bytes(raw)
        tmp.replace(self._path)


def _new_subscription_id() -> str:
    """Globally-unique ID for a new subscription.

    Format: "sub-<unix>-<6 hex>". Time prefix sorts naturally; hex suffix
    disambiguates within the same second.
    """
    return f"sub-{int(datetime.now(UTC).timestamp())}-{secrets.token_hex(3)}"


def now_iso() -> str:
    """Current UTC time as ISO-8601 with second precision."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
