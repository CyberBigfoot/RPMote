"""Windows Credential Manager, spoken to directly.

mstsc finds a saved login by asking Credential Manager under a target name
and a credential *type* it decides, and the two hops of a gateway session
are asked for differently:

  the desktop     a **generic** credential under ``TERMSRV/<host>`` — the
                  long-standing ``cmdkey /generic`` trick
  the RD Gateway  a **Domain Password** credential under the bare gateway
                  address, exactly as it appears in ``gatewayhostname``.
                  A generic one there is ignored, and so is anything under
                  ``TERMSRV/<gateway>``; that was established by trying each
                  in turn against mstsc on Windows 11 and watching which one
                  made the "Enter your credentials" prompt not appear.

Putting both there ahead of time is what turns "open the connection" into
"the desktop appears" rather than "Windows asks for the password you already
typed into the vault".

This used to be done by running ``cmdkey``.  That put the password on a child
process's command line, where any process on the machine can read it, and in
the windowed build it flashed a console window for every call.  ``CredWriteW``
is the API ``cmdkey`` itself calls, minus both problems.

A credential in Credential Manager is *outside* the vault: a generic one is
handed back in plain text to any process running as the user, no master
password asked.  So nothing written here is meant to outlive its use.  Every
entry is session-scoped (Windows drops it at logoff), the launcher releases
it when the mstsc it was written for exits, and :func:`purge_saved_credentials`
sweeps up anything still carrying the RPMote comment — on start-up, on vault
lock and on exit — so a crash cannot leave a customer's password behind.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from dataclasses import dataclass, field

_LOG = logging.getLogger("rpmote.rdp.credentials")

CRED_TYPE_GENERIC = 1
CRED_TYPE_DOMAIN_PASSWORD = 2
#: Gone at logoff.  mstsc reads a session credential exactly as it reads a
#: persisted one; the only difference is what survives the user leaving.
CRED_PERSIST_SESSION = 1
CRED_PERSIST_LOCAL_MACHINE = 2

_TYPE_NAMES = {CRED_TYPE_GENERIC: "generic", CRED_TYPE_DOMAIN_PASSWORD: "domain"}

#: What ``CredWriteW`` allows for one generic credential, in bytes.
CRED_MAX_CREDENTIAL_BLOB_SIZE = 5 * 512
CRED_MAX_GENERIC_TARGET_NAME_LENGTH = 32767
CRED_MAX_USERNAME_LENGTH = 513

#: Left on every credential written here so they can be told apart from the
#: ones mstsc saved itself when looking through Credential Manager.
CREDENTIAL_COMMENT = "Saved by RPMote"


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi32():
    if sys.platform != "win32":
        raise OSError("Credential Manager is only available on Windows")
    lib = ctypes.WinDLL("advapi32", use_last_error=True)
    lib.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
    lib.CredWriteW.restype = wintypes.BOOL
    lib.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    lib.CredDeleteW.restype = wintypes.BOOL
    return lib


def _last_error_text() -> str:
    code = ctypes.get_last_error()
    try:
        return f"{ctypes.FormatError(code).strip()} (error {code})"
    except Exception:
        return f"error {code}"


@dataclass
class CredentialWrite:
    """One attempt to store a login for one target."""

    target: str
    ok: bool
    reason: str = "ok"
    cred_type: int = CRED_TYPE_GENERIC


@dataclass
class CredentialReport:
    """Every write made on behalf of one connection, so a launch can say what
    it did and a log can show it."""

    writes: list[CredentialWrite] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """At least one target took the credential."""
        return any(w.ok for w in self.writes)

    @property
    def attempted(self) -> bool:
        return bool(self.writes)

    @property
    def targets_written(self) -> list[str]:
        return [w.target for w in self.writes if w.ok]

    @property
    def failure(self) -> str:
        """The reason the last failed write gave, or an empty string."""
        for w in reversed(self.writes):
            if not w.ok:
                return w.reason
        return ""


class WindowsCredentialStore:
    """The real thing: Credential Manager, through advapi32."""

    def write(self, target: str, username: str, blob: bytes, comment: str,
              cred_type: int = CRED_TYPE_GENERIC) -> CredentialWrite:
        try:
            advapi32 = _advapi32()
            buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
            cred = _CREDENTIALW()
            cred.Flags = 0
            cred.Type = cred_type
            cred.TargetName = target
            cred.Comment = comment or None
            cred.CredentialBlobSize = len(blob)
            cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
            cred.Persist = CRED_PERSIST_SESSION
            cred.AttributeCount = 0
            cred.Attributes = None
            cred.TargetAlias = None
            cred.UserName = username
            if not advapi32.CredWriteW(ctypes.byref(cred), 0):
                reason = _last_error_text()
                _LOG.warning("CredWriteW failed for %s (%s): %s",
                             target, _TYPE_NAMES.get(cred_type, cred_type), reason)
                return CredentialWrite(target, False, reason, cred_type)
        except Exception as exc:  # pragma: no cover - only on a broken Windows
            _LOG.warning("CredWriteW raised for %s: %s", target, exc)
            return CredentialWrite(target, False, str(exc), cred_type)
        _LOG.info("Stored %s credential for %s as %s",
                  _TYPE_NAMES.get(cred_type, cred_type), target, username)
        return CredentialWrite(target, True, "ok", cred_type)

    def delete(self, target: str, cred_type: int = CRED_TYPE_GENERIC) -> bool:
        try:
            advapi32 = _advapi32()
            if advapi32.CredDeleteW(target, cred_type, 0):
                return True
            # ERROR_NOT_FOUND: nothing to delete is the outcome we wanted.
            return ctypes.get_last_error() == 1168
        except Exception:
            return False

    def enumerate(self) -> list[tuple[str, int, str]]:
        """Every credential of this user as ``(target, type, comment)``.

        Blobs are deliberately not read back: nothing here needs a password
        out of Credential Manager, only to know which entries are ours.
        """
        try:
            advapi32 = _advapi32()
        except OSError:
            return []
        count = wintypes.DWORD(0)
        creds = ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))()
        if not advapi32.CredEnumerateW(None, 0, ctypes.byref(count), ctypes.byref(creds)):
            # ERROR_NOT_FOUND: an empty Credential Manager.
            if ctypes.get_last_error() != 1168:
                _LOG.warning("CredEnumerateW failed: %s", _last_error_text())
            return []
        try:
            out = []
            for i in range(count.value):
                c = creds[i].contents
                out.append((c.TargetName or "", int(c.Type), c.Comment or ""))
            return out
        finally:
            advapi32.CredFree(creds)


class MemoryCredentialStore:
    """A stand-in for tests: remembers what would have been written and
    never touches the machine.  ``fail_targets`` makes named writes fail so
    the reporting around a refused write can be exercised too."""

    def __init__(self, fail_targets: set[str] | None = None):
        self.saved: dict[str, tuple[str, str]] = {}
        self.kinds: dict[str, int] = {}
        self.fail_targets = {t.lower() for t in (fail_targets or set())}

    def write(self, target: str, username: str, blob: bytes, comment: str,
              cred_type: int = CRED_TYPE_GENERIC) -> CredentialWrite:
        if target.lower() in self.fail_targets:
            return CredentialWrite(target, False, "refused by test store", cred_type)
        self.saved[target] = (username, blob.decode("utf-16-le"))
        self.kinds[target] = cred_type
        return CredentialWrite(target, True, "ok", cred_type)

    def delete(self, target: str, cred_type: int = CRED_TYPE_GENERIC) -> bool:
        self.saved.pop(target, None)
        self.kinds.pop(target, None)
        return True

    def enumerate(self) -> list[tuple[str, int, str]]:
        return [(t, self.kinds.get(t, CRED_TYPE_GENERIC), CREDENTIAL_COMMENT)
                for t in self.saved]

    @property
    def targets(self) -> list[str]:
        return list(self.saved)


_STORE = WindowsCredentialStore()


def use_credential_store(store):
    """Swap the backend — the test suite's way of keeping its hands off the
    real Credential Manager.  Returns the store that was in use."""
    global _STORE
    previous = _STORE
    _STORE = store
    return previous


def store_credential(target: str, username: str, password: str,
                     cred_type: int = CRED_TYPE_GENERIC,
                     comment: str = CREDENTIAL_COMMENT) -> CredentialWrite:
    """Write one credential the way mstsc expects to read it.

    The password is stored as UTF-16LE without a terminator — that is what
    mstsc and ``cmdkey`` both write, and mstsc reads the blob back as text of
    exactly that length.  A target that already holds a credential of this
    type is overwritten, which is the point: the vault is the source of truth
    and a password changed there has to win.
    """
    target = str(target or "").strip()
    username = str(username or "").strip()
    password = str(password or "")
    if not target or not username or not password:
        return CredentialWrite(target, False, "missing_fields")
    if len(target) > CRED_MAX_GENERIC_TARGET_NAME_LENGTH:
        return CredentialWrite(target, False, "target name too long")
    if len(username) > CRED_MAX_USERNAME_LENGTH:
        return CredentialWrite(target, False, "username too long")

    blob = password.encode("utf-16-le")
    if len(blob) > CRED_MAX_CREDENTIAL_BLOB_SIZE:
        return CredentialWrite(target, False, "password too long for Credential Manager", cred_type)
    return _STORE.write(target, username, blob, comment, cred_type)


def store_generic_credential(target: str, username: str, password: str,
                             comment: str = CREDENTIAL_COMMENT) -> CredentialWrite:
    """The desktop's kind of credential — see the module docstring."""
    return store_credential(target, username, password, CRED_TYPE_GENERIC, comment)


