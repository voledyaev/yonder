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


# --- sing-box rules (migration target) -------------------------------------
#
# The xray validator above stays for the xkeen data plane until it's retired.
# On the sing-box data plane, rules are sing-box-native: yonder's generated
# config prepends sniff + resolve + private-direct and sets `final` to the
# selector, so a user file carries only the selective logic — which
# destinations go direct/block and which RU resources to override to proxy.
# `proxy` is an alias the route builder rewrites to the selector tag.

# Outbound targets a user rule may name on the sing-box plane.
_SB_VALID_OUTBOUNDS = {"proxy", "direct", "block"}

# sing-box rule matchers we accept; a route rule must carry at least one.
_SB_MATCH_FIELDS = (
    "domain",
    "domain_suffix",
    "domain_keyword",
    "domain_regex",
    "ip_cidr",
    "ip_is_private",
    "source_ip_cidr",
    "source_ip_is_private",
    "port",
    "port_range",
    "source_port",
    "network",
    "protocol",
    "rule_set",
    "process_name",
    "package_name",
    "clash_mode",
)

# Standalone (non-route) actions allowed without an outbound/matcher.
_SB_VALID_ACTIONS = {"sniff", "resolve", "reject", "hijack-dns", "route"}

# Dead giveaways of an xray rule pasted by mistake — fail with a pointer.
_XRAY_MARKERS = ("type", "outboundTag")


def parse_singbox_rules(raw: bytes | str) -> list[dict[str, Any]]:
    """Parse, strip `_comment`s from, and validate a sing-box rules document.

    Accepted top-level shapes: {"route": {"rules": [...]}}, {"rules": [...]},
    or a bare [...]. Returns the cleaned rule list ready for `route.rules`.

    sing-box rejects unknown fields, so `_comment` keys (which xray tolerated)
    are stripped recursively here.
    """
    import json

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        top = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RulesParseError(f"not valid JSON: {exc.msg}") from exc

    rules_raw = _sb_extract_rules_array(top)
    if not rules_raw:
        raise RulesParseError("`rules` is empty")

    out: list[dict[str, Any]] = []
    for i, ri in enumerate(rules_raw):
        if not isinstance(ri, dict):
            raise RulesParseError(f"rule[{i}] is not an object")
        rule = _sb_strip_comments(ri)
        _sb_validate_rule(i, rule)
        out.append(rule)
    return out


def _sb_extract_rules_array(top: Any) -> list[Any]:
    if isinstance(top, list):
        return top
    if isinstance(top, dict):
        route = top.get("route")
        if isinstance(route, dict) and isinstance(route.get("rules"), list):
            return route["rules"]
        if isinstance(top.get("rules"), list):
            return top["rules"]
        raise RulesParseError(
            'expected {"route": {"rules": [...]}} or {"rules": [...]} or a bare [...] array'
        )
    raise RulesParseError("expected JSON object or array at the top level")


def _sb_validate_rule(i: int, rule: dict[str, Any]) -> None:
    for marker in _XRAY_MARKERS:
        if marker in rule:
            raise RulesParseError(
                f"rule[{i}] looks like an xray rule (has {marker!r}); this is "
                "sing-box now — use domain_suffix/ip_cidr/rule_set + "
                "outbound: proxy|direct|block"
            )

    # Standalone action rule (sniff/resolve/...) needs no outbound/matcher.
    action = rule.get("action")
    if action is not None and "outbound" not in rule:
        if action not in _SB_VALID_ACTIONS:
            raise RulesParseError(f"rule[{i}].action {action!r} is not a known sing-box action")
        return

    if "outbound" not in rule:
        raise RulesParseError(f"rule[{i}].outbound is missing")
    tag = rule["outbound"]
    if tag not in _SB_VALID_OUTBOUNDS:
        raise RulesParseError(
            f"rule[{i}].outbound must be one of {sorted(_SB_VALID_OUTBOUNDS)}; got {tag!r}"
        )
    if not any(_present_and_nonempty(rule.get(f)) for f in _SB_MATCH_FIELDS):
        raise RulesParseError(
            f"rule[{i}] has no matcher — need at least one of {list(_SB_MATCH_FIELDS[:5])}…"
        )


def _sb_strip_comments(obj: Any) -> Any:
    """Recursively drop `_comment` keys (sing-box rejects unknown fields)."""
    if isinstance(obj, dict):
        return {k: _sb_strip_comments(v) for k, v in obj.items() if k != "_comment"}
    if isinstance(obj, list):
        return [_sb_strip_comments(x) for x in obj]
    return obj
