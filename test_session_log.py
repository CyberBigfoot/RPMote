"""Recording a session, reading it back, and not keeping it for ever."""

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from app.session_log import (
    CastError, SessionRecorder, cast_duration, cast_text, format_size,
    list_recordings, prune, read_cast, replay_frames, safe_name,
    session_filename, session_path,
)

CONN = {"name": "acme web01", "host": "10.0.0.5"}


class NamingTests(unittest.TestCase):
    def test_a_name_cannot_escape_its_directory(self):
        self.assertEqual(safe_name("../../etc/passwd"), "etc-passwd")
        self.assertEqual(safe_name("a/b\\c"), "a-b-c")
        self.assertEqual(safe_name(""), "session")
        self.assertNotIn("/", safe_name("prod:db|01"))

    def test_a_filename_says_when_what_and_where(self):
        when = datetime(2026, 9, 9, 14, 5, 31)
        self.assertEqual(session_filename(CONN, when),
                         "2026-09-09T14-05-31_acme-web01_10.0.0.5.cast")

    def test_recordings_are_filed_by_day(self):
        when = datetime(2026, 9, 9, 14, 5, 31)
        path = session_path(CONN, when, root=Path("/tmp/x"))
        self.assertEqual(path.parent.name, "2026-09-09")

    def test_sizes_read_like_sizes(self):
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(2048), "2.0 KB")
        self.assertEqual(format_size(5 * 1024 * 1024), "5.0 MB")


class RecordingTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="rpmote-cast-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = self.dir / "session.cast"

    def recorder(self, **kwargs):
        kwargs.setdefault("started", 1000.0)
        recorder = SessionRecorder(self.path, **kwargs)
        self.addCleanup(recorder.close)
        return recorder

    def lines(self):
        return [json.loads(line)
                for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()]


class RecorderTests(RecordingTestCase):
    def test_a_session_that_says_nothing_leaves_no_file(self):
        recorder = self.recorder()
        recorder.close()
        self.assertFalse(self.path.exists())

    def test_the_header_describes_the_terminal(self):
        recorder = self.recorder(width=120, height=40, title="acme web01")
        recorder.write_output("hi", when=1000.0)
        header = self.lines()[0]
        self.assertEqual(header["version"], 2)
        self.assertEqual((header["width"], header["height"]), (120, 40))
        self.assertEqual(header["title"], "acme web01")
        self.assertEqual(header["timestamp"], 1000)

    def test_output_is_stamped_with_when_it_happened(self):
        recorder = self.recorder()
        recorder.write_output("first", when=1000.5)
        recorder.write_output("second", when=1003.25)
        self.assertEqual(self.lines()[1:],
                         [[0.5, "o", "first"], [3.25, "o", "second"]])

    def test_the_ansi_is_kept_exactly_as_it_arrived(self):
        recorder = self.recorder()
        recorder.write_output("\x1b[1;32mok\x1b[0m\r\n", when=1000.0)
        self.assertEqual(self.lines()[1][2], "\x1b[1;32mok\x1b[0m\r\n")

    def test_a_resize_is_recorded_but_only_when_it_changes_something(self):
        recorder = self.recorder(width=80, height=24)
        recorder.write_output("x", when=1000.0)
        recorder.resize(80, 24, when=1001.0)
        recorder.resize(100, 30, when=1002.0)
        kinds = [line[1] for line in self.lines()[1:]]
        self.assertEqual(kinds, ["o", "r"])
        self.assertEqual(self.lines()[-1], [2.0, "r", "100x30"])

    def test_every_write_is_on_disk_before_the_next_one(self):
        # The recording that matters most belongs to the session that just
        # died, so nothing may sit in a buffer.
        recorder = self.recorder()
        recorder.write_output("a", when=1000.0)
        self.assertEqual(len(self.lines()), 2)
        recorder.write_output("b", when=1001.0)
        self.assertEqual(len(self.lines()), 3)

    def test_it_counts_what_it_has_written(self):
        recorder = self.recorder()
        recorder.write_output("hello", when=1000.0)
        recorder.write_output("!", when=1000.1)
        self.assertEqual(recorder.bytes_written, 6)
        self.assertEqual(recorder.event_count, 2)
        self.assertTrue(recorder.active)

    def test_writing_after_close_is_ignored(self):
        recorder = self.recorder()
        recorder.write_output("a", when=1000.0)
        recorder.close()
        recorder.write_output("b", when=1001.0)
        self.assertEqual(len(self.lines()), 2)
        self.assertFalse(recorder.active)

    def test_a_directory_it_cannot_write_to_does_not_kill_the_session(self):
        recorder = SessionRecorder(self.dir / "nope\x00" / "s.cast",
                                   started=1000.0)
        self.addCleanup(recorder.close)
        recorder.write_output("hello", when=1000.0)
        self.assertTrue(recorder.failed)
        self.assertEqual(recorder.bytes_written, 0)

    @unittest.skipIf(os.name == "nt", "POSIX permissions only")
    def test_a_recording_is_not_readable_by_other_users(self):
        recorder = self.recorder()
        recorder.write_output("secret-ish", when=1000.0)
        self.assertEqual(self.path.stat().st_mode & 0o077, 0)


