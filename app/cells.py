"""The character cell, and the compact form rows take once they are history.

A live grid row is a list of :class:`Cell` objects — three Python objects and
the best part of two hundred bytes per character. That is fine for the forty
rows on screen and ruinous for history: at the sizes people actually want to
keep, a hundred thousand lines rather than two thousand, it runs into
gigabytes and the process dies long before the buffer fills.

A row that has scrolled off the screen never changes again, and that is what
makes it packable. The characters collapse into a single string, the styling
collapses into the handful of runs that genuinely differ, trailing blanks are
dropped entirely, and the :class:`CellAttrs` behind each run is shared with
every other row that happens to be styled the same way. A default-styled
eighty-column line goes from roughly twenty kilobytes to roughly two hundred
bytes.

Packed rows are read-only. ``__getitem__`` builds a :class:`Cell` on demand so
that code written against the grid keeps working unchanged, but the
:class:`CellAttrs` it hands back is shared between rows — never mutate one.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class CellAttrs:
    """ANSI style attributes attached to a single cell.

    Slotted because there is one of these per distinct style rather than per
    cell, but the grid still holds tens of thousands of references to them and
    a per-instance ``__dict__`` is pure overhead.
    """
    fg: Optional[str] = None       # Foreground color (#rrggbb or None=default)
    bg: Optional[str] = None       # Background color
    bold: bool = False
    dim: bool = False
    italic: bool = False
    underline: bool = False
    reverse: bool = False
    # SGR param string that produced this state (for pass-through to AnsiParser)
    sgr_seq: str = ""
    # OSC 8 hyperlink target. Empty for ordinary text.
    link: str = ""

    def copy(self):
        return CellAttrs(
            fg=self.fg, bg=self.bg,
            bold=self.bold, dim=self.dim,
            italic=self.italic, underline=self.underline,
            reverse=self.reverse, sgr_seq=self.sgr_seq,
            link=self.link,
        )

    def key(self) -> tuple:
        """Everything that distinguishes one style from another."""
        return (self.fg, self.bg, self.bold, self.dim, self.italic,
                self.underline, self.reverse, self.sgr_seq, self.link)

    def is_default(self) -> bool:
        return self.key() == _DEFAULT_KEY


@dataclass(slots=True)
class Cell:
    """Single character cell in the terminal grid.

    ``attrs`` is normally a shared, interned style rather than a copy, so it
    must never be modified through a cell. ``char`` may be, and is: a
    combining mark is appended to the cluster already in the cell.
    """
    char: str = " "
    attrs: CellAttrs = field(default_factory=CellAttrs)
    width: int = 1


_DEFAULT_KEY = CellAttrs().key()

#: Shared styles, so that the millions of default-styled cells in a long
#: history all point at one object. Bounded because a program drawing a
#: truecolour gradient would otherwise mint a new style per pixel; past the
#: cap rows simply stop sharing, which costs memory but never correctness.
_ATTR_POOL: dict = {}
_ATTR_POOL_LIMIT = 4096


def intern_attrs(attrs: CellAttrs) -> CellAttrs:
    """Return a shared, never-mutated equivalent of ``attrs``."""
    key = attrs.key()
    shared = _ATTR_POOL.get(key)
    if shared is not None:
        return shared
    shared = attrs.copy()
    if len(_ATTR_POOL) < _ATTR_POOL_LIMIT:
        _ATTR_POOL[key] = shared
    return shared


def attr_pool_size() -> int:
    """How many distinct styles are currently shared. For tests."""
    return len(_ATTR_POOL)


class PackedRow:
    """A finished row, stored as text plus the runs of styling over it.

    Behaves like the ``list[Cell]`` it replaces — ``len``, indexing and
    iteration all work — so nothing that reads the grid needs to know the
    difference. Writing does not: a row in history is finished.
    """

    __slots__ = ("_cols", "_text", "_cells", "_widths", "_starts", "_attrs")

    def __init__(self, cols, text, cells, widths, starts, attrs):
        self._cols = cols
        #: Fast path: one character per cell, every one of them width 1.
        self._text = text
        #: Slow path: the per-cell strings, for rows with combining marks or
        #: double-width glyphs.
        self._cells = cells
        self._widths = widths
        #: Run starts and the style each run carries, as parallel sequences so
        #: a lookup is one bisect.
        self._starts = starts
        self._attrs = attrs

    # ── Sequence protocol ───────────────────────────────────────────

    def __len__(self) -> int:
        return self._cols

    @property
    def extent(self) -> int:
        """Columns actually stored — everything past this is blank."""
        return len(self._text) if self._text is not None else len(self._cells)

    def char_at(self, col: int) -> str:
        if not 0 <= col < self.extent:
            return " "
        if self._text is not None:
            return self._text[col]
        return self._cells[col]

    def width_at(self, col: int) -> int:
        if not 0 <= col < self.extent:
            return 1
        if self._widths is None:
            return 1
        return self._widths[col]

    def attrs_at(self, col: int) -> CellAttrs:
        if not self._starts or col < 0:
            return DEFAULT_ATTRS
        index = bisect_right(self._starts, col) - 1
        if index < 0:
            return DEFAULT_ATTRS
        return self._attrs[index]

    def __getitem__(self, col: int) -> Cell:
        if isinstance(col, slice):
            return [self[i] for i in range(*col.indices(self._cols))]
        if col < 0:
            col += self._cols
        if not 0 <= col < self._cols:
            raise IndexError(col)
        if col >= self.extent:
            # Past the stored text the row is blank and unstyled; a fresh Cell
            # keeps callers that poke at one from reaching a shared style.
            return Cell()
        return Cell(char=self.char_at(col),
                    attrs=self.attrs_at(col),
                    width=self.width_at(col))

    def __iter__(self):
        for col in range(self._cols):
            yield self[col]

    # ── Bulk readers, for the renderer and for search ───────────────

    def text(self) -> str:
        """The row's characters, with the trailing blanks left off."""
        if self._text is not None:
            return self._text
        return "".join(self._cells)

    def runs(self):
        """Yield ``(start, end, attrs)`` over the stored columns."""
        extent = self.extent
        if not self._starts:
            if extent:
                yield 0, extent, DEFAULT_ATTRS
            return
        for index, start in enumerate(self._starts):
            end = self._starts[index + 1] if index + 1 < len(self._starts) else extent
            if end > start:
                yield start, end, self._attrs[index]


