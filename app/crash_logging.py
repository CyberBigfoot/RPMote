"""Crash logging and diagnostics for hard-to-reproduce failures."""
from __future__ import annotations

import atexit
import faulthandler
import logging
import os
import signal
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import TextIO

_LOGGER = logging.getLogger("rpmote.crash")
_LOGGING_READY = False
_LOG_PATH: Path | None = None
_DUMP_DIR: Path | None = None
_FAULT_FILE: TextIO | None = None
_QT_HANDLER_INSTALLED = False
_QT_HANDLER_REF = None


def _candidate_log_dirs(app_name: str) -> list[Path]:
    candidates: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / app_name / "logs")

    home = Path.home()
    candidates.append(home / f".{app_name.lower()}" / "logs")
    candidates.append(Path.cwd() / "crash_logs")
    candidates.append(Path(__file__).resolve().parents[1] / "crash_logs")
    return candidates


def _pick_log_dir(app_name: str) -> Path:
    for candidate in _candidate_log_dirs(app_name):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            test_file = candidate / ".write_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
            return candidate
        except Exception:
            continue
    fallback = Path.cwd() / "crash_logs"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _flush_all_handlers() -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        try:
            handler.flush()
        except Exception:
            pass


def _configure_python_hooks() -> None:
    previous_excepthook = sys.excepthook

    def _excepthook(exc_type, exc_value, exc_tb):
        _LOGGER.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
        _flush_all_handlers()
        if previous_excepthook:
            previous_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    if hasattr(threading, "excepthook"):
        previous_thread_excepthook = threading.excepthook

        def _thread_excepthook(args: threading.ExceptHookArgs):
            _LOGGER.error(
                "Unhandled thread exception in %s",
                getattr(args.thread, "name", "<unknown>"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
            _flush_all_handlers()
            if previous_thread_excepthook:
                previous_thread_excepthook(args)

        threading.excepthook = _thread_excepthook


def _configure_faulthandler() -> None:
    global _FAULT_FILE

    if _LOG_PATH is None:
        return

    _FAULT_FILE = open(_LOG_PATH, "a", encoding="utf-8", buffering=1)
    try:
        faulthandler.enable(_FAULT_FILE, all_threads=True)
    except Exception as exc:
        _LOGGER.warning("faulthandler.enable failed: %s", exc)

    for sig_name in ("SIGABRT", "SIGSEGV", "SIGILL", "SIGFPE", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            faulthandler.register(sig, file=_FAULT_FILE, all_threads=True, chain=True)
        except Exception:
            pass


#: Registry values an earlier build wrote to the *unscoped* LocalDumps keys,
#: which made Windows take a full memory dump of every crashing process the
#: user ran — and full dumps of RPMote carry the open vault in plain text.
_WER_BASE = r"Software\Microsoft\Windows\Windows Error Reporting\LocalDumps"
_WER_VALUES = ("DumpFolder", "DumpType", "DumpCount")
#: Names an earlier build registered per-exe; only the frozen exe stays.
_WER_LEGACY_EXES = ("python.exe", "pythonw.exe")
#: The frozen exe's keys from an earlier build asked for full dumps; they
#: are turned down to minidumps rather than removed.
_WER_APP_EXES = ("RPMOTE.exe", "RPMote.exe")


def _wer_points_at_us(winreg, hive, key_path: str) -> bool:
    """True if this LocalDumps key was written by RPMote (its DumpFolder is
    ours), so cleaning it up cannot disturb a setting somebody else made."""
    try:
        with winreg.OpenKey(hive, key_path) as key:
            folder, _ = winreg.QueryValueEx(key, "DumpFolder")
    except OSError:
        return False
    try:
        return Path(str(folder)).resolve() == Path(_DUMP_DIR).resolve()
    except Exception:
        return False


def _remove_legacy_wer_config(winreg) -> None:
    """Undo the global and interpreter-wide dump settings older builds set."""
    for hive, name in ((winreg.HKEY_CURRENT_USER, "HKCU"),
                       (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
        if _wer_points_at_us(winreg, hive, _WER_BASE):
            try:
                with winreg.OpenKey(hive, _WER_BASE, 0, winreg.KEY_SET_VALUE) as key:
                    for value in _WER_VALUES:
                        try:
                            winreg.DeleteValue(key, value)
                        except OSError:
                            pass
                _LOGGER.info("Removed global WER LocalDumps values (%s)", name)
            except OSError as exc:
                _LOGGER.info("Could not remove global WER LocalDumps (%s): %s", name, exc)
        for exe in _WER_LEGACY_EXES:
            sub = f"{_WER_BASE}\\{exe}"
            if _wer_points_at_us(winreg, hive, sub):
                try:
                    winreg.DeleteKey(hive, sub)
                    _LOGGER.info("Removed WER LocalDumps for %s (%s)", exe, name)
                except OSError as exc:
                    _LOGGER.info("Could not remove WER LocalDumps for %s (%s): %s",
                                 exe, name, exc)
        for exe in _WER_APP_EXES:
            sub = f"{_WER_BASE}\\{exe}"
            if _wer_points_at_us(winreg, hive, sub):
                try:
                    with winreg.OpenKey(hive, sub, 0, winreg.KEY_SET_VALUE) as key:
                        winreg.SetValueEx(key, "DumpType", 0, winreg.REG_DWORD, 1)
                        winreg.SetValueEx(key, "DumpCount", 0, winreg.REG_DWORD, 3)
                except OSError:
                    pass


def _configure_windows_local_dumps() -> None:
    """Ask Windows for a crash dump of RPMote — and only RPMote, and only a
    minidump.

    A full dump (DumpType 2) is the process's whole heap, which while the
    vault is open holds every password and 2FA secret in clear text; a
    minidump (DumpType 1) is stacks and module lists, enough to see where a
    native crash happened.  The key is scoped to the frozen exe's name, so a
    plain ``python.exe`` run — and every other program — is left alone.
    """
    if _DUMP_DIR is None or os.name != "nt":
        return

    try:
        import winreg
    except Exception:
        _LOGGER.info("winreg unavailable; skipping WER LocalDumps setup")
        return

    _remove_legacy_wer_config(winreg)

    if not getattr(sys, "frozen", False):
        return
    exe_name = Path(sys.executable).name
    key_path = f"{_WER_BASE}\\{exe_name}"
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "DumpFolder", 0, winreg.REG_EXPAND_SZ, str(_DUMP_DIR))
            winreg.SetValueEx(key, "DumpType", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(key, "DumpCount", 0, winreg.REG_DWORD, 3)
        _LOGGER.info("WER LocalDumps configured for %s (minidump) at %s", exe_name, _DUMP_DIR)
    except Exception as exc:
        _LOGGER.info("WER LocalDumps setup skipped (%s): %s", exe_name, exc)


def _platform_description() -> str:
    """What platform.platform() says, without its cost.

    On Windows that call shells out to ``cmd /c ver`` -- a whole process
    started, 40-70 ms on every launch before the login screen.  The kernel
    version is right there in sys.getwindowsversion().
    """
    getwinver = getattr(sys, "getwindowsversion", None)
    if getwinver is not None:
        v = getwinver()
        release = "11" if (v.major, v.build) >= (10, 22000) else str(v.major)
        return f"Windows-{release}-{v.major}.{v.minor}.{v.build}-SP{v.service_pack_major}"
    import platform
    return platform.platform()


def _write_session_header(app_name: str) -> None:
    _LOGGER.info("=" * 80)
    _LOGGER.info("Crash logging initialized")
    _LOGGER.info("App: %s", app_name)
    _LOGGER.info("Time: %s", datetime.now().isoformat(timespec="seconds"))
    _LOGGER.info("PID: %s", os.getpid())
    _LOGGER.info("Executable: %s", sys.executable)
    _LOGGER.info("argv: %s", sys.argv)
    _LOGGER.info("CWD: %s", os.getcwd())
    _LOGGER.info("Python: %s", sys.version.replace("\n", " "))
    _LOGGER.info("Platform: %s", _platform_description())
    _LOGGER.info("Crash log path: %s", _LOG_PATH)
    _LOGGER.info("Dump directory: %s", _DUMP_DIR)


def setup_crash_logging(app_name: str = "RPMote") -> tuple[Path, Path]:
    """Initialize crash logging once and return (log_path, dump_dir)."""
    global _LOGGING_READY, _LOG_PATH, _DUMP_DIR

    if _LOGGING_READY and _LOG_PATH and _DUMP_DIR:
        return _LOG_PATH, _DUMP_DIR

    log_dir = _pick_log_dir(app_name)
    dump_dir = log_dir / "dumps"
    dump_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"crash-{timestamp}-pid{os.getpid()}.log"

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if not getattr(sys, "frozen", False):
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    logging.captureWarnings(True)

    _LOG_PATH = log_path
    _DUMP_DIR = dump_dir

    _configure_python_hooks()
    _configure_faulthandler()
    _configure_windows_local_dumps()
    _write_session_header(app_name)

    def _on_exit() -> None:
        _LOGGER.info("Process exit")
        _flush_all_handlers()
        if _FAULT_FILE is not None:
            try:
                _FAULT_FILE.flush()
                _FAULT_FILE.close()
            except Exception:
                pass

    atexit.register(_on_exit)
    _LOGGING_READY = True
    return log_path, dump_dir


def install_qt_message_handler() -> None:
    """Install a Qt message handler so Qt warnings/errors hit our crash log."""
    global _QT_HANDLER_INSTALLED, _QT_HANDLER_REF

    if _QT_HANDLER_INSTALLED:
        return

    try:
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception as exc:
        _LOGGER.warning("Qt message handler unavailable: %s", exc)
        return

    msg_type_names = {
        QtMsgType.QtDebugMsg: "DEBUG",
        QtMsgType.QtInfoMsg: "INFO",
        QtMsgType.QtWarningMsg: "WARNING",
        QtMsgType.QtCriticalMsg: "CRITICAL",
        QtMsgType.QtFatalMsg: "FATAL",
    }

    def _qt_message_handler(msg_type, context, message):
        level_name = msg_type_names.get(msg_type, "INFO")
        level = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "CRITICAL": logging.ERROR,
            "FATAL": logging.CRITICAL,
        }.get(level_name, logging.INFO)

        location = ""
        if context and getattr(context, "file", None):
            location = f" ({context.file}:{getattr(context, 'line', 0)})"

        logging.getLogger("rpmote.qt").log(level, "Qt[%s] %s%s", level_name, message, location)

        if msg_type == QtMsgType.QtFatalMsg:
            _flush_all_handlers()

    qInstallMessageHandler(_qt_message_handler)
    _QT_HANDLER_REF = _qt_message_handler
    _QT_HANDLER_INSTALLED = True
    _LOGGER.info("Qt message handler installed")


def log_runtime_event(event: str, **fields) -> None:
    if fields:
        kv = ", ".join(f"{k}={v!r}" for k, v in fields.items())
        logging.getLogger("rpmote.event").info("%s | %s", event, kv)
    else:
        logging.getLogger("rpmote.event").info("%s", event)


def get_crash_log_path() -> str | None:
    return str(_LOG_PATH) if _LOG_PATH else None


def get_crash_dump_dir() -> str | None:
    return str(_DUMP_DIR) if _DUMP_DIR else None
