"""
Embedded VNC viewer — RFB 3.3 / 3.7 / 3.8 in pure Python.

Covers what MSP work actually meets: Linux desktops, Macs with Screen
Sharing's "VNC viewers may control screen with password", TightVNC /
UltraVNC / TigerVNC / RealVNC servers, Proxmox and ESXi consoles behind a
VNC port.

* Security: None and VNC Authentication (the 8-character DES password).
* Encodings: ZRLE, Hextile, CopyRect, Raw, and the DesktopSize pseudo-
  encoding so a resolution change re-sizes the view.
* Input: mouse (buttons, wheel), keyboard as X11 keysyms, Ctrl+Alt+Del, and
  sending the local clipboard as typed text or as a cut-text message.

The protocol runs on a worker thread; it decodes straight into a 32-bit
framebuffer the widget paints from, guarded by a lock.
"""

from __future__ import annotations

import socket
import struct
import threading
import zlib

from PyQt6.QtCore import QPointF, QRect, QRectF, QSize, Qt, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
    QWidget,
)

from app import icons, net_errors
from app.failure_panel import FailurePanel
from app.widgets import ElidedLabel, set_variant

ENC_RAW, ENC_COPYRECT, ENC_HEXTILE, ENC_ZRLE = 0, 1, 5, 16
ENC_DESKTOP_SIZE = -223
SEC_INVALID, SEC_NONE, SEC_VNC = 0, 1, 2


class VncError(Exception):
    def __init__(self, message: str, kind: str = net_errors.OTHER):
        super().__init__(message)
        self.kind = kind


# ── DES for VNC authentication ──────────────────────────────────────────

def _vnc_key(password: str) -> bytes:
    """VNC's quirk: the 8-byte DES key is the password with each byte's bits
    reversed, zero-padded or truncated to 8 bytes."""
    raw = (password or "").encode("latin-1", "replace")[:8].ljust(8, b"\0")
    return bytes(int(f"{b:08b}"[::-1], 2) for b in raw)


def vnc_auth_response(password: str, challenge: bytes) -> bytes:
    try:
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    except ImportError:   # older cryptography
        from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
    key = _vnc_key(password)
    # Triple DES with K1=K2=K3 *is* single DES.
    encryptor = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
    return encryptor.update(challenge) + encryptor.finalize()


# ── keysyms ─────────────────────────────────────────────────────────────

_K = Qt.Key
_KEYSYMS = {k.value: sym for k, sym in {
    _K.Key_Backspace: 0xFF08, _K.Key_Tab: 0xFF09, _K.Key_Backtab: 0xFF09,
    _K.Key_Return: 0xFF0D, _K.Key_Enter: 0xFF8D, _K.Key_Escape: 0xFF1B,
    _K.Key_Insert: 0xFF63, _K.Key_Delete: 0xFFFF, _K.Key_Home: 0xFF50,
    _K.Key_End: 0xFF57, _K.Key_PageUp: 0xFF55, _K.Key_PageDown: 0xFF56,
    _K.Key_Left: 0xFF51, _K.Key_Up: 0xFF52, _K.Key_Right: 0xFF53,
    _K.Key_Down: 0xFF54, _K.Key_Shift: 0xFFE1, _K.Key_Control: 0xFFE3,
    _K.Key_Alt: 0xFFE9, _K.Key_Meta: 0xFFEB, _K.Key_CapsLock: 0xFFE5,
    _K.Key_Print: 0xFF61, _K.Key_Pause: 0xFF13, _K.Key_Menu: 0xFF67,
    _K.Key_NumLock: 0xFF7F, _K.Key_ScrollLock: 0xFF14, _K.Key_Space: 0x0020,
}.items()}
for _i in range(12):
    _KEYSYMS[_K.Key_F1.value + _i] = 0xFFBE + _i

KEY_CTRL, KEY_ALT, KEY_DELETE = 0xFFE3, 0xFFE9, 0xFFFF


