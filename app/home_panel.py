"""
The dashboard's Overview card — what needs you, what you use, what you used.

Logging in used to land on an empty canvas that said "Click ⚡". This card
sits in the corner of the graph and answers the first three questions of the
day without a single click:

* **Needs attention** — machines the last reachability sweep found down or
  degraded;
* **Favorites** — the connections you starred;
* **Recent** — the last ones you opened.

With no connections at all it becomes a welcome card with the three ways to
get started.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget,
)

from app import icons
from app.themes import primary_ink
from app.widgets import ElidedLabel, set_variant

_STATUS_COLORS = {"down": "#f87171", "degraded": "#fbbf24", "up": "#4ade80"}
_ROWS_PER_SECTION = 6


class _Row(QToolButton):
    """One clickable connection line: icon, name, tenant, status dot."""

    def __init__(self, conn: dict, detail: str, status: str, parent=None):
        super().__init__(parent)
        from app.side_panel import PROTOCOL_COLORS
        self.conn = conn
        self.setObjectName("home_row")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(30)
        proto = str(conn.get("protocol", "ssh") or "ssh").lower()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(8)
        icon = QLabel()
        icon.setPixmap(icons.pixmap(icons.protocol_icon_name(proto), 14,
                                    PROTOCOL_COLORS.get(proto, "#cbd5e1")))
        icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(icon)
        name = ElidedLabel(str(conn.get("name", "") or conn.get("host", "")))
        name.setObjectName("home_row_name")
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(name, 1)
        sub = QLabel(detail)
        sub.setObjectName("home_row_detail")
        sub.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(sub)
        dot = QLabel()
        dot.setFixedSize(8, 8)
        color = _STATUS_COLORS.get(status)
        dot.setStyleSheet(
            f"background: {color}; border-radius: 4px;" if color else
            "border: 1px solid rgba(203,213,225,0.45); border-radius: 4px;")
        dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(dot)
        tip = f"{conn.get('name', '')}\n{proto.upper()} · {conn.get('host', '')}"
        self.setToolTip(tip + "\nClick to open")


class HomePanel(QFrame):
    """The Overview card shown over the dashboard graph."""

    open_requested = pyqtSignal(dict)
    new_connection_requested = pyqtSignal()
    import_requested = pyqtSignal(str)      # "file" | "ssh_config" | "rdp" | "csv" …
    check_now_requested = pyqtSignal()
    goto_requested = pyqtSignal()
    visibility_changed = pyqtSignal(bool)

    WIDTH = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("home_panel")
        self.setFixedWidth(self.WIDTH)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._connections: list[dict] = []
        self._statuses: dict = {}
        self._recent_ids: list[str] = []
        self._ping_enabled = False
        self._welcome = False
        #: Nothing is drawn until the first set_data: building with no data
        #: would flash the first-run welcome at someone with 60 connections.
        self._has_data = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(250)
        self._refresh_timer.timeout.connect(self._rebuild)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(6)
        self.title = QLabel("Overview")
        self.title.setObjectName("home_title")
        header.addWidget(self.title)
        header.addStretch()
        self.btn_goto = QToolButton()
        self.btn_goto.setIcon(icons.icon("search"))
        self.btn_goto.setIconSize(QSize(14, 14))
        self.btn_goto.setToolTip("Go to a connection or command (Ctrl+P)")
        self.btn_goto.clicked.connect(self.goto_requested.emit)
        header.addWidget(self.btn_goto)
        self.btn_hide = QToolButton()
        self.btn_hide.setIcon(icons.icon("x"))
        self.btn_hide.setIconSize(QSize(13, 13))
        self.btn_hide.setToolTip("Hide the overview (Graph › Show Overview brings it back)")
        self.btn_hide.clicked.connect(lambda: self.set_shown(False))
        header.addWidget(self.btn_hide)
        outer.addLayout(header)

        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(2)
        outer.addWidget(self.body)

        self._apply_style("#8b5cf6", "#f8fafc", "#94a3b8")

    # ── appearance ────────────────────────────────────────────────

    def set_theme(self, theme: dict):
        def solid(key, fallback):
            value = str(theme.get(key, fallback) or fallback)
            return value if value.startswith("#") else fallback
        self._apply_style(solid("accent", "#8b5cf6"), solid("text_primary", "#f8fafc"),
                          solid("text_secondary", "#94a3b8"))

    def _apply_style(self, accent, text, muted):
        a = QColor(accent)
        self.setStyleSheet(f"""
            QFrame#home_panel {{
                background-color: rgba(12, 13, 22, 0.86);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 14px;
            }}
            QLabel#home_title {{ color: {text}; font-size: 14px; font-weight: 700; }}
            QLabel#home_section {{
                color: {muted}; font-size: 11px; font-weight: 700;
                letter-spacing: 0.6px; padding: 8px 2px 2px 2px;
            }}
            QLabel#home_empty {{ color: {muted}; font-size: 12px; padding: 2px 4px 6px 4px; }}
            QToolButton#home_row {{
                background: transparent; border: none; border-radius: 8px; text-align: left;
            }}
            QToolButton#home_row:hover {{
                background: rgba({a.red()}, {a.green()}, {a.blue()}, 0.16);
            }}
            QLabel#home_row_name {{ color: {text}; font-size: 12px; }}
            QLabel#home_row_detail {{ color: {muted}; font-size: 11px; }}
            QToolButton {{ border: none; border-radius: 6px; padding: 4px; background: transparent; }}
            QToolButton:hover {{ background: rgba(255, 255, 255, 0.08); }}
        """)

    # ── data ──────────────────────────────────────────────────────

    def set_data(self, connections=None, statuses=None, recent_ids=None,
                 ping_enabled=None):
        if connections is not None:
            self._connections = [c for c in connections if isinstance(c, dict)]
        if statuses is not None:
            self._statuses = dict(statuses)
        if recent_ids is not None:
            self._recent_ids = [str(i) for i in recent_ids]
        if ping_enabled is not None:
            self._ping_enabled = bool(ping_enabled)
        if not self._has_data:
            # The first picture goes up at once; later changes are batched.
            self._has_data = True
            self._rebuild()
            return
        self._refresh_timer.start()

    def set_status(self, conn_id: str, info: dict):
        if isinstance(info, dict):
            self._statuses[str(conn_id)] = dict(info)
            self._refresh_timer.start()

    def set_shown(self, shown: bool):
        shown = bool(shown)
        if shown != self.isVisible():
            self.setVisible(shown)
            self.visibility_changed.emit(shown)
            if shown:
                self.raise_()
                self.reposition()

    def reposition(self):
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        max_h = max(160, parent.height() - 32)
        self.setMaximumHeight(max_h)
        if self._welcome:
            self.move(max(16, (parent.width() - self.width()) // 2),
                      max(16, (parent.height() - self.height()) // 2))
        else:
            self.move(16, 16)

    # ── building ──────────────────────────────────────────────────

    def _state(self, conn) -> str:
        return str((self._statuses.get(str(conn.get("id", ""))) or {}).get("state", "unknown"))

    def _clear(self):
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _section(self, title: str):
        label = QLabel(title.upper())
        label.setObjectName("home_section")
        self.body_layout.addWidget(label)

    def _empty(self, text: str):
        label = QLabel(text)
        label.setObjectName("home_empty")
        label.setWordWrap(True)
        self.body_layout.addWidget(label)

    def _row(self, conn: dict, detail: str = ""):
        row = _Row(conn, detail or str(conn.get("tenant", "") or ""), self._state(conn))
        row.clicked.connect(lambda _c=False, c=conn: self.open_requested.emit(c))
        self.body_layout.addWidget(row)

    def _rebuild(self):
        self._clear()
        self._welcome = not self._connections
        if self._welcome:
            self._build_welcome()
        else:
            self._build_overview()
        # Size to the new rows, not the old ones: the layout has to take the
        # new widgets in before adjustSize can measure them (it measured the
        # card 59 px tall otherwise), and wrapped labels settle a pass later.
        self.body_layout.invalidate()
        self.layout().activate()
        self.reposition()
        QTimer.singleShot(0, self.reposition)

    def _build_welcome(self):
        self.title.setText("Welcome to RPMote")
        self.btn_hide.setVisible(False)
        self.btn_goto.setVisible(False)
        intro = QLabel("Your vault is ready. Add the machines you look after, "
                       "grouped by customer — or bring them in from another tool.")
        intro.setObjectName("home_empty")
        intro.setWordWrap(True)
        self.body_layout.addWidget(intro)
        for icon, text, slot, primary in (
                ("plus", "Add your first connection", self.new_connection_requested.emit, True),
                ("import", "Import connections…", lambda: self.import_requested.emit("any"), False),
                ("terminal", "Import from ~/.ssh/config", lambda: self.import_requested.emit("ssh_config"), False)):
            button = QPushButton(text)
            button.setIcon(icons.icon(icon, primary_ink() if primary else None))
            if primary:
                set_variant(button, "primary")
            button.clicked.connect(slot)
            self.body_layout.addWidget(button)
        tip = QLabel("Tip: Ctrl+P finds any connection or command from anywhere.")
        tip.setObjectName("home_empty")
        tip.setWordWrap(True)
        self.body_layout.addWidget(tip)
        self.setFixedWidth(360)

    def _build_overview(self):
        self.title.setText("Overview")
        self.btn_hide.setVisible(True)
        self.btn_goto.setVisible(True)
        self.setFixedWidth(self.WIDTH)
        by_id = {str(c.get("id", "")): c for c in self._connections}

        # Needs attention
        problems = [c for c in self._connections if self._state(c) in ("down", "degraded")]
        problems.sort(key=lambda c: (self._state(c) != "down", str(c.get("tenant", "")),
                                     str(c.get("name", ""))))
        self._section(f"Needs attention · {len(problems)}" if problems else "Needs attention")
        if problems:
            for conn in problems[:_ROWS_PER_SECTION]:
                state = self._state(conn)
                self._row(conn, ("down" if state == "down" else "service down")
                          + f" · {conn.get('tenant', '')}")
            if len(problems) > _ROWS_PER_SECTION:
                self._empty(f"…and {len(problems) - _ROWS_PER_SECTION} more — they are "
                            "red and amber on the graph and in the sidebar.")
        else:
            checked = any(self._state(c) in ("up", "down", "degraded") for c in self._connections)
            if checked:
                self._empty("Everything checked is reachable.")
            else:
                self._empty("Not checked yet.")
                check = QPushButton("Check all now")
                check.setIcon(icons.icon("activity"))
                check.clicked.connect(self.check_now_requested.emit)
                self.body_layout.addWidget(check)

        favorites = [c for c in self._connections if c.get("favorite")]
        self._section("Favorites")
        if favorites:
            for conn in favorites[:_ROWS_PER_SECTION]:
                self._row(conn)
        else:
            self._empty("Star a connection (right-click › Add to favorites) to keep it here.")

        recent = [by_id[i] for i in self._recent_ids if i in by_id]
        self._section("Recent")
        if recent:
            for conn in recent[:_ROWS_PER_SECTION]:
                self._row(conn)
        else:
            self._empty("Connections you open show up here.")
