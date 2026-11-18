"""Bounded text editing and read-only directory comparison over SFTP/FTP.

No shell commands, local plaintext temporary files, or implicit overwrites.
Each save checks the original bytes, backs them up, stages the replacement,
and asks the server to rename it over the original.
"""

from __future__ import annotations

import codecs
import difflib
import io
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MAX_EDIT_BYTES = 2 * 1024 * 1024
MAX_COMPARE_ENTRIES = 2000


class RemoteFileError(Exception):
    pass


class RemoteFileConflict(RemoteFileError):
    pass


def checked_path(path):
    path = str(path)
    if not path or any(c in path for c in "\x00\r\n"):
        raise RemoteFileError("The remote path is empty or contains control characters.")
    return path


@dataclass(frozen=True)
class TextDocument:
    raw: bytes
    text: str
    encoding: str
    bom: bytes
    newline: str

    @classmethod
    def decode(cls, raw):
        if len(raw) > MAX_EDIT_BYTES:
            raise RemoteFileError("The editor supports files up to 2 MiB. Download this file to edit it locally.")
        bom, encoding = b"", "utf-8"
        for marker, codec in ((codecs.BOM_UTF8, "utf-8"),
                              (codecs.BOM_UTF16_LE, "utf-16-le"),
                              (codecs.BOM_UTF16_BE, "utf-16-be")):
            if raw.startswith(marker):
                bom, encoding = marker, codec
                break
        try:
            text = raw[len(bom):].decode(encoding, errors="strict")
        except UnicodeError as exc:
            raise RemoteFileError("This file is not UTF-8 or BOM-marked UTF-16 text. Download it to use a local editor.") from exc
        if any(ord(c) < 32 and c not in "\t\r\n" for c in text) or any(c in text for c in "\u2028\u2029"):
            raise RemoteFileError("This appears to be a binary file or contains unsupported text controls. Download it instead.")
        crlf = text.count("\r\n")
        lf = text.count("\n") - crlf
        cr = text.count("\r") - crlf
        newline = max(((lf, "\n"), (crlf, "\r\n"), (cr, "\r")), key=lambda pair: pair[0])[1]
        return cls(bytes(raw), text.replace("\r\n", "\n").replace("\r", "\n"), encoding, bom, newline)

    def encode(self, text):
        if text == self.text:
            return self.raw
        # Keep the exact endings of unchanged lines, including mixed files.
        before = self.text.splitlines(keepends=True)
        after = text.splitlines(keepends=True)
        original = self.raw[len(self.bom):].decode(self.encoding).splitlines(keepends=True)
        output = []
        for kind, a, b, c, d in difflib.SequenceMatcher(None, before, after).get_opcodes():
            if kind == "equal":
                output.extend(original[a:b])
            else:
                output.extend(line.replace("\n", self.newline) for line in after[c:d])
        raw = self.bom + "".join(output).encode(self.encoding, errors="strict")
        # Validate user input as well as downloaded input.
        self.decode(raw)
        return raw


def text_diff(before, after, before_name="Remote original", after_name="Edited version"):
    lines = difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                 fromfile=before_name, tofile=after_name, n=3)
    out = []
    for line in lines:
        out.append(line)
        if not line.endswith("\n"):
            out.append("\n\\ No newline at end of file\n")
    return "".join(out)


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    target: str
    document: TextDocument
    metadata: object


class _SFTPFiles:
    def __init__(self, client):
        self.client = client
        self.created = set()

    def resolve(self, path):
        return checked_path(self.client.normalize(checked_path(path)))

    def read(self, path, listed_entry=None):
        info = self.client.stat(path)
        if not stat.S_ISREG(info.st_mode or 0):
            raise RemoteFileError("Choose a regular text file.")
        if info.st_size > MAX_EDIT_BYTES:
            raise RemoteFileError("The editor supports files up to 2 MiB. Download this file instead.")
        with self.client.open(path, "rb") as handle:
            raw = handle.read(MAX_EDIT_BYTES + 1)
        if len(raw) > MAX_EDIT_BYTES:
            raise RemoteFileError("The file grew beyond the editor's 2 MiB limit.")
        return raw, info

    def stage(self, path, raw, metadata, backup=False):
        # Exclusive creation avoids clobbering any existing auxiliary file.
        with self.client.open(path, "wx") as handle:
            self.created.add(path)
            self.client.chmod(path, 0o600)
            handle.write(raw)
            handle.flush()
        if not backup:
            current = self.client.stat(path)
            uid, gid = getattr(metadata, "st_uid", None), getattr(metadata, "st_gid", None)
            if uid is not None and gid is not None and (current.st_uid, current.st_gid) != (uid, gid):
                self.client.chown(path, uid, gid)
            self.client.chmod(path, stat.S_IMODE(metadata.st_mode))

    def replace(self, temporary, target):
        try:
            self.client.posix_rename(temporary, target)
        except OSError as exc:
            raise RemoteFileError("Could not confirm atomic replacement. Check server support for SFTP atomic rename and file write permissions, then reload the remote file before retrying.") from exc

    def remove(self, path):
        self.client.remove(path)

    def entries(self, path):
        return [{"name": a.filename, "size": a.st_size or 0,
                 "kind": "dir" if stat.S_ISDIR(a.st_mode or 0) else "file" if stat.S_ISREG(a.st_mode or 0) else "link"}
                for a in self.client.listdir_attr(path)]


