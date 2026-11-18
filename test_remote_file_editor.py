"""Remote edits never silently overwrite a newer version or truncate the original."""

import codecs
import io
import os
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QTextCursor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from app.remote_files import (MAX_EDIT_BYTES, RemoteFileBackend, RemoteFileConflict,
                              RemoteFileError, TextDocument, text_diff)
from app.remote_file_editor import RemoteFileEditor, DirectoryCompareDialog

APP = QApplication.instance() or QApplication([])


class FakeSFTP:
    def __init__(self):
        self.data = {"/etc/app.conf": b"port=80\nname=demo\n"}
        self.metadata = {"/etc/app.conf": (0o640, 1000, 1000)}
        self.directories = {"/", "/etc"}
        self.fail_replace = False
        self.fail_write = False
        self.change_while_staging = False
        self.replacements = []

    def normalize(self, path):
        return path

    def stat(self, path):
        if path in self.directories:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_size=0, st_uid=1000, st_gid=1000)
        if path not in self.data:
            raise FileNotFoundError(path)
        mode, uid, gid = self.metadata.get(path, (0o600, 1000, 1000))
        return SimpleNamespace(st_mode=stat.S_IFREG | mode, st_size=len(self.data[path]), st_uid=uid, st_gid=gid)

    def open(self, path, mode):
        if mode == "rb":
            return io.BytesIO(self.data[path])
        if "x" in mode and path in self.data:
            raise FileExistsError(path)
        self.data[path] = b""
        self.metadata[path] = (0o600, 1000, 1000)
        owner = self
        class Writer(io.BytesIO):
            def write(self, data):
                if owner.fail_write and path.endswith(".tmp"):
                    raise OSError("upload interrupted")
                return super().write(data)
            def close(self):
                if not self.closed:
                    owner.data[path] = self.getvalue()
                    if owner.change_while_staging and path.endswith(".tmp"):
                        owner.data["/etc/app.conf"] = b"changed elsewhere\n"
                super().close()
        return Writer()

    def chmod(self, path, mode):
        _, uid, gid = self.metadata[path]
        self.metadata[path] = (mode, uid, gid)

    def chown(self, path, uid, gid):
        mode, _, _ = self.metadata[path]
        self.metadata[path] = (mode, uid, gid)

    def posix_rename(self, source, target):
        if self.fail_replace:
            raise OSError("unsupported extension")
        self.data[target] = self.data.pop(source)
        self.metadata[target] = self.metadata.pop(source)
        self.replacements.append(target)

    def remove(self, path):
        del self.data[path]
        self.metadata.pop(path, None)

    def listdir_attr(self, path):
        paths = self.directories | self.data.keys()
        output = []
        for child in sorted(paths):
            if child != path and str(Path(child).parent).replace("\\", "/") == path:
                info = self.stat(child)
                info.filename = Path(child).name
                output.append(info)
        return output


def backend_for(client):
    @contextmanager
    def session():
        yield client
    return RemoteFileBackend(session, label="Demo customer · Web01")


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(0.005)
    assert predicate(), "worker did not finish"


@pytest.mark.parametrize("raw", [b"a\nb\n", b"a\r\nb\r\n", b"a\rb\r",
                                b"a\r\nb\nc\r", codecs.BOM_UTF8 + b"hello\n",
                                codecs.BOM_UTF16_LE + "hello\r\n".encode("utf-16-le"),
                                codecs.BOM_UTF16_BE + "hello\n".encode("utf-16-be")])
def test_unchanged_text_round_trips_exactly(raw):
    doc = TextDocument.decode(raw)
    assert doc.encode(doc.text) == raw


def test_edits_preserve_bom_and_line_endings():
    raw = codecs.BOM_UTF16_BE + "port=80\r\nname=demo\r\n".encode("utf-16-be")
    doc = TextDocument.decode(raw)
    assert doc.encode(doc.text.replace("80", "443")) == codecs.BOM_UTF16_BE + "port=443\r\nname=demo\r\n".encode("utf-16-be")
    mixed = TextDocument.decode(b"a\r\nb\nc\r")
    assert mixed.encode("a\nx\nc\n") == b"a\r\nx\nc\r"


