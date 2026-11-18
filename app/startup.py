"""Getting from launch to the vault door, and from the door to the app, fast.

The login screen needs very little: Qt, the vault and the door itself.  The
rest -- the main window and everything it imports (paramiko alone is a
quarter of a second), the terminal fonts, the GPU driver, the Credential
Manager sweep -- used to be paid before the door could appear or between the
password and the window.  Now it happens while the door waits for the
password:

* modules are imported on a background thread, since the GUI thread is idle
  in its event loop and Python releases the GIL there;
* GUI-only work (fonts, the GPU) runs in short slices on the GUI thread, one
  event-loop turn apart, so typing stays responsive.

Whatever has not finished when the password is accepted -- someone fast with
a password manager -- is finished then, by finish().
"""

from __future__ import annotations

import importlib
import logging
import threading
import time
from typing import Callable

_LOG = logging.getLogger("rpmote.startup")

#: Imported on the warm-up thread.  Nothing these import may create a
#: QWidget, QPixmap or QFont at import time (see test_startup.py).
BACKGROUND_IMPORTS = ("app.main_window",)

#: Gap between GUI-thread steps, so a keypress always gets a turn in between.
STEP_GAP_MS = 15


class Warmup:
    """Runs start-up work in the gaps while the login screen is idle."""

    def __init__(self, gui_steps: list[tuple[str, Callable]] | None = None,
                 background_steps: list[tuple[str, Callable]] | None = None,
                 imports: tuple[str, ...] = BACKGROUND_IMPORTS):
        self._gui_steps = list(gui_steps or [])
        self._background_steps = list(background_steps or [])
        self._imports = tuple(imports)
        self._thread: threading.Thread | None = None
        self._finished = False
        #: (step name, seconds) for every step that ran, for the startup log.
        self.timings: list[tuple[str, float]] = []
        self._lock = threading.Lock()

    # ── Running ───────────────────────────────────────────────────

    def start(self, first_step_ms: int = 0):
        """Begin: the thread at once, GUI steps from *first_step_ms*."""
        if self._background_steps or self._imports:
            self._thread = threading.Thread(
                target=self._run_background, name="rpmote-warmup", daemon=True)
            self._thread.start()
        if self._gui_steps:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(first_step_ms, self._next_gui_step)

    def finish(self, everything: bool = True):
        """Run what is left now and wait for the thread.  Idempotent.

        Called before anything on the GUI thread imports app modules or
        builds the main window, so the two threads never import side by side.
        With *everything* False (quitting from the login screen) only the
        background steps are seen through -- the GUI steps and imports are
        skipped if they have not started.
        """
        if self._finished:
            return
        self._finished = True
        if self._thread is None:
            # Never started (the door was never painted): do it here, in order.
            for name, step in self._background_steps:
                self._run(name, step)
            if everything:
                for module in self._imports:
                    self._run(f"import {module}",
                              lambda m=module: importlib.import_module(m))
        if not everything:
            self._gui_steps.clear()
        while self._gui_steps:
            self._run(*self._gui_steps.pop(0))
        if self._thread is not None:
            started = time.perf_counter()
            self._thread.join()
            waited = time.perf_counter() - started
            if waited > 0.005:
                self._note("wait for background imports", waited)

    @property
    def done(self) -> bool:
        return self._finished

    # ── Internals ─────────────────────────────────────────────────

    def _next_gui_step(self):
        if self._finished or not self._gui_steps:
            return
        self._run(*self._gui_steps.pop(0))
        if self._gui_steps:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(STEP_GAP_MS, self._next_gui_step)

    def _run_background(self):
        for name, step in self._background_steps:
            self._run(name, step)
        for module in self._imports:
            self._run(f"import {module}", lambda m=module: importlib.import_module(m))

    def _run(self, name: str, step: Callable):
        started = time.perf_counter()
        try:
            step()
        except Exception:
            # Never fatal here: whatever needed it will fail again in the
            # open, with its own error handling.
            _LOG.exception("startup step failed: %s", name)
        self._note(name, time.perf_counter() - started)

    def _note(self, name: str, seconds: float):
        with self._lock:
            self.timings.append((name, seconds))

    def summary(self) -> str:
        with self._lock:
            return ", ".join(f"{name} {seconds * 1000:.0f} ms"
                             for name, seconds in self.timings)
