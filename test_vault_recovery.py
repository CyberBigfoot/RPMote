"""Recovery-key tests: DEK indirection, recovery codes, and legacy migration.

These run against an isolated temp directory (the module-level vault paths are
monkeypatched in setUp) so they never touch the real ~/.rpmote vault.
"""

import base64
import hashlib
import json
import secrets
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

import app.vault as vault_mod
from app.vault import (
    Vault,
    VaultError,
    format_recovery_code,
    generate_recovery_code,
    normalize_recovery_code,
)

PASSWORD = "correct-horse-battery"


class _VaultTempDirMixin(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rpmote_recovery_")
        root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        vault_mod.use_vault_directory(root)
        self.root = root

    def fresh_vault(self, password: str = PASSWORD) -> Vault:
        v = Vault()
        v.setup(password)
        v.add_connection({"name": "DC01", "host": "10.0.0.1",
                          "protocol": "ssh", "tenant": "Contoso",
                          "password": "s3cret"})
        return v

    def write_legacy_vault(self, password: str = PASSWORD, v1_verifier: bool = False):
        """Recreate the pre-DEK on-disk format: no vault.key at all."""
        salt = secrets.token_bytes(32)
        vault_mod.SALT_FILE.write_bytes(salt)
        if v1_verifier:
            digest = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), salt,
                vault_mod.KDF_ITERATIONS).hex()
        else:
            digest = Vault._hash_password(password, salt)
        vault_mod.HASH_FILE.write_text(digest)

        data = {
            "connections": [{"id": "abc123", "name": "Legacy-DC", "host": "10.9.9.9",
                             "protocol": "ssh", "tenant": "Fabrikam",
                             "password": "legacy-secret"}],
            "snippets": [],
            "settings": {"tenant_colors": {"Fabrikam": "#4ade80"}},
        }
        key = Vault._derive_key(password, salt)
        vault_mod.VAULT_FILE.write_bytes(
            Fernet(key).encrypt(json.dumps(data).encode("utf-8")))
        vault_mod.KEY_FILE.unlink(missing_ok=True)
        return data


class RecoveryCodeFormatTests(_VaultTempDirMixin):

    def test_generated_codes_are_grouped_and_unique(self):
        codes = {generate_recovery_code() for _ in range(200)}
        self.assertEqual(len(codes), 200, "codes must not repeat")
        for code in codes:
            self.assertEqual(len(code), 29, code)          # 25 chars + 4 dashes
            self.assertEqual(code.count("-"), 4, code)
            self.assertEqual(len(normalize_recovery_code(code)), 25)

    def test_generated_codes_avoid_ambiguous_letters(self):
        blob = "".join(generate_recovery_code() for _ in range(300))
        for ch in "ILOU":
            self.assertNotIn(ch, blob, f"{ch} is too easy to misread")

    def test_normalisation_forgives_realistic_typing(self):
        code = generate_recovery_code()
        canonical = normalize_recovery_code(code)
        for variant in (code.lower(), code.replace("-", ""),
                        code.replace("-", " "), f"  {code}  ",
                        code.replace("-", " — ")):
            self.assertEqual(normalize_recovery_code(variant), canonical, variant)

    def test_look_alike_characters_map_to_digits(self):
        self.assertEqual(normalize_recovery_code("I" * 25), "1" * 25)
        self.assertEqual(normalize_recovery_code("L" * 25), "1" * 25)
        self.assertEqual(normalize_recovery_code("O" * 25), "0" * 25)

    def test_malformed_codes_are_rejected_without_hashing(self):
        for bad in ("", None, "too-short", "!" * 25, "A" * 24, "A" * 26,
                    "U" * 25):
            self.assertEqual(normalize_recovery_code(bad), "", repr(bad))

    def test_format_round_trips(self):
        code = generate_recovery_code()
        self.assertEqual(format_recovery_code(normalize_recovery_code(code)), code)