@pytest.mark.parametrize("raw", [b"hello\x00world", b"\xff\xfe\x00", b"\x89PNG\r\n\x1a\n", b"x" * (MAX_EDIT_BYTES + 1)],
                         ids=["nul", "invalid-utf16", "binary-png", "oversized"])
def test_binary_invalid_and_oversized_files_are_rejected(raw):
    with pytest.raises(RemoteFileError):
        TextDocument.decode(raw)


def test_diff_shows_changes_and_missing_final_newline():
    preview = text_diff("port=80\n", "port=443")
    assert "-port=80" in preview and "+port=443" in preview
    assert "No newline at end of file" in preview


def test_save_creates_private_backup_and_preserves_original_permissions():
    client = FakeSFTP()
    client.metadata["/etc/app.conf"] = (0o640, 0, 42)
    backend = backend_for(client)
    original = backend.load("/etc/app.conf")
    saved, backup = backend.save(original, b"port=443\nname=demo\n")
    assert client.data[backup] == original.document.raw
    assert client.metadata[backup][0] == 0o600
    assert client.metadata["/etc/app.conf"] == (0o640, 0, 42)
    assert saved.document.raw == client.data["/etc/app.conf"]
    assert not any(path.endswith(".tmp") for path in client.data)


def test_unchanged_save_creates_no_backup():
    client = FakeSFTP()
    backend = backend_for(client)
    original = backend.load("/etc/app.conf")
    assert backend.save(original, original.document.raw) == (original, "")
    assert len(client.data) == 1


def test_concurrent_remote_edit_is_not_overwritten():
    client = FakeSFTP()
    backend = backend_for(client)
    original = backend.load("/etc/app.conf")
    client.data[original.path] = b"new remote version\n"
    with pytest.raises(RemoteFileConflict):
        backend.save(original, b"my draft\n")
    assert client.data[original.path] == b"new remote version\n"
    assert len(client.data) == 1


def test_change_during_upload_is_detected_too():
    client = FakeSFTP()
    backend = backend_for(client)
    original = backend.load("/etc/app.conf")
    client.change_while_staging = True
    with pytest.raises(RemoteFileConflict):
        backend.save(original, b"my draft\n")
    assert client.data[original.path] == b"changed elsewhere\n"
    assert not client.replacements
    assert not any(path.endswith(".tmp") for path in client.data)


@pytest.mark.parametrize("failure", ["fail_replace", "fail_write"])
def test_failed_upload_or_replace_preserves_original_and_backup(failure):
    client = FakeSFTP()
    backend = backend_for(client)
    original = backend.load("/etc/app.conf")
    setattr(client, failure, True)
    with pytest.raises(RemoteFileError, match="Original backup"):
        backend.save(original, b"my draft\n")
    assert client.data[original.path] == original.document.raw
    assert not any(path.endswith(".tmp") for path in client.data)
    assert any(path.endswith(".bak") for path in client.data)


def test_permission_change_requires_reload():
    client = FakeSFTP()
    backend = backend_for(client)
    original = backend.load("/etc/app.conf")
    client.metadata[original.path] = (0o600, 1000, 1000)
    with pytest.raises(RemoteFileConflict):
        backend.save(original, b"draft\n")


def test_symlink_retarget_requires_reload():
    client = FakeSFTP()
    client.normalize = lambda path: "/etc/app.conf"
    backend = backend_for(client)
    original = backend.load("/app-link")
    client.normalize = lambda path: "/etc/other.conf"
    with pytest.raises(RemoteFileConflict):
        backend.save(original, b"draft\n")


def test_directory_compare_uses_contents_and_does_not_write():
    client = FakeSFTP()
    client.data["/etc/same.txt"] = b"same"
    client.data["/etc/remote-only.txt"] = b"remote"
    original = dict(client.data)
    with tempfile.TemporaryDirectory() as local:
        Path(local, "app.conf").write_bytes(b"port=81\nname=demo\n")
        Path(local, "same.txt").write_bytes(b"same")
        Path(local, "local-only.txt").write_bytes(b"local")
        rows = backend_for(client).compare_directory(local, "/etc")
    states = {r["relative"]: r["state"] for r in rows}
    assert states == {"app.conf": "Different", "same.txt": "Identical", "local-only.txt": "Local only", "remote-only.txt": "Remote only"}
    assert client.data == original


