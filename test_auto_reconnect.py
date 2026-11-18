"""Tests for reconnecting after a drop, and for session port forwards.

A dropped VPN is the normal case these are written for: the session should
come back on its own, without the tab closing underneath it, and without
retrying a host that refused us for a reason.
"""

import os
import socket
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app import host_keys
from app.port_forwarding import LOCAL
from app.ssh_terminal import SSHTerminal

_APP = QApplication.instance() or QApplication([])

CONN = {"name": "test", "host": "example.invalid", "port": 22, "username": "u"}


class _FakeTransport:
    def __init__(self):
        self.requests = []

    def open_channel(self, kind, dest, src):
        self.requests.append((kind, dest))
        raise RuntimeError("not needed for these tests")

    def set_keepalive(self, seconds):
        pass


def free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class AutoReconnectTests(unittest.TestCase):
    def setUp(self):
        self.term = SSHTerminal(dict(CONN))
        self.attempts = []
        self.term.reconnect = lambda: self.attempts.append(1)

    def tearDown(self):
        self.term._reconnect_timer.stop()
        self.term.deleteLater()

    def test_it_is_on_by_default(self):
        self.assertTrue(self.term._auto_reconnect)

    def test_a_connection_can_opt_out(self):
        term = SSHTerminal(dict(CONN, auto_reconnect=False))
        self.addCleanup(term.deleteLater)
        self.assertFalse(term._auto_reconnect)

    def test_a_drop_schedules_a_retry_instead_of_closing_the_tab(self):
        closed = []
        self.term.session_closed.connect(lambda: closed.append(1))
        self.term._schedule_session_close()
        self.assertTrue(self.term._reconnect_timer.isActive())
        self.assertFalse(self.term._close_emitted)
        self.assertEqual(closed, [])

    def test_the_gap_between_attempts_grows(self):
        delays = []
        for _ in range(3):
            self.term._schedule_auto_reconnect()
            delays.append(self.term._reconnect_timer.interval())
        self.assertEqual(delays, sorted(delays))
        self.assertLess(delays[0], delays[-1])

    def test_it_gives_up_rather_than_retrying_for_ever(self):
        for _ in range(len(SSHTerminal.RECONNECT_DELAYS)):
            self.assertTrue(self.term._schedule_auto_reconnect())
        self.assertFalse(self.term._schedule_auto_reconnect())
        self.assertIn("Reconnect failed", self.term.latency_label.text())

    def test_the_tab_closes_once_the_attempts_run_out(self):
        self.term._reconnect_attempt = len(SSHTerminal.RECONNECT_DELAYS)
        self.term._schedule_session_close()
        self.assertTrue(self.term._close_emitted)

    def test_a_deliberate_disconnect_is_never_retried(self):
        self.term.disconnect()
        self.assertFalse(self.term._schedule_auto_reconnect())

    def test_an_opted_out_connection_closes_the_way_it_used_to(self):
        term = SSHTerminal(dict(CONN, auto_reconnect=False))
        self.addCleanup(term.deleteLater)
        term._schedule_session_close()
        self.assertTrue(term._close_emitted)

    def test_the_countdown_says_which_attempt_this_is(self):
        self.term._schedule_auto_reconnect()
        text = self.term.latency_label.text()
        self.assertIn("Reconnecting in", text)
        self.assertIn(f"/{len(SSHTerminal.RECONNECT_DELAYS)}", text)

    def test_connecting_resets_the_attempt_count(self):
        self.term._schedule_auto_reconnect()
        self.term._on_connected()
        self.assertEqual(self.term._reconnect_attempt, 0)
        self.assertFalse(self.term._reconnect_timer.isActive())

    def test_the_timer_firing_calls_reconnect(self):
        self.term._attempt_auto_reconnect()
        self.assertEqual(self.attempts, [1])

    def test_a_timer_that_fires_after_reconnecting_does_nothing(self):
        self.term._connected = True
        self.term._attempt_auto_reconnect()
        self.assertEqual(self.attempts, [])

    def test_a_refused_host_key_stops_the_retrying(self):
        """A key nobody vouched for is a decision, not a network blip."""
        challenge = host_keys.UnknownHostKey("h", 22, None)

        import app.host_key_dialog as dialog_module

        original = dialog_module.ask_about_host_key
        dialog_module.ask_about_host_key = (
            lambda *args, **kwargs: dialog_module.HostKeyDecision(connect=False)
        )
        try:
            self.term._on_host_key_challenge(challenge)
        finally:
            dialog_module.ask_about_host_key = original
        self.assertFalse(self.term._auto_reconnect)
        self.assertFalse(self.term._schedule_auto_reconnect())

    def test_stopping_every_timer_includes_the_reconnect_one(self):
        self.term._schedule_auto_reconnect()
        self.term._stop_all_timers()
        self.assertFalse(self.term._reconnect_timer.isActive())


class SessionForwardTests(unittest.TestCase):
    def setUp(self):
        self.transport = _FakeTransport()

    def make(self, **overrides):
        term = SSHTerminal(dict(CONN, **overrides))
        self.addCleanup(term.deleteLater)
        self.addCleanup(term._stop_port_forwards)
        return term

    def test_no_forwards_means_no_manager(self):
        term = self.make()
        term._start_port_forwards(self.transport)
        self.assertIsNone(term._forwards)

    def test_a_saved_forward_comes_up_with_the_session(self):
        term = self.make(port_forwards=[f"{free_port()}:db.internal:5432"])
        term._start_port_forwards(self.transport)
        self.assertIsNotNone(term._forwards)
        self.assertEqual(len(term._forwards.active), 1)

    def test_forwards_can_be_given_as_dicts_too(self):
        term = self.make(port_forwards=[{
            "type": LOCAL, "listen_port": free_port(),
            "dest_host": "db", "dest_port": 5432,
        }])
        term._start_port_forwards(self.transport)
        self.assertEqual(len(term._forwards.active), 1)

    def test_a_forward_that_cannot_start_is_reported_not_fatal(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        self.addCleanup(busy.close)
        term = self.make(port_forwards=[f"{busy.getsockname()[1]}:db:5432"])
        term._start_port_forwards(self.transport)
        self.assertEqual(term._forwards.active, [])
        self.assertIn("Port forward", term.latency_label.text())

    def test_a_nonsense_forward_does_not_take_the_session_down(self):
        term = self.make(port_forwards=["this is not a forward"])
        term._start_port_forwards(self.transport)
        self.assertEqual(term._forwards.active, [])
        self.assertEqual(len(term._forwards.failures), 1)

    def test_disconnecting_takes_the_forwards_with_it(self):
        term = self.make(port_forwards=[f"{free_port()}:db:5432"])
        term._start_port_forwards(self.transport)
        term.disconnect()
        self.assertIsNone(term._forwards)


if __name__ == "__main__":
    unittest.main()
