"""Playing a recording back through the same emulator that drew it live."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app import session_log
from app.session_replay import ReplayPlayer, SessionReplayDialog, format_time

_APP = QApplication.instance() or QApplication([])

EVENTS = [
    (0.0, "o", "first line\r\n"),
    (1.0, "o", "\x1b[31msecond\x1b[0m\r\n"),
    (30.0, "o", "third\r\n"),
]


class TimeTests(unittest.TestCase):
    def test_times_read_like_times(self):
        self.assertEqual(format_time(0), "0:00")
        self.assertEqual(format_time(9), "0:09")
        self.assertEqual(format_time(75), "1:15")
        self.assertEqual(format_time(None), "0:00")


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.player = ReplayPlayer(EVENTS)

    def text(self):
        return self.player.buffer.get_text()

    def test_it_starts_at_the_beginning_with_a_blank_screen(self):
        self.assertEqual(self.player.index, 0)
        self.assertEqual(self.text().strip(), "")

    def test_stepping_draws_one_chunk_at_a_time(self):
        self.player.step()
        self.assertIn("first line", self.text())
        self.assertNotIn("second", self.text())
        self.player.step()
        self.assertIn("second", self.text())

    def test_it_knows_when_it_is_over(self):
        while self.player.step():
            pass
        self.assertTrue(self.player.finished)
        self.assertFalse(self.player.step())
        self.assertIn("third", self.text())

    def test_seeking_rebuilds_the_screen_at_that_point(self):
        self.player.seek(3)
        self.assertIn("third", self.text())
        self.player.seek(1)
        self.assertIn("first line", self.text())
        self.assertNotIn("second", self.text())

    def test_rewinding_forgets_the_screen_and_the_history(self):
        self.player.seek(3)
        self.player.rewind()
        self.assertEqual(self.player.index, 0)
        self.assertEqual(self.text().strip(), "")
        self.assertEqual(self.player.buffer.scrollback_total_lines, 0)

    def test_seeking_by_time_lands_on_the_right_chunk(self):
        self.player.seek_time(1.0)
        self.assertEqual(self.player.index, 2)
        self.assertIn("second", self.text())

    def test_the_position_and_length_are_reported(self):
        self.assertEqual(self.player.duration, 30.0)
        self.player.seek(2)
        self.assertEqual(self.player.position, 1.0)

    def test_dead_air_is_cut_out_of_the_playback(self):
        self.player.seek(2)
        self.assertEqual(self.player.next_delay(idle_limit=2.0), 2.0)
        self.assertEqual(self.player.next_delay(idle_limit=None), 29.0)

    def test_speed_shortens_the_wait(self):
        self.player.seek(1)
        self.assertEqual(self.player.next_delay(speed=2.0), 0.5)

    def test_only_output_is_played_not_resizes_or_notes(self):
        player = ReplayPlayer([(0.0, "o", "hi"), (1.0, "r", "80x24"),
                               (2.0, "m", "connection lost")])
        self.assertEqual(player.count, 1)

    def test_an_empty_recording_plays_to_nothing(self):
        player = ReplayPlayer([])
        self.assertTrue(player.finished)
        self.assertEqual(player.duration, 0.0)
        self.assertEqual(player.next_delay(), 0.0)


class DialogTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="rpmote-replay-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, name, events=EVENTS, width=80, height=24, title="t"):
        path = self.dir / "2026-09-09" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"version": 2, "width": width, "height": height,
                             "title": title})]
        lines += [json.dumps(list(event)) for event in events]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def dialog(self, **kwargs):
        kwargs.setdefault("root", self.dir)
        dialog = SessionReplayDialog(**kwargs)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_it_lists_what_is_there_newest_first(self):
        self.write("old.cast")
        newer = self.write("new.cast")
        os.utime(newer, (9e9, 9e9))
        dialog = self.dialog()
        self.assertEqual(dialog.list.count(), 2)
        self.assertIn("new.cast", dialog.list.item(0).text())

    def test_choosing_one_loads_it_and_sizes_the_screen_to_it(self):
        self.write("s.cast", width=120, height=40)
        dialog = self.dialog()
        self.assertEqual(dialog.player.count, 3)
        self.assertEqual(dialog.player.buffer.cols, 120)
        self.assertEqual(dialog.player.buffer.rows, 40)
        self.assertEqual(dialog.slider.maximum(), 3)

    def test_playing_advances_the_screen(self):
        self.write("s.cast")
        dialog = self.dialog()
        dialog._tick()
        self.assertIn("first line", dialog.player.buffer.get_text())

    def test_scrubbing_pauses_and_moves(self):
        self.write("s.cast")
        dialog = self.dialog()
        dialog.play()
        dialog._on_scrub(3)
        self.assertFalse(dialog._playing)
        self.assertIn("third", dialog.player.buffer.get_text())

    def test_playing_again_at_the_end_starts_over(self):
        self.write("s.cast")
        dialog = self.dialog()
        dialog.player.seek(dialog.player.count)
        dialog.play()
        self.assertEqual(dialog.player.index, 0)
        dialog.pause()

    def test_a_damaged_recording_is_reported_not_raised(self):
        broken = self.dir / "2026-09-09" / "broken.cast"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.write_text("this is not a cast file\n", encoding="utf-8")
        dialog = self.dialog()
        self.assertFalse(dialog.open_recording(broken))
        self.assertIn("cast header", dialog.status.text())

    def test_no_recordings_says_so(self):
        dialog = self.dialog()
        self.assertEqual(dialog.list.count(), 0)
        self.assertIn("No recordings", dialog.status.text())

    def test_it_can_be_opened_on_one_particular_recording(self):
        self.write("first.cast")
        wanted = self.write("wanted.cast")
        dialog = self.dialog(initial=wanted)
        self.assertIn("wanted.cast", dialog.status.text())


if __name__ == "__main__":
    unittest.main()