def test_editor_requires_review_and_keeps_draft_on_conflict():
    client = FakeSFTP()
    dialog = RemoteFileEditor(backend_for(client), "/etc/app.conf")
    try:
        wait_until(lambda: dialog.snapshot is not None and not dialog._busy)
        dialog.editor.setPlainText("port=443\n")
        assert not dialog.btn_save.isEnabled()
        dialog.save_changes()
        assert dialog.tabs.currentIndex() == 1
        assert dialog.btn_save.isEnabled()
        assert not client.replacements
        client.data["/etc/app.conf"] = b"new remote version\n"
        dialog.save_changes()
        wait_until(lambda: not dialog._busy)
        assert dialog.editor.toPlainText() == "port=443\n"
        assert "remote file changed" in dialog.status.text()
        assert client.data["/etc/app.conf"] == b"new remote version\n"
    finally:
        dialog.deleteLater()


def test_unsaved_close_and_reload_default_to_cancel():
    dialog = RemoteFileEditor(backend_for(FakeSFTP()), "/etc/app.conf")
    try:
        wait_until(lambda: dialog.snapshot is not None)
        dialog.editor.setPlainText("draft\n")
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel):
            assert not dialog._may_close()
            dialog._reload()
        assert dialog.editor.toPlainText() == "draft\n"
    finally:
        dialog.deleteLater()


class FakeFTP:
    def __init__(self):
        self.data = {"/site/app.conf": b"port=80\n"}
        self.directory = "/site"
        self.fail_replace = False
        self.links = set()

    def pwd(self):
        return self.directory

    def cwd(self, path):
        self.directory = path

    def mlsd(self, facts=None):
        for path, raw in sorted(self.data.items()):
            if str(Path(path).parent).replace("\\", "/") == self.directory:
                yield Path(path).name, {"type": "OS.unix=slink" if path in self.links else "file", "size": str(len(raw))}

    def voidcmd(self, command):
        assert command == "TYPE I"

    def size(self, path):
        return len(self.data[path])

    def retrbinary(self, command, callback):
        callback(self.data[command.removeprefix("RETR ")])

    def storbinary(self, command, handle):
        self.data[command.removeprefix("STOR ")] = handle.read()

    def rename(self, source, target):
        if self.fail_replace:
            raise OSError("server refuses replacement")
        self.data[target] = self.data.pop(source)

    def delete(self, path):
        del self.data[path]


def ftp_backend(client):
    @contextmanager
    def session():
        yield client
    return RemoteFileBackend(session, protocol="ftp", label="FTP test")


def test_ftp_save_verifies_backup_and_leaves_browser_directory_alone():
    client = FakeFTP()
    backend = ftp_backend(client)
    snapshot = backend.load("/site/app.conf")
    saved, backup = backend.save(snapshot, b"port=443\n")
    assert client.data[backup] == b"port=80\n"
    assert saved.document.raw == b"port=443\n"
    assert client.directory == "/site"


def test_ftp_rename_failure_and_conflicts_preserve_existing_file():
    client = FakeFTP()
    backend = ftp_backend(client)
    snapshot = backend.load("/site/app.conf")
    client.fail_replace = True
    with pytest.raises(RemoteFileError):
        backend.save(snapshot, b"draft\n")
    assert client.data[snapshot.path] == snapshot.document.raw
    assert not any(path.endswith(".tmp") for path in client.data)
    client.data[snapshot.path] = b"someone else's edit\n"
    with pytest.raises(RemoteFileConflict):
        backend.save(snapshot, b"draft\n")
    assert client.data[snapshot.path] == b"someone else's edit\n"


def test_ftp_links_are_not_replaced():
    client = FakeFTP()
    client.links.add("/site/app.conf")
    with pytest.raises(RemoteFileError, match="regular remote file"):
        ftp_backend(client).load("/site/app.conf")


def test_ftp_directory_comparison_uses_byte_sizes_and_contents():
    client = FakeFTP()
    with tempfile.TemporaryDirectory() as local:
        Path(local, "app.conf").write_bytes(b"port=80\n")
        rows = ftp_backend(client).compare_directory(local, "/site")
    assert rows[0]["state"] == "Identical"
    assert rows[0]["remote"]["size"] == 8
    assert client.directory == "/site"


