"""Choosing the machines a broadcast session will open.

The estate already knows every box and which customer it belongs to, so
picking eight of them should be eight clicks, not eight trips through the
connection list.  Tenants are the grouping because that is how an MSP thinks
about the estate — and because "everything in this tenant" is the selection
people actually want when they are patching.

Production connections are marked here rather than only in the terminal: the
moment to notice that three of your eight are customer-facing is while you
are choosing them.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from app import guardrails, icons

#: Protocols a shell can be opened on.
SHELL_PROTOCOLS = ("ssh", "")

CONN_ROLE = Qt.ItemDataRole.UserRole


def shell_connections(connections) -> list:
    """The connections a broadcast session can actually open."""
    out = []
    for conn in connections or []:
        if not isinstance(conn, dict) or not conn.get("host"):
            continue
        if str(conn.get("protocol", "ssh") or "ssh").lower() != "ssh":
            continue
        out.append(conn)
    return out


def group_by_tenant(connections) -> dict:
    """``{tenant: [conn, ...]}``, tenants in alphabetical order."""
    grouped: dict = {}
    for conn in connections or []:
        tenant = str(conn.get("tenant") or "Default")
        grouped.setdefault(tenant, []).append(conn)
    return {tenant: grouped[tenant] for tenant in sorted(grouped)}

from app.widgets import set_variant

class HostPickerDialog(QDialog):
    """Tick the machines to open side by side."""

    def __init__(self, connections, production_tenants=(), preselect=(),
                 parent=None, title="Open a broadcast session"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(560, 560)

        self._connections = shell_connections(connections)
        self._production_tenants = guardrails.normalize_production_tenants(
            production_tenants)
        preselected = {str(conn.get("id", "")) for conn in (preselect or [])
                       if isinstance(conn, dict)}
        preselected |= {str(item) for item in (preselect or [])
                        if isinstance(item, str)}

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name, host or tenant…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Machine", "Host"])
        self.tree.setColumnWidth(0, 300)
        self.tree.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.tree, 1)

        self._build_tree(preselected)

        row = QHBoxLayout()
        self.select_all = QPushButton("Select all")
        self.select_all.clicked.connect(lambda: self._set_all(True))
        row.addWidget(self.select_all)
        self.select_none = QPushButton("Select none")
        self.select_none.clicked.connect(lambda: self._set_all(False))
        row.addWidget(self.select_none)
        row.addStretch()
        self.count_label = QLabel("")
        row.addWidget(self.count_label)
        layout.addLayout(row)

        self.arm_now = QCheckBox("Start with broadcast already on")
        self.arm_now.setToolTip(
            "Off by default: opening the panes and arming them are two "
            "decisions, and the second one deserves its own click.")
        layout.addWidget(self.arm_now)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Open
            | QDialogButtonBox.StandardButton.Cancel)
        set_variant(self.buttons.button(QDialogButtonBox.StandardButton.Open), "primary")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._refresh_count()

    # ── Building ────────────────────────────────────────────────────

    def _build_tree(self, preselected):
        self.tree.blockSignals(True)
        for tenant, conns in group_by_tenant(self._connections).items():
            parent = QTreeWidgetItem(self.tree, [tenant, ""])
            parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            parent.setCheckState(0, Qt.CheckState.Unchecked)
            parent.setExpanded(True)
            for conn in conns:
                name = str(conn.get("name") or conn.get("host"))
                production = guardrails.is_production(conn,
                                                      self._production_tenants)
                child = QTreeWidgetItem(parent, [name, str(conn.get("host", ""))])
                if production:
                    child.setIcon(0, icons.icon("warning", "#f59e0b"))
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setData(0, CONN_ROLE, conn)
                child.setCheckState(
                    0, Qt.CheckState.Checked
                    if str(conn.get("id", "")) in preselected
                    else Qt.CheckState.Unchecked)
                if production:
                    child.setToolTip(0, guardrails.production_reason(
                        conn, self._production_tenants))
        self.tree.blockSignals(False)

    # ── Selection ───────────────────────────────────────────────────

    def _rows(self):
        for index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(index)
            for position in range(parent.childCount()):
                yield parent, parent.child(position)

    def chosen(self) -> list:
        """The connections that were ticked, in the order they are shown."""
        return [child.data(0, CONN_ROLE) for _parent, child in self._rows()
                if child.checkState(0) == Qt.CheckState.Checked
                and not child.isHidden()]

    def production_chosen(self) -> list:
        return [conn for conn in self.chosen()
                if guardrails.is_production(conn, self._production_tenants)]

    def arm_immediately(self) -> bool:
        return self.arm_now.isChecked()

    def _on_item_changed(self, item, _column):
        self.tree.blockSignals(True)
        if item.childCount():
            # A tenant is a shortcut for everything under it that is showing.
            state = item.checkState(0)
            for position in range(item.childCount()):
                child = item.child(position)
                if not child.isHidden():
                    child.setCheckState(0, state)
        self.tree.blockSignals(False)
        self._refresh_count()

    def _set_all(self, checked: bool):
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.tree.blockSignals(True)
        for parent, child in self._rows():
            if not child.isHidden():
                child.setCheckState(0, state)
            parent.setCheckState(0, Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self._refresh_count()

    def _apply_filter(self, text: str):
        needle = str(text or "").strip().casefold()
        for index in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(index)
            shown = 0
            for position in range(parent.childCount()):
                child = parent.child(position)
                conn = child.data(0, CONN_ROLE) or {}
                haystack = " ".join(str(conn.get(field, "")) for field in
                                    ("name", "host", "tenant")).casefold()
                visible = needle in haystack if needle else True
                child.setHidden(not visible)
                shown += 1 if visible else 0
            parent.setHidden(shown == 0)
        self._refresh_count()

    def _refresh_count(self):
        chosen = self.chosen()
        production = len([conn for conn in chosen
                          if guardrails.is_production(conn,
                                                      self._production_tenants)])
        text = f"{len(chosen)} selected"
        if production:
            text += f" · {production} production"
        self.count_label.setText(text)
        self.count_label.setStyleSheet(
            "color: #fca5a5; font-size: 11px;" if production
            else "color: #94a3b8; font-size: 11px;")
        self.buttons.button(QDialogButtonBox.StandardButton.Open).setEnabled(
            bool(chosen))
