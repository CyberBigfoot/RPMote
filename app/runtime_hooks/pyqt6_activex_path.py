"""Runtime hook to ensure Qt ActiveX DLL directories are resolvable in frozen apps."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _add_dir(path: Path):
    if not path.exists():
        return
    try:
        os.add_dll_directory(str(path))
    except Exception:
        pass
    os.environ["PATH"] = str(path) + os.pathsep + os.environ.get("PATH", "")


if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    base = Path(sys._MEIPASS)
    _add_dir(base)
    _add_dir(base / "PyQt6" / "Qt6" / "bin")

