"""
RPMote Account Recovery — recovery keys and the locked-out escape hatch.

The vault body is encrypted with a key that only the master password (or a
recovery key) can unwrap, so there is no back door and none is added here.
What this module provides is:

  * RecoveryKeyDialog   — issue a recovery key while the vault is open, and
                          make the user actually save it before closing.
  * ForgotPasswordDialog — the "I'm locked out" flow: redeem a recovery key to
                          get back in with the data intact, or, failing that,
                          erase the vault knowingly and start over.

A recovery key is only useful if it was created *before* the lockout.  Nothing
in here can open a vault whose password was forgotten and which never had one.
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QCheckBox, QDialogButtonBox, QMessageBox, QFileDialog, QFrame,
    QApplication,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from app.secure_clipboard import copy_secret
from app.vault import MIN_MASTER_PASSWORD_LEN, VaultError, format_recovery_code

#: Self-contained so these dialogs look right over the login screen, which
#: runs before any application stylesheet is applied.
_STYLE = """
QDialog { background: #14141c; }
QLabel { color: #d4d6e2; font-family: 'Segoe UI', Inter, sans-serif; font-size: 12px; }
QLabel#heading { color: #ffffff; font-size: 16px; font-weight: 700; }
QLabel#warn { color: #fca5a5; }
QLabel#muted { color: #8b8fa3; font-size: 11px; }
QLineEdit {
    background: #1d1d28; border: 1px solid #2f3040; border-radius: 8px;
    padding: 9px 11px; color: #e8e9f0; font-size: 13px;
    font-family: 'Segoe UI', Inter, sans-serif;
}
QLineEdit:focus { border: 1px solid #7c6ff7; }
QLineEdit#code {
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 15px; letter-spacing: 2px;
}
QPushButton {
    background: #262636; border: 1px solid #34354a; border-radius: 8px;
    padding: 8px 16px; color: #e8e9f0; font-size: 12px;
    font-family: 'Segoe UI', Inter, sans-serif;
}
QPushButton:hover { background: #2f3044; border-color: #45465f; }
QPushButton:disabled { color: #5a5c70; background: #1c1c26; border-color: #262636; }
QPushButton#primary { background: #0aa3c2; border-color: #22c5e3; color: #0b0e14; }
QPushButton#primary:hover { background: #22c5e3; }
QPushButton#primary:disabled { background: #2a2740; border-color: #332f4d; color: #6a6a85; }
QPushButton#danger { background: #7f1d1d; border-color: #991b1b; color: #fee2e2; }
QPushButton#danger:hover { background: #991b1b; }
QPushButton#danger:disabled { background: #2a1a1a; border-color: #3a2323; color: #6b5252; }
QCheckBox { color: #d4d6e2; font-size: 12px; font-family: 'Segoe UI', Inter, sans-serif; }
QFrame#rule { background: #26263a; max-height: 1px; border: none; }
QFrame#codebox {
    background: #10101a; border: 1px solid #34354a; border-radius: 10px;
}
"""


def _rule() -> QFrame:
    line = QFrame()
    line.setObjectName("rule")
    line.setFrameShape(QFrame.Shape.HLine)
    return line


class RecoveryKeyDialog(QDialog):
    """Show a freshly issued recovery key and make sure it gets saved."""

    def __init__(self, code: str, parent=None, replaced_existing: bool = False):
        super().__init__(parent)
        self.code = format_recovery_code(code)
        self.setWindowTitle("Your Recovery Key")
        self.setStyleSheet(_STYLE)
        self.setMinimumWidth(470)

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 20)
        root.setSpacing(14)

        heading = QLabel("Save your recovery key")
        heading.setObjectName("heading")
        root.addWidget(heading)

        blurb = QLabel(
            "This is the only way back into your vault if you forget your "
            "master password. RPMote does not keep a copy — if you lose both, "
            "your saved connections cannot be recovered by anyone."
        )
        blurb.setWordWrap(True)
        root.addWidget(blurb)

        box = QFrame()
        box.setObjectName("codebox")
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(16, 16, 16, 16)
        code_label = QLabel(self.code)
        code_label.setObjectName("code")
        code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        code_label.setFont(QFont("Consolas", 14, QFont.Weight.DemiBold))
        code_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        box_layout.addWidget(code_label)
        root.addWidget(box)

        buttons = QHBoxLayout()
        self.btn_copy = QPushButton("Copy to Clipboard")
        self.btn_save = QPushButton("Save to File…")
        buttons.addWidget(self.btn_copy)
        buttons.addWidget(self.btn_save)
        buttons.addStretch(1)
        root.addLayout(buttons)

        root.addWidget(_rule())

        warn_text = (
            "Anyone holding this key can open your vault — store it somewhere "
            "separate from this computer, like a password manager or a safe."
        )
        if replaced_existing:
            warn_text += " Your previous recovery key no longer works."
        warn = QLabel(warn_text)
        warn.setObjectName("warn")
        warn.setWordWrap(True)
        root.addWidget(warn)

        self.chk_saved = QCheckBox("I have saved my recovery key somewhere safe")
        root.addWidget(self.chk_saved)

        self.btn_done = QPushButton("Done")
        self.btn_done.setObjectName("primary")
        self.btn_done.setEnabled(False)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.btn_done)
        root.addLayout(row)

        self.btn_copy.clicked.connect(self._copy)
        self.btn_save.clicked.connect(self._save)
        self.chk_saved.toggled.connect(self.btn_done.setEnabled)
        self.btn_done.clicked.connect(self.accept)

    def _copy(self):
        # A recovery code opens the vault without the password; it must not
        # sit in clipboard history.  Long enough to paste into a manager.
        copy_secret(self.code, clear_after_ms=60_000)
        self.btn_copy.setText("Copied ✓ (clears in 60s)")

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Recovery Key", "RPMote-Recovery-Key.txt",
            "Text Files (*.txt);;All Files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(
                    "RPMote vault recovery key\n"
                    "=========================\n\n"
                    f"{self.code}\n\n"
                    "Keep this somewhere safe and private. Anyone with this key\n"
                    "can open your RPMote vault without the master password.\n"
                    "Creating a new recovery key invalidates this one.\n"
                )
        except OSError as exc:
            QMessageBox.warning(self, "Could Not Save",
                                f"The recovery key was not saved:\n{exc}")
            return
        self.btn_save.setText("Saved ✓")
        self.chk_saved.setChecked(True)

    def reject(self):
        """Closing without confirming is treated as 'not saved yet'."""
        if not self.chk_saved.isChecked():
            confirm = QMessageBox.question(
                self, "Close Without Saving?",
                "You have not confirmed that the recovery key is saved.\n\n"
                "It cannot be shown again — you would have to generate a new "
                "one. Close anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if confirm != QMessageBox.StandardButton.Yes:
                return
        super().reject()


class ForgotPasswordDialog(QDialog):
    """The locked-out flow, offered from the login screen.

    Outcome is readable afterwards via :attr:`outcome`:
      ``"recovered"`` — vault is unlocked and a new password has been set
      ``"erased"``    — the vault was deleted and the app should start fresh
      ``None``        — the user backed out
    """

    def __init__(self, vault, parent=None):
        super().__init__(parent)
        self.vault = vault
        self.outcome: str | None = None
        self.setWindowTitle("Forgot Master Password")
        self.setStyleSheet(_STYLE)
        self.setMinimumWidth(500)

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 20)
        root.setSpacing(12)

        heading = QLabel("Locked out of your vault")
        heading.setObjectName("heading")
        root.addWidget(heading)

        intro = QLabel(
            "Your connections are encrypted with your master password. RPMote "
            "cannot look it up or reset it — there is no copy of it anywhere."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        root.addWidget(_rule())

        # ── Path 1: redeem a recovery key ──────────────────────────
        has_key = vault.has_recovery_key
        title = QLabel("Use a recovery key" if has_key
                       else "Use a recovery key — not available")
        title.setObjectName("heading")
        title.setStyleSheet("font-size: 13px;")
        root.addWidget(title)

        if has_key:
            root.addWidget(self._label(
                "Enter the recovery key you saved, then choose a new master "
                "password. Everything in your vault is kept."))
            self.code_input = QLineEdit()
            self.code_input.setObjectName("code")
            self.code_input.setPlaceholderText("XXXXX-XXXXX-XXXXX-XXXXX-XXXXX")
            root.addWidget(self.code_input)

            pw_row = QHBoxLayout()
            self.pw_input = QLineEdit()
            self.pw_input.setEchoMode(QLineEdit.EchoMode.Password)
            self.pw_input.setPlaceholderText("New master password")
            self.pw_confirm = QLineEdit()
            self.pw_confirm.setEchoMode(QLineEdit.EchoMode.Password)
            self.pw_confirm.setPlaceholderText("Confirm new password")
            pw_row.addWidget(self.pw_input)
            pw_row.addWidget(self.pw_confirm)
            root.addLayout(pw_row)

            self.recover_error = QLabel("")
            self.recover_error.setObjectName("warn")
            self.recover_error.setWordWrap(True)
            root.addWidget(self.recover_error)

            self.btn_recover = QPushButton("Unlock and Set New Password")
            self.btn_recover.setObjectName("primary")
            self.btn_recover.clicked.connect(self._do_recover)
            row = QHBoxLayout()
            row.addStretch(1)
            row.addWidget(self.btn_recover)
            root.addLayout(row)
            for field in (self.code_input, self.pw_input, self.pw_confirm):
                field.returnPressed.connect(self._do_recover)
        else:
            root.addWidget(self._label(
                "No recovery key was ever created for this vault, so there is "
                "nothing to redeem. Once you are back in, you can create one "
                "from File → Recovery Key so this is survivable next time."))

        root.addWidget(_rule())

        # ── Path 2: erase and start over ───────────────────────────
        erase_title = QLabel("Erase the vault and start over")
        erase_title.setObjectName("heading")
        erase_title.setStyleSheet("font-size: 13px;")
        root.addWidget(erase_title)
        root.addWidget(self._label(
            "This permanently deletes every saved connection, credential, "
            "snippet and setting, then lets you create a new master password. "
            "It cannot be undone."))

        erase_row = QHBoxLayout()
        self.erase_input = QLineEdit()
        self.erase_input.setPlaceholderText("Type ERASE to confirm")
        self.btn_erase = QPushButton("Erase Everything")
        self.btn_erase.setObjectName("danger")
        self.btn_erase.setEnabled(False)
        erase_row.addWidget(self.erase_input)
        erase_row.addWidget(self.btn_erase)
        root.addLayout(erase_row)

        self.erase_input.textChanged.connect(
            lambda t: self.btn_erase.setEnabled(t.strip().upper() == "ERASE"))
        self.btn_erase.clicked.connect(self._do_erase)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("muted")
        label.setWordWrap(True)
        return label

    # ── Actions ────────────────────────────────────────────────────

    def _do_recover(self):
        code = self.code_input.text().strip()
        password = self.pw_input.text()
        if not code:
            self.recover_error.setText("Enter your recovery key.")
            return
        if password != self.pw_confirm.text():
            self.recover_error.setText("The new passwords do not match.")
            return
        if len(password) < MIN_MASTER_PASSWORD_LEN:
            self.recover_error.setText(
                f"The new password must be at least {MIN_MASTER_PASSWORD_LEN} characters.")
            return

        self.btn_recover.setEnabled(False)
        self.recover_error.setText("Checking recovery key…")
        QApplication.processEvents()
        try:
            ok = self.vault.unlock_with_recovery_key(code)
        except VaultError as exc:
            self.btn_recover.setEnabled(True)
            self.recover_error.setText(str(exc))
            return
        if not ok:
            self.btn_recover.setEnabled(True)
            self.recover_error.setText(
                "That recovery key is not valid for this vault.")
            return

        try:
            self.vault.set_password_after_recovery(password)
        except VaultError as exc:
            self.btn_recover.setEnabled(True)
            self.recover_error.setText(str(exc))
            return

        self.outcome = "recovered"
        QMessageBox.information(
            self, "Vault Recovered",
            "Your vault is open and your new master password is set.\n\n"
            "Your recovery key still works — keep it somewhere safe.")
        self.accept()

    def _do_erase(self):
        confirm = QMessageBox.question(
            self, "Erase Everything?",
            "<h3>This deletes all saved connections and credentials</h3>"
            "<p>There is no undo and no backup. You will be asked to create a "
            "new master password.</p>",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            self.vault.factory_reset()
        except Exception as exc:
            QMessageBox.critical(self, "Could Not Erase",
                                 f"The vault could not be erased:\n{exc}")
            return
        self.outcome = "erased"
        self.accept()


def offer_recovery_key(vault, parent=None, *, prompt: bool = True) -> bool:
    """Create (or replace) a recovery key, showing it once.  True if issued.

    *prompt* asks first — used right after setup, where the user has not
    explicitly requested one.
    """
    replacing = vault.has_recovery_key
    if prompt:
        if replacing:
            question = (
                "Create a new recovery key?\n\n"
                "Your current recovery key will stop working immediately.")
        else:
            question = (
                "Create a recovery key?\n\n"
                "It is the only way back into your vault if you forget your "
                "master password.")
        answer = QMessageBox.question(
            parent, "Recovery Key", question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        if answer != QMessageBox.StandardButton.Yes:
            return False
    try:
        code = vault.create_recovery_key()
    except VaultError as exc:
        QMessageBox.warning(parent, "Recovery Key", str(exc))
        return False
    RecoveryKeyDialog(code, parent=parent, replaced_existing=replacing).exec()
    return True
