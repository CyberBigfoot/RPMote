"""Picking the machines a broadcast session opens."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDialogButtonBox

from app.host_picker import (
    HostPickerDialog, group_by_tenant, shell_connections,
)

_APP = QApplication.instance() or QApplication([])

CONNECTIONS = [
    {"id": "1", "name": "acme-web01", "host": "10.0.0.1", "tenant": "Acme",
     "protocol": "ssh"},
    {"id": "2", "name": "acme-db01", "host": "10.0.0.2", "tenant": "Acme",
     "protocol": "ssh"},
    {"id": "3", "name": "lab-01", "host": "10.0.1.1", "tenant": "Lab",
     "protocol": "ssh"},
    {"id": "4", "name": "reception-pc", "host": "10.0.2.1", "tenant": "Acme",
     "protocol": "rdp"},
    {"id": "5", "name": "no-host", "tenant": "Acme", "protocol": "ssh"},
]


class FilteringTests(unittest.TestCase):
    def test_only_machines_a_shell_can_open_are_offered(self):
        names = [conn["name"] for conn in shell_connections(CONNECTIONS)]
        self.assertEqual(names, ["acme-web01", "acme-db01", "lab-01"])

    def test_tenants_come_out_in_order(self):
        grouped = group_by_tenant(shell_connections(CONNECTIONS))
        self.assertEqual(list(grouped), ["Acme", "Lab"])
        self.assertEqual(len(grouped["Acme"]), 2)


class PickerTests(unittest.TestCase):
    def picker(self, **kwargs):
        dialog = HostPickerDialog(CONNECTIONS, **kwargs)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def children(self, dialog):
        return [child for _parent, child in dialog._rows()]

    def test_it_groups_the_estate_by_tenant(self):
        dialog = self.picker()
        self.assertEqual(dialog.tree.topLevelItemCount(), 2)
        self.assertEqual(dialog.tree.topLevelItem(0).text(0), "Acme")

    def test_nothing_is_ticked_to_start_with(self):
        dialog = self.picker()
        self.assertEqual(dialog.chosen(), [])
        self.assertFalse(dialog.buttons.button(
            QDialogButtonBox.StandardButton.Open).isEnabled())

    def test_ticking_a_machine_selects_it(self):
        dialog = self.picker()
        self.children(dialog)[0].setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual([conn["name"] for conn in dialog.chosen()],
                         ["acme-web01"])
        self.assertTrue(dialog.buttons.button(
            QDialogButtonBox.StandardButton.Open).isEnabled())

    def test_ticking_a_tenant_takes_everything_under_it(self):
        dialog = self.picker()
        dialog.tree.topLevelItem(0).setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual([conn["name"] for conn in dialog.chosen()],
                         ["acme-web01", "acme-db01"])

    def test_select_all_and_none(self):
        dialog = self.picker()
        dialog._set_all(True)
        self.assertEqual(len(dialog.chosen()), 3)
        dialog._set_all(False)
        self.assertEqual(dialog.chosen(), [])

    def test_the_filter_hides_and_unhides(self):
        dialog = self.picker()
        dialog.filter.setText("db")
        visible = [child for child in self.children(dialog)
                   if not child.isHidden()]
        self.assertEqual(len(visible), 1)
        self.assertIn("db01", visible[0].text(0))
        dialog.filter.setText("")
        self.assertEqual(len([child for child in self.children(dialog)
                              if not child.isHidden()]), 3)

    def test_a_hidden_machine_is_not_quietly_selected(self):
        # Select-all means "everything I can see", not "everything at all".
        dialog = self.picker()
        dialog.filter.setText("lab")
        dialog._set_all(True)
        self.assertEqual([conn["name"] for conn in dialog.chosen()], ["lab-01"])

    def test_production_machines_are_marked_where_they_are_chosen(self):
        dialog = self.picker(production_tenants=["Acme"])
        acme = dialog.tree.topLevelItem(0)
        self.assertFalse(acme.child(0).icon(0).isNull())
        acme.setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual(len(dialog.production_chosen()), 2)
        self.assertIn("production", dialog.count_label.text())

    def test_a_preselection_arrives_already_ticked(self):
        dialog = self.picker(preselect=[CONNECTIONS[2]])
        self.assertEqual([conn["name"] for conn in dialog.chosen()], ["lab-01"])

    def test_broadcast_is_not_armed_just_because_panes_opened(self):
        self.assertFalse(self.picker().arm_immediately())


if __name__ == "__main__":
    unittest.main()
