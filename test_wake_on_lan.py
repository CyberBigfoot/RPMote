"""Wake-on-LAN tests: what actually leaves the machine, and where it aims.

Real UDP sockets are used, but every send in here is pointed at loopback with
broadcasting suppressed — a test run must never spray magic packets across
whatever network the developer happens to be sitting on.
"""

import os
import shutil
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.vault as vault_mod

_TMP = Path(tempfile.mkdtemp(prefix="rpmote_wol_test_"))
# All-or-nothing: redirecting only some paths would leave the rest pointed at
# the real ~/.rpmote and let these tests clobber a live vault.
vault_mod.use_vault_directory(_TMP)

from PyQt6.QtWidgets import QApplication, QMessageBox

from app import wake_on_lan as wol
from app.wake_on_lan import (
    DEFAULT_WOL_PORTS, LIMITED_BROADCAST, WakeResult, WakeRunner,
    connection_mac, is_wakeable, mac_bytes, magic_packet, normalize_mac,
    send_magic_packet, wait_for_wake, wake_connection, wake_targets, wol_ports,
)

_APP = QApplication.instance() or QApplication([])

MAC = "00:11:22:33:44:55"


def udp_listener() -> tuple:
    """A bound UDP socket on loopback, plus its port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    return sock, sock.getsockname()[1]


def tcp_listener() -> tuple:
    """A listening TCP socket on loopback, plus its port."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    return server, server.getsockname()[1]


