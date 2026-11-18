"""The terminal display: a cell grid painted directly, not a rich-text document.

The view this replaces drove a ``QPlainTextEdit`` by mutating a
``QTextDocument`` — for every row that changed, delete the block's text and
insert it again as styled runs. It worked, and it was carefully done, but it
was a rich-text engine being asked to behave like a fixed grid of cells. The
symptom that gave it away was the freeze: rendering had to stop for up to five
seconds while the mouse was down, because otherwise arriving output would move
the document under the pointer and the selection would land somewhere else.

Painting the cells directly turns most of that from hard into free. Selection
is a pair of grid coordinates, so output arriving mid-drag cannot disturb it
and nothing has to freeze. A repaint costs one pass over the rows actually on
screen rather than an edit to a document holding the entire history, so
``cat`` of a large file, or anything running under tmux, stops being expensive.
And several things that were unreachable through a document — block selection,
the cursor shapes programs ask for with DECSCUSR, hit-testing a hyperlink,
highlighting every search hit at once — become ordinary drawing.

The two coordinate systems here are worth keeping straight:

``doc index``    0 .. total_lines-1, indexing scrollback first and then the
                 live screen. What the scrollbar holds, and what the painter
                 walks.
``absolute``     doc index plus the number of lines already dropped off the
                 top of history. Stable while scrollback trims, which is why
                 selections and search hits are stored this way.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from PyQt6.QtCore import QPoint, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import (
    QColor, QFont, QFontMetricsF, QKeyEvent, QPainter, QPen,
)
from PyQt6.QtWidgets import QAbstractScrollArea, QApplication

from app.cells import PackedRow
from app.screen_buffer import CURSOR_BAR, CURSOR_BLOCK, CURSOR_UNDERLINE

#: Characters a double-click treats as part of one word. Wider than "letters
#: and digits" on purpose: the thing being double-clicked in a terminal is
#: usually a path, a URL or a package name, and breaking those at every dot
#: makes the gesture useless.
WORD_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "_-./~:@%+=,?&#"
)

_CTRL = Qt.KeyboardModifier.ControlModifier
_SHIFT = Qt.KeyboardModifier.ShiftModifier
_CTRL_SHIFT = _CTRL | _SHIFT
_MODIFIER_MASK = (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
                  | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier)

#: Chords the window keeps even while a terminal has focus. The split is the
#: one Windows Terminal makes: plain Ctrl+letter belongs to the shell
#: (Ctrl+W deletes a word, Ctrl+L clears, Ctrl+P is history), while these —
#: lock the screen, switch tab, open the window's own tools — belong to the
#: app. Before this, every one of them was typed into the remote shell.
APP_CHORDS = frozenset({
    (_CTRL_SHIFT, Qt.Key.Key_L),       # lock screen
    (_CTRL_SHIFT, Qt.Key.Key_P),       # saved passwords
    (_CTRL_SHIFT, Qt.Key.Key_N),       # quick connect
    (_CTRL_SHIFT, Qt.Key.Key_B),       # broadcast session
    (_CTRL_SHIFT, Qt.Key.Key_S),       # library panel
    (_CTRL_SHIFT, Qt.Key.Key_W),       # close tab
    (_CTRL_SHIFT, Qt.Key.Key_Space),   # go to anything
    (_CTRL, Qt.Key.Key_Tab),           # next tab
    (_CTRL_SHIFT, Qt.Key.Key_Tab),     # previous tab
    (_CTRL_SHIFT, Qt.Key.Key_Backtab),
    (_CTRL, Qt.Key.Key_Comma),         # settings
})


def is_app_chord(event) -> bool:
    """Whether a key event is one the window, not the shell, should get."""
    try:
        mods = event.modifiers() & _MODIFIER_MASK
        return (mods, Qt.Key(event.key())) in APP_CHORDS
    except Exception:
        return False


def _same_attrs(a, b) -> bool:
    """Whether two styles would paint identically.

    Written out field by field rather than as ``a == b`` because the dataclass
    comparison builds a tuple per cell, and this runs for every character on
    screen on every frame.
    """
    if a is b:
        return True
    return (a.fg == b.fg and a.bg == b.bg
            and a.bold == b.bold and a.dim == b.dim
            and a.italic == b.italic and a.underline == b.underline
            and a.reverse == b.reverse and a.link == b.link)


@dataclass
class SearchOptions:
    """How to interpret the text in the search box."""
    regex: bool = False
    case_sensitive: bool = False
    whole_word: bool = False


class TerminalSearch:
    """Finds every hit in the buffer, so they can all be highlighted at once.

    The old search handed the query to ``QPlainTextEdit.find()``, which knows
    how to move a cursor to the next hit and nothing else — no count, no
    regular expressions, no way to see the other matches, and no way to tell
    whether it had wrapped past the end.
    """

    def __init__(self):
        self.options = SearchOptions()
        self.query = ""
        #: (absolute_line, start_col, end_col), in reading order.
        self.matches: list = []
        self.by_line: dict = {}
        self.index = -1
        self.wrapped = False
        self.error = ""

    # ── Building ────────────────────────────────────────────────────

    def _pattern(self):
        if not self.query:
            return None
        flags = 0 if self.options.case_sensitive else re.IGNORECASE
        source = self.query if self.options.regex else re.escape(self.query)
        if self.options.whole_word:
            source = rf"\b(?:{source})\b"
        try:
            return re.compile(source, flags)
        except re.error as exc:
            self.error = str(exc)
            return None

    def run(self, buffer, query: str = None, options: SearchOptions = None):
        """Scan the whole buffer — history and screen — for the query."""
        if query is not None:
            self.query = query
        if options is not None:
            self.options = options
        self.error = ""
        self.matches = []
        self.by_line = {}
        self.index = -1
        self.wrapped = False

        pattern = self._pattern()
        if pattern is None or buffer is None:
            return self

        dropped = dropped_of(buffer)
        total = len(history_of(buffer)) + buffer.rows
        for doc_index in range(total):
            text = line_string(buffer, doc_index)
            if not text:
                continue
            spans = None
            for found in pattern.finditer(text):
                start, end = found.span()
                if end == start:
                    continue
                if spans is None:
                    spans = []
                spans.append((start, end))
                self.matches.append((dropped + doc_index, start, end))
            if spans:
                self.by_line[dropped + doc_index] = spans
        return self

    # ── Navigation ──────────────────────────────────────────────────

    @property
    def count(self) -> int:
        return len(self.matches)

    def current(self):
        if 0 <= self.index < len(self.matches):
            return self.matches[self.index]
        return None

    def select_nearest(self, absolute_line: int):
        """Point at the first hit at or after a line, without moving to it."""
        for position, match in enumerate(self.matches):
            if match[0] >= absolute_line:
                self.index = position
                return match
        self.index = 0 if self.matches else -1
        return self.current()

    def step(self, direction: int):
        """Move to the next (+1) or previous (-1) hit, wrapping round."""
        if not self.matches:
            self.wrapped = False
            return None
        if self.index < 0:
            self.index = 0 if direction > 0 else len(self.matches) - 1
            self.wrapped = False
            return self.current()
        nxt = self.index + direction
        self.wrapped = not 0 <= nxt < len(self.matches)
        self.index = nxt % len(self.matches)
        return self.current()

    def describe(self) -> str:
        """The label shown beside the search box."""
        if self.error:
            return "bad pattern"
        if not self.query:
            return ""
        if not self.matches:
            return "no matches"
        position = self.index + 1 if self.index >= 0 else 1
        text = f"{position} of {len(self.matches)}"
        return f"{text} (wrapped)" if self.wrapped else text


# ── Reading the buffer by document line ─────────────────────────────


def history_of(buffer) -> list:
    """The history lines the view should show.

    Empty while the alternate screen is up: vim and htop own the whole window
    and the shell's scrollback belongs to what was there before them, so
    showing it above would be showing two unrelated things at once.
    """
    if buffer is None or buffer._using_alt:
        return []
    return buffer._scrollback


def _row_extent(row) -> int:
    """Columns of a row worth drawing — trailing blanks are not.

    A live grid row is always full width, and most of a terminal is empty, so
    stopping at the last visible cell is most of the painting saved.
    """
    if isinstance(row, PackedRow):
        return row.extent
    end = len(row)
    while end > 0:
        cell = row[end - 1]
        if cell.char not in (" ", "") or cell.attrs.bg or cell.attrs.reverse:
            break
        end -= 1
    return end


def dropped_of(buffer) -> int:
    """Lines already discarded off the top — the doc-to-absolute offset.

    Zero while the alternate screen is up, because there is no history there
    for a document index to be offset from.
    """
    if buffer is None or buffer._using_alt:
        return 0
    return buffer.scrollback_total_lines - len(buffer._scrollback)


def has_output(buffer) -> bool:
    """True once the remote has put anything at all on the screen.

    Only the "connecting" sign uses this, and scrollback is the wrong test
    for it: a freshly connected shell prints a single prompt on row one and
    produces no history whatsoever, so a sign that waits for scrollback sits
    behind the live session until something scrolls it away.
    """
    if buffer is None:
        return False
    if buffer.scrollback_total_lines or buffer._using_alt:
        return True
    if buffer.cursor_row or buffer.cursor_col:
        return True
    return any(_row_extent(row) for row in buffer.grid)


def row_at(buffer, doc_index: int):
    """The row at a document index — history first, then the live screen."""
    if buffer is None or doc_index < 0:
        return None
    history = history_of(buffer)
    index = doc_index - len(history)
    if index < 0:
        return history[doc_index]
    if 0 <= index < len(buffer.grid):
        return buffer.grid[index]
    return None


def line_string(buffer, doc_index: int) -> str:
    """The characters of a document line, without trailing blanks."""
    row = row_at(buffer, doc_index)
    if row is None:
        return ""
    if isinstance(row, PackedRow):
        return row.text().rstrip()
    return "".join(cell.char for cell in row).rstrip()


class TerminalView(QAbstractScrollArea):
    """Paints a ScreenBuffer as a grid of cells."""

    selection_changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buffer = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFrameStyle(0)
        self.viewport().setMouseTracking(True)
        self.viewport().setAttribute(
            Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        # Colours. Replaced wholesale by set_colors when a theme is applied.
        self._fg = QColor("#e2e8f0")
        self._bg = QColor("#0a0e18")
        self._cursor_color = QColor("#e2e8f0")
        self._selection_color = QColor(120, 160, 255, 96)
        self._search_color = QColor(255, 214, 64, 80)
        self._current_search_color = QColor(255, 152, 0, 150)
        self._placeholder = ""

        # Font and the cell geometry it implies.
        self._font = QFont("Consolas", 10)
        self._fonts: dict = {}
        self._metrics = None
        self._fits_cache: dict = {}
        self._cell_w = 8.0
        self._cell_h = 16
        self._baseline = 12
        self._margin = 4

        # Selection, in absolute line coordinates so that history trimming
        # cannot slide it sideways.
        self._sel_anchor = None
        self._sel_head = None
        self._sel_block = False
        self._sel_mode = "char"
        self._selecting = False
        self._last_double_click = 0.0
        self._last_double_cell = None
        #: (line, start_col, end_col) first picked by a double or triple
        #: click, so a drag can keep it wholly inside the selection.
        self._sel_origin = None

        self.search = TerminalSearch()
        self._show_search = False

        self.follow_output = True
        self._programmatic_scroll = False
        #: History already discarded at the last repaint. Compared against the
        #: current figure so a view the user has parked stays on the same
        #: content while lines fall off the top.
        self._last_dropped = 0
        self._last_alt = False
        #: Where the cursor was painted last, so that moving it repaints the
        #: cell it left as well as the one it arrived at.
        self._last_cursor_row = 0
        self._cursor_blink_on = True
        self.cursor_enabled = True

        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        self._apply_font_metrics()

    # ── Wiring ──────────────────────────────────────────────────────

    def set_buffer(self, buffer):
        self._buffer = buffer
        self.clear_selection()
        self.update_scrollbars()
        self.viewport().update()

    @property
    def buffer(self):
        return self._buffer

    def set_placeholder(self, text: str):
        self._placeholder = text or ""
        self.viewport().update()

    def set_colors(self, fg, bg, cursor=None, selection=None):
        self._fg = QColor(fg)
        self._bg = QColor(bg)
        self._cursor_color = QColor(cursor or fg)
        if selection is not None:
            colour = QColor(selection)
            colour.setAlpha(110)
            self._selection_color = colour
        self.viewport().update()

    def set_terminal_font(self, font: QFont):
        self._font = QFont(font)
        self._fonts.clear()
        self._fits_cache.clear()
        self._apply_font_metrics()
        self.update_scrollbars()
        self.viewport().update()

    def _apply_font_metrics(self):
        metrics = QFontMetricsF(self._font)
        self._metrics = metrics
        # "M" in a monospace face is the cell. Kept as a float: rounding it
        # here is what makes a long run of text drift away from the cell
        # boundaries it is supposed to sit on.
        self._cell_w = max(1.0, metrics.horizontalAdvance("M"))
        self._cell_h = max(1, int(round(metrics.height())))
        self._baseline = int(round(metrics.ascent()))

    def _glyph_fits(self, ch: str, width: int, bold: bool, italic: bool) -> bool:
        """Whether a character advances by exactly the cells it claims.

        A run can only be drawn as one string while every glyph in it advances
        by its own cell width — the moment a fallback font steps in for an
        emoji or an unusual symbol, the rest of the run slides. Checking is
        cheap once cached, and it is what keeps box-drawing and CJK aligned
        while still letting the common case go out as whole strings.
        """
        key = (ch, bold, italic)
        cached = self._fits_cache.get(key)
        if cached is not None:
            return cached
        metrics = QFontMetricsF(self._font_for_flags(bold, italic))
        advance = metrics.horizontalAdvance(ch)
        fits = abs(advance - self._cell_w * max(1, width)) <= 0.5
        if len(self._fits_cache) < 8192:
            self._fits_cache[key] = fits
        return fits

    def _font_for_flags(self, bold: bool, italic: bool) -> QFont:
        """The bold/italic variant of the terminal font, built once."""
        key = (bool(bold), bool(italic))
        font = self._fonts.get(key)
        if font is None:
            font = QFont(self._font)
            if key[0]:
                font.setWeight(QFont.Weight.Bold)
            if key[1]:
                font.setItalic(True)
            self._fonts[key] = font
        return font

    def _font_for(self, attrs) -> QFont:
        return self._font_for_flags(attrs.bold, attrs.italic)

    # ── Geometry ────────────────────────────────────────────────────

    @property
    def margin(self) -> int:
        return self._margin

    @property
    def cell_width(self) -> float:
        return self._cell_w

    @property
    def cell_height(self) -> int:
        return self._cell_h

    def visible_rows(self) -> int:
        return max(1, self.viewport().height() // self._cell_h)

    def total_lines(self) -> int:
        if self._buffer is None:
            return 0
        return len(history_of(self._buffer)) + self._buffer.rows

    def dropped_lines(self) -> int:
        """History lines already discarded — the doc/absolute offset."""
        return dropped_of(self._buffer)

    def screen_top_line(self) -> int:
        """Document index of the first row of the live screen."""
        return len(history_of(self._buffer))

    def top_line(self) -> int:
        return int(self.verticalScrollBar().value())

    def update_scrollbars(self):
        if self._buffer is None:
            return
        vbar = self.verticalScrollBar()
        maximum = max(0, self.total_lines() - self.visible_rows())
        self._programmatic_scroll = True
        try:
            vbar.setRange(0, maximum)
            vbar.setPageStep(self.visible_rows())
            vbar.setSingleStep(1)
        finally:
            self._programmatic_scroll = False

        hbar = self.horizontalScrollBar()
        content = int(self._buffer.cols * self._cell_w) + 2 * self._margin
        hbar.setRange(0, max(0, content - self.viewport().width()))
        hbar.setPageStep(self.viewport().width())
        hbar.setSingleStep(int(self._cell_w) or 1)

    def scroll_to_line(self, line: int, follow: bool = False):
        vbar = self.verticalScrollBar()
        self.follow_output = follow
        if follow and self._selecting:
            # Do not pull the ground out from under a drag. The view catches
            # up the moment the button comes back up — which is why nothing
            # here has to stop rendering, as the old view did for five whole
            # seconds every time the mouse went down.
            return
        self._programmatic_scroll = True
        try:
            vbar.setValue(max(0, min(int(line), vbar.maximum())))
        finally:
            self._programmatic_scroll = False
        self.viewport().update()

    def _on_scrolled(self, value: int):
        if not self._programmatic_scroll:
            self.follow_output = value >= self.verticalScrollBar().maximum()
        self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_scrollbars()

    # ── Repainting ──────────────────────────────────────────────────

    def refresh(self, all_dirty: bool = True, dirty_rows=None):
        """Repaint what changed.

        Row-level damage is still worth honouring even though a full repaint
        is now cheap: a single keystroke touches one row, and asking Qt to
        blit one row's worth of pixels rather than the window is the
        difference between free and merely fast.
        """
        if self._buffer is None:
            self.viewport().update()
            return

        if self._placeholder and has_output(self._buffer):
            # The sign is drawn across the whole viewport, so it cannot be
            # taken down by a repaint of the one row the first prompt landed
            # on — the rest of it would stay on screen.
            self._placeholder = ""
            all_dirty = True

        alt = bool(self._buffer._using_alt)
        if alt != self._last_alt:
            # Switching screens replaces everything the view is showing, so a
            # selection or a set of search hits made against the other one no
            # longer points at anything.
            self._last_alt = alt
            self._last_dropped = self.dropped_lines()
            self.clear_selection()
            if self.search.matches:
                self.search.run(self._buffer)
            self.update_scrollbars()
            self.viewport().update()
            return

        dropped = self.dropped_lines()
        if dropped != self._last_dropped:
            if not self.follow_output:
                shift = dropped - self._last_dropped
                vbar = self.verticalScrollBar()
                self._programmatic_scroll = True
                try:
                    vbar.setValue(max(0, vbar.value() - shift))
                finally:
                    self._programmatic_scroll = False
            self._last_dropped = dropped
            all_dirty = True

        self.update_scrollbars()
        viewport = self.viewport()
        if all_dirty or not dirty_rows or self._sel_anchor is not None:
            viewport.update()
            return

        # The cursor is drawn over a row without dirtying it, so a move that
        # arrives alongside an unrelated edit would otherwise leave the old
        # cursor behind and never draw the new one.
        rows = set(dirty_rows)
        rows.add(self._last_cursor_row)
        rows.add(self._buffer.cursor_row)
        self._last_cursor_row = self._buffer.cursor_row

        top = self.top_line()
        screen_top = self.screen_top_line()
        height = self._cell_h
        width = viewport.width()
        for row in rows:
            y = (screen_top + row - top) * height
            if -height < y < viewport.height():
                viewport.update(0, int(y), width, height)

    def set_cursor_blink(self, on: bool):
        if on == self._cursor_blink_on:
            return
        self._cursor_blink_on = on
        self._repaint_cursor_row()

    def _repaint_cursor_row(self):
        buffer = self._buffer
        if buffer is None:
            return
        y = (self.screen_top_line() + buffer.cursor_row - self.top_line()) * self._cell_h
        self.viewport().update(0, int(y), self.viewport().width(), self._cell_h)
        self._last_cursor_row = buffer.cursor_row

    def paintEvent(self, event):
        painter = QPainter(self.viewport())
        painter.fillRect(event.rect(), self._bg)
        buffer = self._buffer
        if buffer is None:
            painter.end()
            return

        if self._placeholder:
            if has_output(buffer):
                self._placeholder = ""
            else:
                painter.setPen(QPen(QColor(self._fg.red(), self._fg.green(),
                                           self._fg.blue(), 120)))
                painter.setFont(self._font)
                painter.drawText(self.viewport().rect(),
                                 int(Qt.AlignmentFlag.AlignCenter),
                                 self._placeholder)

        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        top = self.top_line()
        x_offset = self._margin - self.horizontalScrollBar().value()
        height = self._cell_h
        first = max(0, event.rect().top() // height)
        last = min(self.visible_rows(), event.rect().bottom() // height + 1)
        total = self.total_lines()

        for screen_row in range(first, last):
            doc_index = top + screen_row
            if doc_index >= total:
                break
            row = row_at(buffer, doc_index)
            if row is None:
                continue
            self._paint_row(painter, x_offset, screen_row * height, doc_index, row)

        self._paint_cursor(painter, x_offset, top)
        painter.end()

    # ── One row ─────────────────────────────────────────────────────

    def _row_runs(self, row, cols: int):
        """Break a row into ``(start_col, end_col, attrs, text)`` runs.

        Continuation cells — the empty second half of a double-width glyph —
        contribute no characters but still advance the column, which is what
        keeps a run's text aligned with the columns it is drawn across.
        """
        runs = []
        col = 0
        extent = min(_row_extent(row), cols)
        start = 0
        chars: list = []
        current = None

        def flush(end):
            if current is not None and chars:
                runs.append((start, end, current, "".join(chars)))

        while col < extent:
            if isinstance(row, PackedRow):
                char = row.char_at(col)
                attrs = row.attrs_at(col)
                width = row.width_at(col)
            else:
                cell = row[col]
                char, attrs, width = cell.char, cell.attrs, cell.width

            if char == "":
                # The tail of a wide glyph. Already drawn by its first half.
                col += 1
                continue

            fits = self._glyph_fits(char, max(1, width), attrs.bold, attrs.italic)
            if current is None:
                start, current, chars = col, attrs, []
            elif not _same_attrs(attrs, current) or not fits:
                flush(col)
                start, current, chars = col, attrs, []
            chars.append(char)
            if not fits:
                # Drawn on its own so a substitute font cannot drag the rest
                # of the line sideways.
                flush(col + max(1, width))
                current, chars = None, []
            col += max(1, width)

        flush(col)
        return runs

    def _colors_for(self, attrs):
        fg = QColor(attrs.fg) if attrs.fg else QColor(self._fg)
        bg = QColor(attrs.bg) if attrs.bg else None
        if attrs.reverse:
            fg, bg = (bg or QColor(self._bg)), (QColor(attrs.fg) if attrs.fg
                                                else QColor(self._fg))
        if attrs.dim:
            fg = fg.darker(150)
        return fg, bg

    def _paint_row(self, painter, x_offset, y, doc_index, row):
        buffer = self._buffer
        cols = buffer.cols
        runs = self._row_runs(row, cols)
        cell_w = self._cell_w
        height = self._cell_h

        # Backgrounds first, so text of every run lands on top of all of them.
        for start, end, attrs, _text in runs:
            _fg, bg = self._colors_for(attrs)
            if bg is None:
                continue
            painter.fillRect(
                QRectF(x_offset + start * cell_w, y,
                       (end - start) * cell_w, height),
                bg)

        absolute = self.dropped_lines() + doc_index
        self._paint_search(painter, x_offset, y, absolute)
        self._paint_selection(painter, x_offset, y, absolute, cols)

        baseline = y + self._baseline
        for start, end, attrs, text in runs:
            has_ink = bool(text.strip())
            rule = attrs.underline or attrs.link
            if not (has_ink or rule):
                continue
            fg, _bg = self._colors_for(attrs)
            if has_ink:
                painter.setPen(QPen(fg))
                painter.setFont(self._font_for(attrs))
                painter.drawText(QRectF(x_offset + start * cell_w, y,
                                        (end - start) * cell_w, height),
                                 int(Qt.AlignmentFlag.AlignLeft
                                     | Qt.AlignmentFlag.AlignVCenter), text)
            if rule:
                painter.fillRect(
                    QRectF(x_offset + start * cell_w, baseline + 1.0,
                           (end - start) * cell_w, 1.0),
                    fg)

    def _paint_search(self, painter, x_offset, y, absolute: int):
        spans = self.search.by_line.get(absolute)
        if not spans:
            return
        current = self.search.current()
        for start, end in spans:
            colour = (self._current_search_color
                      if current and current[0] == absolute and current[1] == start
                      else self._search_color)
            painter.fillRect(
                QRectF(x_offset + start * self._cell_w, y,
                       (end - start) * self._cell_w, self._cell_h),
                colour)

    def _paint_selection(self, painter, x_offset, y, absolute: int, cols: int):
        span = self.selection_span(absolute, cols)
        if span is None:
            return
        start, end = span
        if end <= start:
            return
        painter.fillRect(
            QRectF(x_offset + start * self._cell_w, y,
                   (end - start) * self._cell_w, self._cell_h),
            self._selection_color)

    def _paint_cursor(self, painter, x_offset, top: int):
        buffer = self._buffer
        if not (self.cursor_enabled and buffer.cursor_visible):
            return
        doc_index = self.screen_top_line() + buffer.cursor_row
        screen_row = doc_index - top
        if not 0 <= screen_row < self.visible_rows():
            return

        col = max(0, min(buffer.cursor_col, buffer.cols - 1))
        x = x_offset + col * self._cell_w
        y = screen_row * self._cell_h
        focused = self.hasFocus()

        if not focused:
            # An unfocused terminal shows a hollow cell — the same signal
            # every other terminal gives that keystrokes are going elsewhere.
            painter.setPen(QPen(self._cursor_color, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(QRectF(x + 0.5, y + 0.5,
                                    self._cell_w - 1, self._cell_h - 1))
            return

        if buffer.cursor_blinks and not self._cursor_blink_on:
            return

        shape = getattr(buffer, "cursor_shape", CURSOR_BLOCK)
        if shape == CURSOR_UNDERLINE:
            painter.fillRect(
                QRectF(x, y + self._cell_h - 2, self._cell_w, 2),
                self._cursor_color)
            return
        if shape == CURSOR_BAR:
            painter.fillRect(QRectF(x, y, 2, self._cell_h), self._cursor_color)
            return

        painter.fillRect(QRectF(x, y, self._cell_w, self._cell_h),
                         self._cursor_color)
        row = row_at(self._buffer, doc_index)
        char = ""
        if row is not None:
            char = (row.char_at(col) if isinstance(row, PackedRow)
                    else row[col].char)
        if char.strip():
            painter.setPen(QPen(self._bg))
            painter.setFont(self._font)
            painter.drawText(QRectF(x, y, self._cell_w, self._cell_h),
                             int(Qt.AlignmentFlag.AlignLeft
                                 | Qt.AlignmentFlag.AlignVCenter), char)

    # ── Hit testing ─────────────────────────────────────────────────

    def cell_at(self, point: QPoint) -> tuple:
        """The ``(doc_index, col)`` under a viewport point, clamped to the grid."""
        if self._buffer is None:
            return (0, 0)
        x = point.x() - self._margin + self.horizontalScrollBar().value()
        col = int(x // self._cell_w) if self._cell_w else 0
        col = max(0, min(self._buffer.cols - 1, col))
        row = int(point.y() // self._cell_h)
        doc_index = self.top_line() + max(0, row)
        doc_index = max(0, min(self.total_lines() - 1, doc_index))
        return (doc_index, col)

    def screen_cell_at(self, point: QPoint) -> tuple:
        """The 1-based ``(col, row)`` on the live screen, for mouse reports."""
        doc_index, col = self.cell_at(point)
        row = doc_index - self.screen_top_line()
        row = max(0, min(self._buffer.rows - 1, row))
        return (col + 1, row + 1)

    def attrs_at(self, doc_index: int, col: int):
        row = row_at(self._buffer, doc_index)
        if row is None:
            return None
        if isinstance(row, PackedRow):
            return row.attrs_at(col)
        if 0 <= col < len(row):
            return row[col].attrs
        return None

    def line_text(self, doc_index: int) -> str:
        return line_string(self._buffer, doc_index)

    # ── Selection ───────────────────────────────────────────────────

    def has_selection(self) -> bool:
        return (self._sel_anchor is not None
                and self._sel_head is not None
                and self._sel_anchor != self._sel_head)

    def clear_selection(self):
        had = self.has_selection()
        self._sel_anchor = self._sel_head = None
        self._sel_block = False
        self._sel_mode = "char"
        self._sel_origin = None
        if had:
            self.selection_changed.emit()
            self.viewport().update()

    def select_all(self):
        if self._buffer is None:
            return
        dropped = self.dropped_lines()
        self._sel_anchor = (dropped, 0)
        self._sel_head = (dropped + self.total_lines() - 1, self._buffer.cols)
        self._sel_block = False
        self.selection_changed.emit()
        self.viewport().update()

    def _ordered_selection(self):
        a, b = self._sel_anchor, self._sel_head
        if a is None or b is None:
            return None
        return (a, b) if (a[0], a[1]) <= (b[0], b[1]) else (b, a)

    def selection_span(self, absolute_line: int, cols: int):
        """Columns of a line covered by the selection, or None."""
        ordered = self._ordered_selection()
        if ordered is None:
            return None
        (line_a, col_a), (line_b, col_b) = ordered
        if self._sel_block:
            if not line_a <= absolute_line <= line_b:
                return None
            return (min(col_a, col_b), max(col_a, col_b))
        if not line_a <= absolute_line <= line_b:
            return None
        start = col_a if absolute_line == line_a else 0
        end = col_b if absolute_line == line_b else cols
        return (start, end)

    def selected_text(self) -> str:
        ordered = self._ordered_selection()
        if ordered is None or self._buffer is None:
            return ""
        (line_a, _col_a), (line_b, _col_b) = ordered
        dropped = self.dropped_lines()
        cols = self._buffer.cols
        pieces = []
        for absolute in range(line_a, line_b + 1):
            span = self.selection_span(absolute, cols)
            if span is None:
                continue
            start, end = span
            text = self._raw_line(absolute - dropped)
            piece = text[start:end] if start < len(text) else ""
            # Every terminal drops the run of blanks a selection sweeps up at
            # the end of a line; keeping them would paste ragged whitespace.
            pieces.append(piece.rstrip())
        return "\n".join(pieces)

    def _raw_line(self, doc_index: int) -> str:
        """A line padded out to full width, so column slicing is honest."""
        row = row_at(self._buffer, doc_index)
        if row is None:
            return ""
        cols = self._buffer.cols
        if isinstance(row, PackedRow):
            text = row.text()
        else:
            text = "".join(cell.char for cell in row)
        return text.ljust(cols)

    # ── Mouse ───────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        point = event.position().toPoint()
        doc_index, col = self.cell_at(point)
        absolute = self.dropped_lines() + doc_index

        # A third click lands here as an ordinary press, so the only sign that
        # it belongs with the double-click before it is how soon it arrived.
        interval = QApplication.doubleClickInterval() / 1000.0
        if (self._last_double_cell == doc_index
                and time.monotonic() - self._last_double_click <= interval):
            self._last_double_click = 0.0
            self.select_line(doc_index)
            self._selecting = True
            return

        # Alt turns the drag into a rectangle, which is the only sane way to
        # lift one column out of `ps` or `docker ps` output.
        self._sel_block = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        self._sel_mode = "char"
        self._sel_anchor = (absolute, col)
        self._sel_head = (absolute, col)
        self._selecting = True
        self.selection_changed.emit()
        self.viewport().update()

    def mouseMoveEvent(self, event):
        if not self._selecting:
            super().mouseMoveEvent(event)
            return
        point = event.position().toPoint()
        # Dragging past the top or bottom edge scrolls, the way it should.
        if point.y() < 0:
            self.scroll_to_line(self.top_line() - 1)
        elif point.y() > self.viewport().height():
            self.scroll_to_line(self.top_line() + 1)
        doc_index, col = self.cell_at(point)
        absolute = self.dropped_lines() + doc_index
        if self._sel_mode in ("word", "line"):
            # Extending a word- or line-mode selection has to move both ends:
            # dragging backwards past the start puts the anchor on the far
            # edge of the thing first picked, so the original word or line
            # stays wholly inside the selection instead of collapsing to a
            # point.
            self._extend_snapped(doc_index, col, absolute)
        else:
            self._sel_head = (absolute, col)
        self.selection_changed.emit()
        self.viewport().update()

    def mouseReleaseEvent(self, event):
        if self._selecting and event.button() == Qt.MouseButton.LeftButton:
            self._selecting = False
            self.selection_changed.emit()
            if self.follow_output:
                # Catch up on everything that arrived during the drag.
                self.scroll_to_line(self.verticalScrollBar().maximum(),
                                    follow=True)
        else:
            super().mouseReleaseEvent(event)

    @property
    def is_selecting(self) -> bool:
        """True while a drag is in progress."""
        return self._selecting

    def mouseDoubleClickEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseDoubleClickEvent(event)
            return
        doc_index, col = self.cell_at(event.position().toPoint())
        start, end = self._word_bounds(doc_index, col)
        absolute = self.dropped_lines() + doc_index
        self._last_double_click = time.monotonic()
        self._last_double_cell = doc_index
        self._sel_origin = (absolute, start, end)
        self._sel_block = False
        self._sel_mode = "word"
        self._sel_anchor = (absolute, start)
        self._sel_head = (absolute, end)
        self._selecting = True
        self.selection_changed.emit()
        self.viewport().update()

    def select_line(self, doc_index: int):
        absolute = self.dropped_lines() + doc_index
        self._sel_block = False
        self._sel_mode = "line"
        self._sel_origin = (absolute, 0, self._buffer.cols)
        self._sel_anchor = (absolute, 0)
        self._sel_head = (absolute, self._buffer.cols)
        self.selection_changed.emit()
        self.viewport().update()

    def _extend_snapped(self, doc_index: int, col: int, absolute: int):
        """Grow a word- or line-mode selection to cover whole words or lines."""
        origin = self._sel_origin
        if origin is None:
            self._sel_head = (absolute, col)
            return
        origin_line, origin_start, origin_end = origin

        if self._sel_mode == "line":
            near, far = 0, self._buffer.cols
        else:
            near, far = self._word_bounds(doc_index, col)

        if (absolute, near) < (origin_line, origin_start):
            # Dragging backwards: the anchor swings to the far edge of what
            # was originally picked.
            self._sel_anchor = (origin_line, origin_end)
            self._sel_head = (absolute, near)
        else:
            self._sel_anchor = (origin_line, origin_start)
            self._sel_head = (absolute, far)

    def _word_bounds(self, doc_index: int, col: int) -> tuple:
        text = self._raw_line(doc_index)
        if col >= len(text) or text[col] not in WORD_CHARS:
            return (col, col + 1)
        start = col
        while start > 0 and text[start - 1] in WORD_CHARS:
            start -= 1
        end = col
        while end < len(text) and text[end] in WORD_CHARS:
            end += 1
        return (start, end)

    # ── Keys go to the owner, never to this widget ──────────────────

    def event(self, event):
        # Accepting ShortcutOverride is what stops Qt matching Ctrl+A and
        # friends as widget shortcuts before the terminal ever sees them —
        # except for the few chords the window keeps (lock, switch tab…).
        if event.type() == event.Type.ShortcutOverride:
            if is_app_chord(event):
                event.ignore()
                return False
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent):
        event.accept()

    def keyReleaseEvent(self, event: QKeyEvent):
        event.accept()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.viewport().update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.viewport().update()
