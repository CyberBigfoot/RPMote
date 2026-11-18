"""Slow SFTP requests must leave Qt responsive and remain on their original host."""

import io
import os
import stat
import threading
import time
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication

from app.remote_file_browser import RemoteFileBrowser

APP = QApplication.instance() or QApplication([])


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        APP.processEvents()
        time.sleep(0.005)
    assert predicate(), "background operation did not finish"


class Server:
    def __init__(self):
        self.nodes = {"/": stat.S_IFDIR, "/srv": stat.S_IFDIR,
                      "/srv/tree": stat.S_IFDIR, "/srv/tree/sub": stat.S_IFDIR,
                      "/srv/tree/sub/a.yml": stat.S_IFREG,
                      "/srv/large.bin": stat.S_IFREG, "/srv/config.yml": stat.S_IFREG}
        self.calls, self.channels, self.gates, self.failures = [], [], {}, set()

    def call(self, operation, path):
        self.calls.append((operation, path, threading.get_ident()))
        gate = self.gates.get((operation, path))
        if gate:
            entered, release = gate
            entered.set()
            assert release.wait(3), "test forgot to release a blocked SFTP request"
        if (operation, path) in self.failures:
            raise PermissionError(f"Permission denied: {path}")

    def block(self, operation, path):
        gate = threading.Event(), threading.Event()
        self.gates[operation, path] = gate
        return gate

    def open_sftp(self):
        self.call("open_sftp", "")
        channel = Channel(self)
        self.channels.append(channel)
        return channel


class Channel:
    def __init__(self, server):
        self.server, self.closed, self.timeout = server, False, None

    def get_channel(self):
        return self

    def settimeout(self, timeout):
        self.timeout = timeout

    def normalize(self, path):
        self.server.call("normalize", path)
        return "/srv"

    def listdir_attr(self, path):
        self.server.call("list", path)
        if self.server.nodes[path] != stat.S_IFDIR:
            raise NotADirectoryError(path)
        return [SimpleNamespace(filename=child.rsplit("/", 1)[-1], st_mode=mode | 0o755, st_size=10)
                for child, mode in list(self.server.nodes.items())
                if child != path and (child.rsplit("/", 1)[0] or "/") == path]

    def lstat(self, path):
        self.server.call("lstat", path)
        if path not in self.server.nodes:
            raise FileNotFoundError(path)
        return SimpleNamespace(st_mode=self.server.nodes[path] | 0o755)

    def mkdir(self, path):
        self.server.call("mkdir", path)
        if path in self.server.nodes:
            raise FileExistsError(path)
        self.server.nodes[path] = stat.S_IFDIR

    def open(self, path, mode):
        self.server.call("file", path)
        assert "w" in mode and "x" in mode, "SFTP create needs WRITE and EXCL"
        if path in self.server.nodes:
            raise FileExistsError(f"Already exists: {path}")
        self.server.nodes[path] = stat.S_IFREG
        return io.BytesIO()

    def remove(self, path):
        self.server.call("remove", path)
        assert self.server.nodes[path] != stat.S_IFDIR
        del self.server.nodes[path]

    def rename(self, source, target):
        self.server.call("rename", source)
        self.server.nodes[target] = self.server.nodes.pop(source)

    def chmod(self, path, mode):
        self.server.call("chmod", path)

    def rmdir(self, path):
        self.server.call("rmdir", path)
        assert not any(child.startswith(path + "/") for child in self.server.nodes)
        del self.server.nodes[path]

    def close(self):
        self.server.call("close", "")
        self.closed = True


@pytest.fixture
def connected():
    server, browser = Server(), RemoteFileBrowser()
    browser.set_ssh_client(server)
    wait_until(lambda: browser._sftp is not None and not browser._loading)
    try:
        yield browser, server
    finally:
        for entered, release in server.gates.values():
            release.set()
        browser.disconnect_sftp()
        browser.deleteLater()
        APP.processEvents()


def select(browser, names):
    browser.tree.clearSelection()
    for index in range(browser.tree.topLevelItemCount()):
        item = browser.tree.topLevelItem(index)
        if item.data(0, Qt.ItemDataRole.UserRole)["name"] in names:
            item.setSelected(True)


