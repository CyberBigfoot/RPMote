"""
RPMote shared file-browser helpers.

Formatting helpers and a local-filesystem operations mixin used by both the
SFTP and FTP dual-pane browsers so "new folder / new file / rename / delete"
behaves identically on the local side of every transfer tab.
"""

import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog,
    QInputDialog, QMessageBox, QTreeWidget,
)


class RemoteEditingMixin:
    """Editing actions shared by the Library, SFTP and FTP browsers."""

    def _file_backend(self):
        from app.remote_files import RemoteFileBackend, RemoteFileError, ssh_file_backend
        info = getattr(self, "connection_info", None) or getattr(self, "_connection_info", {})
        label = " · ".join(str(info.get(key, "")) for key in ("tenant", "name", "host") if info.get(key)) or "Remote host"
        if hasattr(self, "ftp"):
            captured = self.ftp
            if captured is None or not self._connected:
                raise RemoteFileError("Connect to the remote host first.")
            @contextmanager
            def session():
                with self._ftp_lock:
                    if not self._connected or self.ftp is not captured:
                        raise RemoteFileError("The original FTP session is disconnected. Reopen the file after reconnecting.")
                    yield captured
            return RemoteFileBackend(session, protocol="ftp", label=label)
        client = getattr(self, "_ssh_client", None) or getattr(self, "client", None)
        if client is None:
            raise RemoteFileError("Open an SSH or SFTP session first.")
        return ssh_file_backend(client, label)

    def _remote_edit_selected(self):
        getter = getattr(self, "_remote_selection", None) or self._selection
        entries = getter()
        if len(entries) == 1 and not entries[0].get("is_dir") and entries[0].get("name") != "..":
            self._open_remote_file(entries[0]["path"])

    def _open_remote_file(self, path):
        from app.remote_file_editor import RemoteFileEditor
        try:
            backend = self._file_backend()
        except Exception as exc:
            QMessageBox.warning(self, "Remote file editor", str(exc))
            return
        dialog = RemoteFileEditor(backend, path, self)
        dialog.saved.connect(self._remote_editor_saved)
        dialog.exec()

    def _remote_editor_saved(self, _path):
        refresh = getattr(self, "_refresh_remote", None) or self._refresh
        refresh()

    def _compare_folders(self):
        from app.remote_file_editor import DirectoryCompareDialog
        try:
            backend = self._file_backend()
        except Exception as exc:
            QMessageBox.warning(self, "Compare folders", str(exc))
            return
        local = getattr(self, "_local_cwd", None)
        if local is None:
            local = QFileDialog.getExistingDirectory(self, "Choose a local folder to compare", str(Path.home()))
            if not local:
                return
        remote = getattr(self, "_remote_cwd", None) or self._current_path
        DirectoryCompareDialog(backend, local, remote, self).exec()


def human_size(n) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_time(ts) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def perms_str(mode) -> str:
    p = []
    for w in (6, 3, 0):
        p.append(
            ("r" if mode & (4 << w) else "-")
            + ("w" if mode & (2 << w) else "-")
            + ("x" if mode & (1 << w) else "-")
        )
    return "".join(p)


def valid_name(parent, name: str) -> str:
    """Validate a user-entered file/folder name. Returns "" when unusable."""
    name = (name or "").strip().strip("/\\")
    if not name or name in (".", ".."):
        QMessageBox.warning(parent, "Invalid name", "Please enter a valid name.")
        return ""
    if any(ch in name for ch in '\\/:*?"<>|'):
        QMessageBox.warning(
            parent, "Invalid name", 'A name may not contain \\ / : * ? " < > |'
        )
        return ""
    return name


def ask_name(parent, title: str, label: str, preset: str = "") -> str:
    """Prompt for a file/folder name and validate it."""
    text, ok = QInputDialog.getText(parent, title, label, text=preset)
    if not ok:
        return ""
    return valid_name(parent, text)


