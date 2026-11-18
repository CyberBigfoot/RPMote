"""
Hardware (GPU) acceleration for RPMote's heaviest surfaces.

Two things eat CPU once a real MSP estate is loaded: the dashboard topology,
which repaints its animated background and every tenant tile ~30 times a
second through Qt's software rasteriser, and the embedded Chromium views
(NAS / iDRAC browsers), which default to whatever GPU policy Chromium picks.

With acceleration on:
  * the dashboard paints through a QOpenGLWidget, so QPainter runs on the
    OpenGL paint engine (gradients, glows and wide antialiased strokes are
    cheap) and the whole main window is composited by the GPU;
  * Chromium keeps its GPU process (WebGL works) but composites in software
    and hands Qt finished bitmaps -- its GPU frames leak on the way into the
    OpenGL window (see GPU_ON_FLAGS).

With it off, everything takes the software path: the dashboard is a plain
QWidget and Chromium runs with --disable-gpu.  That is the escape hatch for
broken GPU drivers, VMs without a GPU, or sessions over RDP.

The choice has to be known before Qt is imported (environment variables and
the dashboard's base class are fixed at import time), so it lives in the
pre-login runtime settings rather than the vault and only changes on restart.

``RPMOTE_HW_ACCEL=0`` / ``=1`` overrides the saved setting for one launch,
which is handy for support and keeps the test-suite on the software path.
"""

from __future__ import annotations

import os

from app.runtime_settings import (
    is_hardware_acceleration_enabled,
    save_runtime_settings,
)

ENV_OVERRIDE = "RPMOTE_HW_ACCEL"

# Chromium switches appended to QTWEBENGINE_CHROMIUM_FLAGS.
#: With acceleration on the window is composited through OpenGL (see
#: SCENE_GRAPH_BACKEND_FOR_GL), and a GPU-composited Chromium frame has to
#: cross from Direct3D 11 into it.  On Qt WebEngine 6.11 every crossing
#: leaks about a frame's worth of memory and a handle, inside RPMote itself:
#: ~185 MB a second on a page that keeps redrawing, like a NAS dashboard or a
#: noVNC console (23 GB in 150 s, measured 2026-10-02 -- the "slowly eats all
#: the RAM" crash).  Software compositing has no crossing: memory stayed flat,
#: pages drew at the same ~30 fps the GPU path managed in this window, and
#: WebGL still works.  ANGLE on OpenGL also avoids the crossing but drew a
#: blank page at 160% CPU; GPU rasterisation and zero-copy need GPU
#: compositing, so they went with it.
GPU_ON_FLAGS = "--disable-gpu-compositing"
GPU_OFF_FLAGS = "--disable-gpu --disable-gpu-compositing"

#: Always off, whichever way GPU goes.  Qt WebEngine runs Chromium's GPU
#: service in-process, so a fault in a hardware codec is a fault in RPMote.
#: DSM's QuickConnect page opens a WebRTC peer connection to find a direct
#: path to the NAS; setting one up makes Chromium enumerate the Media
#: Foundation hardware encoders, which on this driver logs "Set output type
#: failed" and, on a slow relay, hangs the window and then dies with an
#: access violation.  Nothing this app shows needs a hardware codec -- the
#: WebRTC channel carries data, not video -- so software codecs it is.
MEDIA_FLAGS = "--disable-accelerated-video-encode --disable-accelerated-video-decode"

#: A window can only be composited through one graphics API.  QOpenGLWidget
#: needs OpenGL; Qt WebEngine's view is a QQuickWidget that defaults to D3D11
#: on Windows.  Whichever lands first wins and the other draws nothing (the
#: dashboard goes flat grey, or every NAS / iDRAC tab is black).  Pinning the
#: scene graph to OpenGL keeps them compatible.  It is also why Chromium must
#: not GPU-composite while it is set -- see GPU_ON_FLAGS.
SCENE_GRAPH_BACKEND_VAR = "QSG_RHI_BACKEND"
SCENE_GRAPH_BACKEND_FOR_GL = "opengl"

#: Multisampling for the GL-backed dashboard.  Qt's OpenGL paint engine only
#: antialiases geometry when the surface is multisampled, so without this
#: every node edge and streamer would come out jagged.
CANVAS_SAMPLES = 4

_decision: bool | None = None


