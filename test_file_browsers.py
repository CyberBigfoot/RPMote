"""Tests for adding and removing files/folders in the SFTP, FTP and panel browsers."""

import os
import shutil
import stat
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

import app.file_browser_ops as opsmod
import app.ftp_browser as ftpmod
import app.remote_file_browser as rfbmod
import app.sftp_browser as sftpmod
from app.ftp_browser import FTPBrowser, _parse_list_line
from app.remote_file_browser import RemoteFileBrowser
from app.sftp_browser import SFTPBrowser

_APP = QApplication.instance() or QApplication([])

CONN = {"name": "test", "host": "example.invalid", "port": 22, "username": "u"}


def _select(tree, names):
    """Select the rows whose entry name is in `names`."""
    tree.clearSelection()
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data and data.get("name") in names:
            item.setSelected(True)


class _PromptStub:
    """Answer the name prompt and the delete confirmation without a UI."""

    MODULES = (opsmod, sftpmod, ftpmod, rfbmod)

    def __init__(self):
        self.name = ""
        self._saved = []

    def __enter__(self):
        for mod in self.MODULES:
            self._saved.append((mod, mod.ask_name, mod.confirm_delete))
            mod.ask_name = lambda parent, title, label, preset="": self.name
            mod.confirm_delete = lambda parent, names, where: True
        return self

    def __exit__(self, *exc):
        for mod, ask, confirm in self._saved:
            mod.ask_name, mod.confirm_delete = ask, confirm
        self._saved.clear()
        return False


# ── Fakes ───────────────────────────────────────────────────────────


class _FakeAttr:
    def __init__(self, name, mode):
        self.filename = name
        self.st_mode = mode
        self.st_size = 10
        self.st_mtime = 0


class FakeSFTP:
    """In-memory SFTP client: `tree` maps a directory to {name: 'dir'|'file'}."""

    def __init__(self):
        self.tree = {
            "/": {"app": "dir", "readme": "file"},
            "/app": {"logs": "dir", "main.py": "file"},
            "/app/logs": {"a.log": "file", "b.log": "file"},
        }
        self.removed = []
        self.rmdirs = []
        self.made = []
        self.renamed = []
        self.chmods = []

    def listdir_attr(self, path):
        return [
            _FakeAttr(
                name,
                (stat.S_IFDIR | 0o755) if kind == "dir" else (stat.S_IFREG | 0o644),
            )
            for name, kind in self.tree[path].items()
        ]

    def listdir(self, path):
        return list(self.tree[path])

    def remove(self, path):
        self.removed.append(path)

    def rmdir(self, path):
        self.rmdirs.append(path)

    def mkdir(self, path):
        self.made.append(path)

    def rename(self, old, new):
        self.renamed.append((old, new))

    def chmod(self, path, mode):
        self.chmods.append((path, mode))

    def stat(self, path):
        raise IOError("no such file")

    def lstat(self, path):
        if path in self.tree:
            return _FakeAttr(path, stat.S_IFDIR | 0o755)
        parent, name = path.rsplit("/", 1)
        if name in self.tree.get(parent or "/", {}):
            return _FakeAttr(name, stat.S_IFREG | 0o644)
        raise FileNotFoundError(path)

    def get_channel(self):
        return self

    def settimeout(self, timeout):
        pass

    def close(self):
        pass

    def normalize(self, path):
        return "/home/u"

    def open(self, path, mode):
        import io

        self.made.append(path)
        return io.BytesIO()


class FakeFTP:
    def __init__(self):
        self.tree = {
            "/site": [
                "drwxr-xr-x 2 u g 4096 Jan 01 12:00 img",
                "-rw-r--r-- 1 u g  120 Jan 01 12:00 index.html",
            ],
            "/site/img": ["-rw-r--r-- 1 u g   90 Jan 01 12:00 logo.png"],
        }
        self.cwds = []
        self.deleted = []
        self.rmds = []

    def cwd(self, path):
        self.cwds.append(path)

    def mlsd(self, facts=None):
        import ftplib

        raise ftplib.error_perm("500 Unknown command")

    def retrlines(self, cmd, callback):
        for line in self.tree[self.cwds[-1]]:
            callback(line)

    def delete(self, path):
        self.deleted.append(path)

    def rmd(self, path):
        self.rmds.append(path)


