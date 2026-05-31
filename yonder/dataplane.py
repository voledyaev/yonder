"""Data-plane strategies behind the apply pipeline.

`ApplyPipeline` owns the worker loop + signal coalescing; the data plane owns
*how* an apply is carried out. Two implementations coexist during the
migration, chosen by the `YONDER_DATA_PLANE` env var:

* `XkeenDataPlane` — the original model: write xray config files, toggle the
  router's DoH via RCI, `xkeen restart`/`stop`. Restart flushes TPROXY rules.
* `SingBoxDataPlane` — the new model: one long-lived sing-box process with a
  selector outbound. A *structural* change (servers/rules/DNS) regenerates
  config.json and reloads; a pure *selection* change (pick server / on-off)
  is a live Clash API call — no restart, no netfilter flush.

Each plane also supplies the rules parser for its config format.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Protocol

from yonder.doh import disable_doh, enable_doh
from yonder.keenetic import KeeneticClient
from yonder.rules import parse_singbox_rules, parse_xray_rules
from yonder.services import XKeenService
from yonder.singbox.clash import ClashClient, ClashError
from yonder.singbox.config import SELECTOR_TAG, build_config, selector_default
from yonder.singbox.service import SingBoxService, write_config
from yonder.state import Data, State
from yonder.xray import XKEEN_CONFIGS_DIR, write_xkeen_split

logger = logging.getLogger(__name__)


class DataPlane(Protocol):
    """What the apply pipeline and the rules route need from a data plane."""

    def parse_rules(self, raw: bytes | str) -> list[dict[str, Any]]: ...

    async def apply(self, snap: Data) -> tuple[bool, str]: ...


def resolve_active_server(snap: Data):
    """Pull the active Server out of snap.subscriptions (or None)."""
    ref = snap.active_server
    if ref is None:
        return None
    for sub in snap.subscriptions:
        if sub.id != ref.subscription_id:
            continue
        for srv in sub.servers:
            if srv.id == ref.server_id:
                return srv
    return None


class XkeenDataPlane:
    """xkeen + xray + router-DoH (the original behaviour, unchanged)."""

    def __init__(
        self,
        state: State,
        services: XKeenService,
        keenetic: KeeneticClient,
        *,
        configs_dir: str | Path = XKEEN_CONFIGS_DIR,
    ):
        self._state = state
        self._services = services
        self._keenetic = keenetic
        self._configs_dir = configs_dir

    def parse_rules(self, raw: bytes | str) -> list[dict[str, Any]]:
        return parse_xray_rules(raw)

    async def apply(self, snap: Data) -> tuple[bool, str]:
        active = resolve_active_server(snap)
        try:
            write_xkeen_split(active, snap.rules or None, self._configs_dir)
        except Exception as exc:
            return False, f"write config failed: {exc}"

        if snap.vpn_on and active is not None:
            return await self._apply_on()
        return await self._apply_off()

    async def _apply_on(self) -> tuple[bool, str]:
        ok_doh, msg_doh = await enable_doh(self._state, self._keenetic)
        if not ok_doh:
            return False, f"DoH: {msg_doh}"
        ok_svc, msg_svc = await self._services.restart()
        if not ok_svc:
            try:
                await disable_doh(self._state, self._keenetic)
            except Exception:
                logger.exception("DoH rollback after xkeen failure also failed")
            return False, f"xkeen: {msg_svc}"
        return True, ""

    async def _apply_off(self) -> tuple[bool, str]:
        ok_svc, msg_svc = await self._services.stop()
        if not ok_svc:
            return False, f"xkeen: {msg_svc}"
        ok_doh, msg_doh = await disable_doh(self._state, self._keenetic)
        if not ok_doh:
            return False, f"DoH: {msg_doh}"
        return True, ""


class SingBoxDataPlane:
    """One sing-box process; structural change → reload, selection → live API."""

    def __init__(
        self,
        service: SingBoxService,
        clash: ClashClient,
        *,
        config_path: str | Path,
        selector_tag: str = SELECTOR_TAG,
    ):
        self._service = service
        self._clash = clash
        self._config_path = config_path
        self._selector = selector_tag
        # Structural fingerprint of the last-written config (servers/rules/dns,
        # excluding the selection). None until the first apply.
        self._last_key: str | None = None

    def parse_rules(self, raw: bytes | str) -> list[dict[str, Any]]:
        return parse_singbox_rules(raw)

    async def apply(self, snap: Data) -> tuple[bool, str]:
        cfg = build_config(snap)
        key = _structural_key(cfg)
        target = selector_default(snap)

        running = await self._service.is_running()
        if self._last_key != key or not running:
            return await self._reload(cfg, key)

        # Pure selection change — switch live, no restart / netfilter churn.
        try:
            await self._clash.select(self._selector, target)
            return True, ""
        except ClashError as exc:
            logger.warning("clash select failed (%s); falling back to reload", exc)
            return await self._reload(cfg, key)

    async def _reload(self, cfg: dict[str, Any], key: str) -> tuple[bool, str]:
        try:
            write_config(cfg, self._config_path)
        except Exception as exc:
            return False, f"write config failed: {exc}"
        ok, msg = await self._service.restart()
        if ok:
            self._last_key = key
        else:
            # Force a reload next time rather than trusting a half-applied run.
            self._last_key = None
        return ok, ("" if ok else f"sing-box: {msg}")


def _structural_key(cfg: dict[str, Any]) -> str:
    """Stable fingerprint of everything that requires a reload.

    The selector's `default` (which encodes the current server / on-off) is
    normalised out, so a pure selection change does NOT look structural and
    routes to the live Clash switch instead of a process restart.
    """
    c = copy.deepcopy(cfg)
    for ob in c.get("outbounds", []):
        if ob.get("type") == "selector":
            ob["default"] = ""
    return json.dumps(c, sort_keys=True)
