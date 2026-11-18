import unittest

from app.screen_buffer import (
    CURSOR_BAR, CURSOR_BLOCK, CURSOR_UNDERLINE, ScreenBuffer,
)


class ScreenBufferTests(unittest.TestCase):
    def _archive_text(self, buffer: ScreenBuffer) -> str:
        lines = []
        for row in buffer._scrollback:
            lines.append("".join(cell.char for cell in row).rstrip())
        return "\n".join(lines)

    def test_docker_compose_status_rows_do_not_enter_scrollback(self):
        buffer = ScreenBuffer(rows=4, cols=100)

        buffer.feed("root@host:/apps# docker compose up -d\r\n")
        for index in range(10):
            buffer.feed(
                f"[+] Running 3/3\r\n"
                f"Container jellyfin Started 0.{index}s\r\n"
                f"Container prowlarr Started 0.{index}s\r\n"
                f"Network aar-stack_default Created 0.0s\r\n"
            )

        archive = self._archive_text(buffer)
        self.assertNotIn("Container jellyfin Started", archive)
        self.assertNotIn("Container prowlarr Started", archive)
        self.assertNotIn("Network aar-stack_default Created", archive)

    def test_regular_nonvolatile_rows_still_enter_scrollback(self):
        buffer = ScreenBuffer(rows=2, cols=80)

        buffer.feed("first normal line\r\n")
        buffer.feed("second normal line\r\n")
        buffer.feed("third normal line\r\n")

        self.assertIn("first normal line", self._archive_text(buffer))

    def test_compose_stop_and_remove_headers_recognized(self):
        # `docker compose stop/down` use "Stopping"/"Removing" headers that were
        # previously not recognized and could leak into scrollback.
        self.assertTrue(ScreenBuffer._is_compose_status_row("[+] Stopping 2/2"))
        self.assertTrue(ScreenBuffer._is_compose_status_row("[+] Removing 3/3"))
        self.assertTrue(ScreenBuffer._is_compose_status_row("[+] Restarting 1/2"))
        self.assertTrue(ScreenBuffer._is_compose_status_row(" Container web  Stopped 0.3s"))
        self.assertTrue(ScreenBuffer._is_compose_status_row(" Container db   Removed 0.1s"))

    def test_compose_stop_rows_do_not_enter_scrollback(self):
        buffer = ScreenBuffer(rows=4, cols=100)
        buffer.feed("root@host:/apps# docker compose stop\r\n")
        for index in range(10):
            buffer.feed(
                f"[+] Stopping {index % 3}/2\r\n"
                f" Container web  Stopping 0.{index}s\r\n"
                f" Container db   Stopping 0.{index}s\r\n"
                f"[+] Stopping 2/2\r\n"
            )
        archive = self._archive_text(buffer)
        self.assertNotIn("[+] Stopping", archive)
        self.assertNotIn("Container web  Stopping", archive)

    def test_strong_progress_frames_never_archived_even_when_nonvolatile(self):
        # Percentage / byte-ratio frames that scroll off before their redraw
        # marks them volatile must still be kept out of scrollback.
        self.assertTrue(ScreenBuffer._is_strong_transient_row("55ab12: Downloading 45%"))
        self.assertTrue(ScreenBuffer._is_strong_transient_row("layer pull 12.3 MB / 40.0 MB"))
        self.assertFalse(ScreenBuffer._is_strong_transient_row("Service started successfully"))

        buffer = ScreenBuffer(rows=2, cols=80)
        for pct in range(0, 101, 10):
            buffer.feed(f"Extracting abcd1234: {pct}%\n")  # bare LF -> non-volatile
        self.assertNotIn("%", self._archive_text(buffer))

    def test_clear_command_erases_scrollback(self):
        # The `clear` command emits ESC[3J to wipe the saved lines; without
        # handling it the visible screen blanks but the history is still there.
        buffer = ScreenBuffer(rows=3, cols=80)
        for i in range(10):
            buffer.feed(f"history line {i}\r\n")
        self.assertTrue(len(buffer._scrollback) > 0)

        buffer.feed("\x1b[3J\x1b[H\x1b[2J")  # what `clear` sends

        self.assertEqual(len(buffer._scrollback), 0)
        self.assertEqual(buffer.scrollback_total_lines, 0)
        self.assertEqual(buffer.get_text(), "")

    def test_plain_screen_erase_keeps_scrollback(self):
        # ESC[2J alone (clear visible screen) must NOT wipe the history.
        buffer = ScreenBuffer(rows=3, cols=80)
        for i in range(10):
            buffer.feed(f"history line {i}\r\n")
        before = len(buffer._scrollback)
        self.assertTrue(before > 0)

        buffer.feed("\x1b[2J")

        self.assertEqual(len(buffer._scrollback), before)


