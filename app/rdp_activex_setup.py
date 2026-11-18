"""One-time Microsoft RDP ActiveX bootstrap/registration helpers."""

from __future__ import annotations

import json
import os
import sys
import ctypes
import subprocess
from datetime import datetime, timezone
from pathlib import Path


RPMOTE_DIR = Path.home() / ".rpmote"
BOOTSTRAP_FILE = RPMOTE_DIR / "rdp_activex_bootstrap.json"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_registered() -> bool:
    """Check whether Microsoft RDP ActiveX can be instantiated."""
    try:
        import win32com.client  # type: ignore

        win32com.client.Dispatch("MsTscAx.MsTscAx")
        return True
    except Exception:
        return False


def _qt_ax_available() -> tuple[bool, str]:
    """Check whether Qt ActiveX bindings are importable in this runtime."""
    # Prime loader search paths and attempt explicit Ax DLL preload first.
    candidates = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)
        candidates.extend([base, base / "PyQt6" / "Qt6" / "bin"])

    try:
        import PyQt6

        candidates.append(Path(PyQt6.__file__).resolve().parent / "Qt6" / "bin")
    except Exception:
        pass

    for d in candidates:
        if not d.exists():
            continue
        try:
            os.add_dll_directory(str(d))
        except Exception:
            pass
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        for dll in ("Qt6AxBase.dll", "Qt6AxContainer.dll"):
            p = d / dll
            if p.exists():
                try:
                    ctypes.WinDLL(str(p))
                except Exception:
                    pass

    try:
        from PyQt6.QAxContainer import QAxWidget  # noqa: F401

        return True, "ok"
    except Exception as e:
        return False, str(e)


def _register_dll(path: str) -> tuple[bool, str]:
    """Attempt to register a COM DLL using regsvr32."""
    if not os.path.exists(path):
        return False, f"Missing: {path}"

    cmd = ["regsvr32", "/s", path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            return True, f"Registered: {path}"
        return False, f"regsvr32 failed ({proc.returncode}): {path}"
    except Exception as e:
        return False, f"Error running regsvr32 on {path}: {e}"


def ensure_activex_registered_once(force: bool = False) -> dict:
    """
    Run one-time bootstrap for RDP ActiveX registration.

    This is intentionally one-shot and writes a marker file so it does not
    repeat on later startups.
    """
    RPMOTE_DIR.mkdir(parents=True, exist_ok=True)

    if BOOTSTRAP_FILE.exists() and not force:
        try:
            return json.loads(BOOTSTRAP_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"already_bootstrapped": True, "status": "unknown"}

    result: dict = {
        "already_bootstrapped": False,
        "ran_at": _iso_now(),
        "qt_ax_present": False,
        "qt_ax_error": "",
        "registered_before": False,
        "registered_after": False,
        "attempts": [],
        "status": "attempted",
    }

    qt_ok, qt_err = _qt_ax_available()
    result["qt_ax_present"] = qt_ok
    result["qt_ax_error"] = qt_err

    result["registered_before"] = _is_registered()

    if not result["registered_before"]:
        for dll in [r"C:\Windows\System32\mstscax.dll", r"C:\Windows\SysWOW64\mstscax.dll"]:
            ok, msg = _register_dll(dll)
            result["attempts"].append({"dll": dll, "ok": ok, "message": msg})

    result["registered_after"] = _is_registered()
    if not qt_ok:
        result["status"] = "qt_ax_missing"
    elif result["registered_after"]:
        result["status"] = "ok"
    else:
        result["status"] = "needs_admin_or_unavailable"

    try:
        BOOTSTRAP_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception:
        pass

    return result

