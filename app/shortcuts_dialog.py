"""
Keyboard shortcuts reference — searchable, and short enough for any screen.

It used to be a message box 832 px tall, which put its OK button below the
taskbar on a 768-px-high laptop.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QLabel, QLineEdit, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout,
)

from app import icons
from app.widgets import fit_to_screen

#: (section, keys, what it does). Keep in step with the actions themselves.
SHORTCUTS = (
    ("Anywhere", "Ctrl+P  or  Ctrl+Shift+Space", "Go to a connection or command"),
    ("Anywhere", "Ctrl+N", "New connection"),
    ("Anywhere", "Ctrl+Shift+N", "Show / hide Quick Connect"),
    ("Anywhere", "Ctrl+,", "Settings"),
    ("Anywhere", "Ctrl+Tab  /  Ctrl+Shift+Tab", "Next / previous tab"),
    ("Anywhere", "Ctrl+Shift+W", "Close the current tab (also inside a terminal)"),
    ("Anywhere", "Ctrl+W", "Close the current tab (outside a terminal)"),
    ("Anywhere", "Ctrl+B", "Show / hide the Connections sidebar"),
    ("Anywhere", "Ctrl+Shift+S", "Show / hide the Library panel"),
    ("Anywhere", "Ctrl+T", "Next theme (View › Theme picks one)"),
    ("Anywhere", "Ctrl+K", "Run a command or snippet in the current session"),
    ("Anywhere", "Ctrl+Shift+B", "Broadcast session (type into many machines at once)"),
    ("Anywhere", "Ctrl+Shift+P", "Saved passwords"),
    ("Anywhere", "Ctrl+Shift+L", "Lock the screen (sessions keep running)"),
    ("Anywhere", "Ctrl+L", "Lock the vault (closes sessions)"),
    ("Anywhere", "Ctrl+Q", "Quit"),
    ("Dashboard graph", "Ctrl+F", "Find a tenant or connection (↑↓ step, Enter go, Ctrl+Enter open)"),
    ("Dashboard graph", "Double-click", "Open the connection under the cursor"),
    ("Dashboard graph", "Right-click", "Menu for the tenant, connection, link or canvas under the cursor"),
    ("Dashboard graph", "Right-drag / middle-drag", "Pan (left-drag on empty canvas works too)"),
    ("Dashboard graph", "Wheel", "Zoom toward the cursor"),
    ("Dashboard graph", "Ctrl+=  /  Ctrl+-  /  Ctrl+0", "Zoom in / out / reset"),
    ("Dashboard graph", "Arrow keys", "Pan"),
    ("Dashboard graph", "Alt+drag", "Draw a link between any two nodes"),
    ("Dashboard graph", "Delete", "Remove the selected link"),
    ("Dashboard graph", "Ctrl+Shift+F", "Fit the graph to the view"),
    ("Dashboard graph", "Ctrl+Shift+R", "Reset the view"),
    ("Dashboard graph", "Ctrl+Shift+G", "Link mode"),
    ("Dashboard graph", "Ctrl+Shift+H", "Check every connection's reachability now"),
    ("Dashboard graph", "Esc", "Clear the lens, cancel link mode, close search"),
    ("Terminal", "Ctrl+C", "Copy the selection — or interrupt when nothing is selected"),
    ("Terminal", "Ctrl+Shift+C  /  Ctrl+Shift+V", "Copy / paste"),
    ("Terminal", "Ctrl+V", "Paste"),
    ("Terminal", "Ctrl+Shift+A", "Select everything"),
    ("Terminal", "Ctrl+F", "Search the scrollback"),
    ("Terminal", "Ctrl+Shift+↑  /  Ctrl+Shift+↓", "Jump to the previous / next prompt"),
    ("Terminal", "Ctrl+Shift+O", "Copy the last command's output"),
    ("Terminal", "Shift+PgUp  /  Shift+PgDn", "Scroll the history"),
    ("Terminal", "Ctrl++  /  Ctrl+-", "Larger / smaller text"),
    ("Terminal", "Ctrl+K", "Command palette (Ctrl+Shift+K sends the shell's own ^K)"),
    ("Terminal", "Ctrl+W, Ctrl+L, Ctrl+B…", "Plain Ctrl+letter always goes to the shell"),
    ("Terminal", "Drag a file in", "Upload it to the directory the shell is in"),
)


class ShortcutsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard Shortcuts")
        self.setWindowIcon(icons.icon("keyboard"))
        self.setMinimumSize(560, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        intro = QLabel("Inside a terminal, plain Ctrl+letter belongs to the shell; "
                       "Ctrl+Shift chords and Ctrl+Tab belong to RPMote.")
        intro.setWordWrap(True)
        intro.setProperty("role", "hint")
        layout.addWidget(intro)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter — type a key or an action")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Shortcut", "What it does"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        layout.addWidget(self.tree, 1)

        self._sections: dict[str, QTreeWidgetItem] = {}
        for section, keys, action in SHORTCUTS:
            parent_item = self._sections.get(section)
            if parent_item is None:
                parent_item = QTreeWidgetItem([section, ""])
                font = parent_item.font(0)
                font.setBold(True)
                parent_item.setFont(0, font)
                parent_item.setFirstColumnSpanned(True)
                parent_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.tree.addTopLevelItem(parent_item)
                self._sections[section] = parent_item
            item = QTreeWidgetItem([keys, action])
            parent_item.addChild(item)
        self.tree.expandAll()
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setFirstColumnSpanned(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.resize(720, 640)
        fit_to_screen(self)
        self.search.setFocus()

    def _filter(self, text: str):
        needle = text.strip().casefold()
        for i in range(self.tree.topLevelItemCount()):
            section = self.tree.topLevelItem(i)
            shown = 0
            for j in range(section.childCount()):
                item = section.child(j)
                hay = f"{item.text(0)} {item.text(1)}".casefold()
                visible = not needle or all(part in hay for part in needle.split())
                item.setHidden(not visible)
                shown += visible
            section.setHidden(shown == 0)
