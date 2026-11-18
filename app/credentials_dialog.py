"""
Shared Credentials — one login, many connections.

An MSP signs in to thirty machines of one customer with the same domain
admin account. Stored thirty times, a password change is thirty edits (and
the one you miss locks the account out). Stored once here and referenced by
each connection, it is one edit, and every connection uses the new password
the next time it opens.
"""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from app import icons
from app.dialogs import confirm
from app.saved_passwords import attach_reveal_toggle, confirm_master_password
from app.vault import VaultError
from app.widgets import fit_to_screen, set_variant


def usage_count(vault, cred_id: str) -> int:
    try:
        return sum(1 for c in vault.connections
                   if isinstance(c, dict) and str(c.get("credential_id", "")) == str(cred_id))
    except Exception:
        return 0


class CredentialEditDialog(QDialog):
    """Add or edit one shared credential."""

    def __init__(self, vault, credential: dict | None = None, parent=None):
        super().__init__(parent)
        self.vault = vault
        self.credential = dict(credential or {})
        self._authorised = not bool(self.credential.get("id"))
        editing = bool(self.credential.get("id"))
        self.setWindowTitle("Edit shared credential" if editing else "New shared credential")
        self.setMinimumWidth(500)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 14)
        layout.setSpacing(12)
        intro = QLabel("Connections that use this credential sign in with it. "
                       "Change the password here and they all follow.")
        intro.setWordWrap(True)
        intro.setProperty("role", "hint")
        layout.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.name = QLineEdit(self.credential.get("name", ""))
        self.name.setPlaceholderText("e.g. Contoso domain admin")
        form.addRow("Name", self.name)
        self.username = QLineEdit(self.credential.get("username", ""))
        self.username.setPlaceholderText("DOMAIN\\user, user@domain or a Linux user")
        form.addRow("Username", self.username)
        self.password = QLineEdit(self.credential.get("password", ""))
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        attach_reveal_toggle(self.password, can_reveal=self._can_reveal,
                             tooltip="Show the password")
        form.addRow("Password", self.password)
        key_row = QHBoxLayout()
        self.key_file = QLineEdit(self.credential.get("key_file", ""))
        self.key_file.setPlaceholderText("Optional SSH private key")
        key_row.addWidget(self.key_file, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_key)
        key_row.addWidget(browse)
        form.addRow("Private key", key_row)
        self.passphrase = QLineEdit(self.credential.get("key_passphrase", ""))
        self.passphrase.setEchoMode(QLineEdit.EchoMode.Password)
        self.passphrase.setPlaceholderText("Only if the key has one")
        form.addRow("Key passphrase", self.passphrase)
        self.notes = QPlainTextEdit(self.credential.get("notes", ""))
        self.notes.setObjectName("notes_input")
        self.notes.setFixedHeight(70)
        self.notes.setPlaceholderText("Where it is used, who owns it, rotation schedule…")
        form.addRow("Notes", self.notes)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        set_variant(buttons.button(QDialogButtonBox.StandardButton.Save), "primary")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        fit_to_screen(self)

    def _can_reveal(self) -> bool:
        if self._authorised:
            return True
        if confirm_master_password(self, self.vault, "You are about to view a shared password."):
            self._authorised = True
            return True
        return False

    def _browse_key(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a private key", "",
                                              "Key files (*.ppk *.pem *.key);;All files (*)")
        if path:
            self.key_file.setText(path)

    def _accept(self):
        if not self.name.text().strip():
            QMessageBox.warning(self, "Shared credential", "Give the credential a name.")
            return
        if not self.username.text().strip():
            QMessageBox.warning(self, "Shared credential", "Enter the username it signs in as.")
            return
        self.accept()

    def result_data(self) -> dict:
        return {
            "name": self.name.text().strip(),
            "username": self.username.text().strip(),
            "password": self.password.text(),
            "key_file": self.key_file.text().strip(),
            "key_passphrase": self.passphrase.text(),
            "notes": self.notes.toPlainText().strip(),
        }


