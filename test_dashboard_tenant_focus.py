"""Tenant selection frames the cluster without changing the saved layout."""

import copy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QPixmap, QWheelEvent
from PyQt6.QtWidgets import QApplication

from app.dashboard import DashboardWidget, HOVER_IN_S, POSITION_UNITS, make_ep
from app.main_window import MainWindow
from app.side_panel import CONN_ROLE, SidePanel, TENANT_ROLE


_APP = QApplication.instance() or QApplication([])


class TenantFocusTests(unittest.TestCase):
    def setUp(self):
        # These tests call the sidebar click handler directly. Make the click
        # unmodified so desktop key state or earlier Qt tests cannot turn it
        # into a multi-selection instead of navigation.
        modifiers = patch("app.side_panel.QGuiApplication.keyboardModifiers",
                          return_value=Qt.KeyboardModifier.NoModifier)
        modifiers.start()
        self.addCleanup(modifiers.stop)
        self.connections = [
            {"id": f"t{tenant}c{conn}", "name": f"Server {tenant}-{conn}",
             "host": f"10.{tenant}.0.{conn + 1}", "protocol": "ssh",
             "tenant": f"Tenant{tenant}"}
            for tenant in range(4) for conn in range(12)
        ]
        self.widget = DashboardWidget(self.connections, {})
        self.addCleanup(self.widget.deleteLater)
        self.widget.resize(1200, 800)
        self.widget.set_graph_settings({"animate": False})
        self.widget.set_node_positions({
            POSITION_UNITS: "px", "Tenant0": [-1600, -700],
            "Tenant1": [1800, 900], "Tenant2": [2300, -900],
            "Tenant3": [-2000, 1300],
        })
        self.widget.set_conn_positions({
            "t1c0": [-550, 220], "t1c1": [620, -310],
        })

    def render(self):
        self.widget.render(QPixmap(self.widget.size()))

    def camera(self):
        return self.widget._zoom, self.widget._pan.x(), self.widget._pan.y()

    def assert_cluster_framed(self, tenant, count=12):
        self.render()
        tiles = [rect for rect, name in self.widget._node_rects if name == tenant]
        branches = [rect for rect, _conn, name in self.widget._conn_rects
                    if name == tenant]
        self.assertEqual(len(tiles), 1)
        self.assertEqual(len(branches), count)
        for rect in tiles + branches:
            self.assertGreaterEqual(rect.left(), 0)
            self.assertGreaterEqual(rect.top(), 0)
            self.assertLessEqual(rect.right(), self.widget.width())
            self.assertLessEqual(rect.bottom(), self.widget.height())
        bounds = self.widget._content_bounds(tenant)
        x, y = self.widget._to_screen(bounds.center().x(), bounds.center().y())
        self.assertAlmostEqual(x, self.widget.width() / 2)
        self.assertAlmostEqual(y, self.widget.height() / 2)

    def assert_saved_camera(self, expected):
        state = self.widget.get_view_state()
        self.assertEqual((state["zoom"], state["pan_x"], state["pan_y"]), expected)

    def select_from_canvas(self, tenant):
        self.render()
        rect = next(rect for rect, name in self.widget._node_rects if name == tenant)
        pos = rect.center()
        for kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            event = QMouseEvent(kind, pos, pos, Qt.MouseButton.LeftButton,
                                Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier)
            (self.widget.mousePressEvent if kind == QEvent.Type.MouseButtonPress
             else self.widget.mouseReleaseEvent)(event)

    def test_sidebar_selection_frames_an_offscreen_hub_tenant(self):
        self.widget._zoom = 0.35
        self.widget._pan = QPointF(-450, 310)
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        self.assert_cluster_framed("Tenant1")

    def test_selection_from_expand_all_frames_only_the_selected_cluster(self):
        self.widget.expand_all_tenants()
        self.widget.fit_to_view()
        estate_camera = self.camera()
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        self.assert_cluster_framed("Tenant1")
        self.assertTrue(self.widget._show_all_connections)
        self.assertNotEqual(self.camera(), estate_camera)

    def test_free_selection_opens_the_collapsed_tenants_connections(self):
        self.widget.set_graph_settings({"layout_mode": "free",
                                        "collapse_tenants": "always"})
        self.widget.collapse_all_tenants()
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        self.assert_cluster_framed("Tenant1")
        self.assertFalse(self.widget.is_tenant_expanded("Tenant0"))

    def test_canvas_click_frames_the_tenant_and_closing_restores_the_view(self):
        self.widget.set_graph_settings({"layout_mode": "free",
                                        "collapse_tenants": "always"})
        self.widget.collapse_all_tenants()
        self.widget.fit_to_view()
        before = self.camera()
        self.select_from_canvas("Tenant1")
        self.assert_cluster_framed("Tenant1")
        self.select_from_canvas("Tenant1")
        self.assertEqual(self.camera(), before)
        self.assertFalse(self.widget.is_tenant_expanded("Tenant1"))

    def test_canvas_selection_also_frames_an_already_expanded_tenant(self):
        self.widget.expand_all_tenants()
        self.widget.fit_to_view()
        self.select_from_canvas("Tenant1")
        self.assert_cluster_framed("Tenant1")

    def test_selection_never_changes_or_saves_node_positions(self):
        nodes = copy.deepcopy(self.widget._custom_positions)
        branches = copy.deepcopy(self.widget._conn_positions)
        saved_nodes, saved_branches = [], []
        self.widget.node_positions_changed.connect(saved_nodes.append)
        self.widget.conn_positions_changed.connect(saved_branches.append)
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        self.assert_cluster_framed("Tenant1")
        self.assertEqual(self.widget._custom_positions, nodes)
        self.assertEqual(self.widget._conn_positions, branches)
        self.assertEqual(saved_nodes, [])
        self.assertEqual(saved_branches, [])

    def test_temporary_camera_is_not_saved_even_after_manual_panning(self):
        self.widget._zoom = 0.7
        self.widget._pan = QPointF(130, -85)
        before = self.camera()
        emitted = []
        self.widget.view_state_changed.connect(emitted.append)
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        self.widget.pan_by(50, 70)
        self.widget.zoom_out()
        self.assert_saved_camera(before)
        for state in emitted:
            self.assertEqual((state["zoom"], state["pan_x"], state["pan_y"]), before)

    def test_expand_all_restores_the_original_camera_after_switching_tenants(self):
        self.widget.expand_all_tenants()
        self.widget.fit_to_view()
        before = self.camera()
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        self.widget.reveal_tenant("Tenant2", exclusive=True)
        self.assert_cluster_framed("Tenant2")
        self.assertTrue(self.widget.expand_all_tenants())
        self.assertEqual(self.camera(), before)

    def test_collapsing_all_restores_the_original_camera(self):
        self.widget._pan = QPointF(90, -40)
        before = self.camera()
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        self.widget.collapse_all_tenants()
        self.assertEqual(self.camera(), before)

    def test_sidebar_resize_keeps_the_selected_cluster_in_view(self):
        self.widget.show()
        _APP.processEvents()
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        self.widget.resize(850, 620)
        _APP.processEvents()
        self.assert_cluster_framed("Tenant1")
        self.widget.hide()

    def test_a_distant_saved_branch_fits_below_the_normal_zoom_limit(self):
        self.widget.set_conn_positions({"t1c0": [9000, 4200]})
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        self.assertLess(self.widget._zoom, self.widget._min_zoom)
        self.assert_cluster_framed("Tenant1")

    def test_zoom_out_does_not_zoom_in_from_a_wide_tenant_view(self):
        self.widget.set_conn_positions({"t1c0": [9000, 4200]})
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        before = self.widget._zoom
        self.widget.zoom_out()
        self.assertLess(self.widget._zoom, before)
        self.assert_cluster_framed("Tenant1")

    def test_wheel_zoom_out_keeps_its_direction_and_cursor_anchor(self):
        self.widget.set_conn_positions({"t1c0": [9000, 4200]})
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        zoom = self.widget._zoom
        pos = QPointF(400, 270)
        logical = self.widget._to_logical(pos.x(), pos.y())
        event = QWheelEvent(pos, pos, QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        self.widget.wheelEvent(event)
        self.assertLess(self.widget._zoom, zoom)
        x, y = self.widget._to_screen(*logical)
        self.assertAlmostEqual(x, pos.x())
        self.assertAlmostEqual(y, pos.y())

    def test_idless_connections_are_included_in_the_frame(self):
        self.connections[12].pop("id")
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        self.assert_cluster_framed("Tenant1")

    def test_hidden_branches_frame_the_tenant_tile(self):
        self.widget.set_graph_settings({"show_branches": False})
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        self.assert_cluster_framed("Tenant1", count=0)

    def test_opening_a_connection_can_reveal_without_changing_the_camera(self):
        before = self.camera()
        self.widget.reveal_tenant("Tenant1", focus=False)
        self.assertEqual(self.camera(), before)
        self.assert_saved_camera(before)

    def test_deleting_the_focused_tenant_restores_the_view(self):
        before = self.camera()
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        remaining = [conn for conn in self.connections if conn["tenant"] != "Tenant1"]
        self.widget.set_data(remaining, {})
        self.assertEqual(self.camera(), before)

    def test_sidebar_tenant_click_and_close_use_the_correct_window_handlers(self):
        panel = SidePanel()
        self.addCleanup(panel.deleteLater)
        panel.update_connections(self.connections)
        window = SimpleNamespace(dashboard=self.widget)
        panel.tenant_focus_requested.connect(
            lambda tenant: MainWindow._on_sidebar_tenant_focus_requested(window, tenant))
        panel.tenant_close_requested.connect(
            lambda tenant: MainWindow._on_sidebar_tenant_close_requested(window, tenant))
        item = next(panel.tree.topLevelItem(i)
                    for i in range(panel.tree.topLevelItemCount())
                    if panel.tree.topLevelItem(i).data(0, TENANT_ROLE) == "Tenant1")
        before = self.camera()
        panel._on_item_clicked(item, 0)
        self.assert_cluster_framed("Tenant1")
        self.assertTrue(item.isExpanded())
        panel._on_item_clicked(item, 0)
        self.assertFalse(item.isExpanded())
        self.assertEqual(self.camera(), before)
        self.assertNotIn("Tenant1", self.widget._visible_tenants)

    def test_expand_all_window_action_restores_without_queuing_another_fit(self):
        self.widget.expand_all_tenants()
        self.widget.fit_to_view()
        before = self.camera()
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        window = SimpleNamespace(dashboard=self.widget)
        with patch("app.main_window.QTimer.singleShot") as queued:
            MainWindow._expand_all_tenants(window)
        queued.assert_not_called()
        self.assertEqual(self.camera(), before)

    def test_expand_all_window_action_still_fits_when_there_is_no_tenant_focus(self):
        window = SimpleNamespace(dashboard=self.widget)
        with patch("app.main_window.QTimer.singleShot") as queued:
            MainWindow._expand_all_tenants(window)
        queued.assert_called_once_with(0, self.widget.fit_to_view)

    def test_sidebar_connection_click_highlights_its_node_without_opening_a_session(self):
        panel = SidePanel()
        self.addCleanup(panel.deleteLater)
        panel.update_connections(self.connections)
        window = SimpleNamespace(dashboard=self.widget)
        panel.connection_focus_requested.connect(
            lambda cid: MainWindow._on_sidebar_connection_focus_requested(window, cid))
        opened = []
        panel.connect_requested.connect(opened.append)
        item = next(panel.tree.topLevelItem(i).child(j)
                    for i in range(panel.tree.topLevelItemCount())
                    for j in range(panel.tree.topLevelItem(i).childCount())
                    if (panel.tree.topLevelItem(i).child(j).data(0, CONN_ROLE) or {})
                    .get("id") == "t1c2")
        panel._on_item_clicked(item, 0)
        self.assertEqual(opened, [])
        self.assertEqual(self.widget._selected_conn, "t1c2")
        self.assertEqual(self.widget._hover_targets(), {make_ep("conn", "t1c2")})
        self.assert_cluster_framed("Tenant1")
        rect = next(r for r, c, _t in self.widget._conn_rects if c["id"] == "t1c2")
        self.assertGreater(self.widget._drawn_nodes[make_ep("conn", "t1c2")][0].width(),
                           rect.width())

    def test_sidebar_selection_renders_the_same_highlight_as_mouse_hover(self):
        self.widget.select_connection("t1c2")
        selected = QPixmap(self.widget.size())
        self.widget.render(selected)
        self.widget._highlighted_conn = None
        self.widget._hovered_conn = "t1c2"
        hovered = QPixmap(self.widget.size())
        self.widget.render(hovered)
        self.assertEqual(selected.toImage(), hovered.toImage())

    def test_connection_selection_lights_only_its_own_branch(self):
        self.widget.select_connection("t1c2")
        tenants = {"Tenant1": [c for c in self.connections if c["tenant"] == "Tenant1"]}
        focus = self.widget._hover_focus(tenants)
        self.assertEqual((focus.kind, focus.tenant, focus.conn_id),
                         ("conn", "Tenant1", "t1c2"))
        self.assertTrue(focus.lights_line("Tenant1", "t1c2"))
        self.assertFalse(focus.lights_line("Tenant1", "t1c3"))
        self.assertLess(focus.fade(make_ep("conn", "t1c3")), 1.0)
        lit = []
        self.widget._draw_lit_line = lambda *args: lit.append(args)
        self.render()
        self.assertEqual(len(lit), 1)

    def test_connection_selection_preserves_the_centered_view_and_node_positions(self):
        self.widget.reveal_tenant("Tenant1", exclusive=True)
        self.widget.pan_by(15, -20)
        camera = self.camera()
        layout = self.widget._layout_nodes()
        stored = self.widget.get_view_state()
        moves = []
        self.widget.node_positions_changed.connect(moves.append)
        self.widget.conn_positions_changed.connect(moves.append)
        self.widget.select_connection("t1c2")
        self.assertEqual(self.camera(), camera)
        self.assertEqual(self.widget._layout_nodes(), layout)
        self.assertEqual(self.widget.get_view_state(), stored)
        self.assertEqual(moves, [])

    def test_selecting_another_connection_moves_the_highlight(self):
        self.widget.select_connection("t1c2")
        self.widget.select_connection("t1c3")
        self.assertEqual(self.widget._selected_conn, "t1c3")
        self.assertEqual(self.widget._hover_amount(make_ep("conn", "t1c2")), 0.0)
        self.assertEqual(self.widget._hover_amount(make_ep("conn", "t1c3")), 1.0)

    def test_real_hover_takes_priority_and_leaving_restores_the_selected_highlight(self):
        self.widget.select_connection("t1c2")
        self.widget._hovered_conn = "t1c3"
        self.assertEqual(self.widget._hover_targets(), {make_ep("conn", "t1c3")})
        self.widget.leaveEvent(QEvent(QEvent.Type.Leave))
        self.assertEqual(self.widget._hover_targets(), {make_ep("conn", "t1c2")})

    def test_selected_connection_eases_in_with_the_normal_hover_animation(self):
        self.widget.set_graph_settings({"animate": True})
        self.widget._anim_timer.stop()
        self.widget.select_connection("t1c2")
        ep = make_ep("conn", "t1c2")
        self.widget._advance_hover(HOVER_IN_S / 2)
        self.assertGreater(self.widget._hover_amount(ep), 0.0)
        self.assertLess(self.widget._hover_amount(ep), 1.0)
        self.widget._time += 0.7
        self.widget._advance_hover(0.7)
        self.assertEqual(self.widget._hover_amount(ep), 1.0)
        self.assertEqual(self.widget._card_ep, ep)
        self.assertEqual(self.widget._card_level, 1.0)

    def test_tenant_navigation_and_expand_all_clear_the_connection_highlight(self):
        self.widget.select_connection("t1c2")
        self.widget.reveal_tenant("Tenant2", exclusive=True)
        self.assertEqual(self.widget._hover_targets(), set())
        self.assertIsNone(self.widget._selected_conn)
        for close in (lambda: self.widget.set_tenant_expanded("Tenant1", False),
                      self.widget.expand_all_tenants):
            self.widget.select_connection("t1c2")
            close()
            self.assertEqual(self.widget._hover_targets(), set())

    def test_refresh_keeps_a_valid_highlight_and_deletion_clears_it(self):
        self.widget.select_connection("t1c2")
        self.widget.set_data(list(self.connections), {})
        self.assertEqual(self.widget._hover_targets(), {make_ep("conn", "t1c2")})
        self.widget.set_data([c for c in self.connections if c["id"] != "t1c2"], {})
        self.assertEqual(self.widget._hover_targets(), set())
        self.assertIsNone(self.widget._selected_conn)


if __name__ == "__main__":
    unittest.main()