def keysym_for(event) -> int | None:
    """The X11 keysym VNC expects for a Qt key event, or None to ignore it."""
    key = event.key()
    key = key.value if isinstance(key, Qt.Key) else int(key)
    if key in _KEYSYMS:
        return _KEYSYMS[key]
    text = event.text()
    if text and text.isprintable():
        code = ord(text[0])
        return code if code < 0x100 else 0x01000000 + code
    # With Ctrl held Qt gives a control character; send the letter instead
    # (the Ctrl key itself went over as its own press).
    if _K.Key_A.value <= key <= _K.Key_Z.value:
        base = ord("a") + (key - _K.Key_A.value)
        shifted = event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        return base - 32 if shifted else base
    if _K.Key_0.value <= key <= _K.Key_9.value:
        return ord("0") + (key - _K.Key_0.value)
    return None


# ── protocol worker ─────────────────────────────────────────────────────

class _Reader:
    def __init__(self, sock: socket.socket):
        self._file = sock.makefile("rb", buffering=1 << 16)

    def read(self, n: int) -> bytes:
        data = self._file.read(n)
        if data is None or len(data) < n:
            raise ConnectionError("connection closed")
        return data

    def u8(self) -> int:
        return self.read(1)[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.read(2))[0]

    def u32(self) -> int:
        return struct.unpack(">I", self.read(4))[0]

    def s32(self) -> int:
        return struct.unpack(">i", self.read(4))[0]