DEFAULT_ATTRS = intern_attrs(CellAttrs())


def _packed_extent(cells) -> int:
    """Where the row stops being worth storing.

    Trailing blanks are dropped, but only genuinely invisible ones: a space
    carrying a background colour is a painted cell and has to survive.
    """
    end = len(cells)
    while end > 0:
        cell = cells[end - 1]
        if cell.char not in (" ", "") or cell.attrs.bg or cell.attrs.reverse:
            break
        end -= 1
    return end


def pack_row(cells, cols: Optional[int] = None) -> PackedRow:
    """Pack a finished grid row into its compact form."""
    if isinstance(cells, PackedRow):
        return cells
    total = len(cells) if cols is None else cols
    extent = _packed_extent(cells)

    starts: list = []
    attrs: list = []
    previous_key = None
    simple = True
    widths = bytearray(extent)

    previous_attrs = None
    for col in range(extent):
        cell = cells[col]
        char = cell.char
        width = cell.width
        widths[col] = width if 0 <= width <= 255 else 1
        if len(char) != 1 or width != 1:
            simple = False
        cell_attrs = cell.attrs
        # Styles are interned as they are written, so a whole run of cells
        # normally shares one object and identity settles it without building
        # a comparison key per character.
        if cell_attrs is previous_attrs:
            continue
        key = cell_attrs.key()
        if key != previous_key:
            starts.append(col)
            attrs.append(intern_attrs(cell_attrs))
            previous_key = key
        previous_attrs = cell_attrs

    # An entirely default-styled row needs no runs at all, which is the common
    # case and the one worth making free.
    if len(attrs) == 1 and attrs[0] is DEFAULT_ATTRS:
        starts, attrs = [], []

    if simple:
        text = "".join(cells[col].char for col in range(extent))
        return PackedRow(total, text, None, None, tuple(starts), tuple(attrs))

    return PackedRow(
        total, None,
        tuple(cells[col].char for col in range(extent)),
        bytes(widths),
        tuple(starts), tuple(attrs),
    )


def row_text(row) -> str:
    """Characters of a row, packed or not, without trailing blanks."""
    if isinstance(row, PackedRow):
        return row.text().rstrip()
    return "".join(cell.char for cell in row).rstrip()
