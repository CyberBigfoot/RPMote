"""
RPMote Wake-on-LAN — magic packets for connections that are switched off.

A reachability dot that says "down" has two very different causes: something
broke, or nobody has turned the box on yet.  Wake-on-LAN answers the second one
without a drive to site, so it lives right next to the probe engine and reuses
its verdict to say whether the machine actually came back.

The wire format is trivial — six 0xFF bytes followed by the target MAC repeated
sixteen times, sent as a UDP datagram the sleeping NIC is listening for.  The
awkward part is *where* to send it: the target has no IP yet, so the packet has
to reach its broadcast domain.  We therefore fan out across every address that
could plausibly get there (limited broadcast from each local NIC, the target's
own /24 directed broadcast, and unicast in case an ARP entry survives) on both
customary ports.  Datagrams are cheap; a machine that stays asleep is not.

Dependency-free on purpose, like reachability.py, so the packaged .exe needs no
extra wheels.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from PyQt6.QtCore import QObject, pyqtSignal

#: Ports a sleeping NIC conventionally listens on.  Neither is authoritative —
#: the packet is matched on its payload, not its port — so we use both.
DEFAULT_WOL_PORTS = (9, 7)

#: The limited broadcast address never leaves the local segment, which is
#: exactly the guarantee we want as a baseline.
LIMITED_BROADCAST = "255.255.255.255"

#: How long "wake and tell me if it came back" waits before giving up.  A cold
#: boot through POST is comfortably inside this on most hardware.
DEFAULT_WAIT_SECONDS = 120.0
_POLL_INTERVAL = 5.0

_MAC_SEPARATORS = re.compile(r"[\s:\-.]")
_HEX12 = re.compile(r"^[0-9a-fA-F]{12}$")
_IPV4 = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


# ── MAC addresses ──────────────────────────────────────────────────

def normalize_mac(raw) -> str | None:
    """Canonical ``AA:BB:CC:DD:EE:FF``, or None if *raw* is not a MAC.

    Accepts every form an admin is likely to paste: colons, hyphens, the Cisco
    ``aabb.ccdd.eeff`` style, or twelve bare hex digits out of a DHCP table.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    stripped = _MAC_SEPARATORS.sub("", text)
    if not _HEX12.match(stripped):
        return None
    upper = stripped.upper()
    return ":".join(upper[i:i + 2] for i in range(0, 12, 2))


def mac_bytes(mac: str) -> bytes:
    """Six raw bytes for *mac*.  Raises ValueError on anything else."""
    normalized = normalize_mac(mac)
    if normalized is None:
        raise ValueError(f"not a MAC address: {mac!r}")
    return bytes.fromhex(normalized.replace(":", ""))


def magic_packet(mac: str) -> bytes:
    """The 102-byte AMD magic packet for *mac*."""
    return b"\xff" * 6 + mac_bytes(mac) * 16


# ── Connection records ─────────────────────────────────────────────

def connection_mac(conn: dict) -> str | None:
    """The MAC saved against a connection, in canonical form."""
    if not isinstance(conn, dict):
        return None
    for key in ("mac", "mac_address", "wol_mac"):
        found = normalize_mac(conn.get(key))
        if found is not None:
            return found
    return None


def is_wakeable(conn: dict) -> bool:
    """True when we know a MAC to aim a magic packet at."""
    return connection_mac(conn) is not None


def wol_ports(conn: dict) -> tuple:
    """Ports to send on — an explicit ``wol_port`` wins, else both defaults."""
    raw = (conn or {}).get("wol_port", "")
    try:
        port = int(str(raw).strip())
        if 0 < port < 65536:
            return (port,)
    except (TypeError, ValueError):
        pass
    return DEFAULT_WOL_PORTS


def _resolve_ipv4(host: str) -> str | None:
    """The IPv4 address of *host*, or None if it will not resolve."""
    host = str(host or "").strip()
    if not host:
        return None
    if _IPV4.match(host):
        return host
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


