"""Host key verification — known-host tracking for every SSH path in RPMote.

Every SSH connection in RPMote used to be made with paramiko's
``AutoAddPolicy``: whatever key the far end offered was trusted, and a key that
changed between sessions was re-trusted without a word. That is the one thing
an encrypted credential vault cannot compensate for, because the credentials
are handed to whoever answered on that address.

This module keeps the known-host list. It lives in the vault when one is open,
so it inherits the vault's encryption and travels with an export; when the
vault is locked (or absent, as in tests) it falls back to a JSON file in the
vault directory.

Nothing here imports Qt. The trust prompt lives in ``host_key_dialog.py`` so
that headless callers — remote restart, reachability, health polling — can
verify a host without a UI in the process.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import threading
from datetime import datetime, timezone

import paramiko
from paramiko.hostkeys import HostKeyEntry

DEFAULT_PORT = 22

#: Vault settings key holding the known-host map.
SETTINGS_KEY = "known_host_keys"
#: Vault settings key holding the verification mode.
POLICY_KEY = "host_key_policy"

#: Ask the user the first time a host is seen; refuse a changed key. (Default.)
POLICY_PROMPT = "prompt"
#: Trust the first key seen without asking; still refuse a changed key.
POLICY_TOFU = "tofu"
#: Never accept an unknown key — the host must be trusted beforehand.
POLICY_STRICT = "strict"

POLICIES = (POLICY_PROMPT, POLICY_TOFU, POLICY_STRICT)

_FILE_NAME = "known_hosts.json"


def host_entry_name(host: str, port: int | str = DEFAULT_PORT) -> str:
    """Return the lookup name paramiko uses for a host/port pair.

    Must match ``SSHClient.connect`` exactly, which uses the bare hostname on
    port 22 and the bracketed ``[host]:port`` form everywhere else — a key
    filed under the wrong name reads as an unknown host on every connect.
    """
    host = (host or "").strip()
    try:
        port = int(port or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    return host if port == DEFAULT_PORT else f"[{host}]:{port}"


def fingerprint(key) -> str:
    """Return the OpenSSH SHA256 fingerprint, matching ``ssh-keygen -lf``."""
    if key is None:
        return ""
    try:
        blob = key.asbytes()
    except Exception:
        return ""
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def legacy_fingerprint(key) -> str:
    """Return the MD5 colon-separated fingerprint older tools still print."""
    if key is None:
        return ""
    try:
        raw = binascii.hexlify(key.get_fingerprint()).decode("ascii")
    except Exception:
        return ""
    return ":".join(raw[i:i + 2] for i in range(0, len(raw), 2))


def key_algorithm(key) -> str:
    """Human-readable algorithm name for a host key."""
    if key is None:
        return "unknown"
    try:
        name = key.get_name()
    except Exception:
        return "unknown"
    pretty = {
        "ssh-ed25519": "ED25519",
        "ssh-rsa": "RSA",
        "rsa-sha2-256": "RSA",
        "rsa-sha2-512": "RSA",
        "ssh-dss": "DSA",
        "ecdsa-sha2-nistp256": "ECDSA",
        "ecdsa-sha2-nistp384": "ECDSA",
        "ecdsa-sha2-nistp521": "ECDSA",
    }.get(name, name)
    try:
        bits = key.get_bits()
    except Exception:
        bits = 0
    return f"{pretty} {bits}-bit" if bits else pretty


# ── Failures ────────────────────────────────────────────────────────


class HostKeyUnverified(Exception):
    """Base for a connection stopped because the host key was not trusted."""

    def __init__(self, host: str, port: int, key, message: str):
        super().__init__(message)
        self.host = host
        self.port = int(port or DEFAULT_PORT)
        self.key = key
        self.fingerprint = fingerprint(key)
        self.algorithm = key_algorithm(key)


class UnknownHostKey(HostKeyUnverified):
    """First time this host has been seen — the user has to vouch for it."""

    def __init__(self, host, port, key):
        super().__init__(
            host, port, key,
            f"Host key for {host}:{port} is not known ({fingerprint(key)})",
        )


class HostKeyMismatch(HostKeyUnverified):
    """The host answered with a different key than the one on record."""

    def __init__(self, host, port, key, expected_key=None, expected_fingerprints=None):
        expected = list(expected_fingerprints or [])
        if expected_key is not None and not expected:
            expected = [fingerprint(expected_key)]
        shown = ", ".join(expected) if expected else "a different key"
        super().__init__(
            host, port, key,
            f"HOST KEY CHANGED for {host}:{port} — expected {shown}, "
            f"got {fingerprint(key)}",
        )
        self.expected_key = expected_key
        self.expected_fingerprints = expected


# ── Storage ─────────────────────────────────────────────────────────


class HostKeyStore:
    """The known-host list, in the vault when it can be, on disk otherwise."""

    def __init__(self):
        self._lock = threading.RLock()
        self._vault = None
        self._cache: dict | None = None

    # -- binding ----------------------------------------------------

    def bind_vault(self, vault):
        """Use ``vault`` for storage, migrating anything held in the file."""
        with self._lock:
            self._vault = vault
            self._cache = None
            file_entries = self._read_file()
            if file_entries:
                stored = self._read_vault()
                changed = False
                for name, entries in file_entries.items():
                    known = {(e.get("type"), e.get("key")) for e in stored.get(name, [])}
                    for entry in entries:
                        if (entry.get("type"), entry.get("key")) not in known:
                            stored.setdefault(name, []).append(entry)
                            changed = True
                if changed and self._write_vault(stored):
                    self._delete_file()

    def unbind_vault(self):
        with self._lock:
            self._vault = None
            self._cache = None

    # -- backends ---------------------------------------------------

    def _file_path(self):
        # Read through the module rather than importing the name:
        # use_vault_directory() rebinds VAULT_DIR at runtime and tests rely
        # on that rebinding being visible here.
        import app.vault as vault_module
        return vault_module.VAULT_DIR / _FILE_NAME

    def _read_file(self) -> dict:
        try:
            path = self._file_path()
            if not path.exists():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _write_file(self, data: dict) -> bool:
        try:
            path = self._file_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(path)
            return True
        except Exception:
            return False

    def _delete_file(self):
        try:
            self._file_path().unlink()
        except Exception:
            pass

    def _read_vault(self) -> dict:
        vault = self._vault
        if vault is None:
            return {}
        try:
            data = vault.settings.get(SETTINGS_KEY, {})
        except Exception:
            return {}
        return dict(data) if isinstance(data, dict) else {}

    def _write_vault(self, data: dict) -> bool:
        vault = self._vault
        if vault is None:
            return False
        try:
            vault.update_settings({SETTINGS_KEY: data})
            return True
        except Exception:
            return False

    def _read(self) -> dict:
        with self._lock:
            if self._cache is not None:
                return self._cache
            data = self._read_vault() if self._vault is not None else {}
            if not data:
                # A locked vault still has to be able to verify — fall back to
                # the file rather than treating every host as brand new.
                data = self._read_file()
            clean: dict = {}
            for name, entries in data.items():
                if isinstance(entries, list):
                    clean[str(name)] = [e for e in entries if isinstance(e, dict)]
            self._cache = clean
            return clean

    def _write(self, data: dict) -> bool:
        with self._lock:
            self._cache = data
            if self._vault is not None and self._write_vault(data):
                return True
            return self._write_file(data)

    def invalidate(self):
        """Drop the in-memory copy — used after the vault is unlocked."""
        with self._lock:
            self._cache = None

    # -- policy -----------------------------------------------------

    @property
    def policy(self) -> str:
        vault = self._vault
        if vault is not None:
            try:
                mode = str(vault.settings.get(POLICY_KEY, POLICY_PROMPT))
                if mode in POLICIES:
                    return mode
            except Exception:
                pass
        return POLICY_PROMPT

    def set_policy(self, mode: str):
        if mode not in POLICIES:
            raise ValueError(f"Unknown host key policy: {mode}")
        vault = self._vault
        if vault is not None:
            try:
                vault.update_settings({POLICY_KEY: mode})
            except Exception:
                pass

    # -- queries ----------------------------------------------------

    def entries_for(self, host: str, port: int = DEFAULT_PORT) -> list:
        return list(self._read().get(host_entry_name(host, port), []))

    def is_known_host(self, host: str, port: int = DEFAULT_PORT) -> bool:
        return bool(self.entries_for(host, port))

    def is_trusted(self, host: str, port: int, key) -> bool:
        if key is None:
            return False
        try:
            blob = key.get_base64()
            name = key.get_name()
        except Exception:
            return False
        return any(
            e.get("key") == blob and e.get("type") == name
            for e in self.entries_for(host, port)
        )

    def fingerprints_for(self, host: str, port: int = DEFAULT_PORT) -> list:
        return [e.get("fingerprint") for e in self.entries_for(host, port) if e.get("fingerprint")]

    def all_hosts(self) -> dict:
        return {name: list(entries) for name, entries in self._read().items()}

    # -- mutations --------------------------------------------------

    def trust(self, host: str, port: int, key, *, replace: bool = False) -> bool:
        """Record ``key`` as trusted for this host. Returns True when stored."""
        if key is None:
            return False
        name = host_entry_name(host, port)
        try:
            entry = {
                "type": key.get_name(),
                "key": key.get_base64(),
                "fingerprint": fingerprint(key),
                "algorithm": key_algorithm(key),
                "host": host,
                "port": int(port or DEFAULT_PORT),
                "added": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        except Exception:
            return False

        with self._lock:
            data = dict(self._read())
            existing = list(data.get(name, []))
            existing = [
                e for e in existing
                if not (e.get("type") == entry["type"] and e.get("key") == entry["key"])
            ]
            if replace:
                # A key the user deliberately replaced leaves no trace of the
                # old one, so a rollback to it cannot pass unnoticed later.
                existing = [e for e in existing if e.get("type") != entry["type"]]
            existing.append(entry)
            data[name] = existing
            return self._write(data)

    def forget(self, host: str, port: int = DEFAULT_PORT) -> int:
        """Drop every key stored for a host. Returns how many were removed."""
        name = host_entry_name(host, port)
        with self._lock:
            data = dict(self._read())
            removed = len(data.pop(name, []))
            if removed:
                self._write(data)
            return removed


_STORE = HostKeyStore()


def store() -> HostKeyStore:
    """The process-wide known-host store."""
    return _STORE


def bind_vault(vault):
    _STORE.bind_vault(vault)


def unbind_vault():
    _STORE.unbind_vault()


# ── paramiko integration ────────────────────────────────────────────


class VerifyingPolicy(paramiko.MissingHostKeyPolicy):
    """Refuse unknown hosts by raising, so the caller can ask the user.

    ``AutoAddPolicy`` answers this question by saying yes. Raising instead
    hands the decision up to a layer that can show a fingerprint and wait for
    a human — or, under ``tofu``, records the key and carries on.
    """

    def __init__(self, host: str, port: int, host_store: HostKeyStore | None = None):
        self.host = host
        self.port = int(port or DEFAULT_PORT)
        self._store = host_store or _STORE

    def missing_host_key(self, client, hostname, key):
        if self._store.policy == POLICY_TOFU:
            self._store.trust(self.host, self.port, key)
            return
        raise UnknownHostKey(self.host, self.port, key)


def prepare_client(client, host: str, port: int = DEFAULT_PORT,
                   host_store: HostKeyStore | None = None,
                   extra_keys=None):
    """Load the trusted keys for this host into ``client`` and arm the policy.

    Once the keys are loaded paramiko does the comparison itself: a match
    connects, a mismatch raises ``BadHostKeyException``, and only a host with
    nothing on record reaches the policy above.

    ``extra_keys`` are trusted for this client only and never written down —
    that is what "Connect Once" hands in.
    """
    host_store = host_store or _STORE
    name = host_entry_name(host, port)
    keys = client.get_host_keys()
    for key in extra_keys or ():
        try:
            keys.add(name, key.get_name(), key)
        except Exception:
            continue
    for entry in host_store.entries_for(host, port):
        keytype = entry.get("type")
        blob = entry.get("key")
        if not keytype or not blob:
            continue
        try:
            parsed = HostKeyEntry.from_line(f"{name} {keytype} {blob}")
            if parsed is not None and parsed.key is not None:
                keys.add(name, keytype, parsed.key)
        except Exception:
            # A corrupt entry must not make the host unreachable; it simply
            # does not count as trusted, and the unknown-host path runs.
            continue
    client.set_missing_host_key_policy(VerifyingPolicy(host, port, host_store))
    return client


def connect(client, host: str, port: int = DEFAULT_PORT,
            host_store: HostKeyStore | None = None, extra_keys=None,
            **connect_kwargs):
    """``SSHClient.connect`` with host key verification wired in.

    Raises ``UnknownHostKey`` or ``HostKeyMismatch`` — both
    ``HostKeyUnverified`` — instead of trusting a key nobody vouched for.
    """
    host_store = host_store or _STORE
    prepare_client(client, host, port, host_store, extra_keys)
    # Callers migrated from client.connect(**kw) may still carry a hostname in
    # the kwargs; host/port are the arguments that count.
    connect_kwargs.pop("hostname", None)
    try:
        client.connect(hostname=host, port=int(port or DEFAULT_PORT), **connect_kwargs)
    except paramiko.BadHostKeyException as exc:
        raise HostKeyMismatch(
            host, port, exc.key, exc.expected_key,
            host_store.fingerprints_for(host, port),
        ) from exc
    return client
