"""Type once, run everywhere.

Patching thirty machines by hand means thirty chances to type it differently.
A broadcast group ties a set of open terminals together so that one keystroke
reaches all of them, which is the whole point — and also exactly why it needs
to be visible, per-pane revocable, and off unless someone turned it on.

The group holds terminals, not connections: a pane that drops off the network
is still a member, it just stops being a target until it reconnects.  That
distinction is what makes "you typed this on 7 of 8 machines" reportable
instead of silent.

The Qt side of this is deliberately thin — membership, arming and fan-out are
plain object operations, so they can be tested without opening an SSH session
to anywhere.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QObject, pyqtSignal


def grid_shape(count: int) -> tuple:
    """(rows, columns) for *count* panes: as square as it goes, wider than tall.

    Eight machines in one row is eight unreadable machines; the shape a person
    would draw is what makes a broadcast grid usable at all.
    """
    count = max(0, int(count or 0))
    if count <= 1:
        return (1, max(1, count))
    columns = math.ceil(math.sqrt(count))
    rows = math.ceil(count / columns)
    return (rows, columns)


def distribute(count: int) -> list:
    """How many panes go in each row of the grid for *count* panes."""
    count = max(0, int(count or 0))
    if not count:
        return []
    rows, _columns = grid_shape(count)
    # Spread the shortfall across the rows rather than leaving one lonely
    # pane stretched along the bottom: seven panes are 3-2-2, not 3-3-1.
    base, extra = divmod(count, rows)
    return [base + 1] * extra + [base] * (rows - extra)


class BroadcastGroup(QObject):
    """The set of terminals typing in unison, and whether they currently are.

    ``armed`` is the master switch.  ``excluded`` is the per-pane escape
    hatch: an excluded pane keeps its place in the grid and its scrollback,
    it simply stops receiving.  Both are needed — arming everything is the
    common case, and "everything except the one I already broke" is the next
    most common.
    """

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._members: list = []
        self._excluded: set = set()
        self._armed = False
        self._sending = False

    # ── Membership ──────────────────────────────────────────────────

    @property
    def members(self) -> list:
        return list(self._members)

    def __len__(self) -> int:
        return len(self._members)

    def __contains__(self, terminal) -> bool:
        return any(member is terminal for member in self._members)

    def add(self, terminal) -> bool:
        if terminal is None or terminal in self:
            return False
        self._members.append(terminal)
        self.changed.emit()
        return True

    def discard(self, terminal) -> bool:
        for index, member in enumerate(self._members):
            if member is terminal:
                del self._members[index]
                self._excluded.discard(id(terminal))
                self.changed.emit()
                return True
        return False

    def clear(self):
        if not self._members and not self._excluded:
            return
        self._members.clear()
        self._excluded.clear()
        self.changed.emit()

    # ── Arming ──────────────────────────────────────────────────────

    @property
    def armed(self) -> bool:
        return self._armed and len(self._members) > 1

    def set_armed(self, armed: bool):
        armed = bool(armed)
        if armed == self._armed:
            return
        self._armed = armed
        self.changed.emit()

    def is_included(self, terminal) -> bool:
        return terminal in self and id(terminal) not in self._excluded

    def set_included(self, terminal, included: bool):
        if terminal not in self:
            return
        key = id(terminal)
        if included:
            changed = key in self._excluded
            self._excluded.discard(key)
        else:
            changed = key not in self._excluded
            self._excluded.add(key)
        if changed:
            self.changed.emit()

    # ── Targets ─────────────────────────────────────────────────────

    @staticmethod
    def _is_live(terminal) -> bool:
        return bool(getattr(terminal, "_connected", False))

    def targets(self, origin=None) -> list:
        """Included, connected members — the ones a send would reach now."""
        return [member for member in self._members
                if member is not origin
                and self.is_included(member)
                and self._is_live(member)]

    def connections(self, origin=None) -> list:
        """The connection dicts behind the current targets, for guardrails."""
        out = []
        for member in self.targets(origin):
            info = getattr(member, "connection_info", None)
            if isinstance(info, dict):
                out.append(info)
        return out

    def labels(self) -> list:
        """(terminal, label, included, live) for each member, for the UI."""
        rows = []
        for member in self._members:
            info = getattr(member, "connection_info", None) or {}
            label = str(info.get("name") or info.get("host") or "session")
            rows.append((member, label, self.is_included(member),
                         self._is_live(member)))
        return rows

    # ── Sending ─────────────────────────────────────────────────────

    def fan_out(self, origin, data) -> list:
        """Mirror *data* from *origin* to every other target.

        Returns the terminals that were written to.  Re-entrant calls are
        dropped: each mirrored write goes through the receiving terminal's own
        send path, and without the guard the first keystroke would ricochet
        around the group forever.
        """
        if not data or not self.armed or self._sending:
            return []
        targets = self.targets(origin)
        if not targets:
            return []

        self._sending = True
        try:
            sent = []
            for member in targets:
                try:
                    member._send_data(data)
                except Exception:
                    # One dead socket must not stop the other twenty-nine.
                    continue
                sent.append(member)
            return sent
        finally:
            self._sending = False

    def send(self, data) -> list:
        """Send *data* to every target, with no pane treated as the origin.

        This is the broadcast bar's path: nobody typed it into a pane, so
        everything included gets it, including the pane that happens to have
        focus.
        """
        if not data:
            return []
        targets = self.targets()
        if not targets:
            return []
        was_sending, self._sending = self._sending, True
        try:
            sent = []
            for member in targets:
                try:
                    member._send_data(data)
                except Exception:
                    continue
                sent.append(member)
            return sent
        finally:
            self._sending = was_sending
