"""
RPMote Main Window — Central application window with tabs, sidebar, and toolbar.
"""
import logging
import os
import subprocess
import sys
import time
from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QTabBar, QVBoxLayout, QHBoxLayout, QWidget,
    QToolBar, QToolButton, QLabel, QPushButton, QLineEdit, QComboBox, QSpinBox,
    QStatusBar, QMenuBar, QMessageBox, QFileDialog, QApplication,
    QStackedLayout, QSizePolicy, QFrame, QGraphicsDropShadowEffect, QCheckBox, QMenu,
    QDialog
)
from PyQt6.QtCore import Qt, QTimer, QPointF, QRectF, QPropertyAnimation, QEasingCurve, QPoint
from PyQt6.QtGui import QAction, QActionGroup, QFont, QKeySequence, QShortcut, QColor, QPainter, QRadialGradient, QBrush, QIcon, QPixmap
from PyQt6 import sip

from app import guardrails, icons, session_log
from app.vault import Vault, VaultError, MIN_MASTER_PASSWORD_LEN
from app.side_panel import SidePanel
from app.snippets_panel import SnippetsPanel
from app.connection_manager import ConnectionDialog
from app.saved_passwords import SavedPasswordsDialog, confirm_master_password
from app.idle_lock import IdleLock, idle_lock_minutes
from app.ssh_terminal import SSHTerminal, TerminalWorkspace
from app.sftp_browser import SFTPBrowser
from app.ftp_browser import FTPBrowser
from app.rdp_launcher import RDPLauncher
from app.web_browser import WebBrowserTab
from app.browser_trust import CertificateTrustStore, manage_certificate_trust
from app.azure_powershell_terminal import AzurePowerShellTerminal
from app.themes import (MOTE_THEME, ALL_THEMES, button_text_color, generate_stylesheet,
                        transparent_container_stylesheet)
from app.rdp_activex_setup import ensure_activex_registered_once
from app.resource_paths import get_resource_path, get_qss_resource_url
from app.version import APP_VERSION
from app.dashboard import DashboardWidget
from app.relationships import LENS_HINTS, LENS_LABELS, LENS_NONE, LENSES
from app.graph_settings import GraphSettingsDialog
from app import hardware_acceleration
from app.crash_logging import log_runtime_event


def _alive(widget) -> bool:
    """False for None and for a widget whose Qt side has been deleted.

    A closed tab's widget is deleted, but a Python reference to it (a popup's
    opener, the session history) can outlast it; handing such a reference to
    Qt raises instead of answering.
    """
    return widget is not None and not sip.isdeleted(widget)


class MainWindow(QMainWindow):
    """Primary application window."""

    def __init__(self, vault: Vault):
        super().__init__()
        self.vault = vault
        self._certificate_trust = CertificateTrustStore(vault)
        # Default to external RDP for stability unless user explicitly enables embed.
        self._rdp_default_embedded = bool(self.vault.settings.get("rdp_open_embedded_default", False))
        self._ssh_terminal_style = self._normalized_ssh_terminal_style(
            self.vault.settings.get("ssh_terminal_style", "System Theme")
        )
        # The face and size the user last picked in a terminal header, so a
        # new tab opens the way they left the previous one.
        self._ssh_terminal_font = SSHTerminal.normalized_font(
            self.vault.settings.get("ssh_terminal_font", "Default Font")
        )
        self._ssh_font_size = SSHTerminal.normalized_font_size(
            self.vault.settings.get("ssh_terminal_font_size",
                                    SSHTerminal.DEFAULT_FONT_SIZE)
        )
        # Tenants the user has marked production. Terminals opened under one
        # wear a badge and ask before running anything destructive.
        self._production_tenants = guardrails.normalize_production_tenants(
            self.vault.settings.get("production_tenants", []))
        self._themes = list(ALL_THEMES)
        saved_idx = int(self.vault.settings.get("theme_index", 0))
        self._current_theme_index = saved_idx if 0 <= saved_idx < len(self._themes) else 0
        self._current_theme = self._themes[self._current_theme_index]
        self.setWindowTitle("RPMote — Remote Connection Manager")
        self.setMinimumSize(1100, 700)
        self.resize(1400, 850)

        # Set Window Icon
        icon_path = get_resource_path("app", "resources", "icons", "app_icon.ico")
        self.setWindowIcon(QIcon(icon_path))

        # Cover the window after a stretch of no input; sessions keep running.
        self.idle_lock = IdleLock(self.vault, self, idle_lock_minutes(self.vault.settings), settings=self.vault.settings)
        self.idle_lock.locked.connect(lambda: self._set_ssh_windows_visible(False))
        self.idle_lock.unlocked.connect(lambda: self._set_ssh_windows_visible(True))
        # When each session was open — Recents and the time-per-tenant report.
        from app.session_history import SessionHistory
        self.session_history = SessionHistory(self.vault, self)
        self._history_widgets: dict = {}
        # Closed tabs' widgets waiting to be deleted (see _free_closed_tabs).
        self._closed_tab_widgets: list = []
        self._free_tabs_timer = QTimer(self)
        self._free_tabs_timer.setSingleShot(True)
        self._free_tabs_timer.setInterval(500)
        self._free_tabs_timer.timeout.connect(self._free_closed_tabs)
        # Per-tenant "connect the VPN first" (sidebar › tenant › Connect VPN first).
        from app.vpn import VpnGate
        self.vpn_gate = VpnGate(self)
        self._before_open = self.vpn_gate.before_open
        # A Remote Desktop window closing is a session ending too.
        self.session_history.ended.connect(lambda _token: self.vpn_gate.sessions_changed())

        # Lumina: Background Aura Setup
        self.central_container = QWidget()
        self.setCentralWidget(self.central_container)
        self.stack = QStackedLayout(self.central_container)
        self.stack.setStackingMode(QStackedLayout.StackingMode.StackAll)

        self.aura = BackgroundAura(self)
        self.stack.addWidget(self.aura)

        self.main_content = QWidget()
        # Transparent so the aura shows through; see
        # themes.transparent_container_stylesheet for why it is not just
        # "background: transparent;" any more. Set again on theme changes.
        self.main_content.setStyleSheet(
            transparent_container_stylesheet(self._current_theme))
        self.stack.addWidget(self.main_content)
        self.stack.setCurrentWidget(self.main_content)
        
        self.main_layout = QVBoxLayout(self.main_content)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        self._setup_menu()
        self._setup_toolbar()
        self._setup_side_panel()
        self._setup_snippets_panel()
        self._setup_tabs()
        self._setup_status_bar()
        self._setup_shortcuts()
        self._apply_theme()
        self._refresh_sidebar()
        self._show_welcome()
        self._start_power_monitor()
        # Sweeping recordings touches the disk, so it waits until the window
        # is up rather than sitting between the user and their estate.
        QTimer.singleShot(3000, self._sweep_old_recordings)
        QTimer.singleShot(4000, self._daily_backup)

    def _daily_backup(self):
        try:
            from app import backups
            backups.daily_backup_if_due()
        except Exception:
            logging.getLogger("rpmote").exception("daily vault backup failed")

    # ── Menu Bar ────────────────────────────────────────────────────

    def _setup_menu(self):
        mb = self.menuBar()

        # File
        file_menu = mb.addMenu("&File")
        goto = QAction(icons.icon("search"), "&Go to…", self, triggered=self._show_goto_palette)
        goto.setShortcuts([QKeySequence("Ctrl+P"), QKeySequence("Ctrl+Shift+Space")])
        goto.setToolTip("Find any connection, tenant or command (Ctrl+P)")
        file_menu.addAction(goto)
        file_menu.addAction(QAction(icons.icon("plus"), "&New Connection...", self,
            shortcut="Ctrl+N", triggered=self._new_connection))
        file_menu.addSeparator()
        import_menu = file_menu.addMenu(icons.icon("import"), "&Import")
        import_menu.addAction(QAction("RPMote &export (.rpvault)…", self,
            triggered=self._import_connections))
        import_menu.addAction(QAction(icons.icon("monitor"), "Remote Desktop &files (.rdp)…", self,
            triggered=lambda: self._import_from("rdp")))
        import_menu.addAction(QAction("&CSV (RDM, Royal TS, spreadsheets)…", self,
            triggered=lambda: self._import_from("csv")))
        import_menu.addAction(QAction("&mRemoteNG (confCons.xml)…", self,
            triggered=lambda: self._import_from("mremoteng")))
        import_menu.addAction(QAction(icons.icon("terminal"), "&PuTTY saved sessions", self,
            triggered=lambda: self._import_from("putty")))
        import_menu.addAction(QAction(icons.icon("terminal"), "~/.ssh/&config…", self,
            triggered=self._import_ssh_config))
        file_menu.addAction(QAction(icons.icon("export"), "&Export Connections...", self,
            triggered=self._export_connections))
        file_menu.addSeparator()
        file_menu.addAction(QAction("Saved &Passwords...", self,
            shortcut="Ctrl+Shift+P",
            triggered=lambda: self._show_saved_passwords()))
        file_menu.addAction(QAction(icons.icon("users"), "Shared &Credentials…", self,
            triggered=self._show_shared_credentials))
        file_menu.addAction(QAction(icons.icon("trash"), "Recently &Deleted…", self,
            triggered=self._show_recently_deleted))
        file_menu.addAction(QAction(icons.icon("archive"), "&Backups…", self,
            triggered=self._show_backups))
        file_menu.addSeparator()
        file_menu.addAction(QAction(icons.icon("key"), "Change &Master Password...", self,
            triggered=self._change_password))
        file_menu.addAction(QAction(icons.icon("shield-check"), "&Recovery Key...", self,
            triggered=self._manage_recovery_key))
        settings = QAction(icons.icon("settings"), "Se&ttings…", self,
                           triggered=lambda: self._show_settings())
        settings.setShortcut(QKeySequence("Ctrl+,"))
        file_menu.addAction(settings)
        file_menu.addSeparator()
        file_menu.addAction(QAction(icons.icon("lock"), "Lock &Screen Now", self,
            shortcut="Ctrl+Shift+L", triggered=lambda: self.idle_lock.lock_now()))
        from app.lock_options import LockOptionsMenu
        self.lock_options_menu = LockOptionsMenu(self)
        file_menu.addMenu(self.lock_options_menu)
        file_menu.addAction(QAction("&Lock Vault", self,
            shortcut="Ctrl+L", triggered=self._lock_vault))
        file_menu.addAction(QAction("E&xit", self,
            shortcut="Ctrl+Q", triggered=self._exit_app))

        # View
        view_menu = mb.addMenu("&View")
        self.act_quick_connect = QAction("Quick &Connect", self, checkable=True,
                                        shortcut="Ctrl+Shift+N")
        self.act_quick_connect.setChecked(
            bool(self.vault.settings.get("quick_connect_expanded", False)))
        view_menu.addAction(self.act_quick_connect)
        view_menu.addAction(QAction("Toggle &Sidebar", self,
            shortcut="Ctrl+B", triggered=self._toggle_sidebar))
        view_menu.addAction(QAction(icons.icon("library"), "Toggle &Library", self,
            shortcut="Ctrl+Shift+S", triggered=self._toggle_snippets))
        view_menu.addAction(QAction("Next &Theme", self,
            shortcut="Ctrl+T", triggered=self._toggle_theme))
        theme_menu = view_menu.addMenu("T&heme")
        self._theme_actions = []
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        for i, theme in enumerate(self._themes):
            act = QAction(theme["name"], self, checkable=True)
            if theme.get("dashboard_theme"):
                act.setToolTip("Animated dashboard background")
            act.triggered.connect(lambda _checked, idx=i: self._select_theme(idx))
            theme_group.addAction(act)
            theme_menu.addAction(act)
            self._theme_actions.append(act)
        self._theme_actions[self._current_theme_index].setChecked(True)
        view_menu.addSeparator()
        # Reflects the *saved* preference; the running process keeps whatever
        # it started with until restarted (see app.hardware_acceleration).
        self.act_hw_accel = QAction("Use &Hardware Acceleration (GPU)", self,
                                    checkable=True)
        self.act_hw_accel.setChecked(hardware_acceleration.is_saved_enabled())
        self.act_hw_accel.setToolTip(
            "Render the dashboard and embedded browsers on the GPU.\n"
            "Turn off if you see glitches or run without a GPU (VMs, RDP).\n"
            "Takes effect after a restart.")
        self.act_hw_accel.toggled.connect(self._on_hw_accel_toggled)
        view_menu.addAction(self.act_hw_accel)
        # The app-wide RDP default used to live only on the Quick Connect bar,
        # hidden whenever that bar was folded away.
        self.act_rdp_embedded = QAction("Open RDP Inside &Tabs", self, checkable=True)
        self.act_rdp_embedded.setChecked(self._rdp_default_embedded)
        self.act_rdp_embedded.setToolTip(
            "Where RDP connections set to \"App default\" open: a tab inside "
            "RPMote, or their own Remote Desktop window.")
        view_menu.addAction(self.act_rdp_embedded)
        view_menu.addSeparator()
        view_menu.addAction(QAction("Dashboard Zoom &In", self,
            shortcut="Ctrl+=", triggered=self._dashboard_zoom_in))
        view_menu.addAction(QAction("Dashboard Zoom &Out", self,
            shortcut="Ctrl+-", triggered=self._dashboard_zoom_out))
        view_menu.addAction(QAction("Dashboard Zoom &Reset", self,
            shortcut="Ctrl+0", triggered=self._dashboard_zoom_reset))

        # Session
        session_menu = mb.addMenu("&Session")
        session_menu.addAction(QAction("&Run a Command…", self,
            shortcut="Ctrl+K", triggered=self._open_command_palette))
        session_menu.addAction(QAction(icons.icon("broadcast"), "&Broadcast Session…", self,
            shortcut="Ctrl+Shift+B",
            triggered=lambda: self._open_broadcast_session()))
        session_menu.addSeparator()
        session_menu.addAction(QAction(icons.icon("record"), "Session &Recordings…", self,
            triggered=self._open_session_recordings))
        session_menu.addAction(QAction(icons.icon("history"), "Session &History && Time…", self,
            triggered=self._open_session_history))

        # Graph
        self._build_graph_menu(mb)

        # Help
        help_menu = mb.addMenu("&Help")
        help_menu.addAction(QAction(icons.icon("keyboard"), "&Keyboard Shortcuts", self,
            triggered=self._show_shortcuts))
        trouble = help_menu.addMenu(icons.icon("help"), "&Troubleshooting")
        trouble.addAction(QAction(icons.icon("monitor"), "Run RDP &ActiveX Setup…", self,
            triggered=self._run_rdp_setup_manual))
        trouble.addAction(QAction(icons.icon("globe"), "Repair &Web Console Browser…", self,
            triggered=self._repair_webengine))
        trouble.addAction(QAction(icons.icon("shield"), "Trusted Web &Certificates…", self,
            triggered=lambda: manage_certificate_trust(self._certificate_trust, self)))
        trouble.addAction(QAction(icons.icon("external"), "Open &Log Folder", self,
            triggered=self._open_log_folder))
        help_menu.addSeparator()
        help_menu.addAction(QAction(icons.icon("info"), "&About RPMote", self,
            triggered=self._show_about))

    # ── Graph Menu ──────────────────────────────────────────────────

    def _build_graph_menu(self, mb):
        """What you do with the dashboard graph; how it looks is one level down.

        It used to be one flat list of thirty-odd items — on a 768-px-high
        screen Qt had to wrap it into two columns.
        """
        menu = mb.addMenu("&Graph")
        self._graph_actions = {}
        self.act_show_overview = QAction(icons.icon("home"), "Show &Overview", self,
                                         checkable=True)
        self.act_show_overview.setChecked(True)
        self.act_show_overview.setToolTip(
            "The card over the graph with what needs attention, favourites and recents")
        self.act_show_overview.triggered.connect(self._set_home_visible)
        menu.addAction(self.act_show_overview)
        menu.addSeparator()

        menu.addAction(QAction(icons.icon("search"), "Fin&d Node…\tCtrl+F", self,
                               triggered=self._dashboard_find))
        menu.addAction(QAction(icons.icon("zoom-fit"), "&Fit Graph to View", self,
                               shortcut="Ctrl+Shift+F", triggered=self._dashboard_fit))
        menu.addAction(QAction("&Reset View", self, shortcut="Ctrl+Shift+R",
                               triggered=self._dashboard_reset_view))
        menu.addSeparator()
        menu.addAction(QAction("&Open Every Tenant", self,
                               triggered=self._expand_all_tenants))
        menu.addAction(QAction("Co&llapse Every Tenant", self,
                               triggered=self._collapse_all_tenants))

        lens_menu = menu.addMenu(icons.icon("link"), "Relationship &Lens")
        self._lens_actions = {}
        lens_group = QActionGroup(self)
        lens_group.setExclusive(True)
        for kind in LENSES:
            act = QAction(LENS_LABELS[kind], self, checkable=True)
            act.setToolTip(LENS_HINTS[kind])
            act.triggered.connect(
                lambda _checked, k=kind: self._set_relation_lens(k))
            lens_group.addAction(act)
            lens_menu.addAction(act)
            self._lens_actions[kind] = act
        self._lens_actions[LENS_NONE].setChecked(True)

        menu.addSeparator()
        self.act_ping = QAction("&Ping Connections", self, checkable=True,
                                triggered=self._toggle_ping)
        menu.addAction(self.act_ping)
        menu.addAction(QAction(icons.icon("activity"), "Check All &Now", self,
                               shortcut="Ctrl+Shift+H",
                               triggered=lambda: self._check_reachability_now([])))
        menu.addAction(QAction(icons.icon("power"), "&Wake All  (Wake-on-LAN)", self,
                               triggered=lambda: self._wake_connections([])))
        menu.addAction(QAction(icons.icon("restart"), "Restart Ser&vers…", self,
                               triggered=self._open_restart_manager))

        menu.addSeparator()
        look = menu.addMenu(icons.icon("palette"), "&Appearance")
        layout_menu = look.addMenu("&Layout")
        self._graph_group(layout_menu, "layout_mode", [
            ("hub", "&Hub — RPMote orb in the centre"),
            ("free", "&Free Floating — no centre"),
        ])
        arrange_menu = look.addMenu("&Arrangement")
        self._graph_group(arrange_menu, "free_arrangement", [
            ("grid", "&Grid"),
            ("scatter", "Organic &Scatter"),
        ])
        self._graph_arrange_menu = arrange_menu
        line_menu = look.addMenu("Line &Style")
        self._graph_group(line_menu, "line_style", [
            ("curved", "&Curved"),
            ("straight", "S&traight"),
            ("elbow", "&Elbow (right angles)"),
        ])
        label_menu = look.addMenu("Branch La&bels")
        self._graph_group(label_menu, "branch_labels", [
            ("name", "Connection &Name"),
            ("name_host", "Name and &Host"),
            ("host", "&Host Only"),
            ("none", "&None — dots only"),
        ])
        tile_menu = look.addMenu("&Tenant Tiles")
        self._graph_group(tile_menu, "tile_detail", [
            ("auto", "&Auto — compact while branches are shown"),
            ("full", "&Full — name, count and icons"),
            ("compact", "&Compact — one row"),
        ])
        background_menu = look.addMenu("Bac&kground")
        self._graph_group(background_menu, "background", [
            ("calm", "&Calm — the graph stands out"),
            ("balanced", "&Balanced"),
            ("vivid", "&Vivid — full-strength scene"),
            ("plain", "&Plain — no animated scene"),
        ])
        look.addSeparator()
        self._graph_toggle(look, "show_branches", "Show &Connection Branches")
        self._graph_toggle(look, "show_hub_spokes", "Show Hub &Spokes")
        self._graph_toggle(look, "particles", "Animated &Particles")
        self._graph_toggle(look, "animate", "&Animation")
        self._graph_toggle(look, "show_status", "Reachability &Dots")

        arrange = menu.addMenu(icons.icon("grid"), "Arrange && &Link")
        arrange.addAction(QAction("&Tidy Up Layout", self,
                                  triggered=self._tidy_layout))
        arrange.addAction(QAction("Reset Node &Positions", self,
                                  triggered=self._dashboard_reset_positions))
        arrange.addSeparator()
        self.act_link_mode = QAction("Link &Mode  (draw links between nodes)", self,
                                     checkable=True,
                                     triggered=self._toggle_link_mode)
        self.act_link_mode.setShortcut("Ctrl+Shift+G")
        arrange.addAction(self.act_link_mode)
        arrange.addAction(QAction("Clear All &Links", self,
                                  triggered=self._clear_tenant_links))

        menu.addSeparator()
        menu.addAction(QAction(icons.icon("settings"), "&Graph Settings…", self,
                               triggered=self._open_graph_settings))

        self._graph_menu = menu

    def _graph_group(self, menu, key: str, choices: list):
        """Add a mutually-exclusive set of options for one graph setting."""
        from PyQt6.QtGui import QActionGroup
        group = QActionGroup(self)
        group.setExclusive(True)
        for value, label in choices:
            act = QAction(label, self, checkable=True)
            act.setData(value)
            act.triggered.connect(
                lambda _checked, k=key, v=value: self._set_graph_option(k, v))
            group.addAction(act)
            menu.addAction(act)
            self._graph_actions[(key, value)] = act

    def _graph_toggle(self, menu, key: str, label: str):
        act = QAction(label, self, checkable=True)
        act.triggered.connect(
            lambda checked, k=key: self._set_graph_option(k, bool(checked)))
        menu.addAction(act)
        self._graph_actions[(key, None)] = act

    def _expand_all_tenants(self):
        if hasattr(self, "dashboard"):
            restored = self.dashboard.expand_all_tenants()
            if not restored:
                # Frame the estate unless returning from a temporary tenant view.
                QTimer.singleShot(0, self.dashboard.fit_to_view)

    def _collapse_all_tenants(self):
        if hasattr(self, "dashboard"):
            self.dashboard.collapse_all_tenants()
            self.status.showMessage(
                "Tenants collapsed — click one to open it", 4000)

    def _tidy_layout(self):
        """Snap the free-floating layout back onto its grid."""
        if not hasattr(self, "dashboard"):
            return
        if not self.dashboard.is_free_mode:
            self.status.showMessage(
                "Tidy Up applies to the free-floating layout", 4000)
            return
        moved = self.dashboard.tidy_layout()
        self.dashboard.fit_to_view()
        self.status.showMessage(
            f"Tidied {moved} tenant tile{'' if moved == 1 else 's'}"
            if moved else "Layout is already tidy", 4000)

    def _set_relation_lens(self, kind: str):
        """Point a relationship lens at the estate, or clear it."""
        if hasattr(self, "dashboard"):
            self.dashboard.set_relation_lens(kind)
            self.dashboard.setFocus()

    def _on_relation_lens_changed(self, kind: str):
        """Keep the menu honest when the lens is changed from the canvas."""
        act = getattr(self, "_lens_actions", {}).get(kind)
        if act is not None:
            # Not blockSignals(): the exclusive QActionGroup unticks the old
            # lens off the back of this, and muting the action leaves two
            # lenses showing as active at once.  setChecked only emits
            # toggled(), never triggered(), so this cannot loop.
            act.setChecked(True)
        if kind == LENS_NONE:
            self.status.showMessage("Relationship lens cleared", 3000)
            return
        view = self.dashboard.relation_view(kind)
        self.status.showMessage(
            view.summary() if not view.empty
            else f"{LENS_LABELS.get(kind, kind)} — nothing found", 6000)

    def _set_graph_option(self, key: str, value):
        if hasattr(self, "dashboard"):
            self.dashboard.set_graph_option(key, value)

    def _sync_graph_menu(self, gs: dict | None = None):
        """Mirror the dashboard's current options back into the menu."""
        if not hasattr(self, "dashboard") or not hasattr(self, "_graph_actions"):
            return
        gs = gs or self.dashboard.get_graph_settings()
        for (key, value), act in self._graph_actions.items():
            act.blockSignals(True)
            if value is None:
                act.setChecked(bool(gs.get(key)))
            else:
                act.setChecked(gs.get(key) == value)
            act.blockSignals(False)
        free = gs.get("layout_mode") == "free"
        if hasattr(self, "_graph_arrange_menu"):
            self._graph_arrange_menu.setEnabled(free)
        spokes = self._graph_actions.get(("show_hub_spokes", None))
        if spokes is not None:
            spokes.setEnabled(not free)
        dots = self._graph_actions.get(("show_status", None))
        if dots is not None:
            dots.setEnabled(self.dashboard.is_ping_enabled)
        if hasattr(self, "act_link_mode"):
            self.act_link_mode.blockSignals(True)
            self.act_link_mode.setChecked(self.dashboard.is_link_mode())
            self.act_link_mode.blockSignals(False)
        if hasattr(self, "act_ping"):
            monitor = getattr(self, "reachability", None)
            self.act_ping.blockSignals(True)
            self.act_ping.setChecked(bool(monitor and monitor.enabled))
            self.act_ping.blockSignals(False)
        if hasattr(self, "_graph_dialog") and self._graph_dialog is not None:
            self._graph_dialog.load_from(gs)

    # ── Graph Actions ───────────────────────────────────────────────

    def _toggle_link_mode(self, checked: bool):
        if hasattr(self, "dashboard"):
            self.dashboard.set_link_mode(bool(checked))
            self.dashboard.setFocus()
            if checked:
                self.status.showMessage(
                    "Link mode: click a tenant, then another, to draw a visual "
                    "link. Esc to cancel.", 6000)

    def _on_link_mode_changed(self, enabled: bool):
        if hasattr(self, "act_link_mode"):
            self.act_link_mode.blockSignals(True)
            self.act_link_mode.setChecked(bool(enabled))
            self.act_link_mode.blockSignals(False)

    def _clear_tenant_links(self):
        if not hasattr(self, "dashboard"):
            return
        if not self.dashboard.get_tenant_links():
            self.status.showMessage("No custom tenant links to clear", 3000)
            return
        confirm = QMessageBox.question(
            self, "Clear Tenant Links",
            "Remove every link you have drawn between tenants?\n"
            "This only changes the picture — no connection is affected.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirm == QMessageBox.StandardButton.Yes:
            self.dashboard.clear_tenant_links()

    def _dashboard_find(self):
        """Open the graph's spotlight search, focusing the canvas first."""
        if hasattr(self, "dashboard"):
            self.dashboard.setFocus()
            self.dashboard.open_search()

    def _dashboard_fit(self):
        if hasattr(self, "dashboard"):
            self.dashboard.fit_to_view()

    def _dashboard_reset_view(self):
        if hasattr(self, "dashboard"):
            self.dashboard.reset_view()

    def _dashboard_reset_positions(self):
        if hasattr(self, "dashboard"):
            self.dashboard.reset_all_positions()
            self.status.showMessage("Graph node positions reset", 3000)

    def _open_graph_settings(self):
        if not hasattr(self, "dashboard"):
            return
        if getattr(self, "_graph_dialog", None) is not None:
            self._graph_dialog.raise_()
            self._graph_dialog.activateWindow()
            return
        monitor = getattr(self, "reachability", None)
        dialog = GraphSettingsDialog(
            self.dashboard, parent=self,
            ping_settings=monitor.settings if monitor is not None else None)
        if monitor is not None:
            dialog.ping_settings_changed.connect(self._apply_ping_settings)
            dialog.check_now_requested.connect(
                lambda: self._check_reachability_now([]))
        self._graph_dialog = dialog
        dialog.finished.connect(lambda _r: setattr(self, "_graph_dialog", None))
        dialog.show()

    # ── Toolbar ─────────────────────────────────────────────────────

    def _setup_toolbar(self):
        # Utilities live beside the menus, independent of the expandable form.
        self.header_controls = QWidget(self)
        header = QHBoxLayout(self.header_controls)
        header.setContentsMargins(0, 0, 8, 0)
        header.setSpacing(6)

        self.btn_quick_connect = QToolButton()
        self.btn_quick_connect.setDefaultAction(self.act_quick_connect)
        self.btn_quick_connect.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.btn_quick_connect.setArrowType(
            Qt.ArrowType.DownArrow if self.act_quick_connect.isChecked()
            else Qt.ArrowType.RightArrow)
        self.btn_quick_connect.setFixedHeight(32)
        self.btn_quick_connect.setToolTip("Show / hide Quick Connect (Ctrl+Shift+N)")
        self.btn_quick_connect.setAccessibleName("Toggle Quick Connect")
        header.addWidget(self.btn_quick_connect)
        header.addSpacing(6)

        self.btn_goto = self._header_icon_button(
            "search", "Go to a connection, tenant or command (Ctrl+P)",
            "Go to", self._show_goto_palette)
        header.addWidget(self.btn_goto)
        self.btn_theme = self._header_icon_button(
            "palette", "Next theme (Ctrl+T) — View › Theme to pick one",
            "Change theme", self._toggle_theme)
        self.btn_sidebar = self._header_icon_button(
            "sidebar", "Toggle Connections sidebar (Ctrl+B)",
            "Toggle Connections sidebar", self._toggle_sidebar)
        self.btn_library = self._header_icon_button(
            "library", "Library: snippets, commands and files (Ctrl+Shift+S)",
            "Toggle Library panel", self._toggle_snippets)
        self.btn_screenshot = self._header_icon_button(
            "camera", "Take a Screenshot", "Take a screenshot",
            self._show_screenshot_menu)
        self.btn_settings = self._header_icon_button(
            "settings", "Settings (Ctrl+,)", "Settings", lambda: self._show_settings())
        for button in (self.btn_theme, self.btn_sidebar, self.btn_library,
                       self.btn_screenshot, self.btn_settings):
            header.addWidget(button)
        self.menuBar().setCornerWidget(self.header_controls, Qt.Corner.TopRightCorner)

        toolbar = self.quick_connect_toolbar = QToolBar("Quick Connect", self)
        toolbar.setObjectName("quick_connect_toolbar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setAllowedAreas(Qt.ToolBarArea.TopToolBarArea)
        self.addToolBar(toolbar)

        form = QWidget()
        form.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row = QHBoxLayout(form)
        row.setContentsMargins(4, 2, 4, 2)
        row.setSpacing(8)

        self.quick_host = QLineEdit()
        self.quick_host.setPlaceholderText("hostname or IP")
        self.quick_host.setAccessibleName("Quick Connect host")
        self.quick_host.setMinimumWidth(160)
        self.quick_host.setFixedHeight(32)
        row.addWidget(self.quick_host, 2)

        self.quick_protocol = QComboBox()
        for label in self.QUICK_PROTOCOLS:
            self.quick_protocol.addItem(
                icons.protocol_icon(ConnectionDialog.PROTOCOL_KEYS[label]), label)
        self.quick_protocol.setAccessibleName("Quick Connect protocol")
        self.quick_protocol.setFixedHeight(32)
        self.quick_protocol.setMinimumWidth(136)
        self.quick_protocol.currentTextChanged.connect(self._on_quick_proto_change)
        row.addWidget(self.quick_protocol)

        port_label = QLabel("Port:")
        row.addWidget(port_label)
        self.quick_port = QSpinBox()
        self.quick_port.setRange(1, 65535)
        self.quick_port.setValue(22)
        self.quick_port.setAccessibleName("Quick Connect port")
        self.quick_port.setFixedHeight(32)
        self.quick_port.setFixedWidth(64)
        self.quick_port.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.quick_port.setAlignment(Qt.AlignmentFlag.AlignCenter)
        port_label.setBuddy(self.quick_port)
        row.addWidget(self.quick_port)

        self.quick_user = QLineEdit()
        self.quick_user.setPlaceholderText("username")
        self.quick_user.setAccessibleName("Quick Connect username")
        self.quick_user.setMinimumWidth(100)
        self.quick_user.setMaximumWidth(180)
        self.quick_user.setFixedHeight(32)
        self.quick_user.returnPressed.connect(self._quick_connect)
        row.addWidget(self.quick_user, 1)

        self.quick_pass = QLineEdit()
        self.quick_pass.setPlaceholderText("password")
        self.quick_pass.setAccessibleName("Quick Connect password")
        self.quick_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.quick_pass.setMinimumWidth(100)
        self.quick_pass.setMaximumWidth(180)
        self.quick_pass.setFixedHeight(32)
        self.quick_pass.returnPressed.connect(self._quick_connect)
        row.addWidget(self.quick_pass, 1)

        btn_connect = self.btn_quick_go = QPushButton("Connect")
        btn_connect.setIcon(icons.icon("bolt", "#ffffff"))  # re-inked in _apply_theme
        from app.widgets import set_variant
        set_variant(btn_connect, "primary")
        btn_connect.setFixedHeight(32)
        btn_connect.clicked.connect(self._quick_connect)
        self.quick_host.returnPressed.connect(self._quick_connect)
        row.addWidget(btn_connect)

        self.rdp_mode_toggle = QCheckBox("Embed RDP in app")
        self.rdp_mode_toggle.setChecked(self._rdp_default_embedded)
        self.rdp_mode_toggle.setToolTip(
            "Default for RDP connections set to \"App default\" (and Quick Connect).\n"
            "Off: RDP opens in its own Remote Desktop window.\n"
            "Same as View › Open RDP Inside Tabs.")
        self.rdp_mode_toggle.toggled.connect(self._on_rdp_mode_toggled)
        self.rdp_mode_toggle.toggled.connect(self.act_rdp_embedded.setChecked)
        self.act_rdp_embedded.toggled.connect(self.rdp_mode_toggle.setChecked)
        row.addWidget(self.rdp_mode_toggle)
        toolbar.addWidget(form)
        toolbar.setVisible(self.act_quick_connect.isChecked())
        self.act_quick_connect.toggled.connect(self._set_quick_connect_expanded)
        # Keep the native toolbar context menu in sync with the header toggle.
        toolbar.toggleViewAction().triggered.connect(self.act_quick_connect.setChecked)
        QShortcut(QKeySequence("Esc"), form,
                  activated=lambda: self.act_quick_connect.setChecked(False),
                  context=Qt.ShortcutContext.WidgetWithChildrenShortcut)

    def _set_quick_connect_expanded(self, expanded: bool):
        self.quick_connect_toolbar.setVisible(expanded)
        self.btn_quick_connect.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        if expanded:
            self.quick_host.setFocus(Qt.FocusReason.ShortcutFocusReason)
        try:
            self.vault.update_settings({"quick_connect_expanded": expanded})
        except VaultError:
            pass

    def _header_icon_button(self, icon: str, tooltip: str, name: str, callback):
        from app.widgets import icon_button
        button = icon_button(icon, tooltip, size=32, icon_size=17)
        button.setAccessibleName(name)
        button.clicked.connect(callback)
        return button

    def _show_screenshot_menu(self):
        menu = QMenu(self)
        menu.addAction(icons.icon("monitor"), "Full window", self._screenshot_full_window)
        menu.addAction(icons.icon("sidebar"), "Active connection", self._screenshot_active_tab)
        menu.addAction(icons.icon("zoom-fit"), "Select area…", self._screenshot_select_area)
        menu.exec(self.btn_screenshot.mapToGlobal(
            QPoint(0, self.btn_screenshot.height())))

    # ── Side Panel ──────────────────────────────────────────────────

    def _setup_side_panel(self):
        self.side_panel = SidePanel(self)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.side_panel)
        self.side_panel.set_theme(self._current_theme)
        self.side_panel.connect_requested.connect(self._open_connection)
        self.side_panel.edit_requested.connect(self._edit_connection)
        self.side_panel.show_password_requested.connect(self._show_connection_password)
        self.side_panel.delete_requested.connect(self._delete_connection)
        self.side_panel.disconnect_requested.connect(self._disconnect_connection)
        self.side_panel.reconnect_requested.connect(self._reconnect_connection)
        self.side_panel.wake_requested.connect(
            lambda conn: self._wake_connections([str(conn.get("id", ""))]))
        self.side_panel.new_connection_for_tenant_requested.connect(self._new_connection_for_tenant)
        self.side_panel.connections_reordered.connect(self._reorder_connections)
        self.side_panel.tenant_color_changed.connect(self._on_tenant_color_changed)
        self.side_panel.delete_tenant_requested.connect(self._delete_tenant)
        self.side_panel.tenant_focus_requested.connect(self._on_sidebar_tenant_focus_requested)
        self.side_panel.connection_focus_requested.connect(self._on_sidebar_connection_focus_requested)
        self.side_panel.tenant_close_requested.connect(self._on_sidebar_tenant_close_requested)
        self.side_panel.production_tenant_toggled.connect(self._set_tenant_production)
        self.side_panel.broadcast_tenant_requested.connect(self._open_broadcast_session)
        self.side_panel.set_production_tenants(self._production_tenants)
        self.side_panel.btn_add.clicked.connect(self._new_connection)
        self.side_panel.favorite_toggled.connect(self._set_favorite)
        self.side_panel.duplicate_requested.connect(self._duplicate_connection)
        self.side_panel.open_many_requested.connect(self._open_many)
        self.side_panel.delete_many_requested.connect(self._delete_many)
        self.side_panel.move_requested.connect(self._move_connections)
        self.side_panel.wake_many_requested.connect(
            lambda ids: self._wake_connections(list(ids)))
        self.side_panel.check_requested.connect(
            lambda ids: self._check_reachability_now(list(ids)))
        self.side_panel.rename_tenant_requested.connect(self._rename_tenant)
        self.side_panel.tenant_notes_requested.connect(self._edit_tenant_notes)
        self.side_panel.tenant_vpn_requested.connect(self._configure_tenant_vpn)
        self.side_panel.density_changed.connect(self._save_sidebar_density)
        try:
            self.side_panel.set_density(self.vault.settings.get("sidebar_density", "comfortable"))
        except VaultError:
            pass

    def _setup_snippets_panel(self):
        self.snippets_panel = SnippetsPanel(self.vault, self)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.snippets_panel)
        self.snippets_panel.execute_requested.connect(self._execute_snippet)
        self.snippets_panel.hide()  # Collapsed by default

    def _execute_snippet(self, command: str):
        """Execute snippet in the active terminal tab."""
        widget = self.tabs.currentWidget()

        from app.command_palette import fill_command, session_context
        if "{" in str(command or ""):
            # A snippet that mentions {host} is the whole point of snippets;
            # sending the braces to the shell would be the wrong answer.
            command = fill_command(command, session_context(widget), parent=self)
            if not command:
                return

        if hasattr(widget, "run_command"):
            # Through run_command, not straight down the wire: a snippet
            # arrives complete and runs in one keystroke, which is exactly
            # when the guardrails are worth having.
            widget.run_command(command)
        elif hasattr(widget, "_send_data"):
            widget._send_data(command)
        else:
            self.status.showMessage("The active tab is not a terminal, so it cannot run commands", 4000)

    # ── Tier 3: palette, broadcast, recordings, production ───────────

    def _palette_entries(self):
        """Everything Ctrl+K can run, newest snippets first."""
        from app.command_palette import palette_entries
        return palette_entries(self.vault)

    def _open_command_palette(self):
        """Ctrl+K: run something in the tab that has focus."""
        widget = self.tabs.currentWidget()
        if hasattr(widget, "open_command_palette"):
            widget.open_command_palette()
            return

        from app.command_palette import CommandPalette
        palette = CommandPalette(self._palette_entries(), parent=self)
        palette.command_chosen.connect(self._execute_snippet)
        palette.exec()

    def _open_broadcast_session(self, tenant: str = ""):
        """Pick machines in the estate and open them as one synced grid."""
        from app.host_picker import HostPickerDialog, shell_connections

        connections = shell_connections(self.vault.connections)
        if not connections:
            QMessageBox.information(
                self, "Broadcast session",
                "There are no SSH connections saved yet.")
            return

        preselect = [conn for conn in connections
                     if tenant and str(conn.get("tenant") or "Default") == tenant]
        dialog = HostPickerDialog(connections, self._production_tenants,
                                  preselect=preselect, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.chosen()
        if not chosen:
            return

        first = self._resolve_route(self._with_credentials(dict(chosen[0])))
        workspace = TerminalWorkspace(
            first,
            theme=self._current_theme,
            terminal_style=self._ssh_terminal_style,
            terminal_font=self._ssh_terminal_font,
            font_size=self._ssh_font_size,
            production_tenants=self._production_tenants,
            snippet_provider=self._palette_entries,
        )
        rest = []
        for conn in chosen[1:]:
            rest.append(self._resolve_route(self._with_credentials(dict(conn))))
        if rest:
            workspace.open_hosts(rest)

        label = tenant or f"{len(chosen)} hosts"
        index = self.tabs.addTab(workspace, icons.icon("broadcast", "#f59e0b"), label)
        self.tabs.setTabToolTip(index, f"Broadcast session — {len(chosen)} machines")
        self.tabs.setCurrentWidget(workspace)
        workspace.session_closed.connect(self._on_session_closed)
        workspace.style_changed.connect(self._save_ssh_terminal_style)
        workspace.font_changed.connect(self._save_ssh_terminal_font)
        for pane in workspace.terminals():
            pane.connection_established.connect(self._on_ssh_connected)
        if dialog.arm_immediately():
            workspace.broadcast.set_armed(True)
        self.status.showMessage(
            f"Broadcast session opened on {len(chosen)} machines", 5000)

    def _open_session_history(self):
        from app.history_dialog import SessionHistoryDialog
        SessionHistoryDialog(self.session_history, self).exec()

    def _open_session_recordings(self):
        from app.session_replay import SessionReplayDialog
        dialog = SessionReplayDialog(self, theme=self._current_theme)
        dialog.show()

    def _set_tenant_production(self, tenant: str, production: bool):
        """Mark (or unmark) a tenant, and tell every open session about it."""
        tenant = str(tenant or "").strip()
        if not tenant:
            return
        tenants = list(self._production_tenants)
        if production and tenant not in tenants:
            tenants.append(tenant)
        elif not production:
            tenants = [name for name in tenants
                       if name.casefold() != tenant.casefold()]
        tenants = guardrails.normalize_production_tenants(tenants)
        if tenants == self._production_tenants:
            return

        self._production_tenants = tenants
        try:
            self.vault.update_settings({"production_tenants": tenants})
        except Exception:
            pass

        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if hasattr(widget, "set_production_tenants"):
                widget.set_production_tenants(tenants)
        if hasattr(self, "side_panel"):
            self.side_panel.set_production_tenants(tenants)
        self.status.showMessage(
            f"“{tenant}” is "
            f"{'now marked production' if production else 'no longer production'}",
            4000)

    def _sweep_old_recordings(self):
        """Keep the recording folder from growing without limit."""
        try:
            removed = session_log.prune()
        except Exception:
            return
        if removed:
            self.status.showMessage(
                f"Tidied {len(removed)} old session recording"
                f"{'s' if len(removed) != 1 else ''}", 4000)

    # ── Screen Capture ────────────────────────────────────────────────

    #: The capture menu has only just closed when its action fires. Reading
    #: the screen that instant can still find the menu's pixels there, so
    #: every capture waits this long for the compositor to catch up.
    SCREENSHOT_SETTLE_MS = 150

    def _capture_later(self, widget, name: str, label: str):
        """Photograph ``widget`` off the screen once the menu has gone."""
        def capture():
            try:
                from app.screenshot_tool import (
                    grab_widget_from_screen, save_pixmap_to_desktop)
                pixmap = grab_widget_from_screen(widget)
                path = save_pixmap_to_desktop(pixmap, name)
                self.status.showMessage(f"{label} saved to {path}", 5000)
            except Exception as e:
                self.status.showMessage(f"Screenshot failed: {e}", 5000)
        QTimer.singleShot(self.SCREENSHOT_SETTLE_MS, capture)

    def _screenshot_full_window(self):
        name = "Window"
        idx = self.tabs.currentIndex()
        if idx >= 0:
            name = self.tabs.tabText(idx).replace(" ", "_")
        self._capture_later(self, name, "Screenshot")

    def _screenshot_active_tab(self):
        widget = self.tabs.currentWidget()
        if not widget:
            self.status.showMessage("No active connection to screenshot", 3000)
            return
        name = self.tabs.tabText(self.tabs.currentIndex()).replace(" ", "_")
        self._capture_later(widget, name, "Tab screenshot")

    def _screenshot_select_area(self):
        # The overlay photographs the screen the moment it is built, so it
        # too waits for the menu to clear.
        QTimer.singleShot(self.SCREENSHOT_SETTLE_MS, self._open_screenshot_overlay)

    def _open_screenshot_overlay(self):
        try:
            from app.screenshot_tool import ScreenshotOverlay, save_pixmap_to_desktop
            self._screenshot_overlay = ScreenshotOverlay()
            
            name = "Area"
            idx = self.tabs.currentIndex()
            if idx >= 0:
                name = self.tabs.tabText(idx).replace(" ", "_")

            def on_capture(pixmap):
                try:
                    path = save_pixmap_to_desktop(pixmap, name)
                    self.status.showMessage(f"Screenshot saved to {path}", 5000)
                except Exception as e:
                    self.status.showMessage(f"Screenshot save failed: {e}", 5000)
                self._screenshot_overlay = None

            def on_cancel():
                self.status.showMessage("ℹ️ Screenshot canceled", 3000)
                self._screenshot_overlay = None
                
            self._screenshot_overlay.capture_complete.connect(on_capture)
            self._screenshot_overlay.capture_canceled.connect(on_cancel)
            
            self._screenshot_overlay.show()
            self._screenshot_overlay.activateWindow()
        except Exception as e:
            self.status.showMessage(f"Failed to start screenshot tool: {e}", 5000)

    # ── Tab Widget ──────────────────────────────────────────────────

    def _setup_tabs(self):
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.setStyleSheet("QTabWidget::pane { background: transparent; }")
        self.main_layout.addWidget(self.tabs)

        from app.widgets import icon_button
        nav = QWidget()
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(2, 2, 6, 2)
        nav_layout.setSpacing(4)

        self._btn_tab_prev = icon_button("chevron-left", "Previous tab (Ctrl+Shift+Tab)",
                                         size=26, icon_size=14)
        self._btn_tab_prev.clicked.connect(self._prev_tab)
        nav_layout.addWidget(self._btn_tab_prev)

        self._btn_tab_next = icon_button("chevron-right", "Next tab (Ctrl+Tab)",
                                         size=26, icon_size=14)
        self._btn_tab_next.clicked.connect(self._next_tab)
        nav_layout.addWidget(self._btn_tab_next)

        self.tabs.setCornerWidget(nav, Qt.Corner.TopRightCorner)

    def _prev_tab(self):
        i = self.tabs.currentIndex()
        if i > 0:
            self.tabs.setCurrentIndex(i - 1)

    def _next_tab(self):
        i = self.tabs.currentIndex()
        if i < self.tabs.count() - 1:
            self.tabs.setCurrentIndex(i + 1)

    def _update_tab_nav_buttons(self):
        if not hasattr(self, "_btn_tab_prev"):
            return
        i = self.tabs.currentIndex()
        n = self.tabs.count()
        self._btn_tab_prev.setEnabled(i > 0)
        self._btn_tab_next.setEnabled(i < n - 1)

    def _on_tab_changed(self, _index: int):
        self._update_aura_activity()
        self._update_file_browser_sftp()
        self._update_tab_nav_buttons()

    def _update_aura_activity(self):
        """Reduce repaint pressure when embedded native RDP is active.

        The animated aura repaints the whole window backdrop and can cause
        visible flashing when a native/ActiveX RDP surface is embedded in the
        same top-level window.
        """
        if not hasattr(self, "aura"):
            return

        cur = self.tabs.currentWidget() if hasattr(self, "tabs") else None
        embedded_rdp_active = bool(
            isinstance(cur, RDPLauncher) and getattr(cur, "embedded_mode", False)
        )

        if embedded_rdp_active:
            self.aura.timer.stop()
            self.aura.hide()
        else:
            self.aura.show()
            if not self.aura.timer.isActive():
                self.aura.timer.start(BackgroundAura.INTERVAL_MS)

    # ── Status Bar ──────────────────────────────────────────────────

    def _setup_status_bar(self):
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status_label = QLabel("Vault unlocked")
        self.status_label.setToolTip("Lock the vault with Ctrl+L, or the screen with Ctrl+Shift+L")
        self.lock_button = QToolButton()
        self.lock_button.setIcon(icons.icon("lock"))
        self.lock_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.lock_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.lock_button.setMenu(self.lock_options_menu)
        self.lock_button.setToolTip("Lock now, change the idle timeout, or choose automatic lock events")
        self.status.addPermanentWidget(self.lock_button)
        self.idle_lock.settings_changed.connect(self._update_lock_summary)
        self._update_lock_summary()
        self.status.addWidget(self.status_label)
        self.tab_count_label = QLabel("0 open sessions")
        self.status.addPermanentWidget(self.tab_count_label)
        # Sessions drop and come back on their own; keep the count honest
        # without every session having to report in.
        self._session_count_timer = QTimer(self)
        self._session_count_timer.timeout.connect(self._update_tab_count)
        self._session_count_timer.start(3000)

    def _update_lock_summary(self, _settings=None):
        from app.lock_policy import LOCK_TRIGGER_LABELS
        minutes = self.idle_lock.minutes
        events = any(self.idle_lock.policy[key] for key in LOCK_TRIGGER_LABELS)
        self.lock_button.setText(f"Lock: {minutes} min" if minutes else "Lock: events" if events else "Lock: manual")

    def _session_tabs(self) -> list:
        """Tabs that are a connection of their own.

        The Dashboard is not a session, and a page a web console opened in a
        tab of its own belongs to the console that opened it.
        """
        tabs = []
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            if widget is None or widget is getattr(self, "dashboard", None):
                continue
            if getattr(widget, "opener_tab", None) is not None:
                continue
            tabs.append(widget)
        return tabs

    def _update_tab_count(self):
        self._reconcile_history()
        gate = getattr(self, "vpn_gate", None)
        if gate is not None and gate._tunnels:
            gate.sessions_changed()
        sessions = self._session_tabs()
        n = len(sessions)
        offline = sum(1 for w in sessions if not self._tab_is_live(w))
        text = f"{n} open session{'s' if n != 1 else ''}"
        if n and offline:
            text += f" · {offline} offline"
        self.tab_count_label.setText(text)
        if sessions:
            names = [self.tabs.tabText(self.tabs.indexOf(w)).strip() for w in sessions]
            self.tab_count_label.setToolTip("\n".join(names))
        else:
            self.tab_count_label.setToolTip("No connections open")

    # ── Shortcuts ───────────────────────────────────────────────────

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+W"), self, self._close_current_tab)
        # Ctrl+W is "delete word" inside a terminal, so the terminal keeps it
        # and Ctrl+Shift+W closes the tab from anywhere (as in most terminals).
        QShortcut(QKeySequence("Ctrl+Shift+W"), self, self._close_current_tab)
        # Tab switching that also works while a terminal has focus.
        QShortcut(QKeySequence("Ctrl+Tab"), self, lambda: self._cycle_tab(1))
        QShortcut(QKeySequence("Ctrl+Shift+Tab"), self, lambda: self._cycle_tab(-1))
        QShortcut(QKeySequence("Ctrl+Shift+Backtab"), self, lambda: self._cycle_tab(-1))
        # The View actions already own =, - and 0; only + needs an alias.
        QShortcut(QKeySequence("Ctrl++"), self, self._dashboard_zoom_in)

    def _cycle_tab(self, step: int):
        count = self.tabs.count()
        if count > 1:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % count)

    # ── Dashboard Tab ───────────────────────────────────────────────

    def _show_welcome(self):
        try:
            conns = self.vault.connections
            colors = self.vault.settings.get("tenant_colors", {})
        except Exception:
            conns = []
            colors = {}
        self._graph_dialog = None
        self.dashboard = DashboardWidget(conns, colors)
        self.dashboard.tenant_clicked.connect(self._on_dashboard_tenant_clicked)
        self.dashboard.node_positions_changed.connect(self._save_node_positions)
        self.dashboard.view_state_changed.connect(self._save_dashboard_view_state)
        self.dashboard.conn_positions_changed.connect(self._save_conn_positions)
        self.dashboard.graph_settings_changed.connect(self._save_graph_settings)
        self.dashboard.tenant_links_changed.connect(self._save_tenant_links)
        self.dashboard.connection_activated.connect(self._open_connection)
        self.dashboard.tenant_color_change_requested.connect(
            self._pick_tenant_color)
        self.dashboard.new_connection_requested.connect(
            self._new_connection_for_tenant)
        self.dashboard.settings_dialog_requested.connect(self._open_graph_settings)
        self.dashboard.link_mode_changed.connect(self._on_link_mode_changed)
        self.dashboard.restart_requested.connect(self._restart_connection)
        self.dashboard.restart_direct_requested.connect(
            self._restart_connection_directly)
        self.dashboard.broadcast_tenant_requested.connect(self._open_broadcast_session)
        self.dashboard.check_now_requested.connect(self._check_reachability_now)
        self.dashboard.wake_requested.connect(self._wake_connections)
        self.dashboard.relation_lens_changed.connect(self._on_relation_lens_changed)
        self.dashboard.ping_toggle_requested.connect(self._set_ping_enabled)
        self.dashboard.restart_setup_requested.connect(self._configure_restart_server)
        # Apply current theme so matrix rain activates immediately if saved
        self.dashboard.set_theme(self._current_theme)
        # Graph options must land before the view state — free-float mode
        # decides whether the saved "collapsed" flag means anything.
        saved_gs = self.vault.settings.get("dashboard_graph_settings", {})
        if saved_gs:
            self.dashboard.set_graph_settings(saved_gs)
        self.dashboard.set_tenant_links(
            self.vault.settings.get("dashboard_tenant_links", []))
        # Restore any previously saved node positions
        saved_pos = self.vault.settings.get("dashboard_node_positions", {})
        if saved_pos:
            self.dashboard.set_node_positions(saved_pos)
        saved_conn_pos = self.vault.settings.get("dashboard_conn_positions", {})
        if saved_conn_pos:
            self.dashboard.set_conn_positions(saved_conn_pos)
        saved_view = self.vault.settings.get("dashboard_view_state", {})
        if saved_view:
            self.dashboard.set_view_state(saved_view)
        self._init_reachability()
        self._sync_graph_menu()
        self.tabs.addTab(self.dashboard, icons.icon("home"), "Dashboard")
        self._setup_home_panel()
        # Dashboard tab is permanent — remove its close button
        self.tabs.tabBar().setTabButton(0, QTabBar.ButtonPosition.RightSide, None)

    def _setup_home_panel(self):
        """The Overview card over the graph: problems, favourites, recents."""
        from app.home_panel import HomePanel
        panel = HomePanel(self.dashboard)
        self.home_panel = panel
        panel.set_theme(self._current_theme)
        panel.open_requested.connect(self._open_connection)
        panel.new_connection_requested.connect(self._new_connection)
        panel.import_requested.connect(self._import_from_home)
        panel.check_now_requested.connect(lambda: self._check_reachability_now([]))
        panel.goto_requested.connect(self._open_goto)
        panel.visibility_changed.connect(self._on_home_visibility_changed)
        self.dashboard.resized.connect(panel.reposition)
        self.dashboard.show_empty_hint = False
        try:
            shown = bool(self.vault.settings.get("home_panel_visible", True))
        except VaultError:
            shown = True
        panel.setVisible(shown)
        if hasattr(self, "act_show_overview"):
            self.act_show_overview.setChecked(shown)
        self._refresh_home_panel()

    def _refresh_home_panel(self):
        panel = getattr(self, "home_panel", None)
        if panel is None:
            return
        monitor = getattr(self, "reachability", None)
        try:
            conns = self.vault.connections
        except VaultError:
            return
        history = getattr(self, "session_history", None)
        panel.set_data(
            connections=conns,
            statuses=monitor.statuses() if monitor is not None else {},
            recent_ids=history.recent_ids() if history is not None else [],
            ping_enabled=bool(monitor.enabled) if monitor is not None else False)
        if not conns:
            panel.setVisible(True)   # the welcome card is not optional

    def _on_home_visibility_changed(self, shown: bool):
        if hasattr(self, "act_show_overview"):
            self.act_show_overview.setChecked(bool(shown))
        try:
            self.vault.update_settings({"home_panel_visible": bool(shown)})
        except VaultError:
            pass

    def _set_home_visible(self, shown: bool):
        panel = getattr(self, "home_panel", None)
        if panel is not None:
            panel.set_shown(bool(shown))
            if shown:
                self.tabs.setCurrentWidget(self.dashboard)

    def _import_from_home(self, kind: str):
        if kind == "ssh_config":
            self._import_ssh_config()
            return
        menu = QMenu(self)
        menu.addAction("RPMote export (.rpvault)…", self._import_connections)
        menu.addAction("Remote Desktop files (.rdp)…", lambda: self._import_from("rdp"))
        menu.addAction("CSV (RDM, Royal TS, spreadsheets)…", lambda: self._import_from("csv"))
        menu.addAction("mRemoteNG (confCons.xml)…", lambda: self._import_from("mremoteng"))
        menu.addAction("PuTTY saved sessions", lambda: self._import_from("putty"))
        menu.exec(self.cursor().pos())

    def _import_from(self, kind: str):
        """Read connections from another tool, preview them, then save."""
        from app import importers
        from PyQt6.QtWidgets import QInputDialog
        records, source = [], ""
        try:
            if kind == "rdp":
                paths, _ = QFileDialog.getOpenFileNames(
                    self, "Import Remote Desktop files", "", "Remote Desktop files (*.rdp)")
                if not paths:
                    return
                errors = []
                for path in paths:
                    try:
                        records.append(importers.parse_rdp_file(path))
                    except Exception as exc:
                        errors.append(f"{os.path.basename(path)}: {exc}")
                source = f"{len(paths)} .rdp file{'s' if len(paths) != 1 else ''}"
                if errors:
                    QMessageBox.warning(self, "Import", "Some files were skipped:\n" + "\n".join(errors[:10]))
            elif kind == "csv":
                path, _ = QFileDialog.getOpenFileName(self, "Import CSV", "", "CSV files (*.csv *.txt)")
                if not path:
                    return
                records = importers.parse_csv(path)
                source = os.path.basename(path)
            elif kind == "mremoteng":
                path, _ = QFileDialog.getOpenFileName(
                    self, "Import mRemoteNG connections", "",
                    "mRemoteNG connections (confCons.xml *.xml)")
                if not path:
                    return
                password = importers.MREMOTE_DEFAULT_PASSWORD
                if importers.mremoteng_needs_password(path):
                    password, ok = QInputDialog.getText(
                        self, "mRemoteNG master password",
                        "This file is protected with a master password:",
                        QLineEdit.EchoMode.Password)
                    if not ok:
                        return
                records = importers.parse_mremoteng(path, password)
                source = os.path.basename(path)
            elif kind == "putty":
                records = importers.putty_sessions()
                source = "PuTTY saved sessions"
        except Exception as exc:
            QMessageBox.warning(self, "Import", f"Could not read that file:\n{exc}")
            return
        if not records:
            QMessageBox.information(self, "Import", f"No connections found in {source or 'that source'}.")
            return
        try:
            existing = list(self.vault.connections)
        except VaultError:
            return
        tenants = sorted({str(c.get("tenant", "Default") or "Default") for c in existing},
                         key=str.casefold)
        dialog = importers.ImportPreviewDialog(records, existing, source, tenants, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dialog.chosen()
        if not chosen:
            return
        self._backup_before("import")
        for record in chosen:
            self.vault.add_connection(record)
        self._refresh_sidebar()
        self.status.showMessage(f"Imported {len(chosen)} connection{'s' if len(chosen) != 1 else ''} from {source}", 6000)

    def _open_goto(self):
        self._show_goto_palette()

    def _show_goto_palette(self):
        """Ctrl+P: find any connection, tenant or command from anywhere."""
        from app.goto_palette import GotoPalette
        try:
            conns = list(self.vault.connections)
            notes = self.vault.settings.get("tenant_notes", {})
        except VaultError:
            return
        monitor = getattr(self, "reachability", None)
        history = getattr(self, "session_history", None)
        palette = GotoPalette(
            conns,
            statuses=monitor.statuses() if monitor is not None else {},
            recent_ids=history.recent_ids() if history is not None else [],
            menubar=self.menuBar(), tenant_notes=notes, parent=self)
        palette.open_requested.connect(
            lambda conn, force: self._open_connection(conn, force_new=force))
        palette.edit_requested.connect(self._edit_connection)
        palette.tenant_requested.connect(self._focus_tenant)
        self._goto_palette = palette
        palette.show_over(self)

    def _focus_tenant(self, tenant: str):
        """Show a tenant: open it on the graph and in the sidebar."""
        if hasattr(self, "dashboard"):
            self.tabs.setCurrentWidget(self.dashboard)
            self.dashboard.reveal_tenant(tenant, exclusive=False)
        if not self.side_panel.isVisible():
            self._toggle_sidebar()
        self.side_panel.expand_tenant(tenant)

    # ── Reachability ─────────────────────────────────────────────

    def _init_reachability(self):
        """Start the probe engine and point the dashboard at its results."""
        from app.reachability import ReachabilityMonitor, normalize_ping_settings
        from app.restart import RestartRunner
        from app.wake_on_lan import WakeRunner

        settings = normalize_ping_settings(
            self.vault.settings.get("ping_settings", {}))
        self.reachability = ReachabilityMonitor(settings, parent=self)
        self.reachability.status_changed.connect(self._on_status_changed)
        self.reachability.sweep_finished.connect(self._on_sweep_finished)
        self.reachability.set_connections(self._safe_connections())

        self.restart_runner = RestartRunner(self)
        self.restart_runner.finished.connect(self._on_restart_finished)

        self.wake_runner = WakeRunner(self)
        self.wake_runner.result_ready.connect(self._on_wake_result)
        self.wake_runner.finished.connect(self._on_wake_finished)
        #: sudo passwords the user asked us to hold on to, keyed by the id of
        #: the machine they unlock.  Memory only — never written to the vault,
        #: and dropped when the window closes.
        self._sudo_cache: dict[str, str] = {}

        self.dashboard.set_restart_relations(*self._restart_config())
        self.dashboard.set_ping_enabled(bool(settings["enabled"]))
        if settings["enabled"]:
            self.reachability.start()
        if settings.get("check_on_login", True):
            # Deferred until the login door has finished uncovering the window,
            # so the first sweep does not compete with the reveal animation.
            QTimer.singleShot(1500, self._sweep_on_login)

    def _safe_connections(self) -> list:
        try:
            return list(self.vault.connections)
        except Exception:
            return []

    def _set_ping_enabled(self, enabled: bool):
        """Turn probing on or off, and remember the choice."""
        monitor = getattr(self, "reachability", None)
        if monitor is None:
            return
        enabled = bool(enabled)
        monitor.set_connections(self._safe_connections())
        monitor.apply_settings({"enabled": enabled})
        self.dashboard.set_ping_enabled(enabled)
        self._save_ping_settings(monitor.settings)
        if hasattr(self, "act_ping"):
            self.act_ping.setChecked(enabled)
        self.status.showMessage(
            "Checking connections…" if enabled
            else "Connection checks turned off", 4000)

    def _toggle_ping(self, checked: bool):
        self._set_ping_enabled(bool(checked))

    def _apply_ping_settings(self, settings: dict):
        """Live-apply the Graph Settings reachability controls."""
        monitor = getattr(self, "reachability", None)
        if monitor is None:
            return
        monitor.set_connections(self._safe_connections())
        monitor.apply_settings(settings)
        self.dashboard.set_ping_enabled(monitor.enabled)
        self._save_ping_settings(monitor.settings)
        if hasattr(self, "act_ping"):
            self.act_ping.setChecked(monitor.enabled)

    def _check_reachability_now(self, conn_ids=None):
        """Probe immediately.  An empty/None id list means the whole estate."""
        monitor = getattr(self, "reachability", None)
        if monitor is None:
            return
        monitor.set_connections(self._safe_connections())
        ids = [str(c) for c in (conn_ids or []) if str(c)]
        monitor.check_now(ids or None)
        if not monitor.enabled:
            # A one-off check should still show its result, so reveal the dots
            # without committing the user to continuous probing.
            self.dashboard.set_ping_enabled(True)
        self.status.showMessage(
            f"Checking {len(ids)} connection{'s' if len(ids) != 1 else ''}…"
            if ids else "Checking all connections…", 4000)

    def _sweep_on_login(self):
        """One sweep of the whole estate straight after unlock.

        Runs whether or not continuous probing is on: opening RPMote to a graph
        that still shows yesterday's colours — or no colours at all — is how a
        dead server goes unnoticed until a customer rings.
        """
        if getattr(self, "_closing", False):
            return
        monitor = getattr(self, "reachability", None)
        if monitor is None:
            return
        self._check_reachability_now([])

    def _save_ping_settings(self, settings: dict):
        try:
            self.vault.update_settings({"ping_settings": dict(settings)})
        except VaultError:
            pass

    def _on_status_changed(self, conn_id: str, info: dict):
        self.dashboard.set_conn_status(conn_id, info)
        self.side_panel.set_conn_status(conn_id, info)
        if getattr(self, "home_panel", None) is not None:
            self.home_panel.set_status(conn_id, info)

    def _on_sweep_finished(self, statuses: dict):
        self.dashboard.set_conn_statuses(statuses)
        monitor = getattr(self, "reachability", None)
        self.side_panel.set_conn_statuses(
            monitor.statuses() if monitor is not None else statuses)
        self._refresh_home_panel()

    # ── Wake-on-LAN ──────────────────────────────────────────────

    #: Above this many machines a wake stops waiting for each one and simply
    #: re-probes the batch, rather than parking a poller per host.
    WAKE_WAIT_LIMIT = 4
    #: How long after a bulk wake to look again — enough for a cold boot.
    WAKE_RECHECK_MS = 60_000

    def _wake_connections(self, conn_ids=None):
        """Send magic packets.  An empty/None id list means every wakeable box."""
        from app.wake_on_lan import DEFAULT_WAIT_SECONDS, is_wakeable

        runner = getattr(self, "wake_runner", None)
        if runner is None:
            return
        requested = list(conn_ids or [])
        wanted = {str(c) for c in requested if str(c)}
        if requested and not wanted:
            # Asking for specific machines and naming none of them must not
            # quietly become "wake the whole estate".
            return
        connections = [c for c in self._safe_connections() if isinstance(c, dict)]
        targets = [c for c in connections
                   if is_wakeable(c)
                   and (not wanted or str(c.get("id", "")) in wanted)]
        if not targets:
            self._explain_nothing_to_wake(connections, wanted)
            return

        # Waiting is what turns "packet sent" into "the machine is back", but
        # one poller per host does not scale to waking a whole tenant.
        wait = DEFAULT_WAIT_SECONDS if len(targets) <= self.WAKE_WAIT_LIMIT else 0.0
        runner.submit(targets, wait_seconds=wait)
        if len(targets) == 1:
            self.status.showMessage(
                f"Waking {targets[0].get('name', 'connection')}…", 0)
        else:
            self.status.showMessage(f"Waking {len(targets)} machines…", 0)

    def _explain_nothing_to_wake(self, connections: list, wanted: set):
        """Say why nothing happened, naming the connection when there is one."""
        asked = [c for c in connections if str(c.get("id", "")) in wanted]
        if len(asked) == 1:
            name = str(asked[0].get("name", "") or "This connection")
            body = f"<b>{name}</b> has no MAC address saved."
        elif wanted:
            body = "None of the chosen connections has a MAC address saved."
        else:
            body = "No saved connection has a MAC address."
        QMessageBox.information(
            self, "Wake-on-LAN",
            f"{body}<br><br>Edit the connection and fill in its "
            f"<b>MAC Address</b> — RPMote can then wake it with a magic "
            f"packet, provided Wake-on-LAN is enabled on the machine and "
            f"you are on the same network segment.")

    def _on_wake_result(self, conn, result):
        """One machine's outcome, as it lands."""
        if getattr(self, "_closing", False):
            return
        name = str(conn.get("name", "") or result.host or "connection")
        self.status.showMessage(f"{name} — {result.summary()}", 10000)

    def _on_wake_finished(self, results: list):
        """The batch is done: report it, then let the probe engine confirm."""
        if getattr(self, "_closing", False) or not results:
            return
        woken = [(c, r) for c, r in results if r.ok]
        failed = [(c, r) for c, r in results if not r.ok]

        ids = [str(c.get("id", "")) for c, _ in woken if str(c.get("id", ""))]
        if ids:
            QTimer.singleShot(
                self.WAKE_RECHECK_MS, lambda: self._recheck_after_wake(ids))

        if len(results) > 1:
            self.status.showMessage(
                f"Magic packets sent to {len(woken)} of {len(results)} machines"
                + (f" — {len(failed)} could not be sent" if failed else ""),
                10000)

        if failed and len(failed) == len(results):
            first = failed[0][1]
            detail = "\n".join(first.errors[:5]) or first.summary()
            QMessageBox.warning(
                self, "Wake-on-LAN",
                f"No magic packet could be sent.\n\n{detail}")

    def _recheck_after_wake(self, conn_ids: list):
        """Re-probe machines we woke, so the graph catches up on its own."""
        if getattr(self, "_closing", False):
            return
        monitor = getattr(self, "reachability", None)
        if monitor is None:
            return
        self._check_reachability_now(list(conn_ids))

    # ── Restart ──────────────────────────────────────────────────

    def _restart_config(self) -> tuple[dict, dict]:
        from app.restart import normalize_config_map
        settings = self.vault.settings
        return (normalize_config_map(settings.get("restart_servers", {})),
                normalize_config_map(settings.get("restart_overrides", {})))

    def _save_restart_config(self, servers: dict, overrides: dict):
        try:
            self.vault.update_settings({"restart_servers": dict(servers),
                                        "restart_overrides": dict(overrides)})
        except VaultError as exc:
            QMessageBox.warning(self, "Restart Servers", str(exc))

    def _configure_restart_server(self, scope_kind: str, scope_key: str):
        """Nominate the machine that restarts a tenant, or one connection."""
        from app.restart_dialogs import RestartServerDialog

        connections = self._safe_connections()
        servers, overrides = self._restart_config()
        store = servers if scope_kind == "tenant" else overrides

        if scope_kind == "tenant":
            label, tenant = scope_key, scope_key
        else:
            match = next((c for c in connections
                          if str(c.get("id", "")) == str(scope_key)), None)
            if match is None:
                return
            label = f"“{match.get('name') or match.get('host') or scope_key}”"
            tenant = str(match.get("tenant", "Default") or "Default")

        dialog = RestartServerDialog(scope_kind, str(scope_key), label,
                                     connections, store.get(str(scope_key)),
                                     preferred_tenant=tenant, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.removed:
            store.pop(str(scope_key), None)
            self.status.showMessage(f"Restart server removed for {label}", 4000)
        elif dialog.result_config:
            store[str(scope_key)] = dialog.result_config
            self.status.showMessage(f"Restart server saved for {label}", 4000)
        self._save_restart_config(servers, overrides)

    def _open_restart_manager(self):
        from app.restart_dialogs import RestartManagerDialog

        servers, overrides = self._restart_config()
        dialog = RestartManagerDialog(servers, overrides,
                                      self._safe_connections(), parent=self)
        dialog.exec()
        if dialog.changed:
            self._save_restart_config(dialog.servers, dialog.overrides)

    def _prune_restart_config(self, connections: list):
        """Forget restart setup whose tenant or server no longer exists.

        Left alone, a stale entry means a right-click offers to restart a
        machine through a server that was deleted months ago.
        """
        from app.restart import prune_configs

        servers, overrides = self._restart_config()
        kept_servers, kept_overrides = prune_configs(servers, overrides,
                                                     connections)
        if kept_servers != servers or kept_overrides != overrides:
            self._save_restart_config(kept_servers, kept_overrides)

    def _restart_connection_directly(self, conn: dict):
        """Reboot the machine over its own connection, with nothing set up."""
        self._restart_connection(conn, direct=True)

    def _restart_connection(self, conn: dict, direct: bool = False):
        """Confirm, then hand the restart to whoever is going to carry it out."""
        from app.restart import (
            RestartNotConfigured, RestartError, direct_plan, resolve_plan)
        from app.restart_dialogs import RestartConfirmDialog

        conn = self._with_credentials(conn)
        connections = self._resolved_connections()
        servers, overrides = self._restart_config()
        try:
            plan = (direct_plan(conn) if direct
                    else resolve_plan(conn, connections, servers, overrides))
        except RestartNotConfigured as exc:
            box = QMessageBox(self)
            box.setWindowTitle("Restart")
            box.setIcon(QMessageBox.Icon.Information)
            box.setText(str(exc))
            from app.restart import can_restart_directly
            straight = (box.addButton("Restart Directly…",
                                      QMessageBox.ButtonRole.AcceptRole)
                        if (not direct and can_restart_directly(conn)) else None)
            setup = box.addButton("Set Up a Restart Server…",
                                  QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if straight is not None and box.clickedButton() is straight:
                self._restart_connection(conn, direct=True)
            elif box.clickedButton() is setup:
                self._configure_restart_server(
                    "tenant", str(conn.get("tenant", "Default") or "Default"))
            return
        except RestartError as exc:
            QMessageBox.warning(self, "Restart", str(exc))
            return

        # Offer the password we already have for that machine, so the common
        # case is one click rather than a typing exercise.
        server_id = str(plan.server.get("id", "") or "")
        saved = (self._sudo_cache.get(server_id)
                 or str(plan.server.get("password", "") or ""))
        dialog = RestartConfirmDialog(plan, saved_password=saved, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.remember and dialog.sudo_password and server_id:
            self._sudo_cache[server_id] = dialog.sudo_password

        route = ("directly" if plan.direct else f"via {plan.server_name}")
        self.status.showMessage(f"Restarting {plan.target_name} {route}…", 0)
        self.restart_runner.submit(plan)

    def _on_restart_finished(self, plan, result):
        if result.ok:
            self.status.showMessage(
                f"Restart command sent to {plan.target_name}", 8000)
            # The box is going down, so its next probe should reflect that
            # rather than a green dot from thirty seconds ago.
            monitor = getattr(self, "reachability", None)
            if monitor is not None and monitor.enabled:
                QTimer.singleShot(
                    5000, lambda: monitor.check_now(
                        [str(plan.target.get("id", ""))]))
            return

        self.status.showMessage(f"Restart of {plan.target_name} failed", 8000)
        box = QMessageBox(self)
        box.setWindowTitle("Restart Failed")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"<b>{plan.target_name}</b> was not restarted.")
        box.setInformativeText(result.summary())
        box.setDetailedText(f"Command:\n{result.command}\n\n"
                            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}")
        box.exec()

    def _save_dashboard_view_state(self, state: dict):
        """Persist dashboard zoom/pan/collapse state to the vault."""
        try:
            self.vault.update_settings({"dashboard_view_state": state})
        except VaultError:
            pass

    def _save_graph_settings(self, settings: dict):
        """Persist how the user wants the topology drawn."""
        try:
            self.vault.update_settings({"dashboard_graph_settings": settings})
        except VaultError:
            pass
        self._sync_graph_menu(settings)

    def _save_tenant_links(self, links: list):
        """Persist the purely-visual tenant relationships."""
        try:
            self.vault.update_settings({"dashboard_tenant_links": links})
        except VaultError:
            pass

    def _save_conn_positions(self, positions: dict):
        """Persist hand-placed connection branch nodes."""
        try:
            self.vault.update_settings({"dashboard_conn_positions": positions})
        except VaultError:
            pass

    def _pick_tenant_color(self, tenant_name: str):
        """Colour picker reached from the dashboard's tenant context menu."""
        from PyQt6.QtWidgets import QColorDialog
        current = self.vault.settings.get("tenant_colors", {}).get(
            tenant_name, self._current_theme.get("accent", "#a78bfa"))
        chosen = QColorDialog.getColor(QColor(current), self,
                                       f"Colour for {tenant_name}")
        if chosen.isValid():
            self._on_tenant_color_changed(tenant_name, chosen.name())
            self._refresh_sidebar()

    def _on_sidebar_tenant_focus_requested(self, tenant_name: str):
        """Show selected tenant's node(s) on the dashboard without expanding all."""
        if hasattr(self, "dashboard"):
            self.dashboard.reveal_tenant(tenant_name, exclusive=True)

    def _on_sidebar_tenant_close_requested(self, tenant_name: str):
        if hasattr(self, "dashboard"):
            self.dashboard.set_tenant_expanded(tenant_name, False)

    def _on_sidebar_connection_focus_requested(self, conn_id: str):
        if hasattr(self, "dashboard"):
            self.dashboard.select_connection(conn_id)

    def _dashboard_zoom_in(self):
        if hasattr(self, "dashboard"):
            self.dashboard.zoom_in()

    def _dashboard_zoom_out(self):
        if hasattr(self, "dashboard"):
            self.dashboard.zoom_out()

    def _dashboard_zoom_reset(self):
        if hasattr(self, "dashboard"):
            self.dashboard.reset_zoom()

    # ── Connection Actions ──────────────────────────────────────────

    def _on_dashboard_tenant_clicked(self, tenant_name: str):
        """Show sidebar and expand the tenant when its dashboard node is clicked."""
        if not self.side_panel.isVisible():
            self._toggle_sidebar()
        self.side_panel.expand_tenant(tenant_name)

    def _save_node_positions(self, positions: dict):
        """Persist user-dragged node positions to the vault."""
        try:
            self.vault.update_settings({"dashboard_node_positions": positions})
        except VaultError:
            pass

    def _new_connection(self):
        dialog = ConnectionDialog(parent=self, vault=self.vault)
        if dialog.exec() == ConnectionDialog.DialogCode.Accepted:
            data = dialog.get_result()
            self.vault.add_connection(data)
            self._persist_tenant_color(data)
            self._refresh_sidebar()

    def _new_connection_for_tenant(self, tenant: str):
        dialog = ConnectionDialog(parent=self, default_tenant=tenant, vault=self.vault)
        if dialog.exec() == ConnectionDialog.DialogCode.Accepted:
            data = dialog.get_result()
            self.vault.add_connection(data)
            self._persist_tenant_color(data)
            self._refresh_sidebar()

    def _new_connection_with_protocol(self, protocol: str):
        # Dashboard dock may use abbreviated labels — map back to full names.
        label_map = {"SYN": "NAS", "Synology": "NAS", "AZPS": "Azure PowerShell"}
        protocol = label_map.get(protocol, protocol)
        dialog = ConnectionDialog(parent=self, vault=self.vault)
        idx = dialog.protocol.findText(protocol)
        if idx >= 0:
            dialog.protocol.setCurrentIndex(idx)
        if dialog.exec() == ConnectionDialog.DialogCode.Accepted:
            data = dialog.get_result()
            self.vault.add_connection(data)
            self._persist_tenant_color(data)
            self._refresh_sidebar()

    def _edit_connection(self, conn: dict) -> bool:
        dialog = ConnectionDialog(connection=conn, parent=self, vault=self.vault)
        if dialog.exec() == ConnectionDialog.DialogCode.Accepted:
            data = dialog.get_result()
            conn_id = conn.get("id", "")
            if conn_id:
                self.vault.update_connection(conn_id, data)
            self._persist_tenant_color(data)
            self._refresh_sidebar()
            return True
        return False

    def _saved_connection(self, conn_id: str) -> dict | None:
        for record in self.vault.connections:
            if str(record.get("id", "") or "") == conn_id:
                return record
        return None

    def _edit_from_session(self, session, conn: dict):
        """Edit a connection from its session's failure panel, then retry with the fix."""
        conn_id = str((conn or {}).get("id", "") or "")
        saved = self._saved_connection(conn_id) if conn_id else None
        if saved is None:
            self.status.showMessage("This session is not a saved connection — nothing to edit", 5000)
            return
        if not self._edit_connection(saved):
            return
        updated = self._saved_connection(conn_id)
        if updated is None:
            return
        try:
            updated = self.vault.resolve_credentials(updated)
        except Exception:
            pass
        old_protocol = str(conn.get("protocol", "") or "").lower()
        new_protocol = str(updated.get("protocol", "") or "").lower()
        apply = getattr(session, "apply_connection_update", None)
        if new_protocol != old_protocol or not callable(apply):
            # A different kind of session altogether: open it afresh.
            index = self.tabs.indexOf(session)
            if index < 0:
                parent = session.parentWidget()
                while parent is not None and self.tabs.indexOf(parent) < 0:
                    parent = parent.parentWidget()
                index = self.tabs.indexOf(parent) if parent is not None else -1
            if index >= 0:
                self._close_tab(index)
            self._open_connection(updated, force_new=True)
            return
        if new_protocol in ("ssh", "sftp"):
            updated = self._resolve_route(updated)
        apply(updated)

    def _persist_tenant_color(self, data: dict):
        """Save tenant_color from a connection save into vault settings."""
        tenant = data.get("tenant", "Default") or "Default"
        color = data.get("tenant_color", "")
        if color:
            try:
                colors = self.vault.settings.get("tenant_colors", {})
                colors[tenant] = color
                self.vault.update_settings({"tenant_colors": colors})
            except VaultError:
                pass

    def _on_tenant_color_changed(self, tenant_name: str, hex_color: str):
        """Called when a user picks a color from the sidebar right-click menu."""
        try:
            colors = self.vault.settings.get("tenant_colors", {})
            colors[tenant_name] = hex_color
            self.vault.update_settings({"tenant_colors": colors})
        except VaultError:
            pass

    def _delete_connection(self, conn_id: str):
        try:
            conn = self.vault.get_connection(conn_id) or {}
        except VaultError:
            return
        name = conn.get("name") or "this connection"
        from app.dialogs import confirm
        from app.vault import TRASH_DAYS
        if not confirm(self, "Delete connection", f"Delete “{name}”?", "Delete",
                       danger=True,
                       informative=f"It stays in File › Recently Deleted for {TRASH_DAYS} days."):
            return
        self.vault.delete_connection(conn_id)
        self._refresh_sidebar()
        self.status.showMessage(f"Deleted {name} — File › Recently Deleted can bring it back", 6000)

    def _delete_many(self, conn_ids: list):
        ids = [str(c) for c in (conn_ids or []) if str(c)]
        if not ids:
            return
        names = []
        try:
            for cid in ids:
                conn = self.vault.get_connection(cid)
                if conn:
                    names.append(str(conn.get("name", "?")))
        except VaultError:
            return
        from app.dialogs import confirm
        from app.vault import TRASH_DAYS
        listed = ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
        if not confirm(self, "Delete connections",
                       f"Delete {len(ids)} connections?", f"Delete {len(ids)}",
                       danger=True,
                       informative=f"{listed}\n\nThey stay in File › Recently Deleted "
                                   f"for {TRASH_DAYS} days."):
            return
        self._backup_before("delete connections")
        removed = self.vault.delete_connections(ids)
        self._refresh_sidebar()
        self.status.showMessage(f"Deleted {removed} connections — File › Recently Deleted can bring them back", 6000)

    def _backup_before(self, reason: str):
        """Snapshot the vault before something destructive. Never blocks the action."""
        try:
            from app import backups
            backups.create_backup(f"before {reason}")
        except Exception:
            logging.getLogger("rpmote").exception("backup before %s failed", reason)

    def _delete_tenant(self, tenant_name: str):
        """Delete a tenant and all its connections."""
        try:
            conns = self.vault.connections
        except VaultError:
            return
        tenant_conns = [c for c in conns if (c.get("tenant", "Default") or "Default") == tenant_name]
        count = len(tenant_conns)

        if count == 0:
            QMessageBox.information(self, "Delete Tenant", f"Tenant '{tenant_name}' has no connections.")
            return

        from app.dialogs import confirm
        from app.vault import TRASH_DAYS
        if not confirm(self, "Delete tenant",
                       f"Delete tenant “{tenant_name}” and its {count} "
                       f"connection{'s' if count != 1 else ''}?",
                       "Delete tenant", danger=True,
                       informative=f"Open sessions for it are closed. The connections stay in "
                                   f"File › Recently Deleted for {TRASH_DAYS} days, and a vault "
                                   f"backup is taken first."):
            return
        self._backup_before(f"deleting tenant {tenant_name}")

        # Close/cleanup any active tabs for connections in this tenant
        tenant_conn_ids = {str(c.get("id", "")).strip() for c in tenant_conns if c.get("id")}
        for widget in self._tab_widgets():
            tab_conn = getattr(widget, "connection_info", None)
            if not isinstance(tab_conn, dict):
                continue
            tab_id = str(tab_conn.get("id", "")).strip()
            tab_tenant = (tab_conn.get("tenant", "Default") or "Default")
            if tab_id in tenant_conn_ids or tab_tenant == tenant_name:
                self._end_session_tab(widget)

        # Delete all connections from vault (into Recently Deleted)
        self.vault.delete_connections_by_tenant(tenant_name)

        # Clean up tenant-specific settings
        try:
            tenant_colors = self.vault.settings.get("tenant_colors", {})
            if tenant_name in tenant_colors:
                del tenant_colors[tenant_name]
                self.vault.update_settings({"tenant_colors": tenant_colors})

            node_positions = self.vault.settings.get("dashboard_node_positions", {})
            if tenant_name in node_positions:
                del node_positions[tenant_name]
                self.vault.update_settings({"dashboard_node_positions": node_positions})
        except VaultError:
            pass

        self._update_aura_activity()
        self._update_tab_count()
        self._refresh_sidebar()
        self.status.showMessage(
            f"Deleted tenant '{tenant_name}' with {count} connection{'s' if count != 1 else ''}"
            " — File › Recently Deleted can bring them back", 6000
        )

    # ── Favourites, duplicates, moves and tenants ─────────────────

    def _set_favorite(self, conn_ids: list, favorite: bool):
        changed = 0
        for cid in conn_ids or []:
            try:
                self.vault.update_connection(str(cid), {"favorite": bool(favorite)})
                changed += 1
            except VaultError:
                continue
        if changed:
            self._refresh_sidebar()
            self.status.showMessage(
                f"{'Added to' if favorite else 'Removed from'} favorites: {changed}", 3000)

    def _duplicate_connection(self, conn: dict):
        """Open the editor on a copy, so DC02 is one host change away from DC01."""
        copy = {k: v for k, v in dict(conn or {}).items() if k != "id"}
        copy["name"] = f"{conn.get('name', 'Connection')} (copy)"
        copy["favorite"] = False
        dialog = ConnectionDialog(connection=copy, parent=self, vault=self.vault)
        if dialog.exec() == ConnectionDialog.DialogCode.Accepted:
            data = dialog.get_result()
            data.pop("id", None)
            self.vault.add_connection(data)
            self._persist_tenant_color(data)
            self._refresh_sidebar()

    def _open_many(self, conns: list):
        for conn in conns or []:
            if isinstance(conn, dict):
                self._open_connection(conn)

    def _move_connections(self, conn_ids: list, tenant: str):
        tenant = str(tenant or "").strip() or "Default"
        moved = 0
        for cid in conn_ids or []:
            try:
                self.vault.update_connection(str(cid), {"tenant": tenant})
                moved += 1
            except VaultError:
                continue
        if moved:
            self._refresh_sidebar()
            self.status.showMessage(f"Moved {moved} connection{'s' if moved != 1 else ''} to {tenant}", 4000)

    def _rename_tenant(self, old: str):
        """Rename a tenant everywhere it is remembered, not just on its machines."""
        from PyQt6.QtWidgets import QInputDialog
        new, ok = QInputDialog.getText(self, "Rename tenant", f"New name for “{old}”:", text=old)
        new = (new or "").strip()
        if not ok or not new or new == old:
            return
        try:
            conns = self.vault.connections
            settings = self.vault.settings
        except VaultError:
            return
        existing = {str(c.get("tenant", "Default") or "Default") for c in conns}
        if new in existing:
            from app.dialogs import confirm
            if not confirm(self, "Merge tenants",
                           f"“{new}” already exists. Move everything from “{old}” into it?",
                           "Merge"):
                return
        self._backup_before(f"renaming tenant {old}")
        for conn in conns:
            if (conn.get("tenant", "Default") or "Default") == old:
                conn["tenant"] = new
        updates = {}

        def rekey(name):
            mapping = settings.get(name)
            if isinstance(mapping, dict) and old in mapping:
                value = mapping.pop(old)
                mapping.setdefault(new, value)
                updates[name] = mapping

        for key in ("tenant_colors", "dashboard_node_positions", "tenant_notes",
                    "tenant_vpn", "restart_servers"):
            rekey(key)
        prod = list(settings.get("production_tenants", []))
        if any(t == old for t in prod):
            prod = [new if t == old else t for t in prod]
            updates["production_tenants"] = list(dict.fromkeys(prod))
            self._production_tenants = guardrails.normalize_production_tenants(updates["production_tenants"])
            self.side_panel.set_production_tenants(self._production_tenants)
        links = settings.get("dashboard_tenant_links", [])
        if isinstance(links, list):
            for link in links:
                for end in ("a", "b"):
                    if link.get(end) in (old, f"tenant:{old}"):
                        link[end] = f"tenant:{new}"
            updates["dashboard_tenant_links"] = links
        view = settings.get("dashboard_view_state", {})
        if isinstance(view, dict) and old in (view.get("visible_tenants") or []):
            view["visible_tenants"] = [new if t == old else t for t in view["visible_tenants"]]
            updates["dashboard_view_state"] = view
        self.vault.update_settings(updates)  # also saves the connection edits
        if hasattr(self, "dashboard"):
            self.dashboard.set_tenant_links(self.vault.settings.get("dashboard_tenant_links", []))
        self._refresh_sidebar()
        self.status.showMessage(f"Renamed “{old}” to “{new}”", 4000)

    def _edit_tenant_notes(self, tenant: str):
        from app.dialogs import NotesDialog
        notes = dict(self.vault.settings.get("tenant_notes", {}) or {})
        dialog = NotesDialog(f"Notes — {tenant}",
                             "Shown when you hover the tenant in the sidebar, and in Go to.",
                             notes.get(tenant, ""), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        text = dialog.text()
        if text:
            notes[tenant] = text
        else:
            notes.pop(tenant, None)
        self.vault.update_settings({"tenant_notes": notes})
        self.side_panel.set_tenant_notes(notes)

    def _configure_tenant_vpn(self, tenant: str):
        from app.vpn import SETTINGS_KEY, TenantVpnDialog
        configs = dict(self.vault.settings.get(SETTINGS_KEY, {}) or {})
        dialog = TenantVpnDialog(tenant, configs.get(tenant), self, gate=getattr(self, "vpn_gate", None))
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if dialog.removed:
            configs.pop(tenant, None)
            message = f"No VPN step for {tenant} any more"
        else:
            configs[tenant] = dialog.config()
            message = f"{tenant}: VPN connects first when a machine does not answer"
        self.vault.update_settings({SETTINGS_KEY: configs})
        self.status.showMessage(message, 5000)

    def _open_sessions(self) -> dict:
        """Every open session, keyed so each can be told apart, with its tenant."""
        sessions = {}
        for widget in self._session_tabs():
            info = getattr(widget, "connection_info", None)
            if isinstance(info, dict):
                key = getattr(widget, "_history_token", "") or f"tab-{id(widget)}"
                sessions[key] = str(info.get("tenant", "Default") or "Default")
        history = getattr(self, "session_history", None)
        if history is not None:
            # Includes Remote Desktop in its own window, which has no tab.
            for token, entry in getattr(history, "_open", {}).items():
                sessions[token] = str(entry.get("tenant", "Default") or "Default")
        return sessions

    def _external_sessions(self) -> dict:
        """Open sessions that outlive this window (Remote Desktop in its own window)."""
        history = getattr(self, "session_history", None)
        if history is None:
            return {}
        return {token: str(entry.get("tenant", "Default") or "Default")
                for token, entry in getattr(history, "_open", {}).items()
                if token not in self._history_widgets}

    def _disconnect_vpns_for_exit(self):
        gate = getattr(self, "vpn_gate", None)
        if gate is not None:
            try:
                gate.shutdown(self._external_sessions())
            except Exception:
                pass

    def _save_sidebar_density(self, density: str):
        try:
            self.vault.update_settings({"sidebar_density": density})
        except VaultError:
            pass

    def _show_shared_credentials(self):
        from app.credentials_dialog import CredentialsManagerDialog
        dialog = CredentialsManagerDialog(self.vault, self)
        dialog.exec()
        if dialog.changed:
            self._refresh_sidebar()

    def _show_recently_deleted(self):
        from app.data_safety import RecentlyDeletedDialog
        dialog = RecentlyDeletedDialog(self.vault, self)
        dialog.exec()
        if dialog.restored:
            self._refresh_sidebar()
            self.status.showMessage(f"Restored {dialog.restored} connection{'s' if dialog.restored != 1 else ''}", 4000)

    def _show_backups(self):
        from app.data_safety import BackupsDialog
        from app import backups
        dialog = BackupsDialog(self)
        dialog.exec()
        path = dialog.restore_requested_path
        if not path:
            return
        try:
            backups.restore_backup(path)
        except Exception as exc:
            QMessageBox.warning(self, "Restore backup", f"The backup could not be restored:\n{exc}")
            return
        QMessageBox.information(
            self, "Restore backup",
            "The backup is in place. RPMote will now restart — unlock it with "
            "the master password that was in use when the backup was taken.")
        self._restart_application()

    def _disconnect_connection(self, conn: dict):
        """Disconnect active tabs matching the selected saved connection."""
        closed = 0
        for widget in self._tab_widgets():
            if self._tab_matches_connection(widget, conn):
                self._end_session_tab(widget)
                closed += 1

        if closed:
            self.status.showMessage(f"Disconnected {closed} active session{'s' if closed != 1 else ''}", 3000)
        else:
            self.status.showMessage("No active session found for that connection", 3000)

        self._update_aura_activity()
        self._update_tab_count()
        self._refresh_sidebar()

    def _reconnect_connection(self, conn: dict):
        """Reconnect a saved connection: close matching active tabs, then open fresh."""
        closed = 0
        for widget in self._tab_widgets():
            if self._tab_matches_connection(widget, conn):
                self._end_session_tab(widget)
                closed += 1

        self._open_connection(conn)
        name = conn.get("name", "connection")
        if closed:
            self.status.showMessage(f"Reconnected {name} (closed {closed} active session{'s' if closed != 1 else ''})", 3500)
        else:
            self.status.showMessage(f"Connected {name}", 3000)

    def _reorder_connections(self, ordered_ids: list[str]):
        """Persist sidebar drag/drop ordering in the vault."""
        try:
            self.vault.reorder_connections(ordered_ids)
            self._refresh_sidebar()
            self.status.showMessage("Connection order updated", 2000)
        except VaultError as e:
            QMessageBox.warning(self, "Reorder Error", str(e))

    @staticmethod
    def _tab_matches_connection(widget, conn: dict) -> bool:
        """Best-effort tab↔connection matching for disconnect-from-sidebar."""
        if not widget or not conn:
            return False

        tab_conn = getattr(widget, "connection_info", None)
        if not isinstance(tab_conn, dict):
            return False

        tab_id = str(tab_conn.get("id", "")).strip()
        conn_id = str(conn.get("id", "")).strip()
        if tab_id and conn_id:
            return tab_id == conn_id

        # Fallback for ad-hoc/legacy entries without IDs
        key_fields = ("protocol", "host", "port", "username", "name")
        for field in key_fields:
            if str(tab_conn.get(field, "")).strip().lower() != str(conn.get(field, "")).strip().lower():
                return False
        return True

    #: Quick Connect's protocols: the ones that need nothing but a host.
    QUICK_PROTOCOLS = ("SSH", "Telnet", "SFTP", "FTP", "RDP", "VNC", "Web", "iDRAC", "NAS",
                       "Azure PowerShell")

    def _on_quick_proto_change(self, proto):
        self.quick_port.setValue(ConnectionDialog.PROTOCOL_PORTS.get(proto, 22) or 22)

    def _normalized_ssh_terminal_style(self, style_name: str) -> str:
        if style_name in SSHTerminal.TERMINAL_STYLES:
            return style_name
        return "System Theme"

    def _save_ssh_terminal_style(self, style_name: str):
        style_name = self._normalized_ssh_terminal_style(style_name)
        if style_name == self._ssh_terminal_style:
            return
        self._ssh_terminal_style = style_name
        try:
            self.vault.update_settings({"ssh_terminal_style": style_name})
        except Exception:
            pass

    def _save_ssh_terminal_font(self, font_name: str, size: int):
        font_name = SSHTerminal.normalized_font(font_name)
        size = SSHTerminal.normalized_font_size(size)
        if (font_name, size) == (self._ssh_terminal_font, self._ssh_font_size):
            return
        self._ssh_terminal_font = font_name
        self._ssh_font_size = size
        try:
            self.vault.update_settings({"ssh_terminal_font": font_name,
                                        "ssh_terminal_font_size": size})
        except Exception:
            pass

    def _save_rdp_resources(self, conn_id: str, resources: dict):
        """Remember which local devices an RDP connection may use.

        Asked once on first connect, then reused for every later session on that
        device. Quick-connect sessions have no vault record, so there is nothing
        to persist for them.
        """
        if not conn_id:
            return
        try:
            self.vault.update_connection(conn_id, {"rdp_resources": dict(resources)})
        except Exception:
            self.status.showMessage("Could not save local resource settings", 4000)

    def _with_credentials(self, conn: dict) -> dict:
        """*conn* with its shared credential (if any) filled in."""
        try:
            return self.vault.resolve_credentials(conn)
        except Exception:
            return conn

    def _resolved_connections(self) -> list:
        """Every saved connection with shared credentials filled in.

        For code that signs in to a machine other than the one being opened —
        a jump host, a restart server.
        """
        return [self._with_credentials(c) for c in self._safe_connections()]

    def _resolve_route(self, conn: dict) -> dict:
        """Attach the resolved jump-host chain to a connection about to open.

        Resolution needs every saved connection, which the session widgets do
        not have — so it happens once, here, and they are handed the answer.
        """
        if not conn.get("jump_via"):
            return conn
        try:
            from app import ssh_connect

            chain = ssh_connect.resolve_jump_chain(conn, self._resolved_connections())
        except Exception as exc:
            self.status.showMessage(f"Jump host: {exc}", 6000)
            return conn
        return {**conn, "jump_chain": chain} if chain else conn

    def _open_connection(self, conn: dict, force_new: bool = False):
        """Open a connection in a tab (or its own window, for external RDP).

        A connection that is already open is switched to rather than opened
        twice — a second RDP login usually takes over the first — unless the
        user asks for another session.
        """
        if not isinstance(conn, dict):
            return
        if not force_new and self._focus_existing_session(conn):
            return
        # Shared credentials are resolved here, at the last moment, so a
        # rotated password is picked up by every connection that uses it.
        try:
            conn = self.vault.resolve_credentials(conn)
        except Exception:
            pass
        before = getattr(self, "_before_open", None)
        if callable(before):
            # VPN-first and friends: they call back into _open_now when ready.
            if before(conn, lambda c=conn: self._open_now(c)):
                return
        self._open_now(conn)

    def _focus_existing_session(self, conn: dict) -> bool:
        conn_id = str(conn.get("id", "") or "")
        if not conn_id:
            return False
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            if widget is getattr(self, "dashboard", None):
                continue
            info = getattr(widget, "connection_info", None)
            if isinstance(info, dict) and str(info.get("id", "") or "") == conn_id:
                box = QMessageBox(self)
                box.setWindowTitle("Already open")
                box.setIcon(QMessageBox.Icon.Question)
                box.setText(f"“{conn.get('name', 'This connection')}” is already open.")
                if str(conn.get("protocol", "")).lower() == "rdp":
                    box.setInformativeText("A second Remote Desktop login usually takes "
                                           "over the first session.")
                switch = box.addButton("Switch to it", QMessageBox.ButtonRole.AcceptRole)
                another = box.addButton("Open another", QMessageBox.ButtonRole.ActionRole)
                box.addButton(QMessageBox.StandardButton.Cancel)
                from app.widgets import set_variant
                set_variant(switch, "primary")
                box.setDefaultButton(switch)
                box.exec()
                clicked = box.clickedButton()
                if clicked is switch:
                    self.tabs.setCurrentIndex(i)
                    return True
                if clicked is another:
                    return False
                return True  # cancelled: nothing to open
        return False

    def _add_session_tab(self, widget, conn: dict, label: str | None = None,
                         icon_name: str | None = None):
        """Add *widget* as a tab with its protocol icon and a useful tooltip."""
        from app.side_panel import PROTOCOL_COLORS
        proto = str(conn.get("protocol", "ssh") or "ssh").lower()
        name = label or conn.get("name") or conn.get("host") or proto.upper()
        icon = icons.icon(icon_name or icons.protocol_icon_name(proto),
                          PROTOCOL_COLORS.get(proto))
        index = self.tabs.addTab(widget, icon, str(name))
        host = str(conn.get("host", "") or "")
        port = conn.get("port", "")
        tip = [str(name)]
        if host:
            tip.append(f"{proto.upper()} · {host}" + (f":{port}" if port else ""))
        tip.append(f"Tenant: {conn.get('tenant', 'Default') or 'Default'}")
        self.tabs.setTabToolTip(index, "\n".join(tip))
        self.tabs.setCurrentWidget(widget)
        self._start_history(widget, conn)
        return index

    def _start_history(self, widget, conn: dict, process=None):
        history = getattr(self, "session_history", None)
        if history is None:
            return ""
        token = history.start(conn)
        if widget is not None and token:
            widget._history_token = token
            self._history_widgets[token] = widget
        if process is not None and token:
            history.watch_process(process, token)
        return token

    def _on_rdp_external_opened(self, process):
        token = getattr(self.sender(), "_history_token", "")
        if token:
            self._history_widgets.pop(token, None)
            self.session_history.watch_process(process, token)
        self.status.showMessage("Opened Remote Desktop in its own window", 3000)

    def _reconcile_history(self):
        """End the history entry of any tab that has gone, however it went."""
        history = getattr(self, "session_history", None)
        if history is None:
            return
        for token, widget in list(self._history_widgets.items()):
            gone = False
            try:
                # A closed tab's widget is deleted soon after it goes.
                gone = not _alive(widget) or self.tabs.indexOf(widget) < 0
            except RuntimeError:
                gone = True
            if gone:
                self._history_widgets.pop(token, None)
                history.end(token)

    def _open_now(self, conn: dict):
        """Build the session widget for *conn* (no more questions asked)."""
        protocol = conn.get("protocol", "ssh").lower()
        if protocol in ("ssh", "sftp"):
            conn = self._resolve_route(conn)
        tenant_name = conn.get("tenant", "Default") or "Default"
        if hasattr(self, "dashboard"):
            self.dashboard.reveal_tenant(tenant_name, exclusive=False, focus=False)

        if protocol in ("ssh", "telnet", "serial"):
            widget = TerminalWorkspace(
                conn,
                theme=self._current_theme,
                terminal_style=self._ssh_terminal_style,
                terminal_font=self._ssh_terminal_font,
                font_size=self._ssh_font_size,
                production_tenants=self._production_tenants,
                snippet_provider=self._palette_entries,
            )
            self._add_session_tab(widget, conn)
            widget.session_closed.connect(self._on_session_closed)
            widget.style_changed.connect(self._save_ssh_terminal_style)
            widget.font_changed.connect(self._save_ssh_terminal_font)
            widget.edit_connection_requested.connect(self._edit_from_session)
            # Auto-connect SFTP file browser when SSH connection is established
            for i in range(widget.splitter.count()):
                term = widget.splitter.widget(i)
                if hasattr(term, 'connection_established'):
                    term.connection_established.connect(self._on_ssh_connected)

        elif protocol == "sftp":
            widget = SFTPBrowser(conn)
            self._add_session_tab(widget, conn)
            widget.connect_sftp()
            widget.session_closed.connect(self._on_session_closed)

        elif protocol == "ftp":
            widget = FTPBrowser(conn)
            self._add_session_tab(widget, conn)
            widget.connect_ftp()
            widget.session_closed.connect(self._on_session_closed)

        elif protocol == "rdp":
            # The connection's own choice wins; "default" follows the
            # app-wide Open RDP Inside Tabs setting.
            from app.connection_manager import rdp_open_mode
            mode = rdp_open_mode(conn)
            use_embedded = (mode == "embedded" or
                            (mode == "default" and self.rdp_mode_toggle.isChecked()))
            conn_id = conn.get("id", "")
            if use_embedded:
                widget = RDPLauncher(conn, embedded_mode=True)
                self._add_session_tab(widget, conn)
                widget.session_closed.connect(self._on_session_closed)
                widget.external_opened.connect(self._on_rdp_external_opened)
                widget.resources_changed.connect(
                    lambda res, cid=conn_id: self._save_rdp_resources(cid, res)
                )
            else:
                launcher = RDPLauncher(conn, embedded_mode=False)
                launcher.resources_changed.connect(
                    lambda res, cid=conn_id: self._save_rdp_resources(cid, res)
                )
                launcher._launch_rdp()
                process = getattr(launcher, "last_process", None)
                if process is not None:
                    self._start_history(None, conn, process=process)
                launcher.deleteLater()
                self.status.showMessage("Opened RDP in its own window", 3000)

        elif protocol in ("idrac", "synology", "web"):
            widget = WebBrowserTab(conn, certificate_trust=self._certificate_trust)
            self._add_session_tab(widget, conn)
            widget.session_closed.connect(self._on_session_closed)
            widget.open_in_new_tab.connect(self._add_browser_child_tab)

        elif protocol == "azure_ps":
            widget = AzurePowerShellTerminal(conn)
            self._add_session_tab(widget, conn)
            widget.session_closed.connect(self._on_session_closed)

        elif protocol == "vnc":
            from app.vnc_client import VncViewerTab
            widget = VncViewerTab(conn)
            self._add_session_tab(widget, conn)
            widget.session_closed.connect(self._on_session_closed)
            widget.edit_requested.connect(
                lambda data, tab=widget: self._edit_from_session(tab, data))

        else:
            opener = getattr(self, f"_open_{protocol}", None)
            if callable(opener):
                opener(conn)
            else:
                self.status.showMessage(f"Don't know how to open “{protocol}” connections", 5000)

        self._update_aura_activity()
        self._update_tab_count()
        self._refresh_sidebar()

    def _quick_connect(self):
        host = self.quick_host.text().strip()
        if not host:
            return
        label = self.quick_protocol.currentText()
        protocol = ConnectionDialog.PROTOCOL_KEYS.get(label, label.lower())
        conn = {
            "name": host,
            "tenant": "Default",
            "protocol": protocol,
            "host": host,
            "port": self.quick_port.value(),
            "username": self.quick_user.text().strip(),
            "password": self.quick_pass.text(),
            "rdp_embedded": self.rdp_mode_toggle.isChecked(),
            "azure_auth_mode": "interactive",
            "azure_cloud": "AzureCloud",
        }
        self._open_connection(conn)

    def _on_rdp_mode_toggled(self, checked: bool):
        self._rdp_default_embedded = checked
        try:
            self.vault.update_settings({"rdp_open_embedded_default": checked})
        except VaultError:
            pass

    # ── Hardware acceleration ───────────────────────────────────────

    def _on_hw_accel_toggled(self, checked: bool):
        hardware_acceleration.set_saved_enabled(checked)
        if checked == hardware_acceleration.is_enabled():
            # Toggled back to what this process already runs with: nothing
            # to restart for.
            return
        mode = "on" if checked else "off"
        answer = QMessageBox.question(
            self, "Restart RPMote?",
            f"Hardware acceleration will be turned {mode} the next time\n"
            "RPMote starts.\n\n"
            "Restart now?  Open sessions will be closed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._restart_application()

    def _restart_application(self):
        """Close every session cleanly, then replace this process with a
        fresh RPMote so startup-only settings get re-read."""
        for w in self._tab_widgets():
            if w and hasattr(w, "cleanup"):
                try:
                    w.cleanup()
                except Exception:
                    pass
        self._disconnect_vpns_for_exit()
        python = sys.executable
        os.execv(python, [python] + sys.argv)

    # ── Tab Management ──────────────────────────────────────────────

    def _set_ssh_windows_visible(self, visible: bool):
        for widget in self._tab_widgets():
            if isinstance(widget, TerminalWorkspace):
                widget.set_terminal_windows_visible(visible)

    def _tab_widgets(self) -> list:
        """Every tab's widget, collected before any of them is cleaned up.

        Cleaning up an SSH, SFTP or FTP session disconnects it, and the
        disconnect closes its tab, so the indexes shift under a loop that
        walks them; a loop over this list reaches every session.
        """
        return [self.tabs.widget(i) for i in range(self.tabs.count())]

    def _close_tab(self, index):
        widget = self.tabs.widget(index)
        # The Dashboard tab is permanent. Tabs can be dragged, so it is told
        # apart by what it is, not by sitting at index 0.
        if widget is None or widget is getattr(self, "dashboard", None):
            return
        self._end_session_tab(widget)
        self._update_aura_activity()
        self._update_tab_count()
        self._refresh_sidebar()

    def _end_session_tab(self, widget):
        """Disconnect one session, take its tab away and free its widget.

        The caller refreshes the tab count and the sidebar afterwards.
        """
        if not _alive(widget) or widget is getattr(self, "dashboard", None):
            return
        if self.tabs.indexOf(widget) < 0:
            return
        was_current = widget is self.tabs.currentWidget()
        # This tab is closing already: the session_closed its cleanup emits
        # must not remove it a second time. That removal used to run first,
        # and a removeTab(index) after the cleanup then took the next tab
        # with it, still connected, so closing one tab closed two.
        try:
            widget.session_closed.disconnect(self._on_session_closed)
        except (AttributeError, TypeError, RuntimeError):
            pass
        if hasattr(widget, "cleanup"):
            widget.cleanup()
        index = self.tabs.indexOf(widget)
        if index >= 0:
            self.tabs.removeTab(index)
        self._after_tab_removed(widget, was_current)
        self._free_tab_widget(widget)

    def _close_current_tab(self):
        idx = self.tabs.currentIndex()
        if idx >= 0:            # _close_tab never closes the dashboard
            self._close_tab(idx)

    def _on_session_closed(self):
        sender = self.sender()
        if sender:
            # Prevent recursive calls by disconnecting the signal
            try:
                sender.session_closed.disconnect(self._on_session_closed)
            except:
                pass
            idx = self.tabs.indexOf(sender)
            if idx >= 0:
                was_current = idx == self.tabs.currentIndex()
                self.tabs.removeTab(idx)
                if hasattr(sender, "cleanup"):
                    sender.cleanup()
                self._after_tab_removed(sender, was_current)
                self._free_tab_widget(sender)
                self._update_aura_activity()
                self._update_tab_count()
                self._refresh_sidebar()

    def _free_tab_widget(self, widget):
        """Delete a closed session tab's widget.

        removeTab() only takes a page off the tab bar: the page stays a child
        of the tab widget, alive and hidden, until the window closes. Every
        session tab ever closed used to keep its terminal, browser page or
        desktop in memory for the rest of the run.
        """
        if not _alive(widget) or widget is getattr(self, "dashboard", None):
            return
        self._closed_tab_widgets.append(widget)
        self._free_closed_tabs()

    def _free_closed_tabs(self):
        # A session can end on its own while a dialog or menu it opened is
        # still up (an SSH drop under its own context menu). That dialog runs
        # an event loop of its own, the deletion would happen inside it, and
        # the code waiting on the dialog would come back to a deleted widget.
        # Wait for it to close.
        if (QApplication.activeModalWidget() is not None
                or QApplication.activePopupWidget() is not None):
            self._free_tabs_timer.start()
            return
        widgets, self._closed_tab_widgets = self._closed_tab_widgets, []
        for widget in widgets:
            if not _alive(widget) or self.tabs.indexOf(widget) >= 0:
                continue
            self._keep_tab_windows_open(widget)
            widget.deleteLater()

    def _keep_tab_windows_open(self, widget):
        """Move a window the tab opened (an SSH session's replay) to this one.

        Deleting the tab's widget deletes its children, windows included, and
        a replay the user is still watching must not vanish with the session.
        """
        for child in widget.findChildren(QWidget):
            parent = child.parentWidget()
            # Windows opened from inside one of these move along with it.
            if (not child.isWindow() or not child.isVisible()
                    or parent is None or parent.window() is not self):
                continue
            geometry = child.geometry()
            child.setParent(self, child.windowFlags())
            child.setGeometry(geometry)
            child.show()

    def _after_tab_removed(self, widget, was_current: bool):
        """Keep browser popup tabs and the tab that opened them in step.

        Closing a popup the user was looking at lands them back on the console
        that opened it — QTabWidget would otherwise pick whichever neighbour
        happens to sit next to it. Closing the console itself takes the way
        back away from any popups it still has open.
        """
        if widget is None:
            return
        if was_current:
            opener = getattr(widget, "opener_tab", None)
            if _alive(opener) and self.tabs.indexOf(opener) >= 0:
                self.tabs.setCurrentWidget(opener)
        for i in range(self.tabs.count()):
            child = self.tabs.widget(i)
            if getattr(child, "opener_tab", None) is widget:
                child.opener_closed()

    def _add_browser_child_tab(self, child_tab):
        """Handle a WebBrowserTab requesting a new tab (target=_blank, window.open)."""
        preset = child_tab._preset
        name = child_tab.connection_info.get("name", preset["label"])
        origin = f"{name}"
        proto = str(child_tab.connection_info.get("protocol", "web") or "web").lower()
        from app.side_panel import PROTOCOL_COLORS
        # "↗" marks the tab as one the console opened, so it can be told from
        # the console tab itself, which carries the plain connection name.
        self.tabs.addTab(child_tab, icons.icon(icons.protocol_icon_name(proto),
                                               PROTOCOL_COLORS.get(proto)),
                         f"{origin} ↗")
        self.tabs.setTabToolTip(self.tabs.indexOf(child_tab),
                                self._child_tab_tooltip(child_tab, origin))
        self.tabs.setCurrentWidget(child_tab)
        child_tab.session_closed.connect(self._on_session_closed)
        child_tab.open_in_new_tab.connect(self._add_browser_child_tab)
        child_tab.return_to_opener.connect(self._focus_browser_opener)
        child_tab.title_changed.connect(
            lambda title, tab=child_tab, origin=origin:
            self._on_browser_child_title(tab, origin, title))
        self._update_tab_count()

    def _focus_browser_opener(self, opener):
        """A popup tab's "Back to …" button: show the tab that opened it."""
        idx = self.tabs.indexOf(opener) if _alive(opener) else -1
        if idx >= 0:
            self.tabs.setCurrentIndex(idx)
            return
        # The opener was closed (and maybe deleted) under it — stop offering
        # a way back.
        child = self.sender()
        if child is not None and hasattr(child, "opener_closed"):
            child.opener_closed()
        self.status.showMessage("The tab that opened this window has been closed", 4000)

    # Page titles run long ("File Station - Home NAS - Synology NAS"); the tab
    # keeps only enough to tell popups apart, the tooltip carries the rest.
    _CHILD_TAB_TITLE_MAX = 28

    @staticmethod
    def _child_tab_tooltip(child_tab, origin: str, title: str = "") -> str:
        label = child_tab.opener_label()
        way_back = (f"Opened from {origin} — use “Back to {label}” or close this tab to return"
                    if label else f"Opened from {origin}")
        return f"{title}\n{way_back}" if title else way_back

    def _on_browser_child_title(self, child_tab, origin: str, title: str):
        idx = self.tabs.indexOf(child_tab)
        if idx < 0:
            return
        opener = getattr(child_tab, "opener_tab", None)
        if opener is not None and not _alive(opener):
            # Closed and deleted without this window hearing of it.
            child_tab.opener_closed()
        title = " ".join(str(title or "").split())
        if not title:
            self.tabs.setTabText(idx, f"{origin} ↗")
            self.tabs.setTabToolTip(idx, self._child_tab_tooltip(child_tab, origin))
            return
        short = title
        if len(short) > self._CHILD_TAB_TITLE_MAX:
            short = short[: self._CHILD_TAB_TITLE_MAX - 1].rstrip() + "…"
        self.tabs.setTabText(idx, f"{origin} ↗ {short}")
        self.tabs.setTabToolTip(idx, self._child_tab_tooltip(child_tab, origin, title))

    def _collect_active_connection_ids(self) -> set[str]:
        """Collect saved-connection IDs currently represented by open tabs."""
        active_ids: set[str] = set()
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            conn = getattr(widget, "connection_info", None)
            if isinstance(conn, dict):
                conn_id = str(conn.get("id", "")).strip()
                if conn_id:
                    active_ids.add(conn_id)
        return active_ids

    def _collect_active_connection_signatures(self) -> set[tuple]:
        """Collect fallback signatures for active tabs when IDs are unavailable."""
        active_sigs: set[tuple] = set()
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            conn = getattr(widget, "connection_info", None)
            if not isinstance(conn, dict):
                continue
            sig = (
                str(conn.get("protocol", "")).strip().lower(),
                str(conn.get("host", "")).strip().lower(),
                str(conn.get("port", "")).strip().lower(),
                str(conn.get("username", "")).strip().lower(),
                str(conn.get("name", "")).strip().lower(),
            )
            if any(sig):
                active_sigs.add(sig)
        return active_sigs

    def _collect_active_tenants(self) -> set[str]:
        """Collect tenant names that have at least one open connection tab."""
        active_tenants: set[str] = set()
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            conn = getattr(widget, "connection_info", None)
            if isinstance(conn, dict):
                tenant = conn.get("tenant", "Default") or "Default"
                active_tenants.add(tenant)
        return active_tenants

    # ── Sidebar ─────────────────────────────────────────────────────

    def _refresh_sidebar(self):
        try:
            conns = self.vault.connections
            tenant_colors = self.vault.settings.get("tenant_colors", {})
            self.side_panel.set_tenant_colors(tenant_colors)
            # Set active IDs/signatures BEFORE rebuilding tree so dots are correct
            self.side_panel.set_active_connection_ids(self._collect_active_connection_ids())
            self.side_panel.set_active_connection_signatures(self._collect_active_connection_signatures())
            self.side_panel.set_tenant_notes(self.vault.settings.get("tenant_notes", {}))
            monitor = getattr(self, "reachability", None)
            if monitor is not None:
                self.side_panel.set_conn_statuses(monitor.statuses())
            self.side_panel.update_connections(conns)
            self._refresh_home_panel()
            # Update dashboard if it exists
            if hasattr(self, "dashboard"):
                self.dashboard.set_data(conns, tenant_colors)
                monitor = getattr(self, "reachability", None)
                if monitor is not None:
                    monitor.set_connections(conns)
                self._prune_restart_config(conns)
                self.dashboard.set_restart_relations(*self._restart_config())
                self.dashboard.set_active_tenants(self._collect_active_tenants())
                self.dashboard.set_active_connections(
                    self._collect_active_connection_ids())
                # Reload node positions (tenant list may have changed)
                saved_pos = self.vault.settings.get("dashboard_node_positions", {})
                if saved_pos:
                    self.dashboard.set_node_positions(saved_pos)
        except VaultError:
            pass

    def _toggle_sidebar(self):
        isVisible = self.side_panel.isVisible()
        
        if not hasattr(self, "_sidebar_anim"):
            self._sidebar_anim = QPropertyAnimation(self.side_panel, b"maximumWidth")
            self._sidebar_anim.setDuration(300)
            self._sidebar_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        # Always disconnect stale handlers first to prevent accumulation
        try:
            self._sidebar_anim.finished.disconnect()
        except Exception:
            pass

        if isVisible:
            self._sidebar_anim.setStartValue(self.side_panel.width())
            self._sidebar_anim.setEndValue(0)
            self._sidebar_anim.finished.connect(lambda: self.side_panel.hide())
            self._sidebar_anim.start()
        else:
            self.side_panel.show()
            self._sidebar_anim.setStartValue(0)
            self._sidebar_anim.setEndValue(250)
            self._sidebar_anim.start()

    def _toggle_snippets(self):
        isVisible = self.snippets_panel.isVisible()
        if isVisible:
            self.snippets_panel.hide()
        else:
            self.snippets_panel.show()
            self.snippets_panel.raise_()

    def _update_file_browser_sftp(self):
        """Pass the active SSH tab's client to the file browser."""
        idx = self.tabs.currentIndex()
        if idx < 0:
            self.snippets_panel.file_browser.disconnect_sftp()
            return
        widget = self.tabs.widget(idx)
        # TerminalWorkspace wraps SSH terminals and holds the ssh_client
        proto = str(getattr(widget, "connection_info", {}).get("protocol", "ssh") or "ssh").lower()
        if proto != "ssh":
            self.snippets_panel.file_browser.disconnect_sftp()
        elif isinstance(widget, TerminalWorkspace) and widget.ssh_client:
            self.snippets_panel.file_browser.set_ssh_client(widget.ssh_client, widget.connection_info)
        elif isinstance(widget, SSHTerminal) and widget._connected and widget.client:
            self.snippets_panel.file_browser.set_ssh_client(widget.client, widget.connection_info)
        else:
            self.snippets_panel.file_browser.disconnect_sftp()

    def _on_ssh_connected(self):
        """Called when an SSH terminal establishes its connection — auto-connect SFTP."""
        # Small delay to let paramiko fully initialize
        QTimer.singleShot(500, self._update_file_browser_sftp)

    # ── Theme ───────────────────────────────────────────────────────

    def _toggle_theme(self):
        """Ctrl+T / the palette button: step to the next theme in the list."""
        self._select_theme((self._current_theme_index + 1) % len(self._themes))

    def _select_theme(self, index: int):
        """Switch to the theme at *index*, remember it, and restyle everything."""
        if not 0 <= index < len(self._themes):
            return
        self._current_theme_index = index
        self._current_theme = self._themes[index]
        try:
            self.vault.update_settings({"theme_index": self._current_theme_index})
        except Exception:
            pass
        actions = getattr(self, "_theme_actions", None)
        if actions and not actions[index].isChecked():
            actions[index].setChecked(True)
        self._apply_theme()
        
        # Micro-animation: Icon rotation
        if not hasattr(self, "_theme_btn_shadow"):
            self._theme_btn_shadow = QGraphicsDropShadowEffect(self.btn_theme)
            self._theme_btn_shadow.setBlurRadius(15)
            self._theme_btn_shadow.setOffset(0, 0)
            self.btn_theme.setGraphicsEffect(self._theme_btn_shadow)
        
        self._theme_btn_shadow.setColor(parse_color(self._current_theme['accent']))
        self._theme_btn_shadow.setColor(parse_color(self._current_theme['accent']))

        # Update tabs
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if hasattr(w, "set_theme"):
                w.set_theme(self._current_theme)

    def _apply_theme(self):
        # Application-wide, so dialogs and message boxes opened without a
        # parent wear the theme too instead of falling back to the system look.
        css = generate_stylesheet(self._current_theme)
        app = QApplication.instance()
        if app is not None:
            # Setting it re-polishes every widget in the process, so only do
            # that when the theme actually changed.
            if app.styleSheet() != css:
                app.setStyleSheet(css)
        else:
            self.setStyleSheet(css)
        if app is not None:
            # The theme goes into the palette too. Widgets that style
            # themselves read the accent from it (themes.accent_rgba), and
            # anything Qt fills with the Window colour — the page inside
            # every scroll area, for one — gets the theme's surface instead
            # of a fixed navy that only suited the Dark theme.
            from PyQt6.QtGui import QPalette
            from app.themes import solid
            theme = self._current_theme
            accent = parse_color(theme["accent"])
            accent.setAlpha(255)
            wanted = {
                QPalette.ColorRole.Highlight: accent,
                QPalette.ColorRole.Link: accent,
                QPalette.ColorRole.HighlightedText: QColor(button_text_color(theme)),
                QPalette.ColorRole.Window: QColor(solid(theme.get("bg_secondary",
                                                                  theme["bg_primary"]))),
                QPalette.ColorRole.WindowText: QColor(solid(theme["text_primary"])),
            }
            palette = app.palette()
            if any(palette.color(role) != colour for role, colour in wanted.items()):
                for role, colour in wanted.items():
                    palette.setColor(role, colour)
                app.setPalette(palette)
        quick_go = getattr(self, "btn_quick_go", None)
        if quick_go is not None:
            quick_go.setIcon(icons.icon("bolt", button_text_color(self._current_theme)))
        content = getattr(self, "main_content", None)
        if content is not None:
            content_css = transparent_container_stylesheet(self._current_theme)
            if content.styleSheet() != content_css:
                content.setStyleSheet(content_css)
        if hasattr(self, "aura"):
            self.aura.set_theme(self._current_theme)
        if hasattr(self, "dashboard"):
            self.dashboard.set_theme(self._current_theme)
        if hasattr(self, "side_panel"):
            self.side_panel.set_theme(self._current_theme)
        if getattr(self, "home_panel", None) is not None:
            self.home_panel.set_theme(self._current_theme)

    # ── Vault Actions ───────────────────────────────────────────────

    def _show_saved_passwords(self, initial_filter: str = ""):
        """Open the credential review dialog for saved connections."""
        SavedPasswordsDialog(
            self.vault, parent=self, initial_filter=initial_filter
        ).exec()

    def _show_connection_password(self, conn: dict):
        """Open the credential review dialog focused on one connection."""
        self._show_saved_passwords(str(conn.get("name", "") or ""))

    def _manage_recovery_key(self):
        """Create, replace or revoke the vault's recovery key."""
        from app.recovery import offer_recovery_key

        if not self.vault.has_recovery_key:
            box = QMessageBox(self)
            box.setWindowTitle("Recovery Key")
            box.setIcon(QMessageBox.Icon.Information)
            box.setText(
                "<h3>No recovery key set</h3>"
                "<p>A recovery key is the only way back into your vault if you "
                "forget your master password. RPMote keeps no copy of either, "
                "so without one a forgotten password means starting over.</p>"
            )
            create = box.addButton("Create Recovery Key",
                                   QMessageBox.ButtonRole.AcceptRole)
            box.addButton("Not Now", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is create:
                offer_recovery_key(self.vault, parent=self, prompt=False)
            return

        box = QMessageBox(self)
        box.setWindowTitle("Recovery Key")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            "<h3>A recovery key is set for this vault</h3>"
            "<p>The key itself is not stored, so it cannot be shown again. "
            "If you have lost it, generate a new one — the old key stops "
            "working immediately.</p>"
        )
        regen = box.addButton("Generate New Key", QMessageBox.ButtonRole.AcceptRole)
        remove = box.addButton("Remove Key", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        box.exec()

        if box.clickedButton() is regen:
            offer_recovery_key(self.vault, parent=self, prompt=False)
        elif box.clickedButton() is remove:
            confirm = QMessageBox.question(
                self, "Remove Recovery Key",
                "Remove the recovery key?\n\n"
                "If you then forget your master password, your vault cannot be "
                "recovered by anyone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if confirm == QMessageBox.StandardButton.Yes:
                try:
                    self.vault.clear_recovery_key()
                    self.status.showMessage("Recovery key removed", 4000)
                except VaultError as exc:
                    QMessageBox.warning(self, "Recovery Key", str(exc))

    def _change_password(self):
        from PyQt6.QtWidgets import QInputDialog
        old_pw, ok = QInputDialog.getText(
            self, "Change Master Password", "Current password:",
            QLineEdit.EchoMode.Password
        )
        if not ok:
            return
        new_pw, ok = QInputDialog.getText(
            self, "Change Master Password",
            f"New password (at least {MIN_MASTER_PASSWORD_LEN} characters):",
            QLineEdit.EchoMode.Password
        )
        if not ok:
            return
        try:
            self.vault.change_password(old_pw, new_pw)
            QMessageBox.information(self, "Success", "Master password changed.")
        except VaultError as e:
            QMessageBox.warning(self, "Error", str(e))

    def _lock_vault(self):
        # Close first, lock second: closeEvent may ask about open sessions,
        # and a declined close must leave the vault usable.
        self._close_reason = "lock_vault"
        if self.close():
            self.vault.lock()

    def _exit_app(self):
        self._close_reason = "exit_action"
        self.close()

    def close_without_prompt(self, reason: str = "lock_screen.quit") -> bool:
        """Close for a caller that has already made sure the user means it."""
        self._close_reason = reason
        self._skip_close_prompt = True
        return self.close()

    # ── Shutdown ────────────────────────────────────────────────────

    @staticmethod
    def _tab_is_live(widget) -> bool:
        """Does this tab still hold an open connection?"""
        if isinstance(widget, TerminalWorkspace):
            return any(getattr(t, "_connected", False) for t in widget.terminals())
        if isinstance(widget, RDPLauncher):
            button = getattr(widget, "btn_disconnect", None)
            return button is not None and button.isEnabled()
        if isinstance(widget, AzurePowerShellTerminal):
            return widget.has_open_session()
        return bool(getattr(widget, "_connected", False))

    def _live_sessions(self) -> list:
        return [
            self.tabs.tabText(i)
            for i in range(self.tabs.count())
            if self._tab_is_live(self.tabs.widget(i))
        ]

    def _confirm_close(self, live: list) -> bool:
        """Ask before a close that would drop open sessions.

        Ctrl+L is "clear screen" in every shell and Ctrl+Q is "quit" in every
        editor; pressed with focus in the file browser or the sidebar instead
        of the terminal they reach the window's Lock Vault / Exit actions and
        silently take every session down.  The prompt turns that slip into a
        question.  Windows shutting down gets no question: the answer would
        never be seen.
        """
        if not live:
            return True
        app = QApplication.instance()
        if app is not None and app.isSavingSession():
            return True
        names = "\n".join(f"  •  {name}" for name in live[:8])
        if len(live) > 8:
            names += f"\n  •  … and {len(live) - 8} more"
        answer = QMessageBox.question(
            self, "Close RPMote?",
            f"{len(live)} session{'s are' if len(live) != 1 else ' is'} still "
            f"connected and will be disconnected:\n\n{names}\n\nClose anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _show_settings(self, page: str | None = None):
        from app.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self, page=page)
        dialog.exec()
        dialog.deleteLater()

    def _open_log_folder(self):
        from pathlib import Path
        from app.crash_logging import get_crash_log_path
        path = get_crash_log_path()
        folder = Path(path).parent if path else Path(os.environ.get("LOCALAPPDATA", "")) / "RPMote" / "logs"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))   # noqa: S606 — the user's own log folder
        except OSError as exc:
            QMessageBox.warning(self, "Logs", f"Could not open {folder}:\n{exc}")

    def _apply_terminal_appearance(self, style: str | None = None, font: str | None = None,
                                   size: int | None = None):
        """Change the terminal look everywhere: saved, and on every open pane."""
        if style is not None:
            style = self._normalized_ssh_terminal_style(style)
            self._save_ssh_terminal_style(style)
        if font is not None or size is not None:
            self._save_ssh_terminal_font(
                self._ssh_terminal_font if font is None else font,
                self._ssh_font_size if size is None else size)
        for i in range(self.tabs.count()):
            workspace = self.tabs.widget(i)
            if not isinstance(workspace, TerminalWorkspace):
                continue
            if style is not None:
                workspace.terminal_style = style
            if font is not None:
                workspace.terminal_font = self._ssh_terminal_font
            if size is not None:
                workspace.font_size = self._ssh_font_size
            for terminal in workspace.terminals():
                if style is not None:
                    terminal.set_terminal_style(style)
                if font is not None:
                    terminal.set_terminal_font_choice(self._ssh_terminal_font)
                if size is not None:
                    terminal.set_terminal_font_size(self._ssh_font_size)

    @staticmethod
    def _purge_windows_credentials():
        """Nothing RPMote put in Credential Manager should outlive the vault
        being open; this is the backstop behind the per-session release."""
        try:
            from app.windows_credentials import purge_saved_credentials
            purge_saved_credentials()
        except Exception:
            logging.getLogger("rpmote").exception("credential sweep failed")

    def _factory_reset(self):
        """Two-step confirmation factory reset: wipe everything and restart fresh."""
        import sys, os

        # ── Step 1: Strong warning ──────────────────────────────────
        warn = QMessageBox(self)
        warn.setWindowTitle("Factory Reset")
        warn.setIcon(QMessageBox.Icon.Warning)
        warn.setText(
            "<h3>You are about to permanently delete ALL data</h3>"
            "<p>This will erase:</p>"
            "<ul>"
            "<li>All saved connections and credentials</li>"
            "<li>All command snippets</li>"
            "<li>All application settings</li>"
            "<li>Your master password</li>"
            "</ul>"
            "<p style='color:#ef4444; font-weight:bold;'>"
            "This action is irreversible. There is no undo.</p>"
        )
        warn.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        warn.setDefaultButton(QMessageBox.StandardButton.Cancel)
        warn.button(QMessageBox.StandardButton.Yes).setText("Yes, I understand")
        if warn.exec() != QMessageBox.StandardButton.Yes:
            return

        # ── Step 2: Type "DELETE" to confirm ────────────────────────
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(
            self,
            "Confirm Factory Reset",
            "Type DELETE to confirm and erase everything:",
        )
        if not ok or text.strip() != "DELETE":
            QMessageBox.information(
                self, "Cancelled",
                "Factory reset was cancelled. No data was deleted."
            )
            return

        # ── Perform the reset ───────────────────────────────────────
        # Close all active sessions first
        for w in self._tab_widgets():
            if w and hasattr(w, "cleanup"):
                w.cleanup()

        self.vault.factory_reset()
        self._purge_windows_credentials()

        QMessageBox.information(
            self, "Factory Reset Complete",
            "All data has been erased.\n\n"
            "The application will now restart so you can\n"
            "create a new master password."
        )

        # Restart the application process
        python = sys.executable
        os.execv(python, [python] + sys.argv)

    def _import_connections(self):
        filepath, _ = QFileDialog.getOpenFileName(
            self, "Import Connections", "", "Encrypted Files (*.rpvault);;All Files (*)"
        )
        if not filepath:
            return
        from PyQt6.QtWidgets import QInputDialog
        pw, ok = QInputDialog.getText(
            self, "Import", "Enter the export password:",
            QLineEdit.EchoMode.Password
        )
        if not ok:
            return
        try:
            count = self.vault.import_connections(filepath, pw)
            self._refresh_sidebar()
            QMessageBox.information(self, "Import", f"Imported/updated {count} connections.")
        except VaultError as e:
            QMessageBox.warning(self, "Import Error", str(e))

    def _import_ssh_config(self):
        """Offer everything in ~/.ssh/config that is not already saved."""
        from app import ssh_config_import

        path = ssh_config_import.default_config_path()
        try:
            entries = ssh_config_import.read_importable(
                path, self.vault.connections)
        except Exception as exc:
            QMessageBox.warning(self, "Import from ssh config",
                                f"Could not read {path}:\n\n{exc}")
            return

        if not entries:
            QMessageBox.information(
                self, "Import from ssh config",
                f"Nothing new to import from {path}.\n\n"
                f"Either the file has no host entries, or every host in it is "
                f"already saved here.")
            return

        shown = "\n".join(f"  • {e['name']}  —  {e['username'] or '?'}@{e['host']}"
                           for e in entries[:15])
        if len(entries) > 15:
            shown += f"\n  … and {len(entries) - 15} more"
        answer = QMessageBox.question(
            self, "Import from ssh config",
            f"Import {len(entries)} connection(s) from {path}?\n\n{shown}\n\n"
            f"They will be filed under the \"{ssh_config_import.DEFAULT_TENANT}\" "
            f"tenant. Passwords are not stored in an ssh config, so each one "
            f"will use its key file or ask when it connects.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        # Two passes: everything gets an id first, then ProxyJump links are
        # pointed at those ids rather than at names.
        ids_by_name = {}
        for entry in entries:
            record = {k: v for k, v in entry.items() if k != "jump_via"}
            ids_by_name[entry["name"]] = self.vault.add_connection(record)
        for entry in entries:
            hop_name = entry.get("jump_via")
            if hop_name and hop_name in ids_by_name:
                self.vault.update_connection(
                    ids_by_name[entry["name"]],
                    {"jump_via": ids_by_name[hop_name]})

        self._refresh_sidebar()
        self.status.showMessage(
            f"Imported {len(entries)} connection(s) from ssh config", 5000)

    def _export_connections(self):
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Export Connections", "connections.rpvault",
            "Encrypted Files (*.rpvault);;All Files (*)"
        )
        if not filepath:
            return
        # An export is every credential in the vault in one portable file;
        # the same re-authentication that guards a single reveal guards it.
        if not confirm_master_password(
            self, self.vault, "You are about to export every saved credential."
        ):
            return
        from PyQt6.QtWidgets import QInputDialog
        pw, ok = QInputDialog.getText(
            self, "Export",
            f"Set an export password (at least {MIN_MASTER_PASSWORD_LEN} characters):",
            QLineEdit.EchoMode.Password
        )
        if not ok or not pw:
            return
        try:
            self.vault.export_connections(filepath, pw)
            QMessageBox.information(self, "Export", "Connections exported successfully.")
        except VaultError as e:
            QMessageBox.warning(self, "Export Error", str(e))

    def _repair_webengine(self):
        """Reinstall PyQt6-WebEngine to match the running PyQt6.

        WebEngine ships as a compiled extension bound to one PyQt6 minor, so the
        pin has to track PYQT_VERSION_STR — a fixed version here is exactly what
        produces the 'could not be loaded' failure it is meant to repair.
        """
        from PyQt6.QtCore import PYQT_VERSION_STR

        target = f"PyQt6-WebEngine=={PYQT_VERSION_STR}"

        # In a PyInstaller build WebEngine lives in _internal/PyQt6 and there is no
        # pip to reach it; installing would only touch some unrelated interpreter.
        if getattr(sys, "frozen", False):
            QMessageBox.information(
                self,
                "Repair WebEngine",
                "This is a packaged build, so pip cannot repair it.\n\n"
                "The browser engine is bundled inside the installation folder, not "
                "installed from PyPI. If the NAS/iDRAC browser is unavailable here, "
                "the build itself is missing or has a mismatched WebEngine and needs "
                "to be rebuilt and reinstalled.\n\n"
                "Run this option from a source checkout instead.",
            )
            return

        from app.web_browser import _ensure_webengine

        if _ensure_webengine():
            QMessageBox.information(
                self,
                "Repair WebEngine",
                f"WebEngine is already loading correctly (PyQt6 {PYQT_VERSION_STR}).\n\n"
                "No repair needed.",
            )
            return

        reply = QMessageBox.question(
            self,
            "Repair WebEngine",
            f"Install {target} into:\n{sys.executable}\n\n"
            "RPMote must be restarted afterwards for the browser tabs to load.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", target],
                capture_output=True,
                text=True,
                timeout=600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(
                self,
                "Repair WebEngine",
                f"Could not run pip.\n\n{type(exc).__name__}: {exc}",
            )
            return
        QApplication.restoreOverrideCursor()

        if proc.returncode == 0:
            QMessageBox.information(
                self,
                "Repair WebEngine",
                f"Installed {target}.\n\nRestart RPMote to load the browser tabs.",
            )
        else:
            detail = (proc.stderr or proc.stdout or "").strip()
            QMessageBox.critical(
                self,
                "Repair WebEngine",
                f"pip failed (exit {proc.returncode}).\n\n{detail[-1500:]}",
            )

    def _run_rdp_setup_manual(self):
        reply = QMessageBox.question(
            self,
            "RDP ActiveX Setup",
            "Run Microsoft RDP ActiveX setup now?\n\n"
            "This may require Administrator privileges.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        result = ensure_activex_registered_once(force=True)
        status = result.get("status", "unknown")
        if status == "ok":
            QMessageBox.information(
                self,
                "RDP ActiveX Setup",
                "RDP ActiveX appears ready for embedded mode.",
            )
        elif status == "qt_ax_missing":
            qt_err = result.get("qt_ax_error", "Unknown Qt ActiveX import error")
            QMessageBox.critical(
                self,
                "RDP ActiveX Setup",
                "Qt ActiveX runtime is missing in this app build.\n"
                "Embedded mode cannot work until the app is rebuilt with ActiveX support.\n\n"
                f"Details: {qt_err}",
            )
        else:
            details = []
            for item in result.get("attempts", []):
                details.append(f"- {item.get('message', 'No message')}")
            detail_text = "\n".join(details) if details else "No registration attempts were recorded."
            QMessageBox.warning(
                self,
                "RDP ActiveX Setup",
                "Setup completed but embedded ActiveX is still unavailable.\n"
                "Try running the app as Administrator and rerun setup.\n\n"
                f"Attempt details:\n{detail_text}",
            )

    # ── Dialogs ─────────────────────────────────────────────────────

    def _show_about(self):
        from app.connection_manager import ConnectionDialog
        protocols = " • ".join(ConnectionDialog.PROTOCOL_KEYS)
        QMessageBox.about(self, "About RPMote",
            "<h2>✦ RPMote</h2>"
            "<p>Remote Connection Manager</p>"
            "<p><i>Every remote. One mote.</i></p>"
            f"<p>{protocols}</p>"
            # Fernet is AES-128-CBC with an HMAC-SHA256 tag; say what it is.
            "<p>Vault encrypted with Fernet (AES-128-CBC + HMAC-SHA256), "
            "key derived from your master password with PBKDF2-SHA256 "
            "(600,000 iterations).</p>"
            f"<p style='color:#94a3b8;'>v{APP_VERSION}<br>"
            f"Rendering: {hardware_acceleration.describe()}</p>"
        )

    def _show_shortcuts(self):
        from app.shortcuts_dialog import ShortcutsDialog
        ShortcutsDialog(self).exec()

    # ── Power Management (Sleep / Hibernate) ─────────────────────

    def _start_power_monitor(self):
        """Start a lightweight timer that detects sleep/hibernate by gap."""
        self._power_last_tick = time.monotonic()
        self._power_suspended = False
        self._power_timer = QTimer(self)
        self._power_timer.setInterval(2000)
        self._power_timer.timeout.connect(self._power_tick)
        self._power_timer.start()

    def _power_tick(self):
        now = time.monotonic()
        elapsed = now - self._power_last_tick
        self._power_last_tick = now
        # Timer fires every 2s. On modern Windows with background throttling,
        # an inactive window can legitimately delay timer callbacks by many
        # seconds without the system ever sleeping. A low threshold triggers
        # false-positive wake handlers that tear down every session — which
        # is itself a crash vector on ActiveX/WebEngine widgets.
        # 45s is well above any throttle pause but well below any real sleep.
        if elapsed > 45:
            self._on_sleep_wake_detected()

    def _on_sleep_wake_detected(self):
        """System just woke from sleep/hibernate — clean up and reconnect."""
        self.status.showMessage("System wake detected \u2014 cleaning up stale sessions...", 5000)
        # Immediately suspend (stop timers, close dead sockets)
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            if widget and hasattr(widget, "suspend"):
                try:
                    widget.suspend()
                except Exception:
                    pass
        # Delay reconnection to let networking reinitialize
        QTimer.singleShot(3000, self._resume_all_connections)

    def _resume_all_connections(self):
        self.status.showMessage("Reconnecting sessions...", 5000)
        for i in range(self.tabs.count()):
            widget = self.tabs.widget(i)
            if widget and hasattr(widget, "resume"):
                try:
                    widget.resume()
                except Exception:
                    pass
        self.status.showMessage("Reconnection complete", 3000)
        self._refresh_sidebar()

    def closeEvent(self, event):
        """Clean up all tabs before closing."""
        # "window" is the title-bar X, Alt+F4 or the OS; the actions that
        # close programmatically name themselves first.
        reason = getattr(self, "_close_reason", None) or "window"
        self._close_reason = None
        skip_prompt = getattr(self, "_skip_close_prompt", False)
        self._skip_close_prompt = False
        live = self._live_sessions()
        focus = QApplication.focusWidget()
        focus_name = type(focus).__name__ if focus is not None else None
        if not skip_prompt and not self._confirm_close(live):
            log_runtime_event(
                "shutdown.declined", reason=reason, live_sessions=len(live),
                focus=focus_name,
            )
            event.ignore()
            return
        log_runtime_event(
            "shutdown.close_event", reason=reason, live_sessions=len(live),
            tabs=self.tabs.count(), focus=focus_name,
        )
        # Timers queued by a wake or the login sweep can still fire while the
        # window tears down; this is what tells them not to touch anything.
        self._closing = True
        self.idle_lock.shutdown()
        if hasattr(self, "dashboard"):
            self._save_dashboard_view_state(self.dashboard.get_view_state())
        # Probe threads hold a reference to this window's slots; detach them
        # before Qt tears the C++ side down.
        monitor = getattr(self, "reachability", None)
        if monitor is not None:
            monitor.shutdown()
        runner = getattr(self, "restart_runner", None)
        if runner is not None:
            runner.shutdown()
        waker = getattr(self, "wake_runner", None)
        if waker is not None:
            waker.shutdown()
        if getattr(self, "_sudo_cache", None):
            self._sudo_cache.clear()
        for w in self._tab_widgets():
            if w and hasattr(w, "cleanup"):
                w.cleanup()
        # The sessions that needed a VPN are gone now; so is the VPN. Before
        # end_all(), which forgets the Remote Desktop windows still using one.
        self._disconnect_vpns_for_exit()
        # Sessions end with the window; write their end times while the
        # vault is still open.
        history = getattr(self, "session_history", None)
        if history is not None:
            try:
                history.end_all()
            except Exception:
                pass
        self._purge_windows_credentials()
        event.accept()


def parse_color(color_str: str) -> QColor:
    """Safely parse hex or rgba(r,g,b,a) strings into QColor."""
    if not color_str:
        return QColor(0, 0, 0, 0)
    if color_str.startswith("rgba"):
        try:
            # Handle rgba(r, g, b, a)
            import re
            m = re.match(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d\.]+)\s*\)", color_str)
            if m:
                r, g, b, a = m.groups()
                return QColor(int(r), int(g), int(b), int(float(a) * 255))
        except:
            pass
    return QColor(color_str)


class BackgroundAura(QWidget):
    """Dynamic, slowly shifting glowing background for Project Lumina."""

    #: Every tick dirties the backdrop under the whole window, and with the
    #: GPU compositing the window that means re-uploading it.  The glow
    #: drifts a few pixels a tick on a gradient a thousand pixels wide, so
    #: 10 Hz with a doubled step moves exactly as before at half the cost.
    INTERVAL_MS = 100
    STEP = 0.02

    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme = MOTE_THEME
        self._pos1 = QPointF(0.2, 0.2)
        self._pos2 = QPointF(0.8, 0.8)
        self._step = 0

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_aura)
        self.timer.start(self.INTERVAL_MS)

    def set_theme(self, theme):
        self.theme = theme
        self.update()

    def _update_aura(self):
        try:
            window = self.window()
            if window is not None and window.isMinimized():
                return      # nothing on screen to drift
            self._step += self.STEP
            import math
            self._pos1.setX(0.5 + 0.3 * math.cos(self._step))
            self._pos1.setY(0.5 + 0.3 * math.sin(self._step * 0.7))
            self._pos2.setX(0.5 + 0.3 * math.cos(self._step * 1.3))
            self._pos2.setY(0.5 + 0.3 * math.sin(self._step + 0.5))
            self.update()
        except RuntimeError:
            # Underlying C++ widget was deleted — stop the timer to prevent
            # further callbacks firing on a dead object.
            try:
                self.timer.stop()
            except Exception:
                pass

    #: The aura is two window-sized radial gradients: pure blur, no edges.
    #: Painting them at 1/6 resolution and scaling up looks the same and
    #: costs 36x fewer pixels -- this used to be 20 full-window gradient
    #: fills a second on the CPU, under everything else.
    DOWNSCALE = 6

    def paintEvent(self, event):
        w, h = self.width(), self.height()
        lw = max(1, w // self.DOWNSCALE)
        lh = max(1, h // self.DOWNSCALE)
        low = QPixmap(lw, lh)
        lp = QPainter(low)

        # Base background
        lp.fillRect(low.rect(), parse_color(self.theme.get('bg_primary', '#05060f')))

        # Aura 1
        grad1 = QRadialGradient(self._pos1.x() * lw, self._pos1.y() * lh, lw * 0.8)
        rgba1 = self.theme.get('aura_1', 'rgba(124, 111, 247, 0.15)')
        grad1.setColorAt(0, parse_color(rgba1))
        grad1.setColorAt(1, Qt.GlobalColor.transparent)
        lp.fillRect(low.rect(), QBrush(grad1))

        # Aura 2
        grad2 = QRadialGradient(self._pos2.x() * lw, self._pos2.y() * lh, lw * 0.6)
        rgba2 = self.theme.get('aura_2', 'rgba(167, 139, 250, 0.1)')
        grad2.setColorAt(0, parse_color(rgba2))
        grad2.setColorAt(1, Qt.GlobalColor.transparent)
        lp.fillRect(low.rect(), QBrush(grad2))
        lp.end()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(self.rect(), low)
