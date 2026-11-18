"""Drop a file on the terminal and it lands where the shell is standing.

The usual way to get a file onto a box you are already logged into is to open
a second tool, navigate it to the same directory by hand, and drag the file
there.  RPMote already knows that directory: OSC 7 has the shell reporting its
working directory on every prompt, so a drop can go straight to it.

The destination is worked out here, apart from the transfer, because getting
it wrong is the part that bites — a path is untrusted input the moment it
comes from a remote shell, and "upload to whatever the machine said" is not a
sentence anyone should write.
"""

from __future__ import annotations

import os
import threading
from pathlib import PurePosixPath

from PyQt6.QtCore import QObject, pyqtSignal

from app.file_browser_ops import count_files

#: Where a drop goes when the shell has not said where it is.
FALLBACK_DIR = "."


def normalize_remote_dir(cwd) -> str:
    """The directory to upload into, from what OSC 7 reported.

    Refuses anything that is not a plain absolute POSIX directory: a relative
    path, a Windows drive, a ``..`` segment or an embedded newline all mean
    the shell said something RPMote should not act on, and "." — the session's
    own working directory as SFTP sees it — is the safe answer.
    """
    text = str(cwd or "").strip()
    if not text or "\n" in text or "\r" in text or "\x00" in text:
        return FALLBACK_DIR
    if not text.startswith("/"):
        return FALLBACK_DIR
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return FALLBACK_DIR
    return "/" + "/".join(parts) if parts else "/"


def remote_target(cwd, local_path) -> str:
    """Where one local file ends up on the remote host."""
    name = os.path.basename(str(local_path or "").rstrip("/\\"))
    if not name:
        raise ValueError("That is not a file that can be uploaded.")
    directory = normalize_remote_dir(cwd)
    return str(PurePosixPath(directory) / name)


def describe_drop(paths, cwd) -> str:
    """What the confirmation should say a drop is about to do."""
    paths = list(paths or [])
    where = normalize_remote_dir(cwd)
    where_text = "the session's working directory" if where == FALLBACK_DIR else where
    if not paths:
        return "Nothing to upload."
    if len(paths) == 1:
        name = os.path.basename(str(paths[0]).rstrip("/\\")) or str(paths[0])
        kind = "folder" if os.path.isdir(paths[0]) else "file"
        return f"Upload the {kind} “{name}” to {where_text}?"
    return f"Upload {len(paths)} items to {where_text}?"


def local_paths_from_mime(mime) -> list:
    """Local file paths out of a drag, ignoring anything that isn't one."""
    if mime is None or not mime.hasUrls():
        return []
    paths = []
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        path = url.toLocalFile()
        if path and os.path.exists(path):
            paths.append(path)
    return paths


class TerminalUploader(QObject):
    """Puts dropped files on the remote host without blocking the terminal.

    paramiko's SFTP client is not thread-safe and the session's own channel is
    busy carrying the shell, so each run opens a channel of its own and closes
    it again.
    """

    progress = pyqtSignal(str)          # human-readable, for the status line
    finished = pyqtSignal(int, int, list)   # done, total, errors

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread = None

    @property
    def busy(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def upload(self, client, local_paths, cwd) -> bool:
        """Start an upload. False when there is nothing to do or one is running."""
        paths = [str(path) for path in (local_paths or []) if path]
        if not paths or client is None or self.busy:
            return False
        directory = normalize_remote_dir(cwd)
        self._thread = threading.Thread(
            target=self._run, args=(client, paths, directory),
            daemon=True, name="rpmote-terminal-upload")
        self._thread.start()
        return True

    def _run(self, client, paths, directory):
        errors, done = [], 0
        total = 0
        sftp = None
        try:
            total = count_files(paths)
            sftp = client.open_sftp()
            for path in paths:
                name = os.path.basename(path.rstrip("/\\")) or path
                target = str(PurePosixPath(directory) / name)
                try:
                    if os.path.isdir(path):
                        done += self._put_tree(sftp, path, target, errors)
                    else:
                        self.progress.emit(f"Uploading {name}…")
                        sftp.put(path, target)
                        done += 1
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
        except Exception as exc:
            errors.append(str(exc))
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass
        self.finished.emit(done, total, errors)

    def _put_tree(self, sftp, local_dir, remote_dir, errors) -> int:
        try:
            sftp.mkdir(remote_dir)
        except Exception:
            pass  # already there, which is fine
        done = 0
        for entry in sorted(os.listdir(local_dir)):
            local = os.path.join(local_dir, entry)
            remote = str(PurePosixPath(remote_dir) / entry)
            try:
                if os.path.isdir(local):
                    done += self._put_tree(sftp, local, remote, errors)
                else:
                    self.progress.emit(f"Uploading {entry}…")
                    sftp.put(local, remote)
                    done += 1
            except Exception as exc:
                errors.append(f"{entry}: {exc}")
        return done
