"""Answering a server's keyboard-interactive questions from the GUI.

paramiko's own fallback for keyboard-interactive auth reads the answers from
stdin. A windowed application has no stdin, so on a host with MFA the connect
would sit there until it timed out with nothing on screen to explain why.

``MFAPrompter`` bridges the two threads: the connect runs in the background,
the question is asked on the GUI thread, and the worker waits for the answer.
"""

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout,
)

_HINT = "color: #9aa0b4; font-size: 11px;"

#: How long a worker thread will wait for the person to answer.
ANSWER_TIMEOUT = 180.0

from app.widgets import set_variant

class MFADialog(QDialog):
    """Ask whatever the server asked, in the order it asked."""

    def __init__(self, challenge, parent=None):
        super().__init__(parent)
        self.challenge = challenge
        self.answers = None
        self._fields = []

        self.setWindowTitle(challenge.title or "Additional Authentication")
        self.setMinimumWidth(420)
        self.setModal(True)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        heading = QLabel(f"<b>{challenge.host}:{challenge.port} needs more than "
                         f"a password</b>")
        heading.setWordWrap(True)
        root.addWidget(heading)

        if challenge.instructions:
            note = QLabel(challenge.instructions.strip())
            note.setWordWrap(True)
            note.setStyleSheet(_HINT)
            root.addWidget(note)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        for prompt, echo in challenge.prompts:
            field = QLineEdit()
            if not echo:
                field.setEchoMode(QLineEdit.EchoMode.Password)
            self._fields.append(field)
            form.addRow(str(prompt).strip() or "Response:", field)
        root.addLayout(form)

        if self._fields:
            self._fields[0].setFocus()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        set_variant(buttons.button(QDialogButtonBox.StandardButton.Ok), "primary")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _accept(self):
        self.answers = [field.text() for field in self._fields]
        self.accept()


class MFAPrompter(QObject):
    """Hands a keyboard-interactive challenge to the GUI and waits."""

    #: (challenge, result dict, threading.Event) — queued to the GUI thread.
    _challenge = pyqtSignal(object, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._challenge.connect(self._ask, Qt.ConnectionType.QueuedConnection)

    def handler(self, challenge):
        """Called on the connect thread; returns the answers, or raises."""
        import threading

        done = threading.Event()
        result: dict = {}
        self._challenge.emit(challenge, result, done)
        if not done.wait(ANSWER_TIMEOUT):
            raise TimeoutError("No response to the authentication prompt")
        answers = result.get("answers")
        if answers is None:
            raise PermissionError("Authentication cancelled")
        return answers

    def _ask(self, challenge, result, done):
        try:
            parent = self.parent()
            dialog = MFADialog(challenge, parent if isinstance(parent, QDialog) else None)
            # A non-dialog parent still centres the prompt correctly.
            if not isinstance(parent, QDialog) and parent is not None:
                dialog.setParent(parent, Qt.WindowType.Dialog)
            dialog.exec()
            result["answers"] = dialog.answers
        except Exception:
            result["answers"] = None
        finally:
            done.set()
