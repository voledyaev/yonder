"""SSH transports for the Keenetic structured CLI + the Entware Linux shell.

Two abstractions share the same SSH connection model (admin@router:22, password
auth) but use it differently:

  - `KeeneticCLI` holds an interactive shell session and reads until the
    structured-CLI prompt regex matches. Used for commands that only Keenetic
    can run (opkg disk, system reboot, ip http ssl port).

  - `EntwareShell` runs single Linux commands by wrapping them as `exec sh -c
    "<cmd>"`. The Keenetic CLI's `exec` builtin spawns /opt/bin/sh from the
    USB drive, so this only works after Entware bootstrap. Each call is its
    own SSH session — simpler than maintaining a long-lived PTY shell.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import io
import re
import socket
import tarfile
from pathlib import Path

import asyncssh

# Strip ANSI control sequences + bare carriage returns from Keenetic output.
# The CLI uses them aggressively to redraw lines (esp. ndmc progress).
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\[K|\x1b\][^\x07]*\x07|\r")

# Structured-CLI prompt at end-of-buffer. Examples: `(config)>`, `(dns-proxy)>`,
# `(KN-1012)>`. Allows leading whitespace from line-continuations.
_PROMPT_RE = re.compile(r"\([\w\-]*\)>\s*$")

# Marker echoed after each EntwareShell command. Keenetic's `exec` builtin
# always returns rc=0 to the SSH transport regardless of the wrapped command's
# real exit status, so we parse exit codes from a trailing line we emit
# ourselves.
EXIT_MARKER = "__YONDER_EXIT__"
_EXIT_MARKER_RE = re.compile(re.escape(EXIT_MARKER) + r"=(\d+)\s*$")

# `exec sh` opens a non-login, non-interactive shell that doesn't source
# /opt/etc/profile, so /opt/bin and /opt/sbin (where opkg / xkeen / curl
# live after Entware bootstrap) aren't on PATH by default. We prepend them
# explicitly so every command we run sees a normal Entware environment.
_PATH_PREFIX = "export PATH=/opt/sbin:/opt/bin:/opt/usr/sbin:/opt/usr/bin:$PATH;"


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def kquote(s: str) -> str:
    """Quote `s` for use as one argument to Keenetic CLI's `exec sh -c`.

    Keenetic's CLI argument parser only recognises double quotes; single quotes
    pass through literally and confuse the inner `sh -c`. We escape `\\` and
    `"` only — `$` and backticks still expand in the inner shell, which is
    what we want for `; echo MARKER=$?`.
    """
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def extract_exit_marker(out: str) -> tuple[int, str]:
    """Return (rc, cleaned_output). Missing marker → treated as rc=1."""
    m = _EXIT_MARKER_RE.search(out)
    if not m:
        return 1, out
    rc = int(m.group(1))
    return rc, out[: m.start()].rstrip(" \r\n\t")


class SSHError(Exception):
    """Any SSH-layer failure (connect, run, timeout)."""


def _connect_options(user: str, password: str) -> asyncssh.SSHClientConnectionOptions:
    return asyncssh.SSHClientConnectionOptions(
        username=user,
        password=password,
        known_hosts=None,  # TOFU; the installer is a one-shot tool over LAN
        connect_timeout=15,
    )


# --- KeeneticCLI ----------------------------------------------------------


class KeeneticCLI:
    """Interactive Keenetic structured-CLI session.

    Construct with `await KeeneticCLI.connect(host, user, password)`, send
    commands with `await cli.cmd(line, timeout=...)`, close with
    `await cli.close()`.
    """

    def __init__(
        self, host: str, conn: asyncssh.SSHClientConnection, proc: asyncssh.SSHClientProcess
    ):
        self.host = host
        self._conn = conn
        self._proc = proc
        self._buf = ""

    @classmethod
    async def connect(cls, host: str, user: str, password: str) -> KeeneticCLI:
        try:
            conn = await asyncssh.connect(host, options=_connect_options(user, password))
        except (OSError, asyncssh.Error) as exc:
            raise SSHError(f"ssh dial {host}: {exc}") from exc
        try:
            proc = await conn.create_process(
                term_type="xterm", term_size=(200, 2000), encoding="utf-8"
            )
        except asyncssh.Error as exc:
            conn.close()
            raise SSHError(f"open shell: {exc}") from exc
        cli = cls(host, conn, proc)
        try:
            await cli._wait_for_prompt(30.0)
        except SSHError:
            await cli.close()
            raise
        return cli

    async def cmd(self, command: str, timeout: float = 30.0) -> str:
        """Send a command (newline appended); return ANSI-cleaned output up
        to (and including) the next prompt. Raises SSHError on timeout."""
        self._proc.stdin.write(command + "\n")
        return await self._wait_for_prompt(timeout)

    async def _wait_for_prompt(self, timeout: float) -> str:
        """Read from the shell until _PROMPT_RE matches the accumulated buffer."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                snippet = strip_ansi(self._buf)[-200:]
                raise SSHError(f"timed out (last output: {snippet!r})")
            try:
                chunk = await asyncio.wait_for(self._proc.stdout.read(65536), timeout=remaining)
            except TimeoutError:
                continue
            if not chunk:
                # EOF — session closed unexpectedly.
                raise SSHError(f"shell closed unexpectedly: {strip_ansi(self._buf)[-200:]!r}")
            self._buf += chunk
            clean = strip_ansi(self._buf)
            if _PROMPT_RE.search(clean):
                result = clean
                self._buf = ""
                return result

    async def close(self) -> None:
        try:
            self._proc.stdin.write("exit\n")
        except (BrokenPipeError, asyncssh.Error):
            pass
        try:
            await asyncio.wait_for(self._proc.wait_closed(), timeout=3.0)
        except (TimeoutError, asyncssh.Error):
            self._proc.close()
        self._conn.close()
        await self._conn.wait_closed()