class _FTPFiles:
    def __init__(self, client):
        self.client = client
        self.created = set()

    def resolve(self, path):
        return checked_path(path)

    def read(self, path, listed_entry=None):
        path = checked_path(path)
        parent = str(PurePosixPath(path).parent)
        name = PurePosixPath(path).name
        entry = listed_entry or next((e for e in self.entries(parent) if e["name"] == name), None)
        if entry is None or entry["kind"] != "file":
            raise RemoteFileError("Choose a regular remote file. FTP links and special files cannot be edited safely.")
        self.client.voidcmd("TYPE I")
        size = self.client.size(checked_path(path))
        if size is None or size > MAX_EDIT_BYTES:
            raise RemoteFileError("FTP editing needs a reported file size of at most 2 MiB. Download this file instead.")
        chunks, length = [], 0
        def receive(chunk):
            nonlocal length
            length += len(chunk)
            if length <= MAX_EDIT_BYTES:
                chunks.append(chunk)
        # Drain a file that changes size so the shared FTP control channel
        # remains usable; never retain more than the edit limit in memory.
        self.client.retrbinary("RETR " + path, receive)
        if length > MAX_EDIT_BYTES:
            raise RemoteFileError("The file grew beyond the editor's 2 MiB limit.")
        return b"".join(chunks), None

    def stage(self, path, raw, metadata, backup=False):
        parent, name = str(PurePosixPath(path).parent), PurePosixPath(path).name
        if any(e["name"] == name for e in self.entries(parent)):
            raise FileExistsError("An auxiliary remote file already exists; reopen the editor and retry.")
        self.created.add(path)
        self.client.storbinary("STOR " + checked_path(path), io.BytesIO(raw))

    def replace(self, temporary, target):
        self.client.rename(temporary, target)

    def remove(self, path):
        self.client.delete(path)

    def entries(self, path):
        from app.ftp_browser import FTPBrowser
        previous = self.client.pwd()
        try:
            return [{"name": e["name"], "size": e.get("size_bytes", 0),
                     "kind": "link" if e.get("is_link") else "dir" if e.get("is_dir") else "file"}
                    for e in FTPBrowser._list_remote(self.client, checked_path(path))]
        finally:
            self.client.cwd(previous)


