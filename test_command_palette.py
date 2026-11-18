"""Ctrl+K: finding a command, filling it in, and seeing it before it runs."""

import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDialog

from app.command_palette import CommandPalette, PlaceholderDialog, palette_entries

_APP = QApplication.instance() or QApplication([])

CONN = {"name": "acme-web01", "host": "10.0.0.5", "port": 22,
        "username": "admin", "tenant": "Acme Ltd"}

ENTRIES = [
    {"name": "Restart nginx", "command": "systemctl restart nginx",
     "description": "bounce the web server", "category": "Snippet"},
    {"name": "Tail syslog", "command": "tail -f /var/log/syslog",
     "description": "follow the log", "category": "Logs"},
    {"name": "Ping this host", "command": "ping -c4 {host}",
     "description": "", "category": "Snippet"},
    {"name": "Service status", "command": "systemctl status {service}",
     "description": "", "category": "Snippet"},
]


class FakeVault:
    def __init__(self, snippets):
        self.snippets = snippets


class EntryTests(unittest.TestCase):
    def test_the_users_own_snippets_come_first(self):
        vault = FakeVault([{"name": "Mine", "command": "echo mine"}])
        entries = palette_entries(vault)
        self.assertEqual(entries[0]["name"], "Mine")
        self.assertEqual(entries[0]["source"], "snippet")
        self.assertTrue(any(entry["source"] == "library" for entry in entries))

    def test_a_snippet_with_no_command_is_skipped(self):
        vault = FakeVault([{"name": "Empty", "command": "   "}, "not a dict"])
        entries = palette_entries(vault, include_library=False)
        self.assertEqual(entries, [])

    def test_a_locked_vault_still_gives_the_library(self):
        class Locked:
            @property
            def snippets(self):
                raise RuntimeError("Vault is locked")

        self.assertTrue(palette_entries(Locked()))

    def test_no_vault_at_all_is_fine(self):
        self.assertTrue(palette_entries(None))


class PaletteTestCase(unittest.TestCase):
    def palette(self, entries=None, session=None):
        palette = CommandPalette(entries if entries is not None else ENTRIES,
                                 session=session)
        self.addCleanup(palette.deleteLater)
        return palette


class FilteringTests(PaletteTestCase):
    def test_everything_is_offered_before_anything_is_typed(self):
        palette = self.palette()
        self.assertEqual(palette.results_count, len(ENTRIES))

    def test_typing_narrows_it_to_what_was_meant(self):
        palette = self.palette()
        palette.search.setText("nginx")
        self.assertEqual(palette.current_entry()["name"], "Restart nginx")

    def test_a_command_can_be_found_by_what_it_runs(self):
        palette = self.palette()
        palette.search.setText("tail -f")
        self.assertEqual(palette.current_entry()["name"], "Tail syslog")

    def test_nothing_matching_says_so_rather_than_showing_a_stale_pick(self):
        palette = self.palette()
        palette.search.setText("zzzzzz")
        self.assertEqual(palette.results_count, 0)
        self.assertIsNone(palette.current_entry())
        self.assertIn("No command", palette.preview.text())


class PreviewTests(PaletteTestCase):
    def test_the_session_is_filled_in_before_you_press_enter(self):
        palette = self.palette(session={"conn": CONN, "cwd": "/srv"})
        palette.search.setText("ping")
        self.assertIn("ping -c4 10.0.0.5", palette.preview.text())

    def test_what_it_still_needs_is_named(self):
        palette = self.palette(session={"conn": CONN})
        palette.search.setText("service status")
        self.assertIn("Will ask for: service", palette.preview.text())

    def test_with_no_session_everything_is_still_asked_for(self):
        palette = self.palette()
        palette.search.setText("ping this")
        self.assertIn("Will ask for: host", palette.preview.text())


