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
        print("\n[3/4] Installing dependencies and deploying app...")
        await steps.install_xkeen(shell)
        await steps.free_port_443(cli)
        await steps.install_python(shell)
        await steps.install_pip_deps(shell)
        await steps.deploy_yonder_source(shell, _local_yonder_dir())
        await steps.install_init_script(shell, _init_script_bytes())
        await steps.write_env_file(shell, password)
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
    cli: KeeneticCLI | None = None
    try:
        # Read state.json BEFORE we kill anything — we need active_url to know
        # whether to clean a DoH upstream off the router.
        active_doh_url = await steps.read_active_doh_url(shell)

        # Order matters:
        # 1. Stop daemon first so it doesn't fight back (e.g. watchdog
        #    restarting xkeen after we stop it).
        # 2. Stop xkeen so xray drops the in-memory VLESS credentials and
        #    tproxy iptables disappear before we deauth the rest.
        # 3. Clean up router-side state the daemon would have on toggle-off:
        #    DoH upstream that was set while VPN was on at uninstall time.
        # 4. Scrub xray configs so leftover 04_outbounds.json doesn't leak
        #    the user's VLESS UUID + server credentials.
        # 5. Remove our own files + firewall rule.
        await steps.stop_daemon(shell)
        await steps.stop_xkeen(shell)
        # One Keenetic CLI session covers both DoH cleanup (only if VPN was
        # on at uninstall) and the HTTPS port revert (always, if it was
        # moved). Open it lazily to avoid a needless session when neither
        # applies (e.g. xkeen never installed).
        cli = await KeeneticCLI.connect(host, user, password)
        if active_doh_url:
            await steps.clear_router_doh_upstream(cli, active_doh_url)
        await steps.restore_port_443(cli)
        await steps.scrub_xray_configs(shell)
        await steps.remove_init_script(shell)
        await steps.remove_app(shell)
        await steps.close_firewall_port(shell)
        ok("uninstalled")
    finally:
        if cli is not None:
            await cli.close()
        await shell.close()
    print("\n  Note: Entware itself is left in place. xkeen + xray binaries")
    print("  remain (they survive a re-install). xray configs are reset to")
    print("  safe defaults — no VLESS credentials left on disk.")