def delete_credential(target: str, cred_type: int = CRED_TYPE_GENERIC) -> bool:
    """Remove a credential; True if it is gone (or never existed)."""
    target = str(target or "").strip()
    if not target:
        return False
    return _STORE.delete(target, cred_type)


def release_credentials(report: CredentialReport) -> int:
    """Remove every credential a report says was written.  Returns how many
    are now gone.  Idempotent: releasing twice, or releasing a report whose
    writes all failed, does nothing."""
    gone = 0
    for w in report.writes:
        if not w.ok:
            continue
        if delete_credential(w.target, w.cred_type):
            gone += 1
            _LOG.info("Released %s credential for %s",
                      _TYPE_NAMES.get(w.cred_type, w.cred_type), w.target)
        else:
            _LOG.warning("Could not release credential for %s", w.target)
    return gone


def purge_saved_credentials(comment: str = CREDENTIAL_COMMENT) -> int:
    """Delete every credential RPMote ever wrote, recognised by its comment.

    The backstop for the per-launch release: a crash, a kill from Task
    Manager or a build from before credentials were session-scoped can all
    leave entries behind, and each one is a customer password readable
    without the vault.  Entries mstsc or the user saved themselves carry a
    different comment and are left alone.  Returns the number removed.
    """
    removed = 0
    for target, cred_type, found_comment in _STORE.enumerate():
        if found_comment != comment:
            continue
        if _STORE.delete(target, cred_type):
            removed += 1
        else:
            _LOG.warning("Could not purge credential for %s", target)
    if removed:
        _LOG.info("Purged %d credential(s) left in Credential Manager", removed)
    return removed


def store_credentials(targets: list[str], username: str, password: str,
                      cred_type: int = CRED_TYPE_GENERIC) -> CredentialReport:
    """Store one login under several target names.

    A host is sometimes asked for with its port and sometimes without, so a
    login is written under every spelling it might be asked for; each write
    is reported separately so a log can show which one failed.
    """
    report = CredentialReport()
    seen: set[str] = set()
    for target in targets:
        target = str(target or "").strip()
        if not target or target.lower() in seen:
            continue
        seen.add(target.lower())
        report.writes.append(store_credential(target, username, password, cred_type))
    return report