def test_corrupt_staged_upload_is_not_applied():
    client = FakeSFTP()
    backend = backend_for(client)
    snapshot = backend.load("/etc/app.conf")
    original_open = client.open
    def corrupt_open(path, mode):
        if mode == "rb" and path.endswith(".tmp"):
            return io.BytesIO(b"corrupt")
        return original_open(path, mode)
    client.open = corrupt_open
    with pytest.raises(RemoteFileError, match="staged upload did not match"):
        backend.save(snapshot, b"my draft\n")
    assert client.data[snapshot.path] == snapshot.document.raw
    assert not client.replacements


def test_exclusive_creation_failure_does_not_delete_an_existing_auxiliary_file():
    client = FakeSFTP()
    backend = backend_for(client)
    snapshot = backend.load("/etc/app.conf")
    path = "/etc/.app.conf.rpmote-fixed.tmp"
    client.data[path] = b"belongs to someone else"
    with mock.patch("app.remote_files.secrets.token_hex", return_value="fixed"):
        with pytest.raises(RemoteFileError):
            backend.save(snapshot, b"draft\n")
    assert client.data[path] == b"belongs to someone else"


def test_recursive_comparison_and_large_equal_size_files_are_explicit():
    client = FakeSFTP()
    client.directories.add("/etc/sub")
    client.data["/etc/sub/child.conf"] = b"same"
    client.data["/etc/large.txt"] = b"x" * (MAX_EDIT_BYTES + 1)
    with tempfile.TemporaryDirectory() as local:
        Path(local, "sub").mkdir()
        Path(local, "sub", "child.conf").write_bytes(b"same")
        Path(local, "large.txt").write_bytes(client.data["/etc/large.txt"])
        backend = backend_for(client)
        flat = backend.compare_directory(local, "/etc")
        recursive = backend.compare_directory(local, "/etc", True)
    assert not any(row["relative"] == "sub/child.conf" for row in flat)
    states = {row["relative"]: row["state"] for row in recursive}
    assert states["sub/child.conf"] == "Identical"
    assert states["large.txt"].startswith("Not compared")


def test_editor_successful_save_updates_baseline_and_reports_backup():
    client = FakeSFTP()
    dialog = RemoteFileEditor(backend_for(client), "/etc/app.conf")
    saved = []
    dialog.saved.connect(saved.append)
    try:
        wait_until(lambda: dialog.snapshot is not None and not dialog._busy)
        dialog.editor.setPlainText("port=443\n")
        dialog.review_changes()
        wait_until(lambda: not dialog._busy)
        dialog.save_changes()
        wait_until(lambda: not dialog._busy)
        assert not dialog.dirty
        assert client.data["/etc/app.conf"] == b"port=443\n"
        assert "Original backup:" in dialog.backup_label.text()
        assert saved == ["/etc/app.conf"]
    finally:
        dialog.deleteLater()


def test_local_version_opens_as_reviewable_draft_without_writing():
    client = FakeSFTP()
    with tempfile.TemporaryDirectory() as local:
        draft = Path(local, "app.conf")
        draft.write_bytes(b"local version\n")
        dialog = RemoteFileEditor(backend_for(client), "/etc/app.conf", draft_path=str(draft))
        try:
            wait_until(lambda: dialog.snapshot is not None and not dialog._busy)
            assert dialog.editor.toPlainText() == "local version\n"
            assert dialog.tabs.currentIndex() == 1
            assert dialog.dirty and dialog.btn_save.isEnabled()
            assert not client.replacements
        finally:
            dialog.deleteLater()


