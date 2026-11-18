"""Every session, written down as it happens, and playable back afterwards.

Touching a customer's infrastructure without a record of what was on the
screen is a bad place to be when someone asks what happened at 3am.  So every
SSH session records itself, in `asciicast v2`_ — the format asciinema uses:
one JSON header line, then one JSON array per chunk of output, each stamped
with the seconds since the session opened.

Two consequences of that choice are worth stating.  It keeps the ANSI intact,
so a replay shows the colours, the progress bars and the full-screen editor
exactly as they appeared — a stripped text log throws all of that away and
cannot be replayed at all.  And it is a format other tools already read, so a
recording can leave RPMote and still be worth something.

**Only output is recorded.**  Keystrokes are not, and the difference matters:
a sudo prompt does not echo, so a transcript of what the machine printed has
no password in it, while a transcript of what was typed would.

Recordings are plain files in the user's profile — readable by anything that
can read the user's home directory, unlike the vault.  That is the price of
an audit trail that survives the process that made it; the retention sweep
exists so it is not also an unbounded one.

.. _asciicast v2: https://docs.asciinema.org/manual/asciicast/v2/
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

#: Where recordings live. Under the same directory as the vault, so a user
#: who knows where their data is only has one place to look.
SESSIONS_DIR = Path.home() / ".rpmote" / "sessions"

#: Anything older than this is swept up on startup.
DEFAULT_KEEP_DAYS = 30
#: ...and the whole directory is held under this, newest kept.
DEFAULT_MAX_BYTES = 512 * 1024 * 1024

CAST_SUFFIX = ".cast"


def use_sessions_directory(path) -> Path:
    """Point recordings at *path* instead of the user's profile.

    Test-support, and the same shape as the vault's own redirect: a test that
    opens a terminal must not leave a transcript in a real person's home
    directory.
    """
    global SESSIONS_DIR
    SESSIONS_DIR = Path(path)
    return SESSIONS_DIR

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(text, fallback="session") -> str:
    """A filename fragment that cannot escape the directory it is put in."""
    cleaned = _UNSAFE_NAME.sub("-", str(text or "")).strip("-.")
    return (cleaned or fallback)[:48]


def session_filename(conn, when=None) -> str:
    """``2026-09-09T14-05-31_acme-web01_10.0.0.5.cast``."""
    conn = conn if isinstance(conn, dict) else {}
    stamp = (when or datetime.now()).strftime("%Y-%m-%dT%H-%M-%S")
    name = safe_name(conn.get("name") or conn.get("host") or "session")
    host = safe_name(conn.get("host") or "", fallback="host")
    return f"{stamp}_{name}_{host}{CAST_SUFFIX}"


def session_path(conn, when=None, root=None) -> Path:
    """Full path for a new recording, in a per-day folder."""
    when = when or datetime.now()
    root = Path(root) if root is not None else SESSIONS_DIR
    return root / when.strftime("%Y-%m-%d") / session_filename(conn, when)


def format_size(num_bytes) -> str:
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


class SessionRecorder:
    """Writes one session's output to one asciicast file.

    Opened lazily: a session that never produces a byte leaves no file behind,
    which keeps the folder free of stubs from connections that failed.  Every
    write is flushed, because the recording that matters most is the one
    belonging to the session that just died.
    """

    def __init__(self, path, width=80, height=24, title="", env=None,
                 started=None):
        self._path = Path(path)
        self._width = max(1, int(width or 80))
        self._height = max(1, int(height or 24))
        self._title = str(title or "")
        self._env = dict(env or {})
        self._started = float(started if started is not None else time.time())
        self._handle = None
        self._closed = False
        self._bytes = 0
        self._events = 0
        self._failed = False

    # ── State ───────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return self._path

    @property
    def started(self) -> float:
        return self._started

    @property
    def bytes_written(self) -> int:
        return self._bytes

    @property
    def event_count(self) -> int:
        return self._events

    @property
    def active(self) -> bool:
        return bool(self._handle) and not self._closed

    @property
    def failed(self) -> bool:
        """True once a write has failed — a full disk must not kill a session."""
        return self._failed

    # ── Writing ─────────────────────────────────────────────────────

    def _open(self) -> bool:
        if self._handle is not None or self._closed or self._failed:
            return self._handle is not None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self._path, "w", encoding="utf-8", newline="\n")
            self._restrict_permissions()
            header = {
                "version": 2,
                "width": self._width,
                "height": self._height,
                "timestamp": int(self._started),
            }
            if self._title:
                header["title"] = self._title
            if self._env:
                header["env"] = self._env
            self._handle.write(json.dumps(header) + "\n")
            self._handle.flush()
            return True
        except Exception:
            self._failed = True
            self._handle = None
            return False

    def _restrict_permissions(self):
        """Keep other local users out where the OS makes that a one-liner."""
        try:
            os.chmod(self._path, 0o600)
        except Exception:
            pass

    def _emit(self, kind: str, payload: str, when=None):
        if self._closed or self._failed:
            return
        if not self._open():
            return
        offset = round(max(0.0, (when if when is not None else time.time())
                           - self._started), 6)
        try:
            line = json.dumps([offset, kind, payload],
                              ensure_ascii=False, separators=(",", ":"))
            self._handle.write(line + "\n")
            self._handle.flush()
        except Exception:
            self._failed = True
            self.close()
            return
        self._bytes += len(payload)
        self._events += 1

    def write_output(self, text, when=None):
        """Record a chunk exactly as it came off the wire."""
        if text:
            self._emit("o", str(text), when)

    def resize(self, width, height, when=None):
        width, height = max(1, int(width or 1)), max(1, int(height or 1))
        if (width, height) == (self._width, self._height):
            return
        self._width, self._height = width, height
        self._emit("r", f"{width}x{height}", when)

    def note(self, text, when=None):
        """Record something RPMote itself wants remembered (a disconnect)."""
        if text:
            self._emit("m", str(text), when)

    def close(self):
        self._closed = True
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


# ── Reading one back ────────────────────────────────────────────────


class CastError(Exception):
    """A recording could not be read."""


def read_cast(path):
    """``(header, events)`` from an asciicast file.

    Damaged lines are skipped rather than fatal: a recording cut short by a
    crash is exactly the one someone needs, and it is still perfectly
    playable up to the point it stops.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise CastError(f"Could not read {path.name}: {exc}") from exc

    lines = text.splitlines()
    if not lines:
        raise CastError(f"{path.name} is empty.")

    try:
        header = json.loads(lines[0])
    except ValueError as exc:
        raise CastError(f"{path.name} does not start with a cast header.") from exc
    if not isinstance(header, dict) or header.get("version") != 2:
        raise CastError(f"{path.name} is not an asciicast v2 recording.")

    events = []
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if (isinstance(item, list) and len(item) >= 3
                and isinstance(item[1], str)):
            try:
                offset = float(item[0])
            except (TypeError, ValueError):
                continue
            events.append((offset, item[1], str(item[2])))
    return header, events


