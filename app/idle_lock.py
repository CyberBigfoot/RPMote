"""Locking the screen after the technician walks away.

An unlocked RPMote on an unattended desk is every customer's password, every
2FA code and every open console, available to whoever sits down next.  So
after a stretch with no keyboard or mouse input the window is covered by a
lock screen that only the master password opens.

What it deliberately does *not* do is tear anything down.  SSH sessions
keep their connection, RDP windows keep their desktop, a long-running
command keeps running — coming back from lunch should cost one password,
not every session.  The vault itself stays open in memory for the same
reason; what the lock takes away is the screen and the keyboard.  Anyone
who wants secrets flushed from memory has Lock Vault (Ctrl+L), which closes
the whole application.

The cover is part of the window, not a window of its own, so it is
exactly as big as RPMote is at the time: the whole screen when the app was
left maximised or full screen, a window on the desk when it was a window,
and nothing at all while it is minimised or behind another program.  What
keeps it a lock rather than a picture is IdleLock's event filter, which
swallows every click, key and menu shortcut aimed anywhere but the cover
for as long as it is up.

The timeout lives in the vault's settings as ``idle_lock_minutes``; zero
turns the lock off.
"""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QGuiApplication, QPalette, QShortcut
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

#: Setting key and default.  Fifteen minutes is long enough not to fire in
#: the middle of watching a command run, short enough for a walk to the car.
from app.lock_policy import (
    IDLE_LOCK_SETTING, DEFAULT_IDLE_LOCK_MINUTES, LOCK_ON_MINIMIZE,
    LOCK_ON_WINDOWS_LOCK, LOCK_ON_SLEEP, idle_lock_minutes, normalize_lock_settings,
)

#: Events that count as "someone is here".  Mouse movement alone is enough:
#: a technician reading output moves the mouse without clicking.
_ACTIVITY_EVENTS = frozenset({
    QEvent.Type.KeyPress,
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.MouseMove,
    QEvent.Type.Wheel,
    QEvent.Type.TouchBegin,
    QEvent.Type.TabletPress,
})

#: Input that must not reach anything but the cover while it is up.  The
#: cover hides the window's widgets, so this is about what a picture cannot
#: stop: keyboard focus left in a terminal, and the menu shortcuts a window
#: answers to no matter which of its children has focus.
_BLOCKED_WHILE_LOCKED = frozenset({
    QEvent.Type.KeyPress,
    QEvent.Type.KeyRelease,
    QEvent.Type.ShortcutOverride,
    QEvent.Type.Shortcut,
    QEvent.Type.InputMethod,
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonRelease,
    QEvent.Type.MouseButtonDblClick,
    QEvent.Type.MouseMove,
    QEvent.Type.Wheel,
    QEvent.Type.ContextMenu,
    QEvent.Type.TouchBegin,
    QEvent.Type.TouchUpdate,
    QEvent.Type.TouchEnd,
    QEvent.Type.TabletPress,
    QEvent.Type.TabletMove,
    QEvent.Type.TabletRelease,
    QEvent.Type.DragEnter,
    QEvent.Type.DragMove,
    QEvent.Type.Drop,
})

#: Everything the filter looks at; anything else is passed through before
#: the lock's own state is touched.  (While a window is being destroyed
#: Qt still routes its teardown events past the filter, after Python has
#: let go of the object's attributes.)
_WATCHED = _ACTIVITY_EVENTS | _BLOCKED_WHILE_LOCKED