class RecoveryKeyTests(_VaultTempDirMixin):

    def test_recovery_code_opens_the_vault(self):
        v = self.fresh_vault()
        code = v.create_recovery_key()
        v.lock()

        v2 = Vault()
        self.assertTrue(v2.unlock_with_recovery_key(code))
        self.assertEqual(v2.connections[0]["name"], "DC01")
        self.assertEqual(v2.connections[0]["password"], "s3cret")

    def test_recovery_code_is_accepted_in_any_reasonable_format(self):
        v = self.fresh_vault()
        code = v.create_recovery_key()
        v.lock()
        for variant in (code.lower(), code.replace("-", ""), f" {code} "):
            with self.subTest(variant=variant):
                self.assertTrue(Vault().unlock_with_recovery_key(variant))

    def test_a_wrong_code_is_rejected(self):
        v = self.fresh_vault()
        v.create_recovery_key()
        v.lock()
        v2 = Vault()
        self.assertFalse(v2.unlock_with_recovery_key(generate_recovery_code()))
        self.assertFalse(v2.is_unlocked)
        with self.assertRaises(VaultError):
            _ = v2.connections

    def test_a_malformed_code_is_rejected(self):
        v = self.fresh_vault()
        v.create_recovery_key()
        v.lock()
        self.assertFalse(Vault().unlock_with_recovery_key("nope"))

    def test_recovery_is_unavailable_until_one_is_created(self):
        v = self.fresh_vault()
        self.assertFalse(v.has_recovery_key)
        v.lock()
        with self.assertRaises(VaultError):
            Vault().unlock_with_recovery_key(generate_recovery_code())

    def test_creating_a_new_code_revokes_the_old_one(self):
        v = self.fresh_vault()
        first = v.create_recovery_key()
        second = v.create_recovery_key()
        self.assertNotEqual(first, second)
        v.lock()
        self.assertFalse(Vault().unlock_with_recovery_key(first))
        self.assertTrue(Vault().unlock_with_recovery_key(second))

    def test_clearing_the_recovery_key_revokes_it(self):
        v = self.fresh_vault()
        code = v.create_recovery_key()
        self.assertTrue(v.has_recovery_key)
        v.clear_recovery_key()
        self.assertFalse(v.has_recovery_key)
        v.lock()
        with self.assertRaises(VaultError):
            Vault().unlock_with_recovery_key(code)

    def test_the_code_itself_is_never_written_to_disk(self):
        v = self.fresh_vault()
        code = v.create_recovery_key()
        normalized = normalize_recovery_code(code)
        for path in self.root.iterdir():
            blob = path.read_bytes()
            self.assertNotIn(normalized.encode(), blob, f"{path.name} leaks the code")
            self.assertNotIn(code.encode(), blob, f"{path.name} leaks the code")

    def test_the_recovery_file_does_not_leak_the_master_password(self):
        v = self.fresh_vault()
        v.create_recovery_key()
        blob = vault_mod.RECOVERY_FILE.read_bytes()
        self.assertNotIn(PASSWORD.encode(), blob)
        # ...and the wrapped payload is the data key, not the password key.
        salt = vault_mod.SALT_FILE.read_bytes()
        self.assertNotIn(Vault._derive_key(PASSWORD, salt), blob)

    def test_the_recovery_file_alone_cannot_open_the_vault(self):
        v = self.fresh_vault()
        v.create_recovery_key()
        v.lock()
        blob = json.loads(vault_mod.RECOVERY_FILE.read_text())
        wrapped = blob["wrapped"].encode()
        salt = base64.b64decode(blob["salt"])
        # The stored salt is not itself a usable key.
        with self.assertRaises(Exception):
            Fernet(base64.urlsafe_b64encode(salt)).decrypt(wrapped)

    def test_a_corrupt_recovery_file_reports_clearly(self):
        v = self.fresh_vault()
        code = v.create_recovery_key()
        v.lock()
        vault_mod.RECOVERY_FILE.write_text("{not json")
        with self.assertRaises(VaultError):
            Vault().unlock_with_recovery_key(code)

    def test_recovery_key_requires_an_unlocked_vault(self):
        v = self.fresh_vault()
        v.lock()
        with self.assertRaises(VaultError):
            v.create_recovery_key()
        with self.assertRaises(VaultError):
            v.clear_recovery_key()


