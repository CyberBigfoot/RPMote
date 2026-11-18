"""Relationship tests: what the graph is entitled to claim two records share.

The stakes are that a wrong edge invents infrastructure that is not there — a
"shared credential" between two customers who have nothing in common is an
alarming thing to draw, so the rules for drawing one are pinned down here.
"""

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent, QPixmap
from PyQt6.QtWidgets import QApplication

from app.dashboard import DashboardWidget
from app.relationships import (
    LENS_CREDENTIAL, LENS_MACHINE, LENS_NONE, LENS_RESTART, LENS_SUBNET,
    RelationView, compute_relations, credential_groups, credential_key,
    describe_connection, machine_groups, restart_edges, subnet_groups,
    subnet_of,
)

_APP = QApplication.instance() or QApplication([])


def conn(cid, **fields) -> dict:
    base = {"id": cid, "name": cid.upper(), "tenant": "Contoso",
            "protocol": "ssh", "host": "", "username": "", "key_file": ""}
    base.update(fields)
    return base


def members_of(groups) -> list:
    return [sorted(g.members) for g in groups]


class SubnetTests(unittest.TestCase):

    def test_a_literal_ip_yields_its_slash_24(self):
        self.assertEqual(subnet_of("192.168.10.37"), "192.168.10.0/24")
        self.assertEqual(subnet_of("10.0.0.1", bits=16), "10.0.0.0/16")

    def test_a_hostname_is_never_resolved(self):
        """Three hundred DNS lookups on the UI thread is a frozen window."""
        self.assertIsNone(subnet_of("dc01.contoso.local"))
        self.assertIsNone(subnet_of(""))
        self.assertIsNone(subnet_of("999.1.1.1"))

    def test_machines_on_one_subnet_group_together(self):
        conns = [conn("a", host="10.0.0.10"), conn("b", host="10.0.0.11"),
                 conn("c", host="10.9.0.1"), conn("d", host="server.local")]
        groups = subnet_groups(conns)
        self.assertEqual(members_of(groups), [["a", "b"]])
        self.assertEqual(groups[0].label, "10.0.0.0/24")

    def test_a_subnet_of_one_is_not_a_relationship(self):
        self.assertEqual(subnet_groups([conn("a", host="10.0.0.10")]), [])


class MachineTests(unittest.TestCase):

    def test_records_sharing_a_host_are_one_machine(self):
        conns = [conn("rdp", host="10.0.0.10", protocol="rdp"),
                 conn("ssh", host="10.0.0.10"),
                 conn("other", host="10.0.0.11")]
        self.assertEqual(members_of(machine_groups(conns)), [["rdp", "ssh"]])

    def test_host_matching_ignores_case(self):
        conns = [conn("a", host="DC01.contoso.local"),
                 conn("b", host="dc01.CONTOSO.local")]
        self.assertEqual(members_of(machine_groups(conns)), [["a", "b"]])

    def test_a_mac_merges_records_filed_under_different_names(self):
        """A dual-homed box under two addresses is still one box."""
        conns = [conn("a", host="10.0.0.10", mac="00:11:22:33:44:55"),
                 conn("b", host="192.168.5.10", mac="00-11-22-33-44-55")]
        groups = machine_groups(conns)
        self.assertEqual(members_of(groups), [["a", "b"]])

    def test_a_host_match_and_a_mac_match_fuse_into_one_group(self):
        conns = [conn("a", host="10.0.0.10"),
                 conn("b", host="10.0.0.10", mac="00:11:22:33:44:55"),
                 conn("c", host="10.9.9.9", mac="00:11:22:33:44:55")]
        self.assertEqual(members_of(machine_groups(conns)), [["a", "b", "c"]])

    def test_one_machine_across_two_tenants_is_flagged(self):
        conns = [conn("a", host="10.0.0.10", tenant="Contoso"),
                 conn("b", host="10.0.0.10", tenant="Fabrikam")]
        self.assertTrue(machine_groups(conns)[0].cross_tenant)

    def test_hostless_records_relate_to_nothing(self):
        conns = [conn("az1", protocol="azure_ps"), conn("az2", protocol="azure_ps")]
        self.assertEqual(machine_groups(conns), [])


