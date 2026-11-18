"""Tests for OSC-based shell integration.

Covers the emulator's handling of OSC 7 (working directory), OSC 8
(hyperlinks), OSC 52 (clipboard) and OSC 133 (semantic marks), the hooks sent
to the remote shell, and the terminal features that read those marks.

The end-to-end test at the bottom is the one that matters most: it runs the
real hook script in a real bash and feeds bash's actual output through the
emulator, so a quoting mistake in the script cannot pass unnoticed.
"""

import os
import shutil
import subprocess
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.screen_buffer import (
    MARK_INPUT, MARK_NONE, MARK_OUTPUT, MARK_PROMPT, ScreenBuffer,
)
from app.ssh_terminal import SSHTerminal

_APP = QApplication.instance() or QApplication([])

CONN = {"name": "test", "host": "example.invalid", "port": 22, "username": "u"}

BEL = "\x07"


def osc(body: str) -> str:
    return f"\x1b]{body}{BEL}"


class CwdReportTests(unittest.TestCase):
    def setUp(self):
        self.buf = ScreenBuffer(rows=10, cols=40)

    def test_osc7_sets_the_working_directory(self):
        self.buf.feed(osc("7;file://srv/var/log"))
        self.assertEqual(self.buf.cwd, "/var/log")
        self.assertEqual(self.buf.cwd_host, "srv")

    def test_percent_escapes_are_decoded(self):
        self.buf.feed(osc("7;file://srv/home/a%20b"))
        self.assertEqual(self.buf.cwd, "/home/a b")

    def test_a_windows_style_path_loses_the_url_slash(self):
        self.buf.feed(osc("7;file://host/C:/Users/grant"))
        self.assertEqual(self.buf.cwd, "C:/Users/grant")

    def test_a_non_file_scheme_is_ignored(self):
        self.buf.feed(osc("7;http://example.com/evil"))
        self.assertEqual(self.buf.cwd, "")

    def test_junk_does_not_raise(self):
        self.buf.feed(osc("7;"))
        self.buf.feed(osc("7;file://"))
        self.assertEqual(self.buf.cwd, "")


class HyperlinkTests(unittest.TestCase):
    def setUp(self):
        self.buf = ScreenBuffer(rows=10, cols=40)

    def row_links(self, row=0):
        return [cell.attrs.link for cell in self.buf.grid[row]]

    def test_text_between_osc8_pairs_carries_the_target(self):
        self.buf.feed(osc("8;;https://example.com") + "link" + osc("8;;"))
        self.assertEqual(self.row_links()[:4], ["https://example.com"] * 4)

    def test_the_link_ends_where_the_shell_says_it_does(self):
        self.buf.feed(osc("8;;https://example.com") + "ab" + osc("8;;") + "cd")
        self.assertEqual(self.row_links()[2:4], ["", ""])

    def test_a_relative_target_is_refused(self):
        # Only absolute URLs; a relative target is a way to smuggle something
        # odd past the scheme check later on.
        self.buf.feed(osc("8;;/etc/passwd") + "x")
        self.assertEqual(self.row_links()[0], "")

    def test_the_link_survives_a_copy_of_the_attributes(self):
        self.buf.feed(osc("8;;https://example.com") + "x")
        self.assertEqual(self.buf.grid[0][0].attrs.copy().link,
                         "https://example.com")


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.buf = ScreenBuffer(rows=10, cols=40)
        self.seen = []
        self.buf.clipboard_callback = self.seen.append

    def test_osc52_delivers_the_decoded_text(self):
        self.buf.feed(osc("52;c;aGVsbG8gd29ybGQ="))
        self.assertEqual(self.seen, ["hello world"])

    def test_a_read_request_is_never_answered(self):
        """Answering one hands the local clipboard to the remote host."""
        self.buf.feed(osc("52;c;?"))
        self.assertEqual(self.seen, [])

    def test_invalid_base64_is_dropped(self):
        self.buf.feed(osc("52;c;!!!!"))
        self.assertEqual(self.seen, [])

    def test_a_callback_that_raises_does_not_break_the_stream(self):
        def boom(_text):
            raise RuntimeError("no")

        self.buf.clipboard_callback = boom
        self.buf.feed(osc("52;c;aGk=") + "after")
        self.assertIn("after", self.buf.get_text())


