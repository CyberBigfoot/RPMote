"""SSH port forwarding — the ``-L`` and ``-R`` of the command line.

A forward is saved on the connection and comes up with it, so the RDP tunnel
or the database port a customer's box needs is there the moment the session
is, rather than being a command someone has to remember to run.

Threads here are deliberately plain: a listener thread per local forward, and
a pump thread per open pair. Nothing touches Qt, so the same code serves the
terminal, the file browser and anything headless.
"""

from __future__ import annotations

import re
import select
import socket
import threading

#: A forward that has not moved a byte in this long is still kept: idle is
#: normal. This is only how long a closing pump waits before giving up.
_PUMP_TIMEOUT = 0.5
_CHUNK = 32768

LOCAL = "local"
REMOTE = "remote"

_SPEC_RE = re.compile(
    r"""^\s*
    (?:(?P<kind>[LR])[:\s]+)?          # optional L: / R: prefix
    (?:(?P<listen_host>[^\s:]+):)?     # optional bind address
    (?P<listen_port>\d+):
    (?P<dest_host>[^\s:]+):
    (?P<dest_port>\d+)
    \s*$""",
    re.IGNORECASE | re.VERBOSE,
)


class ForwardError(Exception):
    """A forward could not be set up, with a message worth showing."""


def parse_spec(spec: str) -> dict:
    """Parse ``8080:db:5432``, ``127.0.0.1:8080:db:5432`` or ``R:9000:x:22``.

    The same shapes ssh itself takes, so a line can be copied straight out of
    a runbook.
    """
    match = _SPEC_RE.match(spec or "")
    if not match:
        raise ForwardError(
            f"'{spec}' is not a port forward. Use listen:host:port, for "
            f"example 8080:127.0.0.1:80, or R:9000:127.0.0.1:22 for a reverse "
            f"forward."
        )
    kind = (match.group("kind") or "L").upper()
    listen_port = int(match.group("listen_port"))
    dest_port = int(match.group("dest_port"))
    for port in (listen_port, dest_port):
        if not 1 <= port <= 65535:
            raise ForwardError(f"{port} is not a usable port number.")
    return {
        "type": REMOTE if kind == "R" else LOCAL,
        # Local forwards bind loopback unless told otherwise: opening a
        # customer's database to the whole office network by default would be
        # a poor surprise.
        "listen_host": match.group("listen_host") or "127.0.0.1",
        "listen_port": listen_port,
        "dest_host": match.group("dest_host"),
        "dest_port": dest_port,
    }


def describe(forward: dict) -> str:
    """One line describing what a forward does, for a tooltip or a list."""
    listen = f"{forward.get('listen_host', '127.0.0.1')}:{forward['listen_port']}"
    dest = f"{forward['dest_host']}:{forward['dest_port']}"
    if forward.get("type") == REMOTE:
        return f"remote {listen} → {dest} (opened on the server)"
    return f"local {listen} → {dest} (through this session)"


def _pump(source, sink, stop: threading.Event):
    """Copy bytes one way until either end closes."""
    try:
        while not stop.is_set():
            readable, _, _ = select.select([source], [], [], _PUMP_TIMEOUT)
            if source not in readable:
                continue
            data = source.recv(_CHUNK)
            if not data:
                break
            sink.sendall(data)
    except Exception:
        pass
    finally:
        for endpoint in (source, sink):
            try:
                endpoint.close()
            except Exception:
                pass


class LocalForward:
    """Listen locally; each connection becomes a channel through the session."""

    def __init__(self, transport, listen_host, listen_port, dest_host, dest_port):
        self.transport = transport
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.dest_host = dest_host
        self.dest_port = dest_port
        self._server = None
        self._thread = None
        self._stop = threading.Event()
        self.error = ""

    @property
    def bound_port(self) -> int:
        """The port actually bound — meaningful when 0 was requested."""
        if self._server is None:
            return self.listen_port
        try:
            return self._server.getsockname()[1]
        except Exception:
            return self.listen_port

    def start(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind((self.listen_host, self.listen_port))
        except OSError as exc:
            server.close()
            raise ForwardError(
                f"Could not listen on {self.listen_host}:{self.listen_port} "
                f"— {exc.strerror or exc}."
            ) from exc
        server.listen(16)
        server.settimeout(0.5)
        self._server = server
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,),
                             daemon=True).start()

    def _handle(self, client):
        try:
            channel = self.transport.open_channel(
                "direct-tcpip",
                (self.dest_host, self.dest_port),
                client.getpeername(),
            )
        except Exception as exc:
            self.error = str(exc)
            try:
                client.close()
            except Exception:
                pass
            return
        if channel is None:
            try:
                client.close()
            except Exception:
                pass
            return
        threading.Thread(target=_pump, args=(client, channel, self._stop),
                         daemon=True).start()
        threading.Thread(target=_pump, args=(channel, client, self._stop),
                         daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None


class RemoteForward:
    """Ask the server to listen, and carry what arrives back to a local host."""

    def __init__(self, transport, listen_host, listen_port, dest_host, dest_port):
        self.transport = transport
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.dest_host = dest_host
        self.dest_port = dest_port
        self._stop = threading.Event()
        self.bound_port = listen_port
        self.error = ""

    def start(self):
        try:
            self.bound_port = self.transport.request_port_forward(
                self.listen_host, self.listen_port, handler=self._handle)
        except Exception as exc:
            raise ForwardError(
                f"The server refused to listen on {self.listen_host}:"
                f"{self.listen_port} — {exc}."
            ) from exc
        return self

    def _handle(self, channel, origin, destination):
        try:
            target = socket.create_connection((self.dest_host, self.dest_port), 10)
        except Exception as exc:
            self.error = str(exc)
            try:
                channel.close()
            except Exception:
                pass
            return
        threading.Thread(target=_pump, args=(channel, target, self._stop),
                         daemon=True).start()
        threading.Thread(target=_pump, args=(target, channel, self._stop),
                         daemon=True).start()

    def stop(self):
        self._stop.set()
        try:
            self.transport.cancel_port_forward(self.listen_host, self.bound_port)
        except Exception:
            pass


class ForwardManager:
    """Every forward belonging to one session, brought up and torn down together."""

    def __init__(self, transport):
        self.transport = transport
        self.active: list = []
        self.failures: list = []

    def start_all(self, forwards) -> list:
        """Start each forward, collecting failures instead of raising.

        One port already in use must not cost the session; the terminal is
        what the user came for.
        """
        for forward in forwards or []:
            try:
                self.start(forward)
            except ForwardError as exc:
                self.failures.append((forward, str(exc)))
        return self.active

    def start(self, forward: dict):
        if isinstance(forward, str):
            forward = parse_spec(forward)
        kind = forward.get("type", LOCAL)
        args = (
            self.transport,
            forward.get("listen_host") or "127.0.0.1",
            int(forward["listen_port"]),
            forward["dest_host"],
            int(forward["dest_port"]),
        )
        runner = RemoteForward(*args) if kind == REMOTE else LocalForward(*args)
        runner.start()
        self.active.append(runner)
        return runner

    def stop_all(self):
        for runner in self.active:
            try:
                runner.stop()
            except Exception:
                pass
        self.active = []
