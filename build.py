"""Build RPMote: PyInstaller bundle, verification, then the Inno Setup installer.

    .venv\\Scripts\\python.exe build.py            # bundle + verify
    .venv\\Scripts\\python.exe build.py --installer  # …and compile the installer
    .venv\\Scripts\\python.exe build.py --installer --sign   # …Authenticode-signed
    .venv\\Scripts\\python.exe build.py --clean --installer  # clean export + installer

Signing uses signtool from the Windows SDK and a code-signing certificate:
set RPMOTE_SIGN_PFX (+ RPMOTE_SIGN_PASSWORD) for a .pfx file, or
RPMOTE_SIGN_THUMBPRINT for a certificate in the Windows certificate store.

The verification step exists because a bundle can look fine while being missing
Qt WebEngine entirely — the NAS / iDRAC browser imports it lazily, so a broken
build starts up perfectly and only fails when a user opens that tab.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "RPMote"
INTERNAL = DIST / "_internal"
BUILD_CACHE = ROOT / "build"
INSTALLER_OUTPUT = ROOT / "Output" / "RPMote_Setup.exe"

# Paths that must exist in the finished bundle, relative to _internal.
REQUIRED_FILES = (
    "PyQt6/QtWebEngineWidgets.pyd",
    "PyQt6/QtWebEngineCore.pyd",
    "PyQt6/Qt6/bin/Qt6WebEngineCore.dll",
    "PyQt6/Qt6/bin/QtWebEngineProcess.exe",
    "PyQt6/Qt6/resources/icudtl.dat",
    "PyQt6/Qt6/resources/qtwebengine_resources.pak",
    "PyQt6/Qt6/resources/v8_context_snapshot.bin",
    "PyQt6/Qt6/translations/qtwebengine_locales/en-US.pak",
)

# The terminal faces app/fonts.py registers (the spec bundles only those).
REQUIRED_FONTS = (
    "Meslo/MesloLGSNerdFontMono-Regular.ttf",
    "Meslo/MesloLGLNerdFontMono-Regular.ttf",
)

# These must be directly beside pythonXY.dll. Qt's nested copies are not in
# the bootloader's DLL search path when it first loads the Python interpreter.
REQUIRED_WINDOWS_RUNTIME = (
    "vcruntime140.dll",
    "vcruntime140_1.dll",
    "msvcp140.dll",
)

#: Where Inno Setup's compiler is usually found: a machine-wide install, or
#: the per-user one its installer offers (under %LOCALAPPDATA%\Programs).
#: RPMOTE_ISCC points at any other copy.
INNO_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
    Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
)


def find_iscc() -> Path | None:
    override = os.environ.get("RPMOTE_ISCC", "").strip()
    if override:
        return Path(override) if Path(override).is_file() else None
    on_path = shutil.which("ISCC")
    if on_path:
        return Path(on_path)
    return next((c for c in INNO_CANDIDATES if c.is_file()), None)
TIMESTAMP_URL = "http://timestamp.digicert.com"


def app_version() -> str:
    """APP_VERSION from app/version.py, read without importing the app."""
    text = (ROOT / "app" / "version.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("APP_VERSION"):
            return line.split("=", 1)[1].strip().strip("\"'")
    fail("APP_VERSION not found in app/version.py")


def fail(message: str) -> None:
    print(f"\n[BUILD FAILED] {message}\n", file=sys.stderr)
    raise SystemExit(1)


def ensure_project_venv() -> None:
    """Re-run this script with the project's .venv Python if started with another.

    The bundle takes PyInstaller and every package from whichever Python runs
    the build, and the global Python here has its own, older PyInstaller: an
    exe built with it, mixed with the cache a .venv build had left in build/,
    died at launch ("module 'pyimod02_importers' has no attribute
    'PyiFrozenImporter'").  RPMOTE_ALLOW_ANY_PYTHON=1 builds with whatever
    Python started it.
    """
    venv = ROOT / ".venv"
    venv_python = venv / "Scripts" / "python.exe"
    if not venv_python.is_file() or os.environ.get("RPMOTE_ALLOW_ANY_PYTHON") == "1":
        return
    if Path(sys.prefix).resolve() == venv.resolve():
        return
    print(f"  {sys.executable} is not the project's virtualenv;")
    print(f"  re-running with {venv_python}\n", flush=True)
    result = subprocess.run([str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]],
                            cwd=ROOT)
    raise SystemExit(result.returncode)


def check_interpreter() -> None:
    """The bundle only contains what this interpreter can see."""
    try:
        from ruamel.yaml import YAML  # noqa: F401
    except ImportError:
        fail("The YAML formatter is missing. Install the project's requirements before building.")
    try:
        import PyQt6.QtWebEngineWidgets  # noqa: F401
        from PyQt6.QtCore import PYQT_VERSION_STR
    except ImportError as exc:
        fail(
            f"PyQt6-WebEngine is not importable from {sys.executable}\n"
            f"  {exc}\n"
            "  Run this script with the project virtualenv:\n"
            "      .venv\\Scripts\\python.exe build.py"
        )

    try:
        from PyQt6.QtWebEngineCore import qWebEngineVersion
        web_version = qWebEngineVersion()
    except Exception:
        web_version = "unknown"

    try:
        import PyInstaller
        pyinstaller_version = PyInstaller.__version__
    except ImportError:
        fail(f"PyInstaller is not installed for {sys.executable}")

    print(f"  interpreter : {sys.executable}")
    print(f"  PyInstaller : {pyinstaller_version}")
    print(f"  PyQt6       : {PYQT_VERSION_STR}")
    print(f"  WebEngine   : {web_version}")

    # PyQt6-WebEngine must track PyQt6's minor version. A mismatch loads on the
    # build machine and then fails to import elsewhere.
    if web_version != "unknown":
        want = ".".join(PYQT_VERSION_STR.split(".")[:2])
        got = ".".join(str(web_version).split(".")[:2])
        if want != got:
            fail(
                f"PyQt6 {PYQT_VERSION_STR} and Qt WebEngine {web_version} disagree.\n"
                f"  Install a matching pair:  pip install PyQt6-WebEngine=={PYQT_VERSION_STR}"
            )


def run_pyinstaller(clean: bool) -> None:
    if clean:
        print("\n== Cleaning previous export ==")
        for path in (BUILD_CACHE, DIST):
            if path.exists():
                print(f"  removing {path}")
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        if INSTALLER_OUTPUT.exists():
            print(f"  removing {INSTALLER_OUTPUT}")
            INSTALLER_OUTPUT.unlink()

    cmd = [sys.executable, "-m", "PyInstaller", "RPMOTE.spec", "--noconfirm"]
    if clean:
        cmd.append("--clean")
    print("\n== PyInstaller ==")
    print("  " + " ".join(cmd))
    build_env = os.environ.copy()
    if os.name == "nt":
        # Resolve Windows' own DLLs before unrelated tools on PATH. Qt 6.11
        # imports Windows' unversioned ICU API; a Poppler/Conda icuuc.dll uses
        # different exports and makes the finished app fail to import QtWidgets.
        system_root = build_env.get("SystemRoot") or build_env.get("WINDIR")
        if system_root:
            system_dir = str(Path(system_root) / "System32")
            build_env["PATH"] = system_dir + os.pathsep + build_env.get("PATH", "")
    result = subprocess.run(cmd, cwd=ROOT, env=build_env)
    if result.returncode != 0:
        fail(f"PyInstaller exited with {result.returncode}")


def verify_windows_runtime() -> None:
    """Reject bundles that depend on a VC runtime installed on the build PC."""
    if sys.platform != "win32":
        return
    python_dll = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    required = (python_dll, *REQUIRED_WINDOWS_RUNTIME)
    missing = [name for name in required if not (INTERNAL / name).is_file()]
    for name in required:
        print(f"  [{'MISSING' if name in missing else 'ok':>7}] {name}")
    if missing:
        fail(
            "The Python / Visual C++ runtime is incomplete at _internal's root:\n"
            f"  {', '.join(missing)}\n"
            "  DLLs inside PyQt6/Qt6/bin do not satisfy the launcher's search path.\n"
            "  Rebuild with the updated RPMOTE.spec before creating an installer."
        )


def verify_bundle() -> None:
    print("\n== Verifying the bundle ==")
    if not INTERNAL.is_dir():
        fail(f"{INTERNAL} does not exist — did PyInstaller run?")

    verify_windows_runtime()

    missing = [name for name in REQUIRED_FILES if not (INTERNAL / name).is_file()]
    for name in REQUIRED_FILES:
        mark = "MISSING" if name in missing else "ok"
        print(f"  [{mark:>7}] {name}")

    locales = INTERNAL / "PyQt6/Qt6/translations/qtwebengine_locales"
    count = len(list(locales.glob("*.pak"))) if locales.is_dir() else 0
    print(f"  [{'ok' if count else 'MISSING':>7}] {count} WebEngine locale file(s)")

    if missing or not count:
        fail(
            "Qt WebEngine is not in the bundle. Installing this build would give\n"
            "  \"PyQt6-WebEngine is required but could not be loaded\" on the NAS tab."
        )
    print("\n  Qt WebEngine bundled correctly.")

    fonts_missing = [name for name in REQUIRED_FONTS if not (INTERNAL / name).is_file()]
    for name in REQUIRED_FONTS:
        print(f"  [{'MISSING' if name in fonts_missing else 'ok':>7}] {name}")
    if fonts_missing:
        fail("The terminal fonts are not in the bundle; terminals would fall back "
             "to a face without the Nerd Font icons.")


def build_installer() -> None:
    iscc = find_iscc()
    if iscc is None:
        fail("Inno Setup compiler (ISCC.exe) not found. Install Inno Setup 6, or "
             "set RPMOTE_ISCC to its ISCC.exe.")
    print("\n== Inno Setup ==")
    version = app_version()
    print(f"  Compiler    : {iscc}")
    print(f"  Version     : {version}")
    result = subprocess.run([str(iscc), f"/DMyAppVersion={version}", "installer.iss"], cwd=ROOT)
    if result.returncode != 0:
        fail(f"ISCC exited with {result.returncode}")
    print(f"\n  Installer written to {ROOT / 'Output'}")


def find_signtool() -> Path | None:
    found = shutil.which("signtool")
    if found:
        return Path(found)
    kits = Path(r"C:\Program Files (x86)\Windows Kits\10\bin")
    candidates = sorted(kits.glob("*/x64/signtool.exe"), reverse=True) if kits.is_dir() else []
    return candidates[0] if candidates else None


def sign_files(paths) -> None:
    """Authenticode-sign *paths*, so SmartScreen and Defender see a publisher."""
    pfx = os.environ.get("RPMOTE_SIGN_PFX", "").strip()
    thumbprint = os.environ.get("RPMOTE_SIGN_THUMBPRINT", "").strip()
    if not pfx and not thumbprint:
        fail("--sign needs RPMOTE_SIGN_PFX (a .pfx file) or RPMOTE_SIGN_THUMBPRINT "
             "(a certificate in the Windows store).")
    signtool = find_signtool()
    if signtool is None:
        fail("signtool.exe not found — install the Windows SDK signing tools.")
    print("\n== Signing ==")
    for path in paths:
        cmd = [str(signtool), "sign", "/fd", "SHA256", "/td", "SHA256",
               "/tr", TIMESTAMP_URL]
        if pfx:
            cmd += ["/f", pfx]
            password = os.environ.get("RPMOTE_SIGN_PASSWORD", "")
            if password:
                cmd += ["/p", password]
        else:
            cmd += ["/sha1", thumbprint]
        cmd.append(str(path))
        print(f"  {path.name}")
        result = subprocess.run(cmd, cwd=ROOT)
        if result.returncode != 0:
            fail(f"signtool failed on {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", action="store_true",
                        help="also compile installer.iss with Inno Setup")
    parser.add_argument("--clean", action="store_true",
                        help="remove the previous bundle, build cache and installer "
                             "before exporting")
    parser.add_argument("--skip-build", action="store_true",
                        help="only verify the bundle already in dist/")
    parser.add_argument("--sign", action="store_true",
                        help="Authenticode-sign RPMote.exe (and the installer)")
    args = parser.parse_args()

    print("== Environment ==")
    ensure_project_venv()
    check_interpreter()

    if not args.skip_build:
        run_pyinstaller(args.clean)
    verify_bundle()
    if args.sign:
        sign_files([DIST / "RPMote.exe"])

    if args.installer:
        build_installer()
        if args.sign:
            sign_files([ROOT / "Output" / "RPMote_Setup.exe"])

    print("\nDone.")


if __name__ == "__main__":
    main()
