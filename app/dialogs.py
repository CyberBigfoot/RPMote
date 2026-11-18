"""
Small, consistent dialogs: confirmations that name what they act on and
make the safe choice the default, and a plain notes editor.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QMessageBox, QPlainTextEdit, QVBoxLayout,
)

from app.widgets import fit_to_screen, set_variant


def confirm(parent, title: str, text: str, confirm_text: str = "OK", *,
            danger: bool = False, informative: str = "",
            cancel_text: str = "Cancel") -> bool:
    """Ask a yes/no question with buttons that say what they do.

    A dangerous action gets a red button and Cancel as the default, so an
    absent-minded Enter never deletes anything.
    """
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    ok = box.addButton(confirm_text, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton(cancel_text, QMessageBox.ButtonRole.RejectRole)
    set_variant(ok, "danger" if danger else "primary")
    box.setDefaultButton(cancel if danger else ok)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is ok


class NotesDialog(QDialog):
    """Free-text notes for a tenant: VPN first, who to call, quirks."""

    def __init__(self, title: str, intro: str, text: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(460, 300)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        label = QLabel(intro)
        label.setWordWrap(True)
        label.setProperty("role", "hint")
        layout.addWidget(label)
        self.editor = QPlainTextEdit()
        self.editor.setObjectName("notes_input")
        self.editor.setPlainText(text or "")
        self.editor.setPlaceholderText(
            "e.g. Connect the FortiClient VPN first · On-site contact: Dana, "
            "555-0100 · Never reboot SQL01 during business hours")
        layout.addWidget(self.editor, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        set_variant(buttons.button(QDialogButtonBox.StandardButton.Save), "primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(560, 380)
        fit_to_screen(self)
        self.editor.setFocus()

    def text(self) -> str:
        return self.editor.toPlainText().strip()
