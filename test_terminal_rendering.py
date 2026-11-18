"""Tests for how the SSH terminal puts the virtual screen on screen.

The view is a grid of cells painted straight out of the buffer: history first,
then exactly one line per screen row, with the first of those rows pinned to
the top of the viewport. These tests pin down the invariants that keep a
redrawing program from making the text jump, that put the shell back where it
belongs when a full-screen program exits, and that keep a selection on the
content it was made over.
"""

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QApplication

from app.screen_buffer import ScreenBuffer
from app.ssh_terminal import SSHTerminal

_APP = QApplication.instance() or QApplication([])

CONN = {"name": "test", "host": "example.invalid", "port": 22, "username": "u"}


class RenderingTestCase(unittest.TestCase):
    ROWS = 12
    COLS = 40
    HISTORY = 200

    def setUp(self):
        self.term = SSHTerminal(dict(CONN, scrollback_lines=self.HISTORY))
        self.term._connected = True
        self.term._shell_integration_enabled = False
        self.term.resize(700, 400)
        self.term._screen_buf.resize(self.ROWS, self.COLS)
        self.term._force_full_render()

    def tearDown(self):
        self.term._connected = False
        self.term.deleteLater()

    def feed(self, text: str):
        """Push data through the screen buffer and repaint."""
        self.term._screen_buf.feed(text)
        self.term._render_screen()

    @property
    def view(self):
        return self.term.terminal

    def line(self, doc_index: int) -> str:
        return self.view.line_text(doc_index)


class ViewShapeTests(RenderingTestCase):
    def test_the_view_is_history_plus_one_line_per_screen_row(self):
        self.feed("hello\r\n")
        self.assertEqual(
            self.view.total_lines(),
            len(self.term._screen_buf._scrollback) + self.ROWS,
        )

    def test_the_line_count_never_changes_while_a_program_redraws(self):
        """A full-screen redraw must not resize the view.

        The blank status row at the bottom appearing and disappearing used to
        add and remove a line, which moved every line above it — the text
        "jumping up and down".
        """
        self.feed("\x1b[?1049h")
        counts = set()
        anchors = set()
        for step in range(6):
            self.feed("\x1b[H\x1b[2J")
            for row in range(self.ROWS - 1):
                self.feed(f"\x1b[{row + 1};1Hrow {row}")
            if step % 2:
                self.feed(f"\x1b[{self.ROWS};1Hstatus line")
            counts.add(self.view.total_lines())
            anchors.add(self.view.screen_top_line())

        self.assertEqual(counts, {self.ROWS})
        self.assertEqual(anchors, {0})

    def test_alternate_screen_hides_history_and_gives_it_back(self):
        for index in range(30):
            self.feed(f"line {index}\r\n")
        history = self.view.screen_top_line()
        self.assertGreater(history, 0)

        self.feed("\x1b[?1049h")
        self.assertEqual(self.view.total_lines(), self.ROWS)

        self.feed("\x1b[?1049l")
        self.assertEqual(self.view.screen_top_line(), history)
        self.assertEqual(self.view.total_lines(), history + self.ROWS)

    def test_what_the_view_reads_back_is_what_the_emulator_holds(self):
        buffer = self.term._screen_buf
        for index in range(buffer.scrollback_limit + 40):
            buffer.feed(f"line {index}\r\n")
        self.term._render_screen()
        self.assertEqual(len(buffer._scrollback), buffer.scrollback_limit)
        self.assertEqual(
            self.view.total_lines(),
            len(buffer._scrollback) + self.ROWS,
        )
        self.assertEqual(self.line(0), buffer.line_text(0))

    def test_a_redrawn_row_reads_back_as_the_new_text(self):
        self.feed("\x1b[5;1Hfirst")
        self.assertIn("first", self.line(self.view.screen_top_line() + 4))
        self.feed("\x1b[5;1H\x1b[2Ksecond")
        self.assertEqual(self.line(self.view.screen_top_line() + 4), "second")


