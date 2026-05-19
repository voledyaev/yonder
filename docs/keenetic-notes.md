# Keenetic CLI / Entware notes

Reference notes from setting this up on a Keenetic Giga KN-1012 (aarch64) running KeeneticOS 5.0.11. Updated 2026-05.

## Enabling SSH (post-factory-reset gotcha)

After a factory reset (and on some fresh installs) the SSH server is **stopped** even when the `admin` user has the `cli` tag. The `cli` tag controls *what a user is allowed to do once connected* — it does not start the daemon.

Two unrelated commands look like they should enable the server but don't:

- `ip ssh` — enters a `(config-ssh)` block where you can set port and ciphers, but doesn't actually start the daemon. Exiting and saving config leaves the server `STOPPED`.
- `show ip ssh` — not a valid command at all; Keenetic doesn't expose SSH state under the `ip` namespace.

The actual command is **`service ssh`** at the `(config)` prompt:

```
service ssh                  ! starts the daemon (and generates host keys on first run)
system configuration save
```

To verify, use `show processes` and look for the `SSH server` entry — `state` should switch from `STOPPED` to `RUNNING`. From a client: `nc -zv <router> 22` should print `succeeded`.

The same `service` namespace controls other daemons too (`service telnet`, `service http`, etc.) — it's the Keenetic equivalent of systemd unit start.

If you can't SSH in at all and need to issue CLI commands, the web admin has a hidden CLI page at **`http://<router>/webcli/parse`**. It accepts the same commands as the SSH CLI and returns JSON responses with prompt/error info. Indispensable for unbricking.

## SSH access modes — what yonder actually uses

Keenetic SSH at port 22 lands the `admin` user (CLI tag) into the structured CLI (`(config)>` prompt). That's a command tree — not a Unix shell. To run Linux commands you have two options:

1. **`tag opt` + `opkg chroot` enabled** — drops the same login straight into the Entware shell. Requires per-user configuration in Keenetic and Entware-bootstrap completion.
2. **`exec sh -c '<cmd>'` as a CLI command** — the structured CLI's `exec` builtin spawns the named binary (we always pass `sh` from `/opt/bin/`, available after Entware bootstrap). One-line shell escapes from inside the CLI session.

**yonder's installer uses option 2.** It works on a vanilla CLI-tagged admin user without touching Keenetic user-tag configuration. Trade-off: the CLI's `exec` builtin returns rc=0 to the SSH transport regardless of the wrapped command's real exit status — we work around this by appending `; echo MARKER=$?` and parsing the trailer (see `installer/ssh.py: extract_exit_marker`).

At runtime, the daemon does **not** use SSH at all — it talks to Keenetic's RCI HTTP API (see [RCI HTTP API](#rci-http-api) below), which is much cheaper than spawning a shell per command and doesn't depend on the daemon having admin SSH credentials in the conventional sense.

Port 222 (an SSH listener that some Entware builds open with `root@router` direct access to `/opt/bin/sh`) was **closed** on KN-1012 / KOS 5.0.11 in our testing, so we don't rely on it. If your build has it open, change the default `keenetic` password immediately.

## Keenetic CLI is not a Unix shell

It's a structured command tree. Top-level groups:

```
access-list, authentication, cifs, cloud, components, copy, discovery,
dns-proxy, dpn, dyndns, easyconfig, erase, eula, igmp-proxy, interface,
ip, ipv6, isolate-private, known, ls, mdns, mkdir, more, mws, ndns, ntp,
object-group, opkg, ping-check, ppe, pppoe, printer, schedule, service,
show, sms, system, tools, upnp, user, ussd, whoami
```

Useful for our purposes:

