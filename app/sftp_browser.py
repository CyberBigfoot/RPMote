"""
RPMote SFTP Browser — Dual-pane file browser over SFTP.
Supports drag-and-drop transfers, sudo elevation, and full disconnect.
"""
import os
import stat
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QLabel, QPushButton, QLineEdit, QProgressBar, QSplitter,
    QMenu, QInputDialog, QMessageBox, QHeaderView, QAbstractItemView
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QMimeData, QByteArray
from PyQt6.QtGui import QColor, QAction, QFont, QDrag, QShortcut, QKeySequence
from app.icons import FOLDER_COLOR, file_icon_name
from app.themes import accent_rgba
from app.widgets import iconize_children, iconize_menu, tree_item
import paramiko
import json
from app import host_keys, ssh_connect
from app.mfa_dialog import MFAPrompter
from app.file_browser_ops import (
    ExternalDropTree, LocalFileOpsMixin, ask_name, confirm_delete, copy_into,
    fmt_time as _fmt_time, human_size as _human_size, perms_str as _perms,
    pick_files_and_folders, RemoteEditingMixin,
)


# ── Drag-and-Drop Tree Widget ────────────────────────────────────

class _DragDropTree(ExternalDropTree):
    """Pane tree: transfers between the two panes, and accepts desktop drops."""

    file_dropped = pyqtSignal(list)  # list of dicts with file info from the OTHER pane

    def __init__(self, pane_id: str, parent=None):
        super().__init__(parent)
        self._pane_id = pane_id  # "local" or "remote"
        self.setDropIndicatorShown(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def mimeTypes(self):
        return ["application/x-rpmote-sftp"]

    def mimeData(self, items):
        mime = QMimeData()
        entries = []
        for item in items:
            d = item.data(0, Qt.ItemDataRole.UserRole)
            if d:
                d["source_pane"] = self._pane_id
                entries.append(d)
        mime.setData("application/x-rpmote-sftp",
                     QByteArray(json.dumps(entries).encode("utf-8")))
        return mime

    def _cross_pane_entries(self, event):
        """Rows dragged from the OTHER pane, or None."""
        mime = event.mimeData()
        if not mime.hasFormat("application/x-rpmote-sftp"):
            return None
        try:
            entries = json.loads(bytes(mime.data("application/x-rpmote-sftp")))
        except Exception:
            return None
        if entries and entries[0].get("source_pane") != self._pane_id:
            return entries
        return None

    def _internal_drag_enter(self, event) -> bool:
        if self._cross_pane_entries(event) is not None:
            event.acceptProposedAction()
            return True
        return False

    def _internal_drag_move(self, event) -> bool:
        if event.mimeData().hasFormat("application/x-rpmote-sftp"):
            event.acceptProposedAction()
            return True
        return False

    def _internal_drop(self, event) -> bool:
        entries = self._cross_pane_entries(event)
        if entries is None:
            return False
        self.file_dropped.emit(entries)
        event.acceptProposedAction()
        return True


class SFTPBrowser(RemoteEditingMixin, LocalFileOpsMixin, QWidget):
    transfer_progress = pyqtSignal(int)
    transfer_complete = pyqtSignal(str)
    transfer_error = pyqtSignal(str)
    session_closed = pyqtSignal()
    connection_lost = pyqtSignal(str)
    connection_established = pyqtSignal()
    #: Unknown/changed host key, carried to the GUI thread for a decision.
    host_key_challenge = pyqtSignal(object)

    def __init__(self, connection_info, parent=None):
        super().__init__(parent)
        self.connection_info = connection_info
        self.client = None
        self.sftp = None
        self.sftp_ops = None  # second SFTP channel: listing + metadata only
        self._connected = False
        self._sudo_active = False
        self._remote_cwd = "/"
        self._local_cwd = str(Path.home())
        self._sftp_lock = threading.Lock()   # transfer channel
        self._ops_lock = threading.RLock()   # metadata channel
        # Host keys accepted with "Connect Once": trusted for this pane only.
        self._session_host_keys: list = []
        self._host_key_prompt_open = False
        self._mfa = MFAPrompter(self)
        self._setup_ui()
        iconize_children(self)
        self._connect_signals()
        QTimer.singleShot(0, self._refresh_local)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)
        # Header
        self.header = QWidget()
        self.header.setFixedHeight(36)
        self.header.setStyleSheet("background-color: transparent;")
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(12,0,12,0)
        self.status_indicator = QLabel("●")
        self.status_indicator.setStyleSheet("color:#f87171;font-size:10px;")
        hl.addWidget(self.status_indicator)
        name = self.connection_info.get("name","SFTP")
        host = self.connection_info.get("host","")
        self.title_label = QLabel(f"  {name} — {host}")
        self.title_label.setStyleSheet("font-weight:600;font-size:12px;")
        hl.addWidget(self.title_label)
        hl.addStretch()
        self.sudo_label = QLabel("")
        self.sudo_label.setStyleSheet("font-size:11px;color:#fbbf24;font-weight:600;")
        hl.addWidget(self.sudo_label)
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
        self.btn_disconnect.setStyleSheet("font-size:11px;padding:2px 10px;background-color:#ef4444;")
        self.btn_disconnect.clicked.connect(self.disconnect)
        hl.addWidget(self.btn_disconnect)
        layout.addWidget(self.header)
        # Splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)
        # Local pane
        lw = QWidget()
        ll = QVBoxLayout(lw)
        ll.setContentsMargins(8,8,4,8)
        lh = QHBoxLayout()
        lh.addWidget(QLabel("Local"))
        self.local_path = QLineEdit(self._local_cwd)
        self.local_path.returnPressed.connect(self._nav_local)
        lh.addWidget(self.local_path)
        bu = QPushButton("↑"); bu.setFixedSize(30,30); bu.setStyleSheet("padding:0px;font-size:16px;"); bu.clicked.connect(self._local_up)
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
        self.local_tree = _DragDropTree("local")
        self.local_tree.setFont(QFont("MesloLGL Nerd Font Mono", 10))
        self.local_tree.setHeaderLabels(["Name","Size","Modified"])
        self.local_tree.setRootIsDecorated(False)
        self.local_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.local_tree.setColumnWidth(1,100); self.local_tree.setColumnWidth(2,140)
        self.local_tree.itemDoubleClicked.connect(self._local_dblclick)
        self.local_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.local_tree.customContextMenuRequested.connect(self._local_ctx)
        # Drop on local tree = download from remote
        self.local_tree.file_dropped.connect(self._on_drop_to_local)
        # …and files dragged in from the desktop are copied into this folder
        self.local_tree.external_drop.connect(self._on_local_external_drop)
        self.local_tree.setToolTip(
            "Drag files and folders here from your desktop to copy them into "
            "this folder."
        )
        ll.addWidget(self.local_tree)
        splitter.addWidget(lw)
        # Center buttons
        cw = QWidget(); cw.setFixedWidth(60)
        cl = QVBoxLayout(cw); cl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.btn_upload = QPushButton("▶"); self.btn_upload.setFixedSize(40,40)
        self.btn_upload.setToolTip("Upload"); self.btn_upload.setStyleSheet("font-size:18px;border-radius:20px;")
        self.btn_upload.clicked.connect(self._upload)
        cl.addWidget(self.btn_upload)
        self.btn_download = QPushButton("◀"); self.btn_download.setFixedSize(40,40)
        self.btn_download.setToolTip("Download"); self.btn_download.setStyleSheet("font-size:18px;border-radius:20px;")
        self.btn_download.clicked.connect(self._download)
        cl.addWidget(self.btn_download)
        splitter.addWidget(cw)
        # Remote pane
        rw = QWidget()
        rl = QVBoxLayout(rw)
        rl.setContentsMargins(4,8,8,8)
        rh = QHBoxLayout()
        rh.addWidget(QLabel("Remote"))
        self.remote_path = QLineEdit("/")
        self.remote_path.returnPressed.connect(self._nav_remote)
        rh.addWidget(self.remote_path)
        bru = QPushButton("↑"); bru.setFixedSize(30,30); bru.setStyleSheet("padding:0px;font-size:16px;"); bru.clicked.connect(self._remote_up)
        rh.addWidget(bru)
        rl.addLayout(rh)
        rl.addLayout(self._build_pane_toolbar([
            ("⬆ Upload…", "Pick any mix of files and folders to upload",
             self._upload_from_picker),
            ("＋ Folder", "New remote folder", self._remote_new_folder),
            ("＋ File", "New empty remote file", self._remote_new_file),
            ("✎ Rename", "Rename selected item (F2)", self._remote_rename),
            ("🗑 Delete", "Delete selected items, folders included (Del)", self._remote_delete),
            ("↺", "Refresh", self._refresh_remote),
            ("⌂", "Go to home directory", self._remote_home),
        ]))
        self.remote_tree = _DragDropTree("remote")
        self.remote_tree.setFont(QFont("MesloLGL Nerd Font Mono", 10))
        self.remote_tree.setHeaderLabels(["Name","Size","Modified","Perms"])
        self.remote_tree.setRootIsDecorated(False)
        self.remote_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.remote_tree.setColumnWidth(1,100); self.remote_tree.setColumnWidth(2,140); self.remote_tree.setColumnWidth(3,80)
        self.remote_tree.itemDoubleClicked.connect(self._remote_dblclick)
        self.remote_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.remote_tree.customContextMenuRequested.connect(self._remote_ctx)
        # Drop on remote tree = upload from local
        self.remote_tree.file_dropped.connect(self._on_drop_to_remote)
        # …and files dragged in from the desktop are uploaded
        self.remote_tree.external_drop.connect(self._on_remote_external_drop)
        self.remote_tree.setToolTip(
            "Drag files and folders here from your desktop to upload them.\n"
            "Drop onto a folder row to upload into that folder."
        )
        rl.addWidget(self.remote_tree)
        splitter.addWidget(rw)
        splitter.setSizes([400,60,400])
        layout.addWidget(splitter,1)
        # Progress
        pw = QWidget(); pw.setFixedHeight(30)
        pl = QHBoxLayout(pw); pl.setContentsMargins(8,2,8,2)
        self.progress_label = QLabel("Ready")
        self.progress_label.setStyleSheet("font-size:11px;color:#a0a3b8;")
        pl.addWidget(self.progress_label)
        self.progress_bar = QProgressBar(); self.progress_bar.setFixedHeight(8); self.progress_bar.setVisible(False)
        pl.addWidget(self.progress_bar)
        layout.addWidget(pw)

        # Keyboard: Delete / F2 act on whichever pane has focus.
        QShortcut(QKeySequence("Delete"), self.local_tree,
                  activated=self._local_delete,
                  context=Qt.ShortcutContext.WidgetShortcut)
        QShortcut(QKeySequence("F2"), self.local_tree,
                  activated=self._local_rename,
                  context=Qt.ShortcutContext.WidgetShortcut)
        QShortcut(QKeySequence("Delete"), self.remote_tree,
                  activated=self._remote_delete,
                  context=Qt.ShortcutContext.WidgetShortcut)
        QShortcut(QKeySequence("F2"), self.remote_tree,
                  activated=self._remote_rename,
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

    def _notify(self, text: str):
        self.progress_label.setText(text)

    def _connect_signals(self):
        self.transfer_progress.connect(self._on_progress)
        self.transfer_complete.connect(self._on_complete)
        self.transfer_error.connect(self._on_error)
        self.connection_established.connect(self._on_connected)
        self.connection_lost.connect(self._on_lost)
        self.host_key_challenge.connect(
            self._on_host_key_challenge, Qt.ConnectionType.QueuedConnection
        )

    def connect_sftp(self):
        threading.Thread(target=self._do_connect, daemon=True).start()

    def _do_connect(self):
        pw = self.connection_info.get("password", "")
        try:
            # The same connect path the terminal uses, so a machine behind a
            # bastion is reachable from the file browser too.
            self.client = ssh_connect.open_client(
                self.connection_info,
                extra_host_keys=list(self._session_host_keys),
                interactive=self._mfa.handler,
            )

            # Try sudo sftp-server first for root-level access
            sftp = self._try_sudo_sftp(pw)
            if sftp:
                self.sftp = sftp
                self._sudo_active = True
            else:
                # Fall back to normal SFTP
                self.sftp = self.client.open_sftp()
                self._sudo_active = False

            # Second channel dedicated to directory listing and metadata
            # (mkdir/rename/delete/chmod). Keeping it off the transfer channel
            # means the browser stays responsive during a large upload and the
            # two never interleave requests on one non-thread-safe SFTPClient.
            try:
                self.sftp_ops = self.client.open_sftp()
            except Exception:
                self.sftp_ops = self.sftp

            self._connected = True
            try: self._remote_cwd = self.sftp.normalize(".")
            except: self._remote_cwd = "/"
            self.connection_established.emit()
        except host_keys.HostKeyUnverified as challenge:
            client, self.client = self.client, None
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            self.host_key_challenge.emit(challenge)
        except Exception as e:
            from app.net_errors import describe_failure
            info = self.connection_info
            _kind, reason = describe_failure(e, info.get("host", ""), info.get("port", 22),
                                             str(info.get("username", "") or ""))
            self.connection_lost.emit(f"SFTP failed: {reason}")

    def _on_host_key_challenge(self, challenge):
        """Ask about an unknown or changed host key, then retry (GUI thread)."""
        if self._host_key_prompt_open:
            return
        self._host_key_prompt_open = True
        try:
            from app.host_key_dialog import ask_about_host_key

            store = host_keys.store()
            if store.policy == host_keys.POLICY_STRICT:
                self.connection_lost.emit(
                    "Host key not trusted and the policy is strict — refused."
                )
                return
            decision = ask_about_host_key(
                challenge, self.connection_info.get("name", ""), self
            )
            if not decision.connect:
                self.connection_lost.emit("Host key not trusted — connection refused.")
                return
            if decision.remember:
                store.trust(challenge.host, challenge.port, challenge.key,
                            replace=decision.replace)
            else:
                self._session_host_keys.append(challenge.key)
        finally:
            self._host_key_prompt_open = False
        self.connect_sftp()

    def _try_sudo_sftp(self, password):
        """Attempt to open an SFTP session running as root via sudo sftp-server.

        NOTE: Disabled — SFTPClient.from_transport() opens a new subsystem channel
        that bypasses the sudo exec channel entirely, so sudo elevation never
        actually worked. Normal SFTP fallback is used instead.
        """
        return None

    def _drop_connection(self, emit_close: bool = True):
        self._connected = False
        self._sudo_active = False
        try:
            if self.sftp_ops is not None and self.sftp_ops is not self.sftp:
                self.sftp_ops.close()
        except Exception:
            pass
        try:
            if self.sftp:
                self.sftp.close()
        except Exception:
            pass
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        self.sftp = None
        self.sftp_ops = None
        self.client = None
        self.status_indicator.setStyleSheet("color:#f87171;font-size:10px;")
        self.sudo_label.setText("")
        self.progress_label.setText("Disconnected")
        self.remote_tree.clear()
        if emit_close:
            self.session_closed.emit()

    def disconnect(self):
        self._drop_connection(emit_close=True)

    def reconnect(self):
        self._drop_connection(emit_close=False)
        self.progress_label.setText("Reconnecting...")
        self.connect_sftp()

    def _on_connected(self):
        self.status_indicator.setStyleSheet("color:#4ade80;font-size:10px;")
        if self._sudo_active:
            self.sudo_label.setText("SUDO")
            self.progress_label.setText("Connected (sudo)")
        else:
            self.sudo_label.setText("")
            self.progress_label.setText("Connected")
        self._refresh_local(); self._refresh_remote()

    def _on_lost(self, msg):
        self.status_indicator.setStyleSheet("color:#f87171;font-size:10px;")
        self.progress_label.setText(msg)

    # ── Local pane ────────────────────────────────────────────────

    def _refresh_local(self):
        self.local_path.setText(self._local_cwd)
        self.local_path.setToolTip(self._local_cwd)
        self.local_path.setCursorPosition(len(self._local_cwd))
        self.local_tree.setUpdatesEnabled(False)
        self.local_tree.blockSignals(True)
        self.local_tree.clear()
        try:
            items = []
            parent_dir = str(Path(self._local_cwd).parent)
            if parent_dir != self._local_cwd:
                up = tree_item(["..", "", ""], "folder", FOLDER_COLOR)
                up.setData(0, Qt.ItemDataRole.UserRole,
                           {"name": "..", "is_dir": True, "path": parent_dir})
                up.setForeground(0, QColor("#fbbf24"))
                items.append(up)
            for name in sorted(os.listdir(self._local_cwd), key=lambda x:(not os.path.isdir(os.path.join(self._local_cwd,x)),x.lower())):
                full = os.path.join(self._local_cwd, name)
                isd = os.path.isdir(full)
                try:
                    s = os.stat(full); sz = "" if isd else _human_size(s.st_size); mt = _fmt_time(int(s.st_mtime))
                except: sz=""; mt="—"
                it = tree_item([name, sz, mt], "folder" if isd else file_icon_name(name),
                               FOLDER_COLOR if isd else None)
                it.setData(0, Qt.ItemDataRole.UserRole, {"name":name,"is_dir":isd,"path":full})
                items.append(it)
            self.local_tree.addTopLevelItems(items)
        except: pass
        finally:
            self.local_tree.blockSignals(False)
            self.local_tree.setUpdatesEnabled(True)

    def _nav_local(self):
        p = self.local_path.text().strip()
        if os.path.isdir(p): self._local_cwd = p; self._refresh_local()

    def _local_up(self):
        par = str(Path(self._local_cwd).parent)
        if par != self._local_cwd: self._local_cwd = par; self._refresh_local()

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

    # ── Remote pane ───────────────────────────────────────────────

    @contextmanager
    def _ops_session(self):
        """Serialised access to the metadata SFTP channel.

        paramiko's SFTPClient is not thread-safe, and browsing now happens from
        the GUI thread while deletes and recursive transfers run on workers, so
        every request on this channel goes through one lock.
        """
        with self._ops_lock:
            client = self.sftp_ops or self.sftp
            if client is None:
                raise RuntimeError("Not connected")
            yield client

    def _refresh_remote(self):
        if not self._connected or not self.sftp: return
        self.remote_path.setText(self._remote_cwd)
        self.remote_tree.setUpdatesEnabled(False)
        self.remote_tree.blockSignals(True)
        self.remote_tree.clear()
        try:
            items = []
            if self._remote_cwd not in ("/", ""):
                parent_dir = str(PurePosixPath(self._remote_cwd).parent)
                up = tree_item(["..", "", "", ""], "folder", FOLDER_COLOR)
                up.setData(0, Qt.ItemDataRole.UserRole,
                           {"name": "..", "is_dir": True, "path": parent_dir})
                up.setForeground(0, QColor("#fbbf24"))
                items.append(up)
            with self._ops_session() as ops:
                listing = ops.listdir_attr(self._remote_cwd)
            for e in sorted(listing, key=lambda x:(not stat.S_ISDIR(x.st_mode or 0),x.filename.lower())):
                isd = stat.S_ISDIR(e.st_mode or 0)
                n = e.filename; sz = "" if isd else _human_size(e.st_size or 0)
                mt = _fmt_time(e.st_mtime or 0); pm = _perms(e.st_mode & 0o777) if e.st_mode else "—"
                it = tree_item([n, sz, mt, pm], "folder" if isd else file_icon_name(n),
                               FOLDER_COLOR if isd else None)
                rp = str(PurePosixPath(self._remote_cwd)/n)
                it.setData(0, Qt.ItemDataRole.UserRole, {
                    "name": n, "is_dir": isd, "path": rp,
                    "mode": int(e.st_mode or 0) & 0o777,
                })
                items.append(it)
            self.remote_tree.addTopLevelItems(items)
        except Exception as e:
            self.progress_label.setText(f"Error: {e}")
        finally:
            self.remote_tree.blockSignals(False)
            self.remote_tree.setUpdatesEnabled(True)

    def _nav_remote(self):
        p = self.remote_path.text().strip()
        if self._connected and self.sftp:
            try:
                with self._ops_session() as ops:
                    ops.listdir(p)
                self._remote_cwd = p
                self._refresh_remote()
            except Exception:
                self.progress_label.setText("Path not found")

    def _remote_up(self):
        par = str(PurePosixPath(self._remote_cwd).parent)
        if par != self._remote_cwd: self._remote_cwd = par; self._refresh_remote()

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
            menu.addAction(QAction("🔒 Permissions…", self, triggered=self._remote_chmod))
        menu.addSeparator()
        menu.addAction(QAction("Compare folders…", self, triggered=self._compare_folders))
        menu.addAction(QAction("🔄 Refresh", self, triggered=self._refresh_remote))
        iconize_menu(menu).exec(self.remote_tree.viewport().mapToGlobal(pos))

    # ── Remote create / rename / delete / chmod ───────────────────

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
        """Directory new remote items land in: selected folder, else cwd."""
        sel = self._remote_selection()
        if len(sel) == 1 and sel[0].get("is_dir"):
            return sel[0]["path"]
        return self._remote_cwd

    def _require_remote(self) -> bool:
        if self._connected and self.sftp:
            return True
        self._notify("Not connected")
        return False

    def _remote_home(self):
        if not self._require_remote():
            return
        try:
            with self._ops_session() as ops:
                self._remote_cwd = ops.normalize(".")
        except Exception:
            self._remote_cwd = "/"
        self._refresh_remote()

    def _remote_new_folder(self):
        if not self._require_remote():
            return
        name = ask_name(self, "New remote folder", "Folder name:", "New folder")
        if not name:
            return
        path = str(PurePosixPath(self._remote_target_dir()) / name)
        try:
            with self._ops_session() as ops:
                ops.mkdir(path)
            self._notify(f"Created folder: {name}")
        except Exception as e:
            QMessageBox.warning(self, "Create folder failed", str(e))
        self._refresh_remote()

    def _remote_new_file(self):
        if not self._require_remote():
            return
        name = ask_name(self, "New remote file", "File name:", "new_file.txt")
        if not name:
            return
        path = str(PurePosixPath(self._remote_target_dir()) / name)
        try:
            with self._ops_session() as ops:
                if self._remote_exists(ops, path):
                    self._notify(f"Already exists: {name}")
                    return
                # "x" maps to SSH_FXF_CREAT|SSH_FXF_EXCL — it never truncates.
                with ops.open(path, "x"):
                    pass
            self._notify(f"Created file: {name}")
        except Exception as e:
            QMessageBox.warning(self, "Create file failed", str(e))
        self._refresh_remote()

    @staticmethod
    def _remote_exists(ops, path: str) -> bool:
        try:
            ops.stat(path)
            return True
        except IOError:
            return False

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
        # Dialogs stay outside the channel lock — a modal window must never
        # hold the metadata channel while a transfer worker waits on it.
        clash = False
        error = ""
        try:
            with self._ops_session() as ops:
                clash = self._remote_exists(ops, new_path)
                if not clash:
                    ops.rename(old["path"], new_path)
        except Exception as e:
            error = str(e)

        if clash:
            QMessageBox.warning(self, "Rename failed", f"'{name}' already exists.")
        elif error:
            QMessageBox.warning(self, "Rename failed", error)
        else:
            self._notify(f"Renamed to {name}")
        self._refresh_remote()

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
        threading.Thread(target=self._do_remote_delete, args=(sel,), daemon=True).start()

    def _do_remote_delete(self, entries):
        """Thread entry point: delete remote files, recursing into folders."""
        failed = []
        try:
            with self._ops_session() as ops:
                for d in entries:
                    try:
                        if d.get("is_dir"):
                            self._rmtree_remote(ops, d["path"])
                        else:
                            ops.remove(d["path"])
                    except Exception as e:
                        failed.append(f"{d['name']}: {e}")
        except Exception as e:
            self.transfer_error.emit(str(e))
            return
        if failed:
            self.transfer_error.emit("; ".join(failed[:3]))
        else:
            self.transfer_complete.emit(f"Deleted {len(entries)} item(s)")

    def _rmtree_remote(self, ops, path):
        """Recursively remove a remote directory (SFTP rmdir needs it empty)."""
        for entry in ops.listdir_attr(path):
            child = str(PurePosixPath(path) / entry.filename)
            if stat.S_ISDIR(entry.st_mode or 0):
                self._rmtree_remote(ops, child)
            else:
                ops.remove(child)
        ops.rmdir(path)

    def _remote_chmod(self):
        if not self._require_remote():
            return
        sel = self._remote_selection()
        if not sel:
            return
        preset = "%03o" % sel[0].get("mode", 0o644)
        text, ok = QInputDialog.getText(
            self, "Permissions",
            f"Octal mode for {len(sel)} item(s) — e.g. 644, 755:",
            text=preset,
        )
        if not ok:
            return
        try:
            mode = int(text.strip(), 8)
        except ValueError:
            QMessageBox.warning(self, "Permissions", f"'{text.strip()}' is not an octal mode.")
            return
        if not 0 <= mode <= 0o7777:
            QMessageBox.warning(self, "Permissions", "Mode must be between 000 and 7777.")
            return
        failed = []
        with self._ops_session() as ops:
            for d in sel:
                try:
                    ops.chmod(d["path"], mode)
                except Exception as e:
                    failed.append(f"{d['name']}: {e}")
        if failed:
            QMessageBox.warning(self, "Permissions failed", "\n".join(failed[:6]))
        else:
            self._notify("Set mode %o on %d item(s)" % (mode, len(sel)))
        self._refresh_remote()

    # ── Transfers (button-based) ─────────────────────────────────

    def _upload(self):
        entries = [d for d in self._local_selection() if d.get("path")]
        self._start_upload(entries)

    def _download(self):
        entries = [d for d in self._remote_selection() if d.get("path")]
        self._start_download(entries)

    # ── Drag-and-drop handlers ───────────────────────────────────

    def _on_drop_to_local(self, entries):
        """Files dragged from remote pane → download to local."""
        self._start_download([e for e in entries if e.get("name") != ".."])

    def _on_drop_to_remote(self, entries):
        """Files dragged from local pane → upload to remote."""
        self._start_upload([e for e in entries if e.get("name") != ".."])

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
        self._start_upload(
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
        if not self._connected:
            self._notify("Not connected")
            return
        paths = pick_files_and_folders(
            self, "Select files and folders to upload", self._local_cwd
        )
        if paths:
            self._start_upload(
                self._entries_from_paths(paths), self._remote_target_dir()
            )

    # ── Batch transfer dispatch ──────────────────────────────────

    def _start_upload(self, entries, dest=None):
        """Queue an upload batch. Folders are uploaded recursively."""
        if not entries:
            return
        if not self._connected:
            self._notify("Not connected")
            return
        dest = dest or self._remote_cwd
        threading.Thread(
            target=self._run_upload_batch, args=(entries, dest), daemon=True
        ).start()

    def _start_download(self, entries):
        """Queue a download batch. Folders are downloaded recursively."""
        if not entries:
            return
        if not self._connected:
            self._notify("Not connected")
            return
        dest = self._local_cwd
        threading.Thread(
            target=self._run_download_batch, args=(entries, dest), daemon=True
        ).start()

    def _run_upload_batch(self, entries, remote_dest):
        """Upload every entry sequentially on one worker thread.

        One worker (not one per file) keeps requests off a single SFTPClient
        from interleaving and keeps the progress bar meaningful.
        """
        done, failed = 0, []
        for entry in entries:
            if not self._connected:
                break
            name = entry.get("name") or os.path.basename(entry["path"])
            target = str(PurePosixPath(remote_dest) / name)
            try:
                if entry.get("is_dir"):
                    self._upload_dir_recursive(entry["path"], target)
                else:
                    self._xfer_one("up", entry["path"], target)
                done += 1
            except Exception as e:
                failed.append(f"{name}: {e}")
        self._finish_batch("Upload", done, failed)

    def _run_download_batch(self, entries, local_dest):
        """Download every entry sequentially on one worker thread."""
        done, failed = 0, []
        for entry in entries:
            if not self._connected:
                break
            name = entry.get("name") or PurePosixPath(entry["path"]).name
            target = os.path.join(local_dest, name)
            try:
                if entry.get("is_dir"):
                    self._sync_dir_recursive(entry["path"], target)
                else:
                    self._xfer_one("down", entry["path"], target)
                done += 1
            except Exception as e:
                failed.append(f"{name}: {e}")
        self._finish_batch("Download", done, failed)

    def _finish_batch(self, verb, done, failed):
        if failed:
            self.transfer_error.emit(
                f"{verb}: {done} ok, {len(failed)} failed — " + "; ".join(failed[:2])
            )
        else:
            self.transfer_complete.emit(f"{verb} done: {done} item(s)")

    def _upload_dir_recursive(self, local_dir, remote_dir):
        """Mirror a local directory tree onto the remote host."""
        with self._ops_session() as ops:
            try:
                ops.mkdir(remote_dir)
            except IOError:
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
                self._upload_dir_recursive(local_path, remote_path)
            else:
                self._xfer_one("up", local_path, remote_path)

    # ── Transfer engine ──────────────────────────────────────────

    def _xfer_one(self, direction, src, dst):
        """Transfer a single file. Raises on failure so the batch can report it."""
        if not self.sftp or not self._connected:
            raise RuntimeError("Not connected")

        def cb(done, total):
            self.transfer_progress.emit(int(done / total * 100) if total else 0)

        with self._sftp_lock:
            if direction == "up":
                self.sftp.put(src, dst, callback=cb)
            else:
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                self.sftp.get(src, dst, callback=cb)

    def _sync_dir_recursive(self, remote_dir, local_dir):
        """Recursively mirror remote_dir into local_dir."""
        os.makedirs(local_dir, exist_ok=True)
        with self._ops_session() as ops:
            entries = ops.listdir_attr(remote_dir)
        for entry in entries:
            if not self._connected:
                return
            name = entry.filename
            remote_path = str(PurePosixPath(remote_dir) / name)
            local_path = os.path.join(local_dir, name)
            if stat.S_ISDIR(entry.st_mode or 0):
                self._sync_dir_recursive(remote_path, local_path)
            else:
                self._xfer_one("down", remote_path, local_path)

    # ── Progress UI ──────────────────────────────────────────────

    def _on_progress(self, pct):
        self.progress_bar.setVisible(True); self.progress_bar.setValue(pct)
        self.progress_label.setText(f"Transferring... {pct}%")

    def _on_complete(self, msg):
        self.progress_bar.setVisible(False); self.progress_label.setText(msg)
        self._refresh_local(); self._refresh_remote()

    def _on_error(self, msg):
        self.progress_bar.setVisible(False); self.progress_label.setText(f"Error: {msg}")

    def set_theme(self, theme):
        self.header.setStyleSheet(f"background-color: {theme['bg_tertiary']}; border-bottom: 1px solid {theme['border']};")
        self.local_tree.setStyleSheet("") # Use global style
        self.remote_tree.setStyleSheet("") # Use global style
        self._refresh_local()
        self._refresh_remote()

    def cleanup(self): self.disconnect()

    def suspend(self):
        """Close SFTP connection before system sleep without closing the tab."""
        self._drop_connection(emit_close=False)

    def resume(self):
        """Reconnect SFTP after system wake."""
        self.reconnect()
