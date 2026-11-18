"""Dashboard backgrounds and hover motion.

The animated themes' scenes sit behind the graph at a chosen strength
("calm" by default), and a hovered tenant or connection eases up, lights its
lines, fans out its names when zoomed out and shows a card.  These pin the
behaviour those effects promise: what fades and what stays lit, that a hover
never moves a hit rect, and that nothing animates when motion is off.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("RPMOTE_HW_ACCEL", "0")

from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication

from app import themes
from app.dashboard import (
    BACKGROUND_LEVELS, DEFAULT_GRAPH_SETTINGS, FOCUS_DIM_TENANT,
    HOVER_CARD_DELAY_S, HOVER_IN_S, HOVER_OUT_S, RIPPLE_S, DashboardWidget,
    make_ep,
)
from app.relationships import LENS_SUBNET

_APP = QApplication.instance() or QApplication([])


def estate(tenants: int, per_tenant: int) -> list:
    return [
        {"id": f"t{t}c{c}", "name": f"SRV{t}-{c}", "host": f"10.{t}.0.{c + 10}",
         "port": 22, "protocol": ["ssh", "rdp", "sftp"][c % 3],
         "tenant": f"Tenant{t}"}
        for t in range(tenants) for c in range(per_tenant)
    ]


def dashboard(conns=None, animate=True) -> DashboardWidget:
    w = DashboardWidget(conns if conns is not None else estate(3, 4), {})
    w.resize(1200, 800)
    w.set_graph_settings({"animate": animate})
    w.expand_all_tenants()
    w._anim_timer.stop()            # the tests drive time themselves
    return w


def render(w: DashboardWidget) -> QPixmap:
    pm = QPixmap(w.width(), w.height())
    w.render(pm)
    return pm


def advance(w: DashboardWidget, seconds: float, step: float = 1 / 60):
    t = 0.0
    while t < seconds - 1e-9:
        w._time += step
        w._advance_hover(step)
        t += step


def mean_brightness(pm: QPixmap) -> float:
    img = pm.toImage().scaled(80, 54)
    total = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            total += c.red() + c.green() + c.blue()
    return total / (img.width() * img.height() * 3)


class BackgroundLevelTests(unittest.TestCase):

    def test_calm_is_the_default(self):
        self.assertEqual(DEFAULT_GRAPH_SETTINGS["background"], "calm")
        self.assertEqual(dashboard().background_level(), "calm")

    def test_an_unknown_level_falls_back_to_calm(self):
        w = dashboard()
        w._gs["background"] = "blinding"
        self.assertEqual(w.background_level(), "calm")

    def test_every_level_paints_every_theme(self):
        w = dashboard()
        for theme in themes.ALL_THEMES:
            w.set_theme(theme)
            for level in BACKGROUND_LEVELS:
                with self.subTest(theme=theme["name"], level=level):
                    w.set_graph_settings({"background": level})
                    render(w)

    def test_calm_dims_the_scene_vivid_leaves_it_alone(self):
        w = dashboard(estate(1, 1))
        w.set_theme(themes.SYNTHWAVE_THEME)
        w.set_graph_settings({"background": "vivid"})
        vivid = mean_brightness(render(w))
        w.set_graph_settings({"background": "calm"})
        calm = mean_brightness(render(w))
        self.assertLess(calm, vivid * 0.75)

    def test_scene_speed_follows_the_level(self):
        w = dashboard(estate(1, 1))
        w.set_theme(themes.EMBER_THEME)
        w._shown_once = False            # never shown: _tick still animates
        speeds = {}
        for level in ("vivid", "calm", "plain"):
            w.set_graph_settings({"background": level})
            before = w._background.t
            w._clock = _Clock()
            w._last_tick_ns = 0
            w._clock.ns = 50_000_000     # 50 ms
            w._tick()
            speeds[level] = (w._background.t - before) / 0.05
        self.assertAlmostEqual(speeds["vivid"], 1.0, places=3)
        self.assertLess(speeds["calm"], 0.6)
        self.assertEqual(speeds["plain"], 0.0)

    def test_plain_draws_no_scene(self):
        w = dashboard(estate(1, 1))
        w.set_theme(themes.CIRCUIT_THEME)
        w.set_graph_settings({"background": "plain"})
        calls = []
        w._background.paint = lambda *a, **k: calls.append(a)
        render(w)
        self.assertEqual(calls, [])

    def test_calm_shifts_a_scenes_palette(self):
        w = dashboard(estate(1, 1))
        w.set_theme(themes.SYNTHWAVE_THEME)
        w.set_graph_settings({"background": "calm"})
        render(w)
        self.assertEqual(w._background.calm, 1.0)
        w.set_graph_settings({"background": "vivid"})
        render(w)
        self.assertEqual(w._background.calm, 0.0)

    def test_settings_dialog_offers_the_levels(self):
        from app.graph_settings import BACKGROUND_CHOICES, GraphSettingsDialog
        self.assertEqual([v for v, _ in BACKGROUND_CHOICES], list(BACKGROUND_LEVELS))
        w = dashboard()
        dialog = GraphSettingsDialog(w)
        dialog.cmb_background.setCurrentIndex(dialog.cmb_background.findData("vivid"))
        self.assertEqual(w.background_level(), "vivid")
        dialog.deleteLater()


class _Clock:
    def __init__(self):
        self.ns = 0

    def nsecsElapsed(self):
        return self.ns


class HoverMotionTests(unittest.TestCase):

    def test_a_hover_eases_in_springs_and_eases_back_out(self):
        w = dashboard()
        ep = make_ep("tenant", "Tenant1")
        w._hovered_node = "Tenant1"
        advance(w, HOVER_IN_S / 2)
        half = w._hover_amount(ep)
        self.assertTrue(0.0 < half < 1.0)
        advance(w, HOVER_IN_S)
        self.assertEqual(w._hover_amount(ep), 1.0)
        self.assertGreater(w._hover_lift(ep, 0.1), 1.0)
        w._hovered_node = None
        advance(w, HOVER_OUT_S + 0.05)
        self.assertEqual(w._hover_amount(ep), 0.0)
        self.assertNotIn(ep, w._hover_levels)
        self.assertEqual(w._hover_lift(ep, 0.1), 1.0)

    def test_arriving_on_a_node_sends_one_ripple(self):
        w = dashboard()
        w._hovered_conn = "t0c1"
        advance(w, 0.1)
        self.assertEqual([ep for ep, _ in w._ripples], [make_ep("conn", "t0c1")])
        advance(w, RIPPLE_S)
        self.assertEqual(w._ripples, [])

    def test_a_hover_never_moves_a_hit_rect(self):
        w = dashboard()
        render(w)
        before = {c["id"]: QRectFTuple(r) for r, c, _t in w._conn_rects}
        w._hovered_conn = "t1c2"
        advance(w, 0.5)
        render(w)
        after = {c["id"]: QRectFTuple(r) for r, c, _t in w._conn_rects}
        self.assertEqual(before, after)
        # ...but the drawn outline grew, which is what the ripple follows.
        drawn = w._drawn_nodes[make_ep("conn", "t1c2")][0]
        self.assertGreater(drawn.width(), before["t1c2"][2])

    def test_a_tenant_hover_keeps_its_cluster_lit_and_fades_the_rest(self):
        w = dashboard()
        w._hovered_node = "Tenant0"
        advance(w, 0.5)
        tenants = {}
        for c in w._connections:
            tenants.setdefault(c["tenant"], []).append(c)
        focus = w._hover_focus(tenants)
        self.assertEqual(focus.kind, "tenant")
        self.assertEqual(focus.fade(make_ep("conn", "t0c3")), 1.0)
        self.assertEqual(focus.fade(make_ep("tenant", "Tenant0")), 1.0)
        self.assertAlmostEqual(focus.fade(make_ep("tenant", "Tenant2")),
                               1.0 - FOCUS_DIM_TENANT)
        self.assertTrue(focus.lights_line("Tenant0", "t0c2"))
        self.assertFalse(focus.lights_line("Tenant1", "t1c2"))

    def test_a_machine_hover_lights_only_its_own_line(self):
        w = dashboard()
        w._hovered_conn = "t2c1"
        advance(w, 0.5)
        tenants = {}
        for c in w._connections:
            tenants.setdefault(c["tenant"], []).append(c)
        focus = w._hover_focus(tenants)
        self.assertEqual((focus.kind, focus.tenant, focus.conn_id), ("conn", "Tenant2", "t2c1"))
        self.assertTrue(focus.lights_line("Tenant2", "t2c1"))
        self.assertFalse(focus.lights_line("Tenant2", "t2c0"))
        self.assertEqual(focus.fade(make_ep("tenant", "Tenant2")), 1.0)
        self.assertLess(focus.fade(make_ep("conn", "t2c0")), 1.0)

    def test_lit_lines_are_drawn_for_the_hovered_tenant(self):
        w = dashboard()
        w._hovered_node = "Tenant1"
        advance(w, 0.5)
        lit = []
        w._draw_lit_line = lambda p, path, color, amount, phase: lit.append(amount)
        render(w)
        self.assertEqual(len(lit), 4)
        self.assertTrue(all(a == 1.0 for a in lit))

    def test_a_lens_or_the_spotlight_switches_hover_dimming_off(self):
        w = dashboard()
        w._hovered_node = "Tenant0"
        advance(w, 0.5)
        tenants = {"Tenant0": [c for c in w._connections if c["tenant"] == "Tenant0"]}
        self.assertIsNotNone(w._hover_focus(tenants))
        w.set_relation_lens(LENS_SUBNET)
        self.assertIsNone(w._hover_focus(tenants))
        w.clear_relation_lens()
        w._search_active = True
        self.assertIsNone(w._hover_focus(tenants))

    def test_with_motion_off_a_hover_is_all_or_nothing(self):
        w = dashboard(animate=False)
        w._hovered_conn = "t0c0"
        self.assertEqual(w._hover_amount(make_ep("conn", "t0c0")), 1.0)
        self.assertEqual(w._hover_amount(make_ep("conn", "t0c1")), 0.0)
        w._draw_ripples(_NoPainter())        # no motion, no rings
        render(w)

    def test_zoomed_out_a_hovered_tenant_names_its_machines(self):
        w = dashboard()
        w.set_ping_enabled(True)
        w.set_conn_statuses({"t0c0": {"state": "down"}})
        w._set_zoom(0.3)
        w._hovered_node = "Tenant0"
        advance(w, 0.5)
        named = []
        w._draw_peek_tag = lambda p, rect, conn, color, amount, anchor, dpr: named.append(conn["id"])
        render(w)
        # The down machine already wears a problem tag; the rest get names.
        self.assertEqual(sorted(named), ["t0c1", "t0c2", "t0c3"])


class QRectFTuple(tuple):
    def __new__(cls, r):
        return super().__new__(cls, (round(r.x(), 3), round(r.y(), 3),
                                     round(r.width(), 3), round(r.height(), 3)))


class _NoPainter:
    def __getattr__(self, name):
        raise AssertionError(f"painted ({name}) with motion off")


class HoverCardTests(unittest.TestCase):

    def test_the_card_waits_then_fades_in_and_out(self):
        w = dashboard()
        w._hovered_conn = "t1c0"
        advance(w, HOVER_CARD_DELAY_S * 0.8)
        self.assertEqual(w._card_level, 0.0)
        advance(w, 0.4)
        self.assertEqual(w._card_level, 1.0)
        w._hovered_conn = None
        advance(w, 0.3)
        self.assertEqual(w._card_level, 0.0)
        self.assertIsNone(w._card_ep)

    def test_once_a_card_is_up_the_next_one_comes_straight_up(self):
        w = dashboard()
        w._hovered_conn = "t1c0"
        advance(w, 1.0)
        w._hovered_conn = "t1c1"
        advance(w, 0.1)
        self.assertGreater(w._card_level, 0.5)

    def test_machine_card_says_where_it_is_and_how_it_is(self):
        w = dashboard()
        w.set_ping_enabled(True)
        w.set_conn_statuses({"t0c1": {"state": "up", "latency_ms": 4.0}})
        title, _accent, rows = w._hover_card_content(make_ep("conn", "t0c1"))
        texts = [t for t, _c, _d in rows]
        self.assertEqual(title, "SRV0-1")
        self.assertIn("10.0.0.11:22", texts)
        self.assertIn("RDP  ·  Tenant0", texts)
        self.assertTrue(any(t.startswith("Reachable") for t in texts))

    def test_tenant_card_counts_its_trouble(self):
        w = dashboard()
        w.set_ping_enabled(True)
        w.set_conn_statuses({"t0c0": {"state": "down"}, "t0c1": {"state": "down"},
                             "t0c2": {"state": "degraded"}, "t0c3": {"state": "up"}})
        _title, _accent, rows = w._hover_card_content(make_ep("tenant", "Tenant0"))
        texts = [t for t, _c, _d in rows]
        self.assertIn("4 connections", texts)
        self.assertIn("2 unreachable", texts)
        self.assertIn("1 with a service down", texts)
        w.set_conn_statuses({f"t1c{i}": {"state": "up"} for i in range(4)})
        _title, _accent, rows = w._hover_card_content(make_ep("tenant", "Tenant1"))
        self.assertIn("All reachable", [t for t, _c, _d in rows])

    def test_the_card_stays_away_while_searching(self):
        w = dashboard()
        w._hovered_conn = "t0c0"
        advance(w, 1.0)
        render(w)
        drawn = []
        w._hover_card_pixmap = lambda *a: drawn.append(a) or QPixmap(10, 10)
        w._search_active = True
        render(w)
        self.assertEqual(drawn, [])
        w._search_active = False
        render(w)
        self.assertEqual(len(drawn), 1)

    def test_hovering_keeps_the_text_for_screen_readers(self):
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        w = dashboard()
        render(w)
        rect = next(r for r, c, _t in w._conn_rects if c["id"] == "t0c2")
        ev = QMouseEvent(QEvent.Type.MouseMove, rect.center(), rect.center(),
                         Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                         Qt.KeyboardModifier.NoModifier)
        w.mouseMoveEvent(ev)
        self.assertEqual(w.toolTip(), "")
        self.assertIn("10.0.0.12:22", w.accessibleDescription())


class SidebarHoverTests(unittest.TestCase):

    def setUp(self):
        from app.side_panel import SidePanel
        self.panel = SidePanel()
        self.panel.resize(320, 600)
        self.panel.update_connections(estate(2, 3))
        self.hover = self.panel._delegate.hover

    def tearDown(self):
        self.panel.deleteLater()

    def _first_index(self, kind):
        from app.side_panel import KIND_ROLE
        model = self.panel.tree.model()
        for row in range(model.rowCount()):
            index = model.index(row, 0)
            if index.data(KIND_ROLE) == kind:
                return index
        self.fail(f"no {kind} row")

    def test_a_row_eases_in_and_back_out(self):
        from app.side_panel import _row_key
        index = self._first_index("tenant")
        key = _row_key(index)
        self.hover._aim(key)
        self.hover._step(self.hover.IN_S / 2)
        self.assertTrue(0.0 < self.hover.level(index) < 1.0)
        self.assertTrue(self.hover._step(self.hover.IN_S / 2 - 0.001))
        self.hover._step(0.01)
        self.assertEqual(self.hover.level(index), 1.0)
        self.hover._aim(None)
        self.assertFalse(self.hover._step(self.hover.OUT_S + 0.01))
        self.assertEqual(self.hover.level(index), 0.0)

    def test_arriving_from_outside_counts_as_a_hover(self):
        """The first event into the list is an Enter, and a cursor that then
        rests sends no move after it."""
        from PyQt6.QtCore import QPointF
        from PyQt6.QtGui import QEnterEvent
        from app.side_panel import _row_key
        index = self._first_index("tenant")
        rect = self.panel.tree.visualRect(index)
        centre = QPointF(rect.center())
        self.hover.eventFilter(self.panel.tree.viewport(),
                               QEnterEvent(centre, centre, centre))
        self.assertEqual(self.hover.current, _row_key(index))

    def test_rows_paint_while_easing(self):
        index = self._first_index("tenant")
        from app.side_panel import _row_key
        self.hover._aim(_row_key(index))
        self.hover._step(0.05)
        pm = QPixmap(self.panel.tree.viewport().size())
        self.panel.tree.viewport().render(pm)


if __name__ == "__main__":
    unittest.main()