def _env_override() -> bool | None:
    raw = os.environ.get(ENV_OVERRIDE, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None


def is_enabled() -> bool:
    """Whether this process runs with hardware acceleration.

    Decided once, on first call, so every consumer (environment setup, the
    dashboard's base class, the menu checkbox) agrees for the whole run even
    if the saved preference is flipped later.
    """
    global _decision
    if _decision is None:
        override = _env_override()
        _decision = override if override is not None else is_hardware_acceleration_enabled()
    return _decision


def is_saved_enabled() -> bool:
    """The persisted preference — what the *next* launch will use."""
    return is_hardware_acceleration_enabled()


def set_saved_enabled(enabled: bool) -> None:
    save_runtime_settings({"hardware_acceleration_enabled": bool(enabled)})


def configure_environment(environ: dict | None = None) -> bool:
    """Push the GPU choice into Chromium's flags.

    Must run before any PyQt6 module is imported: Qt WebEngine reads
    QTWEBENGINE_CHROMIUM_FLAGS when its first process starts.  Returns the
    decision so the caller can log it.
    """
    env = os.environ if environ is None else environ
    enabled = is_enabled()
    flags = env.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    for wanted in (GPU_ON_FLAGS if enabled else GPU_OFF_FLAGS, MEDIA_FLAGS):
        if wanted not in flags:
            flags = f"{flags} {wanted}".strip()
    env["QTWEBENGINE_CHROMIUM_FLAGS"] = flags
    if enabled:
        # setdefault: an explicit choice in the environment still wins.
        env.setdefault(SCENE_GRAPH_BACKEND_VAR, SCENE_GRAPH_BACKEND_FOR_GL)
    return enabled


def canvas_base_class():
    """Base class for RPMote's custom-painted canvases.

    QOpenGLWidget when accelerated and available, else plain QWidget.
    ``QPainter(self)`` works the same on both, only the engine underneath
    changes -- but on the GL widget it must run inside ``paintGL()``, not a
    ``paintEvent`` override, or nothing reaches the framebuffer.
    """
    from PyQt6.QtWidgets import QWidget

    if not is_enabled():
        return QWidget
    try:
        from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    except Exception:
        return QWidget
    return QOpenGLWidget


def is_gl_canvas(widget) -> bool:
    try:
        from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    except Exception:
        return False
    return isinstance(widget, QOpenGLWidget)


def prepare_canvas(widget) -> None:
    """Configure a freshly-constructed canvas.  No-op for the software path.

    Has to happen before the widget is first shown, because the surface
    format is baked into the GL context on initialisation.
    """
    if not is_gl_canvas(widget):
        return
    from PyQt6.QtGui import QSurfaceFormat

    fmt = QSurfaceFormat(QSurfaceFormat.defaultFormat())
    fmt.setSamples(CANVAS_SAMPLES)
    widget.setFormat(fmt)


#: The surface and context prewarm_gpu() made, held until the main window has
#: painted with its own (release_gpu_prewarm).
_prewarm: tuple | None = None


def prewarm_gpu() -> bool:
    """Load the OpenGL driver and make one context current, ahead of time.

    The first GL widget a process shows pays for the driver's DLLs and its
    start-up -- about a quarter of a second here, more on a cold start -- and
    that used to land between unlocking the vault and seeing the main
    window.  Called while the login screen waits for a password, it is paid
    while nobody is waiting.  GUI thread only; does nothing on the software
    path.  Returns whether a context came up.
    """
    global _prewarm
    if not is_enabled() or _prewarm is not None:
        return False
    try:
        from PyQt6.QtGui import QOffscreenSurface, QOpenGLContext, QSurfaceFormat
    except Exception:
        return False
    surface = QOffscreenSurface()
    surface.setFormat(QSurfaceFormat.defaultFormat())
    surface.create()
    context = QOpenGLContext()
    share = QOpenGLContext.globalShareContext()
    if share is not None:
        context.setShareContext(share)
    if not context.create():
        return False
    ok = bool(context.makeCurrent(surface))
    context.doneCurrent()
    _prewarm = (surface, context)
    return ok


def release_gpu_prewarm() -> None:
    """Let go of the warm-up context once the real canvas has its own."""
    global _prewarm
    held, _prewarm = _prewarm, None
    if held is None:
        return
    surface, context = held
    try:
        context.deleteLater()
        surface.destroy()
    except RuntimeError:
        pass


def describe() -> str:
    """Short human-readable label for About / diagnostics."""
    if not is_enabled():
        return "Software rendering"
    if _env_override() is not None:
        return f"GPU (OpenGL) — forced by {ENV_OVERRIDE}"
    return "GPU (OpenGL)"
