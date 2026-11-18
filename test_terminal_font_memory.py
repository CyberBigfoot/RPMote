"""The terminal font is a preference, not a chore to repeat every session.

Picking a face or zooming in used to last exactly as long as the tab did.
These tests follow one choice the whole way: header combo to workspace to
vault, and back into the terminal that opens next time.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app import vault as vault_mod
from app.ssh_terminal import SSHTerminal, TerminalWorkspace
from app.vault import Vault

_APP = QApplication.instance() or QApplication([])

CONN = {"name": "t", "host": "h", "port": 22, "username": "u"}


class RecordingWorkspace(TerminalWorkspace):
    """A workspace that records what it would have opened.

    The real ``add_terminal`` connects over SSH, which a test has no business
    doing to find out which font a new pane would have started with.
    """

    def add_terminal(self, vertical=True, source_terminal=None):
        self.opened = getattr(self, "opened", [])
        self.opened.append((self.terminal_font, self.font_size))


class FontChoiceTests(unittest.TestCase):
    """What a single terminal does with the font it is handed."""

    def terminal(self, **kwargs):
        term = SSHTerminal(dict(CONN), **kwargs)
        self.addCleanup(term.deleteLater)
        return term

    def test_a_terminal_opens_with_the_font_it_is_given(self):
        term = self.terminal(terminal_font="Consolas", font_size=15)
        self.assertEqual(term.font_combo.currentText(), "Consolas")
        self.assertEqual(term.terminal._font.family(), "Consolas")
        self.assertEqual(term.terminal._font.pointSize(), 15)

    def test_no_saved_choice_means_the_defaults(self):
        term = self.terminal()
        self.assertEqual(term.font_combo.currentText(), "Default Font")
        self.assertEqual(term._font_size, SSHTerminal.DEFAULT_FONT_SIZE)

    def test_a_face_that_is_not_offered_falls_back(self):
        # A settings file from a future version, or one edited by hand.
        self.assertEqual(self.terminal(terminal_font="Comic Sans")._local_font,
                         "Default Font")

    def test_a_nonsense_size_falls_back_and_a_wild_one_is_clamped(self):
        self.assertEqual(self.terminal(font_size="big")._font_size,
                         SSHTerminal.DEFAULT_FONT_SIZE)
        self.assertEqual(self.terminal(font_size=900)._font_size,
                         SSHTerminal.MAX_FONT_SIZE)
        self.assertEqual(self.terminal(font_size=1)._font_size,
                         SSHTerminal.MIN_FONT_SIZE)

    def test_picking_a_face_announces_both_halves_of_the_choice(self):
        term = self.terminal(font_size=13)
        seen = []
        term.font_changed.connect(lambda face, size: seen.append((face, size)))
        term.font_combo.setCurrentText("Fira Code")
        self.assertEqual(seen, [("Fira Code", 13)])
        self.assertEqual(term.terminal._font.family(), "Fira Code")

    def test_zooming_announces_the_new_size(self):
        term = self.terminal(terminal_font="Consolas", font_size=12)
        seen = []
        term.font_changed.connect(lambda face, size: seen.append((face, size)))
        term._increase_font()
        term._decrease_font()
        term._decrease_font()
        self.assertEqual(seen, [("Consolas", 13), ("Consolas", 12),
                                ("Consolas", 11)])
        self.assertEqual(term.terminal._font.pointSize(), 11)

    def test_zoom_stops_at_the_limits(self):
        term = self.terminal(font_size=SSHTerminal.MAX_FONT_SIZE)
        term._increase_font()
        self.assertEqual(term._font_size, SSHTerminal.MAX_FONT_SIZE)
        term = self.terminal(font_size=SSHTerminal.MIN_FONT_SIZE)
        term._decrease_font()
        self.assertEqual(term._font_size, SSHTerminal.MIN_FONT_SIZE)

    def test_setting_the_face_in_code_does_not_announce_it_again(self):
        # How a caller pushes a remembered choice in without it looking like
        # the user just made one.
        term = self.terminal()
        seen = []
        term.font_changed.connect(lambda face, size: seen.append((face, size)))
        term.set_terminal_font_choice("Cascadia Code")
        self.assertEqual(seen, [])
        self.assertEqual(term.font_combo.currentText(), "Cascadia Code")

    def test_the_combo_offers_exactly_the_faces_that_are_accepted(self):
        term = self.terminal()
        offered = [term.font_combo.itemText(i)
                   for i in range(term.font_combo.count())]
        self.assertEqual(offered, list(SSHTerminal.TERMINAL_FONTS))


class WorkspaceFontTests(unittest.TestCase):
    """A split workspace passes the choice on and hands it upwards."""

    def workspace(self, **kwargs):
        ws = RecordingWorkspace(dict(CONN), **kwargs)
        self.addCleanup(ws.deleteLater)
        return ws

    def test_a_new_pane_starts_from_the_remembered_choice(self):
        ws = self.workspace(terminal_font="JetBrains Mono", font_size=14)
        self.assertEqual(ws.opened, [("JetBrains Mono", 14)])

    def test_a_change_is_what_the_next_pane_opens_with(self):
        ws = self.workspace()
        seen = []
        ws.font_changed.connect(lambda face, size: seen.append((face, size)))
        ws._on_font_changed("Fira Code", 16)
        ws.add_terminal()
        self.assertEqual(ws.opened[-1], ("Fira Code", 16))
        self.assertEqual(seen, [("Fira Code", 16)])

    def test_rubbish_from_a_pane_is_still_normalised(self):
        ws = self.workspace()
        ws._on_font_changed("Wingdings", 4000)
        self.assertEqual((ws.terminal_font, ws.font_size),
                         ("Default Font", SSHTerminal.MAX_FONT_SIZE))


class SavedFontTests(unittest.TestCase):
    """The window writes the choice down, and the vault keeps it."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp(prefix="rpmote-font-"))
        cls.previous = vault_mod.VAULT_DIR
        vault_mod.use_vault_directory(cls.dir)

    @classmethod
    def tearDownClass(cls):
        vault_mod.use_vault_directory(cls.previous)
        shutil.rmtree(cls.dir, ignore_errors=True)

    def window(self, face="Default Font", size=SSHTerminal.DEFAULT_FONT_SIZE):
        """Just enough of a MainWindow for the save path to run on."""
        from app.main_window import MainWindow
        saved = {}
        stub = SimpleNamespace(
            _ssh_terminal_font=face,
            _ssh_font_size=size,
            vault=SimpleNamespace(update_settings=saved.update),
        )
        return MainWindow._save_ssh_terminal_font.__get__(stub), stub, saved

    def test_a_choice_is_written_to_the_vault(self):
        save, stub, saved = self.window()
        save("Consolas", 14)
        self.assertEqual(saved, {"ssh_terminal_font": "Consolas",
                                 "ssh_terminal_font_size": 14})
        self.assertEqual(stub._ssh_terminal_font, "Consolas")
        self.assertEqual(stub._ssh_font_size, 14)

    def test_choosing_what_is_already_saved_writes_nothing(self):
        save, _stub, saved = self.window()
        save("Default Font", SSHTerminal.DEFAULT_FONT_SIZE)
        self.assertEqual(saved, {})

    def test_a_bad_choice_never_reaches_the_vault(self):
        save, _stub, saved = self.window("Consolas", 14)
        save("Comic Sans", "huge")
        self.assertEqual(saved, {"ssh_terminal_font": "Default Font",
                                 "ssh_terminal_font_size":
                                     SSHTerminal.DEFAULT_FONT_SIZE})

    def test_the_choice_survives_a_restart(self):
        for path in (vault_mod.VAULT_FILE, vault_mod.SALT_FILE,
                     vault_mod.HASH_FILE, vault_mod.KEY_FILE):
            if path.exists():
                path.unlink()
        vault = Vault()
        vault.setup("test-password-123")
        vault.update_settings({"ssh_terminal_font": "Cascadia Code",
                               "ssh_terminal_font_size": 18})

        # A new process, a new unlock: the terminal that opens next has to
        # start where the last one was left.
        reopened = Vault()
        self.assertTrue(reopened.unlock("test-password-123"))
        self.assertEqual(reopened.settings["ssh_terminal_font"],
                         "Cascadia Code")
        self.assertEqual(reopened.settings["ssh_terminal_font_size"], 18)

    def test_a_vault_that_predates_the_setting_gets_the_defaults(self):
        # An existing user's vault has no font keys in it at all; opening it
        # has to fill them in rather than leave the lookup to fail.
        for path in (vault_mod.VAULT_FILE, vault_mod.SALT_FILE,
                     vault_mod.HASH_FILE, vault_mod.KEY_FILE):
            if path.exists():
                path.unlink()
        vault = Vault()
        vault.setup("test-password-123")
        self.assertNotIn("ssh_terminal_font", vault.settings)

        reopened = Vault()
        self.assertTrue(reopened.unlock("test-password-123"))
        self.assertEqual(reopened.settings["ssh_terminal_font"], "Default Font")
        self.assertEqual(reopened.settings["ssh_terminal_font_size"],
                         SSHTerminal.DEFAULT_FONT_SIZE)


if __name__ == "__main__":
    unittest.main()