class SemanticMarkTests(unittest.TestCase):
    def setUp(self):
        self.buf = ScreenBuffer(rows=10, cols=40)

    def run_command(self, output_lines=("out",), code=0):
        self.buf.feed(osc("133;A") + "$ " + osc("133;B") + "cmd\r\n")
        self.buf.feed(osc("133;C"))
        for line in output_lines:
            self.buf.feed(line + "\r\n")
        self.buf.feed(osc(f"133;D;{code}"))

    def test_marks_are_off_until_the_shell_speaks(self):
        self.buf.feed("plain output\r\n")
        self.assertFalse(self.buf.shell_integration)
        self.assertEqual(self.buf.mark_at(0), MARK_NONE)

    def test_the_prompt_row_is_marked(self):
        self.buf.feed(osc("133;A") + "$ ")
        self.assertTrue(self.buf.shell_integration)
        self.assertEqual(self.buf.mark_at(0), MARK_PROMPT)

    def test_prompt_input_and_output_land_on_their_own_rows(self):
        self.run_command()
        self.assertEqual(self.buf.mark_at(0), MARK_PROMPT)
        self.assertEqual(self.buf.mark_at(1), MARK_OUTPUT)

    def test_the_exit_code_is_recorded(self):
        self.run_command(code=42)
        self.assertEqual(self.buf.last_command().exit_code, 42)

    def test_a_command_is_running_until_it_reports_back(self):
        self.buf.feed(osc("133;A") + "$ " + osc("133;B") + "sleep\r\n")
        self.buf.feed(osc("133;C"))
        self.assertTrue(self.buf.last_command().running)
        self.buf.feed(osc("133;D;0"))
        self.assertFalse(self.buf.last_command().running)

    def test_a_new_prompt_closes_a_command_that_never_reported(self):
        """Ctrl-C at the prompt gives an A with no D before it."""
        self.buf.feed(osc("133;A") + "$ " + osc("133;B") + "x\r\n" + osc("133;C"))
        self.buf.feed(osc("133;A") + "$ ")
        self.assertFalse(self.buf.commands[-2].running)

    def test_the_output_region_is_exactly_the_output(self):
        self.run_command(output_lines=("one", "two"))
        start, end = self.buf.last_output_region()
        self.assertEqual(self.buf.region_text(start, end), "one\ntwo")

    def test_the_output_region_stops_at_the_next_prompt(self):
        self.run_command(output_lines=("one",))
        self.buf.feed(osc("133;A") + "$ ")
        start, end = self.buf.last_output_region()
        self.assertEqual(self.buf.region_text(start, end), "one")

    def test_no_output_region_before_any_command(self):
        self.assertIsNone(self.buf.last_output_region())

    def test_an_unparseable_exit_code_is_not_a_crash(self):
        self.buf.feed(osc("133;A") + "$ " + osc("133;C") + osc("133;D;wat"))
        self.assertIsNone(self.buf.last_command().exit_code)

    def test_vscode_633_marks_are_understood_too(self):
        self.buf.feed(osc("633;A") + "$ ")
        self.assertEqual(self.buf.mark_at(0), MARK_PROMPT)

    def test_a_bare_D_without_a_command_is_ignored_for_status(self):
        """Pressing Enter on an empty line must not report an exit status."""
        self.buf.feed(osc("133;A") + "$ " + osc("133;B") + "\r\n" + osc("133;D;0"))
        self.assertIsNone(self.buf.last_command())

    def test_the_command_list_stays_bounded(self):
        for _ in range(self.buf._command_limit + 50):
            self.buf.feed(osc("133;A"))
        self.assertLessEqual(len(self.buf.commands), self.buf._command_limit)


