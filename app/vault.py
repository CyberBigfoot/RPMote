"""
RPMote Credential Vault — Master-password-protected, AES-256 encrypted storage.
Uses PBKDF2-HMAC-SHA256 for key derivation and Fernet for symmetric encryption.
"""

import json
import os
import base64
import hashlib
import hmac
import secrets
import shutil
import threading
import time
from pathlib import Path
from cryptography.fernet import Fernet, InvalidToken

VAULT_DIR = Path.home() / ".rpmote"
VAULT_FILE = VAULT_DIR / "vault.enc"
SALT_FILE = VAULT_DIR / "vault.salt"
HASH_FILE = VAULT_DIR / "vault.hash"
KEY_FILE = VAULT_DIR / "vault.key"
RECOVERY_FILE = VAULT_DIR / "vault.recovery"

KDF_ITERATIONS = 600_000

#: How long a deleted connection can be brought back from Recently Deleted.
TRASH_DAYS = 30

#: The one thing standing between a copied ~/.rpmote and every password in
#: it is the master password, and PBKDF2 only buys time per guess: at 600k
#: iterations a six-character password falls to one GPU in hours.  Twelve
#: puts a dictionary-shaped one out of reach for years.  Only enforced when a
#: password is set — a vault created under the old rule still opens.
MIN_MASTER_PASSWORD_LEN = 12

# ── Data-encryption key (DEK) indirection ───────────────────────────
#
# vault.enc is encrypted with a random DEK, never with the password key
# directly.  vault.key holds that DEK wrapped under the password key, and
# vault.recovery (optional) holds the same DEK wrapped under a recovery code.
#
# That indirection is what makes both a password change and a recovery key
# possible: changing the password only re-wraps the DEK, so the recovery key
# keeps working and the vault body never has to be re-encrypted.
#
# Vaults written before this existed have no vault.key; they are migrated on
# the next successful unlock (see _ensure_dek_indirection).

#: Crockford base32 — no I, L, O or U, so a written-down code cannot be
#: misread.  25 characters carries 125 bits of entropy.
_RECOVERY_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_RECOVERY_LEN = 25
_RECOVERY_GROUP = 5
#: Same shape as _VERIFIER_DOMAIN: keeps the recovery-derived key from ever
#: colliding with a password-derived one.
_RECOVERY_DOMAIN = "rpmote-recovery-v1|"


def generate_recovery_code() -> str:
    """A fresh, human-transcribable recovery code (125 bits)."""
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_LEN))
    return "-".join(raw[i:i + _RECOVERY_GROUP]
                    for i in range(0, _RECOVERY_LEN, _RECOVERY_GROUP))


def normalize_recovery_code(code: str) -> str:
    """Canonicalise user-typed input: strip separators, fix look-alikes.

    Returns "" when the code cannot possibly be valid, so callers can reject
    typos without burning a 600k-iteration KDF round on them.
    """
    if not code:
        return ""
    cleaned = "".join(ch for ch in str(code).upper() if ch.isalnum())
    cleaned = cleaned.translate(str.maketrans({"I": "1", "L": "1", "O": "0"}))
    if len(cleaned) != _RECOVERY_LEN:
        return ""
    if any(ch not in _RECOVERY_ALPHABET for ch in cleaned):
        return ""
    return cleaned


def format_recovery_code(code: str) -> str:
    """Group a normalised code back into readable blocks."""
    norm = normalize_recovery_code(code)
    if not norm:
        return str(code or "")
    return "-".join(norm[i:i + _RECOVERY_GROUP]
                    for i in range(0, _RECOVERY_LEN, _RECOVERY_GROUP))

# Domain-separation tag for the password-verification hash. Without it, the
# stored verifier (vault.hash) was PBKDF2 over the *raw* password with the same
# salt/iterations as the encryption key — i.e. the same bytes as the key — so
# the hash file alone could decrypt the vault. Mixing this prefix in means the
# verifier can be stored in plaintext without revealing the Fernet key.
_VERIFIER_DOMAIN = b"rpmote-verify-v2|"


def use_vault_directory(path) -> Path:
    """Point every vault file at *path*.

    Test-support, and deliberately all-or-nothing: redirecting some paths by
    hand and forgetting the others is how a test ends up writing into a real
    user's ~/.rpmote.  Always use this instead of assigning the globals.
    """
    global VAULT_DIR, VAULT_FILE, SALT_FILE, HASH_FILE, KEY_FILE, RECOVERY_FILE
    VAULT_DIR = Path(path)
    VAULT_FILE = VAULT_DIR / "vault.enc"
    SALT_FILE = VAULT_DIR / "vault.salt"
    HASH_FILE = VAULT_DIR / "vault.hash"
    KEY_FILE = VAULT_DIR / "vault.key"
    RECOVERY_FILE = VAULT_DIR / "vault.recovery"
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    return VAULT_DIR


