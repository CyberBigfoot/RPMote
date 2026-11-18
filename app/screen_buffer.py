"""
RPMote Screen Buffer — Virtual terminal screen (2D character grid).

Provides a proper terminal emulation layer so that cursor movement,
full-screen apps (nano, vim, htop), and line editing all work correctly.
"""

import base64
import re
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

from app.cells import (
    DEFAULT_ATTRS, Cell, CellAttrs, PackedRow, intern_attrs, pack_row,
    row_text,
)

__all__ = [
    "Cell", "CellAttrs", "PackedRow", "ScreenBuffer", "CommandBlock",
    "pack_row", "row_text", "intern_attrs",
    "MARK_NONE", "MARK_PROMPT", "MARK_INPUT", "MARK_OUTPUT",
    "DEFAULT_SCROLLBACK", "CURSOR_BLOCK", "CURSOR_UNDERLINE", "CURSOR_BAR",
]

#: Lines of history kept by default. Two thousand was under half of what a
#: single `dmesg` produces; the packed row storage in app.cells is what makes
#: a hundred thousand affordable (a few tens of megabytes rather than tens of
#: gigabytes).
DEFAULT_SCROLLBACK = 100_000

# Cursor shapes a program can ask for with DECSCUSR (CSI Ps SP q).
CURSOR_BLOCK = 0
CURSOR_UNDERLINE = 1
CURSOR_BAR = 2


# Rows that a program animates in place (progress bars, spinners, byte
# counters, Compose status lines) must not pile up in scrollback. The patterns
# are compiled once here: the check runs for every line that scrolls off the
# top of the screen, so it sits directly in the output hot path.
_STRONG_TRANSIENT_RE = re.compile(
    r"^#\d+(?:\s|$)"
    r"|^[a-f0-9]{8,64}:\s+(?:pulling fs layer|waiting|downloading|extracting|"
    r"verifying checksum|download complete|pull complete)"
    r"|\[[=+#>\- ]{3,}\]\s*\d{1,3}\s*%"
    r"|\b\d{1,3}(?:\.\d+)?\s*%"
    r"|\b\d+(?:\.\d+)?\s*(?:kb|mb|gb|b)\s*/\s*\d+(?:\.\d+)?\s*(?:kb|mb|gb|b)\b"
    r"|[\u2801-\u28ff]",
    re.IGNORECASE,
)

_VOLATILE_TRANSIENT_RE = re.compile(
    r"^#\d+(?:\s|$)"
    r"|^[a-f0-9]{8,64}:\s+(?:pulling fs layer|waiting|downloading|extracting|"
    r"verifying checksum|download complete|pull complete)"
    r"|\[[=+#>\- ]{3,}\]"
    r"|\b(?:pulling|pulled|pushing|downloading|extracting|installing|building|"
    r"built|creating|created|starting|started|waiting|verifying|running|healthy|"
    r"removing|removed|stopping|stopped|restarting|recreating|recreated|paused)\b"
    r"|\b(?:reading package lists|building dependency tree|"
    r"reading state information)\b"
    r"|\b\d{1,3}(?:\.\d+)?\s*%"
    r"|\b\d+(?:\.\d+)?\s*(?:kb|mb|gb)\s*/\s*\d+(?:\.\d+)?\s*(?:kb|mb|gb)\b"
    r"|^[\s\[\]+\-*|/\\\u2801-\u28ff]+[^\r\n]{0,160}$",
    re.IGNORECASE,
)

_COMPOSE_HEADER_RE = re.compile(
    r"^\[\+\]\s+(?:running|up|down|pulling|pushing|building|stopping|"
    r"starting|creating|removing|restarting|recreating|waiting|killing|"
    r"pausing|unpausing)\s+\d+\s*/\s*\d+",
    re.IGNORECASE,
)

_COMPOSE_ROW_RE = re.compile(
    r"^(?:container|network|volume|image|service)\s+\S+.*\b"
    r"(?:creating|created|starting|started|stopping|stopped|removing|removed|"
    r"running|waiting|healthy|pulling|pulled|building|built|recreating|recreated|"
    r"restarting|restarted|killing|killed|pausing|paused|unpausing|exited|error)\b"
    r"(?:\s+\d+(?:\.\d+)?s)?\s*$",
    re.IGNORECASE,
)

_WHITESPACE_RE = re.compile(r"\s+")
_LEADING_JUNK_RE = re.compile(r"^[^\w\[]+")


# Semantic marks (OSC 133 shell integration). One per row, tracked alongside
# the grid and carried into scrollback, so "the previous prompt" and "the
# output of the last command" are facts the shell told us rather than
# something guessed from the text.
MARK_NONE = 0
MARK_PROMPT = 1     # a shell prompt begins on this row
MARK_INPUT = 2      # the typed command line begins on this row
MARK_OUTPUT = 3     # the command's output begins on this row


@dataclass
class CommandBlock:
    """One command, from the prompt that invited it to its exit status."""
    exit_code: Optional[int] = None
    started: float = 0.0
    finished: float = 0.0
    command: str = ""

    @property
    def running(self) -> bool:
        return self.started > 0.0 and self.finished == 0.0

    @property
    def duration(self) -> float:
        if not self.started:
            return 0.0
        end = self.finished or time.monotonic()
        return max(0.0, end - self.started)


@dataclass
class _SavedCursor:
    row: int = 0
    col: int = 0
    attrs: CellAttrs = field(default_factory=CellAttrs)
    origin: bool = False