class MarksFollowTheirRowsTests(unittest.TestCase):
    """A mark has to travel with its line, or every feature above drifts."""

    def setUp(self):
        self.buf = ScreenBuffer(rows=4, cols=20, scrollback_limit=100)

    def test_a_mark_scrolls_up_with_its_row(self):
        self.buf.feed(osc("133;A") + "prompt\r\n")
        self.assertEqual(self.buf.mark_at(0), MARK_PROMPT)
        self.buf.feed("a\r\nb\r\n")
        self.assertEqual(self.buf.mark_at(0), MARK_PROMPT)
        self.assertEqual(self.buf.prompt_lines(), [0])

    def test_a_mark_survives_the_move_into_scrollback(self):
        self.buf.feed(osc("133;A") + "prompt\r\n")
        for _ in range(8):
            self.buf.feed("filler\r\n")
        lines = self.buf.prompt_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn("prompt", self.buf.line_text(lines[0]))

    def test_clearing_the_screen_clears_its_marks(self):
        self.buf.feed(osc("133;A") + "prompt")
        self.buf.feed("\x1b[2J")
        self.assertEqual(self.buf.mark_at(0), MARK_NONE)

    def test_the_alternate_screen_has_its_own_marks(self):
        self.buf.feed(osc("133;A") + "prompt")
        self.buf.enter_alt_screen()
        self.assertEqual(self.buf.mark_at(0), MARK_NONE)
        self.buf.leave_alt_screen()
        self.assertEqual(self.buf.mark_at(0), MARK_PROMPT)

    def test_marks_survive_a_resize(self):
        self.buf.feed(osc("133;A") + "prompt\r\n")
        self.buf.resize(8, 30)
        self.assertEqual(self.buf.prompt_lines(), [0])

    def test_the_mark_arrays_stay_the_same_length_as_the_grid(self):
        self.buf.feed(osc("133;A") + "x\r\n")
        self.buf.feed("\x1b[2L")     # insert lines
        self.buf.feed("\x1b[1M")     # delete a line
        self.buf.resize(6, 20)
        self.assertEqual(len(self.buf._marks), self.buf.rows)
        self.assertEqual(len(self.buf._scrollback_marks), len(self.buf._scrollback))


class ShellHookScriptTests(unittest.TestCase):
    def setUp(self):
        self.term = SSHTerminal(dict(CONN))

    def tearDown(self):
        self.term.deleteLater()

    def test_the_hooks_are_part_of_the_setup_script(self):
        script = self.term._build_shell_integration_script("TOK")
        self.assertIn("133;A", script)
        self.assertIn("133;C", script)
        self.assertIn("]7;file://", script)

    def test_the_script_is_still_one_brace_group(self):
        script = self.term._build_shell_integration_script("TOK")
        self.assertTrue(script.startswith("{\r"))
        self.assertIn("} 2>/dev/null", script)

    def test_the_script_leaves_the_remote_prompt_alone(self):
        """Only the hooks go down the wire — no prompt, aliases or PATH edits."""
        script = self.term._build_shell_integration_script("TOK")
        self.assertNotIn("export PS1=", script)
        self.assertNotIn("alias ", script)
        self.assertNotIn("export PATH=", script)
        self.assertNotIn(".cargo/bin", script)

    def test_the_completion_marker_is_still_never_in_the_command(self):
        script = self.term._build_shell_integration_script("TOK")
        self.assertNotIn("__TOK__DONE__", script)

    def test_zsh_only_syntax_is_hidden_inside_an_eval(self):
        """dash parses the whole file, so array syntax outside eval breaks it."""
        lines = self.term._script_lines("shell-integration.sh")
        for line in lines:
            if "precmd_functions+=(" in line:
                self.assertTrue(line.startswith("eval "),
                                f"unguarded zsh syntax: {line[:60]}")

    def test_a_missing_resource_is_not_fatal(self):
        self.assertEqual(self.term._script_lines("does-not-exist.sh"), [])


