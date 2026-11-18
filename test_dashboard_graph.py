"""Dashboard graph tests: layout modes, branch nodes, panning and tenant links.

These build the real MainWindow against an isolated temp vault (the module-level
vault paths are monkeypatched in setUpClass) so they never touch ~/.rpmote.
Rendering happens offscreen into a QPixmap, which exercises the full paint path
including hit-test rect collection.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.vault as vault_mod

_TMP = Path(tempfile.mkdtemp(prefix="rpmote_graph_test_"))
# All-or-nothing: redirecting only some paths would leave the rest pointed at
# the real ~/.rpmote and let these tests clobber a live vault.
vault_mod.use_vault_directory(_TMP)

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QPixmap
from PyQt6.QtWidgets import QApplication

from app.dashboard import DEFAULT_GRAPH_SETTINGS, DashboardWidget, make_ep
from app.graph_settings import GraphSettingsDialog
from app.main_window import MainWindow
from app.vault import Vault

_APP = QApplication.instance() or QApplication([])

ESTATE = {
    "Contoso Ltd": ["DC01", "WEB02", "SQL03", "FS04"],
    "Fabrikam": ["APP01", "VPN02"],
    "Northwind Traders": ["EX01", "DC02", "RDS03", "NAS04", "SW05"],
}
TOTAL_CONNS = sum(len(v) for v in ESTATE.values())


def _press(widget, x, y, button=Qt.MouseButton.LeftButton,
           mods=Qt.KeyboardModifier.NoModifier):
    widget.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y), QPointF(x, y),
        button, button, mods))


def _move(widget, x, y, button=Qt.MouseButton.LeftButton,
          mods=Qt.KeyboardModifier.NoModifier):
    widget.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y), QPointF(x, y),
        Qt.MouseButton.NoButton, button, mods))


def _release(widget, x, y, button=Qt.MouseButton.LeftButton,
             mods=Qt.KeyboardModifier.NoModifier):
    widget.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(x, y), QPointF(x, y),
        button, button, mods))


def _drag(widget, x1, y1, x2, y2, button=Qt.MouseButton.LeftButton,
          mods=Qt.KeyboardModifier.NoModifier):
    _press(widget, x1, y1, button, mods)
    for t in (0.34, 0.67, 1.0):
        _move(widget, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, button, mods)
    _release(widget, x2, y2, button, mods)


class DashboardGraphTests(unittest.TestCase):

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    def setUp(self):
        # Re-assert our own directory: another test module imported later in the
        # same process would otherwise still own the module-level globals.
        vault_mod.use_vault_directory(_TMP)
        for f in (vault_mod.VAULT_FILE, vault_mod.SALT_FILE):
            if f.exists():
                f.unlink()
        self.vault = Vault()
        self.vault.setup("test-password-123")
        for tenant, hosts in ESTATE.items():
            for i, hn in enumerate(hosts):
                self.vault.add_connection({
                    "name": f"{tenant.split()[0]}-{hn}",
                    "host": f"10.10.{i}.{i + 5}",
                    "protocol": ["ssh", "rdp", "sftp"][i % 3],
                    "username": "admin",
                    "tenant": tenant,
                })
        self.win = MainWindow(self.vault)
        self.dash = self.win.dashboard
        self._pm = QPixmap(1280, 820)
        # The first paint lets the tab layout impose its own geometry; burn one
        # render so every later pass sees a stable 1280x820 canvas.
        self.dash.resize(1280, 820)
        self.dash.render(self._pm)
        self.dash.resize(1280, 820)

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()

    def render(self, dash=None):
        widget = dash or self.dash
        widget.resize(1280, 820)
        widget.render(self._pm)

    def solo_dashboard(self, **settings):
        """A parentless dashboard holding the same estate.

        The real dashboard lives inside a QTabWidget whose layout re-imposes
        geometry on the first paint, and the scene centre is h/2 - 50 — so
        anything comparing coordinates across two renders uses this instead.
        """
        dash = DashboardWidget(self.vault.connections,
                               self.vault.settings.get("tenant_colors", {}))
        self.addCleanup(dash.deleteLater)
        dash.resize(1280, 820)
        if settings:
            dash.set_graph_settings(settings)
        return dash

    # ── Layout modes ───────────────────────────────────────────────

    def test_hub_mode_starts_collapsed_with_a_clickable_orb(self):
        self.render()
        self.assertFalse(self.dash.is_free_mode)
        self.assertEqual(self.dash._node_rects, [])
        self.assertFalse(self.dash._center_orb_rect.isEmpty())

    def test_free_mode_shows_every_tenant_and_branch_without_an_orb(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.render()
        self.assertTrue(self.dash.is_free_mode)
        self.assertTrue(self.dash._show_all_connections)
        self.assertEqual(len(self.dash._node_rects), len(ESTATE))
        self.assertEqual(len(self.dash._conn_rects), TOTAL_CONNS)
        self.assertTrue(self.dash._center_orb_rect.isEmpty(),
                        "free mode must not draw the RPMote orb")

    def test_free_mode_ignores_a_saved_collapsed_state(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.dash.set_view_state({"zoom": 1.0, "show_all_connections": False,
                                  "visible_tenants": []})
        self.assertTrue(self.dash._show_all_connections)

    def test_scatter_arrangement_is_stable_across_repaints(self):
        dash = self.solo_dashboard(layout_mode="free", free_arrangement="scatter")
        self.render(dash)
        first = {t: r.center() for r, t in dash._node_rects}
        dash._layout_cache = None
        self.render(dash)
        second = {t: r.center() for r, t in dash._node_rects}
        self.assertEqual(first.keys(), second.keys())
        for tenant, point in first.items():
            self.assertAlmostEqual(point.x(), second[tenant].x(), places=3)
            self.assertAlmostEqual(point.y(), second[tenant].y(), places=3)

    # ── Branch nodes ───────────────────────────────────────────────

    def test_branches_can_be_turned_off_and_back_on(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.dash.set_graph_option("show_branches", False)
        self.render()
        self.assertEqual(self.dash._conn_rects, [])
        self.dash.set_graph_option("show_branches", True)
        self.render()
        self.assertEqual(len(self.dash._conn_rects), TOTAL_CONNS)

    def test_branch_nodes_never_overlap_their_tenant_tile(self):
        for detail in ("full", "compact"):
            with self.subTest(tile_detail=detail):
                self.dash.set_graph_settings(
                    {"layout_mode": "free", "tile_detail": detail})
                self.render()
                tiles = {t: r for r, t in self.dash._node_rects}
                for rect, conn, tenant in self.dash._conn_rects:
                    self.assertFalse(
                        rect.intersects(tiles[tenant]),
                        f"{conn['name']} overlaps the {tenant} tile")

    def test_tenant_tiles_never_overlap_each_other(self):
        for detail in ("full", "compact"):
            with self.subTest(tile_detail=detail):
                self.dash.set_graph_settings(
                    {"layout_mode": "free", "tile_detail": detail})
                self.render()
                tiles = list(self.dash._node_rects)
                for i in range(len(tiles)):
                    for j in range(i + 1, len(tiles)):
                        self.assertFalse(
                            tiles[i][0].intersects(tiles[j][0]),
                            f"{tiles[i][1]} overlaps {tiles[j][1]}")

    def test_branch_nodes_of_a_tenant_never_overlap_each_other(self):
        for detail in ("full", "compact"):
            with self.subTest(tile_detail=detail):
                self.dash.set_graph_settings(
                    {"layout_mode": "free", "tile_detail": detail})
                self.render()
                by_tenant = {}
                for rect, conn, tenant in self.dash._conn_rects:
                    by_tenant.setdefault(tenant, []).append((rect, conn))
                for tenant, items in by_tenant.items():
                    for i in range(len(items)):
                        for j in range(i + 1, len(items)):
                            self.assertFalse(
                                items[i][0].intersects(items[j][0]),
                                f"{items[i][1]['name']} overlaps "
                                f"{items[j][1]['name']}")

    # ── Tenant tile detail ─────────────────────────────

    def test_compact_tiles_are_a_single_short_row(self):
        dash = self.solo_dashboard(layout_mode="free", tile_detail="full")
        self.render(dash)
        full = {t: r.height() for r, t in dash._node_rects}
        dash.set_graph_settings({"tile_detail": "compact"})
        dash.reset_view()
        self.render(dash)
        compact = {t: r.height() for r, t in dash._node_rects}
        self.assertEqual(full.keys(), compact.keys())
        for tenant, height in compact.items():
            self.assertLess(height, full[tenant],
                            f"{tenant} compact tile is not shorter")
            self.assertLess(height, 60, f"{tenant} compact tile is too tall")

    def test_auto_tile_detail_follows_the_branch_setting(self):
        self.dash.set_graph_settings({"tile_detail": "auto", "show_branches": True})
        self.assertTrue(self.dash._tile_compact())
        self.dash.set_graph_option("show_branches", False)
        self.assertFalse(self.dash._tile_compact(),
                         "with no branches the count and icons are worth showing")

    def test_explicit_tile_detail_ignores_the_branch_setting(self):
        self.dash.set_graph_settings({"tile_detail": "compact", "show_branches": False})
        self.assertTrue(self.dash._tile_compact())
        self.dash.set_graph_settings({"tile_detail": "full", "show_branches": True})
        self.assertFalse(self.dash._tile_compact())

    def test_tile_detail_round_trips_through_the_vault(self):
        self.dash.set_graph_settings({"tile_detail": "compact"}, emit=True)
        win2 = MainWindow(self.vault)
        self.addCleanup(win2.deleteLater)
        self.assertEqual(
            win2.dashboard.get_graph_settings()["tile_detail"], "compact")

    def test_a_long_tenant_name_stays_inside_its_compact_tile(self):
        self.vault.add_connection({
            "name": "Very-Long-Host", "host": "10.9.9.9", "protocol": "ssh",
            "tenant": "A Ridiculously Long Managed Service Customer Name Ltd"})
        dash = self.solo_dashboard(layout_mode="free", tile_detail="compact")
        self.render(dash)
        for rect, _tenant in dash._node_rects:
            self.assertLessEqual(rect.width(), 260)

    def test_double_clicking_a_branch_opens_that_connection(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.render()
        opened = []
        self.dash.connection_activated.connect(opened.append)
        rect, conn, _tenant = self.dash._conn_rects[0]
        self.dash.mouseDoubleClickEvent(QMouseEvent(
            QEvent.Type.MouseButtonDblClick, rect.center(), rect.center(),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["id"], conn["id"])

    # ── Panning ────────────────────────────────────────────────────

    def test_right_drag_pans_without_opening_a_context_menu(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.render()
        start = QPointF(self.dash._pan)
        _drag(self.dash, 640, 410, 480, 520, button=Qt.MouseButton.RightButton)
        self.assertAlmostEqual(self.dash._pan.x(), start.x() - 160, places=3)
        self.assertAlmostEqual(self.dash._pan.y(), start.y() + 110, places=3)
        self.assertFalse(self.dash._panning)

    def test_middle_drag_also_pans(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.render()
        start = QPointF(self.dash._pan)
        _drag(self.dash, 600, 400, 700, 300, button=Qt.MouseButton.MiddleButton)
        self.assertAlmostEqual(self.dash._pan.x(), start.x() + 100, places=3)
        self.assertAlmostEqual(self.dash._pan.y(), start.y() - 100, places=3)

    def test_panning_moves_the_nodes_on_screen(self):
        dash = self.solo_dashboard(layout_mode="free")
        self.render(dash)
        before = {t: r.center() for r, t in dash._node_rects}
        dash.pan_by(-120, 60)
        self.render(dash)
        after = {t: r.center() for r, t in dash._node_rects}
        for tenant, point in before.items():
            self.assertAlmostEqual(after[tenant].x(), point.x() - 120, places=3)
            self.assertAlmostEqual(after[tenant].y(), point.y() + 60, places=3)

    def test_wheel_zoom_keeps_the_point_under_the_cursor_fixed(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.render()
        cursor = QPointF(900.0, 300.0)
        before = self.dash._to_logical(cursor.x(), cursor.y())
        zoom_before = self.dash._zoom
        self.dash.wheelEvent(_wheel_event(cursor, 120))
        self.assertGreater(self.dash._zoom, zoom_before)
        after = self.dash._to_logical(cursor.x(), cursor.y())
        self.assertAlmostEqual(before[0], after[0], places=3)
        self.assertAlmostEqual(before[1], after[1], places=3)

        self.dash.wheelEvent(_wheel_event(cursor, -120))
        self.assertAlmostEqual(self.dash._zoom, zoom_before, places=6)
        back = self.dash._to_logical(cursor.x(), cursor.y())
        self.assertAlmostEqual(before[0], back[0], places=3)
        self.assertAlmostEqual(before[1], back[1], places=3)

    def test_pan_survives_a_restart(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.dash.pan_by(37, -91)
        self.dash.view_state_changed.emit(self.dash.get_view_state())
        expected = self.dash.get_view_state()
        win2 = MainWindow(self.vault)
        self.addCleanup(win2.deleteLater)
        self.assertAlmostEqual(win2.dashboard._pan.x(), expected["pan_x"], places=3)
        self.assertAlmostEqual(win2.dashboard._pan.y(), expected["pan_y"], places=3)

    def test_hub_mode_reopens_with_the_orb_centred(self):
        # Hub mode reopens collapsed, so a pan saved while tenants were open
        # would only push the lone logo off-centre.
        self.dash.expand_all_tenants()
        self.dash.pan_by(-240, 130)
        self.dash.view_state_changed.emit(self.dash.get_view_state())
        win2 = MainWindow(self.vault)
        self.addCleanup(win2.deleteLater)
        dash2 = win2.dashboard
        self.assertEqual((dash2._pan.x(), dash2._pan.y()), (0.0, 0.0))
        dash2.resize(1280, 820)
        dash2.render(self._pm)
        orb = dash2._center_orb_rect.center()
        cx, cy = dash2._scene_center()
        self.assertAlmostEqual(orb.x(), cx, places=3)
        self.assertAlmostEqual(orb.y(), cy, places=3)

    # ── Visual tenant links ────────────────────────────────────────

    def test_alt_drag_between_tenants_creates_a_link(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.render()
        tiles = {t: r for r, t in self.dash._node_rects}
        a, b = "Contoso Ltd", "Fabrikam"
        _drag(self.dash,
              tiles[a].center().x(), tiles[a].center().y(),
              tiles[b].center().x(), tiles[b].center().y(),
              mods=Qt.KeyboardModifier.AltModifier)
        links = self.dash.get_tenant_links()
        self.assertEqual(len(links), 1)
        # Endpoints are stored prefixed now that a link can also land on a
        # single connection.
        self.assertEqual({links[0]["a"], links[0]["b"]},
                         {make_ep("tenant", a), make_ep("tenant", b)})

    def test_link_mode_links_on_two_separate_clicks(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.render()
        tiles = {t: r for r, t in self.dash._node_rects}
        self.dash.set_link_mode(True)
        a, b = "Contoso Ltd", "Northwind Traders"
        _press(self.dash, tiles[a].center().x(), tiles[a].center().y())
        _release(self.dash, tiles[a].center().x(), tiles[a].center().y())
        self.assertEqual(self.dash.get_tenant_links(), [],
                         "first click only arms the link")
        _press(self.dash, tiles[b].center().x(), tiles[b].center().y())
        self.assertEqual(len(self.dash.get_tenant_links()), 1)

    def test_links_are_visual_only_and_never_touch_connections(self):
        before = [dict(c) for c in self.vault.connections]
        self.dash.add_tenant_link("Contoso Ltd", "Fabrikam")
        self.assertEqual(self.vault.connections, before)

    def test_duplicate_and_self_links_are_rejected(self):
        self.assertTrue(self.dash.add_tenant_link("Contoso Ltd", "Fabrikam"))
        self.assertFalse(self.dash.add_tenant_link("Fabrikam", "Contoso Ltd"))
        self.assertFalse(self.dash.add_tenant_link("Fabrikam", "Fabrikam"))
        self.assertEqual(len(self.dash.get_tenant_links()), 1)

    def test_link_style_colour_and_label_round_trip(self):
        self.dash.add_tenant_link("Contoso Ltd", "Fabrikam")
        self.dash._tenant_links[0].update(
            {"style": "dotted", "color": "#ff0055", "label": "site-to-site"})
        self.dash.tenant_links_changed.emit(self.dash.get_tenant_links())
        stored = self.vault.settings["dashboard_tenant_links"][0]
        self.assertEqual(stored["style"], "dotted")
        self.assertEqual(stored["color"], "#ff0055")
        self.assertEqual(stored["label"], "site-to-site")

        win2 = MainWindow(self.vault)
        self.addCleanup(win2.deleteLater)
        restored = win2.dashboard.get_tenant_links()[0]
        self.assertEqual(restored["style"], "dotted")
        self.assertEqual(restored["label"], "site-to-site")

    def test_selected_link_is_removed_by_the_delete_key(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.dash.add_tenant_link("Contoso Ltd", "Fabrikam")
        self.render()
        self.dash._selected_link = 0
        self.dash.keyPressEvent(_key_event(Qt.Key.Key_Delete))
        self.assertEqual(self.dash.get_tenant_links(), [])

    def test_links_to_a_deleted_tenant_are_pruned_and_persisted(self):
        self.dash.add_tenant_link("Contoso Ltd", "Fabrikam")
        self.dash.add_tenant_link("Contoso Ltd", "Northwind Traders")
        self.vault.delete_connections_by_tenant("Fabrikam")
        self.win._refresh_sidebar()
        self.assertEqual(len(self.dash.get_tenant_links()), 1)
        self.assertEqual(len(self.vault.settings["dashboard_tenant_links"]), 1)

    def test_malformed_saved_links_are_discarded(self):
        self.dash.set_tenant_links([
            {"a": "Contoso Ltd", "b": "Fabrikam"},        # ok
            {"a": "Contoso Ltd", "b": "Contoso Ltd"},     # self link
            {"a": "Contoso Ltd"},                         # missing end
            "not-a-dict",                                 # wrong type
            {"a": "Ghost Corp", "b": "Fabrikam"},         # unknown tenant
        ])
        links = self.dash.get_tenant_links()
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["style"], "dashed")

    def test_a_link_label_slides_off_a_node_it_would_cover(self):
        dash = self.solo_dashboard(layout_mode="free")
        self.render(dash)
        target = {t: r for r, t in dash._node_rects}["Fabrikam"]
        # A path whose midpoint sits dead centre on the Fabrikam tile.
        path = dash._line_path(target.center().x() - 400, target.center().y(),
                               target.center().x() + 400, target.center().y(),
                               wobble=False)
        box = dash._place_link_label(path, 60.0, 20.0)
        self.assertFalse(box.intersects(target),
                         "label must not bury the tenant name it crosses")
        for rect, tenant in dash._node_rects:
            self.assertFalse(box.intersects(rect), f"label lands on {tenant}")

    def test_a_link_label_stays_at_the_midpoint_when_nothing_is_in_the_way(self):
        dash = self.solo_dashboard(layout_mode="free")
        self.render(dash)
        bottom = max(r.bottom() for r, _t in dash._node_rects) + 200
        path = dash._line_path(200.0, bottom, 900.0, bottom, wobble=False)
        box = dash._place_link_label(path, 60.0, 20.0)
        mid = path.pointAtPercent(0.5)
        self.assertAlmostEqual(box.center().x(), mid.x(), places=3)
        self.assertAlmostEqual(box.center().y(), mid.y(), places=3)

    def test_labelled_links_render_without_leaking_state(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.dash.add_tenant_link("Contoso Ltd", "Northwind Traders")
        self.dash._tenant_links[0]["label"] = "MPLS"
        self.render()
        self.assertEqual(self.dash._pending_link_labels, [],
                         "the label queue must be drained every paint")
        self.render()
        self.assertEqual(self.dash._pending_link_labels, [])

    # ── Settings ───────────────────────────────────────────────────

    def test_graph_settings_round_trip_through_the_vault(self):
        self.dash.set_graph_settings(
            {"layout_mode": "free", "line_style": "elbow",
             "branch_labels": "host", "particles": False}, emit=True)
        win2 = MainWindow(self.vault)
        self.addCleanup(win2.deleteLater)
        gs = win2.dashboard.get_graph_settings()
        self.assertEqual(gs["layout_mode"], "free")
        self.assertEqual(gs["line_style"], "elbow")
        self.assertEqual(gs["branch_labels"], "host")
        self.assertFalse(gs["particles"])

    def test_unknown_settings_keys_are_ignored(self):
        self.dash.set_graph_settings({"layout_mode": "free", "bogus_key": 42})
        self.assertNotIn("bogus_key", self.dash.get_graph_settings())
        self.assertTrue(self.dash.is_free_mode)

    def test_graph_menu_mirrors_the_dashboard(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.assertTrue(self.win._graph_actions[("layout_mode", "free")].isChecked())
        self.assertFalse(self.win._graph_actions[("layout_mode", "hub")].isChecked())
        self.assertFalse(
            self.win._graph_actions[("show_hub_spokes", None)].isEnabled(),
            "hub spokes are meaningless without a hub")
        self.dash.set_graph_option("layout_mode", "hub")
        self.assertTrue(self.win._graph_actions[("layout_mode", "hub")].isChecked())

    def test_settings_dialog_applies_live_and_restores_defaults(self):
        dialog = GraphSettingsDialog(self.dash, parent=self.win)
        self.addCleanup(dialog.deleteLater)
        dialog.cmb_layout.setCurrentIndex(dialog.cmb_layout.findData("free"))
        self.assertTrue(self.dash.is_free_mode)
        dialog.cmb_lines.setCurrentIndex(dialog.cmb_lines.findData("elbow"))
        self.assertEqual(self.dash.get_graph_settings()["line_style"], "elbow")
        dialog.chk_animate.setChecked(False)
        self.assertFalse(dialog.chk_particles.isEnabled())
        dialog._restore_defaults()
        self.assertEqual(self.dash.get_graph_settings(),
                         dict(DEFAULT_GRAPH_SETTINGS))

    def test_every_option_combination_renders(self):
        """Guard against a paint crash in any corner of the settings space."""
        for layout in ("hub", "free"):
            for line in ("curved", "straight", "elbow"):
                for labels in ("name", "name_host", "host", "none"):
                    for branches, detail in (
                            (True, "auto"), (False, "auto"),
                            (True, "full"), (True, "compact"),
                            (False, "compact")):
                        self.dash.set_graph_settings({
                            "layout_mode": layout, "line_style": line,
                            "branch_labels": labels, "show_branches": branches,
                            "tile_detail": detail,
                        })
                        if layout == "hub":
                            self.dash._show_all_connections = True
                            self.dash._visible_tenants = set(ESTATE)
                            self.dash._layout_cache = None
                        self.render()

    # ── Node positions ─────────────────────────────────────────────

    def test_dragging_a_tenant_persists_its_position(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.render()
        rect, tenant = self.dash._node_rects[0]
        _drag(self.dash, rect.center().x(), rect.center().y(),
              rect.center().x() + 90, rect.center().y() - 70)
        self.assertIn(tenant, self.vault.settings["dashboard_node_positions"])

    def test_resetting_positions_clears_both_tenants_and_branches(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.render()
        rect, tenant = self.dash._node_rects[0]
        _drag(self.dash, rect.center().x(), rect.center().y(),
              rect.center().x() + 60, rect.center().y() + 40)
        self.dash._conn_positions["whatever"] = (10.0, 10.0)
        self.dash.reset_all_positions()
        self.assertEqual(self.vault.settings["dashboard_node_positions"], {})
        self.assertEqual(self.vault.settings["dashboard_conn_positions"], {})

    def test_fit_to_view_brings_every_node_on_screen(self):
        for spread in (1.0, 2.0):
            with self.subTest(branch_spread=spread):
                dash = self.solo_dashboard(layout_mode="free",
                                           branch_spread=spread)
                dash.fit_to_view()
                self.render(dash)
                rects = [r for r, _t in dash._node_rects]
                rects += [r for r, _c, _t in dash._conn_rects]
                self.assertTrue(rects)
                for rect in rects:
                    self.assertGreaterEqual(rect.left(), -1)
                    self.assertGreaterEqual(rect.top(), -1)
                    self.assertLessEqual(rect.right(), dash.width() + 1)
                    self.assertLessEqual(rect.bottom(), dash.height() + 1)


def _key_event(key):
    from PyQt6.QtGui import QKeyEvent
    return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


    # ── Relationship lens, from the window's side ──────────────────

    def test_only_one_lens_can_be_ticked_in_the_menu_at_a_time(self):
        """Two ticked lenses would tell the user the graph is showing both."""
        from app.relationships import LENS_NONE, LENS_SUBNET

        actions = self.win._lens_actions
        self.assertEqual([k for k, a in actions.items() if a.isChecked()],
                         [LENS_NONE])

        self.win._set_relation_lens(LENS_SUBNET)
        self.assertEqual([k for k, a in actions.items() if a.isChecked()],
                         [LENS_SUBNET])

        # Cleared from the canvas rather than the menu — the tick must follow.
        self.dash.clear_relation_lens()
        self.assertEqual([k for k, a in actions.items() if a.isChecked()],
                         [LENS_NONE])

    def test_the_restart_routing_reaches_the_graph(self):
        from app.relationships import LENS_RESTART

        server = self.vault.connections[0]
        members = [c for c in self.vault.connections
                   if c["tenant"] == server["tenant"]]
        self.vault.update_settings({"restart_servers": {server["tenant"]: {
            "conn_id": str(server["id"]), "transport": "ssh",
            "command": "reboot {host}", "enabled": True}}})
        self.win._refresh_sidebar()

        view = self.dash.relation_view(LENS_RESTART)
        self.assertEqual(len(view.edges), len(members) - 1)


def _wheel_event(pos, delta):
    from PyQt6.QtCore import QPoint
    from PyQt6.QtGui import QWheelEvent
    return QWheelEvent(
        pos, pos, QPoint(0, 0), QPoint(0, delta),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase, False)


if __name__ == "__main__":
    unittest.main()