class ViewportAnchorTests(RenderingTestCase):
    def test_leaving_a_full_screen_program_returns_to_the_live_screen(self):
        """Quitting nano must land on the prompt, not in the middle of history."""
        for index in range(40):
            self.feed(f"line {index}\r\n")

        self.feed("\x1b[?1049h")
        self.feed("\x1b[H\x1b[2Jnano is editing")
        self.feed("\x1b[?1049l")
        self.feed("user@host:~$ ")

        vbar = self.view.verticalScrollBar()
        self.assertEqual(vbar.value(),
                         min(self.view.screen_top_line(), vbar.maximum()))

    def test_scrolling_up_stops_the_view_following_output(self):
        for index in range(40):
            self.feed(f"line {index}\r\n")

        vbar = self.view.verticalScrollBar()
        vbar.setValue(max(0, vbar.maximum() - 10))
        self.assertFalse(self.term._follow_output)

        parked = vbar.value()
        self.feed("more output\r\n")
        self.assertEqual(vbar.value(), parked)

    def test_a_parked_view_stays_on_the_same_line_while_history_trims(self):
        """History falling off the top must not slide the view sideways.

        The scrollbar counts document lines, which shift when the oldest line
        is dropped; the view holds its place in absolute line numbers and
        corrects for the difference.
        """
        buffer = self.term._screen_buf
        for index in range(buffer.scrollback_limit + 5):
            buffer.feed(f"line {index}\r\n")
        self.term._render_screen()

        self.view.scroll_to_line(20, follow=False)
        parked = self.line(20)
        self.assertTrue(parked)

        for index in range(10):
            buffer.feed(f"more {index}\r\n")
        self.term._render_screen()

        self.assertEqual(self.line(self.view.top_line()), parked)

    def test_typing_snaps_the_view_back_to_the_live_screen(self):
        self.term._follow_output = False
        self.term._send_data("x")
        self.assertTrue(self.term._follow_output)


class EditorStreamTests(RenderingTestCase):
    def test_editor_output_survives_every_split_in_screen_entry(self):
        # SSH may split a screen switch at any byte. File contents can look
        # like download progress, but must never be rewritten as a status bar.
        for entry in ("\x1b[?1049h", "\x1b[?25;1049h"):
            for split in range(len(entry) + 1):
                with self.subTest(entry=repr(entry), split=split):
                    self.term._screen_buf.feed("\x1bc")
                    self.term._progress_coalescer.reset()
                    self.term._on_data_received(entry[:split])
                    self.term._on_data_received(
                        entry[split:] + "\x1b[H\x1b[2JGNU nano\r\nStarting the service")
                    self.assertTrue(self.term._screen_buf._using_alt)
                    self.assertEqual(self.term._screen_buf.get_text(),
                                     "GNU nano\nStarting the service")

    def test_progress_before_partial_entry_cannot_swallow_the_switch(self):
        self.term._on_data_received("Downloading 15%\x1b[?104")
        self.term._on_data_received("9h\x1b[H\x1b[2JGNU nano\r\nStarting the service")
        self.assertTrue(self.term._screen_buf._using_alt)
        self.assertEqual(self.term._screen_buf.get_text(), "GNU nano\nStarting the service")

    def test_return_from_editor_renders_prompt_at_saved_shell_position(self):
        for index in range(40):
            self.term._on_data_received(f"line {index}\r\n")
        before = (self.term._screen_buf.cursor_row, self.term._screen_buf.cursor_col)
        self.term._on_data_received("\x1b[?1049h\x1b[6;20H\x1b7edit")
        self.term._on_data_received("\x1b[?1049")
        self.term._on_data_received("luser@host$ ")
        self.term._render_screen()
        buffer = self.term._screen_buf
        self.assertEqual((buffer.cursor_row, buffer.cursor_col),
                         (before[0], before[1] + 11))
        self.assertEqual(self.line(self.view.screen_top_line() + before[0]),
                         "user@host$")
        self.assertNotIn("edit", buffer.get_text())


class DamageTrackingTests(RenderingTestCase):
    def test_only_touched_rows_are_reported_as_dirty(self):
        buffer = self.term._screen_buf
        buffer.consume_dirty()
        buffer.feed("\x1b[3;1Hedited")
        all_dirty, rows = buffer.consume_dirty()
        self.assertFalse(all_dirty)
        self.assertEqual(rows, {2})

    def test_a_scroll_marks_the_whole_screen(self):
        buffer = self.term._screen_buf
        buffer.feed("\r\n" * (self.ROWS + 2))
        buffer.consume_dirty()
        buffer.feed("\r\n")
        all_dirty, _ = buffer.consume_dirty()
        self.assertTrue(all_dirty)

    def test_a_render_leaves_nothing_dirty_behind(self):
        self.feed("something\r\n")
        self.assertFalse(self.term._screen_buf.dirty)


