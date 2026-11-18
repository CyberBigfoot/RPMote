"""Tests for SSH host key verification.

Covers the store (where keys live, and how a changed key is treated), the
paramiko wiring that does the actual comparison, and the guarantee that
matters most: an unverified host stops the connection instead of being
trusted silently.
"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import paramiko

import app.vault as vault_module
from app import host_keys
from app.host_keys import (
    HostKeyMismatch, HostKeyStore, UnknownHostKey, VerifyingPolicy,
    fingerprint, host_entry_name,
)

# Generating keys is slow enough that once for the module is worth it.
_KEY_A = paramiko.RSAKey.generate(2048)
_KEY_B = paramiko.RSAKey.generate(2048)


class _FakeVault:
    """Just enough Vault to stand in for storage."""

    def __init__(self, locked=False):
        self._settings = {}
        self.locked = locked
        self.writes = 0

    @property
    def settings(self):
        if self.locked:
            raise RuntimeError("Vault is locked")
        return self._settings

    def update_settings(self, values):
        if self.locked:
            raise RuntimeError("Vault is locked")
        self.writes += 1
        self._settings.update(values)


class _TempVaultDir(unittest.TestCase):
    """Point the vault directory at a scratch folder for file-backed tests."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_dir = vault_module.VAULT_DIR
        vault_module.use_vault_directory(self._tmp.name)
        self.store = HostKeyStore()

    def tearDown(self):
        vault_module.use_vault_directory(self._old_dir)
        self._tmp.cleanup()


class EntryNameTests(unittest.TestCase):
    def test_default_port_uses_the_bare_hostname(self):
        self.assertEqual(host_entry_name("srv.example", 22), "srv.example")

    def test_other_ports_use_the_bracketed_form_paramiko_looks_up(self):
        # This must match SSHClient.connect exactly or every connect on a
        # non-standard port reads as a brand new host.
        self.assertEqual(host_entry_name("srv.example", 2222), "[srv.example]:2222")

    def test_missing_port_is_treated_as_22(self):
        self.assertEqual(host_entry_name("srv.example", None), "srv.example")
        self.assertEqual(host_entry_name("srv.example", "not-a-port"), "srv.example")


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_the_openssh_sha256_form(self):
        fp = fingerprint(_KEY_A)
        self.assertTrue(fp.startswith("SHA256:"))
        self.assertNotIn("=", fp)          # OpenSSH strips base64 padding

    def test_different_keys_have_different_fingerprints(self):
        self.assertNotEqual(fingerprint(_KEY_A), fingerprint(_KEY_B))

    def test_a_missing_key_does_not_raise(self):
        self.assertEqual(fingerprint(None), "")


class StoreTests(_TempVaultDir):
    def test_an_unseen_host_is_not_trusted(self):
        self.assertFalse(self.store.is_known_host("srv.example", 22))
        self.assertFalse(self.store.is_trusted("srv.example", 22, _KEY_A))

    def test_trusting_a_key_makes_it_known(self):
        self.assertTrue(self.store.trust("srv.example", 22, _KEY_A))
        self.assertTrue(self.store.is_trusted("srv.example", 22, _KEY_A))
        self.assertIn(fingerprint(_KEY_A),
                      self.store.fingerprints_for("srv.example", 22))

    def test_a_different_key_on_a_known_host_is_not_trusted(self):
        self.store.trust("srv.example", 22, _KEY_A)
        self.assertFalse(self.store.is_trusted("srv.example", 22, _KEY_B))

    def test_the_same_host_on_another_port_is_a_separate_host(self):
        self.store.trust("srv.example", 22, _KEY_A)
        self.assertFalse(self.store.is_known_host("srv.example", 2222))

    def test_trust_survives_a_new_store_reading_the_same_directory(self):
        self.store.trust("srv.example", 22, _KEY_A)
        self.assertTrue(HostKeyStore().is_trusted("srv.example", 22, _KEY_A))

    def test_replacing_a_key_leaves_no_trace_of_the_old_one(self):
        # A rollback to the superseded key has to read as a mismatch again.
        self.store.trust("srv.example", 22, _KEY_A)
        self.store.trust("srv.example", 22, _KEY_B, replace=True)
        self.assertTrue(self.store.is_trusted("srv.example", 22, _KEY_B))
        self.assertFalse(self.store.is_trusted("srv.example", 22, _KEY_A))

    def test_forget_removes_the_host(self):
        self.store.trust("srv.example", 22, _KEY_A)
        self.assertEqual(self.store.forget("srv.example", 22), 1)
        self.assertFalse(self.store.is_known_host("srv.example", 22))

    def test_trusting_the_same_key_twice_stores_one_entry(self):
        self.store.trust("srv.example", 22, _KEY_A)
        self.store.trust("srv.example", 22, _KEY_A)
        self.assertEqual(len(self.store.entries_for("srv.example", 22)), 1)

    def test_a_corrupt_entry_does_not_break_the_store(self):
        path = Path(self._tmp.name) / "known_hosts.json"
        path.write_text('{"srv.example": ["not-a-dict", {"type": "x"}]}',
                        encoding="utf-8")
        self.assertEqual(len(HostKeyStore().entries_for("srv.example", 22)), 1)


