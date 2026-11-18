"""Remote text editor and folder comparison dialogs, with worker-thread I/O."""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QKeySequence, QShortcut, QSyntaxHighlighter, QTextCharFormat
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from app import icons
from app.remote_files import MAX_EDIT_BYTES, TextDocument, text_diff
from app.widgets import fit_to_screen, set_variant
from app.yaml_editor import YamlTextEdit
from app.yaml_support import format_yaml, prepare_yaml


def _label(text=""):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class _DiffHighlighter(QSyntaxHighlighter):
    def highlightBlock(self, text):
        color = "#94a3b8"
        if text.startswith(("---", "+++", "@@")):
            color = "#a78bfa"
        elif text.startswith("+"):
            color = "#4ade80"
        elif text.startswith("-"):
            color = "#f87171"
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        self.setFormat(0, len(text), fmt)


class _WorkerDialog(QDialog):
    _result = pyqtSignal(str, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = ""
        self._closing_allowed = False
        self._result.connect(self._finished)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

    def _work(self, operation, fn):
        if self._busy:
            return
        self._busy = operation
        self._update_controls()
        def worker():
            value, error = None, None
            try:
                value = fn()
            except Exception as exc:
                error = exc
            try:
                self._result.emit(operation, value, error)
            except RuntimeError:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _may_close(self):
        if self._busy == "save":
            self.status.setText("Finishing the remote save. Please wait before closing.")
            return False
        return True

    def reject(self):
        if self._closing_allowed or self._may_close():
            super().reject()

    def closeEvent(self, event):
        if not self._may_close():
            event.ignore()
            return
        self._closing_allowed = True
        try:
            super().closeEvent(event)
        finally:
            self._closing_allowed = False


class RemoteFileEditor(_WorkerDialog):
    saved = pyqtSignal(str)

    def __init__(self, backend, path, parent=None, draft_path=None):
        super().__init__(parent)
        self.backend, self.path = backend, path
        self.snapshot = None
        self._reviewed_raw = None
        self._yaml_checked_text = None
        self._draft_path = draft_path
        self.setWindowTitle(f"Edit remote file — {Path(path).name}")
        self.resize(1050, 740)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        title = _label(path)
        title.setStyleSheet("font-size:16px;font-weight:600;")
        layout.addWidget(title)
        self.details = _label(backend.label)
        self.details.setProperty("role", "hint")
        layout.addWidget(self.details)

        self.tabs = QTabWidget()
        self.editor = YamlTextEdit(yaml=Path(path).suffix.lower() in (".yaml", ".yml"))
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setFont(QFont("Consolas", 11))
        self.editor.setTabStopDistance(self.editor.fontMetrics().horizontalAdvance(" ") * 4)
        self.editor.textChanged.connect(self._edited)
        self.editor.indentation_changed.connect(self._indentation_changed)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff.setFont(QFont("Consolas", 11))
        self._highlighter = _DiffHighlighter(self.diff.document())
        self.tabs.addTab(self.editor, icons.icon("edit"), "Edit")
        self.tabs.addTab(self.diff, icons.icon("code"), "Changes")
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs, 1)
        self.status = _label("Loading remote file…")
        layout.addWidget(self.status)
        self.backup_label = _label("A backup of the original is created beside the remote file on every save.")
        self.backup_label.setProperty("role", "hint")
        layout.addWidget(self.backup_label)
        row = QHBoxLayout()
        self.btn_reload = QPushButton("Reload remote file")
        self.btn_reload.clicked.connect(self._reload)
        row.addWidget(self.btn_reload)
        self.btn_format = QPushButton("Format YAML")
        self.btn_format.setVisible(self.editor.yaml)
        self.btn_format.setToolTip("Normalize indentation while preserving comments, quoted values and multiline contents")
        self.btn_format.clicked.connect(self._format_yaml)
        row.addWidget(self.btn_format)
        row.addStretch()
        self.btn_review = QPushButton("Review changes")
        self.btn_review.clicked.connect(self.review_changes)
        row.addWidget(self.btn_review)
        self.btn_save = set_variant(QPushButton("Save with backup"), "primary")
        self.btn_save.clicked.connect(self.save_changes)
        row.addWidget(self.btn_save)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)
        self._diff_timer = QTimer(self)
        self._diff_timer.setSingleShot(True)
        self._diff_timer.setInterval(200)
        self._diff_timer.timeout.connect(self._refresh_diff)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save_shortcut)
        self._update_controls()
        fit_to_screen(self)
        QTimer.singleShot(0, self._load)

    @property
    def dirty(self):
        return self.snapshot is not None and self.editor.toPlainText() != self.snapshot.document.text

    def _update_controls(self):
        ready = self.snapshot is not None and not self._busy
        self.editor.setReadOnly(not ready)
        self.btn_reload.setEnabled(not self._busy)
        self.btn_format.setEnabled(ready)
        self.btn_review.setEnabled(ready and self.dirty and self.tabs.currentIndex() != 1)
        yaml_ready = not self.editor.yaml or self._yaml_checked_text == self.editor.toPlainText()
        self.btn_save.setEnabled(ready and self.dirty and self.tabs.currentIndex() == 1 and self._reviewed_raw is not None and yaml_ready)

    def _load(self):
        draft_path, self._draft_path = self._draft_path, None
        def load():
            snapshot = self.backend.load(self.path)
            draft = None
            if draft_path:
                with open(draft_path, "rb") as handle:
                    draft = TextDocument.decode(handle.read(MAX_EDIT_BYTES + 1)).text
            return snapshot, draft
        self.status.setText("Loading remote file…")
        self._work("load", load)

    def _finished(self, operation, value, error):
        self._busy = ""
        if error is not None:
            self.status.setText(str(error))
            self.status.setStyleSheet("color:#f87171;")
            self._update_controls()
            return
        self.status.setStyleSheet("")
        if operation == "load":
            self.snapshot, draft = value
            if self.editor.yaml:
                self.editor.configure_indentation(self.snapshot.document.text)
            self.editor.setPlainText(self.snapshot.document.text if draft is None else draft)
            self._update_details()
            self.status.setText("Loaded. Edit the file, then review the changes." if draft is None else "Local version loaded as a draft. Review the changes before saving to the remote file.")
            if draft is not None:
                self.review_changes()
        elif operation == "save":
            self.snapshot, backup = value
            self._update_details()
            self._reviewed_raw = None
            self.status.setText("Saved to the remote host.")
            self.backup_label.setText(f"Original backup: {backup}")
            self._refresh_diff()
            self.saved.emit(self.path)
        elif operation in ("yaml-review", "yaml-format"):
            source, prepared = value
            if self.editor.toPlainText() != source:
                self.status.setText("The draft changed while checking YAML. Review the changes again.")
                self._update_controls()
                return
            self.editor.replace_text(prepared)
            self._yaml_checked_text = prepared
            if operation == "yaml-format":
                self._reviewed_raw = None
                self.tabs.setCurrentIndex(0)
                self.status.setText("YAML formatted. Review the changes before saving.")
            else:
                self.status.setText("YAML is valid. Review the changes, then save with backup.")
                self.tabs.setCurrentIndex(1)
                self._refresh_diff()
        self._update_controls()

    def _update_details(self):
        doc = self.snapshot.document
        ending = {"\n": "LF", "\r\n": "CRLF", "\r": "CR"}[doc.newline]
        self.details.setText(f"{self.backend.label} · {doc.encoding.upper()} · {ending} · {len(doc.raw):,} bytes\n{self.snapshot.target}")
        if self.editor.yaml:
            self.details.setText(self.details.text() + f"\nYAML · {self.editor.indent_width} spaces · Tab / Shift+Tab to indent / outdent")

    def _edited(self):
        self._reviewed_raw = None
        self._yaml_checked_text = None
        if self.editor.yaml and self.snapshot is not None:
            self.status.setStyleSheet("")
            self.status.setText("YAML draft changed. Review changes to check it before saving.")
        self._update_controls()
        self._diff_timer.start()

    def _indentation_changed(self, _width):
        if self.snapshot is not None:
            self._update_details()

    def _format_yaml(self):
        if self._busy or self.snapshot is None or not self.editor.yaml:
            return
        source, width = self.editor.toPlainText(), self.editor.indent_width
        self.status.setText("Formatting YAML…")
        self._work("yaml-format", lambda: (source, format_yaml(source, width)))

    def _tab_changed(self, index):
        if index == 1:
            self._refresh_diff()
        self._update_controls()

    def _refresh_diff(self):
        if self.snapshot is None:
            return
        try:
            raw = self.snapshot.document.encode(self.editor.toPlainText())
        except Exception as exc:
            self._reviewed_raw = None
            self.diff.setPlainText(str(exc))
            self._update_controls()
            return
        preview = text_diff(self.snapshot.document.text, self.editor.toPlainText(), self.snapshot.target, "Edited version")
        self.diff.setPlainText(preview or "No changes.")
        lines = preview.splitlines()[2:]
        added = sum(line.startswith("+") for line in lines)
        removed = sum(line.startswith("-") for line in lines)
        self.tabs.setTabText(1, f"Changes · +{added} / −{removed}" if preview else "Changes")
        if self.tabs.currentIndex() == 1:
            self._reviewed_raw = raw
        self._update_controls()

    def review_changes(self):
        if self._busy:
            return
        self.tabs.setCurrentIndex(1)
        self._refresh_diff()
        if self.editor.yaml and self.snapshot is not None:
            source = self.editor.toPlainText()
            if self._yaml_checked_text != source:
                width = self.editor.indent_width
                self.status.setText("Checking YAML syntax and indentation…")
                self._work("yaml-review", lambda: (source, prepare_yaml(source, width)))

    def _save_shortcut(self):
        if self.tabs.currentIndex() == 1:
            self.save_changes()
        else:
            self.review_changes()

    def save_changes(self):
        if self._busy or not self.dirty:
            return
        if self.editor.yaml and self._yaml_checked_text != self.editor.toPlainText():
            self.review_changes()
            return
        try:
            raw = self.snapshot.document.encode(self.editor.toPlainText())
        except Exception as exc:
            self.status.setText(str(exc))
            return
        if self.tabs.currentIndex() != 1 or raw != self._reviewed_raw:
            self.review_changes()
            return
        snapshot = self.snapshot
        self.status.setText("Checking the remote file, creating a backup, and saving…")
        self._work("save", lambda: self.backend.save(snapshot, raw))

    def _discard_confirmed(self):
        return not self.dirty or QMessageBox.question(
            self, "Unsaved remote edits", "Discard your unsaved edits?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Discard

    def _reload(self):
        if not self._busy and self._discard_confirmed():
            self._load()

    def _may_close(self):
        return super()._may_close() and self._discard_confirmed()


class DirectoryCompareDialog(_WorkerDialog):
    def __init__(self, backend, local_root, remote_root, parent=None):
        super().__init__(parent)
        self.backend, self.local_root, self.remote_root = backend, local_root, remote_root
        self.setWindowTitle("Compare local and remote folders")
        self.resize(980, 640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.addWidget(_label(f"Local: {local_root}\n{backend.label}: {remote_root}"))
        options = QHBoxLayout()
        self.recursive = QCheckBox("Include subfolders")
        options.addWidget(self.recursive)
        options.addStretch()
        self.btn_compare = QPushButton("Compare again")
        self.btn_compare.clicked.connect(self.compare)
        options.addWidget(self.btn_compare)
        layout.addLayout(options)
        self.tree = QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setHeaderLabels(["File", "Comparison", "Local bytes", "Remote bytes"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(1, 230)
        self.tree.itemSelectionChanged.connect(self._update_controls)
        layout.addWidget(self.tree, 1)
        self.status = _label("Comparing folder contents…")
        layout.addWidget(self.status)
        hint = _label("Review a local file as a draft before saving it to an existing remote file. Links and special files are skipped; large files are compared by size only.")
        hint.setProperty("role", "hint")
        layout.addWidget(hint)
        buttons = QHBoxLayout()
        self.btn_edit = QPushButton("Edit remote file…")
        self.btn_edit.clicked.connect(lambda: self._open_editor(False))
        buttons.addWidget(self.btn_edit)
        self.btn_local = set_variant(QPushButton("Review local version…"), "primary")
        self.btn_local.clicked.connect(lambda: self._open_editor(True))
        buttons.addWidget(self.btn_local)
        buttons.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self._update_controls()
        fit_to_screen(self)
        QTimer.singleShot(0, self.compare)

    def _selected(self):
        item = self.tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else None

    def _update_controls(self):
        self.btn_compare.setEnabled(not self._busy)
        self.recursive.setEnabled(not self._busy)
        row = self._selected()
        editable = bool(row and row.get("remote") and row["remote"]["kind"] == "file")
        self.btn_edit.setEnabled(not self._busy and editable)
        self.btn_local.setEnabled(not self._busy and editable and bool(row.get("local") and row["local"]["kind"] == "file"))

    def compare(self):
        if self._busy:
            return
        self.tree.clear()
        self.status.setText("Comparing folder contents…")
        recursive = self.recursive.isChecked()
        self._work("compare", lambda: self.backend.compare_directory(self.local_root, self.remote_root, recursive))

    def _finished(self, operation, rows, error):
        self._busy = ""
        if error is not None:
            self.status.setText(str(error))
        else:
            for row in rows:
                a, b = row["local"], row["remote"]
                item = QTreeWidgetItem([row["relative"], row["state"],
                                       str(a["size"]) if a and a["kind"] == "file" else "",
                                       str(b["size"]) if b and b["kind"] == "file" else ""])
                item.setData(0, Qt.ItemDataRole.UserRole, row)
                item.setToolTip(1, row["state"])
                color = "#4ade80" if row["state"] == "Identical" else "#fbbf24" if row["state"] != "Folder" else "#94a3b8"
                item.setForeground(1, QColor(color))
                self.tree.addTopLevelItem(item)
            changed = sum(row["state"] not in ("Identical", "Folder") for row in rows)
            self.status.setText(f"{len(rows)} entries · {changed} different, missing, or needing review")
        self._update_controls()

    def _open_editor(self, use_local):
        row = self._selected()
        if not row or not row["remote"] or row["remote"]["kind"] != "file":
            return
        dialog = RemoteFileEditor(self.backend, row["remote_path"], self,
                                  draft_path=row["local_path"] if use_local else None)
        dialog.saved.connect(lambda _path: self.compare())
        dialog.exec()
