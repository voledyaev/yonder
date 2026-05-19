# Architecture

## Overview

```
┌─ Mac / Linux (one-time, for install/uninstall) ────────┐
│  yonder — Python installer, asyncssh.                  │
│  Orchestrates SSH into Keenetic CLI (Entware bootstrap)│
│  AND Entware shell (deploy). Ships the yonder/ Python  │
│  package as a tar.gz to the router.                    │
└────────────────┬───────────────────────────────────────┘
                 │ SSH :22 (Keenetic CLI + `exec sh`)
                 │  — install / uninstall / probe only
                 ▼
┌─ User devices on LAN ──────────────────────────────────┐
│  Phones, laptops, TV — all DHCP clients of the router  │
└────────────────┬───────────────────────────────────────┘
                 │ HTTP :8080  (admin UI, any device)
                 │ all traffic (transparently routed by XKeen)
                 ▼
┌─ Keenetic Giga KN-1012 ────────────────────────────────┐
│                                                        │
│  /opt/  (Entware on USB ext4 drive)                    │
│    ├─ etc/init.d/                                      │
│    │   ├─ S99xkeen           ← XKeen's autostart       │
│    │   └─ S99yonder          ← our autostart           │
│    ├─ etc/xray/configs/      ← split JSON merged by    │
│    │                            xray; we own 04 + 05   │
│    ├─ sbin/                                            │
│    │   ├─ xkeen              ← XKeen wrapper script    │
│    │   └─ xray               ← Xray-core binary        │
│    └─ yonder/                                          │
│        ├─ yonder/            ← Python package          │
│        ├─ lib/               ← pip-installed deps      │
│        ├─ yonder.env         ← admin creds (chmod 600) │
│        └─ data/                                        │
│            ├─ state.json     ← saved settings          │
│            └─ yonderd.log    ← stdout/stderr           │
│                                                        │
│  Two persistent processes:                             │
│   - xray         (the VLESS proxy, supervised by XKeen)│
│   - python3 -m yonder  (HTTP UI + apply pipeline +     │
│                         watchdog + DoH toggle via RCI) │
│                                                        │
│  XKeen → intercepts LAN traffic via iptables/tproxy →  │
│          forwards through xray → out via active VLESS  │
│                                                        │
│  yonder → talks to Keenetic's RCI HTTP API on :80      │
│           to add/remove dns-proxy https upstream when  │
│           VPN is on/off                                │
└────────────────────────────────────────────────────────┘
                 │
                 ▼
   VLESS provider (e.g. provider.example) — multiple
   country endpoints; user picks one at a time
```

## Components

### Daemon — `yonder/` package

Python 3.11+, asyncio throughout. FastAPI for HTTP, Uvicorn as ASGI server, Pydantic v2 for state + request schemas, httpx for outbound HTTP (subscription fetch, rules fetch, RCI client). No paramiko / no asyncssh in the daemon — those live only in the installer.

Configuration via env vars, set by the init script:

| Variable | Default | Purpose |
|---|---|---|
| `YONDER_BASE_DIR` | `/opt/yonder/data` | Where `state.json` lives. No default — refuses to guess. |
| `YONDER_LISTEN` | `0.0.0.0:8080` | Listen address. |
| `YONDER_XRAY_CONFIGS` | `/opt/etc/xray/configs` | XKeen's configs dir; overridable for local dev. |
| `YONDER_KEENETIC_HOST` | `http://192.168.1.1` | RCI base URL; on the router itself, `http://localhost` also works. |
| `YONDER_KEENETIC_USER` | `admin` | RCI login. |
| `YONDER_KEENETIC_PW` | — | RCI password. Empty → DoH-toggle fails gracefully. Sourced from `/opt/yonder/yonder.env` (chmod 600). |

Three concurrent things at startup, all on the same asyncio event loop:

- **uvicorn** — serves the FastAPI app.
- **`ApplyPipeline`** — single-worker coroutine. Reads apply signals from an `asyncio.Event`, regenerates xray configs, calls DoH on/off, restarts xkeen.
- **`Watchdog`** — process supervisor. Every 30 s checks `pidof xray`; if dead while `vpn_on=true`, calls `xkeen -restart` with exponential backoff on repeated failures.

