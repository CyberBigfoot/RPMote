"""Tests for the compact form rows take once they scroll into history.

Two things have to hold at once: a packed row must read back exactly like the
list of cells it replaced — nothing that walks the grid should be able to tell
the difference — and it must be small enough that a hundred thousand lines of
history is a few tens of megabytes rather than a few tens of gigabytes.
"""

import os
import sys
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.cells import (
    Cell, CellAttrs, DEFAULT_ATTRS, PackedRow, attr_pool_size, intern_attrs,
    pack_row, row_text,
)
from app.screen_buffer import ScreenBuffer


def row(text: str, **attrs) -> list:
    style = CellAttrs(**attrs)
    return [Cell(char=ch, attrs=style.copy()) for ch in text]


class ReadBackTests(unittest.TestCase):
    def test_a_packed_row_reads_back_the_same_characters(self):
        packed = pack_row(row("hello world"))
        self.assertEqual("".join(cell.char for cell in packed), "hello world")

    def test_indexing_matches_the_list_it_replaced(self):
        cells = row("abc")
        packed = pack_row(cells)
        for index in range(3):
            self.assertEqual(packed[index].char, cells[index].char)

    def test_the_row_keeps_its_full_width(self):
        cells = row("hi") + [Cell() for _ in range(78)]
        packed = pack_row(cells)
        self.assertEqual(len(packed), 80)
        self.assertEqual(packed[50].char, " ")

    def test_negative_indexing_works_like_a_list(self):
        packed = pack_row(row("abc"))
        self.assertEqual(packed[-1].char, "c")

    def test_reading_past_the_end_is_an_error_like_a_list(self):
        packed = pack_row(row("abc"))
        with self.assertRaises(IndexError):
            packed[len(packed)]

    def test_styling_survives_the_round_trip(self):
        cells = row("plain") + row("red", fg="#ff0000")
        packed = pack_row(cells)
        self.assertIsNone(packed[0].attrs.fg)
        self.assertEqual(packed[5].attrs.fg, "#ff0000")

    def test_runs_cover_exactly_the_stored_columns(self):
        packed = pack_row(row("ab") + row("cd", bold=True))
        spans = [(start, end) for start, end, _attrs in packed.runs()]
        self.assertEqual(spans, [(0, 2), (2, 4)])

    def test_a_wide_character_keeps_its_width_and_its_empty_partner(self):
        cells = [Cell(char="中", width=2), Cell(char="", width=0),
                 Cell(char="x")]
        packed = pack_row(cells)
        self.assertEqual(packed[0].char, "中")
        self.assertEqual(packed[0].width, 2)
        self.assertEqual(packed[1].char, "")
        self.assertEqual(packed[2].char, "x")

    def test_a_combining_mark_stays_with_its_base_character(self):
        cells = [Cell(char="é"), Cell(char="!")]
        packed = pack_row(cells)
        self.assertEqual(packed[0].char, "é")
        self.assertEqual(packed.text(), "é!")

    def test_packing_an_already_packed_row_is_a_no_op(self):
        packed = pack_row(row("abc"))
        self.assertIs(pack_row(packed), packed)


class TrimmingTests(unittest.TestCase):
    def test_trailing_blanks_are_not_stored(self):
        packed = pack_row(row("hi") + [Cell() for _ in range(118)])
        self.assertEqual(packed.extent, 2)
        self.assertEqual(len(packed), 120)

    def test_a_coloured_blank_is_a_painted_cell_and_survives(self):
        """A space with a background is something you can see."""
        cells = row("hi") + [Cell(char=" ", attrs=CellAttrs(bg="#00ff00"))]
        packed = pack_row(cells)
        self.assertEqual(packed.extent, 3)
        self.assertEqual(packed[2].attrs.bg, "#00ff00")

    def test_row_text_drops_the_trailing_blanks(self):
        self.assertEqual(row_text(pack_row(row("hi   "))), "hi")

    def test_row_text_handles_an_unpacked_row_too(self):
        self.assertEqual(row_text(row("hi   ")), "hi")


