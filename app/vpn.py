"""
Connect VPN first — per-tenant "bring the tunnel up, then open the machine".

Plenty of customer networks are only reachable over their VPN. Choose a tunnel
config, enter PPTP/L2TP details, use a Windows profile or configure a command
once per tenant, and opening any of that tenant's machines does the rest:

1. if the machine already answers, it just opens — nothing is started;
2. otherwise the VPN connects, and RPMote waits (with a Cancel button)
   until the machine answers on its port;
3. then the connection opens. If it never answers you can open it anyway.

A VPN that RPMote brought up belongs to the sessions opened while it was up.
When the last of them closes (or RPMote quits), RPMote disconnects it, unless
the tenant is set to leave it connected. A VPN that was already up before
RPMote touched it is never taken down.
"""

from __future__ import annotations

import contextlib
import html
import logging
import subprocess
import threading
import time
import uuid
from pathlib import Path

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QFileDialog, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)

from app import icons
from app.reachability import connection_host, connection_port, tcp_probe
from app.widgets import fit_to_screen, set_variant
from app import vpn_connections

SETTINGS_KEY = "tenant_vpn"
_LOG = logging.getLogger("rpmote.vpn")

#: (label, command template). {name} / {config} are filled from the fields.
PRESETS = (
    ("Windows VPN profile (rasdial)", 'rasdial "{name}"', 'rasdial "{name}" /disconnect'),
    ("OpenVPN GUI", '"C:\\Program Files\\OpenVPN\\bin\\openvpn-gui.exe" --command connect "{name}"',
     '"C:\\Program Files\\OpenVPN\\bin\\openvpn-gui.exe" --command disconnect "{name}"'),
    ("WireGuard tunnel (needs admin)", '"C:\\Program Files\\WireGuard\\wireguard.exe" /installtunnelservice "{name}"',
     '"C:\\Program Files\\WireGuard\\wireguard.exe" /uninstalltunnelservice "{name}"'),
    ("Custom command", "", ""),
)


def normalize(config) -> dict:
    config = dict(config or {})
    try:
        wait = int(config.get("wait_seconds", 45))
    except (TypeError, ValueError):
        wait = 45
    return {
        "enabled": bool(config.get("enabled", True)),
        "command": str(config.get("command", "") or "").strip(),
        "disconnect_command": str(config.get("disconnect_command", "") or "").strip(),
        # Disconnecting is the default; this is the way out of it.
        "keep_connected": bool(config.get("keep_connected", False)),
        "wait_seconds": max(5, min(300, wait)),
        "skip_if_reachable": bool(config.get("skip_if_reachable", True)),
        "kind": str(config.get("kind", "custom") or "custom"),
        "profile_name": str(config.get("profile_name", "") or "").strip(),
        "profile_id": str(config.get("profile_id", "") or ""),
        "config_file": str(config.get("config_file", "") or "").strip(),
        "server": str(config.get("server", "") or "").strip(),
        "username": str(config.get("username", "") or "").strip(),
        "domain": str(config.get("domain", "") or "").strip(),
        "password": str(config.get("password", "") or ""),
        "psk": str(config.get("psk", "") or ""),
        "authentication": (config.get("authentication") if config.get("authentication")
                           in {"MSChapv2", "Pap", "Chap"} else "MSChapv2"),
        "encryption": (config.get("encryption") if config.get("encryption")
                       in {"Required", "Optional", "NoEncryption", "Maximum"} else "Required"),
        "split_tunneling": bool(config.get("split_tunneling", False)),
    }


def run_command(command: str):
    """Start *command* without a console window; returns the Popen or raises."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(command, shell=True, creationflags=flags,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def connect_vpn(config: dict):
    """A custom command's Popen, or whether the tunnel was brought up (False: already up)."""
    if config["kind"] == "custom":
        return run_command(config["command"])
    return vpn_connections.connect(config)


def disconnect_vpn(config: dict):
    if config["kind"] == "custom":
        if config["disconnect_command"]:
            return run_command(config["disconnect_command"])
        return None
    return vpn_connections.disconnect(config)


def can_disconnect(config: dict) -> bool:
    return config["kind"] != "custom" or bool(config["disconnect_command"])


#: The settings that pick which VPN this is and how it signs in.
_IDENTITY = ("kind", "command", "disconnect_command", "profile_name", "profile_id", "config_file",
             "server", "username", "domain", "password", "psk", "authentication", "encryption",
             "split_tunneling")


def same_vpn(a: dict, b: dict) -> bool:
    return all(a.get(key) == b.get(key) for key in _IDENTITY)


def vpn_identity(config: dict) -> tuple:
    """The tunnel a config brings up, as Windows knows it: two tenants set to
    the same profile or tunnel file share one VPN."""
    kind = config.get("kind")
    if kind == "custom":
        return ("custom", str(config.get("command", "")).strip())
    if kind in vpn_connections.FILE_KINDS:
        return (kind, Path(config.get("config_file", "")).stem.casefold())
    return ("ras", vpn_connections.adapter_alias(config).casefold())


def _disconnect_and_wait(config: dict) -> str:
    """Disconnect on a worker thread; returns the error to show, or ""."""
    try:
        process = disconnect_vpn(config)
        if isinstance(process, subprocess.Popen):
            # Wait, so a reconnect cannot overtake it; a stuck command is left be.
            with contextlib.suppress(subprocess.TimeoutExpired):
                if (code := process.wait(timeout=30)):
                    return f"The disconnect command failed (exit code {code})."
    except OSError as exc:
        return str(exc)
    except Exception:
        return "The VPN client could not disconnect. Disconnect it from the VPN client."
    return ""