class DeferredWrapTests(unittest.TestCase):
    """The DECAWM last-column rule.

    Writing into the final column must NOT break the line straight away. A real
    terminal parks the cursor there and only wraps when the next printable
    character arrives; a CR, LF, cursor move or erase cancels the wrap instead.
    Wrapping eagerly made every full-width line occupy two rows, which silently
    doubled the height of anything that redraws a block in place.
    """

    def test_full_width_line_occupies_a_single_row(self):
        buffer = ScreenBuffer(rows=10, cols=40)
        buffer.feed("A" * 40)
        self.assertEqual(buffer.cursor_row, 0)
        self.assertEqual(buffer.cursor_col, 39)

    def test_full_width_lines_advance_one_row_each(self):
        buffer = ScreenBuffer(rows=20, cols=40)
        for index in range(5):
            buffer.feed(f"line{index}".ljust(40) + "\r\n")
        self.assertEqual(buffer.cursor_row, 5)
        rendered = ["".join(c.char for c in row).rstrip() for row in buffer.grid[:5]]
        self.assertEqual(rendered, ["line0", "line1", "line2", "line3", "line4"])

    def test_cursor_up_lands_on_the_top_of_a_full_width_block(self):
        """This is the arithmetic Docker Compose relies on for its redraw."""
        buffer = ScreenBuffer(rows=20, cols=40)
        for index in range(5):
            buffer.feed(f"line{index}".ljust(40) + "\r\n")
        buffer.feed("\x1b[5A")
        self.assertEqual(buffer.cursor_row, 0)

    def test_next_printable_character_performs_the_wrap(self):
        buffer = ScreenBuffer(rows=10, cols=10)
        buffer.feed("0123456789")
        self.assertEqual((buffer.cursor_row, buffer.cursor_col), (0, 9))
        buffer.feed("X")
        self.assertEqual((buffer.cursor_row, buffer.cursor_col), (1, 1))
        self.assertEqual(buffer.grid[1][0].char, "X")
        # the row it wrapped off must be intact
        self.assertEqual("".join(c.char for c in buffer.grid[0]), "0123456789")

    def test_carriage_return_cancels_a_pending_wrap(self):
        buffer = ScreenBuffer(rows=10, cols=10)
        buffer.feed("0123456789\rX")
        self.assertEqual(buffer.cursor_row, 0)
        self.assertEqual(buffer.grid[0][0].char, "X")

    def test_line_feed_cancels_a_pending_wrap(self):
        buffer = ScreenBuffer(rows=10, cols=10)
        buffer.feed("0123456789\r\nX")
        self.assertEqual(buffer.cursor_row, 1)
        self.assertEqual(buffer.grid[1][0].char, "X")

    def test_cursor_movement_cancels_a_pending_wrap(self):
        buffer = ScreenBuffer(rows=10, cols=10)
        buffer.feed("0123456789\x1b[3GX")
        self.assertEqual(buffer.cursor_row, 0)
        self.assertEqual(buffer.grid[0][2].char, "X")

    def test_erase_in_line_cancels_a_pending_wrap(self):
        buffer = ScreenBuffer(rows=10, cols=10)
        buffer.feed("0123456789\x1b[2KX")
        self.assertEqual(buffer.cursor_row, 0)

    def test_sgr_does_not_cancel_a_pending_wrap(self):
        """Style changes are not cursor movement, so the wrap still happens."""
        buffer = ScreenBuffer(rows=10, cols=10)
        buffer.feed("0123456789\x1b[31mX")
        self.assertEqual(buffer.cursor_row, 1)
        self.assertEqual(buffer.grid[1][0].char, "X")

    def test_autowrap_disabled_overwrites_the_last_column(self):
        buffer = ScreenBuffer(rows=10, cols=10)
        buffer.feed("\x1b[?7l")            # DECAWM off
        buffer.feed("0123456789ABC")
        self.assertEqual(buffer.cursor_row, 0)
        self.assertEqual(buffer.grid[0][9].char, "C")

    def test_wide_character_never_straddles_the_margin(self):
        buffer = ScreenBuffer(rows=10, cols=10)
        buffer.feed("012345678")           # 9 columns used, one left
        buffer.feed("界")                   # needs two columns
        self.assertEqual(buffer.cursor_row, 1)
        self.assertEqual(buffer.grid[1][0].char, "界")


