"""Telnet and serial transports, failure wording, and the failure panel.

* The telnet channel negotiates like a polite client and answers the login
  prompts with the saved username and password.
* Connection failures are classified (DNS, timeout, refused, login…) and
  explained in words, not Winsock numbers.
* A terminal that cannot connect shows a panel with the next things to try,
  and a failed retry after a drop carries on backing off instead of stopping
  after the first attempt.
"""

import os
import socket
import sys
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app import net_errors
from app.stream_transports import (
    DO, DONT, IAC, OPT_ECHO, OPT_NAWS, OPT_SGA, OPT_TTYPE, SB, SE, WILL, WONT,
    TelnetChannel, describe_serial_error,
)

APP = QApplication.instance() or QApplication(sys.argv)


def free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


class Classification(unittest.TestCase):
    def test_socket_failures(self):
        self.assertEqual(net_errors.classify(socket.gaierror(11001, "getaddrinfo failed")),
                         net_errors.DNS)
        self.assertEqual(net_errors.classify(socket.timeout()), net_errors.TIMEOUT)
        self.assertEqual(net_errors.classify(ConnectionRefusedError()), net_errors.REFUSED)
        no_route = OSError(10065, "No route to host")
        no_route.winerror = 10065
        self.assertEqual(net_errors.classify(no_route), net_errors.UNREACHABLE)
        self.assertEqual(net_errors.classify(RuntimeError("odd")), net_errors.OTHER)

    def test_paramiko_failures(self):
        import paramiko
        from paramiko.ssh_exception import NoValidConnectionsError
        self.assertEqual(net_errors.classify(paramiko.AuthenticationException("no")),
                         net_errors.AUTH)
        self.assertEqual(net_errors.classify(paramiko.BadAuthenticationType("no", ["publickey"])),
                         net_errors.AUTH)
        self.assertEqual(net_errors.classify(
            paramiko.SSHException("Error reading SSH protocol banner")), net_errors.PROTOCOL)
        refused = NoValidConnectionsError({("10.0.0.5", 22): ConnectionRefusedError()})
        self.assertEqual(net_errors.classify(refused), net_errors.REFUSED)
        kind, text = net_errors.describe_failure(refused, "10.0.0.5", 22)
        self.assertIn("nothing is listening", text)

    def test_wording(self):
        import paramiko
        kind, text = net_errors.describe_failure(
            paramiko.AuthenticationException("Authentication failed."), "web01", 22, "deploy")
        self.assertEqual(kind, net_errors.AUTH)
        self.assertIn("rejected the login for “deploy”", text)
        kind, text = net_errors.describe_failure(
            socket.gaierror(11001, "getaddrinfo failed"), "dc01.internal", 22)
        self.assertIn("did not resolve", text)
        self.assertNotIn("11001", text)
        self.assertEqual(net_errors.failure_title(net_errors.TIMEOUT, "web01"),
                         "web01 isn't answering")

    def test_old_import_location_still_works(self):
        from app.connection_manager import describe_network_error
        self.assertIs(describe_network_error, net_errors.describe_network_error)


class SerialWording(unittest.TestCase):
    def test_common_errors(self):
        class FakeWinError(Exception):
            def __init__(self, code):
                super().__init__(code, "CreateFile", "x")
                self.winerror = code
        self.assertIn("does not exist", describe_serial_error(FakeWinError(2), "COM9"))
        self.assertIn("in use by another program", describe_serial_error(FakeWinError(5), "COM9"))

    @unittest.skipUnless(sys.platform == "win32", "Windows COM ports")
    def test_a_port_that_is_not_there(self):
        from app.stream_transports import SerialChannel
        with self.assertRaises(Exception) as caught:
            SerialChannel("COM251")
        self.assertIn("does not exist", describe_serial_error(caught.exception, "COM251"))


