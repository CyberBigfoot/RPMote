"""
Go to — Ctrl+P (or Ctrl+Shift+Space, which also works inside a terminal).

One box that finds anything: a connection by name, host, tenant, user or
protocol; a tenant; or any menu command. Empty, it lists your recent and
favourite connections. Prefix with ``>`` to search commands only.

  Enter         open the connection / run the command / show the tenant
  Ctrl+Enter    open another session even if one is already open
  Shift+Enter   edit the connection
"""

from __future__ import annotations

from PyQt6.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QFontMetrics, QPainter, QPalette
from PyQt6.QtWidgets import (
    QDialog, QFrame, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QStyle, QStyledItemDelegate, QVBoxLayout,
)

from app import icons
from app.dashboard import SEARCH_FIELDS, fuzzy_match, match_record

MAX_ROWS = 60
_STATUS_COLORS = {"down": "#f87171", "degraded": "#fbbf24", "up": "#4ade80"}
_ROLE = Qt.ItemDataRole.UserRole


def menu_actions(menubar) -> list[tuple[str, QAction]]:
    """Every runnable action in the menu bar, as ("File › Export…", action)."""
    found = []

    def clean(text: str) -> str:
        return text.replace("&&", "\0").replace("&", "").replace("\0", "&").split("\t")[0].strip()

    def walk(menu, path):
        for action in menu.actions():
            if action.isSeparator():
                continue
            sub = action.menu()
            text = clean(action.text())
            if not text:
                continue
            if sub is not None:
                walk(sub, path + [text])
            elif action.isEnabled() and action.isVisible():
                found.append((" › ".join(path + [text]), action))

    for top in menubar.actions():
        if top.menu() is not None:
            walk(top.menu(), [clean(top.text())])
    return found


class _Delegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 40)

    def paint(self, painter: QPainter, option, index):
        data = index.data(_ROLE) or {}
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            hl = option.palette.color(QPalette.ColorRole.Highlight)
            painter.setBrush(QColor(hl.red(), hl.green(), hl.blue(), 70))
            painter.drawRoundedRect(rect.adjusted(4, 1, -4, -1), 7, 7)
        if data.get("kind") == "header":
            painter.setPen(QColor("#94a3b8"))
            font = QFont("Segoe UI", 8)
            font.setBold(True)
            font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
            painter.setFont(font)
            painter.drawText(rect.adjusted(14, 0, -14, 0),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             str(data.get("label", "")).upper())
            painter.restore()
            return
        icon = data.get("icon")
        x = rect.left() + 14
        if icon is not None and not icon.isNull():
            icon.paint(painter, QRect(x, rect.center().y() - 8, 16, 16))
        x += 28
        right = rect.right() - 14
        status = data.get("status")
        if status in _STATUS_COLORS:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(_STATUS_COLORS[status]))
            painter.drawEllipse(right - 8, rect.center().y() - 4, 8, 8)
            right -= 16
        if data.get("star"):
            icons.icon("star-filled", "#fbbf24").paint(
                painter, QRect(right - 12, rect.center().y() - 6, 12, 12))
            right -= 22
        hint = str(data.get("hint", "") or "")
        if hint:
            hint_font = QFont("Segoe UI", 8)
            painter.setFont(hint_font)
            painter.setPen(QColor("#94a3b8"))
            width = QFontMetrics(hint_font).horizontalAdvance(hint) + 4
            painter.drawText(QRect(right - width, rect.top(), width, rect.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, hint)
            right -= width + 10
        name_font = QFont("Segoe UI", 10)
        name_font.setWeight(QFont.Weight.DemiBold)
        sub_font = QFont("Segoe UI", 8)
        label = str(data.get("label", ""))
        sub = str(data.get("sub", "") or "")
        width = max(0, right - x)
        painter.setFont(name_font)
        painter.setPen(QColor("#f8fafc"))
        top = rect.top() + (4 if sub else 0)
        height = 18 if sub else rect.height()
        painter.drawText(QRect(x, top, width, height),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         QFontMetrics(name_font).elidedText(label, Qt.TextElideMode.ElideRight, width))
        if sub:
            painter.setFont(sub_font)
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRect(x, rect.top() + 21, width, 15),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             QFontMetrics(sub_font).elidedText(sub, Qt.TextElideMode.ElideRight, width))
        painter.restore()