def _start_worker(config: dict, relay, lock=None, tunnel=None):
    """Connect on a worker; *relay* hears launched(error, started) and a custom command's exit.

    *lock* keeps it from overlapping a disconnect of the same VPN. *tunnel*
    learns, while the lock is still held, whether this call brought it up.
    """
    def launch():
        error, started, process = "", False, None
        with lock or contextlib.nullcontext():
            try:
                result = connect_vpn(config)
                if isinstance(result, subprocess.Popen):
                    process, started = result, True
                else:
                    started = result is not False
            except OSError as exc:
                error = str(exc)
            except Exception:
                error = "The VPN client could not connect. Check its settings and try again."
            if tunnel is not None and started and not error:
                tunnel.owned = True
        try:
            relay.launched.emit(error, started)
        except RuntimeError:
            pass
        if config["kind"] == "custom" and process is not None:
            # A custom command is handed off; still say so if it fails.
            if (code := process.wait()):
                try:
                    relay.exited.emit(code)
                except RuntimeError:
                    pass
    threading.Thread(target=launch, name="rpmote-vpn-connect", daemon=True).start()


#: Seconds of silence from the machine before the wait dialog says why.
_DIAGNOSE_AFTER = 8


def started_text(config: dict) -> str:
    return ("VPN connected" if config["kind"] in vpn_connections.CONFIRMED_KINDS
            else "VPN connection requested")


def exited_text(code: int) -> str:
    return f"The connect command failed (exit code {code})."


