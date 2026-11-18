"""
RPMote Restart Engine — reboot a remote machine through a server you nominate.

RPMote never restarts a machine directly.  It hands a command to a *restart
server* — a box you already have a connection to, that already has the rights
and the network path to do the job.  For an MSP that is normally the customer's
management server or jump host, which is why the mapping is per tenant.

Two transports, chosen per server:

  ssh         RPMote opens an SSH session to the restart server (paramiko, and
              already shipped) and runs the command there.
  powershell  RPMote runs the command in local PowerShell, which then reaches
              the restart server over WinRM.  Needs WinRM/TrustedHosts set up.

The command itself is a template you own and can edit.  RPMote only fills in
the placeholders and refuses to substitute anything that would turn one command
into two.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading

from PyQt6.QtCore import QObject, pyqtSignal

TRANSPORTS = ("ssh", "powershell")

TRANSPORT_LABELS = {
    "ssh": "SSH to the restart server",
    "powershell": "Local PowerShell (WinRM to the restart server)",
}

DEFAULT_COMMANDS = {
    "ssh": 'shutdown /r /m \\\\{host} /t 0 /f /c "Restart requested via RPMote"',
    "powershell": ("Invoke-Command -ComputerName '{server}' -ScriptBlock "
                   "{ Restart-Computer -ComputerName '{host}' -Force }"),
}

#: (label, transport, template) offered in the setup dialog's preset picker.
COMMAND_PRESETS = (
    ("Windows target — shutdown.exe on the server", "ssh",
     'shutdown /r /m \\\\{host} /t 0 /f /c "Restart requested via RPMote"'),
    ("Windows target — PowerShell on the server", "ssh",
     "powershell -NoProfile -Command \"Restart-Computer -ComputerName '{host}' -Force\""),
    ("Linux target — reboot over SSH from the server", "ssh",
     "ssh -o BatchMode=yes {host} 'sudo systemctl reboot'"),
    ("Restart the server itself", "ssh",
     "sudo systemctl reboot"),
    ("Windows target — Invoke-Command through the server", "powershell",
     ("Invoke-Command -ComputerName '{server}' -ScriptBlock "
      "{ Restart-Computer -ComputerName '{host}' -Force }")),
    ("Windows target — direct WinRM restart", "powershell",
     "Restart-Computer -ComputerName '{host}' -Force"),
)

#: Reboot commands offered for a *direct* restart, where RPMote connects to
#: the machine itself rather than routing through a management server.
#: The first is the default because it survives not knowing the OS: systemctl
#: is missing on macOS and BSD, so `sh -c` falls through to shutdown.
REBOOT_METHODS = (
    ("Auto — systemctl, falling back to shutdown",
     "sudo sh -c 'systemctl reboot || shutdown -r now || reboot'"),
    ("Linux — systemctl reboot", "sudo systemctl reboot"),
    ("macOS / BSD — shutdown -r now", "sudo shutdown -r now"),
    ("Linux — reboot", "sudo reboot"),
    ("Already root — no sudo", "shutdown -r now"),
    ("Windows over OpenSSH — shutdown /r", "shutdown /r /t 0 /f"),
)

DEFAULT_DIRECT_COMMAND = REBOOT_METHODS[0][1]

#: Protocols where the saved credentials are enough to open a shell on the
#: machine itself, so no restart server needs configuring at all.
DIRECT_PROTOCOLS = {"ssh", "sftp"}

PLACEHOLDERS = (
    ("{host}", "target hostname or IP"),
    ("{name}", "target connection name"),
    ("{port}", "target port"),
    ("{user}", "target username"),
    ("{tenant}", "tenant name"),
    ("{server}", "restart server's hostname"),
    ("{server_name}", "restart server's connection name"),
)

DEFAULT_TIMEOUT = 45.0

#: A hostname containing any of these is not a hostname — it is an attempt to
#: append a second command to the template.  Refuse rather than substitute.
_UNSAFE = re.compile(r"[;&|`$<>\r\n\"']|\$\(|\|\|")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

#: A `sudo` at the start of the command or of a piped/chained segment.
_SUDO_TOKEN = re.compile(r"(?:^|[;&|]\s*)sudo(?:\s|$)")
#: The same, excluding invocations already told how to read a password.
_SUDO_NEEDS_STDIN = re.compile(r"(^|[;&|]\s*)sudo(?=\s)(?!\s+-[Sn]\b)")

#: Output that means the password was the problem, not the command.  Checked
#: so a wrong password is never reported as "the machine is rebooting".
_SUDO_FAILURES = (
    "sorry, try again",
    "incorrect password",
    "a terminal is required",
    "no askpass program",
    "is not in the sudoers file",
    "no passwd entry",
    "authentication failure",
)

_REDACTED = "••••••••"


def needs_sudo(command: str) -> bool:
    """True when *command* will have to authenticate before it can run."""
    return bool(_SUDO_TOKEN.search(str(command or "")))


def prepare_sudo(command: str) -> str:
    """Rewrite `sudo` so it takes the password on stdin, with no prompt.

    Without this, sudo tries to open the terminal it was never given and dies
    with "a terminal is required to authenticate".
    """
    return _SUDO_NEEDS_STDIN.sub(r"\1sudo -S -p ''", str(command or ""))


def _redact(text: str, secret: str) -> str:
    """Keep a password out of anything we are about to show or log.

    With a PTY attached the remote tty echoes what we type, so the password
    can come straight back to us in stdout.
    """
    if not secret or not text:
        return text or ""
    return text.replace(secret, _REDACTED)


def can_restart_directly(conn: dict) -> bool:
    """True when RPMote can open a shell on this machine with what it knows."""
    if not isinstance(conn, dict):
        return False
    proto = str(conn.get("protocol", "") or "").strip().lower()
    return proto in DIRECT_PROTOCOLS and bool(str(conn.get("host", "") or "").strip())


class RestartError(Exception):
    """Restart could not be attempted (not configured, unsafe, unreachable)."""


class RestartNotConfigured(RestartError):
    """No restart server has been nominated for this connection."""


# ── Stored configuration ───────────────────────────────────────────

def normalize_config(raw) -> dict | None:
    """Coerce one stored restart-server record, or None if unusable."""
    if not isinstance(raw, dict):
        return None
    conn_id = str(raw.get("conn_id", "") or "").strip()
    if not conn_id:
        return None
    transport = str(raw.get("transport", "") or "").strip().lower()
    if transport not in TRANSPORTS:
        transport = "ssh"
    command = str(raw.get("command", "") or "").strip()
    return {
        "conn_id": conn_id,
        "transport": transport,
        "command": command or DEFAULT_COMMANDS[transport],
        "enabled": bool(raw.get("enabled", True)),
    }


def normalize_config_map(raw) -> dict:
    """Coerce a whole ``{key: config}`` blob, dropping unusable entries."""
    out = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            cfg = normalize_config(value)
            if cfg is not None and str(key).strip():
                out[str(key)] = cfg
    return out


def prune_configs(servers: dict, overrides: dict, connections: list) -> tuple[dict, dict]:
    """Drop configuration pointing at tenants or connections that are gone.

    An empty connection list is treated as "not loaded yet" rather than "the
    user deleted everything", so a transient blank never wipes their setup.
    """
    conns = [c for c in (connections or []) if isinstance(c, dict)]
    if not conns:
        return dict(servers or {}), dict(overrides or {})

    live_ids = {str(c.get("id", "") or "") for c in conns}
    live_tenants = {str(c.get("tenant", "Default") or "Default") for c in conns}

    kept_servers = {
        tenant: cfg for tenant, cfg in (servers or {}).items()
        if tenant in live_tenants and cfg.get("conn_id") in live_ids
    }
    kept_overrides = {
        cid: cfg for cid, cfg in (overrides or {}).items()
        if cid in live_ids and cfg.get("conn_id") in live_ids
    }
    return kept_servers, kept_overrides


# ── Plan resolution ────────────────────────────────────────────────

class RestartPlan:
    """Everything needed to run one restart, resolved and ready to show."""

    def __init__(self, target: dict, server: dict, transport: str, command: str,
                 scope: str, sudo_password: str | None = None):
        self.target = target
        self.server = server
        self.transport = transport
        self.command = command
        self.scope = scope          # "connection" | "tenant" | "direct"
        #: Held in memory for one restart only — never written to the vault.
        self.sudo_password = sudo_password

    @property
    def direct(self) -> bool:
        """True when the command runs on the target itself, with no middleman."""
        return self.scope == "direct"

    @property
    def needs_sudo(self) -> bool:
        return needs_sudo(self.command)

    @property
    def target_name(self) -> str:
        return str(self.target.get("name", "") or self.target.get("host", "") or "?")

    @property
    def target_host(self) -> str:
        return str(self.target.get("host", "") or "")

    @property
    def server_name(self) -> str:
        return str(self.server.get("name", "") or self.server.get("host", "") or "?")

    @property
    def server_host(self) -> str:
        return str(self.server.get("host", "") or "")

    def __repr__(self):
        route = "direct" if self.direct else f"via {self.server_name}"
        return f"<RestartPlan {self.target_name} {route} [{self.transport}]>"


def _safe(value: str, field: str) -> str:
    text = str(value or "")
    if _UNSAFE.search(text):
        raise RestartError(
            f"Refusing to build a restart command: the {field} "
            f"({text!r}) contains shell control characters.")
    return text


def render_command(template: str, target: dict, server: dict) -> str:
    """Fill a command template.

    Substitution is literal replacement, not ``str.format``: a brace in a
    hostname must never be able to reach the formatter, and a PowerShell
    script block full of braces must survive untouched.
    """
    sources = {
        "{host}": (target.get("host", ""), "target host"),
        "{name}": (target.get("name", ""), "target name"),
        "{port}": (target.get("port", ""), "target port"),
        "{user}": (target.get("username", ""), "target username"),
        "{tenant}": (target.get("tenant", "") or "Default", "tenant name"),
        "{server}": (server.get("host", ""), "restart server host"),
        "{server_name}": (server.get("name", ""), "restart server name"),
    }
    out = str(template or "")
    for token, (value, field) in sources.items():
        # Only vet what the template actually asks for: an apostrophe in a
        # tenant name must not break a command that never mentions the tenant.
        if token in out:
            out = out.replace(token, _safe(value, field))
    return out.strip()


def direct_plan(conn: dict, command: str | None = None,
                sudo_password: str | None = None) -> RestartPlan:
    """Restart *conn* by connecting to it and rebooting it, with no setup.

    The machine is both target and server here, which is the whole point: for
    anything RPMote can already open a shell on, nominating a separate restart
    server is ceremony for its own sake.
    """
    if not can_restart_directly(conn):
        name = str((conn or {}).get("name", "") or "This connection")
        raise RestartNotConfigured(
            f"“{name}” cannot be restarted directly.\n\n"
            "A direct restart runs the reboot over SSH, so it needs an SSH or "
            "SFTP connection with a hostname. For anything else — an RDP-only "
            "Windows box, for instance — set up a restart server that can "
            "reach it.")
    command = render_command(command or DEFAULT_DIRECT_COMMAND, conn, conn)
    if not command:
        raise RestartNotConfigured("The reboot command is empty.")
    return RestartPlan(conn, conn, "ssh", command, "direct", sudo_password)


def resolve_plan(conn: dict, connections: list, servers: dict,
                 overrides: dict | None = None,
                 allow_direct: bool = True) -> RestartPlan:
    """Work out how *conn* would be restarted, or say why it cannot be.

    A configured restart server wins, because configuring one was deliberate.
    Failing that, and if *allow_direct*, fall back to rebooting the machine
    over its own connection.
    """
    if not isinstance(conn, dict):
        raise RestartNotConfigured("No connection selected.")

    conn_id = str(conn.get("id", "") or "")
    tenant = str(conn.get("tenant", "Default") or "Default")
    by_id = {str(c.get("id", "") or ""): c
             for c in (connections or []) if isinstance(c, dict)}

    cfg = (overrides or {}).get(conn_id)
    scope = "connection"
    if not cfg or not cfg.get("enabled", True):
        cfg = (servers or {}).get(tenant)
        scope = "tenant"
    if not cfg or not cfg.get("enabled", True):
        if allow_direct and can_restart_directly(conn):
            return direct_plan(conn)
        raise RestartNotConfigured(
            f"No restart server is set up for tenant “{tenant}”.\n\n"
            "Right-click the tenant and choose “Restart Server…” to nominate "
            "the machine that should carry out restarts for it.")

    server = by_id.get(str(cfg.get("conn_id", "") or ""))
    if server is None:
        if allow_direct and can_restart_directly(conn):
            return direct_plan(conn)
        raise RestartNotConfigured(
            f"The restart server configured for “{tenant}” no longer exists.\n\n"
            "Pick a new one from the tenant's “Restart Server…” menu.")

    transport = cfg.get("transport", "ssh")
    template = cfg.get("command") or DEFAULT_COMMANDS.get(transport, "")
    command = render_command(template, conn, server)
    if not command:
        raise RestartNotConfigured(
            "The restart command for this connection is empty.")
    return RestartPlan(conn, server, transport, command, scope)


# ── Execution ──────────────────────────────────────────────────────

class RestartResult:
    def __init__(self, ok: bool, detail: str = "", stdout: str = "",
                 stderr: str = "", exit_code: int | None = None,
                 command: str = ""):
        self.ok = ok
        self.detail = detail
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.command = command

    def summary(self) -> str:
        parts = [self.detail] if self.detail else []
        if self.exit_code is not None:
            parts.append(f"exit code {self.exit_code}")
        tail = (self.stderr or self.stdout or "").strip()
        if tail:
            parts.append(tail[:600])
        return "\n".join(parts) or ("Restart command sent." if self.ok
                                    else "Restart failed.")

    def __repr__(self):
        return f"<RestartResult ok={self.ok} exit={self.exit_code}>"


def _read_stream(stream, secret: str = "") -> str:
    """Read what a channel managed to send us.

    A reboot can cut the link mid-read; whatever arrived before that is still
    the most useful thing we have to show, so a failure here is not fatal.
    """
    try:
        raw = stream.read()
    except Exception:
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    return _redact(text, secret)


def _sudo_rejected(text: str) -> str | None:
    """The sudo failure named in *text*, if the output contains one."""
    lowered = (text or "").lower()
    for marker in _SUDO_FAILURES:
        if marker in lowered:
            return marker
    return None


def _run_over_ssh(plan: RestartPlan, timeout: float) -> RestartResult:
    import paramiko
    from app.connection_manager import load_ssh_private_key
    from app import host_keys

    server = plan.server
    where = "this machine" if plan.direct else "the restart server"
    host = str(server.get("host", "") or "").strip()
    if not host:
        return RestartResult(False, f"The connection for {where} has no "
                                    f"hostname.", command=plan.command)
    try:
        port = int(server.get("port") or 22)
    except (TypeError, ValueError):
        port = 22

    client = paramiko.SSHClient()
    login_password = str(server.get("password", "") or "")
    key_file = str(server.get("key_file", "") or "").strip()
    kwargs = {
        "username": str(server.get("username", "") or ""),
        "timeout": min(timeout, 20.0),
        "auth_timeout": min(timeout, 20.0),
        "banner_timeout": min(timeout, 20.0),
    }
    if key_file:
        try:
            kwargs["pkey"] = load_ssh_private_key(key_file, login_password or None)
        except Exception:
            if login_password:
                kwargs["password"] = login_password
            else:
                raise
    elif login_password:
        kwargs["password"] = login_password

    elevate = plan.needs_sudo
    secret = plan.sudo_password if elevate else None
    # Fall back to the login password: on most boxes the account's own password
    # is exactly what sudo is asking for.
    if elevate and not secret:
        secret = login_password or None
    command = prepare_sudo(plan.command) if elevate else plan.command
    label = plan.target_name if plan.direct else plan.server_name

    try:
        # A reboot has no UI to prompt from, so an unknown host is refused
        # rather than trusted. Open a terminal or SFTP tab to the machine once
        # and the key is verified there, where the fingerprint can be shown.
        host_keys.connect(client, host, port, **kwargs)
        # sudo needs a terminal to authenticate against.  exec_command does not
        # allocate one, which is exactly what "a terminal is required to
        # authenticate" means — so ask for a PTY whenever we are elevating.
        stdin, stdout, stderr = client.exec_command(
            command, timeout=timeout, get_pty=elevate)

        if elevate:
            try:
                stdin.write((secret or "") + "\n")
                stdin.flush()
            except Exception:
                pass
        try:
            # Close stdin so a rejected password makes sudo give up instead of
            # sitting there waiting for a second attempt until we time out.
            stdin.channel.shutdown_write()
        except Exception:
            pass

        out = _read_stream(stdout, secret or "")
        # A PTY merges stderr into stdout, so there is nothing separate to read.
        err = "" if elevate else _read_stream(stderr, secret or "")

        try:
            code = stdout.channel.recv_exit_status()
        except Exception:
            code = None

        rejected = _sudo_rejected(f"{out}\n{err}")
        if rejected:
            if "sudoers" in rejected or "passwd entry" in rejected:
                detail = (f"{kwargs['username'] or 'That account'} is not "
                          f"permitted to run this command under sudo on {label}.")
            else:
                detail = f"The sudo password for {label} was not accepted."
            return RestartResult(False, detail, out, err, code, plan.command)

        if code == 0:
            detail = (f"Reboot command accepted by {label} ({host})."
                      if plan.direct else
                      f"Sent via SSH to {label} ({host}).")
            return RestartResult(True, detail, out, err, code, plan.command)

        if code in (None, -1):
            # The channel closed without reporting a status.  For a reboot that
            # is the expected ending: sshd went down with the rest of the box.
            return RestartResult(
                True, f"{label} closed the connection without reporting back, "
                      f"which is what a reboot looks like from here. Check "
                      f"reachability in a minute to confirm it came back.",
                out, err, code, plan.command)

        return RestartResult(False, f"{label} rejected the command.",
                             out, err, code, plan.command)
    except host_keys.HostKeyMismatch as exc:
        return RestartResult(
            False, f"The host key for {label} ({host}) has CHANGED. Nothing was "
                   f"sent and no password was offered. Expected "
                   f"{', '.join(exc.expected_fingerprints) or 'a different key'}, "
                   f"got {exc.fingerprint}. Verify the machine before restarting "
                   f"it.",
            command=plan.command)
    except host_keys.UnknownHostKey as exc:
        return RestartResult(
            False, f"{label} ({host}) is not a known host yet ({exc.fingerprint}). "
                   f"Open a terminal or SFTP tab to it once to check and trust "
                   f"the fingerprint, then restart it from here.",
            command=plan.command)
    except Exception as exc:
        return RestartResult(
            False, f"Could not run the command on {label}: "
                   f"{_redact(str(exc), secret or '')}",
            command=plan.command)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _powershell_executable() -> str | None:
    import shutil
    return shutil.which("pwsh") or shutil.which("powershell")


def _run_over_powershell(plan: RestartPlan, timeout: float) -> RestartResult:
    shell = _powershell_executable()
    if not shell:
        return RestartResult(
            False, "PowerShell was not found on this machine "
                   "(install pwsh, or use the SSH transport instead).",
            command=plan.command)
    try:
        proc = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", plan.command],
            capture_output=True, text=True, errors="replace",
            timeout=timeout, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return RestartResult(False, f"PowerShell did not finish within "
                                    f"{timeout:.0f}s.", command=plan.command)
    except OSError as exc:
        return RestartResult(False, f"Could not start PowerShell: {exc}",
                             command=plan.command)

    ok = proc.returncode == 0
    detail = ("Sent via PowerShell." if ok
              else "PowerShell reported a failure.")
    return RestartResult(ok, detail, proc.stdout, proc.stderr,
                         proc.returncode, plan.command)


def run_restart(plan: RestartPlan, timeout: float = DEFAULT_TIMEOUT) -> RestartResult:
    """Execute *plan* synchronously.  Never raises — failures come back as a
    result so the caller can show them."""
    try:
        if plan.transport == "powershell":
            return _run_over_powershell(plan, timeout)
        return _run_over_ssh(plan, timeout)
    except Exception as exc:
        return RestartResult(False, f"Restart failed: {exc}",
                             command=getattr(plan, "command", ""))


class RestartRunner(QObject):
    """Runs restarts off the UI thread, one signal per finished attempt."""

    finished = pyqtSignal(object, object)   # RestartPlan, RestartResult
    started = pyqtSignal(object)            # RestartPlan

    def __init__(self, parent=None):
        super().__init__(parent)
        self._threads: list[threading.Thread] = []

    def submit(self, plan: RestartPlan, timeout: float = DEFAULT_TIMEOUT):
        self.started.emit(plan)

        def work():
            result = run_restart(plan, timeout)
            self.finished.emit(plan, result)

        thread = threading.Thread(target=work, name="rpmote-restart", daemon=True)
        self._threads.append(thread)
        self._threads = [t for t in self._threads if t.is_alive() or t is thread]
        thread.start()

    def shutdown(self):
        for signal in (self.finished, self.started):
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass
