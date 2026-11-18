"""
Telnet and serial-console transports for the terminal.

The terminal was written against a paramiko channel. These classes look
enough like one — ``recv``, ``sendall``, ``settimeout``, ``resize_pty``,
``close``, ``closed`` — that the same emulator, search, recording and
broadcast work for a switch's console port or an old telnet device.

* **Telnet** speaks just enough of RFC 854 to be a good citizen: it agrees to
  echo and suppress-go-ahead, reports the terminal type and window size
  (NAWS), refuses everything else, and can answer the login prompts with the
  saved username and password.
* **Serial** opens a COM port through Win32 (pywin32 is already bundled for
  the RDP code) with the line settings saved on the connection.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

# Telnet bytes.
IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
OPT_ECHO, OPT_SGA, OPT_TTYPE, OPT_NAWS = 1, 3, 24, 31
TTYPE_IS, TTYPE_SEND = 0, 1

TERM_TYPE = b"xterm-256color"

BAUD_RATES = (300, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400)
PARITIES = ("none", "even", "odd", "mark", "space")
FLOW_CONTROLS = ("none", "rts/cts", "xon/xoff")


class StreamClient:
    """Stands in for paramiko.SSHClient: one channel, no transport."""

    def __init__(self, channel):
        self.channel = channel

    def invoke_shell(self, term="xterm-256color", width=80, height=24):
        self.channel.resize_pty(width, height)
        return self.channel

    def get_transport(self):
        return None

    def close(self):
        self.channel.close()


class TelnetChannel:
    def __init__(self, host: str, port: int = 23, timeout: float = 15.0,
                 username: str = "", password: str = ""):
        self._sock = socket.create_connection((host, int(port or 23)), timeout=timeout)
        self._sock.settimeout(0.05)
        self.closed = False
        self._lock = threading.Lock()
        self._size = (80, 24)
        self._naws_agreed = False
        self._pending = b""
        # Auto-login: answer the first login/password prompts, then stop.
        self._login = [("login", username), ("password", password)] if username else []
        self._login_deadline = time.monotonic() + 20.0
        self._tail = ""

    # ── paramiko-shaped API ───────────────────────────────────────

    def settimeout(self, timeout):
        self._sock.settimeout(timeout)

    def recv(self, size: int) -> bytes:
        if self.closed:
            return b""
        try:
            raw = self._sock.recv(size)
        except socket.timeout:
            raise
        except OSError:
            self.closed = True
            return b""
        if not raw:
            self.closed = True
            return b""
        data = self._strip_commands(self._pending + raw)
        if data:
            self._maybe_login(data)
            return data
        # Only negotiation arrived: that is not end-of-file.
        raise socket.timeout()

    def sendall(self, data: bytes):
        if isinstance(data, str):
            data = data.encode("utf-8")
        data = data.replace(bytes([IAC]), bytes([IAC, IAC]))
        # NVT: a bare CR must be followed by NUL (or LF).
        out = bytearray()
        for i, byte in enumerate(data):
            out.append(byte)
            if byte == 13 and (i + 1 >= len(data) or data[i + 1] != 10):
                out.append(0)
        with self._lock:
            self._sock.sendall(bytes(out))

    def resize_pty(self, width=80, height=24, *_args):
        self._size = (int(width or 80), int(height or 24))
        if self._naws_agreed:
            self._send_naws()

    def close(self):
        self.closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    # ── negotiation ───────────────────────────────────────────────

    def _send(self, *parts: int):
        with self._lock:
            try:
                self._sock.sendall(bytes(parts))
            except OSError:
                pass

    def _send_naws(self):
        width, height = self._size
        payload = struct.pack(">HH", max(1, width), max(1, height))
        payload = payload.replace(bytes([IAC]), bytes([IAC, IAC]))
        with self._lock:
            try:
                self._sock.sendall(bytes([IAC, SB, OPT_NAWS]) + payload + bytes([IAC, SE]))
            except OSError:
                pass

    def _strip_commands(self, raw: bytes) -> bytes:
        out = bytearray()
        i = 0
        n = len(raw)
        while i < n:
            byte = raw[i]
            if byte != IAC:
                out.append(byte)
                i += 1
                continue
            if i + 1 >= n:
                self._pending = raw[i:]
                return bytes(out)
            cmd = raw[i + 1]
            if cmd == IAC:
                out.append(IAC)
                i += 2
                continue
            if cmd in (DO, DONT, WILL, WONT):
                if i + 2 >= n:
                    self._pending = raw[i:]
                    return bytes(out)
                self._negotiate(cmd, raw[i + 2])
                i += 3
                continue
            if cmd == SB:
                end = raw.find(bytes([IAC, SE]), i + 2)
                if end < 0:
                    self._pending = raw[i:]
                    return bytes(out)
                self._subnegotiate(raw[i + 2:end])
                i = end + 2
                continue
            i += 2  # NOP, GA, etc.
        self._pending = b""
        return bytes(out)

    def _negotiate(self, cmd: int, opt: int):
        if cmd == DO:
            if opt == OPT_TTYPE:
                self._send(IAC, WILL, OPT_TTYPE)
            elif opt == OPT_NAWS:
                self._send(IAC, WILL, OPT_NAWS)
                self._naws_agreed = True
                self._send_naws()
            elif opt == OPT_SGA:
                self._send(IAC, WILL, OPT_SGA)
            else:
                self._send(IAC, WONT, opt)
        elif cmd == WILL:
            if opt in (OPT_ECHO, OPT_SGA):
                self._send(IAC, DO, opt)
            else:
                self._send(IAC, DONT, opt)
        # DONT / WONT: nothing to agree to.

    def _subnegotiate(self, body: bytes):
        if len(body) >= 2 and body[0] == OPT_TTYPE and body[1] == TTYPE_SEND:
            with self._lock:
                try:
                    self._sock.sendall(bytes([IAC, SB, OPT_TTYPE, TTYPE_IS]) + TERM_TYPE
                                       + bytes([IAC, SE]))
                except OSError:
                    pass

    def _maybe_login(self, data: bytes):
        if not self._login or time.monotonic() > self._login_deadline:
            self._login = []
            return
        self._tail = (self._tail + data.decode("utf-8", "replace"))[-200:].lower()
        stripped = self._tail.rstrip()
        prompt, value = self._login[0]
        words = ("login:", "username:", "user name:") if prompt == "login" else ("password:",)
        if any(stripped.endswith(w) for w in words):
            self._login.pop(0)
            self._tail = ""
            if value:
                self.sendall((value + "\r").encode("utf-8"))


def list_serial_ports() -> list[str]:
    """COM ports Windows knows about right now (from the device map)."""
    try:
        import winreg
    except ImportError:
        return []
    ports = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            index = 0
            while True:
                try:
                    _name, value, _type = winreg.EnumValue(key, index)
                except OSError:
                    break
                ports.append(str(value))
                index += 1
    except OSError:
        return []

    def sort_key(name):
        digits = "".join(ch for ch in name if ch.isdigit())
        return int(digits) if digits else 0
    return sorted(set(ports), key=sort_key)


class SerialChannel:
    def __init__(self, port: str, baud: int = 9600, data_bits: int = 8,
                 parity: str = "none", stop_bits: float = 1, flow: str = "none"):
        import win32con  # noqa: F401  (pywin32 present for RDP)
        import win32file

        self._win32file = win32file
        name = str(port or "").strip().upper()
        if not name:
            raise OSError("No serial port chosen")
        path = name if name.startswith("\\\\.\\") else "\\\\.\\" + name
        self._handle = win32file.CreateFile(
            path, win32con.GENERIC_READ | win32con.GENERIC_WRITE, 0, None,
            win32con.OPEN_EXISTING, 0, None)
        self.closed = False
        dcb = win32file.GetCommState(self._handle)
        dcb.BaudRate = int(baud or 9600)
        dcb.ByteSize = int(data_bits or 8)
        dcb.Parity = {"none": 0, "odd": 1, "even": 2, "mark": 3, "space": 4}.get(
            str(parity or "none").lower(), 0)
        dcb.StopBits = {1: 0, 1.5: 1, 2: 2}.get(float(stop_bits or 1), 0)
        flow = str(flow or "none").lower()
        dcb.fOutxCtsFlow = flow == "rts/cts"
        dcb.fRtsControl = 2 if flow == "rts/cts" else 1     # handshake / enable
        dcb.fOutX = dcb.fInX = flow == "xon/xoff"
        dcb.fDtrControl = 1                                 # DTR on
        dcb.fBinary = True
        win32file.SetCommState(self._handle, dcb)
        # Return as soon as anything arrives, or after 50 ms of silence.
        maxdword = 0xFFFFFFFF
        win32file.SetCommTimeouts(self._handle, (maxdword, maxdword, 50, 0, 2000))

    def settimeout(self, _timeout):
        pass

    def recv(self, size: int) -> bytes:
        if self.closed:
            return b""
        try:
            _hr, data = self._win32file.ReadFile(self._handle, min(size, 4096))
        except Exception:
            self.closed = True
            return b""      # unplugged
        if not data:
            raise socket.timeout()
        return bytes(data)

    def sendall(self, data: bytes):
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not self.closed:
            self._win32file.WriteFile(self._handle, data)

    def resize_pty(self, *_args):
        pass  # a serial console has no idea how big the window is

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._win32file.CloseHandle(self._handle)
        except Exception:
            pass


def describe_serial_error(exc: Exception, port: str) -> str:
    """Plain words for the ways opening a COM port fails."""
    code = getattr(exc, "winerror", None)
    if code is None and getattr(exc, "args", None) and isinstance(exc.args[0], int):
        code = exc.args[0]
    port = port or "The port"
    if code == 2:
        return (f"{port} does not exist. Check the adapter is plugged in, then look in "
                "Device Manager › Ports (COM & LPT) for its number.")
    if code == 5:
        return (f"{port} is in use by another program — PuTTY, a vendor tool, or "
                "another RPMote tab.")
    if code == 31:
        return (f"{port} is there but not working; the adapter's driver may need "
                "reinstalling.")
    if code == 87:
        return f"{port} refused those line settings (try 9600 8N1)."
    text = getattr(exc, "strerror", None) or str(exc)
    return f"{port}: {text}"


def open_stream_client(conn: dict) -> StreamClient:
    """A connected client for a telnet or serial connection record."""
    protocol = str(conn.get("protocol", "telnet") or "telnet").lower()
    if protocol == "serial":
        channel = SerialChannel(
            conn.get("serial_port") or conn.get("host", ""),
            baud=int(conn.get("baud_rate", 9600) or 9600),
            data_bits=int(conn.get("data_bits", 8) or 8),
            parity=conn.get("parity", "none"),
            stop_bits=float(conn.get("stop_bits", 1) or 1),
            flow=conn.get("flow_control", "none"))
    else:
        channel = TelnetChannel(conn.get("host", ""), int(conn.get("port", 23) or 23),
                                username=str(conn.get("username", "") or ""),
                                password=str(conn.get("password", "") or ""))
    return StreamClient(channel)
