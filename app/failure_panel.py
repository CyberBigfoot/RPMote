"""
The card a session shows when it cannot connect: what happened, in words, and
the next things worth trying — Retry, Wake & retry, Check reachability, Edit.

It replaces a red line in the scrollback that said ``[Errno 11001]
getaddrinfo failed`` and left the rest to the reader.
"""

from __future__ import annotations

import threading

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QPalette
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from app import icons, net_errors
from app.widgets import set_variant


class FailurePanel(QWidget):
    """A dismissible card; hidden until :meth:`show_failure`."""

    retry_requested = pyqtSignal()
    edit_requested = pyqtSignal()
    #: Results from worker threads, run on the GUI thread.
    _call = pyqtSignal(object)

    def __init__(self, parent=None, margins=(11, 10, 11, 0)):
        super().__init__(parent)
        self._conn: dict = {}
        # Deliberately no handler on our own ``destroyed``: PyQt calling into
        # a Python subclass while Qt is half-way through deleting it crashes
        # the process. Workers ask sip whether the panel is gone instead.
        self._call.connect(lambda fn: fn(), Qt.ConnectionType.QueuedConnection)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(*margins)
        self.card = QFrame()
        self.card.setObjectName("failure_card")
        outer.addWidget(self.card)

        row = QHBoxLayout(self.card)
        row.setContentsMargins(14, 12, 10, 12)
        row.setSpacing(12)
        self.icon = QLabel()
        self.icon.setFixedSize(24, 24)
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)

        column = QVBoxLayout()
        column.setSpacing(6)
        self.title = QLabel()
        self.title.setObjectName("failure_title")
        self.title.setWordWrap(True)
        column.addWidget(self.title)
        self.detail = QLabel()
        self.detail.setObjectName("failure_detail")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        column.addWidget(self.detail)
        self.result = QLabel()
        self.result.setObjectName("failure_result")
        self.result.setWordWrap(True)
        self.result.hide()
        column.addWidget(self.result)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.btn_retry = QPushButton("Retry")
        self.btn_retry.setIcon(icons.icon("refresh"))
        self.btn_retry.clicked.connect(self._retry)
        self.btn_wake = QPushButton("Wake && retry")
        self.btn_wake.setIcon(icons.icon("power"))
        self.btn_wake.setToolTip("Send a Wake-on-LAN magic packet, wait for the machine "
                                 "to answer, then connect")
        self.btn_wake.clicked.connect(self._wake)
        self.btn_check = QPushButton("Check reachability")
        self.btn_check.setIcon(icons.icon("activity"))
        self.btn_check.setToolTip("Try the port, then ping, and say which answered")
        self.btn_check.clicked.connect(self._check)
        self.btn_edit = QPushButton("Edit connection…")
        self.btn_edit.setIcon(icons.icon("edit"))
        self.btn_edit.setToolTip("Fix the saved host, port or login, then try again")
        self.btn_edit.clicked.connect(self.edit_requested.emit)
        for button in (self.btn_retry, self.btn_wake, self.btn_check, self.btn_edit):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            buttons.addWidget(button)
        buttons.addStretch(1)
        column.addLayout(buttons)
        row.addLayout(column, 1)

        self.btn_close = QToolButton()
        self.btn_close.setAutoRaise(True)
        self.btn_close.setIcon(icons.icon("x"))
        self.btn_close.setIconSize(QSize(14, 14))
        self.btn_close.setToolTip("Hide")
        self.btn_close.clicked.connect(self.hide)
        row.addWidget(self.btn_close, 0, Qt.AlignmentFlag.AlignTop)
        self.hide()

    # ── showing ───────────────────────────────────────────────────

    def _apply_colors(self):
        """Alert colours for the theme, buttons included.

        The card styles its own buttons because the session around it often
        has a ``QPushButton`` rule of its own (the terminal does), and a
        container's style sheet beats the application's button roles.
        """
        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        if dark:
            bg, border, title, text = ("rgba(127, 29, 29, 0.38)", "rgba(248, 113, 113, 0.55)",
                                       "#fecaca", "#fee2e2")
            button_bg, button_hover, button_border = (
                "rgba(255, 255, 255, 0.08)", "rgba(255, 255, 255, 0.16)", "rgba(255, 255, 255, 0.24)")
            primary_bg, primary_hover, primary_text = "#fee2e2", "#ffffff", "#7f1d1d"
        else:
            bg, border, title, text = "#fef2f2", "#fca5a5", "#991b1b", "#450a0a"
            button_bg, button_hover, button_border = "#ffffff", "#fee2e2", "#fca5a5"
            primary_bg, primary_hover, primary_text = "#b91c1c", "#dc2626", "#ffffff"
        self.card.setStyleSheet(
            f"#failure_card {{ background: {bg}; border: 1px solid {border};"
            f" border-radius: 10px; }}"
            f"#failure_card QLabel {{ background: transparent; border: none; }}"
            f"#failure_title {{ color: {title}; font-size: 14px; font-weight: 700; }}"
            f"#failure_detail {{ color: {text}; font-size: 12px; }}"
            f"#failure_card QPushButton {{ background: {button_bg}; color: {text};"
            f" border: 1px solid {button_border}; border-radius: 8px; padding: 5px 14px;"
            f" font-weight: 600; }}"
            f"#failure_card QPushButton:hover {{ background: {button_hover}; }}"
            f"#failure_card QPushButton:disabled {{ color: {button_border}; }}"
            f"#failure_card QPushButton[variant=\"primary\"] {{ background: {primary_bg};"
            f" color: {primary_text}; border-color: {primary_bg}; }}"
            f"#failure_card QPushButton[variant=\"primary\"]:hover {{ background: {primary_hover}; }}")
        self.icon.setPixmap(icons.pixmap("warning", 22, title, self.devicePixelRatioF()))
        self._colors = {"text": text, "primary_text": primary_text}

    def _set_button_role(self, button: QPushButton, icon_name: str, primary: bool):
        set_variant(button, "primary" if primary else "secondary")
        colors = getattr(self, "_colors", {})
        color = colors.get("primary_text" if primary else "text")
        button.setIcon(icons.icon(icon_name, color))

    def show_failure(self, conn: dict, kind: str, detail: str, title: str | None = None):
        """Show what went wrong with *conn* and the actions that fit *kind*."""
        from app.reachability import is_probeable
        from app.wake_on_lan import is_wakeable

        self._conn = dict(conn or {})
        name = self._conn.get("name") or self._conn.get("host") or "the server"
        self._apply_colors()
        self.title.setText(title or net_errors.failure_title(kind, str(name)))
        self.detail.setText(detail)
        self.result.hide()
        self.btn_wake.setEnabled(True)
        self.btn_check.setEnabled(True)
        self.btn_wake.setVisible(is_wakeable(self._conn) and kind in net_errors.WAKEABLE)
        self.btn_check.setVisible(is_probeable(self._conn)
                                  and kind not in (net_errors.AUTH, net_errors.HOST_KEY))
        editable = bool(self._conn.get("id"))
        self.btn_edit.setVisible(editable)
        # The button that fixes it is the one to look at: a wrong login is
        # fixed by editing, everything else by trying again.
        fix_by_editing = editable and kind in (net_errors.AUTH, net_errors.DNS)
        self._set_button_role(self.btn_edit, "edit", fix_by_editing)
        self._set_button_role(self.btn_retry, "refresh", not fix_by_editing)
        self._set_button_role(self.btn_wake, "power", False)
        self._set_button_role(self.btn_check, "activity", False)
        self.show()

    def _set_result(self, text: str, color: str):
        self.result.setText(text)
        self.result.setStyleSheet(f"color: {color}; font-size: 12px;")
        self.result.show()

    def _gone(self) -> bool:
        """Has Qt deleted this panel (its tab closed) since a worker started?"""
        try:
            from PyQt6 import sip
            return sip.isdeleted(self)
        except Exception:
            return True

    def _post(self, fn):
        """Hand *fn* to the GUI thread, unless the panel has gone."""
        if self._gone():
            return
        try:
            self._call.emit(fn)
        except RuntimeError:
            pass

    # ── actions ───────────────────────────────────────────────────

    def _retry(self):
        self.hide()
        self.retry_requested.emit()

    def _check(self):
        self.btn_check.setEnabled(False)
        self._set_result("Checking…", "#fbbf24")
        conn = dict(self._conn)

        def work():
            from app import reachability
            info = reachability.probe_connection(conn, timeout=3.0)
            self._post(lambda: self._show_check(info))
        threading.Thread(target=work, name="rpmote-failure-check", daemon=True).start()

    def _show_check(self, info: dict):
        from app import reachability
        self.btn_check.setEnabled(True)
        state = info.get("state")
        port = info.get("port")
        detail = str(info.get("detail", "") or "")
        latency = info.get("latency_ms")
        if state == reachability.STATE_UP:
            ms = f" ({latency:.0f} ms)" if isinstance(latency, (int, float)) else ""
            self._set_result(f"✓ Port {port} answers now{ms} — try again.", "#4ade80")
        elif state == reachability.STATE_DEGRADED:
            self._set_result(
                f"The machine answers ping, but nothing answers on port {port}: the "
                "service is stopped, or a firewall blocks that port.", "#fbbf24")
        elif "getaddrinfo" in detail or "11001" in detail:
            self._set_result("✗ The name does not resolve from this PC — check the "
                             "spelling, or use the IP address.", "#f87171")
        else:
            self._set_result(
                f"✗ No answer on port {port} or to ping — the machine is off, or this PC "
                "cannot reach its network (VPN?).", "#f87171")

    def _wake(self):
        self.btn_wake.setEnabled(False)
        self._set_result("Sending a magic packet…", "#fbbf24")
        conn = dict(self._conn)

        def work():
            from app.wake_on_lan import wait_for_wake, wake_connection
            result = wake_connection(conn)
            if not result.ok:
                self._post(lambda: self._wake_done(False, result.summary()))
                return
            self._post(lambda: self._set_result(
                "Magic packet sent — waiting for it to answer (up to 2 minutes)…", "#fbbf24"))
            came_up, waited = wait_for_wake(conn, 120, should_stop=self._gone)
            text = (f"Up after {waited:.0f} s — connecting." if came_up
                    else "Still no answer after 2 minutes. Wake-on-LAN may be off in the "
                         "BIOS, or the packet cannot reach that network from here.")
            self._post(lambda: self._wake_done(came_up, text))
        threading.Thread(target=work, name="rpmote-failure-wake", daemon=True).start()

    def _wake_done(self, ok: bool, text: str):
        self.btn_wake.setEnabled(True)
        self._set_result(("✓ " if ok else "✗ ") + text, "#4ade80" if ok else "#f87171")
        if ok:
            self._retry()
