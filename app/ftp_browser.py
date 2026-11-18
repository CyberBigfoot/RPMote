"""
RPMote FTP Browser — Dual-pane file browser over FTP/FTPS.

An FTP session has a single control connection, so every command here is
serialised behind ``_ftp_lock`` and issued from a worker thread. The GUI thread
never touches ``self.ftp`` directly; results come back over signals.
"""
import os
import ftplib
import re
import threading
from datetime import datetime
from pathlib import Path, PurePosixPath
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QLabel, QPushButton, QLineEdit, QProgressBar, QSplitter,
    QMenu, QMessageBox, QHeaderView, QCheckBox, QAbstractItemView
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QAction, QFont, QShortcut, QKeySequence
from app.icons import FOLDER_COLOR, file_icon_name
from app.themes import accent_rgba
from app.widgets import iconize_children, iconize_menu, tree_item

from app.file_browser_ops import (
    ExternalDropTree, LocalFileOpsMixin, ask_name, confirm_delete, copy_into,
    fmt_time as _fmt_time, human_size as _human_size, pick_files_and_folders, RemoteEditingMixin,
)


# ── Remote listing parsers ──────────────────────────────────────────

_UNIX_LINE = re.compile(
    r"^([\-dlbcps])(\S{9})\s+\d+\s+\S+\s+\S+\s+(\d+)\s+"
    r"(\w{3}\s+\d+\s+(?:\d{4}|\d{2}:\d{2}))\s+(.+)$"
)
_DOS_LINE = re.compile(
    r"^(\d{2}-\d{2}-\d{2,4}\s+\d{2}:\d{2}(?:[AP]M)?)\s+(<DIR>|\d+)\s+(.+)$",
    re.IGNORECASE,
)


def _parse_list_line(line: str):
    """Parse one LIST line into (name, is_dir, size, modified) or None."""
    line = line.rstrip("\r\n")
    if not line:
        return None

    m = _UNIX_LINE.match(line)
    if m:
        kind, _perms, size, when, name = m.groups()
        # "link -> target": show the link, treat it as a file unless probed.
        if " -> " in name:
            name = name.split(" -> ", 1)[0]
        if name in (".", ".."):
            return None
        return name, kind == "d", int(size), when

    m = _DOS_LINE.match(line)
    if m:
        when, size, name = m.groups()
        if name in (".", ".."):
            return None
        is_dir = size.upper() == "<DIR>"
        return name, is_dir, 0 if is_dir else int(size), when

    return None


def _mlsd_time(value: str) -> str:
    try:
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


