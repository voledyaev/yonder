"""Validates user-supplied xray routing rules.

XKeen ships its own rules in 05_routing.json (we own that file and rewrite it).
Users may want to add or replace rules with their own — typically pointing
specific domains at the `proxy` outbound and the rest at `direct`. This
module accepts the three top-level shapes commonly seen in xray rule files
and normalises them into a flat list of rule dicts that yonder can drop
straight into 05_routing.json.
"""

from __future__ import annotations

from typing import Any

# xray accepts arbitrary outbound tag names, but yonder only constructs
# three (proxy / direct / block). Rules referencing any other tag would
# silently fall back to the first outbound, so we reject them at validation.
_VALID_OUTBOUND_TAGS = {"direct", "proxy", "block"}

# A rule that doesn't include any of these match fields matches nothing —
# almost always a user mistake. We surface it before the proxy restarts and
# the rule silently does nothing useful.
_MATCH_FIELDS = (
    "domain",
    "ip",
    "port",
    "network",
    "source",
    "user",
    "inboundTag",
    "protocol",
    "attrs",
)


class RulesParseError(ValueError):
    """Raised when a rules document fails validation."""


def parse_xray_rules(raw: bytes | str) -> list[dict[str, Any]]:
    """Parse and validate a routing-rules document.

    Three top-level shapes are accepted for convenience:
      1. {"routing": {"rules": [...], "domainStrategy": "..."}} — the exact
         shape XKeen ships in /opt/etc/xray/configs/05_routing.json.
      2. {"rules": [...]} — same minus the wrapping `routing` key.
      3. [...] — bare rules array.

    Each rule must have `outboundTag` in {direct, proxy, block} and at least
    one match field. Missing `type` is normalised to `"field"` (xray's only
    rule type).
    """
    import json

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        top = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RulesParseError(f"not valid JSON: {exc.msg}") from exc

    rules_raw = _extract_rules_array(top)
    if not rules_raw:
        raise RulesParseError("`rules` is empty")

    out: list[dict[str, Any]] = []
    for i, ri in enumerate(rules_raw):
        if not isinstance(ri, dict):
            raise RulesParseError(f"rule[{i}] is not an object")
        if "outboundTag" not in ri:
            raise RulesParseError(f"rule[{i}].outboundTag is missing")
        tag = ri["outboundTag"]
        if tag not in _VALID_OUTBOUND_TAGS:
            raise RulesParseError(
                f"rule[{i}].outboundTag must be one of {sorted(_VALID_OUTBOUND_TAGS)}; got {tag!r}"
            )
        if not any(_present_and_nonempty(ri.get(f)) for f in _MATCH_FIELDS):
            raise RulesParseError(
                f"rule[{i}] has no match field — need at least one of {list(_MATCH_FIELDS[:4])}…"
            )
        # xray expects type=field; some users omit it. Normalise.
        ri.setdefault("type", "field")
        out.append(ri)
    return out


def _extract_rules_array(top: Any) -> list[Any]:
    if isinstance(top, list):
        return top
    if isinstance(top, dict):
        routing = top.get("routing")
        if isinstance(routing, dict) and isinstance(routing.get("rules"), list):
            return routing["rules"]
        if isinstance(top.get("rules"), list):
            return top["rules"]
        raise RulesParseError(
            'expected {"routing": {"rules": [...]}} or {"rules": [...]} or a bare [...] array'
        )
    raise RulesParseError("expected JSON object or array at the top level")


def _present_and_nonempty(v: Any) -> bool:
    if v is None or v == "":
        return False
    if isinstance(v, (list, dict)) and len(v) == 0:
        return False
    return True