class TenantVpnDialog(QDialog):
    """Configure the VPN a tenant needs."""

    def __init__(self, tenant: str, config: dict | None, parent=None, gate: "VpnGate | None" = None):
        super().__init__(parent)
        self.tenant = tenant
        # Connect VPN now goes through the gate, so it cannot cross a connect
        # or disconnect of the same VPN for a machine.
        self._gate = gate
        config = normalize(config) if config else normalize({"enabled": True})
        self._profile_id = config["profile_id"] or uuid.uuid4().hex
        self.removed = False
        self.setWindowTitle(f"Connect VPN first — {tenant}")
        self.setWindowIcon(icons.icon("vpn"))
        self.setMinimumWidth(640)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 14)
        layout.setSpacing(12)
        intro = QLabel(f"Before opening a machine in “{tenant}”, RPMote can connect its VPN "
                       "and wait for the machine to answer.")
        intro.setWordWrap(True)
        intro.setProperty("role", "hint")
        layout.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.enabled = QCheckBox("Use a VPN for this tenant")
        self.enabled.setChecked(config["enabled"])
        form.addRow("", self.enabled)

        self.kind = QComboBox()
        for key, label in vpn_connections.KINDS:
            self.kind.addItem(label, key)
        index = self.kind.findData(config["kind"])
        self.kind.setCurrentIndex(max(0, index))
        form.addRow("Kind of VPN", self.kind)

        self.profile_panel = QWidget()
        profile_form = QFormLayout(self.profile_panel)
        profile_form.setContentsMargins(0, 0, 0, 0)
        self.profile = QLineEdit(config["profile_name"] or tenant)
        profile_form.addRow("Connection name", self.profile)
        form.addRow(self.profile_panel)

        self.file_panel = QWidget()
        file_form = QFormLayout(self.file_panel)
        file_form.setContentsMargins(0, 0, 0, 0)
        file_row = QHBoxLayout()
        self.config_file = QLineEdit(config["config_file"])
        self.config_file.setReadOnly(True)
        self.config_file.setPlaceholderText("Choose a tunnel configuration file")
        file_row.addWidget(self.config_file, 1)
        self.btn_upload = QPushButton("Upload config…")
        self.btn_upload.clicked.connect(self._upload_config)
        file_row.addWidget(self.btn_upload)
        file_form.addRow("Config file", file_row)
        self.file_hint = QLabel()
        self.file_hint.setWordWrap(True)
        self.file_hint.setProperty("role", "hint")
        file_form.addRow(self.file_hint)
        form.addRow(self.file_panel)

        self.manual_panel = QWidget()
        manual_form = QFormLayout(self.manual_panel)
        manual_form.setContentsMargins(0, 0, 0, 0)
        self.server = QLineEdit(config["server"])
        self.server.setPlaceholderText("vpn.example.com or IP address")
        manual_form.addRow("Server address", self.server)
        self.username = QLineEdit(config["username"])
        manual_form.addRow("Username", self.username)
        self.domain = QLineEdit(config["domain"])
        self.domain.setPlaceholderText("Optional")
        manual_form.addRow("Domain", self.domain)
        self.password = QLineEdit(config["password"])
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        manual_form.addRow("Password", self.password)
        self.psk = QLineEdit(config["psk"])
        self.psk.setEchoMode(QLineEdit.EchoMode.Password)
        manual_form.addRow("IPsec pre-shared key", self.psk)
        self.authentication = QComboBox()
        for method in ("MSChapv2", "Pap", "Chap"):
            self.authentication.addItem(method)
        self.authentication.setCurrentText(config["authentication"])
        manual_form.addRow("Authentication", self.authentication)
        self.encryption = QComboBox()
        for value, label in (("Required", "Require encryption"), ("Maximum", "Maximum strength"),
                             ("Optional", "Optional encryption"), ("NoEncryption", "No PPP encryption")):
            self.encryption.addItem(label, value)
        self.encryption.setCurrentIndex(self.encryption.findData(config["encryption"]))
        manual_form.addRow("Data encryption", self.encryption)
        self.split_tunneling = QCheckBox("Use split tunneling")
        self.split_tunneling.setChecked(config["split_tunneling"])
        manual_form.addRow("", self.split_tunneling)
        secret_hint = QLabel("Login details are saved in your encrypted vault. Windows creates "
                            "a separate RPMote VPN profile when you connect.")
        secret_hint.setWordWrap(True)
        secret_hint.setProperty("role", "hint")
        manual_form.addRow(secret_hint)
        self._manual_form = manual_form
        form.addRow(self.manual_panel)

        self.command_panel = QWidget()
        command_form = QFormLayout(self.command_panel)
        command_form.setContentsMargins(0, 0, 0, 0)
        preset_row = QHBoxLayout()
        self.preset = QComboBox()
        for label, *_ in PRESETS:
            self.preset.addItem(label)
        self.preset.setCurrentIndex(len(PRESETS) - 1)
        preset_row.addWidget(self.preset, 1)
        self.preset_profile = QLineEdit()
        self.preset_profile.setPlaceholderText("profile / tunnel name")
        preset_row.addWidget(self.preset_profile, 1)
        apply_btn = QPushButton("Fill in")
        apply_btn.setToolTip("Write the commands for this kind of VPN")
        apply_btn.clicked.connect(self._apply_preset)
        preset_row.addWidget(apply_btn)
        command_form.addRow("Command preset", preset_row)

        self.command = QLineEdit(config["command"])
        self.command.setPlaceholderText('e.g. rasdial "Contoso VPN"')
        command_form.addRow("Connect command", self.command)
        self.disconnect_command = QLineEdit(config["disconnect_command"])
        self.disconnect_command.setPlaceholderText('optional — e.g. rasdial "Contoso VPN" /disconnect')
        command_form.addRow("Disconnect command", self.disconnect_command)
        form.addRow(self.command_panel)
        self.keep_connected = QCheckBox("Leave the VPN connected after the sessions that needed it close")
        self.keep_connected.setToolTip("Normally RPMote disconnects a VPN it connected as soon as the "
                                       "last session that needed it closes, and when RPMote quits.")
        self.keep_connected.setChecked(config["keep_connected"])
        form.addRow("", self.keep_connected)
        self.skip = QCheckBox("Skip the VPN when the machine already answers")
        self.skip.setChecked(config["skip_if_reachable"])
        form.addRow("", self.skip)
        self.wait = QSpinBox()
        self.wait.setRange(5, 300)
        self.wait.setSuffix(" s")
        self.wait.setValue(config["wait_seconds"])
        self.wait.setMaximumWidth(120)
        form.addRow("Wait up to", self.wait)
        form_container = QWidget()
        form_container.setLayout(form)
        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.form_scroll.setWidget(form_container)
        form_container.setAutoFillBackground(False)
        self.form_scroll.viewport().setAutoFillBackground(False)
        self.form_scroll.setMinimumHeight(220)
        layout.addWidget(self.form_scroll, 1)
        self._form_container = form_container

        row = QHBoxLayout()
        self.btn_test = QPushButton("Connect VPN now")
        self.btn_test.setIcon(icons.icon("play"))
        self.btn_test.clicked.connect(self._test_clicked)
        row.addWidget(self.btn_test)
        row.addStretch(1)
        layout.addLayout(row)
        # Its own full-width row: a reason can run to several lines and end
        # in a public key to copy for the site's admin.
        self.test_status = QLabel("")
        self.test_status.setProperty("role", "hint")
        self.test_status.setWordWrap(True)
        self.test_status.setTextFormat(Qt.TextFormat.PlainText)
        self.test_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.test_status)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        set_variant(buttons.button(QDialogButtonBox.StandardButton.Save), "primary")
        remove = buttons.addButton("Remove", QDialogButtonBox.ButtonRole.DestructiveRole)
        set_variant(remove, "ghost")
        remove.clicked.connect(self._remove)
        remove.setVisible(vpn_connections.configured(config))
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self._test_relay = _ProbeRelay(self)
        self._test_relay.launched.connect(self._test_finished)
        self._test_relay.exited.connect(self._test_exited)
        self._test_relay.disconnected.connect(self._test_disconnected)
        self._test_config = None
        self._test_connected = None   # (config, started) while the button holds a VPN up
        self._held = None             # the gate's tunnel, while the button holds it
        self._test_via_gate = False
        self._closed = False
        self._kind_changed()
        fit_to_screen(self)

    def _kind_changed(self):
        kind = self.kind.currentData()
        self.profile_panel.setVisible(kind == "windows" or kind in vpn_connections.MANUAL_KINDS)
        self.file_panel.setVisible(kind in vpn_connections.FILE_KINDS)
        self.manual_panel.setVisible(kind in vpn_connections.MANUAL_KINDS)
        self.command_panel.setVisible(kind == "custom")
        self._manual_form.setRowVisible(self.psk, kind == "l2tp")
        self.file_hint.setText(
            "OpenVPN GUI imports this file when you connect, with any certificate and key "
            "files it names from the same folder. OpenVPN GUI must be installed."
            if kind == "openvpn" else
            "WireGuard must be installed. Keep the config file at this location. "
            "The first connection asks for administrator approval to install the tunnel; "
            "after that RPMote connects and disconnects it without asking.")
        if self._test_connected is None:
            self.test_status.setText("")
        self.adjustSize()
        self.resize(self.width(), max(380, self._form_container.sizeHint().height() + 160))
        fit_to_screen(self)

    def _upload_config(self):
        kind = self.kind.currentData()
        extension = "ovpn" if kind == "openvpn" else "conf"
        path, _filter = QFileDialog.getOpenFileName(
            self, "Upload tunnel configuration", self.config_file.text(),
            f"Tunnel configuration (*.{extension})")
        if not path:
            return
        try:
            vpn_connections.validate({"kind": kind, "config_file": path})
        except OSError as exc:
            QMessageBox.warning(self, "VPN config", str(exc))
            return
        self.config_file.setText(str(Path(path).resolve()))
        self.config_file.setToolTip(self.config_file.text())
        if (warning := vpn_connections.config_warning({"kind": kind, "config_file": path})):
            QMessageBox.warning(self, "VPN config", warning)

    def _apply_preset(self):
        label, connect, disconnect = PRESETS[self.preset.currentIndex()]
        name = self.preset_profile.text().strip() or self.tenant
        if connect:
            self.command.setText(connect.replace("{name}", name))
            self.disconnect_command.setText(disconnect.replace("{name}", name))

    def _test_clicked(self):
        if self._test_connected is not None:
            self._test_disconnect()
        else:
            self._test()

    def _test(self):
        config = self.config()
        try:
            vpn_connections.validate(config)
        except OSError as exc:
            self.test_status.setText(str(exc))
            return
        self.btn_test.setEnabled(False)
        self.test_status.setText("Connecting VPN…")
        self._test_config = config
        # Still holding from an earlier click when a custom command has no
        # disconnect: let go only after holding again, so it is not dropped between.
        previous = self._held
        self._held = self._gate.hold(self.tenant, config, self._test_relay) if self._gate else None
        if previous is not None:
            self._gate.release(previous, self._test_relay)
        self._test_via_gate = self._held is not None
        if self._held is None:
            _start_worker(config, self._test_relay, lock=self._lock())

    def _lock(self):
        """The gate's lock for this tenant's VPN, so the button never crosses it."""
        return self._gate.lock(self.tenant) if self._gate else contextlib.nullcontext()

    def _disconnect_in_background(self, config: dict, relay=None):
        lock = self._lock()

        def work():
            with lock:
                error = _disconnect_and_wait(config)
            if relay is not None:
                try:
                    relay.disconnected.emit(error)
                except RuntimeError:
                    pass
        threading.Thread(target=work, name="rpmote-vpn-disconnect", daemon=True).start()

    def _test_finished(self, error: str, started: bool = True):
        config = self._test_config
        if error and self._held is not None:
            self._gate.release(self._held, self._test_relay)
            self._held = None
        if self._closed:
            # Closed while connecting: nothing is left to hold the VPN up. (The
            # gate saw to its own when the window let go of it.)
            if (not self._test_via_gate and not error and started and can_disconnect(config)
                    and not self.keep_connected.isChecked()):
                self._disconnect_in_background(config)
            return
        self.btn_test.setEnabled(True)
        if error:
            self.test_status.setText(error)
            return
        text = ("VPN connected." if config["kind"] in vpn_connections.CONFIRMED_KINDS
                else "Connection requested — check that the VPN comes up.")
        if can_disconnect(config):
            self._test_connected = (config, started)
            self.btn_test.setText("Disconnect VPN")
            self.btn_test.setIcon(icons.icon("stop"))
            if started and not self.keep_connected.isChecked():
                text += " It disconnects when you close this window."
        self.test_status.setText(text)

    def _test_exited(self, code: int):
        self.test_status.setText(exited_text(code))

    def _test_disconnect(self):
        config, _started = self._test_connected
        self._test_connected = None
        self.btn_test.setEnabled(False)
        self.test_status.setText("Disconnecting VPN…")
        if self._held is not None:
            held, self._held = self._held, None
            self._gate.disconnect_now(held, self._test_relay)
        else:
            self._disconnect_in_background(config, self._test_relay)

    def _test_disconnected(self, error: str):
        if self._closed:
            return
        self.btn_test.setEnabled(True)
        self.btn_test.setText("Connect VPN now")
        self.btn_test.setIcon(icons.icon("play"))
        self.test_status.setText(error or "VPN disconnected.")

    def done(self, result):
        # A VPN the button connected is not left running behind a closed window.
        self._closed = True
        if self._held is not None:
            # The gate takes it down, unless a session opened over it meanwhile.
            held, self._held = self._held, None
            self._test_connected = None
            self._gate.release(held, self._test_relay, keep=self.keep_connected.isChecked())
        elif self._test_connected is not None:
            config, started = self._test_connected
            self._test_connected = None
            if started and not self.keep_connected.isChecked():
                self._disconnect_in_background(config)
        super().done(result)

    def _remove(self):
        self.removed = True
        self.accept()

    def _accept(self):
        if self.enabled.isChecked():
            try:
                vpn_connections.validate(self.config())
            except OSError as exc:
                QMessageBox.warning(self, "VPN", str(exc))
                return
        self.accept()

    def config(self) -> dict:
        return normalize({
            "enabled": self.enabled.isChecked(),
            "command": self.command.text(),
            "disconnect_command": self.disconnect_command.text(),
            "keep_connected": self.keep_connected.isChecked(),
            "wait_seconds": self.wait.value(),
            "skip_if_reachable": self.skip.isChecked(),
            "kind": self.kind.currentData(),
            "profile_name": self.profile.text(),
            "profile_id": self._profile_id,
            "config_file": self.config_file.text(),
            "server": self.server.text(),
            "username": self.username.text(),
            "domain": self.domain.text(),
            "password": self.password.text(),
            "psk": self.psk.text(),
            "authentication": self.authentication.currentText(),
            "encryption": self.encryption.currentData(),
            "split_tunneling": self.split_tunneling.isChecked(),
        })


