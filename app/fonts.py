"""The bundled Meslo Nerd Fonts, registered once per process.

The Meslo folder ships 72 faces (about 195 MB), but RPMote only ever asks for
two families -- MesloLGS and MesloLGL Nerd Font Mono, the terminal and file
browser faces -- so only their eight files are loaded.  Registering all 72 at
every launch used to read 195 MB from disk before the login screen could
appear, and far longer than that on a cold start, with an antivirus scan of
each file.
"""

from __future__ import annotations

import os

#: File-name prefixes of the faces RPMote uses: Regular, Bold, Italic and
#: BoldItalic of each.
FAMILY_PREFIXES = ("MesloLGSNerdFontMono-", "MesloLGLNerdFontMono-")

_registered = False


def bundled_font_files(directory: str) -> list[str]:
    """Paths of the faces to register, from the Meslo folder at *directory*."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(
        os.path.join(directory, name) for name in names
        if name.lower().endswith(".ttf") and name.startswith(FAMILY_PREFIXES)
    )


def register_bundled_fonts() -> int:
    """Register the Meslo faces with Qt, once.  Returns how many were added.

    Needs a QGuiApplication; safe to call again (later calls do nothing).
    """
    global _registered
    if _registered:
        return 0
    _registered = True
    from PyQt6.QtGui import QFontDatabase

    from app.resource_paths import get_resource_path

    added = 0
    for path in bundled_font_files(get_resource_path("Meslo")):
        if QFontDatabase.addApplicationFont(path) >= 0:
            added += 1
    return added
