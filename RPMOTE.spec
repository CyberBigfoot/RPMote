# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for RPMote.

BUILD WITH THE PROJECT VIRTUALENV:

    .venv\\Scripts\\python.exe -m PyInstaller RPMOTE.spec

or just run ``.venv\\Scripts\\python.exe build.py``, which does that for you.

Why this matters: PyInstaller bundles what it finds in the interpreter that
*runs* it. A build run from an interpreter without PyQt6-WebEngine produces an
app with no Qt WebEngine at all — and because the NAS/iDRAC browser imports it
lazily, nothing fails until a user opens that tab on a machine that doesn't
happen to have PyQt6-WebEngine installed. The preflight and post-build checks
below turn that into a loud build failure instead of a broken installer.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all

# Qt WebEngine pieces the packaged app cannot run without. Substrings, matched
# case-insensitively against the bundled destination paths.
REQUIRED_WEBENGINE = (
    "QtWebEngineWidgets",                  # the Python extension module
    "QtWebEngineCore",                     # ditto
    "Qt6WebEngineCore.dll",                # Chromium core
    "QtWebEngineProcess.exe",              # the out-of-process renderer
    "icudtl.dat",                          # ICU data
    "qtwebengine_resources.pak",           # Chromium resources
    "v8_context_snapshot.bin",             # V8 snapshot
    "qtwebengine_locales/en-US.pak",       # at least one locale
)


def _preflight():
    """Refuse to build from an interpreter that has no Qt WebEngine."""
    import importlib.util

    missing = [
        name
        for name in ("PyQt6.QtWebEngineWidgets", "PyQt6.QtWebEngineCore")
        if importlib.util.find_spec(name) is None
    ]
    if not missing:
        return

    raise SystemExit(
        "\n"
        "=========================================================\n"
        " RPMote build aborted: Qt WebEngine is missing\n"
        "=========================================================\n"
        f" Interpreter : {sys.executable}\n"
        f" Missing     : {', '.join(missing)}\n"
        "\n"
        " The NAS / iDRAC browser needs PyQt6-WebEngine. Building from\n"
        " this interpreter would produce an installer whose browser tab\n"
        " fails on every machine.\n"
        "\n"
        " Build with the project virtualenv instead:\n"
        "     .venv\\Scripts\\python.exe -m PyInstaller RPMOTE.spec\n"
        "\n"
        " If the virtualenv itself is missing the package:\n"
        "     .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n"
        "=========================================================\n"
    )


def _require_project_venv():
    """Refuse to build from any Python but the project's own .venv.

    A bundle takes PyInstaller, PyQt6 and every other package from whichever
    interpreter runs the build.  The global Python on the build machine has
    its own, older PyInstaller (6.11) and PyQt6, so an exe built from it is not
    the one that was tested -- and one such build also picked up the .venv
    build's cached loader and died at launch (see _clear_stale_loader_cache).
    Set RPMOTE_ALLOW_ANY_PYTHON=1 to build from another interpreter anyway.
    """
    venv = os.path.join(SPECPATH, '.venv')
    if not os.path.isdir(venv) or os.environ.get('RPMOTE_ALLOW_ANY_PYTHON') == '1':
        return
    here = os.path.normcase(os.path.realpath(sys.prefix))
    if here == os.path.normcase(os.path.realpath(venv)):
        return
    raise SystemExit(
        "\n"
        "=========================================================\n"
        " RPMote build aborted: wrong Python\n"
        "=========================================================\n"
        f" Interpreter : {sys.executable}\n"
        f" Expected    : {os.path.join(venv, 'Scripts', 'python.exe')}\n"
        "\n"
        " The bundle is built from whichever Python runs PyInstaller,\n"
        " and this is not the project's virtualenv (a bare\n"
        " `pyinstaller RPMOTE.spec` runs the global one). Build with:\n"
        "     .venv\\Scripts\\python.exe build.py\n"
        "\n"
        " Set RPMOTE_ALLOW_ANY_PYTHON=1 to build from it anyway.\n"
        "=========================================================\n"
    )


def _clear_stale_loader_cache():
    """Start the work folder afresh when another PyInstaller version made it.

    build/RPMOTE/localpycs holds PyInstaller's own bootstrap modules
    (pyimod0*.pyc), compiled once and reused for as long as they look newer
    than their sources.  Building with one PyInstaller version after a build
    with another can therefore pair the old run's loader with the new run's
    runtime hooks -- which is how an exe came to fail at launch with
    "module 'pyimod02_importers' has no attribute 'PyiFrozenImporter'".
    A stamp of the version that filled the folder catches it.
    """
    import shutil

    import PyInstaller

    stamp = os.path.join(workpath, 'pyinstaller-version.txt')
    try:
        with open(stamp, encoding='utf-8') as fh:
            previous = fh.read().strip()
    except OSError:
        previous = None
    if previous == PyInstaller.__version__:
        return
    if os.path.isdir(workpath):
        print(f"[RPMOTE.spec] work folder made by PyInstaller {previous or 'unknown'}, "
              f"this is {PyInstaller.__version__}: starting it afresh")
        for name in os.listdir(workpath):
            path = os.path.join(workpath, name)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                try:
                    os.remove(path)
                except OSError:
                    pass
    os.makedirs(workpath, exist_ok=True)
    with open(stamp, 'w', encoding='utf-8') as fh:
        fh.write(PyInstaller.__version__)