class ComposeRedrawTests(unittest.TestCase):
    """Docker Compose repaints its whole progress block every ~100ms.

    Each frame walks the cursor back up over the rows it printed last time and
    reprints them, every row padded to the terminal width. If the emulator gets
    the block height wrong the cursor lands mid-block, so frames pile up instead
    of overwriting — the failure this guards against.
    """

    WIDTH = 100
    SERVICES = ["api-1", "relay-1", "proxy-1", "web-1", "db-1"]

    def _frames(self):
        """The byte stream `docker compose down` writes to a TTY."""
        total = len(self.SERVICES)
        frames = []
        printed = 0
        for done in range(total + 1):
            parts = []
            if printed:
                parts.append("\x1b[1A" * printed)   # back to the top of the block
                parts.append("\x1b[0G")
            parts.append("\x1b[?25l")
            rows = [f"[+] down {done}/{total}".ljust(self.WIDTH)]
            for index, name in enumerate(self.SERVICES):
                verb = "Removed" if index < done else "Removing"
                rows.append(f" Container livevideo-{name}   {verb}   1.1s".ljust(self.WIDTH))
            parts.append("\r\n".join(rows) + "\r\n")
            parts.append("\x1b[?25h")
            printed = len(rows)
            frames.append("".join(parts))
        return frames

    def _render(self):
        buffer = ScreenBuffer(rows=40, cols=self.WIDTH)
        buffer.feed("user@host:~/app$ docker compose down\r\n")
        for frame in self._frames():
            buffer.feed(frame)
        return buffer

    @staticmethod
    def _visible(buffer):
        lines = ["".join(c.char for c in row).rstrip() for row in buffer.grid]
        while lines and not lines[-1]:
            lines.pop()
        return lines

    def test_block_is_overwritten_in_place(self):
        buffer = self._render()
        lines = self._visible(buffer)
        self.assertEqual(lines[0], "user@host:~/app$ docker compose down")
        self.assertEqual(lines[1], "[+] down 5/5")
        self.assertEqual(len(lines), 2 + len(self.SERVICES))

    def test_only_the_final_frame_survives(self):
        buffer = self._render()
        text = "\n".join(self._visible(buffer))
        for done in range(len(self.SERVICES)):
            self.assertNotIn(f"[+] down {done}/5", text)
        self.assertNotIn("Removing", text)

    def test_no_rows_are_double_spaced(self):
        """Eager wrapping left a blank row between every Compose row."""
        buffer = self._render()
        lines = self._visible(buffer)
        self.assertNotIn("", lines)

    def test_intermediate_frames_never_reach_scrollback(self):
        buffer = self._render()
        history = "\n".join(
            "".join(c.char for c in row) for row in buffer._scrollback
        )
        self.assertNotIn("Removing", history)
        self.assertNotIn("[+] down 1/5", history)