class CredentialsManagerDialog(QDialog):
    """List, add, edit and remove shared credentials."""

    def __init__(self, vault, parent=None):
        super().__init__(parent)
        self.vault = vault
        self.changed = False
        self.setWindowTitle("Shared Credentials")
        self.setWindowIcon(icons.icon("users"))
        self.setMinimumSize(620, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        intro = QLabel("Logins that several connections share. Pick one in a "
                       "connection's Sign-in section instead of typing the password again.")
        intro.setWordWrap(True)
        intro.setProperty("role", "hint")
        layout.addWidget(intro)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Username", "Used by", "Password changed"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.itemDoubleClicked.connect(lambda *_: self._edit())
        layout.addWidget(self.tree, 1)

        row = QHBoxLayout()
        self.btn_add = QPushButton("New credential…")
        self.btn_add.setIcon(icons.icon("plus"))
        set_variant(self.btn_add, "primary")
        self.btn_add.clicked.connect(self._add)
        self.btn_edit = QPushButton("Edit…")
        self.btn_edit.setIcon(icons.icon("edit"))
        self.btn_edit.clicked.connect(self._edit)
        self.btn_delete = QPushButton("Delete…")
        self.btn_delete.setIcon(icons.icon("trash", "#f87171"))
        set_variant(self.btn_delete, "ghost")
        self.btn_delete.clicked.connect(self._delete)
        for b in (self.btn_add, self.btn_edit, self.btn_delete):
            row.addWidget(b)
        row.addStretch()
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)

        self.tree.itemSelectionChanged.connect(self._sync)
        self._load()
        self.resize(760, 440)
        fit_to_screen(self)

    def _load(self):
        self.tree.clear()
        for cred in self.vault.credentials:
            changed = cred.get("updated_at")
            when = datetime.fromtimestamp(float(changed)).strftime("%Y-%m-%d") if changed else "—"
            used = usage_count(self.vault, cred.get("id"))
            item = QTreeWidgetItem([cred.get("name", ""), cred.get("username", ""),
                                    f"{used} connection{'s' if used != 1 else ''}", when])
            item.setIcon(0, icons.icon("key"))
            item.setData(0, Qt.ItemDataRole.UserRole, cred.get("id"))
            self.tree.addTopLevelItem(item)
        self._sync()

    def _selected_id(self):
        items = self.tree.selectedItems()
        return items[0].data(0, Qt.ItemDataRole.UserRole) if items else None

    def _sync(self):
        has = self._selected_id() is not None
        self.btn_edit.setEnabled(has)
        self.btn_delete.setEnabled(has)

    def _add(self):
        dialog = CredentialEditDialog(self.vault, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.vault.add_credential(dialog.result_data())
            self.changed = True
            self._load()

    def _edit(self):
        cred_id = self._selected_id()
        if not cred_id:
            return
        cred = self.vault.get_credential(cred_id)
        if cred is None:
            return
        dialog = CredentialEditDialog(self.vault, cred, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.vault.update_credential(cred_id, dialog.result_data())
            except VaultError as exc:
                QMessageBox.warning(self, "Shared credential", str(exc))
                return
            self.changed = True
            self._load()

    def _delete(self):
        cred_id = self._selected_id()
        cred = self.vault.get_credential(cred_id) if cred_id else None
        if cred is None:
            return
        used = usage_count(self.vault, cred_id)
        detail = (f"The {used} connection{'s' if used != 1 else ''} using it keep a copy of "
                  "the login, so nothing stops working." if used else
                  "No connection uses it.")
        if not confirm(self, "Delete shared credential",
                       f"Delete “{cred.get('name', '')}”?", "Delete",
                       danger=True, informative=detail):
            return
        self.vault.delete_credential(cred_id)
        self.changed = True
        self._load()
