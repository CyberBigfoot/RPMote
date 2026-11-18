"""
RPMote Azure / Microsoft 365 PowerShell Terminal.

A local PowerShell host that can sign in to a customer/tenant across the
Microsoft admin surface — Azure (Az), Exchange Online (mailboxes), Microsoft
Graph (users, groups, licensing), Teams, SharePoint (PnP) and Security &
Compliance — and drive it from a curated command palette. Built for MSP /
systems-admin work against tenants you administer.
"""

from __future__ import annotations

import codecs
import re
import shutil
import uuid

from PyQt6.QtCore import QProcess, QTimer, pyqtSignal, Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QComboBox,
    QSplitter,
)

from app.azure_commands_library import (
    get_azure_command_library,
    AZURE_SERVICES,
    get_disconnect_commands,
)
from app.azure_shell import AzureShellConfig, encode_command, host_arguments, marker, ps_quote


# Strip ANSI control sequences so PowerShell/cmdlet colour codes don't show as
# raw escape text in the simple read-only console.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[()][A-B0-2]")

_PLACEHOLDER_RE = re.compile(r"<[^<>\r\n]+>")
_SHUTDOWN_TIMEOUT_MS = 15000

from app import icons

class AzurePowerShellTerminal(QWidget):
    """Embedded local PowerShell console prepped for Microsoft 365 / Azure admin."""

    session_closed = pyqtSignal()
    connection_established = pyqtSignal()
    connection_lost = pyqtSignal(str)

    def __init__(self, connection_info: dict, parent=None):
        super().__init__(parent)
        self.connection_info = dict(connection_info or {})
        self.process: QProcess | None = None
        self._shell_exe = self._detect_shell()
        self._closing = False
        self._reconnecting = False
        self._connected = False
        self._busy = ""
        self._shell_ready = False
        self._shutdown_action = ""
        self._closed_emitted = False
        self._service_status: dict[str, bool] = {}
        self._config = AzureShellConfig(self.connection_info)
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setSingleShot(True)
        self._shutdown_timer.timeout.connect(self._force_shutdown)
        self._commands = get_azure_command_library()
        self._services = self._resolve_services()
        self._setup_ui()
        self._start_shell()

    # ── Configuration helpers ────────────────────────────────────────

    def _resolve_services(self) -> list[str]:
        """Return the ordered list of Microsoft services to manage."""
        return list(self._config.services)

    @staticmethod
    def _detect_shell() -> str | None:
        """Prefer pwsh, fallback to Windows PowerShell."""
        return shutil.which("pwsh") or shutil.which("powershell")

    @staticmethod
    def _ps_quote(value: str) -> str:
        """Single-quote a PowerShell string literal safely."""
        return ps_quote(value)

    def _sharepoint_admin_url(self) -> str:
        """Derive the SharePoint admin URL from the organization domain."""
        return self._config.sharepoint_url()

    # ── UI ────────────────────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        header = QWidget()
        header.setFixedHeight(40)
        h = QHBoxLayout(header)
        h.setContentsMargins(12, 0, 12, 0)

        self.status_indicator = QLabel("●")
        self.status_indicator.setStyleSheet("color:#f87171;font-size:10px;")
        h.addWidget(self.status_indicator)

        name = self.connection_info.get("name", "Azure PowerShell")
        service_labels = ", ".join(
            AZURE_SERVICES[s]["label"] for s in self._services if s in AZURE_SERVICES
        ) or "Azure"
        self.title_label = QLabel(f"  {name} — {service_labels}")
        self.title_label.setStyleSheet("font-weight:600;font-size:12px;")
        h.addWidget(self.title_label)
        h.addStretch()

        self.badge = QLabel("M365")
        self.badge.setStyleSheet(
            "color: #38bdf8; font-size: 11px; font-weight: 700;"
            "padding: 1px 8px; border-radius: 8px;"
            "background: rgba(56,189,248,0.12);"
            "border: 1px solid rgba(56,189,248,0.35);"
        )
        h.addWidget(self.badge)

        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setIcon(icons.icon("link", "#ffffff"))
        self.btn_connect.setFixedHeight(26)
        self.btn_connect.setStyleSheet("font-size:11px;padding:2px 10px;background-color:#2d7d46;")
        self.btn_connect.setToolTip("Sign in to the selected Microsoft services")
        self.btn_connect.clicked.connect(self.connect_services)
        h.addWidget(self.btn_connect)

        self.btn_commands = QPushButton("Commands")
        self.btn_commands.setIcon(icons.icon("bolt"))
        self.btn_commands.setFixedHeight(26)
        self.btn_commands.setStyleSheet("font-size:11px;padding:2px 10px;")
        self.btn_commands.setCheckable(True)
        self.btn_commands.setChecked(True)
        self.btn_commands.setToolTip("Show/hide the admin command palette")
        self.btn_commands.toggled.connect(self._toggle_palette)
        h.addWidget(self.btn_commands)

        self.btn_reconnect = QPushButton("Reconnect")
        self.btn_reconnect.setIcon(icons.icon("refresh"))
        self.btn_reconnect.setFixedHeight(26)
        self.btn_reconnect.setStyleSheet("font-size:11px;padding:2px 10px;")
        self.btn_reconnect.clicked.connect(self.reconnect)
        h.addWidget(self.btn_reconnect)

        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setIcon(icons.icon("x", "#ffffff"))
        self.btn_disconnect.setFixedHeight(26)
        self.btn_disconnect.setStyleSheet("font-size:11px;padding:2px 10px;background-color:#ef4444;")
        self.btn_disconnect.setToolTip("Sign out of every service and close the session")
        self.btn_disconnect.clicked.connect(self._disconnect)
        h.addWidget(self.btn_disconnect)
        layout.addWidget(header)
        self.status_text = QLabel("Starting PowerShell…")
        self.status_text.setWordWrap(True)
        self.status_text.setStyleSheet("color:#94a3b8;font-size:11px;padding:4px 12px;")
        layout.addWidget(self.status_text)

        # Split view: console output on the left, command palette on the right.
        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        console_pane = QWidget()
        console_layout = QVBoxLayout(console_pane)
        console_layout.setContentsMargins(0, 0, 0, 0)
        console_layout.setSpacing(0)

        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.output.setStyleSheet("font-family: 'Consolas'; font-size: 12px;")
        self.output.installEventFilter(self)
        self.output.viewport().installEventFilter(self)
        console_layout.addWidget(self.output, 1)

        self.splitter.addWidget(console_pane)
        self.splitter.addWidget(self._build_command_palette())
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([620, 380])
        layout.addWidget(self.splitter, 1)

        self.input_hint = QLabel(
            "Type a command and press Enter — or click a command on the right to load it here for review."
        )
        self.input_hint.setStyleSheet("color:#94a3b8;font-size:11px;padding:4px 8px;")
        layout.addWidget(self.input_hint)

        command_row = QHBoxLayout()
        self.command_input = QLineEdit()
        self.command_input.setPlaceholderText("Enter PowerShell command and press Enter")
        self.command_input.returnPressed.connect(self._run_user_command)
        command_row.addWidget(self.command_input, 1)

        self.btn_run = QPushButton("Run")
        self.btn_run.setFixedHeight(28)
        self.btn_run.clicked.connect(self._run_user_command)
        command_row.addWidget(self.btn_run)
        self.btn_reply = QPushButton("Send input")
        self.btn_reply.setToolTip("Send a response to a running PowerShell command's prompt")
        self.btn_reply.setEnabled(False)
        self.btn_reply.clicked.connect(self._send_prompt_reply)
        command_row.addWidget(self.btn_reply)
        layout.addLayout(command_row)

    def _build_command_palette(self) -> QWidget:
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        title = QLabel("Admin Command Palette")
        title.setStyleSheet("font-weight:700;font-size:12px;")
        v.addWidget(title)

        self.cmd_search = QLineEdit()
        self.cmd_search.setPlaceholderText("Search commands…")
        self.cmd_search.addAction(icons.icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.cmd_search.textChanged.connect(self._refresh_palette)
        v.addWidget(self.cmd_search)

        self.cmd_category = QComboBox()
        self.cmd_category.addItem("All Categories")
        for cat in sorted({c["category"] for c in self._commands}):
            self.cmd_category.addItem(cat)
        self.cmd_category.currentTextChanged.connect(self._refresh_palette)
        v.addWidget(self.cmd_category)

        self.cmd_list = QListWidget()
        self.cmd_list.setStyleSheet(
            "QListWidget{border:none;background:transparent;outline:0;}"
            "QListWidget::item{padding:8px;border-bottom:1px solid rgba(255,255,255,0.05);"
            "border-radius:6px;margin-bottom:3px;}"
            "QListWidget::item:selected{background:rgba(56,189,248,0.20);color:white;}"
            "QListWidget::item:hover{background:rgba(255,255,255,0.05);}"
        )
        self.cmd_list.itemClicked.connect(self._on_palette_item_clicked)
        self.cmd_list.itemDoubleClicked.connect(self._on_palette_item_run)
        v.addWidget(self.cmd_list, 1)

        self.palette_hint = QLabel("Select a command, edit its placeholders, then press Run.")
        self.palette_hint.setStyleSheet("color:#94a3b8;font-size:10px;")
        self.palette_hint.setWordWrap(True)
        v.addWidget(self.palette_hint)

        self._refresh_palette()
        return panel

    def _refresh_palette(self):
        self.cmd_list.clear()
        text = self.cmd_search.text().strip().lower() if hasattr(self, "cmd_search") else ""
        chosen = self.cmd_category.currentText() if hasattr(self, "cmd_category") else "All Categories"

        grouped: dict[str, list[dict]] = {}
        for cmd in self._commands:
            if chosen and chosen != "All Categories" and cmd["category"] != chosen:
                continue
            blob = f"{cmd['name']} {cmd['command']} {cmd.get('description', '')} {cmd['category']}".lower()
            if text and text not in blob:
                continue
            grouped.setdefault(cmd["category"], []).append(cmd)

        count = 0
        for category in sorted(grouped):
            header = QListWidgetItem(f"— {category} —")
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.cmd_list.addItem(header)
            for cmd in grouped[category]:
                item = QListWidgetItem(f"{cmd['name']}\n{cmd['command']}")
                item.setData(Qt.ItemDataRole.UserRole, cmd)
                desc = cmd.get("description", "")
                item.setToolTip(f"{desc}\n\n{cmd['command']}" if desc else cmd["command"])
                self.cmd_list.addItem(item)
                count += 1
        self.palette_hint.setText(
            f"Select, review, then press Run. {count} commands."
        )

    def _toggle_palette(self, visible: bool):
        widget = self.splitter.widget(1)
        if widget is not None:
            widget.setVisible(visible)

    def _on_palette_item_clicked(self, item):
        cmd = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(cmd, dict):
            return
        # Load (do not auto-run) so placeholders can be reviewed/edited first.
        self.command_input.setText(cmd.get("command", ""))
        self.command_input.setFocus()

    def _on_palette_item_run(self, item):
        cmd = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(cmd, dict):
            return
        self._on_palette_item_clicked(item)

    # ── Shell lifecycle ──────────────────────────────────────────────

    def _start_shell(self):
        self._shell_exe = self._detect_shell()
        if not self._shell_exe:
            self._append("PowerShell was not found (pwsh/powershell).")
            self._append("Install PowerShell 7+ or ensure powershell.exe is available in PATH.")
            self.btn_run.setEnabled(False)
            self.command_input.setEnabled(False)
            self.btn_connect.setEnabled(False)
            self._set_status("PowerShell was not found. Install PowerShell, then use Reconnect.", "#f87171")
            return
        self._launch_interactive_shell()

    def _launch_interactive_shell(self):
        self._token = uuid.uuid4().hex
        self._marker_re = re.compile(r"__RPMOTE_" + self._token + r"\|(.+?)__")
        self._stdout_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._stderr_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self._service_status.clear()
        self._connected = False
        self._shell_ready = False
        self._busy = ""
        self._shutdown_action = ""
        self._closed_emitted = False
        self._update_controls()
        self.process = QProcess(self)
        self.process.setProgram(self._shell_exe)
        self.process.setArguments(host_arguments(self._token))
        self.process.started.connect(self._on_started)
        self.process.readyReadStandardOutput.connect(self._on_stdout)
        self.process.readyReadStandardError.connect(self._on_stderr)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_process_error)
        self.process.start()

    def _on_started(self):
        self._append(f"Launching PowerShell: {self._shell_exe}")
        self._set_status("Waiting for PowerShell…", "#fbbf24")

    def _set_status(self, text: str, color: str):
        self.status_text.setText(text)
        self.status_indicator.setStyleSheet(f"color:{color};font-size:10px;")

    def _update_controls(self):
        available = self._shell_ready and not self._closing and not self._shutdown_action
        idle = available and not self._busy
        self.btn_run.setEnabled(idle)
        prompting = available and self._busy == "command"
        self.command_input.setEnabled(idle or prompting)
        self.command_input.setPlaceholderText("Reply to a PowerShell prompt, then send input" if prompting else "Enter PowerShell command and press Enter")
        self.btn_reply.setEnabled(prompting)
        self.btn_connect.setEnabled(idle)
        self.btn_disconnect.setEnabled(not self._closing and not self._shutdown_action)
        self.btn_reconnect.setEnabled(not self._closing and not self._shutdown_action)

    def _send_preflight(self):
        self._send_data(self._config.preflight_script())

    def connect_services(self):
        """Connect only services that have not already authenticated."""
        if self._busy or self._shutdown_action or self._closing:
            return
        if not self.process or not self._shell_ready:
            self._append("PowerShell is not ready. Use Reconnect to start it.")
            return
        if not self._services:
            self._append("No recognized Microsoft services are configured.")
            return
        self._busy = "connect"
        self._update_controls()
        self._set_status("Signing in to Microsoft services…", "#fbbf24")
        commands = self._connect_sequence()
        self._send_data("try { " + "; ".join(commands) + " } finally { " + marker(self._token, "CONNECT_DONE") + " }")

    def _connect_sequence(self) -> list[str]:
        return self._config.connect_commands(self._token, [s for s in self._services if not self._service_status.get(s)])

    def _disconnect_sequence(self) -> list[str]:
        return get_disconnect_commands()

    def disconnect_services(self):
        """Sign out without closing the PowerShell host."""
        if not self._shell_ready or self._busy or self._shutdown_action:
            return
        self._busy = "disconnect"
        self._update_controls()
        self._append("Signing out of Microsoft services…")
        self._send_data(
            "try { " + "; ".join(self._disconnect_sequence()) + " } finally { "
            "if ($global:rpmoteDisconnectFailed) { " + marker(self._token, "DISCONNECT_FAILED") + " } else { "
            "$global:rpmoteServices = @{}; " + marker(self._token, "DISCONNECT_DONE") + " } }"
        )

    def _handle_marker(self, event: str):
        if event == "READY":
            self._shell_ready = True
            if self._closing or self._shutdown_action:
                return
            self._update_controls()
            self._set_status("PowerShell ready. No services signed in yet.", "#94a3b8")
            self._send_preflight()
            self.connect_services()
        elif event.startswith("SERVICE|"):
            _, service, result = event.split("|", 2)
            if service in self._services:
                self._service_status[service] = result == "OK"
        elif event == "CONNECT_DONE":
            previous = self._connected
            self._busy = ""
            connected = [AZURE_SERVICES[s]["label"] for s in self._services if self._service_status.get(s)]
            self._connected = bool(connected)
            if len(connected) == len(self._services) and connected:
                self._set_status("Connected: " + ", ".join(connected), "#4ade80")
            elif connected:
                self._set_status("Partially connected: " + ", ".join(connected) + ". Review failures below.", "#fbbf24")
            else:
                self._set_status("Sign-in failed. Review the errors below, then retry Connect.", "#f87171")
                self.connection_lost.emit("Microsoft service sign-in failed")
            if self._connected and not previous:
                self.connection_established.emit()
            self._update_controls()
        elif event == "DISCONNECT_DONE":
            self._busy = ""
            self._connected = False
            self._service_status.clear()
            self._set_status("Services signed out. PowerShell remains open.", "#94a3b8")
            self.connection_lost.emit("Microsoft services signed out")
            self._update_controls()
        elif event == "DISCONNECT_FAILED":
            self._busy = ""
            self._set_status("Some services could not sign out. Review warnings and retry or close the shell.", "#fbbf24")
            self._update_controls()
        elif event.startswith("SIGNED_OUT|"):
            service = event.split("|", 1)[1]
            self._service_status[service] = False
            self._connected = any(self._service_status.values())
            remaining = [AZURE_SERVICES[s]["label"] for s, active in self._service_status.items() if active and s in AZURE_SERVICES]
            self._set_status("Connected: " + ", ".join(remaining) if remaining else "Services signed out. PowerShell remains open.", "#4ade80" if remaining else "#94a3b8")
            if not self._connected:
                self.connection_lost.emit("Microsoft services signed out")
        elif event == "COMMAND_DONE":
            self._busy = ""
            self.command_input.setEchoMode(QLineEdit.EchoMode.Normal)
            self._update_controls()
            self.command_input.setFocus()
        elif event == "PROMPT_SECURE":
            self.command_input.setEchoMode(QLineEdit.EchoMode.Password)

    # ── Output / input ───────────────────────────────────────────────

    def _append(self, text: str, *, newline: bool = True):
        clean = _ANSI_RE.sub("", text)
        secret = str(self.connection_info.get("azure_client_secret") or "")
        if secret:
            clean = clean.replace(secret, "[redacted]").replace(secret.replace("'", "''"), "[redacted]")
        self.output.moveCursor(QTextCursor.MoveOperation.End)
        self.output.insertPlainText(clean)
        if newline and not clean.endswith("\n"):
            self.output.insertPlainText("\n")
        self.output.moveCursor(QTextCursor.MoveOperation.End)

    def _consume_output(self, text: str, *, stderr: bool = False, final: bool = False):
        attribute = "_stderr_buffer" if stderr else "_stdout_buffer"
        buffer = getattr(self, attribute, "") + text
        lines = buffer.splitlines(keepends=True)
        remainder = ""
        for line in lines:
            if not final and not line.endswith(("\n", "\r")):
                remainder = line
                continue
            position = 0
            for match in self._marker_re.finditer(line):
                if match.start() > position:
                    self._append(line[position:match.start()], newline=False)
                self._handle_marker(match.group(1))
                position = match.end()
            tail = line[position:]
            if position == 0 or tail.strip():
                self._append(tail, newline=False)
        # Display prompts immediately, even when PowerShell has not written a
        # newline. Retain possible partial status markers and client secrets.
        if remainder and not final:
            prefix = "__RPMOTE_" + self._token + "|"
            marker_start = remainder.find(prefix)
            retained = len(remainder) - marker_start if marker_start >= 0 else 0
            secret = str(self.connection_info.get("azure_client_secret") or "")
            for sensitive in (prefix, secret, secret.replace("'", "''")):
                for length in range(1, min(len(sensitive), len(remainder)) + 1):
                    if remainder.endswith(sensitive[:length]):
                        retained = max(retained, length)
            flushed = remainder[:-retained] if retained else remainder
            if flushed:
                self._append(flushed, newline=False)
            remainder = remainder[-retained:] if retained else ""
        setattr(self, attribute, remainder)

    def _on_stdout(self):
        if self.process:
            data = bytes(self.process.readAllStandardOutput())
            self._consume_output(self._stdout_decoder.decode(data))

    def _on_stderr(self):
        if self.process:
            data = bytes(self.process.readAllStandardError())
            self._consume_output(self._stderr_decoder.decode(data), stderr=True)

    def _on_process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._shell_ready = False
            self._connected = False
            self._append("PowerShell could not start: " + self.process.errorString())
            self._set_status("PowerShell failed to start. Use Reconnect to retry.", "#f87171")
            self.connection_lost.emit("PowerShell failed to start")
            self._update_controls()

    def _on_finished(self, *_):
        self._shutdown_timer.stop()
        self._on_stdout()
        self._on_stderr()
        self._consume_output(self._stdout_decoder.decode(b"", final=True), final=True)
        self._consume_output(self._stderr_decoder.decode(b"", final=True), stderr=True, final=True)
        process = self.process
        self.process = None
        if process:
            process.deleteLater()
        action = self._shutdown_action
        self._shell_ready = False
        self._connected = False
        self._busy = ""
        self._service_status.clear()
        self._set_status("PowerShell session closed.", "#94a3b8")
        self.command_input.setEchoMode(QLineEdit.EchoMode.Normal)
        self._append("PowerShell session closed.")
        if action == "reconnect" and not self._closing:
            self._shutdown_action = ""
            self._reconnecting = False
            QTimer.singleShot(0, self._restart_if_open)
        elif action == "close":
            self._emit_closed()
        elif not self._closing:
            self._shutdown_action = ""
            self.connection_lost.emit("PowerShell exited; use Reconnect to restart")
        self._update_controls()

    def _restart_if_open(self):
        if not self._closing:
            self._start_shell()

    def has_open_session(self) -> bool:
        return self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning

    def _run_user_command(self):
        cmd = self.command_input.text().strip()
        if self._busy == "command":
            self._send_prompt_reply()
            return
        if not cmd or self._busy or self._closing or self._shutdown_action:
            return
        if _PLACEHOLDER_RE.search(cmd):
            self._append("Replace all <placeholders> before running this command.")
            return
        if not self.process or not self._shell_ready:
            self._append("PowerShell is not ready. Use Reconnect first.")
            return
        self._busy = "command"
        self._update_controls()
        # Parse user input inside a try so even syntax errors release the UI.
        service_event = ""
        disconnect_names = {
            "Disconnect Azure": ["azure"], "Disconnect Exchange Online": ["exchange", "compliance"],
            "Disconnect Microsoft Graph": ["graph"], "Disconnect Teams": ["teams"],
            "Disconnect SharePoint (PnP)": ["spo"], "Disconnect ALL services": list(AZURE_SERVICES),
        }
        for entry in self._commands:
            if entry["category"] == "Disconnect" and entry["command"] == cmd:
                service_event = "; ".join(marker(self._token, f"SIGNED_OUT|{s}") for s in disconnect_names.get(entry["name"], []))
                if entry["name"] == "Disconnect ALL services":
                    service_event = "if (-not $global:rpmoteDisconnectFailed) { " + service_event + " }"
                break
        script = (
            f"try {{ . ([ScriptBlock]::Create({ps_quote(cmd)})) | Out-Default; {service_event} }} "
            "catch { Write-Error -Message $_.Exception.Message -ErrorAction Continue } "
            f"finally {{ {marker(self._token, 'COMMAND_DONE')} }}"
        )
        if self._send_data(script):
            self._append("PS> " + cmd)
            self.command_input.clear()
        else:
            self._busy = ""
            self._update_controls()

    def _send_prompt_reply(self):
        if self._busy != "command" or not self._shell_ready or self._closing or self._shutdown_action:
            return
        if self.process and self.process.state() == QProcess.ProcessState.Running:
            # A running cmdlet reads prompt responses directly, before control
            # returns to the encoded-command dispatcher. Do not echo responses.
            response = self.command_input.text()
            self.process.write((response + "\n").encode("utf-8"))
            self.command_input.clear()
            self.command_input.setFocus()

    def _send_data(self, cmd: str) -> bool:
        if not self.process or self.process.state() != QProcess.ProcessState.Running:
            return False
        return self.process.write(encode_command(cmd)) >= 0

    def _emit_closed(self):
        if not self._closed_emitted:
            self._closed_emitted = True
            self.session_closed.emit()

    def _begin_shutdown(self, action: str):
        self._shutdown_action = action
        self._update_controls()
        self._set_status("Signing out and closing PowerShell…", "#fbbf24")
        self._append("Signing out of all services and closing PowerShell…")
        if not self.process or self.process.state() == QProcess.ProcessState.NotRunning:
            if self.process:
                self.process.deleteLater()
                self.process = None
            self._shell_ready = False
            self._busy = ""
            self._connected = False
            if action == "reconnect":
                self._shutdown_action = ""
                self._reconnecting = False
                self._start_shell()
            elif action == "close":
                self._emit_closed()
            return
        # EOF releases Read-Host and runs the host's finally block. An encoded
        # `exit` packet could otherwise be consumed as a prompt response.
        self.process.closeWriteChannel()
        self._shutdown_timer.start(_SHUTDOWN_TIMEOUT_MS)

    def _force_shutdown(self):
        if self.process and self.process.state() != QProcess.ProcessState.NotRunning:
            self._append("PowerShell did not finish sign-out within 15 seconds (a command or sign-in may still be waiting). Forcing the shell to close; sign-out could not be verified.")
            self.process.kill()

    def _disconnect(self):
        if self._shutdown_action or self._closing:
            return
        self._begin_shutdown("close")

    def reconnect(self):
        if self._shutdown_action or self._closing:
            return
        self._reconnecting = True
        self._begin_shutdown("reconnect")

    def eventFilter(self, obj, event):
        et = event.type()
        if obj in (self.output, self.output.viewport()):
            if et in (
                event.Type.MouseButtonPress,
                event.Type.MouseButtonDblClick,
                event.Type.FocusIn,
            ):
                self.command_input.setFocus()
                return False
            if et == event.Type.KeyPress:
                self.command_input.setFocus()
                try:
                    txt = event.text()
                    if txt and txt.isprintable():
                        self.command_input.insert(txt)
                        return True
                except Exception:
                    pass
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event):
        # Route ad-hoc typing to the command input so keyboard always works.
        try:
            txt = event.text()
        except Exception:
            txt = ""
        if txt and txt.isprintable():
            self.command_input.setFocus()
            self.command_input.insert(txt)
            return
        super().keyPressEvent(event)

    def cleanup(self):
        """Bounded, synchronous cleanup for tab close and application shutdown."""
        if self._closing:
            return
        self._closing = True
        self._reconnecting = False
        self._terminate_process()

    def suspend(self):
        """The local PowerShell host survives system sleep."""
        pass

    def resume(self):
        if not self._closing and (not self.process or self.process.state() == QProcess.ProcessState.NotRunning):
            self.reconnect()

    def _terminate_process(self):
        self._begin_shutdown("cleanup")
        process = self.process
        if not process:
            return
        if process.state() != QProcess.ProcessState.NotRunning:
            if not process.waitForFinished(_SHUTDOWN_TIMEOUT_MS):
                self._force_shutdown()
                process.waitForFinished(1000)
        self._shutdown_timer.stop()
