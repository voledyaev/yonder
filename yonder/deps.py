"""FastAPI dependency accessors.

The app factory attaches state / pipeline / fetcher onto `app.state`; route
handlers pull them via `Annotated[..., Depends(get_X)]`. Tests can override
these dependencies via `app.dependency_overrides[get_X] = lambda: fake`.
"""

from __future__ import annotations

from typing import Annotated, Protocol

import httpx
from fastapi import Depends, Request

from yonder.state import State


class PipelineLike(Protocol):
    """Surface ApplyPipeline exposes to the API layer.

    Route handlers only need to nudge the pipeline; they don't care about
    its internal lifecycle. Protocol-typed so tests can pass any minimal
    stand-in that just records `signal()` calls.
    """

    def signal(self) -> None: ...


def get_state(request: Request) -> State:
    return request.app.state.yonder_state  # type: ignore[no-any-return]


def get_pipeline(request: Request) -> PipelineLike:
    return request.app.state.yonder_pipeline  # type: ignore[no-any-return]


def get_fetcher(request: Request) -> httpx.AsyncClient:
    return request.app.state.yonder_fetcher  # type: ignore[no-any-return]


StateDep = Annotated[State, Depends(get_state)]
PipelineDep = Annotated[PipelineLike, Depends(get_pipeline)]
FetcherDep = Annotated[httpx.AsyncClient, Depends(get_fetcher)]
