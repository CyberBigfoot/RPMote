"""
Session history — when each connection was open, and for how long.

Every session RPMote opens is logged with its start and end time (an
embedded tab when it closes, an external Remote Desktop window when its
mstsc process exits). The log lives encrypted in the vault, next to the
connections it describes, and is what Recents and the time-per-tenant report
(for billing customers) are built from.

The time is "session open", not "hands on keyboard" — good for timesheets
and invoices, which is what it is for.
"""

from __future__ import annotations

import csv
import secrets
import threading
import time
from datetime import datetime

from PyQt6.QtCore import QObject, pyqtSignal

#: Entries kept; the oldest go first. A busy year is a few thousand.
HISTORY_LIMIT = 5000
#: Recent connections offered in Go to and on the dashboard.
RECENT_LIMIT = 20


def _now() -> float:
    return time.time()


class SessionHistory(QObject):
    """Writes session starts/ends into the vault's "history" list."""

    #: A session ended (token) — emitted on the GUI thread.
    ended = pyqtSignal(str)
    _process_exited = pyqtSignal(str)

    def __init__(self, vault, parent=None):
        super().__init__(parent)
        self.vault = vault
        self._open: dict[str, dict] = {}
        self._process_exited.connect(self.end)

    # ── storage ───────────────────────────────────────────────────

    def _entries(self) -> list:
        data = getattr(self.vault, "_data", None)
        if not isinstance(data, dict):
            return []
        history = data.get("history")
        if not isinstance(history, list):
            history = []
            data["history"] = history
        return history

    def _save(self):
        try:
            self.vault._save()
        except Exception:
            pass

    # ── recording ─────────────────────────────────────────────────

    def start(self, conn: dict) -> str:
        """Log a session opening. Returns a token to end it with."""
        if not getattr(self.vault, "is_unlocked", False) or not isinstance(conn, dict):
            return ""
        token = secrets.token_hex(6)
        entry = {
            "token": token,
            "id": str(conn.get("id", "") or ""),
            "name": str(conn.get("name", "") or conn.get("host", "") or ""),
            "tenant": str(conn.get("tenant", "Default") or "Default"),
            "protocol": str(conn.get("protocol", "") or "").lower(),
            "host": str(conn.get("host", "") or ""),
            "start": _now(),
            "end": None,
        }
        history = self._entries()
        history.append(entry)
        if len(history) > HISTORY_LIMIT:
            del history[: len(history) - HISTORY_LIMIT]
        self._open[token] = entry
        self._touch_recent(entry["id"], entry["start"])
        self._save()
        return token

    def end(self, token: str):
        entry = self._open.pop(str(token or ""), None)
        if entry is None or entry.get("end"):
            return
        entry["end"] = _now()
        self._save()
        self.ended.emit(str(token))

    def end_all(self):
        """Close every open entry (the app is quitting or locking)."""
        now = _now()
        for entry in list(self._open.values()):
            if not entry.get("end"):
                entry["end"] = now
        self._open.clear()
        self._save()

    def watch_process(self, proc, token: str):
        """End *token* when an external process (mstsc) exits."""
        if proc is None or not token:
            return

        def wait():
            try:
                proc.wait()
            except Exception:
                pass
            try:
                self._process_exited.emit(token)
            except RuntimeError:
                pass  # the window went first

        threading.Thread(target=wait, name="rpmote-session-watch", daemon=True).start()

    def _touch_recent(self, conn_id: str, when: float):
        if not conn_id:
            return
        settings = self.vault._data.setdefault("settings", {})
        recent = [r for r in settings.get("recent_connections", [])
                  if isinstance(r, dict) and r.get("id") != conn_id]
        recent.insert(0, {"id": conn_id, "ts": when})
        settings["recent_connections"] = recent[:RECENT_LIMIT]

    # ── reading ───────────────────────────────────────────────────

    def recent_ids(self) -> list[str]:
        try:
            recent = self.vault.settings.get("recent_connections", [])
        except Exception:
            return []
        return [str(r.get("id")) for r in recent if isinstance(r, dict) and r.get("id")]

    def entries(self, since: float | None = None, until: float | None = None) -> list[dict]:
        out = []
        now = _now()
        for entry in self._entries():
            if not isinstance(entry, dict):
                continue
            start = float(entry.get("start") or 0)
            end = float(entry.get("end") or (now if entry.get("token") in self._open else start))
            if since is not None and end < since:
                continue
            if until is not None and start > until:
                continue
            item = dict(entry)
            item["end_effective"] = end
            item["duration"] = max(0.0, end - start)
            item["open"] = entry.get("token") in self._open
            out.append(item)
        out.sort(key=lambda e: e.get("start", 0), reverse=True)
        return out

    @staticmethod
    def totals_by_tenant(entries: list[dict]) -> dict[str, float]:
        totals: dict[str, float] = {}
        for e in entries:
            totals[e.get("tenant", "Default")] = totals.get(e.get("tenant", "Default"), 0.0) + float(e.get("duration", 0))
        return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))

    def clear(self):
        self._entries().clear()
        self._open.clear()
        self._save()

    @staticmethod
    def export_csv(path: str, entries: list[dict]):
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Tenant", "Connection", "Protocol", "Host", "Start", "End",
                             "Minutes", "Hours (decimal)"])
            for e in sorted(entries, key=lambda x: x.get("start", 0)):
                start = datetime.fromtimestamp(float(e.get("start", 0)))
                end = datetime.fromtimestamp(float(e.get("end_effective", e.get("start", 0))))
                minutes = float(e.get("duration", 0)) / 60.0
                writer.writerow([e.get("tenant", ""), e.get("name", ""), e.get("protocol", ""),
                                 e.get("host", ""), start.strftime("%Y-%m-%d %H:%M"),
                                 end.strftime("%Y-%m-%d %H:%M"), f"{minutes:.0f}",
                                 f"{minutes / 60.0:.2f}"])


def format_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min"