def confirm_delete(parent, names: list, where: str) -> bool:
    """Confirm deletion of one or more entries. Folders delete recursively."""
    if not names:
        return False
    if len(names) == 1:
        body = f"Delete '{names[0]}' from {where}?"
    else:
        preview = "\n".join(f"  • {n}" for n in names[:8])
        more = f"\n  … and {len(names) - 8} more" if len(names) > 8 else ""
        body = f"Delete these {len(names)} items from {where}?\n\n{preview}{more}"
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Confirm delete")
    box.setText(body)
    box.setInformativeText("Folders are deleted with everything inside them. This cannot be undone.")
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes

from app.themes import accent_rgba

class LocalFileOpsMixin:
    """Create / rename / delete on the local pane.

    Host widget must provide: ``self._local_cwd``, ``self.local_tree``,
    ``self._refresh_local()`` and ``self._notify(text)``.
    """

    # ── selection helpers ───────────────────────────────────────────

    def _local_selection(self) -> list:
        items = self.local_tree.selectedItems()
        if not items:
            current = self.local_tree.currentItem()
            items = [current] if current else []
        out = []
        for item in items:
            from PyQt6.QtCore import Qt
            d = item.data(0, Qt.ItemDataRole.UserRole)
            if d and d.get("name") != "..":
                out.append(d)
        return out

    def _local_target_dir(self) -> str:
        """Directory new items land in: the selected folder, else the cwd."""
        sel = self._local_selection()
        if len(sel) == 1 and sel[0].get("is_dir"):
            return sel[0]["path"]
        return self._local_cwd

    # ── operations ──────────────────────────────────────────────────

    def _local_new_folder(self):
        name = ask_name(self, "New folder", "Folder name:", "New folder")
        if not name:
            return
        target = os.path.join(self._local_target_dir(), name)
        try:
            os.makedirs(target, exist_ok=False)
            self._notify(f"Created folder: {name}")
        except FileExistsError:
            self._notify(f"Already exists: {name}")
        except Exception as e:
            QMessageBox.warning(self, "Create folder failed", str(e))
        self._refresh_local()

    def _local_new_file(self):
        name = ask_name(self, "New file", "File name:", "new_file.txt")
        if not name:
            return
        target = os.path.join(self._local_target_dir(), name)
        if os.path.exists(target):
            self._notify(f"Already exists: {name}")
            return
        try:
            with open(target, "x", encoding="utf-8"):
                pass
            self._notify(f"Created file: {name}")
        except Exception as e:
            QMessageBox.warning(self, "Create file failed", str(e))
        self._refresh_local()

    def _local_rename(self):
        sel = self._local_selection()
        if len(sel) != 1:
            self._notify("Select a single item to rename")
            return
        old = sel[0]
        name = ask_name(self, "Rename", "New name:", old["name"])
        if not name or name == old["name"]:
            return
        new_path = os.path.join(os.path.dirname(old["path"]), name)
        if os.path.exists(new_path):
            QMessageBox.warning(self, "Rename failed", f"'{name}' already exists.")
            return
        try:
            os.rename(old["path"], new_path)
            self._notify(f"Renamed to {name}")
        except Exception as e:
            QMessageBox.warning(self, "Rename failed", str(e))
        self._refresh_local()

    def _local_delete(self):
        sel = self._local_selection()
        if not sel:
            return
        if not confirm_delete(self, [d["name"] for d in sel], "this computer"):
            return
        failed = []
        for d in sel:
            try:
                if d.get("is_dir"):
                    shutil.rmtree(d["path"])
                else:
                    os.remove(d["path"])
            except Exception as e:
                failed.append(f"{d['name']}: {e}")
        self._refresh_local()
        if failed:
            QMessageBox.warning(self, "Delete failed", "\n".join(failed[:6]))
            self._notify(f"Deleted {len(sel) - len(failed)}/{len(sel)}")
        else:
            self._notify(f"Deleted {len(sel)} item{'s' if len(sel) != 1 else ''}")

    def _local_reveal(self):
        """Open the current local folder in the system file manager."""
        target = self._local_target_dir()
        try:
            if sys.platform.startswith("win"):
                os.startfile(target)  # noqa: S606 - user-initiated
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except Exception as e:
            self._notify(f"Could not open folder: {e}")

    def _local_home(self):
        self._local_cwd = str(Path.home())
        self._refresh_local()