On startup `pipeline.signal()` is called once to reconcile router state with whatever `vpn_on` persisted from the last run — a daemon restart never leaves xkeen out of sync.

### Modules

| Module | LOC | What it does |
|---|---:|---|
| `yonder/api.py` | 450 | FastAPI app factory + routes (subscriptions CRUD, server select, VPN toggle, rules URL, **dns config**, state, health). Exception handlers normalise to `{"error": msg}`. |
| `yonder/state.py` | 267 | Pydantic v2 models for persistent state. `State` wraps the JSON file with `asyncio.Lock` for writes; readers take `model_copy(deep=True)` snapshots without locking. Atomic writes via `tmp.write_bytes()` + `tmp.replace()`. |
| `yonder/keenetic.py` | 223 | HTTP client for Keenetic's RCI. Implements the Keenetic-specific challenge-response auth (MD5 + SHA256 over `realm` + `challenge`). Uses `/rci/parse` for writes (`dns-proxy https upstream <url>`) and `/rci/<path>` JSON for reads. |
| `yonder/vless.py` | 221 | Subscription parser. Handles base64-wrapped and plaintext bodies. Country detection from flag emoji + multilingual name aliases. |
| `yonder/apply.py` | 168 | Apply pipeline orchestrator. Signal+Event coalescing, ON/OFF state machine, DoH rollback on xkeen failure. |
| `yonder/xray.py` | 158 | Generates `04_outbounds.json` + `05_routing.json`. Atomic file writes. |
| `yonder/watchdog.py` | 131 | Async process supervisor with exponential backoff. Decoupled from `State` via a `WatchdogDeps` Protocol. |
| `yonder/services.py` | 104 | Wraps `xkeen -start/-stop/-restart`. **Stdio explicitly redirected to `subprocess.DEVNULL`** — see the load-bearing comment in `_run` about xray-fork inherited fd hangs. |
| `yonder/rules.py` | 101 | xray routing rules validator. Accepts three top-level shapes and normalises to a flat list. |
| `yonder/doh.py` | 90 | DoH on/off with `previous_upstreams` snapshot+restore. Idempotent via `state.dns.active_url`. |
| `yonder/fetch.py` | 57 | Bounded HTTP GET (1 MiB / 30s) for subscription + rules URLs. Streams to detect overflow. |

### State — `data/state.json`

Single source of truth for runtime config. Atomic write via `Path.replace()` from a `.tmp` sibling. Schema lives in `yonder.state.Data` (Pydantic v2 model). Current version: **v2**, no migration from v1 (clean break for a single-user project; v1 file gets dropped at load → defaults).

```jsonc
{
  "version": 2,
  "subscriptions": [
    {
      "id": "sub-1747269000-3a4f9b",        // stable, generated at add time
      "label": "My provider",                // user-facing (auto-derived from host if empty)
      "source": "https://provider.example/connection/subs/UUID",
      "fetched_at": "2026-05-15T12:00:00Z",
      "servers": [
        {"id": "pl.example:8443", "country": "PL", "name": "🇵🇱 Польша",
         "host": "pl.example", "port": 8443, "uuid": "...",
         "params": {"security": "reality", "type": "tcp", "flow": "...",
                    "sni": "...", "fp": "...", "pbk": "...", "sid": "..."}}
      ]
    }
  ],
  "active_server": {                         // composite ref, nullable
    "subscription_id": "sub-1747269000-3a4f9b",
    "server_id": "pl.example:8443"
  },
  "vpn_on": true,
  "rules_url": "https://gist.../xray-routing.json",
  "rules_fetched_at": "2026-05-15T12:05:00Z",
  "rules": [...],                            // list[dict], passes through xray-routing JSON
  "rules_warnings": [],
  "rules_skipped_count": 0,
  "last_error": "",
  "last_apply": {                            // outcome of most recent apply cycle
    "at": "2026-05-15T12:05:08Z",
    "ok": true,
    "msg": ""
  },
  "applying": false,                         // true while apply pipeline is mid-iteration
  "dns": {                                   // new in v2.1 (backwards-compatible field add)
    "doh_url": "https://cloudflare-dns.com/dns-query",   // user-configurable
    "active_url": "https://cloudflare-dns.com/dns-query", // what we've actually pushed (null = nothing)
    "previous_upstreams": []                              // saved on enable, restored on disable
  }
}
```

