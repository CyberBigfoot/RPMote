"""Animated full-canvas backgrounds for the dashboard.

Each background is a small self-contained particle system or procedural
scene: it owns its state, advances it by wall-clock ``dt`` in ``tick`` and
paints it in ``paint``.  The dashboard holds one instance for the active
theme and calls those two methods; nothing here knows about tenants, nodes
or the rest of the widget.

They are written for the same budget as the existing Matrix rain and Dot
Wave scenes -- a few hundred primitives a frame, batched into a handful of
painter calls, with anything static rendered once into a pixmap.

    Synthwave  retro perspective grid scrolling toward a banded sun
    Circuit    gold PCB traces with pulses of current running along them
    Plexus     drifting nodes joined by lines that fade with distance
    Warp       a starfield streaking past at hyperspace speed
    Ember      sparks rising from a bed of embers
"""

from __future__ import annotations

import bisect
import math
import random

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (QBrush, QColor, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPixmap, QPolygonF,
                         QRadialGradient)

TAU = math.tau

# ── Shared helpers ─────────────────────────────────────────────────

_SPRITES: dict = {}
_SPRITE_CACHE_MAX = 256


def glow_sprite(color: QColor, radius: float, dpr: float) -> QPixmap:
    """A soft glowing dot, rendered once per (colour, size) and blitted.

    A radial gradient per particle per frame is what makes a pure-QPainter
    particle system slow; a cached pixmap makes each one a single blit.
    """
    key = (color.rgb(), round(radius * 2) / 2, dpr)
    pm = _SPRITES.get(key)
    if pm is not None:
        return pm
    if len(_SPRITES) >= _SPRITE_CACHE_MAX:
        _SPRITES.clear()
    r = radius * 3
    size = int(math.ceil(2 * r + 2))
    pm = QPixmap(int(math.ceil(size * dpr)), int(math.ceil(size * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    sp = QPainter(pm)
    sp.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QPointF(size / 2, size / 2)
    g = QRadialGradient(c, r)
    g.setColorAt(0.0, QColor(color.red(), color.green(), color.blue(), 150))
    g.setColorAt(0.35, QColor(color.red(), color.green(), color.blue(), 60))
    g.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
    sp.setPen(Qt.PenStyle.NoPen)
    sp.setBrush(QBrush(g))
    sp.drawEllipse(c, r, r)
    sp.setBrush(QBrush(QColor(min(255, color.red() + 60),
                              min(255, color.green() + 60),
                              min(255, color.blue() + 60), 230)))
    sp.drawEllipse(c, radius, radius)
    sp.end()
    _SPRITES[key] = pm
    return pm


def _blit(p: QPainter, sprite: QPixmap, x: float, y: float, alpha: float = 1.0):
    half = sprite.width() / sprite.devicePixelRatio() / 2
    if alpha < 1.0:
        p.setOpacity(alpha)
        p.drawPixmap(QPointF(x - half, y - half), sprite)
        p.setOpacity(1.0)
    else:
        p.drawPixmap(QPointF(x - half, y - half), sprite)


def _lerp3(a, b, t: float) -> tuple[int, int, int]:
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


def _vignette(p: QPainter, w: int, h: int, strength: int = 90):
    vg = QRadialGradient(QPointF(w / 2, h / 2), max(w, h) * 0.72)
    vg.setColorAt(0.0, QColor(0, 0, 0, 0))
    vg.setColorAt(0.55, QColor(0, 0, 0, 0))
    vg.setColorAt(1.0, QColor(0, 0, 0, strength))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(vg))
    p.drawRect(0, 0, w, h)


class Background:
    """Base class: size tracking, a clock, and the two hooks subclasses fill."""

    key = ""

    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.Random()
        self._w = 0
        self._h = 0
        self.t = 0.0
        #: 0 = full strength .. 1 = calm.  The dashboard lays a dark scrim over
        #: a calm scene; a scene whose colours turn muddy when darkened (a
        #: yellow sun goes olive) shifts its palette by this much first.
        self.calm = 0.0

    def set_calm(self, amount: float):
        self.calm = max(0.0, min(1.0, float(amount)))

    # ── Lifecycle ──
    def reset(self, w: int, h: int):
        """(Re)seed for a canvas of *w* x *h*; called on theme switch and resize."""
        self._w, self._h = int(w), int(h)
        if self._w > 0 and self._h > 0:
            self._build()

    def tick(self, dt: float, w: int, h: int):
        if (int(w), int(h)) != (self._w, self._h):
            self.reset(w, h)
        self.t += dt
        if self._w > 0 and self._h > 0:
            self._advance(dt)

    def paint(self, p: QPainter, w: int, h: int, dpr: float = 1.0):
        if (int(w), int(h)) != (self._w, self._h):
            self.reset(w, h)
        if self._w > 0 and self._h > 0:
            self._paint(p, self._w, self._h, dpr)

    # ── Hooks ──
    def _build(self):
        pass

    def _advance(self, dt: float):
        pass

    def _paint(self, p: QPainter, w: int, h: int, dpr: float):
        raise NotImplementedError


# ── Synthwave ──────────────────────────────────────────────────────

class Synthwave(Background):
    """Outrun: a scrolling perspective grid under a striped sunset sun."""

    key = "synthwave"
    HORIZON = 0.60
    PINK = (255, 45, 149)
    CYAN = (34, 211, 238)
    STARS = 90
    GRID_COLS = 13          # vertical lines each side of centre
    GRID_ROWS = 12          # horizontal lines between horizon and bottom
    SCROLL = 0.28           # rows per second

    def _build(self):
        rng = self._rng
        self._stars = [(rng.random(), rng.random() * 0.85, rng.uniform(0.5, 1.4),
                        rng.random() * TAU) for _ in range(self.STARS)]
        # Two mountain ranges, as height profiles sampled every 16 px.
        self._ranges = []
        for base, amp, seed in ((0.06, 0.09, rng.random() * 9),
                                (0.02, 0.05, rng.random() * 9)):
            pts = []
            for i in range(self._w // 16 + 3):
                x = i * 16.0
                u = x / max(1, self._w)
                y = (base + amp * (0.55 + 0.45 * math.sin(u * 9.3 + seed))
                     * (0.6 + 0.4 * math.sin(u * 23.7 + seed * 1.7))
                     * (0.7 + 0.3 * math.sin(u * 51.1 + seed * 0.3)))
                pts.append((x, y))
            self._ranges.append(pts)

    def _paint(self, p: QPainter, w: int, h: int, dpr: float):
        t = self.t
        hy = h * self.HORIZON
        p.setPen(Qt.PenStyle.NoPen)

        # Sky
        sky = QLinearGradient(0.0, 0.0, 0.0, hy)
        sky.setColorAt(0.0, QColor(11, 2, 28))
        sky.setColorAt(0.65, QColor(38, 6, 60))
        sky.setColorAt(1.0, QColor(84, 14, 92))
        p.fillRect(QRectF(0, 0, w, hy + 1), QBrush(sky))

        # Stars, twinkling
        for fx, fy, r, ph in self._stars:
            a = int(110 + 95 * math.sin(t * 1.6 + ph))
            p.setBrush(QBrush(QColor(255, 235, 250, max(20, a))))
            p.drawEllipse(QPointF(fx * w, fy * hy), r, r)

        # Sun: glow, then a banded disc.  Calm, it sets: a yellow top only
        # turns olive under the dashboard's scrim, so it warms to pink first.
        calm = self.calm
        sr = min(w, h) * 0.21
        sx, sy = w / 2.0, hy - sr * 0.62
        glow = QRadialGradient(QPointF(sx, sy), sr * 2.4)
        glow.setColorAt(0.0, QColor(255, 90, 170, int(95 * (1.0 - 0.35 * calm))))
        glow.setColorAt(0.45, QColor(255, 60, 140, int(34 * (1.0 - 0.35 * calm))))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPointF(sx, sy), sr * 2.4, sr * 2.4)

        p.save()
        clip = QPainterPath()
        clip.addEllipse(QPointF(sx, sy), sr, sr)
        p.setClipPath(clip)
        disc = QLinearGradient(0.0, sy - sr, 0.0, sy + sr)
        disc.setColorAt(0.0, QColor(*_lerp3((255, 236, 110), (255, 150, 200), calm)))
        disc.setColorAt(0.45, QColor(*_lerp3((255, 120, 150), (240, 96, 178), calm)))
        disc.setColorAt(1.0, QColor(*_lerp3((214, 30, 130), (176, 34, 140), calm)))
        p.fillRect(QRectF(sx - sr, sy - sr, sr * 2, sr * 2), QBrush(disc))
        # Horizontal cuts, thicker toward the bottom, drifting downward
        band_h = sr / 7.0
        phase = (t * 14.0) % band_h
        y = sy - sr * 0.42 + phase
        k = 0
        while y < sy + sr:
            thick = 1.5 + k * 1.3
            p.fillRect(QRectF(sx - sr, y, sr * 2, thick), QColor(46, 6, 62))
            y += band_h
            k += 1
        p.restore()

        # Mountains on the horizon (behind the grid, in front of the sun)
        for i, pts in enumerate(self._ranges):
            poly = QPolygonF()
            poly.append(QPointF(-4, hy + 2))
            for x, yy in pts:
                poly.append(QPointF(x, hy - yy * h))
            poly.append(QPointF(w + 4, hy + 2))
            shade = (28, 6, 48) if i == 0 else (16, 3, 34)
            p.setBrush(QBrush(QColor(*shade)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(poly)
            rim = QPen(QColor(*self.PINK, 70 if i == 0 else 40), 1.0)
            p.setPen(rim)
            p.setBrush(Qt.BrushStyle.NoBrush)
            path = QPainterPath()
            path.moveTo(pts[0][0], hy - pts[0][1] * h)
            for x, yy in pts[1:]:
                path.lineTo(x, hy - yy * h)
            p.drawPath(path)

        # Ground
        p.setPen(Qt.PenStyle.NoPen)
        ground = QLinearGradient(0.0, hy, 0.0, float(h))
        ground.setColorAt(0.0, QColor(24, 4, 44))
        ground.setColorAt(1.0, QColor(8, 2, 20))
        p.fillRect(QRectF(0, hy, w, h - hy), QBrush(ground))

        # Grid: verticals converge on the vanishing point
        vx = w / 2.0
        span = h - hy
        spacing = w / 12.0
        verticals = QPainterPath()
        for k in range(-self.GRID_COLS, self.GRID_COLS + 1):
            xb = vx + k * spacing * 1.9
            verticals.moveTo(vx, hy)
            verticals.lineTo(xb, h + 2)
        # Horizontals bunch toward the horizon and scroll toward the viewer
        horizontals = []
        phase = (t * self.SCROLL) % 1.0
        for k in range(self.GRID_ROWS):
            u = (k + phase) / self.GRID_ROWS
            yy = hy + span * (u ** 2.6)
            horizontals.append((yy, u))

        p.setBrush(Qt.BrushStyle.NoBrush)
        for width, alpha in ((5.0, 18), (1.1, 118)):
            pen = QPen(QColor(*self.PINK, alpha), width)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen)
            p.drawPath(verticals)
        for yy, u in horizontals:
            a = int(26 + 124 * u)
            for width, alpha in ((4.0 + 3.0 * u, a // 5), (1.0 + 1.2 * u, a)):
                pen = QPen(QColor(*self.PINK, alpha), width)
                p.setPen(pen)
                p.drawLine(QPointF(0, yy), QPointF(w, yy))

        # Horizon line, cyan
        for width, alpha in ((6.0, 40), (1.5, 210)):
            p.setPen(QPen(QColor(*self.CYAN, alpha), width))
            p.drawLine(QPointF(0, hy), QPointF(w, hy))

        _vignette(p, w, h, 70)


# ── Circuit ────────────────────────────────────────────────────────

class Circuit(Background):
    """A printed circuit board: gold traces, with current pulsing along them."""

    key = "circuit"
    CELL = 26
    BOARD = (5, 10, 7)
    GOLD = (250, 204, 21)
    PULSE_SPEED = (70.0, 150.0)     # px / s
    TRAIL = 22.0                    # px of bright trail behind a pulse
    # Eight compass directions in angular order, so a "turn" is +/-1.
    DIRS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))

    def _build(self):
        rng = self._rng
        cell = self.CELL
        cols = self._w // cell + 2
        rows = self._h // cell + 2
        occupied: set = set()
        traces: list = []
        wanted = max(14, (cols * rows) // 34)
        for _ in range(wanted * 3):
            if len(traces) >= wanted:
                break
            cur = (rng.randrange(cols), rng.randrange(rows))
            if cur in occupied:
                continue
            d = rng.choice((0, 2, 4, 6))
            pts = [cur]
            occupied.add(cur)
            for _seg in range(rng.randint(3, 9)):
                if rng.random() < 0.55:
                    d = (d + rng.choice((-1, 1))) % 8
                dx, dy = self.DIRS[d]
                for _step in range(rng.randint(1, 5)):
                    nxt = (cur[0] + dx, cur[1] + dy)
                    if (nxt in occupied or not (0 <= nxt[0] < cols)
                            or not (0 <= nxt[1] < rows)):
                        break
                    occupied.add(nxt)
                    cur = nxt
                if pts[-1] != cur:
                    pts.append(cur)
            if len(pts) < 2:
                continue
            xy = [(x * cell + cell / 2.0, y * cell + cell / 2.0) for x, y in pts]
            cum = [0.0]
            for (x0, y0), (x1, y1) in zip(xy, xy[1:]):
                cum.append(cum[-1] + math.hypot(x1 - x0, y1 - y0))
            if cum[-1] < cell * 2:
                continue
            traces.append((xy, cum))
        self._traces = traces
        vias = []
        for xy, _cum in traces:
            if len(xy) > 3 and rng.random() < 0.6:
                vias.append(xy[rng.randrange(1, len(xy) - 1)])
        self._vias = vias
        n_pulses = min(max(6, len(traces) // 2), 44)
        self._pulses = [self._new_pulse(rng.random()) for _ in range(n_pulses)]
        self._layer = None
        self._layer_key = None

    def _new_pulse(self, s: float = 0.0) -> list:
        rng = self._rng
        i = rng.randrange(len(self._traces))
        return [i, s, rng.uniform(*self.PULSE_SPEED)]

    @staticmethod
    def _point_at(xy: list, cum: list, dist: float) -> tuple[float, float]:
        if dist <= 0:
            return xy[0]
        if dist >= cum[-1]:
            return xy[-1]
        i = bisect.bisect_right(cum, dist) - 1
        seg = cum[i + 1] - cum[i]
        u = (dist - cum[i]) / seg if seg > 0 else 0.0
        (x0, y0), (x1, y1) = xy[i], xy[i + 1]
        return (x0 + (x1 - x0) * u, y0 + (y1 - y0) * u)

    def _advance(self, dt: float):
        if not self._traces:
            return
        for pulse in self._pulses:
            xy, cum = self._traces[pulse[0]]
            pulse[1] += pulse[2] * dt / cum[-1]
            if pulse[1] > 1.0 + self.TRAIL / cum[-1]:
                pulse[:] = self._new_pulse(0.0)

    def _render_layer(self, w: int, h: int, dpr: float) -> QPixmap:
        pm = QPixmap(int(math.ceil(w * dpr)), int(math.ceil(h * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(QColor(*self.BOARD))
        lp = QPainter(pm)
        lp.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Faint solder-mask grid
        lp.setPen(QPen(QColor(255, 255, 255, 7), 1.0))
        cell = self.CELL
        for x in range(0, w + cell, cell):
            lp.drawLine(QPointF(x, 0), QPointF(x, h))
        for y in range(0, h + cell, cell):
            lp.drawLine(QPointF(0, y), QPointF(w, y))

        paths = QPainterPath()
        for xy, _cum in self._traces:
            paths.moveTo(*xy[0])
            for pt in xy[1:]:
                paths.lineTo(*pt)
        lp.setBrush(Qt.BrushStyle.NoBrush)
        for width, alpha in ((6.0, 10), (2.2, 30), (1.0, 46)):
            pen = QPen(QColor(*self.GOLD, alpha), width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            lp.setPen(pen)
            lp.drawPath(paths)

        # Pads at both ends of every trace, vias along the way
        lp.setPen(QPen(QColor(*self.GOLD, 80), 1.4))
        lp.setBrush(QBrush(QColor(*self.BOARD)))
        for xy, _cum in self._traces:
            for x, y in (xy[0], xy[-1]):
                lp.drawEllipse(QPointF(x, y), 4.5, 4.5)
        lp.setPen(Qt.PenStyle.NoPen)
        lp.setBrush(QBrush(QColor(*self.GOLD, 110)))
        for x, y in self._vias:
            lp.drawEllipse(QPointF(x, y), 2.2, 2.2)

        _vignette(lp, w, h, 110)
        lp.end()
        return pm

    def _paint(self, p: QPainter, w: int, h: int, dpr: float):
        key = (w, h, dpr)
        if self._layer is None or self._layer_key != key:
            self._layer = self._render_layer(w, h, dpr)
            self._layer_key = key
        p.drawPixmap(QPointF(0, 0), self._layer)
        if not self._traces:
            return

        p.setBrush(Qt.BrushStyle.NoBrush)
        sprite = glow_sprite(QColor(*self.GOLD), 2.6, dpr)
        trail_glow = QPen(QColor(*self.GOLD, 70), 6.0)
        trail_glow.setCapStyle(Qt.PenCapStyle.RoundCap)
        trail_core = QPen(QColor(255, 236, 150, 220), 2.0)
        trail_core.setCapStyle(Qt.PenCapStyle.RoundCap)
        point_at = self._point_at
        for idx, s, _speed in self._pulses:
            xy, cum = self._traces[idx]
            total = cum[-1]
            d = s * total
            if d < 0:
                continue
            pts = [point_at(xy, cum, d - k * self.TRAIL / 3.0) for k in range(4)]
            path = QPainterPath()
            path.moveTo(*pts[3])
            path.lineTo(*pts[2])
            path.lineTo(*pts[1])
            path.lineTo(*pts[0])
            p.setPen(trail_glow)
            p.drawPath(path)
            p.setPen(trail_core)
            p.drawPath(path)
            if d <= total:
                _blit(p, sprite, pts[0][0], pts[0][1])
        p.setPen(Qt.PenStyle.NoPen)


# ── Plexus ─────────────────────────────────────────────────────────

class Plexus(Background):
    """Drifting points, joined to their neighbours by lines that fade with distance."""

    key = "plexus"
    BG = (7, 5, 24)
    NODE = (192, 132, 252)
    LINK = (167, 139, 250)
    REACH = 150.0
    LEVELS = 8            # alpha buckets, one painter path each

    def _build(self):
        rng = self._rng
        n = max(40, min(100, (self._w * self._h) // 16000))
        self._nodes = []
        for _ in range(n):
            ang = rng.random() * TAU
            spd = rng.uniform(9.0, 26.0)
            self._nodes.append([rng.uniform(0, self._w), rng.uniform(0, self._h),
                                math.cos(ang) * spd, math.sin(ang) * spd,
                                rng.uniform(1.2, 2.8), rng.random() * TAU])
        self._bright = sorted(range(n), key=lambda i: -self._nodes[i][4])[:14]

    def _advance(self, dt: float):
        w, h = self._w, self._h
        m = 24.0
        for nd in self._nodes:
            nd[0] += nd[2] * dt
            nd[1] += nd[3] * dt
            if nd[0] < -m:
                nd[0], nd[2] = -m, abs(nd[2])
            elif nd[0] > w + m:
                nd[0], nd[2] = w + m, -abs(nd[2])
            if nd[1] < -m:
                nd[1], nd[3] = -m, abs(nd[3])
            elif nd[1] > h + m:
                nd[1], nd[3] = h + m, -abs(nd[3])

    def _paint(self, p: QPainter, w: int, h: int, dpr: float):
        t = self.t
        p.fillRect(0, 0, w, h, QColor(*self.BG))
        gx = w * (0.5 + 0.18 * math.sin(t * 0.11))
        gy = h * (0.45 + 0.14 * math.cos(t * 0.09))
        glow = QRadialGradient(QPointF(gx, gy), max(w, h) * 0.55)
        glow.setColorAt(0.0, QColor(120, 70, 220, 46))
        glow.setColorAt(0.5, QColor(80, 40, 160, 18))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawRect(0, 0, w, h)

        # Bucket nodes into cells one REACH wide, so each node is only
        # tested against its own and neighbouring cells.
        reach = self.REACH
        reach2 = reach * reach
        nodes = self._nodes
        cells: dict = {}
        for i, nd in enumerate(nodes):
            cells.setdefault((int(nd[0] // reach), int(nd[1] // reach)), []).append(i)
        levels = self.LEVELS
        paths = [QPainterPath() for _ in range(levels)]
        for (cx, cy), members in cells.items():
            for i in members:
                xi, yi = nodes[i][0], nodes[i][1]
                for ox in (-1, 0, 1):
                    for oy in (-1, 0, 1):
                        for j in cells.get((cx + ox, cy + oy), ()):
                            if j <= i:
                                continue
                            dx = nodes[j][0] - xi
                            dy = nodes[j][1] - yi
                            d2 = dx * dx + dy * dy
                            if d2 >= reach2:
                                continue
                            lvl = int((1.0 - math.sqrt(d2) / reach) * levels)
                            if lvl >= levels:
                                lvl = levels - 1
                            pth = paths[lvl]
                            pth.moveTo(xi, yi)
                            pth.lineTo(nodes[j][0], nodes[j][1])
        p.setBrush(Qt.BrushStyle.NoBrush)
        for lvl, pth in enumerate(paths):
            if pth.isEmpty():
                continue
            a = int(14 + 96 * (lvl + 0.5) / levels)
            p.setPen(QPen(QColor(*self.LINK, a), 0.8 + 0.5 * lvl / levels))
            p.drawPath(pth)

        p.setPen(Qt.PenStyle.NoPen)
        node_col = QColor(*self.NODE, 210)
        p.setBrush(QBrush(node_col))
        for nd in nodes:
            p.drawEllipse(QPointF(nd[0], nd[1]), nd[4], nd[4])
        sprite = glow_sprite(QColor(*self.NODE), 3.0, dpr)
        for i in self._bright:
            nd = nodes[i]
            a = 0.55 + 0.45 * math.sin(t * 1.3 + nd[5])
            _blit(p, sprite, nd[0], nd[1], a)


# ── Warp ───────────────────────────────────────────────────────────

class Warp(Background):
    """Stars streaking past the viewer, as seen from the bridge."""

    key = "warp"
    BG = (2, 4, 12)
    NEAR = (230, 244, 255)
    FAR = (56, 189, 248)
    COUNT = 340
    SPREAD = 0.55       # spawn half-width, as a fraction of the focal length
    FAR_STARS = 140
    SPEED = 0.30          # fraction of depth per second
    STREAK = 0.055        # depth behind the head that the streak covers
    BUCKETS = 6

    def _build(self):
        rng = self._rng
        sp = self.SPREAD
        self._stars = [[rng.uniform(-sp, sp), rng.uniform(-sp, sp),
                        rng.uniform(0.06, 1.0), rng.random()]
                       for _ in range(self.COUNT)]
        self._far = [(rng.random(), rng.random(), rng.uniform(0.4, 1.1),
                      rng.random() * TAU) for _ in range(self.FAR_STARS)]

    def _respawn(self, s: list):
        rng = self._rng
        sp = self.SPREAD
        s[0], s[1], s[2], s[3] = (rng.uniform(-sp, sp), rng.uniform(-sp, sp),
                                  1.0, rng.random())

    def _advance(self, dt: float):
        step = self.SPEED * dt
        for s in self._stars:
            s[2] -= step * (0.7 + 0.6 * s[3])
            if s[2] <= 0.035:
                self._respawn(s)

    def _paint(self, p: QPainter, w: int, h: int, dpr: float):
        t = self.t
        p.fillRect(0, 0, w, h, QColor(*self.BG))
        cx, cy = w / 2.0, h / 2.0
        pulse = 0.85 + 0.15 * math.sin(t * 0.9)
        core = QRadialGradient(QPointF(cx, cy), min(w, h) * 0.45 * pulse)
        core.setColorAt(0.0, QColor(56, 189, 248, 54))
        core.setColorAt(0.5, QColor(30, 90, 160, 20))
        core.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(core))
        p.drawRect(0, 0, w, h)

        # Distant stars that do not move: the backdrop the streaks fly over
        for fx, fy, r, ph in self._far:
            a = int(70 + 60 * math.sin(t * 1.1 + ph))
            p.setBrush(QBrush(QColor(200, 225, 255, a)))
            p.drawEllipse(QPointF(fx * w, fy * h), r, r)

        f = min(w, h) * 0.75
        buckets = self.BUCKETS
        paths = [QPainterPath() for _ in range(buckets)]
        heads = []
        streak = self.STREAK
        for x, y, z, hue in self._stars:
            hx = cx + x / z * f
            hy = cy + y / z * f
            if hx < -10 or hx > w + 10 or hy < -10 or hy > h + 10:
                continue
            tz = z + streak * (0.6 + 0.8 * (1.0 - z))
            tx = cx + x / tz * f
            ty = cy + y / tz * f
            b = int((1.0 - z) * buckets)
            if b >= buckets:
                b = buckets - 1
            paths[b].moveTo(tx, ty)
            paths[b].lineTo(hx, hy)
            if z < 0.22:
                heads.append((hx, hy, hue))
        p.setBrush(Qt.BrushStyle.NoBrush)
        # Calm, the near streaks are what still flash past the graph: they
        # lose most of their white and some of their width.
        bright = 1.0 - 0.5 * self.calm
        thick = 1.0 - 0.3 * self.calm
        for b, pth in enumerate(paths):
            if pth.isEmpty():
                continue
            u = (b + 0.5) / buckets
            col = _lerp3(self.FAR, self.NEAR, u * (1.0 - 0.4 * self.calm))
            pen = QPen(QColor(*col, int((60 + 195 * u ** 1.5) * bright)),
                       (0.7 + 2.4 * u) * thick)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawPath(pth)
        sprite = glow_sprite(QColor(*self.NEAR), 1.8, dpr)
        for hx, hy, hue in heads:
            _blit(p, sprite, hx, hy, (0.5 + 0.5 * hue) * bright)
        _vignette(p, w, h, 120)


# ── Ember ──────────────────────────────────────────────────────────

class Ember(Background):
    """Sparks rising from a bed of coals, flickering as they climb."""

    key = "ember"
    TOP = (12, 6, 4)
    BOTTOM = (46, 14, 8)
    RED = (220, 50, 40)
    ORANGE = (251, 146, 60)
    YELLOW = (255, 230, 140)

    def _build(self):
        rng = self._rng
        n = max(60, min(170, (self._w * self._h) // 8500))
        self._embers = [self._spawn(rng.uniform(-10, self._h)) for _ in range(n)]

    def _spawn(self, y: float | None = None) -> list:
        rng = self._rng
        return [rng.uniform(0, self._w),                   # x
                self._h + rng.uniform(0, 60) if y is None else y,
                rng.uniform(16.0, 58.0),                   # rise speed
                rng.uniform(1.3, 3.6),                     # radius
                rng.random() * TAU,                        # phase
                rng.random()]                              # heat 0..1

    def _advance(self, dt: float):
        t = self.t
        for e in self._embers:
            e[1] -= e[2] * dt
            e[0] += math.sin(t * 0.9 + e[4]) * 16.0 * dt
            if e[1] < -14:
                e[:] = self._spawn()

    def _paint(self, p: QPainter, w: int, h: int, dpr: float):
        t = self.t
        bg = QLinearGradient(0.0, 0.0, 0.0, float(h))
        bg.setColorAt(0.0, QColor(*self.TOP))
        bg.setColorAt(1.0, QColor(*self.BOTTOM))
        p.fillRect(QRectF(0, 0, w, h), QBrush(bg))

        flicker = (0.6 + 0.25 * math.sin(t * 3.1) + 0.15 * math.sin(t * 7.7 + 1.3))
        heat = QRadialGradient(QPointF(w / 2.0, h * 1.08), w * 0.75)
        heat.setColorAt(0.0, QColor(255, 120, 40, int(140 * flicker)))
        heat.setColorAt(0.35, QColor(200, 60, 30, int(70 * flicker)))
        heat.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(heat))
        p.drawRect(0, 0, w, h)

        hot = QColor(*self.YELLOW)
        warm = QColor(*self.ORANGE)
        cool = QColor(*self.RED)
        for x, y, _vy, r, ph, hue in self._embers:
            if y < -10 or y > h + 10:
                continue
            fl = 0.55 + 0.45 * math.sin(t * (2.0 + hue * 3.0) + ph)
            climb = y / h
            a = fl * (0.4 + 0.6 * climb)
            col = hot if hue > 0.8 else (warm if hue > 0.35 else cool)
            _blit(p, glow_sprite(col, r, dpr), x, y, a)
        _vignette(p, w, h, 70)


# ── Registry ───────────────────────────────────────────────────────

BACKGROUNDS: dict[str, type[Background]] = {
    cls.key: cls for cls in (Synthwave, Circuit, Plexus, Warp, Ember)
}


def make_background(key: str | None,
                    rng: random.Random | None = None) -> Background | None:
    """A fresh painter for *key*, or ``None`` if the theme has no
    module-level background (the built-in rain / wave / aurora scenes, or
    the plain ambient glow)."""
    cls = BACKGROUNDS.get(key or "")
    return cls(rng) if cls is not None else None
