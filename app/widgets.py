"""
Small shared widgets and helpers for a consistent UI.

* :class:`ElidedLabel` — a label that shortens itself with "…" instead of
  pushing its neighbours around or being overdrawn by them.
* :func:`set_variant` — tags a button as ``primary`` / ``secondary`` /
  ``ghost`` / ``danger`` / ``icon`` so the global stylesheet can give the
  UI a hierarchy (one obvious main action per dialog).
* :func:`icon_button` — a compact, icon-only button with a tooltip.
* :func:`fit_to_screen` — keeps a window inside the usable screen area, so
  no dialog is taller than the monitor it opens on.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QFontMetrics, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractButton, QLabel, QPushButton, QSizePolicy, QToolButton, QTreeWidgetItem,
    QWidget,
)

from app import icons

#: Variants the global stylesheet knows how to draw.
VARIANTS = ("primary", "secondary", "ghost", "danger", "icon", "success")


class ElidedLabel(QLabel):
    """A single-line label that elides text that does not fit.

    ``text()`` still returns the full string; the tooltip shows it too when
    it had to be shortened.
    """

    def __init__(self, text: str = "", parent=None,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight):
        super().__init__(parent)
        self._full_text = ""
        self._mode = mode
        self._own_tooltip = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(24)
        self.setText(text)

    def setText(self, text):  # noqa: N802 — Qt naming
        self._full_text = str(text or "")
        self._refresh()

    def text(self) -> str:  # noqa: D401
        return self._full_text

    def setToolTip(self, tip):  # noqa: N802
        self._own_tooltip = str(tip or "")
        self._refresh()

    def setElideMode(self, mode: Qt.TextElideMode):  # noqa: N802
        self._mode = mode
        self._refresh()

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        hint = super().sizeHint()
        return QSize(fm.horizontalAdvance(self._full_text) + 4, hint.height())

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(self.minimumWidth(), hint.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self):
        fm = QFontMetrics(self.font())
        available = max(0, self.width() - 2)
        shown = fm.elidedText(self._full_text, self._mode, available)
        QLabel.setText(self, shown)
        if shown != self._full_text:
            tip = self._full_text if not self._own_tooltip else (
                f"{self._full_text}\n{self._own_tooltip}")
        else:
            tip = self._own_tooltip
        QLabel.setToolTip(self, tip)


class CollapsibleSection(QWidget):
    """A titled section whose body folds away.

    Used for the parts of a form most people never touch, so the common case
    fits on one screen without hiding anything for good.
    """

    def __init__(self, title: str, body: QWidget, expanded: bool = False,
                 parent=None):
        super().__init__(parent)
        from PyQt6.QtWidgets import QVBoxLayout

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.toggle = QToolButton(self)
        self.toggle.setObjectName("section_toggle")
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setIconSize(QSize(14, 14))
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.toggle.toggled.connect(self.setExpanded)
        layout.addWidget(self.toggle)
        self.body = body
        layout.addWidget(body)
        self.setExpanded(expanded)

    def isExpanded(self) -> bool:  # noqa: N802
        return self.toggle.isChecked()

    def setExpanded(self, expanded: bool):  # noqa: N802
        expanded = bool(expanded)
        if self.toggle.isChecked() != expanded:
            self.toggle.setChecked(expanded)
            return  # toggled() calls back in
        self.toggle.setIcon(icons.icon("chevron-down" if expanded else "chevron-right"))
        self.body.setVisible(expanded)


#: Glyphs older screens drew as icons, longest first, and the icon each meant.
GLYPH_ICONS = (
    ("＋📁", "folder-plus"), ("＋📄", "file-plus"), ("＋ Folder", "folder-plus"),
    ("＋ File", "file-plus"), ("❚❚", "pause"),
    ("↻", "refresh"), ("↺", "refresh"), ("🔄", "refresh"), ("⟳", "refresh"),
    ("✕", "x"), ("✖", "x"), ("❌", "x"), ("🗑", "trash"),
    ("⬆", "upload"), ("⬇", "download"), ("＋", "plus"), ("➕", "plus"),
    ("✎", "edit"), ("✏", "edit"), ("⌂", "home"), ("🏠", "home"),
    ("📁", "folder"), ("📂", "folder"), ("📄", "file"), ("📝", "note"),
    ("🔒", "lock"), ("🔐", "lock"), ("🔑", "key"), ("🔗", "link"), ("🌐", "globe"),
    ("🔍", "search"), ("⚡", "bolt"), ("⇉", "broadcast"), ("🚀", "play"),
    ("▶", "play"), ("◀", "chevron-left"), ("↑", "chevron-up"), ("˄", "chevron-up"),
    ("˅", "chevron-down"), ("📋", "copy"), ("💾", "download"), ("↗", "external"),
)
_GLYPH_TRAILERS = "\ufe0f\u200d "   # emoji variation selector, joiner, space


def split_glyph(text: str) -> tuple[str | None, str]:
    """``("refresh", "Reconnect")`` for ``"↻ Reconnect"``; ``(None, text)`` otherwise."""
    text = str(text or "")
    for glyph, name in GLYPH_ICONS:
        if text.startswith(glyph):
            rest = text[len(glyph):].lstrip(_GLYPH_TRAILERS)
            if glyph in ("＋ Folder", "＋ File"):
                rest = glyph[2:]
            return name, rest
    return None, text


def iconize(item, color: str | None = None):
    """Swap a button's or action's leading glyph (↻, 🗑, 📁…) for its icon."""
    name, rest = split_glyph(item.text())
    if name is not None:
        item.setIcon(icons.icon(name, color))
        item.setText(rest)
    return item


def iconize_children(root: QWidget):
    """Iconize every button under *root* that still has a glyph for an icon."""
    for button in root.findChildren(QAbstractButton):
        if button.icon().isNull():
            iconize(button)
    return root


def iconize_menu(menu):
    """Iconize a menu's actions (and its submenus') before it is shown."""
    for action in menu.actions():
        if action.isSeparator():
            continue
        if action.icon().isNull():
            iconize(action)
        if action.menu() is not None:
            iconize_menu(action.menu())
    return menu


def tree_item(columns, icon_name: str | None = None,
              color: str | None = None) -> QTreeWidgetItem:
    """A tree row with an icon in its first column."""
    item = QTreeWidgetItem([str(c) for c in columns])
    if icon_name:
        item.setIcon(0, icons.icon(icon_name, color))
    return item


def set_variant(button: QAbstractButton, variant: str) -> QAbstractButton:
    """Give *button* one of the stylesheet's visual roles."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown button variant {variant!r}")
    button.setProperty("variant", variant)
    style = button.style()
    if style is not None:
        style.unpolish(button)
        style.polish(button)
    button.update()
    return button


