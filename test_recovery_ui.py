"""Recovery UI tests: the forgot-password flow and the recovery-key dialog.

Modal dialogs are neutered (exec/question patched) so the flows can be driven
headlessly. Vault paths are redirected to a temp directory in setUp.
"""

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.vault as vault_mod

from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from app import recovery as recovery_mod
from app.recovery import (
    ForgotPasswordDialog,
    RecoveryKeyDialog,
    offer_recovery_key,
)
from app.vault import Vault, format_recovery_code, generate_recovery_code

_APP = QApplication.instance() or QApplication([])

PASSWORD = "correct-horse-battery"


class _RecoveryUITestCase(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="rpmote_recovery_ui_")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        vault_mod.use_vault_directory(root)
        self.root = root

        self.vault = Vault()
        self.vault.setup(PASSWORD)
        self.vault.add_connection({"name": "DC01", "host": "10.0.0.1",
                                   "protocol": "ssh", "tenant": "Contoso",
                                   "password": "s3cret"})

    def silence_modals(self, answer=QMessageBox.StandardButton.Yes):
        """Stop QMessageBox/QDialog from blocking on a real window."""
        self.addCleanup(setattr, QMessageBox, "question", QMessageBox.question)
        self.addCleanup(setattr, QMessageBox, "information",
                        QMessageBox.information)
        self.addCleanup(setattr, QMessageBox, "warning", QMessageBox.warning)
        QMessageBox.question = staticmethod(lambda *a, **k: answer)
        QMessageBox.information = staticmethod(lambda *a, **k: None)
        QMessageBox.warning = staticmethod(lambda *a, **k: None)


class ForgotPasswordFlowTests(_RecoveryUITestCase):

    def make_dialog(self):
        dialog = ForgotPasswordDialog(self.vault, parent=None)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_a_valid_recovery_key_reopens_the_vault_and_resets_the_password(self):
        code = self.vault.create_recovery_key()
        self.vault.lock()
        self.silence_modals()

        dialog = self.make_dialog()
        dialog.code_input.setText(code)
        dialog.pw_input.setText("brand-new-password")
        dialog.pw_confirm.setText("brand-new-password")
        dialog._do_recover()

        self.assertEqual(dialog.outcome, "recovered")
        self.assertTrue(self.vault.is_unlocked)
        self.assertEqual(self.vault.connections[0]["password"], "s3cret")

        self.vault.lock()
        self.assertTrue(Vault().unlock("brand-new-password"))
        self.assertFalse(Vault().unlock(PASSWORD))

    def test_the_recovery_key_still_works_after_the_reset(self):
        code = self.vault.create_recovery_key()
        self.vault.lock()
        self.silence_modals()

        dialog = self.make_dialog()
        dialog.code_input.setText(code)
        dialog.pw_input.setText("another-password")
        dialog.pw_confirm.setText("another-password")
        dialog._do_recover()
        self.vault.lock()

        self.assertTrue(Vault().unlock_with_recovery_key(code))

    def test_a_wrong_recovery_key_reports_and_leaves_the_vault_shut(self):
        self.vault.create_recovery_key()
        self.vault.lock()

        dialog = self.make_dialog()
        dialog.code_input.setText(generate_recovery_code())
        dialog.pw_input.setText("brand-new-password")
        dialog.pw_confirm.setText("brand-new-password")
        dialog._do_recover()

        self.assertIsNone(dialog.outcome)
        self.assertFalse(self.vault.is_unlocked)
        self.assertIn("not valid", dialog.recover_error.text())
        self.assertTrue(dialog.btn_recover.isEnabled(),
                        "the user must be able to try again")

    def test_mismatched_new_passwords_are_caught_before_the_key_is_used(self):
        code = self.vault.create_recovery_key()
        self.vault.lock()

        dialog = self.make_dialog()
        dialog.code_input.setText(code)
        dialog.pw_input.setText("password-one")
        dialog.pw_confirm.setText("password-two")
        dialog._do_recover()

        self.assertIsNone(dialog.outcome)
        self.assertFalse(self.vault.is_unlocked)
        self.assertIn("do not match", dialog.recover_error.text())

    def test_a_short_new_password_is_rejected(self):
        code = self.vault.create_recovery_key()
        self.vault.lock()

        dialog = self.make_dialog()
        dialog.code_input.setText(code)
        dialog.pw_input.setText("abc")
        dialog.pw_confirm.setText("abc")
        dialog._do_recover()

        self.assertIsNone(dialog.outcome)
        self.assertIn("12 characters", dialog.recover_error.text())

    def test_an_empty_code_is_rejected(self):
        self.vault.create_recovery_key()
        self.vault.lock()
        dialog = self.make_dialog()
        dialog.pw_input.setText("brand-new-password")
        dialog.pw_confirm.setText("brand-new-password")
        dialog._do_recover()
        self.assertIsNone(dialog.outcome)
        self.assertIn("Enter your recovery key", dialog.recover_error.text())

    def test_without_a_recovery_key_only_the_erase_path_is_offered(self):
        self.vault.lock()
        dialog = self.make_dialog()
        self.assertFalse(hasattr(dialog, "code_input"),
                         "there is nothing to redeem, so do not ask for a code")
        self.assertIsNotNone(dialog.erase_input)
        self.assertFalse(dialog.btn_erase.isEnabled())

    def test_erase_requires_typing_the_confirmation_word(self):
        self.vault.lock()
        dialog = self.make_dialog()
        for text in ("", "erase everything", "DELETE", "ERAS"):
            dialog.erase_input.setText(text)
            self.assertFalse(dialog.btn_erase.isEnabled(), repr(text))
        for text in ("ERASE", "erase", "  Erase  "):
            dialog.erase_input.setText(text)
            self.assertTrue(dialog.btn_erase.isEnabled(), repr(text))

    def test_erase_wipes_every_vault_file(self):
        self.vault.create_recovery_key()
        self.vault.lock()
        self.silence_modals()

        dialog = self.make_dialog()
        dialog.erase_input.setText("ERASE")
        dialog._do_erase()

        self.assertEqual(dialog.outcome, "erased")
        self.assertFalse(self.vault.is_setup)
        for path in (vault_mod.VAULT_FILE, vault_mod.SALT_FILE,
                     vault_mod.HASH_FILE, vault_mod.KEY_FILE,
                     vault_mod.RECOVERY_FILE):
            self.assertFalse(path.exists(), path.name)

    def test_declining_the_erase_confirmation_keeps_the_vault(self):
        self.vault.lock()
        self.silence_modals(answer=QMessageBox.StandardButton.Cancel)

        dialog = self.make_dialog()
        dialog.erase_input.setText("ERASE")
        dialog._do_erase()

        self.assertIsNone(dialog.outcome)
        self.assertTrue(self.vault.is_setup)
        self.assertTrue(Vault().unlock(PASSWORD))


