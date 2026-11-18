"""Tenant selection must preserve identity and the tenant's current color."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from app.connection_manager import ConnectionDialog

APP = QApplication.instance() or QApplication(["rpmote-tests"])


class TenantSelectionTests(unittest.TestCase):
    def dialog(self, **kwargs):
        vault = SimpleNamespace(
            connections=[{"tenant": "Acme", "tenant_color": "#111111"},
                         {"tenant": "Beta", "tenant_color": "#222222"}],
            settings={"tenant_colors": {"Acme": "#00aaff", "Beta": "#ff8800"}})
        dialog = ConnectionDialog(vault=vault, **kwargs)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def save(self, dialog):
        dialog.name_input.setText("Server A")
        dialog.host_input.setText("example.invalid")
        dialog._save()
        return dialog.get_result()

    def test_dropdown_offers_existing_tenants_and_starts_empty(self):
        dialog = self.dialog()
        self.assertEqual(dialog.tenant_input.text(), "")
        self.assertEqual([dialog.tenant_combo.itemText(i) for i in range(2)], ["Acme", "Beta"])
        self.assertTrue(dialog.tenant_combo.isEditable())
        self.assertEqual(dialog.tenant_combo.completer().caseSensitivity(),
                         Qt.CaseSensitivity.CaseInsensitive)

    def test_existing_tenant_uses_current_color_not_stale_connection_color(self):
        dialog = self.dialog(default_tenant="Acme")
        self.assertEqual(self.save(dialog)["tenant_color"], "#00aaff")

    def test_editing_an_old_connection_keeps_the_current_tenant_color(self):
        dialog = self.dialog(connection={"tenant": "Acme", "tenant_color": "#111111",
                                         "name": "old", "host": "host", "protocol": "ssh"})
        self.assertEqual(self.save(dialog)["tenant_color"], "#00aaff")

    def test_typing_an_existing_name_reuses_its_spelling_and_color(self):
        dialog = self.dialog()
        dialog.tenant_input.setText(" acME ")
        saved = self.save(dialog)
        self.assertEqual((saved["tenant"], saved["tenant_color"]), ("Acme", "#00aaff"))

    def test_picking_another_tenant_updates_color(self):
        dialog = self.dialog(default_tenant="Acme")
        dialog.tenant_combo.setCurrentText("Beta")
        self.assertEqual(self.save(dialog)["tenant_color"], "#ff8800")

    def test_custom_color_belongs_to_the_tenant_it_was_chosen_for(self):
        dialog = self.dialog(default_tenant="Acme")
        with patch("app.connection_manager.QColorDialog.getColor", return_value=QColor("#123456")):
            dialog._pick_tenant_color()
        dialog.tenant_combo.setCurrentText("Beta")
        self.assertEqual(dialog._tenant_color, "#ff8800")
        dialog.tenant_combo.setCurrentText("Acme")
        self.assertEqual(self.save(dialog)["tenant_color"], "#123456")

    def test_new_name_is_still_allowed(self):
        dialog = self.dialog()
        dialog.tenant_input.setText("New Customer")
        self.assertEqual(self.save(dialog)["tenant"], "New Customer")

    def test_tenant_then_name_keyboard_order(self):
        dialog = self.dialog()
        dialog.show()
        APP.processEvents()
        self.assertIs(dialog.focusWidget(), dialog.tenant_combo)
        QTest.keyClick(dialog.tenant_combo, Qt.Key.Key_Tab)
        self.assertIs(dialog.focusWidget(), dialog.name_input)
        dialog.close()
