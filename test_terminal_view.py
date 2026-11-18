"""Tests for the cell renderer: what it draws, and what it lets you grab.

Painting is checked by rendering into a pixmap and reading pixels back, which
is the only way to tell the difference between "the code ran" and "the right
thing appeared on screen". Everything else — selection, hit-testing, cursor
shapes — is checked through the view's own API.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QColor, QFont, QMouseEvent, QPixmap
from PyQt6.QtWidgets import QApplication

from app.screen_buffer import (
    CURSOR_BAR, CURSOR_BLOCK, CURSOR_UNDERLINE, ScreenBuffer,
)
from app.terminal_view import TerminalView, WORD_CHARS

_APP = QApplication.instance() or QApplication([])


def mouse(kind, x, y, button=Qt.MouseButton.LeftButton, modifiers=Qt.KeyboardModifier.NoModifier):
    held = button if kind == QMouseEvent.Type.MouseButtonPress else Qt.MouseButton.NoButton
    if kind == QMouseEvent.Type.MouseMove:
        held = button
        button = Qt.MouseButton.NoButton
    return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), button, held, modifiers)


class ViewTestCase(unittest.TestCase):
    ROWS = 10
    COLS = 40

    def setUp(self):
        self.buffer = ScreenBuffer(rows=self.ROWS, cols=self.COLS,
                                   scrollback_limit=500)
        self.view = TerminalView()
        self.view.set_terminal_font(QFont("Courier New", 12))
        self.view.set_buffer(self.buffer)
        self.view.resize(640, 400)
        self.view.show()
        self.view.activateWindow()
        self.view.setFocus()
        _APP.processEvents()
        self.addCleanup(self.view.deleteLater)

    def feed(self, text):
        self.buffer.feed(text)
        self.view.refresh()

    def paint(self) -> QPixmap:
        return self.view.viewport().grab()

    def pixel(self, col, row) -> QColor:
        """Colour at the middle of a cell, in viewport coordinates."""
        image = self.paint().toImage()
        x = int(self.view.margin + (col + 0.5) * self.view.cell_width)
        y = int((row + 0.5) * self.view.cell_height)
        return QColor(image.pixel(x, y))

    def cell_pixels(self, col, row) -> list:
        """Every pixel of a cell, for asking whether any ink landed in it."""
        image = self.paint().toImage()
        left = int(self.view.margin + col * self.view.cell_width)
        top = row * self.view.cell_height
        return [QColor(image.pixel(x, y))
                for x in range(left, left + max(1, int(self.view.cell_width)))
                for y in range(top, top + self.view.cell_height)
                if 0 <= x < image.width() and 0 <= y < image.height()]

    def point_of(self, col, row) -> tuple:
        return (self.view.margin + (col + 0.5) * self.view.cell_width,
                (row + 0.5) * self.view.cell_height)


class PaintingTests(ViewTestCase):
    def test_the_background_is_the_theme_background(self):
        self.view.set_colors("#ffffff", "#102030")
        self.assertEqual(self.pixel(35, 8).name(), "#102030")

    def test_a_coloured_cell_background_is_actually_painted(self):
        self.view.set_colors("#ffffff", "#000000")
        self.feed("\x1b[41m    \x1b[0m")
        painted = self.pixel(1, 0)
        self.assertGreater(painted.red(), 80)
        self.assertLess(painted.blue(), 80)

    def test_text_is_drawn_in_its_foreground_colour(self):
        self.view.set_colors("#ffffff", "#000000")
        self.feed("\x1b[32m########\x1b[0m")
        # Somewhere in a solid block of hashes there must be green ink.
        green = [colour for col in range(8)
                 for colour in self.cell_pixels(col, 0)
                 if colour.green() > colour.red() and colour.green() > 60]
        self.assertTrue(green, "no green pixels where green text was drawn")

    def test_a_reversed_cell_swaps_ink_and_paper(self):
        self.view.set_colors("#ffffff", "#000000")
        self.feed("\x1b[7m    \x1b[0m")
        self.assertGreater(self.pixel(1, 0).lightness(), 200)

    def test_nothing_is_painted_outside_the_rows_that_exist(self):
        self.view.set_colors("#ffffff", "#101010")
        self.feed("only one line")
        self.assertEqual(self.pixel(2, self.ROWS + 2).name(), "#101010")

    def test_history_is_drawn_above_the_screen(self):
        for index in range(30):
            self.feed(f"line {index}\r\n")
        self.view.scroll_to_line(0, follow=False)
        self.assertEqual(self.view.line_text(0), "line 0")
        self.paint()  # must not raise while history is on screen


class CursorTests(ViewTestCase):
    def setUp(self):
        super().setUp()
        self.view.set_colors("#ffffff", "#000000", cursor="#ff00ff")
        self.view.setFocus()
        self.view.cursor_enabled = True
        self.view._cursor_blink_on = True

    def test_a_block_cursor_fills_its_cell(self):
        self.assertTrue(self.view.hasFocus(), "the view never took focus")
        self.buffer.cursor_shape = CURSOR_BLOCK
        self.feed("")
        colour = self.pixel(0, 0)
        self.assertGreater(colour.red(), 150)
        self.assertGreater(colour.blue(), 150)

    def test_a_bar_cursor_does_not_fill_the_cell(self):
        """vim in insert mode asks for a bar; drawing a block there is wrong."""
        self.buffer.feed("\x1b[5 q")
        self.view.refresh()
        self.assertEqual(self.buffer.cursor_shape, CURSOR_BAR)
        # The middle of the cell stays background — the bar is at the edge.
        self.assertLess(self.pixel(0, 0).red(), 100)

    def test_an_underline_cursor_leaves_the_middle_alone(self):
        self.buffer.feed("\x1b[4 q")
        self.view.refresh()
        self.assertEqual(self.buffer.cursor_shape, CURSOR_UNDERLINE)
        self.assertLess(self.pixel(0, 0).red(), 100)

    def test_a_steady_cursor_ignores_the_blink_timer(self):
        self.buffer.feed("\x1b[2 q")
        self.view.set_cursor_blink(False)
        self.assertFalse(self.buffer.cursor_blinks)
        self.assertGreater(self.pixel(0, 0).red(), 150)

    def test_a_blinking_cursor_disappears_between_blinks(self):
        self.buffer.feed("\x1b[1 q")
        self.view.set_cursor_blink(False)
        self.assertTrue(self.buffer.cursor_blinks)
        self.assertLess(self.pixel(0, 0).red(), 100)

    def test_hiding_the_cursor_hides_it(self):
        self.feed("\x1b[?25l")
        self.assertLess(self.pixel(0, 0).red(), 100)

    def test_a_reset_puts_the_cursor_shape_back(self):
        self.buffer.feed("\x1b[5 q")
        self.buffer.feed("\x1bc")
        self.assertEqual(self.buffer.cursor_shape, CURSOR_BLOCK)


class HitTestingTests(ViewTestCase):
    def test_a_point_maps_to_the_cell_under_it(self):
        x, y = self.point_of(7, 3)
        self.assertEqual(self.view.cell_at(QPoint(int(x), int(y))), (3, 7))

    def test_the_screen_cell_is_one_based_for_mouse_reports(self):
        x, y = self.point_of(7, 3)
        self.assertEqual(self.view.screen_cell_at(QPoint(int(x), int(y))),
                         (8, 4))

    def test_hit_testing_accounts_for_the_scroll_position(self):
        for index in range(40):
            self.feed(f"line {index}\r\n")
        self.view.scroll_to_line(5, follow=False)
        x, y = self.point_of(0, 2)
        doc_index, _col = self.view.cell_at(QPoint(int(x), int(y)))
        self.assertEqual(doc_index, 7)
        self.assertEqual(self.view.line_text(doc_index), "line 7")

    def test_a_point_past_the_last_column_clamps_to_the_grid(self):
        doc_index, col = self.view.cell_at(QPoint(10_000, 0))
        self.assertEqual(col, self.COLS - 1)

    def test_the_style_under_a_point_is_readable(self):
        self.feed("\x1b[31mred\x1b[0m")
        self.assertTrue(self.view.attrs_at(self.view.screen_top_line(), 0).fg)


class SelectionTests(ViewTestCase):
    def drag(self, from_cell, to_cell, modifiers=Qt.KeyboardModifier.NoModifier):
        x0, y0 = self.point_of(*from_cell)
        x1, y1 = self.point_of(*to_cell)
        self.view.mousePressEvent(
            mouse(QMouseEvent.Type.MouseButtonPress, x0, y0, modifiers=modifiers))
        self.view.mouseMoveEvent(
            mouse(QMouseEvent.Type.MouseMove, x1, y1, modifiers=modifiers))
        self.view.mouseReleaseEvent(
            mouse(QMouseEvent.Type.MouseButtonRelease, x1, y1))

    def test_dragging_across_a_line_selects_what_it_covers(self):
        self.feed("hello world")
        self.drag((0, 0), (5, 0))
        self.assertEqual(self.view.selected_text(), "hello")

    def test_a_selection_spanning_lines_joins_them_with_newlines(self):
        self.feed("first\r\nsecond\r\nthird\r\n")
        self.drag((0, 0), (5, 2))
        self.assertEqual(self.view.selected_text().splitlines(),
                         ["first", "second", "third"])

    def test_trailing_blanks_are_not_dragged_along(self):
        self.feed("short\r\nlonger line here\r\n")
        self.drag((0, 0), (16, 1))
        self.assertEqual(self.view.selected_text(),
                         "short\nlonger line here")

    def test_alt_makes_the_drag_rectangular(self):
        """Lifting one column out of `docker ps` is the whole point."""
        self.feed("aaa BBB ccc\r\nddd EEE fff\r\nggg HHH iii\r\n")
        self.drag((4, 0), (7, 2), modifiers=Qt.KeyboardModifier.AltModifier)
        self.assertEqual(self.view.selected_text().splitlines(),
                         ["BBB", "EEE", "HHH"])

    def test_a_plain_drag_is_not_rectangular(self):
        self.feed("aaa BBB ccc\r\nddd EEE fff\r\n")
        self.drag((4, 0), (7, 1))
        self.assertEqual(self.view.selected_text().splitlines(),
                         ["BBB ccc", "ddd EEE"])

    def test_double_click_takes_the_whole_word(self):
        self.feed("run /usr/local/bin/thing now")
        x, y = self.point_of(8, 0)
        self.view.mouseDoubleClickEvent(
            mouse(QMouseEvent.Type.MouseButtonDblClick, x, y))
        self.assertEqual(self.view.selected_text(), "/usr/local/bin/thing")

    def test_a_word_is_more_than_letters_and_digits(self):
        for ch in "/-._~:@":
            self.assertIn(ch, WORD_CHARS)

    def test_double_clicking_a_gap_selects_just_that_cell(self):
        self.feed("a b")
        x, y = self.point_of(1, 0)
        self.view.mouseDoubleClickEvent(
            mouse(QMouseEvent.Type.MouseButtonDblClick, x, y))
        self.assertEqual(self.view.selected_text(), "")

    def test_triple_click_takes_the_whole_line(self):
        """Qt reports the third click as an ordinary press, not a gesture."""
        self.feed("some long command with arguments\r\n")
        x, y = self.point_of(6, 0)
        self.view.mouseDoubleClickEvent(
            mouse(QMouseEvent.Type.MouseButtonDblClick, x, y))
        self.assertEqual(self.view.selected_text(), "long")
        self.view.mousePressEvent(
            mouse(QMouseEvent.Type.MouseButtonPress, x, y))
        self.assertEqual(self.view.selected_text(),
                         "some long command with arguments")

    def test_a_slow_third_click_is_just_a_click(self):
        self.feed("some long command\r\n")
        x, y = self.point_of(6, 0)
        self.view.mouseDoubleClickEvent(
            mouse(QMouseEvent.Type.MouseButtonDblClick, x, y))
        self.view._last_double_click = 0.0
        self.view.mousePressEvent(
            mouse(QMouseEvent.Type.MouseButtonPress, x, y))
        self.assertEqual(self.view.selected_text(), "")

    def test_dragging_after_a_double_click_keeps_whole_words(self):
        self.feed("alpha beta gamma delta")
        x, y = self.point_of(7, 0)
        self.view.mouseDoubleClickEvent(
            mouse(QMouseEvent.Type.MouseButtonDblClick, x, y))
        x2, y2 = self.point_of(13, 0)
        self.view.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, x2, y2))
        self.assertEqual(self.view.selected_text(), "beta gamma")

    def test_dragging_backwards_after_a_double_click_grows_the_selection(self):
        """The word first picked has to stay inside it, not collapse away."""
        self.feed("alpha beta gamma delta")
        x, y = self.point_of(13, 0)
        self.view.mouseDoubleClickEvent(
            mouse(QMouseEvent.Type.MouseButtonDblClick, x, y))
        self.assertEqual(self.view.selected_text(), "gamma")
        x2, y2 = self.point_of(2, 0)
        self.view.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, x2, y2))
        self.assertEqual(self.view.selected_text(), "alpha beta gamma")

    def test_dragging_backwards_after_a_triple_click_keeps_whole_lines(self):
        self.feed("first line\r\nsecond line\r\nthird line\r\n")
        x, y = self.point_of(2, 2)
        self.view.mouseDoubleClickEvent(
            mouse(QMouseEvent.Type.MouseButtonDblClick, x, y))
        self.view.mousePressEvent(
            mouse(QMouseEvent.Type.MouseButtonPress, x, y))
        self.assertEqual(self.view.selected_text(), "third line")
        x2, y2 = self.point_of(2, 0)
        self.view.mouseMoveEvent(mouse(QMouseEvent.Type.MouseMove, x2, y2))
        self.assertEqual(self.view.selected_text().splitlines(),
                         ["first line", "second line", "third line"])

    def test_select_all_takes_history_as_well_as_the_screen(self):
        for index in range(20):
            self.feed(f"line {index}\r\n")
        self.view.select_all()
        lines = self.view.selected_text().splitlines()
        self.assertIn("line 0", lines)
        self.assertIn("line 19", lines)

    def test_clearing_a_selection_leaves_nothing_selected(self):
        self.feed("hello")
        self.drag((0, 0), (4, 0))
        self.assertTrue(self.view.has_selection())
        self.view.clear_selection()
        self.assertFalse(self.view.has_selection())
        self.assertEqual(self.view.selected_text(), "")

    def test_a_selection_is_painted(self):
        self.view.set_colors("#ffffff", "#000000", selection="#4060ff")
        self.feed("hello world")
        plain = self.pixel(20, 0)
        self.drag((0, 0), (30, 0))
        selected = self.pixel(20, 0)
        self.assertNotEqual(plain.name(), selected.name())

    def test_the_view_does_not_scroll_while_a_drag_is_in_progress(self):
        for index in range(30):
            self.feed(f"line {index}\r\n")
        x, y = self.point_of(0, 1)
        self.view.mousePressEvent(mouse(QMouseEvent.Type.MouseButtonPress, x, y))
        parked = self.view.top_line()
        for index in range(10):
            self.buffer.feed(f"flood {index}\r\n")
        self.view.refresh()
        self.view.scroll_to_line(self.view.total_lines(), follow=True)
        self.assertEqual(self.view.top_line(), parked)

    def test_letting_go_catches_the_view_back_up(self):
        for index in range(30):
            self.feed(f"line {index}\r\n")
        x, y = self.point_of(0, 1)
        self.view.mousePressEvent(mouse(QMouseEvent.Type.MouseButtonPress, x, y))
        for index in range(10):
            self.buffer.feed(f"flood {index}\r\n")
        self.view.refresh()
        self.view.mouseReleaseEvent(
            mouse(QMouseEvent.Type.MouseButtonRelease, x, y))
        vbar = self.view.verticalScrollBar()
        self.assertEqual(vbar.value(), vbar.maximum())


class WideCharacterTests(ViewTestCase):
    def test_a_double_width_glyph_occupies_two_columns(self):
        self.feed("中x")
        row = self.buffer.grid[0]
        self.assertEqual(row[0].char, "中")
        self.assertEqual(row[1].char, "")
        self.assertEqual(row[2].char, "x")

    def test_runs_skip_the_empty_half_of_a_wide_glyph(self):
        self.feed("中x")
        runs = self.view._row_runs(self.buffer.grid[0], self.COLS)
        text = "".join(piece for _s, _e, _a, piece in runs)
        self.assertEqual(text, "中x")

    def test_runs_stop_at_the_last_visible_cell(self):
        """Most of a terminal is blank; drawing it would be most of the work."""
        self.feed("hi")
        runs = self.view._row_runs(self.buffer.grid[0], self.COLS)
        self.assertEqual(runs[-1][1], 2)

    def test_a_combining_mark_rides_with_its_base(self):
        self.feed("éx")
        runs = self.view._row_runs(self.buffer.grid[0], self.COLS)
        self.assertEqual("".join(p for _s, _e, _a, p in runs), "éx")

    def test_painting_a_line_of_emoji_does_not_raise(self):
        self.feed("ok 👍🏽 done ✅ 🇬🇧")
        self.paint()


class ScrollbarTests(ViewTestCase):
    def test_the_scrollbar_counts_lines_not_pixels(self):
        for index in range(50):
            self.feed(f"line {index}\r\n")
        vbar = self.view.verticalScrollBar()
        self.assertEqual(vbar.maximum(),
                         max(0, self.view.total_lines() - self.view.visible_rows()))

    def test_scrolling_away_stops_following_output(self):
        for index in range(50):
            self.feed(f"line {index}\r\n")
        self.view.verticalScrollBar().setValue(3)
        self.assertFalse(self.view.follow_output)

    def test_scrolling_back_to_the_bottom_follows_again(self):
        for index in range(50):
            self.feed(f"line {index}\r\n")
        vbar = self.view.verticalScrollBar()
        vbar.setValue(3)
        vbar.setValue(vbar.maximum())
        self.assertTrue(self.view.follow_output)


class PlaceholderTests(ViewTestCase):
    """The "connecting" sign has to come down once the session is there."""

    BACKGROUND = "#101010"

    def setUp(self):
        super().setUp()
        self.view.set_colors("#ffffff", self.BACKGROUND)
        self.view.set_placeholder("Connecting to remote host...")

    def sign_pixels(self) -> int:
        """Ink in the band across the middle, where the sign is centred.

        Nothing else is drawn there: the grid is ten rows tall at the top of
        a much taller viewport, and blank rows paint no ink at all.
        """
        image = self.paint().toImage()
        background = QColor(self.BACKGROUND).rgb()
        middle = image.height() // 2
        return sum(1
                   for y in range(middle - 10, middle + 10)
                   for x in range(image.width())
                   if image.pixel(x, y) != background)

    def test_the_sign_shows_while_nothing_has_arrived(self):
        self.assertGreater(self.sign_pixels(), 0)

    def test_a_first_prompt_takes_the_sign_down(self):
        # The bug as reported: one prompt on row one and no scrollback behind
        # it, and the sign went on hanging over a live session.
        self.feed("root@grant:~# ")
        self.assertEqual(self.sign_pixels(), 0)

    def test_a_single_dirty_row_still_clears_the_whole_sign(self):
        # The sign spans the viewport, so repainting only the row the prompt
        # landed on would leave the rest of it on screen.
        self.buffer.feed("x")
        self.view.refresh(all_dirty=False, dirty_rows=[0])
        self.assertEqual(self.sign_pixels(), 0)

    def test_the_sign_does_not_return_when_the_screen_is_cleared(self):
        self.feed("hello")
        self.feed("\x1b[2J\x1b[H")
        self.assertEqual(self.sign_pixels(), 0)

    def test_a_full_screen_program_counts_as_output(self):
        self.buffer.feed("\x1b[?1049h")
        self.view.refresh()
        self.assertEqual(self.sign_pixels(), 0)


class ConnectingSignTests(unittest.TestCase):
    """A connected but silent shell is still connected."""

    def test_connecting_is_shown_before_the_session_is_up(self):
        self.assertTrue(self.terminal().terminal._placeholder)

    def test_the_sign_goes_when_the_connection_lands(self):
        term = self.terminal()
        term._on_connected()
        self.assertEqual(term.terminal._placeholder, "")

    def terminal(self):
        from app.ssh_terminal import SSHTerminal
        term = SSHTerminal({"name": "t", "host": "h", "port": 22,
                            "username": "u"})
        self.addCleanup(term.deleteLater)
        return term


class ScrollbackSettingTests(unittest.TestCase):
    """The depth is per connection, and the terminal honours what it is told."""

    def terminal(self, **overrides):
        from app.ssh_terminal import SSHTerminal
        term = SSHTerminal(dict(
            {"name": "t", "host": "h", "port": 22, "username": "u"},
            **overrides))
        self.addCleanup(term.deleteLater)
        return term

    def test_a_connection_gets_the_default_depth(self):
        self.assertGreaterEqual(
            self.terminal()._screen_buf.scrollback_limit, 100_000)

    def test_a_connection_can_ask_for_a_different_depth(self):
        self.assertEqual(
            self.terminal(scrollback_lines=500)._screen_buf.scrollback_limit,
            500)

    def test_zero_means_keep_no_history(self):
        term = self.terminal(scrollback_lines=0)
        self.assertEqual(term._screen_buf.scrollback_limit, 0)
        for index in range(20):
            term._screen_buf.feed(f"line {index}\r\n")
        self.assertEqual(term._screen_buf._scrollback, [])

    def test_a_nonsense_setting_falls_back_to_the_default(self):
        self.assertGreaterEqual(
            self.terminal(scrollback_lines="lots")._screen_buf.scrollback_limit,
            100_000)

    def test_the_dialog_offers_the_setting(self):
        from app.connection_manager import ConnectionDialog
        self.assertTrue(hasattr(ConnectionDialog, "_populate"))
        import inspect
        source = inspect.getsource(ConnectionDialog)
        self.assertIn("scrollback_lines", source)


if __name__ == "__main__":
    unittest.main()