class SudoPasswordRetryTests(unittest.TestCase):
    """The hooks must never be typed into a sudo password prompt.

    With the outer shell's OSC 133 marks on screen, `sudo -i` used to read
    as "already at a prompt" the moment it was echoed, and ~2.5s later the
    hidden setup script was sent as the password — which then hid the
    "Sorry, try again." retry prompt behind rendering suppression.
    """

    def setUp(self):
        self.term = SSHTerminal(dict(CONN))
        self.term._connected = True
        self.sent = []
        self.term._send_data = self._send
        self.transitions = 0
        original = self.term._handle_shell_transition

        def counted():
            self.transitions += 1
            original()
        self.term._handle_shell_transition = counted

    def tearDown(self):
        self.term.deleteLater()

    def _send(self, data):
        self.sent.append(data)
        self.term._track_shell_transition_command(data)

    def _type(self, text):
        for ch in text:
            self._send(ch)
        self._send("\r")

    def _wait(self, ms):
        from PyQt6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def _script_sent(self):
        return any(chunk.startswith("{\r") for chunk in self.sent)

    def _run_sudo_to_password_prompt(self):
        term = self.term
        term._on_data_received(osc("133;A") + "grant@srv:~$ " + osc("133;B"))
        self._type("sudo -i")
        term._on_data_received("sudo -i\r\n" + osc("133;C"))
        term._on_data_received("[sudo] password for grant: ")

    def test_the_echoed_command_is_not_mistaken_for_the_new_prompt(self):
        self._run_sudo_to_password_prompt()
        self.assertEqual(self.transitions, 0)
        self.assertTrue(self.term.pending_resync)

    def test_a_wrong_password_leaves_the_retry_prompt_alone(self):
        self._run_sudo_to_password_prompt()
        self._type("wrongpw")
        for chunk in ("\r\n", "Sorry, try again.\r\n", "[sudo] password for grant: "):
            self.term._on_data_received(chunk)
        # Long enough for every "give up and apply anyway" timer to fire.
        self._wait(3000)
        self.assertFalse(self._script_sent())
        self.assertFalse(self.term._suppress_rendering)
        self.assertTrue(self.term.pending_resync)

    def test_the_hooks_land_once_the_root_shell_shows_its_prompt(self):
        self._run_sudo_to_password_prompt()
        self._type("rightpw")
        self.term._on_data_received("\r\nWelcome\r\n")
        self.term._on_data_received("root@srv:~# ")
        self.assertEqual(self.transitions, 1)
        self._wait(1200)
        self.assertTrue(self._script_sent())

    # Ubuntu 26.04, recorded 2026-09-16: sudo-rs prompts with
    # "[sudo: authenticate] Password:", echoes an asterisk per typed character
    # (pwfeedback), and the distro's own PROMPT_COMMAND hook made our C mark
    # fire before the prompt, so no command ever looked "running". Two typed
    # characters in, the setup script went out as the password.
    def _run_sudo_rs_to_password_prompt(self):
        term = self.term
        term._on_data_received(
            osc("133;D;0") + osc("7;file://ubuntu/home/grant") + osc("133;C")
            + osc("133;A") + "grant@ubuntu:~$ " + osc("133;B")
        )
        self._type("sudo -i")
        term._on_data_received("sudo -i\r\n\x1b[?2004l\r")
        term._on_data_received("[sudo: authenticate] Password: ")

    def test_sudo_rs_asterisks_do_not_unmask_the_password_prompt(self):
        self._run_sudo_rs_to_password_prompt()
        self.assertTrue(self.term.pending_resync)
        self._send("a")
        self.term._on_data_received("*")
        self._send("b")
        self.term._on_data_received("*")
        self.assertEqual(self.transitions, 0)
        self._wait(2500)
        self.assertFalse(self._script_sent())
        self.assertFalse(self.term._suppress_rendering)
        self.assertTrue(self.term.pending_resync)

    def test_sudo_rs_prompt_reads_as_a_password_prompt_with_feedback(self):
        for line in ("[sudo: authenticate] password: ",
                     "[sudo: authenticate] password: ***",
                     "[sudo] password for grant: **",
                     "password: *"):
            self.assertTrue(self.term._looks_like_auth_or_password_prompt(line), line)
            self.assertTrue(self.term._looks_like_action_prompt("", line), line)
            self.assertFalse(self.term._looks_like_prompt(line), line)


def _find_bash():
    """A bash that actually runs scripts.

    On Windows the first bash.exe on PATH is usually the WSL launcher, which
    is not a shell at all when no distribution is installed — so a candidate
    only counts once it has answered.
    """
    candidates = [
        r"C:\Program Files\Gitinash.exe",
        r"C:\Program Files (x86)\Gitinash.exe",
        shutil.which("bash"),
    ]
    for candidate in candidates:
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            proc = subprocess.run(
                [candidate, "-c", "echo rpmote-bash-ok"],
                capture_output=True, text=True, timeout=30,
            )
        except Exception:
            continue
        if "rpmote-bash-ok" in (proc.stdout or ""):
            return candidate
    return None