class AlternateScreenRestoreTests(unittest.TestCase):
    def test_editor_cursor_saves_cannot_replace_the_shell_return_position(self):
        for save, restore in (("\x1b7", "\x1b8"),
                              ("\x1b[s", "\x1b[u"),
                              ("\x1b[?1048h", "\x1b[?1048l")):
            with self.subTest(save=repr(save)):
                buffer = ScreenBuffer(rows=12, cols=40)
                buffer.feed("\x1b[10;1Huser@host$ nano file\r\n\x1b[32m")
                saved_color = buffer.current_attrs.fg
                buffer.feed("\x1b[?1049h\x1b[6;20H\x1b[31m" + save)
                buffer.feed("\x1b[Hmenu" + restore + "edit")
                self.assertEqual((buffer.cursor_row, buffer.cursor_col), (5, 23))
                buffer.feed("\x1b[?1049luser@host$ ")
                self.assertEqual((buffer.cursor_row, buffer.cursor_col), (10, 11))
                self.assertEqual(buffer.grid[10][0].char, "u")
                self.assertEqual(buffer.current_attrs.fg, saved_color)
                self.assertNotIn("edit", buffer.get_text())

    def test_repeated_screen_switches_do_not_move_the_shell_cursor(self):
        buffer = ScreenBuffer(rows=12, cols=40)
        buffer.feed("\x1b[10;1H\x1b[?1049h\x1b[6;20H\x1b[?1049h")
        buffer.feed("\x1b[?1049l")
        self.assertEqual((buffer.cursor_row, buffer.cursor_col), (9, 0))
        buffer.feed("prompt$ \x1b[?1049l")
        self.assertEqual((buffer.cursor_row, buffer.cursor_col), (9, 8))

    def test_resize_during_editor_preserves_shell_content_and_saved_cursor(self):
        buffer = ScreenBuffer(rows=12, cols=40)
        for index in range(10):
            buffer.feed(f"line {index}\r\n")
        buffer.feed("user@host$ nano file\r\n\x1b[?1049h")
        buffer.feed("\x1b[6;20H\x1b7editor")
        buffer.resize(6, 30)
        buffer.feed("\x1b[?1049luser@host$ ")
        self.assertEqual((buffer.cursor_row, buffer.cursor_col), (5, 11))
        self.assertEqual(buffer.line_text(0), "line 0")
        self.assertIn("user@host$ nano file", buffer.get_text())
        self.assertEqual("".join(cell.char for cell in buffer.grid[5]).rstrip(),
                         "user@host$")

    def test_1048_restores_pen_and_origin_like_dec_save_restore(self):
        buffer = ScreenBuffer(rows=12, cols=40)
        buffer.feed("\x1b[32m\x1b[?6h\x1b[5;7H\x1b[?1048h")
        saved_color = buffer.current_attrs.fg
        buffer.feed("\x1b[31m\x1b[?6l\x1b[H\x1b[?1048l")
        self.assertEqual((buffer.cursor_row, buffer.cursor_col), (4, 6))
        self.assertEqual(buffer.current_attrs.fg, saved_color)
        self.assertTrue(buffer.origin_mode)


class CursorShapeTests(unittest.TestCase):
    """DECSCUSR — how a program says which cursor it wants.

    Worth having because the shape is how vim, fish and zsh's vi bindings tell
    you which mode you are in; without it they all look like they are in the
    same one.
    """

    def setUp(self):
        self.buffer = ScreenBuffer(rows=5, cols=20)

    def test_the_cursor_starts_as_a_blinking_block(self):
        self.assertEqual(self.buffer.cursor_shape, CURSOR_BLOCK)
        self.assertTrue(self.buffer.cursor_blinks)

    def test_a_bar_cursor_can_be_asked_for(self):
        self.buffer.feed("[5 q")
        self.assertEqual(self.buffer.cursor_shape, CURSOR_BAR)
        self.assertTrue(self.buffer.cursor_blinks)

    def test_a_steady_bar_is_different_from_a_blinking_one(self):
        self.buffer.feed("[6 q")
        self.assertEqual(self.buffer.cursor_shape, CURSOR_BAR)
        self.assertFalse(self.buffer.cursor_blinks)

    def test_an_underline_cursor_can_be_asked_for(self):
        self.buffer.feed("[3 q")
        self.assertEqual(self.buffer.cursor_shape, CURSOR_UNDERLINE)

    def test_zero_means_the_default_block(self):
        self.buffer.feed("[5 q")
        self.buffer.feed("[0 q")
        self.assertEqual(self.buffer.cursor_shape, CURSOR_BLOCK)

    def test_an_undefined_shape_leaves_the_cursor_alone(self):
        self.buffer.feed("[5 q")
        self.buffer.feed("[99 q")
        self.assertEqual(self.buffer.cursor_shape, CURSOR_BAR)

    def test_the_space_is_what_makes_it_a_cursor_request(self):
        """`CSI 2 q` is load-LEDs; only `CSI 2 SP q` sets the cursor.

        The parser used to throw the intermediate byte away, which made the
        two indistinguishable.
        """
        self.buffer.feed("[5 q")
        self.buffer.feed("[2q")
        self.assertEqual(self.buffer.cursor_shape, CURSOR_BAR)

    def test_leaving_a_full_screen_program_restores_the_shape(self):
        self.buffer.feed("[?1049h[5 q")
        self.buffer.feed("[?1049l")
        self.assertEqual(self.buffer.cursor_shape, CURSOR_BLOCK)

    def test_a_full_reset_restores_the_shape(self):
        self.buffer.feed("[5 qc")
        self.assertEqual(self.buffer.cursor_shape, CURSOR_BLOCK)


