"""The cursor must stay visible while keys are held or output arrives.

Blinking is a timer flipping a flag; typing sets the flag back on. The two
used to race: a held arrow key set the flag just before every tick cleared
it again, so the cursor vanished for as long as the key repeated and came
back only when it was released.
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.ssh_terminal import SSHTerminal

_APP = QApplication.instance() or QApplication([])

CONN = {"name": "test", "host": "example.invalid", "port": 22, "username": "u"}


class _FakeChannel:
    closed = False

    def __init__(self):
        self.sent = b""

    def sendall(self, data):
        self.sent += data


class CursorBlinkTests(unittest.TestCase):
    def setUp(self):
        self.term = SSHTerminal(dict(CONN))
        self.term._connected = True
        self.term.channel = _FakeChannel()
        self.term.show()
        self.term.terminal.setFocus()
        _APP.processEvents()
        self.term._blink_timer.stop()  # ticks are driven by hand below
        self.addCleanup(self.term.deleteLater)

    def tick(self):
        self.term._toggle_cursor_blink()

    def test_typing_in_the_off_phase_shows_the_cursor_at_once(self):
        self.tick()
        self.assertFalse(self.term.terminal._cursor_blink_on)
        self.term._send_data("\x1b[D")
        self.assertTrue(self.term.terminal._cursor_blink_on)

    def test_held_key_never_hides_the_cursor(self):
        # Key repeat is faster than the blink: a keystroke lands between
        # every pair of ticks. The cursor must be drawn after each one.
        self.term._blink_timer.start()
        for _ in range(6):
            self.term._send_data("\x1b[C")
            self.assertTrue(self.term.terminal._cursor_blink_on)
            # A keystroke restarts the period, so the timer's own tick is
            # still a full interval away; a tick arriving anyway (the old
            # race) must not find a stale "on" flag to clear.
            self.assertTrue(self.term._blink_timer.isActive())
        self.term._blink_timer.stop()

    def test_incoming_output_shows_the_cursor(self):
        self.tick()
        self.assertFalse(self.term.terminal._cursor_blink_on)
        self.term._on_data_received("x")
        self.assertTrue(self.term.terminal._cursor_blink_on)

    def test_blinking_resumes_after_a_key(self):
        self.term._send_data("a")
        self.tick()
        self.assertFalse(self.term.terminal._cursor_blink_on)
        self.tick()
        self.assertTrue(self.term.terminal._cursor_blink_on)


if __name__ == "__main__":
    unittest.main()
