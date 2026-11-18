"""The Tier 3 features, joined up to the window, the vault and the dialogs.

Each feature is tested on its own elsewhere. What is checked here is that
marking a tenant reaches the sessions already open, that the marking survives
a restart, and that the connection dialog can say what a machine is.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget

from app import guardrails, vault as vault_mod
from app.connection_manager import ConnectionDialog
from app.side_panel import SidePanel
from app.vault import Vault

_APP = QApplication.instance() or QApplication([])


class FakeVault:
    """Enough vault for a dialog to open against."""

    connections = []
    snippets = []
    settings = {}

    def get_connection(self, _conn_id):
        return None


class ProductionTenantTests(unittest.TestCase):
    """Marking a tenant is saved, and every open session hears about it."""

    def window(self, tenants=()):
        from app.main_window import MainWindow
        saved = {}
        workspace = SimpleNamespace(
            seen=[],
            set_production_tenants=lambda names: workspace.seen.append(list(names)),
        )
        panel = SimpleNamespace(
            seen=[],
            set_production_tenants=lambda names: panel.seen.append(list(names)),
        )
        stub = SimpleNamespace(
            _production_tenants=guardrails.normalize_production_tenants(tenants),
            vault=SimpleNamespace(update_settings=saved.update),
            tabs=SimpleNamespace(count=lambda: 1, widget=lambda _index: workspace),
            side_panel=panel,
            status=SimpleNamespace(showMessage=lambda *_args: None),
        )
        return (MainWindow._set_tenant_production.__get__(stub), stub, saved,
                workspace, panel)

    def test_marking_a_tenant_saves_it(self):
        mark, stub, saved, _workspace, _panel = self.window()
        mark("Acme Ltd", True)
        self.assertEqual(saved, {"production_tenants": ["Acme Ltd"]})
        self.assertEqual(stub._production_tenants, ["Acme Ltd"])

    def test_open_sessions_and_the_sidebar_are_told(self):
        mark, _stub, _saved, workspace, panel = self.window()
        mark("Acme Ltd", True)
        self.assertEqual(workspace.seen, [["Acme Ltd"]])
        self.assertEqual(panel.seen, [["Acme Ltd"]])

    def test_unmarking_takes_it_off_again(self):
        mark, stub, saved, _workspace, _panel = self.window(["Acme Ltd"])
        mark("Acme Ltd", False)
        self.assertEqual(saved, {"production_tenants": []})
        self.assertEqual(stub._production_tenants, [])

    def test_marking_what_is_already_marked_writes_nothing(self):
        mark, _stub, saved, workspace, _panel = self.window(["Acme Ltd"])
        mark("Acme Ltd", True)
        self.assertEqual(saved, {})
        self.assertEqual(workspace.seen, [])

    def test_a_blank_tenant_is_ignored(self):
        mark, _stub, saved, _workspace, _panel = self.window()
        mark("   ", True)
        self.assertEqual(saved, {})


class SnippetExecutionTests(unittest.TestCase):
    """A snippet with {host} in it must not reach the shell with braces."""

    def window(self, widget):
        from app.main_window import MainWindow
        # A real widget, because the prompt is parented to the window.
        stub = QWidget()
        self.addCleanup(stub.deleteLater)
        stub.tabs = SimpleNamespace(currentWidget=lambda: widget)
        stub.status = SimpleNamespace(showMessage=lambda *_args: None)
        return MainWindow._execute_snippet.__get__(stub)

    def widget(self):
        pane = SimpleNamespace(connection_info={"host": "10.0.0.5",
                                                "username": "admin"},
                               remote_cwd="/srv")
        widget = SimpleNamespace(ran=[], focused_terminal=lambda: pane)
        widget.run_command = widget.ran.append
        return widget

    def test_a_plain_snippet_runs_as_written(self):
        widget = self.widget()
        self.window(widget)("uptime")
        self.assertEqual(widget.ran, ["uptime"])

    def test_the_session_is_substituted_before_it_runs(self):
        widget = self.widget()
        self.window(widget)("ssh {user}@{host}")
        self.assertEqual(widget.ran, ["ssh admin@10.0.0.5"])

    def test_a_shell_variable_is_left_alone(self):
        widget = self.widget()
        self.window(widget)("echo ${HOME}")
        self.assertEqual(widget.ran, ["echo ${HOME}"])

    def test_backing_out_of_the_prompt_runs_nothing(self):
        from app.command_palette import PlaceholderDialog
        from unittest import mock
        widget = self.widget()
        with mock.patch.object(PlaceholderDialog, "exec", return_value=0):
            self.window(widget)("tail {file}")
        self.assertEqual(widget.ran, [])


class DashboardEntryTests(unittest.TestCase):
    def test_the_graph_can_ask_for_a_broadcast_session(self):
        from app.dashboard import DashboardWidget
        dashboard = DashboardWidget([], {})
        self.addCleanup(dashboard.deleteLater)
        seen = []
        dashboard.broadcast_tenant_requested.connect(seen.append)
        dashboard.broadcast_tenant_requested.emit("Acme")
        self.assertEqual(seen, ["Acme"])


class VaultSettingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="rpmote-tier3-"))
        cls.previous = vault_mod.VAULT_DIR
        vault_mod.use_vault_directory(cls.dir)

    @classmethod
    def tearDownClass(cls):
        vault_mod.use_vault_directory(cls.previous)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def fresh_vault(self):
        for path in (vault_mod.VAULT_FILE, vault_mod.SALT_FILE,
                     vault_mod.HASH_FILE, vault_mod.KEY_FILE):
            if path.exists():
                path.unlink()
        vault = Vault()
        vault.setup("test-password-123")
        return vault

    def test_the_marking_survives_a_restart(self):
        vault = self.fresh_vault()
        vault.update_settings({"production_tenants": ["Acme Ltd"]})

        reopened = Vault()
        self.assertTrue(reopened.unlock("test-password-123"))
        self.assertEqual(reopened.settings["production_tenants"], ["Acme Ltd"])

    def test_an_older_vault_gets_the_setting_when_it_is_opened(self):
        self.fresh_vault()
        reopened = Vault()
        self.assertTrue(reopened.unlock("test-password-123"))
        self.assertEqual(reopened.settings["production_tenants"], [])


class ConnectionEnvironmentTests(unittest.TestCase):
    def dialog(self, connection=None):
        dialog = ConnectionDialog(connection=connection, parent=None,
                                  vault=FakeVault())
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_a_new_connection_says_nothing_about_its_environment(self):
        self.assertEqual(self.dialog().environment.currentData(), "")

    def test_the_saved_choice_comes_back(self):
        dialog = self.dialog({"name": "db", "host": "10.0.0.1",
                              "protocol": "ssh", "environment": "production"})
        self.assertEqual(dialog.environment.currentData(), "production")

    def test_a_value_from_somewhere_else_falls_back(self):
        dialog = self.dialog({"name": "db", "host": "10.0.0.1",
                              "protocol": "ssh", "environment": "prod"})
        self.assertEqual(dialog.environment.currentData(), "")

    def test_the_dialog_offers_exactly_the_environments_that_are_understood(self):
        dialog = self.dialog()
        offered = [dialog.environment.itemData(index)
                   for index in range(dialog.environment.count())]
        self.assertEqual(offered, list(guardrails.ENVIRONMENTS))


class SidePanelTests(unittest.TestCase):
    def panel(self):
        panel = SidePanel()
        self.addCleanup(panel.deleteLater)
        return panel

    def test_the_panel_remembers_which_tenants_are_production(self):
        panel = self.panel()
        panel.set_production_tenants(["Acme", "", None])
        self.assertEqual(panel._production_tenants, ["Acme"])

    def test_it_can_ask_for_a_broadcast_and_a_marking(self):
        panel = self.panel()
        marked, broadcast = [], []
        panel.production_tenant_toggled.connect(
            lambda tenant, on: marked.append((tenant, on)))
        panel.broadcast_tenant_requested.connect(broadcast.append)
        panel.production_tenant_toggled.emit("Acme", True)
        panel.broadcast_tenant_requested.emit("Acme")
        self.assertEqual(marked, [("Acme", True)])
        self.assertEqual(broadcast, ["Acme"])


if __name__ == "__main__":
    unittest.main()