class _WaitDialog(QDialog):
    """"Connecting Contoso VPN…" with a Cancel and an Open-anyway escape."""

    def __init__(self, tenant: str, target: str, seconds: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connecting VPN")
        self.setWindowIcon(icons.icon("vpn"))
        self.setMinimumWidth(420)
        self.choice = "cancel"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 14)
        self.label = QLabel(f"Starting the VPN for <b>{html.escape(tenant)}</b>…")
        layout.addWidget(self.label)
        self.detail = QLabel(f"Waiting for {target} to answer")
        self.detail.setProperty("role", "hint")
        self.detail.setWordWrap(True)
        # The reasons can end in a public key to send to the site's admin.
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail)
        self.bar = QProgressBar()
        self.bar.setRange(0, seconds)
        self.bar.setTextVisible(False)
        layout.addWidget(self.bar)
        buttons = QDialogButtonBox()
        self.btn_anyway = buttons.addButton("Open anyway", QDialogButtonBox.ButtonRole.AcceptRole)
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.btn_anyway.clicked.connect(self._anyway)
        cancel.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _anyway(self):
        self.choice = "open"
        self.accept()

    def set_detail(self, text: str):
        """Show *text* in full; an open dialog does not grow for a wrapped label by itself."""
        self.detail.setText(text)
        self.layout().activate()
        height = self.heightForWidth(self.width())
        if height > self.height():
            self.resize(self.width(), height)
            fit_to_screen(self, center=False)


