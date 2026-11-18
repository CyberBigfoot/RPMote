"""Helpers for locating bundled resources in dev and PyInstaller builds."""

from __future__ import annotations

import sys
from pathlib import Path


def _base_path() -> Path:
    """Return the runtime base path for resources."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def get_resource_path(*parts: str) -> str:
    """Return an absolute filesystem path to a bundled resource."""
    return str(_base_path().joinpath(*parts))


def get_qss_resource_url(*parts: str) -> str:
    """Return a Qt stylesheet-friendly absolute path (forward slashes)."""
    return get_resource_path(*parts).replace("\\", "/")

