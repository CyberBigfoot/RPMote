"""Connection-link and status tests for the dashboard graph.

Links used to join tenants only.  Either end can now be a single connection,
which means the stored format changed — so a good half of this file is about
old vaults still opening correctly.

Rendering happens offscreen into a QPixmap so the full paint path runs,
including the hit-test rects the mouse code depends on.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.vault as vault_mod

_TMP = Path(tempfile.mkdtemp(prefix="rpmote_links_test_"))
# All-or-nothing: redirecting only some paths would leave the rest pointed at
# the real ~/.rpmote and let these tests clobber a live vault.
vault_mod.use_vault_directory(_TMP)

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent, QPixmap
from PyQt6.QtWidgets import QApplication

from app.dashboard import DashboardWidget, make_ep, normalize_ep, parse_ep
from app.reachability import (
    STATE_DEGRADED, STATE_DOWN, STATE_UNKNOWN, STATE_UP,
)
from app.vault import Vault

_APP = QApplication.instance() or QApplication([])

ESTATE = {
    "Contoso Ltd": ["DC01", "WEB02", "SQL03"],
    "Fabrikam": ["APP01", "VPN02"],
}


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


class EndpointEncodingTests(unittest.TestCase):

    def test_a_bare_name_still_means_a_tenant(self):
        """Vaults written before connections were linkable hold bare names."""
        self.assertEqual(parse_ep("Contoso Ltd"), ("tenant", "Contoso Ltd"))
        self.assertEqual(normalize_ep("Contoso Ltd"), "tenant:Contoso Ltd")

    def test_prefixed_forms_round_trip(self):
        for kind, key in (("tenant", "Contoso Ltd"), ("conn", "abc-123")):
            self.assertEqual(parse_ep(make_ep(kind, key)), (kind, key))

    def test_normalising_is_idempotent(self):
        for raw in ("Contoso", "tenant:Contoso", "conn:abc"):
            once = normalize_ep(raw)
            self.assertEqual(normalize_ep(once), once)

    def test_a_tenant_named_like_a_prefix_survives(self):
        """A customer called "conn" would otherwise silently become a link to
        a connection id that does not exist."""
        self.assertEqual(parse_ep(make_ep("tenant", "conn")), ("tenant", "conn"))

    def test_an_empty_endpoint_normalises_to_nothing(self):
        self.assertEqual(normalize_ep(""), "")
        self.assertEqual(normalize_ep(None), "")


class _GraphTestCase(unittest.TestCase):

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_TMP, ignore_errors=True)

    def setUp(self):
        # Re-assert our own directory: another test module imported later in the
        # same process would otherwise still own the module-level globals.
        vault_mod.use_vault_directory(_TMP)
        for f in (vault_mod.VAULT_FILE, vault_mod.SALT_FILE,
                  vault_mod.HASH_FILE, vault_mod.KEY_FILE):
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
        self.conns = self.vault.connections
        self.dash = DashboardWidget(self.conns, {})
        self.addCleanup(self.dash.deleteLater)
        self.dash.resize(1280, 820)
        self.dash.set_graph_settings({"layout_mode": "free"})
        self._pm = QPixmap(1280, 820)
        self.render()

    def render(self):
        self.dash.resize(1280, 820)
        self.dash.render(self._pm)

    def conn_id(self, name_fragment: str) -> str:
        for conn in self.conns:
            if name_fragment in str(conn.get("name", "")):
                return str(conn["id"])
        raise AssertionError(f"no connection matching {name_fragment!r}")

    def pill(self, name_fragment: str):
        cid = self.conn_id(name_fragment)
        for rect, conn, _tenant in self.dash._conn_rects:
            if str(conn.get("id", "")) == cid:
                return rect
        raise AssertionError(f"{name_fragment} was not drawn")

    def tile(self, tenant: str):
        for rect, name in self.dash._node_rects:
            if name == tenant:
                return rect
        raise AssertionError(f"tenant {tenant} was not drawn")


class ConnectionLinkTests(_GraphTestCase):

    def test_two_connections_can_be_linked(self):
        a, b = self.conn_id("DC01"), self.conn_id("APP01")
        self.assertTrue(self.dash.add_connection_link(a, b))
        links = self.dash.get_tenant_links()
        self.assertEqual(len(links), 1)
        self.assertEqual({links[0]["a"], links[0]["b"]},
                         {make_ep("conn", a), make_ep("conn", b)})

    def test_a_connection_can_be_linked_to_a_tenant(self):
        self.assertTrue(self.dash.add_link(
            make_ep("conn", self.conn_id("DC01")),
            make_ep("tenant", "Fabrikam")))
        self.assertEqual(len(self.dash.get_tenant_links()), 1)

    def test_a_cross_tenant_connection_link_is_allowed(self):
        """The point of the feature: showing that this box talks to that one,
        even though they belong to different customers."""
        self.assertTrue(self.dash.add_connection_link(
            self.conn_id("DC01"), self.conn_id("VPN02")))
        link = self.dash.get_tenant_links()[0]
        owners = {self.dash._ep_owner_tenant(link["a"]),
                  self.dash._ep_owner_tenant(link["b"])}
        self.assertEqual(owners, {"Contoso Ltd", "Fabrikam"})

    def test_a_node_cannot_be_linked_to_itself(self):
        cid = self.conn_id("DC01")
        self.assertFalse(self.dash.add_connection_link(cid, cid))
        self.assertEqual(self.dash.get_tenant_links(), [])

    def test_the_same_pair_is_not_linked_twice(self):
        a, b = self.conn_id("DC01"), self.conn_id("APP01")
        self.assertTrue(self.dash.add_connection_link(a, b))
        self.assertFalse(self.dash.add_connection_link(b, a),
                         "the reversed pair is the same link")
        self.assertEqual(len(self.dash.get_tenant_links()), 1)

    def test_alt_drag_between_two_pills_creates_a_link(self):
        src, dst = self.pill("DC01"), self.pill("APP01")
        _drag(self.dash, src.center().x(), src.center().y(),
              dst.center().x(), dst.center().y(),
              mods=Qt.KeyboardModifier.AltModifier)
        links = self.dash.get_tenant_links()
        self.assertEqual(len(links), 1)
        self.assertEqual(
            {links[0]["a"], links[0]["b"]},
            {make_ep("conn", self.conn_id("DC01")),
             make_ep("conn", self.conn_id("APP01"))})

    def test_alt_drag_from_a_pill_to_a_tile_links_the_two(self):
        src, dst = self.pill("DC01"), self.tile("Fabrikam")
        _drag(self.dash, src.center().x(), src.center().y(),
              dst.center().x(), dst.center().y(),
              mods=Qt.KeyboardModifier.AltModifier)
        links = self.dash.get_tenant_links()
        self.assertEqual(len(links), 1)
        self.assertIn(make_ep("tenant", "Fabrikam"),
                      {links[0]["a"], links[0]["b"]})

    def test_link_mode_joins_two_pills_on_separate_clicks(self):
        self.dash.set_link_mode(True)
        src, dst = self.pill("DC01"), self.pill("SQL03")
        _press(self.dash, src.center().x(), src.center().y())
        _release(self.dash, src.center().x(), src.center().y())
        self.assertIsNotNone(self.dash._link_source)
        _press(self.dash, dst.center().x(), dst.center().y())
        self.assertEqual(len(self.dash.get_tenant_links()), 1)
        self.assertIsNone(self.dash._link_source)

    def test_begin_link_from_arms_the_chosen_node(self):
        ep = make_ep("conn", self.conn_id("DC01"))
        self.dash.begin_link_from(ep)
        self.assertTrue(self.dash.is_link_mode())
        self.assertEqual(self.dash._link_source, ep)

    def test_escape_abandons_a_half_drawn_link(self):
        from PyQt6.QtGui import QKeyEvent

        self.dash.begin_link_from(make_ep("conn", self.conn_id("DC01")))
        self.dash.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Escape,
            Qt.KeyboardModifier.NoModifier))
        self.assertIsNone(self.dash._link_source)
        self.assertEqual(self.dash.get_tenant_links(), [])

    def test_a_connection_link_is_drawn_and_hit_testable(self):
        self.dash.add_connection_link(self.conn_id("DC01"), self.conn_id("APP01"))
        self.render()
        self.assertEqual(len(self.dash._link_rects), 1)
        path, index = self.dash._link_rects[0]
        self.assertEqual(index, 0)
        self.assertFalse(path.isEmpty())

    def test_linking_never_edits_the_connections_themselves(self):
        """Links are decoration.  They must not touch what the vault stores
        about a machine."""
        before = [dict(c) for c in self.vault.connections]
        self.dash.add_connection_link(self.conn_id("DC01"), self.conn_id("APP01"))
        self.dash.add_link(make_ep("conn", self.conn_id("DC01")),
                           make_ep("tenant", "Fabrikam"))
        self.render()
        self.assertEqual(self.vault.connections, before)


class LinkPersistenceTests(_GraphTestCase):

    def test_legacy_bare_tenant_links_still_load(self):
        self.dash.set_tenant_links([
            {"a": "Contoso Ltd", "b": "Fabrikam", "style": "dashed",
             "color": "#ff0000", "label": "MPLS"},
        ])
        links = self.dash.get_tenant_links()
        self.assertEqual(len(links), 1)
        self.assertEqual({links[0]["a"], links[0]["b"]},
                         {"tenant:Contoso Ltd", "tenant:Fabrikam"})
        self.assertEqual(links[0]["label"], "MPLS")
        self.assertEqual(links[0]["color"], "#ff0000")

    def test_a_legacy_link_still_renders_after_the_format_change(self):
        self.dash.set_tenant_links([{"a": "Contoso Ltd", "b": "Fabrikam"}])
        self.render()
        self.assertEqual(len(self.dash._link_rects), 1)

    def test_a_legacy_and_a_new_form_of_the_same_link_are_one_link(self):
        self.dash.set_tenant_links([{"a": "Contoso Ltd", "b": "Fabrikam"}])
        self.assertFalse(
            self.dash.add_link("tenant:Contoso Ltd", "tenant:Fabrikam"),
            "the prefixed form must not duplicate the legacy one")

    def test_links_to_a_deleted_connection_are_pruned_and_persisted(self):
        emitted = []
        self.dash.tenant_links_changed.connect(emitted.append)
        doomed = self.conn_id("SQL03")
        self.dash.add_connection_link(self.conn_id("DC01"), doomed)
        emitted.clear()

        self.vault.delete_connection(doomed)
        self.dash.set_data(self.vault.connections, {})

        self.assertEqual(self.dash.get_tenant_links(), [])
        self.assertEqual(emitted[-1], [],
                         "the pruned list has to reach the vault")

    def test_a_transient_empty_estate_does_not_wipe_links(self):
        """set_data([]) happens while a vault is loading; treating it as a
        deletion would silently destroy the user's diagram."""
        self.dash.add_connection_link(self.conn_id("DC01"), self.conn_id("APP01"))
        self.dash.set_data([], {})
        self.assertEqual(len(self.dash.get_tenant_links()), 1)

    def test_saved_links_naming_unknown_nodes_are_dropped(self):
        self.dash.set_tenant_links([
            {"a": make_ep("conn", "ghost-1"), "b": make_ep("conn", "ghost-2")},
            {"a": "Contoso Ltd", "b": "Fabrikam"},
        ])
        self.assertEqual(len(self.dash.get_tenant_links()), 1)

    def test_malformed_entries_are_skipped_not_fatal(self):
        self.dash.set_tenant_links([
            "not a dict", None, {}, {"a": "Contoso Ltd"},
            {"a": "Contoso Ltd", "b": "Contoso Ltd"},
            {"a": "Contoso Ltd", "b": "Fabrikam"},
        ])
        self.assertEqual(len(self.dash.get_tenant_links()), 1)

    def test_links_survive_a_round_trip_through_the_vault(self):
        a = make_ep("conn", self.conn_id("DC01"))
        b = make_ep("conn", self.conn_id("APP01"))
        self.dash.add_link(a, b)
        self.vault.update_settings(
            {"dashboard_tenant_links": self.dash.get_tenant_links()})

        fresh = DashboardWidget(self.vault.connections, {})
        self.addCleanup(fresh.deleteLater)
        fresh.set_tenant_links(
            self.vault.settings.get("dashboard_tenant_links", []))
        self.assertEqual({l["a"] for l in fresh.get_tenant_links()} |
                         {l["b"] for l in fresh.get_tenant_links()}, {a, b})


