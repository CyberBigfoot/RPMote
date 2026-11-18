"""One keystroke, many machines — and the rules about when it isn't."""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.broadcast import BroadcastGroup, distribute, grid_shape

_APP = QApplication.instance() or QApplication([])


class FakeTerminal:
    """Stands in for an SSHTerminal: it can be sent to, and it can be down."""

    def __init__(self, name, connected=True, group=None, fails=False):
        self.connection_info = {"name": name, "host": f"10.0.0.{len(name)}"}
        self._connected = connected
        self.sent = []
        self.group = group
        self.fails = fails

    def _send_data(self, data):
        if self.fails:
            raise OSError("socket is gone")
        self.sent.append(data)
        # Real terminals mirror what they are given, which is how a naive
        # group would send the same keystroke round and round for ever.
        if self.group is not None:
            self.group.fan_out(self, data)


class GridShapeTests(unittest.TestCase):
    def test_a_grid_is_as_square_as_it_can_be(self):
        self.assertEqual(grid_shape(1), (1, 1))
        self.assertEqual(grid_shape(2), (1, 2))
        self.assertEqual(grid_shape(4), (2, 2))
        self.assertEqual(grid_shape(8), (3, 3))
        self.assertEqual(grid_shape(9), (3, 3))
        self.assertEqual(grid_shape(12), (3, 4))

    def test_no_panes_is_not_a_crash(self):
        self.assertEqual(grid_shape(0), (1, 1))
        self.assertEqual(distribute(0), [])

    def test_the_rows_add_up_and_stay_balanced(self):
        for count in range(1, 31):
            rows = distribute(count)
            self.assertEqual(sum(rows), count, count)
            self.assertLessEqual(max(rows) - min(rows), 1, count)


class MembershipTests(unittest.TestCase):
    def setUp(self):
        self.group = BroadcastGroup()
        self.a = FakeTerminal("a")
        self.b = FakeTerminal("b")

    def test_terminals_go_in_and_come_out(self):
        self.assertTrue(self.group.add(self.a))
        self.assertIn(self.a, self.group)
        self.assertEqual(len(self.group), 1)
        self.assertTrue(self.group.discard(self.a))
        self.assertNotIn(self.a, self.group)

    def test_the_same_terminal_only_joins_once(self):
        self.group.add(self.a)
        self.assertFalse(self.group.add(self.a))
        self.assertEqual(len(self.group), 1)

    def test_membership_changes_are_announced(self):
        seen = []
        self.group.changed.connect(lambda: seen.append(True))
        self.group.add(self.a)
        self.group.add(self.b)
        self.group.set_armed(True)
        self.group.set_included(self.b, False)
        self.group.discard(self.a)
        self.assertEqual(len(seen), 5)

    def test_arming_needs_something_to_broadcast_to(self):
        self.group.add(self.a)
        self.group.set_armed(True)
        self.assertFalse(self.group.armed)   # one pane is not a broadcast
        self.group.add(self.b)
        self.assertTrue(self.group.armed)


class FanOutTests(unittest.TestCase):
    def setUp(self):
        self.group = BroadcastGroup()
        self.terminals = [FakeTerminal(name, group=self.group)
                          for name in ("a", "b", "c")]
        for terminal in self.terminals:
            self.group.add(terminal)
        self.group.set_armed(True)

    def test_typing_in_one_pane_reaches_the_others(self):
        origin = self.terminals[0]
        sent = self.group.fan_out(origin, "uptime\r")
        self.assertEqual(sent, self.terminals[1:])
        self.assertEqual(self.terminals[1].sent, ["uptime\r"])
        self.assertEqual(self.terminals[2].sent, ["uptime\r"])

    def test_a_keystroke_does_not_go_round_for_ever(self):
        # Each recipient sends through its own path, which asks the group to
        # fan out again. Once is right; twice is a loop.
        self.group.fan_out(self.terminals[0], "x")
        self.assertEqual(self.terminals[1].sent, ["x"])
        self.assertEqual(self.terminals[2].sent, ["x"])

    def test_nothing_moves_until_it_is_armed(self):
        self.group.set_armed(False)
        self.assertEqual(self.group.fan_out(self.terminals[0], "x"), [])
        self.assertEqual(self.terminals[1].sent, [])

    def test_an_excluded_pane_is_left_out(self):
        self.group.set_included(self.terminals[1], False)
        sent = self.group.fan_out(self.terminals[0], "x")
        self.assertEqual(sent, [self.terminals[2]])
        self.assertEqual(self.terminals[1].sent, [])

    def test_a_disconnected_pane_is_not_a_target(self):
        self.terminals[2]._connected = False
        self.assertEqual(self.group.fan_out(self.terminals[0], "x"),
                         [self.terminals[1]])

    def test_a_pane_that_comes_back_is_a_target_again(self):
        self.terminals[2]._connected = False
        self.group.fan_out(self.terminals[0], "x")
        self.terminals[2]._connected = True
        self.assertIn(self.terminals[2], self.group.targets(self.terminals[0]))

    def test_one_dead_socket_does_not_stop_the_rest(self):
        self.terminals[1].fails = True
        sent = self.group.fan_out(self.terminals[0], "x")
        self.assertEqual(sent, [self.terminals[2]])
        self.assertEqual(self.terminals[2].sent, ["x"])

    def test_the_bar_sends_to_everyone_including_the_focused_pane(self):
        sent = self.group.send("systemctl restart nginx\r")
        self.assertEqual(sent, self.terminals)
        for terminal in self.terminals:
            self.assertEqual(terminal.sent, ["systemctl restart nginx\r"])

    def test_an_empty_send_does_nothing(self):
        self.assertEqual(self.group.send(""), [])
        self.assertEqual(self.group.fan_out(self.terminals[0], ""), [])


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.group = BroadcastGroup()
        self.a = FakeTerminal("a")
        self.b = FakeTerminal("b", connected=False)
        self.group.add(self.a)
        self.group.add(self.b)
        self.group.set_armed(True)

    def test_the_connections_behind_the_targets_are_available(self):
        self.assertEqual(self.group.connections(), [self.a.connection_info])

    def test_the_ui_can_see_every_pane_and_its_state(self):
        rows = self.group.labels()
        self.assertEqual([(label, included, live)
                          for _terminal, label, included, live in rows],
                         [("a", True, True), ("b", True, False)])

    def test_clearing_forgets_everything(self):
        self.group.clear()
        self.assertEqual(len(self.group), 0)
        self.assertEqual(self.group.targets(), [])


if __name__ == "__main__":
    unittest.main()
