"""Regression tests for embedded-RDP keyboard focus.

Hosted (compat) RDP sessions only receive keystrokes while Qt keyboard focus
lives inside the EmbeddedRDPWidget subtree, because input is delivered by
forwarding Qt key events to the mstsc window.  Clicks inside the session land on
mstsc's own HWND and never reach Qt, so the widget has to claim focus itself —
otherwise a freshly opened session stays keyboard-dead until the user clicks a
Qt button such as "Reconnect".
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLineEdit, QSpinBox, QTreeWidget, QWidget

from app import rdp_launcher
from app.rdp_launcher import EmbeddedRDPWidget


class _FakeProc:
    """Stand-in for a live mstsc subprocess."""

    def poll(self):
        return None


class RDPFocusClaimRulesTests(unittest.TestCase):
    """_should_claim_rdp_focus: when may the session grab the keyboard?"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_claims_focus_when_no_widget_holds_it(self):
        self.assertTrue(rdp_launcher._should_claim_rdp_focus(None, False))

    def test_claims_focus_from_a_non_text_widget(self):
        # The connection tree in the sidebar keeps focus after launching a
        # session; taking it is exactly what makes the keyboard work.
        self.assertTrue(rdp_launcher._should_claim_rdp_focus(QTreeWidget(), False))

    def test_leaves_focus_alone_when_it_is_already_ours(self):
        self.assertFalse(rdp_launcher._should_claim_rdp_focus(QWidget(), True))

    def test_never_interrupts_typing_elsewhere(self):
        self.assertFalse(rdp_launcher._should_claim_rdp_focus(QLineEdit(), False))
        self.assertFalse(rdp_launcher._should_claim_rdp_focus(QSpinBox(), False))


class RDPKeyForwardingGateTests(unittest.TestCase):
    """_should_forward_fallback_key_event: whose keystroke is it?"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = EmbeddedRDPWidget({"host": ""})
        self.addCleanup(self.widget.cleanup)
        self.addCleanup(self.widget.deleteLater)

        # Live hosted session, visible tab, active window.
        self.widget._fallback_mode = True
        self.widget._embedded_hwnd = 0x1234
        self.widget._rdp_proc = _FakeProc()
        self.widget.isVisible = lambda: True

        self.top = QWidget()
        self.addCleanup(self.top.deleteLater)
        self.top.isActiveWindow = lambda: True
        self.widget.window = lambda: self.top

    def test_forwards_when_no_qt_widget_has_focus(self):
        # Clicking inside the session activates the top-level window without
        # giving any Qt widget focus, so Qt routes the key to the window itself.
        self.assertIsNone(QApplication.focusWidget())
        self.assertTrue(self.widget._should_forward_fallback_key_event(self.top))

    def test_forwards_events_delivered_to_the_canvas(self):
        self.assertTrue(self.widget._should_forward_fallback_key_event(self.widget.canvas))

    def test_forwards_events_delivered_to_own_buttons(self):
        # This is the path that made "Reconnect" appear to fix the keyboard.
        self.assertTrue(self.widget._should_forward_fallback_key_event(self.widget.btn_reconnect))

    def test_does_not_forward_for_unrelated_widgets(self):
        stranger = QLineEdit()
        self.addCleanup(stranger.deleteLater)
        self.assertFalse(self.widget._should_forward_fallback_key_event(stranger))

    def test_does_not_forward_when_window_is_inactive(self):
        self.top.isActiveWindow = lambda: False
        self.assertFalse(self.widget._should_forward_fallback_key_event(self.top))

    def test_does_not_forward_when_mstsc_has_exited(self):
        self.widget._rdp_proc = None
        self.assertFalse(self.widget._should_forward_fallback_key_event(self.top))

    def test_does_not_forward_from_a_background_tab(self):
        self.widget.isVisible = lambda: False
        self.assertFalse(self.widget._should_forward_fallback_key_event(self.top))


class RDPFocusClaimTests(unittest.TestCase):
    """_claim_keyboard_focus moves Qt focus onto the RDP canvas."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_claim_puts_focus_on_the_canvas(self):
        widget = EmbeddedRDPWidget({"host": ""})
        self.addCleanup(widget.cleanup)
        self.addCleanup(widget.deleteLater)
        widget._fallback_mode = True
        widget.isVisible = lambda: True

        widget._claim_keyboard_focus()

        # focusWidget() is window-scoped without an activated window offscreen,
        # so assert on the window's stored focus widget instead.
        self.assertIs(widget.focusWidget(), widget.canvas)

    def test_claim_is_a_no_op_for_a_hidden_tab(self):
        widget = EmbeddedRDPWidget({"host": ""})
        self.addCleanup(widget.cleanup)
        self.addCleanup(widget.deleteLater)
        widget._fallback_mode = True
        widget.isVisible = lambda: False

        widget._claim_keyboard_focus()

        self.assertIsNone(widget.focusWidget())


if __name__ == "__main__":
    unittest.main()