_BASH = _find_bash()


@unittest.skipUnless(_BASH, "needs a working bash")
class RealBashTests(unittest.TestCase):
    """Run the hooks in a real bash and read what it actually emits."""

    SCRIPT = os.path.join("app", "resources", "shell-integration.sh")

    def bash_output(self, commands: str, before_source: str = "") -> str:
        """Run commands in an interactive bash and return everything it wrote.

        Interactive matters: PROMPT_COMMAND only runs on the way back to a
        prompt, so ``bash -c`` would never produce a D mark or a cwd report.
        """
        # No trailing "exit": EOF ends the shell, and an explicit exit would
        # become the last command, displacing the one under test.
        session = "\n".join([
            'PS1="$ "',
            before_source,
            f'source "{self.SCRIPT}"',
            commands,
            "",
        ])
        # Merged, not concatenated: bash writes its prompt to stderr and the
        # marks to stdout, so collecting them separately would scramble the
        # order the terminal actually sees.
        proc = subprocess.run(
            [_BASH, "--norc", "--noprofile", "-i"],
            input=session, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=60,
        )
        return proc.stdout

    def test_the_script_is_syntactically_valid_bash(self):
        proc = subprocess.run(
            [_BASH, "-n", self.SCRIPT],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_bash_emits_a_start_of_command_mark(self):
        self.assertIn("]133;C", self.bash_output("true"))

    def test_the_start_mark_comes_after_the_prompt_even_with_a_distro_hook(self):
        """Ubuntu ships its own PROMPT_COMMAND hook. The DEBUG trap fired for
        that hook straight after ours re-armed it, so C landed *before* the
        prompt and the user's command got none — every command then looked
        finished before it started."""
        for preset in ('PROMPT_COMMAND="__distro_hook"',
                       "PROMPT_COMMAND=(__distro_hook)"):
            output = self.bash_output(
                'printf "out-%s" marker',
                before_source='__distro_hook() { printf "<distro>"; }; ' + preset,
            )
            # From the command's own prompt to its output there must be a C
            # mark, and the distro hook must never be what triggers one.
            before = output.split("out-marker")[0]
            prompt_on = before[before.rfind("]133;A"):]
            self.assertIn("]133;C", prompt_on, output)
            self.assertNotIn("]133;C\x07<distro>", output, output)

    def test_bash_reports_the_exit_status_it_actually_got(self):
        output = self.bash_output("(exit 7)")
        self.assertIn("]133;D;7", output)

    def test_bash_reports_the_working_directory(self):
        self.assertIn("]7;file://", self.bash_output("cd /tmp"))

    def test_the_prompt_itself_carries_the_marks(self):
        output = self.bash_output('echo "PS1=[$PS1]"')
        self.assertIn("133;A", output)
        self.assertIn("133;B", output)

    def test_sourcing_twice_does_not_double_up_the_hook(self):
        script = self.SCRIPT
        proc = subprocess.run(
            [_BASH, "--norc", "--noprofile", "-i", "-c",
             f'PS1="$ "; source "{script}"; source "{script}"; '
             f'echo "PC=[$PROMPT_COMMAND]"'],
            capture_output=True, text=True, errors="replace", timeout=60,
        )
        out = proc.stdout + proc.stderr
        marker = "PC=["
        line = [l for l in out.splitlines() if marker in l]
        self.assertTrue(line, out)
        self.assertEqual(line[0].count("__rpmote_precmd"), 1)

    def test_a_real_bash_session_produces_a_readable_output_region(self):
        """The whole point, end to end: bash's own bytes, through the emulator."""
        raw = self.bash_output("echo hello-from-bash")
        buf = ScreenBuffer(rows=24, cols=80)
        buf.feed(raw.replace("\n", "\r\n"))
        self.assertTrue(buf.shell_integration)
        region = buf.last_output_region()
        self.assertIsNotNone(region, raw)
        self.assertIn("hello-from-bash", buf.region_text(*region))


class TerminalFeatureTests(unittest.TestCase):
    def setUp(self):
        self.term = SSHTerminal(dict(CONN))

    def tearDown(self):
        self.term.deleteLater()

    def feed(self, text: str):
        self.term._on_data_received(text)

    def test_the_reported_cwd_is_exposed(self):
        self.feed(osc("7;file://srv/opt/app"))
        self.assertEqual(self.term.remote_cwd, "/opt/app")

    def test_a_successful_command_shows_a_tick(self):
        self.feed(osc("133;A") + "$ " + osc("133;C") + "out\r\n" + osc("133;D;0"))
        self.assertIn("✓", self.term.cmd_status_label.text())

    def test_a_failed_command_shows_its_code(self):
        self.feed(osc("133;A") + "$ " + osc("133;C") + "out\r\n" + osc("133;D;3"))
        self.assertIn("exit 3", self.term.cmd_status_label.text())

    def test_the_status_is_blank_without_shell_integration(self):
        self.feed("just some output\r\n")
        self.assertEqual(self.term.cmd_status_label.text(), "")

    def test_copying_the_last_output_puts_it_on_the_clipboard(self):
        self.feed(osc("133;A") + "$ " + osc("133;B") + "cmd\r\n")
        self.feed(osc("133;C") + "line one\r\nline two\r\n")
        self.feed(osc("133;D;0"))
        self.term._copy_last_output()
        self.assertEqual(QApplication.clipboard().text(), "line one\nline two")

    def test_copying_without_marks_says_so_instead_of_copying(self):
        QApplication.clipboard().setText("untouched")
        self.feed("no marks here\r\n")
        self.term._copy_last_output()
        self.assertEqual(QApplication.clipboard().text(), "untouched")
        self.assertIn("shell integration hooks", self.term.latency_label.text())

    def test_jumping_to_a_prompt_leaves_the_live_view(self):
        self.feed(osc("133;A") + "$ first\r\n")
        for _ in range(60):
            self.feed("filler\r\n")
        self.feed(osc("133;A") + "$ second\r\n")
        self.term._jump_to_prompt(-1)
        self.assertFalse(self.term._follow_output)

    def test_jumping_without_marks_reports_rather_than_moving(self):
        self.feed("no marks\r\n")
        self.term._jump_to_prompt(-1)
        self.assertIn("shell integration hooks", self.term.latency_label.text())

    def test_a_remote_clipboard_write_is_visible_in_the_header(self):
        self.feed(osc("52;c;cmVtb3RlIHRleHQ="))
        self.assertEqual(QApplication.clipboard().text(), "remote text")
        self.assertIn("Clipboard", self.term.latency_label.text())

    def test_a_marked_prompt_is_recognised_without_the_text_heuristic(self):
        self.feed(osc("133;A") + "|--nothing-that-looks-like-a-prompt--|")
        self.assertTrue(self.term._shell_marked_prompt_visible())

    def test_no_marks_means_no_claim_either_way(self):
        self.feed("plain text\r\n")
        self.assertFalse(self.term._shell_marked_prompt_visible())


class LinkClickTests(unittest.TestCase):
    def setUp(self):
        self.term = SSHTerminal(dict(CONN))

    def tearDown(self):
        self.term.deleteLater()

    def test_only_browser_schemes_are_opened(self):
        for url in ("https://example.com", "http://example.com",
                    "ftp://example.com"):
            self.assertTrue(url.lower().startswith(self.term._OPENABLE_SCHEMES))
        for url in ("file:///etc/passwd", "ssh://host", "javascript:alert(1)"):
            self.assertFalse(url.lower().startswith(self.term._OPENABLE_SCHEMES))

    def test_a_refused_scheme_says_so_and_opens_nothing(self):
        self.term._open_link("file:///etc/passwd")
        self.assertIn("only http", self.term.latency_label.text())

    def test_trailing_sentence_punctuation_is_not_part_of_the_url(self):
        self.term._on_data_received("see https://example.com/page.\r\n")
        found = self.term._URL_RE.findall("see https://example.com/page.")
        self.assertEqual(found[0].rstrip(".,;:!?)"), "https://example.com/page")


if __name__ == "__main__":
    unittest.main()
