"""Tests for importing an ssh config and for port forwarding.

The forwarding tests use a real loopback socket with a stand-in transport, so
bytes genuinely travel through the code under test rather than past a mock.
"""

import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app import ssh_config_import as sci
from app.port_forwarding import (
    LOCAL, REMOTE, ForwardError, ForwardManager, LocalForward, describe,
    parse_spec,
)

CONFIG = """
Host bastion
    HostName bastion.corp.example
    User jump
    Port 2222
    IdentityFile ~/.ssh/id_bastion

Host web01
    HostName 10.0.0.11
    User deploy
    ProxyJump bastion

Host db01
    HostName 10.0.0.12

Host *
    ServerAliveInterval 30
    User fallback
"""


class ConfigParsingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "config"
        self.path.write_text(CONFIG, encoding="utf-8")

    def entries(self):
        return sci.parse_config(self.path)

    def by_name(self, name):
        return next(e for e in self.entries() if e["name"] == name)

    def test_every_named_host_is_imported(self):
        self.assertEqual([e["name"] for e in self.entries()],
                         ["bastion", "db01", "web01"])

    def test_a_wildcard_block_is_settings_not_a_machine(self):
        self.assertNotIn("*", [e["name"] for e in self.entries()])

    def test_hostname_port_and_user_are_read(self):
        entry = self.by_name("bastion")
        self.assertEqual(entry["host"], "bastion.corp.example")
        self.assertEqual(entry["port"], 2222)
        self.assertEqual(entry["username"], "jump")

    def test_settings_from_the_wildcard_block_still_apply(self):
        # Host * sets User fallback, and db01 names no user of its own.
        self.assertEqual(self.by_name("db01")["username"], "fallback")

    def test_a_specific_block_beats_the_wildcard_the_way_openssh_does(self):
        """First value obtained wins, which is why Host * belongs last."""
        self.assertEqual(self.by_name("bastion")["username"], "jump")

    def test_parsing_does_not_wait_on_a_reverse_dns_lookup(self):
        """paramiko resolves the local FQDN while expanding values, and that
        blocks for the full DNS timeout on a misconfigured network."""
        import time

        started = time.monotonic()
        sci.parse_config(self.path)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_an_identity_file_is_expanded_to_a_real_path(self):
        key = self.by_name("bastion")["key_file"]
        self.assertNotIn("~", key)
        self.assertTrue(key.endswith("id_bastion"))

    def test_a_host_with_no_hostname_uses_its_own_name(self):
        path = Path(self.dir.name) / "plain"
        path.write_text("Host solo\n    User root\n", encoding="utf-8")
        self.assertEqual(sci.parse_config(path)[0]["host"], "solo")

    def test_a_missing_file_imports_nothing(self):
        self.assertEqual(sci.parse_config(Path(self.dir.name) / "nope"), [])

    def test_everything_imported_is_an_ssh_connection(self):
        self.assertTrue(all(e["protocol"] == "SSH" for e in self.entries()))


class JumpLinkTests(ConfigParsingTests):
    def test_proxyjump_becomes_a_link_between_imported_hosts(self):
        entries = sci.link_jump_hosts(self.entries())
        web = next(e for e in entries if e["name"] == "web01")
        self.assertEqual(web["jump_via"], "bastion")

    def test_a_host_without_proxyjump_gets_no_link(self):
        entries = sci.link_jump_hosts(self.entries())
        self.assertNotIn("jump_via", next(e for e in entries if e["name"] == "db01"))

    def test_a_proxyjump_to_something_unknown_is_left_unset(self):
        path = Path(self.dir.name) / "dangling"
        path.write_text("Host a\n    HostName a.example\n    ProxyJump ghost\n",
                        encoding="utf-8")
        entries = sci.link_jump_hosts(sci.parse_config(path))
        self.assertNotIn("jump_via", entries[0])

    def test_a_user_at_host_proxyjump_still_resolves(self):
        path = Path(self.dir.name) / "userat"
        path.write_text(
            "Host bastion\n    HostName b.example\n"
            "Host web\n    HostName w.example\n    ProxyJump admin@bastion:22\n",
            encoding="utf-8")
        entries = sci.link_jump_hosts(sci.parse_config(path))
        web = next(e for e in entries if e["name"] == "web")
        self.assertEqual(web["jump_via"], "bastion")

    def test_the_proxy_jump_scratch_field_is_not_left_on_the_connection(self):
        entries = sci.link_jump_hosts(self.entries())
        self.assertTrue(all("proxy_jump" not in e for e in entries))


class DedupeTests(ConfigParsingTests):
    def test_a_host_already_saved_is_not_offered_again(self):
        existing = [{"host": "10.0.0.12", "port": 22, "username": "fallback"}]
        fresh = sci.read_importable(self.path, existing)
        self.assertNotIn("db01", [e["name"] for e in fresh])

    def test_a_different_user_on_the_same_host_is_a_different_connection(self):
        existing = [{"host": "10.0.0.12", "port": 22, "username": "someone-else"}]
        fresh = sci.read_importable(self.path, existing)
        self.assertIn("db01", [e["name"] for e in fresh])

    def test_nothing_saved_means_everything_is_offered(self):
        self.assertEqual(len(sci.read_importable(self.path, [])), 3)


