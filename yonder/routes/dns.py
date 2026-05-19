"""DNS-over-HTTPS upstream config — POST /api/dns/config."""

from __future__ import annotations

from fastapi import APIRouter

from yonder.deps import PipelineDep, StateDep
from yonder.schemas import DnsConfigReq
from yonder.state import Data

router = APIRouter(prefix="/api/dns", tags=["dns"])


@router.post("/config")
async def set_dns_config(req: DnsConfigReq, state: StateDep, pipeline: PipelineDep) -> Data:
    def mutate(d: Data) -> None:
        d.dns.doh_url = req.doh_url
        d.applying = True

    snap = await state.update(mutate)
    # Always signal — apply pipeline figures out whether to swap the upstream
    # right now (if VPN is on) or just record the new default (if off).
    pipeline.signal()
    return snap
