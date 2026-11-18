"""Ctrl+K — find a command and run it, without reaching for the mouse.

A snippet library you have to open a panel to browse is a library you stop
using.  The palette is the same commands reached the way a person actually
reaches for them: press a key, type three letters of what you meant, press
Enter.

Everything it offers goes through :mod:`app.snippets`, so a command can refer
to the session it is about to run in — ``{host}``, ``{user}``, ``{cwd}`` are
filled from the live session, and anything else it asks for is prompted for
with the finished command shown underneath, because the moment to catch a
mistake is before it has been sent to a machine.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QVBoxLayout, QWidget,
)

from app import snippets as snippet_engine
from app.bash_commands_library import get_bash_command_library

#: How many matches the list shows. Beyond this nobody is reading, they are
#: typing more letters.
MAX_RESULTS = 60


def palette_entries(vault=None, include_library: bool = True) -> list:
    """Everything the palette can run: the user's snippets first.

    Their own snippets come first because they wrote them for this estate;
    the bundled library is the long tail behind them.
    """
    entries = []
    try:
        saved = list(vault.snippets) if vault is not None else []
    except Exception:
        saved = []
    for snippet in saved:
        if not isinstance(snippet, dict):
            continue
        command = str(snippet.get("command", "") or "")
        if not command.strip():
            continue
        entries.append({
            "name": str(snippet.get("name", "") or command[:40]),
            "command": command,
            "description": str(snippet.get("description", "") or ""),
            "category": "Snippet",
            "source": "snippet",
        })

    if include_library:
        for command in get_bash_command_library():
            entries.append({
                "name": str(command.get("name", "")),
                "command": str(command.get("command", "")),
                "description": str(command.get("description", "")),
                "category": str(command.get("category", "")),
                "source": "library",
                "copy_only": bool(command.get("copy_only")),
                "requires_root": bool(command.get("requires_root")),
            })
    return entries


def session_context(widget) -> dict:
    """What a terminal-ish widget can tell a snippet about where it is."""
    terminal = widget
    if hasattr(widget, "focused_terminal"):
        terminal = widget.focused_terminal()
    if terminal is None:
        return {}
    return {"conn": getattr(terminal, "connection_info", None),
            "cwd": getattr(terminal, "remote_cwd", "")}


def fill_command(command, session=None, parent=None):
    """Resolve a command's placeholders, asking for whatever is left.

    Returns the finished command, or None if the user backed out — which is
    also what happens when a value from the estate could turn one command
    into two.
    """
    command = str(command or "")
    if not command.strip():
        return None

    session = session or {}
    values = snippet_engine.session_values(session.get("conn"),
                                           session.get("cwd", ""))
    values = {name: value for name, value in values.items() if value}
    try:
        filled = snippet_engine.render(command, values)
    except snippet_engine.SnippetError:
        return None

    outstanding = snippet_engine.placeholders_in(filled)
    if not outstanding:
        return filled

    dialog = PlaceholderDialog(filled, outstanding, parent=parent)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    if snippet_engine.missing_placeholders(filled, dialog.values()):
        return None
    return snippet_engine.render(filled, dialog.values(), trusted=outstanding)

from app.widgets import set_variant

class PlaceholderDialog(QDialog):
    """Ask for the values a snippet needs before it runs."""

    def __init__(self, command: str, names, defaults=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fill in the command")
        self.setMinimumWidth(520)
        self._command = str(command or "")
        self._names = list(names or [])
        self._fields = {}
        defaults = defaults or {}

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        form = QFormLayout()
        for name in self._names:
            field = QLineEdit(str(defaults.get(name, "") or ""))
            field.setPlaceholderText(
                snippet_engine.SESSION_PLACEHOLDERS.get(name, f"value for {name}"))
            field.textChanged.connect(self._update_preview)
            form.addRow(f"{name}:", field)
            self._fields[name] = field
        layout.addLayout(form)

        layout.addWidget(QLabel("This will run:"))
        self.preview = QLabel()
        self.preview.setObjectName("palette_preview")
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview.setStyleSheet(
            "background: rgba(0,0,0,0.35); border-radius: 6px; padding: 8px;"
            " font-family: Consolas, monospace; color: #e2e8f0;")
        layout.addWidget(self.preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Run")
        set_variant(buttons.button(QDialogButtonBox.StandardButton.Ok), "primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._update_preview()
        if self._names:
            self._fields[self._names[0]].setFocus()

    def values(self) -> dict:
        return {name: field.text() for name, field in self._fields.items()}

    def rendered(self) -> str:
        """The command with the boxes filled in, or as far as it gets."""
        try:
            return snippet_engine.render(self._command, self.values(),
                                         trusted=self._names)
        except snippet_engine.SnippetError as exc:
            return str(exc)

    def _update_preview(self):
        self.preview.setText(self.rendered() or " ")


class CommandPalette(QDialog):
    """A search box over every command RPMote can run here."""

    command_chosen = pyqtSignal(str)

    def __init__(self, entries, session=None, parent=None, title="Run a command"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(680, 460)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        self._entries = [entry for entry in (entries or [])
                         if isinstance(entry, dict) and entry.get("command")]
        self._session = session or {}
        self._results = []

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Type part of a command…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refilter)
        self.search.installEventFilter(self)
        layout.addWidget(self.search)

        self.results = QListWidget()
        self.results.setAlternatingRowColors(False)
        self.results.itemActivated.connect(lambda _item: self._accept_current())
        self.results.currentRowChanged.connect(self._update_preview)
        layout.addWidget(self.results, 1)

        row = QHBoxLayout()
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet(
            "background: rgba(0,0,0,0.35); border-radius: 6px; padding: 8px;"
            " font-family: Consolas, monospace; color: #e2e8f0;")
        row.addWidget(self.preview, 1)
        layout.addLayout(row)

        self.hint = QLabel("↑↓ to choose · Enter to run · Esc to close")
        self.hint.setStyleSheet("color: #94a3b8; font-size: 11px;")
        layout.addWidget(self.hint)

        self._refilter("")

    # ── Filtering ───────────────────────────────────────────────────

    @property
    def results_count(self) -> int:
        return len(self._results)

    def current_entry(self):
        row = self.results.currentRow()
        if 0 <= row < len(self._results):
            return self._results[row]
        return None

    def _refilter(self, text: str):
        self._results = [entry for entry, _score, _positions
                         in snippet_engine.rank(text, self._entries)][:MAX_RESULTS]
        self.results.clear()
        for entry in self._results:
            label = entry.get("name") or entry.get("command", "")
            category = entry.get("category") or ""
            item = QListWidgetItem(f"{label}    ·  {category}" if category
                                   else label)
            item.setToolTip(entry.get("command", ""))
            self.results.addItem(item)
        if self._results:
            self.results.setCurrentRow(0)
        self._update_preview()

    def _update_preview(self, *_args):
        entry = self.current_entry()
        if entry is None:
            self.preview.setText("No command matches that.")
            return
        self.hint.setText("↑↓ to choose · Enter to review template · Esc to close" if entry.get("copy_only")
                          else "↑↓ to choose · Enter to run · Esc to close")
        self.preview.setText(self.preview_text(entry))

    def preview_text(self, entry) -> str:
        """The command as it will run, with what we already know filled in."""
        command = str(entry.get("command", ""))
        if entry.get("copy_only"):
            return f"Cron template — review and copy into a crontab.\n\n{command}"
        try:
            filled = snippet_engine.render(command, self._session_values())
        except snippet_engine.SnippetError as exc:
            return str(exc)
        outstanding = snippet_engine.placeholders_in(filled)
        if outstanding:
            return f"{filled}\n\nWill ask for: {', '.join(outstanding)}"
        return filled

    def _session_values(self) -> dict:
        conn = self._session.get("conn") if isinstance(self._session, dict) else None
        cwd = self._session.get("cwd", "") if isinstance(self._session, dict) else ""
        values = snippet_engine.session_values(conn, cwd)
        # A blank is not an answer: drop it so the user is asked instead of
        # having "{cwd}" quietly become nothing.
        return {name: value for name, value in values.items() if value}

    # ── Choosing ────────────────────────────────────────────────────

    def eventFilter(self, obj, event):
        """Let the arrow keys drive the list while the caret stays in the box."""
        if obj is self.search and isinstance(event, QKeyEvent) \
                and event.type() == QKeyEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up,
                               Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                self.results.keyPressEvent(event)
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._accept_current()
                return True
        return super().eventFilter(obj, event)

    def resolve(self, entry):
        """The finished command for *entry*, or None if the user backed out."""
        command = str((entry or {}).get("command", ""))
        if (entry or {}).get("copy_only"):
            from app.cron_template_dialog import CronTemplateDialog
            def open_crontab(command):
                self.command_chosen.emit(command)
                self.accept()
            CronTemplateDialog(entry, self, open_crontab=open_crontab).exec()
            return None
        if not command:
            return None
        try:
            snippet_engine.render(command, self._session_values())
        except snippet_engine.SnippetError as exc:
            # Say why rather than opening a prompt for a command that can
            # never be built from this session.
            self.preview.setText(str(exc))
            return None
        return fill_command(command, self._session, parent=self)

    def _accept_current(self):
        entry = self.current_entry()
        if entry is None:
            return
        command = self.resolve(entry)
        if not command:
            return
        self.command_chosen.emit(command)
        self.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)
