"""CLI output + interactive prompts for the installer."""

from __future__ import annotations

import getpass
import sys

# Global toggle for `--yes` mode.
_auto_yes = False


def set_auto_yes(value: bool) -> None:
    global _auto_yes
    _auto_yes = value


def info(msg: str) -> None:
    print(f"  • {msg}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def warn(msg: str) -> None:
    print(f"  ! {msg}")


def fail(msg: str) -> None:
    """Print and exit non-zero. Use for unrecoverable install errors."""
    print(f"\n  ✗ {msg}\n", file=sys.stderr)
    sys.exit(1)


def confirm(prompt: str, default_yes: bool = False) -> bool:
    """Y/N prompt. Honors --yes by auto-confirming."""
    if _auto_yes:
        print(f"\n  {prompt} [auto: yes]")
        return True
    suffix = " [Y/n] " if default_yes else " [y/N] "
    raw = input(f"\n  {prompt}{suffix}").strip().lower()
    if not raw:
        return default_yes
    return raw in ("y", "yes")


def prompt_password(target: str) -> str:
    pw = getpass.getpass(f"SSH password for {target}: ").strip()
    if not pw:
        fail("password is empty")
    return pw
