"""Install / uninstall steps for the yonder daemon.

Each function is one logical operation against the router. They're called
from flows.py in a fixed order; calling out of order is undefined.

The data plane is sing-box: the installer downloads the sing-box binary + RU
geo rule-sets and registers an init script; yonder generates config.json and
drives it via the Clash API at runtime. DNS/DoH lives inside sing-box, so the
installer needs no router-side DoH setup (and the daemon needs no router
credentials). The xkeen install/uninstall steps remain below for rollback
during the migration and are scheduled for removal.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path

from yonder.xray import OUTBOUNDS_FILE, ROUTING_FILE, write_xkeen_split

from installer.parsers import (
    UsbDrive,
    parse_arch,
    parse_installed_components,
    parse_usb_drives,
    ping_succeeded,
)
from installer.ssh import EntwareShell, KeeneticCLI, SSHError, is_entware_ready, wait_for_ssh_up
from installer.ui import fail, info, ok, warn

# --- Constants ------------------------------------------------------------

REMOTE_BASE = "/opt/yonder"
REMOTE_DATA = "/opt/yonder/data"
REMOTE_LIB = "/opt/yonder/lib"
REMOTE_INIT_SCRIPT = "/opt/etc/init.d/S99yonder"
REMOTE_ENV_FILE = "/opt/yonder/yonder.env"
WEB_UI_PORT = 8080
REBOOT_WAIT_TIMEOUT_S = 240.0
REBOOT_POLL_INTERVAL_S = 5.0

GEOIP_URL = "https://github.com/v2fly/geoip/releases/latest/download/geoip.dat"
GEOSITE_URL = "https://github.com/v2fly/domain-list-community/releases/latest/download/dlc.dat"
GEO_DAT_DIR = "/opt/etc/xray/dat"

ENTWARE_INSTALLERS = {
    "aarch64": "https://bin.entware.net/aarch64-k3.10/installer/EN_aarch64-installer.tar.gz",
    "mipsel": "https://bin.entware.net/mipselsf-k3.4/installer/EN_mipsel-installer.tar.gz",
    "armv7": "https://bin.entware.net/armv7sf-k3.2/installer/EN_armv7-installer.tar.gz",
}

# Piped to stdin during `xkeen -i`. We bypass the upstream install.sh — as
# of 2026 it's a 122-line wrapper that just downloads + extracts the tarball
# behind a Stable/Beta prompt, then exits without running `xkeen -i` itself.
# We do the download/extract directly (one `run()` call) so this constant
# only answers `xkeen -i`'s prompts:
#
#   1 → choice_add_proxy_cores: Xray only (vs Mihomo / both / skip)
#   1 → download_xray: pick the latest Xray release (item #1)
#   0 → choice_geosite: skip GeoSite download (we install our own)
#   0 → choice_geoip: skip GeoIP download (same)
#   1 → autostart prompt: register S99xkeen for boot-time start
#   (extra zeros guard against future-added prompts)
XKEEN_INSTALL_ANSWERS = "1\n1\n0\n0\n1\n0\n0\n0\n"

XKEEN_TARBALL_URL = "https://github.com/jameszeroX/XKeen/releases/latest/download/xkeen.tar.gz"

REQUIRED_COMPONENTS = {
    "opkg",  # OPKG package manager
    "ext",  # ext2/3/4 filesystem support
    "opkg-kmod-netfilter",  # iptables modules (tun/auto_route still uses them)
    "opkg-kmod-netfilter-addons",
    # No "dns-https": the sing-box plane does DNS/DoH itself, not via the router.
}

MIN_USB_FREE_MB = 200

# Python deps the daemon needs on the router. Pinned to majors known to work.
PIP_PACKAGES = (
    "fastapi>=0.110,<1",
    "uvicorn>=0.27,<1",
    "httpx>=0.27,<1",
    "pydantic>=2,<3",
)

# --- sing-box data plane --------------------------------------------------
#
# Pinned for reproducibility (asset names embed the version). musl builds are
# mandatory: Entware is musl-based and the plain/glibc sing-box binary fails
# to exec (missing ELF interpreter).
SINGBOX_VERSION = "1.13.12"
# Keenetic arch (from detect_arch) → sing-box release GOARCH.
SINGBOX_GOARCH = {"aarch64": "arm64", "armv7": "armv7", "mipsel": "mipsle"}
SINGBOX_URL_TMPL = (
    "https://github.com/SagerNet/sing-box/releases/download/"
    "v{ver}/sing-box-{ver}-linux-{goarch}-musl.tar.gz"
)
SINGBOX_BIN = "/opt/sbin/sing-box"
SINGBOX_DIR = "/opt/etc/sing-box"
SINGBOX_CONFIG = "/opt/etc/sing-box/config.json"
SINGBOX_INIT = "/opt/etc/init.d/S99singbox"
# RU geo rule-sets (sing-box .srs) from the rule-set branches — the analogue
# of the v2fly geoip.dat/geosite.dat, but native to sing-box.
SINGBOX_GEOIP_URL = "https://raw.githubusercontent.com/SagerNet/sing-geoip/rule-set/geoip-ru.srs"
SINGBOX_GEOSITE_URL = (
    "https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-category-ru.srs"
)


# --- Pre-flight ----------------------------------------------------------


async def preflight_components(cli: KeeneticCLI) -> None:
    out = await cli.cmd("show version", timeout=30.0)
    have = parse_installed_components(out)
    missing = REQUIRED_COMPONENTS - have
    if missing:
        fail(
            "missing required Keenetic firmware components: "
            + ", ".join(sorted(missing))
            + "\n  Open the Keenetic web UI → System → Components and install "
            "them, then re-run."
        )
    ok(f"firmware components: all required present ({len(REQUIRED_COMPONENTS)})")


async def preflight_internet(cli: KeeneticCLI) -> None:
    out = await cli.cmd("tools ping bin.entware.net count 1", timeout=30.0)
    if ping_succeeded(out):
        ok("router can reach bin.entware.net")
        return
    fail(
        "router cannot reach bin.entware.net (used to download Entware).\n"
        "  Check the router's WAN connection. If it's working in general but "
        "DNS fails, also check that DNS is configured."
    )


def preflight_disk_space(drive: UsbDrive) -> None:
    try:
        free_bytes = int(drive.free)
    except ValueError:
        warn(f"could not parse free space from drive entry: {drive}")
        return
    free_mb = free_bytes // (1024 * 1024)
    if free_mb < MIN_USB_FREE_MB:
        fail(
            f"USB drive has only {free_mb} MB free; need at least "
            f"{MIN_USB_FREE_MB} MB for Entware + yonderd + Xray + headroom."
        )
    ok(f"USB drive free: {free_mb} MB")


async def detect_arch(cli: KeeneticCLI) -> str:
    out = await cli.cmd("show version", timeout=30.0)
    return parse_arch(out)


async def list_usb_drives(cli: KeeneticCLI) -> list[UsbDrive]:
    out = await cli.cmd("ls", timeout=30.0)
    return parse_usb_drives(out)


# --- Entware bootstrap ----------------------------------------------------


async def bootstrap_entware(cli: KeeneticCLI, drive_id: str, arch: str) -> None:
    url = ENTWARE_INSTALLERS.get(arch)
    if not url:
        raise SSHError(f"no Entware installer URL known for arch={arch!r}")
    info(f"triggering Entware download from {url}")
    out = await cli.cmd(f"opkg disk {drive_id}:/ {url}", timeout=180.0)
    if "Disk is unchanged" in out:
        # Factory reset can preserve the opkg-disk UUID setting without
        # actually having Entware on the drive. Clear and retry.
        info("opkg disk already set (stale) — clearing and retrying")
        await cli.cmd("no opkg disk", timeout=30.0)
        out = await cli.cmd(f"opkg disk {drive_id}:/ {url}", timeout=180.0)
    if "Disk is set to" not in out:
        warn(f"unexpected `opkg disk` response:\n{out.strip()}")
    info("saving running configuration")
    await cli.cmd("system configuration save", timeout=60.0)
    info("rebooting router (~3 minutes for /opt to mount and Entware to come up)")
    # Best-effort: the SSH session will die mid-write. We catch errors.
    try:
        await cli.cmd("system reboot", timeout=5.0)
    except SSHError:
        pass


async def wait_for_entware(host: str, user: str, password: str) -> None:
    info(f"waiting for SSH back up (up to {int(REBOOT_WAIT_TIMEOUT_S)}s)")
    await asyncio.sleep(10.0)  # let it actually go down
    await wait_for_ssh_up(host, REBOOT_WAIT_TIMEOUT_S, poll_s=REBOOT_POLL_INTERVAL_S)
    info("waiting for /opt to mount and Entware to be reachable")
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 120.0
    while loop.time() < deadline:
        if await is_entware_ready(host, user, password):
            ok("Entware ready")
            return
        await asyncio.sleep(5.0)
    fail(
        "router rebooted but Entware shell never became reachable (waited 120s). "
        "The USB drive may not have a working Entware installation yet. "
        "Re-running the installer will retry the bootstrap."
    )


# --- XKeen + xray ---------------------------------------------------------


async def xkeen_fully_installed(shell: EntwareShell) -> bool:
    rc, _, _ = await shell.run(
        "test -x /opt/sbin/xkeen && test -x /opt/sbin/xray && test -f /opt/etc/init.d/S99xkeen",
        check=False,
        timeout=10.0,
    )
    return rc == 0


async def install_xkeen(shell: EntwareShell) -> None:
    if await xkeen_fully_installed(shell):
        ok("XKeen + Xray already installed")
        await _ensure_xkeen_runtime_deps(shell)
        await _ensure_geo_dats(shell)
        return

    info("cleaning up any previously-failed XKeen install fragments")
    await shell.run(
        "rm -rf /opt/sbin/xkeen /opt/sbin/_xkeen /opt/sbin/.xkeen "
        "/tmp/xkeen.tar.gz /tmp/xray.tar.gz /opt/etc/init.d/S99xkeen",
        check=False,
        timeout=30.0,
    )

    info("installing curl + tar + findutils (XKeen needs them)")
    await shell.run("opkg install curl tar findutils", check=True, timeout=180.0)

    info(f"downloading + extracting XKeen tarball from {XKEEN_TARBALL_URL}")
    await shell.run(
        "cd /tmp && "
        f"curl -fLo xkeen.tar.gz --connect-timeout 15 -m 120 {XKEEN_TARBALL_URL} 2>&1 | tail -3 && "
        "tar -xzf xkeen.tar.gz -C /opt/sbin && "
        "rm -f xkeen.tar.gz && "
        "test -x /opt/sbin/xkeen",
        check=True,
        timeout=300.0,
    )

    info("running `xkeen -i` to download Xray and register init script (~30 MB, 1-3 min)")
    # printf '...\n...\n' interprets backslash-n as newlines, feeding xkeen -i
    # the canned multi-prompt answers we want.
    rc, out, _ = await shell.run(
        f"printf '{XKEEN_INSTALL_ANSWERS}' | /opt/sbin/xkeen -i 2>&1",
        check=False,
        timeout=600.0,
    )
    # Echo the tail of the output so the user sees the verification messages
    # ("Установка XKeen выполнена!") rather than just our own checkpoint.
    for line in out.splitlines()[-15:]:
        line = line.rstrip()
        if line:
            print(f"      | {line}")

    if not await xkeen_fully_installed(shell):
        fail(
            f"XKeen install verification failed (xkeen -i exited rc={rc}): missing "
            "one of /opt/sbin/xkeen, /opt/sbin/xray, /opt/etc/init.d/S99xkeen.\n"
            "  Inspect the output above. Common causes:\n"
            "    - GitHub unreachable from the router (try again later)\n"
            "    - prompts have changed in a new XKeen release "
            "(file an issue with the output above)"
        )
    ok("XKeen + Xray installed")
    await _ensure_xkeen_runtime_deps(shell)
    await _ensure_geo_dats(shell)


async def _ensure_xkeen_runtime_deps(shell: EntwareShell) -> None:
    """Install/create things XKeen needs at runtime but doesn't bring itself.

    See the matching Go function in steps.go for the rationale behind each
    item (findutils for `find`, /opt/etc/ndm/* for hook scripts, /opt/etc/
    {passwd,group,shadow} for `adduser`).
    """
    rc, _, _ = await shell.run("test -x /opt/bin/find", check=False, timeout=5.0)
    if rc != 0:
        info("installing findutils (XKeen's S99xkeen needs `find`)")
        await shell.run("opkg install findutils", check=True, timeout=120.0)
    info("ensuring /opt/etc/ndm hook directories exist")
    await shell.run(
        "mkdir -p /opt/etc/ndm/netfilter.d /opt/etc/ndm/ifstatechanged.d /opt/etc/ndm/fs.d",
        check=True,
        timeout=10.0,
    )
    rc, _, _ = await shell.run("test -f /opt/etc/passwd", check=False, timeout=5.0)
    if rc != 0:
        info("seeding /opt/etc/{passwd,group,shadow} for adduser")
        await shell.run(
            r"printf 'root:x:0:0:root:/opt/root:/opt/bin/sh\n' > /opt/etc/passwd && "
            r"printf 'root:x:0:\n' > /opt/etc/group && "
            "touch /opt/etc/shadow && chmod 600 /opt/etc/shadow",
            check=True,
            timeout=10.0,
        )


async def _ensure_geo_dats(shell: EntwareShell) -> None:
    """Download v2fly's geoip.dat + geosite.dat into xray's data dir.

    User-supplied rule sets routinely reference `geoip:cn` / `geosite:google`
    and xray refuses to start if the .dat file is missing.
    """
    rc, _, _ = await shell.run(
        f"test -s {GEO_DAT_DIR}/geoip.dat && test -s {GEO_DAT_DIR}/geosite.dat",
        check=False,
        timeout=5.0,
    )
    if rc == 0:
        ok("geoip.dat + geosite.dat already present")
        return
    info("downloading geoip.dat + geosite.dat (~10 MB) from v2fly releases")
    await shell.run(f"mkdir -p {GEO_DAT_DIR}", check=True, timeout=10.0)
    for url, dest in [
        (GEOIP_URL, f"{GEO_DAT_DIR}/geoip.dat"),
        (GEOSITE_URL, f"{GEO_DAT_DIR}/geosite.dat"),
    ]:
        await shell.run(
            f"curl -fL --connect-timeout 15 -m 120 -o {dest} {url}",
            check=True,
            timeout=180.0,
        )
    ok("geofiles installed")


async def install_singbox(shell: EntwareShell, arch: str) -> None:
    """Download the pinned sing-box (musl) binary for `arch` → /opt/sbin/sing-box.

    Idempotent: if the right version is already installed, does nothing.
    """
    rc, out, _ = await shell.run(
        f"{SINGBOX_BIN} version 2>/dev/null | head -1", check=False, timeout=10.0
    )
    if rc == 0 and SINGBOX_VERSION in out:
        ok(f"sing-box {SINGBOX_VERSION} already installed")
        return

    goarch = SINGBOX_GOARCH.get(arch)
    if goarch is None:
        fail(f"no sing-box build mapping for arch {arch!r} (supported: {sorted(SINGBOX_GOARCH)})")
    url = SINGBOX_URL_TMPL.format(ver=SINGBOX_VERSION, goarch=goarch)

    info("installing curl + tar (sing-box download needs them)")
    await shell.run("opkg install curl tar", check=True, timeout=180.0)
    info(f"downloading sing-box {SINGBOX_VERSION} ({goarch}-musl, ~22 MB)")
    await shell.run(
        "cd /tmp && rm -rf sb_dl && mkdir sb_dl && cd sb_dl && "
        f"curl -fL --connect-timeout 15 -m 180 -o sb.tar.gz {url} && "
        "tar -xzf sb.tar.gz && "
        f'BIN=$(find . -name sing-box -type f | head -1) && cp "$BIN" {SINGBOX_BIN} && '
        f"chmod +x {SINGBOX_BIN} && cd /tmp && rm -rf sb_dl",
        check=True,
        timeout=240.0,
    )
    rc, out, _ = await shell.run(f"{SINGBOX_BIN} version 2>&1 | head -1", check=False, timeout=10.0)
    if rc != 0 or SINGBOX_VERSION not in out:
        fail(
            f"sing-box install verification failed: {out.strip() or '(no output)'}.\n"
            "  Common causes: GitHub unreachable, or wrong arch/libc build."
        )
    ok(f"sing-box installed ({out.strip()})")


async def install_geo_rulesets(shell: EntwareShell) -> None:
    """Download the RU geo rule-sets (.srs) sing-box's routing references.

    Idempotent: skips files already present and non-empty.
    """
    await shell.run(f"mkdir -p {SINGBOX_DIR}", check=True, timeout=10.0)
    targets = [
        (SINGBOX_GEOIP_URL, f"{SINGBOX_DIR}/geoip-ru.srs"),
        (SINGBOX_GEOSITE_URL, f"{SINGBOX_DIR}/geosite-ru.srs"),
    ]
    rc, _, _ = await shell.run(
        " && ".join(f"test -s {dest}" for _, dest in targets), check=False, timeout=5.0
    )
    if rc == 0:
        ok("sing-box geo rule-sets already present")
        return
    info("downloading sing-box RU geo rule-sets (.srs)")
    for url, dest in targets:
        await shell.run(
            f"curl -fL --connect-timeout 15 -m 120 -o {dest} {url} && test -s {dest}",
            check=True,
            timeout=180.0,
        )
    ok("sing-box geo rule-sets installed")


async def install_singbox_init(shell: EntwareShell, init_script_bytes: bytes) -> None:
    info(f"installing sing-box init script → {SINGBOX_INIT}")
    await shell.run("mkdir -p /opt/etc/init.d", check=True, timeout=10.0)
    await shell.upload_bytes(init_script_bytes, SINGBOX_INIT, mode=0o755)
    ok("sing-box init script installed")


async def free_port_443(cli: KeeneticCLI) -> None:
    """Move Keenetic's HTTPS admin from 443 to 8443 so xkeen's tproxy doesn't
    conflict. After this `https://router/` becomes `https://router:8443/`.
    """
    out = await cli.cmd("show running-config", timeout=30.0)
    if "ip http ssl port 8443" in out:
        ok("Keenetic HTTPS admin already on port 8443")
        return
    if "ip http ssl port 443" not in out and "ip http ssl enable" not in out:
        # SSL admin not enabled at all — nothing to free.
        return
    info("moving Keenetic HTTPS admin from 443 to 8443 (avoids VPN tproxy conflict)")
    await cli.cmd("ip http ssl port 8443", timeout=15.0)
    await cli.cmd("system configuration save", timeout=30.0)
    ok("HTTPS admin → 8443 (use https://<router>:8443/ from now on)")


async def restore_port_443(cli: KeeneticCLI) -> None:
    """Revert the 443→8443 move on uninstall.

    Mirrored against `free_port_443` so the router ends up as it was before
    `yonder` ever touched its admin config. Saves running-config to flash
    (one-time flash write per uninstall — acceptable).
    """
    out = await cli.cmd("show running-config", timeout=30.0)
    if "ip http ssl port 8443" not in out:
        # Either not enabled or already on 443. Either way: nothing to undo.
        return
    info("restoring Keenetic HTTPS admin to port 443")
    await cli.cmd("ip http ssl port 443", timeout=15.0)
    await cli.cmd("system configuration save", timeout=30.0)


# --- Python deps + yonderd deploy ----------------------------------------


async def install_python(shell: EntwareShell) -> None:
    """Ensure python3 + pip are installed."""
    rc, _, _ = await shell.run("test -x /opt/bin/python3", check=False, timeout=5.0)
    if rc != 0:
        info("installing python3 + python3-pip (~10 MB)")
        await shell.run("opkg install python3 python3-pip", check=True, timeout=180.0)
    ok("python3 installed")


async def install_pip_deps(shell: EntwareShell) -> None:
    """Install yonder's pip dependencies into /opt/yonder/lib (target dir).

    Avoids the system site-packages and removes the need for a venv. The init
    script puts /opt/yonder/lib on PYTHONPATH.
    """
    info(f"installing pip deps into {REMOTE_LIB} (~25 MB, may take 1-3 min)")
    await shell.run(f"mkdir -p {REMOTE_LIB}", check=True, timeout=10.0)
    packages = " ".join(f"'{p}'" for p in PIP_PACKAGES)
    # --no-cache-dir keeps the USB drive from filling up with pip's HTTP cache.
    cmd = f"pip3 install --no-cache-dir --target={REMOTE_LIB} {packages}"
    await shell.run(cmd, check=True, timeout=600.0)
    ok("pip deps installed")


async def deploy_yonder_source(shell: EntwareShell, local_yonder_dir: Path | str) -> None:
    info(f"uploading yonder/ → {REMOTE_BASE}/yonder")
    # Stop the daemon first so we can overwrite running files (Linux ETXTBSY
    # is harder to hit with .py than with a binary, but be defensive).
    await shell.run(
        f"test -x {REMOTE_INIT_SCRIPT} && {REMOTE_INIT_SCRIPT} stop || true",
        check=False,
        timeout=15.0,
    )
    await shell.upload_directory(local_yonder_dir, f"{REMOTE_BASE}/yonder")
    await shell.run(f"mkdir -p {REMOTE_DATA}", check=True, timeout=10.0)
    ok("yonder source uploaded")


async def install_init_script(shell: EntwareShell, init_script_bytes: bytes) -> None:
    info(f"installing init script → {REMOTE_INIT_SCRIPT}")
    await shell.run("mkdir -p /opt/etc/init.d", check=True, timeout=10.0)
    await shell.upload_bytes(init_script_bytes, REMOTE_INIT_SCRIPT, mode=0o755)
    ok("init script installed")


# Allowed characters in the value half of an env-file line. We require the
# password to be sh-safe so we can write a simple `KEY=VAL\n` line without
# quoting heroics (and so a future installer-of-the-installer can read it
# back).
_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_/.@:+=-]+$")


async def write_env_file(shell: EntwareShell, password: str) -> None:
    """Write `/opt/yonder/yonder.env` (chmod 600) with the admin password.

    The init script sources this file to get YONDER_KEENETIC_PW into yonderd's
    environment without ever putting it in `ps -ef`.
    """
    if not _ENV_VALUE_RE.match(password):
        fail(
            "admin password contains characters that cannot be safely written "
            "to a shell env file (allowed: alnum and `_/.@:+=-`).\n"
            "  Either change the password to use only safe characters or open "
            "an issue."
        )
    # `export` is load-bearing: the init script sources this file with `.`,
    # which would only set the variable in the init shell — child python
    # processes wouldn't inherit it without an explicit export.
    contents = (
        "# yonder daemon environment — sourced by /opt/etc/init.d/S99yonder\n"
        f"export YONDER_KEENETIC_PW={password}\n"
    )
    await shell.upload_bytes(contents.encode(), REMOTE_ENV_FILE, mode=0o600)
    ok("admin credentials saved (chmod 600)")


async def open_firewall_port(shell: EntwareShell) -> None:
    info(f"opening LAN-side TCP port {WEB_UI_PORT}")
    rule = f"-p tcp --dport {WEB_UI_PORT} -m comment --comment 'yonder' -j ACCEPT"
    # Idempotent: try to delete first (silently ignored if absent), then insert.
    await shell.run(
        f"iptables -D INPUT {rule} 2>/dev/null; iptables -I INPUT {rule}",
        check=False,
        timeout=10.0,
    )
    ok("firewall rule added")


async def start_daemon(shell: EntwareShell) -> None:
    info("starting yonder daemon")
    await shell.run(f"{REMOTE_INIT_SCRIPT} restart", check=False, timeout=20.0)
    # Python + FastAPI + uvicorn takes ~5s to import-and-bind on aarch64
    # Entware. Poll up to 15s before declaring it dead — the early checks
    # would otherwise false-negative even when the daemon is healthy.
    check_cmd = (
        f"netstat -lnt 2>/dev/null | grep ':{WEB_UI_PORT} ' || "
        f"ss -lnt 2>/dev/null | grep ':{WEB_UI_PORT} ' || true"
    )
    for _ in range(15):
        await asyncio.sleep(1.0)
        rc, out, _ = await shell.run(check_cmd, check=False, timeout=10.0)
        if out.strip():
            ok(f"daemon listening: {out.strip()}")
            return
    warn(f"daemon does not appear to be listening on port {WEB_UI_PORT}")
    warn(
        f"check the log: ssh {shell.user}@{shell.host} "
        f"'exec sh -c \"cat {REMOTE_DATA}/yonderd.log\"'"
    )


# --- Uninstall ------------------------------------------------------------


async def stop_daemon(shell: EntwareShell) -> None:
    info("stopping daemon")
    await shell.run(
        f"test -x {REMOTE_INIT_SCRIPT} && {REMOTE_INIT_SCRIPT} stop || true",
        check=False,
        timeout=15.0,
    )


async def read_active_doh_url(shell: EntwareShell) -> str | None:
    """Best-effort: read state.json to find which DoH URL yonder pushed to the
    router. Returns None if state.json is missing/unreadable, daemon never
    enabled DoH, or VPN was already off when uninstall started.
    """
    rc, out, _ = await shell.run(
        f"cat {REMOTE_DATA}/state.json 2>/dev/null", check=False, timeout=10.0
    )
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None
    return ((data.get("dns") or {}).get("active_url")) or None


async def stop_xkeen(shell: EntwareShell) -> None:
    """Cleanly stop xkeen (which kills xray + removes iptables tproxy rules).

    Without this, a daemon-killed uninstall leaves xray running with the
    user's VLESS credentials still in process memory and tproxy active.
    """
    rc, _, _ = await shell.run("test -x /opt/sbin/xkeen", check=False, timeout=5.0)
    if rc != 0:
        return  # xkeen not installed; nothing to stop
    info("stopping xkeen + xray (so credentials stop serving traffic)")
    await shell.run("/opt/sbin/xkeen -stop", check=False, timeout=30.0)


async def stop_singbox(shell: EntwareShell) -> None:
    """Stop sing-box (drops the tun + auto_route rules and the in-memory VLESS
    credentials). No-op if its init script isn't present."""
    rc, _, _ = await shell.run(f"test -x {SINGBOX_INIT}", check=False, timeout=5.0)
    if rc != 0:
        return
    info("stopping sing-box (so credentials stop serving traffic)")
    await shell.run(f"{SINGBOX_INIT} stop", check=False, timeout=30.0)


async def scrub_singbox_config(shell: EntwareShell) -> None:
    """Overwrite the sing-box config with a credential-free one.

    Without this, uninstall would leave the user's VLESS UUIDs + servers in
    config.json. Reuses yonder's own generator with empty state → a config
    with no vless outbounds (selector points only at `direct`).
    """
    rc, _, _ = await shell.run(f"test -f {SINGBOX_CONFIG}", check=False, timeout=5.0)
    if rc != 0:
        return  # never installed; nothing to scrub
    info("scrubbing sing-box config (removing VLESS credentials)")
    from yonder.singbox.config import build_config
    from yonder.singbox.service import write_config
    from yonder.state import Data

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "config.json"
        write_config(build_config(Data()), local)
        await shell.upload_bytes(local.read_bytes(), SINGBOX_CONFIG, mode=0o600)


async def remove_singbox_init(shell: EntwareShell) -> None:
    info("removing sing-box init script")
    await shell.run(f"rm -f {SINGBOX_INIT}", check=False, timeout=10.0)


async def clear_router_doh_upstream(cli: KeeneticCLI, url: str) -> None:
    """Remove a specific DoH URL from Keenetic's dns-proxy via CLI.

    The runtime daemon normally does this on VPN-off, but uninstall kills
    the daemon directly. If VPN was on at uninstall time, this is the only
    way to take our DoH upstream off the router.

    We do NOT `system configuration save` — the upstream goes away on reboot
    anyway, and saving every install/uninstall cycle wears out flash for
    no real benefit.
    """
    info(f"removing DoH upstream from router: {url}")
    try:
        await cli.cmd("dns-proxy", timeout=10.0)
        await cli.cmd(f"no https upstream {url}", timeout=10.0)
        await cli.cmd("exit", timeout=10.0)
    except SSHError as exc:
        warn(f"could not remove DoH upstream (continuing): {exc}")


async def scrub_xray_configs(shell: EntwareShell) -> None:
    """Replace /opt/etc/xray/configs/04_outbounds.json + 05_routing.json
    with safe defaults — no VLESS credentials, just direct+block outbounds
    and RFC1918-only direct rules.

    Without this, uninstall leaves the user's last-active VLESS UUID + server
    on disk indefinitely. Reuses yonder's own no-server config generator so
    the safe-default shape stays consistent.
    """
    configs_dir = "/opt/etc/xray/configs"
    rc, _, _ = await shell.run(f"test -d {configs_dir}", check=False, timeout=5.0)
    if rc != 0:
        return  # xkeen never installed; nothing to scrub
    info("scrubbing xray configs (removing VLESS credentials)")
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp)
        write_xkeen_split(srv=None, rules=None, configs_dir=local)
        for name in (OUTBOUNDS_FILE, ROUTING_FILE):
            await shell.upload_bytes(
                (local / name).read_bytes(),
                f"{configs_dir}/{name}",
                mode=0o644,
            )


async def remove_init_script(shell: EntwareShell) -> None:
    info("removing init script")
    await shell.run(f"rm -f {REMOTE_INIT_SCRIPT}", check=False, timeout=10.0)


async def remove_app(shell: EntwareShell) -> None:
    info(f"removing {REMOTE_BASE}")
    await shell.run(f"rm -rf {REMOTE_BASE}", check=False, timeout=10.0)


async def close_firewall_port(shell: EntwareShell) -> None:
    info("removing firewall rule")
    rule = f"-p tcp --dport {WEB_UI_PORT} -m comment --comment 'yonder' -j ACCEPT"
    await shell.run(f"iptables -D INPUT {rule} 2>/dev/null; true", check=False, timeout=10.0)