def test_browser_double_clicks_open_the_editor_and_directory_clicks_navigate():
    from app.sftp_browser import SFTPBrowser
    from app.ftp_browser import FTPBrowser
    from app.remote_file_browser import RemoteFileBrowser
    from PyQt6.QtWidgets import QTreeWidgetItem
    for cls in (SFTPBrowser, FTPBrowser, RemoteFileBrowser):
        browser = cls({"host": "example.invalid"}) if cls is not RemoteFileBrowser else cls()
        opened = []
        try:
            if cls is RemoteFileBrowser:
                browser.file_open_requested.disconnect(browser._open_remote_file)
                browser.file_open_requested.connect(opened.append)
                action = browser._on_double_click
            else:
                browser._open_remote_file = opened.append
                action = browser._remote_dblclick
            item = QTreeWidgetItem(["app.conf"])
            item.setData(0, Qt.ItemDataRole.UserRole, {"name": "app.conf", "path": "/etc/app.conf", "is_dir": False})
            action(item, 0)
            assert opened == ["/etc/app.conf"]
        finally:
            browser.deleteLater()


@pytest.mark.parametrize("suffix,raw", [
    (".yml", b""),
    (".yaml", codecs.BOM_UTF8 + b"# compose\r\nservices:\r\n    web: app\r\n"),
    (".YML", codecs.BOM_UTF16_LE + "# compose\r\nservices:\r\n  web: app\r\n".encode("utf-16-le")),
])
def test_yaml_editor_typing_and_remote_save_preserve_the_existing_format(suffix, raw):
    client = FakeSFTP()
    path = "/etc/compose" + suffix
    client.data[path] = raw
    client.metadata[path] = (0o640, 1000, 1000)
    dialog = RemoteFileEditor(backend_for(client), path)
    try:
        wait_until(lambda: dialog.snapshot is not None and not dialog._busy)
        assert dialog.editor.yaml
        assert not dialog.dirty
        expected_width = 4 if suffix == ".yaml" else 2
        assert dialog.editor.indent_width == expected_width
        dialog.editor.moveCursor(QTextCursor.MoveOperation.End)
        QTest.keyClick(dialog.editor, Qt.Key.Key_Tab)
        QTest.keyClicks(dialog.editor, "port: 80")
        QTest.keyClick(dialog.editor, Qt.Key.Key_Return)
        assert "\t" not in dialog.editor.toPlainText()
        expected_text = TextDocument.decode(raw).text + " " * expected_width + "port: 80\n" + " " * expected_width
        assert dialog.editor.toPlainText() == expected_text
        assert not dialog.btn_save.isEnabled()
        dialog.review_changes()
        wait_until(lambda: not dialog._busy)
        dialog.save_changes()
        wait_until(lambda: not dialog._busy)
        expected = TextDocument.decode(raw).encode(expected_text)
        assert client.data[path] == expected
        assert not dialog.dirty
        backups = [content for name, content in client.data.items() if name.endswith(".bak")]
        assert backups == [raw]
    finally:
        dialog.deleteLater()


def yaml_dialog(raw=b"# original\nservices:\n  web:\n    image: app\n"):
    client, path = FakeSFTP(), "/etc/compose.yml"
    client.data[path] = raw
    client.metadata[path] = (0o640, 1000, 1000)
    dialog = RemoteFileEditor(backend_for(client), path)
    wait_until(lambda: dialog.snapshot is not None and not dialog._busy)
    return dialog, client, path


def test_invalid_yaml_is_not_saved_and_corrected_draft_is_rechecked():
    dialog, client, path = yaml_dialog()
    baseline = client.data[path]
    invalid = "services:\n  web:\n    image: app\n   bad: indent\n"
    try:
        dialog.editor.setPlainText(invalid)
        dialog.save_changes()
        wait_until(lambda: not dialog._busy)
        assert "YAML error at line" in dialog.status.text()
        assert "column" in dialog.status.text()
        assert not dialog.btn_save.isEnabled()
        assert dialog.editor.toPlainText() == invalid
        assert not client.replacements
        assert client.data[path] == baseline
        assert not any(name.endswith(".bak") for name in client.data)
        corrected = "services:\n  web:\n    image: app:next\n"
        dialog.editor.setPlainText(corrected)
        dialog.review_changes()
        wait_until(lambda: not dialog._busy)
        assert dialog.btn_save.isEnabled()
        dialog.save_changes()
        wait_until(lambda: not dialog._busy)
        assert client.data[path] == corrected.encode()
    finally:
        dialog.deleteLater()


