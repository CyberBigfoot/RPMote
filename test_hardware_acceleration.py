"""Hardware acceleration: the choice is made once, before Qt, and everything
downstream (Chromium flags, dashboard base class, menu state) agrees."""

import os

import pytest

from app import hardware_acceleration as hw
from app import runtime_settings


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Fresh decision per test, settings file in a temp dir, env override off."""
    monkeypatch.setattr(runtime_settings, "RPMOTE_DIR", tmp_path)
    monkeypatch.setattr(runtime_settings, "RUNTIME_SETTINGS_FILE",
                        tmp_path / "runtime_settings.json")
    monkeypatch.delenv(hw.ENV_OVERRIDE, raising=False)
    monkeypatch.setattr(hw, "_decision", None)
    yield
    monkeypatch.setattr(hw, "_decision", None)


def test_defaults_to_enabled():
    assert hw.is_enabled() is True
    assert hw.is_saved_enabled() is True


def test_saved_preference_drives_next_launch():
    hw.set_saved_enabled(False)
    assert hw.is_saved_enabled() is False
    assert hw.is_enabled() is False           # nothing decided yet this "run"


def test_decision_is_sticky_for_the_process():
    assert hw.is_enabled() is True
    hw.set_saved_enabled(False)
    # The running process keeps what it started with; only the saved
    # preference changed.
    assert hw.is_enabled() is True
    assert hw.is_saved_enabled() is False


@pytest.mark.parametrize("raw, expected", [
    ("0", False), ("off", False), ("no", False),
    ("1", True), ("on", True), ("yes", True),
])
def test_env_override_beats_saved_setting(monkeypatch, raw, expected):
    hw.set_saved_enabled(not expected)
    monkeypatch.setenv(hw.ENV_OVERRIDE, raw)
    assert hw.is_enabled() is expected


def test_garbage_env_value_is_ignored(monkeypatch):
    hw.set_saved_enabled(False)
    monkeypatch.setenv(hw.ENV_OVERRIDE, "maybe")
    assert hw.is_enabled() is False


def test_environment_gets_gpu_flags_when_enabled():
    env = {"QTWEBENGINE_CHROMIUM_FLAGS": "--ignore-certificate-errors"}
    assert hw.configure_environment(env) is True
    flags = env["QTWEBENGINE_CHROMIUM_FLAGS"]
    assert flags.startswith("--ignore-certificate-errors")   # existing kept
    assert hw.GPU_ON_FLAGS in flags
    # The GPU process stays (WebGL needs it); only compositing is software.
    assert "--disable-gpu" not in flags.split()


def test_chromium_never_gpu_composites_into_the_opengl_window():
    """A GPU-composited Chromium frame leaks on its way into the OpenGL scene
    graph (Qt WebEngine 6.11): ~185 MB a second on a NAS dashboard or noVNC
    console, until Windows ran out of memory.  Wherever the scene graph is
    pinned to OpenGL, Chromium has to composite in software."""
    env = {}
    hw.configure_environment(env)
    assert env[hw.SCENE_GRAPH_BACKEND_VAR] == "opengl"
    flags = env["QTWEBENGINE_CHROMIUM_FLAGS"].split()
    assert "--disable-gpu-compositing" in flags
    for leaking in ("--enable-gpu-rasterization", "--enable-zero-copy"):
        assert leaking not in flags


def test_environment_disables_gpu_when_off():
    hw.set_saved_enabled(False)
    env = {}
    assert hw.configure_environment(env) is False
    assert env["QTWEBENGINE_CHROMIUM_FLAGS"] == f"{hw.GPU_OFF_FLAGS} {hw.MEDIA_FLAGS}"


def test_hardware_codecs_are_off_either_way():
    """The in-process GPU service means a Media Foundation codec fault
    takes RPMote down; DSM's WebRTC probe trips it. Off on both paths."""
    on = {}
    hw.configure_environment(on)
    assert hw.MEDIA_FLAGS in on["QTWEBENGINE_CHROMIUM_FLAGS"]
    hw.set_saved_enabled(False)
    off = {}
    hw.configure_environment(off)
    assert hw.MEDIA_FLAGS in off["QTWEBENGINE_CHROMIUM_FLAGS"]


def test_gl_pins_the_scene_graph_to_opengl():
    """QOpenGLWidget and WebEngine's D3D11 QQuickWidget cannot share a
    window; with GL on, Qt's scene graph must composite through OpenGL."""
    env = {}
    hw.configure_environment(env)
    assert env[hw.SCENE_GRAPH_BACKEND_VAR] == "opengl"


def test_explicit_scene_graph_choice_is_respected():
    env = {hw.SCENE_GRAPH_BACKEND_VAR: "d3d12"}
    hw.configure_environment(env)
    assert env[hw.SCENE_GRAPH_BACKEND_VAR] == "d3d12"


def test_software_path_leaves_the_scene_graph_alone():
    hw.set_saved_enabled(False)
    env = {}
    hw.configure_environment(env)
    assert hw.SCENE_GRAPH_BACKEND_VAR not in env


def test_environment_flags_are_not_duplicated():
    env = {}
    hw.configure_environment(env)
    hw.configure_environment(env)
    assert env["QTWEBENGINE_CHROMIUM_FLAGS"].count("--disable-gpu-compositing") == 1
    assert env["QTWEBENGINE_CHROMIUM_FLAGS"].count("--disable-accelerated-video-encode") == 1


def test_canvas_base_is_plain_widget_when_off():
    from PyQt6.QtWidgets import QWidget
    hw.set_saved_enabled(False)
    assert hw.canvas_base_class() is QWidget


def test_canvas_base_is_opengl_when_on():
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    assert hw.canvas_base_class() is QOpenGLWidget


def test_describe_names_the_path(monkeypatch):
    assert hw.describe() == "GPU (OpenGL)"
    monkeypatch.setattr(hw, "_decision", None)
    hw.set_saved_enabled(False)
    assert hw.describe() == "Software rendering"
    monkeypatch.setattr(hw, "_decision", None)
    monkeypatch.setenv(hw.ENV_OVERRIDE, "1")
    assert hw.ENV_OVERRIDE in hw.describe()


def test_dashboard_base_class_follows_the_decision(monkeypatch):
    """The dashboard's base class is fixed when its module is imported.
    conftest pins RPMOTE_HW_ACCEL=0 for the suite (the offscreen platform has
    no GL); this reproduces that and leaves the module in the same state."""
    import importlib
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QWidget
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    import app.dashboard as dashboard
    _app = QApplication.instance() or QApplication([])

    monkeypatch.setenv(hw.ENV_OVERRIDE, "0")
    monkeypatch.setattr(hw, "_decision", None)
    importlib.reload(dashboard)
    assert issubclass(dashboard.DashboardWidget, QWidget)
    assert not issubclass(dashboard.DashboardWidget, QOpenGLWidget)

    widget = dashboard.DashboardWidget([], {})
    assert widget._gl_backed is False
    widget.deleteLater()
