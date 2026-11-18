"""Tests for viewing the credentials saved in the vault."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLineEdit

import app.saved_passwords as spmod
from app.connection_manager import ConnectionDialog
from app.saved_passwords import (
    MASK,
    REVEAL_TIMEOUT_MS,
    SavedPasswordsDialog,
    attach_reveal_toggle,
    confirm_master_password,
)

_APP = QApplication.instance() or QApplication([])

SECRET_COLUMN = 7


class FakeVault:
    """Stands in for a real unlocked Vault."""

    MASTER = "correct horse"

    def __init__(self, connections=None):
        self._connections = connections if connections is not None else [
            {
                "id": "a1", "name": "prod-web", "tenant": "Contoso",
                "protocol": "ssh", "host": "10.0.0.5", "port": 22,
                "username": "root", "password": "hunter2",
            },
            {
                "id": "b2", "name": "backup-nas", "tenant": "Contoso",
                "protocol": "synology", "host": "nas.local", "port": 5001,
                "username": "admin", "password": "s3cr3t",
            },
            {
                "id": "c3", "name": "no-secret", "tenant": "Default",
                "protocol": "rdp", "host": "pc.local", "port": 3389,
                "username": "user", "password": "",
            },
            {
                "id": "d4", "name": "fabrikam-tenant", "tenant": "Fabrikam",
                "protocol": "azure_ps", "host": "", "port": 443,
                "username": "", "password": "",
                "azure_client_id": "app-guid", "azure_client_secret": "sp-secret",
            },
        ]
        self.verify_calls = []

    @property
    def connections(self):
        return self._connections

    def verify_password(self, password):
        self.verify_calls.append(password)
        return password == self.MASTER


class _PromptStub:
    """Answer the master-password challenge without a UI."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = 0
        self._saved = None

    def __enter__(self):
        self._saved = spmod.QInputDialog.getText

        def fake_get_text(*args, **kwargs):
            self.asked += 1
            if not self.answers:
                return "", False
            value = self.answers.pop(0)
            return ("", False) if value is None else (value, True)

        spmod.QInputDialog.getText = staticmethod(fake_get_text)
        return self

    def __exit__(self, *exc):
        spmod.QInputDialog.getText = self._saved
        return False


class MasterPasswordChallengeTests(unittest.TestCase):
    def test_correct_password_passes(self):
        vault = FakeVault()
        with _PromptStub([FakeVault.MASTER]) as prompt:
            self.assertTrue(confirm_master_password(None, vault, "why"))
        self.assertEqual(prompt.asked, 1)

    def test_cancelling_refuses(self):
        vault = FakeVault()
        with _PromptStub([None]):
            self.assertFalse(confirm_master_password(None, vault, "why"))

    def test_three_wrong_attempts_then_refusal(self):
        vault = FakeVault()
        saved = spmod.QMessageBox.warning
        spmod.QMessageBox.warning = staticmethod(lambda *a, **k: None)
        try:
            with _PromptStub(["no", "nope", "wrong"]) as prompt:
                self.assertFalse(confirm_master_password(None, vault, "why"))
            self.assertEqual(prompt.asked, 3)
        finally:
            spmod.QMessageBox.warning = saved


