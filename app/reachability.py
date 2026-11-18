"""
RPMote Reachability Engine — background health probing for saved connections.

A connection is "up" when the port you would actually connect on answers.  That
is a stronger claim than ICMP: a domain controller that pings but has RDP
stopped is still a broken connection, and saying so is the whole point.  ICMP is
only used as a *fallback* to tell "the box is gone" apart from "the box is there
but the service isn't" — which is the difference between driving out to site and
restarting a service.

Probing is deliberately dependency-free (socket + the system ping binary) so it
keeps working inside the packaged .exe with no extra wheels to ship.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import QObject, pyqtSignal

# ── States ─────────────────────────────────────────────────────────

STATE_UNKNOWN = "unknown"    # never probed, or probing is switched off
STATE_CHECKING = "checking"  # a probe is in flight
STATE_UP = "up"              # the service port answered
STATE_DEGRADED = "degraded"  # host replies to ping, but the port is shut
STATE_DOWN = "down"          # nothing answered
STATE_SKIPPED = "skipped"    # nothing meaningful to probe (e.g. Azure PowerShell)

#: Colours the dashboard and sidebar both key off, so a dot means the same
#: thing everywhere in the app.
STATE_COLORS = {
    STATE_UP:       "#4ade80",
    STATE_DEGRADED: "#fbbf24",
    STATE_DOWN:     "#f87171",
    STATE_CHECKING: "#60a5fa",
    STATE_UNKNOWN:  "#5b6072",
    STATE_SKIPPED:  "#5b6072",
}

STATE_LABELS = {
    STATE_UP:       "Reachable",
    STATE_DEGRADED: "Host up, service down",
    STATE_DOWN:     "Unreachable",
    STATE_CHECKING: "Checking…",
    STATE_UNKNOWN:  "Not checked",
    STATE_SKIPPED:  "No host to check",
}

#: Rank used when rolling per-connection states up to a tenant tile: the worst
#: real result wins, because a tenant with one dead server is not "green".
_SEVERITY = {
    STATE_DOWN: 4,
    STATE_DEGRADED: 3,
    STATE_UP: 2,
    STATE_CHECKING: 1,
    STATE_UNKNOWN: 0,
    STATE_SKIPPED: 0,
}

#: Fallback port per protocol when a connection does not carry one.
DEFAULT_PORTS = {
    "ssh": 22,
    "sftp": 22,
    "ftp": 21,
    "rdp": 3389,
    "idrac": 443,
    "synology": 5001,
    "telnet": 23,
    "vnc": 5900,
    "http": 80,
    "https": 443,
    "web": 443,
}

#: Protocols with no network target of their own — probing them is meaningless.
HOSTLESS_PROTOCOLS = {"azure_ps", "serial"}

DEFAULT_PING_SETTINGS = {
    "enabled": False,
    "interval": 30,        # seconds between sweeps
    "timeout": 2.0,        # seconds per individual probe
    "icmp_fallback": True,  # distinguish "host up, port shut" from "host gone"
    "workers": 8,          # concurrent probes per sweep
    # One sweep of the whole estate straight after unlock, whether or not
    # continuous probing is on: the first thing an admin wants on opening
    # RPMote is to know what is already broken.
    "check_on_login": True,
}

_INTERVAL_RANGE = (5, 3600)
_TIMEOUT_RANGE = (0.5, 15.0)
_WORKER_RANGE = (1, 32)

# Windows: keep the ping subprocess from flashing a console window.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def normalize_ping_settings(raw) -> dict:
    """Coerce stored settings into something the engine can safely run on."""
    out = dict(DEFAULT_PING_SETTINGS)
    if isinstance(raw, dict):
        for key, default in DEFAULT_PING_SETTINGS.items():
            if key not in raw:
                continue
            value = raw[key]
            try:
                if isinstance(default, bool):
                    out[key] = bool(value)
                elif isinstance(default, int):
                    out[key] = int(value)
                else:
                    out[key] = float(value)
            except (TypeError, ValueError):
                continue
    out["interval"] = max(_INTERVAL_RANGE[0], min(_INTERVAL_RANGE[1], out["interval"]))
    out["timeout"] = max(_TIMEOUT_RANGE[0], min(_TIMEOUT_RANGE[1], out["timeout"]))
    out["workers"] = max(_WORKER_RANGE[0], min(_WORKER_RANGE[1], out["workers"]))
    return out


# ── Individual probes ──────────────────────────────────────────────

def connection_host(conn: dict) -> str:
    host = str((conn or {}).get("host", "") or "").strip()
    if "://" in host:
        # A web console saved as a URL: probe the machine behind it.
        from urllib.parse import urlparse
        return urlparse(host).hostname or ""
    return host


def connection_port(conn: dict) -> int | None:
    """The port this connection would actually dial, or None if unknowable."""
    host = str((conn or {}).get("host", "") or "").strip()
    if "://" in host:
        from urllib.parse import urlparse
        parsed = urlparse(host)
        if parsed.port:
            return parsed.port
        return 80 if parsed.scheme == "http" else 443
    raw = (conn or {}).get("port", "")
    try:
        port = int(str(raw).strip())
        if 0 < port < 65536:
            return port
    except (TypeError, ValueError):
        pass
    proto = str((conn or {}).get("protocol", "") or "").strip().lower()
    return DEFAULT_PORTS.get(proto)


def is_probeable(conn: dict) -> bool:
    """False for records with nothing on the network to look at."""
    if not isinstance(conn, dict):
        return False
    proto = str(conn.get("protocol", "") or "").strip().lower()
    if proto in HOSTLESS_PROTOCOLS:
        return False
    return bool(connection_host(conn))


def tcp_probe(host: str, port: int, timeout: float = 2.0) -> tuple[bool, float, str]:
    """Open and immediately close a TCP connection.  (ok, ms, detail)."""
    started = time.perf_counter()
    sock = None
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        elapsed = (time.perf_counter() - started) * 1000.0
        return True, elapsed, f"port {port} open"
    except socket.timeout:
        return False, (time.perf_counter() - started) * 1000.0, "timed out"
    except OSError as exc:
        detail = getattr(exc, "strerror", None) or str(exc) or exc.__class__.__name__
        return False, (time.perf_counter() - started) * 1000.0, str(detail)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def icmp_probe(host: str, timeout: float = 2.0) -> tuple[bool, float, str]:
    """One ICMP echo via the system ping binary.  (ok, ms, detail).

    Raw ICMP sockets need administrator rights on Windows, so shelling out to
    ping is the only option that works for an ordinary user.
    """
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", str(int(max(timeout, 0.1) * 1000)), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(int(timeout), 1)), host]

    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace",
            timeout=max(timeout + 2.0, 3.0), creationflags=_NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, (time.perf_counter() - started) * 1000.0, str(exc) or "ping failed"

    elapsed = (time.perf_counter() - started) * 1000.0
    out = f"{proc.stdout}\n{proc.stderr}"
    if os.name == "nt":
        # Windows ping exits 0 for "Destination host unreachable" replies from
        # an intermediate router, so a zero exit code alone proves nothing.  A
        # TTL in the output means a genuine echo reply from the target.
        ok = proc.returncode == 0 and re.search(r"ttl\s*=", out, re.I) is not None
    else:
        ok = proc.returncode == 0

    match = re.search(r"time[=<]\s*([\d.]+)\s*ms", out, re.I)
    if match:
        try:
            elapsed = float(match.group(1))
        except ValueError:
            pass
    return ok, elapsed, "ping replied" if ok else "no ping reply"


def probe_connection(conn: dict, timeout: float = 2.0,
                     icmp_fallback: bool = True) -> dict:
    """Full three-state probe of one connection.

    Returns ``{"state", "latency_ms", "detail", "host", "port", "checked_at"}``.
    """
    now = time.time()
    if not is_probeable(conn):
        return {"state": STATE_SKIPPED, "latency_ms": None,
                "detail": "no host to check", "host": connection_host(conn),
                "port": None, "checked_at": now}

    host = connection_host(conn)
    port = connection_port(conn)
    if port is None:
        # Nothing sensible to dial, but the box itself can still be pinged.
        ok, ms, detail = icmp_probe(host, timeout)
        return {"state": STATE_DEGRADED if ok else STATE_DOWN,
                "latency_ms": ms if ok else None,
                "detail": detail if ok else "unknown port, no ping reply",
                "host": host, "port": None, "checked_at": time.time()}

    ok, ms, detail = tcp_probe(host, port, timeout)
    if ok:
        return {"state": STATE_UP, "latency_ms": ms, "detail": detail,
                "host": host, "port": port, "checked_at": time.time()}

    if icmp_fallback:
        ping_ok, ping_ms, ping_detail = icmp_probe(host, timeout)
        if ping_ok:
            return {"state": STATE_DEGRADED, "latency_ms": ping_ms,
                    "detail": f"port {port}: {detail} — but {ping_detail}",
                    "host": host, "port": port, "checked_at": time.time()}

    return {"state": STATE_DOWN, "latency_ms": None,
            "detail": f"port {port}: {detail}",
            "host": host, "port": port, "checked_at": time.time()}


def rollup_state(states) -> str:
    """Worst state in *states* — how a tenant tile summarises its members."""
    worst, rank = STATE_UNKNOWN, -1
    for state in states:
        value = _SEVERITY.get(state, 0)
        if value > rank:
            worst, rank = state, value
    return worst


def describe(info: dict) -> str:
    """One-line human summary for a tooltip."""
    if not isinstance(info, dict):
        return STATE_LABELS[STATE_UNKNOWN]
    state = info.get("state", STATE_UNKNOWN)
    text = STATE_LABELS.get(state, state)
    latency = info.get("latency_ms")
    if state in (STATE_UP, STATE_DEGRADED) and isinstance(latency, (int, float)):
        text += f"  ·  {latency:.0f} ms"
    detail = str(info.get("detail", "") or "")
    if detail and state in (STATE_DOWN, STATE_DEGRADED):
        text += f"\n{detail}"
    checked = info.get("checked_at")
    if isinstance(checked, (int, float)) and checked:
        text += f"\nChecked {time.strftime('%H:%M:%S', time.localtime(checked))}"
    return text


# ── Background sweeper ─────────────────────────────────────────────

class ReachabilityMonitor(QObject):
    """Polls every probeable connection on an interval, off the UI thread.

    Results are cached, so a repaint can ask for a state without waiting on
    anything.  Signals are emitted from worker threads; because this object
    lives on the UI thread Qt queues them across automatically.
    """

    status_changed = pyqtSignal(str, dict)   # conn_id, info
    sweep_started = pyqtSignal()
    sweep_finished = pyqtSignal(dict)        # {conn_id: info}

    def __init__(self, settings: dict | None = None, parent=None):
        super().__init__(parent)
        self._settings = normalize_ping_settings(settings)
        self._connections: dict[str, dict] = {}
        self._statuses: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._force: set[str] | None = None   # None = whole estate
        self._pending = False            # a sweep has been asked for
        self._sweeping = False

    # ── Configuration ─────────────────────────────────────────────

    @property
    def settings(self) -> dict:
        return dict(self._settings)

    @property
    def enabled(self) -> bool:
        return bool(self._settings["enabled"])

    def apply_settings(self, settings: dict):
        """Merge new options and start/stop the worker to match."""
        was_enabled = self.enabled
        old_interval = self._settings["interval"]
        merged = dict(self._settings)
        merged.update(settings or {})
        self._settings = normalize_ping_settings(merged)

        if self.enabled and not was_enabled:
            self.start()
        elif was_enabled and not self.enabled:
            self.stop()
            self._reset_statuses()
        elif self.enabled and self._settings["interval"] != old_interval:
            self._wake.set()   # re-sleep on the new interval

    def set_enabled(self, enabled: bool):
        self.apply_settings({"enabled": bool(enabled)})

    def set_connections(self, connections):
        """Replace the probe set, keeping cached results for surviving ids."""
        fresh: dict[str, dict] = {}
        for conn in (connections or []):
            if not isinstance(conn, dict):
                continue
            cid = str(conn.get("id", "") or "").strip()
            if not cid:
                continue
            fresh[cid] = {
                "id": cid,
                "name": str(conn.get("name", "") or ""),
                "host": connection_host(conn),
                "port": connection_port(conn),
                "protocol": str(conn.get("protocol", "") or "").lower(),
                "probeable": is_probeable(conn),
            }
        with self._lock:
            self._connections = fresh
            # A connection whose host changed should not keep showing the old
            # box's green dot, so drop any cached result that no longer matches.
            for cid in list(self._statuses):
                target = fresh.get(cid)
                if target is None:
                    self._statuses.pop(cid, None)
                elif self._statuses[cid].get("host") != target["host"]:
                    self._statuses.pop(cid, None)

    # ── Results ───────────────────────────────────────────────────

    def status(self, conn_id: str) -> dict:
        with self._lock:
            return dict(self._statuses.get(str(conn_id), {})) or {
                "state": STATE_UNKNOWN, "latency_ms": None,
                "detail": "", "checked_at": 0.0}

    def state(self, conn_id: str) -> str:
        return self.status(conn_id).get("state", STATE_UNKNOWN)

    def statuses(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._statuses.items()}

    def tenant_state(self, conn_ids) -> str:
        return rollup_state(self.state(cid) for cid in conn_ids)

    def _reset_statuses(self):
        with self._lock:
            self._statuses = {}
        self.sweep_finished.emit({})

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            self._wake.set()
            return
        self._stopping = False
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run, name="rpmote-reachability", daemon=True)
        self._thread.start()

    def stop(self, join: bool = True):
        self._stopping = True
        self._wake.set()
        thread = self._thread
        self._thread = None
        if join and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3.0)

    def shutdown(self):
        """Stop for good and detach signals so a late emit cannot reach a
        deleted C++ receiver."""
        self.stop()
        for signal in (self.status_changed, self.sweep_started, self.sweep_finished):
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass

    def check_now(self, conn_ids=None):
        """Probe immediately — the whole estate, or just *conn_ids*."""
        with self._lock:
            if conn_ids is None:
                self._force = None          # widen to everything
            elif not (self._pending and self._force is None):
                # Only narrow the request when a full sweep is not already
                # queued; "check everything" must not become "check this one".
                wanted = {str(c) for c in conn_ids}
                self._force = wanted if not self._pending else (self._force | wanted)
            self._pending = True
        if self._thread is None or not self._thread.is_alive():
            self._stopping = False
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._run, name="rpmote-reachability",
                kwargs={"once": not self.enabled}, daemon=True)
            self._thread.start()
        else:
            self._wake.set()

    # ── Worker ────────────────────────────────────────────────────

    def _run(self, once: bool = False):
        while not self._stopping:
            with self._lock:
                forced = self._force
                self._force = None
                self._pending = False
                targets = [
                    dict(meta) for cid, meta in self._connections.items()
                    if meta["probeable"] and (forced is None or cid in forced)
                ]
                skipped = [
                    cid for cid, meta in self._connections.items()
                    if not meta["probeable"] and (forced is None or cid in forced)
                ]

            self._sweep(targets, skipped)

            if self._stopping:
                break
            if once or not self.enabled:
                # A request that arrived while this sweep was running would
                # otherwise be swallowed as the one-shot worker exits.
                if not self._pending:
                    break
                continue
            if self._wake.wait(self._settings["interval"]):
                self._wake.clear()   # woken early by check_now / a settings change
        self._sweeping = False

    def _sweep(self, targets: list, skipped: list):
        now = time.time()
        with self._lock:
            for cid in skipped:
                self._statuses[cid] = {
                    "state": STATE_SKIPPED, "latency_ms": None,
                    "detail": "no host to check", "checked_at": now,
                    "host": self._connections.get(cid, {}).get("host", "")}
        if not targets:
            self.sweep_finished.emit(self.statuses())
            return

        self._sweeping = True
        self.sweep_started.emit()
        self._mark_checking(targets)

        timeout = float(self._settings["timeout"])
        icmp = bool(self._settings["icmp_fallback"])
        workers = min(int(self._settings["workers"]), max(len(targets), 1))

        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="rpmote-probe") as pool:
            futures = {
                pool.submit(probe_connection, meta, timeout, icmp): meta["id"]
                for meta in targets
            }
            for future in futures:
                cid = futures[future]
                try:
                    info = future.result()
                except Exception as exc:          # a probe must never kill the sweep
                    info = {"state": STATE_DOWN, "latency_ms": None,
                            "detail": f"probe error: {exc}", "checked_at": time.time()}
                if self._stopping:
                    # Probes not started yet are dropped; the window is going.
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                with self._lock:
                    self._statuses[cid] = info
                self.status_changed.emit(cid, dict(info))

        self._sweeping = False
        if self._stopping:
            # Probes that timed out (a dead network) can outlast the window,
            # and with it the C++ side of this monitor.
            return
        self.sweep_finished.emit(self.statuses())

    def _mark_checking(self, targets: list):
        with self._lock:
            for meta in targets:
                previous = self._statuses.get(meta["id"], {})
                self._statuses[meta["id"]] = {
                    **previous,
                    "state": STATE_CHECKING,
                    "host": meta["host"],
                    "port": meta["port"],
                }
        for meta in targets:
            self.status_changed.emit(meta["id"], self.status(meta["id"]))