class RemoteFileBackend:
    def __init__(self, session_factory, protocol="sftp", label="Remote host"):
        self.session_factory = session_factory
        self.protocol = protocol
        self.label = label

    @contextmanager
    def files(self):
        with self.session_factory() as client:
            yield _FTPFiles(client) if self.protocol == "ftp" else _SFTPFiles(client)

    def load(self, path):
        with self.files() as files:
            target = files.resolve(path)
            raw, metadata = files.read(target)
            return FileSnapshot(checked_path(path), target, TextDocument.decode(raw), metadata)

    def save(self, snapshot, raw):
        TextDocument.decode(raw)
        if raw == snapshot.document.raw:
            return snapshot, ""
        token = secrets.token_hex(12)
        root = PurePosixPath(snapshot.target)
        temporary = str(root.with_name(f".{root.name}.rpmote-{token}.tmp"))
        backup = str(root.with_name(f".{root.name}.rpmote-{token}.bak"))
        backed_up = False
        with self.files() as files:
            try:
                self._check_original(files, snapshot)
                files.stage(backup, snapshot.document.raw, snapshot.metadata, backup=True)
                backup_bytes, _ = files.read(backup)
                if backup_bytes != snapshot.document.raw:
                    raise RemoteFileError(f"Backup verification failed at {backup}. Reload before trying again.")
                backed_up = True
                files.stage(temporary, raw, snapshot.metadata)
                staged_bytes, _ = files.read(temporary)
                if staged_bytes != raw:
                    raise RemoteFileError("The staged upload did not match your edits. Reload before trying again.")
                # Catch a change made while the backup or upload was running.
                self._check_original(files, snapshot)
                files.replace(temporary, snapshot.target)
            except Exception as exc:
                suffix = f"\nOriginal backup: {backup}" if backed_up else ""
                kind = RemoteFileConflict if isinstance(exc, RemoteFileConflict) else RemoteFileError
                raise kind(f"{exc}{suffix}") from exc
            finally:
                if temporary in files.created:
                    try:
                        files.remove(temporary)
                    except Exception:
                        pass
        return FileSnapshot(snapshot.path, snapshot.target, TextDocument.decode(raw), snapshot.metadata), backup

    @staticmethod
    def _check_original(files, snapshot):
        if files.resolve(snapshot.path) != snapshot.target:
            raise RemoteFileConflict("The file's target changed since it was opened. Reload it before saving.")
        current, metadata = files.read(snapshot.target)
        if current != snapshot.document.raw:
            raise RemoteFileConflict("The remote file changed since it was opened. Your edits are still here; reload the remote file before saving.")
        old = snapshot.metadata
        if old is not None and metadata is not None:
            keys = ("st_mode", "st_uid", "st_gid")
            if any(getattr(old, key, None) != getattr(metadata, key, None) for key in keys):
                raise RemoteFileConflict("The remote file's permissions or ownership changed. Reload it before saving.")

    def compare_directory(self, local_root, remote_root, recursive=False):
        local_root = Path(local_root)
        if not local_root.is_dir():
            raise RemoteFileError("Choose an existing local folder.")
        local, remote = {}, {}
        def record(mapping, relative, entry):
            mapping[relative] = entry
            if len(local) + len(remote) > MAX_COMPARE_ENTRIES:
                raise RemoteFileError("Comparison reached 2,000 entries. Choose a smaller folder or turn off subfolders.")
        def walk_local(folder, prefix="", depth=0):
            if depth > 40:
                raise RemoteFileError("This directory tree is too deep to compare.")
            with os.scandir(folder) as entries:
                for e in entries:
                    rel = prefix + e.name
                    kind = "link" if e.is_symlink() else "dir" if e.is_dir() else "file" if e.is_file() else "link"
                    record(local, rel, {"kind": kind, "size": e.stat(follow_symlinks=False).st_size})
                    if recursive and kind == "dir":
                        walk_local(e.path, rel + "/", depth + 1)
        walk_local(local_root)
        rows = []
        compared_bytes = 0
        with self.files() as files:
            remote_root = files.resolve(remote_root)
            def walk_remote(folder, prefix="", depth=0):
                if depth > 40:
                    raise RemoteFileError("This directory tree is too deep to compare.")
                for e in files.entries(folder):
                    name = e["name"]
                    if name in (".", ".."):
                        continue
                    if not name or any(c in name for c in '/\\\x00\r\n:*?"<>|'):
                        raise RemoteFileError("The remote listing contains a name that cannot be compared on this PC.")
                    rel = prefix + name
                    record(remote, rel, e)
                    if recursive and e["kind"] == "dir":
                        walk_remote(str(PurePosixPath(folder) / name), rel + "/", depth + 1)
            walk_remote(remote_root)
            for rel in sorted(local.keys() | remote.keys(), key=str.casefold):
                a, b = local.get(rel), remote.get(rel)
                state = "Local only" if b is None else "Remote only" if a is None else "Type conflict"
                if a and b and a["kind"] == b["kind"]:
                    if a["kind"] == "dir":
                        state = "Folder"
                    elif a["kind"] == "link":
                        state = "Link / special file skipped"
                    elif a["size"] != b["size"]:
                        state = "Different"
                    elif a["size"] > MAX_EDIT_BYTES:
                        state = "Not compared (over 2 MiB)"
                    elif compared_bytes + a["size"] > 32 * 1024 * 1024:
                        state = "Not compared (32 MiB comparison limit)"
                    else:
                        try:
                            with (local_root / rel).open("rb") as handle:
                                local_bytes = handle.read(MAX_EDIT_BYTES + 1)
                            remote_bytes, _ = files.read(str(PurePosixPath(remote_root) / rel), listed_entry=b)
                            compared_bytes += max(len(local_bytes), len(remote_bytes))
                            state = "Identical" if local_bytes == remote_bytes else "Different"
                        except Exception as exc:
                            state = f"Cannot compare: {exc}"
                rows.append({"relative": rel, "state": state, "local": a, "remote": b,
                             "local_path": str(local_root / rel),
                             "remote_path": str(PurePosixPath(remote_root) / rel)})
        return rows


def ssh_file_backend(ssh_client, label="SSH host"):
    @contextmanager
    def session():
        transport = ssh_client.get_transport()
        if transport is None or not transport.is_active():
            raise RemoteFileError("The original SSH session is disconnected. Reconnect and reopen the file.")
        client = ssh_client.open_sftp()
        try:
            client.get_channel().settimeout(15)
            yield client
        finally:
            client.close()
    return RemoteFileBackend(session, label=label)