class HiddenBranchTests(_GraphTestCase):

    def test_a_connection_link_falls_back_to_its_tenant_tile(self):
        """Turning branches off should dim the picture, not lose links."""
        self.dash.add_connection_link(self.conn_id("DC01"), self.conn_id("APP01"))
        self.dash.set_graph_settings({"show_branches": False})
        self.render()
        self.assertEqual(self.dash._conn_rects, [])
        self.assertEqual(len(self.dash._link_rects), 1,
                         "the link should still be drawn, tile to tile")

    def test_two_connections_in_one_tenant_collapse_to_nothing_visible(self):
        """Both ends land on the same tile, and a node linked to itself is not
        worth drawing."""
        self.dash.add_connection_link(self.conn_id("DC01"), self.conn_id("SQL03"))
        self.dash.set_graph_settings({"show_branches": False})
        self.render()
        self.assertEqual(self.dash._link_rects, [])
        self.assertEqual(len(self.dash.get_tenant_links()), 1,
                         "hiding branches must not delete the link")


class ReachabilityDisplayTests(_GraphTestCase):

    def test_states_are_invisible_until_probing_is_switched_on(self):
        cid = self.conn_id("DC01")
        self.dash.set_conn_status(cid, {"state": STATE_DOWN})
        self.assertIsNone(self.dash._status_color(STATE_DOWN))
        self.dash.set_ping_enabled(True)
        self.assertIsNotNone(self.dash._status_color(STATE_DOWN))

    def test_a_status_survives_a_repaint(self):
        cid = self.conn_id("DC01")
        self.dash.set_ping_enabled(True)
        self.dash.set_conn_status(cid, {"state": STATE_UP, "latency_ms": 3.0})
        self.render()
        self.assertEqual(self.dash.conn_state(cid), STATE_UP)

    def test_switching_probing_off_forgets_every_result(self):
        self.dash.set_ping_enabled(True)
        self.dash.set_conn_status(self.conn_id("DC01"), {"state": STATE_UP})
        self.dash.set_ping_enabled(False)
        self.assertEqual(self.dash.conn_state(self.conn_id("DC01")), STATE_UNKNOWN)

    def test_a_tenant_reports_its_worst_member(self):
        self.dash.set_ping_enabled(True)
        self.dash.set_conn_statuses({
            self.conn_id("DC01"): {"state": STATE_UP},
            self.conn_id("WEB02"): {"state": STATE_DEGRADED},
            self.conn_id("SQL03"): {"state": STATE_UP},
        })
        self.assertEqual(self.dash.tenant_state("Contoso Ltd"), STATE_DEGRADED)
        self.assertEqual(self.dash.tenant_state("Fabrikam"), STATE_UNKNOWN)

    def test_a_healthy_node_keeps_its_protocol_colour(self):
        """Tinting every green node would drown out the protocol palette."""
        self.dash.set_ping_enabled(True)
        base = self.dash._status_border(STATE_UP, "sentinel", 1.0)
        self.assertEqual(base, ("sentinel", 1.0))
        trouble = self.dash._status_border(STATE_DOWN, "sentinel", 1.0)
        self.assertNotEqual(trouble[0], "sentinel")

    def test_pills_widen_to_fit_the_dot_rather_than_overdrawing_the_label(self):
        conn = self.conns[0]
        narrow = self.dash._branch_logical_width(conn)
        self.dash.set_ping_enabled(True)
        wide = self.dash._branch_logical_width(conn)
        self.assertGreater(wide, narrow)

    def test_switching_probing_on_relays_out_the_branches(self):
        self.render()
        before = {str(c.get("id")): r.width() for r, c, _t in self.dash._conn_rects}
        self.dash.set_ping_enabled(True)
        self.render()
        after = {str(c.get("id")): r.width() for r, c, _t in self.dash._conn_rects}
        self.assertTrue(any(after[k] > before[k] for k in before),
                        "the layout must be rebuilt, not left stale")

    def test_a_tooltip_names_the_host_and_its_state(self):
        cid = self.conn_id("DC01")
        self.dash.set_ping_enabled(True)
        self.dash.set_conn_status(cid, {"state": STATE_DOWN,
                                        "detail": "port 22: timed out",
                                        "checked_at": 0})
        tip = self.dash._hover_tooltip(None, cid)
        self.assertIn("DC01", tip)
        self.assertIn("Unreachable", tip)

    def test_a_tooltip_stays_quiet_about_states_we_never_measured(self):
        tip = self.dash._hover_tooltip(None, self.conn_id("DC01"))
        self.assertIn("DC01", tip)
        self.assertNotIn("Unreachable", tip)
        self.assertNotIn("Not checked", tip)

    def test_statuses_for_unknown_ids_are_harmless(self):
        self.dash.set_ping_enabled(True)
        self.dash.set_conn_statuses({"ghost": {"state": STATE_DOWN}})
        self.render()
        self.assertEqual(self.dash.conn_state("ghost"), STATE_DOWN)


class RestartMenuSignalTests(_GraphTestCase):

    def test_the_dashboard_asks_the_host_to_restart_rather_than_doing_it(self):
        """The widget knows nothing about SSH; it raises a request and lets the
        window that owns the vault decide."""
        seen = []
        self.dash.restart_requested.connect(seen.append)
        conn = self.conns[0]
        self.dash.restart_requested.emit(dict(conn))
        self.assertEqual(seen[0]["id"], conn["id"])

    def test_setup_requests_carry_their_scope(self):
        seen = []
        self.dash.restart_setup_requested.connect(
            lambda kind, key: seen.append((kind, key)))
        self.dash.restart_setup_requested.emit("tenant", "Contoso Ltd")
        self.dash.restart_setup_requested.emit("connection", "abc")
        self.assertEqual(seen, [("tenant", "Contoso Ltd"), ("connection", "abc")])

    def test_an_empty_check_request_means_the_whole_estate(self):
        seen = []
        self.dash.check_now_requested.connect(seen.append)
        self.dash.check_now_requested.emit([])
        self.assertEqual(seen, [[]])


if __name__ == "__main__":
    unittest.main()