def icon_button(icon_name: str, tooltip: str, parent=None, *, size: int = 30,
                icon_size: int = 16, color: str | None = None,
                checkable: bool = False, tool: bool = False) -> QAbstractButton:
    """A square, icon-only button. Its tooltip is its label, so keep it real."""
    button = QToolButton(parent) if tool else QPushButton(parent)
    button.setIcon(icons.icon(icon_name, color))
    button.setIconSize(QSize(icon_size, icon_size))
    button.setFixedSize(size, size)
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip.split("(")[0].strip() or tooltip)
    button.setCheckable(checkable)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    set_variant(button, "icon")
    return button


def available_geometry(widget: QWidget | None = None):
    """The usable area (no taskbar) of the screen *widget* is on."""
    screen = None
    if widget is not None:
        try:
            screen = widget.screen()
        except Exception:
            screen = None
        parent = widget.parentWidget() if screen is None else None
        if screen is None and parent is not None:
            screen = parent.screen()
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    return screen.availableGeometry()


def fit_to_screen(widget: QWidget, margin: int = 24, center: bool = True):
    """Shrink *widget* to fit the usable screen area and keep it on screen.

    Dialogs were designed on tall monitors; on a 768-px-high laptop their
    buttons ended up below the taskbar. Call after the layout is built.
    """
    area = available_geometry(widget)
    max_w = max(200, area.width() - 2 * margin)
    max_h = max(200, area.height() - 2 * margin)
    hint = widget.sizeHint()
    width = min(max(widget.width(), hint.width()), max_w)
    height = min(max(widget.height(), hint.height()), max_h)
    widget.resize(width, height)
    if center:
        parent = widget.parentWidget()
        if parent is not None and parent.isVisible():
            target = parent.frameGeometry().center()
        else:
            target = area.center()
        geo = widget.frameGeometry()
        geo.moveCenter(target)
        x = min(max(area.left() + margin // 2, geo.left()),
                area.right() - geo.width() - margin // 2)
        y = min(max(area.top() + margin // 2, geo.top()),
                area.bottom() - geo.height() - margin // 2)
        widget.move(max(area.left(), x), max(area.top(), y))
