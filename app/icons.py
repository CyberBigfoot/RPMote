"""
RPMote icon set — one consistent family of line icons, tinted at runtime.

Every icon is drawn on a 24×24 grid with a 2 px round-capped stroke, so they
sit together the way the emoji they replace never did (emoji render in full
colour from Segoe UI Emoji, ignore the theme, and change shape between
Windows builds).

Use :func:`icon` for widgets, :func:`pixmap` for painting, and
:func:`file_url` where a stylesheet needs a path (combo arrows, checkmarks).
``{c}`` in the markup is replaced by the tint, for the few icons that need a
filled detail.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from PyQt6.QtCore import QByteArray, QRectF, QSize, Qt
from PyQt6.QtGui import QIcon, QIconEngine, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer

#: Neutral tint for icons on the (always dark) chrome.
DEFAULT_COLOR = "#cbd5e1"

_ICONS: dict[str, str] = {
    # Protocols and places
    "terminal": '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M7 9.5l3 2.5-3 2.5"/><path d="M12.5 15H17"/>',
    "folder": '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.5h7.5A2.5 2.5 0 0 1 21 10v7.5a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z"/>',
    "monitor": '<rect x="2.5" y="4" width="19" height="13" rx="2"/><path d="M8.5 21h7M12 17v4"/>',
    "server": '<rect x="3" y="3.5" width="18" height="7" rx="2"/><rect x="3" y="13.5" width="18" height="7" rx="2"/><path d="M7 7h.01M7 17h.01"/>',
    "nas": '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M3 13.5h18"/><path d="M7 16.5h.01M10.5 16.5h.01"/>',
    "cloud": '<path d="M17.5 19H7a5 5 0 1 1 .9-9.92A6 6 0 0 1 19.5 11a4 4 0 0 1-2 8z"/>',
    "globe": '<circle cx="12" cy="12" r="9"/><path d="M3 12h18"/><path d="M12 3a14 14 0 0 1 0 18a14 14 0 0 1 0-18z"/>',
    "vnc": '<rect x="2.5" y="4" width="19" height="13" rx="2"/><path d="M8.5 21h7M12 17v4"/><path d="M12 13.5V8M9.5 10.5L12 8l2.5 2.5"/>',
    "serial": '<path d="M3 8h18l-2 8H5z"/><path d="M7.5 11h.01M10.5 11h.01M13.5 11h.01M16.5 11h.01M9 13.5h.01M12 13.5h.01M15 13.5h.01"/>',
    "home": '<path d="M4 10.5L12 4l8 6.5V20H4z"/><path d="M10 20v-5h4v5"/>',
    # Actions
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "x": '<path d="M6 6l12 12M18 6L6 18"/>',
    "check": '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    "edit": '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
    "copy": '<rect x="8.5" y="8.5" width="12" height="12" rx="2"/><path d="M15.5 8.5v-3a2 2 0 0 0-2-2h-8a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h3"/>',
    "trash": '<path d="M4 7h16"/><path d="M9.5 7V4.5h5V7"/><path d="M6 7l1 13h10l1-13"/><path d="M10 11v5M14 11v5"/>',
    "refresh": '<path d="M20 11.5A8 8 0 0 0 6.3 6.3L4 8.5"/><path d="M4 4v4.5h4.5"/><path d="M4 12.5a8 8 0 0 0 13.7 5.2L20 15.5"/><path d="M20 20v-4.5h-4.5"/>',
    "restart": '<path d="M4 12a8 8 0 1 0 2.35-5.65L4 8.7"/><path d="M4 4v4.7h4.7"/>',
    "power": '<path d="M12 3v8"/><path d="M6.6 6.6a8 8 0 1 0 10.8 0"/>',
    "plug": '<path d="M9 2.5v4.5M15 2.5v4.5"/><path d="M6 7h12v3a6 6 0 0 1-12 0z"/><path d="M12 16v5.5"/>',
    "search": '<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.3-4.3"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1.2 1.2"/><path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1.2-1.2"/>',
    "download": '<path d="M12 4v11"/><path d="M7.5 10.5L12 15l4.5-4.5"/><path d="M4.5 19.5h15"/>',
    "upload": '<path d="M12 20V9"/><path d="M7.5 13.5L12 9l4.5 4.5"/><path d="M4.5 4.5h15"/>',
    "import": '<path d="M12 3.5v11"/><path d="M8 10.5l4 4 4-4"/><path d="M4 14.5v4a1.5 1.5 0 0 0 1.5 1.5h13a1.5 1.5 0 0 0 1.5-1.5v-4"/>',
    "export": '<path d="M12 14.5v-11"/><path d="M8 7.5l4-4 4 4"/><path d="M4 14.5v4a1.5 1.5 0 0 0 1.5 1.5h13a1.5 1.5 0 0 0 1.5-1.5v-4"/>',
    "external": '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v4.5a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 18.5v-11A1.5 1.5 0 0 1 5.5 6H10"/>',
    "undo": '<path d="M9 14L4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "play": '<path d="M7.5 4.5v15L19 12z" fill="{c}"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1.5"/>',
    "record": '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="3.5" fill="{c}"/>',
    "more": '<circle cx="5.5" cy="12" r="1.3" fill="{c}"/><circle cx="12" cy="12" r="1.3" fill="{c}"/><circle cx="18.5" cy="12" r="1.3" fill="{c}"/>',
    "split-v": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16"/>',
    "split-h": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 12h18"/>',
    "grid": '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>',
    "zoom-fit": '<path d="M4 9V5.5A1.5 1.5 0 0 1 5.5 4H9M15 4h3.5A1.5 1.5 0 0 1 20 5.5V9M20 15v3.5a1.5 1.5 0 0 1-1.5 1.5H15M9 20H5.5A1.5 1.5 0 0 1 4 18.5V15"/>',
    "logout": '<path d="M15 4h3.5A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5H15"/><path d="M10 16.5L5.5 12 10 7.5"/><path d="M5.5 12H15"/>',
    "arrow-right": '<path d="M5 12h14M13 6l6 6-6 6"/>',
    "chevron-down": '<path d="M6 9l6 6 6-6"/>',
    "chevron-up": '<path d="M6 15l6-6 6 6"/>',
    "chevron-right": '<path d="M9 6l6 6-6 6"/>',
    "chevron-left": '<path d="M15 6l-6 6 6 6"/>',
    # Security and status
    "lock": '<rect x="4.5" y="10.5" width="15" height="10" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 8 0v3.5"/>',
    "unlock": '<rect x="4.5" y="10.5" width="15" height="10" rx="2"/><path d="M8 10.5V7a4 4 0 0 1 7.75-1.4"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="M10.85 12.15L19 4"/><path d="M16 7l2.5 2.5"/><path d="M14 9l2 2"/>',
    "shield": '<path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.3-7.5 9.5-4.3-1.2-7.5-4.9-7.5-9.5V6z"/>',
    "shield-check": '<path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.3-7.5 9.5-4.3-1.2-7.5-4.9-7.5-9.5V6z"/><path d="M8.75 12l2.25 2.25 4.25-4.5"/>',
    "vpn": '<path d="M12 3l7.5 3v5.5c0 4.6-3.2 8.3-7.5 9.5-4.3-1.2-7.5-4.9-7.5-9.5V6z"/><rect x="9.25" y="11" width="5.5" height="4.5" rx=".75"/><path d="M10.5 11V9.75a1.5 1.5 0 0 1 3 0V11"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "eye-off": '<path d="M3 3l18 18"/><path d="M10.6 5.6A10 10 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a17 17 0 0 1-2.9 3.7"/><path d="M6.6 6.6C3.9 8.3 2.5 12 2.5 12S6 18.5 12 18.5a9.7 9.7 0 0 0 5.2-1.5"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    "warning": '<path d="M12 3.5L2.5 20h19z"/><path d="M12 10v4.5"/><path d="M12 17.5h.01"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><path d="M12 7.75h.01"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.25a2.6 2.6 0 0 1 5 .9c0 1.75-2.5 2.35-2.5 3.85"/><path d="M12 17h.01"/>',
    "activity": '<path d="M3 12h4l2.5-6 5 12 2.5-6h4"/>',
    "broadcast": '<circle cx="12" cy="12" r="2"/><path d="M8.2 8.2a5.5 5.5 0 0 0 0 7.6M15.8 8.2a5.5 5.5 0 0 1 0 7.6"/><path d="M5.3 5.3a9.5 9.5 0 0 0 0 13.4M18.7 5.3a9.5 9.5 0 0 1 0 13.4"/>',
    # People, notes, tools
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4.5 20.5a7.5 7.5 0 0 1 15 0"/>',
    "users": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M15.5 4.6a3.5 3.5 0 0 1 0 6.8"/><path d="M18 13.8a6.5 6.5 0 0 1 3.5 6.2"/>',
    "star": '<path d="M12 3.5l2.6 5.3 5.9.9-4.25 4.1 1 5.85L12 16.9l-5.25 2.75 1-5.85L3.5 9.7l5.9-.9z"/>',
    "star-filled": '<path d="M12 3.5l2.6 5.3 5.9.9-4.25 4.1 1 5.85L12 16.9l-5.25 2.75 1-5.85L3.5 9.7l5.9-.9z" fill="{c}"/>',
    "tag": '<path d="M3.5 12.4V4.5a1 1 0 0 1 1-1h7.9l8.1 8.1a1.4 1.4 0 0 1 0 2l-6.4 6.4a1.4 1.4 0 0 1-2 0z"/><path d="M8 8h.01"/>',
    "note": '<path d="M5 3.5h10l4 4v13H5z"/><path d="M15 3.5v4h4"/><path d="M8.5 12h7M8.5 15.5h7"/>',
    "history": '<path d="M3.5 12a8.5 8.5 0 1 0 2.5-6"/><path d="M3.5 4v4h4"/><path d="M12 7.5V12l3 2"/>',
    "archive": '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9"/><path d="M10 13h4"/>',
    "settings": '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/><circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    "palette": '<path d="M12 3a9 9 0 1 0 0 18c1.1 0 1.8-.8 1.8-1.7 0-.5-.2-.9-.5-1.2-.3-.3-.5-.7-.5-1.2 0-.9.8-1.7 1.7-1.7h2A4.5 4.5 0 0 0 21 10.6C21 6.4 17 3 12 3z"/><circle cx="7.5" cy="11.5" r="1" fill="{c}"/><circle cx="10" cy="7.5" r="1" fill="{c}"/><circle cx="14.5" cy="7.5" r="1" fill="{c}"/><circle cx="17" cy="11" r="1" fill="{c}"/>',
    "type": '<path d="M5 6.5V5h14v1.5"/><path d="M12 5v14"/><path d="M9 19h6"/>',
    "keyboard": '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6.5 10h.01M10 10h.01M14 10h.01M17.5 10h.01M8 14h8"/>',
    "camera": '<path d="M4 8h3l1.5-2.5h7L17 8h3a1 1 0 0 1 1 1v9.5a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><circle cx="12" cy="13.25" r="3.25"/>',
    "sidebar": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    "library": '<path d="M5 4.5A1.5 1.5 0 0 1 6.5 3H19v15H6.5A1.5 1.5 0 0 0 5 19.5z"/><path d="M5 19.5A1.5 1.5 0 0 0 6.5 21H19"/>',
    "code": '<path d="M8.5 7L3.5 12l5 5M15.5 7l5 5-5 5"/>',
    "file": '<path d="M6 3.5h8l4.5 4.5v12.5H6z"/><path d="M14 3.5V8h4.5"/>',
    "file-plus": '<path d="M6 3.5h8l4.5 4.5v12.5H6z"/><path d="M14 3.5V8h4.5"/><path d="M12.25 11.5v6M9.25 14.5h6"/>',
    "folder-plus": '<path d="M3 7.5A2.5 2.5 0 0 1 5.5 5H9l2 2.5h7.5A2.5 2.5 0 0 1 21 10v7.5a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z"/><path d="M12 10.5v6M9 13.5h6"/>',
    "pause": '<path d="M8.5 5.5v13M15.5 5.5v13"/>',
    "image": '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.75"/><path d="M20.5 16l-5-5-8.5 8.5"/>',
    "bolt": '<path d="M13 2.5L4.5 13.5H11l-1 8 8.5-11H12z"/>',
    # The RPMote mote (✦): a four-pointed star with concave sides.
    "mote": '<path d="M12 2.5C12.7 8.3 15.7 11.3 21.5 12C15.7 12.7 12.7 15.7 12 21.5C11.3 15.7 8.3 12.7 2.5 12C8.3 11.3 11.3 8.3 12 2.5z" fill="{c}"/>',
}

#: Which icon stands for each connection protocol.
PROTOCOL_ICONS = {
    "ssh": "terminal",
    "telnet": "terminal",
    "serial": "serial",
    "sftp": "folder",
    "ftp": "folder",
    "rdp": "monitor",
    "vnc": "vnc",
    "idrac": "server",
    "synology": "nas",
    "nas": "nas",
    "web": "globe",
    "azure_ps": "cloud",
}

_SVG_HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" '
             'viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="{w}" '
             'stroke-linecap="round" stroke-linejoin="round">')

_default_color = DEFAULT_COLOR
_icon_cache: dict[tuple, QIcon] = {}


def names() -> list[str]:
    return sorted(_ICONS)


def has(name: str) -> bool:
    return name in _ICONS


def protocol_icon_name(protocol: str) -> str:
    return PROTOCOL_ICONS.get(str(protocol or "").lower(), "terminal")


def default_color() -> str:
    return _default_color


def set_default_color(color: str):
    """Change the tint used when a caller does not name one."""
    global _default_color
    color = str(color or DEFAULT_COLOR)
    if color != _default_color:
        _default_color = color
        _icon_cache.clear()


def svg_markup(name: str, color: str | None = None, stroke: float = 2.0) -> str:
    color = color or _default_color
    body = _ICONS.get(name)
    if body is None:
        raise KeyError(f"unknown icon {name!r}")
    head = _SVG_HEAD.replace("{c}", color).replace("{w}", f"{stroke:g}")
    return head + body.replace("{c}", color) + "</svg>"


def svg_bytes(name: str, color: str | None = None, stroke: float = 2.0) -> bytes:
    return svg_markup(name, color, stroke).encode("utf-8")


class _SvgIconEngine(QIconEngine):
    """Renders the SVG at whatever size Qt asks for, so icons stay sharp at
    every display scale instead of being stretched from a fixed bitmap."""

    def __init__(self, data: bytes):
        super().__init__()
        self._data = data
        self._renderer = QSvgRenderer(QByteArray(data))

    def paint(self, painter: QPainter, rect, mode, state):
        painter.save()
        if mode == QIcon.Mode.Disabled:
            painter.setOpacity(painter.opacity() * 0.38)
        self._renderer.render(painter, QRectF(rect))
        painter.restore()

    def pixmap(self, size: QSize, mode, state) -> QPixmap:
        pm = QPixmap(size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.paint(p, pm.rect(), mode, state)
        p.end()
        return pm

    def clone(self):
        return _SvgIconEngine(self._data)


_FILE_TYPES = {
    "code": (".py", ".js", ".ts", ".html", ".css", ".json", ".yaml", ".yml", ".xml",
             ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".bat", ".cmd", ".go", ".rb",
             ".php", ".java", ".c", ".h", ".cpp", ".cs", ".rs", ".sql", ".toml"),
    "note": (".md", ".txt", ".log", ".csv", ".conf", ".cfg", ".ini", ".env", ".rtf"),
    "key": (".key", ".pem", ".crt", ".cer", ".pub", ".pfx", ".p12", ".ppk", ".kdbx"),
    "archive": (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".deb",
                ".rpm", ".iso", ".img", ".vhd", ".vhdx", ".vmdk"),
    "image": (".jpg", ".jpeg", ".png", ".gif", ".svg", ".bmp", ".webp", ".ico"),
}


def file_icon_name(filename: str) -> str:
    """The icon for a file in a file browser, by its extension."""
    name = str(filename or "").lower()
    dot = name.rfind(".")
    ext = name[dot:] if dot > 0 else ""
    for icon_name, extensions in _FILE_TYPES.items():
        if ext in extensions:
            return icon_name
    return "file"


#: Folders are amber in every file browser, as in Explorer and Finder.
FOLDER_COLOR = "#fbbf24"


def icon(name: str, color: str | None = None, stroke: float = 2.0) -> QIcon:
    """A tinted icon; an empty QIcon for an unknown name (never raises)."""
    color = color or _default_color
    key = (name, color, stroke)
    cached = _icon_cache.get(key)
    if cached is not None:
        return cached
    if name not in _ICONS:
        return QIcon()
    result = QIcon(_SvgIconEngine(svg_bytes(name, color, stroke)))
    _icon_cache[key] = result
    return result


def protocol_icon(protocol: str, color: str | None = None) -> QIcon:
    return icon(protocol_icon_name(protocol), color)


def pixmap(name: str, size: int, color: str | None = None,
           device_pixel_ratio: float = 1.0) -> QPixmap:
    """A rendered icon for code that paints rather than sets widget icons."""
    dpr = max(1.0, float(device_pixel_ratio or 1.0))
    px = max(1, int(round(size * dpr)))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(QByteArray(svg_bytes(name, color)))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(p, QRectF(0, 0, px, px))
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def _cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = Path(base) / "RPMote" / "icon-cache"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        path = Path(tempfile.gettempdir()) / "rpmote-icon-cache"
        path.mkdir(parents=True, exist_ok=True)
    return path


def file_url(name: str, color: str | None = None, stroke: float = 2.0) -> str:
    """A tinted copy on disk, as a forward-slash path a stylesheet can load.

    Qt style sheets only take file or resource paths, so the few places that
    need an icon there (combo arrows, checkbox ticks) get a cached file.
    """
    markup = svg_markup(name, color, stroke)
    digest = hashlib.sha1(markup.encode("utf-8")).hexdigest()[:12]
    target = _cache_dir() / f"{name}-{digest}.svg"
    if not target.exists():
        try:
            target.write_text(markup, encoding="utf-8")
        except OSError:
            return ""
    return str(target).replace("\\", "/")
