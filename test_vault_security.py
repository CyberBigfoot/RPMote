"""Vault security tests: verifier/key separation and legacy re-key migration.

These run against an isolated temp directory (the module-level vault paths are
monkeypatched in setUp) so they never touch the real ~/.rpmote vault.
"""

import base64
import json
import secrets
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

import app.vault as vault_mod
from app.vault import Vault


class VaultSecurityTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="rpmote-vault-test-"))
        self._orig_dir = vault_mod.VAULT_DIR
        # Redirect every vault path at once — vault.key and vault.recovery were
        # added later, and naming them one by one is how a real vault gets hit.
        vault_mod.use_vault_directory(self._tmp)

    def tearDown(self):
        vault_mod.use_vault_directory(self._orig_dir)
        for f in self._tmp.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            self._tmp.rmdir()
        except OSError:
            pass

    def test_stored_hash_is_not_the_encryption_key(self):
        """The plaintext verifier must NOT be reusable as the Fernet key."""
        v = Vault()
        v.setup("hunter2-secret")
        salt = vault_mod.SALT_FILE.read_bytes()
        stored = vault_mod.HASH_FILE.read_text().strip()
        key = Vault._derive_key("hunter2-secret", salt)  # urlsafe-b64 of 32 key bytes
        key_hex = base64.urlsafe_b64decode(key).hex()
        self.assertNotEqual(stored, key_hex)
        # And the stored verifier, treated as a key, cannot decrypt the vault.
        forged_key = base64.urlsafe_b64encode(bytes.fromhex(stored))
        with self.assertRaises(InvalidToken):
            Fernet(forged_key).decrypt(vault_mod.VAULT_FILE.read_bytes())

    def test_setup_unlock_roundtrip(self):
        v = Vault()
        v.setup("hunter2-secret")
        v.add_connection({"protocol": "ssh", "host": "h", "name": "n"})
        v.lock()

        v2 = Vault()
        self.assertTrue(v2.unlock("hunter2-secret"))
        self.assertTrue(v2.is_unlocked)
        self.assertEqual(len(v2.connections), 1)

    def test_wrong_password_rejected(self):
        v = Vault()
        v.setup("right-password")
        v.lock()
        v2 = Vault()
        self.assertFalse(v2.unlock("wrong-password"))

    def test_verify_password_checks_without_changing_state(self):
        """Re-auth before revealing a secret must not lock, unlock or re-key."""
        v = Vault()
        v.setup("hunter2-secret")
        v.add_connection({"protocol": "ssh", "host": "h", "name": "n"})
        salt_before = vault_mod.SALT_FILE.read_bytes()
        hash_before = vault_mod.HASH_FILE.read_text()

        self.assertTrue(v.verify_password("hunter2-secret"))
        self.assertFalse(v.verify_password("wrong-password"))
        self.assertFalse(v.verify_password(""))

        self.assertTrue(v.is_unlocked)
        self.assertEqual(len(v.connections), 1)
        self.assertEqual(vault_mod.SALT_FILE.read_bytes(), salt_before)
        self.assertEqual(vault_mod.HASH_FILE.read_text(), hash_before)

    def test_verify_password_accepts_a_legacy_verifier(self):
        """Vaults not yet migrated to v2 must still be able to re-authenticate."""
        password = "legacy-pass-123"
        salt = secrets.token_bytes(32)
        vault_mod.SALT_FILE.write_bytes(salt)
        vault_mod.HASH_FILE.write_text(Vault._hash_password_legacy(password, salt))

        v = Vault()
        self.assertTrue(v.verify_password(password))
        self.assertFalse(v.verify_password("something-else"))

    def test_verify_password_is_false_before_setup(self):
        self.assertFalse(Vault().verify_password("anything"))

    def test_legacy_vault_migrates_and_invalidates_old_hash(self):
        """A pre-v2 vault (hash == key) must still unlock, then re-key itself."""
        password = "legacy-pass-123"
        salt = secrets.token_bytes(32)
        legacy_hash = Vault._hash_password_legacy(password, salt)
        key = Vault._derive_key(password, salt)

        # Confirm the old vulnerability holds for the synthetic legacy vault:
        # the stored hash equals the encryption key bytes.
        self.assertEqual(base64.urlsafe_b64decode(key).hex(), legacy_hash)

        vault_mod.SALT_FILE.write_bytes(salt)
        vault_mod.HASH_FILE.write_text(legacy_hash)
        data = {
            "connections": [{"protocol": "ssh", "host": "old", "name": "old", "id": "x"}],
            "snippets": [],
            "settings": {},
        }
        vault_mod.VAULT_FILE.write_bytes(Fernet(key).encrypt(json.dumps(data).encode()))

        v = Vault()
        self.assertTrue(v.unlock(password))
        self.assertEqual(v.connections[0]["host"], "old")  # data survived migration

        new_salt = vault_mod.SALT_FILE.read_bytes()
        new_hash = vault_mod.HASH_FILE.read_text().strip()
        self.assertNotEqual(new_salt, salt)            # re-keyed onto a fresh salt
        self.assertNotEqual(new_hash, legacy_hash)     # verifier changed

        # The previously-leaked hash (== old key) can no longer open the vault.
        old_key = base64.urlsafe_b64encode(bytes.fromhex(legacy_hash))
        with self.assertRaises(InvalidToken):
            Fernet(old_key).decrypt(vault_mod.VAULT_FILE.read_bytes())

        # A fresh instance still unlocks with the same password.
        v2 = Vault()
        self.assertTrue(v2.unlock(password))
        self.assertEqual(v2.connections[0]["host"], "old")

    def test_change_password_after_migration(self):
        password = "legacy-pass-123"
        salt = secrets.token_bytes(32)
        vault_mod.SALT_FILE.write_bytes(salt)
        vault_mod.HASH_FILE.write_text(Vault._hash_password_legacy(password, salt))
        key = Vault._derive_key(password, salt)
        data = {"connections": [], "snippets": [], "settings": {}}
        vault_mod.VAULT_FILE.write_bytes(Fernet(key).encrypt(json.dumps(data).encode()))

        v = Vault()
        self.assertTrue(v.unlock(password))      # migrates to v2
        v.change_password(password, "new-password-456")
        v.lock()

        v2 = Vault()
        self.assertFalse(v2.unlock(password))            # old password gone
        self.assertTrue(v2.unlock("new-password-456"))   # new password works


