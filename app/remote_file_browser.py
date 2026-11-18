"""
RPMote Remote File Browser — MobaXterm-style SFTP file tree for the Library panel.
"""

import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QLabel, QLineEdit, QPushButton, QMenu, QFileDialog, QApplication,
    QMessageBox, QInputDialog, QHeaderView, QAbstractItemView, QProgressBar
)
from PyQt6.QtCore import Qt, pyqtSignal, QMimeData, QUrl, QTimer

from app.file_browser_ops import (
    ExternalDropTree, ask_name, confirm_delete, count_files,
    pick_files_and_folders, RemoteEditingMixin,
)
from PyQt6.QtGui import (
    QAction, QColor, QFont, QDrag, QCursor, QShortcut, QKeySequence
)


def _human_size(size_bytes: int) -> str:
    """Format bytes to human-readable size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{size_bytes} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"

from app.icons import FOLDER_COLOR, file_icon_name
from app.widgets import iconize_children, iconize_menu, tree_item

class _LibraryTree(ExternalDropTree):
    """The Library's file tree.

    Everything it needs — accepting desktop drops, refusing internal row drags,
    the drop highlight — comes from ExternalDropTree, which the SFTP and FTP
    browser panes share.
    """


class RemoteFileBrowser(RemoteEditingMixin, QWidget):
    """SFTP file browser tree widget with path bar, context menu, and drag-and-drop."""

    file_download_requested = pyqtSignal(str)  # remote path
    file_open_requested = pyqtSignal(str)  # remote path
    upload_progress = pyqtSignal(str)
    upload_finished = pyqtSignal(int, int, list)  # uploaded, total, errors
    #: status text, overall progress in permille (-1 while still counting)
    download_progress = pyqtSignal(str, int)
    download_finished = pyqtSignal(int, int, list)  # downloaded, total, errors
    _remote_result = pyqtSignal(int, str, object, object)
    _mutation_progress = pyqtSignal(int, str)

    #: Progress bar resolution: per-mille keeps a multi-GB download moving.
    PROGRESS_SCALE = 1000
    #: Minimum gap between progress repaints. paramiko calls back once per
    #: 32 KB chunk; a large file would otherwise flood the GUI event queue.
    PROGRESS_INTERVAL = 0.15
    #: Error marker for a download stopped by a disconnect, not a failure.
    CANCELLED = "Cancelled: connection closed"
    REQUEST_TIMEOUT = 20

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sftp = None
        self._ssh_client = None
        self._connection_info = {}
        self._current_path = "/"
        self._loading = False
        self._generation = 0
        self._browse_lock = threading.Lock()
        self._pending_directory = None
        self._requested_path = self._current_path
        self._home_path = "/"
        self._mutation_busy = False
        self._upload_busy = False
        self._download_busy = False
        self._transfer_cancelled = False
        self._setup_ui()
        iconize_children(self)
        self.upload_progress.connect(self._on_upload_progress)
        self.upload_finished.connect(self._on_upload_finished)
        self.download_progress.connect(self._on_download_progress)
        self.download_finished.connect(self._on_download_finished)
        self.file_open_requested.connect(self._open_remote_file)
        self._remote_result.connect(self._on_remote_result)
        self._mutation_progress.connect(self._on_mutation_progress)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)

        # Path bar
        path_bar = QHBoxLayout()
        path_bar.setSpacing(4)

        _btn_ss = """
            QPushButton {
                font-size: 14px; padding: 0px;
                font-family: 'Segoe UI Symbol', 'Segoe UI', sans-serif;
                background: rgba(51,65,85,0.5);
                border: 1px solid rgba(255,255,255,0.1); border-radius: 6px; color: white;
            }
            QPushButton:hover { background: rgba(71,85,105,0.8); border-color: palette(highlight); }
        """

        self.btn_up = QPushButton("↑")
        self.btn_up.setFixedSize(28, 28)
        self.btn_up.setToolTip("Go to parent directory")
        self.btn_up.setStyleSheet(_btn_ss)
        self.btn_up.clicked.connect(self._go_up)
        path_bar.addWidget(self.btn_up)

        self.btn_home = QPushButton("⌂")
        self.btn_home.setFixedSize(28, 28)
        self.btn_home.setToolTip("Go to home directory")
        self.btn_home.setStyleSheet(_btn_ss)
        self.btn_home.clicked.connect(self._go_home)
        path_bar.addWidget(self.btn_home)

        self.btn_refresh = QPushButton("↺")
        self.btn_refresh.setFixedSize(28, 28)
        self.btn_refresh.setToolTip("Refresh current directory")
        self.btn_refresh.setStyleSheet(_btn_ss)
        self.btn_refresh.clicked.connect(self._refresh)
        path_bar.addWidget(self.btn_refresh)

        self.btn_upload = QPushButton("Upload")
        self.btn_upload.setFixedSize(64, 28)
        self.btn_upload.setToolTip("Upload files or folders to this directory")
        self.btn_upload.setStyleSheet(_btn_ss)
        self.btn_upload.clicked.connect(self._upload_files)
        path_bar.addWidget(self.btn_upload)

        self.btn_new_folder = QPushButton("＋📁")
        self.btn_new_folder.setFixedSize(36, 28)
        self.btn_new_folder.setToolTip("Create a new folder here")
        self.btn_new_folder.setStyleSheet(_btn_ss)
        self.btn_new_folder.clicked.connect(self._create_folder)
        path_bar.addWidget(self.btn_new_folder)

        self.btn_new_file = QPushButton("＋📄")
        self.btn_new_file.setFixedSize(36, 28)
        self.btn_new_file.setToolTip("Create a new empty file here")
        self.btn_new_file.setStyleSheet(_btn_ss)
        self.btn_new_file.clicked.connect(self._create_file)
        path_bar.addWidget(self.btn_new_file)

        self.btn_delete = QPushButton("🗑")
        self.btn_delete.setFixedSize(30, 28)
        self.btn_delete.setToolTip("Delete the selected files and folders (Del)")
        self.btn_delete.setStyleSheet(_btn_ss)
        self.btn_delete.clicked.connect(self._delete_selection)
        path_bar.addWidget(self.btn_delete)

        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("/home/user")
        self.path_edit.setFixedHeight(28)
        self.path_edit.setStyleSheet("""
            QLineEdit {
                background: rgba(30,30,46,0.8); border: 1px solid rgba(255,255,255,0.12);
                border-radius: 6px; padding: 0 8px; color: #cdd6f4; font-size: 12px;
                font-family: 'MesloLGL Nerd Font Mono', 'Consolas', monospace;
            }
            QLineEdit:focus { border-color: palette(highlight); }
        """)
        self.path_edit.returnPressed.connect(self._navigate_to_path)
        path_bar.addWidget(self.path_edit)
        layout.addLayout(path_bar)

        # File tree
        self.tree = _LibraryTree()
        self.tree.setHeaderLabels(["Name", "Size", "Permissions"])
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(20)
        self.tree.setAnimated(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)

        # Drag files out, and accept files/folders dragged in from the desktop.
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.external_drop.connect(self._on_external_drop)

        # Column sizing
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        self.tree.set_base_style("""
            QTreeWidget {
                border: none; background: transparent; outline: 0;
                font-family: 'MesloLGL Nerd Font Mono', 'Consolas', monospace;
                font-size: 11px;
            }
            QTreeWidget::item {
                padding: 4px 4px; border-bottom: 1px solid rgba(255,255,255,0.03);
            }
            QTreeWidget::item:selected {
                background: rgba(124,111,247,0.2); color: white;
            }
            QTreeWidget::item:hover {
                background: rgba(255,255,255,0.05);
            }
            QHeaderView::section {
                background: rgba(30,30,46,0.6); color: #a6adc8;
                border: none; border-bottom: 1px solid rgba(255,255,255,0.08);
                padding: 4px 8px; font-size: 11px; font-weight: 600;
            }
        """)
        self.tree.setToolTip(
            "Drag files and folders here from your desktop to upload them.\n"
            "Drop onto a folder row to upload into that folder."
        )
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        layout.addWidget(self.tree, 1)

        QShortcut(QKeySequence("Delete"), self.tree,
                  activated=self._delete_selection,
                  context=Qt.ShortcutContext.WidgetShortcut)
        QShortcut(QKeySequence("F2"), self.tree,
                  activated=self._rename_selection,
                  context=Qt.ShortcutContext.WidgetShortcut)
        QShortcut(QKeySequence("F5"), self.tree,
                  activated=self._refresh,
                  context=Qt.ShortcutContext.WidgetShortcut)

        # Transfer progress: hidden until a download starts.
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, self.PROGRESS_SCALE)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                background: rgba(255,255,255,0.06); border: none; border-radius: 2px;
            }
            QProgressBar::chunk { background: palette(highlight); border-radius: 2px; }
        """)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)

        # Status bar
        self.status_label = QLabel("Not connected — open an SSH session to browse files")
        self.status_label.setStyleSheet("color: #6b6e85; font-size: 11px; padding: 4px 0;")
        layout.addWidget(self.status_label)

    # ── Public API ──────────────────────────────────────────────────

    def set_ssh_client(self, ssh_client, connection_info=None):
        """Connect to a remote host's SFTP subsystem."""
        if ssh_client is self._ssh_client and (self._sftp or self._loading):
            self._connection_info = dict(connection_info or {})
            return
        self.disconnect_sftp()
        self._ssh_client = ssh_client
        self._connection_info = dict(connection_info or {})
        if ssh_client is None:
            return

        self._loading = True
        self.status_label.setText("Connecting to remote files…")
        def connect():
            sftp = ssh_client.open_sftp()
            try:
                self._configure_timeout(sftp)
                try:
                    home = sftp.normalize(".")
                except Exception:
                    home = "/"
                return sftp, home
            except Exception:
                sftp.close()
                raise
        self._work_remote("connect", connect)

    def disconnect_sftp(self):
        """Clean up SFTP connection."""
        self._generation += 1
        if self._sftp is not None:
            self._close_channel(self._sftp, self._browse_lock)
        self._sftp = None
        self._browse_lock = threading.Lock()
        self._ssh_client = None
        self._loading = False
        self._pending_directory = None
        self._requested_path = self._current_path
        self._home_path = "/"
        self._set_mutation_busy(False)
        self.tree.clear()
        self.path_edit.clear()
        self.status_label.setText("Not connected — open an SSH session to browse files")

    # ── Navigation ──────────────────────────────────────────────────

    @staticmethod
    def _close_channel(sftp, lock=None):
        def close():
            try:
                if lock is None:
                    sftp.close()
                else:
                    with lock:
                        sftp.close()
            except Exception:
                pass
        threading.Thread(target=close, daemon=True).start()

    @classmethod
    def _configure_timeout(cls, sftp):
        sftp.get_channel().settimeout(cls.REQUEST_TIMEOUT)

    def _work_remote(self, operation, fn):
        generation = self._generation
        def worker():
            value, error = None, None
            try:
                value = fn()
            except Exception as exc:
                error = str(exc) or type(exc).__name__
            try:
                self._remote_result.emit(generation, operation, value, error)
            except RuntimeError:
                if operation == "connect" and value is not None:
                    self._close_channel(value[0])
        threading.Thread(target=worker, daemon=True).start()

    def _on_remote_result(self, generation, operation, value, error):
        if generation != self._generation:
            if operation == "connect" and value is not None:
                self._close_channel(value[0])
            return
        if operation in ("connect", "list"):
            self._loading = False
        if operation == "connect":
            if error is not None:
                self.status_label.setText(f"SFTP error: {error}")
                return
            self._sftp, self._home_path = value
            self._load_directory(self._home_path)
        elif operation == "list":
            pending, self._pending_directory = self._pending_directory, None
            if pending is not None:
                self._load_directory(*pending)
                return
            if error is not None:
                self.path_edit.setText(self._current_path)
                self._requested_path = self._current_path
                self.status_label.setText(f"Error: {error}")
                return
            path, entries, message = value
            self._show_directory(path, entries)
            if message:
                self.status_label.setText(message)
        elif operation == "mutation":
            self._set_mutation_busy(False)
            message = f"Failed: {error}" if error is not None else value
            self.status_label.setText(message)
            self._load_directory(self._requested_path, message)

    def _load_directory(self, path: str, message=None):
        """Fetch directories on a worker; only paint the results on the GUI thread."""
        if not self._sftp:
            return
        self._requested_path = path
        if self._loading:
            self._pending_directory = (path, message)
            return

        self._loading = True
        self.status_label.setText(f"Loading {path}...")
        sftp, lock = self._sftp, self._browse_lock
        def load():
            with lock:
                return path, sftp.listdir_attr(path), message
        self._work_remote("list", load)

    def _show_directory(self, path, entries):
        self.tree.setUpdatesEnabled(False)
        sorting = self.tree.isSortingEnabled()
        self.tree.setSortingEnabled(False)
        try:
            self.tree.clear()
            self._current_path = path
            self.path_edit.setText(path)

            # Add parent directory entry
            if path != "/":
                parent_item = tree_item(["..", "", ""], "folder", FOLDER_COLOR)
                parent_item.setData(0, Qt.ItemDataRole.UserRole, {"name": "..", "is_dir": True})
                parent_item.setForeground(0, QColor("#fbbf24"))
                font = QFont("MesloLGL Nerd Font Mono", 10, QFont.Weight.Bold)
                parent_item.setFont(0, font)
                self.tree.addTopLevelItem(parent_item)

            # Sort: directories first, then files, alphabetical
            dirs = []
            files = []
            for entry in entries:
                is_dir = stat.S_ISDIR(entry.st_mode) if entry.st_mode else False
                if is_dir:
                    dirs.append(entry)
                else:
                    files.append(entry)

            dirs.sort(key=lambda e: e.filename.lower())
            files.sort(key=lambda e: e.filename.lower())

            for entry in dirs:
                self._add_entry_item(entry, is_dir=True)

            for entry in files:
                self._add_entry_item(entry, is_dir=False)

            self.status_label.setText(
                f"{path}  —  {len(dirs)} folder{'s' if len(dirs) != 1 else ''}, "
                f"{len(files)} file{'s' if len(files) != 1 else ''}"
            )

        finally:
            self.tree.setSortingEnabled(sorting)
            self.tree.setUpdatesEnabled(True)

    def _add_entry_item(self, entry, is_dir: bool):
        """Add a single file/folder entry to the tree."""
        name = entry.filename

        # Skip hidden files starting with . (but we'll show them)
        display_name = name

        size_str = "" if is_dir else _human_size(entry.st_size or 0)

        # Permissions
        perms = ""
        if entry.st_mode:
            perms = stat.filemode(entry.st_mode)

        item = tree_item([display_name, size_str, perms],
                         "folder" if is_dir else file_icon_name(name),
                         FOLDER_COLOR if is_dir else None)
        item.setData(0, Qt.ItemDataRole.UserRole, {
            "name": name,
            "is_dir": is_dir,
            "size": entry.st_size or 0,
            "path": self._join(self._current_path, name)
        })

        if is_dir:
            item.setForeground(0, QColor("#fbbf24"))  # Gold for folders
            item.setFont(0, QFont("MesloLGL Nerd Font Mono", 10, QFont.Weight.Bold))
        else:
            item.setForeground(0, QColor("#e8e9f0"))
            item.setFont(0, QFont("MesloLGL Nerd Font Mono", 10))

        item.setForeground(1, QColor("#94a3b8"))
        item.setForeground(2, QColor("#6b7280"))

        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsDragEnabled)

        self.tree.addTopLevelItem(item)

    def _on_double_click(self, item, column):
        """Navigate into directories, open files."""
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return

        if data.get("is_dir"):
            name = data["name"]
            if name == "..":
                self._go_up()
            else:
                new_path = self._join(self._current_path, name)
                self._load_directory(new_path)
        else:
            # File double-clicked — emit signal
            self.file_open_requested.emit(data.get("path", ""))

    def _go_up(self):
        """Navigate to parent directory."""
        if self._current_path == "/":
            return
        parent = os.path.dirname(self._current_path)
        if not parent:
            parent = "/"
        self._load_directory(parent)

    def _go_home(self):
        """Navigate to the user's home directory."""
        self._load_directory(self._home_path)

    def _refresh(self):
        """Reload the current directory."""
        if self._sftp:
            self._load_directory(self._current_path)

    def _navigate_to_path(self):
        """Navigate to the path typed in the path bar."""
        path = self.path_edit.text().strip()
        if path and self._sftp:
            self._load_directory(path)

    # ── Context Menu ────────────────────────────────────────────────

    def _selection(self) -> list:
        """Selected entries, excluding the '..' row."""
        items = self.tree.selectedItems()
        if not items:
            current = self.tree.currentItem()
            items = [current] if current else []
        out = []
        for item in items:
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if data and data.get("name") != "..":
                out.append(data)
        return out

    def _target_dir(self) -> str:
        """Directory new items land in: the selected folder, else the cwd."""
        sel = self._selection()
        if len(sel) == 1 and sel[0].get("is_dir"):
            return sel[0].get("path") or self._current_path
        return self._current_path

    def _context_menu(self, pos):
        """Show right-click context menu."""
        item = self.tree.itemAt(pos)
        if item is not None and not item.isSelected():
            self.tree.setCurrentItem(item)
        sel = self._selection()

        menu = QMenu(self)
        if sel:
            if len(sel) == 1 and not sel[0].get("is_dir"):
                menu.addAction(QAction("Edit remote file…", self, triggered=self._remote_edit_selected))
            label = "⬇️ Download" if len(sel) == 1 else f"⬇️ Download {len(sel)} items"
            act = QAction(label, self)
            act.triggered.connect(self._download_selection)
            menu.addAction(act)
            menu.addSeparator()

        upload_act = QAction("⬆️ Upload Files or Folders…", self)
        upload_act.setToolTip(
            "Pick any mix of files and folders — folders upload with everything "
            "inside them"
        )
        upload_act.triggered.connect(self._upload_files)
        menu.addAction(upload_act)

        menu.addSeparator()
        mkdir_act = QAction("📁 New Folder", self)
        mkdir_act.triggered.connect(self._create_folder)
        menu.addAction(mkdir_act)

        mkfile_act = QAction("📄 New File", self)
        mkfile_act.triggered.connect(self._create_file)
        menu.addAction(mkfile_act)

        if sel:
            menu.addSeparator()
            rename_act = QAction("✏️ Rename", self)
            rename_act.setEnabled(len(sel) == 1)
            rename_act.triggered.connect(self._rename_selection)
            menu.addAction(rename_act)

            del_label = "🗑️ Delete" if len(sel) == 1 else f"🗑️ Delete {len(sel)} items"
            del_act = QAction(del_label, self)
            del_act.triggered.connect(self._delete_selection)
            menu.addAction(del_act)

            chmod_act = QAction("🔒 Permissions…", self)
            chmod_act.triggered.connect(self._chmod_selection)
            menu.addAction(chmod_act)

        menu.addSeparator()
        compare = menu.addAction("Compare with local folder…")
        compare.setEnabled(self._ssh_client is not None)
        compare.triggered.connect(self._compare_folders)
        refresh_act = QAction("🔄 Refresh", self)
        refresh_act.triggered.connect(self._refresh)
        menu.addAction(refresh_act)

        iconize_menu(menu).exec(self.tree.viewport().mapToGlobal(pos))

    def _download_file(self, data: dict):
        """Download a remote file to the local machine."""
        if not self._sftp:
            return
        remote_path = data.get("path", "")
        name = data.get("name", "file")

        local_path, _ = QFileDialog.getSaveFileName(self, "Save File As", name)
        if not local_path:
            return
        self._start_download([(remote_path, local_path, False)])

    def _upload_file(self):
        """Compatibility wrapper for older signal connections."""
        self._upload_files()

    def _upload_files(self):
        """Upload any mix of local files and folders into the current directory."""
        if not self._require_connection():
            return
        paths = pick_files_and_folders(
            self, "Select files and folders to upload", str(Path.home())
        )
        if paths:
            self._start_upload(paths, self._target_dir())

    def _upload_folder(self):
        """Kept for older call sites — the picker already takes folders."""
        self._upload_files()

    def _on_external_drop(self, paths: list, target_dir):
        """Files and folders dragged in from the desktop."""
        if not self._require_connection():
            return
        self._start_upload(paths, target_dir or self._current_path)

    def _require_connection(self) -> bool:
        if self._sftp and self._ssh_client:
            return True
        self.status_label.setText("Not connected — open an SSH session first")
        return False

    # ── Upload engine ───────────────────────────────────────────────

    def _start_upload(self, local_paths: list, remote_dir: str):
        """Upload files and folders in the background, recursing into folders."""
        local_paths = [p for p in local_paths if p and os.path.exists(p)]
        if not local_paths:
            return
        if self._upload_busy:
            self.status_label.setText("An upload is already running…")
            return

        self._upload_busy = True
        self.status_label.setText("Preparing upload…")
        threading.Thread(
            target=self._run_upload,
            args=(local_paths, remote_dir or self._current_path),
            daemon=True,
        ).start()

    def _run_upload(self, local_paths: list, remote_dir: str):
        """Thread entry point. Uses its own SFTP channel so the tree stays live.

        paramiko's SFTPClient is not thread-safe and the panel keeps using
        ``self._sftp`` on the GUI thread while this runs, so the worker opens a
        channel of its own rather than sharing one.
        """
        errors: list[str] = []
        state = {"done": 0}
        total = 0
        sftp = None
        try:
            total = count_files(local_paths)
            sftp = self._ssh_client.open_sftp()
            for path in local_paths:
                name = os.path.basename(path.rstrip("/\\")) or path
                target = str(PurePosixPath(remote_dir) / name)
                try:
                    if os.path.isdir(path):
                        self._upload_tree(sftp, path, target, state, total, errors)
                    else:
                        self._upload_one(sftp, path, target, name, state, total)
                except Exception as e:
                    errors.append(f"{name}: {e}")
        except Exception as e:
            errors.append(str(e))
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass

        self.upload_finished.emit(state["done"], total, errors)

    def _upload_one(self, sftp, local_path, remote_path, label, state, total):
        self.upload_progress.emit(
            f"Uploading {state['done'] + 1}/{total}: {label}"
        )
        sftp.put(local_path, remote_path)
        state["done"] += 1

    def _upload_tree(self, sftp, local_dir, remote_dir, state, total, errors):
        """Recreate a local directory tree on the remote host."""
        try:
            sftp.mkdir(remote_dir)
        except IOError:
            pass  # already exists

        try:
            entries = sorted(os.listdir(local_dir))
        except OSError as e:
            errors.append(f"{os.path.basename(local_dir)}: {e}")
            return

        for entry in entries:
            local_path = os.path.join(local_dir, entry)
            remote_path = str(PurePosixPath(remote_dir) / entry)
            try:
                if os.path.islink(local_path):
                    # Don't follow links: a self-referential one would recurse
                    # for ever, and the target may sit outside the tree.
                    continue
                if os.path.isdir(local_path):
                    self._upload_tree(sftp, local_path, remote_path, state, total, errors)
                else:
                    self._upload_one(sftp, local_path, remote_path, entry, state, total)
            except Exception as e:
                errors.append(f"{entry}: {e}")

    def _on_upload_progress(self, text: str):
        self.status_label.setText(text)

    def _on_upload_finished(self, uploaded: int, total: int, errors: list):
        self._upload_busy = False
        if errors:
            self.status_label.setText(
                f"Uploaded {uploaded}/{total} — {len(errors)} failed: {errors[0]}"
            )
        elif uploaded:
            self.status_label.setText(
                f"Uploaded {uploaded} file{'s' if uploaded != 1 else ''}"
            )
        else:
            self.status_label.setText("Nothing to upload")
        self._load_directory(self._requested_path, self.status_label.text())

    def _create_folder(self):
        """Create a new folder in the current directory."""
        if not self._can_mutate():
            return

        generation, target = self._generation, self._target_dir()
        name = ask_name(self, "New Folder", "Folder name:", "New folder")
        if not name or generation != self._generation:
            return

        remote_path = self._join(target, name)
        def create(sftp, check, progress):
            check()
            sftp.mkdir(remote_path)
            return f"Created folder: {name}"
        self._start_mutation(f"Creating folder: {name}…", create)

    def _create_file(self):
        """Create a new empty file in the current directory."""
        if not self._can_mutate():
            return

        generation, target = self._generation, self._target_dir()
        name = ask_name(self, "New File", "File name:", "new_file.txt")
        if not name or generation != self._generation:
            return

        remote_path = self._join(target, name)
        def create(sftp, check, progress):
            check()
            # Paramiko requires 'w' for WRITE as well as 'x' for EXCL.
            # EXCL prevents an existing file from being truncated.
            with sftp.open(remote_path, "wx"):
                pass
            return f"Created file: {name}"
        self._start_mutation(f"Creating file: {name}…", create)

    @staticmethod
    def _exists(sftp, path: str) -> bool:
        try:
            sftp.lstat(path)
            return True
        except FileNotFoundError:
            return False

    def _can_mutate(self):
        if not self._require_connection():
            return False
        if self._mutation_busy:
            self.status_label.setText("A file operation is already running…")
            return False
        return True

    def _set_mutation_busy(self, busy):
        self._mutation_busy = busy
        for button in (self.btn_new_folder, self.btn_new_file, self.btn_delete):
            button.setEnabled(not busy)

    @contextmanager
    def _mutation_session(self, client):
        sftp = client.open_sftp()
        try:
            self._configure_timeout(sftp)
            yield sftp
        finally:
            try:
                sftp.close()
            except Exception:
                pass

    def _start_mutation(self, label, fn):
        if not self._can_mutate():
            return
        client, generation = self._ssh_client, self._generation
        self._set_mutation_busy(True)
        self.status_label.setText(label)
        last_progress = 0.0
        def check():
            if generation != self._generation or not self._client_alive(client):
                raise OSError("Cancelled: original connection closed or changed")
        def progress(text):
            nonlocal last_progress
            now = time.monotonic()
            if now - last_progress >= self.PROGRESS_INTERVAL:
                last_progress = now
                self._mutation_progress.emit(generation, text)
        def run():
            check()
            with self._mutation_session(client) as sftp:
                check()
                return fn(sftp, check, progress)
        self._work_remote("mutation", run)

    def _on_mutation_progress(self, generation, text):
        if generation == self._generation and self._mutation_busy:
            self.status_label.setText(text)

    @staticmethod
    def _join(directory: str, name: str) -> str:
        return str(PurePosixPath(directory or "/") / name)

    def _rename_selection(self):
        """Rename the single selected remote file or folder."""
        if not self._sftp:
            return
        sel = self._selection()
        if len(sel) != 1:
            self.status_label.setText("Select a single item to rename")
            return
        self._rename_item(sel[0])

    def _rename_item(self, data: dict):
        """Rename a remote file or folder."""
        if not self._can_mutate():
            return

        generation = self._generation
        old_name = data.get("name", "")
        old_path = data.get("path", "")
        new_name = ask_name(self, "Rename", "New name:", old_name)
        if not new_name or new_name == old_name or generation != self._generation:
            return

        new_path = self._join(str(PurePosixPath(old_path).parent), new_name)
        def rename(sftp, check, progress):
            check()
            if self._exists(sftp, new_path):
                return f"Already exists: {new_name}"
            check()
            sftp.rename(old_path, new_path)
            return f"Renamed: {old_name} → {new_name}"
        self._start_mutation(f"Renaming: {old_name}…", rename)

    def _delete_selection(self):
        """Delete every selected remote entry, recursing into folders."""
        if not self._can_mutate():
            return
        sel = self._selection()
        self._delete_entries(sel)

    def _delete_entries(self, sel):
        if not sel:
            return
        generation = self._generation
        if not confirm_delete(self, [d.get("name", "") for d in sel], "the remote host"):
            return
        if generation != self._generation:
            return

        def delete(sftp, check, progress):
            failed, done = [], 0
            for data in sel:
                check()
                try:
                    self._rmtree(sftp, data.get("path", ""), check, progress)
                    done += 1
                except Exception as exc:
                    check()
                    failed.append(f"{data.get('name', '?')}: {exc}")
            if failed:
                return f"Deleted {done}/{len(sel)} item(s) — {len(failed)} failed: {failed[0]}"
            return f"Deleted {done} item(s)"
        self._start_mutation(f"Deleting {len(sel)} item(s)…", delete)

    def _rmtree(self, sftp, path: str, check=lambda: None, progress=lambda text: None):
        """Remove bottom-up without recursion or following directory symlinks."""
        if not path or path in ("/", ".", ".."):
            raise ValueError("Select a file or folder to delete")
        stack = [(path, False)]
        while stack:
            check()
            current, visited = stack.pop()
            progress(f"Deleting: {current}")
            mode = sftp.lstat(current).st_mode or 0
            if not stat.S_ISDIR(mode):
                sftp.remove(current)
            elif visited:
                sftp.rmdir(current)
            else:
                entries = sftp.listdir_attr(current)
                stack.append((current, True))
                for entry in reversed(entries):
                    name = entry.filename
                    if not name or name in (".", "..") or "/" in name:
                        raise ValueError("The server returned an invalid directory entry")
                    stack.append((self._join(current, name), False))

    def _delete_item(self, data: dict):
        """Delete a single remote entry (kept for older call sites)."""
        if not data or not self._can_mutate():
            return
        self._delete_entries([data])

    def _chmod_selection(self):
        """Set octal permissions on the selected remote entries."""
        if not self._can_mutate():
            return
        sel = self._selection()
        if not sel:
            return
        generation = self._generation
        text, ok = QInputDialog.getText(
            self, "Permissions",
            f"Octal mode for {len(sel)} item(s) — e.g. 644, 755:",
            text="644",
        )
        if not ok or generation != self._generation:
            return
        try:
            mode = int(text.strip(), 8)
        except ValueError:
            self.status_label.setText(f"'{text.strip()}' is not an octal mode")
            return
        if not 0 <= mode <= 0o7777:
            self.status_label.setText("Mode must be between 000 and 7777")
            return

        def chmod(sftp, check, progress):
            failed = []
            for data in sel:
                check()
                try:
                    sftp.chmod(data.get("path", ""), mode)
                except Exception as exc:
                    failed.append(f"{data.get('name', '?')}: {exc}")
            if failed:
                return f"{len(failed)} failed — {failed[0]}"
            return "Set mode %o on %d item(s)" % (mode, len(sel))
        self._start_mutation("Setting permissions…", chmod)

    def _download_selection(self):
        """Download the selected entries; folders come down recursively."""
        if not self._sftp:
            return
        sel = self._selection()
        if not sel:
            return

        if len(sel) == 1 and not sel[0].get("is_dir"):
            self._download_file(sel[0])
            return

        dest = QFileDialog.getExistingDirectory(self, "Download Into Folder")
        if not dest:
            return

        self._start_download([
            (
                data.get("path", ""),
                os.path.join(dest, data.get("name", "file")),
                bool(data.get("is_dir")),
            )
            for data in sel
        ])

    # ── Download engine ─────────────────────────────────────────────

    def _start_download(self, jobs: list):
        """Download in the background. `jobs` is (remote, local, is_dir) tuples."""
        if not jobs:
            return
        if not self._require_connection():
            return
        if self._download_busy:
            self.status_label.setText("A download is already running…")
            return

        self._download_busy = True
        self._transfer_cancelled = False
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.status_label.setText("Preparing download…")
        threading.Thread(
            target=self._run_download, args=(list(jobs),), daemon=True
        ).start()

    def _run_download(self, jobs: list):
        """Thread entry point. Uses its own SFTP channel so the tree stays live.

        The GUI thread keeps using ``self._sftp`` while this runs, and
        paramiko's SFTPClient is not thread-safe, so the worker opens a channel
        of its own — the same arrangement as the uploader.

        The whole tree is walked first so the status line can say how many
        files are left, and the bar can track bytes rather than file count:
        one ISO next to a hundred config files would otherwise sit at 99%.
        """
        errors: list[str] = []
        state = {"done": 0, "bytes_done": 0, "bytes_total": 0, "last_emit": 0.0}
        files: list = []
        sftp = None
        client = self._ssh_client
        try:
            sftp = client.open_sftp()
            self.download_progress.emit("Counting files…", -1)
            for remote, local, is_dir in jobs:
                try:
                    if is_dir:
                        self._collect_remote_tree(sftp, remote, local, files, errors)
                    else:
                        files.append((remote, local, self._remote_size(sftp, remote)))
                except Exception as e:
                    errors.append(f"{PurePosixPath(remote).name}: {e}")
            state["bytes_total"] = sum(size for _r, _l, size in files)

            for remote, local, size in files:
                # Switching tabs swaps the panel's own channel but leaves this
                # one alone; only a dead transport (tab closed, link dropped)
                # or an explicit cancel stops the batch, and it stops between
                # files so nothing is left half-written.
                if self._transfer_cancelled or not self._client_alive(client):
                    errors.append(self.CANCELLED)
                    break
                try:
                    self._download_one(sftp, remote, local, size, state, len(files))
                except Exception as e:
                    errors.append(f"{PurePosixPath(remote).name}: {e}")
                    # Keep the bar honest even for files that never arrived.
                    state["bytes_done"] += size
        except Exception as e:
            errors.append(str(e))
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:
                    pass

        self.download_finished.emit(state["done"], len(files), errors)

    @staticmethod
    def _client_alive(client) -> bool:
        """True unless the SSH transport under `client` is known to be gone."""
        try:
            transport = client.get_transport()
        except Exception:
            return True  # a stand-in without transports: assume alive
        if transport is None:
            return False
        try:
            return bool(transport.is_active())
        except Exception:
            return False

    @staticmethod
    def _remote_size(sftp, remote_path: str) -> int:
        try:
            return int(sftp.stat(remote_path).st_size or 0)
        except Exception:
            return 0

    def _collect_remote_tree(self, sftp, remote_dir, local_dir, files, errors):
        """List every file under remote_dir, creating local folders as it goes.

        Folders are made here (not while downloading) so an empty remote
        directory still appears on the local side.
        """
        os.makedirs(local_dir, exist_ok=True)
        try:
            entries = sftp.listdir_attr(remote_dir)
        except Exception as e:
            errors.append(f"{PurePosixPath(remote_dir).name}: {e}")
            return
        for entry in entries:
            remote_path = self._join(remote_dir, entry.filename)
            local_path = os.path.join(local_dir, entry.filename)
            if stat.S_ISDIR(entry.st_mode or 0):
                self._collect_remote_tree(sftp, remote_path, local_path, files, errors)
            elif stat.S_ISLNK(entry.st_mode or 0):
                # A link's target may sit outside the tree or point back into
                # it; the listing size is the link's, not the file's. Skip it.
                continue
            else:
                files.append((remote_path, local_path, int(entry.st_size or 0)))

    def _download_one(self, sftp, remote, local, size, state, total):
        label = PurePosixPath(remote).name
        index = state["done"] + 1
        self._emit_download_progress(state, total, index, label, 0, size, force=True)

        def on_chunk(transferred, _reported_total):
            self._emit_download_progress(
                state, total, index, label, transferred, size
            )

        os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
        sftp.get(remote, local, callback=on_chunk)
        # The last chunk usually lands inside the throttle window; say 100%
        # anyway so a file never looks stuck at 97% while the next one starts.
        self._emit_download_progress(state, total, index, label, size, size, force=True)
        state["done"] += 1
        state["bytes_done"] += size

    def _emit_download_progress(self, state, total, index, label,
                                transferred, size, force=False):
        now = time.monotonic()
        if not force and now - state["last_emit"] < self.PROGRESS_INTERVAL:
            return
        state["last_emit"] = now
        overall = state["bytes_done"] + min(transferred, size)
        self.download_progress.emit(
            self._download_status(index, total, label, transferred, size),
            self._permille(overall, state["bytes_total"]),
        )

    @staticmethod
    def _download_status(index: int, total: int, label: str,
                         transferred: int, size: int) -> str:
        """One line the technician can read at a glance: where in the batch,
        how many are still to come, and how far the current file is."""
        left = total - index
        text = f"Downloading {index}/{total}"
        if left:
            text += f" ({left} left)"
        text += f": {label}"
        if size > 0:
            pct = min(100, int(transferred * 100 / size))
            text += f" — {pct}% of {_human_size(size)}"
        return text

    @classmethod
    def _permille(cls, done: int, total: int) -> int:
        if total <= 0:
            return 0
        return max(0, min(cls.PROGRESS_SCALE, int(done * cls.PROGRESS_SCALE / total)))

    def _on_download_progress(self, text: str, permille: int):
        self.status_label.setText(text)
        if permille < 0:
            # Still counting: a busy bar says "working" without lying about how far.
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, self.PROGRESS_SCALE)
            self.progress_bar.setValue(permille)

    def _on_download_finished(self, downloaded: int, total: int, errors: list):
        self._download_busy = False
        self.progress_bar.setRange(0, self.PROGRESS_SCALE)
        self.progress_bar.hide()
        if errors == [self.CANCELLED]:
            self.status_label.setText(
                f"Download stopped after {downloaded}/{total} — connection closed"
            )
        elif errors:
            self.status_label.setText(
                f"Downloaded {downloaded}/{total} — {len(errors)} failed: {errors[0]}"
            )
        elif downloaded:
            self.status_label.setText(
                f"Downloaded {downloaded} file{'s' if downloaded != 1 else ''}"
            )
        else:
            self.status_label.setText("Nothing to download")