class GotoPalette(QDialog):
    """The Ctrl+P box."""

    open_requested = pyqtSignal(dict, bool)     # connection, force a new session
    edit_requested = pyqtSignal(dict)
    tenant_requested = pyqtSignal(str)

    def __init__(self, connections, statuses=None, recent_ids=None, menubar=None,
                 tenant_notes=None, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._connections = [c for c in (connections or []) if isinstance(c, dict)]
        self._statuses = dict(statuses or {})
        self._recent_ids = [str(i) for i in (recent_ids or [])]
        self._tenant_notes = dict(tenant_notes or {})
        self._actions = menu_actions(menubar) if menubar is not None else []

        frame = QFrame(self)
        frame.setObjectName("goto_frame")
        frame.setStyleSheet("""
            QFrame#goto_frame {
                background-color: rgba(17, 18, 29, 0.97);
                border: 1px solid rgba(255, 255, 255, 0.14);
                border-radius: 14px;
            }
            QLineEdit#goto_input {
                font-size: 15px; padding: 10px 12px; border-radius: 10px;
            }
            QListWidget#goto_list {
                background: transparent; border: none; padding: 2px; outline: 0;
            }
            QLabel#goto_hint { color: #94a3b8; font-size: 11px; padding: 0 6px; }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 12, 12, 10)
        layout.setSpacing(8)

        self.input = QLineEdit()
        self.input.setObjectName("goto_input")
        self.input.setPlaceholderText("Go to a connection, tenant or command…   (type > for commands only)")
        self.input.addAction(icons.icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.input.textChanged.connect(self._refresh)
        self.input.installEventFilter(self)
        layout.addWidget(self.input)

        self.list = QListWidget()
        self.list.setObjectName("goto_list")
        self.list.setItemDelegate(_Delegate(self.list))
        self.list.setUniformItemSizes(True)
        self.list.itemActivated.connect(lambda _item: self._activate())
        self.list.itemClicked.connect(lambda _item: self._activate())
        layout.addWidget(self.list, 1)

        hint = QLabel("↑↓ choose · Enter open · Ctrl+Enter new session · "
                      "Shift+Enter edit · Esc close")
        hint.setObjectName("goto_hint")
        layout.addWidget(hint)

        self.resize(680, 460)
        self._refresh("")

    # ── positioning ───────────────────────────────────────────────

    def show_over(self, window):
        if window is not None:
            geo = window.geometry()
            self.move(geo.x() + (geo.width() - self.width()) // 2, geo.y() + 70)
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    # ── results ───────────────────────────────────────────────────

    def _state(self, conn) -> str:
        return str((self._statuses.get(str(conn.get("id", ""))) or {}).get("state", ""))

    def _conn_row(self, conn: dict, hint: str = "", star: bool = False) -> dict:
        from app.side_panel import PROTOCOL_COLORS, protocol_tag
        proto = str(conn.get("protocol", "ssh") or "ssh").lower()
        host = str(conn.get("host", "") or "")
        tenant = str(conn.get("tenant", "Default") or "Default")
        return {
            "kind": "conn", "conn": conn,
            "label": str(conn.get("name", "") or host),
            "sub": "  ·  ".join(x for x in (tenant, protocol_tag(proto), host) if x),
            "icon": icons.icon(icons.protocol_icon_name(proto), PROTOCOL_COLORS.get(proto)),
            "status": self._state(conn), "hint": hint, "star": star,
        }

    def _add_header(self, text: str):
        item = QListWidgetItem()
        item.setData(_ROLE, {"kind": "header", "label": text})
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setSizeHint(QSize(0, 26))
        self.list.addItem(item)

    def _add(self, row: dict):
        item = QListWidgetItem()
        item.setData(_ROLE, row)
        self.list.addItem(item)

    def _refresh(self, text: str):
        self.list.clear()
        query = str(text or "").strip()
        commands_only = query.startswith(">")
        if commands_only:
            query = query[1:].strip()

        if not query and not commands_only:
            by_id = {str(c.get("id", "")): c for c in self._connections}
            recent = [by_id[i] for i in self._recent_ids if i in by_id][:8]
            favorites = [c for c in self._connections if c.get("favorite") and c not in recent][:8]
            if recent:
                self._add_header("Recent")
                for conn in recent:
                    self._add(self._conn_row(conn))
            if favorites:
                self._add_header("Favorites")
                for conn in favorites:
                    self._add(self._conn_row(conn, star=True))
            problems = [c for c in self._connections if self._state(c) == "down"][:5]
            if problems:
                self._add_header("Unreachable")
                for conn in problems:
                    self._add(self._conn_row(conn))
            if not (recent or favorites or problems):
                self._add_header("Connections")
                for conn in self._connections[:12]:
                    self._add(self._conn_row(conn))
            self._select_first()
            return

        hits = []
        if not commands_only:
            tenants = sorted({str(c.get("tenant", "Default") or "Default") for c in self._connections})
            for tenant in tenants:
                score = fuzzy_match(query, tenant)
                if score is not None:
                    count = sum(1 for c in self._connections
                                if (c.get("tenant", "Default") or "Default") == tenant)
                    note = self._tenant_notes.get(tenant, "")
                    sub = f"Tenant · {count} connection{'s' if count != 1 else ''}"
                    if note:
                        sub += f" · {note.splitlines()[0][:60]}"
                    hits.append((score * 1.05, {
                        "kind": "tenant", "tenant": tenant, "label": tenant, "sub": sub,
                        "icon": icons.icon("users"),
                    }))
            fields = tuple(SEARCH_FIELDS) + (("notes", 0.35),)
            for conn in self._connections:
                best = match_record(query, conn, fields)
                if best is not None:
                    if conn.get("favorite"):
                        best *= 1.03
                    hits.append((best, self._conn_row(conn)))
        for path, action in self._actions:
            score = fuzzy_match(query, path.split(" › ")[-1]) if query else 1.0
            whole = fuzzy_match(query, path) if query else None
            if whole is not None:
                score = max(score or 0, whole * 0.9)
            if score is None:
                continue
            shortcut = action.shortcut().toString() if not action.shortcut().isEmpty() else ""
            hits.append((score * (1.0 if commands_only else 0.92), {
                "kind": "action", "action": action, "label": path.split(" › ")[-1],
                "sub": path, "icon": action.icon() if not action.icon().isNull() else icons.icon("arrow-right"),
                "hint": shortcut,
            }))
        hits.sort(key=lambda h: (-h[0], h[1]["label"].lower()))
        if not hits:
            self._add_header("Nothing matches")
            return
        for _score, row in hits[:MAX_ROWS]:
            self._add(row)
        self._select_first()

    def _select_first(self):
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.flags() & Qt.ItemFlag.ItemIsSelectable:
                self.list.setCurrentItem(item)
                return

    # ── keys ──────────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        if obj is self.input and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                self._step(1 if key in (Qt.Key.Key_Down, Qt.Key.Key_PageDown) else -1,
                           5 if key in (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp) else 1)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                mods = event.modifiers()
                self._activate(force_new=bool(mods & Qt.KeyboardModifier.ControlModifier),
                               edit=bool(mods & Qt.KeyboardModifier.ShiftModifier))
                return True
            if key == Qt.Key.Key_Escape:
                self.reject()
                return True
        return super().eventFilter(obj, event)

    def _step(self, direction: int, count: int = 1):
        row = self.list.currentRow()
        for _ in range(count):
            nxt = row
            while True:
                nxt += direction
                if nxt < 0 or nxt >= self.list.count():
                    nxt = row
                    break
                if self.list.item(nxt).flags() & Qt.ItemFlag.ItemIsSelectable:
                    break
            row = nxt
        if 0 <= row < self.list.count():
            self.list.setCurrentRow(row)

    def _activate(self, force_new: bool = False, edit: bool = False):
        item = self.list.currentItem()
        if item is None:
            return
        row = item.data(_ROLE) or {}
        kind = row.get("kind")
        if kind == "header":
            return
        self.accept()
        if kind == "conn":
            if edit:
                self.edit_requested.emit(row["conn"])
            else:
                self.open_requested.emit(row["conn"], force_new)
        elif kind == "tenant":
            self.tenant_requested.emit(row["tenant"])
        elif kind == "action":
            row["action"].trigger()