`last_apply` records the outcome of the most recent apply attempt and **persists across subsequent successes** — earlier we wiped `last_error` on the next OK apply, which hid transient failures from the UI. `applying` is a transient flag flipped on by the handler synchronously (so the very first poll response after a click already shows it) and cleared by the apply worker at the end of its iteration; the UI disables interactive controls while it's true.

The `dns` section is the linchpin of the runtime DoH-toggle. `active_url` separates "what the user wants" (`doh_url`, editable in UI) from "what's actually on the router right now" — critical when the user edits the URL while VPN is on: disable_doh must remove the URL we actually pushed, not the new one in settings.

A `source` starting with `vless://` is parsed in place (no HTTP fetch) — supports both subscription URLs and single inline links.

### XKeen integration — `yonder/xray.py`

- Reads active server + rules from `state.snapshot()`
- Writes `04_outbounds.json` (proxy / direct / block) and `05_routing.json` (rules + `domainStrategy: AsIs`) — both atomic via `.tmp` + rename
- The other four XKeen config files (`01_log`, `02_dns`, `03_inbounds`, `06_policy`) are left at XKeen's tested defaults — we never touch tproxy / iptables setup ourselves.

### Rules pipeline — `yonder/rules.py`

- Input: JSON in xray's native routing format
  (`{"routing": {"rules": [...]}}`, `{"rules": [...]}`, or a bare array)
- Validation: each rule must have a recognised `outboundTag` (direct / proxy / block) and at least one match field (domain / ip / port / network / …).
- Each validated rule has `type: "field"` auto-filled if missing.
- Output: rules go into `state.rules`; `xray.write_xkeen_split` splices them into `05_routing.json` on every reapply.
- If no URL set: bundled default in `yonder.xray.default_rules()` — only RFC 1918 / link-local / multicast direct, everything else falls through to `proxy`.
- See [docs/rules-format.md](./docs/rules-format.md) for accepted shapes and a Shadowrocket / Clash / Surge migration cheat sheet.

### Frontend — `yonder/static/`

- **Vanilla HTML + Alpine.js** (single CDN script tag, no build).
- Reactive state via `x-data`. Polls `/api/state` every 10 s (1.5 s while `applying=true`).
- Cards:
  - **Status** — VPN on/off, country tiles, current selection.
  - **Subscriptions** — multiple cards, each is one provider/source.
  - **DNS over HTTPS** — `state.dns.doh_url` input, Save, Reset to Cloudflare.
  - **Routing rules** — URL + refresh.
- No router/SPA framework — direct fetch + state. Served via FastAPI's `StaticFiles` at `/`.

### Apply pipeline — `yonder/apply.py`

Single-worker coroutine consumes apply signals from an `asyncio.Event` and walks the on/off state machine:

```
ON  = write xray configs → enable_doh → xkeen restart
OFF = xkeen stop → disable_doh
```

If xkeen fails during ON, DoH is rolled back (`disable_doh`) so the router doesn't end up with our DNS upstream but no working tunnel.

Handlers do not call apply steps directly. They mutate state + call `pipeline.signal()` (non-blocking, just sets the `Event`). Multiple rapid signals coalesce into one extra worker iteration — the worker `Event.clear()`s before doing work, so signals arriving during an iteration trigger another pass. Same property as Go's buffered-1 channel.

### Watchdog — `yonder/watchdog.py`

Async coroutine launched from `__main__.py`:

```
every 30 s:
  if not vpn_on:            continue
  if is_running(xray):       continue
  ok, msg = services.restart()
  on repeated failure: exponential back-off up to 5 min
```

Watchdog calls `services.restart()` **directly**, not through the apply pipeline — a recovery restart doesn't need to re-write configs or re-toggle DoH (those haven't changed; the only thing that happened is xray died).

