"""Parses VLESS subscriptions into structured Server values.

Most providers serve subscriptions as a base64-encoded list of vless:// URIs
separated by newlines; some serve plaintext. Both forms are accepted.

Reference: docs/vless-format.md.
"""

from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field


class VlessParseError(ValueError):
    """Raised when a vless:// URI or subscription body cannot be parsed."""


class Server(BaseModel):
    """A parsed VLESS endpoint.

    `id` is "host:port" and is the stable key used by the rest of the app
    (state, UI, xray config).
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    country: str
    host: str
    port: int
    uuid: str
    params: dict[str, str] = Field(default_factory=dict)


_COUNTRY_ALIASES: dict[str, str] = {
    "польша": "PL",
    "poland": "PL",
    "испания": "ES",
    "spain": "ES",
    "германия": "DE",
    "germany": "DE",
    "венгрия": "HU",
    "hungary": "HU",
    "италия": "IT",
    "italy": "IT",
    "нидерланды": "NL",
    "голландия": "NL",
    "netherlands": "NL",
    "финляндия": "FI",
    "finland": "FI",
    "франция": "FR",
    "france": "FR",
    "великобритания": "GB",
    "англия": "GB",
    "uk": "GB",
    "united kingdom": "GB",
    "сша": "US",
    "usa": "US",
    "united states": "US",
    "america": "US",
    "швеция": "SE",
    "sweden": "SE",
    "норвегия": "NO",
    "norway": "NO",
    "дания": "DK",
    "denmark": "DK",
    "австрия": "AT",
    "austria": "AT",
    "швейцария": "CH",
    "switzerland": "CH",
    "бельгия": "BE",
    "belgium": "BE",
    "чехия": "CZ",
    "czech": "CZ",
    "словакия": "SK",
    "slovakia": "SK",
    "румыния": "RO",
    "romania": "RO",
    "болгария": "BG",
    "bulgaria": "BG",
    "молдова": "MD",
    "moldova": "MD",
    "украина": "UA",
    "ukraine": "UA",
    "казахстан": "KZ",
    "kazakhstan": "KZ",
    "армения": "AM",
    "armenia": "AM",
    "грузия": "GE",
    "georgia": "GE",
    "турция": "TR",
    "turkey": "TR",
    "япония": "JP",
    "japan": "JP",
    "корея": "KR",
    "south korea": "KR",
    "сингапур": "SG",
    "singapore": "SG",
    "гонконг": "HK",
    "hong kong": "HK",
    "канада": "CA",
    "canada": "CA",
    "австралия": "AU",
    "australia": "AU",
    "литва": "LT",
    "lithuania": "LT",
    "латвия": "LV",
    "latvia": "LV",
    "эстония": "EE",
    "estonia": "EE",
}

# Strips punctuation/decoration while preserving Unicode letters and digits.
_NON_WORD_RE = re.compile(r"[^\w\s-]+", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")

_FLAG_BASE = 0x1F1E6
_FLAG_UPPER = 0x1F1FF


def _flag_to_country(text: str) -> str:
    """Extract an ISO country code from a leading regional-indicator flag emoji.

    Flag emoji are pairs of code points in U+1F1E6..U+1F1FF; each pair maps to
    two ASCII letters via (cp - 0x1F1E6 + ord('A')).
    """
    if len(text) < 2:
        return ""
    a, b = ord(text[0]), ord(text[1])
    if _FLAG_BASE <= a <= _FLAG_UPPER and _FLAG_BASE <= b <= _FLAG_UPPER:
        return chr(a - _FLAG_BASE + ord("A")) + chr(b - _FLAG_BASE + ord("A"))
    return ""


def _name_to_country(text: str) -> str:
    s = _NON_WORD_RE.sub(" ", text.strip().lower())
    s = _WHITESPACE_RE.sub(" ", s).strip()
    if not s:
        return ""
    if s in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[s]
    for word in s.split():
        if word in _COUNTRY_ALIASES:
            return _COUNTRY_ALIASES[word]
    return ""


def detect_country(fragment: str) -> str:
    """Return ISO-3166 alpha-2 from fragment text, or '??' if undetectable."""
    if not fragment:
        return "??"
    return _flag_to_country(fragment) or _name_to_country(fragment) or "??"


def parse_link(uri: str) -> Server:
    """Parse a single vless://... URI into a Server.

    Raises VlessParseError on malformed input or missing uuid/host.
    """
    if not uri.startswith("vless://"):
        raise VlessParseError(f"not a vless URI: {uri[:80]}")

    # urlsplit handles vless:// even though it's not a registered scheme,
    # parsing userinfo / host / port / query / fragment the same as for http.
    parts = urlsplit(uri)

    uuid = unquote(parts.username) if parts.username else ""
    host = parts.hostname or ""
    if not uuid or not host:
        raise VlessParseError(f"missing uuid or host in: {uri[:80]}")

    try:
        port = parts.port if parts.port is not None else 443
    except ValueError as exc:
        raise VlessParseError(f"invalid port in: {uri[:80]}") from exc

    fragment = unquote(parts.fragment)

    # Collapse single-value query lists into scalars; drop empties.
    params: dict[str, str] = {}
    if parts.query:
        for pair in parts.query.split("&"):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            if k and v:
                params[unquote(k)] = unquote(v)

    server_id = f"{host}:{port}"
    name = fragment if fragment else server_id

    return Server(
        id=server_id,
        name=name,
        country=detect_country(fragment),
        host=host,
        port=port,
        uuid=uuid,
        params=params,
    )


def parse_subscription(body: bytes | str) -> list[Server]:
    """Parse a subscription body (base64 or plaintext auto-detected).

    Returns a deduplicated list of Servers (first occurrence wins per host:port).
    Malformed individual lines are silently skipped — providers occasionally
    include comments or future-format entries.
    """
    text = _decode_subscription_body(body)

    servers: list[Server] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not line.startswith("vless://"):
            continue
        try:
            srv = parse_link(line)
        except VlessParseError:
            continue
        if srv.id in seen:
            continue
        seen.add(srv.id)
        servers.append(srv)
    return servers


def _decode_subscription_body(body: bytes | str) -> str:
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="replace").strip()
    else:
        text = body.strip()

    if "vless://" in text:
        return text

    compact = re.sub(r"\s", "", text)
    if not compact:
        raise VlessParseError(
            "subscription body is neither plain vless:// list nor a base64-encoded one"
        )

    # Pad to multiple of 4, then try standard + URL-safe variants.
    padded = compact + "=" * ((4 - len(compact) % 4) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            decoded = decoder(padded).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            continue
        if "vless://" in decoded:
            return decoded

    raise VlessParseError(
        "subscription body is neither plain vless:// list nor a base64-encoded one"
    )