class ResolveTests(PaletteTestCase):
    def test_a_plain_command_comes_back_as_it_is(self):
        palette = self.palette()
        self.assertEqual(palette.resolve(ENTRIES[0]),
                         "systemctl restart nginx")

    def test_the_session_answers_what_it_can(self):
        palette = self.palette(session={"conn": CONN})
        self.assertEqual(palette.resolve(ENTRIES[2]), "ping -c4 10.0.0.5")

    def test_anything_left_over_is_asked_for(self):
        palette = self.palette(session={"conn": CONN})
        with mock.patch.object(PlaceholderDialog, "exec",
                               return_value=QDialog.DialogCode.Accepted), \
             mock.patch.object(PlaceholderDialog, "values",
                               return_value={"service": "nginx"}):
            self.assertEqual(palette.resolve(ENTRIES[3]),
                             "systemctl status nginx")

    def test_backing_out_of_the_prompt_runs_nothing(self):
        palette = self.palette(session={"conn": CONN})
        with mock.patch.object(PlaceholderDialog, "exec",
                               return_value=QDialog.DialogCode.Rejected):
            self.assertIsNone(palette.resolve(ENTRIES[3]))

    def test_leaving_a_box_empty_runs_nothing(self):
        palette = self.palette(session={"conn": CONN})
        with mock.patch.object(PlaceholderDialog, "exec",
                               return_value=QDialog.DialogCode.Accepted), \
             mock.patch.object(PlaceholderDialog, "values",
                               return_value={"service": "  "}):
            self.assertIsNone(palette.resolve(ENTRIES[3]))

    def test_a_hostname_that_could_chain_a_command_is_refused(self):
        palette = self.palette(session={"conn": {"host": "1.2.3.4; rm -rf /"}})
        self.assertIsNone(palette.resolve(ENTRIES[2]))
        self.assertIn("control characters", palette.preview.text())

    def test_choosing_announces_the_finished_command(self):
        palette = self.palette()
        chosen = []
        palette.command_chosen.connect(chosen.append)
        palette.search.setText("nginx")
        palette._accept_current()
        self.assertEqual(chosen, ["systemctl restart nginx"])


class FillCommandTests(unittest.TestCase):
    """The path a snippet takes when it is run from the panel, not the palette."""

    def test_a_plain_command_needs_no_asking(self):
        from app.command_palette import fill_command
        self.assertEqual(fill_command("uptime"), "uptime")

    def test_the_session_fills_in_what_it_knows(self):
        from app.command_palette import fill_command
        self.assertEqual(fill_command("ping -c4 {host}", {"conn": CONN}),
                         "ping -c4 10.0.0.5")

    def test_the_rest_is_asked_for(self):
        from app.command_palette import fill_command
        with mock.patch.object(PlaceholderDialog, "exec",
                               return_value=QDialog.DialogCode.Accepted), \
             mock.patch.object(PlaceholderDialog, "values",
                               return_value={"unit": "nginx"}):
            self.assertEqual(
                fill_command("systemctl status {unit} on {host}",
                             {"conn": CONN}),
                "systemctl status nginx on 10.0.0.5")

    def test_backing_out_runs_nothing(self):
        from app.command_palette import fill_command
        with mock.patch.object(PlaceholderDialog, "exec",
                               return_value=QDialog.DialogCode.Rejected):
            self.assertIsNone(fill_command("tail {file}"))

    def test_an_empty_command_is_nothing_to_run(self):
        from app.command_palette import fill_command
        self.assertIsNone(fill_command("   "))
        self.assertIsNone(fill_command(None))

    def test_a_session_value_that_could_chain_a_command_is_refused(self):
        from app.command_palette import fill_command
        self.assertIsNone(
            fill_command("ping {host}", {"conn": {"host": "1.2.3.4 && rm -rf /"}}))

    def test_the_context_comes_from_the_focused_pane(self):
        from app.command_palette import session_context
        pane = SimpleNamespace(connection_info=CONN, remote_cwd="/srv")
        workspace = SimpleNamespace(focused_terminal=lambda: pane)
        self.assertEqual(session_context(workspace),
                         {"conn": CONN, "cwd": "/srv"})

    def test_a_tab_with_no_terminal_offers_no_context(self):
        from app.command_palette import session_context
        self.assertEqual(session_context(SimpleNamespace(
            focused_terminal=lambda: None)), {})


class PlaceholderDialogTests(unittest.TestCase):
    def dialog(self, command, names, **kwargs):
        dialog = PlaceholderDialog(command, names, **kwargs)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_it_asks_for_exactly_what_is_missing(self):
        dialog = self.dialog("systemctl {action} {unit}", ["action", "unit"])
        self.assertEqual(sorted(dialog.values()), ["action", "unit"])

    def test_the_preview_updates_as_you_type(self):
        dialog = self.dialog("systemctl {action} nginx", ["action"])
        dialog._fields["action"].setText("restart")
        self.assertEqual(dialog.preview.text(), "systemctl restart nginx")

    def test_what_the_user_types_is_taken_at_its_word(self):
        # They are writing a command line; quoting is theirs to get right.
        dialog = self.dialog("grep {pattern} log", ["pattern"])
        dialog._fields["pattern"].setText("'error|warn'")
        self.assertEqual(dialog.rendered(), "grep 'error|warn' log")


if __name__ == "__main__":
    unittest.main()