def test_save_repairs_indentation_tabs_in_the_reviewed_draft_before_uploading():
    dialog, client, path = yaml_dialog(b"")
    try:
        dialog.editor.setPlainText("services:\n\tweb:\n\t\timage: app\n")
        dialog.save_changes()
        wait_until(lambda: not dialog._busy)
        expected = "services:\n  web:\n    image: app\n"
        assert dialog.editor.toPlainText() == expected
        assert "+  web:" in dialog.diff.toPlainText()
        assert client.data[path] == b""
        assert dialog.btn_save.isEnabled()
        dialog.save_changes()
        wait_until(lambda: not dialog._busy)
        assert client.data[path] == expected.encode()
        assert not dialog.dirty
    finally:
        dialog.deleteLater()


def test_format_yaml_changes_only_the_draft_as_one_undoable_edit():
    dialog, client, path = yaml_dialog(b"")
    source = '# keep\nservices:\n   web:\n       image: "app:latest"\n'
    try:
        dialog.editor.setPlainText(source)
        dialog._format_yaml()
        wait_until(lambda: not dialog._busy)
        formatted = dialog.editor.toPlainText()
        assert '\n  web:\n    image: "app:latest"' in formatted
        assert "# keep" in formatted
        assert dialog.tabs.currentIndex() == 0
        assert not dialog.btn_save.isEnabled()
        assert client.data[path] == b""
        dialog.editor.undo()
        assert dialog.editor.toPlainText() == source
        assert dialog._yaml_checked_text is None
    finally:
        dialog.deleteLater()


def test_save_after_format_preserves_utf16_bom_line_endings_and_original_backup():
    raw = codecs.BOM_UTF16_BE + '# keep\r\nservices:\r\n   web:\r\n       image: "app:latest"\r\n'.encode("utf-16-be")
    dialog, client, path = yaml_dialog(raw)
    try:
        dialog._format_yaml()
        wait_until(lambda: not dialog._busy)
        formatted = dialog.editor.toPlainText()
        assert dialog.dirty
        dialog.review_changes()
        wait_until(lambda: not dialog._busy)
        dialog.save_changes()
        wait_until(lambda: not dialog._busy)
        assert client.data[path] == codecs.BOM_UTF16_BE + formatted.replace("\n", "\r\n").encode("utf-16-be")
        assert [content for name, content in client.data.items() if name.endswith(".bak")] == [raw]
        assert not dialog.dirty
    finally:
        dialog.deleteLater()


@pytest.mark.parametrize("action,function", [("review_changes", "prepare_yaml"), ("_format_yaml", "format_yaml")])
def test_slow_yaml_processing_runs_in_a_worker_and_leaves_the_ui_responsive(action, function):
    dialog, client, path = yaml_dialog(b"")
    entered, release = threading.Event(), threading.Event()
    original = "services:\n  web: app\n"
    def slow(text, width):
        assert threading.get_ident() != main_thread
        entered.set()
        assert release.wait(3)
        return text
    main_thread = threading.get_ident()
    try:
        dialog.editor.setPlainText(original)
        with mock.patch("app.remote_file_editor." + function, side_effect=slow):
            getattr(dialog, action)()
            assert entered.wait(1)
            ticks = []
            QTimer.singleShot(0, lambda: ticks.append(True))
            APP.processEvents()
            assert ticks == [True]
            assert dialog.editor.isReadOnly()
            assert not dialog.btn_save.isEnabled()
            assert client.data[path] == b""
            release.set()
            wait_until(lambda: not dialog._busy)
        assert not dialog.editor.isReadOnly()
    finally:
        release.set()
        dialog.deleteLater()


def test_a_changed_draft_invalidates_an_earlier_yaml_validation():
    dialog, client, path = yaml_dialog(b"")
    try:
        dialog.editor.setPlainText("name: valid\n")
        dialog.review_changes()
        wait_until(lambda: not dialog._busy)
        assert dialog.btn_save.isEnabled()
        invalid = "name: [broken\n"
        dialog.editor.setPlainText(invalid)
        assert not dialog.btn_save.isEnabled()
        dialog.save_changes()
        wait_until(lambda: not dialog._busy)
        assert "YAML error" in dialog.status.text()
        assert client.data[path] == b""
    finally:
        dialog.deleteLater()