While `vpn_on=true` but xray is dead, XKeen's iptables rules stay in place: client traffic gets REDIRECT'd to port 1181 (xray's tproxy) and finds nothing listening → connection refused. The LAN **fails closed** (no leak to a direct route) during recovery. That's the kill-switch property of the design.

### DoH toggle — `yonder/doh.py` + `yonder/keenetic.py`

The DoH-on-with-VPN feature has two halves:

**The protocol layer** (`keenetic.py`) talks HTTP to Keenetic's REST Core Interface (RCI). Auth is a Keenetic-specific challenge-response:

```
GET /auth                → 401, response sets a session cookie + X-NDM-Realm + X-NDM-Challenge headers
compute token            = sha256(challenge + md5(login + ":" + realm + ":" + password))
POST /auth {login, token} → 200, session cookie now authorised
```

Subsequent calls reuse the cookie. Writes go to `/rci/parse` with CLI-mirror commands (`dns-proxy https upstream <url>`); reads use the JSON tree (`/rci/dns-proxy/https/upstream` returns the current upstream list). On 401 mid-session (cookie expiry, ~5 min idle) the client transparently re-auths and retries.

**The state machine** (`doh.py`) orchestrates the user's edits + VPN on/off transitions:

- On `enable_doh`: snapshots whatever upstreams the user already had into `state.dns.previous_upstreams`, removes them, adds our `doh_url`, records it as `active_url`.
- On `disable_doh`: removes `active_url`, restores `previous_upstreams`, clears both.
- Idempotent: re-running `enable_doh` with the same `doh_url` is a no-op. URL change mid-session (user edited in UI while VPN is on) is detected by `active_url != doh_url` → swaps cleanly.

Note: we never run `system configuration save`. The DoH upstream lives only in running-config, gets wiped on reboot, and yonder re-applies on startup based on persisted `vpn_on`. This avoids NAND wear from frequent toggles.

### Installer — `installer/` package

- Python 3.11+ on the install machine (Mac / Linux).
- **`asyncssh`** for SSH — handles password auth + interactive PTY + per-command exec without the boilerplate `paramiko` requires.
- Two SSH transports:
  - **`KeeneticCLI`** (`installer/ssh.py`) — long-lived PTY shell. Reads stdout until the structured-CLI prompt regex (`(name)>`) matches. Used for things only the Keenetic CLI knows: `show version`, `opkg disk`, `system reboot`, `ip http ssl port`.
  - **`EntwareShell`** (`installer/ssh.py`) — one SSH session per command. Each command is wrapped as `exec sh -c '<cmd>; echo MARKER=$?'` to escape the Keenetic CLI layer and capture real exit codes (Keenetic's `exec` builtin always returns rc=0 to SSH). Chunked base64 upload (no SFTP for `admin`).
- Top-level flows in `installer/flows.py`: `do_install`, `do_uninstall`, `do_probe`.
- Pre-flight / bootstrap / XKeen / Python-deps / deploy / start in `installer/steps.py`.
- Pure parsers for `show version` arch, components list, `ls` USB-drive output, `tools ping` success — `installer/parsers.py`, fully unit-testable.

**DoH configuration is intentionally NOT in the installer.** The daemon manages it at runtime via RCI synchronized with `vpn_on`. The installer's only DoH-related step is checking that the `dns-https` Keenetic firmware component is present (required for the runtime API to work).

### Init script — `installer/resources/S99yonder`

Standard Entware init.d entry: `start | stop | restart | status`.

- Sets `PYTHONPATH=/opt/yonder:/opt/yonder/lib` and the `YONDER_*` env vars.
- Sources `/opt/yonder/yonder.env` (chmod 600) for `YONDER_KEENETIC_PW` — keeps the admin password out of `ps -ef`.
- Launches `python3 -m yonder`, PID in `/var/run/yonder.pid`, stdout/stderr to `/opt/yonder/data/yonderd.log`.

## Data flow examples

All mutation endpoints follow the same shape: update state synchronously, ack the browser immediately, then drive xkeen + DoH asynchronously through the single apply worker. See [Async apply](#async-apply) below.

### Switching country

```
user clicks 🇩🇪 in UI
  → POST /api/server  {"subscription_id": "sub-...", "server_id": "de.example:8443"}
  → api.post_server
     → state.update(d.active_server = ActiveServerRef(...); d.applying = True)
                                                          ↑ synchronous, BEFORE response
     → return state.snapshot()                            // ack browser; applying=true visible
     → pipeline.signal()                                  // non-blocking event.set()
  → UI sees applying=true → disables tiles + toggle, shows "applying changes…"

(meanwhile, in the apply coroutine:)
  → _apply_once():
     → write_xkeen_split(active, rules, configs_dir)
     → if vpn_on:
         → enable_doh(state, keenetic)
         → services.restart()    (rolls back DoH if it fails)
     → else:
         → services.stop()
         → disable_doh(state, keenetic)
  → state.update(d.applying = False, d.last_apply = ApplyResult(...))

UI polls /api/state every 1.5s while applying=true (10s otherwise), sees the
flip, unfreezes controls, shows "last applied at HH:MM:SS".
```

### Editing the DoH URL

```
user types https://dns.google/dns-query in the DoH input, clicks Save
  → POST /api/dns/config {"doh_url": "https://dns.google/dns-query"}
  → api.post_dns_config
     → Pydantic validator: must be https:// + ≤ 2048 chars; else 400
     → state.update(d.dns.doh_url = "..."; d.applying = True)
     → pipeline.signal()
     → return state.snapshot()
  → UI shows applying=true

(in the apply coroutine, if vpn_on=true:)
  → enable_doh sees active_url ≠ doh_url → mid-session swap:
     → keenetic.remove_doh_upstream(old)
     → keenetic.add_doh_upstream(new)
     → state.dns.active_url ← new (previous_upstreams unchanged — already captured)
  → services.restart()  (xray picks up new DNS through Keenetic's dns-proxy)

(if vpn_on=false: enable_doh is skipped; the new URL is just stored
and will be applied on next on.)
```

### Pressing OFF fully restores router to ISP defaults

The headline reason for the runtime-DoH design. Compare two paths after pressing OFF:

```
1. xkeen -stop      → iptables tproxy rules removed; LAN traffic goes direct
2. disable_doh:
   → keenetic.remove_doh_upstream(state.dns.active_url)  // our URL gone
   → for url in state.dns.previous_upstreams:
       keenetic.add_doh_upstream(url)                    // user's prior URLs (if any) back
   → state.dns.active_url ← None
   → state.dns.previous_upstreams ← []
```

Now `show running-config` on the router shows whatever the user had configured before yonder was ever turned on — typically nothing, meaning DNS goes via the ISP-DHCP nameserver. Devices that rely on ISP DNS work again.

### Adding a subscription

```
user fills in label + source in "Add subscription" form
  → POST /api/subscriptions  {"label": "Foo", "source": "https://..."  OR  "vless://..."}
  → api.post_add_subscription
     → Pydantic validator: source must start with http(s)://  or  vless://
     → if source.startswith("vless://"):  parse_subscription(source.encode())
       else:                              fetch_url(fetcher, source)
     → state.add_subscription(label, source, servers)  // generates new ID, atomic write
     → return state.snapshot()
  → UI renders the new card immediately
```

Adding a subscription doesn't touch xkeen — only changing the active server (or rules, or DoH config) does. No `pipeline.signal()` in this path.

### Refreshing rules

```
user clicks "Refresh rules"
  → POST /api/rules/refresh
  → api.post_rules_refresh
     → fetch_url(fetcher, rules_url, max_bytes=1MiB)
     → parse_xray_rules(raw) → list[dict] | RulesParseError → 400
     → state.update(d.rules = rules, d.rules_fetched_at = now_iso(), d.applying = True)
     → return state.snapshot()
     → pipeline.signal()      // worker re-reads state, rewrites 05_routing.json, restarts xkeen
```

### Async apply

`xkeen -restart` takes up to 90 seconds and re-installs LAN-side iptables tproxy rules during that window. If the HTTP response from `/api/toggle` is held open across the restart, the in-flight TCP connection gets torn down as a side effect — the browser hangs on `await fetch()` forever and the UI stays disabled.

Fix: respond to the user **before** kicking off xkeen. A single coroutine (`ApplyPipeline._loop`) reads from an `asyncio.Event`; handlers call `pipeline.signal()` (= `event.set()`) to nudge it. Concurrent requests coalesce harmlessly because the worker re-reads `state.snapshot()` at each iteration — final intent always wins. The coroutine `event.clear()`s before doing work, so signals arriving during the iteration trigger another pass.

Failures surface via `state.last_error` + `state.last_apply.msg`, picked up by the UI's poll.

## Design decisions

| Decision | Why |
|---|---|
| Python + FastAPI on the router instead of a static Go binary | The Go version was ~5600 LOC for the same functional surface; Python is ~3300 LOC. asyncio + Pydantic + FastAPI absorb a lot of the boilerplate (state lock, JSON marshalling, request validation) that Go required hand-rolled. opkg ships python3 and pydantic-core has a prebuilt aarch64 wheel — runtime overhead is negligible. |
| Pydantic v2 for state schema | Validation + JSON (de)serialisation in one decorator. Adding `dns: DnsState` to the schema was backwards-compatible — old v2 state.json files load with the default and need no migration code. |
| `asyncio.Lock` for state writes; readers take `model_copy(deep=True)` | No reader/writer contention without needing an RWLock. Snapshot copies are cheap on Pydantic v2; lock is held only during the write+save. |
| `subprocess.DEVNULL` in `services._run` | `xkeen -restart` forks xray as a daemon. xray inherits the parent's stdio fds. If we pipe either, asyncio's read-until-EOF logic waits forever because the daemon never closes its inherited fd. `proc.wait()` then blocks indefinitely. Even worse: timing out kills xkeen (our direct child) but not xray (already orphaned), so the pipe stays open and the drain task stays stuck. Both fds must be redirected to `/dev/null` so the inheritance chain is broken. Trade-off: we lose xkeen's stderr for error messages, but the daemon-leak protection wins. See the load-bearing comment in `yonder/services.py:_run`. |
| Async-everywhere apply pipeline via `asyncio.Event` | Mirrors Go's buffered-1 channel pattern. Coalesces rapid clicks; final intent wins because the worker re-reads state. |
| DoH-toggle at runtime via Keenetic's RCI | Lets "OFF means OFF" — devices that rely on ISP DNS keep working when VPN is off. The Go installer's one-time DoH-at-install broke that. |
| `/rci/parse` for RCI writes instead of the JSON tree-write API | The CLI-mirror format (`dns-proxy https upstream <url>`) is debuggable, matches docs, and we discovered the tree-write JSON shapes are inconsistent across paths. `/rci/parse` was empirically validated against KOS 5.0.11. |
| No `system configuration save` after DoH toggles | The DoH upstream lives in running-config only, gets wiped on reboot, and yonder re-applies on startup based on persisted `vpn_on`. Saves NAND wear from frequent toggles. |
| Admin password in `/opt/yonder/yonder.env` (chmod 600) sourced by init script | Needed at runtime for RCI auth. Putting it in the init script directly would leak via `ps -ef`. Putting it in state.json would leak via `/api/state` HTTP responses. The env file is owned by root and only ever read by the init script. |
| `asyncssh` for the installer | Same connection pool for `KeeneticCLI` (interactive PTY) and `EntwareShell` (per-command exec). Cleaner than paramiko's two-API split. |
| Chunked base64 upload over SSH (no SFTP) | Keenetic denies SFTP for the `admin` user. tar.gz → base64 → `echo X >> file` chunks under the CLI's argv cap is the only path that works. |
| Pure parsers for installer (arch / components / USB / ping) split from SSH layer | Parsing `show version` and `ls` is regex matching against canned text — testable without a router. Bulk of the installer's correctness lives in unit-testable functions; the SSH layer is mostly orchestration. |
| Alpine.js + no build step | No Node.js on the router; CDN script is enough for this UI. Same approach as the Go version. |
| JSON file for state, no DB | Trivial schema; atomic-write good enough; greppable. |
| No HTTPS / no auth on the web UI | LAN-only trust model. Auth would add complexity and break the "just open a URL" UX. Documented in README. |

## Out of scope (for now)

- Multiple simultaneous outbounds / load balancing across countries
- Per-device routing policies (e.g. only iPhone goes through VPN)
- HTTPS for the admin UI / authentication
- Mobile app
- Other router architectures — currently only Keenetic aarch64 (KN-1012 + modern Keenetic). mipsel / armv7 should work since Entware ships installers and Python is available, but is not currently exercised in CI.
- Other router platforms (OpenWrt, Asus-Merlin) — `installer/` is Keenetic-CLI specific, `yonder/keenetic.py` uses Keenetic's RCI HTTP API. Would need a separate installer driver and a different runtime DoH adapter.