# ── Local pane (shared mixin, used by both dual-pane browsers) ───────


class LocalPaneOpsTests(unittest.TestCase):
    """Create / rename / delete on the local half of a transfer tab."""

    def _run_for(self, cls):
        tmp = tempfile.mkdtemp(prefix="rpmote_test_")
        browser = cls(dict(CONN))
        browser._local_cwd = tmp
        browser._refresh_local()
        try:
            with _PromptStub() as prompt:
                prompt.name = "docs"
                browser._local_new_folder()
                self.assertTrue(os.path.isdir(os.path.join(tmp, "docs")))

                prompt.name = "notes.txt"
                browser._local_new_file()
                self.assertTrue(os.path.isfile(os.path.join(tmp, "notes.txt")))

                # nested content proves delete recurses
                os.makedirs(os.path.join(tmp, "docs", "sub"))
                open(os.path.join(tmp, "docs", "sub", "deep.txt"), "w").close()

                browser._refresh_local()
                _select(browser.local_tree, {"notes.txt"})
                prompt.name = "renamed.txt"
                browser._local_rename()
                self.assertTrue(os.path.isfile(os.path.join(tmp, "renamed.txt")))

                browser._refresh_local()
                _select(browser.local_tree, {"docs", "renamed.txt"})
                browser._local_delete()
                self.assertFalse(os.path.exists(os.path.join(tmp, "docs")))
                self.assertFalse(os.path.exists(os.path.join(tmp, "renamed.txt")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            browser.deleteLater()

    def test_sftp_local_pane_create_rename_delete(self):
        self._run_for(SFTPBrowser)

    def test_ftp_local_pane_create_rename_delete(self):
        self._run_for(FTPBrowser)

    def test_parent_row_is_never_an_operation_target(self):
        tmp = tempfile.mkdtemp(prefix="rpmote_test_")
        browser = SFTPBrowser(dict(CONN))
        browser._local_cwd = tmp
        browser._refresh_local()
        try:
            first = browser.local_tree.topLevelItem(0)
            data = first.data(0, Qt.ItemDataRole.UserRole)
            self.assertEqual(data["name"], "..")
            first.setSelected(True)
            self.assertEqual(browser._local_selection(), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            browser.deleteLater()


# ── Remote pane, SFTP ───────────────────────────────────────────────


class SFTPRemoteOpsTests(unittest.TestCase):
    def setUp(self):
        self.browser = SFTPBrowser(dict(CONN))
        self.fake = FakeSFTP()
        self.browser.sftp = self.browser.sftp_ops = self.fake
        self.browser._connected = True
        self.browser._remote_cwd = "/"
        self.browser._refresh_remote()

    def tearDown(self):
        self.browser.deleteLater()

    def test_listing_puts_directories_first(self):
        names = [
            self.browser.remote_tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)["name"]
            for i in range(self.browser.remote_tree.topLevelItemCount())
        ]
        self.assertEqual(names, ["app", "readme"])

    def test_new_folder_lands_in_the_current_directory(self):
        with _PromptStub() as prompt:
            prompt.name = "newdir"
            self.browser._remote_new_folder()
        self.assertIn("/newdir", self.fake.made)

    def test_new_folder_lands_inside_a_selected_folder(self):
        _select(self.browser.remote_tree, {"app"})
        with _PromptStub() as prompt:
            prompt.name = "inside"
            self.browser._remote_new_folder()
        self.assertIn("/app/inside", self.fake.made)

    def test_new_file_uses_exclusive_create(self):
        with _PromptStub() as prompt:
            prompt.name = "fresh.txt"
            self.browser._remote_new_file()
        self.assertIn("/fresh.txt", self.fake.made)

    def test_rename_moves_within_the_same_directory(self):
        _select(self.browser.remote_tree, {"readme"})
        with _PromptStub() as prompt:
            prompt.name = "README.md"
            self.browser._remote_rename()
        self.assertIn(("/readme", "/README.md"), self.fake.renamed)

    def test_delete_recurses_into_folders_depth_first(self):
        self.browser._rmtree_remote(self.fake, "/app")
        self.assertEqual(
            self.fake.removed,
            ["/app/logs/a.log", "/app/logs/b.log", "/app/main.py"],
        )
        self.assertEqual(self.fake.rmdirs, ["/app/logs", "/app"])

    def test_chmod_applies_an_octal_mode(self):
        _select(self.browser.remote_tree, {"readme"})
        original = sftpmod.QInputDialog.getText
        sftpmod.QInputDialog.getText = staticmethod(lambda *a, **k: ("640", True))
        try:
            self.browser._remote_chmod()
        finally:
            sftpmod.QInputDialog.getText = original
        self.assertEqual(self.fake.chmods, [("/readme", 0o640)])


# ── Remote pane, FTP ────────────────────────────────────────────────


class FTPRemoteOpsTests(unittest.TestCase):
    def test_recursive_delete_empties_folders_before_removing_them(self):
        browser = FTPBrowser(dict(CONN))
        fake = FakeFTP()
        try:
            browser._rmtree_remote(fake, "/site")
        finally:
            browser.deleteLater()
        self.assertEqual(fake.deleted, ["/site/img/logo.png", "/site/index.html"])
        self.assertEqual(fake.rmds, ["/site/img", "/site"])
        # RMD fails while the session is still inside the directory.
        self.assertEqual(fake.cwds[-1], "/")

    def test_list_parser_handles_unix_dos_and_symlink_rows(self):
        self.assertEqual(
            _parse_list_line("drwxr-xr-x 2 u g 4096 Jan 01 12:00 my folder"),
            ("my folder", True, 4096, "Jan 01 12:00"),
        )
        self.assertEqual(
            _parse_list_line("-rw-r--r-- 1 u g 1234 Jan 01 12:00 file.txt"),
            ("file.txt", False, 1234, "Jan 01 12:00"),
        )
        self.assertEqual(
            _parse_list_line("lrwxrwxrwx 1 u g 7 Jan 01 12:00 link -> target")[0],
            "link",
        )
        self.assertEqual(
            _parse_list_line("12-31-99  11:59PM       <DIR>          winfolder")[:2],
            ("winfolder", True),
        )
        self.assertIsNone(_parse_list_line("total 12"))
        self.assertIsNone(_parse_list_line("drwxr-xr-x 2 u g 4096 Jan 01 12:00 ."))


# ── Side panel browser ──────────────────────────────────────────────


class PanelBrowserOpsTests(unittest.TestCase):
    def setUp(self):
        self.browser = RemoteFileBrowser()
        self.fake = FakeSFTP()
        self.browser._sftp = self.fake
        self.browser._ssh_client = type("Client", (), {"open_sftp": lambda client: self.fake})()
        self.browser._current_path = "/"
        self.browser._load_directory("/")
        self._wait()

    def _wait(self):
        deadline = time.monotonic() + 3
        while (self.browser._loading or self.browser._mutation_busy) and time.monotonic() < deadline:
            _APP.processEvents()
            time.sleep(0.005)
        self.assertFalse(self.browser._loading or self.browser._mutation_busy)

    def tearDown(self):
        self.browser.deleteLater()

    def test_new_folder_and_new_file(self):
        with _PromptStub() as prompt:
            prompt.name = "sidebar_dir"
            self.browser._create_folder()
            self._wait()
            prompt.name = "sidebar.txt"
            self.browser._create_file()
            self._wait()
        self.assertIn("/sidebar_dir", self.fake.made)
        self.assertIn("/sidebar.txt", self.fake.made)

    def test_delete_recurses_into_folders(self):
        _select(self.browser.tree, {"app"})
        with _PromptStub():
            self.browser._delete_selection()
            self._wait()
        self.assertEqual(self.fake.rmdirs, ["/app/logs", "/app"])


if __name__ == "__main__":
    unittest.main()
