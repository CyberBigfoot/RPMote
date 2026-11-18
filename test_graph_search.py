"""Spotlight search tests: ranking, keyboard driving, camera and rendering.

Same shape as test_dashboard_graph.py — a real MainWindow against an isolated
temp vault, rendered offscreen into a QPixmap so the full paint path (including
the overlay and its clickable result rows) actually runs.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.vault as vault_mod

_TMP = Path(tempfile.mkdtemp(prefix="rpmote_search_test_"))
# All-or-nothing: redirecting only some paths would leave the rest pointed at
# the real ~/.rpmote and let these tests clobber a live vault.
vault_mod.use_vault_directory(_TMP)

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QKeyEvent, QMouseEvent, QPixmap
from PyQt6.QtWidgets import QApplication

from app.dashboard import DashboardWidget, fuzzy_match, make_ep
from app.main_window import MainWindow
from app.vault import Vault

_APP = QApplication.instance() or QApplication([])

ESTATE = {
    "Contoso Ltd": ["DC01", "WEB02", "SQL03", "FS04"],
    "Fabrikam": ["APP01", "VPN02"],
    "Northwind Traders": ["EX01", "DC02", "RDS03", "NAS04", "SW05"],
}


def _key(widget, key, text="", mods=Qt.KeyboardModifier.NoModifier):
    widget.keyPressEvent(QKeyEvent(
        QEvent.Type.KeyPress, key, mods, text))


def _type(widget, text):
    for ch in text:
        _key(widget, Qt.Key.Key_unknown, ch)


def _click(widget, x, y):
    pos = QPointF(x, y)
    widget.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, pos, pos, Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))


class FuzzyMatchTests(unittest.TestCase):
    """The scorer is plain Python — worth pinning down on its own."""

    def test_a_non_match_scores_none(self):
        self.assertIsNone(fuzzy_match("zzz", "DC01"))
        self.assertIsNone(fuzzy_match("dc", ""))
        self.assertIsNone(fuzzy_match("", "DC01"))

    def test_matching_ignores_case(self):
        self.assertIsNotNone(fuzzy_match("DC01", "dc01"))
        self.assertIsNotNone(fuzzy_match("dc01", "DC01"))

    def test_exact_beats_prefix_beats_substring_beats_scattered(self):
        exact = fuzzy_match("dc01", "dc01")
        prefix = fuzzy_match("dc01", "dc01-primary")
        substr = fuzzy_match("dc01", "old-dc01")
        loose = fuzzy_match("dc01", "d-c-0-x-1")
        self.assertGreater(exact, prefix)
        self.assertGreater(prefix, substr)
        self.assertGreater(substr, loose)

    def test_a_word_boundary_outranks_a_hit_buried_mid_word(self):
        boundary = fuzzy_match("dc", "fs-dc-02")
        buried = fuzzy_match("dc", "oldcache")
        self.assertGreater(boundary, buried)

    def test_a_shorter_host_wins_when_both_start_with_the_query(self):
        self.assertGreater(fuzzy_match("dc", "dc01"),
                           fuzzy_match("dc", "dc01-secondary-replica"))

    def test_subsequence_matching_finds_scattered_characters(self):
        self.assertIsNotNone(fuzzy_match("nwex", "Northwind-EX01"))


class GraphSearchTests(unittest.TestCase):

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

    def labels(self):
        return [h["label"] for h in self.dash._search_hits]

    # ── Opening and closing ────────────────────────────────────────

    def test_ctrl_f_opens_the_bar_and_escape_closes_it(self):
        self.assertFalse(self.dash.is_search_active())
        _key(self.dash, Qt.Key.Key_F, "\x06", Qt.KeyboardModifier.ControlModifier)
        self.assertTrue(self.dash.is_search_active())
        _key(self.dash, Qt.Key.Key_Escape)
        self.assertFalse(self.dash.is_search_active())

    def test_the_graph_menu_entry_opens_the_bar(self):
        self.win._dashboard_find()
        self.assertTrue(self.dash.is_search_active())

    def test_closing_leaves_the_selection_in_place(self):
        self.dash.open_search()
        self.dash.set_search_query("Fabrikam")
        _key(self.dash, Qt.Key.Key_Escape)
        self.assertEqual(self.dash._selected_node, "Fabrikam")

    def test_reopening_keeps_the_query_ready_to_be_replaced(self):
        self.dash.open_search()
        self.dash.set_search_query("dc01")
        self.dash.close_search()
        self.dash.open_search()
        self.assertEqual(self.dash.search_query(), "dc01")
        _type(self.dash, "vpn")
        self.assertEqual(self.dash.search_query(), "vpn")

    # ── What matches ───────────────────────────────────────────────

    def test_an_empty_query_matches_nothing(self):
        self.dash.open_search()
        self.assertEqual(self.dash._search_hits, [])
        self.assertEqual(self.dash._search_index, -1)

    def test_typing_a_hostname_finds_that_connection(self):
        self.dash.open_search()
        _type(self.dash, "VPN02")
        self.assertEqual(self.dash._current_search_hit()["label"],
                         "Fabrikam-VPN02")

    def test_a_tenant_name_ranks_the_tenant_above_its_own_machines(self):
        self.dash.open_search()
        self.dash.set_search_query("Fabrikam")
        top = self.dash._current_search_hit()
        self.assertEqual(top["kind"], "tenant")
        self.assertEqual(top["label"], "Fabrikam")

    def test_search_covers_host_protocol_and_username_too(self):
        self.dash.open_search()
        self.dash.set_search_query("10.10.1.6")
        self.assertTrue(self.dash._search_hits)
        self.dash.set_search_query("rdp")
        self.assertTrue(self.dash._search_hits)
        self.dash.set_search_query("admin")
        self.assertTrue(self.dash._search_hits)

    def test_a_query_matching_nothing_leaves_no_hits_and_still_renders(self):
        self.dash.open_search()
        self.dash.set_search_query("qqqqqq")
        self.assertEqual(self.dash._search_hits, [])
        self.assertIsNone(self.dash._current_search_hit())
        self.render()   # must not raise

    def test_results_are_stable_across_repeated_runs(self):
        self.dash.open_search()
        self.dash.set_search_query("dc")
        first = self.labels()
        self.dash.set_search_query("")
        self.dash.set_search_query("dc")
        self.assertEqual(first, self.labels())

    # ── Stepping ───────────────────────────────────────────────────

    def test_down_and_up_step_through_the_results(self):
        self.dash.open_search()
        self.dash.set_search_query("dc")
        self.assertGreater(len(self.dash._search_hits), 1)
        self.assertEqual(self.dash._search_index, 0)
        _key(self.dash, Qt.Key.Key_Down)
        self.assertEqual(self.dash._search_index, 1)
        _key(self.dash, Qt.Key.Key_Up)
        self.assertEqual(self.dash._search_index, 0)

    def test_stepping_wraps_at_both_ends(self):
        self.dash.open_search()
        self.dash.set_search_query("dc")
        last = len(self.dash._search_hits) - 1
        _key(self.dash, Qt.Key.Key_Up)
        self.assertEqual(self.dash._search_index, last)
        _key(self.dash, Qt.Key.Key_Down)
        self.assertEqual(self.dash._search_index, 0)

    def test_backspace_edits_the_query_and_rebuilds_the_results(self):
        self.dash.open_search()
        _type(self.dash, "vpn0")
        narrow = len(self.dash._search_hits)
        _key(self.dash, Qt.Key.Key_Backspace)
        _key(self.dash, Qt.Key.Key_Backspace)
        self.assertEqual(self.dash.search_query(), "vp")
        self.assertGreaterEqual(len(self.dash._search_hits), narrow)

    # ── Camera ─────────────────────────────────────────────────────

    def test_focusing_a_result_brings_it_to_the_middle_of_the_canvas(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.render()
        self.dash.open_search()
        self.dash.set_search_query("SW05")
        self.render()
        geom = self.dash._ep_geom[self.dash._current_search_hit()["ep"]]
        self.assertAlmostEqual(geom[0], self.dash.width() / 2, delta=6)
        self.assertAlmostEqual(geom[1], self.dash.height() / 2 + 30, delta=6)

    def test_a_deliberate_zoom_in_is_not_undone_by_a_search(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.dash._set_zoom(1.8)
        self.dash.open_search()
        self.dash.set_search_query("SW05")
        self.assertAlmostEqual(self.dash._zoom, 1.8, places=3)

    def test_searching_from_a_zoomed_out_view_zooms_back_in_to_read(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.dash._set_zoom(0.3)
        self.dash.open_search()
        self.dash.set_search_query("SW05")
        self.assertGreaterEqual(self.dash._zoom, 0.85)

    def test_a_hit_inside_a_collapsed_hub_tenant_is_revealed_first(self):
        self.assertFalse(self.dash.is_free_mode)
        self.assertEqual(self.dash._visible_tenants, set())
        self.dash.open_search()
        self.dash.set_search_query("SW05")
        self.assertIn("Northwind Traders", self.dash._visible_tenants)
        self.render()
        self.assertIn(make_ep("conn", self.dash._selected_conn),
                      self.dash._ep_geom)

    def test_a_result_is_selected_so_it_stays_marked_after_closing(self):
        self.dash.open_search()
        self.dash.set_search_query("VPN02")
        hit = self.dash._current_search_hit()
        self.assertEqual(self.dash._selected_conn,
                         str(hit["conn"]["id"]))
        self.assertEqual(self.dash._selected_node, "Fabrikam")

    # ── Committing ─────────────────────────────────────────────────

    def test_enter_closes_the_bar_and_announces_the_connection(self):
        seen = []
        self.dash.connection_clicked.connect(seen.append)
        self.dash.open_search()
        self.dash.set_search_query("VPN02")
        _key(self.dash, Qt.Key.Key_Return)
        self.assertFalse(self.dash.is_search_active())
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["name"], "Fabrikam-VPN02")

    def test_ctrl_enter_opens_the_connection_outright(self):
        opened = []
        # MainWindow answers this signal by actually dialling the host, which
        # would block on a TCP timeout here.  We are testing what the canvas
        # emits, so take the real handler off first.
        self.dash.connection_activated.disconnect()
        self.dash.connection_activated.connect(opened.append)
        self.dash.open_search()
        self.dash.set_search_query("VPN02")
        _key(self.dash, Qt.Key.Key_Return, "\r",
             Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["name"], "Fabrikam-VPN02")

    def test_enter_on_a_tenant_announces_the_tenant_instead(self):
        seen = []
        self.dash.tenant_clicked.connect(seen.append)
        self.dash.open_search()
        self.dash.set_search_query("Fabrikam")
        _key(self.dash, Qt.Key.Key_Return)
        self.assertEqual(seen, ["Fabrikam"])

    def test_enter_with_no_results_just_closes(self):
        self.dash.open_search()
        self.dash.set_search_query("qqqqqq")
        _key(self.dash, Qt.Key.Key_Return)
        self.assertFalse(self.dash.is_search_active())

    # ── Mouse ──────────────────────────────────────────────────────

    def test_clicking_a_result_row_goes_to_it(self):
        seen = []
        self.dash.connection_clicked.connect(seen.append)
        self.dash.open_search()
        self.dash.set_search_query("dc")
        self.render()
        rows = self.dash._search_row_rects
        self.assertTrue(rows)
        target = self.dash._search_hits[1]
        rect = rows[1][0]
        _click(self.dash, rect.center().x(), rect.center().y())
        self.assertFalse(self.dash.is_search_active())
        if target["kind"] == "conn":
            self.assertEqual(seen[0]["name"], target["label"])

    def test_clicking_the_canvas_dismisses_the_bar(self):
        self.dash.open_search()
        self.dash.set_search_query("dc")
        self.render()
        _click(self.dash, 40, self.dash.height() - 40)
        self.assertFalse(self.dash.is_search_active())

    # ── Keys the bar must not steal, and ones it must ──────────────

    def test_typing_f_searches_instead_of_fitting_the_view(self):
        self.dash.set_graph_option("layout_mode", "free")
        # Well above the level centring is willing to zoom back up to, so the
        # only thing that could move this number is a fit.
        self.dash._set_zoom(1.4)
        self.dash.open_search()
        _key(self.dash, Qt.Key.Key_F, "f")
        self.assertEqual(self.dash.search_query(), "f")
        self.assertAlmostEqual(self.dash._zoom, 1.4, places=3)

    def test_arrow_keys_step_results_rather_than_panning(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.dash.open_search()
        self.dash.set_search_query("dc")
        idx = self.dash._search_index
        _key(self.dash, Qt.Key.Key_Down)
        self.assertNotEqual(self.dash._search_index, idx)

    def test_arrow_keys_still_pan_when_the_bar_is_closed(self):
        before = self.dash._pan.x()
        _key(self.dash, Qt.Key.Key_Left)
        self.assertNotEqual(self.dash._pan.x(), before)

    def test_f_still_fits_the_view_when_the_bar_is_closed(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.render()
        self.dash._set_zoom(2.0)
        _key(self.dash, Qt.Key.Key_F, "f")
        self.assertLess(self.dash._zoom, 2.0)

    # ── Rendering ──────────────────────────────────────────────────

    def test_the_overlay_renders_in_every_layout_and_theme(self):
        for mode in ("hub", "free"):
            for theme in ("default", "matrix", "dotwave", "aurora"):
                with self.subTest(mode=mode, theme=theme):
                    self.dash.set_graph_option("layout_mode", mode)
                    self.dash.set_theme({"dashboard_theme": theme,
                                         "accent": "#6366f1",
                                         "bg_primary": "#0a0b10",
                                         "text_primary": "#f8fafc",
                                         "text_secondary": "#94a3b8"})
                    self.dash.open_search()
                    self.dash.set_search_query("dc")
                    self.render()
                    self.assertFalse(self.dash._search_rect.isEmpty())

    def test_the_overlay_renders_with_branches_switched_off(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.dash.set_graph_option("show_branches", False)
        self.dash.open_search()
        self.dash.set_search_query("SW05")
        self.render()
        self.assertFalse(self.dash._search_rect.isEmpty())

    def test_the_bar_still_draws_over_a_collapsed_hub(self):
        self.assertEqual(self.dash._visible_tenants, set())
        self.dash.open_search()
        self.render()
        self.assertFalse(self.dash._search_rect.isEmpty())

    def test_the_bar_still_draws_with_an_empty_estate(self):
        dash = DashboardWidget([], {})
        self.addCleanup(dash.deleteLater)
        dash.resize(1280, 820)
        dash.open_search()
        dash.set_search_query("anything")
        dash.render(self._pm)
        self.assertFalse(dash._search_rect.isEmpty())

    def test_only_the_top_rows_are_listed_but_every_hit_stays_reachable(self):
        self.dash.open_search()
        self.dash.set_search_query("0")      # every host name carries a zero
        self.render()
        from app.dashboard import SEARCH_MAX_ROWS
        self.assertGreater(len(self.dash._search_hits), SEARCH_MAX_ROWS)
        self.assertEqual(len(self.dash._search_row_rects), SEARCH_MAX_ROWS)
        for _ in range(len(self.dash._search_hits)):
            _key(self.dash, Qt.Key.Key_Down)
        self.assertEqual(self.dash._search_index, 0)   # walked the whole ring

    def test_every_match_lights_up_not_just_the_focused_one(self):
        self.dash.set_graph_option("layout_mode", "free")
        self.dash.open_search()
        self.dash.set_search_query("dc")
        self.assertGreater(len(self.dash._search_eps), 1)
        self.assertEqual(self.dash._search_eps,
                         {h["ep"] for h in self.dash._search_hits})

    # ── Live data changes ──────────────────────────────────────────

    def test_deleting_a_connection_mid_search_drops_it_from_the_results(self):
        self.dash.open_search()
        self.dash.set_search_query("VPN02")
        self.assertTrue(self.dash._search_hits)
        remaining = [c for c in self.vault.connections
                     if c.get("name") != "Fabrikam-VPN02"]
        self.dash.set_data(remaining,
                           self.vault.settings.get("tenant_colors", {}))
        self.assertEqual(
            [h for h in self.dash._search_hits if h["kind"] == "conn"], [])
        self.render()


if __name__ == "__main__":
    unittest.main()
