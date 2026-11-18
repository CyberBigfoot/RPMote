"""Tests for the shared SSH connect layer.

The parts worth pinning down: which credentials each host is offered, the
order a chain of bastions is dialled in, where each hop opens its channel to,
and that a failure part-way along does not leave connections behind.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import paramiko

from app import ssh_connect
from app.ssh_connect import (
    MFAChallenge, SSHConnectError, describe_route, resolve_jump_chain,
)


def conn(name, **overrides):
    base = {
        "id": name, "name": name, "host": f"{name}.example", "port": 22,
        "username": "u", "password": "pw",
    }
    base.update(overrides)
    return base


class JumpChainTests(unittest.TestCase):
    def test_no_jump_host_means_no_hops(self):
        self.assertEqual(resolve_jump_chain(conn("a"), []), [])

    def test_one_bastion_is_one_hop(self):
        bastion = conn("bastion")
        target = conn("target", jump_via="bastion")
        self.assertEqual(resolve_jump_chain(target, [bastion, target]), [bastion])

    def test_a_bastion_behind_a_bastion_is_followed(self):
        outer = conn("outer")
        inner = conn("inner", jump_via="outer")
        target = conn("target", jump_via="inner")
        chain = resolve_jump_chain(target, [outer, inner, target])
        self.assertEqual([hop["id"] for hop in chain], ["inner", "outer"])

    def test_a_cycle_is_broken_rather_than_followed(self):
        a = conn("a", jump_via="b")
        b = conn("b", jump_via="a")
        chain = resolve_jump_chain(a, [a, b])
        self.assertEqual([hop["id"] for hop in chain], ["b", "a"])

    def test_a_jump_host_that_was_deleted_is_reported_clearly(self):
        target = conn("target", jump_via="gone")
        with self.assertRaises(SSHConnectError) as caught:
            resolve_jump_chain(target, [target])
        self.assertIn("no longer saved", str(caught.exception))

    def test_the_route_reads_from_bastion_to_target(self):
        outer = conn("outer")
        inner = conn("inner", jump_via="outer")
        target = conn("target", jump_via="inner")
        self.assertEqual(
            describe_route(target, [outer, inner, target]),
            "outer → inner → target.example",
        )


class AuthKwargTests(unittest.TestCase):
    def test_a_password_only_connection_sends_a_password(self):
        kwargs = ssh_connect._auth_kwargs(conn("a"))
        self.assertEqual(kwargs["password"], "pw")
        self.assertNotIn("pkey", kwargs)

    def test_the_agent_is_consulted_by_default(self):
        self.assertTrue(ssh_connect._auth_kwargs(conn("a"))["allow_agent"])

    def test_the_agent_can_be_turned_off_per_connection(self):
        kwargs = ssh_connect._auth_kwargs(conn("a", use_agent=False))
        self.assertFalse(kwargs["allow_agent"])

    def test_a_key_passphrase_is_not_the_login_password(self):
        seen = {}

        def fake_load(path, passphrase=None):
            seen["path"] = path
            seen["passphrase"] = passphrase
            return "PKEY"

        original = ssh_connect.load_ssh_private_key
        ssh_connect.load_ssh_private_key = fake_load
        try:
            kwargs = ssh_connect._auth_kwargs(
                conn("a", key_file="id_ed25519", key_passphrase="secret"))
        finally:
            ssh_connect.load_ssh_private_key = original
        self.assertEqual(seen["passphrase"], "secret")
        self.assertEqual(kwargs["pkey"], "PKEY")

    def test_an_old_connection_still_uses_its_password_as_the_passphrase(self):
        """Before there was a passphrase field, the password box did both."""
        seen = {}

        def fake_load(path, passphrase=None):
            seen["passphrase"] = passphrase
            return "PKEY"

        original = ssh_connect.load_ssh_private_key
        ssh_connect.load_ssh_private_key = fake_load
        try:
            ssh_connect._auth_kwargs(conn("a", key_file="id_rsa"))
        finally:
            ssh_connect.load_ssh_private_key = original
        self.assertEqual(seen["passphrase"], "pw")

    def test_an_unloadable_key_falls_back_to_the_password(self):
        def fake_load(path, passphrase=None):
            raise ValueError("bad key")

        original = ssh_connect.load_ssh_private_key
        ssh_connect.load_ssh_private_key = fake_load
        try:
            kwargs = ssh_connect._auth_kwargs(conn("a", key_file="id_rsa"))
        finally:
            ssh_connect.load_ssh_private_key = original
        self.assertEqual(kwargs["password"], "pw")

    def test_an_unloadable_key_with_no_password_is_an_error_worth_showing(self):
        def fake_load(path, passphrase=None):
            raise ValueError("bad key")

        original = ssh_connect.load_ssh_private_key
        ssh_connect.load_ssh_private_key = fake_load
        try:
            with self.assertRaises(SSHConnectError):
                ssh_connect._auth_kwargs(conn("a", key_file="id_rsa", password=""))
        finally:
            ssh_connect.load_ssh_private_key = original

    def test_a_connection_with_no_host_is_refused_early(self):
        with self.assertRaises(SSHConnectError):
            ssh_connect._host_port(conn("a", host=""))

    def test_a_nonsense_port_falls_back_to_22(self):
        self.assertEqual(ssh_connect._host_port(conn("a", port="wat"))[1], 22)


class _FakeTransport:
    def __init__(self, client):
        self.client = client
        self.channels = []

    def open_channel(self, kind, dest, src):
        self.channels.append((kind, dest))
        return f"channel-to-{dest[0]}:{dest[1]}"


class _FakeClient:
    def __init__(self, host):
        self.host = host
        self.closed = False
        self._transport = _FakeTransport(self)

    def get_transport(self):
        return self._transport

    def close(self):
        self.closed = True


class OpenClientTests(unittest.TestCase):
    """Drive open_client with a stand-in for the per-hop connect."""

    def setUp(self):
        self.calls = []
        self.made = []
        self.fail_on = None
        self._original = ssh_connect._connect_one
        ssh_connect._connect_one = self.fake_connect_one
        self.addCleanup(self.restore)

    def restore(self):
        ssh_connect._connect_one = self._original

    def fake_connect_one(self, host_conn, **kwargs):
        host = host_conn["host"]
        # Mirror the real one: a hop is handed a factory, not a socket, so
        # that a retry can open a fresh channel rather than reuse a dead one.
        sock = kwargs.get("sock")
        if sock is None and kwargs.get("sock_factory") is not None:
            sock = kwargs["sock_factory"]()
        self.calls.append((host, sock))
        if self.fail_on == host:
            raise paramiko.AuthenticationException("nope")
        client = _FakeClient(host)
        self.made.append(client)
        return client

    def test_a_direct_connection_dials_once_with_no_socket(self):
        ssh_connect.open_client(conn("a"), connections=[])
        self.assertEqual(self.calls, [("a.example", None)])

    def test_a_bastion_is_dialled_before_the_target(self):
        bastion = conn("bastion")
        target = conn("target", jump_via="bastion")
        ssh_connect.open_client(target, connections=[bastion, target])
        self.assertEqual([host for host, _ in self.calls],
                         ["bastion.example", "target.example"])

    def test_the_target_is_reached_through_the_bastion_channel(self):
        bastion = conn("bastion")
        target = conn("target", jump_via="bastion", port=2222)
        ssh_connect.open_client(target, connections=[bastion, target])
        self.assertEqual(self.calls[1][1], "channel-to-target.example:2222")

    def test_each_hop_opens_a_channel_to_the_next_one(self):
        outer = conn("outer")
        inner = conn("inner", jump_via="outer")
        target = conn("target", jump_via="inner")
        ssh_connect.open_client(target, connections=[outer, inner, target])
        self.assertEqual([host for host, _ in self.calls],
                         ["outer.example", "inner.example", "target.example"])
        self.assertEqual(self.made[0]._transport.channels,
                         [("direct-tcpip", ("inner.example", 22))])
        self.assertEqual(self.made[1]._transport.channels,
                         [("direct-tcpip", ("target.example", 22))])

    def test_closing_the_far_end_closes_the_bastions_behind_it(self):
        bastion = conn("bastion")
        target = conn("target", jump_via="bastion")
        client = ssh_connect.open_client(target, connections=[bastion, target])
        client.close()
        self.assertTrue(all(made.closed for made in self.made))

    def test_a_failure_at_the_target_still_closes_the_bastion(self):
        self.fail_on = "target.example"
        bastion = conn("bastion")
        target = conn("target", jump_via="bastion")
        with self.assertRaises(paramiko.AuthenticationException):
            ssh_connect.open_client(target, connections=[bastion, target])
        self.assertTrue(self.made[0].closed, "the bastion was left open")

    def test_a_prebuilt_chain_is_used_as_given(self):
        """main_window resolves the chain once; the connect layer trusts it."""
        target = dict(conn("target"), jump_chain=[conn("hop")])
        ssh_connect.open_client(target, connections=[])
        self.assertEqual([host for host, _ in self.calls],
                         ["hop.example", "target.example"])

    def test_a_direct_connection_is_not_wrapped_for_chain_closing(self):
        client = ssh_connect.open_client(conn("a"), connections=[])
        self.assertFalse(hasattr(client, "_rpmote_jump_hops"))


class MFAChallengeTests(unittest.TestCase):
    def test_the_challenge_carries_what_the_server_asked(self):
        challenge = MFAChallenge("h", 22, "Verification",
                                 "Enter your code", [("Code:", False)])
        self.assertEqual(challenge.prompts, [("Code:", False)])
        self.assertIn("h:22", str(challenge))


class AgentTests(unittest.TestCase):
    def test_a_missing_agent_is_an_empty_list_not_an_error(self):
        original = paramiko.Agent

        class Boom:
            def __init__(self):
                raise RuntimeError("no agent here")

        paramiko.Agent = Boom
        try:
            self.assertEqual(ssh_connect.agent_keys(), [])
            self.assertFalse(ssh_connect.has_agent())
        finally:
            paramiko.Agent = original

    def test_agent_keys_are_returned_when_one_is_running(self):
        original = paramiko.Agent

        class Fake:
            def get_keys(self):
                return ("k1", "k2")

        paramiko.Agent = Fake
        try:
            self.assertEqual(ssh_connect.agent_keys(), ["k1", "k2"])
            self.assertTrue(ssh_connect.has_agent())
        finally:
            paramiko.Agent = original


if __name__ == "__main__":
    unittest.main()
