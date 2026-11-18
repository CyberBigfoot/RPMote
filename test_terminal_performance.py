"""Guards on the properties that made the renderer worth replacing.

These are deliberately about shape rather than wall-clock speed. A machine
under load can make any absolute number wrong, but "painting a frame costs the
same whether there are two hundred lines of history or fifty thousand" is
true or it is not, and it is the whole reason the view stopped driving a
document.
"""

import gc
import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.screen_buffer import ScreenBuffer
from app.ssh_terminal import SSHTerminal

_APP = QApplication.instance() or QApplication([])

CONN = {"name": "test", "host": "example.invalid", "port": 22, "username": "u"}


def log_lines(count: int, start: int = 0) -> str:
    return "".join(
        f"2026-09-08 12:00:{i % 60:02d} INFO  worker {i} finished job {i * 7}\r\n"
        for i in range(start, start + count)
    )


class RenderCostTests(unittest.TestCase):
    """The cost of a frame must not depend on how much has scrolled past."""

    def setUp(self):
        self.term = SSHTerminal(dict(CONN, scrollback_lines=60000))
        self.term._connected = True
        self.term._shell_integration_enabled = False
        self.term.resize(1000, 700)
        self.term.show()
        _APP.processEvents()
        self.term._screen_buf.resize(40, 120)
        self.term._force_full_render()
        self.addCleanup(self.term.deleteLater)

    def frame_time(self, repeats: int = 12) -> float:
        """Seconds for one full repaint, best of several."""
        best = float("inf")
        for _ in range(repeats):
            self.term._screen_buf.mark_all_dirty()
            start = time.perf_counter()
            self.term._render_screen()
            self.term.terminal.viewport().grab()
            best = min(best, time.perf_counter() - start)
        return best

    def test_a_frame_costs_the_same_with_a_little_history_and_a_lot(self):
        self.term._screen_buf.feed(log_lines(200))
        gc.collect()
        shallow = self.frame_time()

        self.term._screen_buf.feed(log_lines(50000, start=200))
        gc.collect()
        deep = self.frame_time()

        self.assertGreater(len(self.term._screen_buf._scrollback), 40000)
        self.assertLess(
            deep, shallow * 8 + 0.05,
            f"a frame took {deep * 1000:.1f} ms with 50k lines of history but "
            f"{shallow * 1000:.1f} ms with 200 — the cost is following the "
            f"history again")

    def test_painting_only_touches_the_rows_on_screen(self):
        """A screenful, not a historyful."""
        self.term._screen_buf.feed(log_lines(20000))
        self.term._render_screen()
        view = self.term.terminal
        self.assertLessEqual(view.visible_rows(), 60)
        self.assertGreater(view.total_lines(), 15000)


class ThroughputTests(unittest.TestCase):
    def test_plain_text_takes_the_fast_path(self):
        """A run of printable characters must not go through the ladder.

        Measured against a stream of the same length that is all control
        characters, which cannot take the fast path — the plain one has to be
        the cheaper of the two by a wide margin.
        """
        plain = ScreenBuffer(rows=40, cols=120, scrollback_limit=5000)
        text = ("x" * 100 + "\r\n") * 2000
        start = time.perf_counter()
        plain.feed(text)
        fast = time.perf_counter() - start

        slow_buffer = ScreenBuffer(rows=40, cols=120, scrollback_limit=5000)
        stepped = ("\x1b[Cx" * 25 + "\r\n") * 2000
        start = time.perf_counter()
        slow_buffer.feed(stepped)
        stepwise = time.perf_counter() - start

        self.assertLess(fast, stepwise,
                        "plain text is no cheaper than escape sequences")

    def test_a_scroll_does_not_allocate_a_new_row_every_time(self):
        """The row that leaves the top is the row that arrives at the bottom."""
        buffer = ScreenBuffer(rows=8, cols=40, scrollback_limit=100)
        buffer.feed("\r\n" * 8)
        rows_before = {id(row) for row in buffer.grid}
        buffer.feed("filler\r\n" * 8)
        rows_after = {id(row) for row in buffer.grid}
        self.assertTrue(rows_before & rows_after,
                        "every row object was replaced by a fresh one")

    def test_a_recycled_row_is_genuinely_blank(self):
        buffer = ScreenBuffer(rows=4, cols=20, scrollback_limit=100)
        buffer.feed("\x1b[31mcoloured text\x1b[0m\r\n")
        buffer.feed("\r\n" * 8)
        for row in buffer.grid:
            for cell in row:
                self.assertEqual(cell.char, " ")
                self.assertIsNone(cell.attrs.fg)

    def test_history_of_a_recycled_row_is_not_wiped_with_it(self):
        """Packing copies the text out; recycling the cells must not reach it."""
        buffer = ScreenBuffer(rows=4, cols=40, scrollback_limit=100)
        buffer.feed("important line\r\n")
        buffer.feed("filler\r\n" * 10)
        self.assertEqual(buffer.line_text(0), "important line")


class MemoryTests(unittest.TestCase):
    def test_styles_are_shared_rather_than_copied_per_cell(self):
        buffer = ScreenBuffer(rows=6, cols=40, scrollback_limit=100)
        buffer.feed("\x1b[32mgreen text here\x1b[0m")
        row = buffer.grid[0]
        self.assertIs(row[0].attrs, row[1].attrs)

    def test_the_shared_style_changes_when_the_style_does(self):
        buffer = ScreenBuffer(rows=6, cols=40, scrollback_limit=100)
        buffer.feed("a\x1b[31mb")
        row = buffer.grid[0]
        self.assertIsNot(row[0].attrs, row[1].attrs)
        self.assertIsNone(row[0].attrs.fg)
        self.assertTrue(row[1].attrs.fg)

    def test_writing_a_cell_cannot_change_the_style_of_another(self):
        """Sharing is only safe while nothing writes through a cell's style."""
        buffer = ScreenBuffer(rows=6, cols=40, scrollback_limit=100)
        buffer.feed("ab")
        buffer.feed("\x1b[1;3H\x1b[31mc")
        self.assertIsNone(buffer.grid[0][0].attrs.fg)


if __name__ == "__main__":
    unittest.main()
