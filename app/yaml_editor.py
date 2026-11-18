"""YAML typing support that preserves the user's document and scalar contents."""

import math
import re

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QPlainTextEdit

from app.yaml_support import normalize_indentation_tabs


def _without_comment(text):
    """Ignore comments outside quoted strings when deciding the next indent."""
    quote = None
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'" and (index == 0 or text[index - 1] in " :[{,-"):
            quote = char
        elif char == "#" and (index == 0 or text[index - 1].isspace()):
            return text[:index].rstrip()
    return text.rstrip()


def _mapping_colon(text):
    quote, escaped = None, False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
        elif quote == '"' and char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'" and (index == 0 or text[index - 1] in " :[{,-"):
            quote = char
        elif char == ":" and (index + 1 == len(text) or text[index + 1].isspace()):
            return index
    return None


def _scalar_indicator(content):
    return re.search(r"(?:^|:\s+|-\s+)[|>]([+-]?[1-9]?[+-]?)$", content)


class YamlTextEdit(QPlainTextEdit):
    """Space-only Tab, block indent/dedent, and contextual Enter for YAML files."""
    indentation_changed = pyqtSignal(int)

    def __init__(self, yaml=False, parent=None):
        super().__init__(parent)
        self.yaml = yaml
        self.indent_width = 2

    def configure_indentation(self, text):
        # Infer the existing indent unit, excluding block-scalar bodies whose
        # leading spaces can be meaningful content. Default new files to two.
        levels = []
        scalar_indent = None
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            spaces = len(line) - len(line.lstrip(" "))
            if scalar_indent is not None:
                if spaces > scalar_indent:
                    continue
                scalar_indent = None
            content = _without_comment(line.strip())
            if _scalar_indicator(content):
                scalar_indent = spaces
            if spaces:
                levels.append(spaces)
        width = 0
        for level in levels:
            width = math.gcd(width, level)
        if width == 1 and levels and min(levels) > 1:
            width = 2  # Inconsistent wider indents should not imply one-space style.
        previous = self.indent_width
        self.indent_width = width if 1 <= width <= 8 else 2
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * self.indent_width)
        if previous != self.indent_width:
            self.indentation_changed.emit(self.indent_width)

    def insertFromMimeData(self, source):
        if not self.yaml or not source.hasText():
            super().insertFromMimeData(source)
            return
        if self.isReadOnly():
            return
        cursor = self.textCursor()
        probe = QTextCursor(cursor)
        probe.setPosition(cursor.selectionStart())
        block = probe.block()
        probe.setPosition(block.position(), QTextCursor.MoveMode.KeepAnchor)
        prefix = probe.selectedText()
        indentation = len(prefix) - len(prefix.lstrip(" "))
        text = source.text().replace("\r\n", "\n").replace("\r", "\n")
        empty = not self.toPlainText()
        if empty:
            text = text.removeprefix("\ufeff")
        if self._inside_scalar(block, indentation):
            text = text.replace("\n", "\n" + " " * indentation)
        elif not prefix.strip(" \t"):
            if empty:
                self.configure_indentation(text)
            text = normalize_indentation_tabs(text, self.indent_width)
            lines = text.split("\n")
            margins = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
            margin = min(margins, default=0)
            base = " " * len(prefix.expandtabs(self.indent_width))
            text = "\n".join((base if index else "") + line[min(margin, len(line) - len(line.lstrip(" "))):]
                             for index, line in enumerate(lines))
        cursor.beginEditBlock()
        cursor.insertText(text)
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        if empty:
            self.configure_indentation(self.toPlainText())

    def replace_text(self, text):
        """Apply a formatted draft as one undoable edit."""
        if text == self.toPlainText():
            return
        cursor = self.textCursor()
        position = cursor.position()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(text)
        cursor.endEditBlock()
        cursor.setPosition(min(position, self.document().characterCount() - 1))
        self.setTextCursor(cursor)

    def keyPressEvent(self, event):
        if not self.yaml or self.isReadOnly():
            super().keyPressEvent(event)
            return
        key, modifiers = event.key(), event.modifiers()
        if modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier):
            super().keyPressEvent(event)
            return
        cursor = self.textCursor()
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
            backwards = key == Qt.Key.Key_Backtab or bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
            if cursor.hasSelection() or backwards:
                self._indent_lines(backwards)
            else:
                cursor.insertText(" " * (self.indent_width - cursor.positionInBlock() % self.indent_width))
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not modifiers:
            prefix = cursor.block().text()[:cursor.positionInBlock()]
            indentation = len(prefix) - len(prefix.lstrip(" "))
            content = _without_comment(prefix.strip())
            sequence = re.match(r"-\s+", content)
            # A sequence mapping's nested value starts after '- ', plus the
            # indent unit: '- key:' needs more than a plain 'key:'.
            scalar = _scalar_indicator(content)
            colon = _mapping_colon(content)
            if not self._inside_scalar(cursor.block(), indentation):
                if sequence and colon is not None:
                    indentation += sequence.end()
                if colon == len(content) - 1 or scalar:
                    digit = re.search(r"[1-9]", scalar.group(1)) if scalar else None
                    indentation += int(digit.group()) if digit else self.indent_width
            cursor.beginEditBlock()
            cursor.insertText("\n" + " " * indentation)
            cursor.endEditBlock()
            self.setTextCursor(cursor)
            return
        if key == Qt.Key.Key_Backspace and not cursor.hasSelection() and not modifiers:
            prefix = cursor.block().text()[:cursor.positionInBlock()]
            if prefix and not prefix.strip(" "):
                count = (len(prefix) - 1) % self.indent_width + 1
                cursor.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor, count)
                cursor.removeSelectedText()
                return
        super().keyPressEvent(event)

    def _inside_scalar(self, block, indentation):
        previous = block.previous()
        while previous.isValid():
            line = previous.text()
            if line.strip() and not line.lstrip().startswith("#"):
                spaces = len(line) - len(line.lstrip(" "))
                if spaces < indentation and _scalar_indicator(_without_comment(line.strip())):
                    return True
                if spaces == 0 or spaces < indentation and _mapping_colon(_without_comment(line.strip())) is not None:
                    return False
            previous = previous.previous()
        return False

    def _indent_lines(self, backwards):
        cursor = self.textCursor()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        first = self.document().findBlock(start)
        last = self.document().findBlock(end)
        if end > start and end == last.position():
            last = last.previous()
        blocks = []
        block = first
        while block.isValid() and block.position() <= last.position():
            blocks.append(block)
            block = block.next()
        edit = QTextCursor(self.document())
        edit.beginEditBlock()
        for block in reversed(blocks):
            edit.setPosition(block.position())
            if backwards:
                leading = len(block.text()) - len(block.text().lstrip(" "))
                count = min(self.indent_width, leading)
                edit.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.KeepAnchor, count)
                edit.removeSelectedText()
            else:
                edit.insertText(" " * self.indent_width)
        edit.endEditBlock()
        if start != end:
            cursor.setPosition(first.position())
            cursor.setPosition(last.position() + len(last.text()), QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cursor)