class RecoveryKeyDialogTests(_RecoveryUITestCase):

    def test_done_is_gated_on_confirming_the_key_is_saved(self):
        dialog = RecoveryKeyDialog(generate_recovery_code())
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.btn_done.isEnabled())
        dialog.chk_saved.setChecked(True)
        self.assertTrue(dialog.btn_done.isEnabled())

    def test_the_code_is_shown_in_readable_groups(self):
        code = generate_recovery_code()
        dialog = RecoveryKeyDialog(code.replace("-", "").lower())
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.code, format_recovery_code(code))

    def test_saving_to_a_file_writes_the_code_and_ticks_the_box(self):
        code = generate_recovery_code()
        dialog = RecoveryKeyDialog(code)
        self.addCleanup(dialog.deleteLater)
        target = self.root / "key.txt"

        from PyQt6.QtWidgets import QFileDialog
        self.addCleanup(setattr, QFileDialog, "getSaveFileName",
                        QFileDialog.getSaveFileName)
        QFileDialog.getSaveFileName = staticmethod(
            lambda *a, **k: (str(target), ""))

        dialog._save()
        self.assertIn(dialog.code, target.read_text(encoding="utf-8"))
        self.assertTrue(dialog.chk_saved.isChecked())

    def test_copy_puts_the_code_on_the_clipboard(self):
        code = generate_recovery_code()
        dialog = RecoveryKeyDialog(code)
        self.addCleanup(dialog.deleteLater)
        dialog._copy()
        self.assertEqual(QApplication.clipboard().text(), dialog.code)


class OfferRecoveryKeyTests(_RecoveryUITestCase):

    def setUp(self):
        super().setUp()
        self.shown = []
        real_exec = RecoveryKeyDialog.exec

        def capture(dialog_self):
            self.shown.append(dialog_self.code)
            return QDialog.DialogCode.Accepted

        self.addCleanup(setattr, RecoveryKeyDialog, "exec", real_exec)
        RecoveryKeyDialog.exec = capture

    def test_creating_a_key_shows_it_once(self):
        self.silence_modals()
        self.assertTrue(offer_recovery_key(self.vault, prompt=False))
        self.assertEqual(len(self.shown), 1)
        self.assertTrue(self.vault.has_recovery_key)
        self.vault.lock()
        self.assertTrue(Vault().unlock_with_recovery_key(self.shown[0]))

    def test_declining_the_prompt_creates_nothing(self):
        self.silence_modals(answer=QMessageBox.StandardButton.No)
        self.assertFalse(offer_recovery_key(self.vault, prompt=True))
        self.assertFalse(self.vault.has_recovery_key)
        self.assertEqual(self.shown, [])

    def test_replacing_a_key_revokes_the_previous_one(self):
        self.silence_modals()
        offer_recovery_key(self.vault, prompt=False)
        first = self.shown[-1]
        offer_recovery_key(self.vault, prompt=False)
        second = self.shown[-1]
        self.assertNotEqual(first, second)
        self.vault.lock()
        self.assertFalse(Vault().unlock_with_recovery_key(first))
        self.assertTrue(Vault().unlock_with_recovery_key(second))

    def test_a_vault_that_cannot_be_upgraded_reports_instead_of_crashing(self):
        self.silence_modals()
        warned = []
        self.addCleanup(setattr, QMessageBox, "warning", QMessageBox.warning)
        QMessageBox.warning = staticmethod(lambda *a, **k: warned.append(a))

        vault_mod.KEY_FILE.unlink(missing_ok=True)
        self.vault._dek = None
        self.assertFalse(offer_recovery_key(self.vault, prompt=False))
        self.assertTrue(warned, "the user should be told why, not left guessing")
        self.assertEqual(self.shown, [])


class MainWindowRecoveryMenuTests(_RecoveryUITestCase):

    def test_the_file_menu_exposes_the_recovery_key_action(self):
        from app.main_window import MainWindow

        win = MainWindow(self.vault)
        self.addCleanup(win.deleteLater)
        file_menu = next(a.menu() for a in win.menuBar().actions()
                         if a.menu() and a.menu().title() == "&File")
        labels = [a.text() for a in file_menu.actions()]
        self.assertIn("&Recovery Key...", labels)
        self.assertTrue(hasattr(win, "_manage_recovery_key"))


if __name__ == "__main__":
    unittest.main()
