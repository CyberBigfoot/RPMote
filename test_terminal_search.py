"""Tests for searching the scrollback.

What the old search could do was move a cursor to the next hit. What it could
not do was say how many there were, take a regular expression, match case on
demand, show you the other hits, or tell you it had wrapped past the end —
which is most of what makes searching a long session useful.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.screen_buffer import ScreenBuffer
from app.ssh_terminal import SSHTerminal
from app.terminal_view import SearchOptions, TerminalSearch

_APP = QApplication.instance() or QApplication([])

CONN = {"name": "test", "host": "example.invalid", "port": 22, "username": "u"}


def buffer_with(*lines, rows=6, cols=60, limit=500) -> ScreenBuffer:
    buffer = ScreenBuffer(rows=rows, cols=cols, scrollback_limit=limit)
    for line in lines:
        buffer.feed(line + "\r\n")
    return buffer


class MatchingTests(unittest.TestCase):
    def test_every_hit_is_found_not_just_the_next_one(self):
        buffer = buffer_with("alpha", "beta", "alpha again", "gamma")
        search = TerminalSearch().run(buffer, "alpha")
        self.assertEqual(search.count, 2)

    def test_two_hits_on_one_line_are_both_found(self):
        buffer = buffer_with("error and error again")
        search = TerminalSearch().run(buffer, "error")
        self.assertEqual(search.count, 2)

    def test_hits_are_grouped_by_line_for_highlighting(self):
        buffer = buffer_with("x x x")
        search = TerminalSearch().run(buffer, "x")
        spans = next(iter(search.by_line.values()))
        self.assertEqual(spans, [(0, 1), (2, 3), (4, 5)])

    def test_searching_is_case_insensitive_by_default(self):
        buffer = buffer_with("ERROR: something")
        self.assertEqual(TerminalSearch().run(buffer, "error").count, 1)

    def test_matching_case_can_be_asked_for(self):
        buffer = buffer_with("ERROR: something", "error: something else")
        search = TerminalSearch().run(
            buffer, "error", SearchOptions(case_sensitive=True))
        self.assertEqual(search.count, 1)

    def test_a_regular_expression_is_treated_as_one_when_asked(self):
        buffer = buffer_with("code 404", "code 500", "code abc")
        search = TerminalSearch().run(
            buffer, r"code \d+", SearchOptions(regex=True))
        self.assertEqual(search.count, 2)

    def test_regex_characters_are_literal_unless_regex_is_on(self):
        buffer = buffer_with("a.b", "axb")
        self.assertEqual(TerminalSearch().run(buffer, "a.b").count, 1)

    def test_whole_word_does_not_match_inside_a_longer_word(self):
        buffer = buffer_with("cat", "category", "the cat sat")
        search = TerminalSearch().run(buffer, "cat", SearchOptions(whole_word=True))
        self.assertEqual(search.count, 2)

    def test_a_broken_pattern_is_reported_rather_than_raised(self):
        buffer = buffer_with("anything")
        search = TerminalSearch().run(buffer, "a[b", SearchOptions(regex=True))
        self.assertEqual(search.count, 0)
        self.assertTrue(search.error)
        self.assertEqual(search.describe(), "bad pattern")

    def test_an_empty_query_matches_nothing(self):
        buffer = buffer_with("anything")
        self.assertEqual(TerminalSearch().run(buffer, "").count, 0)

    def test_a_pattern_that_can_match_nothing_does_not_hang(self):
        """`x*` matches an empty string at every position."""
        buffer = buffer_with("xxx yyy")
        search = TerminalSearch().run(buffer, "x*", SearchOptions(regex=True))
        self.assertEqual(search.count, 1)

    def test_history_and_the_live_screen_are_both_searched(self):
        buffer = buffer_with(*[f"line {i}" for i in range(40)], rows=6)
        self.assertTrue(buffer._scrollback, "nothing scrolled off")
        search = TerminalSearch().run(buffer, "line 0")
        self.assertEqual(search.count, 1)
        search = TerminalSearch().run(buffer, "line 39")
        self.assertEqual(search.count, 1)

    def test_the_alternate_screen_is_searched_on_its_own(self):
        """vim owns the window; the shell history underneath is not its text."""
        buffer = buffer_with(*[f"secret {i}" for i in range(40)], rows=6)
        buffer.feed("\x1b[?1049h\x1b[H\x1b[2Jediting a file")
        search = TerminalSearch().run(buffer, "secret")
        self.assertEqual(search.count, 0)
        self.assertEqual(TerminalSearch().run(buffer, "editing").count, 1)


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.buffer = buffer_with("hit one", "miss", "hit two", "hit three")
        self.search = TerminalSearch().run(self.buffer, "hit")

    def test_stepping_forward_walks_the_hits_in_order(self):
        first = self.search.step(1)
        second = self.search.step(1)
        self.assertLess(first[0], second[0])

    def test_stepping_past_the_end_wraps_and_says_so(self):
        # The first press selects the first hit rather than advancing past it,
        # so it takes one more press than there are hits to come back round.
        for _ in range(self.search.count + 1):
            self.search.step(1)
        self.assertTrue(self.search.wrapped)
        self.assertEqual(self.search.index, 0)

    def test_stepping_back_from_the_first_hit_wraps_to_the_last(self):
        self.search.step(1)
        self.search.step(-1)
        self.assertTrue(self.search.wrapped)
        self.assertEqual(self.search.index, self.search.count - 1)

    def test_the_label_counts_the_hits(self):
        self.search.step(1)
        self.assertEqual(self.search.describe(), "1 of 3")

    def test_the_label_says_when_it_wrapped(self):
        for _ in range(self.search.count + 1):
            self.search.step(1)
        self.assertIn("wrapped", self.search.describe())

    def test_the_label_is_honest_when_there_is_nothing(self):
        self.assertEqual(TerminalSearch().run(self.buffer, "zzz").describe(),
                         "no matches")

    def test_stepping_with_no_hits_returns_nothing_rather_than_failing(self):
        empty = TerminalSearch().run(self.buffer, "zzz")
        self.assertIsNone(empty.step(1))

    def test_the_nearest_hit_can_be_picked_without_moving_to_it(self):
        match = self.search.select_nearest(2)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(match[0], 2)


class SearchUITests(unittest.TestCase):
    def setUp(self):
        self.term = SSHTerminal(dict(CONN, scrollback_lines=300))
        self.term._connected = True
        self.term._shell_integration_enabled = False
        self.term.resize(700, 400)
        self.term._screen_buf.resize(8, 40)
        for index in range(30):
            self.term._screen_buf.feed(f"line {index}\r\n")
        self.term._render_screen()
        self.addCleanup(self.term.deleteLater)

    def search_for(self, text, **flags):
        self.term.search_case.setChecked(flags.get("case", False))
        self.term.search_word.setChecked(flags.get("word", False))
        self.term.search_regex.setChecked(flags.get("regex", False))
        self.term.search_input.setText(text)
        self.term._run_search(move=False)

    def test_typing_a_query_finds_and_counts_the_hits(self):
        self.search_for("line 1")
        self.assertEqual(self.term.terminal.search.count, 11)
        self.assertIn("of 11", self.term.search_status.text())

    def test_the_regex_toggle_reaches_the_search(self):
        self.search_for(r"line \d$", regex=True)
        self.assertEqual(self.term.terminal.search.count, 10)

    def test_enter_moves_to_the_next_hit_and_shows_where_it_is(self):
        self.search_for("line 2")
        self.term._search_next()
        self.assertEqual(self.term.search_status.text(), "1 of 11")
        self.term._search_next()
        self.assertEqual(self.term.search_status.text(), "2 of 11")

    def test_moving_to_a_hit_scrolls_it_into_view(self):
        self.search_for("line 0")
        self.term.terminal.scroll_to_line(
            self.term.terminal.total_lines(), follow=True)
        self.term._search_next()
        view = self.term.terminal
        match = view.search.current()
        doc_index = match[0] - view.dropped_lines()
        self.assertLessEqual(view.top_line(), doc_index)
        self.assertLess(doc_index, view.top_line() + view.visible_rows())

    def test_finding_a_hit_stops_the_view_chasing_live_output(self):
        self.search_for("line 0")
        self.term._search_next()
        self.assertFalse(self.term._follow_output)

    def test_closing_the_search_clears_the_highlights(self):
        self.search_for("line")
        self.assertTrue(self.term.terminal.search.matches)
        self.term._close_search()
        self.assertFalse(self.term.terminal.search.matches)
        self.assertFalse(self.term.search_bar.isVisible())
        self.assertEqual(self.term.search_status.text(), "")

    def test_new_output_does_not_lose_your_place(self):
        self.search_for("line 5")
        self.term._search_next()
        position = self.term.terminal.search.current()
        self.term._screen_buf.feed("line 5 again\r\n")
        self.term._run_search(move=False)
        self.assertEqual(self.term.terminal.search.current()[0], position[0])

    def test_a_bad_pattern_says_so_instead_of_blowing_up(self):
        self.search_for("(unclosed", regex=True)
        self.assertEqual(self.term.search_status.text(), "bad pattern")


if __name__ == "__main__":
    unittest.main()