class SavedPasswordsDialogTests(unittest.TestCase):
    def setUp(self):
        self.vault = FakeVault()
        self.dialog = SavedPasswordsDialog(self.vault)

    def tearDown(self):
        self.dialog.deleteLater()

    def _secret_text(self, row):
        return self.dialog.table.item(row, SECRET_COLUMN).text()

    def test_only_connections_with_a_stored_secret_are_listed(self):
        names = [r.name for r in self.dialog._visible_rows]
        self.assertEqual(names, ["prod-web", "backup-nas", "fabrikam-tenant"])

    def test_azure_client_secret_is_listed_as_its_own_row(self):
        row = self.dialog._visible_rows[2]
        self.assertEqual(row.label, "Azure client secret")
        self.assertEqual(row.value, "sp-secret")
        # the client ID stands in for the username on that row
        self.assertEqual(row.username, "app-guid")

    def test_secrets_start_masked(self):
        for index in range(len(self.dialog._visible_rows)):
            self.assertEqual(self._secret_text(index), MASK)

    def test_reveal_requires_the_master_password(self):
        with _PromptStub(["wrong-password"]):
            saved = spmod.QMessageBox.warning
            spmod.QMessageBox.warning = staticmethod(lambda *a, **k: None)
            try:
                self.dialog._toggle_row(0)
            finally:
                spmod.QMessageBox.warning = saved
        self.assertEqual(self._secret_text(0), MASK)
        self.assertEqual(self.dialog._revealed, set())

    def test_reveal_after_authenticating_shows_the_value(self):
        with _PromptStub([FakeVault.MASTER]):
            self.dialog._toggle_row(0)
        self.assertEqual(self._secret_text(0), "hunter2")
        self.assertTrue(self.dialog._hide_timer.isActive())

    def test_authentication_is_asked_once_per_dialog(self):
        with _PromptStub([FakeVault.MASTER]) as prompt:
            self.dialog._toggle_row(0)
            self.dialog._toggle_row(1)
        self.assertEqual(prompt.asked, 1)
        self.assertEqual(self._secret_text(1), "s3cr3t")

    def test_toggling_again_re_masks(self):
        with _PromptStub([FakeVault.MASTER]):
            self.dialog._toggle_row(0)
            self.dialog._toggle_row(0)
        self.assertEqual(self._secret_text(0), MASK)
        self.assertFalse(self.dialog._hide_timer.isActive())

    def test_hide_all_re_masks_everything(self):
        with _PromptStub([FakeVault.MASTER]):
            self.dialog.reveal_all.setChecked(True)
        self.assertEqual(self._secret_text(0), "hunter2")
        self.dialog._hide_all()
        for index in range(len(self.dialog._visible_rows)):
            self.assertEqual(self._secret_text(index), MASK)
        self.assertFalse(self.dialog.reveal_all.isChecked())

    def test_auto_hide_timer_fires_the_re_mask(self):
        with _PromptStub([FakeVault.MASTER]):
            self.dialog._toggle_row(0)
        self.assertEqual(self.dialog._hide_timer.interval(), REVEAL_TIMEOUT_MS)
        self.dialog._hide_timer.timeout.emit()   # simulate the timeout
        self.assertEqual(self._secret_text(0), MASK)

    def test_filtering_narrows_the_list_and_re_masks(self):
        with _PromptStub([FakeVault.MASTER]):
            self.dialog._toggle_row(0)
        self.dialog.search_input.setText("nas")
        self.assertEqual([r.name for r in self.dialog._visible_rows], ["backup-nas"])
        self.assertEqual(self.dialog._revealed, set())
        self.assertEqual(self._secret_text(0), MASK)

    def test_filter_matches_tenant_and_host_too(self):
        self.dialog.search_input.setText("fabrikam")
        self.assertEqual([r.name for r in self.dialog._visible_rows], ["fabrikam-tenant"])
        self.dialog.search_input.setText("10.0.0.5")
        self.assertEqual([r.name for r in self.dialog._visible_rows], ["prod-web"])

    def test_opening_pre_filtered_focuses_one_connection(self):
        dialog = SavedPasswordsDialog(self.vault, initial_filter="backup-nas")
        try:
            self.assertEqual([r.name for r in dialog._visible_rows], ["backup-nas"])
        finally:
            dialog.deleteLater()

    def test_closing_re_masks_everything(self):
        with _PromptStub([FakeVault.MASTER]):
            self.dialog._toggle_row(0)
        self.dialog.done(0)
        self.assertEqual(self._secret_text(0), MASK)

    def test_empty_vault_is_handled(self):
        dialog = SavedPasswordsDialog(FakeVault(connections=[]))
        try:
            self.assertEqual(dialog._visible_rows, [])
            self.assertIn("No saved passwords", dialog.status_label.text())
        finally:
            dialog.deleteLater()


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.vault = FakeVault()
        self.dialog = SavedPasswordsDialog(self.vault)

    def tearDown(self):
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.clear()
        self.dialog.deleteLater()

    def test_copy_requires_authentication_then_copies(self):
        with _PromptStub([FakeVault.MASTER]):
            self.dialog._copy_row(0)
        self.assertEqual(QApplication.clipboard().text(), "hunter2")

    def test_clipboard_is_cleared_only_if_still_ours(self):
        with _PromptStub([FakeVault.MASTER]):
            self.dialog._copy_row(0)
        QApplication.clipboard().setText("something the user copied since")
        self.dialog._clear_clipboard("hunter2")
        self.assertEqual(
            QApplication.clipboard().text(), "something the user copied since"
        )

    def test_clipboard_clear_removes_the_secret(self):
        with _PromptStub([FakeVault.MASTER]):
            self.dialog._copy_row(0)
        self.dialog._clear_clipboard("hunter2")
        self.assertEqual(QApplication.clipboard().text(), "")