def idle(browser):
    wait_until(lambda: not browser._loading and not browser._mutation_busy)


def assert_ui_responsive():
    ticks = []
    QTimer.singleShot(0, lambda: ticks.append(True))
    APP.processEvents()
    assert ticks == [True]


@pytest.mark.parametrize("kind,operation,name", [("folder", "mkdir", "new-folder"), ("file", "file", "new.yml")])
def test_slow_create_keeps_ui_running_and_uses_a_separate_channel(connected, kind, operation, name):
    browser, server = connected
    entered, release = server.block(operation, "/srv/" + name)
    browsing_channel = browser._sftp
    with mock.patch("app.remote_file_browser.ask_name", return_value=name):
        getattr(browser, "_create_" + kind)()
    try:
        assert entered.wait(1)
        assert browser._mutation_busy
        assert not browser.btn_new_file.isEnabled()
        assert_ui_responsive()
        browser._create_folder()  # Duplicate requests are refused before prompting.
        assert "already running" in browser.status_label.text()
    finally:
        release.set()
    idle(browser)
    assert "/srv/" + name in server.nodes
    assert "Created" in browser.status_label.text()
    assert browser.btn_new_file.isEnabled()
    assert not browsing_channel.closed
    assert server.channels[-1].closed
    assert all(channel.timeout == browser.REQUEST_TIMEOUT for channel in server.channels)
    assert all(thread != threading.get_ident() for _, _, thread in server.calls)


def test_create_never_overwrites_an_existing_file(connected):
    browser, server = connected
    with mock.patch("app.remote_file_browser.ask_name", return_value="config.yml"):
        browser._create_file()
    idle(browser)
    assert server.nodes["/srv/config.yml"] == stat.S_IFREG
    assert "Already exists" in browser.status_label.text()


def test_bulk_delete_stays_responsive_and_removes_children_before_parents(connected):
    browser, server = connected
    entered, release = server.block("remove", "/srv/tree/sub/a.yml")
    select(browser, {"tree", "large.bin", "config.yml"})
    with mock.patch("app.remote_file_browser.confirm_delete", return_value=True):
        browser._delete_selection()
    try:
        assert entered.wait(1)
        assert_ui_responsive()
    finally:
        release.set()
    idle(browser)
    assert server.nodes == {"/": stat.S_IFDIR, "/srv": stat.S_IFDIR}
    assert "Deleted 3 item(s)" in browser.status_label.text()
    removals = [(op, path) for op, path, _ in server.calls if op in ("remove", "rmdir")]
    assert removals.index(("remove", "/srv/tree/sub/a.yml")) < removals.index(("rmdir", "/srv/tree/sub"))
    assert removals.index(("rmdir", "/srv/tree/sub")) < removals.index(("rmdir", "/srv/tree"))
    assert all(thread != threading.get_ident() for _, _, thread in server.calls)


def test_bulk_delete_reports_denied_items_and_continues_the_batch(connected):
    browser, server = connected
    server.failures.add(("remove", "/srv/config.yml"))
    select(browser, {"config.yml", "large.bin"})
    with mock.patch("app.remote_file_browser.confirm_delete", return_value=True):
        browser._delete_selection()
    idle(browser)
    assert "/srv/large.bin" not in server.nodes
    assert "/srv/config.yml" in server.nodes
    assert "Deleted 1/2" in browser.status_label.text()
    assert "Permission denied" in browser.status_label.text()


def test_deletion_never_follows_directory_links_and_handles_deep_trees(connected):
    browser, server = connected
    server.nodes["/srv/tree/link"] = stat.S_IFLNK
    # This would exceed Python's recursive call limit in the original walker.
    path = "/srv/tree"
    for _ in range(1050):
        path += "/d"
        server.nodes[path] = stat.S_IFDIR
    browser._rmtree(Channel(server), "/srv/tree")
    assert not any(path.startswith("/srv/tree") for path in server.nodes)
    assert ("remove", "/srv/tree/link") in [(op, path) for op, path, _ in server.calls]


