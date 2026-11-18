"""Dashboard themes: every theme has a skin and a background, both survive
a paint, and the table-driven skins still give the original themes their
original colours.
"""

import os
import random
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("RPMOTE_HW_ACCEL", "0")

from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import QApplication

from app import themes
from app.dashboard import DashboardWidget
from app.dashboard_backgrounds import BACKGROUNDS, Circuit, make_background
from app.dashboard_skins import (SKINS, DEFAULT_SKIN, derive_skin, skin_for)

_APP = QApplication.instance() or QApplication([])

#: Backgrounds the widget paints itself rather than through the registry.
BUILT_IN = {"default", "matrix", "dotwave", "aurora"}


def estate(tenants: int, per_tenant: int) -> list:
    return [
        {"id": f"t{t}c{c}", "name": f"SRV{t}-{c}", "host": f"10.{t}.0.{c + 10}",
         "port": 22, "protocol": ["ssh", "rdp", "sftp"][c % 3],
         "tenant": f"Tenant{t}"}
        for t in range(tenants) for c in range(per_tenant)
    ]


def dashboard(conns, colors=None) -> DashboardWidget:
    w = DashboardWidget(conns, colors or {})
    w.resize(1200, 800)
    w.set_graph_settings({"animate": True})
    w.expand_all_tenants()
    return w


def render(w: DashboardWidget) -> QPixmap:
    pm = QPixmap(w.width(), w.height())
    w.render(pm)
    return pm


class RegistryTests(unittest.TestCase):

    def test_every_theme_resolves_to_a_skin_and_a_background(self):
        names = [t["name"] for t in themes.ALL_THEMES]
        self.assertEqual(len(names), len(set(names)), "theme names must be unique")
        for t in themes.ALL_THEMES:
            key = t.get("dashboard_theme", "default")
            with self.subTest(theme=t["name"]):
                self.assertIn(key, SKINS)
                self.assertTrue(key in BUILT_IN or key in BACKGROUNDS,
                                f"{key} has no background painter")

    def test_saved_theme_index_keeps_pointing_at_the_same_theme(self):
        # theme_index is persisted in the vault; the order RPMote 1.0
        # shipped with must stay put or a saved choice silently changes.
        legacy = ["Mote", "Dark", "Nebula", "Catppuccin", "Tokyo Night",
                  "Matrix", "Dot Wave", "Aurora"]
        self.assertEqual([t["name"] for t in themes.ALL_THEMES[:8]], legacy)

    def test_unknown_key_falls_back_to_the_default_skin(self):
        self.assertIs(skin_for("no-such-theme"), DEFAULT_SKIN)
        self.assertIs(skin_for(None), DEFAULT_SKIN)
        self.assertIsNone(make_background("no-such-theme"))
        self.assertIsNone(make_background("matrix"))

    def test_derived_skins_stay_in_range(self):
        skin = derive_skin("probe", accent=(250, 204, 21), deep=(5, 10, 7))
        for name in ("tile_bg", "tile_bg_sel", "pill_bg", "orb_glass", "orb_halo"):
            for stop in getattr(skin, name):
                self.assertEqual(len(stop), 4, name)
                self.assertTrue(all(0 <= v <= 255 for v in stop), (name, stop))
        for name in ("orb_border", "active_glow", "border_active",
                     "border_sel", "border_hover", "border_idle", "count_text"):
            stop = getattr(skin, name)
            self.assertTrue(all(0 <= v <= 255 for v in stop), (name, stop))
        # Glass is the deep colour lifted toward the accent: darker than the
        # accent, lighter than the board.
        top = skin.tile_bg[0]
        self.assertTrue(all(top[i] >= (5, 10, 7)[i] for i in range(3)))
        self.assertTrue(all(top[i] < (250, 204, 21)[i] or top[i] <= 30 for i in range(3)))