class SharingTests(unittest.TestCase):
    def test_identical_styles_become_one_shared_object(self):
        first = intern_attrs(CellAttrs(fg="#123456", bold=True))
        second = intern_attrs(CellAttrs(fg="#123456", bold=True))
        self.assertIs(first, second)

    def test_a_default_row_carries_no_runs_at_all(self):
        packed = pack_row(row("plain text"))
        self.assertEqual(list(packed.runs()), [(0, 10, DEFAULT_ATTRS)])
        self.assertEqual(packed._starts, ())

    def test_the_style_pool_does_not_grow_without_bound(self):
        """A truecolour gradient must not mint a style object per pixel."""
        before = attr_pool_size()
        for index in range(6000):
            intern_attrs(CellAttrs(fg=f"#{index:06x}"))
        self.assertLessEqual(attr_pool_size(), 4096)
        self.assertGreaterEqual(attr_pool_size(), before)


class SizeTests(unittest.TestCase):
    def measure(self, obj) -> int:
        """Rough deep size, following the containers we actually build."""
        seen = set()

        def walk(item):
            if id(item) in seen:
                return 0
            seen.add(id(item))
            total = sys.getsizeof(item)
            if isinstance(item, PackedRow):
                for slot in PackedRow.__slots__:
                    total += walk(getattr(item, slot))
            elif isinstance(item, (list, tuple)):
                total += sum(walk(entry) for entry in item)
            elif isinstance(item, Cell):
                total += walk(item.char) + walk(item.attrs)
            elif isinstance(item, CellAttrs):
                total += sum(walk(getattr(item, field))
                             for field in ("fg", "bg", "sgr_seq", "link"))
            return total

        return walk(obj)

    def test_packing_a_line_of_history_costs_a_fraction_of_the_cells(self):
        cells = row("root@server:~# systemctl restart nginx") + [
            Cell() for _ in range(82)]
        loose = self.measure(cells)
        packed = self.measure(pack_row(cells))
        self.assertLess(packed * 8, loose,
                        f"packed {packed} vs loose {loose} — not worth it")

    def test_a_hundred_thousand_lines_is_an_affordable_amount_of_memory(self):
        """The whole reason for packing: this used to be gigabytes."""
        line = row("2026-09-08 12:00:01 INFO  worker finished job 12345") + [
            Cell() for _ in range(69)]
        packed = pack_row(line)
        estimate = self.measure(packed) * 100_000
        self.assertLess(estimate, 200 * 1024 * 1024,
                        f"100k lines would take {estimate / 1024 / 1024:.0f} MB")


class BufferIntegrationTests(unittest.TestCase):
    def test_history_is_packed_on_the_way_in(self):
        buffer = ScreenBuffer(rows=5, cols=40, scrollback_limit=50)
        for index in range(12):
            buffer.feed(f"line {index}\r\n")
        self.assertTrue(buffer._scrollback)
        self.assertTrue(all(isinstance(entry, PackedRow)
                            for entry in buffer._scrollback))

    def test_packed_history_reads_back_through_the_buffer(self):
        buffer = ScreenBuffer(rows=5, cols=40, scrollback_limit=50)
        for index in range(12):
            buffer.feed(f"line {index}\r\n")
        self.assertEqual(buffer.line_text(0), "line 0")
        self.assertEqual(buffer.region_text(0, 3),
                         "line 0\nline 1\nline 2")

    def test_colours_survive_into_history(self):
        buffer = ScreenBuffer(rows=3, cols=40, scrollback_limit=50)
        buffer.feed("\x1b[31mred line\x1b[0m\r\n")
        for index in range(5):
            buffer.feed(f"filler {index}\r\n")
        first = buffer._scrollback[0]
        self.assertEqual(first[0].attrs.fg, buffer._scrollback[0][0].attrs.fg)
        self.assertTrue(first[0].attrs.fg)

    def test_an_archived_screen_is_packed_too(self):
        buffer = ScreenBuffer(rows=4, cols=40, scrollback_limit=50)
        buffer.feed("banner\r\nmotd\r\n")
        buffer.archive_visible_screen()
        self.assertTrue(all(isinstance(entry, PackedRow)
                            for entry in buffer._scrollback))

    def test_the_default_history_is_deep_enough_to_be_useful(self):
        """Two thousand lines was under half of one `dmesg`."""
        self.assertGreaterEqual(ScreenBuffer().scrollback_limit, 100_000)


if __name__ == "__main__":
    unittest.main()
