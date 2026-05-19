"""VPN on/off toggle — POST /api/toggle."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from yonder.deps import PipelineDep, StateDep
from yonder.schemas import ToggleReq
from yonder.state import Data

router = APIRouter(prefix="/api", tags=["vpn"])


@router.post("/toggle")
async def toggle_vpn(req: ToggleReq, state: StateDep, pipeline: PipelineDep) -> Data:
    if req.on and state.active_server() is None:
        raise HTTPException(400, "no active server selected")

    def mutate(d: Data) -> None:
        d.vpn_on = req.on
        d.applying = True

    snap = await state.update(mutate)
    pipeline.signal()
    return snap
