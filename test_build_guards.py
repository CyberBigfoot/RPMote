"""Build guards: the exe is built by the project's own Python, from a clean cache.

An exe once died at launch with "module 'pyimod02_importers' has no attribute
'PyiFrozenImporter'": it had been built by the global Python's PyInstaller
(6.11), which reused the loader a .venv build (PyInstaller 6.19) had cached in
build/RPMOTE/localpycs.  RPMOTE.spec now refuses a foreign interpreter and
starts its work folder afresh when another PyInstaller version made it, and
build.py hands itself to the .venv Python.  These run the real guard code.
"""

import ast
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
IN_PROJECT_VENV = Path(sys.prefix).resolve() == (ROOT / ".venv").resolve()


def spec_function(name: str, **namespace):
    """One function from RPMOTE.spec, with the globals PyInstaller would give it."""
    tree = ast.parse((ROOT / "RPMOTE.spec").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    scope = {"os": os, "sys": sys, **namespace}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "RPMOTE.spec", "exec"), scope)
    return scope[name]


def load_build_script():
    spec = importlib.util.spec_from_file_location("rpmote_build_script", ROOT / "build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StaleCacheTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workpath = Path(self._tmp.name) / "RPMOTE"
        (self.workpath / "localpycs").mkdir(parents=True)
        (self.workpath / "localpycs" / "pyimod02_importers.pyc").write_bytes(b"stale")
        (self.workpath / "PYZ-00.toc").write_text("old", encoding="utf-8")
        self.clear = spec_function("_clear_stale_loader_cache", workpath=str(self.workpath))

    def tearDown(self):
        self._tmp.cleanup()

    def stamp(self):
        return (self.workpath / "pyinstaller-version.txt").read_text(encoding="utf-8")

    def test_a_cache_from_another_version_is_thrown_away(self):
        import PyInstaller
        (self.workpath / "pyinstaller-version.txt").write_text("6.11.1", encoding="utf-8")
        self.clear()
        self.assertFalse((self.workpath / "localpycs").exists())
        self.assertFalse((self.workpath / "PYZ-00.toc").exists())
        self.assertEqual(self.stamp(), PyInstaller.__version__)

    def test_an_unstamped_folder_is_thrown_away(self):
        self.clear()
        self.assertFalse((self.workpath / "localpycs").exists())

    def test_the_same_version_keeps_its_cache(self):
        import PyInstaller
        (self.workpath / "pyinstaller-version.txt").write_text(PyInstaller.__version__,
                                                               encoding="utf-8")
        self.clear()
        self.assertTrue((self.workpath / "localpycs" / "pyimod02_importers.pyc").exists())

    def test_the_spec_runs_the_guards_before_anything_else(self):
        source = (ROOT / "RPMOTE.spec").read_text(encoding="utf-8")
        calls = [source.index(f"\n{name}()\n") for name in
                 ("_require_project_venv", "_clear_stale_loader_cache", "_preflight")]
        self.assertEqual(calls, sorted(calls))
        self.assertLess(calls[-1], source.index("\na = Analysis("))


class ProjectVenvTests(unittest.TestCase):

    def test_the_spec_refuses_a_python_outside_the_project_venv(self):
        with tempfile.TemporaryDirectory() as other_project:
            os.mkdir(os.path.join(other_project, ".venv"))
            guard = spec_function("_require_project_venv", SPECPATH=other_project)
            with mock.patch.dict(os.environ, {"RPMOTE_ALLOW_ANY_PYTHON": ""}):
                with self.assertRaises(SystemExit) as caught:
                    guard()
            self.assertIn("wrong Python", str(caught.exception.code))
            with mock.patch.dict(os.environ, {"RPMOTE_ALLOW_ANY_PYTHON": "1"}):
                guard()                      # overridden: no exit

    def test_a_project_without_a_venv_builds_with_any_python(self):
        with tempfile.TemporaryDirectory() as bare:
            spec_function("_require_project_venv", SPECPATH=bare)()

    @unittest.skipUnless(IN_PROJECT_VENV, "run under the project's .venv")
    def test_the_spec_accepts_the_project_venv(self):
        spec_function("_require_project_venv", SPECPATH=str(ROOT))()

    def test_build_py_hands_itself_to_the_venv(self):
        build = load_build_script()
        ran = []

        def fake_run(cmd, cwd=None):
            ran.append(cmd)
            return mock.Mock(returncode=3)

        with mock.patch.object(build.sys, "prefix", str(ROOT / "not-the-venv")), \
                mock.patch.object(build.subprocess, "run", fake_run), \
                mock.patch.dict(os.environ, {"RPMOTE_ALLOW_ANY_PYTHON": ""}), \
                mock.patch.object(build, "print", create=True):
            if not (ROOT / ".venv" / "Scripts" / "python.exe").is_file():
                self.skipTest("no .venv in this checkout")
            with self.assertRaises(SystemExit) as caught:
                build.ensure_project_venv()
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(Path(ran[0][0]), ROOT / ".venv" / "Scripts" / "python.exe")
        self.assertEqual(Path(ran[0][1]), ROOT / "build.py")

    @unittest.skipUnless(IN_PROJECT_VENV, "run under the project's .venv")
    def test_build_py_stays_put_in_the_venv(self):
        build = load_build_script()
        with mock.patch.object(build.subprocess, "run") as run:
            build.ensure_project_venv()
        run.assert_not_called()

    def test_build_py_finds_inno_setup_where_its_installer_puts_it(self):
        build = load_build_script()
        per_user = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe"
        self.assertIn(per_user, build.INNO_CANDIDATES)
        with mock.patch.dict(os.environ, {"RPMOTE_ISCC": str(ROOT / "nope" / "ISCC.exe")}):
            self.assertIsNone(build.find_iscc())


class WindowsRuntimeTests(unittest.TestCase):
    def test_runtime_family_is_collected_beside_python(self):
        import PyQt6

        collect = spec_function("_collect_windows_runtime")
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "PyQt6"
            qt_bin = package / "Qt6" / "bin"
            qt_bin.mkdir(parents=True)
            runtime = ("vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll",
                       "msvcp140_2.dll", "msvcp140_atomic_wait.dll",
                       "vcruntime140_threads.dll", "concrt140.dll")
            for name in (*runtime, "Qt6Core.dll"):
                (qt_bin / name).write_bytes(b"test")
            with mock.patch.object(sys, "platform", "win32"), \
                    mock.patch.object(PyQt6, "__file__", str(package / "__init__.py")):
                entries = collect()
        self.assertEqual({Path(src).name for src, dest in entries}, set(runtime))
        self.assertTrue(all(dest == "." for src, dest in entries))

    def test_collection_refuses_a_missing_runtime(self):
        import PyQt6

        collect = spec_function("_collect_windows_runtime")
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(sys, "platform", "win32"), \
                    mock.patch.object(PyQt6, "__file__", str(Path(temporary) / "__init__.py")):
                with self.assertRaises(SystemExit) as caught:
                    collect()
        self.assertIn("vcruntime140.dll", str(caught.exception))

    def test_other_platforms_do_not_collect_windows_dlls(self):
        with mock.patch.object(sys, "platform", "linux"):
            self.assertEqual(spec_function("_collect_windows_runtime")(), [])

    def test_verifier_rejects_nested_only_runtime_and_missing_python(self):
        build = load_build_script()
        python_dll = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
        required = (python_dll, *build.REQUIRED_WINDOWS_RUNTIME)
        with tempfile.TemporaryDirectory() as temporary:
            internal = Path(temporary)
            qt_bin = internal / "PyQt6" / "Qt6" / "bin"
            qt_bin.mkdir(parents=True)
            for name in build.REQUIRED_WINDOWS_RUNTIME:
                (qt_bin / name).write_bytes(b"nested copy")
            with mock.patch.object(build, "INTERNAL", internal), \
                    mock.patch.object(sys, "platform", "win32"), \
                    mock.patch.object(build, "print", create=True):
                with self.assertRaises(SystemExit):
                    build.verify_windows_runtime()
                for name in required:
                    (internal / name).write_bytes(b"root copy")
                build.verify_windows_runtime()
                for name in required:
                    with self.subTest(missing=name):
                        (internal / name).unlink()
                        with self.assertRaises(SystemExit):
                            build.verify_windows_runtime()
                        (internal / name).write_bytes(b"root copy")


class WindowsDllSearchTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt" and IN_PROJECT_VENV,
                         "Windows build under the project's .venv")
    def test_a_tools_icu_library_cannot_shadow_windows_icu(self):
        from PyInstaller.depend.bindepend import resolve_library_path

        system_dir = Path(os.environ["SystemRoot"]) / "System32"
        windows_icu = system_dir / "icuuc.dll"
        if not windows_icu.is_file():
            self.skipTest("Windows ICU is not available on this OS")
        build = load_build_script()
        with tempfile.TemporaryDirectory() as temporary:
            foreign_icu = Path(temporary) / "icuuc.dll"
            # A real PE library with the wrong exports models a same-named
            # DLL from an unrelated tool, without depending on that tool.
            shutil.copyfile(system_dir / "kernel32.dll", foreign_icu)
            with mock.patch.dict(os.environ, {"PATH": temporary}):
                self.assertEqual(Path(resolve_library_path("icuuc.dll")), foreign_icu)
                with mock.patch.object(build.subprocess, "run") as run:
                    run.return_value.returncode = 0
                    build.run_pyinstaller(clean=False)
                child_env = run.call_args.kwargs["env"]
                self.assertEqual(os.environ["PATH"], temporary)
                with mock.patch.dict(os.environ, child_env, clear=True):
                    self.assertEqual(Path(resolve_library_path("icuuc.dll")), windows_icu)


if __name__ == "__main__":
    unittest.main()