def free_udp_port() -> int:
    """A UDP port nothing is bound to — somewhere harmless to aim a packet."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def closed_port() -> int:
    """A port nothing is listening on."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def pump(predicate, timeout=10.0) -> bool:
    """Spin the Qt event loop until *predicate* holds (queued signals land)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _APP.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    _APP.processEvents()
    return bool(predicate())


class MacParsingTests(unittest.TestCase):

    def test_every_form_an_admin_might_paste(self):
        for raw in ("00:11:22:33:44:55", "00-11-22-33-44-55",
                    "0011.2233.4455", "001122334455",
                    "  00:11:22:33:44:55  ", "00 11 22 33 44 55"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_mac(raw), MAC)

    def test_case_is_normalized_upwards(self):
        self.assertEqual(normalize_mac("aabbccddeeff"), "AA:BB:CC:DD:EE:FF")

    def test_things_that_are_not_macs_are_rejected(self):
        for raw in ("", None, "hostname", "192.168.1.10", "00:11:22:33:44",
                    "00:11:22:33:44:5G", "001122334455aa"):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_mac(raw))

    def test_mac_bytes_refuses_rubbish_rather_than_guessing(self):
        self.assertEqual(mac_bytes(MAC), b"\x00\x11\x22\x33\x44\x55")
        with self.assertRaises(ValueError):
            mac_bytes("not-a-mac")


class MagicPacketTests(unittest.TestCase):

    def test_the_packet_is_the_shape_a_sleeping_nic_looks_for(self):
        packet = magic_packet(MAC)
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:], mac_bytes(MAC) * 16)

    def test_separators_do_not_change_the_bytes_on_the_wire(self):
        self.assertEqual(magic_packet("0011.2233.4455"), magic_packet(MAC))


class ConnectionFieldTests(unittest.TestCase):

    def test_a_saved_mac_makes_a_connection_wakeable(self):
        conn = {"name": "DC01", "host": "10.0.0.5", "mac": "0011.2233.4455"}
        self.assertEqual(connection_mac(conn), MAC)
        self.assertTrue(is_wakeable(conn))

    def test_legacy_field_names_still_read(self):
        self.assertEqual(connection_mac({"mac_address": MAC}), MAC)
        self.assertEqual(connection_mac({"wol_mac": MAC}), MAC)

    def test_no_mac_means_nothing_to_wake(self):
        for conn in ({}, {"host": "10.0.0.5"}, {"mac": ""}, {"mac": "nonsense"},
                     None, "not a dict"):
            with self.subTest(conn=conn):
                self.assertFalse(is_wakeable(conn))

    def test_an_explicit_port_wins_over_the_defaults(self):
        self.assertEqual(wol_ports({}), DEFAULT_WOL_PORTS)
        self.assertEqual(wol_ports({"wol_port": "7"}), (7,))
        self.assertEqual(wol_ports({"wol_port": 0}), DEFAULT_WOL_PORTS)
        self.assertEqual(wol_ports({"wol_port": "soon"}), DEFAULT_WOL_PORTS)


class TargetTests(unittest.TestCase):

    def test_a_literal_ip_yields_its_subnet_broadcast_first(self):
        targets = wake_targets({"host": "192.168.1.50", "mac": MAC})
        self.assertEqual(targets[0], "192.168.1.255")
        self.assertIn("192.168.1.50", targets)      # unicast, for a live ARP entry
        self.assertEqual(targets[-1], LIMITED_BROADCAST)

    def test_an_explicit_broadcast_is_tried_before_anything_guessed(self):
        targets = wake_targets({"host": "192.168.1.50",
                                "wol_broadcast": "10.4.0.255, 10.4.1.255"})
        self.assertEqual(targets[:2], ["10.4.0.255", "10.4.1.255"])

    def test_limited_broadcast_is_always_the_backstop(self):
        # No host at all: there is still one address that reaches the segment.
        self.assertEqual(wake_targets({"mac": MAC}), [LIMITED_BROADCAST])

    def test_a_name_that_will_not_resolve_does_not_sink_the_send(self):
        targets = wake_targets({"host": "no-such-host.invalid", "mac": MAC})
        self.assertEqual(targets, [LIMITED_BROADCAST])

    def test_targets_are_deduplicated(self):
        targets = wake_targets({"host": "192.168.1.255",
                                "wol_broadcast": "192.168.1.255"})
        self.assertEqual(len(targets), len(set(targets)))


class SendTests(unittest.TestCase):
    """Loopback only — bind_addresses=[] keeps the packet off real NICs."""

    def test_the_listener_receives_the_exact_magic_packet(self):
        sock, port = udp_listener()
        self.addCleanup(sock.close)

        result = send_magic_packet(MAC, targets=["127.0.0.1"], ports=(port,),
                                   bind_addresses=[])

        self.assertTrue(result.ok)
        self.assertEqual(result.sent, [f"127.0.0.1:{port}"])
        self.assertEqual(result.errors, [])
        data, _ = sock.recvfrom(1024)
        self.assertEqual(data, magic_packet(MAC))

    def test_every_port_is_tried_because_neither_is_authoritative(self):
        first, port_a = udp_listener()
        second, port_b = udp_listener()
        self.addCleanup(first.close)
        self.addCleanup(second.close)

        result = send_magic_packet(MAC, targets=["127.0.0.1"],
                                   ports=(port_a, port_b), bind_addresses=[])

        self.assertEqual(len(result.sent), 2)
        self.assertEqual(first.recvfrom(1024)[0], magic_packet(MAC))
        self.assertEqual(second.recvfrom(1024)[0], magic_packet(MAC))

    def test_a_bad_mac_sends_nothing_and_says_why(self):
        result = send_magic_packet("nonsense", targets=["127.0.0.1"],
                                   ports=(9,), bind_addresses=[])
        self.assertFalse(result.ok)
        self.assertEqual(result.sent, [])
        self.assertTrue(result.errors)

    def test_ok_means_sent_not_woken(self):
        """UDP gives no delivery signal, so ok must not overclaim."""
        result = WakeResult(mac=MAC, sent=["255.255.255.255:9"])
        self.assertTrue(result.ok)
        self.assertIsNone(result.came_up)
        self.assertIn("Magic packet sent", result.summary())


class WakeConnectionTests(unittest.TestCase):

    def setUp(self):
        # Keep every test in this class on loopback rather than the real LAN.
        self._real_locals = wol.local_ipv4s
        wol.local_ipv4s = lambda: []
        self.addCleanup(setattr, wol, "local_ipv4s", self._real_locals)

    def test_a_connection_without_a_mac_fails_loudly(self):
        result = wake_connection({"name": "DC01", "host": "10.0.0.5"})
        self.assertFalse(result.ok)
        self.assertIn("no MAC address", result.errors[0])
        self.assertIn("No magic packet could be sent", result.summary())

    def test_waking_a_connection_reaches_its_own_address(self):
        sock, port = udp_listener()
        self.addCleanup(sock.close)
        conn = {"name": "DC01", "host": "127.0.0.1", "mac": MAC,
                "wol_port": port}

        result = wake_connection(conn)

        self.assertTrue(result.ok)
        self.assertEqual(result.name, "DC01")
        self.assertIsNone(result.came_up)   # we did not ask it to wait
        self.assertEqual(sock.recvfrom(1024)[0], magic_packet(MAC))

    def test_waiting_confirms_a_host_that_answers(self):
        server, port = tcp_listener()
        self.addCleanup(server.close)
        conn = {"name": "DC01", "host": "127.0.0.1", "port": port,
                "protocol": "ssh", "mac": MAC, "wol_port": free_udp_port()}

        result = wake_connection(conn, wait_seconds=5.0)

        self.assertTrue(result.ok)
        self.assertTrue(result.came_up)
        self.assertIn("answered after", result.summary())

    def test_a_box_that_pings_but_has_no_service_yet_still_counts_as_awake(self):
        """Waking is about power, not about the service being ready.

        A machine mid-boot answers ICMP long before sshd is listening; calling
        that "did not wake" would be wrong, and would send an admin looking for
        a fault that is not there.
        """
        conn = {"host": "127.0.0.1", "port": closed_port(), "protocol": "ssh"}
        came_up, _ = wait_for_wake(conn, timeout=5.0, poll=0.3)
        self.assertTrue(came_up)

    def test_waiting_gives_up_on_a_host_that_stays_down(self):
        down = {"state": "down", "latency_ms": None, "detail": "timed out"}
        with mock.patch("app.reachability.probe_connection", return_value=down):
            came_up, waited = wait_for_wake(
                {"host": "10.255.255.1", "port": 22, "protocol": "ssh"},
                timeout=1.0, poll=0.3)
        self.assertFalse(came_up)
        self.assertLess(waited, 6.0)

    def test_a_stop_request_ends_the_wait_promptly(self):
        conn = {"host": "127.0.0.1", "port": closed_port(), "protocol": "ssh"}
        came_up, waited = wait_for_wake(conn, timeout=30.0, poll=0.2,
                                        should_stop=lambda: True)
        self.assertFalse(came_up)
        self.assertLess(waited, 2.0)


class RunnerTests(unittest.TestCase):

    def setUp(self):
        self._real_locals = wol.local_ipv4s
        wol.local_ipv4s = lambda: []
        self.addCleanup(setattr, wol, "local_ipv4s", self._real_locals)
        self.runner = WakeRunner()
        self.addCleanup(self.runner.shutdown)
        self.finished = []
        self.individual = []
        self.runner.finished.connect(self.finished.append)
        self.runner.result_ready.connect(
            lambda conn, result: self.individual.append((conn, result)))

    def test_an_empty_batch_finishes_immediately(self):
        self.runner.submit([])
        self.assertEqual(self.finished, [[]])

    def test_a_batch_reports_once_per_machine_and_once_overall(self):
        sock_a, port_a = udp_listener()
        sock_b, port_b = udp_listener()
        self.addCleanup(sock_a.close)
        self.addCleanup(sock_b.close)
        conns = [
            {"id": "a", "name": "A", "host": "127.0.0.1", "mac": MAC,
             "wol_port": port_a},
            {"id": "b", "name": "B", "host": "127.0.0.1",
             "mac": "AA:BB:CC:DD:EE:FF", "wol_port": port_b},
        ]

        self.runner.submit(conns)

        self.assertTrue(pump(lambda: bool(self.finished)))
        self.assertEqual(len(self.individual), 2)
        self.assertEqual(len(self.finished[0]), 2)
        self.assertTrue(all(result.ok for _, result in self.finished[0]))
        self.assertEqual(sock_a.recvfrom(1024)[0], magic_packet(MAC))
        self.assertEqual(sock_b.recvfrom(1024)[0],
                         magic_packet("AA:BB:CC:DD:EE:FF"))

    def test_a_connection_with_no_mac_is_reported_not_dropped(self):
        self.runner.submit([{"id": "x", "name": "X", "host": "127.0.0.1"}])
        self.assertTrue(pump(lambda: bool(self.finished)))
        (_, result), = self.finished[0]
        self.assertFalse(result.ok)

    def test_shutdown_stops_results_reaching_a_dead_receiver(self):
        self.runner.shutdown()
        self.runner.submit([{"id": "a", "host": "127.0.0.1", "mac": MAC,
                             "wol_port": free_udp_port()}])
        pump(lambda: False, timeout=0.5)
        self.assertEqual(self.finished, [])
        self.assertEqual(self.individual, [])

class MainWindowWakeTests(unittest.TestCase):
    """The window is what decides which machines a magic packet is aimed at."""

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    def fresh_vault(self, connections):
        # Re-assert our own directory: another test module imported later in
        # the same process would otherwise still own the module-level globals.
        vault_mod.use_vault_directory(_TMP)
        for f in (vault_mod.VAULT_FILE, vault_mod.SALT_FILE,
                  vault_mod.HASH_FILE, vault_mod.KEY_FILE):
            if f.exists():
                f.unlink()

        from app.vault import Vault

        vault = Vault()
        vault.setup("wol-test-password")
        for conn in connections:
            vault.add_connection(dict(conn))
        return vault

    def window(self, vault):
        from app.main_window import MainWindow

        win = MainWindow(vault)
        self.addCleanup(win.close)
        self.addCleanup(win.deleteLater)
        self.addCleanup(win.reachability.shutdown)
        return win

    def recorder(self, win):
        """Replace the runner's send with a recorder — nothing leaves the box."""
        sent = []
        win.wake_runner.submit = lambda conns, wait_seconds=0.0: sent.append(
            (list(conns), wait_seconds))
        return sent

    def setUp(self):
        self.conns = [
            {"name": "DC01", "host": "127.0.0.1", "port": closed_port(),
             "protocol": "ssh", "tenant": "Contoso", "mac": MAC},
            {"name": "FILE01", "host": "127.0.0.1", "port": closed_port(),
             "protocol": "ssh", "tenant": "Contoso", "mac": "AA:BB:CC:DD:EE:FF"},
            {"name": "NO-MAC", "host": "127.0.0.1", "port": closed_port(),
             "protocol": "ssh", "tenant": "Contoso"},
        ]
        self.vault = self.fresh_vault(self.conns)
        self.by_name = {c["name"]: c for c in self.vault.connections}

    def test_waking_one_machine_hands_only_that_one_to_the_runner(self):
        win = self.window(self.vault)
        sent = self.recorder(win)

        win._wake_connections([str(self.by_name["DC01"]["id"])])

        (targets, wait), = sent
        self.assertEqual([c["name"] for c in targets], ["DC01"])
        self.assertGreater(wait, 0, "a single wake should wait for the answer")

    def test_an_empty_id_list_wakes_every_machine_that_has_a_mac(self):
        win = self.window(self.vault)
        sent = self.recorder(win)

        win._wake_connections([])

        (targets, _), = sent
        self.assertEqual(sorted(c["name"] for c in targets), ["DC01", "FILE01"])

    def test_a_machine_without_a_mac_is_explained_rather_than_ignored(self):
        win = self.window(self.vault)
        sent = self.recorder(win)
        shown = []
        with mock.patch.object(QMessageBox, "information",
                               side_effect=lambda *a, **k: shown.append(a)):
            win._wake_connections([str(self.by_name["NO-MAC"]["id"])])

        self.assertEqual(sent, [], "nothing to send means nothing is sent")
        self.assertTrue(shown, "silence would look like a broken button")
        self.assertIn("NO-MAC", " ".join(str(part) for part in shown[0]))

    def test_asking_for_a_nameless_connection_wakes_nothing(self):
        """A blank id must not widen into "wake everything"."""
        win = self.window(self.vault)
        sent = self.recorder(win)
        with mock.patch.object(QMessageBox, "information"):
            win._wake_connections([""])
        self.assertEqual(sent, [])

    def test_a_bulk_wake_does_not_park_a_poller_on_every_host(self):
        vault = self.fresh_vault([
            {"name": f"SRV{i}", "host": "127.0.0.1", "port": 22,
             "protocol": "ssh", "tenant": "Contoso",
             "mac": f"00:11:22:33:44:{i:02d}"}
            for i in range(6)
        ])
        win = self.window(vault)
        sent = self.recorder(win)

        win._wake_connections([])

        (targets, wait), = sent
        self.assertEqual(len(targets), 6)
        self.assertEqual(wait, 0.0)

    def test_the_estate_is_checked_as_soon_as_the_vault_is_unlocked(self):
        """The whole point: open RPMote, and the graph already tells the truth."""
        win = self.window(self.vault)
        self.assertFalse(win.reachability.enabled,
                         "the login sweep must not need continuous probing on")

        self.assertTrue(pump(lambda: win.dashboard.is_ping_enabled, timeout=8.0))
        self.assertTrue(pump(lambda: bool(win.reachability.statuses()), timeout=8.0))
        self.assertFalse(win.reachability.enabled,
                         "one sweep must not subscribe the user to probing")

    def test_the_login_sweep_can_be_switched_off(self):
        self.vault.update_settings({"ping_settings": {"check_on_login": False}})
        win = self.window(self.vault)

        pump(lambda: win.dashboard.is_ping_enabled, timeout=3.0)

        self.assertFalse(win.dashboard.is_ping_enabled)
        self.assertEqual(win.reachability.statuses(), {})



if __name__ == "__main__":
    unittest.main(verbosity=2)