class CredentialTests(unittest.TestCase):

    def test_the_same_key_by_different_paths_is_one_credential(self):
        conns = [conn("a", username="root", key_file="C:/keys/id_rsa"),
                 conn("b", username="root", key_file="/home/x/.ssh/id_rsa")]
        groups = credential_groups(conns)
        self.assertEqual(members_of(groups), [["a", "b"]])
        self.assertIn("id_rsa", groups[0].label)

    def test_a_different_user_is_a_different_credential(self):
        conns = [conn("a", username="root", key_file="id_rsa"),
                 conn("b", username="svc", key_file="id_rsa")]
        self.assertEqual(credential_groups(conns), [])

    def test_a_shared_password_groups_without_naming_it(self):
        conns = [conn("a", username="admin", password="hunter2"),
                 conn("b", username="admin", password="hunter2"),
                 conn("c", username="admin", password="different")]
        groups = credential_groups(conns)
        self.assertEqual(members_of(groups), [["a", "b"]])
        self.assertNotIn("hunter2", groups[0].label)
        self.assertNotIn("hunter2", groups[0].key)

    def test_a_key_beats_a_password_when_a_record_has_both(self):
        with_key = conn("a", username="root", key_file="id_rsa", password="p")
        with_pw = conn("b", username="root", password="p")
        self.assertEqual(credential_key(with_key)[0], "key")
        self.assertEqual(credential_key(with_pw)[0], "password")
        self.assertEqual(credential_groups([with_key, with_pw]), [])

    def test_a_record_with_no_username_has_no_credential_identity(self):
        self.assertIsNone(credential_key(conn("a", password="p")))
        self.assertIsNone(credential_key(conn("a")))


class RestartTests(unittest.TestCase):

    def setUp(self):
        self.conns = [
            conn("mgmt", name="MGMT01", host="10.0.0.5"),
            conn("dc", host="10.0.0.10"),
            conn("fs", host="10.0.0.11"),
            conn("fab", host="10.1.0.5", tenant="Fabrikam"),
            conn("az", protocol="azure_ps"),
        ]
        self.servers = {"Contoso": {"conn_id": "mgmt", "transport": "ssh",
                                    "command": "x", "enabled": True}}

    def test_a_tenant_route_points_at_every_member_but_the_server(self):
        edges = restart_edges(self.conns, self.servers, {})
        self.assertEqual(sorted(e.target for e in edges), ["dc", "fs"])
        self.assertTrue(all(e.source == "mgmt" for e in edges))
        self.assertEqual(edges[0].label, "MGMT01")

    def test_an_azure_record_is_never_a_reboot_target(self):
        targets = [e.target for e in restart_edges(self.conns, self.servers, {})]
        self.assertNotIn("az", targets)

    def test_an_override_replaces_the_tenant_route_rather_than_adding_to_it(self):
        edges = restart_edges(self.conns, self.servers,
                              {"dc": {"conn_id": "fs", "enabled": True}})
        routes = {e.target: e.source for e in edges}
        self.assertEqual(routes["dc"], "fs")
        self.assertEqual(routes["fs"], "mgmt")

    def test_a_disabled_override_removes_the_route_entirely(self):
        edges = restart_edges(self.conns, self.servers,
                              {"dc": {"conn_id": "mgmt", "enabled": False}})
        self.assertNotIn("dc", [e.target for e in edges])

    def test_a_route_through_a_deleted_server_is_dropped(self):
        edges = restart_edges(self.conns, {"Contoso": {"conn_id": "gone",
                                                       "enabled": True}}, {})
        self.assertEqual(edges, [])

    def test_a_server_rebooting_another_tenants_machine_is_flagged(self):
        edges = restart_edges(self.conns, self.servers,
                              {"fab": {"conn_id": "mgmt", "enabled": True}})
        crossing = [e for e in edges if e.cross_tenant]
        self.assertEqual([e.target for e in crossing], ["fab"])


class ViewTests(unittest.TestCase):

    def test_an_unknown_lens_is_an_empty_view_not_a_crash(self):
        view = compute_relations("nonsense", [conn("a", host="10.0.0.1")])
        self.assertEqual(view.kind, LENS_NONE)
        self.assertTrue(view.empty)

    def test_participants_covers_both_shapes(self):
        conns = [conn("a", host="10.0.0.10"), conn("b", host="10.0.0.10")]
        self.assertEqual(
            compute_relations(LENS_MACHINE, conns).participants(), {"a", "b"})
        servers = {"Contoso": {"conn_id": "a", "enabled": True}}
        self.assertEqual(
            compute_relations(LENS_RESTART, conns, servers).participants(),
            {"a", "b"})

    def test_the_summary_counts_what_crosses_tenants(self):
        conns = [conn("a", host="10.0.0.10"),
                 conn("b", host="10.0.0.10", tenant="Fabrikam")]
        summary = compute_relations(LENS_MACHINE, conns).summary()
        self.assertIn("1 group", summary)
        self.assertIn("crossing tenants", summary)

    def test_an_empty_view_says_so(self):
        self.assertIn("nothing found", RelationView(LENS_SUBNET).summary())

    def test_a_connection_can_describe_its_own_place_in_the_estate(self):
        conns = [conn("a", host="10.0.0.10", username="root", key_file="id_rsa"),
                 conn("b", host="10.0.0.10", username="root", key_file="id_rsa"),
                 conn("mgmt", name="MGMT01", host="10.0.0.5")]
        servers = {"Contoso": {"conn_id": "mgmt", "enabled": True}}
        lines = describe_connection(conns[0], conns, servers, {})
        joined = "\n".join(lines)
        self.assertIn("Same machine", joined)
        self.assertIn("Same network", joined)
        self.assertIn("Shared credentials", joined)
        self.assertIn("MGMT01", joined)