class VaultStorageTests(_TempVaultDir):
    def test_a_bound_vault_holds_the_keys(self):
        vault = _FakeVault()
        self.store.bind_vault(vault)
        self.store.trust("srv.example", 22, _KEY_A)
        self.assertIn("known_host_keys", vault.settings)
        self.assertTrue(self.store.is_trusted("srv.example", 22, _KEY_A))

    def test_binding_migrates_keys_trusted_before_the_vault_was_open(self):
        # Keys can be verified before unlock (or in a test); binding must not
        # lose them, or every host looks new again after the first login.
        self.store.trust("srv.example", 22, _KEY_A)
        vault = _FakeVault()
        self.store.bind_vault(vault)
        self.assertTrue(self.store.is_trusted("srv.example", 22, _KEY_A))
        self.assertFalse((Path(self._tmp.name) / "known_hosts.json").exists())

    def test_a_locked_vault_falls_back_to_the_file_instead_of_forgetting(self):
        self.store.trust("srv.example", 22, _KEY_A)
        self.store.bind_vault(_FakeVault(locked=True))
        self.store.invalidate()
        self.assertTrue(self.store.is_trusted("srv.example", 22, _KEY_A))

    def test_policy_defaults_to_prompt(self):
        self.assertEqual(self.store.policy, host_keys.POLICY_PROMPT)

    def test_policy_round_trips_through_the_vault(self):
        vault = _FakeVault()
        self.store.bind_vault(vault)
        self.store.set_policy(host_keys.POLICY_STRICT)
        self.assertEqual(self.store.policy, host_keys.POLICY_STRICT)

    def test_an_unknown_policy_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.set_policy("whatever")


class PolicyTests(_TempVaultDir):
    def test_an_unknown_host_raises_instead_of_being_added(self):
        policy = VerifyingPolicy("srv.example", 22, self.store)
        with self.assertRaises(UnknownHostKey) as caught:
            policy.missing_host_key(None, "srv.example", _KEY_A)
        self.assertEqual(caught.exception.fingerprint, fingerprint(_KEY_A))
        self.assertFalse(self.store.is_known_host("srv.example", 22))

    def test_tofu_records_the_first_key_without_asking(self):
        vault = _FakeVault()
        self.store.bind_vault(vault)
        self.store.set_policy(host_keys.POLICY_TOFU)
        policy = VerifyingPolicy("srv.example", 22, self.store)
        policy.missing_host_key(None, "srv.example", _KEY_A)
        self.assertTrue(self.store.is_trusted("srv.example", 22, _KEY_A))

    def test_the_unknown_host_error_names_the_host_and_fingerprint(self):
        err = UnknownHostKey("srv.example", 2222, _KEY_A)
        self.assertIn("srv.example", str(err))
        self.assertIn(fingerprint(_KEY_A), str(err))
        self.assertEqual(err.port, 2222)


