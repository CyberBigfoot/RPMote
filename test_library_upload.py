"""Tests for dragging files and folders into the Library panel, and for the
Upload picker taking files and folders in one go.
"""

import os
import shutil
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QMimeData, QPointF, Qt, QUrl
from PyQt6.QtGui import QDropEvent
from PyQt6.QtWidgets import QApplication

import app.remote_file_browser as rfbmod
from app.file_browser_ops import count_files
from app.remote_file_browser import RemoteFileBrowser, _LibraryTree

_APP = QApplication.instance() or QApplication([])


def make_tree(root):
    """A small local tree: 2 loose files, a folder with a nested subfolder."""
    os.makedirs(os.path.join(root, "assets", "img"), exist_ok=True)
    for path in (
        ("notes.txt",),
        ("readme.md",),
        ("assets", "style.css"),
        ("assets", "img", "logo.png"),
        ("assets", "img", "icon.svg"),
    ):
        with open(os.path.join(root, *path), "w", encoding="utf-8") as handle:
            handle.write("x")
    return root


def url_mime(paths):
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(p) for p in paths])
    return mime


class _Recorder:
    """Minimal SFTP stand-in with the two calls the uploader makes."""

    def __init__(self, existing_dirs=()):
        self.puts = []
        self.mkdirs = []
        self._existing = set(existing_dirs)

    def mkdir(self, path):
        if path in self._existing:
            raise IOError("File exists")
        self.mkdirs.append(path)
        self._existing.add(path)

    def put(self, local, remote):
        self.puts.append(remote)

    def close(self):
        pass


class FakeSSHClient:
    def __init__(self, recorder):
        self._recorder = recorder

    def open_sftp(self):
        return self._recorder


class DroppedPathExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tree(tempfile.mkdtemp(prefix="rpmote_drop_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_local_files_and_folders_are_accepted(self):
        paths = [
            os.path.join(self.tmp, "notes.txt"),
            os.path.join(self.tmp, "assets"),
        ]
        extracted = _LibraryTree._dropped_paths(url_mime(paths))
        self.assertEqual(
            sorted(os.path.basename(p) for p in extracted), ["assets", "notes.txt"]
        )

    def test_remote_urls_are_ignored(self):
        mime = QMimeData()
        mime.setUrls([QUrl("https://example.com/file.txt")])
        self.assertEqual(_LibraryTree._dropped_paths(mime), [])

    def test_paths_that_no_longer_exist_are_dropped(self):
        mime = url_mime([os.path.join(self.tmp, "gone.txt")])
        self.assertEqual(_LibraryTree._dropped_paths(mime), [])

    def test_plain_text_drag_is_not_a_file_drop(self):
        mime = QMimeData()
        mime.setText("just some text")
        self.assertEqual(_LibraryTree._dropped_paths(mime), [])


class DropTargetTests(unittest.TestCase):
    """Where a drop lands: onto a folder row, or into the current directory."""

    def setUp(self):
        self.tmp = make_tree(tempfile.mkdtemp(prefix="rpmote_drop_"))
        self.browser = RemoteFileBrowser()
        self.browser._current_path = "/srv"
        self.tree = self.browser.tree
        self.tree.resize(400, 300)
        self.received = []
        self.tree.external_drop.connect(
            lambda paths, target: self.received.append((paths, target))
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.browser.deleteLater()

    def _drop_at(self, point):
        paths = [os.path.join(self.tmp, "notes.txt")]
        # QDropEvent borrows the QMimeData rather than owning it, so both have
        # to outlive the call or Qt reads freed memory.
        self._mime = url_mime(paths)
        event = QDropEvent(
            point,
            Qt.DropAction.CopyAction,
            self._mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.tree.dropEvent(event)
        return event

    def test_drop_on_empty_area_targets_the_current_directory(self):
        event = self._drop_at(QPointF(10, 250))
        self.assertTrue(event.isAccepted())
        self.assertEqual(len(self.received), 1)
        _paths, target = self.received[0]
        self.assertIsNone(target)   # panel resolves None to the current path

    def test_drop_on_a_folder_row_targets_that_folder(self):
        item = rfbmod.QTreeWidgetItem(["📁 uploads", "", ""])
        item.setData(
            0, Qt.ItemDataRole.UserRole,
            {"name": "uploads", "is_dir": True, "path": "/srv/uploads"},
        )
        self.tree.addTopLevelItem(item)
        rect = self.tree.visualItemRect(item)
        self._drop_at(QPointF(rect.center()))
        _paths, target = self.received[0]
        self.assertEqual(target, "/srv/uploads")

    def test_drop_on_the_parent_row_falls_back_to_the_current_directory(self):
        item = rfbmod.QTreeWidgetItem(["📁 ..", "", ""])
        item.setData(0, Qt.ItemDataRole.UserRole, {"name": "..", "is_dir": True})
        self.tree.addTopLevelItem(item)
        rect = self.tree.visualItemRect(item)
        self._drop_at(QPointF(rect.center()))
        _paths, target = self.received[0]
        self.assertIsNone(target)

    def test_drop_highlight_turns_on_and_off(self):
        self.tree.set_base_style("QTreeWidget { border: none; }")
        self.tree._set_drop_active(True)
        self.assertIn("dashed", self.tree.styleSheet())
        self.tree._set_drop_active(False)
        self.assertNotIn("dashed", self.tree.styleSheet())


class RecursiveUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_tree(tempfile.mkdtemp(prefix="rpmote_up_"))
        self.browser = RemoteFileBrowser()
        self.browser._current_path = "/srv"
        self.recorder = _Recorder()
        self.browser._sftp = object()          # "connected" for the guard
        self.browser._ssh_client = FakeSSHClient(self.recorder)
        self.finished = []
        self.browser.upload_finished.connect(
            lambda done, total, errors: self.finished.append((done, total, list(errors)))
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.browser.deleteLater()

    def _upload(self, paths, target="/srv"):
        # run the worker body inline so the test stays deterministic
        self.browser._run_upload(paths, target)
        return self.finished[-1]

    def test_counting_walks_into_folders(self):
        self.assertEqual(count_files([self.tmp]), 5)
        self.assertEqual(
            count_files([os.path.join(self.tmp, "notes.txt")]), 1
        )
        self.assertEqual(count_files([os.path.join(self.tmp, "assets")]), 3)

    def test_files_and_folders_upload_together(self):
        paths = [
            os.path.join(self.tmp, "notes.txt"),
            os.path.join(self.tmp, "readme.md"),
            os.path.join(self.tmp, "assets"),
        ]
        done, total, errors = self._upload(paths)
        self.assertEqual(errors, [])
        self.assertEqual((done, total), (5, 5))
        self.assertEqual(
            sorted(self.recorder.puts),
            sorted([
                "/srv/notes.txt",
                "/srv/readme.md",
                "/srv/assets/style.css",
                "/srv/assets/img/logo.png",
                "/srv/assets/img/icon.svg",
            ]),
        )

    def test_nested_directories_are_created(self):
        self._upload([os.path.join(self.tmp, "assets")])
        self.assertEqual(
            sorted(self.recorder.mkdirs), ["/srv/assets", "/srv/assets/img"]
        )

    def test_upload_targets_a_dropped_on_folder(self):
        self._upload([os.path.join(self.tmp, "assets")], target="/srv/uploads")
        self.assertIn("/srv/uploads/assets/img/logo.png", self.recorder.puts)

    def test_existing_remote_directory_is_not_an_error(self):
        self.recorder._existing.add("/srv/assets")
        done, _total, errors = self._upload([os.path.join(self.tmp, "assets")])
        self.assertEqual(errors, [])
        self.assertEqual(done, 3)

    def test_a_single_failure_does_not_abort_the_batch(self):
        original = self.recorder.put

        def flaky(local, remote):
            if remote.endswith("logo.png"):
                raise IOError("permission denied")
            original(local, remote)

        self.recorder.put = flaky
        done, total, errors = self._upload([self.tmp])
        self.assertEqual(len(errors), 1)
        self.assertIn("logo.png", errors[0])
        self.assertEqual(done, total - 1)

    @unittest.skipUnless(hasattr(os, "symlink"), "platform has no symlinks")
    def test_symlinked_directories_are_skipped(self):
        link = os.path.join(self.tmp, "assets", "loop")
        try:
            os.symlink(self.tmp, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation not permitted")
        # Must terminate rather than recursing for ever.
        done, _total, errors = self._upload([os.path.join(self.tmp, "assets")])
        self.assertEqual(errors, [])
        self.assertEqual(done, 3)

    def test_second_upload_is_refused_while_one_is_running(self):
        self.browser._upload_busy = True
        self.browser._start_upload([os.path.join(self.tmp, "notes.txt")], "/srv")
        self.assertIn("already running", self.browser.status_label.text())

    def test_upload_without_a_connection_is_reported(self):
        self.browser._sftp = None
        self.browser._ssh_client = None
        self.browser._on_external_drop([os.path.join(self.tmp, "notes.txt")], None)
        self.assertIn("Not connected", self.browser.status_label.text())


class UploadResultReportingTests(unittest.TestCase):
    def setUp(self):
        self.browser = RemoteFileBrowser()

    def tearDown(self):
        self.browser.deleteLater()

    def test_success_message_counts_files(self):
        self.browser._on_upload_finished(4, 4, [])
        self.assertIn("Uploaded 4 files", self.browser.status_label.text())

    def test_failure_message_surfaces_the_first_error(self):
        self.browser._on_upload_finished(2, 3, ["logo.png: denied"])
        text = self.browser.status_label.text()
        self.assertIn("2/3", text)
        self.assertIn("logo.png", text)

    def test_finishing_clears_the_busy_flag(self):
        self.browser._upload_busy = True
        self.browser._on_upload_finished(1, 1, [])
        self.assertFalse(self.browser._upload_busy)


if __name__ == "__main__":
    unittest.main()


class InternalDragTests(unittest.TestCase):
    """Dragging rows inside the tree must never reorder the remote listing."""

    def setUp(self):
        self.browser = RemoteFileBrowser()
        self.tree = self.browser.tree
        self.tree.resize(400, 300)

    def tearDown(self):
        self.browser.deleteLater()

    def _internal_drop(self):
        self._mime = QMimeData()
        self._mime.setData("application/x-qabstractitemmodeldatalist", b"\x00")
        return QDropEvent(
            QPointF(10, 10),
            Qt.DropAction.MoveAction,
            self._mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )

    def test_internal_drag_is_refused(self):
        received = []
        self.tree.external_drop.connect(lambda *a: received.append(a))
        event = self._internal_drop()
        self.tree.dropEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertEqual(received, [])

    def test_internal_drag_enter_is_refused(self):
        event = self._internal_drop()
        self.tree.dragEnterEvent(event)
        self.assertFalse(event.isAccepted())


class MixedPickerTests(unittest.TestCase):
    """The Upload picker has to return files and folders from one prompt."""

    def setUp(self):
        self.tmp = make_tree(tempfile.mkdtemp(prefix="rpmote_pick_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _picker(self):
        from app.file_browser_ops import MixedFilePicker
        picker = MixedFilePicker(None, "Pick", self.tmp)
        picker.setDirectory(self.tmp)
        return picker

    def test_picker_finds_the_views_it_needs(self):
        picker = self._picker()
        try:
            self.assertTrue(picker.usable)
        finally:
            picker.deleteLater()

    def test_views_allow_multi_selection(self):
        from PyQt6.QtWidgets import QAbstractItemView
        picker = self._picker()
        try:
            for view in picker._file_views:
                self.assertEqual(
                    view.selectionMode(),
                    QAbstractItemView.SelectionMode.ExtendedSelection,
                )
        finally:
            picker.deleteLater()

    def test_a_mixed_selection_is_returned(self):
        from PyQt6.QtCore import QItemSelectionModel
        picker = self._picker()
        try:
            view = picker._file_views[0]
            model = view.model()
            selection = view.selectionModel()
            for name in ("assets", "notes.txt", "readme.md"):
                index = model.index(os.path.join(self.tmp, name))
                selection.select(
                    index,
                    QItemSelectionModel.SelectionFlag.Select
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
            chosen = sorted(os.path.basename(p) for p in picker.chosen_paths())
            self.assertEqual(chosen, ["assets", "notes.txt", "readme.md"])
        finally:
            picker.deleteLater()

    def test_a_files_only_selection_can_still_be_accepted(self):
        """Directory mode would normally refuse a selection with no folder."""
        from PyQt6.QtCore import QItemSelectionModel
        picker = self._picker()
        try:
            view = picker._file_views[0]
            model = view.model()
            selection = view.selectionModel()
            index = model.index(os.path.join(self.tmp, "notes.txt"))
            selection.select(
                index,
                QItemSelectionModel.SelectionFlag.Select
                | QItemSelectionModel.SelectionFlag.Rows,
            )
            self.assertEqual(
                [os.path.basename(p) for p in picker.chosen_paths()], ["notes.txt"]
            )
            picker.accept()
            self.assertEqual(picker.result(), int(picker.DialogCode.Accepted))
        finally:
            picker.deleteLater()


class DropPlumbingTests(unittest.TestCase):
    """Qt only delivers external drags when the viewport accepts drops.

    DragOnly mode (what the tree used before) makes Qt set
    ``viewport().setAcceptDrops(False)``, which silently swallows every drop
    from the desktop — so assert the wiring, not just the handlers.
    """

    def setUp(self):
        self.browser = RemoteFileBrowser()

    def tearDown(self):
        self.browser.deleteLater()

    def test_widget_and_viewport_both_accept_drops(self):
        self.assertTrue(self.browser.tree.acceptDrops())
        self.assertTrue(self.browser.tree.viewport().acceptDrops())

    def test_dragging_rows_out_is_still_enabled(self):
        self.assertTrue(self.browser.tree.dragEnabled())


# ════════════════════════════════════════════════════════════════════
# The SFTP and FTP browser tabs get the same desktop-drop support
# ════════════════════════════════════════════════════════════════════

from app.file_browser_ops import ExternalDropTree, copy_into
from app.ftp_browser import FTPBrowser
from app.sftp_browser import SFTPBrowser

CONN = {"name": "t", "host": "h", "port": 22, "username": "u"}


class CopyIntoTests(unittest.TestCase):
    """Dropping onto a local pane copies rather than transfers."""

    def setUp(self):
        self.src = make_tree(tempfile.mkdtemp(prefix="rpmote_src_"))
        self.dest = tempfile.mkdtemp(prefix="rpmote_dest_")

    def tearDown(self):
        for path in (self.src, self.dest):
            shutil.rmtree(path, ignore_errors=True)

    def test_files_and_folders_are_copied(self):
        copied, errors = copy_into(
            [os.path.join(self.src, "notes.txt"), os.path.join(self.src, "assets")],
            self.dest,
        )
        self.assertEqual((copied, errors), (2, []))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "notes.txt")))
        self.assertTrue(
            os.path.isfile(os.path.join(self.dest, "assets", "img", "logo.png"))
        )

    def test_copying_onto_itself_is_a_no_op(self):
        copied, errors = copy_into([os.path.join(self.src, "notes.txt")], self.src)
        self.assertEqual((copied, errors), (0, []))

    def test_an_existing_folder_is_merged(self):
        os.makedirs(os.path.join(self.dest, "assets"), exist_ok=True)
        with open(os.path.join(self.dest, "assets", "keep.me"), "w") as handle:
            handle.write("x")
        copied, errors = copy_into([os.path.join(self.src, "assets")], self.dest)
        self.assertEqual((copied, errors), (1, []))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "assets", "keep.me")))
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "assets", "style.css")))


