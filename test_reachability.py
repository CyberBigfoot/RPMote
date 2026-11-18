"""Reachability tests: what green, amber and red actually mean.

Real sockets are used for the TCP probes (a listener on 127.0.0.1 is cheap and
deterministic); ICMP is stubbed, because a test suite must not depend on
whether the machine running it can ping the outside world.
"""

import os
import socket
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app import reachability as reach
from app.reachability import (
    DEFAULT_PING_SETTINGS, STATE_CHECKING, STATE_DEGRADED, STATE_DOWN,
    STATE_SKIPPED, STATE_UNKNOWN, STATE_UP, ReachabilityMonitor,
    connection_port, describe, is_probeable, normalize_ping_settings,
    probe_connection, rollup_state, tcp_probe,
)

_APP = QApplication.instance() or QApplication([])


def open_port() -> tuple[socket.socket, int]:
    """A listening socket on loopback, plus its port."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    return server, server.getsockname()[1]


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


class PortResolutionTests(unittest.TestCase):

    def test_an_explicit_port_wins(self):
        self.assertEqual(connection_port({"port": 2222, "protocol": "ssh"}), 2222)
        self.assertEqual(connection_port({"port": "2222", "protocol": "ssh"}), 2222)

    def test_the_protocol_supplies_a_default(self):
        self.assertEqual(connection_port({"protocol": "rdp"}), 3389)
        self.assertEqual(connection_port({"protocol": "ssh"}), 22)
        self.assertEqual(connection_port({"protocol": "sftp"}), 22)

    def test_nonsense_ports_fall_back_rather_than_crash(self):
        for bad in ("", "abc", None, 0, 99999, -1, {"a": 1}):
            self.assertEqual(connection_port({"port": bad, "protocol": "rdp"}), 3389)

    def test_an_unknown_protocol_with_no_port_is_unknowable(self):
        self.assertIsNone(connection_port({"protocol": "carrier-pigeon"}))


class ProbeabilityTests(unittest.TestCase):

    def test_a_normal_connection_is_probeable(self):
        self.assertTrue(is_probeable({"host": "10.0.0.1", "protocol": "rdp"}))

    def test_azure_powershell_has_nothing_to_probe(self):
        self.assertFalse(is_probeable({"host": "", "protocol": "azure_ps"}))
        self.assertFalse(is_probeable({"host": "portal.azure.com",
                                       "protocol": "azure_ps"}))

    def test_a_hostless_record_is_not_probeable(self):
        self.assertFalse(is_probeable({"host": "   ", "protocol": "ssh"}))
        self.assertFalse(is_probeable(None))
        self.assertFalse(is_probeable("not a dict"))


class TcpProbeTests(unittest.TestCase):

    def test_a_listening_port_answers(self):
        server, port = open_port()
        self.addCleanup(server.close)
        ok, ms, detail = tcp_probe("127.0.0.1", port, timeout=2.0)
        self.assertTrue(ok, detail)
        self.assertGreaterEqual(ms, 0.0)

    def test_a_closed_port_does_not(self):
        ok, _ms, detail = tcp_probe("127.0.0.1", closed_port(), timeout=2.0)
        self.assertFalse(ok)
        self.assertTrue(detail, "a failure should say something useful")

    def test_an_unresolvable_host_fails_without_raising(self):
        ok, _ms, _detail = tcp_probe(
            "no-such-host.invalid.rpmote-test", 22, timeout=2.0)
        self.assertFalse(ok)


class ProbeStateTests(unittest.TestCase):

    def stub_icmp(self, result: bool):
        self.addCleanup(setattr, reach, "icmp_probe", reach.icmp_probe)
        reach.icmp_probe = lambda host, timeout=2.0: (
            (True, 4.2, "ping replied") if result else (False, 0.0, "no ping reply"))

    def test_an_open_service_port_is_up(self):
        server, port = open_port()
        self.addCleanup(server.close)
        info = probe_connection({"host": "127.0.0.1", "port": port,
                                 "protocol": "ssh"}, timeout=2.0)
        self.assertEqual(info["state"], STATE_UP)
        self.assertIsNotNone(info["latency_ms"])

    def test_a_shut_port_on_a_live_host_is_degraded_not_down(self):
        """The distinction the whole three-state design exists for: this is a
        stopped service, not a missing server, and they need different work."""
        self.stub_icmp(True)
        info = probe_connection({"host": "127.0.0.1", "port": closed_port(),
                                 "protocol": "rdp"}, timeout=1.0)
        self.assertEqual(info["state"], STATE_DEGRADED)
        self.assertIn("ping", info["detail"])

    def test_nothing_answering_at_all_is_down(self):
        self.stub_icmp(False)
        info = probe_connection({"host": "127.0.0.1", "port": closed_port(),
                                 "protocol": "rdp"}, timeout=1.0)
        self.assertEqual(info["state"], STATE_DOWN)

    def test_icmp_is_not_consulted_when_the_fallback_is_off(self):
        calls = []
        self.addCleanup(setattr, reach, "icmp_probe", reach.icmp_probe)
        reach.icmp_probe = lambda *a, **k: (calls.append(a) or (True, 1.0, "x"))
        info = probe_connection({"host": "127.0.0.1", "port": closed_port(),
                                 "protocol": "rdp"},
                                timeout=1.0, icmp_fallback=False)
        self.assertEqual(info["state"], STATE_DOWN)
        self.assertEqual(calls, [])

    def test_a_hostless_connection_is_skipped_not_failed(self):
        info = probe_connection({"host": "", "protocol": "azure_ps"})
        self.assertEqual(info["state"], STATE_SKIPPED)

    def test_an_unknown_port_falls_back_to_ping_alone(self):
        self.stub_icmp(True)
        info = probe_connection({"host": "127.0.0.1", "protocol": "mystery"},
                                timeout=1.0)
        self.assertEqual(info["state"], STATE_DEGRADED)


class RollupTests(unittest.TestCase):

    def test_the_worst_real_state_wins(self):
        self.assertEqual(rollup_state([STATE_UP, STATE_UP, STATE_DOWN]), STATE_DOWN)
        self.assertEqual(rollup_state([STATE_UP, STATE_DEGRADED]), STATE_DEGRADED)
        self.assertEqual(rollup_state([STATE_UP, STATE_UP]), STATE_UP)

    def test_unknowns_never_outrank_a_real_answer(self):
        self.assertEqual(rollup_state([STATE_UNKNOWN, STATE_UP]), STATE_UP)
        self.assertEqual(rollup_state([STATE_SKIPPED, STATE_UP]), STATE_UP)

    def test_an_empty_estate_is_unknown(self):
        self.assertEqual(rollup_state([]), STATE_UNKNOWN)


class SettingsTests(unittest.TestCase):

    def test_defaults_survive_a_missing_blob(self):
        self.assertEqual(normalize_ping_settings(None), DEFAULT_PING_SETTINGS)
        self.assertEqual(normalize_ping_settings("nonsense"), DEFAULT_PING_SETTINGS)

    def test_out_of_range_values_are_clamped_not_rejected(self):
        got = normalize_ping_settings(
            {"interval": 1, "timeout": 900.0, "workers": 5000})
        self.assertEqual(got["interval"], 5)
        self.assertEqual(got["timeout"], 15.0)
        self.assertEqual(got["workers"], 32)

    def test_garbage_types_fall_back_to_the_default(self):
        got = normalize_ping_settings({"interval": "soon", "enabled": 1})
        self.assertEqual(got["interval"], DEFAULT_PING_SETTINGS["interval"])
        self.assertIs(got["enabled"], True)

    def test_probing_never_starts_switched_on(self):
        """Contacting a customer's estate is something the user opts into."""
        self.assertFalse(DEFAULT_PING_SETTINGS["enabled"])