class MismatchTests(unittest.TestCase):
    def test_the_message_names_both_fingerprints(self):
        err = HostKeyMismatch("srv.example", 22, _KEY_B, _KEY_A)
        self.assertIn("CHANGED", str(err))
        self.assertIn(fingerprint(_KEY_A), str(err))
        self.assertIn(fingerprint(_KEY_B), str(err))

    def test_expected_fingerprints_fall_back_to_the_expected_key(self):
        err = HostKeyMismatch("srv.example", 22, _KEY_B, _KEY_A)
        self.assertEqual(err.expected_fingerprints, [fingerprint(_KEY_A)])

    def test_a_mismatch_with_no_expected_key_still_reads(self):
        err = HostKeyMismatch("srv.example", 22, _KEY_B, None, [])
        self.assertIn("a different key", str(err))


class PrepareClientTests(_TempVaultDir):
    def test_trusted_keys_are_loaded_under_the_name_paramiko_looks_up(self):
        self.store.trust("srv.example", 2222, _KEY_A)
        client = paramiko.SSHClient()
        host_keys.prepare_client(client, "srv.example", 2222, self.store)
        loaded = client.get_host_keys().lookup("[srv.example]:2222")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.get(_KEY_A.get_name()), _KEY_A)

    def test_the_policy_installed_is_the_verifying_one(self):
        client = paramiko.SSHClient()
        host_keys.prepare_client(client, "srv.example", 22, self.store)
        self.assertIsInstance(client._policy, VerifyingPolicy)

    def test_session_only_keys_are_loaded_but_never_stored(self):
        client = paramiko.SSHClient()
        host_keys.prepare_client(client, "srv.example", 22, self.store,
                                 extra_keys=[_KEY_A])
        self.assertIsNotNone(client.get_host_keys().lookup("srv.example"))
        self.assertFalse(self.store.is_known_host("srv.example", 22))

    def test_an_unparseable_stored_entry_is_skipped_not_fatal(self):
        path = Path(self._tmp.name) / "known_hosts.json"
        path.write_text(
            '{"srv.example": [{"type": "ssh-rsa", "key": "!!!not-base64!!!"}]}',
            encoding="utf-8")
        client = paramiko.SSHClient()
        host_keys.prepare_client(client, "srv.example", 22, HostKeyStore())
        self.assertIsInstance(client._policy, VerifyingPolicy)


class ConnectWrapperTests(_TempVaultDir):
    def test_a_bad_host_key_becomes_a_mismatch_with_both_fingerprints(self):
        self.store.trust("srv.example", 22, _KEY_A)

        class _Client:
            def get_host_keys(self):
                return paramiko.hostkeys.HostKeys()

            def set_missing_host_key_policy(self, policy):
                pass

            def connect(self, **kwargs):
                raise paramiko.BadHostKeyException("srv.example", _KEY_B, _KEY_A)

        with self.assertRaises(HostKeyMismatch) as caught:
            host_keys.connect(_Client(), "srv.example", 22, self.store,
                              username="u")
        self.assertEqual(caught.exception.fingerprint, fingerprint(_KEY_B))
        self.assertIn(fingerprint(_KEY_A), caught.exception.expected_fingerprints)

    def test_a_stale_hostname_in_the_kwargs_is_ignored(self):
        seen = {}

        class _Client:
            def get_host_keys(self):
                return paramiko.hostkeys.HostKeys()

            def set_missing_host_key_policy(self, policy):
                pass

            def connect(self, **kwargs):
                seen.update(kwargs)

        # Call sites migrated from client.connect(**kw) may still have a
        # hostname in the dict; host/port are the arguments that count.
        host_keys.connect(_Client(), "srv.example", 2222, self.store,
                          username="u", hostname="ignored")
        self.assertEqual(seen["hostname"], "srv.example")
        self.assertEqual(seen["port"], 2222)


class NoAutoAddAnywhereTests(unittest.TestCase):
    def test_no_ssh_path_still_trusts_whatever_answers(self):
        """AutoAddPolicy defeats the point of everything above."""
        app_dir = Path(__file__).parent / "app"
        offenders = [
            path.name
            for path in app_dir.glob("*.py")
            if "AutoAddPolicy(" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
