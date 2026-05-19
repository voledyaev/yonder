"""Pure-function parsers for Keenetic CLI command outputs.

Split out from ssh.py so they can be unit-tested with canned text — there's
no good way to spin up a fake Keenetic structured-CLI to exercise the SSH
code path in tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ARCH_RE = re.compile(r"\barch:\s*(\S+)")


@dataclass(frozen=True)
class UsbDrive:
    name: str
    fstype: str
    uuid: str
    storage: str
    mounted: str
    free: str  # bytes, as string from CLI output
    total: str


def parse_arch(show_version_output: str) -> str:
    """Extract the `arch:` value from `show version`.

    armv7sf / armv7hf etc are normalised to "armv7".
    """
    m = _ARCH_RE.search(show_version_output)
    if not m:
        raise ValueError(f"could not parse arch from `show version`:\n{show_version_output}")
    arch = m.group(1)
    if arch.startswith("armv7"):
        return "armv7"
    return arch


def parse_installed_components(show_version_output: str) -> set[str]:
    """Parse the `components: a, b, c, ...` block from `show version`.

    The components list spans multiple indented continuation lines (16+ leading
    spaces).
    """
    parts: list[str] = []
    in_section = False
    for line in show_version_output.splitlines():
        stripped = line.lstrip(" \t")
        if stripped.startswith("components:"):
            in_section = True
            parts.append(stripped[len("components:") :])
            continue
        if in_section:
            if line.startswith(" " * 16):
                parts.append(stripped)
            else:
                break
    joined = " ".join(parts)
    return {c.strip() for c in joined.split(",") if c.strip()}


_LS_BLOCK_RE = re.compile(r"\n\s*entry,\s*type\s*=")
_LS_FIELD_KEYS = ("name", "fstype", "uuid", "storage", "mounted", "free", "total")


def parse_usb_drives(ls_output: str) -> list[UsbDrive]:
    """Parse top-level `ls` for ext-filesystem USB drives.

    Returns only entries that are storage=usb, fstype=ext*, mounted=yes —
    matching the criteria the installer uses to pick a target for Entware.
    """
    drives: list[UsbDrive] = []
    for block in _LS_BLOCK_RE.split(ls_output):
        fields: dict[str, str] = {}
        for key in _LS_FIELD_KEYS:
            # Require a non-whitespace value on the same line — otherwise
            # a regex that lets `^\s*key:\s*$` match an empty line would
            # pull in a later field's value as the match.
            field_re = re.compile(rf"(?m)^\s*{re.escape(key)}:\s+(\S(?:.*\S)?)\s*$")
            m = field_re.search(block)
            if not m:
                continue
            val = m.group(1).strip()
            if key in ("name", "uuid"):
                val = val.rstrip(":")
            fields[key] = val
        if (
            fields.get("storage") == "usb"
            and fields.get("fstype", "").startswith("ext")
            and fields.get("mounted") == "yes"
        ):
            drives.append(
                UsbDrive(
                    name=fields.get("name", ""),
                    fstype=fields.get("fstype", ""),
                    uuid=fields.get("uuid", ""),
                    storage=fields.get("storage", ""),
                    mounted=fields.get("mounted", ""),
                    free=fields.get("free", ""),
                    total=fields.get("total", ""),
                )
            )
    return drives


_PING_OK_RE = re.compile(r"\b0%\s+packet\s+loss\b")


def ping_succeeded(tools_ping_output: str) -> bool:
    """Return True iff Keenetic's `tools ping` reports any received packets
    AND zero loss.

    The `\\b` boundary on `0%` keeps us from matching "100% packet loss".
    """
    return "received" in tools_ping_output and bool(_PING_OK_RE.search(tools_ping_output))


def doh_upstream_present(show_running_config: str, doh_url: str) -> bool:
    """True iff `dns-proxy / https upstream <doh_url>` appears in running-config."""
    needle = f"https upstream {doh_url}"
    for line in show_running_config.splitlines():
        if line.strip().startswith(needle):
            return True
    return False
