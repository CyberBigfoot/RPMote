"""
RPMote Snippets Panel — Management and execution of command snippets + Bash library.
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLabel, QLineEdit, QDialog, QTextEdit, QMenu,
    QDockWidget, QTabWidget, QComboBox
)
from PyQt6.QtCore import Qt, pyqtSignal, QSize

from app import icons
from app.bash_commands_library import get_bash_command_library
from app.remote_file_browser import RemoteFileBrowser
from app.snippets import SESSION_PLACEHOLDERS
from app.widgets import icon_button, set_variant

class SnippetDialog(QDialog):
    """Dialog to add or edit a command snippet."""
    def __init__(self, snippet=None, parent=None):
        super().__init__(parent)
        self.snippet = snippet or {"name": "", "command": ""}
        self.setWindowTitle("Edit Snippet" if snippet else "New Snippet")
        self.setMinimumWidth(400)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        layout.addWidget(QLabel("Snippet Name:"))
        self.name_edit = QLineEdit(self.snippet["name"])
        self.name_edit.setPlaceholderText("e.g., Docker Logs")
        layout.addWidget(self.name_edit)

        layout.addWidget(QLabel("Command:"))
        self.cmd_edit = QTextEdit(self.snippet["command"])
        self.cmd_edit.setPlaceholderText("Enter command (use \\r for enter)")
        self.cmd_edit.setFixedHeight(100)
        layout.addWidget(self.cmd_edit)

        # What turns a saved command into one worth saving.
        known = ", ".join("{%s}" % name for name in SESSION_PLACEHOLDERS)
        self.placeholder_hint = QLabel(
            f"Placeholders: {known} are filled in from the session. "
            "Anything else — {service}, {file} — is asked for when it runs.")
        self.placeholder_hint.setWordWrap(True)
        self.placeholder_hint.setProperty("role", "hint")
        layout.addWidget(self.placeholder_hint)

        btns = QHBoxLayout()
        self.btn_save = QPushButton("Save snippet")
        set_variant(self.btn_save, "primary")
        self.btn_save.clicked.connect(self.accept)
        self.btn_cancel = QPushButton("Cancel")
        self.btn_cancel.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(self.btn_cancel)
        btns.addWidget(self.btn_save)
        layout.addLayout(btns)

    def get_result(self):
        return {
            "name": self.name_edit.text(),
            "command": self.cmd_edit.toPlainText()
        }

class SnippetsPanel(QDockWidget):
    """Sidebar panel for command snippets."""
    execute_requested = pyqtSignal(str)

    def __init__(self, vault, parent=None):
        super().__init__("Library", parent)
        self.setObjectName("library_dock")
        # The panel draws its own header; the dock title only repeated it.
        self.setTitleBarWidget(QWidget())
        self.vault = vault
        self._bash_commands = get_bash_command_library()
        self.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable |
                         QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.setMinimumWidth(240)
        self._setup_ui()
        self.refresh()
        self.refresh_bash_commands()

    def _setup_ui(self):
        container = QWidget()
        container.setObjectName("snippets_panel_container")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # Header
        header = QHBoxLayout()
        header.setSpacing(6)
        title_icon = QLabel()
        title_icon.setPixmap(icons.pixmap("library", 16, "#22d3ee"))
        header.addWidget(title_icon)
        title = QLabel("Library")
        title.setStyleSheet("font-weight: 700; font-size: 14px;")
        header.addWidget(title)
        header.addStretch()

        self.btn_add = icon_button("plus", "New snippet")
        self.btn_add.clicked.connect(self._add_snippet)
        header.addWidget(self.btn_add)

        self.btn_collapse = icon_button("chevron-right", "Hide the Library (Ctrl+Shift+S)")
        self.btn_collapse.clicked.connect(self.hide)
        header.addWidget(self.btn_collapse)

        layout.addLayout(header)

        # Shared list style
        list_style = """
            QListWidget { border: none; background: transparent; outline: 0; }
            QListWidget::item { 
                padding: 10px; 
                border-bottom: 1px solid rgba(255,255,255,0.05);
                border-radius: 6px;
                margin-bottom: 4px;
            }
            QListWidget::item:selected { background: palette(highlight); color: palette(highlighted-text); }
            QListWidget::item:hover { background: rgba(255,255,255,0.05); }
        """

        # Tabbed library content
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(False)
        self.tabs.setStyleSheet("""
            QTabWidget::pane { border: none; }
            QTabBar::tab {
                padding: 6px 10px;
                font-size: 12px;
            }
        """)

        # ── Snippets tab ──────────────────────────────────────────────
        snippets_tab = QWidget()
        snippets_layout = QVBoxLayout(snippets_tab)
        snippets_layout.setContentsMargins(0, 0, 0, 0)
        snippets_layout.setSpacing(8)

        self.snippets_search = QLineEdit()
        self.snippets_search.setPlaceholderText("Filter snippets…")
        self.snippets_search.setClearButtonEnabled(True)
        self.snippets_search.addAction(icons.icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.snippets_search.textChanged.connect(self.refresh)
        snippets_layout.addWidget(self.snippets_search)

        self.list = QListWidget()
        self.list.setStyleSheet(list_style)
        self.list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        snippets_layout.addWidget(self.list)

        # ── Bash commands tab ─────────────────────────────────────────
        bash_tab = QWidget()
        bash_layout = QVBoxLayout(bash_tab)
        bash_layout.setContentsMargins(0, 0, 0, 0)
        bash_layout.setSpacing(8)

        bash_filters = QHBoxLayout()
        self.bash_search = QLineEdit()
        self.bash_search.setPlaceholderText("Search command, category or description…")
        self.bash_search.setClearButtonEnabled(True)
        self.bash_search.addAction(icons.icon("search"), QLineEdit.ActionPosition.LeadingPosition)
        self.bash_search.textChanged.connect(self.refresh_bash_commands)
        bash_filters.addWidget(self.bash_search)

        self.bash_category = QComboBox()
        self.bash_category.setMinimumWidth(130)
        self.bash_category.currentTextChanged.connect(self.refresh_bash_commands)
        bash_filters.addWidget(self.bash_category)
        bash_layout.addLayout(bash_filters)

        self.bash_list = QListWidget()
        self.bash_list.setStyleSheet(list_style)
        self.bash_list.itemDoubleClicked.connect(self._on_bash_item_clicked)
        self.bash_list.itemActivated.connect(self._on_bash_item_clicked)
        self.bash_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.bash_list.customContextMenuRequested.connect(self._bash_context_menu)
        bash_layout.addWidget(self.bash_list)

        self.bash_hint = QLabel("Double-click a command (or press Enter) to run it in the terminal.")
        self.bash_hint.setProperty("role", "hint")
        self.bash_hint.setWordWrap(True)
        bash_layout.addWidget(self.bash_hint)

        # ── Files tab (Remote SFTP browser) ─────────────────────────
        self.file_browser = RemoteFileBrowser()

        self.tabs.addTab(snippets_tab, icons.icon("code"), "Snippets")
        self.tabs.addTab(bash_tab, icons.icon("terminal"), "Commands")
        self.tabs.addTab(self.file_browser, icons.icon("folder"), "Files")
        self.tabs.setTabToolTip(1, "Linux commands and cron job templates")
        layout.addWidget(self.tabs)

        self.setWidget(container)
        self._init_bash_categories()

    def refresh(self):
        """Reload snippets from vault."""
        self.list.clear()
        filter_text = self.snippets_search.text().strip().lower() if hasattr(self, "snippets_search") else ""
        try:
            for snip in self.vault.snippets:
                if filter_text:
                    blob = f"{snip.get('name', '')} {snip.get('command', '')}".lower()
                    if filter_text not in blob:
                        continue
                item = QListWidgetItem(snip["name"])
                item.setData(Qt.ItemDataRole.UserRole, snip)
                # Subtitle info
                cmd_preview = snip["command"].split('\n')[0][:30]
                if len(snip["command"]) > 30: cmd_preview += "..."
                item.setToolTip(snip["command"])
                item.setText(f"{snip['name']}\n{cmd_preview}")
                self.list.addItem(item)
        except Exception as e:
            print(f"Error refreshing snippets: {e}")

    def _init_bash_categories(self):
        categories = sorted({cmd["category"] for cmd in self._bash_commands})
        self.bash_category.clear()
        self.bash_category.addItem("All Categories")
        for c in categories:
            self.bash_category.addItem(c)

    def refresh_bash_commands(self):
        self.bash_list.clear()
        text = self.bash_search.text().strip().lower() if hasattr(self, "bash_search") else ""
        chosen = self.bash_category.currentText() if hasattr(self, "bash_category") else "All Categories"
        grouped: dict[str, list[dict]] = {}

        for cmd in self._bash_commands:
            if chosen and chosen != "All Categories" and cmd["category"] != chosen:
                continue
            blob = f"{cmd['name']} {cmd['command']} {cmd.get('description', '')} {cmd['category']}".lower()
            if text and text not in blob:
                continue
            grouped.setdefault(cmd["category"], []).append(cmd)

        count = 0
        for category in sorted(grouped):
            header = QListWidgetItem(f"— {category} —")
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.bash_list.addItem(header)
            for cmd in sorted(grouped[category], key=lambda x: x["name"].lower()):
                subtitle = cmd["command"]
                if cmd.get("copy_only"):
                    fields = subtitle.split(None, 5)
                    schedule = fields[0] if subtitle.startswith("@") else " ".join(fields[:5])
                    subtitle = f"{schedule} · Review and copy" + (" · root" if cmd.get("requires_root") else "")
                item = QListWidgetItem(f"{cmd['name']}\n{subtitle}")
                item.setData(Qt.ItemDataRole.UserRole, cmd)
                desc = cmd.get("description", "")
                item.setToolTip(f"{desc}\n\n{cmd['command']}" if desc else cmd["command"])
                self.bash_list.addItem(item)
                count += 1

        self.bash_hint.setText(
            f"Double-click to run a command or review a cron template. "
            f"Showing {count}.")

    def _add_snippet(self):
        dialog = SnippetDialog(parent=self)
        if dialog.exec():
            data = dialog.get_result()
            self.vault.add_snippet(data)
            self.refresh()

    def _edit_snippet(self, item):
        snip = item.data(Qt.ItemDataRole.UserRole)
        dialog = SnippetDialog(snippet=snip, parent=self)
        if dialog.exec():
            data = dialog.get_result()
            self.vault.update_snippet(snip["id"], data)
            self.refresh()

    def _delete_snippet(self, item):
        snip = item.data(Qt.ItemDataRole.UserRole)
        from app.dialogs import confirm
        if not confirm(self, "Delete snippet", f"Delete the snippet “{snip.get('name', '')}”?",
                       "Delete", danger=True):
            return
        self.vault.delete_snippet(snip["id"])
        self.refresh()

    def _on_item_double_clicked(self, item):
        snip = item.data(Qt.ItemDataRole.UserRole)
        # Ensure it ends with \r if it doesn't have one and is meant to be executed
        cmd = snip["command"]
        if not cmd.endswith('\r') and not cmd.endswith('\n'):
            cmd += '\r'
        self.execute_requested.emit(cmd)

    def _on_bash_item_clicked(self, item):
        cmd_data = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(cmd_data, dict):
            return
        if cmd_data.get("copy_only"):
            from app.cron_template_dialog import CronTemplateDialog
            CronTemplateDialog(cmd_data, self,
                open_crontab=lambda command: self.execute_requested.emit(command + '\r')).exec()
            return
        cmd = cmd_data.get("command", "").strip()
        if not cmd:
            return
        if not cmd.endswith('\r') and not cmd.endswith('\n'):
            cmd += '\r'
        self.execute_requested.emit(cmd)

    def _context_menu(self, pos):
        item = self.list.itemAt(pos)
        if not item: return

        menu = QMenu(self)
        run_act = menu.addAction(icons.icon("play"), "Run in terminal")
        run_act.triggered.connect(lambda: self._on_item_double_clicked(item))
        
        menu.addSeparator()
        
        edit_act = menu.addAction(icons.icon("edit"), "Edit snippet…")
        edit_act.triggered.connect(lambda: self._edit_snippet(item))
        
        delete_act = menu.addAction(icons.icon("trash", "#f87171"), "Delete snippet…")
        delete_act.triggered.connect(lambda: self._delete_snippet(item))
        
        menu.exec(self.list.mapToGlobal(pos))

    def _bash_context_menu(self, pos):
        item = self.bash_list.itemAt(pos)
        if not item:
            return
        cmd = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(cmd, dict):
            return

        menu = QMenu(self)
        run_act = menu.addAction(icons.icon("edit") if cmd.get("copy_only") else icons.icon("play"),
                                 "Review cron template…" if cmd.get("copy_only") else "Run in terminal")
        run_act.triggered.connect(lambda: self._on_bash_item_clicked(item))

        copy_act = menu.addAction(icons.icon("copy"), "Copy cron entry" if cmd.get("copy_only") else "Copy command")
        copy_act.triggered.connect(lambda: self._copy_command(cmd.get("command", "")))
        menu.exec(self.bash_list.mapToGlobal(pos))

    def _copy_command(self, command: str):
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(command)