class PasswordAndRecoveryInteractionTests(_VaultTempDirMixin):

    def test_recovery_code_survives_a_password_change(self):
        v = self.fresh_vault()
        code = v.create_recovery_key()
        v.change_password(PASSWORD, "a-brand-new-password")
        v.lock()

        self.assertTrue(Vault().unlock(  "a-brand-new-password"))
        self.assertTrue(Vault().unlock_with_recovery_key(code))

    def test_password_change_does_not_re_encrypt_the_vault_body(self):
        v = self.fresh_vault()
        before = vault_mod.VAULT_FILE.read_bytes()
        v.change_password(PASSWORD, "another-password")
        self.assertEqual(vault_mod.VAULT_FILE.read_bytes(), before,
                         "the body should not be rewritten — only the wrapped key")

    def test_setting_a_new_password_after_recovery(self):
        v = self.fresh_vault()
        code = v.create_recovery_key()
        v.lock()

        rescued = Vault()
        self.assertTrue(rescued.unlock_with_recovery_key(code))
        rescued.set_password_after_recovery("chosen-after-lockout")
        rescued.lock()

        self.assertTrue(Vault().unlock("chosen-after-lockout"))
        self.assertFalse(Vault().unlock(PASSWORD),
                         "the forgotten password must stop working")
        # The same code still works, so a second lockout is survivable.
        self.assertTrue(Vault().unlock_with_recovery_key(code))

    def test_data_survives_recovery_and_a_password_reset(self):
        v = self.fresh_vault()
        v.add_connection({"name": "EX01", "host": "10.0.0.2",
                          "protocol": "rdp", "tenant": "Contoso"})
        code = v.create_recovery_key()
        v.lock()

        rescued = Vault()
        rescued.unlock_with_recovery_key(code)
        rescued.set_password_after_recovery("new-master-password")
        rescued.lock()

        reopened = Vault()
        reopened.unlock("new-master-password")
        self.assertEqual([c["name"] for c in reopened.connections], ["DC01", "EX01"])
        self.assertEqual(reopened.connections[0]["password"], "s3cret")

    def test_writes_after_recovery_persist(self):
        v = self.fresh_vault()
        code = v.create_recovery_key()
        v.lock()

        rescued = Vault()
        rescued.unlock_with_recovery_key(code)
        rescued.add_connection({"name": "Added-After-Recovery", "host": "10.0.0.9",
                                "protocol": "ssh", "tenant": "Contoso"})
        rescued.set_password_after_recovery("post-recovery-pw")
        rescued.lock()

        reopened = Vault()
        reopened.unlock("post-recovery-pw")
        self.assertIn("Added-After-Recovery",
                      [c["name"] for c in reopened.connections])

    def test_new_password_must_meet_the_length_rule(self):
        v = self.fresh_vault()
        code = v.create_recovery_key()
        v.lock()
        rescued = Vault()
        rescued.unlock_with_recovery_key(code)
        with self.assertRaises(VaultError):
            rescued.set_password_after_recovery("abc")

    def test_factory_reset_removes_the_recovery_key(self):
        v = self.fresh_vault()
        code = v.create_recovery_key()
        v.factory_reset()
        self.assertFalse(vault_mod.RECOVERY_FILE.exists())
        self.assertFalse(vault_mod.KEY_FILE.exists())
        self.assertFalse(v.is_setup)
        with self.assertRaises(VaultError):
            Vault().unlock_with_recovery_key(code)


class LegacyVaultMigrationTests(_VaultTempDirMixin):

    def test_a_pre_dek_vault_still_unlocks(self):
        expected = self.write_legacy_vault()
        v = Vault()
        self.assertTrue(v.unlock(PASSWORD))
        self.assertEqual(v.connections[0]["name"],
                         expected["connections"][0]["name"])
        self.assertEqual(v.connections[0]["password"], "legacy-secret")

    def test_unlocking_a_pre_dek_vault_migrates_it(self):
        self.write_legacy_vault()
        self.assertFalse(vault_mod.KEY_FILE.exists())
        v = Vault()
        v.unlock(PASSWORD)
        self.assertTrue(vault_mod.KEY_FILE.exists(), "migration should add vault.key")
        v.lock()
        # ...and the same password still opens the migrated vault.
        again = Vault()
        self.assertTrue(again.unlock(PASSWORD))
        self.assertEqual(again.connections[0]["password"], "legacy-secret")

    def test_migration_detaches_the_data_key_from_the_password(self):
        self.write_legacy_vault()
        v = Vault()
        v.unlock(PASSWORD)
        salt = vault_mod.SALT_FILE.read_bytes()
        pw_key = Vault._derive_key(PASSWORD, salt)
        self.assertNotEqual(v._dek, pw_key,
                            "the data key must be independent of the password")
        # The body must no longer be openable with the password key directly.
        with self.assertRaises(Exception):
            Fernet(pw_key).decrypt(vault_mod.VAULT_FILE.read_bytes())

    def test_a_migrated_vault_can_issue_a_recovery_key(self):
        self.write_legacy_vault()
        v = Vault()
        v.unlock(PASSWORD)
        code = v.create_recovery_key()
        v.lock()
        rescued = Vault()
        self.assertTrue(rescued.unlock_with_recovery_key(code))
        self.assertEqual(rescued.connections[0]["password"], "legacy-secret")

    def test_a_v1_verifier_vault_migrates_and_keeps_its_data(self):
        self.write_legacy_vault(v1_verifier=True)
        v = Vault()
        self.assertTrue(v.unlock(PASSWORD))
        self.assertEqual(v.connections[0]["password"], "legacy-secret")
        v.lock()
        again = Vault()
        self.assertTrue(again.unlock(PASSWORD))
        self.assertTrue(vault_mod.KEY_FILE.exists())

    def test_a_wrong_password_still_fails_on_a_legacy_vault(self):
        self.write_legacy_vault()
        self.assertFalse(Vault().unlock("not-the-password"))
        self.assertFalse(vault_mod.KEY_FILE.exists(),
                         "a failed unlock must not migrate anything")

    def test_migration_leaves_the_vault_usable_if_it_cannot_write(self):
        """A read-only key file must not cost the user their unlock."""
        self.write_legacy_vault()
        original = Vault._atomic_write_bytes

        def explode(path, data):
            if path == vault_mod.KEY_FILE:
                raise OSError("disk is having a bad day")
            return original(path, data)

        Vault._atomic_write_bytes = staticmethod(explode)
        self.addCleanup(setattr, Vault, "_atomic_write_bytes",
                        staticmethod(original))
        v = Vault()
        self.assertTrue(v.unlock(PASSWORD), "unlock must survive a failed migration")
        self.assertEqual(v.connections[0]["password"], "legacy-secret")
        with self.assertRaises(VaultError):
            v.create_recovery_key()