def cast_duration(events) -> float:
    return float(events[-1][0]) if events else 0.0


def replay_frames(events, speed=1.0, idle_limit=2.0):
    """``(delay, kind, payload)`` per event, with dead air shortened.

    Nobody wants to sit through the four minutes you spent reading the output
    before typing the next command, so a gap longer than *idle_limit* is
    clamped to it — the same thing asciinema does, and for the same reason.
    """
    speed = float(speed) if speed and float(speed) > 0 else 1.0
    limit = None if idle_limit in (None, 0) else max(0.0, float(idle_limit))
    previous = 0.0
    for offset, kind, payload in events or ():
        gap = max(0.0, float(offset) - previous)
        if limit is not None:
            gap = min(gap, limit)
        previous = float(offset)
        yield (gap / speed, kind, payload)


def cast_text(events) -> str:
    """Everything the machine printed, ANSI and all."""
    return "".join(payload for _offset, kind, payload in events or ()
                   if kind == "o")


# ── Housekeeping ────────────────────────────────────────────────────


def list_recordings(root=None) -> list:
    """Every recording under *root*, newest first."""
    root = Path(root) if root is not None else SESSIONS_DIR
    if not root.exists():
        return []
    found = []
    for path in root.rglob(f"*{CAST_SUFFIX}"):
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append({"path": path, "size": stat.st_size,
                      "modified": stat.st_mtime, "name": path.name})
    found.sort(key=lambda row: row["modified"], reverse=True)
    return found


def prune(root=None, keep_days=DEFAULT_KEEP_DAYS,
          max_bytes=DEFAULT_MAX_BYTES) -> list:
    """Delete recordings past their age or over the size cap. Newest kept.

    Returns what was removed, so a caller can say so rather than quietly
    shrinking the evidence.
    """
    root = Path(root) if root is not None else SESSIONS_DIR
    recordings = list_recordings(root)
    if not recordings:
        return []

    removed = []
    keep_days = max(0, int(keep_days or 0))
    cutoff = (time.time() - timedelta(days=keep_days).total_seconds()
              if keep_days else None)

    kept, running = [], 0
    for row in recordings:
        too_old = cutoff is not None and row["modified"] < cutoff
        too_big = (max_bytes and running + row["size"] > int(max_bytes)
                   and kept)
        if too_old or too_big:
            try:
                row["path"].unlink()
                removed.append(row)
                continue
            except OSError:
                pass
        kept.append(row)
        running += row["size"]

    _remove_empty_days(root)
    return removed


def _remove_empty_days(root: Path):
    try:
        for child in root.iterdir():
            if child.is_dir() and not any(child.iterdir()):
                child.rmdir()
    except OSError:
        pass