class SpecParsingTests(unittest.TestCase):
    def test_the_common_two_colon_form(self):
        spec = parse_spec("8080:127.0.0.1:80")
        self.assertEqual(spec["type"], LOCAL)
        self.assertEqual(spec["listen_port"], 8080)
        self.assertEqual(spec["dest_host"], "127.0.0.1")
        self.assertEqual(spec["dest_port"], 80)

    def test_local_forwards_bind_loopback_by_default(self):
        """Otherwise a customer's database is on the office network."""
        self.assertEqual(parse_spec("8080:db:5432")["listen_host"], "127.0.0.1")

    def test_a_bind_address_can_be_given(self):
        self.assertEqual(parse_spec("0.0.0.0:8080:db:5432")["listen_host"],
                         "0.0.0.0")

    def test_an_r_prefix_makes_it_a_reverse_forward(self):
        self.assertEqual(parse_spec("R:9000:127.0.0.1:22")["type"], REMOTE)

    def test_an_l_prefix_is_accepted_and_means_local(self):
        self.assertEqual(parse_spec("L:8080:db:5432")["type"], LOCAL)

    def test_junk_is_refused_with_an_example(self):
        with self.assertRaises(ForwardError) as caught:
            parse_spec("not a forward")
        self.assertIn("8080:127.0.0.1:80", str(caught.exception))

    def test_a_port_outside_the_range_is_refused(self):
        with self.assertRaises(ForwardError):
            parse_spec("70000:host:80")

    def test_a_forward_describes_itself_readably(self):
        self.assertIn("→", describe(parse_spec("8080:db:5432")))
        self.assertIn("remote", describe(parse_spec("R:9000:db:22")))


class _FakeChannel:
    """A channel that is really just the other end of a socket pair."""

    def __init__(self, sock):
        self._sock = sock

    def recv(self, size):
        return self._sock.recv(size)

    def sendall(self, data):
        return self._sock.sendall(data)

    def fileno(self):
        return self._sock.fileno()

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


class _EchoTransport:
    """Stands in for a paramiko Transport; every channel echoes back."""

    def __init__(self):
        self.requests = []
        self.refuse = False

    def open_channel(self, kind, dest, src):
        self.requests.append((kind, dest))
        if self.refuse:
            raise RuntimeError("channel refused")
        near, far = socket.socketpair()
        threading.Thread(target=self._echo, args=(far,), daemon=True).start()
        return _FakeChannel(near)

    @staticmethod
    def _echo(sock):
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                sock.sendall(b"echo:" + data)
        except Exception:
            pass
        finally:
            try:
                sock.close()
            except Exception:
                pass


class LocalForwardTests(unittest.TestCase):
    def setUp(self):
        self.transport = _EchoTransport()
        self.forward = LocalForward(self.transport, "127.0.0.1", 0,
                                    "db.internal", 5432)
        self.forward.start()
        self.addCleanup(self.forward.stop)

    def test_bytes_travel_from_the_local_port_to_the_channel_and_back(self):
        with socket.create_connection(("127.0.0.1", self.forward.bound_port), 5) as s:
            s.sendall(b"hello")
            s.settimeout(5)
            self.assertEqual(s.recv(100), b"echo:hello")

    def test_the_channel_is_opened_to_the_destination_that_was_asked_for(self):
        with socket.create_connection(("127.0.0.1", self.forward.bound_port), 5) as s:
            s.sendall(b"x")
            s.settimeout(5)
            s.recv(100)
        self.assertEqual(self.transport.requests[0],
                         ("direct-tcpip", ("db.internal", 5432)))

    def test_a_refused_channel_closes_the_caller_rather_than_hanging(self):
        self.transport.refuse = True
        with socket.create_connection(("127.0.0.1", self.forward.bound_port), 5) as s:
            s.settimeout(5)
            self.assertEqual(s.recv(100), b"")

    def test_a_port_already_in_use_is_reported_not_swallowed(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        self.addCleanup(busy.close)
        clash = LocalForward(self.transport, "127.0.0.1",
                             busy.getsockname()[1], "db", 5432)
        with self.assertRaises(ForwardError) as caught:
            clash.start()
        self.assertIn("Could not listen", str(caught.exception))

    def test_stopping_releases_the_port(self):
        port = self.forward.bound_port
        self.forward.stop()
        replacement = socket.socket()
        replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        replacement.bind(("127.0.0.1", port))
        replacement.close()


class ForwardManagerTests(unittest.TestCase):
    def setUp(self):
        self.transport = _EchoTransport()
        self.manager = ForwardManager(self.transport)
        self.addCleanup(self.manager.stop_all)

    @staticmethod
    def _free_port():
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def test_a_spec_string_can_be_started_directly(self):
        runner = self.manager.start(f"{self._free_port()}:db:5432")
        self.assertIsInstance(runner, LocalForward)
        self.assertEqual(len(self.manager.active), 1)

    def test_one_bad_forward_does_not_stop_the_others(self):
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        self.addCleanup(busy.close)
        self.manager.start_all([
            {"type": LOCAL, "listen_port": busy.getsockname()[1],
             "dest_host": "db", "dest_port": 5432},
            {"type": LOCAL, "listen_port": self._free_port(),
             "dest_host": "db", "dest_port": 5432},
        ])
        self.assertEqual(len(self.manager.active), 1)
        self.assertEqual(len(self.manager.failures), 1)

    def test_stopping_clears_everything_it_started(self):
        self.manager.start(f"{self._free_port()}:db:5432")
        self.manager.stop_all()
        self.assertEqual(self.manager.active, [])


if __name__ == "__main__":
    unittest.main()
