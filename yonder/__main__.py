"""Entry point for the yonder daemon.

Just reads the listen address from the environment and hands off to
uvicorn. All other configuration (state dir, xray configs path, Keenetic
RCI credentials) is read by the FastAPI lifespan in `yonder.api`, so that
test code can build the same app without touching the environment.

Environment variables (set by the installer's init script):

    YONDER_BASE_DIR        absolute path for per-router data (state.json).
                           Required; refuses to guess.
    YONDER_LISTEN          listen address, default "0.0.0.0:8080".
    YONDER_XRAY_CONFIGS    XKeen configs dir, default /opt/etc/xray/configs.
    YONDER_KEENETIC_HOST   RCI base URL, default http://192.168.1.1.
    YONDER_KEENETIC_USER   RCI login, default "admin".
    YONDER_KEENETIC_PW     RCI password — required for DoH-toggle to work.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from yonder.api import create_app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s yonderd %(name)s %(levelname)s %(message)s",
    )

    listen = os.environ.get("YONDER_LISTEN") or "0.0.0.0:8080"
    host, _, port_s = listen.rpartition(":")
    host = host or "0.0.0.0"
    port = int(port_s)

    logging.getLogger("yonder").info("listening on http://%s:%s/", host, port)

    # Lifespan in yonder.api handles everything else: builds State, ApplyPipeline,
    # Watchdog, KeeneticClient, the httpx fetcher; tears them down cleanly when
    # uvicorn signals shutdown (SIGINT/SIGTERM).
    uvicorn.run(
        create_app(),
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
