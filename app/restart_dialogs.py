"""
RPMote Restart UI — nominate a restart server, then confirm before firing.

Two dialogs, deliberately separate:

  RestartServerDialog   set-up.  Which machine carries out restarts for this
                        tenant (or this one connection), over what transport,
                        running what command.
  RestartConfirmDialog  the last thing between a right-click and a production
                        reboot.  It names the target, the server the command
                        travels through, and the exact command that will run.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout,
)

from app.restart import (
    COMMAND_PRESETS, DEFAULT_COMMANDS, PLACEHOLDERS, REBOOT_METHODS,
    TRANSPORTS, TRANSPORT_LABELS, RestartError, needs_sudo, normalize_config,
    render_command,
)

_MONO = "font-family: 'Cascadia Mono', 'Consolas', monospace; font-size: 12px;"
_HINT = "color: #9aa0b4; font-size: 11px;"
_WARN = "color: #fbbf24; font-size: 11px;"


def _conn_label(conn: dict) -> str:
    name = str(conn.get("name", "") or "").rstrip().rstrip("●• ")
    host = str(conn.get("host", "") or "")
    tenant = str(conn.get("tenant", "Default") or "Default")
    head = name or host or "(unnamed)"
    return f"{head}  —  {host or 'no host'}  ·  {tenant}" if host else f"{head}  ·  {tenant}"

from app.widgets import set_variant

class RestartServerDialog(QDialog):
    """Choose the machine that performs restarts, and the command it runs."""

    def __init__(self, scope_kind: str, scope_key: str, scope_label: str,
                 connections: list, config: dict | None = None,
                 preferred_tenant: str | None = None, parent=None):
        super().__init__(parent)
        self.scope_kind = scope_kind        # "tenant" | "connection"
        self.scope_key = scope_key
        self._connections = [c for c in (connections or []) if isinstance(c, dict)]
        self.result_config: dict | None = None
        self.removed = False

        self.setWindowTitle("Restart Server")
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setSpacing(11)

        heading = QLabel(f"<b>Restart server for {scope_label}</b>")
        root.addWidget(heading)

        blurb = QLabel(
            "RPMote does not restart machines itself.  Pick a server you already "
            "have a connection to — one with the rights and the network path to "
            "do it — and RPMote will hand it the command below."
        )
        blurb.setWordWrap(True)
        blurb.setStyleSheet(_HINT)
        root.addWidget(blurb)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setSpacing(9)

        self.server_combo = QComboBox()
        self._populate_servers(preferred_tenant)
        form.addRow("Restart server:", self.server_combo)

        self.transport_combo = QComboBox()
        for key in TRANSPORTS:
            self.transport_combo.addItem(TRANSPORT_LABELS[key], key)
        self.transport_combo.currentIndexChanged.connect(self._on_transport_change)
        form.addRow("Send via:", self.transport_combo)

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("— pick a starting point —", None)
        for label, transport, template in COMMAND_PRESETS:
            self.preset_combo.addItem(label, (transport, template))
        self.preset_combo.currentIndexChanged.connect(self._on_preset_change)
        form.addRow("Preset:", self.preset_combo)

        root.addLayout(form)

        root.addWidget(QLabel("Command to run:"))
        self.command_edit = QPlainTextEdit()
        self.command_edit.setStyleSheet(_MONO)
        self.command_edit.setFixedHeight(76)
        self.command_edit.textChanged.connect(self._refresh_preview)
        root.addWidget(self.command_edit)

        tokens = QLabel("Placeholders:  " + "   ".join(
            f"<code>{token}</code> {meaning}" for token, meaning in PLACEHOLDERS))
        tokens.setWordWrap(True)
        tokens.setStyleSheet(_HINT)
        root.addWidget(tokens)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color: #2b2f42;")
        root.addWidget(line)

        root.addWidget(QLabel("Preview — what actually runs:"))
        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview.setStyleSheet(
            _MONO + "background: #14172a; border: 1px solid #2b2f42;"
            "border-radius: 6px; padding: 8px; color: #d6d9e6;")
        root.addWidget(self.preview)

        self.enabled_check = QCheckBox("Restarts are enabled for this scope")
        self.enabled_check.setChecked(True)
        root.addWidget(self.enabled_check)

        buttons = QDialogButtonBox()
        self.btn_save = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self.btn_remove = buttons.addButton("Remove",
                                           QDialogButtonBox.ButtonRole.DestructiveRole)
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        set_variant(self.btn_save, "primary")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.btn_remove.clicked.connect(self._remove)
        self.btn_remove.setEnabled(bool(config))
        root.addWidget(buttons)

        self._load(config)

    # ── Population ────────────────────────────────────────────────

    def _populate_servers(self, preferred_tenant: str | None):
        """List every connection, with the relevant tenant's own machines first.

        Anything can be the restart server — plenty of MSP estates run one jump
        box that reaches several customers — but the common case is a machine
        inside the same tenant, so those sort to the top.
        """
        def sort_key(conn):
            tenant = str(conn.get("tenant", "Default") or "Default")
            same = 0 if (preferred_tenant and tenant == preferred_tenant) else 1
            return (same, tenant.lower(), str(conn.get("name", "")).lower())

        self.server_combo.clear()
        for conn in sorted(self._connections, key=sort_key):
            cid = str(conn.get("id", "") or "")
            if cid:
                self.server_combo.addItem(_conn_label(conn), cid)
        if self.server_combo.count() == 0:
            self.server_combo.addItem("No connections available", None)
            self.server_combo.setEnabled(False)

    def _load(self, config: dict | None):
        cfg = normalize_config(config) if config else None
        if cfg:
            index = self.server_combo.findData(cfg["conn_id"])
            if index >= 0:
                self.server_combo.setCurrentIndex(index)
            t_index = self.transport_combo.findData(cfg["transport"])
            if t_index >= 0:
                self.transport_combo.setCurrentIndex(t_index)
            self.command_edit.setPlainText(cfg["command"])
            self.enabled_check.setChecked(cfg["enabled"])
        else:
            self.command_edit.setPlainText(DEFAULT_COMMANDS["ssh"])
        self._refresh_preview()

    # ── Reactions ─────────────────────────────────────────────────

    def _on_transport_change(self):
        transport = self.transport_combo.currentData()
        current = self.command_edit.toPlainText().strip()
        # Only overwrite a command the user has not touched, so switching
        # transport to compare them never eats a hand-written command.
        if current in ("", *DEFAULT_COMMANDS.values(),
                       *(t for _l, _t, t in COMMAND_PRESETS)):
            self.command_edit.setPlainText(DEFAULT_COMMANDS.get(transport, current))
        self._refresh_preview()

    def _on_preset_change(self):
        data = self.preset_combo.currentData()
        if not data:
            return
        transport, template = data
        index = self.transport_combo.findData(transport)
        if index >= 0:
            self.transport_combo.blockSignals(True)
            self.transport_combo.setCurrentIndex(index)
            self.transport_combo.blockSignals(False)
        self.command_edit.setPlainText(template)
        self._refresh_preview()

    def _sample_target(self) -> dict:
        """The connection the preview is rendered against."""
        if self.scope_kind == "connection":
            for conn in self._connections:
                if str(conn.get("id", "")) == self.scope_key:
                    return conn
        for conn in self._connections:
            if str(conn.get("tenant", "Default") or "Default") == self.scope_key:
                return conn
        return {"host": "SERVER01", "name": "SERVER01", "port": "",
                "username": "", "tenant": self.scope_key}

    def _server_conn(self) -> dict:
        cid = self.server_combo.currentData()
        for conn in self._connections:
            if str(conn.get("id", "")) == str(cid):
                return conn
        return {}

    def _refresh_preview(self):
        target = self._sample_target()
        server = self._server_conn()
        try:
            rendered = render_command(self.command_edit.toPlainText(), target, server)
        except RestartError as exc:
            self.preview.setStyleSheet(
                _MONO + "background: #2a1620; border: 1px solid #7f1d1d;"
                "border-radius: 6px; padding: 8px; color: #fca5a5;")
            self.preview.setText(str(exc))
            return
        self.preview.setStyleSheet(
            _MONO + "background: #14172a; border: 1px solid #2b2f42;"
            "border-radius: 6px; padding: 8px; color: #d6d9e6;")
        name = str(target.get("name", "") or target.get("host", "") or "the target")
        self.preview.setText(rendered or "(empty command)")
        self.preview.setToolTip(f"Rendered for “{name}”")

    # ── Outcome ───────────────────────────────────────────────────

    def _save(self):
        cid = self.server_combo.currentData()
        if not cid:
            QMessageBox.warning(self, "Restart Server",
                                "Pick the connection that should carry out restarts.")
            return
        command = self.command_edit.toPlainText().strip()
        if not command:
            QMessageBox.warning(self, "Restart Server",
                                "Enter the command the restart server should run.")
            return
        try:
            render_command(command, self._sample_target(), self._server_conn())
        except RestartError as exc:
            QMessageBox.warning(self, "Restart Server", str(exc))
            return
        self.result_config = {
            "conn_id": str(cid),
            "transport": self.transport_combo.currentData() or "ssh",
            "command": command,
            "enabled": self.enabled_check.isChecked(),
        }
        self.accept()

    def _remove(self):
        confirm = QMessageBox.question(
            self, "Remove Restart Server",
            "Remove this restart-server setup?\n\n"
            "Restarts will stop being offered for it until you configure "
            "another one.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirm == QMessageBox.StandardButton.Yes:
            self.removed = True
            self.result_config = None
            self.accept()


class RestartConfirmDialog(QDialog):
    """Name the target, the route, and the command — then and only then, fire.

    For a direct restart this is also where the reboot method is chosen and
    where sudo gets its password, so the whole thing is one stop rather than a
    setup wizard the user never asked for.
    """

    def __init__(self, plan, saved_password: str = "", parent=None):
        super().__init__(parent)
        self.plan = plan
        self._saved_password = saved_password or ""
        self.sudo_password = ""
        self.remember = False

        self.setWindowTitle("Restart Connection")
        self.setMinimumWidth(580)

        root = QVBoxLayout(self)
        root.setSpacing(11)

        root.addWidget(QLabel(f"<h3>Restart “{plan.target_name}”?</h3>"))

        detail = QLabel(
            f"<b>{plan.target_name}</b> ({plan.target_host or 'no host'}) will be "
            f"told to reboot.<br>Anyone signed in loses their session."
        )
        detail.setWordWrap(True)
        root.addWidget(detail)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.addRow("Target:", QLabel(f"{plan.target_name}  ·  "
                                      f"{plan.target_host or 'no host'}"))
        if plan.direct:
            form.addRow("Through:", QLabel("the machine itself — direct over SSH"))
        else:
            form.addRow("Through:", QLabel(f"{plan.server_name}  ·  "
                                           f"{plan.server_host or 'no host'}"))
            form.addRow("Transport:", QLabel(
                TRANSPORT_LABELS.get(plan.transport, plan.transport)))

        # A direct restart has to guess the OS, so let the user correct it.
        self.method_combo = None
        if plan.direct:
            self.method_combo = QComboBox()
            for label, template in REBOOT_METHODS:
                self.method_combo.addItem(label, template)
            index = next((i for i, (_l, t) in enumerate(REBOOT_METHODS)
                          if t == plan.command), -1)
            if index >= 0:
                self.method_combo.setCurrentIndex(index)
            self.method_combo.currentIndexChanged.connect(self._on_method_change)
            form.addRow("Method:", self.method_combo)
        root.addLayout(form)

        root.addWidget(QLabel("Command:"))
        self.command_edit = QPlainTextEdit(plan.command)
        self.command_edit.setStyleSheet(
            _MONO + "background: #14172a; border: 1px solid #2b2f42;"
            "border-radius: 6px; padding: 6px; color: #d6d9e6;")
        self.command_edit.setFixedHeight(58)
        self.command_edit.setReadOnly(not plan.direct)
        self.command_edit.textChanged.connect(self._on_command_change)
        root.addWidget(self.command_edit)

        # ── sudo ──────────────────────────────────────────────────
        self.pw_input = None
        self.chk_saved = None
        self.chk_remember = None
        self._build_sudo_row(root)

        note = QLabel(
            "Using the restart server configured for this tenant."
            if plan.scope == "tenant" else
            "Using the restart server set for this connection."
            if plan.scope == "connection" else
            "No restart server needed — RPMote signs in to the machine and "
            "reboots it with the credentials you already saved."
        )
        note.setWordWrap(True)
        note.setStyleSheet(_HINT)
        root.addWidget(note)

        buttons = QDialogButtonBox()
        self.btn_restart = buttons.addButton("Restart",
                                             QDialogButtonBox.ButtonRole.AcceptRole)
        set_variant(self.btn_restart, "danger")
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        # Cancel is the safe default, so it is the one Enter lands on.
        self.btn_restart.setAutoDefault(False)
        self.btn_restart.setDefault(False)

        self._sync_sudo()

    # ── sudo row ──────────────────────────────────────────────────

    def _build_sudo_row(self, root):
        box = QFrame()
        box.setObjectName("sudoBox")
        box.setStyleSheet(
            "#sudoBox { background: #171a2b; border: 1px solid #2b2f42;"
            " border-radius: 8px; }")
        inner = QVBoxLayout(box)
        inner.setSpacing(6)

        user = str(self.plan.server.get("username", "") or "") or "the account"
        host = self.plan.server_host or "the machine"
        heading = QLabel(f"<b>sudo password</b> for {user}@{host}")
        heading.setWordWrap(True)
        inner.addWidget(heading)

        row = QFormLayout()
        row.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.pw_input = QLineEdit()
        self.pw_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw_input.setPlaceholderText("sudo password")
        self.pw_input.setMinimumWidth(260)
        # Deliberately not wired to returnPressed: Cancel is this dialog's
        # default button, and Enter in a password field must not sidestep that.
        row.addRow("Password:", self.pw_input)
        inner.addLayout(row)

        self.chk_saved = QCheckBox("Use this connection's saved password")
        self.chk_saved.setEnabled(bool(self._saved_password))
        self.chk_saved.setChecked(bool(self._saved_password))
        self.chk_saved.toggled.connect(self._sync_sudo)
        inner.addWidget(self.chk_saved)

        self.chk_remember = QCheckBox("Remember it until RPMote is closed")
        inner.addWidget(self.chk_remember)

        hint = QLabel(
            "Held in memory for this session only — it is never written to "
            "your vault, and it is masked out of any output shown back to you."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(_HINT)
        inner.addWidget(hint)

        self.sudo_box = box
        root.addWidget(box)

    def _sync_sudo(self):
        """Show the password row only when the command will actually need it."""
        elevated = needs_sudo(self.command_edit.toPlainText())
        self.sudo_box.setVisible(elevated)
        if not elevated:
            return
        using_saved = self.chk_saved.isChecked() and bool(self._saved_password)
        self.pw_input.setEnabled(not using_saved)
        self.pw_input.setPlaceholderText(
            "using the connection's saved password" if using_saved
            else "sudo password")
        if using_saved:
            self.pw_input.clear()

    def _on_method_change(self):
        template = self.method_combo.currentData()
        if template:
            self.command_edit.setPlainText(
                render_command(template, self.plan.target, self.plan.server))

    def _on_command_change(self):
        self._sync_sudo()

    # ── Outcome ───────────────────────────────────────────────────

    def _accept(self):
        command = self.command_edit.toPlainText().strip()
        if not command:
            QMessageBox.warning(self, "Restart",
                                "Enter the command that should reboot it.")
            return
        self.plan.command = command

        if needs_sudo(command):
            if self.chk_saved.isChecked() and self._saved_password:
                self.sudo_password = self._saved_password
            else:
                self.sudo_password = self.pw_input.text()
            if not self.sudo_password:
                QMessageBox.warning(
                    self, "Restart",
                    "This command runs under sudo, so it needs a password.\n\n"
                    "Enter one, tick “Use this connection's saved password”, or "
                    "choose a method that does not use sudo.")
                return
            self.plan.sudo_password = self.sudo_password
            self.remember = self.chk_remember.isChecked()
        self.accept()


class RestartManagerDialog(QDialog):
    """Everything configured, in one list, so nothing is set up and forgotten."""

    def __init__(self, servers: dict, overrides: dict, connections: list,
                 parent=None):
        super().__init__(parent)
        self.servers = dict(servers or {})
        self.overrides = dict(overrides or {})
        self._connections = [c for c in (connections or []) if isinstance(c, dict)]
        self.changed = False

        self.setWindowTitle("Restart Servers")
        self.setMinimumSize(620, 420)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        blurb = QLabel(
            "Each tenant restarts through a server you nominate.  A connection "
            "can override its tenant when it needs a different route."
        )
        blurb.setWordWrap(True)
        blurb.setStyleSheet(_HINT)
        root.addWidget(blurb)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda *_: self._edit())
        root.addWidget(self.list, 1)

        row = QHBoxLayout()
        self.btn_edit = QPushButton("Configure…")
        self.btn_edit.clicked.connect(self._edit)
        row.addWidget(self.btn_edit)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        root.addLayout(row)

        self._reload()

    def _tenants(self) -> list:
        return sorted({str(c.get("tenant", "Default") or "Default")
                       for c in self._connections})

    def _conn_name(self, conn_id: str) -> str:
        for conn in self._connections:
            if str(conn.get("id", "")) == str(conn_id):
                return (str(conn.get("name", "") or "").rstrip().rstrip("●• ")
                        or str(conn.get("host", "") or conn_id))
        return "(missing connection)"

    def _reload(self):
        self.list.clear()
        for tenant in self._tenants():
            cfg = self.servers.get(tenant)
            if cfg:
                state = "" if cfg.get("enabled", True) else "  (disabled)"
                text = (f"{tenant}   →   {self._conn_name(cfg['conn_id'])}   "
                        f"[{cfg.get('transport', 'ssh')}]{state}")
            else:
                text = f"{tenant}   →   not configured"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, ("tenant", tenant))
            if not cfg:
                item.setForeground(Qt.GlobalColor.gray)
            self.list.addItem(item)

        for cid, cfg in sorted(self.overrides.items()):
            text = (f"    ↳ {self._conn_name(cid)}   →   "
                    f"{self._conn_name(cfg['conn_id'])}   "
                    f"[{cfg.get('transport', 'ssh')}]   (override)")
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, ("connection", cid))
            self.list.addItem(item)

    def _edit(self):
        item = self.list.currentItem()
        if item is None:
            return
        kind, key = item.data(Qt.ItemDataRole.UserRole)
        store = self.servers if kind == "tenant" else self.overrides
        label = key if kind == "tenant" else f"“{self._conn_name(key)}”"
        tenant = key if kind == "tenant" else next(
            (str(c.get("tenant", "Default") or "Default") for c in self._connections
             if str(c.get("id", "")) == key), None)

        dialog = RestartServerDialog(kind, key, label, self._connections,
                                     store.get(key), preferred_tenant=tenant,
                                     parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.removed:
            store.pop(key, None)
        elif dialog.result_config:
            store[key] = dialog.result_config
        self.changed = True
        self._reload()
