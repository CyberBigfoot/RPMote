"""
RPMote Side Panel — the Connections sidebar.

Rows are painted by one delegate rather than built from a widget each, so
the sidebar can show live reachability, open sessions and favourites for a
few hundred machines without rebuilding anything when a status changes.

What a row shows, and what the colours mean (the same everywhere in RPMote):

* the status dot is **reachability** — green answered, amber host up but the
  service is down, red nothing answered, grey not checked;
* a coloured bar on the left edge and a bold name mean a **session is open**;
* a tenant row shows its tenant colour, how many machines it holds and the
  worst state among them, so one dead server is never hidden in a group.
"""
from __future__ import annotations

from PyQt6.QtCore import (
    QElapsedTimer, QEvent, QModelIndex, QObject, QPoint, QRect, QRectF, QSize,
    Qt, QTimer, pyqtSignal,
)
from PyQt6.QtGui import (
    QAction, QBrush, QColor, QFont, QFontMetrics, QKeySequence, QPainter, QPen,
    QPixmap, QGuiApplication,
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QColorDialog, QDockWidget, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QMenu, QPushButton, QStyle, QStyledItemDelegate,
    QToolButton, QToolTip, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from app import guardrails, icons
from app.themes import button_text_color
from app.wake_on_lan import is_wakeable

PROTOCOL_COLORS = {
    "ssh": "#4ade80",
    "sftp": "#60a5fa",
    "ftp": "#fbbf24",
    "rdp": "#c084fc",
    "idrac": "#f97316",
    "synology": "#4cb5ae",
    "azure_ps": "#38bdf8",
    "web": "#22d3ee",
    "vnc": "#f472b6",
    "telnet": "#a3e635",
    "serial": "#facc15",
}

#: Short protocol tags shown on each row.
PROTOCOL_TAGS = {
    "synology": "NAS",
    "azure_ps": "AZURE",
    "idrac": "iDRAC",
}

#: Reachability colours, shared with the dashboard (app.reachability).
STATUS_COLORS = {
    "up": "#4ade80",
    "degraded": "#fbbf24",
    "down": "#f87171",
    "checking": "#60a5fa",
}
STATUS_LABELS = {
    "up": "Reachable",
    "degraded": "Host up, service down",
    "down": "Unreachable",
    "checking": "Checking…",
    "unknown": "Not checked",
    "skipped": "Nothing to check",
}
_SEVERITY = {"down": 4, "degraded": 3, "up": 2, "checking": 1}

KIND_ROLE = Qt.ItemDataRole.UserRole + 1     # "tenant" | "favorites" | "connection"
TENANT_ROLE = Qt.ItemDataRole.UserRole + 2   # tenant name (tenant rows)
CONN_ROLE = Qt.ItemDataRole.UserRole         # connection dict (connection rows)

DENSITIES = ("comfortable", "compact")
_ROW_HEIGHTS = {
    "comfortable": {"tenant": 36, "connection": 30},
    "compact": {"tenant": 28, "connection": 23},
}


def protocol_tag(protocol: str) -> str:
    proto = str(protocol or "ssh").strip().lower()
    return PROTOCOL_TAGS.get(proto, proto.upper() or "SSH")


def _qcolor(value: str, alpha: float | None = None) -> QColor:
    color = QColor(value) if value and not str(value).startswith("rgba") else QColor("#cbd5e1")
    if alpha is not None:
        color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color


class ConnectionTreeWidget(QTreeWidget):
    """Tree widget that notifies when internal drag-drop reorder is completed."""

    order_changed = pyqtSignal()
    activate_selected = pyqtSignal()
    delete_selected = pyqtSignal()
    edit_selected = pyqtSignal()

    def dropEvent(self, event):
        super().dropEvent(event)
        self.order_changed.emit()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.activate_selected.emit()
            return
        if key == Qt.Key.Key_Delete:
            self.delete_selected.emit()
            return
        if key == Qt.Key.Key_F2:
            self.edit_selected.emit()
            return
        super().keyPressEvent(event)


def _row_key(index: QModelIndex):
    """A row's identity for hover easing: survives the tree being rebuilt."""
    if not index.isValid():
        return None
    kind = index.data(KIND_ROLE) or "connection"
    if kind == "connection":
        conn = index.data(CONN_ROLE) or {}
        return ("conn", str(conn.get("id") or conn.get("name") or ""))
    return (kind, index.data(TENANT_ROLE) or "")


class _RowHover(QObject):
    """Eases sidebar rows in and out of their hover look.

    Qt only says *whether* a row is under the mouse; this keeps a 0..1 level
    per row so the highlight, the accent bar and the nudge glide instead of
    snapping, and runs its timer only while something is moving.
    """

    IN_S = 0.14
    OUT_S = 0.22

    def __init__(self, tree: QTreeWidget):
        super().__init__(tree)
        self.tree = tree
        self.levels: dict = {}
        self.current = None
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        tree.viewport().installEventFilter(self)

    def level(self, index: QModelIndex) -> float:
        return self.levels.get(_row_key(index), 0.0)

    def eventFilter(self, obj, event):
        kind = event.type()
        # Arriving from outside the list is an Enter/HoverEnter with no move
        # after it if the cursor then rests, so those count as well.
        if kind in (QEvent.Type.MouseMove, QEvent.Type.HoverMove,
                    QEvent.Type.HoverEnter, QEvent.Type.Enter):
            self._aim(_row_key(self.tree.indexAt(event.position().toPoint())))
        elif kind in (QEvent.Type.Leave, QEvent.Type.HoverLeave, QEvent.Type.Hide):
            self._aim(None)
        return False

    def _aim(self, key):
        if key == self.current:
            return
        self.current = key
        if not self._timer.isActive():
            self._clock.restart()
            self._timer.start()

    def _tick(self):
        if not self._step(min(self._clock.restart() / 1000.0, 0.1)):
            self._timer.stop()

    def _step(self, dt: float) -> bool:
        """Advance every level by *dt* seconds; True while anything moves."""
        keys = set(self.levels)
        if self.current is not None:
            keys.add(self.current)
        moving = False
        for key in keys:
            value = self.levels.get(key, 0.0)
            if key == self.current:
                value = min(1.0, value + dt / self.IN_S)
                moving = moving or value < 1.0
            else:
                value = max(0.0, value - dt / self.OUT_S)
                moving = moving or value > 0.0
            if value <= 0.0:
                self.levels.pop(key, None)
            else:
                self.levels[key] = value
        try:
            self.tree.viewport().update()
        except RuntimeError:
            return False
        return moving


def _ease(t: float) -> float:
    """Ease-out cubic: quick to arrive, gentle to settle."""
    u = 1.0 - t
    return 1.0 - u * u * u


class _SidebarDelegate(QStyledItemDelegate):
    """Paints tenant and connection rows."""

    def __init__(self, panel: "SidePanel"):
        super().__init__(panel.tree)
        self.panel = panel
        self._pixmaps: dict[tuple, QPixmap] = {}
        self.hover = _RowHover(panel.tree)

    # ── helpers ───────────────────────────────────────────────────

    def _pixmap(self, name: str, size: int, color: str, dpr: float) -> QPixmap:
        key = (name, size, color, round(dpr, 2))
        pm = self._pixmaps.get(key)
        if pm is None:
            pm = icons.pixmap(name, size, color, dpr)
            self._pixmaps[key] = pm
        return pm

    def clear_cache(self):
        self._pixmaps.clear()

    def sizeHint(self, option, index):
        kind = index.data(KIND_ROLE) or "connection"
        heights = _ROW_HEIGHTS[self.panel.density]
        height = heights["tenant" if kind in ("tenant", "favorites") else "connection"]
        return QSize(max(80, option.rect.width()), height)

    # ── painting ──────────────────────────────────────────────────

    def paint(self, painter: QPainter, option, index: QModelIndex):
        painter.save()
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            kind = index.data(KIND_ROLE) or "connection"
            if kind in ("tenant", "favorites"):
                self._paint_group(painter, option, index, kind)
            else:
                self._paint_connection(painter, option, index)
        finally:
            painter.restore()

    def _backdrop(self, painter, rect: QRect, selected: bool, hovered: float,
                  radius: float = 7.0, accent: str | None = None):
        """Row background.  *hovered* is the eased 0..1 hover level: the wash
        fades in and an accent bar grows out from the row's middle."""
        t = self.panel.theme_colors
        box = QRectF(rect).adjusted(2.5, 1.5, -2.5, -1.5)
        if selected:
            painter.setPen(QPen(_qcolor(t["accent"], 0.55), 1))
            painter.setBrush(_qcolor(t["accent"], 0.20))
            painter.drawRoundedRect(box, radius, radius)
        elif hovered > 0.0:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_qcolor(t["text_primary"], 0.075 * hovered))
            painter.drawRoundedRect(box, radius, radius)
        if hovered > 0.0 and accent:
            bar_h = box.height() * 0.58 * _ease(hovered)
            color = _qcolor(accent, 0.95 * hovered)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(box.left() + 2.0, box.center().y() - bar_h / 2,
                                           3.0, bar_h), 1.5, 1.5)

    def _paint_group(self, painter, option, index, kind):
        panel = self.panel
        t = panel.theme_colors
        rect = option.rect
        dpr = painter.device().devicePixelRatioF() if painter.device() else 1.0
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        item = panel.tree.itemFromIndex(index)
        expanded = bool(item and item.isExpanded())
        tenant = index.data(TENANT_ROLE) or ""
        active = (kind == "tenant" and tenant == panel._active_tenant)
        level = self.hover.level(index) if hovered or self.hover.levels else 0.0
        tenant_color = (panel._tenant_colors.get(tenant) or t["accent"]
                        if kind == "tenant" else "#fbbf24")
        self._backdrop(painter, rect, selected or active, level,
                       accent=tenant_color)

        compact = panel.density == "compact"
        # The row's content leans in a few pixels as it lights up.
        x = rect.left() + 8 + round(3 * _ease(level))
        cy = rect.center().y()
        chevron = self._pixmap("chevron-down" if expanded else "chevron-right",
                               12, t["text_secondary"], dpr)
        painter.drawPixmap(QPoint(x, cy - 6), chevron)
        x += 18

        if kind == "favorites":
            star = self._pixmap("star-filled", 14, "#fbbf24", dpr)
            painter.drawPixmap(QPoint(x - 2, cy - 7), star)
        else:
            color = _qcolor(tenant_color)
            painter.setPen(Qt.PenStyle.NoPen)
            if level > 0.0:
                # The tenant dot glows and swells a little on hover.
                halo = 5 + 4 * _ease(level)
                painter.setBrush(_qcolor(tenant_color, 0.28 * level))
                painter.drawEllipse(QRectF(x + 5 - halo, cy - halo, halo * 2, halo * 2))
            r = 5 + 1.2 * _ease(level)
            painter.setBrush(color)
            painter.drawEllipse(QRectF(x + 5 - r, cy - r, r * 2, r * 2))
        x += 18

        # Right-hand side: count pill, worst state, production badge.
        right = rect.right() - 10
        conns = panel._group_members(item) if item is not None else []
        count_text = str(len(conns))
        font = QFont(panel.ui_font_family, 8 if compact else 9)
        font.setBold(True)
        fm = QFontMetrics(font)
        pill_w = max(22, fm.horizontalAdvance(count_text) + 12)
        pill = QRectF(right - pill_w, cy - 8, pill_w, 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_qcolor(t["text_primary"], 0.09))
        painter.drawRoundedRect(pill, 8, 8)
        painter.setFont(font)
        painter.setPen(_qcolor(t["text_secondary"]))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, count_text)
        right = pill.left() - 6

        worst = panel._worst_state(conns)
        if worst in ("down", "degraded"):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_qcolor(STATUS_COLORS[worst]))
            painter.drawEllipse(QRectF(right - 8, cy - 4, 8, 8))
            right -= 14

        if kind == "tenant" and panel._is_production_tenant(tenant):
            badge_font = QFont(panel.ui_font_family, 7)
            badge_font.setBold(True)
            bfm = QFontMetrics(badge_font)
            bw = bfm.horizontalAdvance("PROD") + 10
            badge = QRectF(right - bw, cy - 7, bw, 14)
            painter.setPen(QPen(QColor("#ef4444"), 1))
            painter.setBrush(QColor(127, 29, 29, 200))
            painter.drawRoundedRect(badge, 4, 4)
            painter.setFont(badge_font)
            painter.setPen(QColor("#fecaca"))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "PROD")
            right = badge.left() - 6

        name_font = QFont(panel.ui_font_family, 10 if compact else 11)
        name_font.setWeight(QFont.Weight.Bold)
        painter.setFont(name_font)
        painter.setPen(_qcolor(t["text_primary"]))
        name = "Favorites" if kind == "favorites" else tenant
        text_rect = QRect(x, rect.top(), max(0, int(right) - x), rect.height())
        shown = QFontMetrics(name_font).elidedText(
            name, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, shown)

    def _paint_connection(self, painter, option, index):
        panel = self.panel
        t = panel.theme_colors
        rect = option.rect
        dpr = painter.device().devicePixelRatioF() if painter.device() else 1.0
        conn = index.data(CONN_ROLE) or {}
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        compact = panel.density == "compact"
        indent = 20 if compact else 24
        row = QRect(rect.left() + indent, rect.top(), rect.width() - indent, rect.height())
        proto = str(conn.get("protocol", "ssh") or "ssh").lower()
        proto_color = PROTOCOL_COLORS.get(proto, t["accent"])
        live = panel._is_live(conn)
        level = self.hover.level(index) if hovered or self.hover.levels else 0.0
        # A live row already wears a bar on that edge.
        self._backdrop(painter, row, selected, level, radius=6,
                       accent=None if live else proto_color)
        cy = rect.center().y()

        if live:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_qcolor(proto_color))
            painter.drawRoundedRect(QRectF(row.left() + 3, cy - (rect.height() * 0.28),
                                           3, rect.height() * 0.56), 1.5, 1.5)

        x = row.left() + 10 + round(3 * _ease(level))
        icon_size = 13 if compact else 15
        painter.drawPixmap(QPoint(x, cy - icon_size // 2),
                           self._pixmap(icons.protocol_icon_name(proto), icon_size,
                                        proto_color, dpr))
        x += icon_size + 8

        # Right-hand side, from the edge inwards: status, tag, star.
        right = row.right() - 8
        state = panel._state(conn)
        dot = QRectF(right - 8, cy - 4, 8, 8)
        if state in STATUS_COLORS:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_qcolor(STATUS_COLORS[state]))
            painter.drawEllipse(dot)
        else:
            painter.setPen(QPen(_qcolor(t["text_secondary"], 0.55), 1.2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(dot.adjusted(0.6, 0.6, -0.6, -0.6))
        right = int(dot.left()) - 8

        if not compact:
            tag = protocol_tag(proto)
            tag_font = QFont(panel.ui_font_family, 7)
            tag_font.setBold(True)
            tfm = QFontMetrics(tag_font)
            tw = tfm.horizontalAdvance(tag) + 10
            tag_rect = QRectF(right - tw, cy - 7, tw, 14)
            painter.setPen(QPen(_qcolor(t["text_primary"], 0.16), 1))
            painter.setBrush(_qcolor(t["text_primary"], 0.06))
            painter.drawRoundedRect(tag_rect, 7, 7)
            painter.setFont(tag_font)
            painter.setPen(_qcolor(t["text_secondary"]))
            painter.drawText(tag_rect, Qt.AlignmentFlag.AlignCenter, tag)
            right = int(tag_rect.left()) - 6

        if conn.get("favorite"):
            painter.drawPixmap(QPoint(right - 12, cy - 6),
                               self._pixmap("star-filled", 12, "#fbbf24", dpr))
            right -= 16

        name_font = QFont(panel.ui_font_family, 9 if compact else 10)
        if live:
            name_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(name_font)
        alpha = 1.0 if (selected or live) else 0.86
        painter.setPen(_qcolor(t["text_primary"], alpha))
        name = str(conn.get("name", "Untitled")).rstrip().rstrip("●• ")
        text_rect = QRect(x, rect.top(), max(0, right - x), rect.height())
        shown = QFontMetrics(name_font).elidedText(
            name, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, shown)

    # ── tooltips with live status ─────────────────────────────────

    def helpEvent(self, event, view, option, index):
        if event is None or event.type() != QEvent.Type.ToolTip:
            return super().helpEvent(event, view, option, index)
        text = self.panel._tooltip_for(index)
        if text:
            QToolTip.showText(event.globalPos(), text, view)
            return True
        QToolTip.hideText()
        return True


class SidePanel(QDockWidget):
    """The Connections sidebar: every saved machine, grouped by tenant."""

    connect_requested = pyqtSignal(dict)  # emits connection info dict
    edit_requested = pyqtSignal(dict)
    show_password_requested = pyqtSignal(dict)
    delete_requested = pyqtSignal(str)  # emits connection ID
    disconnect_requested = pyqtSignal(dict)  # emits connection info dict
    reconnect_requested = pyqtSignal(dict)  # emits connection info dict
    wake_requested = pyqtSignal(dict)  # emits connection info dict (Wake-on-LAN)
    new_connection_for_tenant_requested = pyqtSignal(str)
    connections_reordered = pyqtSignal(list)  # emits ordered connection IDs
    tenant_color_changed = pyqtSignal(str, str)  # (tenant_name, hex_color)
    delete_tenant_requested = pyqtSignal(str)  # emits tenant name
    tenant_focus_requested = pyqtSignal(str)  # emits tenant name to focus dashboard
    connection_focus_requested = pyqtSignal(str)  # select its node on the dashboard
    tenant_close_requested = pyqtSignal(str)  # the selected tenant was closed
    #: (tenant, production) — the user marked a customer's estate as one where
    #: mistakes are expensive, so terminals under it warn before they run.
    production_tenant_toggled = pyqtSignal(str, bool)
    #: Open every machine in this tenant as one broadcast grid.
    broadcast_tenant_requested = pyqtSignal(str)
    #: (connection ids, favourite?)
    favorite_toggled = pyqtSignal(list, bool)
    duplicate_requested = pyqtSignal(dict)
    #: Several connections at once.
    open_many_requested = pyqtSignal(list)          # [conn dict]
    delete_many_requested = pyqtSignal(list)        # [conn id]
    move_requested = pyqtSignal(list, str)          # [conn id], tenant
    wake_many_requested = pyqtSignal(list)          # [conn id]
    check_requested = pyqtSignal(list)              # [conn id] — probe now
    rename_tenant_requested = pyqtSignal(str)
    tenant_notes_requested = pyqtSignal(str)
    tenant_vpn_requested = pyqtSignal(str)
    density_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__("Connections", parent)
        self.setObjectName("connections_dock")
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable |
                         QDockWidget.DockWidgetFeature.DockWidgetMovable)
        # The panel has its own header; the dock's title bar only repeated it.
        self.setTitleBarWidget(QWidget())
        self.setMinimumWidth(240)
        self.setMaximumWidth(380)
        self._connections: list[dict] = []
        self._tenant_colors: dict[str, str] = {}
        self._tenant_notes: dict[str, str] = {}
        self._production_tenants: list[str] = []
        self._active_tenant = None  # tenant currently selected / expanded
        self._active_connection_ids: set[str] = set()
        self._active_connection_signatures: set[tuple] = set()
        self._statuses: dict[str, dict] = {}
        self._theme: dict = {}
        self.theme_colors = self._colors_from({})
        self.ui_font_family = "Segoe UI"
        self.density = "comfortable"
        self._pending_filter_text = ""
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(lambda: self._rebuild_tree(self._pending_filter_text))
        self._setup_ui()

    # ── Construction ──────────────────────────────────────────────

    def _setup_ui(self):
        container = QWidget()
        container.setObjectName("side_panel_container")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 12, 10, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(6)
        self.title_icon = QLabel()
        self.title_icon.setPixmap(icons.pixmap("mote", 16, "#22d3ee"))
        header.addWidget(self.title_icon)
        self.title_label = QLabel("Connections")
        self.title_label.setStyleSheet("font-weight: 700; font-size: 14px; letter-spacing: 0.3px;")
        header.addWidget(self.title_label)
        header.addStretch()

        self.btn_view = QToolButton()
        self.btn_view.setIcon(icons.icon("settings"))
        self.btn_view.setIconSize(QSize(15, 15))
        self.btn_view.setFixedSize(30, 30)
        self.btn_view.setToolTip("Sidebar view options")
        self.btn_view.setAccessibleName("Sidebar view options")
        self.btn_view.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_view.setProperty("variant", "icon")
        view_menu = QMenu(self.btn_view)
        view_menu.aboutToShow.connect(lambda: self._populate_view_menu(view_menu))
        self.btn_view.setMenu(view_menu)
        header.addWidget(self.btn_view)

        self.btn_add = QPushButton()
        self.btn_add.setIcon(icons.icon("plus", "#ffffff", stroke=2.5))
        self.btn_add.setIconSize(QSize(16, 16))
        self.btn_add.setFixedSize(30, 30)
        self.btn_add.setToolTip("New connection (Ctrl+N)")
        self.btn_add.setAccessibleName("New connection")
        self.btn_add.setProperty("variant", "primary")
        self.btn_add.setStyleSheet("QPushButton { padding: 0px; }")
        header.addWidget(self.btn_add)
        layout.addLayout(header)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name, host, tenant, user, notes…")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icons.icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.setFixedHeight(34)
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._open_first_match)
        layout.addWidget(self.search)

        self.tree = ConnectionTreeWidget()
        self.tree.setObjectName("connections_tree")
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setAnimated(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setMouseTracking(True)
        self.tree.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.tree.setDragEnabled(True)
        self.tree.setAcceptDrops(True)
        self.tree.setDropIndicatorShown(True)
        self.tree.setDragDropMode(QTreeWidget.DragDropMode.InternalMove)
        self.tree.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setStyleSheet("""
            QTreeWidget#connections_tree {
                border: none; background: transparent; outline: 0; padding: 0;
            }
            QTreeWidget#connections_tree::item {
                padding: 0; margin: 0; border: none; background: transparent;
            }
            QTreeWidget#connections_tree::item:selected,
            QTreeWidget#connections_tree::item:hover {
                background: transparent;
            }
            QTreeWidget#connections_tree::branch {
                background: transparent; border: none; image: none;
            }
        """)
        self._delegate = _SidebarDelegate(self)
        self.tree.setItemDelegate(self._delegate)
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.itemClicked.connect(self._on_item_clicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.order_changed.connect(self._on_tree_reordered)
        self.tree.activate_selected.connect(self._open_selected)
        self.tree.delete_selected.connect(self._delete_selected)
        self.tree.edit_selected.connect(self._edit_selected)
        layout.addWidget(self.tree, 1)

        self.stats_label = QLabel("0 connections")
        self.stats_label.setProperty("role", "hint")
        self.stats_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.stats_label)

        self.setWidget(container)

    # ── Theme and density ─────────────────────────────────────────

    @staticmethod
    def _colors_from(theme: dict) -> dict:
        def pick(key, fallback):
            value = str(theme.get(key, fallback) or fallback)
            return value if value.startswith("#") else fallback
        return {
            "accent": pick("accent", "#8b5cf6"),
            "text_primary": pick("text_primary", "#f8fafc"),
            "text_secondary": pick("text_secondary", "#94a3b8"),
        }

    def set_theme(self, theme: dict):
        """Update all sidebar colours to match the given theme."""
        self._theme = dict(theme or {})
        self.theme_colors = self._colors_from(self._theme)
        is_matrix = self._theme.get("dashboard_theme") == "matrix"
        self.ui_font_family = "Courier New" if is_matrix else "Segoe UI"
        self.title_label.setStyleSheet(
            f"font-weight: 700; font-size: 14px; letter-spacing: 0.3px;"
            f" color: {self.theme_colors['text_primary']};"
            f" font-family: '{self.ui_font_family}';")
        if self._theme.get("accent"):
            self.btn_add.setIcon(icons.icon("plus", button_text_color(self._theme), stroke=2.5))
        self._delegate.clear_cache()
        self.tree.viewport().update()
        self._update_stats()

    def set_density(self, density: str, emit: bool = False):
        density = density if density in DENSITIES else "comfortable"
        if density == self.density:
            return
        self.density = density
        self.tree.doItemsLayout()
        self.tree.viewport().update()
        if emit:
            self.density_changed.emit(density)

    def _populate_view_menu(self, menu: QMenu):
        menu.clear()
        for value, label in (("comfortable", "Comfortable rows"), ("compact", "Compact rows")):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self.density == value)
            act.triggered.connect(lambda _c=False, v=value: self.set_density(v, emit=True))
        menu.addSeparator()
        menu.addAction(icons.icon("chevron-down"), "Expand every tenant", self._expand_all)
        menu.addAction(icons.icon("chevron-right"), "Collapse every tenant", self._collapse_all)

    def _expand_all(self):
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setExpanded(True)

    def _collapse_all(self):
        self._active_tenant = None
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setExpanded(False)

    # ── Data in ───────────────────────────────────────────────────

    def update_connections(self, connections: list[dict]):
        """Refresh the tree with the given list of connections."""
        self._connections = list(connections or [])
        self._rebuild_tree(self.search.text())

    def set_production_tenants(self, tenants):
        """Which tenants the context menu should show as already marked."""
        self._production_tenants = guardrails.normalize_production_tenants(
            tenants)
        self.tree.viewport().update()

    def set_tenant_colors(self, colors: dict):
        """Load tenant colors from vault settings."""
        self._tenant_colors = dict(colors) if colors else {}
        self.tree.viewport().update()

    def set_tenant_notes(self, notes: dict):
        self._tenant_notes = {str(k): str(v) for k, v in (notes or {}).items() if v}

    def set_active_connection_ids(self, connection_ids: set[str]):
        """Connections with a session open right now."""
        self._active_connection_ids = {str(x).strip() for x in (connection_ids or set()) if str(x).strip()}
        self.tree.viewport().update()

    def set_active_connection_signatures(self, signatures: set[tuple]):
        """Fallback for sessions (Quick Connect) that carry no saved ID."""
        self._active_connection_signatures = set(signatures or set())
        self.tree.viewport().update()

    def set_conn_statuses(self, statuses: dict):
        """Reachability for every connection, from the probe sweep."""
        self._statuses = {str(k): dict(v) for k, v in (statuses or {}).items()
                          if isinstance(v, dict)}
        self.tree.viewport().update()
        self._update_stats()

    def set_conn_status(self, conn_id: str, info: dict):
        if isinstance(info, dict):
            self._statuses[str(conn_id)] = dict(info)
            self.tree.viewport().update()
            self._update_stats()

    # ── State helpers (used by the delegate) ──────────────────────

    @staticmethod
    def _conn_signature(conn: dict) -> tuple:
        if not isinstance(conn, dict):
            return tuple()
        return (
            str(conn.get("protocol", "")).strip().lower(),
            str(conn.get("host", "")).strip().lower(),
            str(conn.get("port", "")).strip().lower(),
            str(conn.get("username", "")).strip().lower(),
            str(conn.get("name", "")).strip().lower(),
        )

    def _is_live(self, conn: dict) -> bool:
        conn_id = str(conn.get("id", "") or "").strip()
        return bool(
            (conn_id and conn_id in self._active_connection_ids)
            or self._conn_signature(conn) in self._active_connection_signatures
        )

    def _state(self, conn: dict) -> str:
        info = self._statuses.get(str(conn.get("id", "") or ""))
        return str((info or {}).get("state", "unknown"))

    def _worst_state(self, conns: list[dict]) -> str:
        worst, rank = "unknown", 0
        for conn in conns:
            state = self._state(conn)
            if _SEVERITY.get(state, 0) > rank:
                worst, rank = state, _SEVERITY[state]
        return worst

    def _is_production_tenant(self, tenant: str) -> bool:
        return any(t.casefold() == str(tenant or "").casefold()
                   for t in self._production_tenants)

    def _group_members(self, item: QTreeWidgetItem) -> list[dict]:
        members = []
        for j in range(item.childCount()):
            conn = item.child(j).data(0, CONN_ROLE)
            if isinstance(conn, dict):
                members.append(conn)
        return members

    def _tooltip_for(self, index: QModelIndex) -> str:
        kind = index.data(KIND_ROLE) or "connection"
        item = self.tree.itemFromIndex(index)
        if kind in ("tenant", "favorites") and item is not None:
            members = self._group_members(item)
            name = "Favorites" if kind == "favorites" else (index.data(TENANT_ROLE) or "")
            lines = [f"<b>{_html(name)}</b> — {len(members)} connection{'s' if len(members) != 1 else ''}"]
            problems = [c for c in members if self._state(c) in ("down", "degraded")]
            if problems:
                lines.append(f"<span style='color:#f87171'>{len(problems)} need attention:</span> "
                             + ", ".join(_html(c.get("name", "?")) for c in problems[:6])
                             + ("…" if len(problems) > 6 else ""))
            if kind == "tenant" and self._is_production_tenant(name):
                lines.append("<span style='color:#fca5a5'>Production tenant</span>")
            note = self._tenant_notes.get(name, "")
            if note:
                lines.append("<i>" + _html(note.strip().splitlines()[0][:160]) + "</i>")
            return "<br>".join(lines)
        conn = index.data(CONN_ROLE)
        if not isinstance(conn, dict):
            return ""
        proto = protocol_tag(conn.get("protocol", "ssh"))
        host = str(conn.get("host", "") or "")
        port = conn.get("port", "")
        lines = [f"<b>{_html(conn.get('name', ''))}</b>"]
        where = f"{proto} · {_html(host)}" + (f":{port}" if host and port else "")
        lines.append(where)
        user = str(conn.get("username", "") or "")
        if user:
            lines.append(f"User: {_html(user)}")
        info = self._statuses.get(str(conn.get("id", "") or ""), {})
        state = str(info.get("state", "unknown"))
        status = STATUS_LABELS.get(state, state)
        latency = info.get("latency_ms")
        if state == "up" and isinstance(latency, (int, float)):
            status += f" ({latency:.0f} ms)"
        color = STATUS_COLORS.get(state, "#94a3b8")
        lines.append(f"<span style='color:{color}'>● {status}</span>")
        if self._is_live(conn):
            lines.append("Session open")
        note = str(conn.get("notes", "") or "").strip()
        if note:
            first = note.splitlines()[0]
            lines.append("<i>" + _html(first[:160]) + ("…" if len(first) > 160 or "\n" in note else "") + "</i>")
        return "<br>".join(lines)

    # ── Building the tree ─────────────────────────────────────────

    def expand_tenant(self, tenant_name: str):
        """Expand, select and scroll to a tenant (the dashboard asked)."""
        self._active_tenant = tenant_name
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item is None or item.data(0, KIND_ROLE) != "tenant":
                continue
            if item.data(0, TENANT_ROLE) == tenant_name:
                for j in range(self.tree.topLevelItemCount()):
                    other = self.tree.topLevelItem(j)
                    if other is not None and other is not item and other.data(0, KIND_ROLE) == "tenant":
                        other.setExpanded(False)
                item.setExpanded(True)
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                break
        self.tree.viewport().update()

    @staticmethod
    def _matches(conn: dict, needles: list[str]) -> bool:
        if not needles:
            return True
        hay = " ".join(str(conn.get(k, "") or "") for k in (
            "name", "host", "tenant", "protocol", "username", "notes")).casefold()
        hay += " " + protocol_tag(conn.get("protocol", "")).casefold()
        return all(n in hay for n in needles)

    def _rebuild_tree(self, filter_text: str = ""):
        expanded = {
            self.tree.topLevelItem(i).data(0, TENANT_ROLE) or self.tree.topLevelItem(i).data(0, KIND_ROLE)
            for i in range(self.tree.topLevelItemCount())
            if self.tree.topLevelItem(i) is not None and self.tree.topLevelItem(i).isExpanded()
        }
        selected_ids = {str((it.data(0, CONN_ROLE) or {}).get("id", ""))
                        for it in self.tree.selectedItems()
                        if isinstance(it.data(0, CONN_ROLE), dict)}
        self.tree.setUpdatesEnabled(False)
        try:
            self.tree.clear()
            needles = [n for n in str(filter_text or "").casefold().split() if n]
            filtering = bool(needles)

            favorites = [c for c in self._connections
                         if isinstance(c, dict) and c.get("favorite") and self._matches(c, needles)]
            if favorites:
                fav_item = QTreeWidgetItem([""])
                fav_item.setData(0, KIND_ROLE, "favorites")
                fav_item.setFlags((fav_item.flags() | Qt.ItemFlag.ItemIsSelectable)
                                  & ~(Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled))
                self.tree.addTopLevelItem(fav_item)
                for conn in favorites:
                    fav_item.addChild(self._connection_item(conn))
                fav_item.setExpanded(filtering or "favorites" in expanded or not expanded)

            groups: dict[str, QTreeWidgetItem] = {}
            for conn in self._connections:
                if not isinstance(conn, dict) or not self._matches(conn, needles):
                    continue
                tenant = conn.get("tenant", "Default") or "Default"
                group = groups.get(tenant)
                if group is None:
                    group = QTreeWidgetItem([""])
                    group.setData(0, KIND_ROLE, "tenant")
                    group.setData(0, TENANT_ROLE, tenant)
                    group.setFlags(group.flags() | Qt.ItemFlag.ItemIsSelectable
                                   | Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled)
                    self.tree.addTopLevelItem(group)
                    groups[tenant] = group
                group.addChild(self._connection_item(conn))

            for tenant, group in groups.items():
                if filtering:
                    group.setExpanded(True)
                elif self._active_tenant:
                    group.setExpanded(tenant == self._active_tenant)
                else:
                    group.setExpanded(tenant in expanded)

            if selected_ids:
                for item in self._iter_connection_items():
                    conn = item.data(0, CONN_ROLE) or {}
                    if str(conn.get("id", "")) in selected_ids:
                        item.setSelected(True)
        finally:
            self.tree.setUpdatesEnabled(True)
        self._update_stats(filter_text)

    def _connection_item(self, conn: dict) -> QTreeWidgetItem:
        child = QTreeWidgetItem([""])
        child.setData(0, KIND_ROLE, "connection")
        child.setData(0, CONN_ROLE, conn)
        # Connection rows stay put: reorder by dragging the tenant header,
        # move between tenants with "Move to tenant".
        child.setFlags((child.flags() | Qt.ItemFlag.ItemIsSelectable)
                       & ~(Qt.ItemFlag.ItemIsDragEnabled | Qt.ItemFlag.ItemIsDropEnabled))
        return child

    def _iter_connection_items(self):
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            if top is None:
                continue
            for j in range(top.childCount()):
                yield top.child(j)

    def _update_stats(self, filter_text: str | None = None):
        if filter_text is None:
            filter_text = self.search.text()
        needles = [n for n in str(filter_text or "").casefold().split() if n]
        conns = [c for c in self._connections if isinstance(c, dict) and self._matches(c, needles)]
        tenants = {c.get("tenant", "Default") or "Default" for c in conns}
        down = sum(1 for c in conns if self._state(c) == "down")
        degraded = sum(1 for c in conns if self._state(c) == "degraded")
        text = (f"{len(conns)} connection{'s' if len(conns) != 1 else ''}"
                f" · {len(tenants)} tenant{'s' if len(tenants) != 1 else ''}")
        if down:
            text += f" · <span style='color:#f87171'>{down} down</span>"
        if degraded:
            text += f" · <span style='color:#fbbf24'>{degraded} degraded</span>"
        self.stats_label.setText(text)

    def _filter(self, text):
        self._pending_filter_text = text
        self._search_timer.start()

    def _open_first_match(self):
        """Enter in the filter box opens the only (or first) match."""
        self._search_timer.stop()
        self._rebuild_tree(self.search.text())
        for item in self._iter_connection_items():
            conn = item.data(0, CONN_ROLE)
            if isinstance(conn, dict):
                self.connect_requested.emit(conn)
                return

    # ── Interaction ───────────────────────────────────────────────

    def _on_item_clicked(self, item, column):
        """Clicking a tenant opens it (and closes the others)."""
        if not item:
            return
        kind = item.data(0, KIND_ROLE)
        modifiers = QGuiApplication.keyboardModifiers()
        if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier):
            return  # building a multi-selection, not navigating
        if kind == "connection":
            conn = item.data(0, CONN_ROLE)
            if isinstance(conn, dict):
                self.connection_focus_requested.emit(str(conn.get("id", "")))
            return
        if kind == "favorites":
            item.setExpanded(not item.isExpanded())
            return

        tenant_name = item.data(0, TENANT_ROLE)
        if item.isExpanded() and self._active_tenant == tenant_name:
            item.setExpanded(False)
            self._active_tenant = None
            self.tenant_close_requested.emit(tenant_name)
            self.tree.viewport().update()
            return
        self._active_tenant = tenant_name
        self.tenant_focus_requested.emit(tenant_name)
        filtering = bool(self.search.text().strip())
        for i in range(self.tree.topLevelItemCount()):
            other = self.tree.topLevelItem(i)
            if other is item:
                other.setExpanded(True)
            elif other is not None and other.data(0, KIND_ROLE) == "tenant" and not filtering:
                other.setExpanded(False)
        self.tree.viewport().update()

    def _on_double_click(self, item, column):
        conn = item.data(0, CONN_ROLE)
        if isinstance(conn, dict):
            self.connect_requested.emit(conn)

    def selected_connections(self) -> list[dict]:
        seen, result = set(), []
        for item in self.tree.selectedItems():
            conn = item.data(0, CONN_ROLE)
            if isinstance(conn, dict):
                cid = str(conn.get("id", "") or id(conn))
                if cid not in seen:
                    seen.add(cid)
                    result.append(conn)
        return result

    def _open_selected(self):
        conns = self.selected_connections()
        if len(conns) == 1:
            self.connect_requested.emit(conns[0])
        elif conns:
            self.open_many_requested.emit(conns)

    def _delete_selected(self):
        conns = self.selected_connections()
        if len(conns) == 1:
            self.delete_requested.emit(str(conns[0].get("id", "")))
        elif conns:
            self.delete_many_requested.emit([str(c.get("id", "")) for c in conns])

    def _edit_selected(self):
        conns = self.selected_connections()
        if len(conns) == 1:
            self.edit_requested.emit(conns[0])

    def _on_tree_reordered(self):
        """Persist tree visual order after drag-and-drop tenant reordering."""
        ordered_ids: list[str] = []
        for i in range(self.tree.topLevelItemCount()):
            tenant_item = self.tree.topLevelItem(i)
            if tenant_item is None or tenant_item.data(0, KIND_ROLE) != "tenant":
                continue
            for j in range(tenant_item.childCount()):
                conn = tenant_item.child(j).data(0, CONN_ROLE)
                if isinstance(conn, dict):
                    conn_id = str(conn.get("id", "")).strip()
                    if conn_id:
                        ordered_ids.append(conn_id)
        if ordered_ids:
            self.connections_reordered.emit(ordered_ids)

    def _tenant_names(self) -> list[str]:
        names = {str(c.get("tenant", "Default") or "Default") for c in self._connections
                 if isinstance(c, dict)}
        return sorted(names, key=str.casefold)

    def _add_move_menu(self, menu: QMenu, conns: list[dict]):
        move = menu.addMenu(icons.icon("folder"), "Move to tenant")
        ids = [str(c.get("id", "")) for c in conns]
        current = {c.get("tenant", "Default") or "Default" for c in conns}
        for name in self._tenant_names():
            act = move.addAction(name)
            act.setEnabled(not (len(current) == 1 and name in current))
            act.triggered.connect(lambda _c=False, n=name: self.move_requested.emit(ids, n))
        move.addSeparator()

        def new_tenant():
            name, ok = QInputDialog.getText(self, "Move to a new tenant", "Tenant name:")
            name = (name or "").strip()
            if ok and name:
                self.move_requested.emit(ids, name)
        move.addAction(icons.icon("plus"), "New tenant…", new_tenant)

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return
        kind = item.data(0, KIND_ROLE)
        if kind == "tenant":
            self._tenant_menu(item, pos)
            return
        if kind == "favorites":
            return

        if not item.isSelected():
            self.tree.clearSelection()
            item.setSelected(True)
            self.tree.setCurrentItem(item)
        conns = self.selected_connections()
        if len(conns) > 1:
            self._multi_menu(conns, pos)
            return
        conn = item.data(0, CONN_ROLE)
        if not isinstance(conn, dict):
            return

        menu = QMenu(self)
        act = menu.addAction(icons.icon("arrow-right"), "Connect")
        act.setShortcut(QKeySequence("Return"))
        act.triggered.connect(lambda: self.connect_requested.emit(conn))
        act = menu.addAction(icons.icon("edit"), "Edit…")
        act.setShortcut(QKeySequence("F2"))
        act.triggered.connect(lambda: self.edit_requested.emit(conn))
        menu.addAction(icons.icon("copy"), "Duplicate",
                       lambda: self.duplicate_requested.emit(conn))
        fav = bool(conn.get("favorite"))
        menu.addAction(icons.icon("star" if fav else "star-filled", None if fav else "#fbbf24"),
                       "Remove from favorites" if fav else "Add to favorites",
                       lambda: self.favorite_toggled.emit([str(conn.get("id", ""))], not fav))
        menu.addSeparator()

        host = str(conn.get("host", "") or "")
        copy_host = menu.addAction(icons.icon("copy"), "Copy host")
        copy_host.setEnabled(bool(host))
        copy_host.triggered.connect(lambda: QGuiApplication.clipboard().setText(host))
        user = str(conn.get("username", "") or "")
        if user and host and str(conn.get("protocol", "")).lower() in ("ssh", "sftp"):
            menu.addAction(icons.icon("copy"), f"Copy {user}@{host}",
                           lambda: QGuiApplication.clipboard().setText(f"{user}@{host}"))
        password_action = menu.addAction(icons.icon("key"), "Show password…")
        password_action.setToolTip("View the credential saved for this connection")
        password_action.setEnabled(bool(conn.get("password") or conn.get("azure_client_secret")
                                        or conn.get("credential_id")))
        password_action.triggered.connect(lambda: self.show_password_requested.emit(conn))
        menu.addSeparator()

        live = self._is_live(conn)
        act = menu.addAction(icons.icon("refresh"), "Reconnect")
        act.setEnabled(live)
        act.triggered.connect(lambda: self.reconnect_requested.emit(conn))
        act = menu.addAction(icons.icon("x"), "Disconnect")
        act.setEnabled(live)
        act.triggered.connect(lambda: self.disconnect_requested.emit(conn))
        wake_action = menu.addAction(icons.icon("power"), "Wake (Wake-on-LAN)")
        wake_action.setEnabled(is_wakeable(conn))
        wake_action.setToolTip(
            "Send a magic packet to this machine's MAC address"
            if wake_action.isEnabled()
            else "Add a MAC address to this connection to wake it")
        wake_action.triggered.connect(lambda: self.wake_requested.emit(conn))
        menu.addAction(icons.icon("activity"), "Check reachability now",
                       lambda: self.check_requested.emit([str(conn.get("id", ""))]))
        self._add_move_menu(menu, [conn])
        menu.addSeparator()
        menu.addAction(icons.icon("trash", "#f87171"), "Delete…",
                       lambda: self.delete_requested.emit(str(conn.get("id", ""))))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _multi_menu(self, conns: list[dict], pos):
        n = len(conns)
        ids = [str(c.get("id", "")) for c in conns]
        menu = QMenu(self)
        menu.addAction(icons.icon("arrow-right"), f"Open {n} connections",
                       lambda: self.open_many_requested.emit(conns))
        all_fav = all(c.get("favorite") for c in conns)
        menu.addAction(icons.icon("star-filled", "#fbbf24"),
                       "Remove from favorites" if all_fav else "Add to favorites",
                       lambda: self.favorite_toggled.emit(ids, not all_fav))
        hosts = "\n".join(str(c.get("host", "") or "") for c in conns if c.get("host"))
        menu.addAction(icons.icon("copy"), "Copy hosts",
                       lambda: QGuiApplication.clipboard().setText(hosts))
        menu.addSeparator()
        menu.addAction(icons.icon("activity"), "Check reachability now",
                       lambda: self.check_requested.emit(ids))
        wakeable = [str(c.get("id", "")) for c in conns if is_wakeable(c)]
        act = menu.addAction(icons.icon("power"), f"Wake {len(wakeable)} (Wake-on-LAN)")
        act.setEnabled(bool(wakeable))
        act.triggered.connect(lambda: self.wake_many_requested.emit(wakeable))
        self._add_move_menu(menu, conns)
        menu.addSeparator()
        menu.addAction(icons.icon("trash", "#f87171"), f"Delete {n} connections…",
                       lambda: self.delete_many_requested.emit(ids))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _tenant_menu(self, item, pos):
        tenant_name = item.data(0, TENANT_ROLE) or "Default"
        members = self._group_members(item)
        menu = QMenu(self)
        menu.addAction(icons.icon("plus"), "Add connection here",
                       lambda: self.new_connection_for_tenant_requested.emit(tenant_name))
        menu.addAction(icons.icon("edit"), "Rename tenant…",
                       lambda: self.rename_tenant_requested.emit(tenant_name))
        current_hex = self._tenant_colors.get(tenant_name, "#a78bfa")
        menu.addAction(icons.icon("palette"), "Change colour…",
                       lambda: self._pick_tenant_color(tenant_name, current_hex))
        menu.addAction(icons.icon("note"), "Notes…",
                       lambda: self.tenant_notes_requested.emit(tenant_name))
        menu.addAction(icons.icon("vpn"), "Connect VPN first…",
                       lambda: self.tenant_vpn_requested.emit(tenant_name))
        menu.addSeparator()

        broadcast_action = menu.addAction(icons.icon("broadcast"), "Broadcast to tenant…")
        broadcast_action.setToolTip(
            "Open this tenant's machines side by side and type to all of them at once.")
        broadcast_action.triggered.connect(
            lambda: self.broadcast_tenant_requested.emit(tenant_name))
        ids = [str(c.get("id", "")) for c in members]
        menu.addAction(icons.icon("activity"), "Check reachability now",
                       lambda: self.check_requested.emit(ids))
        wakeable = [str(c.get("id", "")) for c in members if is_wakeable(c)]
        act = menu.addAction(icons.icon("power"), f"Wake all ({len(wakeable)} with a MAC)")
        act.setEnabled(bool(wakeable))
        act.triggered.connect(lambda: self.wake_many_requested.emit(wakeable))

        marked = self._is_production_tenant(tenant_name)
        production_action = menu.addAction(icons.icon("shield"), "Production tenant")
        production_action.setCheckable(True)
        production_action.setChecked(marked)
        production_action.setToolTip(
            "Terminals opened here wear a PROD badge and ask before running "
            "anything destructive.")
        production_action.triggered.connect(
            lambda checked, tenant=tenant_name: self.production_tenant_toggled.emit(tenant, checked))
        menu.addSeparator()
        menu.addAction(icons.icon("trash", "#f87171"), "Delete tenant…",
                       lambda: self.delete_tenant_requested.emit(tenant_name))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _pick_tenant_color(self, tenant_name: str, current_hex: str):
        """Open a color dialog and emit the chosen color for the tenant."""
        color = QColorDialog.getColor(QColor(current_hex), self, f"Colour for {tenant_name}")
        if color.isValid():
            hex_color = color.name()
            self._tenant_colors[tenant_name] = hex_color
            self.tenant_color_changed.emit(tenant_name, hex_color)
            self.tree.viewport().update()


def _html(text) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))
