"""
Runtime (pre-login) settings for RPMote.

These settings are intentionally stored outside the encrypted vault so the app
can apply startup-only behavior (like OpenGL/GPU backend choice) before the
login dialog appears.
"""

from __future__ import annotations

import json
from pathlib import Path


RPMOTE_DIR = Path.home() / ".rpmote"
RUNTIME_SETTINGS_FILE = RPMOTE_DIR / "runtime_settings.json"

DEFAULT_RUNTIME_SETTINGS = {
    "hardware_acceleration_enabled": True,
}


def _normalize_runtime_settings(raw: dict | None) -> dict:
    data = dict(DEFAULT_RUNTIME_SETTINGS)
    if isinstance(raw, dict):
        data.update(raw)

    data["hardware_acceleration_enabled"] = bool(
        data.get("hardware_acceleration_enabled", True)
    )
    return data


def load_runtime_settings() -> dict:
    """Load runtime settings from disk (with safe defaults)."""
    try:
        if not RUNTIME_SETTINGS_FILE.exists():
            return dict(DEFAULT_RUNTIME_SETTINGS)
        payload = json.loads(RUNTIME_SETTINGS_FILE.read_text(encoding="utf-8"))
        return _normalize_runtime_settings(payload)
    except Exception:
        return dict(DEFAULT_RUNTIME_SETTINGS)


def save_runtime_settings(settings: dict) -> dict:
    """Persist runtime settings to disk and return normalized saved values."""
    current = load_runtime_settings()
    current.update(settings or {})
    normalized = _normalize_runtime_settings(current)

    try:
        RPMOTE_DIR.mkdir(parents=True, exist_ok=True)
        RUNTIME_SETTINGS_FILE.write_text(
            json.dumps(normalized, indent=2),
            encoding="utf-8",
        )
    except Exception:
        # Keep app resilient even if disk write fails.
        pass

    return normalized


def is_hardware_acceleration_enabled(default: bool = True) -> bool:
    """Return hardware acceleration preference for startup."""
    try:
        return bool(load_runtime_settings().get("hardware_acceleration_enabled", default))
    except Exception:
        return bool(default)