# ── Picking files and folders together ──────────────────────────────


class MixedFilePicker(QFileDialog):
    """File dialog that returns any mix of files and folders in one go.

    Qt offers files-only multi-select or a single folder — never both. The
    non-native dialog in Directory mode is the one configuration that can show
    files *and* folders, so this turns multi-select on for its item views and
    relaxes two Directory-mode rules that would otherwise get in the way:
    the OK button is only enabled when the first entry is a folder, and accept()
    refuses a selection that does not start with one.
    """

    def __init__(self, parent=None, caption="", directory=""):
        super().__init__(parent, caption, directory)
        self.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        self.setFileMode(QFileDialog.FileMode.Directory)
        self.setOption(QFileDialog.Option.ShowDirsOnly, False)

        self._file_views = []
        for view in self.findChildren(QAbstractItemView):
            model = view.model()
            if model is not None and hasattr(model, "filePath"):
                view.setSelectionMode(
                    QAbstractItemView.SelectionMode.ExtendedSelection
                )
                self._file_views.append(view)
                selection = view.selectionModel()
                if selection is not None:
                    selection.selectionChanged.connect(self._sync_accept_button)

        self._button_box = self.findChild(QDialogButtonBox)

    @property
    def usable(self) -> bool:
        """False when Qt's internals no longer match what this relies on."""
        return bool(self._file_views)

    def _accept_button(self):
        if self._button_box is None:
            return None
        for standard in (
            QDialogButtonBox.StandardButton.Open,
            QDialogButtonBox.StandardButton.Ok,
            QDialogButtonBox.StandardButton.Save,
        ):
            button = self._button_box.button(standard)
            if button is not None:
                return button
        return None

    def _sync_accept_button(self, *_):
        button = self._accept_button()
        if button is not None:
            button.setEnabled(bool(self.chosen_paths()))

    def chosen_paths(self) -> list:
        """Everything highlighted in the browser, files and folders alike."""
        paths, seen = [], set()
        for view in self._file_views:
            selection = view.selectionModel()
            if selection is None:
                continue
            model = view.model()
            for index in selection.selectedIndexes():
                if index.column() != 0:
                    continue
                try:
                    path = model.filePath(index)
                except Exception:
                    continue
                if path and path not in seen and os.path.exists(path):
                    seen.add(path)
                    paths.append(path)
        if not paths:
            # Nothing highlighted — fall back to whatever the dialog itself
            # considers selected (a typed path, or the folder being viewed).
            for path in self.selectedFiles():
                if path and path not in seen and os.path.exists(path):
                    seen.add(path)
                    paths.append(path)
        return paths

    def accept(self):
        # Directory mode would refuse anything that is not a folder; we accept
        # whatever is highlighted.
        if self.chosen_paths():
            QDialog.accept(self)


def pick_files_and_folders(parent, caption="Select files and folders", directory="") -> list:
    """Prompt for any mix of files and folders. Returns [] if cancelled."""
    dialog = MixedFilePicker(parent, caption, directory)
    if not dialog.usable:
        # Qt changed underneath us — degrade to a files-only picker rather
        # than handing back a dialog that cannot be confirmed.
        paths, _ = QFileDialog.getOpenFileNames(parent, caption, directory)
        return list(paths)
    if dialog.exec() != QFileDialog.DialogCode.Accepted:
        return []
    return dialog.chosen_paths()


def count_files(paths) -> int:
    """Total number of regular files in `paths`, walking into directories."""
    total = 0
    for path in paths:
        if os.path.isdir(path) and not os.path.islink(path):
            for _root, _dirs, files in os.walk(path):
                total += len(files)
        elif os.path.exists(path):
            total += 1
    return total


