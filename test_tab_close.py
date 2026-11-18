"""Closing one session tab closes that tab and no other.

An SSH, SFTP or FTP session's cleanup disconnects it, and the disconnect
emits session_closed, which closes the session's tab. _close_tab used to
call removeTab(index) after that cleanup, by when *index* belonged to the
next tab: closing one session silently closed its neighbour too (still
connected, with no tab to reach it), so a few closes emptied the tab bar.

A closed tab's widget is deleted, too. removeTab() alone left every closed
session alive and hidden until the app exited.
"""

import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.vault as vault_mod

_TMP = Path(tempfile.mkdtemp(prefix="rpmote_tab_close_"))
vault_mod.use_vault_directory(_TMP)

from PyQt6 import sip
from PyQt6.QtCore import QEvent, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialog, QWidget

from app import main_window as mw
from app import windows_credentials
from app.web_browser import WebBrowserTab

_APP = QApplication.instance() or QApplication([])


def _flush_deletes():
    """Run the deleteLater()s queued so far, as the event loop would."""
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _wait_until(predicate, timeout=3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _APP.processEvents()
        _flush_deletes()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _Session(QWidget):
    """A session tab whose cleanup disconnects, and so closes, itself (as SSH does)."""

    session_closed = pyqtSignal()

    def __init__(self, name, emits_on_cleanup=True):
        super().__init__()
        self.connection_info = {"name": name, "protocol": "ssh", "host": name,
                                "tenant": "Default"}
        self.emits_on_cleanup = emits_on_cleanup
        self.cleanups = 0

    def cleanup(self):
        self.cleanups += 1
        if self.emits_on_cleanup and self.cleanups == 1:
            self.session_closed.emit()


class _Popup(_Session):
    """A browser popup tab, with WebBrowserTab's own way back to its opener
    (no Chromium needed for the tab bookkeeping)."""

    open_in_new_tab = pyqtSignal(object)
    return_to_opener = pyqtSignal(object)
    title_changed = pyqtSignal(str)

    OPENER_LABEL_MAX = WebBrowserTab.OPENER_LABEL_MAX
    opener_label = WebBrowserTab.opener_label
    opener_closed = WebBrowserTab.opener_closed
    _go_back_to_opener = WebBrowserTab._go_back_to_opener

    def __init__(self, opener):
        super().__init__(opener.connection_info["name"], emits_on_cleanup=False)
        self._preset = {"label": "Web"}
        self.opener_tab = opener
        self.btn_back_to_opener = None


class TabCloseTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    def setUp(self):
        vault_mod.use_vault_directory(_TMP)
        for f in (vault_mod.VAULT_FILE, vault_mod.SALT_FILE,
                  vault_mod.HASH_FILE, vault_mod.KEY_FILE):
            if f.exists():
                f.unlink()
        from app.vault import Vault

        # Closing the window sweeps Credential Manager; keep that off the real one.
        previous = windows_credentials.use_credential_store(
            windows_credentials.MemoryCredentialStore())
        self.addCleanup(windows_credentials.use_credential_store, previous)
        self.vault = Vault()
        self.vault.setup("tab-close-pw")
        self.win = mw.MainWindow(self.vault)
        self.addCleanup(self.win.deleteLater)
        self.addCleanup(self.win.reachability.shutdown)

    def _open(self, widget):
        """Add *widget* the way _open_now adds a session."""
        self.win._add_session_tab(widget, widget.connection_info)
        widget.session_closed.connect(self.win._on_session_closed)
        return widget

    def _sessions(self, *names, emits_on_cleanup=True):
        return [self._open(_Session(name, emits_on_cleanup)) for name in names]

    def _titles(self):
        return [self.win.tabs.tabText(i) for i in range(self.win.tabs.count())]

    def test_closing_one_tab_leaves_the_others_open(self):
        a, b, c, d = self._sessions("a", "b", "c", "d")
        self.win.tabs.tabCloseRequested.emit(self.win.tabs.indexOf(b))
        self.assertEqual(self._titles(), ["Dashboard", "a", "c", "d"])
        self.assertEqual(b.cleanups, 1)
        for other in (a, c, d):
            self.assertEqual(other.cleanups, 0, other.connection_info["name"])

    def test_closing_tabs_one_by_one_closes_one_each_time(self):
        sessions = self._sessions("a", "b", "c", "d")
        for remaining in (3, 2, 1, 0):
            self.win.tabs.tabCloseRequested.emit(1)
            self.assertEqual(self.win.tabs.count(), 1 + remaining)
        self.assertTrue(all(s.cleanups >= 1 for s in sessions))

    def test_closing_the_last_tab_keeps_the_rest(self):
        self._sessions("a", "b", "c")
        self.win.tabs.tabCloseRequested.emit(self.win.tabs.count() - 1)
        self.assertEqual(self._titles(), ["Dashboard", "a", "b"])

    def test_sessions_that_do_not_close_themselves_close_alone_too(self):
        # Remote Desktop and browser tabs: their cleanup emits nothing.
        a, b, c = self._sessions("a", "b", "c", emits_on_cleanup=False)
        self.win.tabs.tabCloseRequested.emit(self.win.tabs.indexOf(b))
        self.assertEqual(self._titles(), ["Dashboard", "a", "c"])
        self.assertEqual((a.cleanups, b.cleanups, c.cleanups), (0, 1, 0))

    def test_ctrl_shift_w_closes_only_the_current_tab(self):
        _a, b, _c = self._sessions("a", "b", "c")
        self.win.tabs.setCurrentWidget(b)
        self.win._close_current_tab()
        self.assertEqual(self._titles(), ["Dashboard", "a", "c"])

    def test_a_session_ending_by_itself_removes_only_its_tab(self):
        a, b, c = self._sessions("a", "b", "c")
        b.session_closed.emit()
        self.assertEqual(self._titles(), ["Dashboard", "a", "c"])
        self.assertEqual((a.cleanups, c.cleanups), (0, 0))

    def test_open_external_rdp_closes_embedded_tab_and_cancels_reconnect(self):
        from app import rdp_launcher

        conn = {"name": "RDP host", "protocol": "rdp", "host": "192.0.2.1",
                "rdp_open_mode": "embedded", "tenant": "Test",
                "rdp_resources": rdp_launcher.normalize_rdp_resources({})}
        self.win._open_now(conn)
        tab = self.win.tabs.currentWidget()
        embedded = tab._embedded_widget
        token = tab._history_token
        old_process = Mock(pid=101)
        old_process.poll.return_value = None
        exited = threading.Event()
        self.addCleanup(exited.set)
        external_process = Mock(pid=102)
        external_process.wait.side_effect = lambda: exited.wait(5)
        with (patch.object(rdp_launcher.subprocess, "Popen",
                           side_effect=[old_process, external_process]) as launch,
              patch.object(rdp_launcher, "_auto_dismiss_rdp_security_warning")):
            embedded.connect_session()
            embedded._process_health_timer.start()
            embedded._fallback_reconnect_timer.start()
            embedded.btn_cad.click()
            path = Path(launch.call_args.args[0][1])
            self.addCleanup(path.unlink, missing_ok=True)

            self.assertEqual(self._titles(), ["Dashboard"])
            self.assertEqual(self.win._external_sessions(), {token: "Test"})
            self.assertTrue(path.is_file())
            old_process.terminate.assert_called_once()
            external_process.terminate.assert_not_called()
            self.assertFalse(embedded._process_health_timer.isActive())
            self.assertFalse(embedded._fallback_reconnect_timer.isActive())
            embedded._auto_reconnect()
            embedded.connect_session()
            self.win._resume_all_connections()
            self.assertEqual(launch.call_count, 2)

        exited.set()
        self.assertTrue(_wait_until(lambda: not self.win._external_sessions()))
        self.assertTrue(_wait_until(lambda: not path.exists()))

    def test_ssh_pop_out_preserves_split_panes_and_closes_with_the_tab(self):
        conn = {"name": "SSH host", "protocol": "ssh", "host": "192.0.2.1"}
        with patch.object(mw.SSHTerminal, "connect_ssh") as connect:
            self.win._open_now(conn)
            workspace = self.win.tabs.currentWidget()
            first, = workspace.terminals()
            second = workspace.add_terminal(connect=False)
            first._connected = second._connected = True
            token = workspace._history_token
            self.win.show()

            first.btn_pop_out.click()
            second.btn_pop_out.click()
            first_window, second_window = first.window(), second.window()
            self.assertIsNot(first_window, second_window)
            self.assertTrue(first_window.isVisible() and second_window.isVisible())
            self.assertEqual(workspace.terminals(), [first, second])
            self.assertEqual(self.win._live_sessions(), ["SSH host"])
            self.win._reconcile_history()
            self.assertIn(token, self.win.session_history._open)

            self.win.idle_lock.lock_now()
            self.assertFalse(first_window.isVisible() or second_window.isVisible())
            screen = self.win.idle_lock._screen
            screen.password.setText("tab-close-pw")
            screen._try_unlock()
            self.assertTrue(first_window.isVisible() and second_window.isVisible())

            first_window.close()
            self.assertIs(workspace.splitter.widget(0), first)
            self.assertTrue(first.isVisible() and first._connected)
            self.assertIs(second.window(), second_window)
            connect.assert_called_once()

            self.win._close_tab(self.win.tabs.indexOf(workspace))
            self.assertFalse(second_window.isVisible())
            self.assertNotIn(token, self.win.session_history._open)
            _flush_deletes()
            self.assertTrue(sip.isdeleted(first_window))
            self.assertTrue(sip.isdeleted(second_window))

    def test_real_ftp_and_sftp_tabs_close_one_at_a_time(self):
        from app.ftp_browser import FTPBrowser
        from app.sftp_browser import SFTPBrowser

        def conn(name, protocol, port):
            return {"name": name, "protocol": protocol, "host": "192.0.2.1",
                    "port": port, "username": "u", "password": "p", "tenant": "Default"}

        self._open(FTPBrowser(conn("ftp-a", "ftp", 21)))
        ftp_b = self._open(FTPBrowser(conn("ftp-b", "ftp", 21)))
        self._open(SFTPBrowser(conn("sftp-c", "sftp", 22)))
        sftp_d = self._open(SFTPBrowser(conn("sftp-d", "sftp", 22)))
        self._open(FTPBrowser(conn("ftp-e", "ftp", 21)))
        self.win.tabs.tabCloseRequested.emit(self.win.tabs.indexOf(ftp_b))
        self.assertEqual(self._titles(), ["Dashboard", "ftp-a", "sftp-c", "sftp-d", "ftp-e"])
        self.win.tabs.tabCloseRequested.emit(self.win.tabs.indexOf(sftp_d))
        self.assertEqual(self._titles(), ["Dashboard", "ftp-a", "sftp-c", "ftp-e"])
        _flush_deletes()
        self.assertTrue(sip.isdeleted(ftp_b) and sip.isdeleted(sftp_d))

    def test_the_dashboard_stays_when_dragged_away_from_the_first_place(self):
        a, b = self._sessions("a", "b")
        bar = self.win.tabs.tabBar()
        bar.moveTab(0, 2)           # Dashboard dragged to the end
        self.assertEqual(self._titles(), ["a", "b", "Dashboard"])
        self.win.tabs.tabCloseRequested.emit(2)
        self.assertIn("Dashboard", self._titles())
        self.win.tabs.setCurrentWidget(self.win.dashboard)
        self.win._close_current_tab()
        self.assertIn("Dashboard", self._titles())
        # …and the session now in the first place can still be closed.
        self.win.tabs.tabCloseRequested.emit(0)
        self.assertEqual(self._titles(), ["b", "Dashboard"])
        self.assertEqual((a.cleanups >= 1, b.cleanups), (True, 0))

    def test_closing_the_window_cleans_up_every_session(self):
        sessions = self._sessions("a", "b", "c", "d", "e")
        self.win._skip_close_prompt = True
        self.assertTrue(self.win.close())
        for session in sessions:
            self.assertGreaterEqual(session.cleanups, 1, session.connection_info["name"])

    def test_disconnect_from_the_sidebar_closes_only_that_session(self):
        # The sidebar's Disconnect walked the tabs by index and removed each
        # match after its cleanup, which had closed the tab already: the
        # neighbour that moved into the slot went too, still connected.
        a, b, c, d = self._sessions("a", "b", "c", "d")
        self.win._disconnect_connection(dict(b.connection_info))
        self.assertEqual(self._titles(), ["Dashboard", "a", "c", "d"])
        self.assertEqual(b.cleanups, 1)
        for other in (a, c, d):
            self.assertEqual(other.cleanups, 0, other.connection_info["name"])

    # ── Closed tabs are freed ───────────────────────────────────────

    def test_a_closed_tab_widget_is_deleted(self):
        a, b, c = self._sessions("a", "b", "c")
        self.win.tabs.tabCloseRequested.emit(self.win.tabs.indexOf(b))
        _flush_deletes()
        self.assertTrue(sip.isdeleted(b), "the closed tab stayed alive")
        self.assertFalse(sip.isdeleted(a) or sip.isdeleted(c))
        self.assertEqual(self._titles(), ["Dashboard", "a", "c"])

    def test_every_way_a_tab_closes_frees_its_widget(self):
        by_bar, by_shortcut, by_itself, by_sidebar = self._sessions(
            "bar", "shortcut", "itself", "sidebar")
        quiet = self._open(_Session("quiet", emits_on_cleanup=False))
        self.win.tabs.tabCloseRequested.emit(self.win.tabs.indexOf(by_bar))
        self.win.tabs.setCurrentWidget(by_shortcut)
        self.win._close_current_tab()
        by_itself.session_closed.emit()
        self.win._disconnect_connection(dict(by_sidebar.connection_info))
        self.win.tabs.tabCloseRequested.emit(self.win.tabs.indexOf(quiet))
        self.assertEqual(self._titles(), ["Dashboard"])
        _flush_deletes()
        for session in (by_bar, by_shortcut, by_itself, by_sidebar, quiet):
            self.assertTrue(sip.isdeleted(session), session.connection_info["name"])
        self.assertFalse(sip.isdeleted(self.win.dashboard))

    def test_the_dashboard_is_never_freed(self):
        self._sessions("a")
        self.win._free_tab_widget(self.win.dashboard)
        self.win.tabs.tabCloseRequested.emit(self.win.tabs.indexOf(self.win.dashboard))
        _flush_deletes()
        self.assertFalse(sip.isdeleted(self.win.dashboard))
        self.assertIn("Dashboard", self._titles())

    def test_a_session_ending_under_its_own_dialog_waits_for_the_dialog(self):
        # Deleting the tab inside the dialog's event loop would leave the
        # code waiting on that dialog with a deleted widget.
        _a, b = self._sessions("a", "b")
        dialog = QDialog(b)
        dialog.setModal(True)
        dialog.show()
        b.session_closed.emit()
        self.assertEqual(self._titles(), ["Dashboard", "a"])
        _flush_deletes()
        self.assertFalse(sip.isdeleted(b), "deleted while its dialog was open")
        dialog.close()
        self.assertTrue(_wait_until(lambda: sip.isdeleted(b)),
                        "never freed after the dialog closed")

    def test_a_window_the_session_opened_stays_open(self):
        # SSHTerminal.open_replay shows a replay window parented to the
        # terminal; deleting the tab must not take it along.
        a, = self._sessions("a")
        replay = QDialog(a)
        replay.show()
        self.addCleanup(lambda: sip.isdeleted(replay) or replay.close())
        self.win.tabs.tabCloseRequested.emit(self.win.tabs.indexOf(a))
        _flush_deletes()
        self.assertTrue(sip.isdeleted(a))
        self.assertFalse(sip.isdeleted(replay))
        self.assertTrue(replay.isVisible())
        self.assertIs(replay.parentWidget(), self.win)

    def test_session_history_ends_for_a_freed_tab(self):
        a, = self._sessions("a")
        token = a._history_token
        self.assertIn(token, self.win._history_widgets)
        # Gone without the window being told: the timer's reconcile finds it.
        self.win.tabs.removeTab(self.win.tabs.indexOf(a))
        a.deleteLater()
        _flush_deletes()
        self.assertTrue(sip.isdeleted(a))
        self.win._reconcile_history()
        self.assertNotIn(token, self.win._history_widgets)
        self.assertNotIn(token, self.win.session_history._open)

    def test_closing_the_window_after_tabs_were_freed(self):
        a, b, c = self._sessions("a", "b", "c")
        self.win.tabs.tabCloseRequested.emit(self.win.tabs.indexOf(b))
        _flush_deletes()
        self.win._skip_close_prompt = True
        self.assertTrue(self.win.close())
        self.assertTrue(a.cleanups >= 1 and c.cleanups >= 1)

    # ── Browser popups and their freed opener ───────────────────────

    def _popup(self, console):
        popup = _Popup(console)
        self.win._add_browser_child_tab(popup)
        return popup

    def test_a_popup_outlives_its_freed_console(self):
        console, = self._sessions("console")
        popup = self._popup(console)
        self.win.tabs.tabCloseRequested.emit(self.win.tabs.indexOf(console))
        _flush_deletes()
        self.assertTrue(sip.isdeleted(console))
        self.assertIsNone(popup.opener_tab)
        popup.title_changed.emit("File Station")       # refreshes the tooltip
        popup._go_back_to_opener()                     # the "Back to …" button
        self.assertIs(self.win.tabs.currentWidget(), popup)

    def test_way_back_to_a_console_deleted_underneath_the_popup(self):
        console, = self._sessions("console")
        popup = self._popup(console)
        # Gone without the window being told, so the popup still points at it.
        self.win.tabs.removeTab(self.win.tabs.indexOf(console))
        console.deleteLater()
        _flush_deletes()
        self.assertTrue(sip.isdeleted(console))
        self.assertIs(popup.opener_tab, console)

        popup._go_back_to_opener()                     # the "Back to …" button
        self.assertIs(self.win.tabs.currentWidget(), popup)
        self.assertIsNone(popup.opener_tab)

        popup.opener_tab = console      # a page title arriving instead
        popup.title_changed.emit("File Station")
        tooltip = self.win.tabs.tabToolTip(self.win.tabs.indexOf(popup))
        self.assertIn("File Station", tooltip)
        self.assertNotIn("Back to", tooltip)
        self.assertIsNone(popup.opener_tab)

        popup.opener_tab = console      # and closing it while it still points there
        self.win.tabs.tabCloseRequested.emit(self.win.tabs.indexOf(popup))
        _flush_deletes()
        self.assertTrue(sip.isdeleted(popup))
        self.assertEqual(self._titles(), ["Dashboard"])


if __name__ == "__main__":
    unittest.main()
