# yonder

A self-hosted web UI on a Keenetic router that turns a VLESS subscription into a transparent VPN for every device on the LAN. Pick a country, flip a switch, all your phones / laptops / TVs go through the chosen exit point — no per-device clients.

> **Tested on:** Keenetic Giga (KN-1012), KeeneticOS 5.0.11, aarch64. Should work on any modern aarch64 Keenetic with USB and the OPKG component (see [Compatibility](#compatibility) for caveats). Older mipsel / armv7 routers are out of scope today but tracked in [ARCHITECTURE.md](./ARCHITECTURE.md).

---

## Quick start

The whole flow is **3 steps** and ~10 minutes wall-clock — most of it the router rebooting once during Entware bootstrap and pip pulling FastAPI + uvicorn + httpx + pydantic on a router-grade CPU.

### Step 1 — Prepare the router (one-time, manual)

Things that can't be automated remotely — they're either physical or live in Keenetic's authenticated web UI:

1. **Plug in a USB drive** formatted as **ext4**, with at least **200 MB free**. The installer will refuse a non-ext4 drive.
2. **Open the Keenetic web UI** (`http://192.168.1.1`) and install these firmware components under System → Components:
   - **Open Packages support** (`opkg`)
   - **Ext file system** (`ext`)
   - **Netfilter modules** (`opkg-kmod-netfilter`, `opkg-kmod-netfilter-addons`)
   - **DNS-over-HTTPS** (`dns-https`) — required so yonder can toggle the router's built-in DoH upstream at runtime when you flip VPN on/off
3. **Enable SSH** — two things, both needed:
   - **Start the SSH server.** It's off by default after factory reset. In the web UI search bar (top of the admin page) search for "SSH" and toggle it on, **or** open `http://192.168.1.1/webcli/parse` and run `service ssh` followed by `system configuration save`. Verify with `nc -zv 192.168.1.1 22` — should print `succeeded`.
   - **Give `admin` CLI access.** System → Users → admin → set a strong password and check the **CLI access** label (it's on by default but factory reset sometimes drops it).

That's it for manual setup. The installer's pre-flight check verifies all of this and fails with a specific message if anything is missing. (See [docs/keenetic-notes.md § Enabling SSH](./docs/keenetic-notes.md) for the gotcha that `ip ssh` doesn't do what its name suggests.)

### Step 2 — Run the installer

From a Mac (Apple Silicon — M1/M2/M3/M4) or any machine with Python 3.11+ on the same LAN as the router:

```sh
git clone https://github.com/voledyaev/yonder.git
cd yonder
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[installer]"
yonder admin@192.168.1.1
```

The installer asks for the SSH password once and uses it both for the SSH session AND as the credential the runtime daemon will use to talk to Keenetic's RCI for DoH-toggle. (See [Notes & gotchas](#notes--gotchas) below for how the password is stored on the router.)

The first run takes ~10 minutes because it bootstraps Entware (downloads ~3 MB, reboots the router once, ~3 minutes downtime) and pip-installs the daemon's deps on the router. Subsequent runs (e.g. after pulling a new version) take ~30 seconds — they only redeploy the `yonder/` Python source.

What happens, in order:

| | Step | Notes |
|---|---|---|
| 1 | Pre-flight checks | Firmware components present, USB ext4 free space, router has internet. |
| 2 | Entware bootstrap | One-time. Sets `opkg disk <UUID>:/`, downloads the Entware tarball, reboots the router. |
| 3 | XKeen + Xray | Downloads `xkeen.tar.gz` from `jameszeroX/XKeen` releases, extracts to `/opt/sbin/`, then runs `xkeen -i` with canned stdin answers (proxy=Xray only, skip geo files, autostart=yes). ~30 MB Xray binary download. Also seeds `/opt/etc/{passwd,group,shadow}` and `/opt/etc/ndm/*` hook dirs that XKeen's S99xkeen needs at start. Drops `geoip.dat` + `geosite.dat` from v2fly releases into `/opt/etc/xray/dat/` (xray refuses to start if a rule references `geoip:foo` and the file is missing). |
| 4 | Move HTTPS admin to :8443 | `xkeen` proxies all ports by default; admin on :443 conflicts. After install, web UI lives at **`https://<router>:8443/`**. HTTP on :80 is unchanged. `system configuration save`d to flash, so it persists. |
| 5 | Install python3 + pip deps | `opkg install python3 python3-pip`, then `pip install --target=/opt/yonder/lib fastapi uvicorn httpx pydantic`. ~25 MB into `/opt/yonder/lib/`. |
| 6 | Deploy yonder source | Pushes the `yonder/` package as base64-chunked tar.gz to `/opt/yonder/yonder/`, the `S99yonder` init script to `/opt/etc/init.d/`, and `/opt/yonder/yonder.env` (chmod 600) with the admin password for runtime DoH-toggle. |
| 7 | Open firewall port 8080 | Single `iptables -I INPUT` rule. |
| 8 | Start daemon | `python3 -m yonder` via the init script. Polls `netstat` up to 15s waiting for the port to bind. |

### Step 3 — Connect

Open **`http://192.168.1.1:8080/`** in any browser on the LAN.

1. Paste your **VLESS subscription URL** (the standard base64-encoded list of `vless://` lines that most providers serve at a per-user URL).
2. Optionally paste a **routing-rules URL** — a JSON file in [Xray's native routing format](./docs/rules-format.md). The bundled default is "everything through VPN, only local networks direct" — fine to start without one.
3. Pick a country.
4. Flip the **VPN** switch on. Every device on the LAN now exits through the chosen server, with encrypted DNS (Cloudflare DoH by default — editable in the UI's DNS-over-HTTPS card).

---

## Using the UI

| Action | What it does |
|---|---|
| **Pick a different country tile** | Updates the active outbound and reapplies — usually 1–3 seconds. |
| **VPN toggle off** | Stops `xkeen` AND removes the DoH upstream from the router, restoring whatever DNS configuration was there before (typically the ISP-DHCP-acquired one). Services that depend on ISP DNS work again. |
| **VPN toggle on** | Restarts `xkeen` AND pushes the configured DoH upstream to the router (snapshots any pre-existing upstreams so OFF can restore them). |
| **Edit DoH URL** | Lives in the "DNS over HTTPS" card below subscriptions. Save replaces the upstream live (no need to toggle VPN). Reset puts Cloudflare back. |
| **Refresh subscription** | Re-fetches the subscription URL and updates the server list. Useful when the provider rotates nodes. |
| **Add subscription** | Multiple subscriptions can coexist as separate cards. Source can be an `https://...` URL or an inline `vless://...` link. |
| **Set rules URL** | Validates by fetching the URL once and confirming it parses. Stored rules supersede the bundled default. |
| **Refresh rules** | Re-pulls the rules URL and re-applies. |
| **Reset rules to default** | Drops the user-supplied rules URL; falls back to the conservative bundled default. |

The UI polls the backend every 10 seconds (1.5 s while an apply is in flight), so changes you make from another device (a partner toggling on a phone) show up automatically.

---

## What's running on the router

```
/opt/
├── yonder/
│   ├── yonder/             our Python package (the daemon)
│   ├── lib/                pip-installed deps (~25 MB: FastAPI, uvicorn, httpx, pydantic)
│   ├── yonder.env          admin credentials for RCI auth (chmod 600)
│   └── data/
│       ├── state.json      persistent state — atomic writes
│       └── yonderd.log     stdout/stderr of the daemon
├── etc/
│   ├── init.d/
│   │   ├── S99yonder       our init script: launches `python3 -m yonder` on :8080
│   │   └── S99xkeen        XKeen's init script: tproxy iptables + xray daemon
│   └── xray/configs/       six split JSON files merged by xray
│       ├── 01_log.json      ┐
│       ├── 02_dns.json      │
│       ├── 03_inbounds.json ├ XKeen's defaults (we don't touch these)
│       ├── 06_policy.json   ┘
│       ├── 04_outbounds.json  ← we own this — current VLESS server config
│       └── 05_routing.json    ← we own this — your custom rules JSON, or the bundled default
└── sbin/
    ├── xkeen              XKeen wrapper (start/stop/restart, iptables setup)
    └── xray               Xray-core binary
```

Two persistent processes: `xray` (the proxy) and `python3 -m yonder` (the UI + apply pipeline + watchdog + DoH toggle). XKeen sets up iptables rules that REDIRECT TCP and TPROXY UDP from LAN clients to xray on port 1181; xray reads `04_outbounds.json` + `05_routing.json` to decide whether to forward traffic through the VLESS tunnel or send it direct.

The daemon has a small **watchdog** coroutine that polls `pidof xray` every 30 s. If the process died while `vpn_on=true`, it calls `xkeen -restart`. Combined with XKeen's iptables rules (which stay in place when xray dies), this means traffic *fails closed* during a crash — packets to the proxy port find nothing listening and get refused, rather than falling through to a direct route. That's the kill-switch property of the design.

For the deeper architecture, see [ARCHITECTURE.md](./ARCHITECTURE.md).

---

## Notes & gotchas

**HTTPS admin moves to :8443.** This is the most surprising thing the installer does. After install, the Keenetic web UI is at `https://<router>:8443/` — `http://<router>/` (port 80) still works unchanged. The reason is that XKeen tproxies every outbound port by default, and an admin service binding 443 conflicts with that.

**Admin password is on the router.** The installer writes the admin password to `/opt/yonder/yonder.env` (chmod 600), sourced by the init script so the daemon can authenticate to Keenetic's RCI for DoH-toggle. The file is owned by root and unreadable by other Entware users. If you change your Keenetic admin password, re-run the installer to refresh the env file (or edit it manually on the router).

**DoH is on/off with VPN.** When VPN is on, yonder configures the router's `dns-proxy https upstream` to a DoH endpoint (Cloudflare by default). When VPN is off, yonder removes it — the router falls back to whatever DNS configuration was there before (usually the ISP-acquired one). This means devices that *only* resolve through specific ISP DNS (some smart-home services, captive portals, etc.) work normally with VPN off. Editing the URL in the UI takes effect immediately if VPN is on; otherwise stored until next on.

**Devices with their own VPN client bypass us.** If your laptop is running Shadowrocket / WireGuard / similar, that client wraps the traffic before it reaches the router. The router only sees the encrypted tunnel destined for *that* VPN's exit point and routes that one connection — through *our* VPN, ironically — but nothing is decrypted at the router level, so the on-device VPN's exit IP is what shows up. Disable the on-device client for testing the router VPN on that device.

**Country code unknown? `??`** The subscription parser detects country from the leading flag emoji in each `vless://...#name` fragment. If your provider doesn't include a flag, you'll see `??` for those servers. The country code is purely cosmetic — server selection still works.

**Rules use Xray's native JSON format.** No proprietary DSL — same shape as XKeen's `05_routing.json`. The validator accepts the full `{"routing": {"rules": [...]}}` form, the `{"rules": [...]}` shorthand, or a bare `[...]` array. See [docs/rules-format.md](./docs/rules-format.md) for fields, examples, and a migration cheat sheet for users coming from Shadowrocket / Clash / Surge.

**Devices that hardcode their own DNS still use it.** Smart TVs, Chromecasts, etc. that ship with hardcoded `8.8.8.8` or vendor-private DoH bypass our setup. To force them through Keenetic's DoH-equipped DNS-proxy too, SSH into the router and run `dns-proxy / intercept enable / system configuration save` — this DNATs every outbound DNS request to the local proxy. Off by default because some devices break when their preferred resolver is hijacked.

**Updates.** `git pull && pip install -e ".[installer]" && yonder admin@<router>` — the installer is idempotent. It skips steps that are already done and just redeploys the changed Python source (typically ~30 seconds end-to-end). State (subscriptions, picked country, rules, DoH URL) is preserved.

**Local trust model.** The web UI is unauthenticated and bound to all interfaces. Anyone on the LAN can flip the VPN. This is intentional for home use — consider locking it down before exposing to untrusted networks.

**What we deliberately do NOT do.** Some approaches we tried and abandoned, documented so future contributors don't repeat them:

- **xray DNS-via-VPN.** Routing DoH through the VLESS proxy creates a circular dependency (proxy needs DNS, DNS needs proxy) that hard-locks small routers — including ignoring the physical reset button. Even pinning DoH IPs to `direct` to break the cycle left the test router unresponsive ~3 min after every boot. xray's DNS module is too heavy for this hardware. We use Keenetic's built-in DoH instead and keep DNS entirely out of xray's data path.
- **Per-domain DNS overrides** (`ip name-server 1.1.1.1 instagram.com` for each poisoned domain). Works, but requires maintaining a hardcoded list as RKN expands its blocklist. Native DoH covers everything.
- **`opkg dns-override`.** The Keenetic-documented way to free port 53 for an opkg-installed resolver; in our test it left the LAN unable to talk to the router itself, requiring a factory reset.
- **One-shot DoH at install time (the v1 approach).** Got us encrypted DNS, but devices that rely on ISP DNS were broken the moment VPN was off too — no way back without re-running the installer. Replaced by the runtime toggle.

---

## Uninstall

```sh
yonder --uninstall admin@192.168.1.1
```

End state after uninstall:

**Removed / reverted (yonder's footprint, fully cleaned):**
- Daemon stopped, `/opt/yonder/` (package source, pip-installed libs, env file, state.json, log) removed.
- `S99yonder` init script + `/var/run/yonder.pid` removed.
- Firewall rule for port 8080 removed.
- `xkeen -stop` called: xray no longer running, iptables tproxy rules torn down.
- DoH upstream we registered on Keenetic (if VPN was on at uninstall) removed via RCI.
- `04_outbounds.json` + `05_routing.json` overwritten with safe defaults — **VLESS UUID, server hostname, Reality SNI, and custom routing rules are not left on disk.**
- Keenetic HTTPS admin port reverted from 8443 back to 443 (saved to flash).

**Intentionally left in place (shared infrastructure):**
- `Entware` itself (binaries under `/opt/`) and the `opkg disk` registration for the USB drive — Entware is a generic package manager; many users keep other tools on top of it.
- XKeen + Xray binaries (`/opt/sbin/xkeen`, `/opt/sbin/xray`, `/opt/etc/init.d/S99xkeen`) — usable standalone without yonder.
- opkg packages installed by the install flow: `python3`, `python3-pip`, `curl`, `tar`, `findutils`. Removing them is unsafe because other Entware tools may depend on them.
- v2fly `geoip.dat` + `geosite.dat` in `/opt/etc/xray/dat/` — generic xray reference data.

To fully start over, format the USB drive externally (or `opkg remove` the packages manually) and re-run install.

---

## Development

```sh
git clone https://github.com/voledyaev/yonder.git
cd yonder
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,installer]"
pytest tests/                       # ~190 tests, ~2s
yonder --probe admin@192.168.1.1    # SSH connectivity check, no changes
```

Project layout:

```
yonder/         # the daemon package (FastAPI, asyncio, Pydantic)
installer/     # the macOS-side installer (asyncssh)
tests/          # ~190 unit tests + 1 opt-in integration against a real router
docs/           # supplementary docs (Keenetic notes, VLESS / rules formats)
```

To run the integration test against a live router (writes/restores a DoH upstream):

```sh
export YONDER_TEST_ROUTER_HOST=http://192.168.1.1
export YONDER_TEST_ROUTER_PASSWORD=your-admin-password
pytest tests/test_keenetic.py -v -k integration
```

---

## Compatibility

**Keenetic models.** The installer detects router architecture (`aarch64` / `mipsel` / `armv7`) from `show version` and knows the right Entware tarball for each. Modern Keenetic (KN-10xx / KN-11xx / KN-19xx) is aarch64 and fully tested. mipsel / armv7 should work (Entware ships installers for both, Python 3.11 is available in opkg) but is not currently exercised in CI — file an issue if you have an older router.

**KeeneticOS version.** Tested on 5.0.11. The `opkg disk <UUID>:/ <URL>` trick the installer uses for Entware bootstrap is documented for 5.x ([Keenetic support article 18482](https://support.keenetic.com/hero/kn-1012/en/18482.html)). The RCI HTTP API used at runtime for DoH-toggle is available on 4.x+. Earlier 4.x may need manual Entware setup before running our installer.

**Other routers (OpenWrt, Asus-Merlin, etc.).** Out of scope for this installer. The daemon itself is portable Python — the parts that aren't are everything around it: bootstrap (Keenetic-specific via `opkg disk`), XKeen integration (Keenetic-specific iptables work), and Keenetic's RCI API (used for runtime DoH-toggle). Adapting to OpenWrt would mean replacing the installer's Keenetic CLI driver with `opkg` directly and the runtime keenetic.py module with whatever OpenWrt exposes for `dnsmasq` configuration. PRs welcome.

---

## Project docs

- [ARCHITECTURE.md](./ARCHITECTURE.md) — components, data flow, design decisions
- [docs/keenetic-notes.md](./docs/keenetic-notes.md) — Keenetic CLI quirks + RCI HTTP API discovered while building this
- [docs/vless-format.md](./docs/vless-format.md) — subscription / VLESS link parsing reference
- [docs/rules-format.md](./docs/rules-format.md) — accepted custom-rules JSON format