# ── Accepting files dragged in from the desktop ─────────────────────


def dropped_local_paths(mime) -> list:
    """Local filesystem paths carried by a drag, in the order they were dropped."""
    if mime is None or not mime.hasUrls():
        return []
    paths = []
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        path = url.toLocalFile()
        if path and os.path.exists(path):
            paths.append(os.path.normpath(path))
    return paths


def copy_into(paths, dest_dir: str):
    """Copy files and folders into `dest_dir`. Returns (copied, errors)."""
    copied, errors = 0, []
    for path in paths:
        name = os.path.basename(path.rstrip("/\\")) or path
        target = os.path.join(dest_dir, name)
        try:
            if os.path.abspath(path) == os.path.abspath(target):
                continue  # dropped back where it came from
            if os.path.isdir(path):
                shutil.copytree(path, target, dirs_exist_ok=True, symlinks=True)
            else:
                shutil.copy2(path, target)
            copied += 1
        except Exception as e:
            errors.append(f"{name}: {e}")
    return copied, errors


class ExternalDropTree(QTreeWidget):
    """Tree that accepts files and folders dragged in from the desktop.

    Drops carrying local files are handled here; anything else is offered to
    ``_internal_drag_*`` so a subclass can keep its own drag behaviour, and is
    otherwise refused — which stops Qt's built-in row dragging from silently
    reordering a listing that mirrors a remote filesystem.
    """

    external_drop = pyqtSignal(list, object)  # local paths, target dir or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDropIndicatorShown(False)
        self._base_style = ""
        self._drop_active = False

    # ── styling ─────────────────────────────────────────────────────

    def set_base_style(self, style: str):
        self._base_style = style
        self._apply_style()

    def _set_drop_active(self, active: bool):
        if self._drop_active == active:
            return
        self._drop_active = active
        self._apply_style()

    def _apply_style(self):
        highlight = ""
        if self._drop_active:
            highlight = (
                f"QTreeWidget {{ border: 1px dashed {accent_rgba(1)};"
                f" background: {accent_rgba(0.08)}; }}"
            )
        self.setStyleSheet(self._base_style + highlight)

    # ── hooks for subclasses with their own drag behaviour ──────────

    def _internal_drag_enter(self, event) -> bool:
        return False

    def _internal_drag_move(self, event) -> bool:
        return False

    def _internal_drop(self, event) -> bool:
        return False

    # ── events ──────────────────────────────────────────────────────

    @staticmethod
    def _dropped_paths(mime) -> list:
        return dropped_local_paths(mime)

    def _drop_target(self, event):
        """Directory under the cursor, when the row is a folder."""
        item = self.itemAt(event.position().toPoint())
        if item is None:
            return None
        from PyQt6.QtCore import Qt as _Qt
        data = item.data(0, _Qt.ItemDataRole.UserRole)
        if data and data.get("is_dir") and data.get("name") != "..":
            return data.get("path")
        return None

    def dragEnterEvent(self, event):
        if self._dropped_paths(event.mimeData()):
            self._set_drop_active(True)
            event.setDropAction(Qt.DropAction.CopyAction)
            event.acceptProposedAction()
            return
        if self._internal_drag_enter(event):
            return
        event.ignore()

    def dragMoveEvent(self, event):
        if self._dropped_paths(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.acceptProposedAction()
            return
        if self._internal_drag_move(event):
            return
        event.ignore()

    def dragLeaveEvent(self, event):
        self._set_drop_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        paths = self._dropped_paths(event.mimeData())
        self._set_drop_active(False)
        if paths:
            self.external_drop.emit(paths, self._drop_target(event))
            event.setDropAction(Qt.DropAction.CopyAction)
            event.acceptProposedAction()
            return
        if self._internal_drop(event):
            return
        event.ignore()