class RevealToggleTests(unittest.TestCase):
    def test_toggle_flips_echo_mode_when_allowed(self):
        field = QLineEdit("secret")
        field.setEchoMode(QLineEdit.EchoMode.Password)
        action = attach_reveal_toggle(field, can_reveal=lambda: True)
        try:
            action.trigger()
            self.assertEqual(field.echoMode(), QLineEdit.EchoMode.Normal)
            action.trigger()
            self.assertEqual(field.echoMode(), QLineEdit.EchoMode.Password)
        finally:
            field.deleteLater()

    def test_refused_authorisation_keeps_the_field_masked(self):
        field = QLineEdit("secret")
        field.setEchoMode(QLineEdit.EchoMode.Password)
        action = attach_reveal_toggle(field, can_reveal=lambda: False)
        try:
            action.trigger()
            self.assertEqual(field.echoMode(), QLineEdit.EchoMode.Password)
        finally:
            field.deleteLater()

    def test_authorisation_is_only_asked_once(self):
        calls = []

        field = QLineEdit("secret")
        field.setEchoMode(QLineEdit.EchoMode.Password)

        def can_reveal():
            calls.append(1)
            return True

        action = attach_reveal_toggle(field, can_reveal=can_reveal)
        try:
            action.trigger()   # reveal
            action.trigger()   # hide
            action.trigger()   # reveal again
            self.assertEqual(len(calls), 1)
        finally:
            field.deleteLater()


class ConnectionDialogRevealTests(unittest.TestCase):
    def test_new_connection_reveals_without_a_challenge(self):
        """Nothing is saved yet, so there is no stored secret to protect."""
        dialog = ConnectionDialog(parent=None, vault=FakeVault())
        try:
            self.assertTrue(dialog._can_reveal_secret())
        finally:
            dialog.deleteLater()

    def test_editing_a_saved_connection_challenges_once(self):
        vault = FakeVault()
        conn = dict(vault.connections[0])
        dialog = ConnectionDialog(connection=conn, parent=None, vault=vault)
        try:
            with _PromptStub([FakeVault.MASTER]) as prompt:
                self.assertTrue(dialog._can_reveal_secret())
                self.assertTrue(dialog._can_reveal_secret())
            self.assertEqual(prompt.asked, 1)
        finally:
            dialog.deleteLater()

    def test_editing_refuses_on_a_bad_master_password(self):
        vault = FakeVault()
        conn = dict(vault.connections[0])
        dialog = ConnectionDialog(connection=conn, parent=None, vault=vault)
        saved = spmod.QMessageBox.warning
        spmod.QMessageBox.warning = staticmethod(lambda *a, **k: None)
        try:
            with _PromptStub(["nope", "nope", "nope"]):
                self.assertFalse(dialog._can_reveal_secret())
        finally:
            spmod.QMessageBox.warning = saved
            dialog.deleteLater()

    def test_password_field_starts_masked_when_editing(self):
        vault = FakeVault()
        conn = dict(vault.connections[0])
        dialog = ConnectionDialog(connection=conn, parent=None, vault=vault)
        try:
            self.assertEqual(dialog.password_input.text(), "hunter2")
            self.assertEqual(
                dialog.password_input.echoMode(), QLineEdit.EchoMode.Password
            )
        finally:
            dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
