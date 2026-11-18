"""Screenshots must show what is on screen, not what Qt can paint.

``QWidget.grab()`` renders through Qt's painter and cannot see native child
windows — the RDP ActiveX control and the Chromium web view came out blank.
The tool now reads pixels back off the screen and crops them, which also has
to be right on a scaled (125 % / 150 %) display.
"""

import os
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QRect, QSize
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import QApplication, QWidget

from app import screenshot_tool
from app.screenshot_tool import grab_widget_from_screen, logical_to_device

_APP = QApplication.instance() or QApplication([])


class _FakeScreen:
    """A 1000x800 logical screen at ``ratio`` scale, painted one colour."""

    def __init__(self, ratio: float, colour: QColor, origin=QPoint(0, 0)):
        self.ratio = ratio
        self.colour = colour
        self._geometry = QRect(origin, QSize(1000, 800))

    def geometry(self):
        return self._geometry

    def grabWindow(self, _wid):
        pixmap = QPixmap(round(1000 * self.ratio), round(800 * self.ratio))
        pixmap.fill(self.colour)
        return pixmap


class _NullScreen(_FakeScreen):
    def grabWindow(self, _wid):
        return QPixmap()


class ScreenGrabTests(unittest.TestCase):
    def setUp(self):
        self.widget = QWidget()
        self.widget.resize(200, 100)
        self.widget.show()
        _APP.processEvents()
        self.addCleanup(self.widget.deleteLater)

    def test_pixels_come_from_the_screen_not_the_painter(self):
        """A widget that paints nothing must still produce what the screen shows."""
        screen = _FakeScreen(1.0, QColor("red"))
        with mock.patch.object(QWidget, "screen", return_value=screen):
            pixmap = grab_widget_from_screen(self.widget)
        self.assertEqual(pixmap.size(), QSize(200, 100))
        self.assertEqual(pixmap.toImage().pixelColor(50, 50), QColor("red"))

    def test_crop_is_scaled_on_a_hidpi_screen(self):
        screen = _FakeScreen(1.5, QColor("blue"))
        with mock.patch.object(QWidget, "screen", return_value=screen):
            pixmap = grab_widget_from_screen(self.widget)
        self.assertEqual(pixmap.size(), QSize(300, 150))
        self.assertAlmostEqual(pixmap.devicePixelRatio(), 1.5)

    def test_secondary_monitor_offset_is_removed(self):
        """A widget on a screen to the right must not be cropped off the edge."""
        screen = _FakeScreen(1.0, QColor("green"), origin=QPoint(1920, 0))
        self.widget.move(1920 + 300, 200)
        _APP.processEvents()
        with mock.patch.object(QWidget, "screen", return_value=screen):
            pixmap = grab_widget_from_screen(self.widget)
        self.assertEqual(pixmap.size(), QSize(200, 100))
        self.assertEqual(pixmap.toImage().pixelColor(10, 10), QColor("green"))

    def test_falls_back_to_grab_when_the_screen_refuses(self):
        with mock.patch.object(QWidget, "screen", return_value=_NullScreen(1.0, QColor("red"))):
            pixmap = grab_widget_from_screen(self.widget)
        self.assertFalse(pixmap.isNull())
        self.assertEqual(pixmap.size(), QSize(200, 100))


class OverlayCropTests(unittest.TestCase):
    def test_selection_is_mapped_to_device_pixels(self):
        self.assertEqual(logical_to_device(QRect(10, 20, 100, 50), 1.5),
                         QRect(15, 30, 150, 75))
        self.assertEqual(logical_to_device(QRect(10, 20, 100, 50), 1.0),
                         QRect(10, 20, 100, 50))

    def test_overlay_emits_the_scaled_region(self):
        screen = _FakeScreen(2.0, QColor("magenta"))
        with mock.patch.object(QApplication, "screenAt", return_value=screen), \
             mock.patch.object(QApplication, "primaryScreen", return_value=screen):
            overlay = screenshot_tool.ScreenshotOverlay()
        self.addCleanup(overlay.deleteLater)
        seen = []
        overlay.capture_complete.connect(seen.append)
        overlay.begin = QPoint(10, 10)
        overlay.end = QPoint(110, 60)

        class _Release:
            def button(self):
                from PyQt6.QtCore import Qt
                return Qt.MouseButton.LeftButton

        overlay.mouseReleaseEvent(_Release())
        self.assertEqual(len(seen), 1)
        # QRect(begin, end) includes both corners: 101x51 logical, doubled.
        self.assertEqual(seen[0].size(), QSize(202, 102))
        self.assertAlmostEqual(seen[0].devicePixelRatio(), 2.0)


if __name__ == "__main__":
    unittest.main()
