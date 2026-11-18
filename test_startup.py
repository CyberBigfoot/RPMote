"""Start-up: what the login screen waits for, and what it no longer does.

The vault door is meant to appear before the rest of the app has loaded;
everything else is warmed up while it waits for the password.  These pin
that: main.py's imports stay lean, the warm-up runs (and can be finished
early), only the fonts RPMote uses are registered, and nothing imported on
the warm-up thread builds Qt objects at import time.
"""

import ast
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("RPMOTE_HW_ACCEL", "0")

from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer
from PyQt6.QtWidgets import QApplication

from app.startup import Warmup

_APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parent


def spin(ms: int):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


class WarmupTests(unittest.TestCase):

    def test_gui_steps_run_in_order_one_turn_apart(self):
        order = []
        warm = Warmup(gui_steps=[("a", lambda: order.append("a")),
                                 ("b", lambda: order.append("b"))],
                      imports=())
        warm.start(first_step_ms=0)
        self.assertEqual(order, [])          # nothing runs inside start()
        # Allow queued work from the full Qt suite to drain; this checks the
        # order of event-loop steps, not a 150 ms machine-speed threshold.
        deadline = time.monotonic() + 3
        while len(order) < 2 and time.monotonic() < deadline:
            spin(25)
        self.assertEqual(order, ["a", "b"])

    def test_background_steps_and_imports_run_off_the_gui_thread(self):
        seen = []
        warm = Warmup(background_steps=[("t", lambda: seen.append(threading.current_thread()))],
                      imports=("json",))
        warm.start()
        warm.finish()
        self.assertEqual(len(seen), 1)
        self.assertIsNot(seen[0], threading.main_thread())
        self.assertTrue(any(name == "import json" for name, _s in warm.timings))

    def test_finish_runs_what_is_left_and_waits(self):
        order = []
        release = threading.Event()
        warm = Warmup(gui_steps=[("gui", lambda: order.append("gui"))],
                      background_steps=[("slow", lambda: (release.wait(2), order.append("bg")))],
                      imports=())
        warm.start(first_step_ms=10_000)     # the GUI step would come much later
        QTimer.singleShot(50, release.set)
        warm.finish()
        self.assertEqual(sorted(order), ["bg", "gui"])
        self.assertTrue(warm.done)
        warm.finish()                        # idempotent
        self.assertEqual(sorted(order), ["bg", "gui"])

    def test_quitting_only_sees_the_background_steps_through(self):
        order = []
        warm = Warmup(gui_steps=[("gpu", lambda: order.append("gpu"))],
                      background_steps=[("sweep", lambda: order.append("sweep"))],
                      imports=("xml.dom.minidom",))
        # Never started: the door was never painted.
        warm.finish(everything=False)
        self.assertEqual(order, ["sweep"])
        self.assertFalse(any(name.startswith("import") for name, _s in warm.timings))

    def test_a_failing_step_does_not_stop_the_rest(self):
        order = []

        def boom():
            raise RuntimeError("nope")

        warm = Warmup(gui_steps=[("boom", boom), ("next", lambda: order.append("next"))],
                      imports=())
        with self.assertLogs("rpmote.startup", level="ERROR"):
            warm.finish()
        self.assertEqual(order, ["next"])

    def test_summary_names_every_step(self):
        warm = Warmup(gui_steps=[("fonts", lambda: None)], imports=())
        warm.finish()
        self.assertIn("fonts", warm.summary())


class FontTests(unittest.TestCase):

    def test_only_the_two_mono_families_are_registered(self):
        from app.fonts import bundled_font_files
        from app.resource_paths import get_resource_path
        files = [os.path.basename(f) for f in bundled_font_files(get_resource_path("Meslo"))]
        self.assertEqual(len(files), 8)
        self.assertTrue(all(f.startswith(("MesloLGSNerdFontMono-", "MesloLGLNerdFontMono-"))
                            for f in files))

    def test_registered_families_are_the_ones_the_app_asks_for(self):
        from PyQt6.QtGui import QFont
        from app import fonts
        from app.ssh_terminal import SSHTerminal
        fonts.register_bundled_fonts()
        self.assertEqual(fonts.register_bundled_fonts(), 0)   # once only
        for family in ("MesloLGS Nerd Font Mono", "MesloLGL Nerd Font Mono"):
            self.assertIn(family, SSHTerminal.TERMINAL_FONTS)
            self.assertTrue(QFont(family).exactMatch(), family)

    def test_the_spec_bundles_exactly_those_files(self):
        spec = (ROOT / "RPMOTE.spec").read_text(encoding="utf-8")
        self.assertIn("from app.fonts import FAMILY_PREFIXES", spec)
        self.assertNotIn("('Meslo', 'Meslo')", spec)


class LeanLoginTests(unittest.TestCase):

    def test_main_does_not_import_the_app_before_the_door(self):
        """Importing main.py must not pull in the main window or paramiko."""
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ, USERPROFILE=home, HOME=home,
                       LOCALAPPDATA=os.path.join(home, "AppData", "Local"),
                       QT_QPA_PLATFORM="offscreen", RPMOTE_HW_ACCEL="0")
            code = ("import sys, main; "
                    "print(sorted(m for m in ('app.main_window', 'paramiko', 'app.dashboard',"
                    " 'app.rdp_activex_setup') if m in sys.modules))")
            out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                                 capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(out.stdout.strip().splitlines()[-1], "[]")

    def test_app_modules_build_no_qt_objects_at_import(self):
        """The warm-up thread imports app.main_window and everything under it;
        a QWidget or QPixmap made at import time there would be made off the
        GUI thread."""
        offenders = []
        for path in sorted((ROOT / "app").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.Import, ast.ImportFrom)):
                    continue
                bodies = node.body if isinstance(node, ast.ClassDef) else [node]
                for stmt in bodies:
                    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for call in ast.walk(stmt):
                        if not isinstance(call, ast.Call):
                            continue
                        func = call.func
                        name = (func.id if isinstance(func, ast.Name)
                                else func.attr if isinstance(func, ast.Attribute) else "")
                        if name[:1] == "Q" and name[1:2].isupper():
                            offenders.append(f"{path.name}:{stmt.lineno} {name}")
        self.assertEqual(offenders, [])

    def test_the_login_screen_needs_no_rich_text_or_emoji(self):
        """Both cost Qt ~200 ms on first use, in front of the door."""
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        start = source.index("    def _build_ui(self):")
        end = source.index("    def resizeEvent", start)
        build_ui = source[start:end]
        self.assertNotIn("<a href", build_ui)
        self.assertNotIn("⚡", build_ui.replace("the ⚡ character", ""))
        self.assertNotIn("sans-serif", build_ui)


class PlatformHeaderTests(unittest.TestCase):

    def test_the_log_header_does_not_start_a_process(self):
        from app import crash_logging
        with mock.patch("subprocess.Popen", side_effect=AssertionError("spawned")):
            text = crash_logging._platform_description()
        self.assertTrue(text)
        if sys.platform == "win32":
            self.assertTrue(text.startswith("Windows-"), text)


class GpuPrewarmTests(unittest.TestCase):

    def test_prewarm_is_a_no_op_on_the_software_path(self):
        from app import hardware_acceleration as hw
        with mock.patch.object(hw, "is_enabled", return_value=False):
            self.assertFalse(hw.prewarm_gpu())
        hw.release_gpu_prewarm()             # nothing held: harmless


if __name__ == "__main__":
    unittest.main()