def _directed_broadcast(ip: str) -> str | None:
    """``a.b.c.255`` for an IPv4 address, assuming the common /24.

    A guess, but a safe one: on the usual flat customer LAN it *is* the subnet
    broadcast, and where it is wrong the packet is simply not delivered.
    """
    match = _IPV4.match(str(ip or ""))
    if not match:
        return None
    octets = [int(o) for o in match.groups()]
    if any(o > 255 for o in octets):
        return None
    return f"{octets[0]}.{octets[1]}.{octets[2]}.255"


def local_ipv4s() -> list:
    """Every local IPv4 address, so a multi-homed machine broadcasts on each.

    Windows sends a limited broadcast out one interface only, which on an
    admin's laptop (VPN up, Hyper-V switches, a docking NIC) is regularly the
    wrong one — binding per address is what makes the packet actually leave by
    the route the customer's LAN is on.
    """
    found: list = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        address = info[4][0]
        if address and not address.startswith("127.") and address not in found:
            found.append(address)
    return found


def wake_targets(conn: dict) -> list:
    """Addresses worth aiming at, most specific first, without duplicates."""
    targets: list = []

    def add(address):
        address = str(address or "").strip()
        if address and address not in targets:
            targets.append(address)

    # An explicit broadcast is the admin telling us the subnet outright.
    for part in str((conn or {}).get("wol_broadcast", "") or "").split(","):
        add(part)

    ip = _resolve_ipv4((conn or {}).get("host", ""))
    if ip:
        add(_directed_broadcast(ip))
        # Unicast reaches the NIC when the switch or router still holds the ARP
        # entry from before the machine went down — often true, and free.
        add(ip)
    add(LIMITED_BROADCAST)
    return targets


# ── Sending ────────────────────────────────────────────────────────

@dataclass
class WakeResult:
    """What one wake attempt managed to do."""

    mac: str = ""
    host: str = ""
    name: str = ""
    sent: list = field(default_factory=list)     # "addr:port" actually written
    errors: list = field(default_factory=list)   # human-readable failures
    came_up: bool | None = None                  # None when we did not wait
    waited_s: float = 0.0

    @property
    def ok(self) -> bool:
        """True when at least one datagram left the machine.

        Deliberately not "the box woke up": UDP gives no delivery signal, and a
        packet we sent is the whole of what this feature controls.
        """
        return bool(self.sent)

    def summary(self) -> str:
        if not self.ok:
            reason = self.errors[0] if self.errors else "no route to broadcast on"
            return f"No magic packet could be sent — {reason}"
        count = len(self.sent)
        text = (f"Magic packet sent to {self.mac} "
                f"({count} address{'es' if count != 1 else ''})")
        if self.came_up is True:
            text += f" — {self.name or self.host} answered after {self.waited_s:.0f}s"
        elif self.came_up is False:
            text += f" — still no answer after {self.waited_s:.0f}s"
        return text


def send_magic_packet(mac: str, targets=None, ports=DEFAULT_WOL_PORTS,
                      bind_addresses=None) -> WakeResult:
    """Broadcast the magic packet for *mac* at every target, on every port."""
    result = WakeResult(mac=normalize_mac(mac) or str(mac))
    try:
        payload = magic_packet(mac)
    except ValueError as exc:
        result.errors.append(str(exc))
        return result

    targets = list(targets or [LIMITED_BROADCAST])
    ports = tuple(ports or DEFAULT_WOL_PORTS)
    # None means "the default route"; the real addresses are extra chances, not
    # replacements, so a single-NIC machine behaves exactly as it always did.
    sources = [None] + list(bind_addresses if bind_addresses is not None
                            else local_ipv4s())

    for source in sources:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if source:
                sock.bind((source, 0))
            for target in targets:
                for port in ports:
                    try:
                        sock.sendto(payload, (target, int(port)))
                    except OSError as exc:
                        result.errors.append(f"{target}:{port} — {exc}")
                        continue
                    label = f"{target}:{port}"
                    if label not in result.sent:
                        result.sent.append(label)
        except OSError as exc:
            result.errors.append(f"{source or 'default route'} — {exc}")
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    return result


