"""Graph-at-scale tests: collapsing tenants, and tidying a dragged layout.

A three-hundred-connection estate drawn all at once is not a diagram, so the
free-floating layout now holds a tenant's machines back until it is opened.
These tests pin down when that happens, what stays reachable while it is
collapsed, and that tidying a layout rearranges it without throwing away the
arrangement the user built.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QPixmap
from PyQt6.QtWidgets import QApplication

from app.dashboard import (
    COLLAPSE_AUTO_THRESHOLD, DashboardWidget, make_ep, parse_ep,
)

_APP = QApplication.instance() or QApplication([])

TENANT_COLORS = {f"Tenant{i}": "#a78bfa" for i in range(8)}


def estate(tenants: int, per_tenant: int) -> list:
    return [
        {"id": f"t{t}c{c}", "name": f"SRV{t}-{c}", "host": f"10.{t}.0.{c + 10}",
         "port": 22, "protocol": "ssh", "tenant": f"Tenant{t}"}
        for t in range(tenants) for c in range(per_tenant)
    ]


def dashboard(conns, free=True) -> DashboardWidget:
    widget = DashboardWidget(conns, dict(TENANT_COLORS))
    widget.resize(1200, 800)
    if free:
        widget.set_graph_settings({"layout_mode": "free", "animate": False})
    return widget


def tenant_boxes(widget) -> dict:
    return {parse_ep(ep)[1]: box
            for ep, box in widget._layout_nodes().items()
            if parse_ep(ep)[0] == "tenant"}


def branch_count(widget, tenant: str) -> int:
    """Connections of *tenant* that have a laid-out node of their own."""
    laid_out = set(widget._layout_nodes())
    return sum(1 for c in widget._connections
               if c["tenant"] == tenant
               and make_ep("conn", c["id"]) in laid_out)


def overlaps(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return (abs(ax - bx) * 2 < (aw + bw)) and (abs(ay - by) * 2 < (ah + bh))


def click(widget, x, y):
    pos = QPointF(x, y)
    for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        event = QMouseEvent(kind, pos, pos, Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
        (widget.mousePressEvent if kind == QEvent.Type.MouseButtonPress
         else widget.mouseReleaseEvent)(event)


class WhenCollapsingAppliesTests(unittest.TestCase):

    def test_a_small_estate_is_left_exactly_as_it_was(self):
        widget = dashboard(estate(2, 4))
        self.addCleanup(widget.deleteLater)
        self.assertLess(len(widget._connections), COLLAPSE_AUTO_THRESHOLD)
        self.assertFalse(widget.collapsing_active())
        self.assertTrue(widget.is_tenant_expanded("Tenant0"))

    def test_a_large_estate_collapses_on_its_own(self):
        widget = dashboard(estate(8, 9))
        self.addCleanup(widget.deleteLater)
        self.assertGreaterEqual(len(widget._connections), COLLAPSE_AUTO_THRESHOLD)
        self.assertTrue(widget.collapsing_active())
        self.assertFalse(widget.is_tenant_expanded("Tenant0"))

    def test_hub_mode_is_never_governed_by_this(self):
        """Hub mode has its own collapse — a tenant is drawn or it is not."""
        widget = dashboard(estate(8, 9), free=False)
        self.addCleanup(widget.deleteLater)
        self.assertFalse(widget.collapsing_active())

    def test_the_setting_overrides_the_estate_size_both_ways(self):
        small = dashboard(estate(2, 4))
        self.addCleanup(small.deleteLater)
        small.set_graph_option("collapse_tenants", "always")
        self.assertTrue(small.collapsing_active())

        large = dashboard(estate(8, 9))
        self.addCleanup(large.deleteLater)
        large.set_graph_option("collapse_tenants", "never")
        self.assertFalse(large.collapsing_active())
        self.assertTrue(large.is_tenant_expanded("Tenant0"))


class CollapsedBehaviourTests(unittest.TestCase):

    def setUp(self):
        self.widget = dashboard(estate(6, 9))
        self.addCleanup(self.widget.deleteLater)
        self.widget.collapse_all_tenants()

    def paint(self):
        pix = QPixmap(self.widget.size())
        self.widget.render(pix)

    def test_a_collapsed_tenant_keeps_its_tile_but_drops_its_branches(self):
        boxes = tenant_boxes(self.widget)
        self.assertEqual(len(boxes), 6)
        self.assertEqual(branch_count(self.widget, "Tenant0"), 0)

    def test_opening_one_tenant_reveals_only_that_one(self):
        self.widget.set_tenant_expanded("Tenant2", True)
        self.assertEqual(branch_count(self.widget, "Tenant2"), 9)
        self.assertEqual(branch_count(self.widget, "Tenant3"), 0)

    def test_a_collapsed_tenant_takes_only_the_room_its_tile_needs(self):
        """Otherwise a fully collapsed estate is scattered over a huge canvas."""
        collapsed = self.widget._content_bounds()
        self.widget.expand_all_tenants()
        expanded = self.widget._content_bounds()
        self.assertLess(collapsed.width() * collapsed.height(),
                        expanded.width() * expanded.height())

    def test_a_hidden_machine_still_has_somewhere_to_be_pointed_at(self):
        """Links and relationship lenses aim at the tile when a branch is hidden."""
        self.paint()
        hidden = make_ep("conn", "t0c3")
        tile = make_ep("tenant", "Tenant0")
        self.assertIn(hidden, self.widget._ep_geom)
        self.assertEqual(self.widget._ep_geom[hidden],
                         self.widget._ep_geom[tile])

    def test_clicking_a_tile_opens_it_and_clicking_again_closes_it(self):
        self.paint()                       # populates the hit-test rects
        rect, tenant = self.widget._node_rects[0]
        centre = rect.center()
        click(self.widget, centre.x(), centre.y())
        self.assertTrue(self.widget.is_tenant_expanded(tenant))
        self.paint()
        rect = next(r for r, t in self.widget._node_rects if t == tenant)
        centre = rect.center()
        click(self.widget, centre.x(), centre.y())
        self.assertFalse(self.widget.is_tenant_expanded(tenant))

    def test_what_was_open_survives_a_restart(self):
        self.widget.set_tenant_expanded("Tenant4", True)
        state = self.widget.get_view_state()

        fresh = dashboard(estate(6, 9))
        self.addCleanup(fresh.deleteLater)
        fresh.set_view_state(state)
        self.assertTrue(fresh.is_tenant_expanded("Tenant4"))
        self.assertFalse(fresh.is_tenant_expanded("Tenant0"))

    def test_a_tenant_deleted_since_last_time_does_not_come_back(self):
        state = dict(self.widget.get_view_state())
        state["visible_tenants"] = ["Tenant4", "TenantGone"]
        self.widget.set_view_state(state)
        self.assertNotIn("TenantGone", self.widget._visible_tenants)

    def test_collapsed_and_expanded_estates_both_paint(self):
        self.paint()
        self.widget.expand_all_tenants()
        self.paint()


class TidyLayoutTests(unittest.TestCase):

    def setUp(self):
        self.widget = dashboard(estate(6, 4))
        self.addCleanup(self.widget.deleteLater)
        self.moves = []
        self.widget.node_positions_changed.connect(self.moves.append)

    def pile_up(self):
        """Drop every tenant on top of the same spot."""
        self.widget.set_node_positions(
            {f"Tenant{i}": [0.5 + i * 0.008, 0.5 + i * 0.008] for i in range(6)})

    def test_tidying_separates_tiles_that_were_piled_up(self):
        self.pile_up()
        boxes = tenant_boxes(self.widget)
        self.assertTrue(any(overlaps(boxes["Tenant0"], boxes[t])
                            for t in boxes if t != "Tenant0"))

        self.widget.tidy_layout()

        boxes = tenant_boxes(self.widget)
        names = sorted(boxes)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                self.assertFalse(overlaps(boxes[a], boxes[b]),
                                 f"{a} still overlaps {b}")

    def test_tidying_keeps_the_arrangement_the_user_built(self):
        """A tile that was top-left stays top-left — this is not a reset."""
        self.widget.set_node_positions({
            "Tenant0": [0.15, 0.2], "Tenant1": [0.5, 0.2], "Tenant2": [0.85, 0.2],
            "Tenant3": [0.15, 0.8], "Tenant4": [0.5, 0.8], "Tenant5": [0.85, 0.8],
        })
        self.widget.tidy_layout()
        boxes = tenant_boxes(self.widget)
        top = {t for t in boxes if boxes[t][1] < boxes["Tenant4"][1]}
        self.assertEqual(top, {"Tenant0", "Tenant1", "Tenant2"})
        self.assertLess(boxes["Tenant0"][0], boxes["Tenant1"][0])
        self.assertLess(boxes["Tenant1"][0], boxes["Tenant2"][0])

    def test_tidying_reports_and_announces_what_it_moved(self):
        self.pile_up()
        moved = self.widget.tidy_layout()
        self.assertEqual(moved, 6)
        self.assertEqual(len(self.moves), 1, "the new positions must be saved")

    def test_tidying_is_a_free_mode_tool_only(self):
        self.widget.set_graph_option("layout_mode", "hub")
        self.assertEqual(self.widget.tidy_layout(), 0)

    def test_one_tenant_has_no_layout_to_tidy(self):
        solo = dashboard(estate(1, 3))
        self.addCleanup(solo.deleteLater)
        self.assertEqual(solo.tidy_layout(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