class KeyDerivationTests(unittest.TestCase):
    """The key is derived with hashlib, not cryptography's PBKDF2HMAC: hashlib
    lets go of the GIL, so unlock derives the key and the verifier side by
    side.  Every vault already on disk was keyed by PBKDF2HMAC, so the two
    must agree byte for byte."""

    def test_derive_key_matches_cryptography_pbkdf2hmac(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        salt = secrets.token_bytes(32)
        for password in ("hunter2-secret", "p\u00e4ssw\u00f6rd \u2713 with spaces", ""):
            expected = base64.urlsafe_b64encode(PBKDF2HMAC(
                algorithm=hashes.SHA256(), length=32, salt=salt,
                iterations=vault_mod.KDF_ITERATIONS).derive(password.encode("utf-8")))
            self.assertEqual(Vault._derive_key(password, salt), expected)

    def test_key_and_verifier_together_match_each_alone(self):
        salt = secrets.token_bytes(32)
        key, verifier = Vault._derive_key_and_verifier("hunter2-secret", salt)
        self.assertEqual(key, Vault._derive_key("hunter2-secret", salt))
        self.assertEqual(verifier, Vault._hash_password("hunter2-secret", salt))
        self.assertNotEqual(base64.urlsafe_b64decode(key).hex(), verifier)


if __name__ == "__main__":
    unittest.main()
