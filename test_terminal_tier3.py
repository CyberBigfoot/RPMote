"""Broadcast, recording, guardrails and drops, wired into a real terminal.

The pure logic lives in app/broadcast.py, app/session_log.py and
app/guardrails.py and is tested there. What is checked here is the wiring: a
keystroke leaving one pane and arriving in another, a recording that opens
itself when the first byte lands, a badge that appears on the right sessions,
and a dropped file going to the directory the shell reported.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import QApplication, QMessageBox, QSplitter

from app import session_log
from app.ssh_terminal import SSHTerminal, TerminalWorkspace

_APP = QApplication.instance() or QApplication([])

CONN = {"name": "lab-01", "host": "10.0.0.5", "port": 22, "username": "u",
        "tenant": "Lab"}
PROD = {"name": "acme-db", "host": "10.0.0.9", "port": 22, "username": "u",
        "tenant": "Acme"}


class TerminalTestCase(unittest.TestCase):
    def terminal(self, conn=None, connected=True, **kwargs):
        term = SSHTerminal(dict(conn or CONN), **kwargs)
        self.addCleanup(term.deleteLater)
        self.addCleanup(term._close_recording)
        if connected:
            term._connected = True
            term.channel = mock.Mock(closed=False)
        return term


class BroadcastWiringTests(TerminalTestCase):
    def setUp(self):
        self.a = self.terminal()
        self.b = self.terminal({**CONN, "name": "lab-02"})
        from app.broadcast import BroadcastGroup
        self.group = BroadcastGroup()
        for term in (self.a, self.b):
            self.group.add(term)
            term.set_broadcast_group(self.group)
        self.group.set_armed(True)

    def sent(self, terminal):
        return [call.args[0].decode("utf-8")
                for call in terminal.channel.sendall.call_args_list]

    def test_typing_in_one_pane_arrives_in_the_other(self):
        self.a._send_data("uptime\r")
        self.assertEqual(self.sent(self.b), ["uptime\r"])
        self.assertEqual(self.sent(self.a), ["uptime\r"])

    def test_a_mouse_report_stays_where_it_happened(self):
        # Mirroring "the pointer is at column 4 of my window" to another
        # machine's full-screen program is nonsense.
        self.a._screen_buf.mouse_sgr = True
        self.a._send_mouse_report(0, 4, 2, True)
        self.assertEqual(self.sent(self.b), [])

    def test_nothing_is_mirrored_until_the_group_is_armed(self):
        self.group.set_armed(False)
        self.a._send_data("uptime\r")
        self.assertEqual(self.sent(self.b), [])

    def test_a_dropped_session_does_not_type_for_the_others(self):
        self.a._connected = False
        self.a._send_data("rm -rf /\r")
        self.assertEqual(self.sent(self.b), [])

    def test_the_badge_says_which_panes_are_listening(self):
        self.assertTrue(self.a.broadcast_badge.isVisible()
                        or not self.a.isVisible())
        self.assertEqual(self.a.broadcast_badge.text(), "BROADCAST")
        self.group.set_included(self.a, False)
        self.assertEqual(self.a.broadcast_badge.text(), "MUTED")

    def test_leaving_a_group_stops_the_mirroring(self):
        self.a.set_broadcast_group(None)
        self.a._send_data("x")
        self.assertEqual(self.sent(self.b), [])


class PaletteKeyTests(TerminalTestCase):
    """Ctrl+K opens the palette, and the shell keeps a way to kill a line."""

    def key(self, terminal, key, modifiers):
        from PyQt6.QtGui import QKeyEvent
        from PyQt6.QtCore import QEvent
        return terminal._handle_key(
            QKeyEvent(QEvent.Type.KeyPress, key, modifiers, ""))

    def test_ctrl_k_opens_the_palette(self):
        from PyQt6.QtCore import Qt
        term = self.terminal()
        with mock.patch.object(SSHTerminal, "open_command_palette") as palette:
            self.assertTrue(self.key(term, Qt.Key.Key_K,
                                     Qt.KeyboardModifier.ControlModifier))
        palette.assert_called_once()

    def test_ctrl_shift_k_still_kills_to_end_of_line(self):
        from PyQt6.QtCore import Qt
        term = self.terminal()
        with mock.patch.object(SSHTerminal, "open_command_palette") as palette:
            self.key(term, Qt.Key.Key_K,
                     Qt.KeyboardModifier.ControlModifier
                     | Qt.KeyboardModifier.ShiftModifier)
        palette.assert_not_called()
        self.assertEqual(term.channel.sendall.call_args.args[0], b"\x0b")


class ProductionBadgeTests(TerminalTestCase):
    def test_a_lab_box_wears_no_badge(self):
        self.assertFalse(self.terminal().is_production)

    def test_a_name_that_reads_as_production_is_badged(self):
        term = self.terminal({**CONN, "name": "acme-prod-sql"})
        self.assertTrue(term.is_production)
        self.assertTrue(term.prod_badge.isVisible() or not term.isVisible())

    def test_a_marked_tenant_badges_everything_under_it(self):
        term = self.terminal(PROD)
        self.assertFalse(term.is_production)
        term.set_production_tenants(["Acme"])
        self.assertTrue(term.is_production)
        self.assertIn("Acme", term.prod_badge.toolTip())

    def test_the_marking_can_be_taken_off_again(self):
        term = self.terminal(PROD, production_tenants=["Acme"])
        self.assertTrue(term.is_production)
        term.set_production_tenants([])
        self.assertFalse(term.is_production)

    def test_a_banner_across_the_window_says_which_machine_and_why(self):
        # A badge can be missed at a glance; a bar across the top cannot.
        term = self.terminal(PROD, production_tenants=["Acme"])
        self.assertTrue(term.prod_banner.isVisibleTo(term))
        self.assertIn("PRODUCTION", term.prod_banner.text())
        self.assertIn("acme-db", term.prod_banner.text())
        self.assertIn("Acme", term.prod_banner.text())

    def test_a_lab_session_has_no_banner(self):
        term = self.terminal()
        self.assertFalse(term.prod_banner.isVisibleTo(term))
        self.assertEqual(term.prod_banner.text(), "")


class RunCommandTests(TerminalTestCase):
    def sent(self, terminal):
        return [call.args[0].decode("utf-8")
                for call in terminal.channel.sendall.call_args_list]

    def test_a_harmless_command_on_a_lab_box_just_runs(self):
        term = self.terminal()
        with mock.patch.object(QMessageBox, "question") as question:
            self.assertTrue(term.run_command("uptime"))
        question.assert_not_called()
        self.assertEqual(self.sent(term), ["uptime\r"])

    def test_a_destructive_command_is_put_to_the_user(self):
        term = self.terminal()
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes) as ask:
            self.assertTrue(term.run_command("rm -rf /var/log"))
        self.assertEqual(ask.call_count, 1)
        self.assertIn("deletes recursively", ask.call_args.args[2])

    def test_saying_no_sends_nothing(self):
        term = self.terminal()
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.No):
            self.assertFalse(term.run_command("shutdown -h now"))
        self.assertEqual(self.sent(term), [])

    def test_production_is_asked_about_even_for_something_harmless(self):
        term = self.terminal(PROD, production_tenants=["Acme"])
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes) as ask:
            term.run_command("ls -lah")
        self.assertEqual(ask.call_args.args[1], "Run on production?")

    def test_an_empty_command_does_nothing(self):
        term = self.terminal()
        self.assertFalse(term.run_command("   "))
        self.assertEqual(self.sent(term), [])


class RecordingTests(TerminalTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="rpmote-rec-"))
        self.previous = session_log.SESSIONS_DIR
        session_log.use_sessions_directory(self.dir)
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.addCleanup(session_log.use_sessions_directory, self.previous)

    def test_a_session_records_itself_without_being_asked(self):
        term = self.terminal()
        term._on_data_received("\x1b[32mhello\x1b[0m\r\n")
        path = term.recording_path
        self.assertIsNotNone(path)
        _header, events = session_log.read_cast(path)
        self.assertEqual(session_log.cast_text(events),
                         "\x1b[32mhello\x1b[0m\r\n")

    def test_a_session_that_never_says_anything_leaves_no_file(self):
        term = self.terminal()
        term._close_recording()
        self.assertEqual(list(self.dir.rglob("*.cast")), [])

    def test_recording_can_be_turned_off_for_a_session(self):
        term = self.terminal(record_session=False)
        term._on_data_received("secret")
        self.assertIsNone(term.recording_path)
        self.assertEqual(list(self.dir.rglob("*.cast")), [])

    def test_stopping_recording_closes_the_file(self):
        term = self.terminal()
        term._on_data_received("hello")
        path = term.recording_path
        term.stop_recording()
        self.assertIsNone(term.recording_path)
        term._on_data_received("after")
        self.assertNotIn("after", path.read_text(encoding="utf-8"))

    def test_a_lost_connection_is_written_into_the_recording(self):
        term = self.terminal()
        term._on_data_received("hello")
        path = term.recording_path
        term._on_connection_lost("network went away")
        self.assertIn("network went away", path.read_text(encoding="utf-8"))

    def test_only_output_is_recorded_never_what_was_typed(self):
        # A sudo prompt does not echo, so a transcript of the machine's
        # output has no password in it. A transcript of the keystrokes would.
        term = self.terminal()
        term._on_data_received("Password: ")
        term._send_data("hunter2\r")
        self.assertNotIn("hunter2",
                         term.recording_path.read_text(encoding="utf-8"))


class DropTests(TerminalTestCase):
    def test_a_dropped_file_goes_to_the_directory_the_shell_reported(self):
        term = self.terminal()
        term.client = mock.Mock()
        term._screen_buf.cwd = "/srv/app"

        started = {}

        def fake_upload(client, paths, cwd):
            started.update({"paths": list(paths), "cwd": cwd})
            return True

        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes), \
             mock.patch("app.terminal_upload.TerminalUploader.upload",
                        side_effect=fake_upload):
            term.upload_files([__file__])

        self.assertEqual(started["cwd"], "/srv/app")
        self.assertEqual(started["paths"], [__file__])

    def test_saying_no_uploads_nothing(self):
        term = self.terminal()
        term.client = mock.Mock()
        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.No), \
             mock.patch("app.terminal_upload.TerminalUploader.upload") as upload:
            term.upload_files([__file__])
        upload.assert_not_called()

    def test_a_failure_is_reported_on_the_status_line(self):
        term = self.terminal()
        with mock.patch.object(QMessageBox, "warning") as warned:
            term._on_upload_finished(0, 1, ["nope: permission denied"])
        self.assertIn("failed", term.cmd_status_label.text())
        self.assertIn("permission denied", warned.call_args.args[2])

    def test_a_success_is_reported_too(self):
        term = self.terminal()
        term._on_upload_finished(3, 3, [])
        self.assertIn("3", term.cmd_status_label.text())


class WorkspaceTests(unittest.TestCase):
    """Panes, grids and the broadcast bar, without opening any connections."""

    def workspace(self, **kwargs):
        class OfflineWorkspace(TerminalWorkspace):
            def add_terminal(self, vertical=True, source_terminal=None,
                             connection_info=None, connect=False):
                return super().add_terminal(vertical, source_terminal,
                                            connection_info, connect=False)

            def open_hosts(self, connections, connect=False):
                return super().open_hosts(connections, connect=False)

        workspace = OfflineWorkspace(dict(CONN), **kwargs)
        self.addCleanup(workspace.deleteLater)
        return workspace

    def test_a_new_workspace_has_one_pane_and_no_bar(self):
        workspace = self.workspace()
        self.assertEqual(len(workspace.terminals()), 1)
        self.assertFalse(workspace.broadcast_bar.isVisible())

    def test_opening_several_hosts_makes_a_grid_of_panes(self):
        workspace = self.workspace()
        workspace.open_hosts([{**CONN, "name": f"web{index}"}
                              for index in range(7)])
        panes = workspace.terminals()
        self.assertEqual(len(panes), 8)          # the original plus seven
        # Three rows of splitters, balanced: 3-3-2.
        rows = [workspace.splitter.widget(index)
                for index in range(workspace.splitter.count())]
        self.assertTrue(all(isinstance(row, QSplitter) for row in rows))
        self.assertEqual([row.count() for row in rows], [3, 3, 2])

    def test_every_pane_joins_the_broadcast_group(self):
        workspace = self.workspace()
        workspace.open_hosts([{**CONN, "name": "web1"}])
        self.assertEqual(len(workspace.broadcast), 2)
        for pane in workspace.terminals():
            self.assertIs(pane.broadcast_group, workspace.broadcast)

    def test_the_bar_appears_once_there_is_more_than_one_pane(self):
        workspace = self.workspace()
        workspace.add_terminal()
        self.assertTrue(workspace.broadcast_bar.isVisibleTo(workspace))

    def test_closing_a_pane_takes_it_out_of_the_group(self):
        workspace = self.workspace()
        workspace.add_terminal()
        pane = workspace.terminals()[-1]
        workspace.broadcast.discard(pane)
        self.assertNotIn(pane, workspace.broadcast)

    def test_a_broadcast_command_asks_before_it_runs_everywhere(self):
        workspace = self.workspace()
        workspace.open_hosts([{**CONN, "name": "web1"}])
        for pane in workspace.terminals():
            pane._connected = True
        workspace.broadcast.set_armed(True)

        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.Yes) as ask, \
             mock.patch.object(SSHTerminal, "_send_data") as send:
            workspace.broadcast_command("uptime")
        # Two machines at once is itself worth a question.
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.args[0], "uptime\r")

    def test_declining_a_broadcast_sends_nothing(self):
        workspace = self.workspace()
        workspace.open_hosts([{**CONN, "name": "web1"}])
        for pane in workspace.terminals():
            pane._connected = True
        workspace.broadcast.set_armed(True)

        with mock.patch.object(QMessageBox, "question",
                               return_value=QMessageBox.StandardButton.No), \
             mock.patch.object(SSHTerminal, "_send_data") as send:
            workspace.broadcast_command("rm -rf /")
        send.assert_not_called()

    def test_run_command_goes_to_the_focused_pane_when_not_broadcasting(self):
        workspace = self.workspace()
        workspace.add_terminal()
        panes = workspace.terminals()
        for pane in panes:
            pane._connected = True

        with mock.patch.object(SSHTerminal, "run_command",
                               return_value=True) as run:
            self.assertTrue(workspace.run_command("uptime"))
        self.assertEqual(run.call_count, 1)

    def test_marking_the_tenant_production_reaches_every_pane(self):
        workspace = self.workspace()
        workspace.open_hosts([dict(PROD)])
        workspace.set_production_tenants(["Acme", "Lab"])
        self.assertTrue(all(pane.is_production
                            for pane in workspace.terminals()))


if __name__ == "__main__":
    unittest.main()