class _Bytes:
    """Cursor over decompressed ZRLE data."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u8(self) -> int:
        b = self.data[self.pos]
        self.pos += 1
        return b

    def run_length(self) -> int:
        total = 1
        while True:
            b = self.data[self.pos]
            self.pos += 1
            total += b
            if b != 255:
                return total


def _cpixel(c3: bytes) -> bytes:
    return c3 + b"\xff"


def _expand_cpixels(raw: bytes, count: int) -> bytearray:
    out = bytearray(count * 4)
    out[0::4] = raw[0::3]
    out[1::4] = raw[1::3]
    out[2::4] = raw[2::3]
    out[3::4] = b"\xff" * count
    return out


class VncSession(QObject):
    """Talks RFB on a thread; owns the framebuffer."""

    connected = pyqtSignal(str, int, int)        # desktop name, width, height
    resized = pyqtSignal(int, int)
    updated = pyqtSignal(QRect)
    failed = pyqtSignal(str)
    closed = pyqtSignal(str)
    bell = pyqtSignal()
    clipboard = pyqtSignal(str)

    def __init__(self, host: str, port: int, password: str = "", shared: bool = True,
                 parent=None):
        super().__init__(parent)
        self.host, self.port = host, int(port or 5900)
        self.password = password or ""
        self.shared = shared
        self.width = self.height = 0
        self.framebuffer = bytearray()
        self.lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._running = False
        self._zlib = zlib.decompressobj()
        self._thread: threading.Thread | None = None
        #: What kind of failure the last ``failed`` was (see app.net_errors).
        self.failure_kind = net_errors.OTHER

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, name="rpmote-vnc", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _fail(self, kind: str, message: str):
        self.failure_kind = kind
        if self._running:
            self.failed.emit(message)

    def _run(self):
        connected = False
        try:
            try:
                sock = socket.create_connection((self.host, self.port), timeout=15)
            except OSError as exc:
                self._fail(*net_errors.describe_failure(exc, self.host, self.port))
                return
            if not self._running:          # stopped while connecting
                sock.close()
                return
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sock = sock
            reader = _Reader(sock)
            name = self._handshake(sock, reader)
            connected = True
            self.connected.emit(name, self.width, self.height)
            self._request_update(incremental=False)
            while self._running:
                kind = reader.u8()
                if kind == 0:
                    self._framebuffer_update(reader)
                    self._request_update(incremental=True)
                elif kind == 1:
                    reader.read(1)
                    _first = reader.u16()
                    count = reader.u16()
                    reader.read(count * 6)
                elif kind == 2:
                    self.bell.emit()
                elif kind == 3:
                    reader.read(3)
                    length = reader.u32()
                    text = reader.read(length).decode("latin-1", "replace")
                    self.clipboard.emit(text)
                else:
                    raise VncError(f"unexpected server message {kind}")
        except VncError as exc:
            self._fail(exc.kind, str(exc))
        except (OSError, ConnectionError) as exc:
            if not connected:
                self._fail(net_errors.PROTOCOL,
                           f"The server hung up during the VNC handshake ({exc}). Some "
                           "servers only accept a set list of viewers, or one viewer at a time.")
            elif self._running:
                self.closed.emit(str(exc) or "connection closed")
        except Exception as exc:     # a decoding bug must not hang the tab
            self._fail(net_errors.OTHER, f"{exc.__class__.__name__}: {exc}")
        finally:
            self._running = False

    # ── handshake ─────────────────────────────────────────────────

    def _send(self, data: bytes):
        sock = self._sock
        if sock is None:
            return
        with self._send_lock:
            try:
                sock.sendall(data)
            except OSError:
                pass

    def _handshake(self, sock, reader: _Reader) -> str:
        banner = reader.read(12)
        if not banner.startswith(b"RFB "):
            raise VncError("Something answered on that port, but it does not speak VNC "
                           "(no RFB banner). Check the port — VNC is usually 5900.",
                           net_errors.PROTOCOL)
        try:
            major, minor = int(banner[4:7]), int(banner[8:11])
        except ValueError:
            raise VncError("Unreadable RFB version from the server.", net_errors.PROTOCOL)
        if major > 3 or minor >= 8:
            version = 8
        elif minor >= 7:
            version = 7
        else:
            version = 3
        sock.sendall(b"RFB 003.%03d\n" % version)

        if version == 3:
            types = [reader.u32()]
        else:
            count = reader.u8()
            if count == 0:
                raise VncError(self._reason(reader))
            types = list(reader.read(count))
        if SEC_INVALID in types and len(types) == 1:
            raise VncError(self._reason(reader))
        if SEC_VNC in types and (self.password or SEC_NONE not in types):
            chosen = SEC_VNC
        elif SEC_NONE in types:
            chosen = SEC_NONE
        else:
            raise VncError("The server wants an authentication method RPMote does not "
                           f"support (types {types}). Enable classic VNC password "
                           "authentication on the server.")
        if version >= 7:
            sock.sendall(bytes([chosen]))
        if chosen == SEC_VNC:
            if not self.password:
                raise VncError("The server asks for a VNC password; save one on the connection.",
                               net_errors.AUTH)
            challenge = reader.read(16)
            sock.sendall(vnc_auth_response(self.password, challenge))
        if chosen == SEC_VNC or version >= 8:
            result = reader.u32()
            if result != 0:
                reason = self._reason(reader) if version >= 8 else ""
                raise VncError("The VNC password was not accepted." +
                               (f" ({reason})" if reason else ""), net_errors.AUTH)

        sock.sendall(bytes([1 if self.shared else 0]))
        self.width, self.height = reader.u16(), reader.u16()
        reader.read(16)   # server's pixel format — we set our own
        name = reader.read(reader.u32()).decode("utf-8", "replace")
        with self.lock:
            self.framebuffer = bytearray(b"\x00\x00\x00\xff" * (self.width * self.height))

        # 32 bpp, depth 24, little endian, true colour, BGRX in memory —
        # which is exactly QImage.Format_RGB32 on Windows.
        pixel_format = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        sock.sendall(b"\x00\x00\x00\x00" + pixel_format)
        encodings = [ENC_ZRLE, ENC_HEXTILE, ENC_COPYRECT, ENC_RAW, ENC_DESKTOP_SIZE]
        sock.sendall(struct.pack(">BxH", 2, len(encodings)) +
                     b"".join(struct.pack(">i", e) for e in encodings))
        return name

    @staticmethod
    def _reason(reader: _Reader) -> str:
        try:
            return reader.read(reader.u32()).decode("utf-8", "replace")
        except Exception:
            return "the server refused the connection"

    def _request_update(self, incremental: bool):
        self._send(struct.pack(">BBHHHH", 3, 1 if incremental else 0, 0, 0,
                               self.width, self.height))

    # ── framebuffer updates ───────────────────────────────────────

    def _framebuffer_update(self, reader: _Reader):
        reader.read(1)
        count = reader.u16()
        dirty = QRect()
        for _ in range(count):
            x, y, w, h = reader.u16(), reader.u16(), reader.u16(), reader.u16()
            encoding = reader.s32()
            if encoding == ENC_DESKTOP_SIZE:
                with self.lock:
                    self.width, self.height = w, h
                    self.framebuffer = bytearray(b"\x00\x00\x00\xff" * (w * h))
                self.resized.emit(w, h)
                dirty = QRect(0, 0, w, h)
                continue
            if w == 0 or h == 0:
                continue
            if encoding == ENC_RAW:
                data = reader.read(w * h * 4)
                with self.lock:
                    self._blit(x, y, w, h, bytearray(data), force_alpha=True)
            elif encoding == ENC_COPYRECT:
                sx, sy = reader.u16(), reader.u16()
                with self.lock:
                    self._copy(sx, sy, x, y, w, h)
            elif encoding == ENC_HEXTILE:
                self._hextile(reader, x, y, w, h)
            elif encoding == ENC_ZRLE:
                length = reader.u32()
                data = self._zlib.decompress(reader.read(length))
                with self.lock:
                    self._zrle(_Bytes(data), x, y, w, h)
            else:
                raise VncError(f"server sent unsupported encoding {encoding}")
            dirty = dirty.united(QRect(x, y, w, h))
        if not dirty.isNull():
            self.updated.emit(dirty)

    def _blit(self, x, y, w, h, pixels: bytearray, force_alpha=False):
        if force_alpha:
            pixels[3::4] = b"\xff" * (w * h)
        fb, stride, row = self.framebuffer, self.width * 4, w * 4
        if x + w > self.width or y + h > self.height:
            return
        for r in range(h):
            off = (y + r) * stride + x * 4
            fb[off:off + row] = pixels[r * row:(r + 1) * row]

    def _fill(self, x, y, w, h, pixel4: bytes):
        fb, stride = self.framebuffer, self.width * 4
        if x + w > self.width or y + h > self.height:
            return
        line = pixel4 * w
        for r in range(h):
            off = (y + r) * stride + x * 4
            fb[off:off + w * 4] = line

    def _copy(self, sx, sy, x, y, w, h):
        fb, stride, row = self.framebuffer, self.width * 4, w * 4
        if max(sx, x) + w > self.width or max(sy, y) + h > self.height:
            return
        rows = [bytes(fb[(sy + r) * stride + sx * 4:(sy + r) * stride + sx * 4 + row])
                for r in range(h)]
        for r, data in enumerate(rows):
            off = (y + r) * stride + x * 4
            fb[off:off + row] = data

    def _hextile(self, reader: _Reader, x, y, w, h):
        bg = fg = b"\x00\x00\x00\xff"
        for ty in range(y, y + h, 16):
            th = min(16, y + h - ty)
            for tx in range(x, x + w, 16):
                tw = min(16, x + w - tx)
                sub = reader.u8()
                if sub & 1:
                    data = bytearray(reader.read(tw * th * 4))
                    with self.lock:
                        self._blit(tx, ty, tw, th, data, force_alpha=True)
                    continue
                if sub & 2:
                    bg = reader.read(3) + b"\xff"
                    reader.read(1)
                if sub & 4:
                    fg = reader.read(3) + b"\xff"
                    reader.read(1)
                rects = []
                if sub & 8:
                    for _ in range(reader.u8()):
                        if sub & 16:
                            color = reader.read(3) + b"\xff"
                            reader.read(1)
                        else:
                            color = fg
                        xy, wh = reader.u8(), reader.u8()
                        rects.append((tx + (xy >> 4), ty + (xy & 15),
                                      (wh >> 4) + 1, (wh & 15) + 1, color))
                with self.lock:
                    self._fill(tx, ty, tw, th, bg)
                    for rx, ry, rw, rh, color in rects:
                        self._fill(rx, ry, rw, rh, color)

    def _zrle(self, data: _Bytes, x, y, w, h):
        for ty in range(y, y + h, 64):
            th = min(64, y + h - ty)
            for tx in range(x, x + w, 64):
                tw = min(64, x + w - tx)
                n = tw * th
                sub = data.u8()
                if sub == 0:
                    self._blit(tx, ty, tw, th, _expand_cpixels(data.take(n * 3), n))
                elif sub == 1:
                    self._fill(tx, ty, tw, th, _cpixel(data.take(3)))
                elif 2 <= sub <= 16:
                    palette = [_cpixel(data.take(3)) for _ in range(sub)]
                    bits = 1 if sub == 2 else 2 if sub <= 4 else 4
                    mask = (1 << bits) - 1
                    per_byte = 8 // bits
                    row_bytes = (tw * bits + 7) // 8
                    expanded: dict[int, bytes] = {}   # packed byte -> its pixels
                    out = bytearray()
                    for _r in range(th):
                        row = bytearray()
                        for byte in data.take(row_bytes):
                            pixels = expanded.get(byte)
                            if pixels is None:
                                pixels = expanded[byte] = b"".join(
                                    palette[min(len(palette) - 1,
                                                (byte >> (8 - bits * (k + 1))) & mask)]
                                    for k in range(per_byte))
                            row += pixels
                        out += row[:tw * 4]
                    self._blit(tx, ty, tw, th, out)
                elif sub == 128:
                    out = bytearray()
                    while len(out) < n * 4:
                        pixel = _cpixel(data.take(3))
                        out += pixel * data.run_length()
                    self._blit(tx, ty, tw, th, out[:n * 4])
                elif sub >= 130:
                    palette = [_cpixel(data.take(3)) for _ in range(sub - 128)]
                    out = bytearray()
                    while len(out) < n * 4:
                        index = data.u8()
                        if index & 0x80:
                            out += palette[index & 0x7F] * data.run_length()
                        else:
                            out += palette[index]
                    self._blit(tx, ty, tw, th, out[:n * 4])
                else:
                    raise VncError(f"bad ZRLE sub-encoding {sub}")

    # ── input ─────────────────────────────────────────────────────

    def pointer(self, x: int, y: int, buttons: int):
        x = max(0, min(self.width - 1, int(x)))
        y = max(0, min(self.height - 1, int(y)))
        self._send(struct.pack(">BBHH", 5, buttons & 0xFF, x, y))

    def key(self, keysym: int, down: bool):
        self._send(struct.pack(">BBxxI", 4, 1 if down else 0, keysym & 0xFFFFFFFF))

    def ctrl_alt_del(self):
        for sym in (KEY_CTRL, KEY_ALT, KEY_DELETE):
            self.key(sym, True)
        for sym in (KEY_DELETE, KEY_ALT, KEY_CTRL):
            self.key(sym, False)

    def type_text(self, text: str):
        for ch in text:
            sym = 0xFF0D if ch == "\n" else (ord(ch) if ord(ch) < 0x100 else 0x01000000 + ord(ch))
            self.key(sym, True)
            self.key(sym, False)

    def send_clipboard(self, text: str):
        data = text.encode("latin-1", "replace")
        self._send(struct.pack(">BxxxI", 6, len(data)) + data)


# ── widgets ─────────────────────────────────────────────────────────────

class _VncCanvas(QWidget):
    def __init__(self, session: VncSession, parent=None):
        super().__init__(parent)
        self.session = session
        self.fit = True
        self.placeholder = "Connecting…"
        self._buttons = 0
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def sizeHint(self):
        return QSize(max(640, self.session.width), max(400, self.session.height))

    def _geometry(self) -> tuple[QRectF, float]:
        fw, fh = self.session.width, self.session.height
        if not fw or not fh:
            return QRectF(), 1.0
        if not self.fit:
            return QRectF(0, 0, fw, fh), 1.0
        scale = min(self.width() / fw, self.height() / fh)
        w, h = fw * scale, fh * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h), scale

    def paintEvent(self, _event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(8, 9, 14))
        target, scale = self._geometry()
        if target.isNull():
            p.setPen(QColor("#94a3b8"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, scale < 1.0)
        with self.session.lock:
            image = QImage(bytes(self.session.framebuffer), self.session.width,
                           self.session.height, self.session.width * 4,
                           QImage.Format.Format_RGB32)
        p.drawImage(target, image)

    def _to_remote(self, pos) -> tuple[int, int]:
        target, scale = self._geometry()
        if target.isNull() or scale <= 0:
            return 0, 0
        return (int((pos.x() - target.x()) / scale), int((pos.y() - target.y()) / scale))

    @staticmethod
    def _mask(buttons) -> int:
        mask = 0
        if buttons & Qt.MouseButton.LeftButton:
            mask |= 1
        if buttons & Qt.MouseButton.MiddleButton:
            mask |= 2
        if buttons & Qt.MouseButton.RightButton:
            mask |= 4
        return mask

    def mousePressEvent(self, event):
        self.setFocus()
        self._buttons = self._mask(event.buttons())
        self.session.pointer(*self._to_remote(event.position()), self._buttons)

    def mouseReleaseEvent(self, event):
        self._buttons = self._mask(event.buttons())
        self.session.pointer(*self._to_remote(event.position()), self._buttons)

    def mouseMoveEvent(self, event):
        self.session.pointer(*self._to_remote(event.position()), self._mask(event.buttons()))

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if not delta:
            return
        x, y = self._to_remote(event.position())
        step = 8 if delta > 0 else 16   # buttons 4 / 5
        self.session.pointer(x, y, self._buttons | step)
        self.session.pointer(x, y, self._buttons)

    def event(self, event):
        if event.type() == event.Type.ShortcutOverride:
            from app.terminal_view import is_app_chord
            if not is_app_chord(event):
                event.accept()
                return True
        return super().event(event)

    def focusNextPrevChild(self, _next):
        return False   # Tab belongs to the remote desktop

    def keyPressEvent(self, event):
        sym = keysym_for(event)
        if sym is not None:
            self.session.key(sym, True)

    def keyReleaseEvent(self, event):
        if event.isAutoRepeat():
            return
        sym = keysym_for(event)
        if sym is not None:
            self.session.key(sym, False)


class VncViewerTab(QWidget):
    """A VNC session as a tab."""

    session_closed = pyqtSignal()
    #: The saved connection, from the failure panel's Edit button.
    edit_requested = pyqtSignal(object)

    def __init__(self, connection: dict, parent=None):
        super().__init__(parent)
        self.connection_info = connection
        self._connected = False
        self._closing = False
        host = str(connection.get("host", "") or "")
        port = int(connection.get("port", 5900) or 5900)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("vnc_toolbar")
        bar.setStyleSheet("#vnc_toolbar { border-bottom: 1px solid rgba(255,255,255,0.08); }")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 6, 10, 6)
        row.setSpacing(6)
        self.status_dot = QLabel("●")
        self.status_dot.setStyleSheet("color: #fbbf24;")
        row.addWidget(self.status_dot)
        self.title = ElidedLabel(f"{connection.get('name', host)} — {host}:{port}")
        self.title.setStyleSheet("font-weight: 600;")
        row.addWidget(self.title, 1)
        self.status = QLabel("Connecting…")
        self.status.setProperty("role", "hint")
        row.addWidget(self.status)

        self.btn_fit = QPushButton("Fit")
        self.btn_fit.setIcon(icons.icon("zoom-fit"))
        self.btn_fit.setCheckable(True)
        self.btn_fit.setChecked(True)
        self.btn_fit.setToolTip("Scale the desktop to the window (off: actual size, scrollable)")
        self.btn_fit.toggled.connect(self._set_fit)
        self.btn_cad = QPushButton("Ctrl+Alt+Del")
        self.btn_cad.setIcon(icons.icon("keyboard"))
        self.btn_cad.clicked.connect(lambda: self.session.ctrl_alt_del())
        self.btn_paste = QPushButton("Type clipboard")
        self.btn_paste.setIcon(icons.icon("copy"))
        self.btn_paste.setToolTip("Type the local clipboard's text into the remote desktop")
        self.btn_paste.clicked.connect(self._type_clipboard)
        self.btn_reconnect = QPushButton("Reconnect")
        self.btn_reconnect.setIcon(icons.icon("refresh"))
        self.btn_reconnect.clicked.connect(self.reconnect)
        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setIcon(icons.icon("x", "#ffffff"))
        set_variant(self.btn_disconnect, "danger")
        self.btn_disconnect.clicked.connect(self.disconnect)
        for b in (self.btn_fit, self.btn_cad, self.btn_paste, self.btn_reconnect,
                  self.btn_disconnect):
            row.addWidget(b)
        layout.addWidget(bar)

        self.failure_panel = FailurePanel(self)
        self.failure_panel.retry_requested.connect(self.reconnect)
        self.failure_panel.edit_requested.connect(
            lambda: self.edit_requested.emit(dict(self.connection_info)))
        layout.addWidget(self.failure_panel)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        layout.addWidget(self.scroll, 1)
        self._start()

    # ── session ───────────────────────────────────────────────────

    def _start(self):
        conn = self.connection_info
        # No Qt parent: the worker thread keeps the session alive until it
        # exits, and a dropped session must not be deleted under it.
        self.session = VncSession(str(conn.get("host", "") or ""),
                                  int(conn.get("port", 5900) or 5900),
                                  str(conn.get("password", "") or ""))
        self.canvas = _VncCanvas(self.session)
        self.scroll.setWidget(self.canvas)
        self.session.connected.connect(self._on_connected)
        self.session.resized.connect(self._on_resized)
        self.session.updated.connect(lambda _rect: self.canvas.update())
        self.session.failed.connect(self._on_failed)
        self.session.closed.connect(self._on_closed)
        self.session.clipboard.connect(lambda text: QGuiApplication.clipboard().setText(text))
        self.session.start()

    def _on_connected(self, name: str, width: int, height: int):
        self._connected = True
        self.failure_panel.hide()
        self.status_dot.setStyleSheet("color: #4ade80;")
        self.status.setText(f"{name or 'Desktop'} · {width}×{height}")
        self._set_fit(self.btn_fit.isChecked())
        self.canvas.setFocus()

    def _on_resized(self, width: int, height: int):
        self.status.setText(f"{width}×{height}")
        self._set_fit(self.btn_fit.isChecked())

    def _on_failed(self, message: str):
        self._connected = False
        self.status_dot.setStyleSheet("color: #f87171;")
        self.status.setText("Failed")
        self.status.setToolTip(message)
        self.canvas.placeholder = "Not connected"
        self.canvas.update()
        self.failure_panel.show_failure(self.connection_info, self.session.failure_kind, message)

    def _on_closed(self, message: str):
        self._connected = False
        self.status_dot.setStyleSheet("color: #f87171;")
        self.status.setText("Disconnected")
        self.failure_panel.show_failure(
            self.connection_info, net_errors.RESET,
            f"The VNC session ended ({message}). The server may have restarted, or "
            "another viewer took over.")

    def _set_fit(self, fit: bool):
        if not hasattr(self, "canvas") or self.scroll.widget() is not self.canvas:
            return
        self.canvas.fit = bool(fit)
        self.scroll.setWidgetResizable(bool(fit))
        if not fit:
            self.canvas.resize(max(1, self.session.width), max(1, self.session.height))
        self.canvas.update()

    def _type_clipboard(self):
        text = QGuiApplication.clipboard().text()
        if text:
            self.session.send_clipboard(text)
            self.session.type_text(text)

    def _drop_session(self):
        session = getattr(self, "session", None)
        if session is None:
            return
        for signal in (session.connected, session.resized, session.updated, session.failed,
                       session.closed, session.clipboard):
            try:
                signal.disconnect()
            except (TypeError, RuntimeError):
                pass
        session.stop()

    def is_connected(self) -> bool:
        return self._connected

    def apply_connection_update(self, conn: dict):
        """Take the edited connection (from the failure panel) and try again."""
        self.connection_info.clear()
        self.connection_info.update(conn)
        host = str(conn.get("host", "") or "")
        self.title.setText(f"{conn.get('name', host)} — {host}:{conn.get('port', 5900)}")
        self.reconnect()

    def reconnect(self):
        self._drop_session()
        self.failure_panel.hide()
        self._connected = False
        self._closing = False
        self.status_dot.setStyleSheet("color: #fbbf24;")
        self.status.setText("Connecting…")
        self.status.setToolTip("")
        self._start()

    def disconnect(self):
        self.cleanup()
        self.session_closed.emit()

    def cleanup(self):
        if self._closing:
            return
        self._closing = True
        self._connected = False
        self._drop_session()
