"""
RPMote Saved Passwords — review the credentials stored in the vault.

The vault being unlocked is enough to *use* a saved credential, but reading one
back out in plaintext is a different capability: it lets the secret leave the
machine. So every reveal here is gated behind re-entering the master password,
secrets re-mask themselves after a short window, and anything copied to the
clipboard is cleared again shortly afterwards.
"""

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import (
    QColor, QFont, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
)
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QDialog, QDialogButtonBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.secure_clipboard import clear_if_still, copy_secret
from app.vault import Vault

# How long a revealed secret stays on screen, and how long a copied secret
# stays on the clipboard, in milliseconds.
REVEAL_TIMEOUT_MS = 30_000
CLIPBOARD_CLEAR_MS = 30_000

MASK = "••••••••••"

# Vault fields that hold a secret, in the order they should be listed. A
# secret missing from here is a secret the user cannot review or revoke from
# this screen, so anything the connection dialog learns to store belongs in it.
SECRET_FIELDS = (
    ("password", "Password"),
    ("rdp_gateway_password", "RD Gateway password"),
    ("totp_secret", "Two-step verification secret"),
    ("azure_client_secret", "Azure client secret"),
)

PROTOCOL_LABELS = {
    "ssh": "SSH",
    "sftp": "SFTP",
    "ftp": "FTP",
    "rdp": "RDP",
    "idrac": "iDRAC",
    "synology": "NAS",
    "azure_ps": "Azure PowerShell",
}


