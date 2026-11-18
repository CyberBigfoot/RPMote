"""Hand-placed tenants and branches come back exactly where they were left.

Positions used to be normalised by the dashboard's min(width, height), and
only the tiles that were actually dragged were pinned; everything else was
re-laid out from the viewport each time.  So a sidebar toggle, a different
window size, or an added connection shuffled the graph on every restart.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QPixmap
from PyQt6.QtWidgets import QApplication

from app.dashboard import POSITION_UNITS, DashboardWidget, make_ep, parse_ep

_APP = QApplication.instance() or QApplication([])


def estate(tenants: int, per_tenant: int) -> list:
    return [
        {"id": f"t{t}c{c}", "name": f"SRV{t}-{c}", "host": f"10.{t}.0.{c + 10}",
         "port": 22, "protocol": "ssh", "tenant": f"Tenant{t}"}
        for t in range(tenants) for c in range(per_tenant)
    ]


def dashboard(conns, w=1200, h=800, **settings) -> DashboardWidget:
    widget = DashboardWidget(conns, {})
    widget.resize(w, h)
    widget.set_graph_settings({"layout_mode": "free", "animate": False, **settings})
    return widget


def render(widget):
    widget.render(QPixmap(widget.width(), widget.height()))


def tenant_centres(widget) -> dict:
    return {parse_ep(ep)[1]: (x, y)
            for ep, (x, y, _w, _h) in widget._layout_nodes().items()
            if parse_ep(ep)[0] == "tenant"}


def branch_centres(widget) -> dict:
    return {parse_ep(ep)[1]: (x, y)
            for ep, (x, y, _w, _h) in widget._layout_nodes().items()
            if parse_ep(ep)[0] == "conn"}


def relative_to_centre(widget, points: dict) -> dict:
    cx, cy = widget._scene_center()
    return {k: (round(x - cx, 3), round(y - cy, 3)) for k, (x, y) in points.items()}


def _mouse(widget, kind, x, y):
    button = Qt.MouseButton.LeftButton
    held = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else button
    ev = QMouseEvent(kind, QPointF(x, y), QPointF(x, y), held, button,
                     Qt.KeyboardModifier.NoModifier)
    {QEvent.Type.MouseButtonPress: widget.mousePressEvent,
     QEvent.Type.MouseMove: widget.mouseMoveEvent,
     QEvent.Type.MouseButtonRelease: widget.mouseReleaseEvent}[kind](ev)


def drag(widget, x1, y1, x2, y2):
    _mouse(widget, QEvent.Type.MouseButtonPress, x1, y1)
    for t in (0.34, 0.67, 1.0):
        _mouse(widget, QEvent.Type.MouseMove, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
    _mouse(widget, QEvent.Type.MouseButtonRelease, x2, y2)


def screen_rect_of_tenant(widget, tenant):
    render(widget)
    for rect, name in widget._node_rects:
        if name == tenant:
            return rect
    raise AssertionError(f"{tenant} was not drawn")


def screen_rect_of_conn(widget, cid):
    render(widget)
    for rect, conn, _t in widget._conn_rects:
        if str(conn.get("id")) == cid:
            return rect
    raise AssertionError(f"{cid} was not drawn")


class SavedFormTests(unittest.TestCase):

    def test_a_drag_saves_pixel_offsets_from_the_scene_centre(self):
        widget = dashboard(estate(3, 2))
        self.addCleanup(widget.deleteLater)
        saved = []
        widget.node_positions_changed.connect(saved.append)
        rect = screen_rect_of_tenant(widget, "Tenant0")
        drag(widget, rect.center().x(), rect.center().y(),
             rect.center().x() + 120, rect.center().y() - 80)

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0][POSITION_UNITS], "px")
        cx, cy = widget._scene_center()
        (x, y) = tenant_centres(widget)["Tenant0"]
        self.assertAlmostEqual(saved[0]["Tenant0"][0], x - cx, places=3)
        self.assertAlmostEqual(saved[0]["Tenant0"][1], y - cy, places=3)

    def test_saved_positions_survive_a_different_window_size(self):
        """The bug: the same vault opened at another size shuffled the graph."""
        first = dashboard(estate(3, 2), 1500, 900)
        self.addCleanup(first.deleteLater)
        saved = []
        first.node_positions_changed.connect(saved.append)
        rect = screen_rect_of_tenant(first, "Tenant1")
        drag(first, rect.center().x(), rect.center().y(),
             rect.center().x() - 200, rect.center().y() + 150)
        before = relative_to_centre(first, tenant_centres(first))

        # Sidebar shown this time, so the dashboard is narrower and squatter.
        second = dashboard(estate(3, 2), 1000, 700)
        self.addCleanup(second.deleteLater)
        second.set_node_positions(saved[-1])
        self.assertEqual(relative_to_centre(second, tenant_centres(second)), before)

    def test_old_normalised_saves_still_land_where_they_used_to(self):
        widget = dashboard(estate(2, 1), 1200, 800)
        self.addCleanup(widget.deleteLater)
        widget.set_node_positions({"Tenant0": [0.25, -0.125]})   # no marker: legacy
        cx, cy = widget._scene_center()
        x, y = tenant_centres(widget)["Tenant0"]
        self.assertAlmostEqual(x, cx + 0.25 * 800)
        self.assertAlmostEqual(y, cy - 0.125 * 800)
        self.assertTrue(widget.has_custom_position("Tenant0"))

    def test_old_saves_are_upgraded_the_next_time_anything_is_saved(self):
        widget = dashboard(estate(3, 1), 1200, 800)
        self.addCleanup(widget.deleteLater)
        widget.set_node_positions({"Tenant0": [0.25, -0.125]})
        legacy_spot = tenant_centres(widget)["Tenant0"]
        saved = []
        widget.node_positions_changed.connect(saved.append)

        rect = screen_rect_of_tenant(widget, "Tenant2")
        drag(widget, rect.center().x(), rect.center().y(),
             rect.center().x() + 40, rect.center().y() + 40)

        self.assertEqual(saved[-1][POSITION_UNITS], "px")
        cx, cy = widget._scene_center()
        self.assertAlmostEqual(saved[-1]["Tenant0"][0], legacy_spot[0] - cx, places=3)
        self.assertAlmostEqual(saved[-1]["Tenant0"][1], legacy_spot[1] - cy, places=3)
        self.assertEqual(widget._legacy_positions, {})

    def test_reset_all_forgets_old_and_new_saves_alike(self):
        widget = dashboard(estate(2, 1))
        self.addCleanup(widget.deleteLater)
        widget.set_node_positions({"Tenant0": [0.25, -0.125]})
        saved = []
        widget.node_positions_changed.connect(saved.append)
        widget.reset_all_positions()
        self.assertEqual(saved, [{}])
        self.assertFalse(widget.has_custom_position("Tenant0"))


class FreezeOnDragTests(unittest.TestCase):

    def test_dragging_one_tenant_pins_the_others_where_they_are(self):
        widget = dashboard(estate(4, 2), 1200, 800)
        self.addCleanup(widget.deleteLater)
        saved = []
        widget.node_positions_changed.connect(saved.append)
        others_before = {k: v for k, v in tenant_centres(widget).items()
                         if k != "Tenant0"}

        rect = screen_rect_of_tenant(widget, "Tenant0")
        drag(widget, rect.center().x(), rect.center().y(),
             rect.center().x() + 90, rect.center().y() - 60)
        self.assertEqual(set(saved[-1]) - {POSITION_UNITS},
                         {"Tenant0", "Tenant1", "Tenant2", "Tenant3"})

        # Things that used to reflow the automatic layout: a wider viewport
        # (sidebar hidden) and a tenant growing a connection.
        widget.resize(1500, 800)
        widget.set_data(estate(4, 2) + [{"id": "extra", "name": "NEW", "host": "10.9.9.9",
                                         "port": 22, "protocol": "ssh", "tenant": "Tenant3"}], {})
        after = tenant_centres(widget)
        # The scene centre moved with the viewport; everything else did not.
        dx = widget._scene_center()[0] - 600
        for tn, (x, y) in others_before.items():
            self.assertAlmostEqual(after[tn][0], x + dx, places=3, msg=tn)
            self.assertAlmostEqual(after[tn][1], y, places=3, msg=tn)

    def test_an_untouched_graph_still_lays_itself_out(self):
        """Nothing is pinned until the user picks something up."""
        widget = dashboard(estate(3, 2))
        self.addCleanup(widget.deleteLater)
        render(widget)
        self.assertEqual(widget._custom_positions, {})
        self.assertFalse(widget.has_custom_position("Tenant0"))

    def test_dragging_one_branch_pins_its_siblings(self):
        widget = dashboard(estate(2, 4), 1200, 800)
        self.addCleanup(widget.deleteLater)
        saved = []
        widget.conn_positions_changed.connect(saved.append)
        siblings_before = {k: v for k, v in branch_centres(widget).items()
                           if k.startswith("t0c") and k != "t0c1"}

        rect = screen_rect_of_conn(widget, "t0c1")
        drag(widget, rect.center().x(), rect.center().y(),
             rect.center().x() + 70, rect.center().y() + 70)
        self.assertEqual({k for k in saved[-1] if k.startswith("t0c")},
                         {"t0c0", "t0c1", "t0c2", "t0c3"})
        # Tenant1's branches were not part of it and stay automatic.
        self.assertFalse(any(k.startswith("t1c") for k in saved[-1]))

        # A fifth machine used to re-ring all of Tenant0's branches.
        widget.set_data(estate(2, 4) + [{"id": "t0c9", "name": "SRV0-9", "host": "10.0.0.99",
                                         "port": 22, "protocol": "ssh", "tenant": "Tenant0"}], {})
        after = branch_centres(widget)
        for cid, (x, y) in siblings_before.items():
            self.assertAlmostEqual(after[cid][0], x, places=3, msg=cid)
            self.assertAlmostEqual(after[cid][1], y, places=3, msg=cid)

    def test_a_click_without_movement_pins_nothing(self):
        widget = dashboard(estate(3, 2))
        self.addCleanup(widget.deleteLater)
        saved = []
        widget.node_positions_changed.connect(saved.append)
        rect = screen_rect_of_tenant(widget, "Tenant0")
        _mouse(widget, QEvent.Type.MouseButtonPress, rect.center().x(), rect.center().y())
        _mouse(widget, QEvent.Type.MouseButtonRelease, rect.center().x(), rect.center().y())
        self.assertEqual(saved, [])
        self.assertEqual(widget._custom_positions, {})

    def test_hub_mode_pins_the_tenants_on_screen(self):
        widget = dashboard(estate(3, 2), layout_mode="hub")
        self.addCleanup(widget.deleteLater)
        widget.set_tenant_expanded("Tenant0", True)
        widget.set_tenant_expanded("Tenant1", True)
        saved = []
        widget.node_positions_changed.connect(saved.append)
        rect = screen_rect_of_tenant(widget, "Tenant0")
        drag(widget, rect.center().x(), rect.center().y(),
             rect.center().x() + 50, rect.center().y() + 50)
        self.assertEqual(set(saved[-1]) - {POSITION_UNITS}, {"Tenant0", "Tenant1"})
        self.assertFalse(widget.has_custom_position("Tenant2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