def wait_for_wake(conn: dict, timeout: float = DEFAULT_WAIT_SECONDS,
                  poll: float = _POLL_INTERVAL,
                  should_stop=None) -> tuple:
    """Poll the connection until it answers.  Returns (came_up, seconds).

    Reuses the reachability probe so "up" means exactly what the dashboard's
    green dot means, rather than a second, subtly different definition.
    """
    from app.reachability import STATE_DEGRADED, STATE_UP, probe_connection

    started = time.perf_counter()
    deadline = started + max(float(timeout), 0.0)
    while True:
        if should_stop is not None and should_stop():
            return False, time.perf_counter() - started
        info = probe_connection(conn, timeout=min(poll, 3.0))
        if info.get("state") in (STATE_UP, STATE_DEGRADED):
            return True, time.perf_counter() - started
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return False, time.perf_counter() - started
        time.sleep(min(poll, remaining))


def wake_connection(conn: dict, wait_seconds: float = 0.0,
                    should_stop=None) -> WakeResult:
    """Wake one saved connection, optionally waiting for it to answer."""
    mac = connection_mac(conn)
    host = str((conn or {}).get("host", "") or "").strip()
    name = str((conn or {}).get("name", "") or "").strip()
    if mac is None:
        result = WakeResult(host=host, name=name)
        result.errors.append("no MAC address saved for this connection")
        return result

    result = send_magic_packet(mac, wake_targets(conn), wol_ports(conn))
    result.host, result.name = host, name
    if result.ok and wait_seconds and host:
        result.came_up, result.waited_s = wait_for_wake(
            conn, wait_seconds, should_stop=should_stop)
    return result


# ── Background runner ──────────────────────────────────────────────

class WakeRunner(QObject):
    """Sends magic packets off the UI thread, one batch at a time.

    Signals are emitted from worker threads; because this object lives on the
    UI thread Qt queues them across automatically — the same arrangement
    ReachabilityMonitor uses.
    """

    result_ready = pyqtSignal(object, object)   # conn dict, WakeResult
    finished = pyqtSignal(list)                 # [(conn, WakeResult), ...]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._threads: list = []
        self._stopping = False

    def submit(self, connections, wait_seconds: float = 0.0):
        """Wake every connection in *connections* concurrently."""
        targets = [dict(c) for c in (connections or []) if isinstance(c, dict)]
        if not targets:
            self.finished.emit([])
            return

        def work():
            results: list = []
            workers = min(8, len(targets))
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="rpmote-wol") as pool:
                futures = {
                    pool.submit(wake_connection, conn, wait_seconds,
                                lambda: self._stopping): index
                    for index, conn in enumerate(targets)
                }
                for future in futures:
                    conn = targets[futures[future]]
                    try:
                        result = future.result()
                    except Exception as exc:   # one bad NIC must not sink the batch
                        result = WakeResult(
                            mac=connection_mac(conn) or "",
                            host=str(conn.get("host", "") or ""),
                            name=str(conn.get("name", "") or ""))
                        result.errors.append(f"wake error: {exc}")
                    results.append((conn, result))
                    if not self._stopping:
                        self.result_ready.emit(conn, result)
            if not self._stopping:
                self.finished.emit(results)

        thread = threading.Thread(target=work, name="rpmote-wake", daemon=True)
        self._threads = [t for t in self._threads if t.is_alive()]
        self._threads.append(thread)
        thread.start()

    def shutdown(self):
        """Stop waiting and detach signals before the C++ receivers go away."""
        self._stopping = True
        for signal in (self.result_ready, self.finished):
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass
