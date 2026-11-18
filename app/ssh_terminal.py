"""
RPMote SSH Terminal — Interactive SSH shell with ANSI color rendering.
"""

import threading
import os
import time
import re
import codecs
import socket
from PyQt6.QtGui import (
    QFont, QKeyEvent, QMouseEvent, QWheelEvent, QColor,
    QKeySequence, QShortcut, QAction, QIcon, QPainter,
    QBrush, QPen
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QLineEdit, QFileDialog, QApplication,
    QGraphicsDropShadowEffect, QMenu, QFrame, QToolButton, QScrollArea, QSplitter,
    QMessageBox, QSizePolicy, QWidgetAction, QFormLayout
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QTimer, QSize, QPoint
)
import paramiko

from app.ansi_parser import AnsiParser
from app.themes import DARK_THEME, button_text_color
from app.monitoring import RemoteMonitor
from app.screen_buffer import DEFAULT_SCROLLBACK, ScreenBuffer
from app.terminal_view import SearchOptions, TerminalView, is_app_chord
from app.progress_coalescer import TerminalProgressCoalescer
from app import guardrails, host_keys, icons, net_errors, port_forwarding, session_log, ssh_connect
from app.failure_panel import FailurePanel
from app.themes import accent_rgba
from app.widgets import ElidedLabel, available_geometry, icon_button
from app.broadcast import BroadcastGroup, distribute
from app.broadcast_bar import BroadcastBar
from app.mfa_dialog import MFAPrompter
from app.terminal_upload import TerminalUploader, describe_drop, local_paths_from_mime


# Alternate-screen switches. A chunk containing one of these is handing the
# screen to (or taking it back from) a full-screen program.
_ALT_SCREEN_RE = re.compile(
    r"\x1b\[\?(?:\d+;)*(?:1049|1047|47)(?:;\d+)*[hl]")
# A screen switch can straddle SSH reads. Preserve a partial CSI at the end
# of a read, and pass its continuation through the emulator unchanged.
_PARTIAL_CSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*)?$")


class _ResponsiveHeader(QWidget):
    """The session header; says when its width changes so it can re-flow."""

    resized = pyqtSignal(int)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit(event.size().width())