class FTPBrowser(RemoteEditingMixin, LocalFileOpsMixin, QWidget):
    transfer_progress = pyqtSignal(int)
    transfer_complete = pyqtSignal(str)
    transfer_error = pyqtSignal(str)
    session_closed = pyqtSignal()
    connection_lost = pyqtSignal(str)
    connection_established = pyqtSignal()
    listing_ready = pyqtSignal(str, list)   # path, entries
    status_message = pyqtSignal(str)

    def __init__(self, connection_info, parent=None):
        super().__init__(parent)
        self.connection_info = connection_info
        self.ftp = None
        self._connected = False
        self._remote_cwd = "/"
        self._local_cwd = str(Path.home())
        self._ftp_lock = threading.RLock()
        self._setup_ui()
        iconize_children(self)
        self._connect_signals()
        QTimer.singleShot(0, self._refresh_local)

    # ── UI ───────────────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        self.header = QWidget()
        self.header.setFixedHeight(36)
        self.header.setStyleSheet("background-color: transparent;")
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(12, 0, 12, 0)
        self.status_indicator = QLabel("●")
        self.status_indicator.setStyleSheet("color:#f87171;font-size:10px;")
        hl.addWidget(self.status_indicator)
        name = self.connection_info.get("name", "FTP")
        host = self.connection_info.get("host", "")
        self.title_label = QLabel(f"  {name} — {host}")
        self.title_label.setStyleSheet("font-weight:600;font-size:12px;")
        hl.addWidget(self.title_label)
        hl.addStretch()
        self.tls_check = QCheckBox("TLS")
        self.tls_check.setChecked(self.connection_info.get("use_tls", False))
        self.tls_check.setStyleSheet("font-size:11px;")
        hl.addWidget(self.tls_check)
        self.btn_compare = QPushButton("Compare folders…")
        self.btn_compare.setFixedHeight(26)
        self.btn_compare.setStyleSheet("font-size:11px;padding:2px 10px;")
        self.btn_compare.clicked.connect(self._compare_folders)
        hl.addWidget(self.btn_compare)
        self.btn_reconnect = QPushButton("↻ Reconnect")
        self.btn_reconnect.setFixedHeight(26)
        self.btn_reconnect.setStyleSheet("font-size:11px;padding:2px 10px;")
        self.btn_reconnect.clicked.connect(self.reconnect)
        hl.addWidget(self.btn_reconnect)
        self.btn_disconnect = QPushButton("✕ Disconnect")
        self.btn_disconnect.setFixedHeight(26)
        self.btn_disconnect.setStyleSheet(
            "font-size:11px;padding:2px 10px;background-color:#ef4444;"
        )
        self.btn_disconnect.clicked.connect(self.disconnect)
        hl.addWidget(self.btn_disconnect)
        layout.addWidget(self.header)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # ── Local pane ───────────────────────────────────────────
        lw = QWidget()
        ll = QVBoxLayout(lw)
        ll.setContentsMargins(8, 8, 4, 8)
        lh = QHBoxLayout()
        lh.addWidget(QLabel("Local"))
        self.local_path = QLineEdit(self._local_cwd)
        self.local_path.returnPressed.connect(self._nav_local)
        lh.addWidget(self.local_path)
        bu = QPushButton("↑")
        bu.setFixedSize(30, 30)
        bu.setToolTip("Parent folder")
        bu.clicked.connect(self._local_up)
        lh.addWidget(bu)
        ll.addLayout(lh)
        ll.addLayout(self._build_pane_toolbar([
            ("＋ Folder", "New folder", self._local_new_folder),
            ("＋ File", "New empty file", self._local_new_file),
            ("✎ Rename", "Rename selected item (F2)", self._local_rename),
            ("🗑 Delete", "Delete selected items (Del)", self._local_delete),
            ("↺", "Refresh", self._refresh_local),
            ("⌂", "Go to home folder", self._local_home),
        ]))
        self.local_tree = ExternalDropTree()
        self.local_tree.setToolTip(
            "Drag files and folders here from your desktop to copy them into "
            "this folder."
        )
        self.local_tree.external_drop.connect(self._on_local_external_drop)
        self.local_tree.setFont(QFont("MesloLGL Nerd Font Mono", 10))
        self.local_tree.setHeaderLabels(["Name", "Size", "Modified"])
        self.local_tree.setRootIsDecorated(False)
        self.local_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.local_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.local_tree.setColumnWidth(1, 100)
        self.local_tree.setColumnWidth(2, 140)
        self.local_tree.itemDoubleClicked.connect(self._local_dblclick)
        self.local_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.local_tree.customContextMenuRequested.connect(self._local_ctx)
        ll.addWidget(self.local_tree)
        splitter.addWidget(lw)

        # ── Transfer buttons ─────────────────────────────────────
        cw = QWidget()
        cw.setFixedWidth(60)
        cl = QVBoxLayout(cw)
        cl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.btn_upload = QPushButton("▶")
        self.btn_upload.setFixedSize(40, 40)
        self.btn_upload.setToolTip("Upload selection")
        self.btn_upload.setStyleSheet("font-size:18px;border-radius:20px;")
        self.btn_upload.clicked.connect(self._upload)
        cl.addWidget(self.btn_upload)
        self.btn_download = QPushButton("◀")
        self.btn_download.setFixedSize(40, 40)
        self.btn_download.setToolTip("Download selection")
        self.btn_download.setStyleSheet("font-size:18px;border-radius:20px;")
        self.btn_download.clicked.connect(self._download)
        cl.addWidget(self.btn_download)
        splitter.addWidget(cw)

        # ── Remote pane ──────────────────────────────────────────
        rw = QWidget()
        rl = QVBoxLayout(rw)
        rl.setContentsMargins(4, 8, 8, 8)
        rh = QHBoxLayout()
        rh.addWidget(QLabel("Remote"))
        self.remote_path = QLineEdit("/")
        self.remote_path.returnPressed.connect(self._nav_remote)
        rh.addWidget(self.remote_path)
        bru = QPushButton("↑")
        bru.setFixedSize(30, 30)
        bru.setToolTip("Parent directory")
        bru.clicked.connect(self._remote_up)
        rh.addWidget(bru)
        rl.addLayout(rh)
        rl.addLayout(self._build_pane_toolbar([
            ("⬆ Upload…", "Pick any mix of files and folders to upload",
             self._upload_from_picker),
            ("＋ Folder", "New remote folder", self._remote_new_folder),
            ("＋ File", "New empty remote file", self._remote_new_file),
            ("✎ Rename", "Rename selected item (F2)", self._remote_rename),
            ("🗑 Delete", "Delete selected items, folders included (Del)",
             self._remote_delete),
            ("↺", "Refresh", self._refresh_remote),
            ("⌂", "Go to root", self._remote_home),
        ]))
        self.remote_tree = ExternalDropTree()
        self.remote_tree.setToolTip(
            "Drag files and folders here from your desktop to upload them.\n"
            "Drop onto a folder row to upload into that folder."
        )
        self.remote_tree.external_drop.connect(self._on_remote_external_drop)
        self.remote_tree.setFont(QFont("MesloLGL Nerd Font Mono", 10))
        self.remote_tree.setHeaderLabels(["Name", "Size", "Modified"])
        self.remote_tree.setRootIsDecorated(False)
        self.remote_tree.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.remote_tree.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.remote_tree.setColumnWidth(1, 100)
        self.remote_tree.setColumnWidth(2, 140)
        self.remote_tree.itemDoubleClicked.connect(self._remote_dblclick)
        self.remote_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.remote_tree.customContextMenuRequested.connect(self._remote_ctx)
        rl.addWidget(self.remote_tree)
        splitter.addWidget(rw)

        splitter.setSizes([400, 60, 400])
        layout.addWidget(splitter, 1)

        # Progress
        pw = QWidget()
        pw.setFixedHeight(30)
        pl = QHBoxLayout(pw)
        pl.setContentsMargins(8, 2, 8, 2)
        self.progress_label = QLabel("Ready")
        self.progress_label.setStyleSheet("font-size:11px;color:#a0a3b8;")
        pl.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setVisible(False)
        pl.addWidget(self.progress_bar)
        layout.addWidget(pw)

        for tree, delete_slot, rename_slot in (
            (self.local_tree, self._local_delete, self._local_rename),
            (self.remote_tree, self._remote_delete, self._remote_rename),
        ):
            QShortcut(QKeySequence("Delete"), tree, activated=delete_slot,
                      context=Qt.ShortcutContext.WidgetShortcut)
            QShortcut(QKeySequence("F2"), tree, activated=rename_slot,
                      context=Qt.ShortcutContext.WidgetShortcut)

    def _build_pane_toolbar(self, actions):
        """Row of small action buttons shown above a file pane."""
        row = QHBoxLayout()
        row.setSpacing(4)
        row.setContentsMargins(0, 0, 0, 0)
        css = (
            "QPushButton { font-size:11px; padding:3px 8px; border-radius:5px;"
            " background:rgba(51,65,85,0.55); color:#e2e8f0;"
            " border:1px solid rgba(255,255,255,0.10); }"
            f"QPushButton:hover {{ background:{accent_rgba(0.45)}; border-color:{accent_rgba(1)}; }}"
        )
        for text, tip, slot in actions:
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.setFixedHeight(24)
            btn.setStyleSheet(css)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        row.addStretch()
        return row

    def _connect_signals(self):
        self.transfer_progress.connect(self._on_progress)
        self.transfer_complete.connect(self._on_complete)
        self.transfer_error.connect(self._on_error)
        self.connection_established.connect(self._on_connected)
        self.connection_lost.connect(self._on_lost)
        self.listing_ready.connect(self._on_listing_ready)
        self.status_message.connect(self._notify)

    def _notify(self, text: str):
        self.progress_label.setText(text)

    # ── Connection ───────────────────────────────────────────────

    def connect_ftp(self):
        threading.Thread(target=self._do_connect, daemon=True).start()

    def _do_connect(self):
        try:
            use_tls = self.tls_check.isChecked()
            ftp = ftplib.FTP_TLS() if use_tls else ftplib.FTP()
            host = self.connection_info["host"]
            port = int(self.connection_info.get("port", 21))
            ftp.connect(host, port, timeout=15)
            username = self.connection_info.get("username", "anonymous")
            password = self.connection_info.get("password", "")
            ftp.login(username, password)
            if use_tls:
                ftp.prot_p()
            ftp.encoding = "utf-8"
            self.ftp = ftp
            try:
                self._remote_cwd = ftp.pwd()
            except Exception:
                self._remote_cwd = "/"
            self._connected = True
            self.connection_established.emit()
        except Exception as e:
            self.ftp = None
            from app.net_errors import describe_failure
            info = self.connection_info
            _kind, reason = describe_failure(e, info.get("host", ""), info.get("port", 21),
                                             str(info.get("username", "") or ""))
            self.connection_lost.emit(f"FTP failed: {reason}")

    def _drop_connection(self, emit_close: bool = True):
        self._connected = False
        ftp, self.ftp = self.ftp, None
        if ftp is not None:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass
        self.status_indicator.setStyleSheet("color:#f87171;font-size:10px;")
        self.progress_label.setText("Disconnected")
        self.remote_tree.clear()
        if emit_close:
            self.session_closed.emit()

    def disconnect(self):
        self._drop_connection(emit_close=True)

    def reconnect(self):
        self._drop_connection(emit_close=False)
        self.progress_label.setText("Reconnecting...")
        self.connect_ftp()

    def _on_connected(self):
        self.status_indicator.setStyleSheet("color:#4ade80;font-size:10px;")
        self.progress_label.setText("Connected")
        self._refresh_local()
        self._refresh_remote()

    def _on_lost(self, msg):
        self.status_indicator.setStyleSheet("color:#f87171;font-size:10px;")
        self.progress_label.setText(msg)

    # ── Worker plumbing ──────────────────────────────────────────

    def _require_remote(self) -> bool:
        if self._connected and self.ftp is not None:
            return True
        self._notify("Not connected")
        return False

    def _run_remote(self, fn, done_msg: str = "", refresh: bool = True):
        """Run an FTP operation on a worker thread, serialised by the lock."""
        if not self._require_remote():
            return

        def worker():
            try:
                with self._ftp_lock:
                    fn(self.ftp)
            except Exception as e:
                self.transfer_error.emit(str(e))
                return
            if refresh:
                self.transfer_complete.emit(done_msg)
            elif done_msg:
                self.status_message.emit(done_msg)

        threading.Thread(target=worker, daemon=True).start()

    # ── Local pane ───────────────────────────────────────────────

    def _refresh_local(self):
        self.local_tree.clear()
        self.local_path.setText(self._local_cwd)
        self.local_path.setToolTip(self._local_cwd)
        self.local_path.setCursorPosition(len(self._local_cwd))
        try:
            parent_dir = str(Path(self._local_cwd).parent)
            if parent_dir != self._local_cwd:
                up = tree_item(["..", "", ""], "folder", FOLDER_COLOR)
                up.setData(0, Qt.ItemDataRole.UserRole,
                           {"name": "..", "is_dir": True, "path": parent_dir})
                up.setForeground(0, QColor("#fbbf24"))
                self.local_tree.addTopLevelItem(up)
            names = sorted(
                os.listdir(self._local_cwd),
                key=lambda x: (
                    not os.path.isdir(os.path.join(self._local_cwd, x)), x.lower()
                ),
            )
            for name in names:
                full = os.path.join(self._local_cwd, name)
                isd = os.path.isdir(full)
                try:
                    st = os.stat(full)
                    size = "" if isd else _human_size(st.st_size)
                    mtime = _fmt_time(st.st_mtime)
                except OSError:
                    size, mtime = "", "—"
                it = tree_item([name, size, mtime],
                               "folder" if isd else file_icon_name(name),
                               FOLDER_COLOR if isd else None)
                it.setData(0, Qt.ItemDataRole.UserRole,
                           {"name": name, "is_dir": isd, "path": full})
                self.local_tree.addTopLevelItem(it)
        except OSError as e:
            self._notify(f"Cannot list folder: {e}")

    def _nav_local(self):
        p = self.local_path.text().strip()
        if os.path.isdir(p):
            self._local_cwd = p
            self._refresh_local()
        else:
            self._notify("Folder not found")
            self.local_path.setText(self._local_cwd)

    def _local_up(self):
        par = str(Path(self._local_cwd).parent)
        if par != self._local_cwd:
            self._local_cwd = par
            self._refresh_local()

    def _local_dblclick(self, item, col):
        d = item.data(0, Qt.ItemDataRole.UserRole)
        if d and d["is_dir"]:
            self._local_cwd = d["path"]
            self._refresh_local()

    def _local_ctx(self, pos):
        item = self.local_tree.itemAt(pos)
        if item is not None and not item.isSelected():
            self.local_tree.setCurrentItem(item)
        sel = self._local_selection()

        menu = QMenu(self)
        if sel and self._connected:
            label = "▶ Upload" if len(sel) == 1 else f"▶ Upload {len(sel)} items"
            menu.addAction(QAction(label, self, triggered=self._upload))
            menu.addSeparator()
        menu.addAction(QAction("＋ New Folder", self, triggered=self._local_new_folder))
        menu.addAction(QAction("＋ New File", self, triggered=self._local_new_file))
        if sel:
            menu.addSeparator()
            rename = QAction("✎ Rename", self, triggered=self._local_rename)
            rename.setEnabled(len(sel) == 1)
            menu.addAction(rename)
            del_label = "🗑 Delete" if len(sel) == 1 else f"🗑 Delete {len(sel)} items"
            menu.addAction(QAction(del_label, self, triggered=self._local_delete))
        menu.addSeparator()
        menu.addAction(QAction("🔄 Refresh", self, triggered=self._refresh_local))
        menu.addAction(QAction("↗ Open in File Explorer", self, triggered=self._local_reveal))
        iconize_menu(menu).exec(self.local_tree.viewport().mapToGlobal(pos))

    # ── Remote pane ──────────────────────────────────────────────

    def _refresh_remote(self):
        if not self._connected or self.ftp is None:
            return
        path = self._remote_cwd
        self.remote_path.setText(path)

        def worker():
            try:
                with self._ftp_lock:
                    entries = self._list_remote(self.ftp, path)
            except Exception as e:
                self.transfer_error.emit(f"Cannot list {path}: {e}")
                return
            self.listing_ready.emit(path, entries)

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _list_remote(ftp, path):
        """List a remote directory. Prefers MLSD, falls back to LIST."""
        ftp.cwd(path)
        entries = []
        try:
            for name, facts in ftp.mlsd(facts=["type", "size", "modify"]):
                kind = (facts.get("type") or "").lower()
                if kind in ("cdir", "pdir") or name in (".", ".."):
                    continue
                is_dir = kind == "dir"
                size = "" if is_dir else _human_size(int(facts.get("size") or 0))
                entries.append({
                    "name": name,
                    "is_dir": is_dir,
                    "size": size,
                    "size_bytes": int(facts.get("size") or 0),
                    "is_link": kind not in ("dir", "file"),
                    "modified": _mlsd_time(facts.get("modify", "")),
                })
        except (ftplib.error_perm, ftplib.error_proto, AttributeError):
            # Server has no MLSD — parse LIST output instead.
            lines = []
            ftp.retrlines("LIST", lines.append)
            for line in lines:
                parsed = _parse_list_line(line)
                if not parsed:
                    continue
                name, is_dir, size, when = parsed
                entries.append({
                    "name": name,
                    "is_dir": is_dir,
                    "size": "" if is_dir else _human_size(size),
                    "size_bytes": size,
                    "is_link": bool(line and line[0] in "lbcps"),
                    "modified": when,
                })
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return entries

    def _on_listing_ready(self, path, entries):
        if path != self._remote_cwd:
            return  # a newer navigation already won
        self.remote_tree.clear()
        if path not in ("/", ""):
            parent_dir = str(PurePosixPath(path).parent)
            up = tree_item(["..", "", ""], "folder", FOLDER_COLOR)
            up.setData(0, Qt.ItemDataRole.UserRole,
                       {"name": "..", "is_dir": True, "path": parent_dir})
            up.setForeground(0, QColor("#fbbf24"))
            self.remote_tree.addTopLevelItem(up)
        for e in entries:
            it = tree_item([e["name"], e["size"], e["modified"]],
                           "folder" if e["is_dir"] else file_icon_name(e["name"]),
                           FOLDER_COLOR if e["is_dir"] else None)
            it.setData(0, Qt.ItemDataRole.UserRole, {
                "name": e["name"],
                "is_dir": e["is_dir"],
                "path": str(PurePosixPath(path) / e["name"]),
            })
            self.remote_tree.addTopLevelItem(it)
        dirs = sum(1 for e in entries if e["is_dir"])
        self._notify(f"{path} — {dirs} folders, {len(entries) - dirs} files")

    def _nav_remote(self):
        p = self.remote_path.text().strip() or "/"
        if not self._require_remote():
            return

        def worker():
            try:
                with self._ftp_lock:
                    self.ftp.cwd(p)
                    resolved = self.ftp.pwd()
            except Exception:
                self.transfer_error.emit(f"Path not found: {p}")
                return
            self._remote_cwd = resolved
            self.status_message.emit(f"Opened {resolved}")
            self._refresh_remote()

        threading.Thread(target=worker, daemon=True).start()

    def _remote_up(self):
        if self._remote_cwd in ("/", ""):
            return
        self._remote_cwd = str(PurePosixPath(self._remote_cwd).parent)
        self._refresh_remote()

    def _remote_home(self):
        self._remote_cwd = "/"
        self._refresh_remote()

    def _remote_dblclick(self, item, col):
        d = item.data(0, Qt.ItemDataRole.UserRole)
        if d and d["is_dir"]:
            self._remote_cwd = d["path"] or "/"
            self._refresh_remote()
        elif d and d.get("name") != "..":
            self._open_remote_file(d["path"])

    def _remote_ctx(self, pos):
        item = self.remote_tree.itemAt(pos)
        if item is not None and not item.isSelected():
            self.remote_tree.setCurrentItem(item)
        sel = self._remote_selection()

        menu = QMenu(self)
        if sel and self._connected:
            if len(sel) == 1 and not sel[0].get("is_dir"):
                menu.addAction(QAction("Edit remote file…", self, triggered=self._remote_edit_selected))
            label = "◀ Download" if len(sel) == 1 else f"◀ Download {len(sel)} items"
            menu.addAction(QAction(label, self, triggered=self._download))
            menu.addSeparator()
        menu.addAction(QAction("＋ New Folder", self, triggered=self._remote_new_folder))
        menu.addAction(QAction("＋ New File", self, triggered=self._remote_new_file))
        if sel:
            menu.addSeparator()
            rename = QAction("✎ Rename", self, triggered=self._remote_rename)
            rename.setEnabled(len(sel) == 1)
            menu.addAction(rename)
            del_label = "🗑 Delete" if len(sel) == 1 else f"🗑 Delete {len(sel)} items"
            menu.addAction(QAction(del_label, self, triggered=self._remote_delete))
        menu.addSeparator()
        menu.addAction(QAction("Compare folders…", self, triggered=self._compare_folders))
        menu.addAction(QAction("🔄 Refresh", self, triggered=self._refresh_remote))
        iconize_menu(menu).exec(self.remote_tree.viewport().mapToGlobal(pos))

    def _remote_selection(self) -> list:
        """Selected remote entries, excluding the '..' row."""
        items = self.remote_tree.selectedItems()
        if not items:
            current = self.remote_tree.currentItem()
            items = [current] if current else []
        out = []
        for item in items:
            d = item.data(0, Qt.ItemDataRole.UserRole)
            if d and d.get("name") != "..":
                out.append(d)
        return out

    def _remote_target_dir(self) -> str:
        sel = self._remote_selection()
        if len(sel) == 1 and sel[0].get("is_dir"):
            return sel[0]["path"]
        return self._remote_cwd

    # ── Remote create / rename / delete ──────────────────────────

    def _remote_new_folder(self):
        if not self._require_remote():
            return
        name = ask_name(self, "New remote folder", "Folder name:", "New folder")
        if not name:
            return
        path = str(PurePosixPath(self._remote_target_dir()) / name)
        self._run_remote(lambda ftp: ftp.mkd(path), f"Created folder: {name}")

    def _remote_new_file(self):
        if not self._require_remote():
            return
        name = ask_name(self, "New remote file", "File name:", "new_file.txt")
        if not name:
            return
        path = str(PurePosixPath(self._remote_target_dir()) / name)

        def make(ftp):
            import io
            ftp.storbinary(f"STOR {path}", io.BytesIO(b""))

        self._run_remote(make, f"Created file: {name}")

    def _remote_rename(self):
        if not self._require_remote():
            return
        sel = self._remote_selection()
        if len(sel) != 1:
            self._notify("Select a single item to rename")
            return
        old = sel[0]
        name = ask_name(self, "Rename remote item", "New name:", old["name"])
        if not name or name == old["name"]:
            return
        new_path = str(PurePosixPath(old["path"]).parent / name)
        self._run_remote(
            lambda ftp: ftp.rename(old["path"], new_path), f"Renamed to {name}"
        )

    def _remote_delete(self):
        if not self._require_remote():
            return
        sel = self._remote_selection()
        if not sel:
            return
        host = self.connection_info.get("host", "the server")
        if not confirm_delete(self, [d["name"] for d in sel], host):
            return
        self._notify(f"Deleting {len(sel)} item(s)…")

        def worker():
            failed = []
            try:
                with self._ftp_lock:
                    for d in sel:
                        try:
                            if d["is_dir"]:
                                self._rmtree_remote(self.ftp, d["path"])
                            else:
                                self.ftp.delete(d["path"])
                        except Exception as e:
                            failed.append(f"{d['name']}: {e}")
            except Exception as e:
                self.transfer_error.emit(str(e))
                return
            if failed:
                self.transfer_error.emit("; ".join(failed[:3]))
            else:
                self.transfer_complete.emit(f"Deleted {len(sel)} item(s)")

        threading.Thread(target=worker, daemon=True).start()

    def _rmtree_remote(self, ftp, path):
        """Recursively remove a remote directory (RMD requires it to be empty)."""
        for entry in self._list_remote(ftp, path):
            child = str(PurePosixPath(path) / entry["name"])
            if entry["is_dir"]:
                self._rmtree_remote(ftp, child)
            else:
                ftp.delete(child)
        # _list_remote left the CWD inside `path`; RMD needs us out of it.
        ftp.cwd(str(PurePosixPath(path).parent))
        ftp.rmd(path)

    # ── Transfers ────────────────────────────────────────────────

    def _upload(self):
        self._upload_entries(
            [d for d in self._local_selection() if d.get("path")]
        )

    def _upload_entries(self, entries, dest=None):
        """Upload entry dicts, recursing into any folders among them."""
        if not entries:
            return
        if not self._require_remote():
            return
        dest = dest or self._remote_cwd

        def worker():
            done, failed = 0, []
            with self._ftp_lock:
                for entry in entries:
                    if not self._connected:
                        break
                    target = str(PurePosixPath(dest) / entry["name"])
                    try:
                        if entry.get("is_dir"):
                            self._upload_dir(self.ftp, entry["path"], target)
                        else:
                            self._upload_file(self.ftp, entry["path"], target)
                        done += 1
                    except Exception as e:
                        failed.append(f"{entry['name']}: {e}")
            self._finish_batch("Upload", done, failed)

        threading.Thread(target=worker, daemon=True).start()

    def _download(self):
        entries = [d for d in self._remote_selection() if d.get("path")]
        if not entries:
            return
        if not self._require_remote():
            return
        dest = self._local_cwd

        def worker():
            done, failed = 0, []
            with self._ftp_lock:
                for entry in entries:
                    if not self._connected:
                        break
                    target = os.path.join(dest, entry["name"])
                    try:
                        if entry.get("is_dir"):
                            self._download_dir(self.ftp, entry["path"], target)
                        else:
                            self._download_file(self.ftp, entry["path"], target)
                        done += 1
                    except Exception as e:
                        failed.append(f"{entry['name']}: {e}")
            self._finish_batch("Download", done, failed)

        threading.Thread(target=worker, daemon=True).start()

    # ── Drops from outside the app ───────────────────────────────

    @staticmethod
    def _entries_from_paths(paths) -> list:
        """Turn desktop paths into the entry dicts the transfer engine takes."""
        entries = []
        for path in paths:
            name = os.path.basename(path.rstrip("/\\")) or path
            entries.append({
                "name": name,
                "is_dir": os.path.isdir(path),
                "path": path,
            })
        return entries

    def _on_remote_external_drop(self, paths, target_dir):
        """Files and folders dragged in from the desktop → upload."""
        self._upload_entries(
            self._entries_from_paths(paths), target_dir or self._remote_cwd
        )

    def _on_local_external_drop(self, paths, target_dir):
        """Files and folders dragged in from the desktop → copy locally."""
        dest = target_dir or self._local_cwd
        copied, errors = copy_into(paths, dest)
        self._refresh_local()
        if errors:
            QMessageBox.warning(self, "Copy failed", "\n".join(errors[:6]))
            self._notify(f"Copied {copied}, {len(errors)} failed")
        elif copied:
            self._notify(f"Copied {copied} item(s) into {os.path.basename(dest) or dest}")

    def _upload_from_picker(self):
        """Upload any mix of files and folders chosen from one dialog."""
        if not self._require_remote():
            return
        paths = pick_files_and_folders(
            self, "Select files and folders to upload", self._local_cwd
        )
        if paths:
            self._upload_entries(
                self._entries_from_paths(paths), self._remote_target_dir()
            )

    def _finish_batch(self, verb, done, failed):
        if failed:
            self.transfer_error.emit(
                f"{verb}: {done} ok, {len(failed)} failed — " + "; ".join(failed[:2])
            )
        else:
            self.transfer_complete.emit(f"{verb} done: {done} item(s)")

    def _upload_file(self, ftp, local_path, remote_path):
        total = os.path.getsize(local_path)
        done = [0]

        def cb(block):
            done[0] += len(block)
            self.transfer_progress.emit(int(done[0] / total * 100) if total else 100)

        with open(local_path, "rb") as f:
            ftp.storbinary(f"STOR {remote_path}", f, 8192, cb)

    def _upload_dir(self, ftp, local_dir, remote_dir):
        try:
            ftp.mkd(remote_dir)
        except ftplib.error_perm:
            pass  # already exists
        for name in sorted(os.listdir(local_dir)):
            if not self._connected:
                return
            local_path = os.path.join(local_dir, name)
            remote_path = str(PurePosixPath(remote_dir) / name)
            if os.path.islink(local_path):
                # Don't follow links: a self-referential one would recurse for
                # ever, and the target may sit outside the tree being sent.
                continue
            if os.path.isdir(local_path):
                self._upload_dir(ftp, local_path, remote_path)
            else:
                self._upload_file(ftp, local_path, remote_path)

    def _download_file(self, ftp, remote_path, local_path):
        try:
            total = ftp.size(remote_path) or 0
        except Exception:
            total = 0
        done = [0]
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        with open(local_path, "wb") as f:
            def cb(block):
                f.write(block)
                done[0] += len(block)
                self.transfer_progress.emit(
                    int(done[0] / total * 100) if total else 0
                )
            ftp.retrbinary(f"RETR {remote_path}", cb, 8192)

    def _download_dir(self, ftp, remote_dir, local_dir):
        os.makedirs(local_dir, exist_ok=True)
        for entry in self._list_remote(ftp, remote_dir):
            if not self._connected:
                return
            remote_path = str(PurePosixPath(remote_dir) / entry["name"])
            local_path = os.path.join(local_dir, entry["name"])
            if entry["is_dir"]:
                self._download_dir(ftp, remote_path, local_path)
            else:
                self._download_file(ftp, remote_path, local_path)

    # ── Progress UI ──────────────────────────────────────────────

    def _on_progress(self, pct):
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(pct)
        self.progress_label.setText(f"Transferring... {pct}%")

    def _on_complete(self, msg):
        self.progress_bar.setVisible(False)
        if msg:
            self.progress_label.setText(msg)
        self._refresh_local()
        self._refresh_remote()

    def _on_error(self, msg):
        self.progress_bar.setVisible(False)
        self.progress_label.setText(f"Error: {msg}")

    # ── Lifecycle ────────────────────────────────────────────────

    def set_theme(self, theme):
        self.header.setStyleSheet(
            f"background-color: {theme['bg_tertiary']}; "
            f"border-bottom: 1px solid {theme['border']};"
        )
        self.local_tree.setStyleSheet("")   # use global style
        self.remote_tree.setStyleSheet("")  # use global style
        self._refresh_local()
        self._refresh_remote()

    def cleanup(self):
        self.disconnect()

    def suspend(self):
        """Close the FTP connection before system sleep without closing the tab."""
        self._drop_connection(emit_close=False)

    def resume(self):
        """Reconnect FTP after system wake."""
        self.reconnect()
