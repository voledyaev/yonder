"""yonder installer — Mac-side tool that brings up the daemon on a Keenetic
router over SSH.

End-to-end flow on a clean router:
  1. SSH as admin (tag cli) → Keenetic structured CLI
  2. Trigger Entware bootstrap (USB ext4 + `opkg disk`), reboot
  3. Wait for /opt to mount; install XKeen + Xray
  4. Install python3 + pip deps, upload yonder/ source
  5. Install init script + env file (admin password, chmod 600)
  6. Open firewall port 8080, start daemon

Re-running the installer is safe (each step is idempotent).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from installer import flows, ui


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="yonder",
        description="Install / uninstall the yonder VPN daemon on a Keenetic router.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
        "  yonder admin@192.168.1.1\n"
        "  yonder --uninstall admin@192.168.1.1\n"
        "  yonder --probe admin@192.168.1.1",
    )
    parser.add_argument("target", help="user@host (e.g. admin@192.168.1.1)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--uninstall", action="store_true", help="uninstall instead of install")
    group.add_argument(
        "--probe",
        action="store_true",
        help="connect and report router state without making changes",
    )
    parser.add_argument(
        "--password-env", metavar="VAR", help="read password from this env var instead of prompting"
    )
    parser.add_argument(
        "-y", "--yes", action="store_true", help="auto-confirm destructive prompts (e.g. reboot)"
    )
    args = parser.parse_args()

    user, _, host = args.target.partition("@")
    if not user or not host:
        parser.error(f"target must look like user@host (got {args.target!r})")

    ui.set_auto_yes(args.yes)

    if args.password_env:
        password = os.environ.get(args.password_env, "")
        if not password:
            ui.fail(f"env var {args.password_env} is empty")
    else:
        password = ui.prompt_password(args.target)

    try:
        if args.probe:
            asyncio.run(flows.do_probe(host, user, password))
        elif args.uninstall:
            asyncio.run(flows.do_uninstall(host, user, password))
        else:
            asyncio.run(flows.do_install(host, user, password))
    except KeyboardInterrupt:
        print("\n  ! interrupted", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
