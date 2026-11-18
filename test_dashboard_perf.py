"""Dashboard frame-budget tests: 60 Hz on the GPU, culling, and caches.

A big estate is repainted many times a second, so the dashboard skips what
is off-screen, remembers what it has measured, and blits node bodies it has
already drawn.  These tests pin the behaviour those shortcuts must preserve:
hit rects for everything, pictures that follow their inputs, motion at one
speed whatever the tick rate.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, QRectF
from PyQt6.QtGui import QPixmap, QPainter
from PyQt6.QtWidgets import QApplication

from app.dashboard import DashboardWidget, VIEW_MARGIN
from app.reachability import STATE_CHECKING, STATE_DOWN, STATE_UP

_APP = QApplication.instance() or QApplication([])


def estate(tenants: int, per_tenant: int) -> list:
    return [
        {"id": f"t{t}c{c}", "name": f"SRV{t}-{c}", "host": f"10.{t}.0.{c + 10}",
         "port": 22, "protocol": ["ssh", "rdp", "sftp"][c % 3],
         "tenant": f"Tenant{t}"}
        for t in range(tenants) for c in range(per_tenant)
    ]


def dashboard(conns) -> DashboardWidget:
    w = DashboardWidget(conns, {})
    w.resize(1200, 800)
    w.set_graph_settings({"animate": False})
    w.expand_all_tenants()
    return w


def render(w: DashboardWidget) -> None:
    """One software paint into a pixmap, the way the other suites do it."""
    pm = QPixmap(w.width(), w.height())
    w.render(pm)


class FakeClock:
    def __init__(self):
        self.ns = 0

    def nsecsElapsed(self):
        return self.ns


class TickRateTests(unittest.TestCase):

    def test_software_path_ticks_at_30hz(self):
        w = dashboard(estate(2, 2))
        self.assertFalse(w._gl_backed)
        self.assertEqual(w.frame_interval_ms, 33)
        self.assertEqual(w._anim_timer.interval(), 33)

    def test_gpu_path_ticks_at_60hz(self):
        """The interval is chosen from the backend, not hard-coded."""
        from unittest import mock
        from app import dashboard as mod
        with mock.patch.object(mod, "is_gl_canvas", lambda widget: True):
            w = DashboardWidget(estate(2, 2), {})
        self.assertTrue(w._gl_backed)
        self.assertEqual(w.frame_interval_ms, 16)
        self.assertEqual(w._anim_timer.interval(), 16)
        w._anim_timer.stop()

    def test_motion_follows_wall_clock_not_tick_count(self):
        w = dashboard(estate(2, 2))
        w.set_graph_settings({"animate": True})
        clock = FakeClock()
        w._clock = clock
        w._last_tick_ns = 0
        start = w._time
        clock.ns = 16_000_000            # one 60 Hz frame
        w._tick()
        clock.ns = 32_000_000
        w._tick()
        self.assertAlmostEqual(w._time - start, 0.032, places=6)

    def test_a_stall_does_not_lurch_the_animation(self):
        w = dashboard(estate(2, 2))
        w.set_graph_settings({"animate": True})
        clock = FakeClock()
        w._clock = clock
        w._last_tick_ns = 0
        start = w._time
        clock.ns = 5_000_000_000         # five seconds behind a modal dialog
        w._tick()
        self.assertAlmostEqual(w._time - start, 0.1, places=6)

    def test_animation_off_skips_the_tick_entirely(self):
        w = dashboard(estate(2, 2))
        clock = FakeClock()
        w._clock = clock
        w._last_tick_ns = 0
        start = w._time
        clock.ns = 16_000_000
        w._tick()
        self.assertEqual(w._time, start)


class CullingTests(unittest.TestCase):

    def test_every_node_keeps_a_hit_rect_when_most_are_off_screen(self):
        conns = estate(20, 6)
        w = dashboard(conns)
        render(w)
        self.assertEqual(len(w._node_rects), 20)
        self.assertEqual(len(w._conn_rects), len(conns))
        # Zoom right in: nearly everything now sits outside the viewport.
        w._zoom = w._max_zoom
        render(w)
        view = QRectF(0, 0, w.width(), w.height())
        on_screen = sum(1 for r, _c, _t in w._conn_rects if view.intersects(r))
        self.assertLess(on_screen, len(conns))
        # ...yet all of them are still hit-testable / findable.
        self.assertEqual(len(w._node_rects), 20)
        self.assertEqual(len(w._conn_rects), len(conns))

    def test_off_screen_nodes_are_not_painted(self):
        w = dashboard(estate(20, 6))
        render(w)
        w._zoom = w._max_zoom
        render(w)
        # Bring one machine to the middle of the view so *something* is on
        # screen at this zoom; the rest of the estate stays outside.
        target = w._conn_rects[0][0].center()
        w._pan += QPointF(w.width() / 2, w.height() / 2) - target
        painted = []
        real = w._render_pill

        def spy(*a, **k):
            painted.append(a[0])
            return real(*a, **k)

        w._render_pill = spy
        w._pill_pixmap_cache.clear()
        render(w)
        view = QRectF(-VIEW_MARGIN, -VIEW_MARGIN,
                      w.width() + 2 * VIEW_MARGIN, w.height() + 2 * VIEW_MARGIN)
        # A pill is worth painting when its rect *or its halo* touches the
        # viewport (the halo is 0.75 of the pill's width all round).
        visible = {c["name"] for r, c, _t in w._conn_rects
                   if view.intersects(r.adjusted(-r.width() * 0.75, -r.width() * 0.75,
                                                 r.width() * 0.75, r.width() * 0.75))}
        self.assertTrue(painted, "something must be on screen")
        self.assertTrue(set(painted) <= visible, set(painted) - visible)
        self.assertLess(len(painted), len(w._conn_rects))

    def test_visibility_helpers(self):
        w = dashboard(estate(1, 1))
        w._view_rect = QRectF(0, 0, 100, 100)
        self.assertTrue(w._rect_visible(QRectF(90, 90, 20, 20)))
        self.assertFalse(w._rect_visible(QRectF(200, 200, 20, 20)))
        # A glow can pull an otherwise-hidden node back into view.
        self.assertTrue(w._rect_visible(QRectF(105, 40, 10, 10), halo=10))
        self.assertTrue(w._segment_visible(-50, -50, 150, 150))
        self.assertFalse(w._segment_visible(150, 150, 300, 300))
        # A perfectly vertical or horizontal branch has a zero-width chord
        # box, which QRectF.intersects treats as empty; it must still count
        # as visible when it crosses the viewport.
        self.assertTrue(w._segment_visible(50, 20, 50, 80))
        self.assertTrue(w._segment_visible(20, 50, 80, 50))
        self.assertTrue(w._segment_visible(50, -200, 50, 300))
        # ...and the curve's bow can bring a just-off-screen line back in.
        self.assertTrue(w._segment_visible(-10, 110, 110, 110))
        self.assertFalse(w._segment_visible(-10, 200, 110, 200))


class CacheTests(unittest.TestCase):

    def test_pill_pictures_are_reused_across_frames(self):
        w = dashboard(estate(4, 4))
        render(w)
        n = len(w._pill_pixmap_cache)
        self.assertEqual(n, 16)
        render(w)
        render(w)
        self.assertEqual(len(w._pill_pixmap_cache), n)

    def test_a_status_change_gets_its_own_picture(self):
        w = dashboard(estate(1, 2))
        w.set_ping_enabled(True)
        render(w)
        before = dict(w._pill_pixmap_cache)
        w.set_conn_status("t0c0", {"state": STATE_DOWN})
        render(w)
        self.assertTrue(set(before) < set(w._pill_pixmap_cache),
                        "a down machine is drawn differently, so it is a new key")

    def test_a_checking_dot_stays_live(self):
        """The checking state breathes, so it is never baked into a pixmap."""
        w = dashboard(estate(1, 1))
        w.set_graph_settings({"animate": True})
        w.set_ping_enabled(True)
        w.set_conn_status("t0c0", {"state": STATE_CHECKING})
        render(w)
        # The picture still carries the checking tint on its border, but is
        # marked as drawn without its dot.
        keys = list(w._pill_pixmap_cache)
        self.assertEqual(len(keys), 1)
        state, live_dot = keys[0][7], keys[0][8]
        self.assertEqual(state, STATE_CHECKING)
        self.assertTrue(live_dot)

    def test_theme_and_font_changes_flush_everything(self):
        w = dashboard(estate(3, 3))
        render(w)
        self.assertTrue(w._pill_pixmap_cache and w._tile_pixmap_cache)
        w.set_theme({"dashboard_theme": "matrix"})
        self.assertFalse(w._pill_pixmap_cache)
        self.assertFalse(w._tile_pixmap_cache)
        self.assertFalse(w._tile_layout_cache)
        self.assertFalse(w._pill_text_cache)

    def test_tenant_state_is_rolled_up_once_per_frame(self):
        w = dashboard(estate(3, 3))
        w.set_ping_enabled(True)
        w.set_conn_statuses({"t1c0": {"state": STATE_DOWN},
                             "t1c1": {"state": STATE_UP}})
        self.assertEqual(w.tenant_state("Tenant1"), STATE_DOWN)
        self.assertEqual(w.tenant_state("Tenant0"), w.tenant_state("Tenant2"))
        # The memo answers without another sweep...
        w._connections = []
        self.assertEqual(w.tenant_state("Tenant1"), STATE_DOWN)
        # ...until something invalidates it.
        w.set_conn_status("t1c0", {"state": STATE_UP})
        self.assertNotEqual(w.tenant_state("Tenant1"), STATE_DOWN)

    def test_pixmap_cache_is_bounded(self):
        from app import dashboard as mod
        w = dashboard(estate(2, 2))
        old = mod.PILL_PIXMAP_CACHE_MAX
        mod.PILL_PIXMAP_CACHE_MAX = 2
        try:
            for z in (0.5, 0.6, 0.7, 0.8, 0.9, 1.0):
                w._zoom = z
                render(w)
            self.assertLessEqual(len(w._pill_pixmap_cache), 4 + 2)
        finally:
            mod.PILL_PIXMAP_CACHE_MAX = old


if __name__ == "__main__":
    unittest.main()
