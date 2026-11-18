"""Review and copy a crontab entry without executing its schedule in a shell."""

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout

from app.widgets import fit_to_screen, set_variant


class CronTemplateDialog(QDialog):
    def __init__(self, entry, parent=None, open_crontab=None):
        super().__init__(parent)
        self.setWindowTitle("Cron job template — " + entry["name"])
        self.resize(860, 510)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        title = QLabel(entry["name"])
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setStyleSheet("font-size:16px;font-weight:600;")
        layout.addWidget(title)
        description = QLabel(entry.get("description", ""))
        description.setTextFormat(Qt.TextFormat.PlainText)
        description.setWordWrap(True)
        layout.addWidget(description)
        help_text = QLabel("minute   hour   day-of-month   month   day-of-week   command\nEdit the example, copy it, and paste it into crontab -e. Times use the remote server's timezone.\nUser crontabs have no username field. Cron uses /bin/sh; verify executable paths and writable logs.")
        help_text.setWordWrap(True)
        help_text.setProperty("role", "hint")
        layout.addWidget(help_text)
        self.editor = QPlainTextEdit(entry["command"])
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.editor.setFont(QFont("Consolas", 11))
        layout.addWidget(self.editor, 1)
        self.status = QLabel("Root crontab example: install as root, without a username field." if entry.get("requires_root") else "Current-user crontab example. Replace example paths before installing.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.btn_open = QPushButton("Open root crontab" if entry.get("requires_root") else "Open user crontab")
        self.btn_open.setEnabled(open_crontab is not None)
        def open_editor():
            self.accept()
            open_crontab("sudo crontab -e" if entry.get("requires_root") else "crontab -e")
        self.btn_open.clicked.connect(open_editor)
        row.addWidget(self.btn_open)
        row.addStretch()
        copy = set_variant(QPushButton("Copy edited entry"), "primary")
        copy.clicked.connect(self.copy_entry)
        row.addWidget(copy)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)
        fit_to_screen(self)

    def copy_entry(self):
        text = self.editor.toPlainText().strip()
        if not text:
            self.status.setText("Enter a cron job before copying.")
            return
        QApplication.clipboard().setText(text + "\n")
        self.status.setText("Copied. Paste the entry into the remote crontab editor and save it there.")
