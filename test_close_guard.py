"""Closing the window with sessions still open asks first.

Ctrl+L is "clear screen" in every shell; with focus in the Library file
browser instead of the terminal it reaches Lock Vault and used to take the
whole application down without a word.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.vault as vault_mod

_TMP = Path(tempfile.mkdtemp(prefix="rpmote_close_guard_"))
vault_mod.use_vault_directory(_TMP)

from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget

from app import main_window as mw

_APP = QApplication.instance() or QApplication([])


class _Tab(QWidget):
    """A stand-in session tab; `_connected` is what every real one exposes."""

    def __init__(self, connected):
        super().__init__()
        self._connected = connected
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True


class _Question:
    """Stands in for QMessageBox.question and records being asked."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, parent, title, text, *args, **kwargs):
        self.calls.append(text)
        return self.answer


class CloseGuardTests(unittest.TestCase):
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

        self.vault = Vault()
        self.vault.setup("close-guard-pw")
        self.win = mw.MainWindow(self.vault)
        self.addCleanup(self.win.deleteLater)
        self.addCleanup(self.win.reachability.shutdown)
        self._question = mw.QMessageBox.question
        self.addCleanup(setattr, mw.QMessageBox, "question", self._question)

    def _ask(self, answer):
        stub = _Question(answer)
        mw.QMessageBox.question = stub
        return stub

    def _add_tab(self, name, connected):
        tab = _Tab(connected)
        self.win.tabs.addTab(tab, name)
        return tab

    def test_no_sessions_closes_without_asking(self):
        stub = self._ask(QMessageBox.StandardButton.No)
        self.assertTrue(self.win.close())
        self.assertEqual(stub.calls, [])

    def test_disconnected_tabs_do_not_count(self):
        self._add_tab("old", connected=False)
        stub = self._ask(QMessageBox.StandardButton.No)
        self.assertTrue(self.win.close())
        self.assertEqual(stub.calls, [])

    def test_declining_keeps_everything_open(self):
        tab = self._add_tab("web01", connected=True)
        stub = self._ask(QMessageBox.StandardButton.No)
        self.assertFalse(self.win.close())
        self.assertEqual(len(stub.calls), 1)
        self.assertIn("web01", stub.calls[0])
        self.assertFalse(tab.cleaned)
        self.assertFalse(getattr(self.win, "_closing", False))

    def test_accepting_closes_and_cleans_up(self):
        tab = self._add_tab("web01", connected=True)
        self._ask(QMessageBox.StandardButton.Yes)
        self.assertTrue(self.win.close())
        self.assertTrue(tab.cleaned)

    def test_lock_vault_only_locks_once_the_close_is_agreed(self):
        self._add_tab("web01", connected=True)
        self._ask(QMessageBox.StandardButton.No)
        self.win._lock_vault()
        self.assertTrue(self.vault.is_unlocked)
        self._ask(QMessageBox.StandardButton.Yes)
        self.win._lock_vault()
        self.assertFalse(self.vault.is_unlocked)

    def test_the_lock_screen_quit_does_not_ask_twice(self):
        self._add_tab("web01", connected=True)
        stub = self._ask(QMessageBox.StandardButton.No)
        self.assertTrue(self.win.close_without_prompt())
        self.assertEqual(stub.calls, [])

    def test_the_prompt_lists_every_live_session(self):
        for name in ("a", "b", "c"):
            self._add_tab(name, connected=True)
        self._add_tab("idle", connected=False)
        stub = self._ask(QMessageBox.StandardButton.No)
        self.win.close()
        text = stub.calls[0]
        self.assertIn("3 sessions", text)
        for name in ("a", "b", "c"):
            self.assertIn(name, text)
        self.assertNotIn("idle", text)



if __name__ == "__main__":
    unittest.main()