class ScreenBuffer:
    """
    Fixed-size terminal screen buffer (rows × cols character grid).

    Supports:
    - Normal + alternate screen buffers
    - Cursor movement, save/restore
    - Scroll regions
    - Line insert/delete, character insert/delete
    - Erase in display / erase in line
    - SGR attribute tracking
    - Bracketed paste mode
    - Application cursor keys mode
    """

    def __init__(self, rows: int = 40, cols: int = 120,
                 scrollback_limit: int = DEFAULT_SCROLLBACK):
        self.rows = rows
        self.cols = cols
        self.scrollback_limit = max(scrollback_limit, 0)

        # Current cursor (0-indexed)
        self.cursor_row = 0
        self.cursor_col = 0

        # Each screen has its own DECSC/DECRC save slot. An editor's cursor
        # saves must not overwrite the shell position saved by mode 1049.
        self._saved_cursors = {False: _SavedCursor(), True: _SavedCursor()}

        # Current SGR state. ``current_attrs`` is mutable and belongs to the
        # parser; cells are given ``_cell_attrs()`` instead, which is an
        # interned, shared, never-modified equivalent. Copying a style per
        # character was the single largest cost of receiving output.
        self.current_attrs = CellAttrs()
        self._normal_attrs = CellAttrs()
        self._shared_attrs = DEFAULT_ATTRS
        self._attrs_dirty = True

        # Normal screen
        self._normal_grid = self._make_grid(rows, cols)
        self._normal_volatile = [False for _ in range(rows)]
        self._normal_cursor = (0, 0)

        # Alternate screen
        self._alt_grid = self._make_grid(rows, cols)
        self._alt_volatile = [False for _ in range(rows)]
        self._alt_cursor = (0, 0)

        # Which screen is active
        self._using_alt = False
        self.grid = self._normal_grid
        self._volatile_lines = self._normal_volatile

        # Scrollback history (list of row lists)
        self._scrollback: list[list[Cell]] = []

        # Semantic marks, one int per row, mirroring the grids above and
        # travelling with each row into scrollback.
        self._normal_marks = [MARK_NONE for _ in range(rows)]
        self._alt_marks = [MARK_NONE for _ in range(rows)]
        self._marks = self._normal_marks
        self._scrollback_marks: list[int] = []

        # True once the remote shell has emitted its first OSC 133, which is
        # what tells the view it can stop guessing where prompts are.
        self.shell_integration = False
        #: Recent commands, oldest first; the last entry is the live one.
        self.commands: list[CommandBlock] = []
        self._command_limit = 200
        #: Working directory reported by OSC 7, and the host that reported it.
        self.cwd = ""
        self.cwd_host = ""
        #: Called with text the remote asked to put on the clipboard (OSC 52).
        self.clipboard_callback = None

        # Scroll region (top, bottom) — 0-indexed, inclusive
        self._scroll_top = 0
        self._scroll_bottom = rows - 1
        self.scrollback_total_lines = 0

        # Modes
        self.application_cursor_keys = False  # DECCKM
        self.bracketed_paste = False
        self.cursor_visible = True
        self.origin_mode = False  # DECOM
        self.autowrap = True
        # DECSCUSR. Programs that set a bar cursor for insert mode (vim, fish,
        # zsh's vi bindings) look broken without this, because the shape is
        # how they show you which mode you are in.
        self.cursor_shape = CURSOR_BLOCK
        self.cursor_blinks = True
        # Deferred wrap (DECAWM last-column rule): writing into the final
        # column leaves the cursor there with this flag set. The line only
        # breaks when the NEXT printable character arrives; a CR, LF, cursor
        # move or erase cancels it instead. Wrapping eagerly made every
        # full-width line occupy two rows, which broke the cursor-up
        # arithmetic of anything that redraws a block in place (Docker
        # Compose's progress display above all).
        self._pending_wrap = False
        self.insert_mode = False
        self.mouse_tracking_mode = 0
        self.mouse_sgr = False
        self.focus_event_mode = False

        # Dirty flag — set whenever content changes
        self.dirty = True

        # Row-level damage tracking. The view repaints only the rows listed
        # here, which is what keeps a single keystroke from re-rendering the
        # whole screen. ``_all_dirty`` means "every row changed" (scroll,
        # screen erase, resize, screen switch).
        self._dirty_rows: set[int] = set()
        self._all_dirty = True

        # Callback used to answer terminal queries (cursor position reports,
        # device attributes). Set by the view to the channel writer. Programs
        # that ask and never get an answer either stall or draw at the wrong
        # offset.
        self.response_callback = None

        # Last printable character, for CSI b (repeat).
        self._last_printed = ""

        # Partial escape sequence accumulator
        self._escape_buffer = ""
        self.window_title = ""

    # ── Grid Management ──────────────────────────────────────────

    def _cell_attrs(self) -> CellAttrs:
        """The style to hand to cells written right now.

        Shared between every cell written under the same SGR state, and never
        modified — which is what makes sharing safe. ``_attrs_dirty`` is set
        wherever ``current_attrs`` is changed.
        """
        if self._attrs_dirty:
            self._shared_attrs = intern_attrs(self.current_attrs)
            self._attrs_dirty = False
        return self._shared_attrs

    def _recycle_row(self, row: list) -> list:
        """Blank a row that has left the screen, and hand it back for reuse.

        The row that scrolls off the top is dead the moment it has been packed
        into history — packing copies out the characters and shares the
        styles, keeping no reference to the cells themselves. Wiping those
        cells and putting the row back at the bottom is the same work as
        allocating a fresh one minus the allocation, and at a screenful of
        columns per scrolled line that was the emulator's largest single
        source of garbage.
        """
        if len(row) != self.cols:
            return self._blank_row()
        for cell in row:
            cell.char = " "
            cell.attrs = DEFAULT_ATTRS
            cell.width = 1
        return row

    def _blank_row(self, cols=None, attrs=None) -> list:
        """A fresh row of blank cells sharing one style object."""
        style = DEFAULT_ATTRS if attrs is None else attrs
        return [Cell(char=" ", attrs=style, width=1)
                for _ in range(self.cols if cols is None else cols)]

    @staticmethod
    def _make_grid(rows, cols):
        return [[Cell(char=" ", attrs=DEFAULT_ATTRS, width=1)
                 for _ in range(cols)] for _ in range(rows)]

    def resize(self, rows: int, cols: int):
        """Resize the screen buffer, preserving content where possible."""
        if rows == self.rows and cols == self.cols:
            return

        old_rows = self.rows
        self.rows = rows
        self.cols = cols
        self._pending_wrap = False

        # Shrinking the window pushes the *top* of the normal screen into
        # scrollback, the way a real terminal does, so the prompt the user is
        # looking at stays visible instead of being clipped off the bottom.
        drop = 0
        if rows < old_rows:
            # The hidden shell still needs resizing while an editor owns the
            # alternate screen, or its prompt gets clipped off the bottom.
            last_used = self._normal_cursor[0] if self._using_alt else self.cursor_row
            for index, row in enumerate(self._normal_grid):
                if self._row_text(row):
                    last_used = max(last_used, index)
            drop = max(0, min(last_used - (rows - 1), len(self._normal_grid)))

        if drop and self.scrollback_limit > 0:
            for index, row in enumerate(self._normal_grid[:drop]):
                self._scrollback.append([
                    Cell(char=cell.char, attrs=cell.attrs.copy(), width=cell.width)
                    for cell in row[:cols]
                ])
                self._scrollback_marks.append(
                    self._normal_marks[index]
                    if index < len(self._normal_marks) else MARK_NONE
                )
                self.scrollback_total_lines += 1
            while len(self._scrollback) > self.scrollback_limit:
                self._scrollback.pop(0)
                if self._scrollback_marks:
                    self._scrollback_marks.pop(0)

        for grid_ref, offset in (('_normal_grid', drop), ('_alt_grid', 0)):
            old = getattr(self, grid_ref)
            new = self._make_grid(rows, cols)
            for r in range(rows):
                src = r + offset
                if src >= len(old):
                    break
                old_row = old[src]
                for c in range(min(cols, len(old_row))):
                    new[r][c] = old_row[c]
            setattr(self, grid_ref, new)

        for volatile_ref, offset in (('_normal_volatile', drop), ('_alt_volatile', 0)):
            old_flags = getattr(self, volatile_ref)
            new_flags = [False for _ in range(rows)]
            for r in range(rows):
                src = r + offset
                if src < len(old_flags):
                    new_flags[r] = old_flags[src]
            setattr(self, volatile_ref, new_flags)

        for marks_ref, offset in (('_normal_marks', drop), ('_alt_marks', 0)):
            old_marks = getattr(self, marks_ref)
            new_marks = [MARK_NONE for _ in range(rows)]
            for r in range(rows):
                src = r + offset
                if src < len(old_marks):
                    new_marks[r] = old_marks[src]
            setattr(self, marks_ref, new_marks)

        # Point to the right grid
        self.grid = self._alt_grid if self._using_alt else self._normal_grid
        self._volatile_lines = self._alt_volatile if self._using_alt else self._normal_volatile
        self._marks = self._alt_marks if self._using_alt else self._normal_marks

        if not self._using_alt:
            self.cursor_row -= drop
        saved_row, saved_col = self._normal_cursor
        self._normal_cursor = (
            max(0, min(saved_row - drop, rows - 1)),
            max(0, min(saved_col, cols - 1)),
        )
        for alternate, saved in self._saved_cursors.items():
            saved.row = max(0, min(saved.row - (0 if alternate else drop), rows - 1))
            saved.col = max(0, min(saved.col, cols - 1))
        self._clamp_cursor()

        # Reset scroll region to full screen
        self._scroll_top = 0
        self._scroll_bottom = rows - 1
        self._touch_all()

    # ── Damage tracking ──────────────────────────────────────────

    def _touch_row(self, row: int):
        """Record that one screen row needs repainting."""
        self.dirty = True
        if not self._all_dirty and 0 <= row < self.rows:
            self._dirty_rows.add(row)

    def _touch_rows(self, start: int, end: int):
        for row in range(max(0, start), min(self.rows, end + 1)):
            self._touch_row(row)

    def _touch_all(self):
        """Record that the whole screen needs repainting."""
        self.dirty = True
        self._all_dirty = True
        self._dirty_rows.clear()

    def mark_all_dirty(self):
        """Public damage reset — used when the view rebuilds from scratch."""
        self._touch_all()

    def consume_dirty(self) -> tuple[bool, set]:
        """Return (every_row_changed, changed_rows) and clear the record."""
        if self._all_dirty:
            self._all_dirty = False
            self._dirty_rows.clear()
            return True, set()
        rows = self._dirty_rows
        self._dirty_rows = set()
        return False, rows

    def respond(self, data: str):
        """Send a reply to the remote program (cursor reports, DA, ...)."""
        callback = self.response_callback
        if not callback:
            return
        try:
            callback(data)
        except Exception:
            pass

    # ── Cursor helpers ───────────────────────────────────────────

    def _clamp_cursor(self):
        self.cursor_row = max(0, min(self.cursor_row, self.rows - 1))
        self.cursor_col = max(0, min(self.cursor_col, self.cols - 1))

    def _mark_current_line_volatile(self):
        if 0 <= self.cursor_row < len(self._volatile_lines):
            self._volatile_lines[self.cursor_row] = True

    def _mark_lines_volatile(self, start: int, end: int):
        lo = max(0, min(start, end))
        hi = min(len(self._volatile_lines) - 1, max(start, end))
        for row in range(lo, hi + 1):
            self._volatile_lines[row] = True

    def _row_text(self, row) -> str:
        return row_text(row)

    def _should_archive_row(self, row: list[Cell], volatile: bool) -> bool:
        text = self._row_text(row)
        if not text:
            return False
        if self._is_compose_status_row(text):
            return False
        # Unambiguous progress frames (percentages, byte ratios, buildkit
        # steps, pull-layer status, progress bars, spinners) are animated in
        # place and must never reach scrollback — even if the row scrolled off
        # before its redraw cursor movement had a chance to mark it volatile.
        # Without this, tall progress output (image pulls on `docker compose
        # up`, package lists on `apt`/OS updates) leaves a trail of near
        # duplicate frames in the history.
        if self._is_strong_transient_row(text):
            return False
        if not volatile:
            return True
        return not _VOLATILE_TRANSIENT_RE.search(text)

    @staticmethod
    def _is_strong_transient_row(text: str) -> bool:
        """Return True for rows that are unmistakably live-progress frames.

        These markers (percent complete, byte ratios, buildkit `#N` steps,
        pull-layer status, progress bars) never appear in genuine final output
        that a user would want to scroll back to, so they are safe to keep out
        of scrollback unconditionally.
        """
        return bool(_STRONG_TRANSIENT_RE.search(text))

    @staticmethod
    def _is_compose_status_row(text: str) -> bool:
        """Return True for Docker Compose live status rows that should not enter scrollback."""
        clean = _LEADING_JUNK_RE.sub("", _WHITESPACE_RE.sub(" ", text).strip())
        if not clean:
            return False
        if _COMPOSE_HEADER_RE.match(clean):
            return True
        return bool(_COMPOSE_ROW_RE.match(clean))

    #: Widths are cached because the same few hundred characters come round
    #: again and again, and three unicodedata lookups per character is real
    #: money in the output hot path.
    _WIDTH_CACHE: dict = {}

    @staticmethod
    def _char_width(ch: str) -> int:
        """Cells used by a printable character, the way wcwidth counts them.

        Matching wcwidth is the whole point rather than a detail: the remote
        laid its output out with wcwidth, so any other answer here puts our
        columns out of step with the ones it drew. That is why a variation
        selector or a zero-width joiner counts as nothing and attaches to the
        character before it, and why an emoji following a joiner still takes
        its own two cells even though the two are drawn as one glyph — the
        remote counted them as four columns, so we must too.
        """
        if not ch:
            return 0
        cached = ScreenBuffer._WIDTH_CACHE.get(ch)
        if cached is not None:
            return cached

        category = unicodedata.category(ch)
        # Mn/Me are the combining marks; catching them by category rather than
        # by combining class is what finally makes U+FE0F (emoji presentation)
        # and the variation selectors weightless, as wcwidth has them.
        if category in ("Mn", "Me", "Cf", "Cc"):
            width = 0
        elif unicodedata.east_asian_width(ch) in ("F", "W"):
            width = 2
        else:
            width = 1

        if len(ScreenBuffer._WIDTH_CACHE) < 8192:
            ScreenBuffer._WIDTH_CACHE[ch] = width
        return width

    # ── Scrolling ────────────────────────────────────────────────

    def _scroll_up(self, n=1):
        """Scroll content up within scroll region (new blank lines at bottom)."""
        # History is only written when the screen itself scrolls. A program
        # scrolling inside a margin (pagers, anything with a status bar) must
        # not leak its redraws into the scrollback.
        keeps_history = (
            self.scrollback_limit > 0
            and not self._using_alt
            and self._scroll_top == 0
        )
        for _ in range(n):
            if self._scroll_top < len(self.grid):
                source_row = self.grid[self._scroll_top]
                source_volatile = (
                    self._volatile_lines[self._scroll_top]
                    if self._scroll_top < len(self._volatile_lines)
                    else False
                )
                if keeps_history and self._should_archive_row(source_row, source_volatile):
                    self._scrollback.append(pack_row(source_row))
                    self._scrollback_marks.append(
                        self._marks[self._scroll_top]
                        if self._scroll_top < len(self._marks) else MARK_NONE
                    )
                    self.scrollback_total_lines += 1
                    if len(self._scrollback) > self.scrollback_limit:
                        self._scrollback.pop(0)
                        if self._scrollback_marks:
                            self._scrollback_marks.pop(0)
                del self.grid[self._scroll_top]
                self.grid.insert(self._scroll_bottom,
                                 self._recycle_row(source_row))
                if self._scroll_top < len(self._volatile_lines):
                    del self._volatile_lines[self._scroll_top]
                    self._volatile_lines.insert(self._scroll_bottom, False)
                if self._scroll_top < len(self._marks):
                    del self._marks[self._scroll_top]
                    self._marks.insert(self._scroll_bottom, MARK_NONE)
        self._touch_all()

    def _scroll_down(self, n=1):
        """Scroll content down within scroll region (new blank lines at top)."""
        for _ in range(n):
            del self.grid[self._scroll_bottom]
            self.grid.insert(self._scroll_top, self._blank_row())
            if self._scroll_bottom < len(self._volatile_lines):
                del self._volatile_lines[self._scroll_bottom]
                self._volatile_lines.insert(self._scroll_top, False)
            if self._scroll_bottom < len(self._marks):
                del self._marks[self._scroll_bottom]
                self._marks.insert(self._scroll_top, MARK_NONE)
        self._touch_all()

    # ── Writing ──────────────────────────────────────────────────

    #: Characters one cell may accumulate before further combining marks are
    #: dropped. Eight covers every real cluster; nothing legitimate is longer.
    MAX_CLUSTER_LENGTH = 8

    def _put_char(self, ch: str):
        """Write a character at the current cursor position and advance."""
        width = self._char_width(ch)

        if width == 0:
            prev_col = self.cursor_col - 1
            if prev_col >= 0:
                cell = self.grid[self.cursor_row][prev_col]
                # A cluster has a sane upper bound. Without one, a stream of
                # combining marks grows a single cell without limit — the
                # "Zalgo" trick — and takes the renderer down with it.
                if len(cell.char) < self.MAX_CLUSTER_LENGTH:
                    cell.char += ch
                    self._touch_row(self.cursor_row)
            return

        # A wrap left pending by the previous character happens now, because a
        # printable character is what finally forces the line to break.
        if self._pending_wrap:
            self._pending_wrap = False
            if self.autowrap:
                self.cursor_col = 0
                self._line_feed()

        if self.cursor_col >= self.cols:
            if self.autowrap:
                self.cursor_col = 0
                self._line_feed()
            else:
                self.cursor_col = self.cols - 1
        elif self.autowrap and width == 2 and self.cursor_col >= self.cols - 1:
            # A double-width glyph cannot straddle the margin, so it moves down
            # immediately rather than leaving a wrap pending.
            self.cursor_col = 0
            self._line_feed()

        r, c = self.cursor_row, self.cursor_col
        if 0 <= r < self.rows and 0 <= c < self.cols:
            if self.insert_mode:
                self._insert_chars(width)
            self.grid[r][c] = Cell(char=ch, attrs=self._cell_attrs(), width=width)
            if width == 2 and c + 1 < self.cols:
                self.grid[r][c + 1] = Cell(char="", attrs=self._cell_attrs(), width=0)

        self._last_printed = ch
        self._touch_row(r)

        self.cursor_col += width
        if self.cursor_col >= self.cols:
            # Stay parked on the last column. Only the next printable character
            # (or nothing at all, if a CR/LF/cursor move comes first) wraps.
            self.cursor_col = self.cols - 1
            self._pending_wrap = self.autowrap

    def _put_ascii(self, text: str):
        """Write a run of plain single-width characters.

        Follows exactly the same rules as ``_put_char`` — deferred wrap at the
        margin, no wrap when DECAWM is off — but settles them once per run
        instead of once per character. Insert mode is the one case that has to
        go the slow way, because each character shifts the rest of the line.
        """
        if self.insert_mode:
            for ch in text:
                self._put_char(ch)
            return

        attrs = self._cell_attrs()
        index = 0
        length = len(text)
        while index < length:
            if self._pending_wrap:
                self._pending_wrap = False
                if self.autowrap:
                    self.cursor_col = 0
                    self._line_feed()

            if self.cursor_col >= self.cols:
                if self.autowrap:
                    self.cursor_col = 0
                    self._line_feed()
                else:
                    self.cursor_col = self.cols - 1

            row_index = self.cursor_row
            if not 0 <= row_index < self.rows:
                return
            # Re-read the row each time round: a line feed may have scrolled
            # the grid out from under the previous one.
            row = self.grid[row_index]
            col = self.cursor_col
            chunk = text[index:index + (self.cols - col)]
            for offset in range(len(chunk)):
                row[col + offset] = Cell(char=chunk[offset], attrs=attrs,
                                         width=1)
            self._touch_row(row_index)

            index += len(chunk)
            self.cursor_col += len(chunk)
            self._last_printed = chunk[-1]
            if self.cursor_col >= self.cols:
                # Park on the last column; only the next printable character
                # actually breaks the line.
                self.cursor_col = self.cols - 1
                self._pending_wrap = self.autowrap

    def _line_feed(self):
        """Move cursor down one line, scrolling if at bottom of scroll region."""
        self._pending_wrap = False
        if self.cursor_row == self._scroll_bottom:
            self._scroll_up(1)
        elif self.cursor_row < self.rows - 1:
            self.cursor_row += 1
            if self.cursor_row < len(self._volatile_lines) and not self._row_text(self.grid[self.cursor_row]):
                self._volatile_lines[self.cursor_row] = False
            self.dirty = True

    def _reverse_line_feed(self):
        """Move cursor up one line, scrolling down if at top of scroll region."""
        if self.cursor_row == self._scroll_top:
            self._scroll_down(1)
        elif self.cursor_row > 0:
            self.cursor_row -= 1
            self.dirty = True

    def _carriage_return(self):
        self._mark_current_line_volatile()
        self._pending_wrap = False
        self.cursor_col = 0
        self.dirty = True

    def _backspace(self):
        self._pending_wrap = False
        if self.cursor_col > 0:
            self.cursor_col -= 1
            if self.grid[self.cursor_row][self.cursor_col].width == 0 and self.cursor_col > 0:
                self.cursor_col -= 1
            self.dirty = True

    def _tab(self):
        self._pending_wrap = False
        # Move to next tab stop (every 8 columns)
        next_tab = ((self.cursor_col // 8) + 1) * 8
        self.cursor_col = min(next_tab, self.cols - 1)
        self.dirty = True

    # ── Erase Operations ────────────────────────────────────────

    def _erase_in_display(self, mode: int):
        self._pending_wrap = False
        blank = Cell(attrs=self._cell_attrs())
        if mode == 0:  # Cursor to end
            # Clear rest of current line
            for c in range(self.cursor_col, self.cols):
                self.grid[self.cursor_row][c] = Cell(attrs=self._cell_attrs())
            # Clear lines below
            for r in range(self.cursor_row + 1, self.rows):
                self.grid[r] = [Cell(attrs=self._cell_attrs()) for _ in range(self.cols)]
            self._touch_rows(self.cursor_row, self.rows - 1)
        elif mode == 1:  # Start to cursor
            for r in range(0, self.cursor_row):
                self.grid[r] = [Cell(attrs=self._cell_attrs()) for _ in range(self.cols)]
            for c in range(0, self.cursor_col + 1):
                self.grid[self.cursor_row][c] = Cell(attrs=self._cell_attrs())
            self._touch_rows(0, self.cursor_row)
        elif mode == 2 or mode == 3:  # Entire screen
            for r in range(self.rows):
                self.grid[r] = [Cell(attrs=self._cell_attrs()) for _ in range(self.cols)]
            self._volatile_lines = [False for _ in range(self.rows)]
            self._marks = [MARK_NONE for _ in range(self.rows)]
            if self._using_alt:
                self._alt_volatile = self._volatile_lines
                self._alt_marks = self._marks
            else:
                self._normal_volatile = self._volatile_lines
            self.cursor_row = 0
            self.cursor_col = 0
            if mode == 3:
                # ESC[3J also erases the scrollback (saved lines). The `clear`
                # command emits this so the history can't be scrolled back to;
                # without it, `clear` only blanks the visible screen.
                self._scrollback.clear()
                self.scrollback_total_lines = 0
            self._touch_all()
        self.dirty = True

    def _erase_in_line(self, mode: int):
        self._mark_current_line_volatile()
        self._pending_wrap = False
        if mode == 0:  # Cursor to end
            for c in range(self.cursor_col, self.cols):
                self.grid[self.cursor_row][c] = Cell(attrs=self._cell_attrs())
        elif mode == 1:  # Start to cursor
            for c in range(0, self.cursor_col + 1):
                self.grid[self.cursor_row][c] = Cell(attrs=self._cell_attrs())
        elif mode == 2:  # Whole line
            self.grid[self.cursor_row] = [Cell(attrs=self._cell_attrs()) for _ in range(self.cols)]
        self._touch_row(self.cursor_row)

    # ── Insert / Delete ─────────────────────────────────────────

    def _insert_lines(self, n: int):
        """Insert n blank lines at cursor row, pushing lines down within scroll region."""
        for _ in range(n):
            if self._scroll_bottom < len(self.grid):
                del self.grid[self._scroll_bottom]
                if self._scroll_bottom < len(self._volatile_lines):
                    del self._volatile_lines[self._scroll_bottom]
                if self._scroll_bottom < len(self._marks):
                    del self._marks[self._scroll_bottom]
            self.grid.insert(self.cursor_row, self._blank_row())
            self._volatile_lines.insert(self.cursor_row, False)
            self._marks.insert(self.cursor_row, MARK_NONE)
        self.cursor_col = 0
        self._touch_rows(self.cursor_row, self._scroll_bottom)

    def _delete_lines(self, n: int):
        """Delete n lines at cursor row, pulling lines up within scroll region."""
        for _ in range(n):
            if self.cursor_row < len(self.grid):
                del self.grid[self.cursor_row]
                if self.cursor_row < len(self._volatile_lines):
                    del self._volatile_lines[self.cursor_row]
                if self.cursor_row < len(self._marks):
                    del self._marks[self.cursor_row]
            self.grid.insert(self._scroll_bottom, self._blank_row())
            self._volatile_lines.insert(self._scroll_bottom, False)
            self._marks.insert(self._scroll_bottom, MARK_NONE)
        self.cursor_col = 0
        self._touch_rows(self.cursor_row, self._scroll_bottom)

    def _insert_chars(self, n: int):
        """Insert n blank spaces at cursor, shifting rest of line right."""
        row = self.grid[self.cursor_row]
        for _ in range(n):
            row.insert(self.cursor_col, Cell(attrs=self._cell_attrs()))
            if len(row) > self.cols:
                row.pop()
        self._touch_row(self.cursor_row)

    def _delete_chars(self, n: int):
        """Delete n characters at cursor, shifting rest of line left."""
        row = self.grid[self.cursor_row]
        for _ in range(n):
            if self.cursor_col < len(row):
                del row[self.cursor_col]
            row.append(Cell(attrs=self._cell_attrs()))
        self._touch_row(self.cursor_row)

    def _erase_chars(self, n: int):
        """Erase n characters at cursor (replace with blanks, don't shift)."""
        for i in range(n):
            c = self.cursor_col + i
            if c < self.cols:
                self.grid[self.cursor_row][c] = Cell(attrs=self._cell_attrs())
        self._touch_row(self.cursor_row)

    # ── Alternate Screen ────────────────────────────────────────

    def _save_cursor(self, position_only=False):
        saved = self._saved_cursors[self._using_alt]
        saved.row, saved.col = self.cursor_row, self.cursor_col
        if not position_only:
            saved.attrs = self.current_attrs.copy()
            saved.origin = self.origin_mode

    def _restore_cursor(self, position_only=False):
        saved = self._saved_cursors[self._using_alt]
        self.cursor_row, self.cursor_col = saved.row, saved.col
        if not position_only:
            self.current_attrs = saved.attrs.copy()
            self._attrs_dirty = True
            self.origin_mode = saved.origin
        self._pending_wrap = False
        self._clamp_cursor()
        self.dirty = True

    def enter_alt_screen(self):
        if not self._using_alt:
            self._normal_cursor = (self.cursor_row, self.cursor_col)
            self._normal_attrs = self.current_attrs.copy()
            self._attrs_dirty = True
            self._using_alt = True
            self._alt_grid = self._make_grid(self.rows, self.cols)
            self._alt_volatile = [False for _ in range(self.rows)]
            self._alt_marks = [MARK_NONE for _ in range(self.rows)]
            self.grid = self._alt_grid
            self._volatile_lines = self._alt_volatile
            self._marks = self._alt_marks
            self.cursor_row = 0
            self.cursor_col = 0
            self._scroll_top = 0
            self._scroll_bottom = self.rows - 1
            self._pending_wrap = False
            self._touch_all()

    def leave_alt_screen(self):
        if self._using_alt:
            self._using_alt = False
            self.grid = self._normal_grid
            self._volatile_lines = self._normal_volatile
            self._marks = self._normal_marks
            self.cursor_row, self.cursor_col = self._normal_cursor
            self.current_attrs = getattr(self, "_normal_attrs", CellAttrs()).copy()
            self._attrs_dirty = True
            self._scroll_top = 0
            self._scroll_bottom = self.rows - 1
            self._pending_wrap = False
            # A full-screen program that died without tidying up would
            # otherwise leave the shell with dead arrow keys, an invisible
            # cursor or mouse reporting that swallows text selection.
            self.application_cursor_keys = False
            self.mouse_tracking_mode = 0
            self.mouse_sgr = False
            self.focus_event_mode = False
            self.cursor_shape = CURSOR_BLOCK
            self.cursor_blinks = True
            self.cursor_visible = True
            self.insert_mode = False
            self.origin_mode = False
            self.autowrap = True
            self._touch_all()

    # ── SGR Processing ──────────────────────────────────────────

    def _process_sgr(self, params: list[int]):
        """Process SGR parameters to update current_attrs."""
        # Once per sequence rather than once per character: this is what lets
        # _cell_attrs hand out a shared object without re-interning.
        self._attrs_dirty = True
        if not params:
            params = [0]

        i = 0
        while i < len(params):
            p = params[i]
            if p == 0:
                self.current_attrs = CellAttrs()
            elif p == 1:
                self.current_attrs.bold = True
            elif p == 2:
                self.current_attrs.dim = True
            elif p == 3:
                self.current_attrs.italic = True
            elif p == 4:
                self.current_attrs.underline = True
            elif p == 7:
                self.current_attrs.reverse = True
            elif p == 22:
                self.current_attrs.bold = False
                self.current_attrs.dim = False
            elif p == 23:
                self.current_attrs.italic = False
            elif p == 24:
                self.current_attrs.underline = False
            elif p == 27:
                self.current_attrs.reverse = False
            elif 30 <= p <= 37:
                self.current_attrs.fg = _BASIC_COLORS[p - 30]
            elif p == 38:
                # Extended foreground
                if i + 1 < len(params) and params[i + 1] == 5 and i + 2 < len(params):
                    self.current_attrs.fg = _color_from_256(params[i + 2])
                    i += 2
                elif i + 1 < len(params) and params[i + 1] == 2 and i + 4 < len(params):
                    r, g, b = params[i + 2], params[i + 3], params[i + 4]
                    self.current_attrs.fg = f"#{r:02x}{g:02x}{b:02x}"
                    i += 4
            elif p == 39:
                self.current_attrs.fg = None
            elif 40 <= p <= 47:
                self.current_attrs.bg = _BASIC_COLORS[p - 40]
            elif p == 48:
                # Extended background
                if i + 1 < len(params) and params[i + 1] == 5 and i + 2 < len(params):
                    self.current_attrs.bg = _color_from_256(params[i + 2])
                    i += 2
                elif i + 1 < len(params) and params[i + 1] == 2 and i + 4 < len(params):
                    r, g, b = params[i + 2], params[i + 3], params[i + 4]
                    self.current_attrs.bg = f"#{r:02x}{g:02x}{b:02x}"
                    i += 4
            elif p == 49:
                self.current_attrs.bg = None
            elif 90 <= p <= 97:
                self.current_attrs.fg = _BRIGHT_COLORS[p - 90]
            elif 100 <= p <= 107:
                self.current_attrs.bg = _BRIGHT_COLORS[p - 100]
            i += 1

    # ── Main Feed ────────────────────────────────────────────────

    def feed(self, data: str):
        """
        Process incoming terminal data string.
        Handles control characters, escape sequences, and printable text.
        """
        i = 0
        n = len(data)

        while i < n:
            # If we're in the middle of accumulating an escape sequence
            if self._escape_buffer:
                self._escape_buffer += data[i]
                i += 1
                result = self._try_complete_escape(self._escape_buffer)
                if result == "incomplete":
                    continue
                # result == "complete" or "invalid"
                self._escape_buffer = ""
                continue

            ch = data[i]

            if ch == '\x1b':
                # Start of escape sequence
                self._escape_buffer = ch
                i += 1
                continue

            if ch == '\r':
                self._carriage_return()
                i += 1
                continue

            if ch in ('\n', '\x0b', '\x0c'):
                # LF, VT and FF all move down one line.
                self._line_feed()
                i += 1
                continue

            if ch == '\x08':  # BS
                self._backspace()
                i += 1
                continue

            if ch == '\t':
                self._tab()
                i += 1
                continue

            if ch == '\x07':  # BEL
                i += 1
                continue

            if ch == '\x0f' or ch == '\x0e':  # SI/SO (shift in/out) — ignore
                i += 1
                continue

            # Plain printable text — the overwhelming majority of what a
            # terminal ever receives. Taking the run in one piece rather than
            # dispatching each character down the ladder above is most of the
            # cost of `cat`, a build log or anything else that talks at length.
            if " " <= ch < "":
                start = i
                i += 1
                while i < n:
                    nxt = data[i]
                    if nxt < " " or nxt >= "":
                        break
                    i += 1
                self._put_ascii(data[start:i])
                continue

            if ch == "":
                # DEL is not a printable character; printing it corrupted the
                # cell before it, because it counts as zero cells wide.
                i += 1
                continue

            if ord(ch) >= 32:
                self._put_char(ch)
            # else: ignore other control chars

            i += 1

    def _try_complete_escape(self, seq: str) -> str:
        """
        Try to parse a (possibly incomplete) escape sequence.
        Returns 'complete', 'incomplete', or 'invalid'.
        """
        if len(seq) < 2:
            return "incomplete"

        # ESC [ ... (CSI)
        if seq[1] == '[':
            return self._parse_csi(seq)

        # ESC ] ... (OSC — Operating System Command)
        if seq[1] == ']':
            # OSC ends with BEL (\x07) or ST (\x1b\\)
            if '\x07' in seq[2:] or '\x1b\\' in seq:
                self._handle_osc(seq)
                return "complete"
            if len(seq) > 4096:
                return "invalid"
            return "incomplete"

        # ESC P / _ / ^ are string controls (DCS/APC/PM). Consume through ST.
        if seq[1] in ('P', '_', '^'):
            if '\x1b\\' in seq:
                return "complete"
            if len(seq) > 4096:
                return "invalid"
            return "incomplete"

        # ESC ( / ESC ) — character set selection, consume next char
        if seq[1] in ('(', ')'):
            if len(seq) < 3:
                return "incomplete"
            return "complete"  # Ignore charset switching

        # ESC 7 — DECSC (save cursor, pen and origin mode)
        if seq[1] == '7':
            self._save_cursor()
            return "complete"

        # ESC 8 — DECRC (restore cursor, pen and origin mode)
        if seq[1] == '8':
            self._restore_cursor()
            return "complete"

        # ESC M — Reverse Index (scroll down / cursor up)
        if seq[1] == 'M':
            self._reverse_line_feed()
            return "complete"

        # ESC D — Index (cursor down + scroll)
        if seq[1] == 'D':
            self._line_feed()
            return "complete"

        # ESC E — Next Line
        if seq[1] == 'E':
            self._carriage_return()
            self._line_feed()
            return "complete"

        # ESC c — Full Reset
        if seq[1] == 'c':
            self.leave_alt_screen()
            self._saved_cursors = {False: _SavedCursor(), True: _SavedCursor()}
            self._erase_in_display(2)
            self.current_attrs = CellAttrs()
            self._attrs_dirty = True
            self.cursor_row = 0
            self.cursor_col = 0
            self.application_cursor_keys = False
            self.mouse_tracking_mode = 0
            self.mouse_sgr = False
            self.bracketed_paste = False
            self.cursor_visible = True
            self.cursor_shape = CURSOR_BLOCK
            self.cursor_blinks = True
            self.insert_mode = False
            self.origin_mode = False
            self.autowrap = True
            self._scroll_top = 0
            self._scroll_bottom = self.rows - 1
            self._scrollback.clear()
            self._scrollback_marks.clear()
            self.commands.clear()
            self.scrollback_total_lines = 0
            self._touch_all()
            return "complete"

        # ESC = / ESC > — Keypad modes (ignore)
        if seq[1] in ('=', '>'):
            return "complete"

        # Unknown single-char escape
        return "complete"

    def _handle_osc(self, seq: str):
        """Handle common OSC sequences while safely consuming the rest."""
        body = seq[2:]
        if body.endswith("\x07"):
            body = body[:-1]
        elif body.endswith("\x1b\\"):
            body = body[:-2]

        if ";" not in body:
            return
        code, value = body.split(";", 1)
        if code in ("0", "2"):
            self.window_title = value[:256]
        elif code == "7":
            self._handle_cwd_report(value)
        elif code == "8":
            self._handle_hyperlink(value)
        elif code == "52":
            self._handle_clipboard_request(value)
        elif code in ("133", "633"):
            # 133 is the FinalTerm/iTerm2 convention every modern shell
            # integration speaks; 633 is the VS Code superset, whose A/B/C/D
            # mean the same thing.
            self._handle_semantic_mark(value)

    # -- Shell integration ----------------------------------------

    def _handle_cwd_report(self, value: str):
        """OSC 7 - the shell telling us which directory it is sitting in."""
        value = value.strip()
        if not value:
            return
        try:
            parsed = urllib.parse.urlparse(value)
        except Exception:
            return
        if parsed.scheme and parsed.scheme != "file":
            return
        path = urllib.parse.unquote(parsed.path or "")
        if not path:
            return
        # file://host/C:/x arrives with a leading slash that belongs to the
        # URL, not to the path.
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        self.cwd = path[:4096]
        self.cwd_host = (parsed.netloc or "")[:256]

    def _handle_hyperlink(self, value: str):
        """OSC 8 - start or end a hyperlink region."""
        # value is "params;URI"; an empty URI closes the current link.
        uri = value.split(";", 1)[1] if ";" in value else ""
        uri = uri.strip()
        if uri and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", uri):
            # Absolute URLs only. A relative target has no meaning here and
            # would be a way to smuggle something odd into a click.
            uri = ""
        self.current_attrs = self.current_attrs.copy()
        self.current_attrs.link = uri[:2048]
        self._attrs_dirty = True

    def _handle_clipboard_request(self, value: str):
        """OSC 52 - the remote asking to put something on the clipboard.

        Read requests ("?") are ignored on purpose: answering one hands the
        local clipboard to whatever is running on the far end.
        """
        if self.clipboard_callback is None:
            return
        parts = value.split(";", 1)
        if len(parts) != 2:
            return
        payload = parts[1].strip()
        if not payload or payload == "?":
            return
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:
            return
        text = raw.decode("utf-8", errors="replace")
        # A megabyte of clipboard from a remote host is not a paste, it is an
        # accident or an attack.
        if len(text) > 1000000:
            return
        try:
            self.clipboard_callback(text)
        except Exception:
            pass

    def _handle_semantic_mark(self, value: str):
        """OSC 133 - where the prompt, the command and its output begin."""
        kind = (value.split(";", 1)[0] or "").strip().upper()
        if kind not in ("A", "B", "C", "D"):
            return
        self.shell_integration = True
        row = max(0, min(self.cursor_row, self.rows - 1))

        if kind == "A":
            self._set_mark(row, MARK_PROMPT)
            # A fresh prompt means whatever was running has finished, even if
            # the shell never sent a D (Ctrl-C at the prompt, say).
            if self.commands and self.commands[-1].running:
                self.commands[-1].finished = time.monotonic()
            self.commands.append(CommandBlock())
            if len(self.commands) > self._command_limit:
                del self.commands[:len(self.commands) - self._command_limit]
        elif kind == "B":
            self._set_mark(row, MARK_INPUT)
        elif kind == "C":
            self._set_mark(row, MARK_OUTPUT)
            if self.commands:
                self.commands[-1].started = time.monotonic()
        elif kind == "D":
            code = None
            rest = value.split(";", 1)[1].strip() if ";" in value else ""
            if rest:
                try:
                    code = int(rest.split(";")[0])
                except ValueError:
                    code = None
            if self.commands:
                block = self.commands[-1]
                block.exit_code = code
                block.finished = time.monotonic()

    def _set_mark(self, row: int, mark: int):
        if 0 <= row < len(self._marks):
            # The first mark on a row wins, except that a prompt always does:
            # a prompt redrawing itself must not turn its row into output.
            if self._marks[row] == MARK_NONE or mark == MARK_PROMPT:
                self._marks[row] = mark

    # -- queries the view uses ------------------------------------

    def mark_at(self, row: int) -> int:
        """Semantic mark for a screen row."""
        return self._marks[row] if 0 <= row < len(self._marks) else MARK_NONE

    def prompt_lines(self) -> list:
        """Document lines (scrollback, then screen) where a prompt starts."""
        lines = [i for i, m in enumerate(self._scrollback_marks) if m == MARK_PROMPT]
        base = len(self._scrollback)
        lines.extend(base + i for i, m in enumerate(self._marks) if m == MARK_PROMPT)
        return lines

    def last_command(self):
        """The most recent command that actually ran.

        A block needs a start: pressing Enter on an empty line produces a
        prompt and an exit status with nothing in between, and reporting
        "exit 0" for that would be noise.
        """
        for block in reversed(self.commands):
            if block.started:
                return block
        return None

    def last_output_region(self):
        """Document line range [start, end) holding the last command output.

        Uses the marks the shell emitted, so it is exact - nothing here has
        to scrape the screen for something that looks like a prompt.
        """
        marks = list(self._scrollback_marks) + list(self._marks)
        start = None
        for index in range(len(marks) - 1, -1, -1):
            if marks[index] == MARK_OUTPUT:
                start = index
                break
        if start is None:
            # No output mark anywhere: some shells only manage A and D, and a
            # prompt that fits on one line takes the C with it. The row after
            # the last prompt is where the output has to begin.
            for index in range(len(marks) - 1, -1, -1):
                if marks[index] in (MARK_PROMPT, MARK_INPUT):
                    start = index + 1
                    break
        if start is None or start >= len(marks):
            return None
        end = len(marks)
        for index in range(start + 1, len(marks)):
            if marks[index] == MARK_PROMPT:
                end = index
                break
        return (start, end) if end > start else None

    def line_text(self, line: int) -> str:
        """Text of a document line - scrollback first, then the screen."""
        if 0 <= line < len(self._scrollback):
            row = self._scrollback[line]
        else:
            index = line - len(self._scrollback)
            if not 0 <= index < len(self.grid):
                return ""
            row = self.grid[index]
        return row_text(row)

    def region_text(self, start: int, end: int) -> str:
        """Text of a document line range, trailing blank lines trimmed."""
        lines = [self.line_text(line) for line in range(max(0, start), max(0, end))]
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)

    def _parse_csi(self, seq: str) -> str:
        """Parse CSI sequence: ESC [ <params> <intermediate> <final>"""
        if len(seq) < 3:
            return "incomplete"

        # The sequence after ESC [
        body = seq[2:]

        # Params are 0x30-0x3F (digits, semicolons, ?, >, =),
        # intermediates are 0x20-0x2F, and final bytes are 0x40-0x7E.
        final_idx = -1
        for j, c in enumerate(body):
            code = ord(c)
            if 0x40 <= code <= 0x7e:
                final_idx = j
                break
            if not (0x20 <= code <= 0x3f):
                return "invalid"

        if final_idx == -1:
            # No final byte yet
            if len(seq) > 64:
                return "invalid"
            return "incomplete"

        final = body[final_idx]
        # Intermediates are kept, not just skipped: "CSI 2 q" and "CSI 2 SP q"
        # are different sequences (load-LEDs and set-cursor-style), and the
        # only thing telling them apart is the space.
        intermediate = "".join(
            ch for ch in body[:final_idx]
            if ch in " !\"#$%&'()*+,-./"
        )
        params_str = "".join(
            ch for ch in body[:final_idx]
            if ch not in " !\"#$%&'()*+,-./"
        )

        # Check for DEC private / secondary prefix
        private = False
        prefix = ""
        if params_str.startswith('?'):
            private = True
            prefix = '?'
            params_str = params_str[1:]
        elif params_str[:1] in ('>', '='):
            prefix = params_str[0]
            params_str = params_str[1:]

        # Parse numeric params
        params = []
        if params_str:
            for p in params_str.split(';'):
                p = p.strip()
                if p.isdigit():
                    params.append(int(p))
                else:
                    params.append(0)

        # ── DEC Private Modes ──
        if private:
            if final in ('h', 'l'):
                self._handle_dec_mode(final, params)
            elif final == 'n' and params and params[0] == 6:
                # DECXCPR — extended cursor position report.
                self.respond(f"\x1b[?{self.cursor_row + 1};{self.cursor_col + 1}R")
            return "complete"

        # ── Standard CSI sequences ──
        self._handle_csi(final, params, prefix, intermediate)
        return "complete"

    def _handle_dec_mode(self, final: str, params: list[int]):
        """Handle DEC private mode set/reset (CSI ? ... h/l)."""
        for p in params:
            if final == 'h':  # Set mode
                if p == 1:
                    self.application_cursor_keys = True
                elif p == 25:
                    self.cursor_visible = True
                elif p in (47, 1047):
                    self.enter_alt_screen()
                elif p == 1048:
                    self._save_cursor()
                elif p == 1049:
                    if not self._using_alt:
                        self._save_cursor()
                        self.enter_alt_screen()
                elif p == 2004:
                    self.bracketed_paste = True
                elif p == 6:
                    self.origin_mode = True
                elif p == 7:
                    self.autowrap = True
                elif p in (1000, 1002, 1003):
                    self.mouse_tracking_mode = p
                elif p == 1006:
                    self.mouse_sgr = True
                elif p == 1004:
                    self.focus_event_mode = True
                elif p in (1005, 1015):
                    # Legacy mouse encodings; SGR (1006) is preferred and the
                    # reports we send are the same shape either way.
                    pass
            elif final == 'l':  # Reset mode
                if p == 1:
                    self.application_cursor_keys = False
                elif p == 25:
                    self.cursor_visible = False
                elif p in (47, 1047):
                    self.leave_alt_screen()
                elif p == 1048:
                    self._restore_cursor()
                elif p == 1049:
                    if self._using_alt:
                        self.leave_alt_screen()
                        self._restore_cursor()
                elif p == 2004:
                    self.bracketed_paste = False
                elif p == 6:
                    self.origin_mode = False
                elif p == 7:
                    self.autowrap = False
                elif p in (1000, 1002, 1003):
                    if self.mouse_tracking_mode == p:
                        self.mouse_tracking_mode = 0
                elif p == 1006:
                    self.mouse_sgr = False
                elif p == 1004:
                    self.focus_event_mode = False
        self.dirty = True

    # CSI finals that reposition the cursor or rewrite the screen. Any of them
    # cancels a wrap left pending by a character in the last column.
    _WRAP_CANCELLING_FINALS = frozenset("ABCDEFGHfdJKLMPX@STruZ`ae")

    #: DECSCUSR parameter -> (shape, blinking). 0 and 1 are the same thing;
    #: anything past 6 is not defined and leaves the cursor alone.
    _CURSOR_STYLES = {
        0: (CURSOR_BLOCK, True),
        1: (CURSOR_BLOCK, True),
        2: (CURSOR_BLOCK, False),
        3: (CURSOR_UNDERLINE, True),
        4: (CURSOR_UNDERLINE, False),
        5: (CURSOR_BAR, True),
        6: (CURSOR_BAR, False),
    }

    def _handle_csi(self, final: str, params: list[int], prefix: str = "",
                    intermediate: str = ""):
        """Handle standard CSI sequences."""
        def p(idx=0, default=1):
            return params[idx] if idx < len(params) and params[idx] else default

        if final == 'q' and intermediate == ' ':  # DECSCUSR
            style = self._CURSOR_STYLES.get(params[0] if params else 0)
            if style is not None:
                self.cursor_shape, self.cursor_blinks = style
            self.dirty = True
            return

        if final in self._WRAP_CANCELLING_FINALS:
            self._pending_wrap = False

        if final == 'm':  # SGR
            self._process_sgr(params if params else [0])

        elif final == 'A':  # Cursor Up
            old_row = self.cursor_row
            self.cursor_row = max(self._scroll_top, self.cursor_row - p())
            self._mark_lines_volatile(old_row, self.cursor_row)

        elif final == 'B':  # Cursor Down
            old_row = self.cursor_row
            self.cursor_row = min(self._scroll_bottom, self.cursor_row + p())
            self._mark_lines_volatile(old_row, self.cursor_row)

        elif final == 'C':  # Cursor Forward
            self.cursor_col = min(self.cols - 1, self.cursor_col + p())

        elif final == 'D':  # Cursor Back
            self.cursor_col = max(0, self.cursor_col - p())
            self._mark_current_line_volatile()

        elif final == 'E':  # Cursor Next Line
            old_row = self.cursor_row
            self.cursor_row = min(self._scroll_bottom, self.cursor_row + p())
            self.cursor_col = 0
            self._mark_lines_volatile(old_row, self.cursor_row)

        elif final == 'F':  # Cursor Previous Line
            old_row = self.cursor_row
            self.cursor_row = max(self._scroll_top, self.cursor_row - p())
            self.cursor_col = 0
            self._mark_lines_volatile(old_row, self.cursor_row)

        elif final == 'G':  # Cursor Horizontal Absolute
            self.cursor_col = max(0, min(self.cols - 1, p() - 1))
            self._mark_current_line_volatile()

        elif final in ('H', 'f'):  # Cursor Position
            old_row = self.cursor_row
            row = max(0, min(self.rows - 1, p(0) - 1))
            col = max(0, min(self.cols - 1, p(1, 1) - 1))
            self.cursor_row = row
            self.cursor_col = col
            self._mark_lines_volatile(old_row, self.cursor_row)

        elif final == 'J':  # Erase in Display
            self._erase_in_display(p(0, 0))

        elif final == 'K':  # Erase in Line
            self._erase_in_line(p(0, 0))

        elif final == 'L':  # Insert Lines
            self._insert_lines(p())

        elif final == 'M':  # Delete Lines
            self._delete_lines(p())

        elif final == 'P':  # Delete Characters
            self._delete_chars(p())

        elif final == '@':  # Insert Characters
            self._insert_chars(p())

        elif final == 'X':  # Erase Characters
            self._erase_chars(p())

        elif final == 'S':  # Scroll Up
            self._scroll_up(p())

        elif final == 'T':  # Scroll Down
            self._scroll_down(p())

        elif final == 'd':  # Vertical Position Absolute
            old_row = self.cursor_row
            self.cursor_row = max(0, min(self.rows - 1, p() - 1))
            self._mark_lines_volatile(old_row, self.cursor_row)

        elif final == '`':  # Horizontal Position Absolute
            self.cursor_col = max(0, min(self.cols - 1, p() - 1))
            self._mark_current_line_volatile()

        elif final == 'a':  # Horizontal Position Relative
            self.cursor_col = max(0, min(self.cols - 1, self.cursor_col + p()))

        elif final == 'e':  # Vertical Position Relative
            self.cursor_row = max(0, min(self.rows - 1, self.cursor_row + p()))

        elif final == 'Z':  # Cursor Backward Tabulation
            col = self.cursor_col
            for _ in range(p()):
                col = ((col - 1) // 8) * 8 if col > 0 else 0
            self.cursor_col = max(0, col)

        elif final == 'b':  # Repeat preceding character
            if self._last_printed:
                for _ in range(min(p(), self.cols * 2)):
                    self._put_char(self._last_printed)

        elif final == 'r':  # Set Scrolling Region (DECSTBM)
            top = p(0, 1) - 1
            bottom = p(1, self.rows) - 1
            self._scroll_top = max(0, min(top, self.rows - 1))
            self._scroll_bottom = max(self._scroll_top, min(bottom, self.rows - 1))
            self.cursor_row = 0
            self.cursor_col = 0

        elif final == 's':  # Save cursor position
            self._save_cursor(position_only=True)

        elif final == 'u':  # Restore cursor position
            self._restore_cursor(position_only=True)

        elif final == 'h' or final == 'l':
            enabled = final == 'h'
            for mode in params:
                if mode == 4:
                    self.insert_mode = enabled

        elif final == 'n':  # Device Status Report
            # Programs that ask where the cursor is (line editors, prompt
            # frameworks, most TUI toolkits) block or draw at the wrong
            # offset when nothing answers.
            if params and params[0] == 6:
                self.respond(f"\x1b[{self.cursor_row + 1};{self.cursor_col + 1}R")
            elif params and params[0] == 5:
                self.respond("\x1b[0n")

        elif final == 'c':  # Device Attributes
            if prefix == '>':
                self.respond("\x1b[>0;276;0c")
            elif not prefix:
                # VT220 with 256 colours — what xterm-256color implies.
                self.respond("\x1b[?62;1;2;6;9;15;22c")

        elif final == 't':
            pass  # Window manipulation — ignore

        self.dirty = True

    # ── Rendering ────────────────────────────────────────────────

    def get_lines(self) -> list[list[Cell]]:
        """Return the current grid for rendering."""
        return self.grid

    def get_text(self) -> str:
        """Return plain-text representation (for logging/search)."""
        lines = []
        for row in self.grid:
            lines.append(row_text(row))
        # Strip trailing empty lines
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def get_last_nonempty_line(self) -> str:
        """Return the last non-blank line from the active grid."""
        for row in reversed(self.grid):
            line = row_text(row)
            if line:
                return line
        return ""

    def archive_visible_screen(self, keep_last: int = 0):
        """Push the visible screen into scrollback before it is cleared.

        Used when the terminal has to blank and realign itself (hidden shell
        setup) but the content already on screen — the login banner, MOTD, the
        output of whatever ran before — is worth keeping. ``keep_last`` drops
        that many trailing non-blank rows, so the prompt about to be redrawn is
        not archived twice.
        """
        if self.scrollback_limit <= 0 or self._using_alt:
            return 0

        last = -1
        for index, row in enumerate(self.grid):
            if "".join(cell.char for cell in row).strip():
                last = index
        if last < 0:
            return 0

        end = last + 1 - max(0, keep_last)
        archived = 0
        for index, row in enumerate(self.grid[:end]):
            self._scrollback.append(pack_row(row))
            self._scrollback_marks.append(
                self._marks[index] if index < len(self._marks) else MARK_NONE
            )
            self.scrollback_total_lines += 1
            archived += 1
        while len(self._scrollback) > self.scrollback_limit:
            self._scrollback.pop(0)
            if self._scrollback_marks:
                self._scrollback_marks.pop(0)
        return archived


# ── Color Tables ────────────────────────────────────────────────

_BASIC_COLORS = [
    "#000000",  # Black
    "#cc0000",  # Red
    "#4e9a06",  # Green
    "#c4a000",  # Yellow
    "#3465a4",  # Blue
    "#75507b",  # Magenta
    "#06989a",  # Cyan
    "#d3d7cf",  # White
]

_BRIGHT_COLORS = [
    "#555753",  # Bright Black
    "#ef2929",  # Bright Red
    "#8ae234",  # Bright Green
    "#fce94f",  # Bright Yellow
    "#729fcf",  # Bright Blue
    "#ad7fa8",  # Bright Magenta
    "#34e2e2",  # Bright Cyan
    "#eeeeec",  # Bright White
]


def _build_256_palette():
    """Build the standard 256-color xterm palette."""
    palette = list(_BASIC_COLORS) + list(_BRIGHT_COLORS)

    # 216 color cube (indices 16-231)
    for r in range(6):
        for g in range(6):
            for b in range(6):
                rv = r * 51
                gv = g * 51
                bv = b * 51
                palette.append(f"#{rv:02x}{gv:02x}{bv:02x}")

    # 24 grayscale (indices 232-255)
    for i in range(24):
        v = 8 + i * 10
        palette.append(f"#{v:02x}{v:02x}{v:02x}")

    return palette


_PALETTE_256 = _build_256_palette()


def _color_from_256(index: int) -> str:
    if 0 <= index < len(_PALETTE_256):
        return _PALETTE_256[index]
    return "#ffffff"