def test_navigation_is_background_and_latest_request_wins(connected):
    browser, server = connected
    entered, release = server.block("list", "/srv/tree")
    browser._load_directory("/srv/tree")
    try:
        assert entered.wait(1)
        browser._load_directory("/")
        browser._load_directory("/srv")
        assert_ui_responsive()
    finally:
        release.set()
    idle(browser)
    assert browser._current_path == "/srv"
    assert browser.path_edit.text() == "/srv"


def test_failed_navigation_keeps_the_existing_directory(connected):
    browser, server = connected
    server.failures.add(("list", "/srv/tree"))
    count = browser.tree.topLevelItemCount()
    browser._load_directory("/srv/tree")
    idle(browser)
    assert "Permission denied" in browser.status_label.text()
    assert browser._current_path == "/srv"
    assert browser.tree.topLevelItemCount() == count


def test_mutation_refresh_does_not_override_navigation_in_progress(connected):
    browser, server = connected
    entered, release = server.block("mkdir", "/srv/new")
    with mock.patch("app.remote_file_browser.ask_name", return_value="new"):
        browser._create_folder()
    try:
        assert entered.wait(1)
        browser._load_directory("/srv/tree")
        wait_until(lambda: not browser._loading)
    finally:
        release.set()
    idle(browser)
    assert browser._current_path == "/srv/tree"
    assert "Created folder: new" in browser.status_label.text()


def test_rename_and_permissions_also_use_background_channels(connected):
    browser, server = connected
    select(browser, {"config.yml"})
    with mock.patch("app.remote_file_browser.ask_name", return_value="renamed.yml"):
        browser._rename_selection()
    idle(browser)
    assert "/srv/renamed.yml" in server.nodes
    assert "/srv/config.yml" not in server.nodes
    select(browser, {"renamed.yml"})
    with mock.patch("app.remote_file_browser.QInputDialog.getText", return_value=("640", True)):
        browser._chmod_selection()
    idle(browser)
    assert "Set mode 640" in browser.status_label.text()
    assert all(thread != threading.get_ident() for _, _, thread in server.calls)


def test_request_timeout_is_reported_and_a_new_operation_can_run(connected):
    browser, server = connected
    def fail_create(path):
        raise TimeoutError("Remote request timed out")
    with mock.patch.object(Channel, "mkdir", side_effect=fail_create), mock.patch("app.remote_file_browser.ask_name", return_value="slow"):
        browser._create_folder()
    idle(browser)
    assert "timed out" in browser.status_label.text()
    assert browser.btn_new_folder.isEnabled()
    with mock.patch("app.remote_file_browser.ask_name", return_value="working"):
        browser._create_folder()
    idle(browser)
    assert "/srv/working" in server.nodes


def test_switching_hosts_cancels_remaining_deletes_and_ignores_old_results(connected):
    browser, server = connected
    entered, release = server.block("remove", "/srv/config.yml")
    select(browser, {"config.yml", "large.bin"})
    results = []
    browser._remote_result.connect(lambda *result: results.append(result))
    with mock.patch("app.remote_file_browser.confirm_delete", return_value=True):
        browser._delete_selection()
    try:
        assert entered.wait(1)
        replacement = Server()
        browser.set_ssh_client(replacement)
        wait_until(lambda: browser._sftp is not None and not browser._loading)
    finally:
        release.set()
    wait_until(lambda: any(result[1] == "mutation" for result in results))
    assert "/srv/large.bin" in server.nodes
    assert "/srv/config.yml" in replacement.nodes
    assert "/srv/large.bin" in replacement.nodes
    assert "Deleted" not in browser.status_label.text()
    assert not browser._mutation_busy


def test_slow_connect_does_not_freeze_and_stale_channel_is_closed():
    browser, server = RemoteFileBrowser(), Server()
    entered, release = server.block("open_sftp", "")
    browser.set_ssh_client(server)
    try:
        assert entered.wait(1)
        assert_ui_responsive()
        replacement = Server()
        browser.set_ssh_client(replacement)
        wait_until(lambda: browser._sftp is not None and not browser._loading)
        release.set()
        wait_until(lambda: server.channels and server.channels[0].closed)
        assert browser._ssh_client is replacement
        assert browser._sftp in replacement.channels
    finally:
        release.set()
        browser.disconnect_sftp()
        browser.deleteLater()