class VerifierSelfHealTests(_VaultTempDirMixin):
    """The ciphertext is the source of truth; vault.hash is only a pre-check.

    A stray process (a mis-scoped test, a half-finished migration, a restored
    backup) can leave vault.hash and vault.key describing a different password
    than the body. The correct password must still open the vault and repair
    the metadata rather than locking the owner out of their own data.
    """

    def _corrupt_verifier(self, foreign_password="some-other-password"):
        """Point vault.hash at a different password.

        vault.hash is redundant — the authenticated ciphertext is what actually
        proves the password — so this has to be survivable.
        """
        salt = vault_mod.SALT_FILE.read_bytes()
        vault_mod.HASH_FILE.write_text(
            Vault._hash_password(foreign_password, salt))

    def test_a_stale_verifier_does_not_lock_the_owner_out(self):
        v = self.fresh_vault()
        v.lock()
        self._corrupt_verifier()

        rescued = Vault()
        self.assertTrue(rescued.unlock(PASSWORD),
                        "the real password must still open the real ciphertext")
        self.assertEqual(rescued.connections[0]["password"], "s3cret")

    def test_the_stale_verifier_is_repaired_in_place(self):
        v = self.fresh_vault()
        v.lock()
        self._corrupt_verifier()

        Vault().unlock(PASSWORD)

        salt = vault_mod.SALT_FILE.read_bytes()
        self.assertEqual(vault_mod.HASH_FILE.read_text().strip(),
                         Vault._hash_password(PASSWORD, salt))
        again = Vault()
        self.assertTrue(again.unlock(PASSWORD))
        self.assertEqual(again.connections[0]["password"], "s3cret")

    def test_a_spurious_key_file_on_a_pre_dek_vault_is_discarded(self):
        """Exactly the damage a mis-scoped test run does: a stray vault.key and
        a rewritten vault.hash dropped on top of a legacy vault."""
        self.write_legacy_vault()
        salt = vault_mod.SALT_FILE.read_bytes()
        vault_mod.HASH_FILE.write_text(Vault._hash_password("not-it", salt))
        foreign = Vault._derive_key("not-it", salt)
        vault_mod.KEY_FILE.write_bytes(
            Fernet(foreign).encrypt(Fernet.generate_key()))

        rescued = Vault()
        self.assertTrue(rescued.unlock(PASSWORD))
        self.assertEqual(rescued.connections[0]["password"], "legacy-secret")
        rescued.lock()
        # ...and it is properly migrated afterwards, not merely limping.
        again = Vault()
        self.assertTrue(again.unlock(PASSWORD))
        self.assertTrue(vault_mod.KEY_FILE.exists())

    def test_a_destroyed_key_file_says_to_use_the_recovery_key(self):
        """On a DEK vault the wrapped key is the only copy of the data key.
        Losing it is fatal without a recovery key, and the message must say so
        rather than blaming the password."""
        v = self.fresh_vault()
        v.lock()
        salt = vault_mod.SALT_FILE.read_bytes()
        foreign = Vault._derive_key("some-other-password", salt)
        vault_mod.KEY_FILE.write_bytes(
            Fernet(foreign).encrypt(Fernet.generate_key()))

        with self.assertRaises(VaultError) as caught:
            Vault().unlock(PASSWORD)
        self.assertIn("recovery key", str(caught.exception).lower())

    def test_a_recovery_key_survives_a_destroyed_key_file(self):
        v = self.fresh_vault()
        code = v.create_recovery_key()
        v.lock()
        salt = vault_mod.SALT_FILE.read_bytes()
        foreign = Vault._derive_key("some-other-password", salt)
        vault_mod.KEY_FILE.write_bytes(
            Fernet(foreign).encrypt(Fernet.generate_key()))

        rescued = Vault()
        self.assertTrue(rescued.unlock_with_recovery_key(code))
        self.assertEqual(rescued.connections[0]["password"], "s3cret")
        rescued.set_password_after_recovery("back-in-business")
        rescued.lock()
        self.assertTrue(Vault().unlock("back-in-business"))

    def assertDenied(self, password):
        """No access, whether that surfaces as False or a raised VaultError."""
        vault = Vault()
        try:
            self.assertFalse(vault.unlock(password))
        except VaultError:
            pass
        self.assertFalse(vault.is_unlocked)
        with self.assertRaises(VaultError):
            _ = vault.connections

    def test_healing_still_rejects_a_genuinely_wrong_password(self):
        v = self.fresh_vault()
        v.lock()
        self._corrupt_verifier()
        self.assertDenied("definitely-not-it")
        # Matching the stale verifier is not enough either: the ciphertext is
        # what decides, and it will not open.
        self.assertDenied("some-other-password")

    def test_a_healed_vault_can_still_issue_a_recovery_key(self):
        v = self.fresh_vault()
        v.lock()
        self._corrupt_verifier()
        rescued = Vault()
        rescued.unlock(PASSWORD)
        code = rescued.create_recovery_key()
        rescued.lock()
        self.assertTrue(Vault().unlock_with_recovery_key(code))


