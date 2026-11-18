"""Downloads from the Library file browser: off the GUI thread, with a
running count of what is done and what is left."""

import os
import shutil
import stat
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.remote_file_browser import RemoteFileBrowser

_APP = QApplication.instance() or QApplication([])


class _Attr:
    def __init__(self, name, mode, size=0):
        self.filename = name
        self.st_mode = mode
        self.st_size = size


class _Recorder:
    """In-memory SFTP: `tree` maps a directory to {name: size | dict}."""

    CHUNK = 4

    def __init__(self, tree, fail=()):
        self.tree = tree
        self.fail = set(fail)
        self.gets = []
        self.closed = False

    def _lookup(self, path):
        node = self.tree
        for part in [p for p in path.split("/") if p]:
            node = node[part]
        return node

    def listdir_attr(self, path):
        out = []
        for name, node in self._lookup(path).items():
            if isinstance(node, dict):
                out.append(_Attr(name, stat.S_IFDIR | 0o755))
            elif node == "link":
                out.append(_Attr(name, stat.S_IFLNK | 0o777, 3))
            else:
                out.append(_Attr(name, stat.S_IFREG | 0o644, node))
        return out

    def stat(self, path):
        node = self._lookup(path)
        if isinstance(node, dict):
            return _Attr(path, stat.S_IFDIR | 0o755)
        return _Attr(path, stat.S_IFREG | 0o644, node)

    def get(self, remote, local, callback=None):
        if remote in self.fail:
            raise IOError("Permission denied")
        size = self._lookup(remote)
        self.gets.append(remote)
        with open(local, "wb") as handle:
            handle.write(b"x" * size)
        if callback:
            for done in range(self.CHUNK, size + self.CHUNK, self.CHUNK):
                callback(min(done, size), size)

    def close(self):
        self.closed = True


class _FakeTransport:
    def __init__(self, active=True):
        self.active = active

    def is_active(self):
        return self.active


class FakeSSHClient:
    def __init__(self, recorder, transport=None):
        self._recorder = recorder
        # paramiko hands back None once the client is closed; a fresh fake
        # is connected unless a test says otherwise.
        self._transport = _FakeTransport() if transport is None else transport

    def open_sftp(self):
        return self._recorder

    def get_transport(self):
        return self._transport


TREE = {
    "srv": {
        "notes.txt": 10,
        "assets": {
            "img": {"logo.png": 40, "icon.svg": 8},
            "empty": {},
            "shortcut": "link",
        },
    }
}


class DownloadEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rpmote_down_")
        self.browser = RemoteFileBrowser()
        self.recorder = _Recorder(TREE)
        self.browser._sftp = object()          # "connected" for the guard
        self.browser._ssh_client = FakeSSHClient(self.recorder)
        self.progress = []
        self.finished = []
        self.browser.download_progress.connect(
            lambda text, permille: self.progress.append((text, permille))
        )
        self.browser.download_finished.connect(
            lambda done, total, errors: self.finished.append((done, total, list(errors)))
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.browser.deleteLater()

    def _download(self, jobs):
        # run the worker body inline so the test stays deterministic
        self.browser._run_download(jobs)
        return self.finished[-1]

    def test_a_folder_comes_down_with_its_structure(self):
        done, total, errors = self._download(
            [("/srv/assets", os.path.join(self.tmp, "assets"), True)]
        )
        self.assertEqual((done, total, errors), (2, 2, []))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "assets", "img", "logo.png")))
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "assets", "img", "icon.svg")))
        # An empty remote folder still appears locally.
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "assets", "empty")))
        # Symlinks are skipped rather than followed.
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "assets", "shortcut")))
        self.assertTrue(self.recorder.closed)

    def test_files_and_folders_download_together(self):
        done, total, errors = self._download([
            ("/srv/notes.txt", os.path.join(self.tmp, "notes.txt"), False),
            ("/srv/assets", os.path.join(self.tmp, "assets"), True),
        ])
        self.assertEqual((done, total, errors), (3, 3, []))
        self.assertEqual(
            self.recorder.gets,
            ["/srv/notes.txt", "/srv/assets/img/logo.png", "/srv/assets/img/icon.svg"],
        )

    def test_status_counts_down_what_is_left(self):
        self._download([
            ("/srv/notes.txt", os.path.join(self.tmp, "notes.txt"), False),
            ("/srv/assets", os.path.join(self.tmp, "assets"), True),
        ])
        texts = [text for text, _p in self.progress]
        self.assertEqual(texts[0], "Counting files…")
        self.assertIn("Downloading 1/3 (2 left): notes.txt — 0% of 10 B", texts)
        self.assertIn("Downloading 2/3 (1 left): logo.png — 0% of 40 B", texts)
        # The last file has nothing left after it.
        self.assertIn("Downloading 3/3: icon.svg — 0% of 8 B", texts)
        # Chunk callbacks report the current file's percentage.
        self.assertIn("Downloading 3/3: icon.svg — 100% of 8 B", texts)

    def test_the_bar_tracks_bytes_across_the_whole_batch(self):
        self._download([
            ("/srv/notes.txt", os.path.join(self.tmp, "notes.txt"), False),
            ("/srv/assets", os.path.join(self.tmp, "assets"), True),
        ])
        permilles = [p for _t, p in self.progress]
        self.assertEqual(permilles[0], -1)              # still counting
        self.assertEqual(permilles[-1], 1000)           # everything landed
        # Monotonic once counting is over: the bar never runs backwards.
        self.assertEqual(permilles[1:], sorted(permilles[1:]))
        # 10 of 58 bytes done when the second file starts.
        self.assertIn(int(10 * 1000 / 58), permilles)

    def test_a_failed_file_is_reported_and_the_rest_still_arrive(self):
        self.recorder.fail.add("/srv/assets/img/logo.png")
        done, total, errors = self._download(
            [("/srv/assets", os.path.join(self.tmp, "assets"), True)]
        )
        self.assertEqual((done, total), (1, 2))
        self.assertEqual(errors, ["logo.png: Permission denied"])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "assets", "img", "icon.svg")))

    def test_a_dead_transport_stops_the_batch_between_files(self):
        self.browser._ssh_client = FakeSSHClient(self.recorder, _FakeTransport(active=False))
        done, total, errors = self._download(
            [("/srv/assets", os.path.join(self.tmp, "assets"), True)]
        )
        self.assertEqual((done, total), (0, 2))
        self.assertEqual(errors, [RemoteFileBrowser.CANCELLED])
        self.assertEqual(self.recorder.gets, [])

    def test_a_live_transport_lets_the_batch_run(self):
        self.browser._ssh_client = FakeSSHClient(self.recorder, _FakeTransport(active=True))
        done, _total, errors = self._download(
            [("/srv/notes.txt", os.path.join(self.tmp, "notes.txt"), False)]
        )
        self.assertEqual((done, errors), (1, []))

    def test_progress_emits_are_throttled_but_file_starts_always_show(self):
        big = _Recorder({"srv": {"big.bin": 4000}})
        self.browser._ssh_client = FakeSSHClient(big)
        self.browser.PROGRESS_INTERVAL = 3600      # nothing but forced emits
        self._download([("/srv/big.bin", os.path.join(self.tmp, "big.bin"), False)])
        texts = [text for text, _p in self.progress]
        self.assertEqual(texts, [
            "Counting files…",
            "Downloading 1/1: big.bin — 0% of 3.9 KB",
            "Downloading 1/1: big.bin — 100% of 3.9 KB",
        ])

    def test_second_download_is_refused_while_one_is_running(self):
        self.browser._download_busy = True
        self.browser._start_download([("/srv/notes.txt", os.path.join(self.tmp, "n"), False)])
        self.assertIn("already running", self.browser.status_label.text())

    def test_download_without_a_connection_is_reported(self):
        self.browser._ssh_client = None
        self.browser._start_download([("/srv/notes.txt", os.path.join(self.tmp, "n"), False)])
        self.assertIn("Not connected", self.browser.status_label.text())
        self.assertFalse(self.browser._download_busy)


class DownloadResultReportingTests(unittest.TestCase):
    def setUp(self):
        self.browser = RemoteFileBrowser()

    def tearDown(self):
        self.browser.deleteLater()

    def test_progress_drives_the_label_and_the_bar(self):
        self.browser._on_download_progress("Counting files…", -1)
        self.assertEqual(self.browser.progress_bar.maximum(), 0)   # busy
        self.browser._on_download_progress("Downloading 2/5 (3 left): a", 400)
        self.assertEqual(self.browser.status_label.text(), "Downloading 2/5 (3 left): a")
        self.assertEqual(self.browser.progress_bar.maximum(), RemoteFileBrowser.PROGRESS_SCALE)
        self.assertEqual(self.browser.progress_bar.value(), 400)

    def test_success_message_counts_files(self):
        self.browser.progress_bar.show()
        self.browser._on_download_finished(4, 4, [])
        self.assertIn("Downloaded 4 files", self.browser.status_label.text())
        self.assertTrue(self.browser.progress_bar.isHidden())

    def test_failure_message_surfaces_the_first_error(self):
        self.browser._on_download_finished(2, 3, ["logo.png: denied"])
        text = self.browser.status_label.text()
        self.assertIn("2/3", text)
        self.assertIn("logo.png", text)

    def test_a_cancelled_batch_is_not_called_a_failure(self):
        self.browser._on_download_finished(2, 9, [RemoteFileBrowser.CANCELLED])
        text = self.browser.status_label.text()
        self.assertIn("stopped after 2/9", text)
        self.assertNotIn("failed", text)

    def test_finishing_clears_the_busy_flag(self):
        self.browser._download_busy = True
        self.browser._on_download_finished(1, 1, [])
        self.assertFalse(self.browser._download_busy)

    def test_status_line_wording(self):
        line = RemoteFileBrowser._download_status
        self.assertEqual(line(1, 4, "a.txt", 0, 0), "Downloading 1/4 (3 left): a.txt")
        self.assertEqual(line(4, 4, "a.txt", 512, 1024),
                         "Downloading 4/4: a.txt — 50% of 1.0 KB")
        # Never claims more than 100% if the server sent extra bytes.
        self.assertIn("100%", line(1, 1, "a", 2048, 1024))


if __name__ == "__main__":
    unittest.main()