class BrowserTabDropTests(unittest.TestCase):
    """Both dual-pane tabs accept desktop drops on both panes."""

    def setUp(self):
        self.tmp = make_tree(tempfile.mkdtemp(prefix="rpmote_tab_"))
        self.sftp = SFTPBrowser(dict(CONN))
        self.ftp = FTPBrowser(dict(CONN))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.sftp.deleteLater()
        self.ftp.deleteLater()

    def _panes(self):
        return [
            ("sftp local", self.sftp.local_tree),
            ("sftp remote", self.sftp.remote_tree),
            ("ftp local", self.ftp.local_tree),
            ("ftp remote", self.ftp.remote_tree),
        ]

    def test_every_pane_accepts_drops(self):
        for label, tree in self._panes():
            with self.subTest(pane=label):
                self.assertIsInstance(tree, ExternalDropTree)
                self.assertTrue(tree.acceptDrops())
                self.assertTrue(tree.viewport().acceptDrops())

    def test_paths_become_transfer_entries(self):
        for label, browser in (("sftp", self.sftp), ("ftp", self.ftp)):
            with self.subTest(browser=label):
                entries = browser._entries_from_paths([
                    os.path.join(self.tmp, "notes.txt"),
                    os.path.join(self.tmp, "assets"),
                ])
                self.assertEqual(
                    [(e["name"], e["is_dir"]) for e in entries],
                    [("notes.txt", False), ("assets", True)],
                )

    def test_dropping_on_a_local_pane_copies_the_files(self):
        dest = tempfile.mkdtemp(prefix="rpmote_localdrop_")
        try:
            self.sftp._local_cwd = dest
            self.sftp._on_local_external_drop(
                [os.path.join(self.tmp, "assets")], None
            )
            self.assertTrue(
                os.path.isfile(os.path.join(dest, "assets", "img", "icon.svg"))
            )
        finally:
            shutil.rmtree(dest, ignore_errors=True)

    def test_dropping_on_a_remote_pane_while_disconnected_is_reported(self):
        self.sftp._connected = False
        self.sftp._on_remote_external_drop([os.path.join(self.tmp, "notes.txt")], None)
        self.assertIn("Not connected", self.sftp.progress_label.text())

    def test_cross_pane_drag_still_works(self):
        """The SFTP panes' own drag-between-panes must survive the refactor."""
        from PyQt6.QtCore import QByteArray
        import json as _json
        payload = _json.dumps(
            [{"name": "f.txt", "is_dir": False, "path": "/r/f.txt",
              "source_pane": "remote"}]
        ).encode()
        self._mime = QMimeData()
        self._mime.setData("application/x-rpmote-sftp", QByteArray(payload))
        received = []
        self.sftp.local_tree.file_dropped.connect(received.append)
        event = QDropEvent(
            QPointF(5, 5),
            Qt.DropAction.CopyAction,
            self._mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.sftp.local_tree.dropEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0]["name"], "f.txt")

    def test_a_pane_refuses_a_drag_from_itself(self):
        from PyQt6.QtCore import QByteArray
        import json as _json
        payload = _json.dumps(
            [{"name": "f.txt", "is_dir": False, "path": "/r/f.txt",
              "source_pane": "local"}]
        ).encode()
        self._mime = QMimeData()
        self._mime.setData("application/x-rpmote-sftp", QByteArray(payload))
        received = []
        self.sftp.local_tree.file_dropped.connect(received.append)
        event = QDropEvent(
            QPointF(5, 5),
            Qt.DropAction.CopyAction,
            self._mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        self.sftp.local_tree.dropEvent(event)
        self.assertEqual(received, [])