- **`ls [<storage>:/<path>]`** — list directory or, at top level, enumerate storage volumes (USB drives, flash, etc.) with their UUID / label / fstype / mount state / free space. yonder parses top-level `ls` output to find ext4-mounted USB drives.
- **`mkdir <storage>:/<path>`** / **`erase`** / **`more`** — directory / file primitives. Useful but rarely needed from yonder (we work in `/opt` via `exec sh`).
- **`copy <source> <dest>`** — copy file. **Source must be a local storage path** (`flash:`, `temp:`, `storage:`, USB UUID). HTTP(S) source URLs are **not** accepted in KOS 5.x despite older docs claiming so.
- **`opkg disk <UUID>:/ [<installer-URL>]`** — register a USB filesystem as the OPKG/Entware host. When given a second argument that points at an Entware installer tarball URL, Keenetic also downloads and unpacks it on next reboot — this is the entire CLI-driven Entware bootstrap mechanism.
- **`opkg chroot`** — toggle: drops `tag opt` users into Entware shell on next login. yonder doesn't use this (we use `exec sh -c` instead).
- **`service ssh`** / **`service telnet`** / etc — start a daemon. See [Enabling SSH](#enabling-ssh-post-factory-reset-gotcha) above.
- **`system configuration save`** — persist running config to flash. Required after any change.
- **`system reboot`** — restart the device.
- **`show running-config`** — full config dump. Useful for verifying state after `dns-proxy` / `ip http` / etc edits.
- **`show processes`** — list all firmware services with state (RUNNING / STOPPED). Indispensable for debugging "why isn't X listening".

## Identifying the USB drive

Top-level `ls` enumerates block devices. Each entry looks like:

```
entry, type = V:
     name: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee:
   fstype: ext4
   storage: usb
  mounted: yes
     free: 123456789
    total: 128000000000
```

In CLI commands the storage prefix is `<UUID>:` (with trailing colon), e.g. `ls aaaaaaaa-...:/`. yonder's installer takes the UUID of the first ext4-USB entry and passes it to `opkg disk <UUID>:/`.

## Entware bootstrap (KeeneticOS 5.x)

### What's needed

- USB drive formatted as ext4, plugged in (state: `mounted: yes` in `ls`)
- Components installed: `opkg`, `ext`, `opkg-kmod-netfilter`, `opkg-kmod-netfilter-addons`
- (For yonder) `dns-https` component — the daemon toggles DoH at runtime via this

### CLI-driven bootstrap via `opkg disk <UUID>:/ <URL>`

KeeneticOS 5.x exposes a one-shot command that handles fetch + unpack + register in one go:

```
opkg disk <UUID>:/ https://bin.entware.net/aarch64-k3.10/installer/EN_aarch64-installer.tar.gz
system configuration save
system reboot
```

After reboot, `/opt` is populated and `exec sh` works over SSH. This is what `installer/steps.py: bootstrap_entware` calls — no manual USB swap, no web UI clicks. Verified end-to-end on KN-1012 / KOS 5.0.x.

> Historical note: earlier project drafts (and some web-search summaries) claimed Keenetic's `copy` command accepts HTTP/HTTPS source URLs as a way to fetch the tarball — it does **not** in KeeneticOS 5.0+ (`copy http://...` returns `argument parse error`). That dead end is why we use `opkg disk` instead. SFTP is also not exposed for `admin`, and CIFS/SMB ports are closed by default. The `opkg disk` route is the only scriptable bootstrap path on stock firmware.

### Alternatives (manual paths)

If `opkg disk` fails for some reason, two fallbacks exist:

- **Web UI.** `http://<router>/` → System Settings → "Install OPKG packages on this drive". Same end result, one click.
- **USB swap.** Eject the drive, drop the tarball at `<usb>/install/EN_<arch>-installer.tar.gz` from another machine, plug back, and `system configuration save && system reboot`.

We don't drive either programmatically — the CLI path is enough.

### Architecture-specific installer URLs

| Keenetic CPU | Architecture | Installer URL |
|---|---|---|
| Most modern Keenetic (KN-10xx, KN-11xx, KN-19xx) | aarch64 | `http://bin.entware.net/aarch64-k3.10/installer/EN_aarch64-installer.tar.gz` |
| Older Keenetic (KN-19xx pre-2022, etc.) | mipsel | `http://bin.entware.net/mipselsf-k3.4/installer/EN_mipsel-installer.tar.gz` |
| Some specialty models | armv7 | `http://bin.entware.net/armv7sf-k3.2/installer/EN_armv7-installer.tar.gz` |

The installer should detect architecture via `show version` (or after bootstrap via `uname -m`) and pick the right URL.

## XKeen install

[jameszeroX/XKeen](https://github.com/jameszeroX/XKeen) is the XKeen fork our installer drives. Verified working end-to-end on KN-1012 / KOS 5.0.x with our default-answers-piped-via-stdin flow.

From the Entware shell, the installer command our `installXKeen` runs is equivalent to:

```sh
opkg update
opkg install curl
sh -c "$(curl -fsSL https://raw.githubusercontent.com/jameszeroX/XKeen/main/install.sh)"
```

A separate upstream exists at [Corvus-Malus/XKeen](https://github.com/Corvus-Malus/XKeen) — also a known port. We pinned to `jameszeroX` because that's what worked first; switching would require re-validating the prompt sequence our installer feeds.

XKeen lays down six configs at `/opt/etc/xray/configs/`:

- `01_log.json`, `02_dns.json`, `03_inbounds.json`, `06_policy.json` — XKeen's defaults; we don't touch
- `04_outbounds.json` — `direct`, `block`, `proxy` outbound stubs (we overwrite)
- `05_routing.json` — routing rules (we overwrite)

`yonder/xray.py: write_xkeen_split` rewrites 04 + 05 atomically; the apply pipeline then runs `xkeen -restart`.

## DNS-proxy and DoH

KeeneticOS 4.x+ has a built-in DNS-over-HTTPS client baked into the system DNS-proxy (`ndnproxy` on port 53). The CLI exposes it as:

```
dns-proxy
  https upstream <url> [<format>] [spki <hash>] [on <interface>] [domain <domain>]
```

Once an `https upstream` is registered, the firmware quietly does encrypted resolution to that endpoint instead of plain UDP/53 to the DHCP-acquired ISP nameserver. Adding Cloudflare's endpoint defeats ISP-level DNS poisoning of services like Meta and X.

The CLI help is the only documentation we found — `dns-proxy ?` and `dns-proxy https upstream ?` work as discovery. The `format` arg defaults to `dnsm` (DNS message JSON, RFC 8484); we leave it implicit. `spki` cert-pinning is supported but skipped — Cloudflare rotates its certificates often enough that pinning is more risk than reward.

**yonder toggles this at runtime, not install-time.** The daemon adds the configured DoH upstream when VPN is turned on and removes it when VPN is off (restoring whatever upstreams the user had before, if any). See `yonder/doh.py: enable_doh` / `disable_doh` and the RCI section below for the protocol. The reason it's not an install-time step anymore: with a sticky DoH upstream, devices that rely on ISP DNS (some smart-home services, captive portals) stayed broken even when VPN was off. The runtime toggle restores "OFF means OFF".

**`dns-proxy intercept enable`** (also under the `dns-proxy` block) DNATs every outbound DNS request from LAN clients to the local DNS-proxy. Useful for forcing smart TVs etc. (which often hardcode `8.8.8.8` directly) through your DoH config — but it can break devices that *rely* on talking to a specific external resolver. Off by default; users can opt in.

## RCI HTTP API

KeeneticOS exposes a REST Core Interface on the same HTTP port as the web admin. It mirrors the CLI tree — every CLI command has an `/rci/<path>` equivalent that returns JSON. Indispensable for programmatic configuration without keeping an SSH PTY open. Discovered via `WWW-Authenticate: x-ndw2-interactive` header on `GET /rci/` and the `! $$$ Agent: http/rci` annotation that appears at the top of `show running-config` when the dump is requested through the RCI.

### Auth handshake

Not Basic, not Digest. Keenetic-specific challenge-response:

```
1. GET /auth                  → 401 + Set-Cookie + headers:
                                X-NDM-Realm: Keenetic Giga
                                X-NDM-Challenge: <32 hex chars>

2. token = sha256_hex(challenge + md5_hex("admin:" + realm + ":" + password))

3. POST /auth                 → 200 + session cookie now authorised
   Content-Type: application/json
   body: {"login": "admin", "password": <token>}

4. subsequent GET/POST /rci/* with the same cookie → JSON responses
```

Sessions expire after ~5 minutes of idle. The client (`yonder/keenetic.py`) re-auths transparently on the first 401 mid-session and replays the request.

### Useful endpoints

- **GET `/rci/`** — full configuration tree as JSON. Same content as `show running-config` but parseable without text scraping.
- **GET `/rci/show/version`** — JSON version info: release, arch, components.
- **GET `/rci/dns-proxy`** — current dns-proxy configuration as a tree (`rebind-protect`, `intercept`, `https` keys).
- **GET `/rci/dns-proxy/https/upstream`** — current DoH upstream list: `[{"url": "...", "format": "dnsm"}, ...]`.
- **POST `/rci/parse`** with `{"parse": "<single CLI command>"}` — executes one CLI command (e.g. `dns-proxy https upstream <url>` or `no dns-proxy https upstream <url>`). Returns the same status block format as the CLI: `{"prompt": "(config)", "status": [{"status": "message"|"error", "code": "...", "ident": "...", "message": "..."}]}`.

We use `/rci/parse` for writes because the JSON tree-write shape is inconsistent across paths and the CLI-mirror format is easier to debug.

### Idempotency

Adding the same upstream twice returns success ("...added.") both times — Keenetic treats it as a no-op. Removing one that doesn't exist returns an error with the ident `Dns::Secure::ManagerDoh` and the message starting `no such DNS-over-HTTPS server` — `yonder/keenetic.py: remove_doh_upstream` swallows that specific error as success.

### Where the docs end and we begin

Keenetic publishes some RCI references in their Command Reference PDFs but coverage is patchy — the `dns-proxy https upstream` JSON shapes specifically aren't documented. Everything we use was validated empirically against KOS 5.0.11 / KN-1012. If you're porting this to a different firmware, expect to spike the exact payload shapes the same way.

## Networking

XKeen uses **tproxy** mode by default, which transparently intercepts all LAN-originated traffic by setting iptables rules and a custom routing table. This means client devices need zero configuration — they just use the router as gateway as usual.

LAN access to our admin UI on port 8080: by default Entware processes can bind to LAN interfaces freely. We add an explicit iptables ACCEPT rule for port 8080 from the LAN bridge to be defensive against future firewall tightening.

## Security gotchas

1. **Port 222 (direct `root@router` → Entware shell).** Closed on KN-1012 / KOS 5.0.x in our testing. If your build opens it, the default `root` password is `keenetic` — change it immediately (`passwd` from inside the Entware shell).
2. **yonder web UI is unauthenticated.** Bound on all interfaces, anyone on LAN can flip the VPN. Intentional for home use; lock down before exposing to untrusted networks. Documented in README.
3. **Entware tarball fetched over HTTPS** (`bin.entware.net`). Keenetic does TLS verification by default. If `bin.entware.net` is ever compromised, we'd execute attacker code — checksums would help but aren't currently verified.
4. **XKeen install.sh** is fetched from GitHub raw and piped to `sh`. Same trust assumption as any `curl | sh` install. The repository is `jameszeroX/XKeen` — pin to a specific commit if you want defense in depth.

## References

- [Keenetic support — Installing Entware on USB drive (KN-1012)](https://support.keenetic.com/hero/kn-1012/en/20980-installing-the-entware-repository-on-a-usb-drive.html)
- [Keenetic forum — Entware Quickstart](https://forum.keenetic.com/topic/4290-entware-quickstart/)
- [Corvus-Malus/XKeen](https://github.com/Corvus-Malus/XKeen)
- [Entware aarch64 installer index](https://bin.entware.net/aarch64-k3.10/installer/)