def confirm_master_password(parent, vault: Vault, reason: str) -> bool:
    """Re-authenticate the user before showing stored secrets.

    Returns True once the master password is entered correctly. The user gets
    three attempts; cancelling at any point returns False.
    """
    for remaining in (3, 2, 1):
        suffix = "" if remaining == 3 else f"\n\n{remaining} attempt(s) left."
        password, ok = QInputDialog.getText(
            parent,
            "Confirm master password",
            f"{reason}\n\nEnter your master password to continue:{suffix}",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return False
        if vault.verify_password(password):
            return True

    QMessageBox.warning(
        parent,
        "Incorrect password",
        "That master password was not correct. Nothing has been revealed.",
    )
    return False


def glyph_icon(text: str, size: int = 16) -> QIcon:
    """Render a short glyph into an icon.

    QLineEdit side actions draw their icon and ignore their text, so the eye
    toggle needs a real QIcon rather than a caption.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        font = QFont()
        font.setPointSizeF(size * 0.72)
        painter.setFont(font)
        painter.setPen(QColor("#cbd5e1"))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, text)
    finally:
        painter.end()
    return QIcon(pixmap)


def attach_reveal_toggle(line_edit: QLineEdit, can_reveal=None, tooltip="Show/hide"):
    """Add an in-field eye button that toggles a password field's echo mode.

    ``can_reveal`` is called before the first reveal and must return True for
    the field to be unmasked; use it to require re-authentication. It is only
    consulted once — after a successful check the field can be toggled freely
    for the life of the widget.
    """
    from app import icons
    hidden_icon = icons.icon("eye")
    shown_icon = icons.icon("eye-off")
    action = line_edit.addAction(hidden_icon, QLineEdit.ActionPosition.TrailingPosition)
    action.setToolTip(tooltip)
    state = {"authorised": False}

    def toggle():
        if line_edit.echoMode() == QLineEdit.EchoMode.Normal:
            line_edit.setEchoMode(QLineEdit.EchoMode.Password)
            action.setIcon(hidden_icon)
            return
        if not state["authorised"]:
            if can_reveal is not None and not can_reveal():
                return
            state["authorised"] = True
        line_edit.setEchoMode(QLineEdit.EchoMode.Normal)
        action.setIcon(shown_icon)

    action.triggered.connect(toggle)
    return action


class _SecretRow:
    """One stored secret: which connection it belongs to and what it is."""

    __slots__ = ("connection", "field", "label", "value", "notice")

    def __init__(self, connection: dict, field: str, label: str, value: str):
        self.connection = connection
        self.field = field
        self.label = label
        self.value = value
        self.notice = ""

    @property
    def name(self) -> str:
        return str(self.connection.get("name", "") or "Unnamed")

    @property
    def tenant(self) -> str:
        return str(self.connection.get("tenant", "Default") or "Default")

    @property
    def protocol(self) -> str:
        raw = str(self.connection.get("protocol", "") or "").lower()
        return PROTOCOL_LABELS.get(raw, raw.upper() or "—")

    @property
    def target(self) -> str:
        host = str(self.connection.get("host", "") or "")
        port = self.connection.get("port", "")
        if host and port:
            return f"{host}:{port}"
        return host or "—"

    @property
    def username(self) -> str:
        if self.field == "azure_client_secret":
            return str(self.connection.get("azure_client_id", "") or "—")
        return str(self.connection.get("username", "") or "—")


    def matches(self, needle: str) -> bool:
        haystack = " ".join(
            (self.name, self.tenant, self.protocol, self.target, self.username, self.label)
        ).lower()
        return needle in haystack


class SavedPasswordsDialog(QDialog):
    """Table of every credential stored in the vault, revealed on demand."""

    # The action buttons sit in the first column so they stay reachable even
    # when a narrow window scrolls the table sideways; "Secret" takes whatever
    # width is left over.
    ACTION_COLUMN = 0
    SECRET_COLUMN = 7
    # Widths are chosen so the whole table still fits at the dialog's minimum
    # width without a horizontal scrollbar; "Secret" absorbs any extra room.
    COLUMNS = (
        ("", 84),
        ("Connection", 150),
        ("Tenant", 100),
        ("Protocol", 90),
        ("Host", 150),
        ("Username", 110),
        ("Type", 120),
        ("Secret", 0),
        ("Notice", 210),
    )
    NOTICE_COLUMN = 8

    def __init__(self, vault: Vault, parent=None, initial_filter: str = ""):
        super().__init__(parent)
        self.vault = vault
        self._initial_filter = (initial_filter or "").strip()
        self._rows: list[_SecretRow] = []
        self._visible_rows: list[_SecretRow] = []
        self._revealed: set[int] = set()          # indices into _visible_rows
        self._show_buttons: dict[int, QPushButton] = {}
        self._authorised = False
        self._copied_value = ""

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._hide_all)

        self.setWindowTitle("Saved Passwords")
        self.setModal(True)
        self.setMinimumSize(880, 480)
        self.resize(1060, 540)

        self._load_rows()
        self._setup_ui()
        if self._initial_filter:
            # setText triggers _populate via textChanged
            self.search_input.setText(self._initial_filter)
        else:
            self._populate()

    # ── Data ─────────────────────────────────────────────────────────

    def _load_rows(self):
        try:
            connections = self.vault.connections
        except Exception:
            connections = []
        for conn in connections:
            if not isinstance(conn, dict):
                continue
            for field, label in SECRET_FIELDS:
                value = conn.get(field, "")
                if isinstance(value, str) and value:
                    self._rows.append(_SecretRow(conn, field, label, value))
        # Shared credentials: one row each, saying how many connections use it.
        try:
            credentials = list(getattr(self.vault, "credentials", []) or [])
        except Exception:
            credentials = []
        for cred in credentials:
            if not isinstance(cred, dict) or not cred.get("password"):
                continue
            users = sum(1 for c in connections if isinstance(c, dict)
                        and str(c.get("credential_id", "")) == str(cred.get("id")))
            pseudo = {
                "name": str(cred.get("name", "") or "Shared credential"),
                "tenant": "Shared",
                "protocol": "",
                "host": f"used by {users} connection{'s' if users != 1 else ''}",
                "username": cred.get("username", ""),
                "password_changed_at": cred.get("updated_at"),
            }
            self._rows.append(_SecretRow(pseudo, "password", "Shared credential",
                                         str(cred.get("password", ""))))
        self._annotate_rows()

    #: A password older than this gets an age warning.
    STALE_DAYS = 365

    def _annotate_rows(self):
        """Flag passwords used in more than one place, and old ones."""
        import hashlib
        import time as _time
        groups: dict[str, list] = {}
        for row in self._rows:
            if row.field != "password":
                continue
            digest = hashlib.sha256(row.value.encode("utf-8", "replace")).hexdigest()
            groups.setdefault(digest, []).append(row)
        for rows in groups.values():
            if len(rows) < 2:
                continue
            tenants = {r.tenant for r in rows if r.tenant != "Shared"}
            for row in rows:
                others = len(rows) - 1
                text = f"Same password as {others} other{'s' if others != 1 else ''}"
                if len(tenants) > 1:
                    text += f" in {len(tenants)} tenants"
                row.notice = text
        now = _time.time()
        for row in self._rows:
            changed = row.connection.get("password_changed_at") if row.field == "password" else None
            try:
                age_days = (now - float(changed)) / 86400 if changed else None
            except (TypeError, ValueError):
                age_days = None
            if age_days is not None and age_days >= self.STALE_DAYS:
                years = age_days / 365
                age = f"Unchanged for {years:.1f} years" if years >= 1.5 else "Unchanged for over a year"
                row.notice = f"{row.notice} · {age}" if row.notice else age

    # ── UI ───────────────────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(10)

        heading = QLabel("Credentials stored in your vault")
        heading.setStyleSheet("font-size: 15px; font-weight: 600;")
        layout.addWidget(heading)

        subtitle = QLabel(
            "Secrets stay hidden until you reveal them, and re-hide themselves "
            f"after {REVEAL_TIMEOUT_MS // 1000} seconds. Double-click a row to "
            "show or hide it."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #94a3b8; font-size: 12px;")
        layout.addWidget(subtitle)

        # Filter + bulk controls
        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Filter by name, host, user or tenant…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self._populate)
        controls.addWidget(self.search_input, 1)

        self.reveal_all = QCheckBox("Reveal all")
        self.reveal_all.setToolTip("Show every secret in the list at once")
        self.reveal_all.toggled.connect(self._on_reveal_all)
        controls.addWidget(self.reveal_all)

        self.btn_hide = QPushButton("Hide all")
        self.btn_hide.clicked.connect(self._hide_all)
        controls.addWidget(self.btn_hide)
        layout.addLayout(controls)

        # Table
        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([title for title, _ in self.COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setColumnWidth(self.ACTION_COLUMN, self.COLUMNS[self.ACTION_COLUMN][1])
        self.table.itemDoubleClicked.connect(
            lambda item: self._toggle_row(item.row())
        )

        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        for index, (_, width) in enumerate(self.COLUMNS):
            if index == self.ACTION_COLUMN:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Fixed)
            elif index == self.SECRET_COLUMN:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Interactive)
                self.table.setColumnWidth(index, width)
        header.setMinimumSectionSize(70)
        layout.addWidget(self.table, 1)

        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #94a3b8; font-size: 12px;")
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        QShortcut(QKeySequence("Ctrl+F"), self, activated=self.search_input.setFocus)

    def _populate(self):
        """Rebuild the table for the current filter, re-masking everything."""
        needle = self.search_input.text().strip().lower()
        self._visible_rows = [r for r in self._rows if not needle or r.matches(needle)]
        self._revealed.clear()
        if self.reveal_all.isChecked():
            self.reveal_all.blockSignals(True)
            self.reveal_all.setChecked(False)
            self.reveal_all.blockSignals(False)

        self.table.setRowCount(len(self._visible_rows))
        self._show_buttons.clear()
        mono = QFont("MesloLGL Nerd Font Mono", 10)

        for index, row in enumerate(self._visible_rows):
            self.table.setCellWidget(index, self.ACTION_COLUMN, self._build_actions(index))
            for offset, text in enumerate(
                (row.name, row.tenant, row.protocol, row.target, row.username, row.label)
            ):
                self.table.setItem(index, offset + 1, QTableWidgetItem(text))

            secret_item = QTableWidgetItem(MASK)
            secret_item.setFont(mono)
            secret_item.setToolTip("Double-click the row to show or hide this secret")
            self.table.setItem(index, self.SECRET_COLUMN, secret_item)

            notice = QTableWidgetItem(row.notice)
            if row.notice:
                notice.setForeground(QColor("#fbbf24"))
                notice.setToolTip(
                    "A password used on several machines (or in several customers) "
                    "is one leak away from all of them. Consider a shared credential "
                    "(File › Shared Credentials) or rotating it.")
            self.table.setItem(index, self.NOTICE_COLUMN, notice)

        if not self._rows:
            self.status_label.setText("No saved passwords yet.")
        elif not self._visible_rows:
            self.status_label.setText("No credentials match that filter.")
        else:
            self.status_label.setText(
                f"{len(self._visible_rows)} of {len(self._rows)} stored credential(s)."
            )

    def _build_actions(self, index: int) -> QWidget:
        holder = QWidget()
        row_layout = QHBoxLayout(holder)
        row_layout.setContentsMargins(4, 2, 4, 2)
        row_layout.setSpacing(4)

        from app.widgets import icon_button
        show = icon_button("eye", "Show or hide this secret", size=28, icon_size=15)
        show.clicked.connect(lambda _=False, i=index: self._toggle_row(i))
        row_layout.addWidget(show)

        copy = icon_button("copy", f"Copy to the clipboard (cleared after "
                                   f"{CLIPBOARD_CLEAR_MS // 1000}s)", size=28, icon_size=15)
        copy.clicked.connect(lambda _=False, i=index: self._copy_row(i))
        row_layout.addWidget(copy)

        self._show_buttons[index] = show
        return holder

    # ── Reveal / hide ────────────────────────────────────────────────

    def _authorise(self, reason: str) -> bool:
        """Ask for the master password once per dialog session."""
        if self._authorised:
            return True
        if confirm_master_password(self, self.vault, reason):
            self._authorised = True
            return True
        return False

    def _toggle_row(self, index: int):
        if not 0 <= index < len(self._visible_rows):
            return
        if index in self._revealed:
            self._set_row_revealed(index, False)
            self._sync_after_change()
            return
        if not self._authorise("You are about to view a saved password."):
            return
        self._set_row_revealed(index, True)
        self._sync_after_change()

    def _set_row_revealed(self, index: int, revealed: bool):
        item = self.table.item(index, self.SECRET_COLUMN)
        if item is None:
            return
        row = self._visible_rows[index]
        item.setText(row.value if revealed else MASK)
        button = self._show_buttons.get(index)
        if button is not None:
            from app import icons
            button.setIcon(icons.icon("eye-off" if revealed else "eye"))
        if revealed:
            self._revealed.add(index)
        else:
            self._revealed.discard(index)

    def _on_reveal_all(self, checked: bool):
        if checked:
            if not self._visible_rows:
                self.reveal_all.setChecked(False)
                return
            if not self._authorise("You are about to view every saved password."):
                self.reveal_all.blockSignals(True)
                self.reveal_all.setChecked(False)
                self.reveal_all.blockSignals(False)
                return
            for index in range(len(self._visible_rows)):
                self._set_row_revealed(index, True)
        else:
            for index in range(len(self._visible_rows)):
                self._set_row_revealed(index, False)
        self._sync_after_change()

    def _sync_after_change(self):
        """Restart the auto-hide countdown and update the status line."""
        if self._revealed:
            self._hide_timer.start(REVEAL_TIMEOUT_MS)
            self.status_label.setText(
                f"{len(self._revealed)} secret(s) visible — hiding again in "
                f"{REVEAL_TIMEOUT_MS // 1000}s."
            )
        else:
            self._hide_timer.stop()
            self.status_label.setText(
                f"{len(self._visible_rows)} of {len(self._rows)} stored credential(s)."
            )

    def _hide_all(self):
        for index in list(self._revealed):
            self._set_row_revealed(index, False)
        if self.reveal_all.isChecked():
            self.reveal_all.blockSignals(True)
            self.reveal_all.setChecked(False)
            self.reveal_all.blockSignals(False)
        self._sync_after_change()

    # ── Clipboard ────────────────────────────────────────────────────

    def _copy_row(self, index: int):
        if not 0 <= index < len(self._visible_rows):
            return
        if not self._authorise("You are about to copy a saved password."):
            return
        value = self._visible_rows[index].value
        if not copy_secret(value, CLIPBOARD_CLEAR_MS):
            return
        self._copied_value = value
        self.status_label.setText(
            f"Copied — the clipboard clears in {CLIPBOARD_CLEAR_MS // 1000}s."
        )

    @staticmethod
    def _clear_clipboard(value: str):
        """Clear the clipboard, but only if it still holds what we put there."""
        clear_if_still(value)

    # ── Lifecycle ────────────────────────────────────────────────────

    def closeEvent(self, event):
        self._hide_all()
        if self._copied_value:
            self._clear_clipboard(self._copied_value)
            self._copied_value = ""
        super().closeEvent(event)

    def done(self, result):
        self._hide_all()
        super().done(result)
