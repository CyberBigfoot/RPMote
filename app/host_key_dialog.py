"""The trust prompt for an unrecognised or changed SSH host key.

Two situations, deliberately shaped differently:

  first contact   an ordinary question with a fingerprint to check. Trusting
                  is one click, because it is the normal case.
  key changed     a warning that defaults to Cancel and will not let you
                  proceed until you tick a box saying you verified it. A
                  changed key is either a rebuilt server or someone sitting
                  in the middle, and only the user knows which.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
    QWidget,
)

from app import icons
from app.host_keys import HostKeyMismatch, legacy_fingerprint

_MONO = "font-family: 'Cascadia Mono', 'Consolas', monospace; font-size: 12px;"
_HINT = "color: #9aa0b4; font-size: 11px;"
_DANGER = "color: #f87171; font-weight: bold;"


class HostKeyDecision:
    """What the user chose. ``connect`` is the only field callers must check."""

    def __init__(self, connect: bool = False, remember: bool = False,
                 replace: bool = False):
        self.connect = connect
        self.remember = remember
        self.replace = replace

    def __bool__(self):
        return self.connect


class HostKeyDialog(QDialog):
    """Ask whether to trust the key ``challenge`` was raised for."""

    def __init__(self, challenge, connection_name: str = "", parent=None):
        super().__init__(parent)
        self.challenge = challenge
        self.decision = HostKeyDecision()
        self._changed = isinstance(challenge, HostKeyMismatch)

        target = f"{challenge.host}:{challenge.port}"
        if connection_name:
            target = f"{connection_name}  ({target})"

        self.setWindowTitle(
            "Host Key Changed" if self._changed else "Unknown Host Key"
        )
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setSpacing(11)

        if self._changed:
            root.addWidget(self._icon_heading(
                "warning", "The host key for this server has CHANGED", _DANGER))
            root.addWidget(self._body(
                "The server answered with a different key than the one RPMote "
                "has on record. That happens when a machine is rebuilt or its "
                "SSH host keys are regenerated — and it is also exactly what a "
                "machine-in-the-middle looks like. Your saved password would be "
                "handed to whoever answered.\n\n"
                "Do not continue until you have confirmed the new fingerprint "
                "out of band with whoever runs the server."))
        else:
            root.addWidget(self._heading(
                "RPMote has not connected to this server before"))
            root.addWidget(self._body(
                "Check the fingerprint below against the one the server's "
                "administrator published, or run "
                "'ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub' on the "
                "machine itself. Once trusted, RPMote refuses to connect if the "
                "key ever changes."))

        root.addWidget(self._field("Server", target))
        root.addWidget(self._field("Key type", challenge.algorithm))

        if self._changed:
            for fp in challenge.expected_fingerprints:
                root.addWidget(self._field("Expected", fp, danger=False))
            root.addWidget(self._field("Received", challenge.fingerprint, danger=True))
        else:
            root.addWidget(self._field("SHA256", challenge.fingerprint))
            legacy = legacy_fingerprint(challenge.key)
            if legacy:
                root.addWidget(self._field("MD5", legacy))

        self.ack = None
        if self._changed:
            self.ack = QCheckBox(
                "I have verified this new fingerprint with the server's administrator"
            )
            self.ack.toggled.connect(self._on_ack_toggled)
            root.addWidget(self.ack)

        root.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)

        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)

        if self._changed:
            self.btn_replace = QPushButton("Replace Key and Connect")
            self.btn_replace.setEnabled(False)
            self.btn_replace.clicked.connect(self._accept_replace)
            buttons.addWidget(self.btn_cancel)
            buttons.addWidget(self.btn_replace)
            self.btn_cancel.setDefault(True)
            self.btn_cancel.setAutoDefault(True)
        else:
            self.btn_once = QPushButton("Connect Once")
            self.btn_once.setToolTip(
                "Connect without saving the key. You will be asked again next time."
            )
            self.btn_once.clicked.connect(self._accept_once)

            self.btn_trust = QPushButton("Trust and Connect")
            self.btn_trust.setObjectName("btn_primary")
            self.btn_trust.clicked.connect(self._accept_trust)

            buttons.addWidget(self.btn_cancel)
            buttons.addWidget(self.btn_once)
            buttons.addWidget(self.btn_trust)
            self.btn_trust.setDefault(True)
            self.btn_trust.setAutoDefault(True)

        root.addLayout(buttons)

    # ── pieces ──────────────────────────────────────────────────────

    @classmethod
    def _icon_heading(cls, icon_name: str, text: str, style: str):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        mark = QLabel()
        mark.setPixmap(icons.pixmap(icon_name, 18, "#f87171", row.devicePixelRatioF()))
        mark.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.addWidget(mark)
        layout.addWidget(cls._heading(text, style), 1)
        return row

    @staticmethod
    def _heading(text: str, style: str = "font-weight: bold; font-size: 13px;"):
        label = QLabel(text)
        label.setStyleSheet(style)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _body(text: str):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(_HINT)
        return label

    @staticmethod
    def _field(name: str, value: str, danger: bool = False):
        row = QFrame()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        caption = QLabel(f"{name}:")
        caption.setStyleSheet(_HINT)
        caption.setFixedWidth(76)
        layout.addWidget(caption)
        shown = QLabel(value or "—")
        shown.setStyleSheet(_MONO + (_DANGER if danger else ""))
        shown.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(shown, 1)
        return row

    # ── outcomes ────────────────────────────────────────────────────

    def _on_ack_toggled(self, checked: bool):
        self.btn_replace.setEnabled(bool(checked))

    def _accept_trust(self):
        self.decision = HostKeyDecision(connect=True, remember=True)
        self.accept()

    def _accept_once(self):
        self.decision = HostKeyDecision(connect=True, remember=False)
        self.accept()

    def _accept_replace(self):
        self.decision = HostKeyDecision(connect=True, remember=True, replace=True)
        self.accept()


def ask_about_host_key(challenge, connection_name: str = "", parent=None) -> HostKeyDecision:
    """Show the prompt and return the decision. Must run on the GUI thread."""
    dialog = HostKeyDialog(challenge, connection_name, parent)
    dialog.exec()
    return dialog.decision
