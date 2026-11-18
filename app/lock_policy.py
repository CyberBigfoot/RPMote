"""Shared, backwards-compatible screen-lock preferences."""

IDLE_LOCK_SETTING = "idle_lock_minutes"
DEFAULT_IDLE_LOCK_MINUTES = 15
LOCK_ON_MINIMIZE = "lock_on_minimize"
LOCK_ON_WINDOWS_LOCK = "lock_on_windows_lock"
LOCK_ON_SLEEP = "lock_on_sleep"
LOCK_TIMEOUT_PRESETS = (0, 1, 5, 10, 15, 30, 60)
LOCK_TRIGGER_LABELS = {
    LOCK_ON_MINIMIZE: "When this app is minimized",
    LOCK_ON_WINDOWS_LOCK: "When Windows is locked",
    LOCK_ON_SLEEP: "When the computer sleeps or resumes",
}


def idle_lock_minutes(settings):
    try:
        value = int(settings.get(IDLE_LOCK_SETTING, DEFAULT_IDLE_LOCK_MINUTES))
    except (AttributeError, TypeError, ValueError, OverflowError):
        value = DEFAULT_IDLE_LOCK_MINUTES
    return max(0, min(value, 1440))


def normalize_lock_settings(settings=None):
    settings = settings if isinstance(settings, dict) else {}
    result = {IDLE_LOCK_SETTING: idle_lock_minutes(settings)}
    for key in LOCK_TRIGGER_LABELS:
        value = settings.get(key, False)
        result[key] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    return result


def timeout_label(minutes):
    return "Never (idle timeout)" if minutes == 0 else f"{minutes} minute{'s' if minutes != 1 else ''}"


def update_window_lock_settings(window, changes):
    """Persist before applying; a failed vault write leaves the live policy alone."""
    values = normalize_lock_settings({**window.vault.settings, **changes})
    window.vault.update_settings(values)
    controller = getattr(window, "idle_lock", None)
    if controller is not None:
        controller.apply_settings(values)
    return values