class CharacterWidthTests(unittest.TestCase):
    """Widths have to agree with the wcwidth the remote laid its output out with.

    Any other answer puts our columns out of step with the ones it drew, and
    the screen tears apart in a way that looks like a rendering bug but is
    really an arithmetic one.
    """

    def width(self, ch):
        return ScreenBuffer._char_width(ch)

    def test_ordinary_text_is_one_cell(self):
        self.assertEqual(self.width("a"), 1)

    def test_a_cjk_glyph_takes_two_cells(self):
        self.assertEqual(self.width("中"), 2)

    def test_a_combining_accent_takes_none(self):
        self.assertEqual(self.width("́"), 0)

    def test_a_variation_selector_takes_none(self):
        """U+FE0F has combining class 0, so the old check let it through.

        Counting it as a cell shifted everything after an emoji by one column.
        """
        self.assertEqual(self.width("️"), 0)

    def test_a_zero_width_joiner_takes_none(self):
        self.assertEqual(self.width("‍"), 0)

    def test_an_emoji_takes_two_cells_the_way_wcwidth_says(self):
        self.assertEqual(self.width("😀"), 2)

    def test_a_combining_mark_attaches_to_the_character_before_it(self):
        buffer = ScreenBuffer(rows=3, cols=10)
        buffer.feed("éx")
        self.assertEqual(buffer.grid[0][0].char, "é")
        self.assertEqual(buffer.grid[0][1].char, "x")

    def test_an_emoji_keeps_its_presentation_selector(self):
        buffer = ScreenBuffer(rows=3, cols=10)
        buffer.feed("ℹ️!")
        self.assertEqual(buffer.grid[0][0].char, "ℹ️")
        self.assertEqual(buffer.grid[0][1].char, "!")

    def test_a_flood_of_combining_marks_cannot_grow_a_cell_without_limit(self):
        """The Zalgo trick, which is a denial of service if nothing stops it."""
        buffer = ScreenBuffer(rows=3, cols=10)
        buffer.feed("a" + "́" * 5000)
        self.assertLessEqual(len(buffer.grid[0][0].char),
                             buffer.MAX_CLUSTER_LENGTH)

    def test_widths_are_cached_but_the_cache_is_bounded(self):
        for code in range(0x4E00, 0x4E00 + 500):
            self.width(chr(code))
        self.assertLessEqual(len(ScreenBuffer._WIDTH_CACHE), 8192)


class FullResetTests(unittest.TestCase):
    """ESC c means forget everything — including the colour you were using."""

    def test_a_reset_puts_the_colours_back_to_default(self):
        buffer = ScreenBuffer(rows=3, cols=10)
        buffer.feed("\x1b[31mred")
        self.assertIsNotNone(buffer.grid[0][0].attrs.fg)
        buffer.feed("\x1bcplain")
        self.assertIsNone(buffer.grid[0][0].attrs.fg)

    def test_a_reset_empties_the_screen_and_the_history(self):
        buffer = ScreenBuffer(rows=3, cols=10, scrollback_limit=100)
        for index in range(10):
            buffer.feed(f"line {index}\r\n")
        self.assertTrue(buffer.scrollback_total_lines)
        buffer.feed("\x1bc")
        self.assertEqual(buffer.scrollback_total_lines, 0)
        self.assertEqual(buffer.get_text().strip(), "")
        self.assertEqual((buffer.cursor_row, buffer.cursor_col), (0, 0))


if __name__ == "__main__":
    unittest.main()