class ScaleTests(unittest.TestCase):
    """A lens is meant to feel instant on a real MSP estate."""

    def test_five_hundred_connections_compute_in_well_under_a_second(self):
        conns = [
            conn(f"c{i}", host=f"10.{i // 250}.{(i // 25) % 10}.{i % 25 + 1}",
                 username="root", key_file=f"key{i % 7}",
                 tenant=f"Tenant{i % 12}")
            for i in range(500)
        ]
        servers = {f"Tenant{i}": {"conn_id": f"c{i}", "enabled": True}
                   for i in range(12)}
        started = time.perf_counter()
        for lens in (LENS_MACHINE, LENS_SUBNET, LENS_CREDENTIAL, LENS_RESTART):
            compute_relations(lens, conns, servers, {})
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0, f"all four lenses took {elapsed:.2f}s")

    def test_a_large_group_stays_one_hub_not_every_pair(self):
        """Fifty machines on a subnet is one group, not 1,225 relationships."""
        conns = [conn(f"c{i}", host=f"10.0.0.{i + 1}") for i in range(50)]
        groups = subnet_groups(conns)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 50)

class DashboardLensTests(unittest.TestCase):
    """The canvas side: switching lenses, caching, and getting back out."""

    ESTATE = [
        conn("dc-rdp", name="DC01", host="10.0.0.10", protocol="rdp"),
        conn("dc-ssh", name="DC01 shell", host="10.0.0.10", username="root",
             key_file="C:/keys/id_rsa"),
        conn("fs", name="FS01", host="10.0.0.11", username="root",
             key_file="/home/x/id_rsa"),
        conn("mgmt", name="MGMT01", host="10.0.0.5", username="svc"),
        conn("fab", name="FAB-APP", host="10.1.0.20", tenant="Fabrikam",
             username="root", key_file="id_rsa"),
    ]

    def setUp(self):
        self.dash = DashboardWidget(
            [dict(c) for c in self.ESTATE],
            {"Contoso": "#a78bfa", "Fabrikam": "#4ade80"})
        self.dash.resize(1000, 700)
        self.addCleanup(self.dash.deleteLater)
        self.seen = []
        self.dash.relation_lens_changed.connect(self.seen.append)

    def paint(self):
        """Force a real paint, so the overlay path is exercised, not just built."""
        pix = QPixmap(1000, 700)
        self.dash.render(pix)

    def test_the_graph_opens_with_no_lens(self):
        self.assertEqual(self.dash.relation_lens(), LENS_NONE)

    def test_switching_lens_announces_itself_once(self):
        self.dash.set_relation_lens(LENS_SUBNET)
        self.dash.set_relation_lens(LENS_SUBNET)      # same again: no news
        self.assertEqual(self.seen, [LENS_SUBNET])

    def test_an_unknown_lens_falls_back_to_none(self):
        self.dash.set_relation_lens("wormhole")
        self.assertEqual(self.dash.relation_lens(), LENS_NONE)

    def test_every_lens_survives_a_real_paint(self):
        for lens in (LENS_MACHINE, LENS_SUBNET, LENS_CREDENTIAL, LENS_RESTART):
            with self.subTest(lens=lens):
                self.dash.set_relation_lens(lens)
                self.paint()

    def test_the_view_is_computed_once_and_reused(self):
        first = self.dash.relation_view(LENS_SUBNET)
        self.assertIs(self.dash.relation_view(LENS_SUBNET), first)

    def test_changing_the_data_invalidates_what_was_computed(self):
        before = self.dash.relation_view(LENS_MACHINE)
        self.assertEqual(len(before.groups), 1)
        self.dash.set_data([dict(self.ESTATE[0])], {"Contoso": "#a78bfa"})
        after = self.dash.relation_view(LENS_MACHINE)
        self.assertIsNot(after, before)
        self.assertEqual(after.groups, [])

    def test_restart_arrows_follow_the_restart_configuration(self):
        self.assertTrue(self.dash.relation_view(LENS_RESTART).empty)
        self.dash.set_restart_relations(
            {"Contoso": {"conn_id": "mgmt", "enabled": True}}, {})
        view = self.dash.relation_view(LENS_RESTART)
        self.assertEqual(sorted(e.target for e in view.edges),
                         ["dc-rdp", "dc-ssh", "fs"])

    def test_escape_clears_the_lens(self):
        self.dash.set_relation_lens(LENS_CREDENTIAL)
        self.dash.keyPressEvent(QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
        self.assertEqual(self.dash.relation_lens(), LENS_NONE)
        self.assertEqual(self.seen, [LENS_CREDENTIAL, LENS_NONE])

    def test_a_hovered_machine_explains_its_own_relationships(self):
        tip = self.dash._hover_tooltip(None, "dc-ssh")
        self.assertIn("Same machine", tip)
        self.assertIn("Shared credentials", tip)

    def test_a_machine_related_to_nothing_says_nothing(self):
        self.assertEqual(self.dash._relationship_lines("mgmt-unknown"), [])



if __name__ == "__main__":
    unittest.main(verbosity=2)