class _ModalCover(QWidget):
    """Hide an already-open dialog without closing it or discarding its draft."""

    def __init__(self, dialog):
        super().__init__(dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("background:#090b14;")
        dialog.installEventFilter(self)
        self.setGeometry(dialog.rect())
        self.show()
        self.raise_()

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
        elif obj is self.parentWidget() and event.type() == QEvent.Type.ChildAdded:
            QTimer.singleShot(0, self.raise_)
        return False


class _UnlockPrompt(QDialog):
    """Keep unlocking reachable above an existing application-modal dialog."""

    def __init__(self, screen):
        super().__init__(screen, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.screen = screen
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle("Unlock RPMote")
        self.setStyleSheet("QDialog { background:#090b14; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.addWidget(screen.card)
        self.adjustSize()

    def reject(self):
        if self.screen.accepted:
            super().reject()

    def closeEvent(self, event):
        if not self.screen.accepted:
            event.ignore()
        else:
            super().closeEvent(event)


class LockScreen(QFrame):
    """The cover: an opaque overlay filling the window it protects.

    A child of that window, so it goes wherever the window goes -- another
    monitor, maximised, minimised, behind the browser -- and is never
    larger than the window itself.  It has no close button and ignores
    Escape and close(); the only ways past are the password or quitting.
    Keeping the rest of the application from taking input meanwhile is
    IdleLock's job.
    """

    unlocked = pyqtSignal()
    quit_requested = pyqtSignal()

    MAX_ATTEMPTS_BEFORE_DELAY = 3

    def __init__(self, vault, window: QWidget, reason: str = "manual", minutes: int = 0):
        super().__init__(window)
        self.vault = vault
        self._failures = 0
        self._accepted = False
        self._modal_covers = []
        self._protected_modals = []
        self._unlock_prompt = None
        #: "idle" when the timer locked it, "manual" for Lock Screen Now.
        self.reason = reason
        self._minutes = int(minutes or 0)
        self.setObjectName("lockScreen")
        # A plain frame only paints a stylesheet background when told to.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # The theme's accent (the main window keeps it in the palette), so
        # the lock screen looks like the rest of RPMote and its login.
        accent = QGuiApplication.palette().color(QPalette.ColorRole.Highlight)
        hover = accent.lighter(115)
        from app.themes import on_color
        on_accent = on_color(accent.name())
        self.setStyleSheet(
            "#lockScreen { background: #090b14; }"
            "#lockCard { background: #11131f; border: 1px solid rgba(255,255,255,0.09);"
            "  border-radius: 16px; }"
            "QLabel { color: #e2e8f0; background: transparent; }"
            "#lockTitle { font-size: 20px; font-weight: 700; }"
            "#lockSubtitle { color: #a3acc2; font-size: 13px; }"
            "QLineEdit { background: #0c0d18; color: #f8fafc; border: 1px solid #262a3d;"
            "  border-radius: 10px; padding: 0 14px; font-size: 15px; }"
            f"QLineEdit:focus {{ border-color: {accent.name()}; }}"
            f"#unlockButton {{ background: {accent.name()}; color: {on_accent}; border: none;"
            "  border-radius: 10px; font-size: 14px; font-weight: 600; }"
            f"#unlockButton:hover {{ background: {hover.name()}; }}"
            "#unlockButton:disabled { background: #2a2d3d; color: #6b7280; }"
            "#quitButton { background: transparent; border: none; color: #8b93a7;"
            "  font-size: 12px; padding: 4px 8px; }"
            "#quitButton:hover { color: #e2e8f0; text-decoration: underline; }"
        )
        self._build()

    def subtitle_text(self) -> str:
        """Why the screen is locked, in a sentence."""
        if self.reason == "idle" and self._minutes > 0:
            when = (f"Locked after {self._minutes} minute{'s' if self._minutes != 1 else ''} "
                    "without input.")
        elif self.reason == "minimized":
            when = "Locked when the app was minimized."
        elif self.reason == "windows_lock":
            when = "Locked with your Windows session."
        elif self.reason == "sleep":
            when = "Locked after a sleep or resume event."
        else:
            when = "Locked."
        return f"{when} Your sessions are still running.\nEnter your master password to continue."

    def _build(self):
        from app import icons

        outer = QVBoxLayout(self)
        outer.addStretch(1)
        card = QFrame()
        self.card = card
        card.setObjectName("lockCard")
        card.setFixedWidth(400)
        column = QVBoxLayout(card)
        column.setContentsMargins(36, 32, 36, 26)
        column.setSpacing(10)

        accent = QGuiApplication.palette().color(QPalette.ColorRole.Highlight)
        badge = QLabel()
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setPixmap(icons.pixmap("lock", 34, accent.name(), self.devicePixelRatioF()))
        column.addWidget(badge)

        title = QLabel("RPMote is locked")
        title.setObjectName("lockTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(title)

        self.subtitle = QLabel(self.subtitle_text())
        self.subtitle.setObjectName("lockSubtitle")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle.setWordWrap(True)
        column.addWidget(self.subtitle)
        column.addSpacing(10)

        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Master password")
        self.password.setFixedHeight(44)
        self.password.returnPressed.connect(self._try_unlock)
        column.addWidget(self.password)

        self.error = QLabel("")
        self.error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error.setFixedHeight(18)
        self.error.setStyleSheet("color: #f87171; font-size: 12px;")
        column.addWidget(self.error)

        self.btn_unlock = QPushButton("Unlock")
        self.btn_unlock.setObjectName("unlockButton")
        self.btn_unlock.setFixedHeight(44)
        self.btn_unlock.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_unlock.setDefault(True)
        self.btn_unlock.clicked.connect(self._try_unlock)
        column.addWidget(self.btn_unlock)

        self.btn_quit = QPushButton("Quit RPMote")
        self.btn_quit.setObjectName("quitButton")
        self.btn_quit.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_quit.clicked.connect(self.quit_requested.emit)
        column.addWidget(self.btn_quit, 0, Qt.AlignmentFlag.AlignHCenter)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)
        self._focus_ring = (self.password, self.btn_unlock, self.btn_quit)

    @property
    def accepted(self) -> bool:
        """True once the right password has been entered (or quit chosen)."""
        return self._accepted

    def cover(self):
        """Fill the window and show; the window is not raised or activated,
        so a technician working in another program is left alone."""
        window = self.parentWidget()
        self.setGeometry(window.rect())
        window.installEventFilter(self)
        self.error.setText("")
        self.password.clear()
        # A modal editor/settings window would otherwise keep the main
        # cover from receiving the password, and expose its own contents.
        self._protected_modals = [w for w in QApplication.topLevelWidgets()
                                  if w is not window and w.isVisible() and w.isModal()]
        for dialog in self._protected_modals:
            self._modal_covers.append(_ModalCover(dialog))
        if self._protected_modals:
            self._unlock_prompt = _UnlockPrompt(self)
        self.show()
        self.raise_()
        if self._unlock_prompt is not None and not window.isMinimized():
            self._unlock_prompt.show()
        self.password.setFocus()

    def eventFilter(self, obj, event):
        # Stay exactly the window's size, and stay on top of anything the
        # window grows later.
        if obj is self.parentWidget():
            if event.type() == QEvent.Type.Resize:
                self.setGeometry(obj.rect())
            elif event.type() == QEvent.Type.ChildAdded:
                QTimer.singleShot(0, self.raise_)
            elif event.type() == QEvent.Type.WindowStateChange and self._unlock_prompt is not None:
                self._unlock_prompt.setVisible(not obj.isMinimized())
        return False

    def focusNextPrevChild(self, next_: bool) -> bool:
        # Tab must not walk focus out under the cover.
        ring = [w for w in self._focus_ring if w.isEnabled()]
        if not ring:
            return True
        current = self.focusWidget()
        idx = ring.index(current) if current in ring else -1
        step = 1 if next_ else -1
        ring[(idx + step) % len(ring)].setFocus(
            Qt.FocusReason.TabFocusReason if next_ else Qt.FocusReason.BacktabFocusReason)
        return True

    def accept(self):
        """Take the cover down for good."""
        self._accepted = True
        parent = self.parentWidget()
        if parent is not None:
            parent.removeEventFilter(self)
        for cover in self._modal_covers:
            dialog = cover.parentWidget()
            if dialog is not None:
                dialog.removeEventFilter(cover)
            cover.hide()
            cover.deleteLater()
        self._modal_covers.clear()
        if self._unlock_prompt is not None:
            self._unlock_prompt.done(QDialog.DialogCode.Accepted)
            self._unlock_prompt.deleteLater()
            self._unlock_prompt = None
        self.hide()
        self.deleteLater()

    def _try_unlock(self):
        entered = self.password.text()
        self.password.clear()
        if entered and self.vault.verify_password(entered):
            self._failures = 0
            self.unlocked.emit()
            self.accept()
            return
        self._failures += 1
        self.error.setText("That master password is not correct.")
        if self._failures >= self.MAX_ATTEMPTS_BEFORE_DELAY:
            # Slow a guesser down without locking the owner out for good.
            self.password.setEnabled(False)
            self.btn_unlock.setEnabled(False)
            QTimer.singleShot(3000, self._allow_retry)

    def _allow_retry(self):
        self.password.setEnabled(True)
        self.btn_unlock.setEnabled(True)
        self.password.setFocus()

    def keyPressEvent(self, event):
        # Escape must not dismiss the cover.
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        # Clicks on the cover end here; nothing underneath gets them.
        event.accept()

    def closeEvent(self, event):
        # A stray close(): stay locked.
        if not self._accepted:
            event.ignore()
            return
        super().closeEvent(event)


class IdleLock(QObject):
    """Watches the whole application for input and raises the lock screen.

    Installed as an event filter on the QApplication so any widget's input
    counts.  ``minutes`` can be changed at runtime; 0 stops the watch.
    """

    locked = pyqtSignal()
    unlocked = pyqtSignal()
    settings_changed = pyqtSignal(dict)

    def __init__(self, vault, window: QWidget, minutes: int = DEFAULT_IDLE_LOCK_MINUTES, settings=None):
        super().__init__(window)
        self.vault = vault
        self.window = window
        self._screen: LockScreen | None = None
        self._closed = False
        self._settings = normalize_lock_settings(settings if settings is not None else {IDLE_LOCK_SETTING: minutes})
        from app.windows_lock_events import WindowsLockEvents
        self._native = WindowsLockEvents(window, self._system_event)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self.lock_now("idle"))
        self._minutes = 0
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.set_minutes(self._settings[IDLE_LOCK_SETTING])
        window.destroyed.connect(self.shutdown)
        QTimer.singleShot(0, self._start_native_events)

    # ── Configuration ────────────────────────────────────────────────

    @property
    def minutes(self) -> int:
        return self._minutes

    @property
    def policy(self):
        return dict(self._settings)

    def set_minutes(self, minutes: int):
        self._minutes = idle_lock_minutes({IDLE_LOCK_SETTING: minutes})
        self._settings[IDLE_LOCK_SETTING] = self._minutes
        if self._minutes <= 0:
            self._timer.stop()
        elif not self.is_locked:
            self._timer.start(self._minutes * 60_000)
        self.settings_changed.emit(dict(self._settings))

    def apply_settings(self, settings):
        self._settings = normalize_lock_settings(settings)
        self.set_minutes(self._settings[IDLE_LOCK_SETTING])

    def _start_native_events(self):
        if not self._closed:
            self._native.start()

    def _system_event(self, reason):
        key = LOCK_ON_WINDOWS_LOCK if reason == "windows_lock" else LOCK_ON_SLEEP
        if not self._closed and self._settings[key]:
            QTimer.singleShot(0, lambda: not self._closed and self.lock_now(reason))

    def shutdown(self, *_args):
        if self._closed:
            return
        self._closed = True
        try:
            self._timer.stop()
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self._native.stop()
            if self._screen is not None:
                self._screen.accept()
                self._screen = None
        except RuntimeError:
            pass

    @property
    def is_locked(self) -> bool:
        return self._screen is not None

    # ── Activity ─────────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        et = event.type()
        if self._closed:
            return False
        if obj is self.window and et == QEvent.Type.WindowStateChange:
            if self._settings[LOCK_ON_MINIMIZE] and self.window.isMinimized():
                self.lock_now("minimized")
            return False
        if et not in _WATCHED:
            return False
        if self.is_locked:
            return et in _BLOCKED_WHILE_LOCKED and self._reaches_past_cover(obj)
        if et in _ACTIVITY_EVENTS and self._minutes > 0:
            self._timer.start(self._minutes * 60_000)
        return False

    def _reaches_past_cover(self, obj) -> bool:
        """Would input to *obj* land somewhere the cover is meant to seal?

        The cover and its children take input, and so does a modal dialog
        that pops up over it (a prompt nobody could otherwise answer).
        Everything else in the application is off limits.  A shortcut is
        judged by the widgets it belongs to: in Qt 6 the event goes to the
        QAction or QShortcut itself, not to a widget.
        """
        if isinstance(obj, QAction):
            owners = [o for o in obj.associatedObjects() if isinstance(o, QWidget)]
            return not any(self._takes_input(w) for w in owners)
        if isinstance(obj, QShortcut):
            owner = obj.parentWidget()
            return owner is None or not self._takes_input(owner)
        if not isinstance(obj, QWidget):
            return False          # the QWindow layer; the widget gets its own event
        return not self._takes_input(obj)

    def _takes_input(self, widget: QWidget) -> bool:
        """The cover, anything inside it, or a modal window over it."""
        screen = self._screen
        w = widget
        while w is not None:
            if w is screen:
                return True
            w = w.parentWidget()
        top = widget.window()
        if top in screen._protected_modals:
            return False
        return top is not self.window and top.isModal()

    def touch(self):
        """Count as activity — for tests and for anything not routed through Qt."""
        if self._minutes > 0 and not self.is_locked:
            self._timer.start(self._minutes * 60_000)

    # ── Lock / unlock ────────────────────────────────────────────────

    def lock_now(self, reason: str = "manual"):
        """Raise the cover.  Safe to call when already locked or when the
        vault is not open (nothing to protect, nothing to verify against).

        *reason* is "idle" when the timer fires, so the screen can say so;
        anything else (Lock Screen Now) just says it is locked."""
        if self.is_locked:
            return
        if self._closed:
            return
        if not getattr(self.vault, "is_unlocked", False):
            return
        if getattr(self.window, "_closing", False):
            return
        self._timer.stop()
        screen = LockScreen(self.vault, self.window, reason=reason, minutes=self._minutes)
        screen.unlocked.connect(self._on_unlocked)
        screen.quit_requested.connect(self._on_quit)
        self._screen = screen
        self.locked.emit()
        screen.cover()

    def _on_unlocked(self):
        self._screen = None
        self.unlocked.emit()
        if self._minutes > 0:
            self._timer.start(self._minutes * 60_000)

    def _on_quit(self):
        screen, self._screen = self._screen, None
        if screen is not None:
            screen.accept()
        self.vault.lock()
        # Quit on the cover is deliberate; the main window must not ask again.
        close = getattr(self.window, "close_without_prompt", self.window.close)
        try:
            close()
        except RuntimeError:
            pass
