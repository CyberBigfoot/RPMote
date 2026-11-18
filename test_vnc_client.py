"""The embedded VNC viewer against a scripted RFB server.

The server is a socket on localhost that plays one handshake and one
framebuffer update per encoding, so the decoders are checked byte for byte
without a real desktop to connect to.
"""

import os
import socket
import struct
import sys
import threading
import time
import unittest
import zlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app import vnc_client
from app.vnc_client import VncSession, vnc_auth_response

APP = QApplication.instance() or QApplication(sys.argv)

W, H = 80, 70          # spans two 64-pixel ZRLE tiles and five hextile tiles


def bgrx(r, g, b):
    """One pixel in the format the client asks for (little-endian BGRX)."""
    return bytes((b, g, r, 0))


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeServer:
    """Accepts one client, runs *script(conn)* against it."""

    def __init__(self, script):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.script = script
        self.error = None
        self.received = {}
        #: Set by a test to let a script carry on to its next step.
        self.go_on = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        conn, _ = self.sock.accept()
        conn.settimeout(5)
        try:
            self.script(self, conn)
        except Exception as exc:     # surfaced by the test
            self.error = exc
        finally:
            time.sleep(0.3)
            conn.close()
            self.sock.close()

    @staticmethod
    def recv_exact(conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError("client went away")
            data += chunk
        return data

    def handshake(self, conn, version=b"RFB 003.008\n", security=(1,), password=None):
        conn.sendall(version)
        self.received["version"] = self.recv_exact(conn, 12)
        if version == b"RFB 003.003\n":
            conn.sendall(struct.pack(">I", security[0]))
            chosen = security[0]
        else:
            conn.sendall(bytes([len(security)]) + bytes(security))
            chosen = self.recv_exact(conn, 1)[0]
        self.received["security"] = chosen
        if chosen == 2:
            challenge = bytes(range(16))
            conn.sendall(challenge)
            response = self.recv_exact(conn, 16)
            ok = response == vnc_auth_response(password, challenge)
            self.received["auth_ok"] = ok
            if ok:
                conn.sendall(struct.pack(">I", 0))
            else:
                reason = b"Authentication failed"
                conn.sendall(struct.pack(">II", 1, len(reason)) + reason)
                return False
        elif version == b"RFB 003.008\n":
            conn.sendall(struct.pack(">I", 0))
        self.received["shared"] = self.recv_exact(conn, 1)
        name = b"test desktop"
        server_format = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        conn.sendall(struct.pack(">HH", W, H) + server_format + struct.pack(">I", len(name)) + name)
        self.received["set_pixel_format"] = self.recv_exact(conn, 20)
        header = self.recv_exact(conn, 4)
        count = struct.unpack(">H", header[2:])[0]
        self.received["encodings"] = struct.unpack(">%di" % count, self.recv_exact(conn, 4 * count))
        self.received["first_request"] = self.recv_exact(conn, 10)
        return True


def rect(x, y, w, h, encoding):
    return struct.pack(">HHHHi", x, y, w, h, encoding)


class VncDecoding(unittest.TestCase):
    def connect(self, script, password=""):
        server = FakeServer(script)
        session = VncSession("127.0.0.1", server.port, password)
        events = {"connected": None, "failed": None, "updates": 0}
        session.connected.connect(lambda name, w, h: events.__setitem__("connected", (name, w, h)))
        session.failed.connect(lambda message: events.__setitem__("failed", message))
        session.updated.connect(lambda _r: events.__setitem__("updates", events["updates"] + 1))
        session.start()
        self.addCleanup(session.stop)
        return server, session, events

    def pixel(self, session, x, y):
        off = (y * session.width + x) * 4
        with session.lock:
            return bytes(session.framebuffer[off:off + 4])

    def test_handshake_none_security_and_requested_format(self):
        def script(server, conn):
            server.handshake(conn)
        server, session, events = self.connect(script)
        self.assertTrue(wait_for(lambda: events["connected"]), events)
        server.thread.join(5)
        self.assertIsNone(server.error)
        self.assertEqual(events["connected"], ("test desktop", W, H))
        self.assertEqual(server.received["version"], b"RFB 003.008\n")
        self.assertEqual(server.received["security"], 1)
        pf = server.received["set_pixel_format"]
        self.assertEqual(pf[0], 0)                        # SetPixelFormat
        self.assertEqual(pf[4:8], bytes([32, 24, 0, 1]))  # 32bpp, depth 24, LE, true colour
        self.assertEqual(server.received["encodings"][0], vnc_client.ENC_ZRLE)
        self.assertIn(vnc_client.ENC_DESKTOP_SIZE, server.received["encodings"])
        self.assertEqual(server.received["first_request"][:2], bytes([3, 0]))  # full update

    def test_vnc_password_accepted_and_rejected(self):
        def script(server, conn):
            server.handshake(conn, security=(2,), password="s3cret")
        server, session, events = self.connect(script, password="s3cret")
        self.assertTrue(wait_for(lambda: events["connected"]), events)
        self.assertTrue(server.received.get("auth_ok"))

        def script_bad(server, conn):
            server.handshake(conn, security=(2,), password="other")
        server, session, events = self.connect(script_bad, password="s3cret")
        self.assertTrue(wait_for(lambda: events["failed"]), events)
        self.assertIn("not accepted", events["failed"])
        self.assertIn("Authentication failed", events["failed"])
        self.assertEqual(session.failure_kind, "auth")

    def test_password_required_but_missing_is_explained(self):
        def script(server, conn):
            conn.sendall(b"RFB 003.008\n")
            server.recv_exact(conn, 12)
            conn.sendall(bytes([1, 2]))
            time.sleep(0.5)
        server, session, events = self.connect(script)
        self.assertTrue(wait_for(lambda: events["failed"]), events)
        self.assertIn("password", events["failed"].lower())

    def test_rfb_33_server(self):
        def script(server, conn):
            server.handshake(conn, version=b"RFB 003.003\n", security=(2,), password="pw")
        server, session, events = self.connect(script, password="pw")
        self.assertTrue(wait_for(lambda: events["connected"]), events)
        self.assertEqual(server.received["version"], b"RFB 003.003\n")

    def test_not_a_vnc_server(self):
        def script(server, conn):
            conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n")
            time.sleep(0.5)
        server, session, events = self.connect(script)
        self.assertTrue(wait_for(lambda: events["failed"]), events)
        self.assertIn("does not speak VNC", events["failed"])
        self.assertEqual(session.failure_kind, "protocol")

    def test_raw_copyrect_hextile_and_resize(self):
        red, green, blue, white = bgrx(255, 0, 0), bgrx(0, 255, 0), bgrx(0, 0, 255), bgrx(255, 255, 255)

        def script(server, conn):
            server.handshake(conn)
            msg = bytearray(b"\x00\x00" + struct.pack(">H", 3))
            # Raw: a 2x2 block at (1,1) — red, green / blue, white.
            msg += rect(1, 1, 2, 2, vnc_client.ENC_RAW) + red + green + blue + white
            # CopyRect: the same block again at (10,10).
            msg += rect(10, 10, 2, 2, vnc_client.ENC_COPYRECT) + struct.pack(">HH", 1, 1)
            # Hextile over (20,20) 20x18, four tiles:
            #   (20,20) 16x16 blue background, one green 2x3 subrect at (1,2)
            #   (36,20)  4x16 raw white
            #   (20,36) 16x2 and (36,36) 4x2 inherit the blue background
            hext = bytearray()
            hext += bytes([2 | 4 | 8]) + blue + green + bytes([1]) + bytes([0x12, 0x12])
            hext += bytes([1]) + white * (4 * 16)
            hext += bytes([0]) + bytes([0])
            msg += rect(20, 20, 20, 18, vnc_client.ENC_HEXTILE) + hext
            conn.sendall(bytes(msg))
            server.recv_exact(conn, 10)                      # incremental request
            server.go_on.wait(5)                             # pixels checked first
            # Then the remote screen grows.
            conn.sendall(b"\x00\x00" + struct.pack(">H", 1)
                         + rect(0, 0, 100, 90, vnc_client.ENC_DESKTOP_SIZE))
            server.recv_exact(conn, 10)
            time.sleep(0.2)

        server, session, events = self.connect(script)
        self.assertTrue(wait_for(lambda: events["updates"] >= 1), events)
        px = lambda x, y: self.pixel(session, x, y)
        # Raw, alpha forced opaque.
        self.assertEqual(px(1, 1), red[:3] + b"\xff")
        self.assertEqual(px(2, 1), green[:3] + b"\xff")
        self.assertEqual(px(1, 2), blue[:3] + b"\xff")
        self.assertEqual(px(2, 2), white[:3] + b"\xff")
        # CopyRect
        self.assertEqual(px(10, 10), px(1, 1))
        self.assertEqual(px(11, 11), px(2, 2))
        # Hextile: background, subrect edges, raw tile, inherited background.
        self.assertEqual(px(20, 20), blue[:3] + b"\xff")
        self.assertEqual(px(21, 22), green[:3] + b"\xff")
        self.assertEqual(px(22, 24), green[:3] + b"\xff")
        self.assertEqual(px(23, 22), blue[:3] + b"\xff")
        self.assertEqual(px(21, 25), blue[:3] + b"\xff")
        self.assertEqual(px(37, 25), white[:3] + b"\xff")
        self.assertEqual(px(25, 37), blue[:3] + b"\xff")
        self.assertEqual(px(39, 37), blue[:3] + b"\xff")

        server.go_on.set()
        self.assertTrue(wait_for(lambda: session.width == 100), (session.width, events))
        server.thread.join(5)
        self.assertIsNone(server.error)
        self.assertIsNone(events["failed"])
        self.assertEqual((session.width, session.height), (100, 90))
        self.assertEqual(len(session.framebuffer), 100 * 90 * 4)

    def test_zrle_tiles(self):
        red, green, blue, white = bgrx(255, 0, 0), bgrx(0, 255, 0), bgrx(0, 0, 255), bgrx(255, 255, 255)
        snapshot = {}

        def script(server, conn):
            server.handshake(conn)
            z = bytearray()
            z += b"\x01" + red[:3]                           # (0,0) 64x64 solid red
            z += b"\x02" + green[:3] + blue[:3]              # (64,0) 16x64 packed, 1 bit
            for _row in range(64):
                z += bytes([0b01010101, 0b01010101])
            # (0,64) 64x6 = 384 px: plain RLE, white x100 then red x284
            z += b"\x80" + white[:3] + bytes([99]) + red[:3] + bytes([255, 28])
            # (64,64) 16x6 = 96 px: palette RLE, green run of 90 then 6 single blues
            z += bytes([128 + 2]) + green[:3] + blue[:3] + bytes([0x80, 89]) + bytes([1] * 6)
            compressor = zlib.compressobj()
            data = compressor.compress(bytes(z)) + compressor.flush(zlib.Z_SYNC_FLUSH)
            conn.sendall(b"\x00\x00" + struct.pack(">H", 1)
                         + rect(0, 0, W, H, vnc_client.ENC_ZRLE) + struct.pack(">I", len(data)) + data)
            server.recv_exact(conn, 10)
            # A second update through the same zlib stream: a raw ZRLE tile.
            z2 = b"\x00" + white[:3] * 4
            data2 = compressor.compress(z2) + compressor.flush(zlib.Z_SYNC_FLUSH)
            conn.sendall(b"\x00\x00" + struct.pack(">H", 1)
                         + rect(5, 5, 2, 2, vnc_client.ENC_ZRLE) + struct.pack(">I", len(data2)) + data2)
            server.recv_exact(conn, 10)
            time.sleep(0.2)

        server, session, events = self.connect(script)
        self.assertTrue(wait_for(lambda: events["updates"] >= 2), events)
        self.assertIsNone(events["failed"])
        px = lambda x, y: self.pixel(session, x, y)[:3]
        self.assertEqual(px(0, 0), red[:3])
        self.assertEqual(px(63, 63), red[:3])
        self.assertEqual(px(64, 0), green[:3])             # bit 0 -> palette[0]
        self.assertEqual(px(65, 0), blue[:3])              # bit 1 -> palette[1]
        self.assertEqual(px(79, 63), blue[:3])
        self.assertEqual(px(0, 64), white[:3])             # first of the white run
        self.assertEqual(px(35, 65), white[:3])            # pixel 99
        self.assertEqual(px(36, 65), red[:3])              # pixel 100
        self.assertEqual(px(63, 69), red[:3])
        self.assertEqual(px(64, 64), green[:3])
        self.assertEqual(px(73, 69), green[:3])            # pixel 89
        self.assertEqual(px(74, 69), blue[:3])             # pixel 90
        self.assertEqual(px(79, 69), blue[:3])
        self.assertEqual(px(5, 5), white[:3])              # second update
        self.assertEqual(px(6, 6), white[:3])
        self.assertEqual(px(7, 7), red[:3])


class VncInput(unittest.TestCase):
    def test_pointer_key_and_ctrl_alt_del_messages(self):
        sent = []
        session = VncSession("127.0.0.1", 1)
        session.width, session.height = 100, 50
        session._sock = object()
        session._send = sent.append
        session.pointer(150, -3, 1)
        session.key(0xFF0D, True)
        session.ctrl_alt_del()
        self.assertEqual(sent[0], struct.pack(">BBHH", 5, 1, 99, 0))   # clamped
        self.assertEqual(sent[1], struct.pack(">BBxxI", 4, 1, 0xFF0D))
        downs = [struct.unpack(">BBxxI", m)[2] for m in sent[2:5]]
        ups = [struct.unpack(">BBxxI", m)[2] for m in sent[5:8]]
        self.assertEqual(downs, [0xFFE3, 0xFFE9, 0xFFFF])
        self.assertEqual(ups, [0xFFFF, 0xFFE9, 0xFFE3])

    def test_keysyms(self):
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QKeyEvent
        from PyQt6.QtCore import QEvent

        def ev(key, text="", mods=Qt.KeyboardModifier.NoModifier):
            return QKeyEvent(QEvent.Type.KeyPress, key, mods, text)
        self.assertEqual(vnc_client.keysym_for(ev(Qt.Key.Key_Return)), 0xFF0D)
        self.assertEqual(vnc_client.keysym_for(ev(Qt.Key.Key_F5)), 0xFFC2)
        self.assertEqual(vnc_client.keysym_for(ev(Qt.Key.Key_A, "a")), ord("a"))
        self.assertEqual(vnc_client.keysym_for(ev(Qt.Key.Key_A, "A", Qt.KeyboardModifier.ShiftModifier)), ord("A"))
        # Ctrl+C arrives as a control character; VNC wants the letter.
        self.assertEqual(vnc_client.keysym_for(ev(Qt.Key.Key_C, "\x03", Qt.KeyboardModifier.ControlModifier)), ord("c"))
        self.assertEqual(vnc_client.keysym_for(ev(Qt.Key.Key_Control)), 0xFFE3)
        self.assertEqual(vnc_client.keysym_for(ev(Qt.Key.Key_E, "é")), 0xE9)
        self.assertEqual(vnc_client.keysym_for(ev(Qt.Key.Key_unknown, "€")), 0x01000000 + 0x20AC)

    def test_des_key_is_bit_reversed_password(self):
        # "a" = 0x61 = 01100001 -> reversed 10000110 = 0x86.
        self.assertEqual(vnc_client._vnc_key("a"), bytes([0x86]) + b"\0" * 7)
        self.assertEqual(len(vnc_client._vnc_key("a much longer password")), 8)
        # Same password, same challenge, same answer; different password, different answer.
        challenge = bytes(range(16))
        self.assertEqual(vnc_auth_response("pw", challenge), vnc_auth_response("pw", challenge))
        self.assertNotEqual(vnc_auth_response("pw", challenge), vnc_auth_response("px", challenge))
        # Only the first 8 characters count, as on every VNC server.
        self.assertEqual(vnc_auth_response("12345678", challenge),
                         vnc_auth_response("123456789", challenge))


class VncTab(unittest.TestCase):
    def test_tab_shows_failure_and_cleans_up(self):
        from app.vnc_client import VncViewerTab
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()                   # nothing listens here now
        tab = VncViewerTab({"name": "lab", "host": "127.0.0.1", "port": port})
        self.assertTrue(wait_for(lambda: tab.status.text() == "Failed"))
        self.assertFalse(tab.is_connected())
        # The failure is explained, with a way to try again.
        self.assertFalse(tab.failure_panel.isHidden())
        self.assertIn("refused", tab.failure_panel.title.text())
        self.assertIn("nothing is listening", tab.failure_panel.detail.text())
        self.assertFalse(tab.failure_panel.btn_retry.isHidden())
        self.assertTrue(tab.failure_panel.btn_edit.isHidden())   # not a saved connection
        tab.cleanup()
        tab.cleanup()                   # twice is harmless
        tab.deleteLater()


if __name__ == "__main__":
    unittest.main()