class VaultPathIsolationTests(_VaultTempDirMixin):

    def test_every_vault_path_lives_under_the_vault_dir(self):
        """Guards against a new vault file escaping test redirection.

        A path left pointing at the real ~/.rpmote is how a test run ends up
        writing into a live vault.
        """
        paths = {name: value for name, value in vars(vault_mod).items()
                 if name.endswith("_FILE") and isinstance(value, Path)}
        self.assertGreaterEqual(len(paths), 5, paths)
        for name, path in paths.items():
            self.assertEqual(path.parent, vault_mod.VAULT_DIR,
                             f"{name} escapes the vault directory")

    def test_a_full_lifecycle_touches_nothing_outside_the_vault_dir(self):
        before = {p.name for p in self.root.iterdir()}
        v = self.fresh_vault()
        code = v.create_recovery_key()
        v.change_password(PASSWORD, "second-password")
        v.lock()
        rescued = Vault()
        rescued.unlock_with_recovery_key(code)
        rescued.set_password_after_recovery("third-password")
        rescued.lock()

        created = {p.name for p in self.root.iterdir()} - before
        self.assertTrue(created)
        for name in created:
            self.assertTrue((self.root / name).exists())
        # Nothing may have leaked into a real user's vault directory.
        self.assertEqual(vault_mod.VAULT_DIR, self.root)


class TamperResistanceTests(_VaultTempDirMixin):

    def test_a_corrupt_key_file_is_reported_not_silently_ignored(self):
        v = self.fresh_vault()
        v.lock()
        vault_mod.KEY_FILE.write_bytes(b"garbage")
        with self.assertRaises(VaultError):
            Vault().unlock(PASSWORD)

    def test_a_swapped_key_file_cannot_open_the_body(self):
        """A vault.key from another vault must not unlock this one."""
        v = self.fresh_vault()
        v.lock()
        victim_body = vault_mod.VAULT_FILE.read_bytes()

        # Build a second, unrelated vault and steal its wrapped key.
        for f in (vault_mod.VAULT_FILE, vault_mod.SALT_FILE,
                  vault_mod.HASH_FILE, vault_mod.KEY_FILE):
            f.unlink(missing_ok=True)
        other = Vault()
        other.setup(PASSWORD)
        foreign_key = vault_mod.KEY_FILE.read_bytes()
        other.lock()

        vault_mod.VAULT_FILE.write_bytes(victim_body)
        vault_mod.KEY_FILE.write_bytes(foreign_key)
        with self.assertRaises(VaultError):
            Vault().unlock(PASSWORD)

    def test_wrong_password_never_reaches_the_data_key(self):
        v = self.fresh_vault()
        v.lock()
        v2 = Vault()
        self.assertFalse(v2.unlock("wrong-password"))
        self.assertIsNone(v2._dek)
        self.assertIsNone(v2._fernet)


if __name__ == "__main__":
    unittest.main()
