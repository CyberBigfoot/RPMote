"""Tests for how the SSH terminal brings a session up.

Covers the hidden shell-integration setup window, PTY sizing before the widget
has been laid out, and preserving the login banner across the realign.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.screen_buffer import ScreenBuffer
from app.ssh_terminal import SSHTerminal

_APP = QApplication.instance() or QApplication([])

CONN = {"name": "test", "host": "example.invalid", "port": 22, "username": "u"}


class ShellIntegrationScriptTests(unittest.TestCase):
    def setUp(self):
        self.term = SSHTerminal(dict(CONN))

    def tearDown(self):
        self.term.deleteLater()

    def test_completion_marker_is_not_present_in_the_echoed_command(self):
        """The shell echoes what we type; that echo must not look like success.

        The marker is assembled by printf at run time, so the command text
        contains the format string and the token separately and never the
        finished marker. Matching the echo used to end the hidden setup
        window early, halfway through the script.
        """
        token = "RPMOTE_SYNC_123456"
        script = self.term._build_shell_integration_script(token)
        self.assertNotIn(f"__{token}__DONE__", script)
        self.assertIn("printf '\\n__%s__DONE__\\n'", script)

    def test_script_is_a_single_posix_brace_group(self):
        script = self.term._build_shell_integration_script("T")
        self.assertTrue(script.startswith("{\r"))
        self.assertIn("} 2>/dev/null", script)

    def test_hidden_window_is_capped_at_three_seconds(self):
        self.assertEqual(SSHTerminal.SHELL_SETUP_TIMEOUT_MS, 3000)

    def test_disabled_integration_never_blanks_the_terminal(self):
        self.term._shell_integration_enabled = False
        self.term._apply_shell_integration()
        self.assertFalse(self.term._suppress_rendering)

    def test_there_is_no_sync_button_in_the_header(self):
        """The prompt/alias push is gone, and so is its toggle."""
        self.assertFalse(hasattr(self.term, "btn_shell_sync"))
        self.assertFalse(hasattr(self.term, "shell_sync_changed"))
        self.assertNotIn("shell_sync", self.term.connection_info)

    def test_the_old_prompt_resource_is_gone(self):
        self.assertEqual(self.term._script_lines("fancy-prompt.sh"), [])


class NonPosixDetectionTests(unittest.TestCase):
    def setUp(self):
        self.term = SSHTerminal(dict(CONN))

    def tearDown(self):
        self.term.deleteLater()

    def test_windows_command_shell_is_recognised(self):
        self.term._shell_setup_tail = (
            "'{' is not recognized as an internal or external command,"
        )
        self.assertTrue(self.term._shell_setup_looks_unsupported())

    def test_network_device_is_recognised(self):
        self.term._shell_setup_tail = "% Invalid input detected at '^' marker."
        self.assertTrue(self.term._shell_setup_looks_unsupported())

    def test_ordinary_shell_output_is_not_flagged(self):
        self.term._shell_setup_tail = "Welcome to Ubuntu 24.04 LTS\r\nuser@host:~$ "
        self.assertFalse(self.term._shell_setup_looks_unsupported())

    def test_giving_up_disables_the_hooks_and_restores_rendering(self):
        self.term._suppress_rendering = True
        self.term._abandon_shell_integration()
        self.assertFalse(self.term._suppress_rendering)
        self.assertFalse(self.term._shell_integration_enabled)


class PromptDetectionTests(unittest.TestCase):
    def setUp(self):
        self.term = SSHTerminal(dict(CONN))

    def tearDown(self):
        self.term.deleteLater()

    def test_real_prompts_are_detected(self):
        for line in ("user@host:~$", "root@box:/etc#", "~ ❯", "user@h ~ %"):
            with self.subTest(line=line):
                self.assertTrue(self.term._looks_like_prompt(line))

    def test_continuation_prompts_are_not_prompts(self):
        """`>` is bash's PS2 — treating it as a prompt fired setup mid-command."""
        for line in (">", ">>", "...", "value ->", "cfg =>", "", "plain output"):
            with self.subTest(line=line):
                self.assertFalse(self.term._looks_like_prompt(line))


class PtySizingTests(unittest.TestCase):
    def test_size_falls_back_to_a_default_before_the_first_show(self):
        """A never-laid-out viewport gave the remote shell a nonsense width."""
        term = SSHTerminal(dict(CONN))
        try:
            self.assertFalse(term._laid_out)
            self.assertEqual(term._terminal_size_for_pty(), SSHTerminal.DEFAULT_PTY_SIZE)
        finally:
            term.deleteLater()


class BannerPreservationTests(unittest.TestCase):
    def test_visible_screen_moves_into_scrollback_before_a_realign(self):
        buf = ScreenBuffer(rows=10, cols=40)
        buf.feed("Welcome to Ubuntu 24.04 LTS\r\nLast login: today\r\nuser@host:~$ ")
        before = buf.scrollback_total_lines

        archived = buf.archive_visible_screen(keep_last=1)

        self.assertEqual(archived, 2)  # banner rows, not the prompt line
        self.assertEqual(buf.scrollback_total_lines, before + 2)
        first = "".join(cell.char for cell in buf._scrollback[0]).strip()
        self.assertEqual(first, "Welcome to Ubuntu 24.04 LTS")

    def test_archiving_an_empty_screen_does_nothing(self):
        buf = ScreenBuffer(rows=5, cols=20)
        self.assertEqual(buf.archive_visible_screen(), 0)
        self.assertEqual(buf.scrollback_total_lines, 0)


if __name__ == "__main__":
    unittest.main()
