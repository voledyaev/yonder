"""Routing rules — set URL + refresh."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException

from yonder.deps import FetcherDep, PipelineDep, StateDep
from yonder.fetch import FetchError, fetch_url
from yonder.rules import RulesParseError, parse_xray_rules
from yonder.schemas import RulesURLReq
from yonder.state import Data, now_iso

router = APIRouter(prefix="/api", tags=["rules"])


@router.post("/rules-url")
async def set_rules_url(
    req: RulesURLReq, state: StateDep, fetcher: FetcherDep, pipeline: PipelineDep
) -> Data:
    if not req.url:
        # Clear: fall back to the bundled default rules.
        def clear(d: Data) -> None:
            d.rules_url = ""
            d.rules_fetched_at = ""
            d.rules = []
            d.rules_warnings = []
            d.rules_skipped_count = 0
            d.applying = True

        snap = await state.update(clear)
        pipeline.signal()
        return snap

    rules = await _fetch_and_validate(fetcher, req.url)

    def install(d: Data) -> None:
        d.rules_url = req.url  # type: ignore[assignment]
        d.rules_fetched_at = now_iso()
        d.rules = rules
        d.rules_warnings = []
        d.rules_skipped_count = 0
        d.applying = True

    snap = await state.update(install)
    pipeline.signal()
    return snap


@router.post("/rules/refresh")
async def refresh_rules(state: StateDep, fetcher: FetcherDep, pipeline: PipelineDep) -> Data:
    snap = state.snapshot()
    if not snap.rules_url:
        raise HTTPException(400, "no rules_url configured")
    rules = await _fetch_and_validate(fetcher, snap.rules_url)

    def install(d: Data) -> None:
        d.rules_fetched_at = now_iso()
        d.rules = rules
        d.rules_warnings = []
        d.rules_skipped_count = 0
        d.applying = True

    out = await state.update(install)
    pipeline.signal()
    return out


async def _fetch_and_validate(fetcher: httpx.AsyncClient, url: str) -> list[dict]:
    try:
        raw = await fetch_url(fetcher, url)
    except FetchError as exc:
        raise HTTPException(502, str(exc)) from exc
    try:
        return parse_xray_rules(raw)
    except RulesParseError as exc:
        raise HTTPException(400, str(exc)) from exc
