import time

#: When this process started running Python, for the startup timings in the
#: log ("t_ms" on every startup.* event).
_STARTED = time.perf_counter()

import sys
import os
import logging
import math
import subprocess

# GPU on/off is decided here, before PyQt6 is imported: Chromium reads its
# flags at first launch and the dashboard picks its base class at import.
from app import hardware_acceleration
HARDWARE_ACCELERATION = hardware_acceleration.configure_environment()

from app.version import APP_VERSION

# --- Auto-Venv Relaunch Logic ---
def _ensure_venv():
    """Automatically relaunch the script using the local .venv if it exists and is not active."""
    # A packaged build carries its own interpreter and must never hand control
    # to some .venv that happens to sit next to it on a user's machine.
    if getattr(sys, "frozen", False):
        return

    # Check if we are already running in a virtual environment
    in_venv = sys.prefix != sys.base_prefix
    if in_venv:
        return

    # Look for the project's local virtual environment
    base_dir = os.path.dirname(os.path.abspath(__file__))
    venv_python = os.path.join(base_dir, ".venv", "Scripts", "python.exe")

    if os.path.exists(venv_python):
        # Prevent infinite relaunch loops
        if os.environ.get("RPMOTE_RELAUNCHED") == "1":
            return
        os.environ["RPMOTE_RELAUNCHED"] = "1"
        
        # Relaunch the script using the venv's Python interpreter
        # subprocess.run is used to wait for the process to finish and capture its exit code
        sys.exit(subprocess.run([venv_python] + sys.argv).returncode)

_ensure_venv()
# -------------------------------
# Initialize crash logging BEFORE any Qt imports so faulthandler can capture
# native/SEH crashes (GPU process, ActiveX COM, segfaults) that otherwise
# leave the process with no Python traceback.
from app.crash_logging import (
    setup_crash_logging,
    install_qt_message_handler,
    log_runtime_event,
)
setup_crash_logging("RPMote")

from PyQt6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFrame, QWidget, QMessageBox
)
from PyQt6.QtCore import (
    Qt, QTimer, QRectF, QPointF, QRect, QPoint, QLineF,
    QEventLoop, QVariantAnimation, QEasingCurve,
)
from PyQt6.QtGui import (
    QFont, QIcon, QPalette, QColor,
    QPainter, QPainterPath, QLinearGradient,
    QRadialGradient, QBrush, QPen, QPixmap, QCursor, QGuiApplication,
)

install_qt_message_handler()

# Only what the vault door needs.  The main window and everything behind it
# are imported while the door waits for the password (see app.startup and
# main() below), so the door appears without waiting for them.
from app.vault import Vault, VaultError, MIN_MASTER_PASSWORD_LEN
from app.resource_paths import get_resource_path


def register_fonts():
    """Register the bundled Meslo terminal faces (just the ones RPMote uses)."""
    from app.fonts import register_bundled_fonts
    register_bundled_fonts()


def _since_start() -> dict:
    """Milliseconds since this process started running Python, for the log."""
    return {"t_ms": int((time.perf_counter() - _STARTED) * 1000)}


# ── VaultActionButton ───────────────────────────────────────────────────────