class ReadingTests(RecordingTestCase):
    def write(self, *lines):
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_a_recording_round_trips(self):
        recorder = self.recorder(width=90, height=30, title="t")
        recorder.write_output("\x1b[31mred\x1b[0m", when=1000.0)
        recorder.write_output(" and more", when=1002.0)
        recorder.close()

        header, events = read_cast(self.path)
        self.assertEqual(header["width"], 90)
        self.assertEqual(cast_text(events), "\x1b[31mred\x1b[0m and more")
        self.assertEqual(cast_duration(events), 2.0)

    def test_a_recording_cut_short_is_still_readable(self):
        # Exactly the recording someone needs: the session crashed mid-write.
        self.write(json.dumps({"version": 2, "width": 80, "height": 24}),
                   json.dumps([0.5, "o", "before"]),
                   '[1.5, "o", "half a li')
        _header, events = read_cast(self.path)
        self.assertEqual(cast_text(events), "before")

    def test_something_that_is_not_a_recording_is_refused(self):
        self.write("not json at all")
        with self.assertRaises(CastError):
            read_cast(self.path)

        self.write(json.dumps({"version": 1}))
        with self.assertRaises(CastError):
            read_cast(self.path)

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(CastError):
            read_cast(self.dir / "nothing-here.cast")

    def test_an_empty_recording_has_no_duration(self):
        self.assertEqual(cast_duration([]), 0.0)
        self.assertEqual(cast_text([]), "")


class ReplayTests(unittest.TestCase):
    EVENTS = [(0.0, "o", "a"), (0.5, "o", "b"), (60.0, "o", "c")]

    def test_frames_come_back_as_gaps_between_them(self):
        frames = list(replay_frames(self.EVENTS, idle_limit=None))
        self.assertEqual([round(delay, 3) for delay, _k, _p in frames],
                         [0.0, 0.5, 59.5])

    def test_dead_air_is_cut_short(self):
        frames = list(replay_frames(self.EVENTS, idle_limit=2.0))
        self.assertEqual([round(delay, 3) for delay, _k, _p in frames],
                         [0.0, 0.5, 2.0])

    def test_speed_scales_the_wait_not_the_content(self):
        frames = list(replay_frames(self.EVENTS, speed=2.0, idle_limit=None))
        self.assertEqual([round(delay, 3) for delay, _k, _p in frames],
                         [0.0, 0.25, 29.75])
        self.assertEqual([payload for _d, _k, payload in frames],
                         ["a", "b", "c"])

    def test_a_nonsense_speed_is_ignored_rather_than_dividing_by_zero(self):
        frames = list(replay_frames(self.EVENTS, speed=0, idle_limit=None))
        self.assertEqual(round(frames[1][0], 3), 0.5)


class HousekeepingTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="rpmote-prune-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def make(self, name, size=1024, age_days=0, day="2026-09-09"):
        folder = self.dir / day
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_text("x" * size, encoding="utf-8")
        when = time.time() - age_days * 86400
        os.utime(path, (when, when))
        return path

    def test_recordings_come_back_newest_first(self):
        self.make("old.cast", age_days=5)
        self.make("new.cast", age_days=0)
        self.assertEqual([row["name"] for row in list_recordings(self.dir)],
                         ["new.cast", "old.cast"])

    def test_nothing_there_is_not_an_error(self):
        self.assertEqual(list_recordings(self.dir / "nope"), [])
        self.assertEqual(prune(self.dir / "nope"), [])

    def test_old_recordings_are_swept_up(self):
        keep = self.make("keep.cast", age_days=2)
        drop = self.make("drop.cast", age_days=40)
        removed = prune(self.dir, keep_days=30, max_bytes=0)
        self.assertEqual([row["name"] for row in removed], ["drop.cast"])
        self.assertTrue(keep.exists())
        self.assertFalse(drop.exists())

    def test_the_size_cap_keeps_the_newest(self):
        self.make("newest.cast", size=600, age_days=0)
        self.make("oldest.cast", size=600, age_days=1)
        removed = prune(self.dir, keep_days=0, max_bytes=1000)
        self.assertEqual([row["name"] for row in removed], ["oldest.cast"])

    def test_the_newest_recording_is_never_swept(self):
        # Even one recording bigger than the whole cap: deleting the session
        # that just happened is never the right answer.
        self.make("huge.cast", size=4096)
        self.assertEqual(prune(self.dir, keep_days=0, max_bytes=10), [])

    def test_empty_day_folders_are_tidied_away(self):
        self.make("drop.cast", age_days=40, day="2026-01-01")
        prune(self.dir, keep_days=30, max_bytes=0)
        self.assertFalse((self.dir / "2026-01-01").exists())


if __name__ == "__main__":
    unittest.main()
