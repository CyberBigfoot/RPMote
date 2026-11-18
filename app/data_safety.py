"""
Recently Deleted and Backups windows — the undo for the vault.
"""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
    QLabel, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from app import backups, icons
from app.dialogs import confirm
from app.vault import TRASH_DAYS, VaultError
from app.widgets import fit_to_screen, set_variant


def _ago(timestamp: float) -> str:
    seconds = max(0, datetime.now().timestamp() - float(timestamp or 0))
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    days = int(seconds // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


class RecentlyDeletedDialog(QDialog):
    """Deleted connections, restorable for TRASH_DAYS days."""

    def __init__(self, vault, parent=None):
        super().__init__(parent)
        self.vault = vault
        self.restored = 0
        self.setWindowTitle("Recently Deleted")
        self.setWindowIcon(icons.icon("trash"))
        self.setMinimumSize(620, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        intro = QLabel(f"Deleted connections stay here for {TRASH_DAYS} days. "
                       "Select some and restore them, or delete them for good.")
        intro.setWordWrap(True)
        intro.setProperty("role", "hint")
        layout.addWidget(intro)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Connection", "Tenant", "Host", "Deleted"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tree, 1)

        row = QHBoxLayout()
        self.btn_restore = QPushButton("Restore")
        self.btn_restore.setIcon(icons.icon("undo"))
        set_variant(self.btn_restore, "primary")
        self.btn_restore.clicked.connect(self._restore)
        self.btn_purge = QPushButton("Delete for good")
        self.btn_purge.setIcon(icons.icon("trash", "#f87171"))
        set_variant(self.btn_purge, "ghost")
        self.btn_purge.clicked.connect(self._purge)
        row.addWidget(self.btn_restore)
        row.addWidget(self.btn_purge)
        row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

        self.tree.itemSelectionChanged.connect(self._sync)
        self._load()
        self.resize(760, 460)
        fit_to_screen(self)

    def _load(self):
        self.tree.clear()
        for entry in self.vault.trash:
            conn = entry.get("connection") or {}
            item = QTreeWidgetItem([
                str(conn.get("name", "?")),
                str(conn.get("tenant", "Default") or "Default"),
                str(conn.get("host", "") or ""),
                _ago(entry.get("deleted_at", 0)),
            ])
            item.setIcon(0, icons.protocol_icon(conn.get("protocol", "ssh")))
            item.setData(0, Qt.ItemDataRole.UserRole, str(conn.get("id", "")))
            self.tree.addTopLevelItem(item)
        if self.tree.topLevelItemCount() == 0:
            empty = QTreeWidgetItem(["Nothing deleted in the last 30 days", "", "", ""])
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tree.addTopLevelItem(empty)
        self._sync()

    def _selected_ids(self) -> list[str]:
        return [str(i.data(0, Qt.ItemDataRole.UserRole)) for i in self.tree.selectedItems()
                if i.data(0, Qt.ItemDataRole.UserRole)]

    def _sync(self):
        has = bool(self._selected_ids())
        self.btn_restore.setEnabled(has)
        self.btn_purge.setEnabled(has)

    def _restore(self):
        ids = self._selected_ids()
        try:
            self.restored += self.vault.restore_from_trash(ids)
        except VaultError as exc:
            QMessageBox.warning(self, "Restore", str(exc))
        self._load()

    def _purge(self):
        ids = self._selected_ids()
        if not ids:
            return
        if not confirm(self, "Delete for good",
                       f"Delete {len(ids)} connection{'s' if len(ids) != 1 else ''} permanently?",
                       "Delete for good", danger=True,
                       informative="They will be gone from Recently Deleted. "
                                   "A vault backup taken before today can still hold them."):
            return
        try:
            self.vault.purge_trash(ids)
        except VaultError as exc:
            QMessageBox.warning(self, "Delete", str(exc))
        self._load()


class BackupsDialog(QDialog):
    """List, take and restore vault backups."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.restore_requested_path = None
        self.setWindowTitle("Vault Backups")
        self.setWindowIcon(icons.icon("archive"))
        self.setMinimumSize(600, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        intro = QLabel(
            "RPMote keeps the last 20 encrypted copies of your vault: one a day, "
            "plus one before anything destructive. A backup opens with the "
            "master password that was in use when it was taken.")
        intro.setWordWrap(True)
        intro.setProperty("role", "hint")
        layout.addWidget(intro)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Taken", "Why", "Size"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.tree, 1)

        row = QHBoxLayout()
        self.btn_now = QPushButton("Back up now")
        self.btn_now.setIcon(icons.icon("archive"))
        self.btn_now.clicked.connect(self._backup_now)
        self.btn_restore = QPushButton("Restore…")
        self.btn_restore.setIcon(icons.icon("undo"))
        set_variant(self.btn_restore, "primary")
        self.btn_restore.clicked.connect(self._restore)
        self.btn_folder = QPushButton("Open folder")
        self.btn_folder.setIcon(icons.icon("folder"))
        set_variant(self.btn_folder, "ghost")
        self.btn_folder.clicked.connect(self._open_folder)
        for b in (self.btn_now, self.btn_restore, self.btn_folder):
            row.addWidget(b)
        row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

        self.tree.itemSelectionChanged.connect(
            lambda: self.btn_restore.setEnabled(bool(self.tree.selectedItems())))
        self._load()
        self.resize(720, 460)
        fit_to_screen(self)

    def _load(self):
        self.tree.clear()
        for info in backups.list_backups():
            item = QTreeWidgetItem([info.label, info.reason or "—", backups.format_size(info.size)])
            item.setData(0, Qt.ItemDataRole.UserRole, str(info.path))
            self.tree.addTopLevelItem(item)
        self.btn_restore.setEnabled(False)

    def _backup_now(self):
        path = backups.create_backup("manual")
        if path is None:
            QMessageBox.information(self, "Back up", "There is no vault file to back up yet.")
        self._load()

    def _restore(self):
        items = self.tree.selectedItems()
        if not items:
            return
        path = items[0].data(0, Qt.ItemDataRole.UserRole)
        label = items[0].text(0)
        if not confirm(
                self, "Restore backup",
                f"Replace the current vault with the backup from {label}?",
                "Restore and restart", danger=True,
                informative="The current vault is backed up first, so this can be "
                            "undone. RPMote restarts afterwards — unlock it with the "
                            "master password you were using on that date."):
            return
        self.restore_requested_path = path
        self.accept()

    def _open_folder(self):
        folder = backups.backup_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