class MonitorTests(unittest.TestCase):

    def setUp(self):
        self.server, self.port = open_port()
        self.addCleanup(self.server.close)
        self.dead_port = closed_port()
        self.addCleanup(setattr, reach, "icmp_probe", reach.icmp_probe)
        reach.icmp_probe = lambda host, timeout=2.0: (False, 0.0, "stubbed")

    def make_monitor(self, **settings):
        monitor = ReachabilityMonitor({"timeout": 1.0, **settings})
        self.addCleanup(monitor.shutdown)
        return monitor

    def sweep(self, monitor, ids=None, timeout=20.0) -> dict:
        """Run one sweep and wait for *all* of it, not just the fast probes."""
        done = []
        monitor.sweep_finished.connect(done.append)
        monitor.check_now(ids)
        self.assertTrue(pump(lambda: bool(done), timeout),
                        "the sweep never finished")
        return done[-1]

    def conns(self):
        return [
            {"id": "up", "name": "Live", "host": "127.0.0.1",
             "port": self.port, "protocol": "ssh"},
            {"id": "down", "name": "Dead", "host": "127.0.0.1",
             "port": self.dead_port, "protocol": "rdp"},
            {"id": "skip", "name": "Azure", "host": "", "protocol": "azure_ps"},
        ]

    def test_a_one_off_check_probes_without_switching_probing_on(self):
        monitor = self.make_monitor()
        monitor.set_connections(self.conns())
        self.sweep(monitor)
        self.assertEqual(monitor.state("up"), STATE_UP)
        self.assertEqual(monitor.state("down"), STATE_DOWN)
        self.assertEqual(monitor.state("skip"), STATE_SKIPPED)
        self.assertFalse(monitor.enabled, "a spot check is not a subscription")

    def test_a_sweep_outliving_the_window_ends_quietly(self):
        """Seen on quit with a dead network: the probes timed out after the
        window, and the monitor's C++ side, had gone."""
        import threading
        from PyQt6 import sip

        entered, release, errors = threading.Event(), threading.Event(), []
        original_probe, original_hook = reach.probe_connection, threading.excepthook

        def slow_probe(meta, timeout, icmp):
            entered.set()
            release.wait(10)
            return original_probe(meta, timeout, icmp)
        reach.probe_connection = slow_probe
        threading.excepthook = errors.append
        self.addCleanup(setattr, reach, "probe_connection", original_probe)
        self.addCleanup(setattr, threading, "excepthook", original_hook)

        monitor = ReachabilityMonitor({"timeout": 1.0})
        monitor.set_connections(self.conns())
        monitor.check_now()
        self.assertTrue(entered.wait(10))
        workers = [t for t in threading.enumerate() if t.name.startswith("rpmote-reach")]
        monitor.shutdown()
        sip.delete(monitor)
        release.set()
        for worker in workers:
            worker.join(15)
        self.assertEqual(errors, [])

    def test_results_reach_the_ui_as_signals(self):
        monitor = self.make_monitor()
        monitor.set_connections(self.conns())
        seen = {}
        monitor.status_changed.connect(lambda cid, info: seen.__setitem__(cid, info))
        monitor.check_now()
        self.assertTrue(pump(lambda: seen.get("up", {}).get("state") == STATE_UP))
        self.assertIn("down", seen)

    def test_a_checking_state_is_published_before_the_answer(self):
        monitor = self.make_monitor()
        monitor.set_connections(self.conns())
        states = []
        monitor.status_changed.connect(
            lambda cid, info: states.append((cid, info["state"])))
        self.sweep(monitor)
        self.assertIn(("up", STATE_CHECKING), states)
        self.assertLess(states.index(("up", STATE_CHECKING)),
                        states.index(("up", STATE_UP)))

    def test_checking_only_the_ids_asked_for(self):
        monitor = self.make_monitor()
        monitor.set_connections(self.conns())
        self.sweep(monitor, ["up"])
        self.assertEqual(monitor.state("up"), STATE_UP)
        self.assertEqual(monitor.state("down"), STATE_UNKNOWN,
                         "a targeted check must not sweep the estate")

    def test_a_moved_host_drops_its_stale_result(self):
        """A green dot from the old address would be a lie about the new one."""
        monitor = self.make_monitor()
        monitor.set_connections(self.conns())
        self.sweep(monitor, ["up"])

        moved = self.conns()
        moved[0]["host"] = "10.99.99.99"
        monitor.set_connections(moved)
        self.assertEqual(monitor.state("up"), STATE_UNKNOWN)

    def test_a_deleted_connection_takes_its_status_with_it(self):
        monitor = self.make_monitor()
        monitor.set_connections(self.conns())
        self.sweep(monitor, ["up"])
        monitor.set_connections([c for c in self.conns() if c["id"] != "up"])
        self.assertEqual(monitor.statuses().get("up"), None)

    def test_turning_probing_off_clears_what_it_learned(self):
        monitor = self.make_monitor()
        monitor.set_connections(self.conns())
        self.sweep(monitor)
        monitor.set_enabled(True)
        monitor.set_enabled(False)
        self.assertEqual(monitor.statuses(), {})
        self.assertEqual(monitor.state("up"), STATE_UNKNOWN)

    def test_connections_with_no_id_are_ignored_rather_than_colliding(self):
        monitor = self.make_monitor()
        monitor.set_connections([{"name": "no id", "host": "127.0.0.1",
                                  "port": self.port, "protocol": "ssh"},
                                 "not a dict"])
        self.sweep(monitor)
        self.assertEqual(monitor.statuses(), {})

    def test_a_probe_that_explodes_does_not_kill_the_sweep(self):
        boom = ["down"]
        real = reach.probe_connection
        self.addCleanup(setattr, reach, "probe_connection", real)

        def flaky(meta, timeout=2.0, icmp=True):
            if meta["id"] in boom:
                raise RuntimeError("probe exploded")
            return real(meta, timeout, icmp)

        reach.probe_connection = flaky
        monitor = self.make_monitor()
        monitor.set_connections(self.conns())
        self.sweep(monitor)
        self.assertEqual(monitor.state("up"), STATE_UP)
        self.assertEqual(monitor.state("down"), STATE_DOWN)
        self.assertIn("probe error", monitor.status("down")["detail"])

    def queued_monitor(self, force, pending=True):
        """A monitor with a request already queued and no worker to eat it.

        _run is stubbed out because a real worker consumes the queue the moment
        check_now starts it, which would make these assertions pass whatever
        the merge logic did.
        """
        monitor = self.make_monitor()
        monitor.set_connections(self.conns())
        monitor._run = lambda once=False: None
        with monitor._lock:
            monitor._force = force
            monitor._pending = pending
        return monitor

    def test_a_targeted_check_cannot_narrow_a_queued_full_sweep(self):
        """check_now() then check_now([one]) must still sweep everything —
        the second call is an additional request, not a replacement."""
        monitor = self.queued_monitor(None)
        monitor.check_now(["up"])
        self.assertIsNone(monitor._force,
                          "the full sweep must survive the targeted request")
        self.assertTrue(monitor._pending)

    def test_two_targeted_checks_are_merged_rather_than_replacing(self):
        monitor = self.queued_monitor({"up"})
        monitor.check_now(["down"])
        self.assertEqual(monitor._force, {"up", "down"})

    def test_a_targeted_check_on_an_idle_monitor_stays_targeted(self):
        monitor = self.queued_monitor(None, pending=False)
        monitor.check_now(["up"])
        self.assertEqual(monitor._force, {"up"},
                         "an idle monitor has no full sweep to preserve")

    def test_asking_for_everything_widens_a_queued_targeted_check(self):
        monitor = self.queued_monitor({"up"})
        monitor.check_now()
        self.assertIsNone(monitor._force)

    def test_a_request_arriving_mid_sweep_is_not_swallowed(self):
        """The one-shot worker exits after its sweep; a click that landed while
        it was running has to still be honoured."""
        monitor = self.make_monitor()
        monitor.set_connections(self.conns())
        self.sweep(monitor, ["up"])
        self.assertEqual(monitor.state("down"), STATE_UNKNOWN)

        # Queue a second request the instant the first finishes.
        self.sweep(monitor, ["down"])
        self.assertEqual(monitor.state("down"), STATE_DOWN)
        self.assertEqual(monitor.state("up"), STATE_UP,
                         "the earlier result should still be there")

    def test_the_tenant_rollup_reports_the_worst_member(self):
        monitor = self.make_monitor()
        monitor.set_connections(self.conns())
        self.sweep(monitor)
        self.assertEqual(monitor.tenant_state(["up", "down"]), STATE_DOWN)
        self.assertEqual(monitor.tenant_state(["up", "skip"]), STATE_UP)

    def test_shutdown_stops_the_worker(self):
        monitor = ReachabilityMonitor({"timeout": 1.0, "enabled": True,
                                       "interval": 5})
        monitor.set_connections(self.conns())
        monitor.start()
        self.assertTrue(pump(lambda: monitor.state("up") == STATE_UP))
        monitor.shutdown()
        self.assertFalse(
            any(t.name == "rpmote-reachability" for t in threading.enumerate()),
            "the sweeper thread should be gone")


class DescribeTests(unittest.TestCase):

    def test_a_healthy_result_shows_its_latency(self):
        text = describe({"state": STATE_UP, "latency_ms": 12.4,
                         "detail": "port 22 open", "checked_at": time.time()})
        self.assertIn("Reachable", text)
        self.assertIn("12 ms", text)

    def test_a_failure_explains_itself(self):
        text = describe({"state": STATE_DOWN, "latency_ms": None,
                         "detail": "port 3389: timed out", "checked_at": 0})
        self.assertIn("Unreachable", text)
        self.assertIn("3389", text)

    def test_garbage_in_does_not_crash_a_tooltip(self):
        self.assertTrue(describe(None))
        self.assertTrue(describe({}))


if __name__ == "__main__":
    unittest.main()
