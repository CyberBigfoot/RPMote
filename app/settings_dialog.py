"""
Settings (Ctrl+,) — every preference in one place.

They used to be spread over the File and View menus, an input box for the
auto-lock, the terminal header and the Graph Settings dialog, with Factory
Reset one slip away from Lock Vault. Changes apply as they are made; there is
no Apply button to forget.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QPushButton, QScrollArea, QSpinBox,
    QStackedWidget, QVBoxLayout, QWidget,
)

from app import icons
from app.themes import primary_ink
from app.widgets import fit_to_screen, set_variant

PAGES = (
    ("appearance", "Appearance", "palette"),
    ("terminal", "Terminal", "terminal"),
    ("connections", "Connections", "plug"),
    ("security", "Security", "shield"),
    ("data", "Data && backups", "archive"),
    ("advanced", "Advanced", "settings"),
)

LABEL_WIDTH = 150


def _form(group: QWidget) -> QFormLayout:
    form = QFormLayout(group)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    # Every field may grow: a word-wrapped hint that is held to its size
    # hint gets squeezed into a narrow column and clipped.
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setHorizontalSpacing(14)
    form.setVerticalSpacing(10)
    return form


def _label(text: str) -> QLabel:
    label = QLabel(text)
    label.setFixedWidth(LABEL_WIDTH)
    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    label.setProperty("role", "form-label")
    return label


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("role", "hint")
    return label


def _button(text: str, icon_name: str | None, slot, variant: str | None = None) -> QPushButton:
    button = QPushButton(text)
    if icon_name:
        color = (primary_ink() if variant == "primary"
                 else "#ffffff" if variant == "danger" else None)
        button.setIcon(icons.icon(icon_name, color))
    button.clicked.connect(slot)
    if variant:
        set_variant(button, variant)
    return button


def _row(*widgets) -> QWidget:
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    for widget in widgets:
        row.addWidget(widget)
    row.addStretch(1)
    return holder


class SettingsDialog(QDialog):
    """Live-applying settings for *window* (the main window)."""

    def __init__(self, window, parent=None, page: str | None = None):
        super().__init__(parent or window)
        self.window = window
        self.setWindowTitle("Settings")
        self.setObjectName("settings_dialog")
        self.setModal(True)
        self._pages: dict[str, int] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body, 1)

        self.nav = QListWidget()
        self.nav.setObjectName("settings_nav")
        self.nav.setFixedWidth(196)
        self.nav.setIconSize(QSize(18, 18))
        self.nav.setSpacing(2)
        self.nav.setFrameShape(QFrame.Shape.NoFrame)
        self.nav.setStyleSheet(
            "#settings_nav { padding: 12px 8px; border-right: 1px solid rgba(127,127,127,0.25); }"
            "#settings_nav::item { padding: 8px 10px; border-radius: 6px; }")
        body.addWidget(self.nav)

        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)

        builders = {
            "appearance": self._appearance_page,
            "terminal": self._terminal_page,
            "connections": self._connections_page,
            "security": self._security_page,
            "data": self._data_page,
            "advanced": self._advanced_page,
        }
        for key, label, icon_name in PAGES:
            item = QListWidgetItem(icons.icon(icon_name), label.replace("&&", "&"))
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.nav.addItem(item)
            self._pages[key] = self.stack.addWidget(self._scrolled(builders[key]()))
        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)

        footer = QFrame()
        footer.setObjectName("dialog_footer")
        foot = QHBoxLayout(footer)
        foot.setContentsMargins(20, 10, 20, 12)
        note = _hint("Changes take effect straight away.")
        note.setWordWrap(False)
        foot.addWidget(note)
        foot.addStretch(1)
        close = _button("Close", None, self.accept, "primary")
        close.setDefault(True)
        foot.addWidget(close)
        outer.addWidget(footer)

        self.show_page(page or "appearance")
        for combo in self.findChildren(QComboBox):
            # Size to the field, not to the longest choice: a long item must
            # not push the whole page past the window's edge.
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(16)
            combo.setMaximumWidth(420)
        self.resize(880, 620)
        fit_to_screen(self)

    def show_page(self, key: str):
        index = self._pages.get(key, 0)
        self.nav.setCurrentRow(index)

    @staticmethod
    def _scrolled(page: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        return scroll

    @staticmethod
    def _page(title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        heading = QLabel(title)
        # The app stylesheet sets label font sizes, so a QFont would lose.
        heading.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(heading)
        layout.addWidget(_hint(subtitle))
        return page, layout

    def _settings(self) -> dict:
        try:
            return dict(self.window.vault.settings)
        except Exception:
            return {}

    # ── Appearance ────────────────────────────────────────────────

    def _appearance_page(self) -> QWidget:
        page, layout = self._page("Appearance", "How RPMote looks.")
        group = QGroupBox("Window")
        form = _form(group)

        self.theme = QComboBox()
        for theme in getattr(self.window, "_themes", []):
            self.theme.addItem(theme.get("name", "Theme"))
        self.theme.setCurrentIndex(getattr(self.window, "_current_theme_index", 0))
        self.theme.currentIndexChanged.connect(
            lambda index: index >= 0 and self.window._select_theme(index))
        form.addRow(_label("Theme"), self.theme)

        self.density = QComboBox()
        self.density.addItem("Comfortable", "comfortable")
        self.density.addItem("Compact — more connections on screen", "compact")
        current = getattr(getattr(self.window, "side_panel", None), "density", "comfortable")
        self.density.setCurrentIndex(max(0, self.density.findData(current)))
        self.density.currentIndexChanged.connect(self._set_density)
        form.addRow(_label("Sidebar density"), self.density)

        self.quick_connect = QCheckBox("Show the Quick Connect bar")
        action = getattr(self.window, "act_quick_connect", None)
        self.quick_connect.setChecked(bool(action and action.isChecked()))
        if action is not None:
            self.quick_connect.toggled.connect(action.setChecked)
        form.addRow(_label("Toolbar"), self.quick_connect)

        self.overview = QCheckBox("Show the overview card on the dashboard")
        overview = getattr(self.window, "act_show_overview", None)
        self.overview.setChecked(bool(overview is None or overview.isChecked()))
        self.overview.toggled.connect(self._set_overview)
        form.addRow(_label("Dashboard"), self.overview)
        layout.addWidget(group)

        graph = QGroupBox("Dashboard graph")
        graph_form = _form(graph)
        from app.graph_settings import BACKGROUND_CHOICES
        self.background = QComboBox()
        for value, text in BACKGROUND_CHOICES:
            self.background.addItem(text, value)
        dashboard = getattr(self.window, "dashboard", None)
        current = (dashboard.background_level() if dashboard is not None
                   else "calm")
        self.background.setCurrentIndex(max(0, self.background.findData(current)))
        self.background.currentIndexChanged.connect(self._set_background)
        graph_form.addRow(_label("Background"), self.background)
        graph_form.addRow(_label(""), _hint(
            "Animated themes only: how strongly their scene shows behind the graph."))
        graph_form.addRow(_label(""), _row(_button(
            "Graph settings…", "settings", self._graph_settings)))
        graph_form.addRow(_label(""), _hint("Node sizes, labels, animation and links."))
        layout.addWidget(graph)
        layout.addStretch(1)
        return page

    def _set_density(self, _index: int):
        density = self.density.currentData()
        panel = getattr(self.window, "side_panel", None)
        if panel is not None:
            panel.set_density(density)
        self.window._save_sidebar_density(density)

    def _set_overview(self, shown: bool):
        overview = getattr(self.window, "act_show_overview", None)
        if overview is not None:
            overview.setChecked(shown)
        self.window._set_home_visible(shown)

    def _set_background(self, _index: int):
        dashboard = getattr(self.window, "dashboard", None)
        if dashboard is not None:
            dashboard.set_graph_option("background", self.background.currentData())

    def _graph_settings(self):
        opener = getattr(self.window, "_open_graph_settings", None)
        if callable(opener):
            opener()

    # ── Terminal ──────────────────────────────────────────────────

    def _terminal_page(self) -> QWidget:
        from app.ssh_terminal import SSHTerminal

        page, layout = self._page(
            "Terminal", "SSH, Telnet and serial sessions. Open terminals change too.")
        group = QGroupBox("Look")
        form = _form(group)
        self.term_style = QComboBox()
        self.term_style.addItems(SSHTerminal.TERMINAL_STYLES)
        self.term_style.setCurrentText(getattr(self.window, "_ssh_terminal_style", "System Theme"))
        self.term_style.currentTextChanged.connect(
            lambda name: self.window._apply_terminal_appearance(style=name))
        form.addRow(_label("Colour scheme"), self.term_style)

        self.term_font = QComboBox()
        self.term_font.addItems(SSHTerminal.TERMINAL_FONTS)
        self.term_font.setCurrentText(getattr(self.window, "_ssh_terminal_font", "Default Font"))
        self.term_font.currentTextChanged.connect(
            lambda name: self.window._apply_terminal_appearance(font=name))
        form.addRow(_label("Font"), self.term_font)

        self.term_size = QSpinBox()
        self.term_size.setRange(SSHTerminal.MIN_FONT_SIZE, SSHTerminal.MAX_FONT_SIZE)
        self.term_size.setSuffix(" pt")
        self.term_size.setMaximumWidth(120)
        self.term_size.setValue(int(getattr(self.window, "_ssh_font_size",
                                            SSHTerminal.DEFAULT_FONT_SIZE)))
        self.term_size.valueChanged.connect(
            lambda size: self.window._apply_terminal_appearance(size=size))
        form.addRow(_label("Font size"), self.term_size)
        layout.addWidget(group)

        records = QGroupBox("Recordings")
        records_form = _form(records)
        records_form.addRow(_label(""), _hint(
            "Session output can be recorded from a terminal's ⋯ menu. Recordings older "
            "than 30 days are tidied away when RPMote starts."))
        records_form.addRow(_label(""), _row(_button(
            "Session recordings…", "record", self.window._open_session_recordings)))
        layout.addWidget(records)
        layout.addStretch(1)
        return page

    # ── Connections ───────────────────────────────────────────────

    def _connections_page(self) -> QWidget:
        from app import host_keys

        page, layout = self._page("Connections", "Defaults for opening and checking connections.")

        group = QGroupBox("Opening")
        form = _form(group)
        self.rdp_tabs = QCheckBox("Open Remote Desktop inside RPMote tabs")
        action = getattr(self.window, "act_rdp_embedded", None)
        self.rdp_tabs.setChecked(bool(action and action.isChecked()))
        if action is not None:
            self.rdp_tabs.toggled.connect(action.setChecked)
        form.addRow(_label("Remote Desktop"), self.rdp_tabs)
        form.addRow(_label(""), _hint(
            "For RDP connections set to \"App default\". Off: each desktop opens in its "
            "own Remote Desktop window."))
        layout.addWidget(group)

        checks = QGroupBox("Reachability checks")
        checks_form = _form(checks)
        ping = self._ping_settings()
        self.ping_enabled = QCheckBox("Keep checking every connection in the background")
        self.ping_enabled.setChecked(bool(ping.get("enabled")))
        self.ping_enabled.toggled.connect(self._set_ping_enabled)
        checks_form.addRow(_label("Background checks"), self.ping_enabled)
        self.ping_interval = QSpinBox()
        self.ping_interval.setRange(10, 3600)
        self.ping_interval.setSingleStep(10)
        self.ping_interval.setSuffix(" s")
        self.ping_interval.setMaximumWidth(120)
        self.ping_interval.setValue(int(ping.get("interval", 30)))
        self.ping_interval.valueChanged.connect(
            lambda value: self._apply_ping({"interval": int(value)}))
        checks_form.addRow(_label("Check every"), self.ping_interval)
        self.check_on_login = QCheckBox("Check everything once after unlocking")
        self.check_on_login.setChecked(bool(ping.get("check_on_login", True)))
        self.check_on_login.toggled.connect(
            lambda on: self._apply_ping({"check_on_login": bool(on)}))
        checks_form.addRow(_label("On unlock"), self.check_on_login)
        layout.addWidget(checks)

        keys = QGroupBox("SSH host keys")
        keys_form = _form(keys)
        self.host_key_policy = QComboBox()
        labels = {
            host_keys.POLICY_PROMPT: "Ask me about new and changed keys",
            host_keys.POLICY_TOFU: "Trust new keys automatically; ask about changed ones",
            host_keys.POLICY_STRICT: "Strict — refuse any key not already trusted",
        }
        for mode in host_keys.POLICIES:
            self.host_key_policy.addItem(labels.get(mode, mode), mode)
        try:
            current = host_keys.store().policy
        except Exception:
            current = host_keys.POLICY_PROMPT
        self.host_key_policy.setCurrentIndex(max(0, self.host_key_policy.findData(current)))
        self.host_key_policy.currentIndexChanged.connect(self._set_host_key_policy)
        keys_form.addRow(_label("Unknown host keys"), self.host_key_policy)
        layout.addWidget(keys)
        layout.addStretch(1)
        return page

    def _ping_settings(self) -> dict:
        monitor = getattr(self.window, "reachability", None)
        if monitor is not None:
            return dict(monitor.settings)
        from app.reachability import normalize_ping_settings
        return normalize_ping_settings(self._settings().get("ping_settings"))

    def _set_ping_enabled(self, enabled: bool):
        setter = getattr(self.window, "_set_ping_enabled", None)
        if callable(setter):
            setter(bool(enabled))

    def _apply_ping(self, changes: dict):
        settings = self._ping_settings()
        settings.update(changes)
        apply = getattr(self.window, "_apply_ping_settings", None)
        if callable(apply):
            apply(settings)

    def _set_host_key_policy(self, _index: int):
        from app import host_keys
        try:
            host_keys.store().set_policy(self.host_key_policy.currentData())
        except Exception:
            pass

    # ── Security ──────────────────────────────────────────────────

    def _security_page(self) -> QWidget:
        from app.lock_options import LockOptionsWidget

        page, layout = self._page("Security", "Locking, the master password and trust.")
        group = QGroupBox("Locking")
        lock_layout = QVBoxLayout(group)
        self.lock_options = LockOptionsWidget(self._settings(), group)
        self.auto_lock = self.lock_options.minutes
        self.lock_options.changed.connect(self._set_lock_options)
        lock_layout.addWidget(self.lock_options)
        lock_layout.addWidget(_hint("Screen locking keeps remote sessions connected. Your master password unlocks the app."))
        self.lock_error = _hint("")
        self.lock_error.setStyleSheet("color:#f87171;")
        lock_layout.addWidget(self.lock_error)
        native = getattr(getattr(self.window, "idle_lock", None), "_native", None)
        if native is not None and native.error:
            lock_layout.addWidget(_hint(native.error))
        lock_layout.addWidget(_button("Lock screen now", "lock", self._lock_now))
        layout.addWidget(group)

        vault = QGroupBox("Vault")
        vault_form = _form(vault)
        vault_form.addRow(_label("Master password"), _row(_button(
            "Change…", "key", self.window._change_password)))
        vault_form.addRow(_label("Recovery key"), _row(_button(
            "Manage…", "shield-check", self.window._manage_recovery_key)))
        vault_form.addRow(_label("Passwords"), _row(
            _button("Saved passwords…", "lock", lambda: self.window._show_saved_passwords()),
            _button("Shared credentials…", "users", self.window._show_shared_credentials)))
        layout.addWidget(vault)

        trust = QGroupBox("Web consoles")
        trust_form = _form(trust)
        trust_form.addRow(_label("Certificates"), _row(_button(
            "Trusted certificates…", "shield", self._certificates)))
        trust_form.addRow(_label(""), _hint(
            "Self-signed certificates you chose to trust for iDRAC, NAS and other web "
            "consoles."))
        layout.addWidget(trust)
        layout.addStretch(1)
        return page

    def _set_auto_lock(self, key: str, minutes: int):
        self._set_lock_options({key: int(minutes)})

    def _set_lock_options(self, changes):
        from app.lock_policy import update_window_lock_settings
        try:
            update_window_lock_settings(self.window, changes)
            self.lock_error.setText("")
        except Exception as exc:
            self.lock_error.setText(f"Could not save the lock settings: {exc}")

    def _lock_now(self):
        lock = getattr(self.window, "idle_lock", None)
        if lock is not None:
            self.accept()
            QTimer.singleShot(0, lock.lock_now)

    def _certificates(self):
        from app.browser_trust import manage_certificate_trust
        manage_certificate_trust(self.window._certificate_trust, self)

    # ── Data ──────────────────────────────────────────────────────

    def _data_page(self) -> QWidget:
        from app import vault as vault_module

        page, layout = self._page("Data && backups".replace("&&", "&"),
                                  "Where your data lives, and getting it back.")
        group = QGroupBox("Vault")
        form = _form(group)
        folder = Path(vault_module.VAULT_DIR)
        path = QLabel(str(folder))
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(_label("Stored in"), path)
        form.addRow(_label(""), _row(_button(
            "Open folder", "external", lambda: self._open_folder(folder))))
        layout.addWidget(group)

        backups = QGroupBox("Backups")
        backups_form = _form(backups)
        self.backup_status = QLabel()
        self._refresh_backup_status()
        backups_form.addRow(_label("Last backup"), self.backup_status)
        backups_form.addRow(_label(""), _row(
            _button("Back up now", "download", self._backup_now),
            _button("Backups…", "archive", self.window._show_backups)))
        backups_form.addRow(_label(""), _hint(
            "RPMote backs the vault up once a day and before any import, restore or "
            "bulk delete. The last 20 are kept; backups are encrypted like the vault."))
        layout.addWidget(backups)

        other = QGroupBox("Connections")
        other_form = _form(other)
        other_form.addRow(_label("Deleted"), _row(_button(
            "Recently deleted…", "trash", self.window._show_recently_deleted)))
        other_form.addRow(_label("Move"), _row(
            _button("Export…", "export", self.window._export_connections)))
        other_form.addRow(_label("History"), _row(_button(
            "Session history && time…", "history", self.window._open_session_history)))
        layout.addWidget(other)
        layout.addStretch(1)
        return page

    def _refresh_backup_status(self):
        try:
            from app import backups
            found = backups.list_backups()
        except Exception:
            found = []
        if not found:
            self.backup_status.setText("None yet")
            return
        newest = max(found, key=lambda info: info.created)
        stamp = newest.created.strftime("%Y-%m-%d %H:%M")
        self.backup_status.setText(f"{stamp}  ·  {len(found)} kept")

    def _backup_now(self):
        try:
            from app import backups
            backups.create_backup("manual")
        except Exception as exc:
            self.backup_status.setText(f"Backup failed: {exc}")
            return
        self._refresh_backup_status()

    @staticmethod
    def _open_folder(folder: Path):
        try:
            os.startfile(str(folder))   # noqa: S606 — Explorer on the user's own folder
        except Exception:
            pass

    # ── Advanced ──────────────────────────────────────────────────

    def _advanced_page(self) -> QWidget:
        page, layout = self._page("Advanced", "Rendering, troubleshooting, and starting over.")
        group = QGroupBox("Rendering")
        form = _form(group)
        self.hw_accel = QCheckBox("Use hardware acceleration (GPU)")
        action = getattr(self.window, "act_hw_accel", None)
        self.hw_accel.setChecked(bool(action and action.isChecked()))
        if action is not None:
            self.hw_accel.toggled.connect(action.setChecked)
        form.addRow(_label("Graphics"), self.hw_accel)
        form.addRow(_label(""), _hint(
            "Draws the dashboard and web consoles on the GPU. Turn it off for glitches, "
            "or inside a VM or an RDP session. Takes effect after a restart."))
        layout.addWidget(group)

        tools = QGroupBox("Troubleshooting")
        tools_form = _form(tools)
        tools_form.addRow(_label("Remote Desktop"), _row(_button(
            "Run ActiveX setup…", "monitor", self.window._run_rdp_setup_manual)))
        tools_form.addRow(_label("Web consoles"), _row(_button(
            "Repair browser…", "globe", self.window._repair_webengine)))
        tools_form.addRow(_label("Logs"), _row(_button(
            "Open log folder", "external", self.window._open_log_folder)))
        layout.addWidget(tools)

        danger = QFrame()
        danger.setObjectName("danger_zone")
        danger.setStyleSheet(
            "#danger_zone { border: 1px solid rgba(239, 68, 68, 0.6); border-radius: 8px; }"
            "#danger_zone QLabel { border: none; }")
        danger_layout = QVBoxLayout(danger)
        danger_layout.setContentsMargins(16, 12, 16, 14)
        title = QLabel("Danger zone")
        title.setStyleSheet("color: #f87171; font-weight: 700;")
        danger_layout.addWidget(title)
        danger_layout.addWidget(_hint(
            "Factory reset deletes every connection, password, setting and backup on "
            "this PC, then starts RPMote fresh. It cannot be undone."))
        reset = _button("Factory reset…", "trash", self._factory_reset, "danger")
        danger_layout.addWidget(reset, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(danger)
        layout.addStretch(1)
        return page

    def _factory_reset(self):
        # The reset asks twice and restarts; this dialog should not be in the way.
        self.accept()
        self.window._factory_reset()