class VaultError(Exception):
    """Raised when vault operations fail."""
    pass


class Vault:
    """Encrypted credential vault with master password protection."""

    def __init__(self):
        self._fernet: Fernet | None = None
        self._dek: bytes | None = None   # data-encryption key (Fernet key bytes)
        self._data: dict = {"connections": [], "snippets": [], "settings": {}}
        self._unlocked = False
        VAULT_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _default_data() -> dict:
        return {
            "connections": [],
            "snippets": [],
            "settings": {"rdp_open_embedded_default": False},
        }

    def _normalize_data(self):
        """Migrate/normalize loaded vault data to current schema."""
        if "connections" not in self._data or not isinstance(self._data["connections"], list):
            self._data["connections"] = []
        if "snippets" not in self._data or not isinstance(self._data["snippets"], list):
            self._data["snippets"] = []
        if "settings" not in self._data or not isinstance(self._data["settings"], dict):
            self._data["settings"] = {}
        if not isinstance(self._data.get("trash"), list):
            self._data["trash"] = []
        if not isinstance(self._data.get("credentials"), list):
            self._data["credentials"] = []
        if not isinstance(self._data.get("history"), list):
            self._data["history"] = []
        self._prune_trash(save=False)

        self._data["settings"].setdefault("rdp_open_embedded_default", False)
        self._data["settings"].setdefault("ssh_terminal_style", "System Theme")
        self._data["settings"].setdefault("ssh_terminal_font", "Default Font")
        self._data["settings"].setdefault("ssh_terminal_font_size", 10)
        # Tenants where a mistake is expensive: terminals opened under one
        # wear a badge and ask before running anything destructive.
        self._data["settings"].setdefault("production_tenants", [])
        self._data["settings"].setdefault("tenant_colors", {})
        self._data["settings"].setdefault("dashboard_node_positions", {})
        self._data["settings"].setdefault("dashboard_conn_positions", {})
        self._data["settings"].setdefault("dashboard_tenant_links", [])
        self._data["settings"].setdefault("dashboard_graph_settings", {})
        self._data["settings"].setdefault("ping_settings", {})
        self._data["settings"].setdefault("restart_servers", {})
        self._data["settings"].setdefault("restart_overrides", {})
        # Verified SSH host keys, and how strictly to apply them.
        self._data["settings"].setdefault("known_host_keys", {})
        self._data["settings"].setdefault("host_key_policy", "prompt")
        # Minutes without input before the screen locks; 0 disables it.
        self._data["settings"].setdefault("idle_lock_minutes", 15)
        self._data["settings"].setdefault("dashboard_view_state", {
            "zoom": 1.0,
            "show_all_connections": False,
            "visible_tenants": [],
            "pan_x": 0.0,
            "pan_y": 0.0,
        })
        # Guard against a hand-edited or older vault handing us the wrong types.
        for key, kind in (("dashboard_conn_positions", dict),
                          ("dashboard_tenant_links", list),
                          ("dashboard_graph_settings", dict),
                          ("ping_settings", dict),
                          ("restart_servers", dict),
                          ("restart_overrides", dict),
                          ("known_host_keys", dict)):
            if not isinstance(self._data["settings"].get(key), kind):
                self._data["settings"][key] = kind()

        for conn in self._data["connections"]:
            if not isinstance(conn, dict):
                continue
            conn.setdefault("tenant", "Default")
            if conn.get("protocol", "").lower() == "rdp":
                conn.setdefault("rdp_embedded", False)
            if conn.get("protocol", "").lower() == "azure_ps":
                conn.setdefault("azure_auth_mode", "interactive")
                conn.setdefault("azure_tenant_id", "")
                conn.setdefault("azure_subscription_id", "")
                conn.setdefault("azure_client_id", "")
                conn.setdefault("azure_client_secret", "")
                conn.setdefault("azure_cloud", "AzureCloud")

    # ── Key Derivation ──────────────────────────────────────────────

    @staticmethod
    def _derive_key(password: str, salt: bytes) -> bytes:
        """Derive a Fernet-compatible key from password + salt via PBKDF2.

        PBKDF2-HMAC-SHA256, 32 bytes -- the same bytes cryptography's
        PBKDF2HMAC gives (test_vault_security pins that), from hashlib,
        which lets go of the GIL while it works so the verifier can be
        derived alongside it (_derive_key_and_verifier).
        """
        raw = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                  KDF_ITERATIONS, dklen=32)
        return base64.urlsafe_b64encode(raw)

    @classmethod
    def _derive_key_and_verifier(cls, password: str, salt: bytes) -> tuple[bytes, str]:
        """_derive_key and _hash_password at once, on two threads.

        They are two independent 600k-round derivations, so unlocking takes
        one derivation's time instead of two.  Nothing about either changes.
        """
        result: dict = {}

        def verifier():
            result["hash"] = cls._hash_password(password, salt)

        worker = threading.Thread(target=verifier, name="rpmote-kdf", daemon=True)
        worker.start()
        try:
            key = cls._derive_key(password, salt)
        finally:
            worker.join()
        if "hash" not in result:
            # The thread failed (and was logged); derive it here instead.
            result["hash"] = cls._hash_password(password, salt)
        return key, result["hash"]

    @classmethod
    def _derive_recovery_key(cls, normalized_code: str, salt: bytes) -> bytes:
        """Derive the wrapping key for a recovery code."""
        return cls._derive_key(_RECOVERY_DOMAIN + normalized_code, salt)

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes):
        """Write via a temp file + replace so a crash cannot truncate a key."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    @staticmethod
    def _hash_password(password: str, salt: bytes) -> str:
        """Derive the password-verification hash (v2).

        Domain-separated from _derive_key so this value can never be reused as
        the encryption key. It is therefore safe to store in plaintext: it
        verifies the master password without exposing the Fernet key.
        """
        return hashlib.pbkdf2_hmac(
            "sha256", _VERIFIER_DOMAIN + password.encode("utf-8"), salt, KDF_ITERATIONS
        ).hex()

    @staticmethod
    def _hash_password_legacy(password: str, salt: bytes) -> str:
        """Pre-v2 verifier — identical bytes to the encryption key.

        Retained ONLY so old vaults can be recognized and transparently
        re-keyed on unlock. Never written back to disk.
        """
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, KDF_ITERATIONS
        ).hex()

    # ── Vault State ─────────────────────────────────────────────────

    @property
    def is_setup(self) -> bool:
        """Check if a master password has been configured."""
        return SALT_FILE.exists() and HASH_FILE.exists()

    @property
    def is_unlocked(self) -> bool:
        return self._unlocked

    @property
    def connections(self) -> list[dict]:
        if not self._unlocked:
            raise VaultError("Vault is locked")
        return self._data.get("connections", [])

    @property
    def snippets(self) -> list[dict]:
        if not self._unlocked:
            raise VaultError("Vault is locked")
        return self._data.get("snippets", [])

    @property
    def settings(self) -> dict:
        if not self._unlocked:
            raise VaultError("Vault is locked")
        return self._data.get("settings", {})

    @property
    def credentials(self) -> list[dict]:
        """Shared logins that connections can point at instead of their own."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        return self._data.get("credentials", [])

    # ── Setup / Unlock / Lock ───────────────────────────────────────

    def setup(self, master_password: str):
        """Initialize the vault with a new master password."""
        if len(master_password) < MIN_MASTER_PASSWORD_LEN:
            raise VaultError(
                f"Master password must be at least {MIN_MASTER_PASSWORD_LEN} characters")

        salt = secrets.token_bytes(32)
        SALT_FILE.write_bytes(salt)

        pw_key, pw_hash = self._derive_key_and_verifier(master_password, salt)
        HASH_FILE.write_text(pw_hash)

        # Fresh vaults are born with the DEK indirection in place.
        self._dek = Fernet.generate_key()
        self._fernet = Fernet(self._dek)
        self._atomic_write_bytes(KEY_FILE, Fernet(pw_key).encrypt(self._dek))
        RECOVERY_FILE.unlink(missing_ok=True)
        self._data = self._default_data()
        self._unlocked = True
        self._save()

    def unlock(self, master_password: str) -> bool:
        """Unlock the vault with the master password. Returns True on success."""
        if not self.is_setup:
            raise VaultError("Vault not set up — create a master password first")

        salt = SALT_FILE.read_bytes()
        expected_hash = HASH_FILE.read_text().strip()
        pw_key, pw_hash = self._derive_key_and_verifier(master_password, salt)

        if hmac.compare_digest(pw_hash, expected_hash):
            verifier = "v2"
        elif hmac.compare_digest(
                self._hash_password_legacy(master_password, salt), expected_hash):
            verifier = "v1"     # pre-v2 vault — re-key once we can decrypt it
        else:
            verifier = None     # stale, corrupt, or simply the wrong password

        # vault.key may be absent (pre-DEK vault) or not belong to this
        # password at all; either way the body is the source of truth.
        wrapped_dek = None
        if KEY_FILE.exists():
            try:
                wrapped_dek = Fernet(pw_key).decrypt(KEY_FILE.read_bytes())
            except InvalidToken:
                wrapped_dek = None

        data = None
        used_wrapped = False
        if VAULT_FILE.exists():
            body = VAULT_FILE.read_bytes()
            # Fernet is authenticated, so a successful decrypt *is* proof of the
            # right password — stronger evidence than the verifier file, which
            # is only a fast pre-check and can legitimately go stale.
            for candidate, is_wrapped in ((wrapped_dek, True), (pw_key, False)):
                if candidate is None:
                    continue
                try:
                    data = json.loads(
                        Fernet(candidate).decrypt(body).decode("utf-8"))
                    used_wrapped = is_wrapped
                    break
                except (InvalidToken, json.JSONDecodeError):
                    continue
            if data is None:
                if KEY_FILE.exists() and wrapped_dek is None and verifier is not None:
                    # The verifier accepts this password but vault.key does not
                    # belong to it, so the data key is gone. Only a recovery key
                    # holds another copy.
                    raise VaultError(
                        "The vault key file does not match this password. If "
                        "you have a recovery key, use 'Forgot your password?' "
                        "to get back in."
                    )
                if verifier is None:
                    return False        # wrong password
                raise VaultError("Vault data is corrupted or password is incorrect")
        else:
            # Nothing to decrypt, so the verifier is all we have to go on.
            if verifier is None:
                return False
            data = self._default_data()
            used_wrapped = wrapped_dek is not None

        self._dek = wrapped_dek if used_wrapped else pw_key
        self._fernet = Fernet(self._dek)
        self._data = data
        self._normalize_data()
        self._unlocked = True

        # A vault.key that this password cannot unwrap, on a body that opened
        # without it, is stale — drop it so the migration writes a correct one.
        stale_key_file = KEY_FILE.exists() and not used_wrapped
        if stale_key_file:
            try:
                KEY_FILE.unlink(missing_ok=True)
            except OSError:
                pass

        # Split the data key away from the password key so a recovery key (and
        # a cheap password change) become possible.
        self._ensure_dek_indirection(master_password)

        if verifier != "v2" or stale_key_file:
            # Pre-v2 verifier, or one that no longer matches a password we have
            # just cryptographically proven. Rewrite it onto a fresh salt.
            self._rekey(master_password)

        return True

    def _ensure_dek_indirection(self, master_password: str) -> bool:
        """Move a legacy vault onto a random data key.  Best-effort.

        Nothing is written until the new ciphertext has been decrypted back and
        compared, and both files land atomically — a failure here leaves the
        vault exactly as it was and simply means no recovery key can be made
        until it succeeds.
        """
        if KEY_FILE.exists():
            return True
        try:
            salt = SALT_FILE.read_bytes()
            pw_key = self._derive_key(master_password, salt)
            new_dek = Fernet.generate_key()
            payload = json.dumps(self._data, indent=2).encode("utf-8")
            body = Fernet(new_dek).encrypt(payload)
            wrapped = Fernet(pw_key).encrypt(new_dek)

            # Prove the whole chain before replacing anything.
            check_dek = Fernet(pw_key).decrypt(wrapped)
            if check_dek != new_dek or Fernet(check_dek).decrypt(body) != payload:
                raise VaultError("re-key verification failed")

            self._atomic_write_bytes(VAULT_FILE, body)
            self._atomic_write_bytes(KEY_FILE, wrapped)
        except Exception:
            return False
        self._dek = new_dek
        self._fernet = Fernet(new_dek)
        return True

    # ── Recovery Key ────────────────────────────────────────────────

    @property
    def has_recovery_key(self) -> bool:
        return RECOVERY_FILE.exists()

    def create_recovery_key(self) -> str:
        """Generate a recovery code that can also open this vault.

        Returns the code in plain text **once** — only the wrapped data key is
        stored, so a lost code cannot be read back out of the vault.  Any
        previously issued code stops working immediately.
        """
        if not self._unlocked:
            raise VaultError("Vault must be unlocked to create a recovery key")
        if not KEY_FILE.exists() or not self._dek:
            raise VaultError(
                "This vault could not be upgraded for recovery keys. "
                "Change your master password once, then try again."
            )
        code = generate_recovery_code()
        salt = secrets.token_bytes(32)
        rec_key = self._derive_recovery_key(normalize_recovery_code(code), salt)
        blob = {
            "v": 1,
            "salt": base64.b64encode(salt).decode("ascii"),
            "wrapped": Fernet(rec_key).encrypt(self._dek).decode("ascii"),
        }
        self._atomic_write_bytes(
            RECOVERY_FILE, json.dumps(blob, indent=2).encode("utf-8"))
        return code

    def clear_recovery_key(self):
        """Revoke the current recovery key, if any."""
        if not self._unlocked:
            raise VaultError("Vault must be unlocked to remove a recovery key")
        RECOVERY_FILE.unlink(missing_ok=True)

    def unlock_with_recovery_key(self, code: str) -> bool:
        """Open the vault with a recovery code instead of the password.

        The master password is *not* revealed by this — the code unwraps the
        data key only.  Callers should immediately set a new password via
        set_password_after_recovery().
        """
        if not self.is_setup:
            raise VaultError("Vault not set up — create a master password first")
        if not RECOVERY_FILE.exists():
            raise VaultError("No recovery key has been set up for this vault")

        normalized = normalize_recovery_code(code)
        if not normalized:
            return False

        try:
            blob = json.loads(RECOVERY_FILE.read_text(encoding="utf-8"))
            salt = base64.b64decode(blob["salt"])
            wrapped = blob["wrapped"].encode("ascii")
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            raise VaultError("Recovery key file is corrupted")

        rec_key = self._derive_recovery_key(normalized, salt)
        try:
            dek = Fernet(rec_key).decrypt(wrapped)
        except InvalidToken:
            return False

        self._dek = dek
        self._fernet = Fernet(dek)
        if VAULT_FILE.exists():
            try:
                self._data = json.loads(
                    self._fernet.decrypt(VAULT_FILE.read_bytes()).decode("utf-8"))
                self._normalize_data()
            except (InvalidToken, json.JSONDecodeError):
                raise VaultError("Vault data is corrupted")
        else:
            self._data = self._default_data()
        self._unlocked = True
        return True

    def set_password_after_recovery(self, new_password: str):
        """Set a brand-new master password without knowing the old one.

        Only valid on a vault opened with a recovery key.  The data key is
        untouched, so the recovery code stays valid and the vault body is not
        re-encrypted.
        """
        if not self._unlocked or not self._dek:
            raise VaultError("Vault must be unlocked to set a new password")
        if len(new_password) < MIN_MASTER_PASSWORD_LEN:
            raise VaultError(
                f"New password must be at least {MIN_MASTER_PASSWORD_LEN} characters")
        self._write_password_material(new_password)

    def verify_password(self, master_password: str) -> bool:
        """Check a master password without changing any vault state.

        Used to re-authenticate before revealing stored secrets: the vault is
        already unlocked at that point, so there is nothing to decrypt — this
        only confirms the person at the keyboard is the vault owner.
        """
        if not self.is_setup:
            return False
        try:
            salt = SALT_FILE.read_bytes()
            expected = HASH_FILE.read_text().strip()
        except OSError:
            return False
        return (
            hmac.compare_digest(self._hash_password(master_password, salt), expected)
            or hmac.compare_digest(
                self._hash_password_legacy(master_password, salt), expected)
        )

    def lock(self):
        """Lock the vault, clearing sensitive data from memory."""
        self._fernet = None
        self._dek = None
        self._data = self._default_data()
        self._unlocked = False

    def factory_reset(self):
        """Permanently delete ALL vault data and reset to fresh state."""
        self.lock()
        # Remove all vault files
        for f in [VAULT_FILE, SALT_FILE, HASH_FILE, KEY_FILE, RECOVERY_FILE]:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        # Remove any other files in the vault directory — backups included:
        # "delete everything" has to mean the encrypted copies as well.
        if VAULT_DIR.exists():
            for child in VAULT_DIR.iterdir():
                try:
                    if child.is_dir() and child.name == "backups":
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                except OSError:
                    pass

    def change_password(self, old_password: str, new_password: str):
        """Change the master password. Vault must be unlocked."""
        if not self._unlocked:
            raise VaultError("Vault must be unlocked to change password")
        if len(new_password) < MIN_MASTER_PASSWORD_LEN:
            raise VaultError(
                f"New password must be at least {MIN_MASTER_PASSWORD_LEN} characters")

        salt = SALT_FILE.read_bytes()
        expected = HASH_FILE.read_text().strip()
        if not (hmac.compare_digest(self._hash_password(old_password, salt), expected)
                or hmac.compare_digest(
                    self._hash_password_legacy(old_password, salt), expected)):
            raise VaultError("Current password is incorrect")

        # A legacy vault has to gain the DEK indirection before the new
        # password can simply re-wrap it.
        self._ensure_dek_indirection(old_password)
        self._write_password_material(new_password)

    def _rekey(self, master_password: str):
        """Re-encrypt the vault under a fresh salt + key and a v2 verifier.

        Used to upgrade pre-v2 vaults on unlock. Generating a new salt also
        invalidates any previously-copied vault.hash (which, under the old
        scheme, equalled the encryption key).
        """
        self._write_password_material(master_password)

    def _write_password_material(self, password: str):
        """Point a (new) password at the existing data key.

        Writes a fresh salt, a v2 verifier and a freshly wrapped copy of the
        DEK.  The vault body is never touched, so this is both instant and safe
        for any recovery key already issued.
        """
        if not self._dek:
            raise VaultError("No data key — vault not unlocked")
        new_salt = secrets.token_bytes(32)
        new_key, new_hash = self._derive_key_and_verifier(password, new_salt)
        wrapped = Fernet(new_key).encrypt(self._dek)
        self._atomic_write_bytes(KEY_FILE, wrapped)
        self._atomic_write_bytes(SALT_FILE, new_salt)
        self._atomic_write_bytes(HASH_FILE, new_hash.encode("ascii"))

    # ── CRUD Operations ─────────────────────────────────────────────

    def add_connection(self, connection: dict) -> str:
        """Add a connection and return its generated ID."""
        if not self._unlocked:
            raise VaultError("Vault is locked")

        conn_id = secrets.token_hex(8)
        connection.setdefault("tenant", "Default")
        if connection.get("password"):
            # Saved Passwords warns about passwords nobody has rotated.
            connection.setdefault("password_changed_at", time.time())
        if connection.get("protocol", "").lower() == "rdp":
            connection.setdefault("rdp_embedded", False)
        if connection.get("protocol", "").lower() == "azure_ps":
            connection.setdefault("azure_auth_mode", "interactive")
            connection.setdefault("azure_tenant_id", "")
            connection.setdefault("azure_organization", "")
            connection.setdefault("azure_use_delegated_org", False)
            connection.setdefault("azure_sharepoint_url", "")
            connection.setdefault("azure_pnp_client_id", "")
            connection.setdefault("azure_upn", "")
            connection.setdefault("azure_subscription_id", "")
            connection.setdefault("azure_client_id", "")
            connection.setdefault("azure_client_secret", "")
            connection.setdefault("azure_cloud", "AzureCloud")
            connection.setdefault("azure_services", ["azure"])
        connection["id"] = conn_id
        self._data["connections"].append(connection)
        self._save()
        return conn_id

    def update_connection(self, conn_id: str, updates: dict):
        """Update fields of an existing connection."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        for conn in self._data["connections"]:
            if conn["id"] == conn_id:
                if "password" in updates and updates["password"] != conn.get("password"):
                    updates = dict(updates)
                    updates["password_changed_at"] = time.time()
                conn.update(updates)
                conn["id"] = conn_id  # prevent ID overwrite
                self._save()
                return
        raise VaultError(f"Connection {conn_id} not found")

    def delete_connection(self, conn_id: str, permanent: bool = False):
        """Delete a connection by ID (kept in Recently Deleted unless *permanent*)."""
        self.delete_connections([conn_id], permanent=permanent)

    def delete_connections(self, conn_ids, permanent: bool = False) -> int:
        """Delete several connections at once. Returns how many went."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        wanted = {str(c) for c in (conn_ids or []) if str(c)}
        gone = [c for c in self._data["connections"] if str(c.get("id", "")) in wanted]
        if not gone:
            return 0
        self._data["connections"] = [
            c for c in self._data["connections"] if str(c.get("id", "")) not in wanted
        ]
        if not permanent:
            self._trash_add(gone)
        self._save()
        return len(gone)

    def delete_connections_by_tenant(self, tenant_name: str, permanent: bool = False) -> int:
        """Delete all connections belonging to a tenant. Returns count deleted."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        ids = [c.get("id") for c in self._data["connections"]
               if (c.get("tenant", "Default") or "Default") == tenant_name]
        return self.delete_connections(ids, permanent=permanent)

    # ── Recently deleted ───────────────────────────────────────────

    def _trash_add(self, connections):
        now = time.time()
        trash = self._data.setdefault("trash", [])
        for conn in connections:
            if isinstance(conn, dict):
                trash.append({"deleted_at": now, "connection": dict(conn)})
        self._prune_trash(save=False)

    def _prune_trash(self, save: bool = True):
        """Forget deletions older than TRASH_DAYS."""
        cutoff = time.time() - TRASH_DAYS * 86400
        before = self._data.get("trash", [])
        kept = []
        for entry in before if isinstance(before, list) else []:
            try:
                if (isinstance(entry, dict) and isinstance(entry.get("connection"), dict)
                        and float(entry.get("deleted_at", 0)) >= cutoff):
                    kept.append(entry)
            except (TypeError, ValueError):
                continue
        changed = len(kept) != len(before if isinstance(before, list) else [])
        self._data["trash"] = kept
        if changed and save and self._unlocked:
            self._save()

    @property
    def trash(self) -> list[dict]:
        """Recently deleted connections, newest first: {deleted_at, connection}."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        return sorted((dict(e) for e in self._data.get("trash", [])),
                      key=lambda e: float(e.get("deleted_at", 0)), reverse=True)

    def restore_from_trash(self, conn_ids) -> int:
        """Bring deleted connections back. Returns how many were restored."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        wanted = {str(c) for c in (conn_ids or [])}
        live_ids = {str(c.get("id", "")) for c in self._data["connections"]}
        keep, restored = [], 0
        for entry in self._data.get("trash", []):
            conn = entry.get("connection") or {}
            cid = str(conn.get("id", ""))
            if cid in wanted:
                conn = dict(conn)
                if not cid or cid in live_ids:
                    conn["id"] = secrets.token_hex(8)
                self._data["connections"].append(conn)
                live_ids.add(str(conn["id"]))
                restored += 1
                wanted.discard(cid)
            else:
                keep.append(entry)
        if restored:
            self._data["trash"] = keep
            self._save()
        return restored

    def purge_trash(self, conn_ids=None) -> int:
        """Delete for good: the given ids, or everything when None."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        before = list(self._data.get("trash", []))
        if conn_ids is None:
            self._data["trash"] = []
        else:
            wanted = {str(c) for c in conn_ids}
            self._data["trash"] = [e for e in before
                                   if str((e.get("connection") or {}).get("id", "")) not in wanted]
        removed = len(before) - len(self._data["trash"])
        if removed:
            self._save()
        return removed

    # ── Shared credentials ─────────────────────────────────────────

    def add_credential(self, credential: dict) -> str:
        if not self._unlocked:
            raise VaultError("Vault is locked")
        cred = dict(credential or {})
        cred["id"] = secrets.token_hex(8)
        cred["updated_at"] = time.time()
        self._data.setdefault("credentials", []).append(cred)
        self._save()
        return cred["id"]

    def update_credential(self, cred_id: str, updates: dict):
        if not self._unlocked:
            raise VaultError("Vault is locked")
        for cred in self._data.get("credentials", []):
            if str(cred.get("id")) == str(cred_id):
                secret_changed = any(
                    k in updates and updates[k] != cred.get(k)
                    for k in ("password", "key_file", "key_passphrase"))
                cred.update(updates)
                cred["id"] = cred_id
                if secret_changed:
                    cred["updated_at"] = time.time()
                self._save()
                return
        raise VaultError(f"Credential {cred_id} not found")

    def delete_credential(self, cred_id: str) -> int:
        """Remove a shared credential; connections using it keep a copy of
        the login so nothing silently stops working. Returns how many
        connections were detached."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        cred = self.get_credential(cred_id)
        if cred is None:
            return 0
        detached = 0
        for conn in self._data["connections"]:
            if str(conn.get("credential_id", "")) == str(cred_id):
                conn.pop("credential_id", None)
                for key in ("username", "password", "key_file", "key_passphrase"):
                    if cred.get(key):
                        conn[key] = cred[key]
                detached += 1
        self._data["credentials"] = [c for c in self._data.get("credentials", [])
                                     if str(c.get("id")) != str(cred_id)]
        self._save()
        return detached

    def get_credential(self, cred_id: str) -> dict | None:
        if not self._unlocked:
            raise VaultError("Vault is locked")
        for cred in self._data.get("credentials", []):
            if str(cred.get("id")) == str(cred_id):
                return dict(cred)
        return None

    def resolve_credentials(self, conn: dict) -> dict:
        """*conn* with its shared credential's login filled in.

        The connection's own record is never changed: rotating the shared
        password is one edit, and every connection using it follows.
        """
        if not isinstance(conn, dict):
            return conn
        cred_id = str(conn.get("credential_id", "") or "")
        if not cred_id or not self._unlocked:
            return conn
        cred = self.get_credential(cred_id)
        if cred is None:
            return conn
        resolved = dict(conn)
        for key in ("username", "password", "key_file", "key_passphrase"):
            if cred.get(key) not in (None, ""):
                resolved[key] = cred[key]
        resolved["credential_name"] = cred.get("name", "")
        return resolved

    # ── Snippet CRUD ──────────────────────────────────────────────

    def add_snippet(self, snippet: dict) -> str:
        """Add a command snippet."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        snippet["id"] = secrets.token_hex(8)
        self._data["snippets"].append(snippet)
        self._save()
        return snippet["id"]

    def update_snippet(self, snippet_id: str, updates: dict):
        """Update a command snippet."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        for snip in self._data["snippets"]:
            if snip["id"] == snippet_id:
                snip.update(updates)
                snip["id"] = snippet_id
                self._save()
                return
        raise VaultError(f"Snippet {snippet_id} not found")

    def delete_snippet(self, snippet_id: str):
        """Delete a command snippet."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        self._data["snippets"] = [
            s for s in self._data["snippets"] if s["id"] != snippet_id
        ]
        self._save()

    def get_connection(self, conn_id: str) -> dict | None:
        """Get a connection by ID."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        for conn in self._data["connections"]:
            if conn["id"] == conn_id:
                return conn.copy()
        return None

    def update_settings(self, settings: dict):
        """Update vault settings."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        self._data["settings"].update(settings)
        self._save()

    def reorder_connections(self, ordered_ids: list[str]):
        """Reorder saved connections by the given ID order."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        if not ordered_ids:
            return

        id_map = {
            str(conn.get("id", "")).strip(): conn
            for conn in self._data["connections"]
            if isinstance(conn, dict)
        }

        seen: set[str] = set()
        reordered: list[dict] = []

        for conn_id in ordered_ids:
            key = str(conn_id).strip()
            if not key or key in seen:
                continue
            conn = id_map.get(key)
            if conn is not None:
                reordered.append(conn)
                seen.add(key)

        # Keep any connections not present in ordered_ids at the end
        for conn in self._data["connections"]:
            key = str(conn.get("id", "")).strip() if isinstance(conn, dict) else ""
            if key and key not in seen:
                reordered.append(conn)

        self._data["connections"] = reordered
        self._save()

    # ── Import / Export ─────────────────────────────────────────────

    @staticmethod
    def _connection_identity_key(conn: dict) -> tuple:
        """Stable key used to detect duplicate/same-target imported connections."""
        if not isinstance(conn, dict):
            return tuple()
        proto = str(conn.get("protocol", "")).strip().lower()
        host = str(conn.get("host", "")).strip().lower()
        port = str(conn.get("port", "")).strip().lower()
        tenant = str(conn.get("tenant", "Default") or "Default").strip().lower()
        return proto, host, port, tenant

    @staticmethod
    def _normalize_connection_for_import(conn: dict) -> dict:
        """Prepare imported records for merge/add while preserving current schema."""
        data = dict(conn or {})
        data.setdefault("tenant", "Default")

        protocol = str(data.get("protocol", "")).strip().lower()
        if protocol == "nas":
            data["protocol"] = "synology"
        elif protocol in ("azure powershell", "azure-powershell", "azps"):
            data["protocol"] = "azure_ps"

        if str(data.get("protocol", "")).lower() == "rdp":
            data.setdefault("rdp_embedded", False)
        if str(data.get("protocol", "")).lower() == "azure_ps":
            data.setdefault("azure_auth_mode", "interactive")
            data.setdefault("azure_tenant_id", "")
            data.setdefault("azure_subscription_id", "")
            data.setdefault("azure_client_id", "")
            data.setdefault("azure_client_secret", "")
            data.setdefault("azure_cloud", "AzureCloud")

        return data

    def export_connections(self, filepath: str, password: str):
        """Export all connections to an encrypted file.

        The file holds every credential in the vault, so its password gets
        the same floor as the master password.
        """
        if not self._unlocked:
            raise VaultError("Vault is locked")
        if len(password) < MIN_MASTER_PASSWORD_LEN:
            raise VaultError(
                f"Export password must be at least {MIN_MASTER_PASSWORD_LEN} characters")
        salt = secrets.token_bytes(32)
        key = self._derive_key(password, salt)
        f = Fernet(key)
        payload = json.dumps(self._data["connections"]).encode("utf-8")
        encrypted = f.encrypt(payload)
        with open(filepath, "wb") as fh:
            fh.write(salt + encrypted)

    def import_connections(self, filepath: str, password: str) -> int:
        """Import/merge connections from encrypted file. Returns count changed."""
        if not self._unlocked:
            raise VaultError("Vault is locked")
        with open(filepath, "rb") as fh:
            raw = fh.read()
        salt = raw[:32]
        encrypted = raw[32:]
        key = self._derive_key(password, salt)
        f = Fernet(key)
        try:
            decrypted = f.decrypt(encrypted)
            connections = json.loads(decrypted.decode("utf-8"))
        except (InvalidToken, json.JSONDecodeError):
            raise VaultError("Invalid password or corrupted export file")

        existing = [c for c in self._data.get("connections", []) if isinstance(c, dict)]
        existing_by_key: dict[tuple, dict] = {
            self._connection_identity_key(c): c
            for c in existing
            if self._connection_identity_key(c)
        }

        changed = 0
        for raw in connections:
            if not isinstance(raw, dict):
                continue

            imported = self._normalize_connection_for_import(raw)
            key = self._connection_identity_key(imported)
            if not key:
                continue

            found = existing_by_key.get(key)
            if found is not None:
                # Update existing in-place (credentials and other fields included), preserve ID.
                preserved_id = found.get("id")
                found.update(imported)
                if preserved_id:
                    found["id"] = preserved_id
                changed += 1
                continue

            imported["id"] = secrets.token_hex(8)
            self._data["connections"].append(imported)
            existing_by_key[key] = imported
            changed += 1

        self._save()
        return changed

    # ── Internal ────────────────────────────────────────────────────

    def _save(self):
        """Encrypt and persist vault data to disk."""
        if not self._fernet:
            raise VaultError("No encryption key — vault not unlocked")
        payload = json.dumps(self._data, indent=2).encode("utf-8")
        encrypted = self._fernet.encrypt(payload)
        # Temp file + replace: a crash or power cut mid-write leaves the
        # previous vault intact instead of a truncated one nothing can open.
        self._atomic_write_bytes(VAULT_FILE, encrypted)