class SSHTerminal(QWidget):
    """Interactive SSH terminal widget with ANSI color support."""

    TERMINAL_STYLES = ("System Theme", "Solid Black", "Deep Blue", "Matrix Green", "Cyberpunk", "Vaporwave")
    #: The faces offered in the header, in the order the combo lists them.
    #: "Default Font" means "the best of the bundled and system monospaces",
    #: which is what _apply_terminal_font goes looking for.
    TERMINAL_FONTS = (
        "Default Font",
        "MesloLGS NF",
        "MesloLGS Nerd Font Mono",
        "MesloLGL Nerd Font Mono",
        "Consolas",
        "Courier New",
        "Cascadia Code",
        "JetBrains Mono",
        "Fira Code",
        "Lucida Console",
    )
    DEFAULT_FONT_SIZE = 10
    MIN_FONT_SIZE = 6
    MAX_FONT_SIZE = 72

    data_received = pyqtSignal(str)
    session_closed = pyqtSignal()
    connection_lost = pyqtSignal(str)
    connection_established = pyqtSignal()
    #: The saved connection, from the failure panel's Edit button.
    edit_requested = pyqtSignal(object)
    split_requested = pyqtSignal(bool) # True for vertical, False for horizontal
    style_changed = pyqtSignal(str)
    #: (face, point size) whenever the user picks either, so the choice can be
    #: saved and become the starting point for the next session.
    font_changed = pyqtSignal(str, int)
    _deferred_close = pyqtSignal()  # Safe cross-thread session close trigger
    #: Carries a host_keys.HostKeyUnverified from the connect thread to the GUI
    #: thread, which is the only place allowed to put a dialog on screen.
    host_key_challenge = pyqtSignal(object)
    _fonts_loaded = False

    @classmethod
    def normalized_font(cls, font_name) -> str:
        """A face the combo actually offers, or the default."""
        return font_name if font_name in cls.TERMINAL_FONTS else "Default Font"

    @classmethod
    def normalized_font_size(cls, size) -> int:
        """A point size inside the range the zoom buttons allow."""
        try:
            size = int(size)
        except (TypeError, ValueError):
            return cls.DEFAULT_FONT_SIZE
        return max(cls.MIN_FONT_SIZE, min(cls.MAX_FONT_SIZE, size))

    def __init__(self, connection_info: dict, theme=None, ssh_client=None, terminal_style: str | None = None, terminal_font: str | None = None, font_size: int | None = None, production_tenants=(), record_session: bool = True, parent=None):
        super().__init__(parent)
        self.connection_info = connection_info
        #: "ssh", or "telnet" / "serial" for the stream transports.
        self._protocol = str(connection_info.get("protocol", "ssh") or "ssh").lower()
        self._is_stream = self._protocol in ("telnet", "serial")
        self.theme = theme or DARK_THEME
        self.client = ssh_client # May be passed for split panes
        self.channel: paramiko.Channel | None = None
        self.ansi_parser = AnsiParser(theme=self.theme)
        self.command_history: list[str] = []
        self.history_index = -1
        self.current_input = ""
        self._connected = False
        self._reader_thread: threading.Thread | None = None
        self._session_log: list[str] = []
        self._local_font = self.normalized_font(terminal_font)
        self._font_size = self.normalized_font_size(
            self.DEFAULT_FONT_SIZE if font_size is None else font_size)
        #: Tenants the user has marked production, so this pane can say so.
        self._production_tenants = guardrails.normalize_production_tenants(
            production_tenants)
        #: Set by a TerminalWorkspace when this pane is part of a broadcast.
        self.broadcast_group: BroadcastGroup | None = None
        #: Callable returning the commands Ctrl+K offers. The host owns the
        #: vault, so it owns the list; a bare terminal falls back to the
        #: bundled library.
        self.snippet_provider = None
        #: Written to as output arrives; opened on the first byte.
        self._record_session = bool(record_session)
        self._recorder: session_log.SessionRecorder | None = None
        self._uploader: TerminalUploader | None = None
        self._utf8_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._last_pty_size: tuple[int, int] | None = None
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._resize_remote_pty)
        self.pending_resync = False
        self.resync_timer = None
        self.monitor = None
        self._suppress_rendering = False  # Suppresses terminal output during setup
        self._shell_setup_sentinel = ""
        self._shell_setup_tail = ""
        self._sending_shell_setup = False
        # Set when a shell-transition resync (sudo -i / su) is requested while a
        # previous setup is still suppressing output. _end_rendering_suppression
        # re-runs the apply so the resync is never silently dropped.
        self._integration_apply_pending = False
        self._shell_integration_applied = False
        # Shell integration only makes sense against a POSIX shell, so it turns
        # itself off for the session on anything that does not look like one
        # (Windows OpenSSH, network gear, restricted shells) — and is never
        # attempted over telnet or a serial console.
        self._shell_integration_enabled = not self._is_stream
        self._laid_out = False
        self._user_input_since_connect = False
        self._initial_shell_integration_deadline = 0.0
        self._last_sent_command = ""
        self._close_emitted = False  # Prevents double session_closed emission
        # Host keys accepted with "Connect Once" — trusted for this widget's
        # lifetime only and never written to the known-host list.
        self._session_host_keys: list = []
        self._host_key_prompt_open = False
        # Answers keyboard-interactive (MFA) challenges from the GUI thread.
        self._mfa = MFAPrompter(self)
        # Port forwards belonging to this session, brought up with it.
        self._forwards = None
        # Automatic reconnection after an unexpected drop. Backs off so a
        # server that is genuinely down is not hammered, and gives up rather
        # than retrying forever behind the user's back.
        self._auto_reconnect = bool(connection_info.get("auto_reconnect", True))
        self._reconnect_attempt = 0
        #: (kind, sentence) from the last failed attempt to connect, handed
        #: from the connect thread to _on_connection_lost.
        self._last_failure = None
        self._reconnect_deadline = 0.0
        self._user_disconnected = False
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._attempt_auto_reconnect)
        self._last_terminal_paint_flush = 0.0
        self._pending_render_bytes = 0
        self._terminal_attention_pending = False
        self._progress_coalescer = TerminalProgressCoalescer()
        # Cancellable fallback timer for rendering suppression — replaces
        # QTimer.singleShot so that a second _apply_shell_integration call (e.g. after
        # sudo -i resync) cancels the previous 7 s window instead of letting it
        # fire mid-resync and prematurely end the new suppression window.
        self._render_fallback_timer = QTimer(self)
        self._render_fallback_timer.setSingleShot(True)
        self._render_fallback_timer.timeout.connect(self._end_rendering_suppression)
        self._local_style = (
            terminal_style
            if terminal_style in self.TERMINAL_STYLES
            else "System Theme"
        )

        # Virtual screen buffer (proper 2D terminal grid)
        # A saved zero means "keep no history", which is a real choice and
        # not the same as the setting being absent.
        history = connection_info.get("scrollback_lines")
        try:
            history = DEFAULT_SCROLLBACK if history in (None, "") else int(history)
        except (TypeError, ValueError):
            history = DEFAULT_SCROLLBACK
        self._screen_buf = ScreenBuffer(
            rows=40, cols=120, scrollback_limit=max(0, history))
        # Programs that ask the terminal a question (cursor position, device
        # attributes) get their answer written straight back to the channel.
        self._screen_buf.response_callback = self._send_terminal_reply
        # A remote yank (OSC 52) lands on the local clipboard.
        self._screen_buf.clipboard_callback = self._on_remote_clipboard
        # Last command reported finished, so the header only updates on a
        # change rather than on every chunk of output.
        self._last_reported_command = None
        self._status_flash_timer = QTimer(self)
        self._status_flash_timer.setSingleShot(True)
        self._status_flash_timer.timeout.connect(self._clear_status_flash)

        # The view paints straight from the buffer, so there is no rendered
        # copy of anything to keep in step — only the guard that stops a
        # repaint re-entering itself.
        self._rendering = False
        self._programmatic_scroll = False
        # Search re-runs on a debounce while the bar is open, so typing does
        # not rescan a hundred thousand lines on every keystroke.
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(lambda: self._run_search(move=False))

        # Render timer — debounces screen refreshes for performance
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(8)
        self._render_timer.timeout.connect(self._render_screen)

        # Cursor blinker
        self._cursor_blink_visible = True
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(530)
        self._blink_timer.timeout.connect(self._toggle_cursor_blink)
        self._blink_timer.start()

        # Procedural setup
        self._setup_ui()
        self._connect_signals()

    def _toggle_cursor_blink(self):
        terminal = getattr(self, "terminal", None)
        if not self._connected or terminal is None or not terminal.hasFocus():
            return
        if self._terminal_has_selection() or self._rendering:
            return
        # Only the overlay changes — no document edit, no relayout.
        self._cursor_blink_visible = not self._cursor_blink_visible
        self._sync_cursor_overlay()

    def _terminal_has_selection(self) -> bool:
        """Return True while the user has highlighted terminal text."""
        terminal = getattr(self, "terminal", None)
        if terminal is None:
            return False
        return terminal.has_selection()

    def _apply_local_style(self, style_name: str):
        self.set_terminal_style(style_name, emit_change=True)

    def set_terminal_style(self, style_name: str, emit_change: bool = False):
        if style_name not in self.TERMINAL_STYLES:
            style_name = "System Theme"
        self._local_style = style_name

        style_combo = getattr(self, "style_combo", None)
        if style_combo is not None and style_combo.currentText() != style_name:
            was_blocked = style_combo.blockSignals(True)
            style_combo.setCurrentText(style_name)
            style_combo.blockSignals(was_blocked)

        self.set_theme(self.theme)
        if emit_change:
            self.style_changed.emit(style_name)

        if getattr(self, "terminal", None) is not None:
            self._force_full_render()

    def _apply_local_font(self, font_name: str):
        self.set_terminal_font_choice(font_name, emit_change=True)

    def set_terminal_font_choice(self, font_name: str, emit_change: bool = False):
        """Switch face, keeping the combo — and the saved default — in step."""
        self._local_font = self.normalized_font(font_name)

        combo = getattr(self, "font_combo", None)
        if combo is not None and combo.currentText() != self._local_font:
            was_blocked = combo.blockSignals(True)
            combo.setCurrentText(self._local_font)
            combo.blockSignals(was_blocked)

        self._apply_terminal_font()
        if emit_change:
            self.font_changed.emit(self._local_font, self._font_size)

    def set_terminal_font_size(self, size, emit_change: bool = False):
        self._font_size = self.normalized_font_size(size)
        self._apply_terminal_font()
        if emit_change:
            self.font_changed.emit(self._local_font, self._font_size)

    # ── Header layout ───────────────────────────────────────────────

    #: Header widths (px) where it gives way. Below the first, buttons drop
    #: their labels; below the second, the secondary controls fold into the
    #: "more" menu; below the third, so do Reconnect and Disconnect.
    HEADER_ICONS_BELOW = 1000
    HEADER_FOLD_BELOW = 720
    HEADER_MINIMAL_BELOW = 470

    def _header_mode_for(self, width: int) -> str:
        if width <= 0:
            return "full"
        if width < self.HEADER_MINIMAL_BELOW:
            return "minimal"
        if width < self.HEADER_FOLD_BELOW:
            return "fold"
        if width < self.HEADER_ICONS_BELOW:
            return "icons"
        return "full"

    def _fit_header(self, width: int | None = None):
        """Re-flow the header for its current width."""
        header = getattr(self, "header", None)
        if header is None:
            return
        if width is None:
            width = header.width() if header.isVisible() else 0
        mode = self._header_mode_for(width)
        if mode == self._header_mode:
            return
        self._header_mode = mode

        labelled = mode == "full"
        for button, label in self._labelled_header_buttons:
            button.setText(label if labelled else "")
            button.setProperty("iconOnly", not labelled)
            if labelled:
                button.setMinimumWidth(0)
                button.setMaximumWidth(16777215)
            else:
                button.setFixedWidth(30)
            button.style().unpolish(button)
            button.style().polish(button)

        folded = mode in ("fold", "minimal")
        for widget in (self.btn_font_dec, self.btn_font_inc,
                       self.btn_appearance, self.btn_split_v,
                       self.btn_split_h, self.btn_log):
            widget.setVisible(not folded)
        self.latency_label.setVisible(not folded)
        minimal = mode == "minimal"
        self.btn_reconnect.setVisible(not minimal)
        self.btn_disconnect.setVisible(not minimal)
        self.shell_badge.setVisible(not minimal)
        self.btn_more.setVisible(folded)
        self._refresh_broadcast_badge()

    def _build_appearance_menu(self) -> QMenu:
        """Terminal colour scheme and face, kept out of the header's way."""
        menu = QMenu(self)
        panel = QWidget()
        form = QFormLayout(panel)
        form.setContentsMargins(12, 10, 12, 10)
        form.setSpacing(8)
        for combo in (self.style_combo, self.font_combo):
            combo.setMinimumWidth(180)
        form.addRow("Colours", self.style_combo)
        form.addRow("Font", self.font_combo)
        action = QWidgetAction(menu)
        action.setDefaultWidget(panel)
        menu.addAction(action)
        return menu

    def _populate_more_menu(self):
        """Fill the ⋯ menu with whatever the header had no room for."""
        menu = self.btn_more.menu()
        menu.clear()
        mode = self._header_mode

        act = menu.addAction(icons.icon("minus"), "Smaller text")
        act.setShortcut(QKeySequence("Ctrl+-"))
        act.triggered.connect(self._decrease_font)
        act = menu.addAction(icons.icon("plus"), "Larger text")
        act.setShortcut(QKeySequence("Ctrl++"))
        act.triggered.connect(self._increase_font)

        styles = menu.addMenu(icons.icon("palette"), "Colours")
        for name in self.TERMINAL_STYLES:
            item = styles.addAction(name)
            item.setCheckable(True)
            item.setChecked(name == self._local_style)
            item.triggered.connect(
                lambda _checked=False, n=name: self._apply_local_style(n))
        faces = menu.addMenu(icons.icon("type"), "Font")
        for name in self.TERMINAL_FONTS:
            item = faces.addAction(name)
            item.setCheckable(True)
            item.setChecked(name == self._local_font)
            item.triggered.connect(
                lambda _checked=False, n=name: self._apply_local_font(n))

        menu.addSeparator()
        act = menu.addAction(icons.icon("split-v"), "Split side by side")
        act.triggered.connect(lambda: self.split_requested.emit(True))
        act = menu.addAction(icons.icon("split-h"), "Split stacked")
        act.triggered.connect(lambda: self.split_requested.emit(False))

        menu.addSeparator()
        self._populate_log_menu(
            menu.addMenu(icons.icon("record", "#f87171"), "Session log"))

        if mode == "minimal":
            menu.addSeparator()
            act = menu.addAction(icons.icon("refresh"), "Reconnect")
            act.triggered.connect(self.reconnect)
            act = menu.addAction(icons.icon("x"), "Disconnect")
            act.triggered.connect(self.disconnect)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Apply initial theme state via set_theme (reuse logic)
        # We'll do this AFTER header is created below to avoid partial state

        # Header bar. It re-flows as the pane narrows (splits, broadcast
        # grids) instead of letting its controls overdraw each other: full
        # labels when there is room, then icons only, then everything but the
        # essentials folds into the "more" menu. See _fit_header.
        self.header = _ResponsiveHeader()
        self.header.setObjectName("terminal_header")
        self.header.setFixedHeight(44)
        self.header.resized.connect(self._fit_header)
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(12, 0, 10, 0)
        header_layout.setSpacing(6)

        self.status_indicator = QLabel("●")
        # Add glow to status
        self._status_glow = QGraphicsDropShadowEffect()
        self._status_glow.setBlurRadius(10)
        self._status_glow.setOffset(0, 0)
        self.status_indicator.setGraphicsEffect(self._status_glow)
        self._set_status_light("#fbbf24")
        header_layout.addWidget(self.status_indicator)
        header_layout.addSpacing(2)

        name = self.connection_info.get("name", "SSH")
        host = self.connection_info.get("host", "")
        port = self.connection_info.get("port", 22)
        if self._protocol == "serial":
            where = (f"{self.connection_info.get('serial_port') or host} @ "
                     f"{self.connection_info.get('baud_rate', 9600)} baud")
        else:
            where = f"{host}:{port}"
        self.title_label = ElidedLabel(f"{name} — {where}")
        self.title_label.setStyleSheet("font-weight: 600; font-size: 12px;")
        self.title_label.setSizePolicy(QSizePolicy.Policy.Maximum,
                                       QSizePolicy.Policy.Preferred)
        self.title_label.setMinimumWidth(36)
        header_layout.addWidget(self.title_label)

        self.shell_badge = QLabel(self._protocol.upper() if self._is_stream else "SSH")
        self.shell_badge.setObjectName("shell_badge")
        self.shell_badge.setFixedHeight(22)
        self.shell_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.shell_badge.setMinimumWidth(42)
        header_layout.addWidget(self.shell_badge)

        # A production session should never be mistaken for a lab one at a
        # glance — that mistake is the whole reason this exists.
        self.prod_badge = QLabel("PROD")
        self.prod_badge.setObjectName("prod_badge")
        self.prod_badge.setFixedHeight(22)
        self.prod_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prod_badge.setMinimumWidth(48)
        self.prod_badge.setStyleSheet(
            "background: #7f1d1d; color: #fecaca; font-size: 10px;"
            " font-weight: 800; padding: 2px 8px; border-radius: 6px;"
            " border: 1px solid #ef4444;")
        self.prod_badge.setVisible(False)
        header_layout.addWidget(self.prod_badge)

        # A pane broadcasting to others has to say so, permanently and where
        # the eye already is.
        self.broadcast_badge = QLabel("BROADCAST")
        self.broadcast_badge.setObjectName("broadcast_badge")
        self.broadcast_badge.setFixedHeight(22)
        self.broadcast_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.broadcast_badge.setStyleSheet(
            "background: #78350f; color: #fde68a; font-size: 10px;"
            " font-weight: 800; padding: 2px 8px; border-radius: 6px;"
            " border: 1px solid #f59e0b;")
        self.broadcast_badge.setVisible(False)
        header_layout.addWidget(self.broadcast_badge)

        header_layout.addStretch(1)

        # Exit status of the last command, when the remote shell reports it
        # (OSC 133). Blank until shell integration is actually running.
        self.cmd_status_label = QLabel("")
        self.cmd_status_label.setStyleSheet("color: #a0a3b8; font-size: 11px;")
        self.cmd_status_label.setToolTip(
            "Exit status and run time of the last command, reported by the "
            "remote shell."
        )
        header_layout.addWidget(self.cmd_status_label)
        header_layout.addSpacing(6)

        self.latency_label = QLabel("")
        self.latency_label.setStyleSheet("color: #a0a3b8; font-size: 11px;")
        header_layout.addWidget(self.latency_label)
        header_layout.addSpacing(8)

        # Font size controls
        self.btn_font_dec = icon_button("minus", "Smaller text (Ctrl+-)", size=28)
        self.btn_font_dec.setObjectName("btn_font")
        self.btn_font_dec.clicked.connect(self._decrease_font)
        header_layout.addWidget(self.btn_font_dec)

        self.btn_font_inc = icon_button("plus", "Larger text (Ctrl++)", size=28)
        self.btn_font_inc.setObjectName("btn_font")
        self.btn_font_inc.clicked.connect(self._increase_font)
        header_layout.addWidget(self.btn_font_inc)

        # Style and face live behind one button: they are set once and left,
        # and two wide combos were most of what made the header overflow.
        self.style_combo = QComboBox()
        self.style_combo.setObjectName("style_combo")
        self.style_combo.addItems(list(self.TERMINAL_STYLES))
        self.style_combo.setCurrentText(self._local_style)
        self.style_combo.currentTextChanged.connect(self._apply_local_style)

        self.font_combo = QComboBox()
        self.font_combo.setObjectName("font_combo")
        self.font_combo.addItems(list(self.TERMINAL_FONTS))
        self.font_combo.setCurrentText(self._local_font)
        self.font_combo.currentTextChanged.connect(self._apply_local_font)

        self.btn_appearance = QToolButton()
        self.btn_appearance.setObjectName("btn_appearance")
        self.btn_appearance.setIcon(icons.icon("type"))
        self.btn_appearance.setIconSize(QSize(16, 16))
        self.btn_appearance.setFixedSize(28, 28)
        self.btn_appearance.setToolTip("Terminal colours and font")
        self.btn_appearance.setAccessibleName("Terminal colours and font")
        self.btn_appearance.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_appearance.setMenu(self._build_appearance_menu())
        header_layout.addWidget(self.btn_appearance)
        header_layout.addSpacing(6)

        # Split Controls
        self.btn_split_v = QPushButton("Split V")
        self.btn_split_v.setObjectName("btn_secondary")
        self.btn_split_v.setIcon(icons.icon("split-v"))
        self.btn_split_v.setToolTip("Split side by side")
        self.btn_split_v.clicked.connect(lambda: self.split_requested.emit(True))
        header_layout.addWidget(self.btn_split_v)

        self.btn_split_h = QPushButton("Split H")
        self.btn_split_h.setObjectName("btn_secondary")
        self.btn_split_h.setIcon(icons.icon("split-h"))
        self.btn_split_h.setToolTip("Split stacked")
        self.btn_split_h.clicked.connect(lambda: self.split_requested.emit(False))
        header_layout.addWidget(self.btn_split_h)

        self.btn_log = QPushButton("Log")
        self.btn_log.setObjectName("btn_secondary")
        self.btn_log.setIcon(icons.icon("record", "#f87171"))
        self.btn_log.setToolTip(
            "This session is being recorded to disk as it happens.")
        self.btn_log.clicked.connect(self._show_log_menu)
        header_layout.addWidget(self.btn_log)

        self.btn_reconnect = QPushButton("Reconnect")
        self.btn_reconnect.setObjectName("btn_secondary")
        self.btn_reconnect.setIcon(icons.icon("refresh"))
        self.btn_reconnect.setToolTip("Reconnect this session")
        self.btn_reconnect.clicked.connect(self.reconnect)
        header_layout.addWidget(self.btn_reconnect)

        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.setObjectName("btn_danger")
        self.btn_disconnect.setIcon(icons.icon("x", "#ffffff"))
        self.btn_disconnect.setToolTip("Disconnect and close this session")
        self.btn_disconnect.clicked.connect(self.disconnect)
        header_layout.addWidget(self.btn_disconnect)

        self.btn_pop_out = icon_button("external", "Pop out SSH shell", size=28)
        self.btn_pop_out.hide()
        header_layout.addWidget(self.btn_pop_out)

        # Everything that did not fit, one click away.
        self.btn_more = QToolButton()
        self.btn_more.setObjectName("btn_more")
        self.btn_more.setIcon(icons.icon("more"))
        self.btn_more.setIconSize(QSize(16, 16))
        self.btn_more.setFixedSize(28, 28)
        self.btn_more.setToolTip("More")
        self.btn_more.setAccessibleName("More session actions")
        self.btn_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        more_menu = QMenu(self.btn_more)
        more_menu.aboutToShow.connect(self._populate_more_menu)
        self.btn_more.setMenu(more_menu)
        self.btn_more.setVisible(False)
        header_layout.addWidget(self.btn_more)

        #: Buttons whose label comes and goes with the width, with that label.
        self._labelled_header_buttons = (
            (self.btn_split_v, "Split V"),
            (self.btn_split_h, "Split H"),
            (self.btn_log, "Log"),
            (self.btn_reconnect, "Reconnect"),
            (self.btn_disconnect, "Disconnect"),
        )
        self._header_mode = ""
        for button, _label in self._labelled_header_buttons:
            button.setFixedHeight(28)
            button.setIconSize(QSize(14, 14))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        # Full until the header is really laid out; resizes re-flow it.
        self._fit_header(0)

        layout.addWidget(self.header)

        # A stripe under the header, for the sessions where a mistake is
        # expensive. A badge can be missed at a glance; a bar across the top
        # of the window cannot.
        self.prod_banner = QLabel("")
        self.prod_banner.setObjectName("prod_banner")
        self.prod_banner.setFixedHeight(24)
        self.prod_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prod_banner.setStyleSheet(
            "#prod_banner { background: #7f1d1d; color: #fee2e2;"
            " font-size: 11px; font-weight: 700;"
            " border-bottom: 1px solid #ef4444; }")
        self.prod_banner.setVisible(False)
        layout.addWidget(self.prod_banner)

        # What went wrong and what to try, instead of a red line that
        # scrolls away.
        self.failure_panel = FailurePanel(self)
        self.failure_panel.retry_requested.connect(self.reconnect)
        self.failure_panel.edit_requested.connect(
            lambda: self.edit_requested.emit(dict(self.connection_info)))
        layout.addWidget(self.failure_panel)

        # Terminal container (floating glass card)
        self.terminal_frame = QFrame()
        self.terminal_frame.setObjectName("terminal_frame")
        self.terminal_frame.setStyleSheet(f"""
            #terminal_frame {{
                background-color: {self.theme['bg_terminal']};
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 12px;
                margin: 10px;
            }}
        """)

        frame_layout = QVBoxLayout(self.terminal_frame)
        frame_layout.setSpacing(0)
        frame_layout.setContentsMargins(1, 1, 1, 1)

        self.terminal = TerminalView()
        self.terminal.setObjectName("terminal_view")
        self.terminal.set_placeholder("Connecting to remote host...")
        self.terminal.setStyleSheet("""
            #terminal_view { background: transparent; border: none; }
            QScrollBar:vertical {
                width: 8px;
                background: transparent;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.15);
                border-radius: 4px;
                min-height: 24px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(255, 255, 255, 0.3);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
            QScrollBar:horizontal {
                height: 8px;
                background: transparent;
            }
            QScrollBar::handle:horizontal {
                background: rgba(255, 255, 255, 0.15);
                border-radius: 4px;
                min-width: 24px;
            }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
                width: 0px;
            }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
                background: transparent;
            }
        """)
        self.terminal.set_buffer(self._screen_buf)
        self.terminal.installEventFilter(self)
        self.terminal.viewport().installEventFilter(self)
        self.terminal.selection_changed.connect(self._on_selection_changed)

        frame_layout.addWidget(self.terminal)

        # No drop shadow on the terminal frame: a QGraphicsEffect forces the
        # whole subtree through an offscreen blur on every repaint, which is
        # ruinous for a widget that redraws on each burst of output.

        layout.addWidget(self.terminal_frame)

        # Apply initial font state immediately
        self._apply_terminal_font()
        self._refresh_prod_badge()

        # A file dropped on the terminal goes to the directory the shell is
        # standing in, so the whole widget has to accept the drop — including
        # the view, which would otherwise swallow it first.
        self.setAcceptDrops(True)
        self.terminal.setAcceptDrops(False)
        self.terminal.viewport().setAcceptDrops(False)

        # Shortcuts
        QShortcut(QKeySequence("Ctrl+="), self, self._increase_font)
        QShortcut(QKeySequence("Ctrl++"), self, self._increase_font)
        QShortcut(QKeySequence("Ctrl+-"), self, self._decrease_font)

        # Search bar (hidden by default)
        self.search_bar = QWidget()
        self.search_bar.setFixedHeight(36)
        self.search_bar.setVisible(False)
        self.search_bar.setStyleSheet("background-color: rgba(0,0,0,0.2); border-top: 1px solid rgba(255,255,255,0.05);")
        sb_layout = QHBoxLayout(self.search_bar)
        sb_layout.setContentsMargins(12, 0, 12, 0)

        sb_layout.setSpacing(6)
        search_icon = QLabel()
        search_icon.setPixmap(icons.pixmap("search", 15, "#94a3b8", self.devicePixelRatioF()))
        sb_layout.addWidget(search_icon)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search scrollback...")
        self.search_input.setFixedHeight(26)
        self.search_input.returnPressed.connect(self._search_next)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        sb_layout.addWidget(self.search_input)

        # Match count, so a search says how much it found rather than just
        # moving the view and leaving you to guess.
        self.search_status = QLabel("")
        self.search_status.setMinimumWidth(96)
        self.search_status.setStyleSheet("color: rgba(255,255,255,0.55); font-size: 11px;")
        sb_layout.addWidget(self.search_status)

        def toggle(label, tip):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setFixedSize(28, 26)
            button.setToolTip(tip)
            button.setStyleSheet(
                "QPushButton { font-size: 11px; border-radius: 4px;"
                " background: rgba(255,255,255,0.06); }"
                f"QPushButton:checked {{ background: {accent_rgba(0.55)}; }}")
            button.toggled.connect(lambda _checked: self._run_search(move=False))
            sb_layout.addWidget(button)
            return button

        self.search_case = toggle("Aa", "Match case")
        self.search_word = toggle("ab|", "Whole word only")
        self.search_regex = toggle(".*", "Regular expression")

        btn_prev = QPushButton()
        btn_prev.setIcon(icons.icon("chevron-up"))
        btn_prev.setFixedSize(26, 26)
        btn_prev.setToolTip("Previous match (Shift+Enter)")
        btn_prev.clicked.connect(self._search_previous)
        sb_layout.addWidget(btn_prev)

        btn_next = QPushButton()
        btn_next.setIcon(icons.icon("chevron-down"))
        btn_next.setFixedSize(26, 26)
        btn_next.setToolTip("Next match (Enter)")
        btn_next.clicked.connect(self._search_next)
        sb_layout.addWidget(btn_next)

        btn_close_search = QPushButton()
        btn_close_search.setIcon(icons.icon("x"))
        btn_close_search.setToolTip("Close search (Esc)")
        btn_close_search.setFixedSize(26, 26)
        btn_close_search.clicked.connect(self._close_search)
        sb_layout.addWidget(btn_close_search)

        layout.addWidget(self.search_bar)

        # Health Dashboard Bar (new)
        self.health_bar = HealthBar(self)
        layout.addWidget(self.health_bar)
        # CPU/RAM come from a shell over SSH; a switch console has none.
        self.health_bar.setVisible(not self._is_stream)
        if self._protocol == "serial":
            # One COM port, one reader: there is nothing to split onto.
            self.btn_split_v.setVisible(False)
            self.btn_split_h.setVisible(False)

        # Initial stats & Theme
        self.set_theme(self.theme)
        self.health_bar.set_theme(self.theme)


    def _connect_signals(self):
        self.data_received.connect(self._on_data_received)
        self.connection_lost.connect(self._on_connection_lost)
        self.connection_established.connect(self._on_connected)
        self._deferred_close.connect(self._schedule_session_close)
        self.host_key_challenge.connect(
            self._on_host_key_challenge, Qt.ConnectionType.QueuedConnection
        )

    # ── Connection ──────────────────────────────────────────────────

    def connect_ssh(self):
        """Start SSH connection in a background thread."""
        self._set_status_light("#fbbf24")
        if getattr(self, "health_bar", None) is not None:
            self.health_bar.set_connection_state(
                "reconnecting" if self._reconnect_attempt else "connecting")
        thread = threading.Thread(target=self._do_connect, daemon=True)
        thread.start()

    def _do_connect(self):
        """Establish the connection (runs in background thread)."""
        if self._is_stream:
            self._do_connect_stream()
            return
        try:
            if not self.client:
                # One shared connect path: jump hosts, agent keys, a key
                # passphrase of its own, MFA prompts, and a verified host key
                # for every hop along the way.
                self.client = ssh_connect.open_client(
                    self.connection_info,
                    extra_host_keys=list(self._session_host_keys),
                    interactive=self._mfa.handler,
                )

            transport = self.client.get_transport()
            if transport:
                transport.set_keepalive(30)
                self._start_port_forwards(transport)

            # Open the interactive shell FIRST. The health monitor opens its own
            # exec channels, and a server with `MaxSessions 1` would then refuse
            # the shell outright — which looked like a broken terminal.
            initial_cols, initial_rows = self._terminal_size_for_pty()
            self._last_pty_size = None
            self.channel = self.client.invoke_shell(
                term=self.connection_info.get("term", "xterm-256color"),
                width=initial_cols,
                height=initial_rows,
            )
            self.channel.settimeout(0.05)
            self._connected = True
            self.connection_established.emit()

            # Start reader thread
            self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
            self._reader_thread.start()

            # Health monitor last: if it cannot get a channel the shell still works.
            try:
                self.monitor = RemoteMonitor(self.client)
                self.monitor.stats_updated.connect(
                    self.health_bar.update_stats, Qt.ConnectionType.QueuedConnection
                )
                self.monitor.start()
            except Exception:
                self.monitor = None

        except host_keys.HostKeyUnverified as challenge:
            # Nothing is connected and no credential has been sent: paramiko
            # checks the host key before authenticating. Hand the decision to
            # the GUI thread and wait to be told what to do.
            self._discard_client()
            self.host_key_challenge.emit(challenge)
        except Exception as e:
            info = self.connection_info
            kind, reason = net_errors.describe_failure(
                e, info.get("host", ""), info.get("port", 22),
                str(info.get("username", "") or ""))
            self._last_failure = (kind, reason)
            self.connection_lost.emit(f"Connection failed: {reason}")

    def _do_connect_stream(self):
        """Telnet or a serial console: no host keys, forwards or monitoring."""
        try:
            if not self.client:
                from app.stream_transports import open_stream_client
                self.client = open_stream_client(self.connection_info)
            initial_cols, initial_rows = self._terminal_size_for_pty()
            self._last_pty_size = None
            self.channel = self.client.invoke_shell(width=initial_cols, height=initial_rows)
            self.channel.settimeout(0.05)
            self._connected = True
            self.connection_established.emit()
            self._reader_thread = threading.Thread(target=self._read_output, daemon=True)
            self._reader_thread.start()
        except Exception as e:
            self._discard_client()
            info = self.connection_info
            if self._protocol == "serial":
                from app.stream_transports import describe_serial_error
                kind = net_errors.OTHER
                reason = describe_serial_error(e, info.get("serial_port") or info.get("host", ""))
            else:
                kind, reason = net_errors.describe_failure(
                    e, info.get("host", ""), info.get("port", 23))
            self._last_failure = (kind, reason)
            self.connection_lost.emit(f"Connection failed: {reason}")

    #: How long to keep trying after a drop, and the gaps between attempts.
    RECONNECT_DELAYS = (2, 5, 10, 20, 30, 60)

    def _start_port_forwards(self, transport):
        """Bring up the tunnels saved on this connection."""
        specs = self.connection_info.get("port_forwards") or []
        if not specs:
            return
        manager = port_forwarding.ForwardManager(transport)
        manager.start_all(specs)
        self._forwards = manager
        if manager.failures:
            first = manager.failures[0][1]
            self._flash_status(f"Port forward: {first}", 6000)

    def _stop_port_forwards(self):
        manager, self._forwards = self._forwards, None
        if manager is not None:
            try:
                manager.stop_all()
            except Exception:
                pass

    # ── Automatic reconnection ──────────────────────────────────────

    def _schedule_auto_reconnect(self):
        """Queue the next reconnection attempt after an unexpected drop."""
        if not self._auto_reconnect or self._user_disconnected:
            return False
        if self._reconnect_attempt >= len(self.RECONNECT_DELAYS):
            self.latency_label.setText("Reconnect failed — use Reconnect")
            return False
        delay = self.RECONNECT_DELAYS[self._reconnect_attempt]
        self._reconnect_attempt += 1
        self.shell_badge.setText("RETRY")
        self.health_bar.set_connection_state("reconnecting")
        self.latency_label.setText(
            f"Reconnecting in {delay}s "
            f"({self._reconnect_attempt}/{len(self.RECONNECT_DELAYS)})"
        )
        self._reconnect_timer.start(delay * 1000)
        return True

    def _attempt_auto_reconnect(self):
        if self._user_disconnected or self._connected:
            return
        self.latency_label.setText("Reconnecting...")
        self.reconnect()

    def _cancel_auto_reconnect(self):
        self._reconnect_timer.stop()
        self._reconnect_attempt = 0

    def _discard_client(self):
        """Throw away a client that never finished connecting."""
        client, self.client = self.client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _on_host_key_challenge(self, challenge):
        """Ask the user about an unknown or changed host key (GUI thread)."""
        if self._host_key_prompt_open:
            return
        self._host_key_prompt_open = True
        try:
            from app.host_key_dialog import ask_about_host_key

            store = host_keys.store()
            if store.policy == host_keys.POLICY_STRICT:
                self.connection_lost.emit(
                    f"Host key not trusted ({challenge.fingerprint}) and the host "
                    f"key policy is set to strict — connection refused."
                )
                return

            decision = ask_about_host_key(
                challenge, self.connection_info.get("name", ""), self
            )
            if not decision.connect:
                kind = (
                    "Host key has changed"
                    if isinstance(challenge, host_keys.HostKeyMismatch)
                    else "Host key not trusted"
                )
                self._auto_reconnect = False
                self.connection_lost.emit(f"{kind} — connection refused.")
                return

            if decision.remember:
                store.trust(
                    challenge.host, challenge.port, challenge.key,
                    replace=decision.replace,
                )
            else:
                self._session_host_keys.append(challenge.key)
        finally:
            self._host_key_prompt_open = False

        self.latency_label.setText("Connecting...")
        self.connect_ssh()

    def _read_output(self):
        """Continuously read output from SSH channel."""
        while self._connected and self.channel and not self.channel.closed:
            try:
                data = self.channel.recv(65536)  # Increased for higher throughput
                if not data:
                    break

                text = self._utf8_decoder.decode(data)
                if text:
                    self.data_received.emit(text)
            except socket.timeout:
                continue
            except Exception:
                break

        # Flush any pending decoder buffer
        try:
            remaining = self._utf8_decoder.decode(b"", final=True)
            if remaining:
                self.data_received.emit(remaining)
        except Exception:
            pass

        if self._connected:
            self._connected = False
            self.connection_lost.emit("Connection closed by remote host")
            # Use signal instead of QTimer from background thread (thread-safe)
            self._deferred_close.emit()


    def _schedule_session_close(self):
        """Schedule session_closed emission on the main thread (called via signal)."""
        if self._close_emitted:
            return
        # A session that is coming back must not take its tab with it. The
        # retry either reconnects, or runs out of attempts and lets the tab
        # close then.
        if self._schedule_auto_reconnect():
            return
        self._close_emitted = True
        QTimer.singleShot(2000, self.session_closed.emit)

    def disconnect(self):
        """Cleanly disconnect the SSH session and close the tab."""
        if self._close_emitted:
            return
        self._user_disconnected = True
        self._cancel_auto_reconnect()
        self._stop_port_forwards()
        self._close_emitted = True
        self._connected = False
        self._progress_coalescer.reset()
        if self.monitor:
            try:
                self.monitor.stop()
            except Exception:
                pass
        if self.channel:
            try:
                self.channel.close()
            except Exception:
                pass
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        try:
            self._set_status_light("#f87171")
            self.latency_label.setText("Disconnected")
            self.health_bar.set_connection_state("offline")
        except Exception:
            pass
        self.session_closed.emit()

    def reconnect(self):
        """Reconnect the SSH session without closing the parent tab."""
        self._reconnect_timer.stop()
        self._stop_port_forwards()
        self._close_emitted = False
        self._connected = False
        self._end_rendering_suppression(realign=False)
        self._shell_integration_applied = False
        self._integration_apply_pending = False
        self._progress_coalescer.reset()
        if self.channel:
            try:
                self.channel.close()
            except Exception:
                pass
        if self.client:
            try:
                if self.monitor:
                    self.monitor.stop()
                self.client.close()
            except Exception:
                pass
        self.channel = None
        self.client = None
        self._follow_output = True
        self.failure_panel.hide()
        self.latency_label.setText("Reconnecting...")
        self.health_bar.set_connection_state("reconnecting")
        self.connect_ssh()

    # ── Terminal I/O ────────────────────────────────────────────────

    #: Session log cap — enough to save a long session, bounded so a runaway
    #: program cannot grow the process without limit.
    SESSION_LOG_LIMIT = 20000

    def _on_data_received(self, text: str):
        """Handle incoming data via virtual screen buffer."""
        self._record_output(text)
        self._session_log.append(text)
        if len(self._session_log) > self.SESSION_LOG_LIMIT:
            del self._session_log[:len(self._session_log) - self.SESSION_LOG_LIMIT]

        # Keep cursor solid while receiving data
        self._hold_cursor_solid()

        # Scanning the stream for prompts costs a full ANSI strip plus a
        # dozen regexes per chunk. A full-screen program has no shell prompt
        # to find and produces the heaviest output, so it is skipped there.
        needs_scan = not self._screen_buf._using_alt or self._suppress_rendering
        clean_text = (
            self.ansi_parser.strip_ansi(text[-2048:]).strip().lower()
            if needs_scan else ""
        )
        if clean_text and self._looks_like_auth_failure(clean_text):
            self._cancel_pending_shell_resync()

        # Keep the hidden shell-integration setup from clearing or rewriting
        # the visible terminal buffer while the hook commands settle.
        # We do NOT feed this data into _screen_buf — the Ctrl+L sent at the
        # end of suppression will realign both the remote PTY and local buffer
        # to row 0.
        if self._suppress_rendering:
            self._shell_setup_tail = (self._shell_setup_tail + text)[-4096:]
            if self._shell_setup_sentinel and self._shell_setup_sentinel in self._shell_setup_tail:
                self._shell_setup_sentinel = ""
                QTimer.singleShot(150, self._end_rendering_suppression)
            elif self._shell_setup_looks_unsupported():
                # Not a POSIX shell — the marker will never arrive. Stop hiding
                # the session instead of burning the whole timeout.
                self._abandon_shell_integration()
            return

        # A full-screen program owns the screen: its escape sequences must
        # reach the emulator byte for byte, so progress coalescing (which
        # rewrites lines) is bypassed while one is running.
        if (self._screen_buf._using_alt or self._screen_buf._escape_buffer
                or _ALT_SCREEN_RE.search(text) or _PARTIAL_CSI_RE.search(text)):
            self._progress_coalescer.reset()
            display_text = text
        else:
            display_text = self._progress_coalescer.feed(text)
        self._pending_render_bytes += len(display_text)

        # Smart Prompt Detector — check for password prompts in raw data (only scan tail of stream for speed)
        # Feed data into the virtual screen buffer FIRST so prompt detection
        # can check the actual terminal state (not just the raw chunk)
        self._screen_buf.feed(display_text)
        self._update_terminal_title_from_osc()
        self._update_command_status()

        if clean_text and self._looks_like_action_prompt(
            clean_text, self._screen_buf.get_last_nonempty_line()
        ):
            self._set_terminal_attention(True)

        # Detect a sudo privilege escalation from the password prompt itself.
        # This handles history-recalled commands (Up arrow → Enter) where
        # _track_shell_transition_command never sees "sudo -i" as keystrokes.
        # Re-armed on every prompt, so a retry after "Sorry, try again." gets
        # its own 15 seconds rather than the tail of the first attempt's.
        if clean_text and re.search(r'\[sudo[:\]].*password', clean_text, re.IGNORECASE):
            self._arm_pending_shell_resync()

        # Now check the screen buffer's last line for a prompt
        if self.pending_resync:
            last_line = self._screen_buf.get_last_nonempty_line()
            if self._looks_like_prompt(last_line) and not self._looks_like_action_prompt(
                clean_text, last_line.lower()
            ):
                self._handle_shell_transition()

        self._schedule_render()

    def _schedule_render(self):
        """Debounce terminal painting with light backpressure during output floods."""
        pending = self._pending_render_bytes
        if pending > 262144:
            interval = 33
        elif pending > 65536:
            interval = 16
        else:
            interval = 8

        if self._render_timer.interval() != interval:
            self._render_timer.setInterval(interval)
        if not self._render_timer.isActive():
            self._render_timer.start()

    def _update_terminal_title_from_osc(self):
        title = getattr(self._screen_buf, "window_title", "")
        if not title:
            return
        host = self.connection_info.get("host", "")
        cwd = self.remote_cwd
        suffix = f"  ({cwd})" if cwd else ""
        self.title_label.setText(f"  {title} - {host}{suffix}")

    @property
    def remote_cwd(self) -> str:
        """Directory the remote shell last reported (OSC 7), or "".

        Worth having beyond the title: it is what lets a duplicated pane or
        an SFTP tab open where the shell actually is rather than at $HOME.
        """
        return getattr(self._screen_buf, "cwd", "") or ""

    # -- Shell integration (OSC 133) ---------------------------------

    def _update_command_status(self):
        """Show the exit status of the last command the shell reported."""
        buf = self._screen_buf
        if not getattr(buf, "shell_integration", False):
            return
        block = buf.last_command()
        if block is None or block.running:
            return
        if block is self._last_reported_command:
            return
        self._last_reported_command = block

        code = block.exit_code
        seconds = block.duration
        if seconds >= 1.0:
            took = f"  {seconds:.1f}s" if seconds < 60 else f"  {seconds / 60:.1f}m"
        else:
            took = ""
        if code is None:
            self.cmd_status_label.setText("")
            return
        if code == 0:
            self.cmd_status_label.setText(f"✓{took}")
            self.cmd_status_label.setStyleSheet(
                "color: #4ade80; font-size: 11px;")
        else:
            self.cmd_status_label.setText(f"✗ exit {code}{took}")
            self.cmd_status_label.setStyleSheet(
                "color: #f87171; font-size: 11px;")

    def _prompt_rows_in_document(self) -> list:
        """Document line numbers where the shell said a prompt begins."""
        try:
            return self._screen_buf.prompt_lines()
        except Exception:
            return []

    def _jump_to_prompt(self, direction: int) -> bool:
        """Move the view to the previous (-1) or next (+1) prompt."""
        lines = self._prompt_rows_in_document()
        if not lines:
            self._flash_status(
                "No prompt marks in this session — the shell integration "
                "hooks did not take on this host")
            return True
        current = self.terminal.top_line()
        if direction < 0:
            targets = [line for line in lines if line < current]
            target = targets[-1] if targets else lines[0]
        else:
            targets = [line for line in lines if line > current]
            target = targets[0] if targets else lines[-1]
        self._scroll_to_document_line(target)
        return True

    def _scroll_to_document_line(self, line: int):
        """Put a document line at the top of the viewport, and stay there."""
        self.terminal.scroll_to_line(int(line), follow=False)

    def _copy_last_output(self) -> bool:
        """Copy the output of the last command, exactly as the shell marked it."""
        region = None
        try:
            region = self._screen_buf.last_output_region()
        except Exception:
            region = None
        if not region:
            self._flash_status(
                "No command output marked — the shell integration hooks "
                "did not take on this host")
            return True
        text = self._screen_buf.region_text(*region)
        if not text.strip():
            self._flash_status("Last command produced no output")
            return True
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        lines = text.count("\n") + 1
        self._flash_status(f"Copied {lines} line{'s' if lines != 1 else ''}")
        return True

    def _on_remote_clipboard(self, text: str):
        """OSC 52 — the remote asked to put something on the local clipboard."""
        clipboard = QApplication.clipboard()
        if clipboard is None or not text:
            return
        clipboard.setText(text)
        # Never silent: a remote host changing the local clipboard is
        # something the person at the keyboard should see happen.
        self._flash_status("Clipboard set by remote")

    def _set_status_light(self, color: str):
        """Colour the header dot, glow included, so its halo agrees with it."""
        indicator = getattr(self, "status_indicator", None)
        if indicator is None:
            return
        indicator.setStyleSheet(f"color: {color}; font-size: 12px;")
        glow = getattr(self, "_status_glow", None)
        if glow is not None:
            c = QColor(color)
            c.setAlpha(150)
            glow.setColor(c)

    def _flash_status(self, message: str, msec: int = 2500):
        """Show a short note in the header, then put the latency back."""
        self.latency_label.setText(message)
        self._status_flash_timer.start(msec)

    def _clear_status_flash(self):
        if self._connected:
            self._update_latency()
        else:
            self.latency_label.setText("Disconnected")

    def _looks_like_action_prompt(self, clean_tail: str, last_line: str) -> bool:
        """Detect prompts where the user needs to type before output continues."""
        text = " ".join(part for part in (clean_tail, last_line.lower()) if part)
        if not text:
            return False
        patterns = (
            r"\bpassword(?: for [^:]+)?:\s*\**\s*$",
            r"\bpassphrase(?: for key [^:]+)?:\s*\**\s*$",
            r"\benter .*:\s*$",
            r"\bpress (?:enter|return|any key)\b",
            r"\bcontinue\? \[(?:y/n|yes/no|y/n/[a-z])\]",
            r"\bare you sure you want to continue connecting\b",
            r"\bdo you want to continue\?",
            r"\bproceed\? \[(?:y/n|yes/no)\]",
            r"\bconfiguration file .* modified\b",
            r"\bselect an option\b",
        )
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)

    def _set_terminal_attention(self, pending: bool):
        if self._terminal_attention_pending == pending:
            return
        self._terminal_attention_pending = pending
        if pending:
            self.shell_badge.setText("INPUT")
            self.shell_badge.setStyleSheet(
                "background:#f59e0b; color:#111827; border-radius:8px; "
                "font-weight:800; padding:2px 8px;"
            )
            vbar = self.terminal.verticalScrollBar()
            if vbar is not None:
                vbar.setValue(vbar.maximum())
        else:
            self.shell_badge.setText("LIVE" if self._connected else "SSH")
            self.set_theme(self.theme)

    # ── Rendering ───────────────────────────────────────────────────
    #
    # The document holds the scrollback history followed by exactly one block
    # per screen row. The screen block is never resized, only rewritten, and
    # the viewport is anchored to its first line — so a program redrawing
    # itself cannot make the text slide up and down. Only rows the emulator
    # actually touched are rewritten, which is what keeps typing cheap.

    def _default_colors(self) -> tuple[str, str]:
        theme = getattr(self, "active_theme", self.theme)
        return (
            theme.get("terminal_fg", "#e2e8f0"),
            theme.get("bg_terminal", "#0a0e18"),
        )

    def _force_full_render(self):
        """Repaint everything from the buffer."""
        self._screen_buf.mark_all_dirty()
        self._render_screen()

    def _render_screen(self):
        """Push what changed in the buffer at the view.

        There is no rendered copy to keep in step any more — the view paints
        straight out of the grid — so a frame is: find out which rows changed,
        tell the view, and put the viewport back where it belongs.
        """
        if self._rendering:
            return
        buf = self._screen_buf
        terminal = getattr(self, "terminal", None)
        if terminal is None or not buf.dirty:
            return

        self._rendering = True
        try:
            all_dirty, dirty_rows = buf.consume_dirty()
            buf.dirty = False
            terminal.refresh(all_dirty, dirty_rows)
            self._apply_scroll_anchor()
            if self.search_bar.isVisible() and self.search_input.text():
                # New output can add or remove hits; re-scan on the debounce
                # rather than on every chunk.
                self._search_timer.start()
        except Exception:
            # A render error must never take the session down, and must not
            # leave the view stale either: ask for another frame.
            buf.dirty = True
            self._render_timer.start(120)
        finally:
            self._rendering = False
        self._pending_render_bytes = 0

    # ── Viewport anchoring ──────────────────────────────────────────

    @property
    def _grid_block0(self) -> int:
        """Document line the live screen starts at."""
        return self.terminal.screen_top_line()

    @property
    def _follow_output(self) -> bool:
        return self.terminal.follow_output

    @_follow_output.setter
    def _follow_output(self, value: bool):
        self.terminal.follow_output = bool(value)

    def _visible_rows(self) -> int:
        return self.terminal.visible_rows()

    def _screen_overflow_rows(self) -> int:
        """Rows the screen needs beyond the viewport (only while resizing)."""
        buf = self._screen_buf
        last_used = buf.cursor_row
        for index in range(buf.rows - 1, -1, -1):
            row = buf.grid[index]
            if any(cell.char != " " for cell in row):
                last_used = max(last_used, index)
                break
        return max(0, last_used - self._visible_rows() + 1)

    def _apply_scroll_anchor(self):
        """Pin the first screen row to the top of the viewport.

        That is what a real terminal shows: the screen fills the window and
        history sits above it. A view the user has scrolled away from is left
        alone — and because the view holds its position in absolute line
        numbers, history falling off the top cannot slide it either.
        """
        if not self.terminal.follow_output:
            return
        self.terminal.scroll_to_line(
            self.terminal.screen_top_line() + self._screen_overflow_rows(),
            follow=True,
        )

    # ── Cursor ──────────────────────────────────────────────────────

    def _sync_cursor_overlay(self):
        """Tell the view whether the cursor should be drawn right now."""
        terminal = getattr(self, "terminal", None)
        if terminal is None:
            return
        terminal.cursor_enabled = self._connected
        terminal.set_cursor_blink(
            self._cursor_blink_visible or not terminal.hasFocus())

    def _hold_cursor_solid(self):
        """Show the cursor at once and start a fresh blink period.

        Typing or output must never catch the cursor in its off phase: the
        view is told to draw it now, and the blink timer is restarted so the
        next toggle is a full period away. Without the restart, key repeat
        (a held arrow key) raced the timer — every keystroke set the flag
        just before the tick cleared it, and the cursor stayed hidden until
        the key was released.
        """
        self._cursor_blink_visible = True
        self._sync_cursor_overlay()
        blink = getattr(self, "_blink_timer", None)
        if blink is not None and blink.isActive():
            blink.start()

    # ── Selection ───────────────────────────────────────────────────

    def _on_selection_changed(self):
        """Note that the selection changed, for anything that reads it.

        Nothing has to freeze for a drag any more. A selection is a pair of
        grid coordinates rather than offsets into a document, so output
        arriving mid-drag cannot move it; the view simply stops scrolling
        while the button is down and catches up when it comes back up.
        """
        self._cursor_blink_visible = True

    def _copy_selection(self) -> bool:
        text = self.terminal.selected_text()
        if not text:
            return False
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        return True

    # ── Font Controls ──────────────────────────────────────────────

    def _increase_font(self):
        self.set_terminal_font_size(self._font_size + 1, emit_change=True)

    def _decrease_font(self):
        self.set_terminal_font_size(self._font_size - 1, emit_change=True)

    def _apply_terminal_font(self):
        """Pick the best available monospace face and hand it to the view."""
        self._ensure_nerd_fonts_loaded()

        local_font = self._local_font

        if local_font != "Default Font":
            font_families = [local_font]
        else:
            # Try premium fonts first
            font_families = [
                "MesloLGS NF",
                "MesloLGSNerdFontMono-Regular",
                "MesloLGS Nerd Font Mono",
                "MesloLGL Nerd Font Mono",
                "JetBrains Mono",
                "Fira Code",
                "Cascadia Code",
                "Consolas",
                "Courier New",
                "monospace",
            ]

        font = QFont()
        for family in font_families:
            font.setFamily(family)
            if font.exactMatch() or family == "monospace" or local_font != "Default Font":
                break

        font.setPointSize(self._font_size)
        font.setFixedPitch(True)
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
        font.setKerning(False)

        # Zooming is now just new cell metrics: no document to walk, and no
        # cost that grows with the length of the history.
        self.terminal.set_terminal_font(font)
        if getattr(self, "_screen_buf", None) is not None:
            self._force_full_render()
        if self._connected:
            self._resize_timer.start(80)

    def _apply_font(self):
        self._apply_terminal_font()

    def _ensure_nerd_fonts_loaded(self):
        """Load bundled Meslo Nerd Fonts so icon glyphs always render correctly."""
        if SSHTerminal._fonts_loaded:
            return

        try:
            # Normally done already, while the login screen waited.
            from app.fonts import register_bundled_fonts
            register_bundled_fonts()
        except Exception:
            pass

        SSHTerminal._fonts_loaded = True

    # Hidden setup gets at most this long before the terminal comes back,
    # whatever the remote is doing. It used to be 7 s, which is a very long
    # time to stare at an empty terminal on every single connect.
    SHELL_SETUP_TIMEOUT_MS = 3000

    # Output that proves the far end is not a POSIX shell, so the hidden setup
    # will never complete and there is no point waiting for it.
    _NON_POSIX_MARKERS = (
        "is not recognized as an internal or external command",
        "the term '{' is not recognized",
        "cannotbeloaded",
        "unexpected token",
        "invalid input detected",
        "% invalid",
        "unknown command",
        "microsoft windows [version",
        "command not found: {",
        "parse error near",
    )

    def _build_shell_integration_script(self, sentinel_token: str) -> str:
        """Return the one-shot setup script sent to the remote shell.

        Only the OSC 133 / OSC 7 hooks go down the wire — the remote prompt,
        aliases and PATH are left exactly as the login shell set them.

        The completion marker is printed by joining two halves at runtime, so
        the shell's own echo of this command can never contain the finished
        marker — the echo was previously mistaken for completion and cut the
        setup off halfway through.
        """
        script_lines: list[str] = ["{"]
        script_lines.extend(self._shell_integration_lines())
        script_lines.append(f"printf '\\n__%s__DONE__\\n' '{sentinel_token}'")
        script_lines.append("} 2>/dev/null")
        return "\r".join(script_lines) + "\r"

    @staticmethod
    def _script_lines(name: str) -> list:
        """Read a shell resource as a list of runnable lines.

        Comments and blank lines are dropped because the whole script is sent
        down the wire as one hidden command; there is no point paying for
        either.
        """
        path = os.path.join(os.path.dirname(__file__), "resources", name)
        if not os.path.exists(path):
            return []
        lines = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    lines.append(stripped)
        return lines

    @staticmethod
    def _shell_integration_lines() -> list:
        """Hooks that report where prompts, commands and output begin.

        This is the OSC 133 convention (FinalTerm, iTerm2 and VS Code all
        speak it). Once the shell emits these marks the terminal stops
        guessing: jump-to-prompt, copy-last-output and the exit status in the
        header all read something the shell placed itself. OSC 7 adds the
        working directory, which is what lets the file browser open where the
        shell actually is.
        """
        return SSHTerminal._script_lines("shell-integration.sh")

    def _apply_shell_integration(self):
        """Install the OSC 133 / OSC 7 hooks on the remote shell.

        Sends one hidden setup script so command text never pollutes the session.
        """
        if not self._shell_integration_enabled:
            return
        if self._suppress_rendering:
            # A previous setup is still running. Don't drop this request (e.g.
            # a sudo -i resync fired mid-window) — remember it and re-apply
            # once the current suppression ends.
            self._integration_apply_pending = True
            return
        try:
            # Suppress all terminal rendering while setup runs
            self._suppress_rendering = True
            token = f"RPMOTE_SYNC_{int(time.monotonic() * 1000)}"
            self._shell_setup_sentinel = f"__{token}__DONE__"
            self._shell_setup_tail = ""

            self._sending_shell_setup = True
            try:
                self._send_data(self._build_shell_integration_script(token))
            finally:
                self._sending_shell_setup = False
            self._shell_integration_applied = True
            self._render_fallback_timer.start(self.SHELL_SETUP_TIMEOUT_MS)
        except Exception as e:
            self._sending_shell_setup = False
            self._end_rendering_suppression()
            print(f"Error applying shell integration: {e}")

    def _shell_setup_looks_unsupported(self) -> bool:
        """True when the captured setup output proves this is not a POSIX shell."""
        tail = self.ansi_parser.strip_ansi(self._shell_setup_tail).lower()
        return any(marker in tail for marker in self._NON_POSIX_MARKERS)

    def _abandon_shell_integration(self):
        """Give up on the hooks for this session and show the raw shell."""
        self._shell_integration_enabled = False
        self._end_rendering_suppression(realign=False)

    def _end_rendering_suppression(self, realign: bool = True):
        """Resume normal rendering after the hidden shell-integration setup."""
        if not self._suppress_rendering:
            return
        self._render_fallback_timer.stop()
        self._suppress_rendering = False
        self._shell_setup_sentinel = ""
        self._shell_setup_tail = ""
        self._progress_coalescer.reset()
        if self._connected:
            self.shell_badge.setText("LIVE")

        # Keep everything that was already on screen — the login banner, the
        # MOTD, whatever the last command printed — by pushing it into
        # scrollback before the realign blanks the grid. `keep_last=1` drops the
        # trailing prompt line, which the redraw below re-prints.
        self._screen_buf.archive_visible_screen(keep_last=1)

        # Clear the screen buffer so stale setup text is never rendered.
        # The Ctrl+L response from the remote will repopulate it with a
        # clean prompt at row 0, realigning both sides.
        self._screen_buf._erase_in_display(2)
        self._screen_buf.dirty = True
        self._render_screen()

        if realign and self._looks_like_auth_or_password_prompt(
            self._screen_buf.get_last_nonempty_line().strip().lower()
        ):
            # Whatever is reading input is a password prompt; a form-feed
            # there becomes part of the password (and sudo-rs echoes it as
            # a bare "^L"). Leave the screen as it is.
            realign = False
        if realign:
            # Send Ctrl+L (form-feed) to clear the remote screen and redraw the
            # prompt at row 0.  This realigns the remote PTY cursor with our
            # local ScreenBuffer so that later absolute-positioning sequences
            # land on the correct rows.
            self._sending_shell_setup = True
            try:
                self._send_data("\x0c")
            finally:
                self._sending_shell_setup = False

        # A shell-transition resync (sudo -i / su) was requested while this
        # setup was still suppressing output. Re-apply it now that the shell is
        # realigned, so the hooks land on the new (e.g. root) shell.
        if self._integration_apply_pending:
            self._integration_apply_pending = False
            if self._shell_integration_enabled:
                QTimer.singleShot(500, self._apply_shell_integration)

    def _maybe_apply_initial_shell_integration(self):
        """Install the hooks once, only before the user starts typing."""
        if (
            not self._connected
            or not self._shell_integration_enabled
            or self._shell_integration_applied
            or self._user_input_since_connect
            or self._suppress_rendering
        ):
            return

        last_line = self._screen_buf.get_last_nonempty_line()
        clean_line = self.ansi_parser.strip_ansi(last_line).strip().lower()
        if self._looks_like_action_prompt(clean_line, clean_line):
            return

        if self._looks_like_prompt(last_line):
            self._apply_shell_integration()
            return

        if time.monotonic() < self._initial_shell_integration_deadline:
            QTimer.singleShot(500, self._maybe_apply_initial_shell_integration)

    def _on_connected(self):
        self._cancel_auto_reconnect()
        self.failure_panel.hide()
        # The session is up: the sign goes now rather than waiting for the
        # remote to say something, since a shell that is merely quiet is
        # still connected.
        self.terminal.set_placeholder("")
        self._set_status_light("#4ade80")
        self.health_bar.set_connection_state("connected")
        self.latency_label.setText("Connected")
        self.shell_badge.setText("LIVE")
        self._shell_integration_applied = False
        self._user_input_since_connect = False
        self._initial_shell_integration_deadline = time.monotonic() + 5.0
        self._start_latency_ping()
        self._resize_remote_pty()
        QTimer.singleShot(0, self.terminal.setFocus)
        QTimer.singleShot(1200, self._maybe_apply_initial_shell_integration)

    def showEvent(self, event):
        super().showEvent(event)
        first_show = not self._laid_out
        self._laid_out = True
        if first_show:
            # The PTY was opened against the placeholder size — force the first
            # real measurement through instead of short-circuiting on equality.
            self._last_pty_size = None
        QTimer.singleShot(0, self.terminal.setFocus)
        QTimer.singleShot(0, self._resize_remote_pty)
        QTimer.singleShot(250, self._resize_remote_pty)

    def _on_connection_lost(self, message: str):
        # Never leave a dropped session stuck behind the hidden-setup blank.
        self._end_rendering_suppression(realign=False)
        self._set_status_light("#f87171")
        self.health_bar.set_connection_state("offline")
        self.shell_badge.setText("OFFLINE")
        # Put the notice through the emulator rather than appending it to
        # the document directly — the renderer owns every block below the
        # history, and text inserted behind its back is dropped on the
        # next repaint.
        self._screen_buf.feed(f"\r\n\x1b[1;31m{message}\x1b[0m\r\n")
        self._render_screen()
        self.latency_label.setText("Disconnected")
        self._close_recording(f"connection lost: {message}")

        failure, self._last_failure = self._last_failure, None
        if failure is None:
            return
        kind, reason = failure
        # A failed attempt to come back is one retry, not the end of them:
        # keep backing off until the attempts run out.
        if self._reconnect_attempt and self._schedule_auto_reconnect():
            return
        title = None
        if self._reconnect_attempt:
            name = self.connection_info.get("name") or self.connection_info.get("host", "")
            title = f"Couldn't reconnect to {name}"
            reason = f"{reason}  (Tried {self._reconnect_attempt} times.)"
        self.failure_panel.show_failure(self.connection_info, kind, reason, title=title)

    def apply_connection_update(self, conn: dict):
        """Take the edited connection (from the failure panel) and try again."""
        self.connection_info.clear()
        self.connection_info.update(conn)
        self._auto_reconnect = bool(conn.get("auto_reconnect", True))
        name = conn.get("name", "SSH")
        if self._protocol == "serial":
            where = f"{conn.get('serial_port') or conn.get('host', '')} @ {conn.get('baud_rate', 9600)} baud"
        else:
            where = f"{conn.get('host', '')}:{conn.get('port', 22)}"
        self.title_label.setText(f"{name} — {where}")
        self.reconnect()

    def _send_terminal_reply(self, data: str):
        """Answer a terminal query from the remote program."""
        if not self._connected or not self.channel or self.channel.closed:
            return
        try:
            self.channel.sendall(data.encode("utf-8"))
        except Exception:
            pass

    # ── Production, broadcast and recording ─────────────────────────

    @property
    def is_production(self) -> bool:
        """Whether this session is on a machine the user called production."""
        return guardrails.is_production(self.connection_info,
                                        self._production_tenants)

    def set_production_tenants(self, tenants):
        self._production_tenants = guardrails.normalize_production_tenants(
            tenants)
        self._refresh_prod_badge()

    def _refresh_prod_badge(self):
        badge = getattr(self, "prod_badge", None)
        if badge is None:
            return
        production = self.is_production
        reason = guardrails.production_reason(
            self.connection_info, self._production_tenants)
        badge.setVisible(production)
        if production:
            badge.setToolTip(reason)

        banner = getattr(self, "prod_banner", None)
        if banner is not None:
            banner.setVisible(production)
            name = self.connection_info.get("name") or self.connection_info.get("host")
            banner.setText(f"PRODUCTION — {name}    ·    {reason}"
                           if production else "")

    def set_broadcast_group(self, group):
        """Join (or leave) a group of panes that type together."""
        previous = self.broadcast_group
        if previous is not None and previous is not group:
            try:
                previous.changed.disconnect(self._refresh_broadcast_badge)
            except TypeError:
                pass
        self.broadcast_group = group
        if group is not None:
            group.changed.connect(self._refresh_broadcast_badge)
        self._refresh_broadcast_badge()

    def _refresh_broadcast_badge(self):
        badge = getattr(self, "broadcast_badge", None)
        if badge is None:
            return
        group = self.broadcast_group
        armed = bool(group is not None and group.armed)
        included = armed and group.is_included(self)
        badge.setVisible(armed)
        # A narrow pane keeps the badge, just shorter — it must never be the
        # thing that gets squeezed out.
        narrow = getattr(self, "_header_mode", "") in ("fold", "minimal")
        if included and narrow:
            badge.setPixmap(icons.pixmap("broadcast", 13, "#fde68a",
                                         badge.devicePixelRatioF()))
        else:
            badge.setText("BROADCAST" if included else "MUTED")
        badge.setMinimumWidth(26 if narrow else 0)
        badge.setToolTip(
            "Typing here also goes to the other panes in this session."
            if included else
            "This pane is left out of the broadcast: typing here stays here.")

    # ── Running a whole command ─────────────────────────────────────

    def open_command_palette(self):
        """Ctrl+K. The host supplies the snippets; the session supplies the rest."""
        from app.command_palette import CommandPalette, palette_entries

        entries = None
        provider = getattr(self, "snippet_provider", None)
        if callable(provider):
            try:
                entries = provider()
            except Exception:
                entries = None
        if entries is None:
            entries = palette_entries(None)

        palette = CommandPalette(
            entries,
            session={"conn": self.connection_info, "cwd": self.remote_cwd},
            parent=self)
        palette.command_chosen.connect(self.run_command)
        palette.exec()

    def run_command(self, command: str, confirm: bool = True) -> bool:
        """Type a whole command into this session, with a look before the leap.

        Returns whether it was sent.  A command that arrives complete — from
        the palette, a snippet, a broadcast — is worth checking against the
        guardrails first: it is about to run in one keystroke, which is
        exactly when nobody re-reads it.
        """
        command = str(command or "").strip()
        if not command:
            return False
        if confirm and not self.confirm_command(command):
            return False
        self._send_data(command + "\r")
        return True

    def confirm_command(self, command) -> bool:
        """Ask about a command when this session is one worth asking about."""
        confirmation = guardrails.confirmation_for(
            command, [self.connection_info], self._production_tenants)
        if confirmation is None:
            return True
        answer = QMessageBox.question(
            self, confirmation.title, confirmation.body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes

    # ── Drag a file onto the terminal ───────────────────────────────

    def dragEnterEvent(self, event):
        if not self._is_stream and self._connected and local_paths_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if not self._is_stream and self._connected and local_paths_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        paths = local_paths_from_mime(event.mimeData())
        if self._is_stream or not paths or not self._connected or self.client is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self.upload_files(paths)

    def upload_files(self, paths):
        """Ask, then put the files where the shell is standing."""
        if self._uploader is not None and self._uploader.busy:
            QMessageBox.information(self, "Upload",
                                    "An upload is already running.")
            return
        cwd = self.remote_cwd
        answer = QMessageBox.question(
            self, "Upload to this session",
            describe_drop(paths, cwd),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            return

        if self._uploader is None:
            self._uploader = TerminalUploader(self)
            self._uploader.progress.connect(self._on_upload_progress)
            self._uploader.finished.connect(self._on_upload_finished)
        if not self._uploader.upload(self.client, paths, cwd):
            self._on_upload_finished(0, 0, ["Could not start the upload."])

    def _on_upload_progress(self, message: str):
        self.cmd_status_label.setText(message)
        self.cmd_status_label.setStyleSheet(
            "color: #a0a3b8; font-size: 11px;")

    def _on_upload_finished(self, done: int, total: int, errors: list):
        if errors:
            self.cmd_status_label.setText(f"✗ upload: {len(errors)} failed")
            self.cmd_status_label.setStyleSheet(
                "color: #f87171; font-size: 11px;")
            QMessageBox.warning(
                self, "Upload",
                f"Uploaded {done} of {total}.\n\n" + "\n".join(errors[:8]))
        else:
            self.cmd_status_label.setText(f"✓ uploaded {done}")
            self.cmd_status_label.setStyleSheet(
                "color: #4ade80; font-size: 11px;")

    # ── Session recording ───────────────────────────────────────────

    @property
    def recording_path(self):
        """Where this session is being written, or None if it is not."""
        recorder = self._recorder
        return recorder.path if recorder is not None and recorder.active else None

    def _ensure_recorder(self):
        """Open the recording on the first byte, not before.

        A connection that never gets off the ground would otherwise leave a
        header-only file behind every time someone fat-fingers a hostname.
        """
        if not self._record_session or self._recorder is not None:
            return None
        try:
            cols, rows = self._terminal_size_for_pty()
            info = self.connection_info
            self._recorder = session_log.SessionRecorder(
                session_log.session_path(info),
                width=cols, height=rows,
                title=f"{info.get('name', 'SSH')} — {info.get('host', '')}",
                env={"TERM": "xterm-256color"},
            )
        except Exception:
            # Recording is an extra, never a reason a session cannot open.
            self._record_session = False
            self._recorder = None
        return self._recorder

    def _record_output(self, text: str):
        if not self._record_session or not text:
            return
        recorder = self._recorder or self._ensure_recorder()
        if recorder is not None:
            recorder.write_output(text)

    def stop_recording(self):
        """Close the recording early, at the user's request."""
        recorder, self._recorder = self._recorder, None
        self._record_session = False
        if recorder is not None:
            recorder.close()
            return recorder.path if recorder.event_count else None
        return None

    def _close_recording(self, note: str = ""):
        recorder = self._recorder
        if recorder is None:
            return
        if note:
            recorder.note(note)
        recorder.close()

    def _send_data(self, data: str, broadcast: bool = True):
        """Send data to the SSH channel.

        *broadcast* is what separates a keystroke from bookkeeping: mouse
        reports and terminal replies belong to this pane's own conversation
        with its program, and mirroring them to thirty other machines would
        be nonsense.
        """
        if (broadcast and data and self._connected
                and not self._sending_shell_setup
                and self.broadcast_group is not None):
            # Only a pane that could send it itself mirrors it: typing into a
            # session that has dropped should do nothing at all, here least
            # of all on thirty other machines.
            self.broadcast_group.fan_out(self, data)

        if data:
            # Typing always snaps the view back to the live screen.
            self._follow_output = True

        if not self._connected or not self.channel or self.channel.closed:
            return

        if data and not self._sending_shell_setup:
            self._user_input_since_connect = True
            self._track_shell_transition_command(data)

        if data:
            self._set_terminal_attention(False)

        # Keep cursor solid while typing
        self._hold_cursor_solid()

        try:
            self.channel.sendall(data.encode("utf-8"))
        except Exception as e:
            self.connection_lost.emit(f"Send error: {str(e)}")

    def _send_paste(self, text: str):
        """Send pasted text using bracketed paste when possible for shell safety."""
        if not text:
            return

        normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        if not normalized:
            return

        if not self._screen_buf.bracketed_paste and self._paste_needs_confirmation(normalized):
            result = QMessageBox.question(
                self,
                "Confirm paste",
                "Paste multiple lines into this SSH session?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if result != QMessageBox.StandardButton.Yes:
                return

        if self._screen_buf.bracketed_paste:
            self._send_data(f"\x1b[200~{normalized}\x1b[201~")
        else:
            self._send_data(normalized)

    @staticmethod
    def _paste_needs_confirmation(text: str) -> bool:
        lines = [line for line in text.split("\n") if line.strip()]
        if len(lines) > 1:
            return True
        risky_tokens = ("sudo ", "rm ", "mkfs", "shutdown", "reboot", ":(){", "dd if=")
        return any(token in text for token in risky_tokens) and "\n" in text

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._connected:
            # debounce to avoid resize storms
            self._resize_timer.start(120)

    DEFAULT_PTY_SIZE = (120, 40)

    def _terminal_size_for_pty(self) -> tuple[int, int]:
        """Return PTY columns/rows based on the actual editor viewport.

        Before the widget has been laid out its viewport is meaningless, and
        sizing the remote PTY off it made the login banner and the first prompt
        wrap at the wrong width. Fall back to a sane default until the first
        showEvent, then re-sync for real.
        """
        terminal = getattr(self, "terminal", None)
        if terminal is None or not self._laid_out:
            return self.DEFAULT_PTY_SIZE

        char_w = terminal.cell_width
        char_h = terminal.cell_height
        viewport = terminal.viewport().size()
        margin = terminal.margin
        usable_w = max(0, viewport.width() - (margin * 2) - 2)
        usable_h = max(0, viewport.height() - (margin * 2) - 2)

        if usable_w < char_w * 20 or usable_h < char_h * 5:
            return self.DEFAULT_PTY_SIZE

        cols = max(40, int(usable_w / char_w))
        rows = max(10, int(usable_h / char_h))
        return cols, rows

    def _resize_remote_pty(self):
        """Keep remote PTY dimensions in sync with visible terminal viewport."""
        if not self._connected:
            return

        cols, rows = self._terminal_size_for_pty()
        size = (cols, rows)
        if size == self._last_pty_size:
            return
        self._last_pty_size = size

        if self._recorder is not None:
            self._recorder.resize(cols, rows)
        self._screen_buf.resize(rows, cols)

        if self.channel and not self.channel.closed:
            try:
                self.channel.resize_pty(width=cols, height=rows)
            except Exception:
                pass

        self._schedule_render()

    def _handle_shell_transition(self):
        """Re-install the hooks after a shell transition (sudo -i / su)."""
        if self.pending_resync:
            self.pending_resync = False
            if self.resync_timer:
                try:
                    self.resync_timer.stop()
                except Exception:
                    pass
                self.resync_timer = None
            # A fresh `sudo -i` / `su -` login shell still needs to source its
            # profiles and print any MOTD/banner. Applying immediately races
            # that output and breaks the hidden setup block (so the hooks
            # never land). Wait for the shell to settle, then confirm we're
            # really sitting at a prompt before sending the setup.
            QTimer.singleShot(700, self._apply_shell_integration_after_transition)

    def _apply_shell_integration_after_transition(self, attempts_left: int = 3):
        """Install the hooks once the post-transition shell has settled."""
        if self._suppress_rendering:
            # Another setup is mid-flight; it will re-arm us on completion.
            self._integration_apply_pending = True
            return
        last_line = self._screen_buf.get_last_nonempty_line()
        if self._looks_like_auth_or_password_prompt(last_line.strip().lower()):
            # sudo/su is still asking for a password — the user mistyped it,
            # or is just slow. Typing the setup script here would send it as
            # the password and then hide the retry prompt behind rendering
            # suppression. Hand the job back to the stream watcher, which
            # picks the new shell's prompt up once the password is accepted.
            self._arm_pending_shell_resync()
            return
        if self._looks_like_prompt(last_line):
            self._apply_shell_integration()
        elif attempts_left > 0:
            QTimer.singleShot(
                600, lambda: self._apply_shell_integration_after_transition(attempts_left - 1)
            )
        else:
            # Give up waiting for a clean prompt and apply anyway.
            self._apply_shell_integration()

    def _track_shell_transition_command(self, data: str):
        """Mark successful shell-changing commands for a one-time hidden resync."""
        if not data:
            return

        if data in ("\r", "\n", "\r\n"):
            command = self._last_sent_command.strip().lower()
            self._last_sent_command = ""
        elif "\r" in data or "\n" in data:
            before_enter = re.split(r"[\r\n]", data, maxsplit=1)[0]
            command = (self._last_sent_command + before_enter).strip().lower()
            self._last_sent_command = ""
        elif len(data) == 1 and ord(data) >= 32:
            self._last_sent_command += data
            return
        elif data == "\x7f":
            self._last_sent_command = self._last_sent_command[:-1]
            return
        else:
            return

        if self._is_shell_transition_command(command):
            self._arm_pending_shell_resync()

    def _arm_pending_shell_resync(self):
        """Expect a new shell's prompt; give up quietly if none shows in 15s."""
        self.pending_resync = True
        if self.resync_timer:
            try:
                self.resync_timer.stop()
            except Exception:
                pass
        self.resync_timer = QTimer(self)
        self.resync_timer.setSingleShot(True)
        self.resync_timer.timeout.connect(self._cancel_pending_shell_resync)
        self.resync_timer.start(15000)

    @staticmethod
    def _is_shell_transition_command(command: str) -> bool:
        """True for commands that drop into a new interactive shell (sudo/su).

        Covers the common privilege-escalation forms so the hooks are
        re-installed afterwards: `sudo -i`, `sudo -s`, `sudo su`, `sudo su -`,
        `sudo -H -i`, `su`, `su -`, `su - root`, `su -l`, etc.
        """
        if not command:
            return False
        norm = re.sub(r"\s+", " ", command).strip().lower()
        # su family — su, su -, su -l, su - <user>, su <user>
        if re.match(r"^su(?:\s+.*)?$", norm):
            return True
        # sudo launching a shell — needs an -i/-s flag or an `su` subcommand
        if re.match(r"^sudo\b", norm) and re.search(r"(?:^|\s)(?:-i|-s|su)\b", norm):
            return True
        return False

    def _cancel_pending_shell_resync(self):
        """Cancel auto-resync after a failed sudo/su/login attempt."""
        self.pending_resync = False
        if self.resync_timer:
            try:
                self.resync_timer.stop()
            except Exception:
                pass
            self.resync_timer = None

    @staticmethod
    def _looks_like_auth_failure(clean_text: str) -> bool:
        if not clean_text:
            return False
        patterns = (
            r"\bauthentication failed\b",
            r"\bauthentication failure\b",
            r"\bmaximum \d+ incorrect authentication attempts\b",
            r"\b\d+ incorrect password attempts?\b",
            r"\bsudo: .*incorrect password attempts?\b",
            r"\bsu: authentication failure\b",
            r"\bpermission denied, please try again\b",
        )
        return any(re.search(pattern, clean_text, re.IGNORECASE) for pattern in patterns)

    @staticmethod
    def _looks_like_auth_or_password_prompt(clean_text: str) -> bool:
        if not clean_text:
            return False
        # Trailing asterisks: sudo built with pwfeedback (Ubuntu's sudo-rs
        # among them) echoes one per typed character, and the prompt has to
        # keep reading as a prompt while the password is being typed.
        patterns = (
            r"\[sudo[:\]].*password",
            r"\bpassword(?: for [^:]+)?:\s*\**\s*$",
            r"\bpassphrase(?: for key [^:]+)?:\s*\**\s*$",
            r"\bauthenticate\]\s*password:\s*\**\s*$",
        )
        return any(re.search(pattern, clean_text, re.IGNORECASE) for pattern in patterns)

    # Shell continuation prompts (bash `>`, `PS2`, heredoc) and banner lines
    # ending in an arrow used to read as "we are at a prompt", which fired the
    # hidden shell setup into the middle of an unfinished command.
    _CONTINUATION_PROMPTS = (">", ">>", "->", "=>", "-->", "...", "…")

    def _shell_marked_prompt_visible(self) -> bool:
        """True when the shell itself marked the current row as a prompt.

        Only meaningful once OSC 133 is arriving. A shell reached through
        sudo -i has not been set up yet, so this stays False there and the
        text heuristic below still does the work.
        """
        buf = self._screen_buf
        if not getattr(buf, "shell_integration", False):
            return False
        try:
            # The marks on screen are the *last* prompt's. Once the shell
            # said the command started (133;C) and has not yet drawn a new
            # prompt, whatever is on screen belongs to that command — a
            # sudo password prompt sitting under the marked row is not a
            # shell prompt, however it is marked.
            block = buf.last_command()
            if block is not None and block.running:
                return False
            from app.screen_buffer import MARK_INPUT, MARK_PROMPT
            # Only the cursor's own row counts. A marked row *above* the
            # cursor is what a just-entered command looks like too — the
            # echo scrolled the cursor down and the output (a sudo password
            # prompt, say) has not arrived yet. A prompt that spans lines
            # still marks the input row, so nothing is lost by not looking up.
            return buf.mark_at(buf.cursor_row) in (MARK_PROMPT, MARK_INPUT)
        except Exception:
            return False

    def _looks_like_prompt(self, clean_text: str) -> bool:
        """Is a shell prompt on screen right now?

        When the remote shell has shell integration running this is not a
        guess at all — it marked its own prompt. The text heuristic below
        still runs for everything else: a shell reached through ``sudo -i``
        has not been set up yet, and that is exactly the case this is asked
        about most.

        A password prompt is never a shell prompt, whatever the marks say:
        typing the setup script there sends it as the password.
        """
        if self._looks_like_auth_or_password_prompt(
            self._screen_buf.get_last_nonempty_line().strip().lower()
        ):
            return False
        if self._shell_marked_prompt_visible():
            return True
        if not clean_text:
            return False
        last_line = clean_text.splitlines()[-1].strip()
        if not last_line or last_line in self._CONTINUATION_PROMPTS:
            return False
        if last_line.endswith(("->", "=>", "-->")):
            return False
        return bool(re.search(r'([#\$❯➜%>])\s*$', last_line))

    # ── Key Handling ────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        terminal_obj = getattr(self, "terminal", None)
        terminal_viewport = terminal_obj.viewport() if terminal_obj is not None else None

        if obj is terminal_obj and isinstance(event, QKeyEvent):
            if event.type() == event.Type.KeyPress:
                return self._handle_key(event)

        if obj in (terminal_obj, terminal_viewport):
            if event.type() == event.Type.Resize:
                if self._connected:
                    self._resize_timer.start(80)
                return False
            if event.type() in (event.Type.FocusIn, event.Type.FocusOut):
                # Settle the cursor overlay at once: an unfocused terminal
                # shows a solid cursor, a focused one starts blinking again.
                self._hold_cursor_solid()
                if self._screen_buf.focus_event_mode:
                    self._send_data(
                        "\x1b[I" if event.type() == event.Type.FocusIn else "\x1b[O"
                    )
                return False
            if event.type() == event.Type.ContextMenu:
                self._show_terminal_context_menu(event.globalPos())
                return True
            if isinstance(event, (QMouseEvent, QWheelEvent)):
                if self._handle_link_click(event):
                    return True
                if self._handle_terminal_mouse(event):
                    return True
        return super().eventFilter(obj, event)

    def _show_terminal_context_menu(self, global_pos):
        menu = QMenu(self)
        copy_action = menu.addAction("Copy")
        copy_action.setEnabled(self._terminal_has_selection())
        paste_action = menu.addAction("Paste")
        menu.addSeparator()
        select_all_action = menu.addAction("Select All")
        clear_action = menu.addAction("Clear Screen")

        chosen = menu.exec(global_pos)
        if chosen == copy_action:
            self._copy_selection()
        elif chosen == paste_action:
            clipboard = QApplication.clipboard()
            if clipboard:
                self._send_paste(clipboard.text())
        elif chosen == select_all_action:
            self.terminal.select_all()
        elif chosen == clear_action:
            self._send_data("\x0c")

    def _handle_terminal_mouse(self, event) -> bool:
        """Send xterm mouse reports when the remote app has enabled them."""
        mode = getattr(self._screen_buf, "mouse_tracking_mode", 0)
        if not mode:
            return False

        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            return False

        event_type = event.type()
        pos = event.position() if hasattr(event, "position") else event.pos()
        col, row = self._terminal_cell_from_point(pos)

        if isinstance(event, QWheelEvent):
            if not self._screen_buf._using_alt:
                # On the normal screen the wheel belongs to the scrollback,
                # not to whatever enabled mouse reporting.
                return False
            delta = event.angleDelta().y()
            if delta == 0:
                return True
            button = 64 if delta > 0 else 65
            self._send_mouse_report(button, col, row, pressed=True)
            return True

        button = self._mouse_button_code(event)
        if button is None:
            return False

        if event_type == event.Type.MouseButtonPress:
            self._send_mouse_report(button, col, row, pressed=True)
            return True

        if event_type == event.Type.MouseButtonRelease:
            self._send_mouse_report(button, col, row, pressed=False)
            return True

        if event_type == event.Type.MouseMove:
            if mode not in (1002, 1003):
                return True
            if not event.buttons() and mode != 1003:
                return True
            move_button = button if event.buttons() else 3
            self._send_mouse_report(move_button + 32, col, row, pressed=True)
            return True

        return False

    #: Bare URLs in output are clickable too, not just OSC 8 links. Kept
    #: deliberately narrow: a scheme a browser understands, and no trailing
    #: punctuation, so a URL at the end of a sentence still works.
    _URL_RE = re.compile(
        r"(?:https?|ftps?)://[^\s<>\"'`{}|\\^\[\]]+", re.IGNORECASE)

    #: Schemes RPMote will hand to the desktop. Anything else — file:, ssh:,
    #: a custom handler — is not opened on the say-so of a remote host.
    _OPENABLE_SCHEMES = ("http://", "https://", "ftp://", "ftps://")

    def _link_at_point(self, pos) -> str:
        """The URL under a viewport point, from OSC 8 or from the text.

        Works over history as well as the live screen now: the view maps a
        point to a document line, so a link that has scrolled up is still a
        link.
        """
        point = pos.toPoint() if hasattr(pos, "toPoint") else pos
        doc_index, col = self.terminal.cell_at(point)

        # An OSC 8 link is authoritative: the shell said what it points at.
        attrs = self.terminal.attrs_at(doc_index, col)
        link = getattr(attrs, "link", "") if attrs is not None else ""
        if link:
            return link

        text = self.terminal.line_text(doc_index)
        for match in self._URL_RE.finditer(text):
            if match.start() <= col < match.end():
                return match.group(0).rstrip(".,;:!?)")
        return ""

    def _handle_link_click(self, event) -> bool:
        """Ctrl+Click opens a link, the way an IDE terminal does.

        Deliberately not a plain click: a plain click is how you place a
        selection, and a remote host should not be able to make a bare click
        anywhere on screen open something.
        """
        if not isinstance(event, QMouseEvent):
            return False
        if event.type() != event.Type.MouseButtonRelease:
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            return False

        pos = event.position() if hasattr(event, "position") else event.pos()
        url = self._link_at_point(pos)
        if not url:
            return False
        self._open_link(url)
        return True

    def _open_link(self, url: str):
        """Hand a link to the desktop, if it is one we are willing to open."""
        lowered = url.lower()
        if not lowered.startswith(self._OPENABLE_SCHEMES):
            self._flash_status("Link ignored — only http(s) and ftp are opened")
            return
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(url))
        self._flash_status(f"Opened {url[:60]}")

    def _terminal_cell_from_point(self, pos) -> tuple[int, int]:
        """The 1-based (col, row) cell of the live screen under a point."""
        point = pos.toPoint() if hasattr(pos, "toPoint") else pos
        return self.terminal.screen_cell_at(point)

    def _mouse_button_code(self, event) -> int | None:
        button = event.button()
        buttons = event.buttons()
        if button == Qt.MouseButton.LeftButton or buttons & Qt.MouseButton.LeftButton:
            return 0
        if button == Qt.MouseButton.MiddleButton or buttons & Qt.MouseButton.MiddleButton:
            return 1
        if button == Qt.MouseButton.RightButton or buttons & Qt.MouseButton.RightButton:
            return 2
        return None

    def _send_mouse_report(self, button: int, col: int, row: int, pressed: bool):
        if getattr(self._screen_buf, "mouse_sgr", False):
            suffix = "M" if pressed else "m"
            self._send_data(f"\x1b[<{button};{col};{row}{suffix}",
                            broadcast=False)
            return

        if not pressed:
            button = 3
        button = max(0, min(223, button + 32))
        col = max(1, min(223, col + 32))
        row = max(1, min(223, row + 32))
        self._send_data(f"\x1b[M{chr(button)}{chr(col)}{chr(row)}",
                        broadcast=False)

    # Keys reported as CSI <letter> (or SS3 <letter> in application mode).
    _CURSOR_KEYS = {
        Qt.Key.Key_Up: "A",
        Qt.Key.Key_Down: "B",
        Qt.Key.Key_Right: "C",
        Qt.Key.Key_Left: "D",
        Qt.Key.Key_Home: "H",
        Qt.Key.Key_End: "F",
    }

    # Keys reported as CSI <number> ~
    _TILDE_KEYS = {
        Qt.Key.Key_Insert: 2,
        Qt.Key.Key_Delete: 3,
        Qt.Key.Key_PageUp: 5,
        Qt.Key.Key_PageDown: 6,
        Qt.Key.Key_F5: 15,
        Qt.Key.Key_F6: 17,
        Qt.Key.Key_F7: 18,
        Qt.Key.Key_F8: 19,
        Qt.Key.Key_F9: 20,
        Qt.Key.Key_F10: 21,
        Qt.Key.Key_F11: 23,
        Qt.Key.Key_F12: 24,
    }

    # F1–F4 are SS3 sequences when unmodified.
    _SS3_FUNCTION_KEYS = {
        Qt.Key.Key_F1: "P",
        Qt.Key.Key_F2: "Q",
        Qt.Key.Key_F3: "R",
        Qt.Key.Key_F4: "S",
    }

    @staticmethod
    def _xterm_modifier(shift: bool, alt: bool, ctrl: bool, meta: bool) -> int:
        """xterm modifier parameter: 1 + shift(1) + alt(2) + ctrl(4) + meta(8)."""
        return 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0) + (8 if meta else 0)

    def _scroll_view_by_page(self, direction: int):
        """Scroll the local view by a screenful."""
        step = direction * max(1, self._visible_rows() - 1)
        self.terminal.scroll_to_line(self.terminal.top_line() + step,
                                     follow=False)

    def _handle_key(self, event: QKeyEvent) -> bool:
        """Handle keyboard input and send to SSH channel."""
        modifiers = event.modifiers()
        key = event.key()
        ctrl_pressed = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift_pressed = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        alt_pressed = bool(modifiers & Qt.KeyboardModifier.AltModifier)
        meta_pressed = bool(modifiers & Qt.KeyboardModifier.MetaModifier)

        ctrl_only = ctrl_pressed and not shift_pressed and not alt_pressed and not meta_pressed
        ctrl_shift = ctrl_pressed and shift_pressed and not alt_pressed and not meta_pressed

        # The window's own chords (lock screen, switch tab…) are never typed
        # into the shell, even when no window shortcut claimed them.
        if is_app_chord(event):
            return False

        # Ctrl+C → send interrupt
        if ctrl_only and key == Qt.Key.Key_C:
            if self._terminal_has_selection():
                self._copy_selection()
                return True
            self._send_data("\x03")
            return True

        # Ctrl+D → send EOF
        if ctrl_only and key == Qt.Key.Key_D:
            self._send_data("\x04")
            return True

        # Ctrl+Z → suspend
        if ctrl_only and key == Qt.Key.Key_Z:
            self._send_data("\x1a")
            return True

        # Ctrl+F → search
        if ctrl_only and key == Qt.Key.Key_F:
            if self.search_bar.isVisible():
                self._close_search()
            else:
                self.search_bar.setVisible(True)
                self.search_input.setFocus()
                self.search_input.selectAll()
                if self.search_input.text():
                    self._run_search(move=False)
            return True

        # Ctrl+K → the command palette. This is the one binding that takes
        # something away from the shell, where Ctrl+K is kill-to-end-of-line,
        # so Ctrl+Shift+K still sends a literal ^K: it falls through to the
        # catch-all below, which is why this branch tests ctrl_only.
        if ctrl_only and key == Qt.Key.Key_K:
            self.open_command_palette()
            return True

        # Ctrl+L -> clear terminal viewport and redraw prompt remotely
        if ctrl_only and key == Qt.Key.Key_L:
            self._send_data("\x0c")
            return True

        # Font zoom, handled here so nothing else can claim the shortcut
        if ctrl_only:
            if key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self._increase_font()
                return True
            if key == Qt.Key.Key_Minus:
                self._decrease_font()
                return True

        # Ctrl+V → paste
        if ctrl_only and key == Qt.Key.Key_V:
            clipboard = QApplication.clipboard()
            if clipboard:
                text = clipboard.text()
                if text:
                    self._send_paste(text)
            return True

        # Ctrl+Shift+V -> explicit bracketed paste path
        if ctrl_shift and key == Qt.Key.Key_V:
            clipboard = QApplication.clipboard()
            if clipboard:
                text = clipboard.text()
                if text:
                    self._send_paste(text)
            return True

        # Ctrl+Shift+C -> copy selection
        if ctrl_shift and key == Qt.Key.Key_C:
            self._copy_selection()
            return True

        # Ctrl+Shift+A selects everything; Escape drops a selection, which is
        # how you get the view following live output again after copying.
        if ctrl_shift and key == Qt.Key.Key_A:
            self.terminal.select_all()
            return True

        # Ctrl+Shift+Up/Down walk between prompts, using the marks the
        # remote shell emits. Ctrl+Shift+O copies the last command's output.
        if ctrl_shift and key == Qt.Key.Key_Up:
            return self._jump_to_prompt(-1)
        if ctrl_shift and key == Qt.Key.Key_Down:
            return self._jump_to_prompt(1)
        if ctrl_shift and key == Qt.Key.Key_O:
            return self._copy_last_output()

        # Shift+PageUp/PageDown walk the local scrollback, like every other
        # terminal — they are never sent to the remote.
        if shift_pressed and key in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self._scroll_view_by_page(-1 if key == Qt.Key.Key_PageUp else 1)
            return True

        if ctrl_only and key == Qt.Key.Key_Space:
            self._send_data("\x00")
            return True

        modifier = self._xterm_modifier(
            shift_pressed, alt_pressed, ctrl_pressed, meta_pressed
        )

        # Cursor and edit keys. DECCKM is honoured exactly as the remote set
        # it — full-screen menus and readline both depend on getting the mode
        # they asked for. leave_alt_screen() clears it, so a program that dies
        # without tidying up cannot leave the shell with dead arrow keys.
        if key in self._CURSOR_KEYS:
            letter = self._CURSOR_KEYS[key]
            if modifier > 1:
                self._send_data(f"\x1b[1;{modifier}{letter}")
            elif self._screen_buf.application_cursor_keys:
                self._send_data(f"\x1bO{letter}")
            else:
                self._send_data(f"\x1b[{letter}")
            return True

        if key in self._TILDE_KEYS:
            number = self._TILDE_KEYS[key]
            suffix = f";{modifier}" if modifier > 1 else ""
            self._send_data(f"\x1b[{number}{suffix}~")
            return True

        if key in self._SS3_FUNCTION_KEYS:
            letter = self._SS3_FUNCTION_KEYS[key]
            if modifier > 1:
                self._send_data(f"\x1b[1;{modifier}{letter}")
            else:
                self._send_data(f"\x1bO{letter}")
            return True

        if key == Qt.Key.Key_Backtab or (key == Qt.Key.Key_Tab and shift_pressed):
            self._send_data("\x1b[Z")
            return True

        if key == Qt.Key.Key_Backspace:
            self._send_data("\x08" if ctrl_pressed else "\x7f")
            return True

        special_map = {
            Qt.Key.Key_Return: "\r",
            Qt.Key.Key_Enter: "\r",
            Qt.Key.Key_Tab: "\t",
            Qt.Key.Key_Escape: "\x1b",
        }
        if key in special_map:
            self._send_data(special_map[key])
            return True

        # Catch-all for remaining Ctrl+<letter> combos — send as control
        # character (e.g. Ctrl+A = \x01, Ctrl+E = \x05, Ctrl+U = \x15, etc.)
        if ctrl_pressed and not alt_pressed and not meta_pressed:
            if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
                ctrl_char = chr(key - Qt.Key.Key_A + 1)
                self._send_data(ctrl_char)
                return True

        # Alt+<key> should send ESC prefix for shell compatibility
        if alt_pressed and not ctrl_pressed and not meta_pressed and event.text():
            self._send_data("\x1b" + event.text())
            return True

        # Regular text
        text = event.text()
        if text:
            self._send_data(text)
            return True

        return False

    # ── Search ──────────────────────────────────────────────────────

    def _search_options(self) -> SearchOptions:
        return SearchOptions(
            regex=self.search_regex.isChecked(),
            case_sensitive=self.search_case.isChecked(),
            whole_word=self.search_word.isChecked(),
        )

    def _on_search_text_changed(self, _text: str):
        self._search_timer.start()

    def _run_search(self, move: bool = True):
        """Find every hit and highlight them all, not just the next one."""
        self._search_timer.stop()
        search = self.terminal.search
        query = self.search_input.text()
        remembered = search.current()
        search.run(self._screen_buf, query, self._search_options())
        if remembered is not None and search.matches:
            # Hold the place across a rescan caused by new output.
            search.select_nearest(remembered[0])
        self.search_status.setText(search.describe())
        if move and search.matches:
            self._show_search_match(search.step(1))
        else:
            self.terminal.viewport().update()

    def _search_step(self, direction: int):
        search = self.terminal.search
        if not search.matches or search.query != self.search_input.text():
            self._run_search(move=False)
            search = self.terminal.search
        if not search.matches:
            self.search_status.setText(search.describe())
            return
        self._show_search_match(search.step(direction))

    def _search_next(self):
        self._search_step(1)

    def _search_previous(self):
        self._search_step(-1)

    def _show_search_match(self, match):
        """Bring a hit into view without snapping back to live output."""
        search = self.terminal.search
        self.search_status.setText(search.describe())
        if match is None:
            self.terminal.viewport().update()
            return
        absolute = match[0]
        doc_index = absolute - self.terminal.dropped_lines()
        top = self.terminal.top_line()
        visible = self.terminal.visible_rows()
        if not top <= doc_index < top + visible:
            # Land the hit a third of the way down rather than at the very
            # top, so the lines around it are readable.
            self.terminal.scroll_to_line(
                max(0, doc_index - visible // 3), follow=False)
        self.terminal.viewport().update()

    def _close_search(self):
        self.search_bar.setVisible(False)
        self.terminal.search.run(self._screen_buf, "", self._search_options())
        self.search_status.setText("")
        self.terminal.viewport().update()
        self.terminal.setFocus()

    # ── Session Log ─────────────────────────────────────────────────

    def _populate_log_menu(self, menu: QMenu):
        """What you can do with the recording this session is already making."""
        recording = self.recording_path

        if recording is not None:
            size = session_log.format_size(self._recorder.bytes_written)
            header = menu.addAction(f"Recording — {size}")
            header.setEnabled(False)
        else:
            header = menu.addAction("Not recording")
            header.setEnabled(False)
        menu.addSeparator()

        act_text = menu.addAction(icons.icon("note"), "Save as plain text…")
        act_text.triggered.connect(self._save_log)
        act_copy = menu.addAction(icons.icon("download"), "Save the recording…")
        act_copy.setEnabled(recording is not None)
        act_copy.triggered.connect(self._save_recording)
        act_replay = menu.addAction(icons.icon("play"), "Replay recordings…")
        act_replay.triggered.connect(self.open_replay)
        menu.addSeparator()
        act_folder = menu.addAction(icons.icon("folder"), "Open the recordings folder")
        act_folder.triggered.connect(self._open_recordings_folder)
        act_stop = menu.addAction(icons.icon("stop"), "Stop recording this session")
        act_stop.setEnabled(recording is not None)
        act_stop.triggered.connect(self.stop_recording)
        return menu

    def _show_log_menu(self):
        menu = self._populate_log_menu(QMenu(self))
        menu.exec(self.btn_log.mapToGlobal(QPoint(0, self.btn_log.height())))

    def _save_log(self):
        """Save session log to file."""
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Save Session Log", "", "Text Files (*.txt);;All Files (*)"
        )
        if filepath:
            with open(filepath, "w", encoding="utf-8") as f:
                raw = "".join(self._session_log)
                f.write(self.ansi_parser.strip_ansi(raw))

    def _save_recording(self):
        """Copy the live recording somewhere the user chose, colours intact."""
        source = self.recording_path
        if source is None:
            return
        target, _ = QFileDialog.getSaveFileName(
            self, "Save Session Recording", source.name,
            "asciinema recording (*.cast);;All Files (*)")
        if not target:
            return
        try:
            import shutil
            shutil.copyfile(source, target)
        except OSError as exc:
            QMessageBox.warning(self, "Save Recording",
                                f"Could not save the recording:\n{exc}")

    def _open_recordings_folder(self):
        from PyQt6.QtGui import QDesktopServices
        from PyQt6.QtCore import QUrl
        folder = session_log.SESSIONS_DIR
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def open_replay(self):
        """Open the replay window, starting on this session's own recording."""
        from app.session_replay import SessionReplayDialog
        dialog = SessionReplayDialog(self, initial=self.recording_path,
                                     theme=self.theme)
        dialog.show()

    # ── Latency Ping ────────────────────────────────────────────────

    def _start_latency_ping(self):
        """Periodically update latency display."""
        self._ping_timer = QTimer(self)
        self._ping_timer.timeout.connect(self._update_latency)
        self._ping_timer.start(10000)  # every 10 seconds

    def _update_latency(self):
        if self._is_stream:
            if self._connected:
                self.latency_label.setText("Connected")
            return
        if self._connected and self.client:
            try:
                transport = self.client.get_transport()
                if transport and transport.is_active():
                    self.latency_label.setText("Connected ✓")
                else:
                    self.latency_label.setText("Connection unstable")
            except Exception:
                self.latency_label.setText("Checking...")

    # ── Theme ───────────────────────────────────────────────────────

    def set_theme(self, theme: dict):
        self.theme = theme
        self.active_theme = theme.copy()

        # Apply local style overrides
        local_style = getattr(self, '_local_style', "System Theme")

        bg_css = f"background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {self.active_theme.get('bg_terminal', '#000000')}, stop:1 rgba(10, 14, 24, 0.92));"

        if local_style == "Matrix Green":
            self.active_theme['terminal_fg'] = "#00ff41"
            self.active_theme['bg_terminal'] = "#000802"
            self.active_theme['terminal_cursor'] = "#00ff41"
            bg_css = "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #000802, stop:1 #001a06);"
        elif local_style == "Cyberpunk":
            self.active_theme['terminal_fg'] = "#00ffff"
            self.active_theme['bg_terminal'] = "#0a0514"
            self.active_theme['terminal_cursor'] = "#ff00ff"
            bg_css = "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0a0514, stop:1 #1e052c);"
        elif local_style == "Vaporwave":
            self.active_theme['terminal_fg'] = "#ffffff"
            self.active_theme['bg_terminal'] = "#1f0033"
            self.active_theme['terminal_cursor'] = "#01cdfe"
            bg_css = "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1f0033, stop:1 #0e0018);"
        elif local_style == "Deep Blue":
            self.active_theme['terminal_fg'] = "#a5d8ff"
            self.active_theme['bg_terminal'] = "#000511"
            self.active_theme['terminal_cursor'] = "#a5d8ff"
            bg_css = "background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #000511, stop:1 #00132b);"
        elif local_style == "Solid Black":
            self.active_theme['terminal_fg'] = "#e5e5e5"
            self.active_theme['bg_terminal'] = "#000000"
            self.active_theme['terminal_cursor'] = "#ffffff"
            bg_css = "background: #000000;"

        self.ansi_parser.theme = self.active_theme
        self.ansi_parser._update_palette()

        # Colours are the painter's business now; the stylesheet is left to
        # style the chrome around it (the scrollbars) and nothing else.
        self.terminal.set_colors(
            self.active_theme.get("terminal_fg", "#e2e8f0"),
            self.active_theme.get("bg_terminal", "#0a0e18"),
            cursor=self.active_theme.get("terminal_cursor",
                                         self.active_theme.get("terminal_fg", "#e2e8f0")),
            selection=self.active_theme.get("accent", "#6366f1"),
        )
        self.terminal.setStyleSheet(f"""
            TerminalView {{
                border: none;
                {bg_css}
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 10px;
                margin: 2px;
            }}
            QScrollBar:horizontal {{
                background: transparent;
                height: 10px;
                margin: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: {self.active_theme.get('scrollbar_handle', '#334155')};
                min-height: 24px;
                border-radius: 5px;
            }}
            QScrollBar::handle:horizontal {{
                background: {self.active_theme.get('scrollbar_handle', '#334155')};
                min-width: 24px;
                border-radius: 5px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {self.active_theme.get('accent', '#6366f1')};
            }}
            QScrollBar::handle:horizontal:hover {{
                background: {self.active_theme.get('accent', '#6366f1')};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
                background: transparent;
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0;
                background: transparent;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
                background: transparent;
            }}
        """)

        # Update floating frame
        self.terminal_frame.setStyleSheet(f"""
            #terminal_frame {{
                background-color: {self.active_theme['bg_terminal']};
                border: 1px solid rgba(255, 255, 255, 0.09);
                border-radius: 12px;
                margin: 10px;
            }}
        """)

        # Update header
        accent = self.active_theme.get('accent', '#6366f1')
        # Use safer rgba for borders
        border_color = f"rgba(124, 111, 247, 0.4)" # Fallback
        if accent.startswith('#'):
            try:
                r = int(accent[1:3], 16)
                g = int(accent[3:5], 16)
                b = int(accent[5:7], 16)
                border_color = f"rgba({r}, {g}, {b}, 0.4)"
            except: pass

        # Same ink the theme's buttons use, so the badge and the primary
        # button read on the accent whatever shade it is.
        accent_ink = button_text_color(theme)
        self.header.setStyleSheet(f"""
            QWidget#terminal_header {{
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 {theme.get('glass_bg', 'rgba(30, 31, 54, 0.85)')},
                    stop:1 rgba(16, 20, 32, 0.92)
                );
                border-bottom: 1px solid {border_color};
                border-top-left-radius: 12px;
                border-top-right-radius: 12px;
            }}
            QLabel {{ color: {theme['text_primary']}; }}
            QLabel#shell_badge {{
                color: {accent_ink};
                font-size: 10px;
                font-weight: 800;
                padding: 0 8px;
                border-radius: 11px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {theme.get('accent', '#6366f1')}, stop:1 {theme.get('accent_hover', '#818cf8')});
                border: 1px solid rgba(255,255,255,0.25);
            }}
            QPushButton {{
                color: #f8fafc;
                border-radius: 8px;
                border: 1px solid rgba(255,255,255,0.14);
                padding: 0 12px;
                font-weight: 700;
                letter-spacing: 0.2px;
                background: rgba(30, 41, 59, 0.55);
            }}
            QPushButton:hover {{
                border-color: rgba(255,255,255,0.35);
                background: rgba(51, 65, 85, 0.8);
            }}
            QPushButton#btn_primary {{
                color: {accent_ink};
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {theme.get('accent', '#6366f1')}, stop:1 {theme.get('accent_hover', '#818cf8')});
            }}
            /* Fixed faces keep the light text; their bright ends are held
               to shades it still reads on. */
            QPushButton#btn_success {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #0f766e, stop:1 #0d9488);
            }}
            QPushButton#btn_magic {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6d28d9, stop:1 #7c3aed);
            }}
            QPushButton#btn_danger {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #b91c1c, stop:1 #dc2626);
                border-color: rgba(255,255,255,0.22);
            }}
            QPushButton#btn_danger:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #dc2626, stop:1 #f87171);
            }}
            QPushButton#btn_font, QPushButton[iconOnly="true"] {{
                padding: 0;
            }}
            QPushButton#btn_font {{
                background: rgba(15, 23, 42, 0.75);
            }}
            QToolButton {{
                color: #f8fafc;
                border-radius: 8px;
                border: 1px solid rgba(255,255,255,0.14);
                background: rgba(30, 41, 59, 0.55);
                padding: 0;
            }}
            QToolButton:hover {{
                border-color: {accent};
                background: rgba(51, 65, 85, 0.8);
            }}
            QToolButton::menu-indicator {{
                image: none;
                width: 0px;
            }}
        """)

        if hasattr(self, 'health_bar'):
            self.health_bar.set_theme(theme)

        # Cached character formats carry the old palette — repaint with the
        # new one so text already on screen changes colour too.
        if getattr(self, "_screen_buf", None) is not None:
            self._force_full_render()

    def _stop_all_timers(self):
        """Stop every QTimer owned by this widget. Safe to call multiple times."""
        for attr in ("_render_timer", "_resize_timer", "_blink_timer",
                     "resync_timer", "_ping_timer",
                     "_reconnect_timer", "_status_flash_timer",
                     "_render_fallback_timer"):
            timer = getattr(self, attr, None)
            if timer is None:
                continue
            try:
                timer.stop()
            except Exception:
                pass

    def cleanup(self):
        """Clean up resources before closing."""
        self._cancel_auto_reconnect()
        self._stop_port_forwards()
        self._stop_all_timers()
        self.disconnect()
        self._close_recording()

    def suspend(self):
        """Prepare for system sleep: stop timers and disconnect cleanly."""
        self._stop_all_timers()
        self._cancel_auto_reconnect()
        self._stop_port_forwards()
        if self.monitor:
            try:
                self.monitor.stop()
            except Exception:
                pass
        self._connected = False
        if self.channel:
            try:
                self.channel.close()
            except Exception:
                pass
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        try:
            self._set_status_light("#fbbf24")
            self.latency_label.setText("Suspended")
            self.health_bar.set_connection_state("suspended")
        except Exception:
            pass

    def resume(self):
        """Reconnect after system wake."""
        self.reconnect()


class _TerminalWindow(QWidget):
    dock_requested = pyqtSignal()

    def __init__(self, pane):
        super().__init__(flags=Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        name = pane.connection_info.get("name") or pane.connection_info.get("host") or "SSH"
        self.setWindowTitle(f"{name} — SSH")
        self.pane = pane

        self.placeholder = QWidget()
        self.placeholder.popped_out_terminal = pane
        layout = QVBoxLayout(self.placeholder)
        show_window = QPushButton("Show SSH window")
        show_window.setToolTip(f"Show SSH window for {name}")
        show_window.clicked.connect(self.bring_forward)
        layout.addWidget(show_window, alignment=Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

    def bring_forward(self):
        if self.isMinimized():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()
        self.pane.terminal.setFocus()

    def closeEvent(self, event):
        self.dock_requested.emit()
        event.accept()


class TerminalWorkspace(QWidget):
    """Container for one or more split SSH terminals."""
    session_closed = pyqtSignal()
    style_changed = pyqtSignal(str)
    font_changed = pyqtSignal(str, int)
    #: (pane, connection) — a pane's failure panel asked to edit its connection.
    edit_connection_requested = pyqtSignal(object, object)

    def __init__(self, connection_info, theme=None, terminal_style: str | None = None,
                 terminal_font: str | None = None, font_size: int | None = None,
                 production_tenants=(), snippet_provider=None, parent=None):
        super().__init__(parent)
        self.connection_info = connection_info
        self.theme = theme or DARK_THEME
        self.terminal_style = (
            terminal_style
            if terminal_style in SSHTerminal.TERMINAL_STYLES
            else "System Theme"
        )
        self.terminal_font = SSHTerminal.normalized_font(terminal_font)
        self.font_size = SSHTerminal.normalized_font_size(
            SSHTerminal.DEFAULT_FONT_SIZE if font_size is None else font_size)
        self.production_tenants = guardrails.normalize_production_tenants(
            production_tenants)
        self.snippet_provider = snippet_provider
        self.ssh_client = None
        self._terminal_windows: dict[SSHTerminal, _TerminalWindow] = {}
        #: Every pane in this tab can be made to type in unison.
        self.broadcast = BroadcastGroup(self)
        self._setup_ui()

    def _setup_ui(self):
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)

        self.broadcast_bar = BroadcastBar(self.broadcast, self)
        self.broadcast_bar.send_requested.connect(self.broadcast_command)
        self.broadcast_bar.setVisible(False)
        self.layout.addWidget(self.broadcast_bar)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.layout.addWidget(self.splitter)

        self.broadcast.changed.connect(self._refresh_broadcast_bar)

        # Add initial terminal
        self.add_terminal()

    # ── The panes ───────────────────────────────────────────────────

    def terminals(self, container=None) -> list:
        """Every pane, in visual order, however deeply the splits are nested.

        A broadcast grid is splitters inside splitters, so nothing may assume
        the panes are the direct children of one of them.
        """
        container = self.splitter if container is None else container
        found = []
        for index in range(container.count()):
            child = container.widget(index)
            if isinstance(child, QSplitter):
                found.extend(self.terminals(child))
            elif isinstance(child, SSHTerminal):
                found.append(child)
            elif hasattr(child, "popped_out_terminal"):
                found.append(child.popped_out_terminal)
        return found

    def _toggle_terminal_window(self, pane):
        if pane in self._terminal_windows:
            self._dock_terminal(pane)
            return

        splitter = pane.parentWidget()
        window = _TerminalWindow(pane)
        window.setWindowIcon(self.window().windowIcon())
        area = available_geometry(self)
        window.resize(pane.size().expandedTo(QSize(800, 500)).boundedTo(area.size()))
        window.move(area.center() - window.rect().center())

        self._terminal_windows[pane] = window
        self.destroyed.connect(window.deleteLater)
        splitter.replaceWidget(splitter.indexOf(pane), window.placeholder)
        window.layout().addWidget(pane)
        pane.btn_pop_out.setIcon(icons.icon("undo"))
        pane.btn_pop_out.setToolTip("Return SSH shell to tab")
        pane.btn_pop_out.setAccessibleName("Return SSH shell to tab")
        window.dock_requested.connect(lambda: self._dock_terminal(pane))
        window.bring_forward()

    def _dock_terminal(self, pane):
        window = self._terminal_windows.pop(pane)
        placeholder = window.placeholder
        splitter = placeholder.parentWidget()
        window.hide()
        splitter.replaceWidget(splitter.indexOf(placeholder), pane)
        placeholder.deleteLater()
        window.deleteLater()
        pane.btn_pop_out.setIcon(icons.icon("external"))
        pane.btn_pop_out.setToolTip("Pop out SSH shell")
        pane.btn_pop_out.setAccessibleName("Pop out SSH shell")
        if pane.isVisible():
            pane.terminal.setFocus()

    def set_terminal_windows_visible(self, visible: bool):
        for window in self._terminal_windows.values():
            window.setVisible(visible)

    def _new_terminal(self, connection_info=None, ssh_client=None):
        """Build a pane and wire it up, without deciding where it goes."""
        info = connection_info if connection_info is not None else self.connection_info
        terminal = SSHTerminal(
            info,
            theme=self.theme,
            ssh_client=ssh_client,
            terminal_style=self.terminal_style,
            terminal_font=self.terminal_font,
            font_size=self.font_size,
            production_tenants=self.production_tenants,
            parent=self,
        )
        terminal.snippet_provider = self.snippet_provider
        terminal.split_requested.connect(
            lambda vertical, src=terminal: self.add_terminal(vertical, src))
        if str(info.get("protocol", "ssh") or "ssh").lower() == "ssh":
            terminal.btn_pop_out.clicked.connect(
                lambda _checked=False: self._toggle_terminal_window(terminal))
            terminal.btn_pop_out.show()
        terminal.connection_established.connect(self._on_connection_established)
        terminal.session_closed.connect(self._on_session_closed)
        terminal.style_changed.connect(self._on_style_changed)
        terminal.font_changed.connect(self._on_font_changed)
        terminal.edit_requested.connect(
            lambda conn, pane=terminal: self.edit_connection_requested.emit(pane, conn))
        self.broadcast.add(terminal)
        terminal.set_broadcast_group(self.broadcast)
        return terminal

    def add_terminal(self, vertical=True, source_terminal=None,
                     connection_info=None, connect=True):
        # Update orientation based on request
        self.splitter.setOrientation(Qt.Orientation.Vertical if not vertical else Qt.Orientation.Horizontal)

        source_client = None
        if source_terminal and getattr(source_terminal, "client", None):
            source_client = source_terminal.client

        # A pane on a different machine needs its own connection; only panes
        # on the same host can ride the session that is already open.
        info = connection_info if connection_info is not None else self.connection_info
        shared = self.ssh_client if info is self.connection_info else None
        if str(info.get("protocol", "ssh") or "ssh").lower() != "ssh":
            # A telnet socket or a COM port cannot be read by two panes.
            source_client = shared = None
        terminal = self._new_terminal(info, source_client or shared)

        self.splitter.addWidget(terminal)
        self._refresh_broadcast_bar()
        if connect:
            terminal.connect_ssh()
            QTimer.singleShot(0, terminal.terminal.setFocus)
        return terminal

    def open_hosts(self, connections, connect=True) -> list:
        """Open one pane per machine, arranged as a grid.

        This is the broadcast entry point: eight machines picked in the
        estate become eight panes you can type into at once, which is a very
        different thing from eight tabs you have to visit in turn.
        """
        rows = [conn for conn in (connections or []) if isinstance(conn, dict)]
        if not rows:
            return []

        existing = self.terminals()
        panes = list(existing)
        for conn in rows:
            terminal = self._new_terminal(conn)
            panes.append(terminal)
        self._arrange_grid(panes)
        if connect:
            for terminal in panes[len(existing):]:
                terminal.connect_ssh()
            if panes:
                QTimer.singleShot(0, panes[0].terminal.setFocus)
        self._refresh_broadcast_bar()
        return panes[len(existing):]

    def _arrange_grid(self, panes):
        """Lay *panes* out as rows of a grid inside the main splitter."""
        panes = [self._terminal_windows[pane].placeholder
                 if pane in self._terminal_windows else pane
                 for pane in panes if pane is not None]
        self.splitter.setOrientation(Qt.Orientation.Vertical)

        # Take everything out first: re-parenting a widget that is still in a
        # splitter leaves the old splitter holding an empty slot.
        for pane in panes:
            pane.setParent(None)
        while self.splitter.count():
            stale = self.splitter.widget(0)
            stale.setParent(None)
            stale.deleteLater()

        index = 0
        for width in distribute(len(panes)):
            row = QSplitter(Qt.Orientation.Horizontal)
            for pane in panes[index:index + width]:
                pane.setParent(row)
                row.addWidget(pane)
            index += width
            self.splitter.addWidget(row)

    def _refresh_broadcast_bar(self):
        bar = getattr(self, "broadcast_bar", None)
        if bar is None:
            return
        # One pane cannot broadcast to anything, so the bar would only be a
        # promise the session cannot keep.
        bar.setVisible(len(self.terminals()) > 1)
        bar.refresh()

    def broadcast_command(self, command: str):
        """Run one command on every listening pane, once it has been checked."""
        command = str(command or "").strip()
        if not command or not self.broadcast.armed:
            return
        targets = self.broadcast.targets()
        if not targets:
            return

        confirmation = guardrails.confirmation_for(
            command, self.broadcast.connections(), self.production_tenants)
        if confirmation is not None:
            answer = QMessageBox.question(
                self, confirmation.title, confirmation.body,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        sent = self.broadcast.send(command + "\r")
        self.broadcast_bar.clear_input()
        self.broadcast_bar.report(len(sent))

    def set_production_tenants(self, tenants):
        self.production_tenants = guardrails.normalize_production_tenants(tenants)
        for terminal in self.terminals():
            terminal.set_production_tenants(self.production_tenants)

    def _on_style_changed(self, style_name: str):
        if style_name not in SSHTerminal.TERMINAL_STYLES:
            return
        self.terminal_style = style_name
        sender = self.sender()
        for terminal in self.terminals():
            if terminal is not sender:
                terminal.set_terminal_style(style_name)
        self.style_changed.emit(style_name)

    def _on_font_changed(self, font_name: str, size: int):
        """Remember the last choice for the next pane and the next session.

        Panes are deliberately left alone: someone who has made one split
        larger to read a log did that on purpose, so only new panes start
        from here.
        """
        self.terminal_font = SSHTerminal.normalized_font(font_name)
        self.font_size = SSHTerminal.normalized_font_size(size)
        self.font_changed.emit(self.terminal_font, self.font_size)

    def _on_connection_established(self):
        sender = self.sender()
        if not self.ssh_client and not getattr(sender, "_is_stream", False):
            self.ssh_client = sender.client

    def _on_session_closed(self):
        panes = self.terminals()
        if len(panes) <= 1:
            self.session_closed.emit()
        else:
            sender = self.sender()
            if sender in self._terminal_windows:
                self._dock_terminal(sender)
            self.broadcast.discard(sender)
            sender.setParent(None)
            sender.deleteLater()
            self._refresh_broadcast_bar()

    def focused_terminal(self):
        """The pane the user is typing in, or the first one."""
        focused = self.focusWidget()
        panes = self.terminals()
        for terminal in panes:
            window = self._terminal_windows.get(terminal)
            if (terminal is focused or terminal.isAncestorOf(focused)
                    or (window is not None and window.isActiveWindow())
                    or (window is not None and window.placeholder.isAncestorOf(focused))):
                return terminal
        return panes[0] if panes else None

    def _send_data(self, data):
        """Send data to the focused terminal."""
        terminal = self.focused_terminal()
        if terminal is not None:
            terminal._send_data(data)

    def run_command(self, command: str) -> bool:
        """Run a whole command: everywhere when armed, here when not."""
        if self.broadcast.armed and self.broadcast.targets():
            self.broadcast_command(command)
            return True
        terminal = self.focused_terminal()
        return bool(terminal is not None and terminal.run_command(command))

    def open_command_palette(self):
        terminal = self.focused_terminal()
        if terminal is not None:
            terminal.open_command_palette()

    def set_snippet_provider(self, provider):
        self.snippet_provider = provider
        for terminal in self.terminals():
            terminal.snippet_provider = provider

    def set_theme(self, theme):
        self.theme = theme
        for terminal in self.terminals():
            terminal.set_theme(theme)

    def cleanup(self):
        for pane in list(self._terminal_windows):
            self._dock_terminal(pane)
        for terminal in self.terminals():
            terminal.cleanup()

    def suspend(self):
        for terminal in self.terminals():
            terminal.suspend()

    def resume(self):
        for terminal in self.terminals():
            terminal.resume()

# socket imported at module top (used for socket.timeout in _read_output)


def format_bytes(value):
    """1536 -> '1.5 KB'.  Uses binary units, like free and df -h."""
    value = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if value < 1024 or unit == "PB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024


def format_rate(value):
    """Bytes per second, compact enough for a status chip: '1.2M/s'."""
    value = float(value)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024 or unit == "T":
            if unit == "B":
                return f"{int(value)}B/s"
            return f"{value:.1f}{unit}/s" if value < 10 else f"{value:.0f}{unit}/s"
        value /= 1024


def format_uptime(seconds):
    """Seconds -> '12d 4h', '4h 12m' or '12m'."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


class HealthBar(QFrame):
    """Bottom status bar showing server health metrics."""

    #: Chips that give way, lowest priority first, when the bar is too narrow
    #: for everything.  CPU, RAM and DISK always stay.
    OPTIONAL_CHIPS = ("users", "net", "uptime", "load")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(30)
        self.setObjectName("health_bar")
        self._chips = {}
        self._setup_ui()
        # An explicit minimum stops the row of chips from dictating how
        # narrow the whole terminal window may become; _fit_chips drops
        # chips instead when the bar is squeezed.
        self.setMinimumWidth(200)

    def set_theme(self, theme):
        grad = "rgba(0,0,0,0.3)" # default fallback
        if "bg_gradient" in theme:
            grad = theme["bg_gradient"].replace("stop:0.5 ", "stop:0.2 ").replace("stop:1 ", "stop:0.8 ")

        self.setStyleSheet(f"""
            #health_bar {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {theme['bg_secondary']}, stop:1 {theme['bg_tertiary']});
                border-top: 1px solid {theme['border']};
            }}
            QLabel {{ font-size: 11px; font-weight: 700; color: {theme['text_secondary']}; }}
            .value {{
                color: {theme['text_primary']};
                margin-right: 15px;
                background: rgba(15, 23, 42, 0.6);
                padding: 2px 8px;
                border-radius: 10px;
                border: 1px solid rgba(255,255,255,0.12);
            }}
        """)

    def _add_chip(self, layout, key, caption, placeholder, tooltip):
        label = QLabel(caption)
        value = QLabel(placeholder)
        value.setProperty("class", "value")
        for w in (label, value):
            w.setToolTip(tooltip)
            layout.addWidget(w)
        self._chips[key] = (label, value)
        return value

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(15, 0, 15, 0)
        layout.setSpacing(10)

        self.cpu_val = self._add_chip(layout, "cpu", "CPU:", "— %",
            "Processor busy time since the last sample")
        self.ram_val = self._add_chip(layout, "ram", "RAM:", "— %",
            "Memory in use (excluding reclaimable cache)")
        self.disk_val = self._add_chip(layout, "disk", "DISK:", "— %",
            "Root filesystem usage")
        self.load_val = self._add_chip(layout, "load", "LOAD:", "—",
            "1-minute load average")
        self.uptime_val = self._add_chip(layout, "uptime", "UP:", "—",
            "Time since the server last booted")
        self.net_val = self._add_chip(layout, "net", "NET:", "—",
            "Network throughput, all interfaces except loopback")
        self.users_val = self._add_chip(layout, "users", "USERS:", "—",
            "Logged-in sessions (who)")

        layout.addStretch()

        self.status_tag = QLabel()
        layout.addWidget(self.status_tag)
        self._state = ""
        self.set_connection_state("connecting")

    #: Connection states the tag can show before (or instead of) health.
    #: (text, foreground, background, border)
    _STATE_TAGS = {
        "connecting": ("CONNECTING", "#bfdbfe", "rgba(30, 58, 138, 0.35)", "#3b82f6"),
        "connected": ("CONNECTED", "#bbf7d0", "rgba(20, 83, 45, 0.35)", "#22c55e"),
        "reconnecting": ("RECONNECTING", "#fef3c7", "rgba(120, 53, 15, 0.4)", "#f59e0b"),
        "suspended": ("SUSPENDED", "#fef3c7", "rgba(120, 53, 15, 0.4)", "#f59e0b"),
        "offline": ("OFFLINE", "#fecaca", "rgba(127, 29, 29, 0.35)", "#ef4444"),
    }

    @staticmethod
    def _tag_style(fg, bg, border):
        return (f"color: {fg}; background: {bg}; font-size: 10px; font-weight: 700;"
                f" padding: 2px 8px; border: 1px solid {border}; border-radius: 4px;")

    @property
    def connection_state(self) -> str:
        return self._state

    def set_connection_state(self, state: str):
        """Say what the session is doing, so the tag never claims CONNECTED
        for a session that is still dialling or has already dropped."""
        if state not in self._STATE_TAGS:
            return
        self._state = state
        text, fg, bg, border = self._STATE_TAGS[state]
        self.status_tag.setText(text)
        self.status_tag.setStyleSheet(self._tag_style(fg, bg, border))
        if state in ("offline", "suspended", "connecting"):
            # Numbers from a session that is gone are not health, they are
            # history; blank them rather than leave them looking current.
            for key, (_label, value) in self._chips.items():
                value.setText("— %" if key in ("cpu", "ram", "disk") else "—")
                value.setStyleSheet("")

    def _set_tooltip(self, key, text):
        for w in self._chips[key]:
            w.setToolTip(text)

    def update_stats(self, stats):
        if self._state in ("offline", "suspended"):
            # A sample that was already in flight when the session dropped.
            return
        self._state = "connected"
        cpu = stats.get('cpu', 0.0)
        ram = stats.get('ram', 0.0)
        disk = stats.get('disk', 0.0)

        self.cpu_val.setText(f"{cpu:.1f}%")
        self.ram_val.setText(f"{ram:.1f}%")
        self.disk_val.setText(f"{disk if disk > 0 else 0:.0f}%")

        self.cpu_val.setStyleSheet(f"color: {self._metric_color(cpu)};")
        self.ram_val.setStyleSheet(f"color: {self._metric_color(ram)};")
        self.disk_val.setStyleSheet(f"color: {self._metric_color(disk)};")

        if "cores" in stats:
            self._set_tooltip("cpu",
                f"Processor busy time since the last sample\n{_plural(stats['cores'], 'core')}")
        if "ram_total" in stats:
            tip = (f"{format_bytes(stats['ram_used'])} of {format_bytes(stats['ram_total'])} "
                   "in use (excluding reclaimable cache)")
            if stats.get("swap_total"):
                tip += (f"\nSwap: {format_bytes(stats['swap_used'])} of "
                        f"{format_bytes(stats['swap_total'])} ({stats['swap']:.0f}%)")
            self._set_tooltip("ram", tip)
        if "disk_total" in stats:
            self._set_tooltip("disk",
                f"Root filesystem: {format_bytes(stats['disk_used'])} of "
                f"{format_bytes(stats['disk_total'])} used")

        # Load average, judged against the core count: 1.0 per core is
        # fully busy, so a load of 4 on a 16-core box is nothing to flag.
        if "load1" in stats:
            self.load_val.setText(f"{stats['load1']:.2f}")
            cores = stats.get("cores") or 1
            self.load_val.setStyleSheet(
                f"color: {self._metric_color(100.0 * stats['load1'] / cores)};")
            tip = (f"Load average: {stats['load1']:.2f} (1m)  "
                   f"{stats['load5']:.2f} (5m)  {stats['load15']:.2f} (15m)")
            if "cores" in stats:
                tip += f"\n{_plural(stats['cores'], 'core')}, so {stats['cores']:.2f} means fully busy"
            if "procs_total" in stats:
                tip += f"\n{stats['procs_running']} of {stats['procs_total']} processes running"
            self._set_tooltip("load", tip)

        if "uptime" in stats:
            self.uptime_val.setText(format_uptime(stats["uptime"]))
            days, rem = divmod(int(stats["uptime"]), 86400)
            hours, rem = divmod(rem, 3600)
            self._set_tooltip("uptime",
                f"Up {_plural(days, 'day')}, {_plural(hours, 'hour')}, {_plural(rem // 60, 'minute')}")

        if "net_rx" in stats:
            self.net_val.setText(
                f"↓{format_rate(stats['net_rx'])} ↑{format_rate(stats['net_tx'])}")
            self._set_tooltip("net",
                f"Receiving {format_bytes(stats['net_rx'])}/s, sending {format_bytes(stats['net_tx'])}/s"
                "\nAll interfaces except loopback, averaged since the last sample")

        if "users" in stats:
            self.users_val.setText(str(stats["users"]))

        if cpu > 90 or ram > 95:
            self.status_tag.setText("HOT")
            self.status_tag.setStyleSheet("color: #fecaca; background: rgba(127, 29, 29, 0.45); font-size: 10px; padding: 2px 8px; border: 1px solid #ef4444; border-radius: 4px;")
        elif cpu > 75 or ram > 85:
            self.status_tag.setText("ELEVATED")
            self.status_tag.setStyleSheet("color: #fef3c7; background: rgba(120, 53, 15, 0.4); font-size: 10px; padding: 2px 8px; border: 1px solid #f59e0b; border-radius: 4px;")
        else:
            self.status_tag.setText("OPTIMAL")
            self.status_tag.setStyleSheet("color: #bbf7d0; background: rgba(20, 83, 45, 0.35); font-size: 10px; padding: 2px 8px; border: 1px solid #22c55e; border-radius: 4px;")

        self._fit_chips()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_chips()

    def _fit_chips(self):
        """Hide the least important chips when the bar cannot fit them all,
        so a narrow terminal keeps CPU/RAM/DISK instead of clipping text."""
        for key in self.OPTIONAL_CHIPS:
            for w in self._chips[key]:
                w.setVisible(True)
        layout = self.layout()
        for key in self.OPTIONAL_CHIPS:
            layout.invalidate()
            if layout.sizeHint().width() <= self.width():
                break
            for w in self._chips[key]:
                w.setVisible(False)

    def _metric_color(self, value):
        if value >= 90:
            return "#f87171"
        if value >= 75:
            return "#fbbf24"
        return "#e2e8f0"