class VaultActionButton(QPushButton):
    """
    Premium vault action button with restrained motion and clear states.

    The control is painted directly so it stays crisp at every Windows display
    scale. Hover lifts the surface and sends a single highlight across it;
    pressing settles it into the shadow, while keyboard focus gets a visible
    outer ring.
    """

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._normal_text = text
        self._denied = False
        self._hover_pct: float = 0.0
        self._press_pct: float = 0.0
        self._shine_pct: float = 1.0
        self._hover:     bool  = False
        self._pressed:   bool  = False

        self._timer = QTimer(self)
        self._timer.setInterval(16)     # ~60 fps
        self._timer.timeout.connect(self._tick)

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(54)
        self.setMinimumWidth(200)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Suppress native QPushButton painting; this class owns every pixel.
        self.setStyleSheet("background: transparent; border: none; color: transparent;")
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.pressed.connect(self._animate_press)
        self.released.connect(self._animate_release)

    def _animate_press(self):
        self._pressed = True
        self._timer.start()

    def _animate_release(self):
        self._pressed = False
        self._timer.start()

    def set_access_denied(self, denied: bool):
        """Swap the action into its short-lived terminal error state."""
        self._denied = denied
        self.setText("ACCESS_DENIED" if denied else self._normal_text)
        self.update()

    # ── Animation loop ─────────────────────────────────────────────

    def _tick(self):
        try:
            hover_target = 1.0 if self._hover and self.isEnabled() else 0.0
            press_target = 1.0 if self._pressed and self.isEnabled() else 0.0
            self._hover_pct += (hover_target - self._hover_pct) * 0.20
            self._press_pct += (press_target - self._press_pct) * 0.32

            if self._hover and self._shine_pct < 1.0:
                self._shine_pct = min(1.0, self._shine_pct + 0.055)

            self.update()

            settled = (
                abs(hover_target - self._hover_pct) < 0.01
                and abs(press_target - self._press_pct) < 0.01
                and (not self._hover or self._shine_pct >= 1.0)
            )
            if settled:
                self._hover_pct = hover_target
                self._press_pct = press_target
                self._timer.stop()
        except RuntimeError:
            # C++ button was deleted (login dialog closed) — halt the timer.
            try:
                self._timer.stop()
            except Exception:
                pass

    def showEvent(self, event):
        super().showEvent(event)
        self.update()

    def hideEvent(self, event):
        # Nothing to animate off-screen — and the login screen hides this button
        # for the whole session once the vault door opens.
        self._timer.stop()
        super().hideEvent(event)

    def enterEvent(self, event):
        self._hover = True
        self._shine_pct = 0.0
        self._timer.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self._timer.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._pressed = True
        self._timer.start()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self._pressed = False
        self._timer.start()
        super().mouseReleaseEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()

    # ── Painting ───────────────────────────────────────────────────

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w, h = float(self.width()), float(self.height())
        enabled = self.isEnabled()
        denied = self._denied and enabled
        hp = self._hover_pct if enabled else 0.0
        pp = self._press_pct if enabled else 0.0
        radius = 13.0

        def _mix(a: int, b: int, t: float) -> int:
            return int(a + (b - a) * t)

        # The body has room to lift on hover and settle downward when pressed.
        body_y = 3.0 - 1.6 * hp + 1.8 * pp
        body = QRectF(2.0, body_y, w - 4.0, h - 7.0)
        body_path = QPainterPath()
        body_path.addRoundedRect(body, radius, radius)

        # Soft shadow anchors the primary action against the near-black card.
        if enabled:
            shadow_alpha = int(54 + 32 * hp - 20 * pp)
            shadow = body.translated(0.0, 3.0 - pp)
            shadow_path = QPainterPath()
            shadow_path.addRoundedRect(shadow, radius, radius)
            shadow_colour = QColor(105, 0, 22, shadow_alpha) if denied else QColor(2, 40, 52, shadow_alpha)
            p.fillPath(shadow_path, shadow_colour)

            if hp > 0.01:
                p.setBrush(Qt.BrushStyle.NoBrush)
                glow_colour = QColor(255, 24, 67, int(52 * hp)) if denied else QColor(34, 211, 238, int(40 * hp))
                p.setPen(QPen(glow_colour, 5.0))
                p.drawRoundedRect(body.adjusted(1.0, 1.0, -1.0, -1.0), radius, radius)

        if denied:
            top = QColor(82, 7, 24)
            bottom = QColor(35, 3, 12)
        elif enabled:
            press_dim = int(18 * pp)
            top = QColor(
                max(0, _mix(10, 34, hp) - press_dim),
                max(0, _mix(163, 197, hp) - press_dim),
                max(0, _mix(194, 227, hp) - press_dim // 2),
            )
            bottom = QColor(
                max(0, _mix(7, 11, hp) - press_dim),
                max(0, _mix(110, 134, hp) - press_dim),
                max(0, _mix(135, 160, hp) - press_dim // 2),
            )
        else:
            top = QColor(47, 50, 68)
            bottom = QColor(31, 34, 49)

        surface = QLinearGradient(body.topLeft(), body.bottomRight())
        surface.setColorAt(0.0, top)
        surface.setColorAt(0.58, QColor(
            _mix(top.red(), bottom.red(), 0.35),
            _mix(top.green(), bottom.green(), 0.35),
            _mix(top.blue(), bottom.blue(), 0.35),
        ))
        surface.setColorAt(1.0, bottom)
        p.fillPath(body_path, QBrush(surface))

        # A faint top edge gives the surface physical definition without chrome.
        edge = QColor(255, 48, 84, 220) if denied else QColor(160, 232, 245, 120 if enabled else 38)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(edge, 1.0))
        p.drawRoundedRect(body.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        top_light = QLinearGradient(0.0, body.top(), 0.0, body.center().y())
        top_light.setColorAt(0.0, QColor(255, 130, 145, 34) if denied else QColor(255, 255, 255, 34 if enabled else 8))
        top_light.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillPath(body_path, QBrush(top_light))

        # One highlight sweep is triggered per hover, keeping the motion deliberate.
        if enabled and not denied and hp > 0.02 and self._shine_pct < 1.0:
            shine_x = -w * 0.25 + self._shine_pct * w * 1.5
            shine = QLinearGradient(shine_x - 34.0, 0.0, shine_x + 34.0, 0.0)
            shine.setColorAt(0.0, QColor(255, 255, 255, 0))
            shine.setColorAt(0.5, QColor(255, 255, 255, int(48 * hp)))
            shine.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.save()
            p.setClipPath(body_path)
            p.fillRect(QRectF(0.0, 0.0, w, h), QBrush(shine))
            p.restore()

        ink_alpha = 245 if enabled else 105
        ink = QColor(255, 72, 96, ink_alpha) if denied else QColor(244, 247, 255, ink_alpha)
        cy = body.center().y()

        # Small lock mark reinforces the vault action without depending on an icon font.
        lock_x = 25.0
        p.setBrush(Qt.BrushStyle.NoBrush)
        icon_pen = QPen(ink, 1.6)
        icon_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        icon_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(icon_pen)
        shackle = QPainterPath(QPointF(lock_x - 4.0, cy - 2.0))
        shackle.lineTo(lock_x - 4.0, cy - 5.0)
        shackle.cubicTo(
            lock_x - 4.0, cy - 10.0,
            lock_x + 4.0, cy - 10.0,
            lock_x + 4.0, cy - 5.0,
        )
        shackle.lineTo(lock_x + 4.0, cy - 2.0)
        p.drawPath(shackle)
        p.drawRoundedRect(QRectF(lock_x - 6.0, cy - 2.5, 12.0, 10.0), 2.5, 2.5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawEllipse(QPointF(lock_x, cy + 1.1), 1.2, 1.2)
        p.fillRect(QRectF(lock_x - 0.65, cy + 1.2, 1.3, 3.0), ink)

        # The trailing circle makes the forward direction readable at a glance.
        arrow_x = w - 25.0
        circle_edge = QColor(255, 55, 87, 95) if denied else QColor(255, 255, 255, 45 if enabled else 16)
        circle_fill = QColor(255, 24, 67, 24) if denied else QColor(255, 255, 255, int((20 + 14 * hp) if enabled else 7))
        p.setPen(QPen(circle_edge, 1.0))
        p.setBrush(circle_fill)
        p.drawEllipse(QPointF(arrow_x, cy), 13.0, 13.0)
        arrow_pen = QPen(ink, 1.8)
        arrow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        arrow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(arrow_pen)
        if denied:
            p.drawLine(QPointF(arrow_x - 3.5, cy - 3.5), QPointF(arrow_x + 3.5, cy + 3.5))
            p.drawLine(QPointF(arrow_x + 3.5, cy - 3.5), QPointF(arrow_x - 3.5, cy + 3.5))
        else:
            p.drawLine(QPointF(arrow_x - 4.0, cy), QPointF(arrow_x + 3.5, cy))
            p.drawLine(QPointF(arrow_x + 0.5, cy - 3.0), QPointF(arrow_x + 3.5, cy))
            p.drawLine(QPointF(arrow_x + 0.5, cy + 3.0), QPointF(arrow_x + 3.5, cy))

        p.setPen(ink)
        label_font = QFont("Consolas" if denied else "Segoe UI", 9 if denied else 10, QFont.Weight.Bold if denied else QFont.Weight.DemiBold)
        if denied:
            label_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.3)
        p.setFont(label_font)
        label_rect = QRectF(44.0, body.top(), w - 88.0, body.height())
        p.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, self.text())

        # Keep keyboard navigation obvious against both the card and button edge.
        if enabled and self.hasFocus():
            p.setBrush(Qt.BrushStyle.NoBrush)
            focus_pen = QPen(QColor(170, 236, 250, 225), 1.8)
            p.setPen(focus_pen)
            p.drawRoundedRect(body.adjusted(-1.0, -1.0, 1.0, 1.0), radius + 1.0, radius + 1.0)

        p.end()


# ── Split vault-door login ────────────────────────────────────────────────────

_CARD_W      = 410               # visible width of the login box
_CARD_RADIUS = 18
_GLOW_PAD    = 44                # bloom margin baked into the card widget
_SEAL_MS     = 420               # glow + shield beat, once credentials check out
_DOOR_MS     = 620               # halves sliding apart
_BG_DEEP     = QColor(7, 7, 14)

_DOOR_EASE = QEasingCurve(QEasingCurve.Type.InOutCubic)


def _door_travel(t: float) -> float:
    """Map animation time 0→1 to door travel 0→1.

    The flat lead-in is the beat where the seam lights up but nothing moves yet —
    it reads as bolts disengaging before something heavy starts to swing.
    """
    lead = 0.14
    if t <= lead:
        return 0.0
    return _DOOR_EASE.valueForProgress((t - lead) / (1.0 - lead))


def _mote_pixmap(height: int) -> QPixmap:
    """The RPMote mote (✦), drawn rather than typed (see _build_ui)."""
    screen = QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio() if screen is not None else 1.0
    size = float(height)
    pm = QPixmap(int(size * dpr + 0.5), int(size * dpr + 0.5))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c, r = size / 2.0, size / 2.0
    # Four points joined by inward curves: the ✦ of the wordmark.
    path = QPainterPath()
    path.moveTo(c, c - r)
    for x, y in ((c + r, c), (c, c + r), (c - r, c), (c, c - r)):
        path.quadTo(c, c, x, y)
    path.closeSubpath()
    fill = QLinearGradient(0.0, 0.0, 0.0, size)
    fill.setColorAt(0.0, QColor("#a5f3fc"))
    fill.setColorAt(1.0, QColor("#22d3ee"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(fill))
    p.drawPath(path)
    p.end()
    return pm


def _shield_path(r: QRectF) -> QPainterPath:
    """Rounded-top heater shield inscribed in `r`."""
    x, y, w, h = r.x(), r.y(), r.width(), r.height()
    p = QPainterPath()
    p.moveTo(x, y + h * 0.10)
    p.quadTo(x, y, x + w * 0.12, y)
    p.lineTo(x + w * 0.88, y)
    p.quadTo(x + w, y, x + w, y + h * 0.10)
    p.lineTo(x + w, y + h * 0.46)
    p.cubicTo(x + w, y + h * 0.79, x + w * 0.80, y + h * 0.94, x + w * 0.50, y + h)
    p.cubicTo(x + w * 0.20, y + h * 0.94, x, y + h * 0.79, x, y + h * 0.46)
    p.closeSubpath()
    return p


def _check_path(r: QRectF, frac: float) -> QPainterPath:
    """The first `frac` of a checkmark stroke, so it can draw itself on."""
    a = QPointF(r.x() + r.width() * 0.24, r.y() + r.height() * 0.50)
    b = QPointF(r.x() + r.width() * 0.43, r.y() + r.height() * 0.68)
    c = QPointF(r.x() + r.width() * 0.78, r.y() + r.height() * 0.31)
    l1, l2 = QLineF(a, b).length(), QLineF(b, c).length()
    d = (l1 + l2) * max(0.0, min(1.0, frac))
    path = QPainterPath(a)
    if d <= l1:
        path.lineTo(a + (b - a) * (d / l1 if l1 else 0.0))
    else:
        path.lineTo(b)
        path.lineTo(b + (c - b) * ((d - l1) / l2 if l2 else 0.0))
    return path


class _ShieldSeal(QWidget):
    """Success overlay: sinks the login fields behind a scrim and stamps a shield.

    Lives as a child of the card rather than being painted by it, because a
    parent's paintEvent runs *under* its children — a shield drawn by the card
    would end up behind the password fields.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._t = 0.0
        self.hide()

    def set_progress(self, t: float):
        self._t = t
        self.update()

    def paintEvent(self, _event):
        t = self._t
        if t <= 0.0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())

        clip = QPainterPath()
        clip.addRoundedRect(QRectF(0.0, 0.0, w, h), _CARD_RADIUS, _CARD_RADIUS)
        p.setClipPath(clip)

        # Matches the card's own body gradient, so once it lands the fields are
        # gone completely rather than ghosting through behind the shield.
        a = int(255 * min(1.0, t / 0.30))
        scrim = QLinearGradient(0.0, 0.0, 0.0, h)
        scrim.setColorAt(0.0, QColor(14, 14, 26, a))
        scrim.setColorAt(1.0, QColor(8, 8, 16, a))
        p.fillRect(QRectF(0.0, 0.0, w, h), QBrush(scrim))

        appear = min(1.0, t / 0.42)
        pop  = QEasingCurve(QEasingCurve.Type.OutBack).valueForProgress(appear)
        size = min(w, h) * 0.46 * (0.55 + 0.45 * pop)
        rect = QRectF((w - size * 0.84) / 2.0, (h - size) / 2.0, size * 0.84, size)

        body = _shield_path(rect)
        fill = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        fill.setColorAt(0.0, QColor(34, 200, 230, int(210 * appear)))
        fill.setColorAt(1.0, QColor(8, 70, 100, int(210 * appear)))
        p.fillPath(body, QBrush(fill))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(190, 245, 252, int(255 * appear)), 2.0))
        p.drawPath(body)

        if t > 0.38:
            frac = QEasingCurve(QEasingCurve.Type.OutCubic).valueForProgress(
                min(1.0, (t - 0.38) / 0.26)
            )
            pen = QPen(QColor(235, 245, 255), max(2.0, size * 0.075))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPath(_check_path(rect, frac))

        # Rings pushing outward from the seal.
        for k in range(2):
            start = 0.34 + k * 0.20
            if t <= start:
                continue
            u = min(1.0, (t - start) / 0.46)
            alpha = int(90 * (1.0 - u) ** 1.6)
            if alpha <= 1:
                continue
            p.setPen(QPen(QColor(34, 211, 238, alpha), 2.0))
            p.setBrush(Qt.BrushStyle.NoBrush)
            radius = size * (0.55 + 1.5 * u)
            p.drawEllipse(QPointF(w / 2.0, h / 2.0), radius, radius)


class _LoginCard(QFrame):
    """The centered login box.

    Carries its own `_GLOW_PAD` margin so the success bloom bleeds outward
    without dragging the full-screen backdrop into a 60fps repaint.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._glow = 0.0

    def set_glow(self, v: float):
        self._glow = max(0.0, min(1.0, v))
        self.update()

    def box_rect(self) -> QRectF:
        """The visible box, inside the bloom margin."""
        return QRectF(self.rect()).adjusted(_GLOW_PAD, _GLOW_PAD, -_GLOW_PAD, -_GLOW_PAD)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = self.box_rect()
        g = self._glow

        # Bloom: concentric strokes fading outward stand in for a blur, and cost
        # far less than a QGraphicsDropShadowEffect over the animated button.
        colour = QColor(int(14 + 20 * g), int(150 + 61 * g), int(190 + 48 * g))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i in range(_GLOW_PAD, 0, -2):
            alpha = (0.10 + 0.90 * g) * (1.0 - i / _GLOW_PAD) ** 2.4 * 0.42
            if alpha <= 0.004:
                continue
            c = QColor(colour)
            c.setAlphaF(min(1.0, alpha))
            p.setPen(QPen(c, 2.0))
            p.drawRoundedRect(box.adjusted(-i, -i, i, i), _CARD_RADIUS + i, _CARD_RADIUS + i)

        body = QLinearGradient(box.topLeft(), box.bottomLeft())
        body.setColorAt(0.0, QColor(14, 14, 26))
        body.setColorAt(1.0, QColor(8, 8, 16))
        path = QPainterPath()
        path.addRoundedRect(box, _CARD_RADIUS, _CARD_RADIUS)
        p.fillPath(path, QBrush(body))

        bw = 1.0 + 1.2 * g
        p.setPen(QPen(QColor(int(24 + 10 * g), int(44 + 167 * g), int(56 + 182 * g)), bw))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(box.adjusted(bw / 2, bw / 2, -bw / 2, -bw / 2), _CARD_RADIUS, _CARD_RADIUS)


class _GlitchOverlay(QWidget):
    """Short-lived RGB-split snapshot used for failed vault authentication."""

    _PAD = 20
    _STUTTER = (0, -17, 13, -9, 19, -15, 8, -5, 12, -8, 4, 0)

    def __init__(self, snapshot: QPixmap, parent=None):
        super().__init__(parent)
        self._snapshot = snapshot
        self._red = self._tinted(snapshot, QColor(255, 18, 56, 170))
        self._blue = self._tinted(snapshot, QColor(0, 132, 255, 155))
        self._frame = 0
        logical = snapshot.deviceIndependentSize()
        self.setFixedSize(int(math.ceil(logical.width())) + 2 * self._PAD, int(math.ceil(logical.height())))
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

    @staticmethod
    def _tinted(source: QPixmap, colour: QColor) -> QPixmap:
        tinted = QPixmap(source.size())
        tinted.setDevicePixelRatio(source.devicePixelRatio())
        tinted.fill(Qt.GlobalColor.transparent)
        p = QPainter(tinted)
        p.drawPixmap(QPointF(0.0, 0.0), source)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        p.fillRect(QRectF(QPointF(0.0, 0.0), source.deviceIndependentSize()), colour)
        p.end()
        return tinted

    def set_progress(self, t: float):
        self._frame = min(len(self._STUTTER) - 1, int(t * len(self._STUTTER)))
        self.update()

    def stutter_offset(self) -> int:
        return self._STUTTER[self._frame]

    def paintEvent(self, _event):
        p = QPainter(self)
        origin = QPointF(float(self._PAD), 0.0)
        frame = self._frame
        separation = 3.0 + float(frame % 3)

        # Full-frame colour ghosts expose red and blue edges around the card.
        p.setOpacity(0.58)
        p.drawPixmap(origin + QPointF(separation, 0.0), self._red)
        p.drawPixmap(origin - QPointF(separation, 0.0), self._blue)
        p.setOpacity(1.0)
        p.drawPixmap(origin, self._snapshot)

        # Repaint discrete horizontal bands at abrupt offsets. The frame-indexed
        # positions intentionally stutter instead of interpolating smoothly.
        height = max(1, self.height())
        for i in range(6):
            band_y = (frame * 29 + i * 67) % height
            band_h = 5 + (frame * 3 + i * 5) % 13
            slice_offset = ((frame * 11 + i * 7) % 19) - 9
            p.save()
            p.setClipRect(QRectF(0.0, float(band_y), float(self.width()), float(band_h)))
            p.drawPixmap(origin + QPointF(float(slice_offset), 0.0), self._snapshot)
            streak = QColor(255, 22, 58, 95) if i % 2 == 0 else QColor(0, 142, 255, 85)
            p.fillRect(QRectF(0.0, float(band_y), float(self.width()), 1.0), streak)
            p.restore()

        p.end()


class _Stage(QWidget):
    """The vault door: an opaque backdrop, seamed down the exact middle.

    This is the only opaque thing in the login window — hiding it is what turns
    the window transparent and uncovers the app behind.
    """

    def __init__(self, card: _LoginCard, hint: QLabel, parent=None):
        super().__init__(parent)
        self._card = card
        self._hint = hint
        card.setParent(self)
        hint.setParent(self)
        #: Called once, after the door has first been painted.
        self.on_first_paint = None

    def card_home(self) -> QPoint:
        """The card's resting position — dead centre, so its box straddles the seam."""
        return QPoint(
            (self.width() - self._card.width()) // 2,
            (self.height() - self._card.height()) // 2,
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Positioned by hand, not by a layout, so the shake can move the card
        # without a relayout yanking it back.
        self._card.move(self.card_home())
        self._hint.setFixedWidth(self.width())
        self._hint.move(0, self.height() - 56)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = float(self.width()), float(self.height())
        rect = QRectF(0.0, 0.0, w, h)

        p.fillRect(rect, _BG_DEEP)

        glow = QRadialGradient(w / 2.0, h * 0.42, max(w, h) * 0.42)
        glow.setColorAt(0.0, QColor(6, 36, 48, 110))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(rect, QBrush(glow))

        # The seam — advertises where this thing is going to split.
        cx = w / 2.0
        halo = QLinearGradient(cx - 110.0, 0.0, cx + 110.0, 0.0)
        halo.setColorAt(0.0, QColor(0, 0, 0, 0))
        halo.setColorAt(0.5, QColor(8, 145, 178, 42))
        halo.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(QRectF(cx - 110.0, 0.0, 220.0, h), QBrush(halo))
        p.fillRect(QRectF(cx - 1.5, 0.0, 3.0, h), QColor(3, 3, 8))
        p.fillRect(QRectF(cx - 0.5, 0.0, 1.0, h), QColor(120, 220, 240, 70))

        vig = QRadialGradient(cx, h / 2.0, max(w, h) * 0.72)
        vig.setColorAt(0.55, QColor(0, 0, 0, 0))
        vig.setColorAt(1.0, QColor(0, 0, 0, 140))
        p.fillRect(rect, QBrush(vig))
        p.end()

        callback, self.on_first_paint = self.on_first_paint, None
        if callback is not None:
            # After this frame reaches the screen, not during it.
            QTimer.singleShot(0, callback)


class _Curtain(QWidget):
    """Half of the frozen door, carrying its slice of the login screen with it.

    Both halves hold the *whole* snapshot and just blit it at different offsets;
    widget bounds do the cropping, and the pixmap's devicePixelRatio keeps it
    1:1 on scaled displays without any manual source-rect maths.
    """

    def __init__(self, parent, pix: QPixmap, pix_x: int, right_edge: bool):
        super().__init__(parent)
        self._pix = pix
        self._pix_x = pix_x
        self._right_edge = right_edge      # which side faces the opening gap
        self._edge = 1.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def set_edge(self, v: float):
        self._edge = v
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.drawPixmap(self._pix_x, 0, self._pix)

        e = self._edge
        if e <= 0.01:
            return
        w, h = float(self.width()), float(self.height())
        bloom = 80.0
        hot = QColor(215, 250, 255, int(235 * e))
        if self._right_edge:
            g = QLinearGradient(w - bloom, 0.0, w, 0.0)
            g.setColorAt(0.0, QColor(8, 145, 178, 0))
            g.setColorAt(1.0, QColor(34, 211, 238, int(130 * e)))
            p.fillRect(QRectF(w - bloom, 0.0, bloom, h), QBrush(g))
            p.fillRect(QRectF(w - 2.0, 0.0, 2.0, h), hot)
        else:
            g = QLinearGradient(bloom, 0.0, 0.0, 0.0)
            g.setColorAt(0.0, QColor(8, 145, 178, 0))
            g.setColorAt(1.0, QColor(34, 211, 238, int(130 * e)))
            p.fillRect(QRectF(0.0, 0.0, bloom, h), QBrush(g))
            p.fillRect(QRectF(0.0, 0.0, 2.0, h), hot)


class SplitLoginScreen(QDialog):
    """Full-screen vault door for master password setup or unlock.

    Two-phase by necessity: MainWindow can't be built until the vault is open,
    but the door has to still be covering the screen when it is. So
    `authenticate()` blocks until the credentials land, the caller builds the
    app behind the still-opaque door, and `reveal()` splits it apart.
    """

    _INPUT_STYLE = """
        QLineEdit {
            background-color: #0c0c1c;
            border: 1px solid #1c1c32;
            border-radius: 10px;
            font-size: 15px;
            color: #c8c8e0;
            padding: 0 16px;
            selection-background-color: #0b6f86;
        }
        QLineEdit:focus {
            border-color: #0aa3c2;
            background-color: #0e0e24;
        }
        QLineEdit:disabled {
            color: #3a3a52;
        }
    """

    _DENIED_INPUT_STYLE = """
        QLineEdit {
            background-color: #2a0009;
            border: 2px solid #ff1744;
            border-radius: 10px;
            font-size: 15px;
            color: #fff1f3;
            padding: 0 15px;
            selection-background-color: #ff1744;
        }
        QLineEdit:focus {
            background-color: #35000c;
            border-color: #ff315c;
        }
    """

    def __init__(self, vault: Vault):
        super().__init__()
        self.vault = vault
        self._phase = "auth"          # auth → sealing → reveal
        self._result = False
        self._loop: QEventLoop | None = None
        self._shake_anim: QVariantAnimation | None = None
        self._denied_anim: QVariantAnimation | None = None
        self._glitch_overlay: _GlitchOverlay | None = None
        self._feedback_active = False
        #: Start-up work running while this screen waits (app.startup.Warmup).
        self.warmup = None

        self.setWindowTitle("RPMote — Unlock Vault")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        # Must be set before the first show to take effect on Windows. The door
        # is an opaque child, so the window still *looks* solid until it hides.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        self._screen_area = screen.availableGeometry()
        self.setGeometry(screen.geometry())

        self._build_ui()

    # ── Construction ───────────────────────────────────────────────

    def _build_ui(self):
        card = _LoginCard()
        card.setFixedWidth(_CARD_W + 2 * _GLOW_PAD)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(
            _GLOW_PAD + 38, _GLOW_PAD + 36, _GLOW_PAD + 38, _GLOW_PAD + 20
        )
        layout.setSpacing(0)

        # Every face below is plain 'Segoe UI' (on every Windows since Vista),
        # and the mote is drawn, not the ✦ character: a glyph Segoe UI lacks,
        # or a font list with a fallback in it, makes Qt build its font
        # fallback table on first use -- 150-200 ms in front of this screen.
        # The main window needs that table, so it is built while this screen
        # waits instead (_warm_text in main()).
        header = QWidget()
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)
        mote = QLabel()
        mote.setPixmap(_mote_pixmap(20))
        title = QLabel("RPMote")
        title.setStyleSheet(
            "color: #ffffff; font-size: 22px; font-weight: 700;"
            "font-family: 'Segoe UI'; letter-spacing: 0.5px;"
        )
        header_row.addStretch(1)
        header_row.addWidget(mote)
        header_row.addWidget(title)
        header_row.addStretch(1)
        layout.addWidget(header)
        layout.addSpacing(9)

        mode_text = "Set a Master Password" if not self.vault.is_setup else "Enter Master Password"
        self.prompt = QLabel(mode_text)
        self.prompt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prompt.setStyleSheet(
            "color: #8b8fa8; font-size: 13px;"
            "font-family: 'Segoe UI';"
        )
        layout.addWidget(self.prompt)
        layout.addSpacing(28)

        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("Master password")
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setFixedHeight(44)
        self.password_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.password_input.setStyleSheet(self._INPUT_STYLE)
        layout.addWidget(self.password_input)

        self.confirm_input = QLineEdit()
        self.confirm_input.setPlaceholderText("Confirm password")
        self.confirm_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirm_input.setFixedHeight(44)
        self.confirm_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.confirm_input.setStyleSheet(self._INPUT_STYLE)
        if not self.vault.is_setup:
            layout.addSpacing(10)
            layout.addWidget(self.confirm_input)

        layout.addSpacing(20)

        btn_label = "Setup Vault" if not self.vault.is_setup else "Unlock"
        self.btn_action = VaultActionButton(btn_label)
        self.btn_action.clicked.connect(self._handle_action)
        self.password_input.returnPressed.connect(self._handle_action)
        self.confirm_input.returnPressed.connect(self._handle_action)
        layout.addWidget(self.btn_action)
        layout.addSpacing(12)

        # Locked-out escape hatch — only meaningful once a vault exists.  A
        # flat button styled as a link: Qt's first clickable rich-text label
        # costs about a fifth of a second to build, all of it in front of
        # the login screen appearing.
        self.forgot_label = None
        if self.vault.is_setup:
            self.forgot_label = QPushButton("Forgot your password?")
            self.forgot_label.setObjectName("forgotLink")
            self.forgot_label.setCursor(Qt.CursorShape.PointingHandCursor)
            self.forgot_label.setFlat(True)
            self.forgot_label.setStyleSheet(
                "QPushButton#forgotLink { color: #a5b4fc; background: transparent;"
                " border: none; padding: 2px 6px; font-size: 11px;"
                " font-family: 'Segoe UI'; }"
                "QPushButton#forgotLink:hover, QPushButton#forgotLink:focus {"
                " color: #c7d2fe; text-decoration: underline; }")
            self.forgot_label.clicked.connect(self._handle_forgot_password)
            layout.addWidget(self.forgot_label, 0, Qt.AlignmentFlag.AlignHCenter)
            layout.addSpacing(6)

        # Fixed height: the card must not resize when an error appears mid-shake.
        self.error_label = QLabel("")
        self.error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_label.setFixedHeight(18)
        self.error_label.setStyleSheet(
            "color: #f87171; font-size: 12px;"
            "font-family: 'Segoe UI';"
        )
        layout.addWidget(self.error_label)

        card.adjustSize()
        self._card = card
        self._seal = _ShieldSeal(card)

        # Frameless leaves no close button, so name the way out.
        self._hint = QLabel("Press Esc to quit")
        self._hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint.setFixedHeight(16)
        self._hint.setStyleSheet(
            "color: #7c8597; font-size: 11px;"
            "font-family: 'Segoe UI'; letter-spacing: 1px;"
        )

        self._stage = _Stage(card, self._hint, self)
        self._stage.setGeometry(self.rect())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._stage.setGeometry(self.rect())

    # ── Phase 1: authenticate ──────────────────────────────────────

    def screen_area(self) -> QRect:
        """Usable area of the monitor the door opened on."""
        return self._screen_area

    def authenticate(self) -> bool:
        """Show the door and block until it is unlocked or the user gives up."""
        self.show()
        self.raise_()
        self.activateWindow()
        self.password_input.setFocus()
        self._loop = QEventLoop()
        self._loop.exec()
        self._loop = None
        return self._result

    def _finish_auth(self, ok: bool):
        self._result = ok
        if self._loop is not None and self._loop.isRunning():
            self._loop.quit()

    def reject(self):
        if self._phase != "auth":
            return                      # no bailing out mid-ceremony
        self._finish_auth(False)
        super().reject()

    def _fail(self, message: str):
        self.error_label.setText(message)
        self._shake()

    def _shake(self):
        # Retry can land mid-shake (hold Enter on an empty field), so take the
        # rest position from the stage rather than from wherever the card is now.
        if self._shake_anim is not None:
            self._shake_anim.stop()
            self._shake_anim.deleteLater()
        base = self._stage.card_home()
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(420)
        anim.valueChanged.connect(
            lambda t: self._card.move(
                base.x() + int(math.sin(t * math.pi * 5) * 14 * (1.0 - t)), base.y()
            )
        )
        anim.finished.connect(lambda: self._card.move(base))
        anim.start()
        self._shake_anim = anim         # outlive the call frame

    def _access_denied(self):
        """Play the stepped 300 ms RGB-split response for a bad password."""
        if self._feedback_active:
            return
        self._feedback_active = True
        self.error_label.setText("")

        if self._shake_anim is not None:
            self._shake_anim.stop()
            self._shake_anim.deleteLater()
            self._shake_anim = None

        for field in (self.password_input, self.confirm_input):
            field.setReadOnly(True)
            field.setStyleSheet(self._DENIED_INPUT_STYLE)
        self.btn_action.set_access_denied(True)

        base = self._stage.card_home()
        self._card.move(base)
        # QWidget.grab() can initialize transparent margins opaquely on some
        # Windows/Qt combinations. Copy only the visible rounded card into a
        # transparent pixmap so the RGB ghosts never flash a rectangular plate.
        grabbed = self._card.grab()
        snapshot = QPixmap(grabbed.size())
        snapshot.setDevicePixelRatio(grabbed.devicePixelRatio())
        snapshot.fill(Qt.GlobalColor.transparent)
        snapshot_painter = QPainter(snapshot)
        snapshot_clip = QPainterPath()
        snapshot_clip.addRoundedRect(self._card.box_rect(), _CARD_RADIUS, _CARD_RADIUS)
        snapshot_painter.setClipPath(snapshot_clip)
        snapshot_painter.drawPixmap(QPointF(0.0, 0.0), grabbed)
        snapshot_painter.end()
        overlay = _GlitchOverlay(snapshot, self._stage)
        overlay.move(base.x() - _GlitchOverlay._PAD, base.y())
        overlay.show()
        overlay.raise_()
        self._glitch_overlay = overlay

        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(300)

        def on_value(value: float):
            if self._glitch_overlay is None:
                return
            self._glitch_overlay.set_progress(value)
            offset = self._glitch_overlay.stutter_offset()
            self._card.move(base.x() + offset, base.y())
            self._glitch_overlay.move(
                base.x() - _GlitchOverlay._PAD + offset,
                base.y(),
            )

        anim.valueChanged.connect(on_value)
        anim.finished.connect(self._finish_access_denied)
        self._denied_anim = anim
        anim.start()

    def _finish_access_denied(self):
        """Snap back to the clean login state and make retrying immediate."""
        self._card.move(self._stage.card_home())
        if self._glitch_overlay is not None:
            self._glitch_overlay.hide()
            self._glitch_overlay.deleteLater()
            self._glitch_overlay = None

        for field in (self.password_input, self.confirm_input):
            field.setStyleSheet(self._INPUT_STYLE)
            field.setReadOnly(False)
        self.btn_action.set_access_denied(False)
        self.password_input.clear()
        self.password_input.setFocus(Qt.FocusReason.OtherFocusReason)
        self._feedback_active = False

        if self._denied_anim is not None:
            self._denied_anim.deleteLater()
            self._denied_anim = None

    def _finish_warmup(self):
        """Let the start-up work finish before anything here imports or
        touches the vault, so two threads never import side by side."""
        if self.warmup is not None:
            self.warmup.finish()

    def _handle_forgot_password(self, *_args):
        """Offer recovery-key redemption, or a knowing wipe-and-restart."""
        if self._phase != "auth" or self._feedback_active:
            return
        self._finish_warmup()
        from app.recovery import ForgotPasswordDialog

        dialog = ForgotPasswordDialog(self.vault, parent=self)
        dialog.exec()

        if dialog.outcome == "recovered":
            # The vault is already open — go straight through the door.
            self._phase = "sealing"
            self.error_label.setText("")
            self.password_input.setEnabled(False)
            self.confirm_input.setEnabled(False)
            self.btn_action.setEnabled(False)
            if self.forgot_label is not None:
                self.forgot_label.hide()
            self._play_seal()
            self._finish_auth(True)
        elif dialog.outcome == "erased":
            QMessageBox.information(
                self, "Vault Erased",
                "The vault has been erased.\n\n"
                "RPMote will restart so you can create a new master password.")
            python = sys.executable
            os.execv(python, [python] + sys.argv)

    def _handle_action(self):
        if self._phase != "auth" or self._feedback_active:
            return

        password = self.password_input.text()
        if not password:
            self._fail("Enter your master password.")
            return

        self._finish_warmup()
        try:
            if not self.vault.is_setup:
                if password != self.confirm_input.text():
                    self._fail("Passwords do not match.")
                    return
                if len(password) < MIN_MASTER_PASSWORD_LEN:
                    self._fail(
                        f"Password must be at least {MIN_MASTER_PASSWORD_LEN} characters.")
                    return
                self.vault.setup(password)
                self._offer_recovery_key_after_setup()
            else:
                if not self.vault.unlock(password):
                    self._access_denied()
                    return
        except VaultError as e:
            self._fail(str(e))
            return

        self._phase = "sealing"
        self.error_label.setText("")
        self.password_input.setEnabled(False)
        self.confirm_input.setEnabled(False)
        self.btn_action.setEnabled(False)
        self._play_seal()
        self._finish_auth(True)

    def _offer_recovery_key_after_setup(self):
        """Right after setup is the one moment the user can safely save one."""
        try:
            from app.recovery import offer_recovery_key
            offer_recovery_key(self.vault, parent=self, prompt=True)
        except Exception:
            # A recovery key is a nicety; never block getting into a new vault.
            pass

    def _play_seal(self):
        """Glow the card and stamp the shield. Blocks until the beat lands."""
        self._seal.setGeometry(self._card.box_rect().toRect())
        self._seal.show()
        self._seal.raise_()

        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(_SEAL_MS)

        def on_value(v: float):
            self._card.set_glow(min(1.0, v * 2.2))   # glow snaps up, then holds
            self._seal.set_progress(v)

        anim.valueChanged.connect(on_value)
        loop = QEventLoop()
        anim.finished.connect(loop.quit)
        anim.start()
        loop.exec()

    # ── Phase 2: reveal ────────────────────────────────────────────

    def reveal(self, beneath: QWidget) -> None:
        """Split the door and slide both halves off-screen, uncovering `beneath`."""
        self._phase = "reveal"
        self.raise_()
        beneath.repaint()               # never uncover a half-painted window

        snapshot = self._stage.grab()
        w, h = self.width(), self.height()
        half = w // 2

        left = _Curtain(self, snapshot, 0, right_edge=True)
        left.setGeometry(0, 0, half, h)
        right = _Curtain(self, snapshot, -half, right_edge=False)
        right.setGeometry(half, 0, w - half, h)
        left.show()
        right.show()
        self._stage.hide()              # window is now transparent behind the halves

        span = half + 4                 # a hair past the edge, so no sliver lingers
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(_DOOR_MS)

        def on_value(t: float):
            travel = span * _door_travel(t)
            left.move(int(-travel), 0)
            right.move(int(half + travel), 0)
            # Seam lights up first, then cools as the gap opens.
            edge = min(1.0, t / 0.10) * (0.15 + 0.85 * max(0.0, 1.0 - travel / 300.0))
            left.set_edge(edge)
            right.set_edge(edge)

        anim.valueChanged.connect(on_value)
        loop = QEventLoop()
        anim.finished.connect(loop.quit)
        anim.start()
        loop.exec()

        # hide(), not close(): close() would route through QDialog::closeEvent to
        # reject(), which vetoes itself outside the auth phase — leaving this
        # full-screen always-on-top window parked over the app for the session.
        self.hide()
        self.deleteLater()


def _center_on(window, area: QRect):
    """Park `window` on the monitor the login opened on, keeping its own size."""
    geo = window.geometry()
    geo.moveCenter(area.center())
    window.move(geo.topLeft())


def _sweep_saved_credentials():
    """Anything a previous run left in Credential Manager is a customer
    password readable without the vault.  Runs on the warm-up thread as soon
    as the door is up -- long before anything could connect."""
    from app.windows_credentials import purge_saved_credentials
    purge_saved_credentials()


def _activex_bootstrap():
    """One-time registration of the embedded RDP control (a marker file
    makes every later launch a single read)."""
    from app.rdp_activex_setup import ensure_activex_registered_once
    ensure_activex_registered_once()
    log_runtime_event("startup.activex.bootstrap.complete")


def _warm_text():
    """Build Qt's font-fallback table ahead of time.

    The theme stylesheet asks for 'Segoe UI', 'Inter', sans-serif, and the
    About box spells the ✦ that Segoe UI has no glyph for; the first of either
    makes Qt build its fallback table (150-200 ms).  Paid here, while the
    login screen waits, instead of between the password and the main window.
    """
    # The mote is there because the About box spells ✦ in the wordmark.
    probe = QLabel("\u2726 RPMote")
    probe.setStyleSheet("font-family: 'Segoe UI', 'Inter', sans-serif; font-size: 13px;")
    probe.sizeHint()
    probe.deleteLater()


def _apply_saved_stylesheet(app, vault):
    """Set the theme's stylesheet before the main window exists.

    MainWindow sets it too, but by then it has built every widget, and an
    application stylesheet re-polishes every widget there is -- a third of
    the window's build time.  Set first, each widget is polished once, as it
    is made, and MainWindow finds the sheet already in place.
    """
    try:
        from app.themes import ALL_THEMES, generate_stylesheet
        index = int(vault.settings.get("theme_index", 0))
        theme = ALL_THEMES[index if 0 <= index < len(ALL_THEMES) else 0]
        app.setStyleSheet(generate_stylesheet(theme))
    except Exception:
        logging.getLogger("rpmote").exception("could not pre-apply the theme")


def main():
    from app.startup import Warmup

    log_runtime_event("startup.main.begin", argv=sys.argv, **_since_start())

    # QWebEngine (used for iDRAC) requires shared OpenGL contexts.
    # This attribute MUST be set before QApplication is created.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

    app = QApplication(sys.argv)
    app.setApplicationName("RPMote")
    app.setOrganizationName("Grant")
    log_runtime_event(
        "startup.qapplication.created",
        hardware_acceleration=HARDWARE_ACCELERATION,
        renderer=hardware_acceleration.describe(),
        **_since_start(),
    )

    # Dark palette for the application overall
    palette = QPalette()
    palette.setColor(QPalette.ColorGroup.All, QPalette.ColorRole.Window, QColor(7, 11, 18))
    palette.setColor(QPalette.ColorGroup.All, QPalette.ColorRole.WindowText, Qt.GlobalColor.white)
    app.setPalette(palette)

    vault = Vault()

    # Set Application Icon
    icon_path = get_resource_path("app", "resources", "icons", "app_icon.ico")
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # Show the vault door.  Everything below happens while it waits for the
    # password: the credential sweep and the imports on a thread, the fonts,
    # the RDP control check and the GPU driver in slices on this one.
    login = SplitLoginScreen(vault)
    log_runtime_event("startup.login.built", **_since_start())
    warmup = Warmup(
        gui_steps=[
            ("fonts", register_fonts),
            ("text", _warm_text),
            ("activex bootstrap", _activex_bootstrap),
            ("gpu", hardware_acceleration.prewarm_gpu),
        ],
        background_steps=[("credential sweep", _sweep_saved_credentials)],
    )
    login.warmup = warmup

    def door_is_up():
        log_runtime_event("startup.login.dialog.opened", **_since_start())
        warmup.start(first_step_ms=40)

    login._stage.on_first_paint = door_is_up
    if not login.authenticate():
        log_runtime_event("startup.login.cancelled")
        # The credential sweep must still complete; nothing else matters now.
        warmup.finish(everything=False)
        sys.exit(0)
    log_runtime_event("startup.login.accepted", **_since_start())
    warmup.finish()
    log_runtime_event("startup.warmup", steps=warmup.summary())

    # Known host keys live in the vault, so they are only reachable once it is
    # open. Bind before anything can connect; this also migrates any keys the
    # pre-unlock fallback file picked up.
    from app import host_keys
    host_keys.bind_vault(vault)
    from app.main_window import MainWindow

    # Built while the door is still covering the screen: MainWindow reads
    # vault.settings, so it cannot exist until the vault is open, but it has to
    # be up and painted before the halves part or there'd be nothing to reveal.
    _apply_saved_stylesheet(app, vault)
    window = MainWindow(vault)
    _center_on(window, login.screen_area())
    window.showMaximized()
    log_runtime_event("startup.main_window.shown", **_since_start())

    login.reveal(window)
    login = None                        # frees the full-screen snapshot
    hardware_acceleration.release_gpu_prewarm()
    log_runtime_event("startup.login.reveal.complete", **_since_start())
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