class TelnetNegotiation(unittest.TestCase):
    def test_negotiation_login_and_data(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        received = bytearray()
        errors = []

        def read_until(conn, needle, timeout=5):
            deadline = time.monotonic() + timeout
            while needle not in received:
                if time.monotonic() > deadline:
                    raise AssertionError(f"never got {needle!r}; got {bytes(received)!r}")
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    raise AssertionError("client closed")
                received.extend(chunk)

        def serve():
            conn, _ = server.accept()
            conn.settimeout(0.2)
            try:
                conn.sendall(bytes([IAC, DO, OPT_TTYPE, IAC, DO, OPT_NAWS, IAC, WILL, OPT_ECHO,
                                    IAC, WILL, OPT_SGA, IAC, DO, 39]) + b"\r\nlogin: ")
                read_until(conn, b"admin\r\x00")
                conn.sendall(b"Password: ")
                read_until(conn, b"secret\r\x00")
                conn.sendall(bytes([IAC, SB, OPT_TTYPE, 1, IAC, SE]) + b"Welcome \xff\xff!\r\n$ ")
                read_until(conn, bytes([IAC, SB, OPT_TTYPE, 0]) + b"xterm-256color")
                read_until(conn, b"ls\r\x00")
            except Exception as exc:
                errors.append(exc)
            finally:
                time.sleep(0.2)
                conn.close()
                server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        channel = TelnetChannel("127.0.0.1", port, username="admin", password="secret")
        channel.resize_pty(100, 30)
        output = bytearray()
        deadline = time.monotonic() + 8
        while b"$ " not in output and time.monotonic() < deadline:
            try:
                output.extend(channel.recv(4096))
            except socket.timeout:
                continue
        channel.sendall(b"ls\r")
        thread.join(8)
        channel.close()
        self.assertEqual(errors, [])

        sent = bytes(received)
        self.assertIn(bytes([IAC, WILL, OPT_TTYPE]), sent)
        self.assertIn(bytes([IAC, WILL, OPT_NAWS]), sent)
        self.assertIn(bytes([IAC, SB, OPT_NAWS, 0, 100, 0, 30, IAC, SE]), sent)
        self.assertIn(bytes([IAC, DO, OPT_ECHO]), sent)
        self.assertIn(bytes([IAC, DO, OPT_SGA]), sent)
        self.assertIn(bytes([IAC, WONT, 39]), sent)      # anything else is refused
        # What reaches the terminal has no telnet commands left in it, and a
        # doubled IAC is one 0xFF byte of data.
        self.assertIn(b"login: ", output)
        self.assertIn(b"Welcome \xff!", output)
        self.assertNotIn(bytes([IAC, SB]), output)

    def test_outgoing_bytes_follow_the_nvt_rules(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        got = bytearray()

        def serve():
            conn, _ = server.accept()
            conn.settimeout(2)
            try:
                while len(got) < 8:
                    chunk = conn.recv(64)
                    if not chunk:
                        break
                    got.extend(chunk)
            finally:
                conn.close()
                server.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        channel = TelnetChannel("127.0.0.1", port)
        channel.sendall(b"a\r\nb\r")        # CR LF stays; a bare CR gains a NUL
        channel.sendall(b"\xff")              # IAC in data is doubled
        thread.join(5)
        channel.close()
        self.assertEqual(bytes(got), b"a\r\nb\r\x00\xff\xff")


class TerminalFailurePanel(unittest.TestCase):
    def make(self, **extra):
        from app.ssh_terminal import SSHTerminal
        conn = {"name": "lab", "host": "127.0.0.1", "port": free_port(), "username": "u"}
        conn.update(extra)
        term = SSHTerminal(conn)
        self.addCleanup(term.deleteLater)
        self.addCleanup(term._reconnect_timer.stop)
        return term

    def test_a_refused_connection_explains_itself(self):
        term = self.make()
        term.connect_ssh()
        panel = term.failure_panel
        self.assertTrue(wait_for(lambda: not panel.isHidden()), "panel never shown")
        self.assertEqual(panel.title.text(), "lab refused the connection")
        self.assertIn("nothing is listening", panel.detail.text())
        self.assertFalse(panel.btn_retry.isHidden())
        self.assertFalse(panel.btn_check.isHidden())
        self.assertTrue(panel.btn_wake.isHidden())     # no MAC saved
        self.assertTrue(panel.btn_edit.isHidden())     # not a saved connection

    def test_actions_fit_the_failure(self):
        term = self.make(id="c1", mac="AA:BB:CC:DD:EE:FF")
        panel = term.failure_panel
        panel.show_failure(term.connection_info, net_errors.TIMEOUT, "no answer")
        self.assertFalse(panel.btn_wake.isHidden())    # might just be switched off
        self.assertFalse(panel.btn_edit.isHidden())
        panel.show_failure(term.connection_info, net_errors.AUTH, "rejected")
        self.assertTrue(panel.btn_wake.isHidden())
        self.assertTrue(panel.btn_check.isHidden())
        self.assertEqual(panel.btn_edit.property("variant"), "primary")
        asked = []
        term.edit_requested.connect(asked.append)
        panel.btn_edit.click()
        self.assertEqual(asked[0]["id"], "c1")

    def test_a_failed_retry_keeps_backing_off_then_says_so(self):
        from app.ssh_terminal import SSHTerminal
        term = self.make()
        # Attempt 1 of the retries after a drop has just failed.
        term._reconnect_attempt = 1
        term._last_failure = (net_errors.REFUSED, "refused")
        term._on_connection_lost("Connection failed: refused")
        self.assertTrue(term._reconnect_timer.isActive())
        self.assertEqual(term._reconnect_attempt, 2)
        self.assertTrue(term.failure_panel.isHidden())
        # The last attempt fails too: now the panel says it gave up.
        term._reconnect_timer.stop()
        term._reconnect_attempt = len(SSHTerminal.RECONNECT_DELAYS)
        term._last_failure = (net_errors.REFUSED, "refused")
        term._on_connection_lost("Connection failed: refused")
        self.assertFalse(term._reconnect_timer.isActive())
        self.assertFalse(term.failure_panel.isHidden())
        self.assertEqual(term.failure_panel.title.text(), "Couldn't reconnect to lab")
        self.assertIn("Tried 6 times", term.failure_panel.detail.text())

    def test_a_drop_without_a_failed_connect_shows_no_panel(self):
        term = self.make()
        term._on_connection_lost("Connection closed by remote host")
        self.assertTrue(term.failure_panel.isHidden())


class DialogProbes(unittest.TestCase):
    def serve_once(self, payload: bytes) -> int:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)

        def serve():
            conn, _ = server.accept()
            conn.sendall(payload)
            time.sleep(0.3)
            conn.close()
            server.close()
        threading.Thread(target=serve, daemon=True).start()
        return server.getsockname()[1]

    def dialog(self, protocol):
        from app.connection_manager import ConnectionDialog
        dialog = ConnectionDialog()
        self.addCleanup(dialog.deleteLater)
        dialog.protocol.setCurrentText(protocol)
        return dialog

    def test_vnc_test_reads_the_rfb_banner(self):
        dialog = self.dialog("VNC")
        port = self.serve_once(b"RFB 003.008\n")
        dialog._do_test({"host": "127.0.0.1", "port": port, "protocol": "VNC"})
        self.assertIn("VNC server answered (RFB 003.008)", dialog.test_status.text())

    def test_vnc_test_spots_a_different_service(self):
        dialog = self.dialog("VNC")
        port = self.serve_once(b"SSH-2.0-OpenSSH_9.6\r\n")
        dialog._do_test({"host": "127.0.0.1", "port": port, "protocol": "VNC"})
        self.assertIn("✗", dialog.test_status.text())

    def test_web_test_uses_the_port_in_a_pasted_url(self):
        dialog = self.dialog("Web")
        port = self.serve_once(b"HTTP/1.1 200 OK\r\n\r\n")
        dialog.host_input.setText(f"http://127.0.0.1:{port}/admin")
        self.assertFalse(dialog.port_input.isEnabled())   # the URL has the port
        dialog._do_test(dialog._test_params())
        self.assertIn("✓", dialog.test_status.text())


if __name__ == "__main__":
    unittest.main()