# --- EntwareShell ---------------------------------------------------------


class EntwareShell:
    """Run Linux commands on a router with Entware bootstrapped.

    Each `run()` opens a new SSH session over the persistent connection. The
    Keenetic CLI's `exec sh -c '<cmd>'` builtin spawns /opt/bin/sh (from the
    USB drive) and pipes our stdin/stdout/stderr through.

    SFTP is denied for `admin`, so uploads stream as base64 chunks into a
    staging file and `base64 -d` reassembles on the router.
    """

    # Keenetic's CLI argv cap is somewhere around 8-12K. 6000 bytes per chunk
    # leaves headroom for the `echo ... >> file` boilerplate.
    _CHUNK_BYTES = 6000

    # Basenames skipped when packing a local directory for upload. Keeps
    # editor/cache cruft off the router.
    _UPLOAD_IGNORE = {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".DS_Store",
        ".git",
        ".venv",
        "node_modules",
    }

    def __init__(self, host: str, user: str, conn: asyncssh.SSHClientConnection):
        self.host = host
        self.user = user
        self._conn = conn

    @classmethod
    async def connect(cls, host: str, user: str, password: str) -> EntwareShell:
        try:
            conn = await asyncssh.connect(host, options=_connect_options(user, password))
        except (OSError, asyncssh.Error) as exc:
            raise SSHError(f"ssh dial {host}: {exc}") from exc
        return cls(host, user, conn)

    async def close(self) -> None:
        self._conn.close()
        await self._conn.wait_closed()

    async def run(
        self, cmd: str, *, check: bool = False, timeout: float = 30.0
    ) -> tuple[int, str, str]:
        """Execute `cmd` via `exec sh -c '...'`. Returns (rc, stdout, stderr).

        If `check` is True, non-zero rc raises SSHError with a verbose
        message including stdout/stderr.
        """
        wrapped = f"exec sh -c {kquote(_PATH_PREFIX + cmd + f'; echo {EXIT_MARKER}=$?')}"
        try:
            result = await asyncio.wait_for(self._conn.run(wrapped, check=False), timeout=timeout)
        except TimeoutError as exc:
            raise SSHError(f"command timed out after {timeout}s: {cmd}") from exc
        except asyncssh.Error as exc:
            raise SSHError(f"ssh run: {exc}") from exc

        stdout = strip_ansi(result.stdout or "")
        stderr = strip_ansi(result.stderr or "")
        rc, cleaned = extract_exit_marker(stdout)
        if check and rc != 0:
            raise SSHError(
                f"remote command failed (rc={rc}): {cmd}\n"
                f"--stdout--\n{cleaned}\n--stderr--\n{stderr}"
            )
        return rc, cleaned, stderr

    async def run_script(
        self,
        script: str,
        *,
        check: bool = False,
        timeout: float = 60.0,
        on_output=None,
    ) -> tuple[int, str, str]:
        """Stage a multi-line shell script to /tmp and exec it.

        Why we don't pipe over stdin: stdin-piping to `exec sh` tears down
        the Keenetic CLI session within seconds with "NDM connection closed"
        in the system log, rc=1, no output. Staging + sourcing keeps every
        command on the same SSH transport path that `run()` uses (which we
        already know works for everything else).

        `on_output` is invoked once with the captured body after the script
        finishes (live streaming requires the broken stdin path).
        """
        if not script.endswith("\n"):
            script += "\n"
        staging = "/tmp/__yonder_script.sh"
        # The exit marker is added by run() automatically, so the staged
        # script doesn't need to echo one itself.
        body = f"{_PATH_PREFIX}\n{script}"
        await self.upload_bytes(body.encode(), staging, mode=0o755)
        try:
            rc, out, err = await self.run(
                f"sh {staging} 2>&1; rc=$?; rm -f {staging}; exit $rc",
                check=False,
                timeout=timeout,
            )
        except SSHError:
            # Best-effort cleanup if run() itself errored.
            try:
                await self.run(f"rm -f {staging}", check=False, timeout=5.0)
            except SSHError:
                pass
            raise
        if on_output and out:
            on_output(out)
        if check and rc != 0:
            raise SSHError(
                f"remote script failed (rc={rc}):\n"
                f"--script--\n{script}--stdout--\n{out}\n--stderr--\n{err}"
            )
        return rc, out, err

    async def is_alive(self) -> bool:
        """Tiny `echo` round-trip to verify Entware is reachable."""
        try:
            rc, out, _ = await self.run("echo __OK__", check=False, timeout=5.0)
        except SSHError:
            return False
        return rc == 0 and "__OK__" in out

    async def upload_bytes(self, content: bytes, remote_path: str, mode: int = 0o755) -> None:
        """Write `content` to `remote_path` with the given mode.

        Streams as base64 chunks via `echo ... >> file`, then decodes with
        `base64 -d`. Used for single files (init script, etc.).
        """
        b64 = base64.b64encode(content).decode()
        staging = remote_path + ".b64.tmp"
        parent = str(Path(remote_path).parent)
        await self.run(f"mkdir -p {parent} && rm -f {staging}", check=True, timeout=15.0)
        await self._upload_b64_chunked(b64, staging)
        await self.run(
            f"base64 -d {staging} > {remote_path} && rm -f {staging} && "
            f"chmod {mode:o} {remote_path}",
            check=True,
            timeout=60.0,
        )

    async def upload_directory(self, local_dir: Path | str, remote_dir: str) -> None:
        """Pack a local directory as tar.gz, ship base64-chunked, untar on
        the router. Existing remote_dir is wiped first so deploys are
        idempotent.
        """
        local = Path(local_dir)
        if not local.is_dir():
            raise SSHError(f"missing local dir: {local}")

        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for path in sorted(local.rglob("*")):
                    rel = path.relative_to(local)
                    parts = rel.parts
                    if any(p in self._UPLOAD_IGNORE for p in parts):
                        continue
                    if path.suffix == ".pyc":
                        continue
                    tar.add(path, arcname=str(rel), recursive=False)

        b64 = base64.b64encode(buf.getvalue()).decode()
        staging = "/tmp/__yonder_upload.b64"
        await self.run(
            f"rm -rf {remote_dir} && mkdir -p {remote_dir} && rm -f {staging}",
            check=True,
            timeout=30.0,
        )
        await self._upload_b64_chunked(b64, staging)
        await self.run(
            f"base64 -d {staging} | tar xzf - -C {remote_dir} && rm -f {staging}",
            check=True,
            timeout=120.0,
        )

    async def _upload_b64_chunked(self, b64: str, remote_path: str) -> None:
        for i in range(0, len(b64), self._CHUNK_BYTES):
            chunk = b64[i : i + self._CHUNK_BYTES]
            redir = ">" if i == 0 else ">>"
            await self.run(f"echo {chunk} {redir} {remote_path}", check=True, timeout=30.0)


# --- Probes ---------------------------------------------------------------


async def is_entware_ready(host: str, user: str, password: str) -> bool:
    """Oracle for "Entware bootstrap done": tries `exec sh -c 'echo MARKER'`
    over a fresh SSH session and looks for the marker in stdout.
    """
    try:
        async with await asyncssh.connect(host, options=_connect_options(user, password)) as conn:
            result = await asyncio.wait_for(
                conn.run('exec sh -c "echo __ENTWARE_OK__"', check=False),
                timeout=10.0,
            )
            return "__ENTWARE_OK__" in (result.stdout or "")
    except (TimeoutError, OSError, asyncssh.Error):
        return False


async def wait_for_ssh_up(host: str, timeout_s: float, poll_s: float = 5.0) -> None:
    """Poll TCP :22 until it accepts a connection. Used after rebooting."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(host, 22), timeout=3.0)
            w.close()
            try:
                await w.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        except (TimeoutError, OSError, socket.gaierror):
            await asyncio.sleep(poll_s)
    raise SSHError(f"router did not come back on {host}:22 within {timeout_s}s")
