"""Top-level installer flows: install / uninstall / probe."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from installer import steps
from installer.ssh import EntwareShell, KeeneticCLI, is_entware_ready
from installer.ui import confirm, fail, info, ok, warn


def _local_yonder_dir() -> Path:
    """Path to the yonder/ package source we ship to the router."""
    # When the installer is run from a source checkout (the normal case for
    # now — no PyInstaller bundle yet), yonder/ lives next to installer/.
    here = Path(__file__).resolve().parent
    candidate = here.parent / "yonder"
    if not candidate.is_dir():
        raise FileNotFoundError(f"yonder/ source dir not found at {candidate}")
    return candidate


def _init_script_bytes() -> bytes:
    """Read the embedded S99yonder init script."""
    return resources.files("installer.resources").joinpath("S99yonder").read_bytes()


def _singbox_init_bytes() -> bytes:
    """Read the embedded S99singbox init script."""
    return resources.files("installer.resources").joinpath("S99singbox").read_bytes()


async def do_probe(host: str, user: str, password: str) -> None:
    print(f"\nProbing {user}@{host}...")
    if await is_entware_ready(host, user, password):
        ok("Entware ready (exec sh works)")
        shell = await EntwareShell.connect(host, user, password)
        try:
            _, out, _ = await shell.run(
                "uname -a; echo --; command -v python3 xkeen iptables; "
                "echo --; test -d /opt/etc/init.d && echo init.d=ok",
                check=False,
                timeout=15.0,
            )
            print(f"\n{out}\n")
        finally:
            await shell.close()
        return

    warn("Entware NOT ready (exec sh did not return our marker)")
    info("collecting diagnostic info via Keenetic CLI...")
    cli = await KeeneticCLI.connect(host, user, password)
    try:
        try:
            arch = await steps.detect_arch(cli)
            ok(f"architecture: {arch}")
        except Exception as exc:  # noqa: BLE001
            warn(str(exc))
        try:
            drives = await steps.list_usb_drives(cli)
            if not drives:
                warn("no ext4 USB drive detected")
            else:
                for d in drives:
                    ok(f"USB drive: uuid={d.uuid} fstype={d.fstype}")
        except Exception as exc:  # noqa: BLE001
            warn(str(exc))
    finally:
        await cli.close()


async def do_install(host: str, user: str, password: str) -> None:
    print(f"\n[1/4] Inspecting {user}@{host}...")
    if await is_entware_ready(host, user, password):
        ok("Entware already up")
    else:
        await _bootstrap(host, user, password)

    shell = await EntwareShell.connect(host, user, password)
    cli = await KeeneticCLI.connect(host, user, password)
    try:
        arch = await steps.detect_arch(cli)
        print("\n[3/4] Installing dependencies and deploying app...")
        await steps.install_singbox(shell, arch)
        await steps.install_geo_rulesets(shell)
        await steps.install_singbox_init(shell, _singbox_init_bytes())
        await steps.install_python(shell)
        await steps.install_pip_deps(shell)
        await steps.deploy_yonder_source(shell, _local_yonder_dir())
        await steps.install_init_script(shell, _init_script_bytes())
        await steps.open_firewall_port(shell)

        print("\n[4/4] Starting daemon...")
        await steps.start_daemon(shell)
    finally:
        await shell.close()
        await cli.close()

    print("\n  ✓ Done.")
    print(f"\n  Open http://{host}:{steps.WEB_UI_PORT}/ on any device on your LAN.\n")


async def _bootstrap(host: str, user: str, password: str) -> None:
    """One-time Entware install path. Called only when isEntwareReady is False."""
    cli = await KeeneticCLI.connect(host, user, password)
    try:
        arch = await steps.detect_arch(cli)
        ok(f"router architecture: {arch}")
        await steps.preflight_components(cli)

        drives = await steps.list_usb_drives(cli)
        if not drives:
            fail("no ext4 USB drive found. Plug one in and try again.")
        if len(drives) > 1:
            warn(f"multiple USB drives detected; using first: {drives[0].uuid}")
        drive = drives[0]
        drive_id = drive.uuid or drive.name
        if not drive_id:
            fail(f"could not determine USB drive identifier: {drive}")
        ok(f"USB drive: {drive_id}")

        steps.preflight_disk_space(drive)
        await steps.preflight_internet(cli)

        print("\n[2/4] Bootstrapping Entware on USB drive...")
        warn("This will REBOOT the router. Existing connections will drop.")
        if not confirm("Proceed?", default_yes=False):
            fail("aborted by user")

        await steps.bootstrap_entware(cli, drive_id, arch)
    finally:
        await cli.close()

    await steps.wait_for_entware(host, user, password)


async def do_uninstall(host: str, user: str, password: str) -> None:
    print(f"\nConnecting to {user}@{host}...")
    if not await is_entware_ready(host, user, password):
        fail("Entware shell not reachable — nothing to uninstall.")
    shell = await EntwareShell.connect(host, user, password)
    try:
        # Order matters:
        # 1. Stop the daemon first so its watchdog doesn't restart sing-box
        #    after we stop it.
        # 2. Stop sing-box so the tun + auto_route rules disappear and the
        #    in-memory VLESS credentials are dropped.
        # 3. Scrub config.json so leftover servers don't leak the user's
        #    VLESS UUIDs + servers.
        # 4. Remove our own files + init scripts + firewall rule.
        #
        # No Keenetic CLI needed: with the sing-box plane there's no router-side
        # DoH upstream to clean and no HTTPS-port move to revert — DNS lives
        # inside sing-box and tun needs no port freed.
        await steps.stop_daemon(shell)
        await steps.stop_singbox(shell)
        await steps.scrub_singbox_config(shell)
        await steps.remove_init_script(shell)
        await steps.remove_singbox_init(shell)
        await steps.remove_app(shell)
        await steps.close_firewall_port(shell)
        ok("uninstalled")
    finally:
        await shell.close()
    print("\n  Note: Entware itself is left in place. The sing-box binary and")
    print("  geo rule-sets remain (they survive a re-install). config.json is")
    print("  reset to safe defaults — no VLESS credentials left on disk.")
