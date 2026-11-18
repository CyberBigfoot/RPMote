"""
RPMote Screenshot Tool — Capture full window or custom screen regions.
"""
import os
import datetime
from PyQt6.QtWidgets import QWidget, QApplication, QLabel, QVBoxLayout
from PyQt6.QtCore import Qt, QPoint, QRect, pyqtSignal, QStandardPaths
from PyQt6.QtGui import QPainter, QPixmap, QColor, QPen, QCursor


def _device_ratio(pixmap: QPixmap, screen) -> float:
    """How many captured pixels there are per logical screen pixel.

    Worked out from the sizes rather than trusted from the pixmap's own
    devicePixelRatio, which not every platform plugin sets after a grab.
    """
    logical = screen.geometry().width()
    return pixmap.width() / logical if logical else 1.0


def logical_to_device(rect: QRect, ratio: float) -> QRect:
    """Map a logical-pixel rect onto a pixmap captured at ``ratio`` scale."""
    return QRect(
        round(rect.x() * ratio), round(rect.y() * ratio),
        round(rect.width() * ratio), round(rect.height() * ratio),
    )


def grab_widget_from_screen(widget: QWidget) -> QPixmap:
    """Photograph a widget as it appears on screen.

    ``QWidget.grab()`` renders through Qt's painter, which cannot see anything
    drawn by a native child window — the RDP ActiveX control and the
    Chromium-composited web view both come out blank. Reading the pixels
    back off the screen captures whatever is actually there. Falls back to
    ``grab()`` only when the screen refuses (a secure desktop, no screen).
    """
    screen = widget.screen() or QApplication.primaryScreen()
    if screen is None or not widget.isVisible():
        return widget.grab()

    full = screen.grabWindow(0)
    if full.isNull():
        return widget.grab()

    ratio = _device_ratio(full, screen)
    origin = widget.mapToGlobal(QPoint(0, 0)) - screen.geometry().topLeft()
    region = logical_to_device(QRect(origin, widget.size()), ratio)
    region = region.intersected(full.rect())
    if region.isEmpty():
        return widget.grab()

    pixmap = full.copy(region)
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


class ScreenshotOverlay(QWidget):
    """Translucent overlay for selecting a screen region to capture."""

    # Emits the captured pixmap when finished
    capture_complete = pyqtSignal(QPixmap)
    capture_canceled = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        # Try to capture the screen where the cursor currently is, or default to primary
        self.screen = QApplication.screenAt(QCursor.pos())
        if not self.screen:
            self.screen = QApplication.primaryScreen()

        self.setGeometry(self.screen.geometry())
        self.setCursor(Qt.CursorShape.CrossCursor)

        # Capture the entire screen immediately to act as our background.
        # The grab is in device pixels; the overlay works in logical ones, so
        # the pixmap is tagged with the ratio and every crop is scaled by it.
        self.original_pixmap = self.screen.grabWindow(0)
        self._ratio = _device_ratio(self.original_pixmap, self.screen)
        self.original_pixmap.setDevicePixelRatio(self._ratio)

        self.begin = QPoint()
        self.end = QPoint()
        self.is_drawing = False

        # Status text to guide the user
        self.hint_label = QLabel(self)
        self.hint_label.setText("Click and drag to select an area (Esc to cancel)")
        self.hint_label.setStyleSheet("color: white; background: rgba(0,0,0,150); padding: 6px; border-radius: 4px;")
        self.hint_label.move(10, 10)

    def paintEvent(self, event):
        painter = QPainter(self)

        # 1. Draw the native screen we just captured
        painter.drawPixmap(0, 0, self.original_pixmap)

        # 2. Draw a dark overlay across the entire screen
        painter.fillRect(self.rect(), QColor(0, 0, 0, 140))

        # 3. If the user is dragging a region, punch a "hole" in the dark overlay
        if not self.begin.isNull() and not self.end.isNull():
            rect = QRect(self.begin, self.end).normalized()

            # Draw the original bright pixels back into the selected rectangle
            painter.drawPixmap(rect, self.original_pixmap,
                               logical_to_device(rect, self._ratio))

            # Draw a neon border around the selection bounds
            painter.setPen(QPen(QColor("#22d3ee"), 2))
            painter.drawRect(rect)

            # Show rect dimensions
            dim_text = f"{rect.width()} x {rect.height()}"
            painter.setPen(QPen(QColor("white")))
            painter.drawText(rect.bottomLeft() + QPoint(4, 16), dim_text)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.begin = event.pos()
            self.end = self.begin
            self.is_drawing = True
            self.hint_label.hide()
            self.update()

    def mouseMoveEvent(self, event):
        if self.is_drawing:
            self.end = event.pos()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_drawing = False
            rect = QRect(self.begin, self.end).normalized()

            # Close the overlay
            self.close()

            # Extract the region and emit it if it's large enough to matter
            if rect.width() > 10 and rect.height() > 10:
                captured_region = self.original_pixmap.copy(
                    logical_to_device(rect, self._ratio))
                captured_region.setDevicePixelRatio(self._ratio)
                self.capture_complete.emit(captured_region)
            else:
                self.capture_canceled.emit()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.capture_canceled.emit()
            self.close()


def save_pixmap_to_desktop(pixmap: QPixmap, source_name: str = "Window") -> str:
    """Save the QPixmap to the user's desktop with a timestamped filename."""
    desktop_path = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
    if not desktop_path:
        # Fallback if Qt fails to resolve the Desktop
        desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    clean_name = "".join([c if c.isalnum() else "_" for c in source_name])
    filename = f"RPMote_{clean_name}_{timestamp}.png"

    full_path = os.path.abspath(os.path.join(desktop_path, filename))

    # Save as high-quality PNG
    pixmap.save(full_path, "PNG", 100)
    return full_path