class _Tunnel:
    """One tenant's VPN as the gate handles it, shared by every machine waiting on it."""

    def __init__(self, tenant: str, config: dict, baseline: set):
        self.tenant = tenant
        self.config = config
        self.state = "connecting"       # then "up"; "disconnecting" on the way down
        self.owned = False              # RPMote brought it up, so RPMote takes it down
        self.keep = None                # the settings window's say, over the saved setting
        self.baseline = set(baseline)   # sessions that were already open without it
        self.waiting = 0                # machines still waiting for it to come up
        self.listeners: list[_ProbeRelay] = []


class VpnGate(QObject):
    """Hooks into MainWindow._open_connection via its ``_before_open``.

    A VPN the gate brings up is used by the tenant's sessions opened from then
    on. When the last of them closes, and no machine is still waiting for it,
    the gate disconnects it. The window reports session changes through
    :meth:`sessions_changed` and its ``_open_sessions()`` ({key: tenant}).
    """

    _probe_done = pyqtSignal(object, bool)   # token, reachable
    _disconnected = pyqtSignal(object, str)  # tunnel, error

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._tunnels: dict[str, _Tunnel] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._workers: list[threading.Thread] = []
        self._closing = False
        self._probe_done.connect(self._on_probe_done)
        self._disconnected.connect(self._on_disconnected)
        self._pending: dict[int, dict] = {}

    def config_for(self, tenant: str) -> dict | None:
        try:
            raw = (self.window.vault.settings.get(SETTINGS_KEY, {}) or {}).get(tenant)
        except Exception:
            return None
        if not raw:
            return None
        config = normalize(raw)
        return config if config["enabled"] and vpn_connections.configured(config) else None

    def lock(self, tenant: str) -> threading.Lock:
        """Held while the tenant's VPN connects or disconnects, so the two never cross."""
        return self._locks.setdefault(tenant, threading.Lock())

    def _sessions(self) -> dict:
        provider = getattr(self.window, "_open_sessions", None)
        try:
            return dict(provider()) if callable(provider) else {}
        except Exception:
            return {}

    def _vpn_sessions(self, tenant: str, config: dict, sessions: dict | None = None) -> set:
        """Open sessions of *tenant*, and of any other tenant set to this same VPN."""
        identity = vpn_identity(config)
        same = {tenant: True}
        found = set()
        for key, owner in (self._sessions() if sessions is None else sessions).items():
            if owner not in same:
                other = self.config_for(owner)
                same[owner] = other is not None and vpn_identity(other) == identity
            if same[owner]:
                found.add(key)
        return found

    def _say(self, text: str):
        status = getattr(self.window, "status", None)
        if status is not None and hasattr(status, "showMessage"):
            status.showMessage(text, 6000)

    # ── the gate ──────────────────────────────────────────────────

    def before_open(self, conn: dict, proceed) -> bool:
        """True when the gate has taken over (it will call *proceed* itself)."""
        if self._closing:
            return False
        tenant = str(conn.get("tenant", "Default") or "Default")
        config = self.config_for(tenant)
        if config is None:
            return False
        host, port, role = self._first_hop(conn)
        if not host or not port:
            return False
        token = id(conn) ^ int(time.time() * 1000)
        self._pending[token] = {"conn": conn, "proceed": proceed, "config": config,
                                "tenant": tenant, "host": host, "port": port, "role": role}
        tunnel = self._tunnels.get(tenant)
        _LOG.info("open | %s | %s:%s%s | tenant %s, VPN %s%s", conn.get("name") or host, host, port,
                  f" ({role})" if role else "", tenant, vpn_connections.describe(config),
                  f" ({tunnel.state})" if tunnel else "")
        # A machine that answers through a VPN on its way down would lose it a moment later.
        if config["skip_if_reachable"] and not (tunnel and tunnel.state == "disconnecting"):
            def probe():
                ok, _ms, _detail = tcp_probe(host, port, timeout=1.5)
                try:
                    self._probe_done.emit(token, ok)
                except RuntimeError:
                    pass
            threading.Thread(target=probe, name="rpmote-vpn-probe", daemon=True).start()
        else:
            self._on_probe_done(token, False)
        return True

    def _first_hop(self, conn: dict) -> tuple[str, int | None, str]:
        """(host, port, what it is) of the machine this PC actually dials for *conn*.

        Through an RD Gateway or SSH jump hosts that is the gateway or the
        outermost jump host: the machine behind it never answers this PC
        directly, VPN or not, so waiting for it would never end.
        """
        protocol = str(conn.get("protocol", "") or "").lower()
        if protocol == "rdp":
            from app.rdp_gateway import normalize_rdp_gateway, split_gateway_address

            gateway = normalize_rdp_gateway(conn)["host"]
            if gateway:
                host, port = split_gateway_address(gateway)
                return host, port, "RD Gateway"
        if protocol in ("ssh", "sftp") and conn.get("jump_via"):
            try:
                from app.ssh_connect import resolve_jump_chain

                saved = getattr(self.window, "_safe_connections", None)
                chain = resolve_jump_chain(conn, saved() if callable(saved) else [])
            except Exception:
                chain = []      # a broken chain is the session's to report
            if chain:
                return connection_host(chain[-1]), connection_port(chain[-1]), "jump host"
        return connection_host(conn), connection_port(conn), ""

    def _on_probe_done(self, token, reachable: bool):
        job = self._pending.pop(token, None)
        if job is None:
            return
        if reachable:
            _LOG.info("open | %s:%s answers as it is; opening", job["host"], job["port"])
            job["proceed"]()
            return
        self._bring_up_and_wait(job)

    def _bring_up(self, tenant: str, config: dict) -> _Tunnel:
        """The tenant's tunnel, connecting unless a connect is already under way."""
        tunnel = self._tunnels.get(tenant)
        if tunnel is None:
            tunnel = _Tunnel(tenant, config, self._vpn_sessions(tenant, config))
            self._tunnels[tenant] = tunnel
        elif tunnel.state == "connecting":
            return tunnel
        else:
            # Up but the machine does not answer (it may have dropped), or on
            # its way down: connect again. The lock queues it after a disconnect.
            tunnel.state = "connecting"
        relay = _ProbeRelay(self)
        relay.launched.connect(lambda error, started, t=tunnel, r=relay: self._on_launched(t, r, error, started))
        relay.exited.connect(lambda code, t=tunnel: [listener.exited.emit(code) for listener in t.listeners])
        _start_worker(tunnel.config, relay, lock=self.lock(tenant), tunnel=tunnel)
        return tunnel

    def _on_launched(self, tunnel: _Tunnel, relay, error: str, started: bool):
        if not error:
            tunnel.owned = tunnel.owned or started
            tunnel.state = "up"
        elif tunnel.owned:
            tunnel.state = "up"     # still RPMote's to take down
        elif self._tunnels.get(tunnel.tenant) is tunnel:
            del self._tunnels[tunnel.tenant]
        if error:
            _LOG.warning("gate | %s: the VPN did not connect: %s", tunnel.tenant, error)
        elif started:
            _LOG.info("gate | %s: VPN %s by RPMote", tunnel.tenant,
                      "connected" if tunnel.config["kind"] in vpn_connections.CONFIRMED_KINDS
                      else "connection requested")
        else:
            _LOG.info("gate | %s: VPN was already connected%s", tunnel.tenant,
                      "" if tunnel.owned else "; it stays up when RPMote is done")
        for listener in list(tunnel.listeners):
            listener.launched.emit(error, started)
        if tunnel.config["kind"] != "custom":   # a custom command may still report its exit
            relay.deleteLater()
        self._check(tunnel)

    def _bring_up_and_wait(self, job: dict):
        config, tenant = job["config"], job["tenant"]
        target = f"{job['host']}:{job['port']}" + (f" ({job['role']})" if job.get("role") else "")
        dialog = _WaitDialog(tenant, target, config["wait_seconds"], self.window)
        state = {"elapsed": 0, "done": False, "inflight": False, "launching": True,
                 "diagnosed_at": None, "diagnosis": "", "probe": "", "error": ""}
        timer = QTimer(dialog)
        result = {"ok": False}
        probe_signal = _ProbeRelay(dialog)
        tunnel = self._bring_up(tenant, config)
        tunnel.waiting += 1
        tunnel.listeners.append(probe_signal)

        def tick():
            if state["done"] or state["launching"]:
                return
            state["elapsed"] += 1
            dialog.bar.setValue(min(state["elapsed"], config["wait_seconds"]))
            if state["elapsed"] >= config["wait_seconds"]:
                timer.stop()
                _LOG.warning("wait | %s did not answer within %s s (last probe: %s)", target,
                             config["wait_seconds"], state["probe"] or "none")
                dialog.label.setText(f"{target} did not answer within "
                                     f"{config['wait_seconds']} s.")
                dialog.set_detail(state["diagnosis"] or
                                  "Check the VPN client — or open the connection anyway.")
                return
            if (state["elapsed"] >= _DIAGNOSE_AFTER and
                    (state["diagnosed_at"] is None or state["elapsed"] - state["diagnosed_at"] >= 10)):
                # Still nothing: say why, as far as Windows can tell, rather than just count down.
                state["diagnosed_at"] = state["elapsed"]

                def diagnose():
                    try:
                        text = vpn_connections.diagnose(config, job["host"], job["port"])
                    except Exception:
                        text = ""
                    try:
                        probe_signal.diagnosis.emit(text)
                    except RuntimeError:
                        pass
                threading.Thread(target=diagnose, name="rpmote-vpn-diagnose", daemon=True).start()
            if not state["inflight"]:
                state["inflight"] = True

                def probe():
                    ok, _ms, detail = tcp_probe(job["host"], job["port"], timeout=1.0)
                    state["probe"] = detail
                    try:
                        probe_signal.result.emit(ok)
                    except RuntimeError:
                        pass
                threading.Thread(target=probe, name="rpmote-vpn-probe", daemon=True).start()

        def on_result(ok: bool):
            state["inflight"] = False
            if ok and not state["done"]:
                state["done"] = True
                result["ok"] = True
                timer.stop()
                dialog.accept()

        def on_launched(error: str, _started: bool = True):
            if state["done"]:
                return
            state["launching"] = False
            state["error"] = error
            if error:
                timer.stop()
                dialog.bar.setRange(0, config["wait_seconds"])
                dialog.bar.setValue(0)
                dialog.label.setText("The VPN could not connect.")
                dialog.set_detail(error)
            else:
                dialog.bar.setRange(0, config["wait_seconds"])
                dialog.label.setText(started_text(config))
                tick()

        def on_diagnosis(text: str):
            if text and not state["done"]:
                if text != state["diagnosis"]:
                    _LOG.info("wait | %s after %s s: %s", target, state["elapsed"], text)
                state["diagnosis"] = text
                dialog.set_detail(text)

        def on_exited(code: int):
            if not state["done"]:
                dialog.label.setText(exited_text(code))
                dialog.set_detail(f"Still waiting for {target} — or open the connection anyway.")

        def finished(_code: int):
            timer.stop()
            state["done"] = True
            if probe_signal in tunnel.listeners:
                tunnel.listeners.remove(probe_signal)
            if result["ok"]:
                outcome = f"answered after {state['elapsed']} s; opening"
            elif dialog.choice == "open":
                outcome = (f"opened anyway after {state['elapsed']} s"
                           + (f" (last probe: {state['probe']})" if state["probe"] else ""))
            else:
                outcome = (f"cancelled after {state['elapsed']} s"
                           if not state["launching"] else "cancelled while the VPN was connecting")
            _LOG.info("wait | %s %s%s", target, outcome,
                      " (the VPN did not connect)" if state["error"] else "")
            try:
                if result["ok"] or dialog.choice == "open":
                    job["proceed"]()
            finally:
                # Only now: the session that just opened is what keeps the VPN up.
                tunnel.waiting -= 1
                self._check(tunnel)
                dialog.deleteLater()

        probe_signal.result.connect(on_result)
        probe_signal.launched.connect(on_launched)
        probe_signal.exited.connect(on_exited)
        probe_signal.diagnosis.connect(on_diagnosis)
        timer.timeout.connect(tick)
        dialog.finished.connect(finished)
        dialog.bar.setRange(0, 0)
        timer.start(1000)
        # Not exec(): several machines may wait on the same VPN at once.
        dialog.open()

    # ── tidy-up ───────────────────────────────────────────────────

    def sessions_changed(self):
        """A session opened or closed: disconnect any VPN nothing uses any more."""
        for tunnel in list(self._tunnels.values()):
            self._check(tunnel)

    def _keep(self, tunnel: _Tunnel) -> bool:
        current = self.config_for(tunnel.tenant)
        if tunnel.keep is not None:
            return tunnel.keep
        return (current or tunnel.config)["keep_connected"]

    def _check(self, tunnel: _Tunnel):
        if self._closing or self._tunnels.get(tunnel.tenant) is not tunnel:
            return
        if tunnel.state != "up" or tunnel.waiting:
            return
        sessions = self._vpn_sessions(tunnel.tenant, tunnel.config)
        tunnel.baseline &= sessions
        if sessions - tunnel.baseline:
            return      # a session opened over it is still open, in this tenant or one sharing it
        if not tunnel.owned or self._keep(tunnel):
            # Not RPMote's to take down; a later connect starts afresh.
            _LOG.info("gate | %s: nothing uses the VPN now; left connected (%s)", tunnel.tenant,
                      "set to stay connected" if tunnel.owned else "it was up before RPMote")
            del self._tunnels[tunnel.tenant]
            return
        heir = self._sharer(tunnel)
        if heir is not None:
            # Another tenant set to the same VPN still uses it: it goes down with theirs.
            _LOG.info("gate | %s: done with the VPN; %s still uses it", tunnel.tenant, heir.tenant)
            heir.owned = True
            del self._tunnels[tunnel.tenant]
            self._check(heir)
            return
        _LOG.info("gate | %s: nothing uses the VPN now; disconnecting", tunnel.tenant)
        tunnel.state = "disconnecting"
        self._disconnect_in_background(tunnel)

    def _sharer(self, tunnel: _Tunnel) -> _Tunnel | None:
        """Another tenant's live record of the very same VPN, if any."""
        identity = vpn_identity(tunnel.config)
        return next((other for other in self._tunnels.values()
                     if other is not tunnel and other.state in ("up", "connecting")
                     and vpn_identity(other.config) == identity), None)

    # ── Connect VPN now, in the tenant's VPN settings ─────────────

    def hold(self, tenant: str, config: dict, relay) -> _Tunnel | None:
        """Bring the tenant's VPN up for the settings window and keep it up until release().

        The window holds it the way a waiting machine does, so opening one
        meanwhile neither connects it twice nor loses it to the window's
        disconnect. None when the gate cannot: RPMote is quitting, or the
        tenant's VPN in use is not the one being tried.
        """
        current = self._tunnels.get(tenant)
        if self._closing or (current is not None and not same_vpn(current.config, config)):
            return None
        tunnel = self._bring_up(tenant, config)
        tunnel.waiting += 1
        tunnel.listeners.append(relay)
        return tunnel

    def release(self, tunnel: _Tunnel, relay, keep: bool | None = None):
        """The settings window lets go: down it goes, unless a session uses it.

        *keep* is the window's Leave-connected box as it closes, which is newer
        than the saved setting.
        """
        if relay in tunnel.listeners:
            tunnel.listeners.remove(relay)
        tunnel.waiting = max(0, tunnel.waiting - 1)
        if keep is not None:
            tunnel.keep = keep
        self._check(tunnel)

    def disconnect_now(self, tunnel: _Tunnel, relay):
        """The settings window's Disconnect VPN: down it goes, whoever brought it up."""
        if relay in tunnel.listeners:
            tunnel.listeners.remove(relay)
        tunnel.waiting = max(0, tunnel.waiting - 1)
        _LOG.info("gate | %s: disconnecting on request", tunnel.tenant)
        tunnel.owned = True
        tunnel.state = "disconnecting"
        self._disconnect_in_background(tunnel, relay)

    def _disconnect_in_background(self, tunnel: _Tunnel, relay=None) -> threading.Thread:
        lock, config = self.lock(tunnel.tenant), tunnel.config

        def work():
            error = ""
            with lock:
                # A connect still under way when RPMote quits has said by now whether it was RPMote's.
                if tunnel.owned:
                    error = _disconnect_and_wait(config)
            for signal, args in ((self._disconnected, (tunnel, error)),
                                 (getattr(relay, "disconnected", None), (error,))):
                try:
                    if signal is not None:
                        signal.emit(*args)
                except RuntimeError:
                    pass
        thread = threading.Thread(target=work, name="rpmote-vpn-disconnect", daemon=True)
        self._workers = [worker for worker in self._workers if worker.is_alive()] + [thread]
        thread.start()
        return thread

    def _on_disconnected(self, tunnel: _Tunnel, error: str):
        if self._tunnels.get(tunnel.tenant) is tunnel and tunnel.state == "disconnecting":
            del self._tunnels[tunnel.tenant]
        if error:
            _LOG.warning("gate | %s: disconnect failed: %s", tunnel.tenant, error)
        else:
            _LOG.info("gate | %s: VPN disconnected", tunnel.tenant)
        if self._closing:
            return
        self._say(f"{tunnel.tenant}: {error}" if error else f"{tunnel.tenant}: VPN disconnected")

    def shutdown(self, surviving: dict | None = None, timeout: float = 15.0):
        """Disconnect the VPNs RPMote brought up, before the app goes.

        *surviving* ({key: tenant}) are sessions that outlive the window, such
        as Remote Desktop in its own window; a VPN one of them uses stays up.
        Waits up to *timeout* seconds, so the disconnects are not cut off.
        """
        self._closing = True
        surviving = surviving or {}
        for tunnel in list(self._tunnels.values()):
            if tunnel.state == "disconnecting" or self._keep(tunnel):
                continue
            # A surviving session opened over it, in this tenant or one sharing it, still uses it.
            if self._vpn_sessions(tunnel.tenant, tunnel.config, surviving) - tunnel.baseline:
                continue
            if tunnel.owned or tunnel.state == "connecting":
                _LOG.info("gate | %s: RPMote is closing; disconnecting", tunnel.tenant)
                tunnel.state = "disconnecting"
                self._disconnect_in_background(tunnel)
        deadline = time.monotonic() + timeout
        for worker in self._workers:
            worker.join(max(0.0, deadline - time.monotonic()))


class _ProbeRelay(QObject):
    result = pyqtSignal(bool)
    launched = pyqtSignal(str, bool)   # error, whether this call brought the VPN up
    exited = pyqtSignal(object)        # a custom command's exit code; NTSTATUS codes overflow int
    disconnected = pyqtSignal(str)     # error
    diagnosis = pyqtSignal(str)        # why the machine is not answering over the VPN
