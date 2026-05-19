"""Pydantic request schemas for the HTTP API.

Kept separate from the route handlers so they're easy to find and easy to
import from tests. Response models live in `yonder.state` (the `Data` type
is what every successful endpoint returns).
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

MAX_LABEL_LEN = 100
MAX_SOURCE_LEN = 4096
MAX_DOH_URL_LEN = 2048


class AddSubscriptionReq(BaseModel):
    label: str = ""
    source: str

    @field_validator("label")
    @classmethod
    def _label_size(cls, v: str) -> str:
        if len(v) > MAX_LABEL_LEN:
            raise ValueError(f"label is too long (max {MAX_LABEL_LEN} chars)")
        return v

    @field_validator("source")
    @classmethod
    def _source_shape(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("source is required")
        if len(v) > MAX_SOURCE_LEN:
            raise ValueError(f"source is too long (max {MAX_SOURCE_LEN} chars)")
        if not v.startswith(("http://", "https://", "vless://")):
            raise ValueError("source must start with http://, https://, or vless://")
        return v


class PatchSubscriptionReq(BaseModel):
    label: str = ""

    @field_validator("label")
    @classmethod
    def _size(cls, v: str) -> str:
        if len(v) > MAX_LABEL_LEN:
            raise ValueError(f"label is too long (max {MAX_LABEL_LEN} chars)")
        return v


class ServerSelectReq(BaseModel):
    """Both null → deselect (set active to None)."""

    subscription_id: str | None = None
    server_id: str | None = None


class ToggleReq(BaseModel):
    on: bool


class RulesURLReq(BaseModel):
    """`url` of null/"" → clear and fall back to bundled default rules."""

    url: str | None = None


class DnsConfigReq(BaseModel):
    doh_url: str = Field(min_length=1, max_length=MAX_DOH_URL_LEN)

    @field_validator("doh_url")
    @classmethod
    def _must_be_https(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith("https://"):
            raise ValueError("DoH URL must start with https://")
        return v
