"""Dropping a file on the terminal, and where it is allowed to land."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QMimeData, QUrl
from PyQt6.QtWidgets import QApplication

from app.terminal_upload import (
    FALLBACK_DIR, TerminalUploader, describe_drop, local_paths_from_mime,
    normalize_remote_dir, remote_target,
)

_APP = QApplication.instance() or QApplication([])


class DestinationTests(unittest.TestCase):
    def test_a_plain_directory_is_used_as_it_is(self):
        self.assertEqual(normalize_remote_dir("/srv/app"), "/srv/app")
        self.assertEqual(normalize_remote_dir("/"), "/")

    def test_extra_separators_are_tidied(self):
        self.assertEqual(normalize_remote_dir("/srv//app/./"), "/srv/app")

    def test_anything_a_shell_could_lie_about_falls_back(self):
        # The path arrives from the remote machine, so it is untrusted input.
        for value in ("../../etc", "srv/app", "C:\\Windows", "/srv/../../etc",
                      "/srv\nrm -rf /", "", None, "~"):
            self.assertEqual(normalize_remote_dir(value), FALLBACK_DIR, value)

    def test_a_file_lands_in_that_directory_under_its_own_name(self):
        self.assertEqual(remote_target("/srv/app", "C:\\temp\\notes.txt"),
                         "/srv/app/notes.txt")
        self.assertEqual(remote_target("/srv/app", "/home/me/notes.txt"),
                         "/srv/app/notes.txt")

    def test_a_path_with_no_filename_is_refused(self):
        with self.assertRaises(ValueError):
            remote_target("/srv", "")

    def test_the_confirmation_says_what_and_where(self):
        message = describe_drop(["/home/me/notes.txt"], "/srv/app")
        self.assertIn("notes.txt", message)
        self.assertIn("/srv/app", message)

    def test_an_unknown_directory_is_described_rather_than_shown_as_a_dot(self):
        self.assertIn("working directory", describe_drop(["/a/b.txt"], ""))

    def test_several_files_are_counted(self):
        self.assertIn("3 items",
                      describe_drop(["a.txt", "b.txt", "c.txt"], "/srv"))

    def test_nothing_dropped_says_so(self):
        self.assertEqual(describe_drop([], "/srv"), "Nothing to upload.")


class MimeTests(unittest.TestCase):
    def test_local_files_come_out_of_a_drag(self):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(__file__)])
        # Qt hands paths back with forward slashes, on Windows too.
        self.assertEqual([os.path.normpath(path)
                          for path in local_paths_from_mime(mime)],
                         [os.path.normpath(__file__)])

    def test_a_web_url_is_not_a_file(self):
        mime = QMimeData()
        mime.setUrls([QUrl("https://example.com/thing.txt")])
        self.assertEqual(local_paths_from_mime(mime), [])

    def test_a_path_that_is_not_there_is_dropped(self):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("/definitely/not/here.txt")])
        self.assertEqual(local_paths_from_mime(mime), [])

    def test_a_drag_with_no_urls_yields_nothing(self):
        self.assertEqual(local_paths_from_mime(QMimeData()), [])
        self.assertEqual(local_paths_from_mime(None), [])


class UploaderTests(unittest.TestCase):
    def setUp(self):
        self.uploader = TerminalUploader()

    def test_nothing_to_upload_starts_nothing(self):
        self.assertFalse(self.uploader.upload(object(), [], "/srv"))
        self.assertFalse(self.uploader.upload(None, [__file__], "/srv"))
        self.assertFalse(self.uploader.busy)

    def test_a_file_is_put_where_the_shell_is_standing(self):
        put = []

        class FakeSFTP:
            def put(self, local, remote):
                put.append((local, remote))

            def close(self):
                pass

        class FakeClient:
            def open_sftp(self):
                return FakeSFTP()

        results = []
        self.uploader.finished.connect(
            lambda done, total, errors: results.append((done, total, errors)))
        self.assertTrue(self.uploader.upload(FakeClient(), [__file__], "/srv/app"))
        self.uploader._thread.join(timeout=10)
        _APP.processEvents()

        self.assertEqual(put, [(__file__, "/srv/app/" + os.path.basename(__file__))])
        self.assertEqual(results, [(1, 1, [])])

    def test_a_failure_is_reported_rather_than_raised(self):
        class FakeClient:
            def open_sftp(self):
                raise OSError("no channel available")

        results = []
        self.uploader.finished.connect(
            lambda done, total, errors: results.append((done, total, errors)))
        self.uploader.upload(FakeClient(), [__file__], "/srv")
        self.uploader._thread.join(timeout=10)
        _APP.processEvents()

        self.assertEqual(results[0][0], 0)
        self.assertIn("no channel available", results[0][2][0])


if __name__ == "__main__":
    unittest.main()