class SkinCompatibilityTests(unittest.TestCase):
    """The original themes must draw in exactly the colours they always did."""

    def test_node_colour_per_theme(self):
        w = dashboard(estate(1, 1), {"Tenant0": "#f472b6"})
        w.set_theme({"dashboard_theme": "matrix"})
        self.assertEqual(w._node_base_color("Tenant0"), QColor(92, 214, 143))
        w.set_theme({"dashboard_theme": "dotwave", "accent": "#58a6ff"})
        self.assertEqual(w._node_base_color("Tenant0"), QColor(88, 166, 255))
        w.set_theme({"dashboard_theme": "aurora", "accent": "#06d6ca"})
        self.assertEqual(w._node_base_color("Tenant0"), QColor(6, 214, 202))
        w.set_theme({})
        self.assertEqual(w._node_base_color("Tenant0"), QColor("#f472b6"))

    def test_matrix_is_the_only_monospaced_neon_skin(self):
        mono = [k for k, s in SKINS.items() if s.mono]
        self.assertEqual(mono, ["matrix"])
        self.assertEqual(SKINS["matrix"].pill_accent, (92, 214, 143))
        self.assertEqual(SKINS["matrix"].link_color, (92, 214, 143))
        for k, s in SKINS.items():
            if k != "matrix":
                self.assertIsNone(s.pill_accent, k)
                self.assertIsNone(s.link_color, k)

    def test_default_keeps_tenant_colours_everyone_else_uses_one_accent(self):
        self.assertIsNone(SKINS["default"].accent)
        for k, s in SKINS.items():
            if k != "default":
                self.assertIsNotNone(s.accent, k)


class PaintTests(unittest.TestCase):

    def test_every_theme_paints_and_animates(self):
        w = dashboard(estate(4, 3))
        for t in themes.ALL_THEMES:
            key = t.get("dashboard_theme", "default")
            with self.subTest(theme=t["name"]):
                w.set_theme(t)
                self.assertEqual(w._skin.key, key)
                bg = w._background
                if key in BUILT_IN:
                    self.assertIsNone(bg)
                else:
                    self.assertIsNotNone(bg)
                    self.assertEqual(bg.key, key)
                    for _ in range(30):
                        bg.tick(1 / 30, w.width(), w.height())
                render(w)
                # A second frame reuses whatever the first cached.
                render(w)

    def test_switching_theme_flushes_node_caches(self):
        w = dashboard(estate(2, 2))
        w.set_theme(themes.SYNTHWAVE_THEME)
        render(w)
        self.assertTrue(w._pill_pixmap_cache and w._tile_pixmap_cache)
        w.set_theme(themes.EMBER_THEME)
        self.assertFalse(w._pill_pixmap_cache)
        self.assertFalse(w._tile_pixmap_cache)
        self.assertEqual(w._background.key, "ember")

    def test_background_follows_a_resize(self):
        w = dashboard(estate(1, 1))
        w.set_theme(themes.CIRCUIT_THEME)
        render(w)
        bg = w._background
        self.assertIsInstance(bg, Circuit)
        first_layer = bg._layer
        self.assertEqual(first_layer.size().width(), 1200)
        w.resize(900, 600)
        render(w)
        self.assertEqual((bg._w, bg._h), (900, 600))
        self.assertIsNot(bg._layer, first_layer)
        self.assertEqual(bg._layer.size().width(), 900)

    def test_backgrounds_are_deterministic_for_a_seed(self):
        for key, cls in BACKGROUNDS.items():
            with self.subTest(background=key):
                a, b = cls(random.Random(7)), cls(random.Random(7))
                a.reset(800, 500)
                b.reset(800, 500)
                pa, pb = QPixmap(800, 500), QPixmap(800, 500)
                from PyQt6.QtGui import QPainter
                for bg, pm in ((a, pa), (b, pb)):
                    for _ in range(10):
                        bg.tick(1 / 30, 800, 500)
                    painter = QPainter(pm)
                    bg.paint(painter, 800, 500)
                    painter.end()
                self.assertEqual(pa.toImage(), pb.toImage())

    def test_zero_size_canvas_is_harmless(self):
        for key, cls in BACKGROUNDS.items():
            with self.subTest(background=key):
                bg = cls(random.Random(1))
                bg.tick(0.1, 0, 0)
                from PyQt6.QtGui import QPainter
                pm = QPixmap(10, 10)
                painter = QPainter(pm)
                bg.paint(painter, 0, 0)
                painter.end()


if __name__ == "__main__":
    unittest.main()
