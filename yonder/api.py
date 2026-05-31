"""FastAPI app factory + lifespan for the yonder daemon.

Two construction modes share `create_app()`:

* **Production**: call `create_app()` with no deps. The default `lifespan`
  reads env vars and builds State / ApplyPipeline / Watchdog / Keenetic
  client / httpx fetcher on startup; tears them all down on shutdown.
  Uvicorn drives the lifespan via SIGINT/SIGTERM, so `__main__.py` can be
  a five-line entry point.

* **Tests**: pass pre-built deps directly. No lifespan runs; tests retain
  full control over wiring and shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from yonder import killswitch
from yonder.apply import ApplyPipeline
from yonder.dataplane import SingBoxDataPlane, SingBoxWatchdogDeps, XkeenDataPlane
from yonder.deps import PipelineLike
from yonder.fetch import DEFAULT_TIMEOUT_S
from yonder.keenetic import KeeneticClient
from yonder.routes import dns, meta, rules, server, subscriptions, vpn
from yonder.services import XKeenService
from yonder.singbox.clash import ClashClient
from yonder.singbox.service import SINGBOX_CONFIG, SingBoxService
from yonder.state import State
from yonder.watchdog import StateServicesDeps, Watchdog
from yonder.xray import XKEEN_CONFIGS_DIR

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    state: State | None = None,
    pipeline: PipelineLike | None = None,
    fetcher: httpx.AsyncClient | None = None,
    *,
    xray_configs_dir: str | Path = XKEEN_CONFIGS_DIR,
) -> FastAPI:
    """Build the FastAPI app.

    If all three deps are passed, the app skips the lifespan and uses the
    given objects as-is (test mode). If any dep is `None`, the production
    `_lifespan` runs at startup to read env vars and build the missing deps.
    """
    test_mode = state is not None and pipeline is not None and fetcher is not None
    app = FastAPI(
        title="yonder",
        default_response_class=JSONResponse,
        lifespan=None if test_mode else _lifespan,
    )

    if test_mode:
        app.state.yonder_state = state
        app.state.yonder_pipeline = pipeline
        app.state.yonder_fetcher = fetcher
        app.state.yonder_xray_configs_dir = str(xray_configs_dir)

    _register_exception_handlers(app)
    _include_routers(app)
    _register_static(app)
    return app


def _include_routers(app: FastAPI) -> None:
    # Specific routers first; catch-all 404 last so it only matches what
    # nothing else picked up.
    for module in (subscriptions, server, vpn, dns, rules, meta):
        app.include_router(module.router)
    app.include_router(meta.catch_all_router)


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation_to_400(_req: Request, exc: RequestValidationError) -> JSONResponse:
        # Go returned 400 for body/field validation errors; FastAPI defaults
        # to 422. Surface a single human-readable message so the UI shows
        # something useful instead of FastAPI's verbose error list.
        msg = exc.errors()[0].get("msg", "invalid request") if exc.errors() else "invalid request"
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]
        return JSONResponse(status_code=400, content={"error": msg})

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_as_error(_req: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Normalise to `{"error": msg}` (Go API shape). Covers both our
        # raised HTTPExceptions and Starlette's auto-raised ones (405 for
        # wrong method, etc).
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail or "error"},
        )


def _register_static(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    # Mounted last so explicit routes above (and the catch-all 404 for
    # /api/*) take precedence; this only serves static files at non-/api
    # paths like /style.css and /app.js.
    app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")


# --- Production lifespan ---------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire deps from env on startup; tear down on shutdown."""
    base_dir = os.environ.get("YONDER_BASE_DIR")
    if not base_dir:
        raise RuntimeError("YONDER_BASE_DIR is not set; refusing to guess where to put state.json")
    xray_dir = os.environ.get("YONDER_XRAY_CONFIGS") or XKEEN_CONFIGS_DIR
    plane = (os.environ.get("YONDER_DATA_PLANE") or "xkeen").lower()

    base = Path(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    state_path = base / "state.json"
    logger.info("state path: %s", state_path)
    logger.info("data plane: %s", plane)

    state = State(state_path)
    fetcher = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S)
    # Teardown closures, accumulated per data plane.
    closers: list = [fetcher.aclose]

    if plane == "singbox":
        sb_config = os.environ.get("YONDER_SINGBOX_CONFIG") or SINGBOX_CONFIG
        clash_url = os.environ.get("YONDER_CLASH_API") or "http://127.0.0.1:9090"
        logger.info("sing-box config: %s; clash api: %s", sb_config, clash_url)
        service = SingBoxService(killswitch_enabled=True)
        clash_http = httpx.AsyncClient(timeout=10.0)
        clash = ClashClient(clash_http, base_url=clash_url)
        data_plane = SingBoxDataPlane(service, clash, config_path=sb_config)
        watchdog = Watchdog(SingBoxWatchdogDeps(state, service, clash))
        closers.append(clash_http.aclose)
    else:
        kn_host = os.environ.get("YONDER_KEENETIC_HOST") or "http://192.168.1.1"
        kn_user = os.environ.get("YONDER_KEENETIC_USER") or "admin"
        kn_pw = os.environ.get("YONDER_KEENETIC_PW", "")
        if not kn_pw:
            logger.warning("YONDER_KEENETIC_PW is empty — DoH apply/restore will fail until set")
        logger.info("xray configs: %s; router RCI: %s", xray_dir, kn_host)
        # Kill switch on in production: bracket every xkeen restart with a
        # fail-closed FORWARD DROP so the rule-flush window can't leak.
        services = XKeenService(killswitch_enabled=True)
        keenetic = KeeneticClient(host=kn_host, login=kn_user, password=kn_pw)
        data_plane = XkeenDataPlane(state, services, keenetic, configs_dir=xray_dir)
        watchdog = Watchdog(StateServicesDeps(state, services))
        closers.append(keenetic.close)

    pipeline = ApplyPipeline(state, data_plane)

    app.state.yonder_state = state
    app.state.yonder_pipeline = pipeline
    app.state.yonder_fetcher = fetcher
    app.state.yonder_dataplane = data_plane
    app.state.yonder_xray_configs_dir = str(xray_dir)

    # Clear any kill-switch DROP left over from a hard kill (SIGKILL skips the
    # disengage `finally`) before anything else, so we never boot with egress
    # silently blocked.
    await killswitch.sweep()

    await pipeline.start()
    await watchdog.start()
    # Reconcile the data plane with whatever vpn_on persisted from the last
    # run — a daemon restart never leaves the proxy out of sync.
    pipeline.signal()

    try:
        yield
    finally:
        logger.info("shutting down background tasks")
        # Best-effort: gather all teardowns so a failure in one doesn't
        # leak the others.
        await asyncio.gather(
            pipeline.stop(),
            watchdog.stop(),
            *(c() for c in closers),
            return_exceptions=True,
        )
