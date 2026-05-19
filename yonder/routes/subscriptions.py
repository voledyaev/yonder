"""Subscription CRUD: add / delete / refresh / rename."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException

from yonder.deps import FetcherDep, PipelineDep, PipelineLike, StateDep
from yonder.fetch import FetchError, fetch_url
from yonder.schemas import AddSubscriptionReq, PatchSubscriptionReq
from yonder.state import Data, State
from yonder.vless import Server, VlessParseError, parse_link, parse_subscription

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])


@router.post("")
async def add_subscription(req: AddSubscriptionReq, state: StateDep, fetcher: FetcherDep) -> Data:
    label = req.label.strip()
    source = req.source.strip()
    if not label:
        label = _derive_label(source)
    servers = await _fetch_and_parse(fetcher, source)
    if not servers:
        raise HTTPException(400, "no usable servers in subscription")
    await state.add_subscription(label, source, servers)
    # New sub doesn't change runtime state — no apply needed.
    return state.snapshot()


@router.delete("/{sub_id}")
async def delete_subscription(sub_id: str, state: StateDep, pipeline: PipelineDep) -> Data:
    if not state.has_subscription(sub_id):
        raise HTTPException(404, f"unknown subscription: {sub_id!r}")
    prev = state.snapshot()
    affected = prev.active_server is not None and prev.active_server.subscription_id == sub_id
    await state.delete_subscription(sub_id)
    # Only trigger apply when deletion actually changes runtime state.
    if affected:
        await _mark_applying_and_signal(state, pipeline)
    return state.snapshot()


@router.post("/{sub_id}/refresh")
async def refresh_subscription(
    sub_id: str, state: StateDep, fetcher: FetcherDep, pipeline: PipelineDep
) -> Data:
    snap = state.snapshot()
    source = next((s.source for s in snap.subscriptions if s.id == sub_id), None)
    if source is None:
        raise HTTPException(404, f"unknown subscription: {sub_id!r}")
    servers = await _fetch_and_parse(fetcher, source)
    if not servers:
        raise HTTPException(400, "no usable servers in subscription")
    was_active_here = (
        snap.active_server is not None and snap.active_server.subscription_id == sub_id
    )
    await state.replace_subscription_servers(sub_id, servers)
    if was_active_here:
        await _mark_applying_and_signal(state, pipeline)
    return state.snapshot()


@router.patch("/{sub_id}")
async def patch_subscription(sub_id: str, req: PatchSubscriptionReq, state: StateDep) -> Data:
    if not state.has_subscription(sub_id):
        raise HTTPException(404, f"unknown subscription: {sub_id!r}")
    label = req.label.strip()
    if not label:
        source = next((s.source for s in state.snapshot().subscriptions if s.id == sub_id), "")
        label = _derive_label(source)
    await state.rename_subscription(sub_id, label)
    return state.snapshot()


# --- Helpers ----------------------------------------------------------------


async def _fetch_and_parse(fetcher: httpx.AsyncClient, source: str) -> list[Server]:
    """Resolve a subscription source to a server list.

    `vless://...` is parsed in place; HTTP(S) sources are fetched first.
    """
    if source.startswith("vless://"):
        raw: bytes = source.encode()
    else:
        try:
            raw = await fetch_url(fetcher, source)
        except FetchError as exc:
            raise HTTPException(502, str(exc)) from exc
    try:
        return parse_subscription(raw)
    except VlessParseError as exc:
        raise HTTPException(400, f"subscription parse failed: {exc}") from exc


def _derive_label(source: str) -> str:
    """Auto-generate a label from a subscription source.

    URL → host:port (or just host). Inline vless://... → embedded proxy host.
    Always returns a non-empty string.
    """
    s = source.strip()
    if s.startswith("vless://"):
        try:
            return parse_link(s).host or "vless link"
        except VlessParseError:
            return "vless link"
    return urlparse(s).netloc or "Subscription"


async def _mark_applying_and_signal(state: State, pipeline: PipelineLike) -> None:
    await state.update(lambda d: setattr(d, "applying", True))
    pipeline.signal()