_require_project_venv()
_clear_stale_loader_cache()
_preflight()


# Only the Meslo faces RPMote registers (app/fonts.py) -- 8 of the folder's 72
# files, which takes about 170 MB off the bundle and the installer.
sys.path.insert(0, os.path.abspath('.'))
from app.fonts import FAMILY_PREFIXES  # noqa: E402

_MESLO = [
    (os.path.join('Meslo', name), 'Meslo')
    for name in sorted(os.listdir('Meslo'))
    if name == 'LICENSE.txt' or (name.endswith('.ttf') and name.startswith(FAMILY_PREFIXES))
]
datas = [('app/resources', 'app/resources')] + _MESLO
binaries = []
hiddenimports = [
    # The browser imports these lazily, inside a function, so PyInstaller's
    # static analysis only ever sees them as "delayed, optional". Name them
    # explicitly so they are always pulled in.
    'PyQt6.QtWebEngineWidgets',
    'PyQt6.QtWebEngineCore',
    'ruamel.yaml',
]
tmp_ret = collect_all('PyQt6')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('paramiko')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('cryptography')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


def _collect_windows_runtime():
    """Put the VC runtime beside Python, before any Qt runtime hook can run.

    Dependency analysis preserves PyQt6's bin directory. A runtime found only
    there cannot satisfy python310.dll when the bootloader loads Python on a
    clean Windows machine. Keep Qt's copies for QtWebEngineProcess.exe, and
    also explicitly collect the same runtime family at _internal's root.
    """
    if sys.platform != 'win32':
        return []

    from pathlib import Path
    import PyQt6

    qt_bin = Path(PyQt6.__file__).resolve().parent / 'Qt6' / 'bin'
    required = ('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll')
    missing = [name for name in required if not (qt_bin / name).is_file()]
    if missing:
        raise SystemExit(
            "RPMote build aborted: Visual C++ runtime missing from PyQt6: "
            + ', '.join(missing)
            + "\nReinstall the pinned PyQt6-Qt6 package before building."
        )

    return [
        (str(path), '.')
        for path in sorted(qt_bin.glob('*.dll'))
        if path.name.lower().startswith(('vcruntime140', 'msvcp140',
                                         'concrt140', 'vccorlib140'))
    ]


binaries += _collect_windows_runtime()


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['app/runtime_hooks/pyqt6_activex_path.py'],
    # paramiko imports `invoke` only for `Match exec` in ssh_config, and guards
    # it with try/except ImportError (paramiko/config.py). Nothing here uses it.
    # Bundling it drags in invoke.vendor.yaml, whose cyaml.py reaches for the
    # top-level `yaml._yaml` C extension -- the import that broke startup once
    # already. The editor uses ruamel.yaml's pure-Python parser, so the
    # unrelated PyYAML/invoke chain can still be excluded.
    excludes=['_yaml', 'yaml', 'invoke'],
    noarchive=False,
    optimize=0,
)


def _verify_webengine_bundled(analysis):
    """Fail the build if the Qt WebEngine payload did not make it in."""
    bundled = []
    for entry in list(analysis.datas) + list(analysis.binaries):
        bundled.append(str(entry[0]).replace(os.sep, "/").lower())
    haystack = "\n".join(bundled)

    missing = [need for need in REQUIRED_WEBENGINE if need.lower() not in haystack]
    if not missing:
        return

    raise SystemExit(
        "\n"
        "=========================================================\n"
        " RPMote build aborted: Qt WebEngine was not bundled\n"
        "=========================================================\n"
        f" Interpreter : {sys.executable}\n"
        " Not bundled :\n"
        + "".join(f"     - {name}\n" for name in missing)
        + "\n"
        " Without these the NAS / iDRAC browser reports\n"
        " \"PyQt6-WebEngine is required but could not be loaded\"\n"
        " on any machine that lacks a system-wide PyQt6-WebEngine.\n"
        "=========================================================\n"
    )


_verify_webengine_bundled(a)


pyz = PYZ(a.pure)

# UPX is left off deliberately. It corrupts large Qt libraries — Qt6WebEngineCore.dll
# above all — producing a DLL that loads on the build machine and fails elsewhere,
# and it reliably trips antivirus heuristics. The Inno Setup script already applies
# lzma2/ultra64, so UPX would buy almost nothing on the shipped installer.
UPX_ENABLED = False
UPX_EXCLUDE = [
    'Qt6WebEngineCore.dll',
    'QtWebEngineProcess.exe',
    'Qt6Core.dll',
    'Qt6Gui.dll',
    'Qt6Widgets.dll',
    'Qt6Network.dll',
    'Qt6Qml.dll',
    'Qt6Quick.dll',
    'vcruntime140.dll',
    'vcruntime140_1.dll',
    'msvcp140.dll',
]

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='RPMote',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=UPX_ENABLED,
    upx_exclude=UPX_EXCLUDE,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app\\resources\\icons\\app_icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=UPX_ENABLED,
    upx_exclude=UPX_EXCLUDE,
    name='RPMote',
)