class SelectionTests(RenderingTestCase):
    def select(self, first_line: int, last_line: int):
        view = self.view
        dropped = view.dropped_lines()
        view._sel_anchor = (dropped + first_line, 0)
        view._sel_head = (dropped + last_line, self.COLS)
        view.selection_changed.emit()

    def test_a_selection_reads_back_the_lines_it_covers(self):
        for index in range(6):
            self.feed(f"line {index}\r\n")
        self.select(1, 3)
        self.assertEqual(self.view.selected_text().splitlines(),
                         ["line 1", "line 2", "line 3"])

    def test_a_selection_in_history_survives_new_output(self):
        for index in range(30):
            self.feed(f"line {index}\r\n")
        self.select(2, 2)
        selected = self.view.selected_text()
        self.assertTrue(selected)

        self.feed("fresh output\r\n")
        self.assertEqual(self.view.selected_text(), selected)

    def test_a_selection_survives_history_being_trimmed(self):
        """The old view stored offsets into a document and had to fix them up
        every time the top line was dropped. Absolute line numbers make it a
        non-event."""
        buffer = self.term._screen_buf
        for index in range(buffer.scrollback_limit + 5):
            buffer.feed(f"line {index}\r\n")
        self.term._render_screen()

        self.select(40, 41)
        selected = self.view.selected_text()

        for index in range(20):
            buffer.feed(f"more {index}\r\n")
        self.term._render_screen()
        self.assertEqual(self.view.selected_text(), selected)

    def test_output_arriving_mid_drag_no_longer_stops_the_rendering(self):
        """The five-second freeze is gone.

        It existed because arriving output moved the document under the
        pointer. Now output keeps painting during a drag; what pauses is the
        auto-scroll, so the ground stays still without anything going stale.
        """
        self.feed("before\r\n")
        view = self.view
        view.mousePressEvent(_press(view, 10, view.cell_height + 2))
        self.assertTrue(view.is_selecting)

        before = view.top_line()
        self.term._screen_buf.feed("during the drag\r\n")
        self.term._render_screen()

        # Painted, not deferred: the new line is readable straight away.
        self.assertIn("during the drag", self.term._screen_buf.get_text())
        self.assertEqual(view.top_line(), before, "the view scrolled mid-drag")

        view.mouseReleaseEvent(_release(view, 10, view.cell_height + 2))
        self.assertFalse(view.is_selecting)

    def test_the_cursor_is_still_drawn_while_text_is_selected(self):
        """A block cursor and a selection are two different colours of paint.

        They used to fight — both were "extra selections" on one document, so
        the cursor had to be withdrawn whenever anything was highlighted.
        """
        self.feed("prompt$ ")
        self.select(0, 0)
        self.term._sync_cursor_overlay()
        self.assertTrue(self.view.cursor_enabled)


def _press(view, x, y):
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtCore import Qt as _Qt
    return QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(x, y), QPointF(x, y),
        _Qt.MouseButton.LeftButton, _Qt.MouseButton.LeftButton,
        _Qt.KeyboardModifier.NoModifier)


def _release(view, x, y):
    from PyQt6.QtCore import QPointF
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtCore import Qt as _Qt
    return QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, QPointF(x, y), QPointF(x, y),
        _Qt.MouseButton.LeftButton, _Qt.MouseButton.NoButton,
        _Qt.KeyboardModifier.NoModifier)


class TerminalReplyTests(unittest.TestCase):
    def test_cursor_position_report_is_answered(self):
        replies = []
        buffer = ScreenBuffer(rows=10, cols=20)
        buffer.response_callback = replies.append
        buffer.feed("\x1b[5;7Habc")
        buffer.feed("\x1b[6n")
        self.assertEqual(replies, ["\x1b[5;10R"])

    def test_device_attributes_are_answered(self):
        replies = []
        buffer = ScreenBuffer(rows=10, cols=20)
        buffer.response_callback = replies.append
        buffer.feed("\x1b[c")
        buffer.feed("\x1b[>c")
        self.assertEqual(len(replies), 2)
        self.assertTrue(replies[0].startswith("\x1b[?6"))
        self.assertTrue(replies[1].startswith("\x1b[>"))

    def test_a_crashed_full_screen_program_cannot_break_the_shell(self):
        buffer = ScreenBuffer(rows=10, cols=20)
        buffer.feed("\x1b[?1049h\x1b[?1h\x1b[?1000h\x1b[?25l")
        self.assertTrue(buffer.application_cursor_keys)
        buffer.feed("\x1b[?1049l")
        self.assertFalse(buffer.application_cursor_keys)
        self.assertEqual(buffer.mouse_tracking_mode, 0)
        self.assertTrue(buffer.cursor_visible)


class ResizeTests(unittest.TestCase):
    def test_shrinking_keeps_the_prompt_on_screen(self):
        buffer = ScreenBuffer(rows=10, cols=20)
        for index in range(9):
            buffer.feed(f"line {index}\r\n")
        buffer.feed("prompt$ ")
        self.assertEqual(buffer.cursor_row, 9)

        buffer.resize(5, 20)
        self.assertEqual(buffer.cursor_row, 4)
        self.assertEqual(buffer.get_last_nonempty_line(), "prompt$")
        self.assertIn("line 0", buffer.line_text(0))


class KeyEncodingTests(unittest.TestCase):
    def test_modifier_parameter_matches_xterm(self):
        modifier = SSHTerminal._xterm_modifier
        self.assertEqual(modifier(False, False, False, False), 1)
        self.assertEqual(modifier(True, False, False, False), 2)
        self.assertEqual(modifier(False, False, True, False), 5)
        self.assertEqual(modifier(True, False, True, False), 6)


if __name__ == "__main__":
    unittest.main()
