"""
RPMote Dashboard — Premium glassmorphism network topology overview.
Features frosted glass panels, soft diffused glow accents, animated
data-flow particles, dynamic node scaling, and click-to-select tenants.
"""
import math
import random
from PyQt6.QtWidgets import QWidget, QSizePolicy, QMenu, QColorDialog, QInputDialog
from PyQt6.QtCore import Qt, QRectF, QPointF, QTimer, QElapsedTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor, QPainter, QPen, QBrush, QFont, QFontMetrics, QRadialGradient,
    QLinearGradient, QPainterPath, QPainterPathStroker, QConicalGradient,
    QCursor, QPolygonF, QPixmap
)

# Branch nodes are colour-coded by protocol using the same palette as the
# sidebar, so a host reads the same in both places.
from app.hardware_acceleration import canvas_base_class, is_gl_canvas, prepare_canvas
from app import icons
from app.side_panel import PROTOCOL_COLORS, protocol_tag
from app.restart import DIRECT_PROTOCOLS
from app.dashboard_skins import skin_for
from app.dashboard_backgrounds import make_background
from app.reachability import (
    STATE_CHECKING, STATE_COLORS, STATE_DEGRADED, STATE_DOWN, STATE_LABELS,
    STATE_SKIPPED, STATE_UNKNOWN, STATE_UP, describe as describe_status,
    rollup_state,
)
from app.wake_on_lan import is_wakeable
from app.relationships import (
    LENS_CREDENTIAL, LENS_LABELS, LENS_MACHINE, LENS_NONE, LENS_RESTART,
    LENS_SUBNET, LENSES, compute_relations,
)

# ── Link endpoints ─────────────────────────────────────────────────
#
# A visual link joins two *endpoints*, and an endpoint is either a tenant tile
# or an individual connection.  Both are encoded as a single prefixed string so
# one flat list keeps persisting the way it always has.  Links saved before
# connections were linkable are bare tenant names, which is why an unprefixed
# string still reads as a tenant.

EP_TENANT = "tenant:"

#: Marker key in a saved ``dashboard_node_positions`` dict.  Present (with
#: value "px") when the entries are logical-pixel offsets from the scene
#: centre; absent on vaults written before that, whose entries were
#: normalised by the widget's min(width, height) and therefore drifted
#: whenever the dashboard came back at a different size.
POSITION_UNITS = "__units__"
EP_CONN = "conn:"


def make_ep(kind: str, key: str) -> str:
    """Build an endpoint reference: ``make_ep("conn", "abc") -> "conn:abc"``."""
    prefix = EP_CONN if kind == "conn" else EP_TENANT
    return f"{prefix}{key}"


def parse_ep(ep: str) -> tuple[str, str]:
    """Split an endpoint into ``(kind, key)``.  Unprefixed means tenant."""
    text = str(ep or "")
    if text.startswith(EP_CONN):
        return "conn", text[len(EP_CONN):]
    if text.startswith(EP_TENANT):
        return "tenant", text[len(EP_TENANT):]
    return "tenant", text          # legacy: bare tenant name


def normalize_ep(ep: str) -> str:
    """Canonical prefixed form, so old and new records compare equal."""
    kind, key = parse_ep(ep)
    return make_ep(kind, key) if key else ""

#: Defaults for every user-tunable graph option.  Persisted to the vault under
#: ``dashboard_graph_settings`` so the look survives restarts.
DEFAULT_GRAPH_SETTINGS = {
    "layout_mode": "hub",         # "hub" (RPMote orb centre) | "free" (free-floating)
    "free_arrangement": "grid",   # "grid" | "scatter"   (free mode only)
    "show_branches": True,        # draw each connection branching off its tenant
    "branch_labels": "name",      # "name" | "name_host" | "host" | "none"
    "line_style": "curved",       # "curved" | "straight" | "elbow"
    "particles": True,            # animated dots travelling along the lines
    "animate": True,              # master switch for all motion
    "show_hub_spokes": True,      # orb -> tenant lines (hub mode only)
    "branch_spread": 1.0,         # multiplier on the branch orbit radius
    "node_scale": 1.0,            # multiplier on tenant tile size
    "tile_detail": "auto",        # "auto" | "full" | "compact" tenant tiles
    "cluster_spacing": 1.0,       # multiplier on the gap between tenants (free mode)
    "show_status": True,          # draw reachability dots when probing is on
    "status_rings": True,         # tint tile/pill borders by reachability too
    # "auto" | "always" | "never" — whether a tenant hides its machines until
    # it is opened.  Free-floating mode used to show every branch always, which
    # is unreadable once an estate runs to hundreds of connections.
    "collapse_tenants": "auto",
    # How loud an animated theme's scenery is behind the graph — see
    # BACKGROUND_LEVELS.
    "background": "calm",
}

#: How strongly an animated theme (Matrix, Aurora, Synthwave…) shows behind
#: the graph.  The scenes were drawn to be looked at, and at full strength
#: their streaks, traces and particles read as more lines and nodes.
#:   calm      dimmed and slowed, with a shade pooled under every tenant
#:             cluster, so the graph is what stands out (the default)
#:   balanced  half way
#:   vivid     the scene at full strength, as it always was
#:   plain     no scene at all: a still backdrop in the theme's colours
BACKGROUND_LEVELS = ("calm", "balanced", "vivid", "plain")
#: level -> (scrim alpha laid over the scene, scene speed, pool alpha,
#: how far a scene shifts its palette for the scrim -- see Background.calm)
_BACKGROUND_TUNING = {
    "calm": (150, 0.45, 115, 1.0),
    "balanced": (85, 0.75, 85, 0.5),
    "vivid": (0, 1.0, 0, 0.0),
    "plain": (0, 0.0, 0, 0.0),
}
#: The built-in scenes that are not module-level Background painters.
_BUILTIN_SCENES = ("matrix", "dotwave", "aurora")
#: Resting alpha of a tenant -> connection line, and of hub spokes.  Lines
#: carry a dark casing underneath (see _casing_pen), so they stay legible over
#: a busy scene without having to shout.
BRANCH_LINE_ALPHA = 92
FLOW_LINE_ALPHA = 96

#: Above this many connections, "auto" starts collapsing.  Below it nothing
#: changes for someone with a handful of servers.
COLLAPSE_AUTO_THRESHOLD = 40
COLLAPSE_MODES = ("auto", "always", "never")

LINE_STYLES = ("curved", "straight", "elbow")
TILE_DETAILS = ("auto", "full", "compact")
LINK_STYLES = ("solid", "dashed", "dotted")

#: Options that relocate nodes — changing one re-frames the view.
_REFRAMING_KEYS = frozenset({
    "layout_mode", "free_arrangement", "show_branches",
    "branch_spread", "node_scale", "cluster_spacing", "branch_labels",
    "tile_detail", "collapse_tenants",
})

#: Nominal branch-node metrics (logical px, before zoom).
BRANCH_H = 26.0
#: Screen px beyond the viewport a node may sit and still be painted, so
#: glows and curved edges never pop at the border.
VIEW_MARGIN = 64.0
#: How far a flow line can stray from its chord: the curved style bows up to
#: 18 px and the glow pen is ~6 px wide.  Used to fatten a segment's cull box.
SEGMENT_CULL_PAD = 24.0
#: Transparent border around a cached pill picture, so a 1.6 px status
#: border and its antialiasing never clip.
PILL_PIXMAP_MARGIN = 3.0
#: Zooming re-renders every visible pill at the new scale; bound the cache
#: so a long zoom session cannot grow it without limit.
PILL_PIXMAP_CACHE_MAX = 4096
BRANCH_W = 134.0
BRANCH_DOT_R = 5.0
BRANCH_FONT_PT = 9       # reference label size at zoom 1.0
BRANCH_FONT_MIN = 5      # smallest label we are willing to draw
#: Pill labels are set semi-bold: at 9pt on a moving backdrop the regular
#: weight thinned out to nothing.
BRANCH_FONT_WEIGHT = QFont.Weight.DemiBold
#: The dark rim and drop shadow every node wears so it lifts off a bright,
#: busy background (Circuit's traces, Synthwave's grid, Aurora's ribbons).
NODE_RIM = (2, 4, 10, 170)
NODE_SHADOW = (0, 0, 0, 95)
#: Below this scale a branch is drawn as a bare dot — a 3pt hostname is
#: noise, and dropping the text keeps a zoomed-out estate readable.
BRANCH_LABEL_MIN_SCALE = BRANCH_FONT_MIN / BRANCH_FONT_PT

#: Reachability dot drawn on a branch pill / tenant tile (logical px).
STATUS_DOT_R = 3.6
STATUS_TILE_DOT_R = 5.0

#: States worth drawing at all — "unknown" and "skipped" stay invisible so a
#: vault full of un-probed hosts does not turn into a wall of grey dots.
_VISIBLE_STATES = (STATE_UP, STATE_DEGRADED, STATE_DOWN, STATE_CHECKING)

#: How often the animation timer looks in while nobody can see the graph
#: (another tab in front, or the window minimised): a slow poll, so it
#: notices being shown again without animating for nobody.
IDLE_POLL_MS = 500

# ── Hover motion ───────────────────────────────────────────────────
#
# Each hovered endpoint carries one 0..1 progress that climbs over HOVER_IN_S
# and falls back over HOVER_OUT_S; the lift, bloom, border sweep and lit lines
# are all drawn from it frame by frame, so nothing about a hover is cached.

HOVER_IN_S = 0.22
HOVER_OUT_S = 0.30
#: How much a hovered node grows.  The bare dots of a zoomed-out estate grow
#: the most: they are the smallest target and say nothing until hovered.
HOVER_LIFT_TILE = 0.06
HOVER_LIFT_PILL = 0.12
HOVER_LIFT_DOT = 0.6
#: A ring spreads out from a node as the cursor lands on it.
RIPPLE_S = 0.7
#: How far everything a hover is not about fades back.  A tenant asks more of
#: the eye (a whole cluster) than a single machine does.
FOCUS_DIM_TENANT = 0.55
FOCUS_DIM_CONN = 0.4
FOCUS_IN_S = 0.25
FOCUS_OUT_S = 0.35
#: Hovered pills and tiles are drawn from pictures this much sharper than
#: the screen, so the lift does not blur their text.
HOVER_OVERSAMPLE = 1.25
#: The card beside a hovered node waits this long (so sweeping the cursor
#: across the graph does not flash a card per node), then fades and rises in.
#: Once one is showing, the next node's card comes straight up.
HOVER_CARD_DELAY_S = 0.32
HOVER_CARD_IN_S = 0.16
HOVER_CARD_OUT_S = 0.10
HOVER_CARD_MAX_W = 340


def _ease_out_back(t: float) -> float:
    """0 -> 1 with a small overshoot near the end: a springy arrival."""
    c1 = 1.70158
    c3 = c1 + 1.0
    u = t - 1.0
    return 1.0 + c3 * u * u * u + c1 * u * u


def _ease_out_cubic(t: float) -> float:
    u = 1.0 - t
    return 1.0 - u * u * u


def _smoothstep(t: float) -> float:
    return t * t * (3.0 - 2.0 * t)


class _HoverFocus:
    """What a hover is about this frame, and how far the rest fades back."""

    __slots__ = ("level", "kind", "tenant", "conn_id", "members", "dim")

    def __init__(self, level: float, kind: str, tenant: str,
                 conn_id: str | None, members: set, dim: float):
        self.level = level
        self.kind = kind            # "tenant" | "conn"
        self.tenant = tenant        # the hovered tenant, or the machine's owner
        self.conn_id = conn_id      # the hovered machine, for a "conn" focus
        self.members = members      # endpoints that stay at full strength
        self.dim = dim

    @property
    def faded(self) -> float:
        """Opacity of everything outside the focus."""
        return 1.0 - self.dim * self.level

    def fade(self, ep: str) -> float:
        return 1.0 if ep in self.members else self.faded

    def lights_line(self, tenant: str, conn_id: str) -> bool:
        """Is the tenant -> machine line from *tenant* to *conn_id* lit?"""
        if self.kind == "tenant":
            return tenant == self.tenant
        return conn_id == self.conn_id

#: Compact tenant tile: one row of name + count, no icon strip.
COMPACT_TILE_H = 46.0
COMPACT_TILE_MIN_W = 104.0
COMPACT_TILE_MAX_W = 250.0
COMPACT_TILE_FONT_PT = 11

# ── Spotlight search ───────────────────────────────────────────────

#: Characters that start a new "word" for scoring purposes.  A query is worth
#: more when it lands on one of these boundaries — "dc" should find *DC01* and
#: *fs-dc-02* before it finds *ol**dc**ache*.
_WORD_SEPS = " -_./:@\\|,()[]"

#: Connection fields a query is matched against, and what a hit in each is
#: worth.  Name and host are what a person actually remembers; tenant, protocol
#: and username are there so "fabrikam" or "rdp" still narrows the estate.
SEARCH_FIELDS = (("name", 1.0), ("host", 0.95), ("tenant", 0.7),
                 ("protocol", 0.55), ("username", 0.5))

#: Results listed under the find bar.  Everything that matched still lights up
#: on the canvas and is still reachable with the arrow keys — this only caps
#: how much of the estate the panel is willing to cover.
SEARCH_MAX_ROWS = 8


def fuzzy_match(query: str, text: str) -> float | None:
    """Score *text* against *query*, or ``None`` when it does not match at all.

    A subsequence match, ranked the way a person expects: an exact hit beats a
    prefix, a prefix beats a substring, a substring on a word boundary beats
    one buried mid-word, and anything contiguous beats characters scattered
    across the string.  Case-insensitive throughout.
    """
    q = (query or "").strip().lower()
    t = (text or "").strip().lower()
    if not q or not t:
        return None
    if q == t:
        return 1000.0
    if t.startswith(q):
        return 900.0 - min(len(t) - len(q), 90)
    pos = t.find(q)
    if pos >= 0:
        boundary = 60.0 if t[pos - 1] in _WORD_SEPS else 0.0
        return 700.0 + boundary - min(pos, 60) - min(len(t) - len(q), 60)

    score, cursor, streak = 0.0, 0, 0
    # A space typed between words stands for whatever separates them in
    # the name: "fab dc" finds FAB-DC01.
    for ch in q.replace(" ", ""):
        found = t.find(ch, cursor)
        if found < 0:
            return None
        if found == cursor and cursor > 0:
            streak += 1
            score += 8 + streak * 2      # reward contiguous runs
        else:
            streak = 0
            score += 2
        if found == 0 or t[found - 1] in _WORD_SEPS:
            score += 10                  # landed on a word boundary
        cursor = found + 1
    # Long strings match by accident more easily, so pay for the extra length.
    return max(score - min(len(t) / 4.0, 40.0), 1.0)


def match_record(query: str, record: dict, fields=SEARCH_FIELDS) -> float | None:
    """Best score of *query* against *record*'s weighted *fields*, or None.

    The whole query is tried against each field first. A query of several
    words that no single field holds — "contoso dc", a tenant and a machine —
    then matches when every word lands in some field, ranked below a match
    on one field.
    """
    def best_for(text: str) -> float | None:
        best = None
        for field, weight in fields:
            score = fuzzy_match(text, str(record.get(field, "") or ""))
            if score is not None:
                score *= weight
                best = score if best is None else max(best, score)
        return best

    whole = best_for(query)
    if whole is not None:
        return whole
    words = str(query or "").split()
    if len(words) < 2:
        return None
    total = 0.0
    for word in words:
        score = best_for(word)
        if score is None:
            return None
        total += score
    return total / len(words) * 0.85


# QOpenGLWidget when hardware acceleration is on, plain QWidget otherwise.
# Everything below paints through QPainter, which is engine-agnostic.
_CanvasBase = canvas_base_class()


_MINI_MONITOR: dict = {}


def _mini_monitor(p: QPainter) -> QPixmap:
    """The small monitor drawn under a tenant tile, once per pixel ratio."""
    device = p.device()
    dpr = float(device.devicePixelRatioF()) if device is not None else 1.0
    pm = _MINI_MONITOR.get(dpr)
    if pm is None:
        pm = _MINI_MONITOR[dpr] = icons.pixmap("monitor", 11, "#94a3b8", dpr)
    return pm


class DashboardWidget(_CanvasBase):
    """Premium glassmorphism network topology dashboard."""

    tenant_clicked = pyqtSignal(str)  # emits tenant name when a node is clicked
    node_positions_changed = pyqtSignal(dict)  # emits {tenant: [nx, ny]} after drag
    view_state_changed = pyqtSignal(dict)  # emits dashboard zoom/expand state
    connection_clicked = pyqtSignal(dict)      # single-click on a branch node
    connection_activated = pyqtSignal(dict)    # double-click on a branch node -> open it
    graph_settings_changed = pyqtSignal(dict)  # emits the full graph-settings dict
    tenant_links_changed = pyqtSignal(list)    # emits the custom tenant-link list
    conn_positions_changed = pyqtSignal(dict)  # emits {conn_id: [dx, dy]} after drag
    tenant_color_change_requested = pyqtSignal(str)  # ask the host to pick a colour
    new_connection_requested = pyqtSignal(str)  # emits tenant name
    link_mode_changed = pyqtSignal(bool)       # link-drawing armed/disarmed
    settings_dialog_requested = pyqtSignal()   # open the Graph Settings dialog
    restart_requested = pyqtSignal(dict)       # restart via the configured route
    restart_direct_requested = pyqtSignal(dict)  # restart over the connection itself
    check_now_requested = pyqtSignal(list)     # probe these connection ids now
    wake_requested = pyqtSignal(list)          # send Wake-on-LAN to these ids
    relation_lens_changed = pyqtSignal(str)    # which relationship lens is on
    ping_toggle_requested = pyqtSignal(bool)   # turn reachability probing on/off
    restart_setup_requested = pyqtSignal(str, str)  # ("tenant"|"connection", key)
    broadcast_tenant_requested = pyqtSignal(str)  # open this tenant as a grid

    def __init__(self, connections: list, tenant_colors: dict, parent=None):
        super().__init__(parent)
        prepare_canvas(self)
        # A GL canvas owns its whole framebuffer: nothing behind it shows
        # through, so the "default" theme (which only draws a glow over the
        # window) needs an explicit ground colour.
        self._gl_backed = is_gl_canvas(self)
        self._connections = connections
        self._tenant_colors = tenant_colors
        self._time = 0.0
        self._particles = []
        self._node_rects = []  # [(QRectF, tenant_name), ...]
        self._hovered_node = None
        #: endpoint -> hover progress 0..1 (see HOVER_IN_S), for every node
        #: that is hovered or still easing back down.
        self._hover_levels: dict[str, float] = {}
        #: Endpoints whose progress is climbing (springy) rather than falling.
        self._hover_rising: set = set()
        #: [(endpoint, started_at)] rings spreading out from just-hovered nodes.
        self._ripples: list = []
        #: Eased 0..1 "something is hovered", and what it is.
        self._focus_level = 0.0
        self._focus_ep: str | None = None
        #: endpoint -> (screen rect as drawn, colour) for this frame's nodes,
        #: so ripples land on the real outline.
        self._drawn_nodes: dict = {}
        #: The hover card: which node, when the cursor arrived, how faded in,
        #: and its last rendered picture.
        self._card_ep: str | None = None
        self._card_since = 0.0
        self._card_level = 0.0
        self._card_cache: tuple | None = None
        self._selected_node = None  # currently selected tenant name
        self._active_tenants = set()  # tenants with at least one open connection
        self._custom_positions = {}   # {tenant_name: (dx, dy)} logical px from scene centre
        # Pre-POSITION_UNITS saves, normalised by min(w, h).  Drawn as they
        # always were, and converted to pixel offsets the next time anything
        # is saved -- by then the widget has its real size.
        self._legacy_positions = {}   # {tenant_name: (nx, ny)}
        self._drag_target = None      # tenant name being dragged
        self._drag_offset = QPointF() # offset from node centre to click point
        self._drag_started = False    # True once the mouse has moved during drag
        self._zoom = 1.0
        # Wide enough that 'Fit to view' can frame a large MSP estate;
        # branch labels drop to dots below BRANCH_LABEL_MIN_SCALE.
        self._min_zoom = 0.25
        self._max_zoom = 2.2
        self._show_all_connections = False
        self._visible_tenants = set()  # shown when collapsed mode is active
        self._center_orb_rect = QRectF()

        # ── Customisable graph behaviour ───────────────────────────
        self._gs = dict(DEFAULT_GRAPH_SETTINGS)
        self._tenant_links: list[dict] = []   # purely-visual tenant <-> tenant links
        self._link_rects: list[tuple] = []    # [(QPainterPath, link_index), ...] hit areas
        self._selected_link: int | None = None
        self._hovered_link: int | None = None
        self._pending_link_labels: list = []  # drawn after the nodes
        self._link_mode = False               # click-A-then-B linking is armed
        self._link_source: str | None = None  # endpoint the pending link starts at
        self._link_cursor: QPointF | None = None   # live endpoint while dragging
        self._link_hover_target: str | None = None  # endpoint under the cursor
        #: {endpoint: (screen_x, screen_y, half_w, half_h)} rebuilt every paint,
        #: so link drawing, the live preview and hit-testing all agree.
        self._ep_geom: dict = {}

        # ── Reachability ──────────────────────────────────────────
        self._statuses: dict = {}             # {conn_id: probe info}
        self._ping_enabled = False

        # ── Relationship lenses ───────────────────────────────────
        #: Off by default — a lens answers a question, and drawing every
        #: relationship all the time at 300+ connections is a hairball.
        self._lens = LENS_NONE
        self._restart_servers: dict = {}
        self._restart_overrides: dict = {}
        #: {lens kind: RelationView}, cleared whenever the data behind it moves.
        self._relation_cache: dict = {}

        # ── Spotlight search ──────────────────────────────────────
        self._search_active = False
        self._search_query = ""
        self._search_hits: list = []       # ranked [{ep, kind, label, ...}]
        self._search_index = -1            # which hit the camera is on
        self._search_eps: set = set()      # every matched endpoint, for the scrim
        self._search_replace = False       # next keystroke replaces the query
        self._search_rect = QRectF()       # find-bar panel, for click routing
        self._search_row_rects: list = []  # [(QRectF, hit_index), ...]

        # ── Panning ────────────────────────────────────────────────
        self._pan = QPointF(0.0, 0.0)
        self._focused_tenant: str | None = None
        self._tenant_camera: tuple[float, QPointF] | None = None
        self._panning = False
        self._pan_anchor = QPointF()
        self._pan_origin = QPointF()
        self._pan_moved = False
        self._press_button = None

        # ── Connection (branch) nodes ─────────────────────────────
        self._conn_rects: list[tuple] = []      # [(QRectF, conn_dict, tenant), ...]
        self._conn_positions: dict = {}         # {conn_id: (dx, dy)} offset from tenant
        self._hovered_conn: str | None = None
        self._selected_conn: str | None = None
        self._highlighted_conn: str | None = None  # sidebar selection uses the hover look
        self._conn_drag_target: dict | None = None
        self._active_conn_ids: set = set()
        self._branch_cache: dict = {}
        # Performance caches
        self._layout_cache: dict | None = None
        self._layout_cache_key: tuple | None = None
        self._orb_glass_gradient = None          # cached center-orb glass gradient
        self._orb_glass_key = None               # (cx, cy, r, theme) it was built for
        self._node_gradient_cache: dict = {}     # keyed by (hex, cw, ch, is_sel, is_hov)
        # Text measured once per (label, size) rather than per node per frame
        # -- a QFont + QFontMetrics pair for 300 pills at 60 Hz is the single
        # largest slice of a frame.  Flushed with _branch_cache.
        self._pill_text_cache: dict = {}         # (fam, label, pt, text_w) -> (QFont, elided)
        self._tile_layout_cache: dict = {}       # (name, num, scale, compact, theme) -> layout
        self._tile_text_cache: dict = {}         # compact-tile chip/name text, per key
        # A pill's body (glass, border, protocol dot, label, resting status
        # dot) is the same picture every frame, so it is painted once into a
        # pixmap and blitted: one painter call per pill instead of a dozen.
        # Only the pulse halo and a breathing "checking" dot are drawn live.
        self._pill_pixmap_cache: dict = {}
        self._tile_pixmap_cache: dict = {}       # same idea for tenant tiles
        self._pen_cache: dict = {}               # (rgba, width, glow) -> QPen
        self._sprite_cache: dict = {}            # (rgb, radius, dpr) -> QPixmap
        #: tenant -> rolled-up state, valid for one painted frame.
        self._tenant_state_memo: dict = {}
        #: Screen-space rect anything must touch to be worth drawing this
        #: frame (the viewport plus a margin for glows).  Off-screen nodes
        #: still get their hit rects computed, just not painted.
        self._view_rect = QRectF()
        # Theme
        self._dashboard_theme = "default"
        self._theme: dict = {}                 # full theme dict for colour lookups
        #: Palette for nodes / pills / orb under the current theme.
        self._skin = skin_for(self._dashboard_theme)
        #: Module-level background painter, or None for the built-in scenes.
        self._background = None
        # Matrix rain state
        self._matrix_rain: list = []
        self._matrix_rain_w: int = 0
        # Dot wave state
        self._dw_count: float = 0.0
        # Aurora state
        self._aurora_time: float = 0.0
        self._MATRIX_CHARS = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            "!@#$%^&*<>[]{}|\\;:,.?/~`=+-_"
            "ｦｧｨｩｪｫｬｭｮｯｰｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ"
        )
        self.setMinimumSize(600, 400)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        # We drive the context menu ourselves from mouseReleaseEvent so that a
        # right-button *drag* pans instead of popping a menu.
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.PreventContextMenu)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # Motion is driven by wall-clock time (see _tick), so the tick rate
        # only sets smoothness: 60 Hz when the GPU is doing the painting,
        # 30 Hz when the CPU rasteriser would otherwise saturate a core.
        self.frame_interval_ms = 16 if self._gl_backed else 33
        self._clock = QElapsedTimer()
        self._clock.start()
        self._last_tick_ns = self._clock.nsecsElapsed()
        self._anim_timer = QTimer(self)
        self._anim_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim_timer.timeout.connect(self._tick)
        self._anim_timer.start(self.frame_interval_ms)

    def _flush_node_caches(self):
        """Forget every measured / pre-rendered node.  Cheap to rebuild
        lazily; called whenever fonts, theme, labels or status budgets move."""
        self._branch_cache.clear()
        self._pill_text_cache.clear()
        self._tile_layout_cache.clear()
        self._tile_text_cache.clear()
        self._pill_pixmap_cache.clear()
        self._tile_pixmap_cache.clear()
        self._sprite_cache.clear()

    def set_data(self, connections: list, tenant_colors: dict):
        self._connections = connections
        self._hover_levels.clear()
        self._hover_rising.clear()
        self._ripples = []
        self._focus_level = 0.0
        self._focus_ep = None
        self._hovered_node = None
        self._hovered_conn = None
        if self._highlighted_conn is not None and self._conn_by_id(self._highlighted_conn) is None:
            if self._selected_conn == self._highlighted_conn:
                self._selected_conn = None
            self._highlighted_conn = None
        self._tenant_state_memo.clear()
        self._tenant_colors = tenant_colors
        valid_tenants = {
            (c.get("tenant", "Default") or "Default")
            for c in self._connections
            if isinstance(c, dict)
        }
        self._visible_tenants = {t for t in self._visible_tenants if t in valid_tenants}
        # Drop state belonging to connections/tenants that no longer exist.  An
        # empty connection list is treated as "not loaded yet" rather than "the
        # user deleted everything", so a transient blank never wipes their work.
        if self._connections:
            live_ids = {str(c.get("id")) for c in self._connections
                        if isinstance(c, dict)}
            pruned_pos = {k: v for k, v in self._conn_positions.items()
                          if k in live_ids}
            if len(pruned_pos) != len(self._conn_positions):
                self._conn_positions = pruned_pos
                self.conn_positions_changed.emit(
                    {k: list(v) for k, v in self._conn_positions.items()})

            known = self._known_endpoints()
            kept = [l for l in self._tenant_links
                    if l.get("a") in known and l.get("b") in known]
            if len(kept) != len(self._tenant_links):
                self._tenant_links = kept
                self._selected_link = None
                self.tenant_links_changed.emit(self.get_tenant_links())
        self._layout_cache = None
        self._flush_node_caches()
        self._node_gradient_cache.clear()
        # Every relationship is derived from the records that just changed.
        self._relation_cache.clear()
        if self._search_active:
            # Results hold live connection dicts; re-rank so an edit or a
            # deletion can never leave the bar pointing at a stale one.
            self._recompute_search()
        if self._focused_tenant is not None:
            if self._focused_tenant in valid_tenants:
                self.focus_tenant(self._focused_tenant)
            else:
                self._restore_tenant_view()
        self.update()

    def set_active_tenants(self, tenants: set):
        """Mark tenants that have at least one open connection tab."""
        self._active_tenants = set(tenants or set())
        self.update()

    def set_active_connections(self, conn_ids: set):
        """Mark individual connections that currently have an open tab."""
        self._active_conn_ids = {str(c) for c in (conn_ids or set())}
        self.update()

    # ── Graph Settings ────────────────────────────────

    def get_graph_settings(self) -> dict:
        return dict(self._gs)

    def set_graph_settings(self, settings: dict, emit: bool = False):
        """Merge *settings* over the current graph options and repaint.

        Unknown keys are ignored so an older vault can never wedge the view.
        """
        if not isinstance(settings, dict):
            return
        changed = False
        for k, v in settings.items():
            if k not in DEFAULT_GRAPH_SETTINGS:
                continue
            if self._gs.get(k) != v:
                self._gs[k] = v
                changed = True
        if not changed:
            return
        # Free-floating mode shows the whole estate — there is no orb to expand
        # from, and with a modest number of tiles nothing needs hiding.  Once
        # collapsing is in play the tiles are still all drawn, so opening every
        # one of them would defeat the point rather than avoid a blank canvas.
        if self._gs["layout_mode"] == "free":
            if self.collapsing_active():
                self._show_all_connections = False
            else:
                self._show_all_connections = True
                self._visible_tenants = {
                    (c.get("tenant", "Default") or "Default")
                    for c in self._connections if isinstance(c, dict)
                }
        self._layout_cache = None
        self._flush_node_caches()
        self._node_gradient_cache.clear()
        # A change that moves everything deserves a re-frame, otherwise the user
        # is left staring at empty canvas while the graph sits off-screen.
        if emit and any(k in settings for k in _REFRAMING_KEYS):
            self.fit_to_view()
        self.update()
        if emit:
            self.graph_settings_changed.emit(self.get_graph_settings())

    def set_graph_option(self, key: str, value):
        """Change one option and persist it (convenience for menu actions)."""
        self.set_graph_settings({key: value}, emit=True)

    @property
    def is_free_mode(self) -> bool:
        return self._gs["layout_mode"] == "free"

    # ── Custom Tenant Links ──────────────────────────

    def get_tenant_links(self) -> list:
        return [dict(l) for l in self._tenant_links]

    def set_tenant_links(self, links: list):
        """Load saved visual links.  Malformed entries are skipped.

        Endpoints are normalised to their prefixed form, so a link saved before
        connections were linkable ("Contoso") and one saved after
        ("tenant:Contoso") are the same link.
        """
        clean = []
        for l in (links or []):
            if not isinstance(l, dict):
                continue
            a = normalize_ep(l.get("a", ""))
            b = normalize_ep(l.get("b", ""))
            if not a or not b or a == b:
                continue
            clean.append({
                "a": a,
                "b": b,
                "color": str(l.get("color", "") or ""),
                "style": (l.get("style") if l.get("style") in LINK_STYLES else "dashed"),
                "label": str(l.get("label", "") or ""),
            })
        # Saved links can outlive the nodes they point at (a tenant deleted
        # while another window held the vault, an imported settings blob, …).
        if self._connections:
            known = self._known_endpoints()
            pruned = [l for l in clean if l["a"] in known and l["b"] in known]
            if len(pruned) != len(clean):
                clean = pruned
                self._tenant_links = clean
                self._selected_link = None
                self.update()
                self.tenant_links_changed.emit(self.get_tenant_links())
                return
        self._tenant_links = clean
        self._selected_link = None
        self.update()

    def add_link(self, a: str, b: str) -> bool:
        """Link two endpoints — tenants, connections, or one of each.

        *a* and *b* are endpoint references (see :func:`make_ep`).  Returns
        False if the pair is degenerate or already linked.
        """
        a, b = normalize_ep(a), normalize_ep(b)
        if not a or not b or a == b:
            return False
        for l in self._tenant_links:
            if {l["a"], l["b"]} == {a, b}:
                return False
        self._tenant_links.append(
            {"a": a, "b": b, "color": "", "style": "dashed", "label": ""})
        self._selected_link = len(self._tenant_links) - 1
        self.update()
        self.tenant_links_changed.emit(self.get_tenant_links())
        return True

    def add_tenant_link(self, a: str, b: str) -> bool:
        """Link two tenants by name (kept for callers that predate endpoints)."""
        return self.add_link(make_ep("tenant", (a or "").strip()),
                             make_ep("tenant", (b or "").strip()))

    def add_connection_link(self, a_id: str, b_id: str) -> bool:
        """Link two connections by id."""
        return self.add_link(make_ep("conn", str(a_id or "").strip()),
                             make_ep("conn", str(b_id or "").strip()))

    def remove_tenant_link(self, index: int):
        if 0 <= index < len(self._tenant_links):
            del self._tenant_links[index]
            self._selected_link = None
            self._hovered_link = None
            self.update()
            self.tenant_links_changed.emit(self.get_tenant_links())

    def clear_tenant_links(self):
        if not self._tenant_links:
            return
        self._tenant_links = []
        self._selected_link = None
        self._hovered_link = None
        self.update()
        self.tenant_links_changed.emit(self.get_tenant_links())

    def set_link_mode(self, enabled: bool):
        """Arm/disarm click-A-then-B link drawing."""
        self._link_mode = bool(enabled)
        self._link_source = None
        self._link_cursor = None
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor if self._link_mode
                               else Qt.CursorShape.ArrowCursor))
        self.update()

    def is_link_mode(self) -> bool:
        return self._link_mode

    def begin_link_from(self, ep: str):
        """Arm link mode with *ep* already chosen as the first end.

        Picking the source from a menu scales to an estate where a "link to"
        submenu listing every machine would be unusable.
        """
        ep = normalize_ep(ep)
        if not ep:
            return
        self.set_link_mode(True)
        self._link_source = ep
        self._link_cursor = None
        self.link_mode_changed.emit(True)
        self.update()

    # ── Branch Node Positions ────────────────────────

    def set_conn_positions(self, positions: dict):
        """Load saved per-connection branch offsets (pixels, tenant-relative)."""
        self._conn_positions = {
            str(k): (float(v[0]), float(v[1]))
            for k, v in (positions or {}).items()
            if isinstance(v, (list, tuple)) and len(v) >= 2
        }
        self.update()

    # ── Reachability ─────────────────────────────────────────────

    def set_ping_enabled(self, enabled: bool):
        """Show/hide reachability decoration.  Probing itself lives elsewhere."""
        enabled = bool(enabled)
        if enabled == self._ping_enabled:
            return
        self._ping_enabled = enabled
        self._tenant_state_memo.clear()
        if not enabled:
            self._statuses = {}
        # Pill widths carry a status-dot allowance, so the layout that packed
        # them is no longer valid.
        self._flush_node_caches()
        self._layout_cache = None
        self.update()

    @property
    def is_ping_enabled(self) -> bool:
        return self._ping_enabled

    def set_conn_statuses(self, statuses: dict):
        """Replace the whole status map (one sweep's worth of results)."""
        self._statuses = {str(k): dict(v) for k, v in (statuses or {}).items()
                          if isinstance(v, dict)}
        self._tenant_state_memo.clear()
        self.update()

    def set_conn_status(self, conn_id: str, info: dict):
        """Update a single connection's status without disturbing the rest."""
        if not isinstance(info, dict):
            return
        self._statuses[str(conn_id)] = dict(info)
        self._tenant_state_memo.clear()
        self.update()

    def conn_status(self, conn_id: str) -> dict:
        return dict(self._statuses.get(str(conn_id), {}))

    def conn_state(self, conn_id: str) -> str:
        return str(self._statuses.get(str(conn_id), {}).get("state", STATE_UNKNOWN))

    def tenant_state(self, tenant: str) -> str:
        """Worst state among a tenant's connections — one dead box is not green."""
        memo = self._tenant_state_memo
        if not memo:
            # One pass over the estate for every tenant, instead of one pass
            # per tenant tile per frame.  Cleared whenever a status changes
            # and at the top of every paint, so it never outlives a frame.
            by_tenant: dict = {}
            for c in self._connections:
                if isinstance(c, dict):
                    by_tenant.setdefault(
                        c.get("tenant", "Default") or "Default", []
                    ).append(self.conn_state(c.get("id", "")))
            for tn, states in by_tenant.items():
                memo[tn] = rollup_state(states)
        state = memo.get(tenant)
        if state is None:
            state = memo[tenant] = rollup_state(())
        return state

    def _status_color(self, state: str) -> QColor | None:
        """Colour for a state, or None when it should not be drawn at all."""
        if not (self._ping_enabled and self._gs.get("show_status", True)):
            return None
        if state not in _VISIBLE_STATES:
            return None
        return QColor(STATE_COLORS.get(state, "#5b6072"))

    # ── Collapsing Tenants ─────────────────────────────────────────

    def collapsing_active(self) -> bool:
        """Whether tenants currently hide their machines until opened.

        Hub mode has always collapsed — a tenant that is not revealed is not
        drawn at all — so this only governs free-floating mode, where every
        branch used to be on screen no matter how large the estate.
        """
        if not self.is_free_mode:
            return False
        mode = str(self._gs.get("collapse_tenants", "auto"))
        if mode == "never":
            return False
        if mode == "always":
            return True
        return len(self._connections) >= COLLAPSE_AUTO_THRESHOLD

    def _tenant_expanded(self, tenant: str) -> bool:
        if not self.collapsing_active():
            return True
        return self._show_all_connections or tenant in self._visible_tenants

    def is_tenant_expanded(self, tenant: str) -> bool:
        return self._tenant_expanded(tenant)

    def set_tenant_expanded(self, tenant: str, expanded: bool):
        tenant = str(tenant or "").strip()
        if not tenant:
            return
        if expanded:
            self._visible_tenants.add(tenant)
        else:
            self._visible_tenants.discard(tenant)
            self._show_all_connections = False
            if self._focused_tenant == tenant:
                self._restore_tenant_view()
        self._layout_cache = None
        if expanded:
            self._highlighted_conn = None
            self.focus_tenant(tenant)
        self.update()
        self.view_state_changed.emit(self.get_view_state())

    def toggle_tenant_expanded(self, tenant: str):
        self.set_tenant_expanded(tenant, not self._tenant_expanded(tenant))

    def expand_all_tenants(self):
        """Open the estate; return whether a temporary camera was restored."""
        restored = self._restore_tenant_view()
        self._show_all_connections = True
        self._visible_tenants = self._all_tenants()
        self._layout_cache = None
        self.update()
        self.view_state_changed.emit(self.get_view_state())
        return restored

    def collapse_all_tenants(self):
        restored = self._restore_tenant_view()
        self._show_all_connections = False
        self._visible_tenants = set()
        self._layout_cache = None
        self.update()
        self.view_state_changed.emit(self.get_view_state())
        return restored

    # ── Relationship Lenses ────────────────────────────────────────

    def set_relation_lens(self, kind: str):
        """Show one kind of derived relationship, or ``none`` for the plain graph."""
        kind = str(kind or LENS_NONE)
        if kind not in LENSES:
            kind = LENS_NONE
        if kind == self._lens:
            return
        self._lens = kind
        if kind != LENS_NONE:
            # A lens draws between machines; on a collapsed graph there were
            # none on screen, so it looked like nothing happened.
            collapsed = (not self.is_free_mode and not self._show_all_connections
                         and not self._visible_tenants)
            hidden = self.is_free_mode and self.collapsing_active() and not self._show_all_connections
            if collapsed or hidden:
                self.expand_all_tenants()
                QTimer.singleShot(0, self.fit_to_view)
        self.update()
        self.relation_lens_changed.emit(kind)

    def relation_lens(self) -> str:
        return self._lens

    def clear_relation_lens(self) -> bool:
        """Turn the lens off.  True when there was one to turn off."""
        if self._lens == LENS_NONE:
            return False
        self.set_relation_lens(LENS_NONE)
        return True

    def set_restart_relations(self, servers: dict, overrides: dict):
        """The restart routing the lens draws arrows from."""
        self._restart_servers = dict(servers or {})
        self._restart_overrides = dict(overrides or {})
        self._relation_cache.pop(LENS_RESTART, None)
        if self._lens == LENS_RESTART:
            self.update()

    def relation_view(self, kind: str | None = None):
        """The computed relationships for *kind*, cached until the data moves."""
        kind = str(kind or self._lens)
        view = self._relation_cache.get(kind)
        if view is None:
            view = compute_relations(kind, self._connections,
                                     self._restart_servers,
                                     self._restart_overrides)
            self._relation_cache[kind] = view
        return view

    def _relationship_lines(self, conn_id: str) -> list:
        """Tooltip lines placing one connection in the estate.

        Reads the same cached views the lens draws from, so hovering three
        hundred nodes in a row costs one computation per lens, not one per
        node.
        """
        cid = str(conn_id or "")
        if not cid:
            return []
        lines = []
        for lens in (LENS_MACHINE, LENS_SUBNET, LENS_CREDENTIAL):
            for group in self.relation_view(lens).groups:
                if cid not in group.members:
                    continue
                others = len(group.members) - 1
                crossing = "  (across tenants)" if group.cross_tenant else ""
                lines.append(f"{LENS_LABELS[lens]}: {group.label} — with "
                             f"{others} other{'' if others == 1 else 's'}"
                             f"{crossing}")
        edges = self.relation_view(LENS_RESTART).edges
        for edge in edges:
            if edge.target == cid:
                lines.append(f"Restart route: via {edge.label}")
                break
        reboots = sum(1 for e in edges if e.source == cid)
        if reboots:
            lines.append(f"Restart server for {reboots} "
                         f"machine{'' if reboots == 1 else 's'}")
        return lines

    # ── Theme ──────────────────────────────────────────────────────

    def set_theme(self, theme: dict):
        """Switch the dashboard visual theme."""
        self._theme = theme                    # store for colour-aware rendering
        new_dt = theme.get("dashboard_theme", "default")
        if new_dt != self._dashboard_theme:
            self._dashboard_theme = new_dt
            self._skin = skin_for(new_dt)
            self._background = make_background(new_dt)
            if self._background is not None:
                self._background.reset(self.width(), self.height())
            # Flush all gradient caches so colours refresh immediately
            self._orb_glass_gradient = None
            self._node_gradient_cache.clear()
            self._flush_node_caches()
            self._layout_cache = None
            if self._dashboard_theme == "matrix":
                self._init_matrix_rain(self.width(), self.height())
            else:
                self._matrix_rain = []
                if self._dashboard_theme == "dotwave":
                    self._dw_count = 0.0
                elif self._dashboard_theme == "aurora":
                    self._aurora_time = 0.0
        self.update()

    # ── Background strength ────────────────────────────────────────

    def background_level(self) -> str:
        level = self._gs.get("background", "calm")
        return level if level in _BACKGROUND_TUNING else "calm"

    def _scene_active(self) -> bool:
        """True when the theme paints an animated scene behind the graph."""
        return (self._dashboard_theme in _BUILTIN_SCENES
                or self._background is not None)

    def _ground_color(self) -> QColor:
        """The theme's deepest colour, opaque: what scrims, pools and line
        casings are made of, so they darken without changing the mood."""
        key = self._theme.get("bg_primary", "#0a0b10")
        cached = getattr(self, "_ground_cache", None)
        if cached is not None and cached[0] == key:
            return cached[1]
        color = QColor(key) if isinstance(key, str) and not key.startswith("rgba") else None
        if color is None or not color.isValid():
            color = QColor(10, 11, 16)
        color.setAlpha(255)
        self._ground_cache = (key, color)
        return color

    def _draw_scene(self, p, w: int, h: int):
        """The theme's backdrop, at the strength the user picked."""
        level = self.background_level()
        if not self._scene_active():
            if level != "plain":
                self._draw_ambient_glow(p, w, h)
            return
        if level == "plain":
            self._draw_plain_backdrop(p, w, h)
            return
        if self._dashboard_theme == "matrix":
            self._draw_matrix_rain(p, w, h)
        elif self._dashboard_theme == "dotwave":
            self._draw_dotwave(p, w, h)
        elif self._dashboard_theme == "aurora":
            self._draw_aurora(p, w, h)
        else:
            self._background.set_calm(_BACKGROUND_TUNING[level][3])
            self._background.paint(p, w, h, float(self.devicePixelRatioF()))
        scrim = _BACKGROUND_TUNING[level][0]
        if scrim:
            ground = self._ground_color()
            p.fillRect(0, 0, w, h, QColor(ground.red(), ground.green(),
                                          ground.blue(), scrim))

    def _draw_plain_backdrop(self, p, w: int, h: int):
        """No scene: the theme's ground with one still glow of its accent."""
        ground = self._ground_color()
        p.fillRect(0, 0, w, h, ground)
        accent = (QColor(*self._skin.accent) if self._skin.accent is not None
                  else QColor(self._theme.get("accent", "#22d3ee")))
        g = QRadialGradient(QPointF(w / 2, h * 0.45), max(w, h) * 0.6)
        g.setColorAt(0, QColor(accent.red(), accent.green(), accent.blue(), 26))
        g.setColorAt(0.6, QColor(accent.red(), accent.green(), accent.blue(), 8))
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawRect(0, 0, w, h)

    def _pool_sprite(self, alpha: int) -> QPixmap:
        """A soft disc of the ground colour, rendered once and stretched."""
        ground = self._ground_color()
        key = ("pool", ground.rgb(), alpha)
        pm = self._sprite_cache.get(key)
        if pm is not None:
            return pm
        size = 128
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        sp = QPainter(pm)
        sp.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = QRadialGradient(QPointF(size / 2, size / 2), size / 2)
        r, gg, b = ground.red(), ground.green(), ground.blue()
        g.setColorAt(0.0, QColor(r, gg, b, alpha))
        g.setColorAt(0.55, QColor(r, gg, b, int(alpha * 0.78)))
        g.setColorAt(1.0, QColor(r, gg, b, 0))
        sp.setPen(Qt.PenStyle.NoPen)
        sp.setBrush(QBrush(g))
        sp.drawEllipse(QRectF(0, 0, size, size))
        sp.end()
        self._sprite_cache[key] = pm
        return pm

    def _draw_cluster_pools(self, p, positions: dict, branches: dict,
                            tenants: dict, node_scale: float):
        """Shade the scene under each tenant and its machines.

        The scenery stays visible around the edges of the canvas, but where
        the graph is, it sits on a quiet patch of the theme's own colour."""
        if not self._scene_active():
            return
        alpha = _BACKGROUND_TUNING[self.background_level()][2]
        if not alpha:
            return
        sprite = self._pool_sprite(alpha)
        z = self._zoom
        for tn, (tx, ty) in positions.items():
            sx, sy = self._to_screen(tx, ty)
            tw, th = self._tile_size(tn, len(tenants.get(tn, ())))
            radius = math.hypot(tw, th) * node_scale * 0.62
            for _conn, bx, by in branches.get(tn, ()):
                reach = math.hypot(bx - tx, by - ty) * z + 46.0 * node_scale
                if reach > radius:
                    radius = reach
            radius *= 1.12
            rect = QRectF(sx - radius, sy - radius, radius * 2, radius * 2)
            if self._view_rect.intersects(rect):
                p.drawPixmap(rect, sprite, QRectF(sprite.rect()))

    # ── Matrix Rain ────────────────────────────────────────────────

    def _init_matrix_rain(self, w: int, h: int):
        """Build per-column state for the digital rain animation."""
        if w <= 0 or h <= 0:
            return
        col_w = 16          # pixels per column
        char_h = 16         # pixels per character row
        num_cols = max(1, w // col_w)
        self._matrix_rain = []
        self._matrix_rain_w = w
        for i in range(num_cols):
            length = random.randint(10, 30)
            # start scattered: some above screen, some mid-fall
            start_y = random.uniform(-length * char_h, h)
            self._matrix_rain.append({
                "x":      i * col_w + col_w // 2,
                "y":      start_y,
                "speed":  random.uniform(55, 160),   # px / sec
                "chars":  [random.choice(self._MATRIX_CHARS) for _ in range(length)],
                "length": length,
                "glitch": random.uniform(0.05, 0.4), # sec until next char swap
            })

    def _tick_matrix_rain(self, dt: float, h: int):
        """Advance rain positions and randomly mutate characters (glitch effect)."""
        char_h = 16
        for col in self._matrix_rain:
            col["y"] += col["speed"] * dt
            col["glitch"] -= dt
            if col["glitch"] <= 0:
                idx = random.randint(0, col["length"] - 1)
                col["chars"][idx] = random.choice(self._MATRIX_CHARS)
                col["glitch"] = random.uniform(0.04, 0.35)
            # Reset column once its tail scrolls off the bottom
            tail_y = col["y"] - col["length"] * char_h
            if tail_y > h:
                col["y"]     = random.uniform(-col["length"] * char_h, -char_h)
                col["speed"] = random.uniform(55, 160)
                col["chars"] = [random.choice(self._MATRIX_CHARS) for _ in range(col["length"])]

    # ── Custom Position Helpers ────────────────────────────────────

    def set_node_positions(self, positions: dict):
        """Load saved positions.  *positions* maps tenant name → [x, y].

        With ``POSITION_UNITS == "px"`` those are logical-pixel offsets from
        the scene centre and land exactly where they were dropped, whatever
        size the window opens at.  Without the marker they are the old
        min(w, h)-normalised values, kept on the side until converted.
        """
        entries = {
            str(k): (float(v[0]), float(v[1]))
            for k, v in (positions or {}).items()
            if k != POSITION_UNITS and isinstance(v, (list, tuple)) and len(v) >= 2
        }
        if (positions or {}).get(POSITION_UNITS) == "px":
            self._custom_positions, self._legacy_positions = entries, {}
        else:
            self._custom_positions, self._legacy_positions = {}, entries
        self._layout_cache = None
        self.update()

    def has_custom_position(self, tenant: str) -> bool:
        return tenant in self._custom_positions or tenant in self._legacy_positions

    def _pinned_position(self, tenant: str):
        """Logical (x, y) of a hand-placed tenant, or None if it floats."""
        if tenant in self._custom_positions:
            cx, cy = self._scene_center()
            dx, dy = self._custom_positions[tenant]
            return cx + dx, cy + dy
        if tenant in self._legacy_positions:
            return self._legacy_denorm(*self._legacy_positions[tenant])
        return None

    def _pin(self, tenant: str, px: float, py: float):
        """Pin a tenant at a logical position."""
        cx, cy = self._scene_center()
        self._custom_positions[tenant] = (px - cx, py - cy)
        self._legacy_positions.pop(tenant, None)

    def _unpin(self, tenant: str):
        self._custom_positions.pop(tenant, None)
        self._legacy_positions.pop(tenant, None)

    def _convert_legacy_positions(self):
        """Turn old normalised entries into pixel offsets at the current size."""
        for tn, (nx, ny) in list(self._legacy_positions.items()):
            self._pin(tn, *self._legacy_denorm(nx, ny))
        self._legacy_positions = {}

    def _position_snapshot(self) -> dict:
        """Everything pinned, in the form that goes into the vault."""
        self._convert_legacy_positions()
        out = {POSITION_UNITS: "px"}
        out.update({k: [v[0], v[1]] for k, v in self._custom_positions.items()})
        return out

    def _freeze_tenant_positions(self):
        """Pin every tenant where it is drawn right now.

        Called when a drag starts: the user is arranging the graph by hand,
        so the tiles they did *not* touch must stop following the automatic
        layout -- which shifts with the window size, the sidebar, the tenant
        count and the expansion state -- or they drift around the ones that
        were placed on purpose.
        """
        for ep, (x, y, _w, _h) in self._layout_nodes().items():
            kind, name = parse_ep(ep)
            if kind == "tenant" and not self.has_custom_position(name):
                self._pin(name, x, y)
        self._convert_legacy_positions()

    def _freeze_branch_positions(self, tenant: str):
        """Pin every branch of one tenant where it is drawn right now, for the
        same reason as _freeze_tenant_positions: moving one branch must not
        let its siblings reflow later."""
        nodes = self._layout_nodes()
        anchor = nodes.get(make_ep("tenant", tenant))
        if anchor is None:
            return
        tx, ty = anchor[0], anchor[1]
        for conn in self._connections:
            if not isinstance(conn, dict):
                continue
            if (conn.get("tenant", "Default") or "Default") != tenant:
                continue
            cid = str(conn.get("id", ""))
            node = nodes.get(make_ep("conn", cid)) if cid else None
            if node is not None and cid not in self._conn_positions:
                self._conn_positions[cid] = (node[0] - tx, node[1] - ty)

    # ── Coordinate Spaces ─────────────────────────────
    #
    #  logical  ── zoom about the scene centre, then + pan ──>  screen
    #
    #  The scene centre stays at (w/2, h/2-50) in *both* layout modes so that
    #  positions the user has dragged survive a hub <-> free-float switch.

    def _scene_center(self) -> tuple[float, float]:
        return self.width() / 2, self.height() / 2 - 50

    def _to_screen(self, px: float, py: float) -> tuple[float, float]:
        # Inlined _scene_center(): this runs for every node and edge, every
        # frame, and the extra call and tuple showed up in profiles.
        cx = self.width() * 0.5
        cy = self.height() * 0.5 - 50
        z = self._zoom
        pan = self._pan
        return (cx + (px - cx) * z + pan.x(),
                cy + (py - cy) * z + pan.y())

    def _to_logical(self, sx: float, sy: float) -> tuple[float, float]:
        cx, cy = self._scene_center()
        z = max(self._zoom, 0.001)
        return (cx + (sx - self._pan.x() - cx) / z,
                cy + (sy - self._pan.y() - cy) / z)

    def _legacy_denorm(self, nx: float, ny: float) -> tuple[float, float]:
        """Old saved form -> logical pixels: centre-relative, scaled by min(w, h)."""
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2 - 50
        scale = min(w, h)
        return cx + nx * scale, cy + ny * scale

    # ── Panning ───────────────────────────────────

    def pan_by(self, dx: float, dy: float):
        self._pan = QPointF(self._pan.x() + dx, self._pan.y() + dy)
        self.update()

    def reset_pan(self):
        self._restore_tenant_view()
        if self._pan.x() == 0 and self._pan.y() == 0:
            return
        self._pan = QPointF(0.0, 0.0)
        self.update()
        self.view_state_changed.emit(self.get_view_state())

    def reset_view(self):
        """Recentre and restore 100% zoom in one go."""
        self._restore_tenant_view()
        self._pan = QPointF(0.0, 0.0)
        self._zoom = 1.0
        self.update()
        self.view_state_changed.emit(self.get_view_state())

    def fit_to_view(self):
        """Zoom/pan so every visible node fits on screen."""
        self._restore_tenant_view()
        bounds = self._content_bounds()
        if bounds is None:
            self.reset_view()
            return
        self._fit_bounds(bounds, self._min_zoom)
        self.update()
        self.view_state_changed.emit(self.get_view_state())

    def _fit_bounds(self, bounds: QRectF, minimum_zoom: float):
        """Frame logical bounds by changing only the camera."""
        w, h = max(self.width(), 1), max(self.height(), 1)
        margin = 60.0
        bw = max(bounds.width(), 1.0)
        bh = max(bounds.height(), 1.0)
        z = min(max(w - 2 * margin, 1) / bw,
                max(h - 2 * margin, 1) / bh)
        self._zoom = max(minimum_zoom, min(self._max_zoom, z))
        cx, cy = self._scene_center()
        bc = bounds.center()
        # Place the content centre at the widget centre.
        self._pan = QPointF(
            (w / 2) - (cx + (bc.x() - cx) * self._zoom),
            (h / 2) - (cy + (bc.y() - cy) * self._zoom),
        )

    def focus_tenant(self, tenant: str):
        """Temporarily frame a tenant and all its visible connection nodes."""
        bounds = self._content_bounds(tenant)
        if bounds is None:
            return
        if self._tenant_camera is None:
            self._tenant_camera = (self._zoom, QPointF(self._pan))
        self._focused_tenant = tenant
        # A hand-placed branch can be far beyond the normal zoom range.  The
        # temporary view must still include it, without moving any nodes.
        self._fit_bounds(bounds, 0.001)
        self.update()

    def _restore_tenant_view(self) -> bool:
        if self._tenant_camera is None:
            return False
        self._zoom, pan = self._tenant_camera
        self._pan = QPointF(pan)
        self._tenant_camera = None
        self._focused_tenant = None
        self._highlighted_conn = None
        self.update()
        return True

    def _layout_nodes(self, tenant: str | None = None) -> dict:
        """``{endpoint: (x, y, w, h)}`` in logical coords for every laid-out node.

        Computed straight from the layout rather than read back off the last
        paint, so callers such as fit_to_view() and the spotlight search work
        before the first render and right after an option change.
        With *tenant*, return only that cluster from the same estate layout.
        """
        tenants: dict = {}
        for conn in self._connections:
            if not isinstance(conn, dict):
                continue
            tenants.setdefault(conn.get("tenant", "Default") or "Default", []).append(conn)
        if not tenants:
            return {}

        cx, cy = self._scene_center()
        free = self.is_free_mode
        if free or self._show_all_connections:
            visible = list(tenants.keys())
        else:
            visible = [t for t in tenants if t in self._visible_tenants]
        if not visible:
            return {}

        positions = dict(self._compute_layout(
            visible, cx, cy, self.width(), self.height(), tenants))
        for tn in visible:
            pinned = self._pinned_position(tn)
            if pinned is not None:
                positions[tn] = pinned

        ns = float(self._gs["node_scale"])
        out: dict = {}
        for tn, (tx, ty) in positions.items():
            if tenant is not None and tn != tenant:
                continue
            cw, ch = self._tile_size(tn, len(tenants[tn]))
            # A wrapped tenant name makes a full tile taller than _node_size
            # reports, so pad the height or fit_to_view clips it.  Compact tiles
            # are a fixed single row and need no allowance.
            grow = 0.0 if self._tile_compact() else 34.0
            out[make_ep("tenant", tn)] = (tx, ty, cw * ns, ch * ns + grow)
            if self._gs["show_branches"] and self._tenant_expanded(tn):
                start = (math.atan2(ty - cy, tx - cx) if not free else -math.pi / 2)
                for conn, bx, by in self._branch_positions(
                        tn, tenants[tn], tx, ty, cw * ns, ch * ns, start):
                    cid = str(conn.get("id", ""))
                    # An id-less connection still occupies space, so it keeps a
                    # slot here (under a throwaway key) or fit_to_view would
                    # frame a box too small to hold it.
                    key = make_ep("conn", cid) if cid else f"anon:{tn}:{len(out)}"
                    out[key] = (bx, by,
                                self._branch_logical_width(conn), BRANCH_H)
        return out

    def _layout_points(self) -> list:
        """Every laid-out node as (x, y, w, h) in logical coordinates."""
        pts = list(self._layout_nodes().values())
        if not pts:
            return []
        if not self.is_free_mode:
            cx, cy = self._scene_center()
            pts.append((cx, cy, 120.0, 120.0))
        return pts

    def _content_bounds(self, tenant: str | None = None) -> QRectF | None:
        """Logical bounds of the estate, or just one tenant and its branches."""
        pts = (self._layout_points() if tenant is None
               else list(self._layout_nodes(tenant).values()))
        if not pts:
            return None
        # Each node contributes its own half-extents; a single global max would
        # inflate the box and leave the graph looking lost in empty space.
        left = min(x - rw / 2 for x, y, rw, rh in pts)
        right = max(x + rw / 2 for x, y, rw, rh in pts)
        top = min(y - rh / 2 for x, y, rw, rh in pts)
        bottom = max(y + rh / 2 for x, y, rw, rh in pts)
        return QRectF(left, top, right - left, bottom - top)

    #: Emitted after a resize, so overlays (the Overview card) can re-dock.
    resized = pyqtSignal()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()
        self._layout_cache = None
        self._orb_glass_gradient = None
        self._node_gradient_cache.clear()
        if self._focused_tenant is not None:
            self.focus_tenant(self._focused_tenant)
        if self._dashboard_theme == "matrix" and self.width() != self._matrix_rain_w:
            self._init_matrix_rain(self.width(), self.height())

    def _compute_layout(self, tenant_list: list, cx: float, cy: float,
                        w: int, h: int, tenants: dict) -> dict:
        """Tenant node positions for the active layout mode.  Result is cached."""
        cache_key = (
            tuple(tenant_list), w, h,
            self._gs["layout_mode"], self._gs["free_arrangement"],
            self._gs["show_branches"], round(self._gs["branch_spread"], 3),
            round(self._gs["node_scale"], 3), round(self._gs["cluster_spacing"], 3),
            tuple(len(tenants.get(t, ())) for t in tenant_list),
        )
        if self._layout_cache_key == cache_key and self._layout_cache is not None:
            return self._layout_cache

        if self._gs["layout_mode"] == "free":
            positions = self._compute_free_layout(tenant_list, cx, cy, w, h, tenants)
        else:
            positions = self._compute_hub_layout(tenant_list, cx, cy, w, h, tenants)

        self._layout_cache = positions
        self._layout_cache_key = cache_key
        return positions

    # ── Free-Floating Layout ─────────────────────────

    def _cluster_radius(self, tenant: str, conns: list) -> float:
        """Radius a tenant needs to itself *plus* its branch ring(s)."""
        cw, ch = self._tile_size(tenant, len(conns))
        cw *= self._gs["node_scale"]
        ch *= self._gs["node_scale"]
        base = math.hypot(cw, ch) / 2
        if not self._gs["show_branches"] or not conns:
            return base
        if not self._tenant_expanded(tenant):
            # A collapsed tenant needs room for its tile and nothing else —
            # reserving space for branches it is not drawing is what would
            # leave a fully collapsed estate scattered across a huge canvas.
            return base
        slot = self._branch_slot(conns)
        rings = self._branch_rings(len(conns), cw, ch, slot)
        outer = rings[-1][0] if rings else base
        return max(base, outer + max(BRANCH_H, slot * 0.5) / 2 + 8)

    def _compute_free_layout(self, tenant_list: list, cx: float, cy: float,
                             w: int, h: int, tenants: dict) -> dict:
        """Tile the tenants across an open canvas — no hub, nothing collapsed.

        Cells are sized by the widest cluster so branch rings never collide,
        and the whole block is centred on the scene origin.  The block may be
        larger than the viewport; that is what right-drag panning is for.
        """
        n = len(tenant_list)
        positions: dict = {}
        if n == 0:
            return positions

        radii = {t: self._cluster_radius(t, tenants.get(t, [])) for t in tenant_list}
        gap = 46.0 * max(self._gs["cluster_spacing"], 0.2)
        cell = max(radii.values()) * 2 + gap

        if n == 1:
            positions[tenant_list[0]] = (cx, cy)
            return positions

        # Column count that best matches the viewport aspect ratio.
        aspect = max(w, 1) / max(h, 1)
        cols = max(1, min(n, int(round(math.sqrt(n * aspect)))))
        rows = math.ceil(n / cols)

        total_w = cols * cell
        total_h = rows * cell
        x0 = cx - total_w / 2 + cell / 2
        y0 = cy - total_h / 2 + cell / 2

        scatter = self._gs["free_arrangement"] == "scatter"
        for i, tn in enumerate(tenant_list):
            r, c = divmod(i, cols)
            # Centre the final (possibly short) row.
            row_count = min(cols, n - r * cols)
            row_x0 = x0 + (cols - row_count) * cell / 2
            px = row_x0 + c * cell
            py = y0 + r * cell
            if scatter:
                # Deterministic jitter keyed off the tenant name — organic but
                # stable across repaints, resizes and restarts.
                seed = 0
                for ch_ in tn:
                    seed = (seed * 131 + ord(ch_)) & 0xFFFFFFFF
                jx = ((seed & 0xFFFF) / 65535.0 - 0.5)
                jy = (((seed >> 16) & 0xFFFF) / 65535.0 - 0.5)
                slack = max(cell - radii[tn] * 2 - 8, 0.0)
                px += jx * slack
                py += jy * slack
            positions[tn] = (px, py)
        return positions

    def tidy_layout(self) -> int:
        """Snap hand-dragged tenants onto the natural grid, keeping their order.

        Not the same as resetting positions: the arrangement the user built —
        this customer top-left, that one bottom-right — is preserved by sorting
        tiles by where they currently sit and handing out grid slots in that
        order.  What goes away is the overlap and the near-misses, which is the
        part nobody arranged on purpose.

        Returns how many tiles moved.
        """
        if not self.is_free_mode:
            return 0
        tenants: dict = {}
        for conn in self._connections:
            if isinstance(conn, dict):
                tenants.setdefault(
                    conn.get("tenant", "Default") or "Default", []).append(conn)
        names = list(tenants)
        if len(names) < 2:
            return 0

        cx, cy = self._scene_center()
        slots = self._compute_free_layout(names, cx, cy,
                                          self.width(), self.height(), tenants)
        self._convert_legacy_positions()
        current = {}
        for tn in names:
            current[tn] = self._pinned_position(tn) or slots[tn]

        # Row-major reading order on both sides, so a tile keeps its place in
        # the arrangement rather than being teleported across the canvas.
        row_height = max(1.0, self._cluster_radius(names[0], tenants[names[0]]))
        by_reading_order = sorted(
            names, key=lambda t: (round(current[t][1] / max(row_height, 1.0)),
                                  current[t][0], t))
        target_order = sorted(slots, key=lambda t: (slots[t][1], slots[t][0], t))

        moved = 0
        for tn, slot_name in zip(by_reading_order, target_order):
            px, py = slots[slot_name]
            before = self._custom_positions.get(tn)
            self._pin(tn, px, py)
            if before != self._custom_positions[tn]:
                moved += 1
        self._layout_cache = None
        self.update()
        if moved:
            self.node_positions_changed.emit(self._position_snapshot())
        return moved

    # ── Hub Layout (RPMote orb in the centre) ──────────────

    def _compute_hub_layout(self, tenant_list: list, cx: float, cy: float,
                            w: int, h: int, tenants: dict) -> dict:
        """Compute tenant node positions on a dynamic ellipse around the orb."""
        n = len(tenant_list)
        positions = {}

        branchy = self._gs["show_branches"]

        if n == 1:
            tn = tenant_list[0]
            ry = min(h * 0.28, 210)
            if branchy:
                ry = max(ry, self._cluster_radius(tn, tenants.get(tn, [])) + 90)
            positions[tn] = (cx, cy - ry)
        else:
            rx = min(w * 0.34, 320)
            ry = min(h * 0.28, 210)
            node_sizes = [self._tile_size(tn, len(tenants[tn]))
                          for tn in tenant_list]
            if branchy:
                # Treat each cluster as a disc so the ring spaces them by their
                # full branch footprint instead of just the tile width.
                node_sizes = [
                    (self._cluster_radius(tn, tenants.get(tn, [])) * 2,
                     self._cluster_radius(tn, tenants.get(tn, [])) * 2)
                    for tn in tenant_list
                ]
            MIN_GAP_PX = 28

            def _total_arc(rx_t, ry_t):
                arcs = []
                for i in range(n):
                    angle = (2 * math.pi * i / n) - math.pi / 2
                    ca, sa = math.cos(angle), math.sin(angle)
                    r_local = (rx_t * ry_t) / max(
                        math.sqrt((ry_t * ca) ** 2 + (rx_t * sa) ** 2), 1)
                    arc = (node_sizes[i][0] + MIN_GAP_PX) / max(r_local, 1)
                    arcs.append(arc)
                return arcs, sum(arcs)

            # With branches on, clusters are big — let the ring grow past the
            # viewport and rely on panning rather than cramming tiles together.
            grow = 3.0 if branchy else 1.0
            max_rx = w * 0.44 * grow
            max_ry = h * 0.38 * grow
            for _ in range(12):
                arcs, total = _total_arc(rx, ry)
                if total <= 2 * math.pi:
                    break
                scale = min(total / (2 * math.pi) * 1.05, 1.6 if branchy else 1.25)
                rx = min(rx * scale, max_rx)
                ry = min(ry * scale, max_ry)
                if rx >= max_rx and ry >= max_ry:
                    break

            arcs, total = _total_arc(rx, ry)
            raw_arcs = []
            for i in range(n):
                angle_est = (2 * math.pi * i / n) - math.pi / 2
                ca, sa = math.cos(angle_est), math.sin(angle_est)
                r_local = (rx * ry) / max(
                    math.sqrt((ry * ca) ** 2 + (rx * sa) ** 2), 1)
                raw_arcs.append(node_sizes[i][0] / max(r_local, 1))

            total_raw = sum(raw_arcs)
            remaining = 2 * math.pi - total_raw
            per_gap = max(remaining / n, 0)

            angle = -math.pi / 2
            for i, tn in enumerate(tenant_list):
                angle += raw_arcs[i] / 2 if i == 0 else (
                    raw_arcs[i - 1] / 2 + per_gap + raw_arcs[i] / 2)
                positions[tn] = (cx + rx * math.cos(angle),
                                 cy + ry * math.sin(angle))

        return positions

    def _graph_is_seen(self) -> bool:
        # Never shown yet means driven directly (before the window appears,
        # or by a test), not hidden from anyone: keep animating.
        if not getattr(self, "_shown_once", False):
            return True
        window = self.window()
        return self.isVisible() and not (window is not None and window.isMinimized())

    def _tick(self):
        try:
            if not self._gs["animate"]:
                # Motion disabled: skip the repaint entirely.  Hover, drag and
                # option changes still call update() directly.
                return
            if not self._graph_is_seen():
                # Behind a terminal tab or in the taskbar: no rain, aurora or
                # pulses to work out, just a slow look-in until it is shown.
                if self._anim_timer.interval() != IDLE_POLL_MS:
                    self._anim_timer.setInterval(IDLE_POLL_MS)
                self._last_tick_ns = self._clock.nsecsElapsed()
                return
            if self._anim_timer.interval() != self.frame_interval_ms:
                self._anim_timer.setInterval(self.frame_interval_ms)
            # Real elapsed time, so the aurora / rain / wave move at the same
            # speed whatever the tick rate; clamped so a stall (dragging the
            # window, a modal dialog) doesn't lurch everything forward.
            now = self._clock.nsecsElapsed()
            dt = min((now - self._last_tick_ns) / 1e9, 0.1)
            self._last_tick_ns = now
            self._time += dt
            self._advance_hover(dt)
            # A calm background also moves slowly; a plain one not at all.
            scene_dt = dt * _BACKGROUND_TUNING[self.background_level()][1]
            if scene_dt <= 0.0:
                pass
            elif self._dashboard_theme == "matrix" and self._matrix_rain:
                self._tick_matrix_rain(scene_dt, self.height())
            elif self._dashboard_theme == "dotwave":
                self._dw_count += scene_dt * 3.0
            elif self._dashboard_theme == "aurora":
                self._aurora_time += scene_dt * 1.0
            elif self._background is not None:
                self._background.tick(scene_dt, self.width(), self.height())
            self.update()
        except RuntimeError:
            try:
                self._anim_timer.stop()
            except Exception:
                pass

    def showEvent(self, event):
        super().showEvent(event)
        self._shown_once = True
        # Back in front: full frame rate at once rather than at the next
        # idle look-in.
        if self._anim_timer.interval() != self.frame_interval_ms:
            self._anim_timer.setInterval(self.frame_interval_ms)
            self._last_tick_ns = self._clock.nsecsElapsed()

    def _hover_targets(self) -> set:
        """The cursor's endpoint, or the connection selected in the sidebar."""
        targets = set()
        if self._hovered_node is not None:
            targets.add(make_ep("tenant", self._hovered_node))
        if self._hovered_conn is not None:
            targets.add(make_ep("conn", self._hovered_conn))
        if not targets and self._highlighted_conn is not None:
            targets.add(make_ep("conn", self._highlighted_conn))
        return targets

    def _advance_hover(self, dt: float):
        """Step each hovered or fading endpoint along its ease.

        Only those endpoints change; every other node keeps its cached body.
        """
        targets = self._hover_targets()
        dt = max(0.0, dt)
        for ep in set(self._hover_levels) | targets:
            current = self._hover_levels.get(ep, 0.0)
            if ep in targets:
                if current <= 0.0:
                    # Arriving on a node sends a ring out from it.
                    self._ripples.append((ep, self._time))
                value = min(1.0, current + dt / HOVER_IN_S)
                self._hover_rising.add(ep)
            else:
                value = max(0.0, current - dt / HOVER_OUT_S)
                self._hover_rising.discard(ep)
            if value <= 0.0:
                self._hover_levels.pop(ep, None)
            else:
                self._hover_levels[ep] = value
        # One level for "something is hovered", eased on its own so gliding
        # from one node straight onto the next does not flash the rest of
        # the graph back to full strength in between.
        if targets:
            self._focus_ep = min(targets)
            self._focus_level = min(1.0, self._focus_level + dt / FOCUS_IN_S)
        elif self._focus_level > 0.0:
            self._focus_level = max(0.0, self._focus_level - dt / FOCUS_OUT_S)
            if self._focus_level <= 0.0:
                self._focus_ep = None
        if self._ripples:
            self._ripples = [r for r in self._ripples
                             if self._time - r[1] < RIPPLE_S]
        self._advance_card(min(targets) if targets else None, dt)

    def _advance_card(self, target: str | None, dt: float):
        """The hover card: wait, then fade in; fade out quickly on leaving."""
        if target is not None and target != self._card_ep:
            warm = self._card_level > 0.5
            self._card_ep = target
            # Gliding from one card to the next: no second wait.
            self._card_since = self._time - (HOVER_CARD_DELAY_S if warm else 0.0)
            self._card_level = 0.35 if warm else 0.0
        if target is not None:
            if self._time - self._card_since >= HOVER_CARD_DELAY_S:
                self._card_level = min(1.0, self._card_level + dt / HOVER_CARD_IN_S)
        elif self._card_level > 0.0:
            self._card_level = max(0.0, self._card_level - dt / HOVER_CARD_OUT_S)
        if self._card_level <= 0.0 and target is None:
            self._card_ep = None

    def _hover_amount(self, ep: str) -> float:
        """How hovered *ep* is, 0..1.  With motion off a hover is all or nothing."""
        if self._gs["animate"]:
            return self._hover_levels.get(ep, 0.0)
        return 1.0 if ep in self._hover_targets() else 0.0

    def _hover_lift(self, ep: str, amount: float) -> float:
        """Scale for a hovered node: a springy rise, a smooth settle back."""
        t = self._hover_amount(ep)
        if t <= 0.0:
            return 1.0
        if not self._gs["animate"]:
            return 1.0 + amount
        eased = _ease_out_back(t) if ep in self._hover_rising else _smoothstep(t)
        return 1.0 + amount * eased

    def _hover_focus(self, tenants: dict):
        """What a hover is pointing at this frame, or None.

        None as well while a relationship lens, the search spotlight, link
        drawing or a drag is running: each of those already decides what is
        lit, and a second dimming layer would only fight it.
        """
        if self._gs["animate"]:
            level, ep = self._focus_level, self._focus_ep
        else:
            targets = self._hover_targets()
            level = 1.0 if targets else 0.0
            ep = min(targets) if targets else None
        if (level <= 0.0 or ep is None or self._lens != LENS_NONE
                or self._search_active or self._link_mode
                or self._link_source is not None or self._panning
                or self._drag_target or self._conn_drag_target):
            return None
        kind, key = parse_ep(ep)
        if kind == "tenant":
            if key not in tenants:
                return None
            members = {ep}
            for conn in tenants[key]:
                cid = str(conn.get("id", "")) if isinstance(conn, dict) else ""
                if cid:
                    members.add(make_ep("conn", cid))
            return _HoverFocus(level, kind, key, None, members, FOCUS_DIM_TENANT)
        conn = self._conn_by_id(key)
        if conn is None:
            return None
        owner = conn.get("tenant", "Default") or "Default"
        return _HoverFocus(level, kind, owner, key,
                           {ep, make_ep("tenant", owner)}, FOCUS_DIM_CONN)

    @staticmethod
    def _lifted(p, x: float, y: float, scale: float) -> bool:
        """Scale what follows about (x, y).  Returns whether it saved state."""
        if abs(scale - 1.0) < 1e-3:
            return False
        p.save()
        p.translate(x, y)
        p.scale(scale, scale)
        p.translate(-x, -y)
        return True

    def _bloom_sprite(self, color: QColor) -> QPixmap:
        """A soft glow in *color*, rendered once and stretched to fit."""
        key = ("bloom", color.rgb())
        pm = self._sprite_cache.get(key)
        if pm is not None:
            return pm
        size = 96
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        sp = QPainter(pm)
        sp.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = QRadialGradient(QPointF(size / 2, size / 2), size / 2)
        r, gg, b = color.red(), color.green(), color.blue()
        g.setColorAt(0.0, QColor(r, gg, b, 120))
        g.setColorAt(0.45, QColor(r, gg, b, 52))
        g.setColorAt(1.0, QColor(r, gg, b, 0))
        sp.setPen(Qt.PenStyle.NoPen)
        sp.setBrush(QBrush(g))
        sp.drawEllipse(QRectF(0, 0, size, size))
        sp.end()
        self._sprite_cache[key] = pm
        return pm

    def _draw_bloom(self, p, rect: QRectF, color: QColor, amount: float,
                    spread_x: float, spread_y: float):
        """Glow under a hovered node, breathing gently while it is held."""
        if amount <= 0.0:
            return
        breathe = (1.0 + 0.06 * math.sin(self._time * 3.2)
                   if self._gs["animate"] else 1.0)
        gx = rect.width() * spread_x * breathe
        gy = rect.height() * spread_y * breathe
        p.save()
        p.setOpacity(p.opacity() * amount)
        sprite = self._bloom_sprite(color)
        p.drawPixmap(rect.adjusted(-gx, -gy, gx, gy), sprite, QRectF(sprite.rect()))
        p.restore()

    def _draw_hover_accent(self, p, rect: QRectF, color: QColor,
                           ep: str, hovered: bool, radius: float):
        """Outline of a hovered node, with a bright head of light running
        round it -- a neon edge that keeps moving while the cursor stays."""
        amount = (self._hover_levels.get(ep, 0.0) if self._gs["animate"]
                  else float(hovered))
        if amount <= 0.0:
            return
        p.save()
        pad = 1.0 + 1.5 * amount
        outline = rect.adjusted(-pad, -pad, pad, pad)
        r = radius + pad
        cr, cg, cb = color.red(), color.green(), color.blue()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(cr, cg, cb, int(90 * amount)), 1.1))
        p.drawRoundedRect(outline, r, r)
        if self._gs["animate"]:
            # A conical gradient's seam is the comet's head: bright on one
            # side of it, fading out round the rest of the turn.  The tail
            # lies counter-clockwise of the head, so the head runs clockwise.
            angle = 360.0 - (self._time * 240.0) % 360.0
            sweep = QConicalGradient(outline.center(), angle)
            sweep.setColorAt(0.0, QColor(255, 255, 255, int(235 * amount)))
            sweep.setColorAt(0.04, QColor(cr, cg, cb, int(230 * amount)))
            sweep.setColorAt(0.32, QColor(cr, cg, cb, 0))
            sweep.setColorAt(1.0, QColor(cr, cg, cb, 0))
            pen = QPen(QBrush(sweep), 1.9)
            p.setPen(pen)
            p.drawRoundedRect(outline, r, r)
        p.restore()

    def _draw_ripples(self, p):
        """Rings spreading out from nodes the cursor has just arrived on."""
        if not self._ripples or not self._gs["animate"]:
            return
        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        for ep, started in self._ripples:
            drawn = self._drawn_nodes.get(ep)
            if drawn is None:
                continue
            rect, color, radius = drawn
            u = (self._time - started) / RIPPLE_S
            if not 0.0 <= u < 1.0:
                continue
            grow = 3.0 + 26.0 * _ease_out_cubic(u)
            alpha = int(170 * (1.0 - u) ** 1.6)
            if alpha <= 2:
                continue
            p.setPen(QPen(QColor(color.red(), color.green(), color.blue(), alpha),
                          1.8 * (1.0 - u) + 0.6))
            p.drawRoundedRect(rect.adjusted(-grow, -grow, grow, grow),
                              radius + grow, radius + grow)
        p.restore()

    def _hover_card_content(self, ep: str):
        """(title, accent, rows) for a node's card, or None.

        Each row is (text, dot colour or None, dim).  The same facts the old
        tooltip gave (_hover_tooltip), laid out to be read at a glance.
        """
        kind, key = parse_ep(ep)
        if kind == "tenant":
            conns = [c for c in self._connections if isinstance(c, dict)
                     and (c.get("tenant", "Default") or "Default") == key]
            if not conns:
                return None
            accent = QColor(self._tenant_colors.get(key, "#a78bfa"))
            rows = [(f"{len(conns)} connection{'s' if len(conns) != 1 else ''}",
                     accent, False)]
            live = sum(1 for c in conns if str(c.get("id", "")) in self._active_conn_ids)
            if live:
                rows.append((f"{live} open now", QColor(74, 222, 128), False))
            if self._ping_enabled:
                rows.extend(self._tenant_health_rows(conns))
            rows.append(("Click to open or close  ·  right-click for actions"
                         if self.collapsing_active()
                         else "Drag to move  ·  right-click for actions", None, True))
            return key, accent, rows
        conn = self._conn_by_id(key)
        if conn is None:
            return None
        proto = str(conn.get("protocol", "ssh") or "ssh").lower()
        accent = QColor(PROTOCOL_COLORS.get(proto, "#a78bfa"))
        rows = []
        host = str(conn.get("host", "") or "")
        port = conn.get("port", "")
        if host:
            rows.append((f"{host}:{port}" if port else host, None, False))
        owner = conn.get("tenant", "Default") or "Default"
        rows.append((f"{protocol_tag(proto)}  ·  {owner}",
                     QColor(self._tenant_colors.get(owner, "#a78bfa")), False))
        if self._ping_enabled:
            info = self.conn_status(key)
            state = info.get("state", STATE_UNKNOWN) if isinstance(info, dict) else STATE_UNKNOWN
            for i, line in enumerate(describe_status(info).split("\n")):
                rows.append((line, self._status_color(state) if i == 0 else None, i > 0))
        rows.extend((line, None, True) for line in self._relationship_lines(key)[:4])
        rows.append(("Double-click to connect  ·  right-click for actions", None, True))
        return self._ep_label(ep), accent, rows

    def _tenant_health_rows(self, conns: list) -> list:
        """How a tenant's machines are doing, counted -- a tenant with one
        dead server is not "Unreachable", it has one unreachable machine."""
        counts: dict = {}
        for conn in conns:
            state = self.conn_state(str(conn.get("id", "")))
            counts[state] = counts.get(state, 0) + 1
        rows = []
        for state, text in ((STATE_DOWN, "{} unreachable"),
                            (STATE_DEGRADED, "{} with a service down"),
                            (STATE_CHECKING, "{} being checked")):
            if counts.get(state):
                rows.append((text.format(counts[state]), self._status_color(state), False))
        if not rows:
            if counts.get(STATE_UP):
                rows.append(("All reachable" if counts[STATE_UP] == len(conns)
                             else f"{counts[STATE_UP]} reachable",
                             self._status_color(STATE_UP), False))
            else:
                rows.append((STATE_LABELS[STATE_UNKNOWN], None, True))
        return rows

    def _hover_card_pixmap(self, title: str, accent: QColor, rows: list,
                           dpr: float) -> QPixmap:
        """The card's picture, rendered once per distinct content."""
        key = (title, accent.rgb(), dpr,
               tuple((t, c.rgb() if c is not None else None, dim) for t, c, dim in rows))
        if self._card_cache is not None and self._card_cache[0] == key:
            return self._card_cache[1]
        title_font = QFont("Segoe UI", 10, QFont.Weight.DemiBold)
        body_font = QFont("Segoe UI", 9)
        tfm, bfm = QFontMetrics(title_font), QFontMetrics(body_font)
        pad_x, pad_y, dot_w = 13.0, 9.0, 13
        room = HOVER_CARD_MAX_W - 2 * pad_x - 4
        title_text = tfm.elidedText(title, Qt.TextElideMode.ElideRight, int(room))
        lines = []
        for text, color, dim in rows:
            indent = dot_w if color is not None else 0
            shown = bfm.elidedText(text, Qt.TextElideMode.ElideRight, int(room - indent))
            lines.append((shown, color, dim, indent))
        width = max([tfm.horizontalAdvance(title_text)]
                    + [bfm.horizontalAdvance(t) + ind for t, _c, _d, ind in lines])
        width += 2 * pad_x + 4
        row_h = bfm.height() + 3
        height = 2 * pad_y + tfm.height() + 4 + row_h * len(lines)
        pm = QPixmap(int(math.ceil((width + 2) * dpr)), int(math.ceil((height + 2) * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        box = QRectF(1, 1, width, height)
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 120), 1.0))
        p.setBrush(QColor(12, 14, 24, 240))
        p.drawRoundedRect(box, 9, 9)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawRoundedRect(QRectF(box.left() + 4, box.top() + 8, 3, height - 16), 1.5, 1.5)
        x0 = box.left() + pad_x + 4
        y = box.top() + pad_y
        p.setFont(title_font)
        p.setPen(QColor(241, 245, 249))
        p.drawText(QRectF(x0, y, width, tfm.height()),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title_text)
        y += tfm.height() + 4
        p.setFont(body_font)
        for text, color, dim, indent in lines:
            if color is not None:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color)
                p.drawEllipse(QPointF(x0 + 4, y + row_h / 2), 3.5, 3.5)
            p.setPen(QColor(140, 150, 172) if dim else QColor(214, 219, 232))
            p.drawText(QRectF(x0 + indent, y, width, row_h),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            y += row_h
        p.end()
        self._card_cache = (key, pm)
        return pm

    def _draw_hover_card(self, p, w: int, h: int):
        """The card beside the hovered node, faded and risen in by its level."""
        if self._gs["animate"]:
            ep, level = self._card_ep, self._card_level
        else:
            targets = self._hover_targets()
            ep = min(targets) if targets else None
            level = 1.0 if ep else 0.0
        if (ep is None or level <= 0.0 or self._search_active or self._link_mode
                or self._link_source is not None or self._panning
                or self._drag_target or self._conn_drag_target):
            return
        drawn = self._drawn_nodes.get(ep)
        content = self._hover_card_content(ep) if drawn is not None else None
        if content is None:
            return
        dpr = float(self.devicePixelRatioF())
        pm = self._hover_card_pixmap(*content, dpr)
        rect = drawn[0]
        cw, ch = pm.width() / dpr, pm.height() / dpr
        gap = 12.0
        kind, key = parse_ep(ep)
        if kind == "tenant":
            # Keep clear of the whole cluster -- its machines' names fan out
            # round it on hover -- so go above or below it, not beside.
            cluster = QRectF(rect)
            for conn in self._connections:
                if (isinstance(conn, dict)
                        and (conn.get("tenant", "Default") or "Default") == key):
                    member = self._drawn_nodes.get(make_ep("conn", str(conn.get("id", ""))))
                    if member is not None:
                        cluster = cluster.united(member[0])
            cluster.adjust(0, -22, 0, 22)          # room for the name tags
            x = min(max(8.0, rect.center().x() - cw / 2), w - cw - 8)
            if cluster.bottom() + gap + ch <= h - 8:
                y = cluster.bottom() + gap
            elif cluster.top() - gap - ch >= 8:
                y = cluster.top() - gap - ch
            else:
                y = min(max(8.0, rect.bottom() + gap), h - ch - 8)
        else:
            x = rect.right() + gap
            if x + cw > w - 8:
                x = rect.left() - gap - cw          # no room on the right
            if x < 8:
                x = min(max(8.0, rect.center().x() - cw / 2), w - cw - 8)
            y = min(max(8.0, rect.center().y() - ch / 2), h - ch - 8)
        rise = (1.0 - _ease_out_cubic(level)) * 6.0
        p.save()
        p.setOpacity(level)
        p.drawPixmap(QPointF(round(x * dpr) / dpr, round((y + rise) * dpr) / dpr), pm)
        p.restore()

    #: Name tags shown on hover at low zoom, by (text, colour, dpr).
    _peek_tag_cache: dict = {}

    def _draw_peek_tag(self, p, rect: QRectF, conn: dict, color: QColor,
                       amount: float, anchor: tuple | None, dpr: float):
        """A machine's name next to its dot, for a zoomed-out hover.

        Laid out away from the tenant's centre, so when a whole cluster is
        hovered the names fan out round it instead of piling up in the middle.
        """
        name = (self._branch_label(conn) or str(conn.get("name", "") or "")
                or str(conn.get("host", "") or "")).strip()
        if not name or amount <= 0.0:
            return
        key = (name, color.rgb(), dpr)
        pm = self._peek_tag_cache.get(key)
        if pm is None:
            if len(self._peek_tag_cache) > 1024:
                self._peek_tag_cache.clear()
            font = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
            fm = QFontMetrics(font)
            text = fm.elidedText(name, Qt.TextElideMode.ElideRight, 160)
            w = fm.horizontalAdvance(text) + 14
            h = fm.height() + 4
            pm = QPixmap(int(math.ceil((w + 2) * dpr)), int(math.ceil((h + 2) * dpr)))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            pp = QPainter(pm)
            pp.setRenderHint(QPainter.RenderHint.Antialiasing)
            pp.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            box = QRectF(1, 1, w, h)
            pp.setPen(QPen(QColor(color.red(), color.green(), color.blue(), 200), 1.0))
            pp.setBrush(QColor(10, 12, 20, 228))
            pp.drawRoundedRect(box, h / 2, h / 2)
            pp.setFont(font)
            pp.setPen(QColor("#f8fafc"))
            pp.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
            pp.end()
            self._peek_tag_cache[key] = pm
        w = pm.width() / dpr
        h = pm.height() / dpr
        cx, cy = rect.center().x(), rect.center().y()
        dx, dy = 0.0, -1.0
        if anchor is not None:
            vx, vy = cx - anchor[0], cy - anchor[1]
            length = math.hypot(vx, vy)
            if length > 1e-3:
                dx, dy = vx / length, vy / length
        # Slides a few pixels outward as it fades in.
        reach = rect.width() / 2 + 3.0 + 6.0 * amount
        ax, ay = cx + dx * reach, cy + dy * reach
        if abs(dx) < 0.35:
            left = ax - w / 2                       # straight above / below
            top = ay - h if dy < 0 else ay
        else:
            left = ax if dx > 0 else ax - w
            top = ay - h / 2 + dy * h * 0.3
        p.save()
        p.setOpacity(p.opacity() * min(1.0, amount * 1.15))
        p.drawPixmap(QPointF(round(left * dpr) / dpr, round(top * dpr) / dpr), pm)
        p.restore()

    def _casing_pen(self, width: float) -> QPen:
        """Dark underlay for a line: the ground colour, a little wider.

        Map makers case roads for the same reason -- a line with a dark edge
        reads over anything, however busy the scenery underneath."""
        ground = self._ground_color()
        key = ("casing", ground.rgb(), width)
        pen = self._pen_cache.get(key)
        if pen is None:
            pen = QPen(QColor(ground.red(), ground.green(), ground.blue(), 165),
                       width + 2.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            self._pen_cache[key] = pen
        return pen

    def _draw_lit_line(self, p, path: QPainterPath, color: QColor,
                       amount: float, phase: float):
        """A tenant -> machine line a hover is about: brighter, a touch
        wider, with a pulse running from the tenant out to the machine."""
        width = 1.25 + 1.0 * amount
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(self._round_pen(color, int(40 * amount), width + 5.0))
        p.drawPath(path)
        p.setPen(self._casing_pen(width))
        p.drawPath(path)
        p.setPen(self._round_pen(color, min(255, BRANCH_LINE_ALPHA + int(150 * amount)),
                                 round(width, 1)))
        p.drawPath(path)
        if not self._gs["animate"]:
            return
        t = (self._time * 0.85 + phase) % 1.0
        pt = path.pointAtPercent(t)
        sprite = self._particle_sprite(color, 3.0)
        half = sprite.width() / sprite.devicePixelRatio() / 2
        p.save()
        # Fades in at the tenant and out at the machine, so the loop is seamless.
        p.setOpacity(p.opacity() * amount * math.sin(math.pi * t))
        p.drawPixmap(QPointF(pt.x() - half, pt.y() - half), sprite)
        p.restore()

    def get_view_state(self) -> dict:
        # Automatic tenant framing lasts only while inspecting that tenant.
        # Even a window close or manual pan must not persist its camera.
        zoom, pan = self._tenant_camera or (self._zoom, self._pan)
        return {
            "zoom": float(zoom),
            "show_all_connections": bool(self._show_all_connections),
            "visible_tenants": sorted(self._visible_tenants),
            "pan_x": float(pan.x()),
            "pan_y": float(pan.y()),
        }

    def set_view_state(self, state: dict):
        if not isinstance(state, dict):
            return
        self._restore_tenant_view()
        zoom = state.get("zoom", self._zoom)
        try:
            zoom = float(zoom)
        except Exception:
            zoom = self._zoom
        self._zoom = max(self._min_zoom, min(self._max_zoom, zoom))
        try:
            self._pan = QPointF(float(state.get("pan_x", 0.0)),
                                float(state.get("pan_y", 0.0)))
        except Exception:
            self._pan = QPointF(0.0, 0.0)
        if self.is_free_mode and self.collapsing_active():
            # Large estate: reopen exactly what was open last time, so the
            # graph does not greet you with three hundred branches.
            saved = state.get("visible_tenants")
            self._show_all_connections = False
            self._visible_tenants = ({str(t) for t in saved} & self._all_tenants()
                                     if isinstance(saved, (list, set, tuple))
                                     else set())
        elif self.is_free_mode:
            # Free-floating mode has no orb to expand from — show everything.
            self._show_all_connections = True
            self._visible_tenants = self._all_tenants()
        else:
            # Hub mode always starts collapsed (ignore saved expand state).
            # The saved pan framed whatever was expanded last session; applied
            # to the lone orb it just shoves the logo off-centre.  Zoom pivots
            # on the orb, so it can stay.
            self._show_all_connections = False
            self._visible_tenants = set()
            self._pan = QPointF(0.0, 0.0)
        self.update()

    def zoom_in(self):
        self._set_zoom(self._zoom * 1.12)

    def zoom_out(self):
        self._set_zoom(self._zoom / 1.12)

    def reset_zoom(self):
        self._set_zoom(1.0)

    def reveal_tenant(self, tenant_name: str, exclusive: bool = False,
                      *, focus: bool = True):
        tenant_name = (tenant_name or "").strip()
        if not tenant_name:
            return
        if focus or tenant_name != self._selected_node:
            self._selected_conn = None
            self._highlighted_conn = None
        if self.is_free_mode:
            # Tiles stay visible, but a large estate hides their branches.
            self._visible_tenants.add(tenant_name)
        elif exclusive:
            # When the user has the main connection manager expanded (show-all),
            # do NOT collapse the view; keep all tiles visible and just mark the
            # clicked tenant as selected. If show-all is off, preserve the
            # previous “pop-out single tenant” behaviour.
            if not self._show_all_connections:
                self._show_all_connections = False
                self._visible_tenants = {tenant_name}
            else:
                # Keep show-all on; still remember the tenant for later restores
                self._visible_tenants.add(tenant_name)
        else:
            self._visible_tenants.add(tenant_name)
        self._selected_node = tenant_name
        self._layout_cache = None
        if focus:
            self.focus_tenant(tenant_name)
        self.update()
        self.view_state_changed.emit(self.get_view_state())

    def select_connection(self, conn_id: str):
        """Reveal a sidebar connection and hold its normal hover highlight."""
        conn_id = str(conn_id)
        conn = self._conn_by_id(conn_id)
        if conn is None:
            return
        tenant = conn.get("tenant", "Default") or "Default"
        if self._focused_tenant != tenant or not self._tenant_expanded(tenant):
            self.reveal_tenant(tenant, exclusive=True)
        self._selected_node = tenant
        self._selected_conn = conn_id
        self._selected_link = None
        self._highlighted_conn = conn_id
        self.update()

    def _set_zoom(self, value: float):
        minimum = 0.001 if self._tenant_camera is not None else self._min_zoom
        value = max(minimum, min(self._max_zoom, float(value)))
        if abs(value - self._zoom) < 0.001:
            return
        self._zoom = value
        self.update()
        self.view_state_changed.emit(self.get_view_state())

    # ── Spotlight Search ───────────────────────────────────────────

    def open_search(self):
        """Arm the find bar.

        Re-opening keeps the last query, ready to be replaced by the next
        keystroke — the way a browser's find bar behaves.
        """
        self._search_active = True
        self._search_replace = bool(self._search_query)
        self._recompute_search()
        self.setFocus()
        self.update()

    def close_search(self):
        """Put the bar away and un-dim the estate.  The selection stays."""
        if not self._search_active:
            return
        self._search_active = False
        self._search_replace = False
        self._search_eps = set()
        self._search_row_rects = []
        self._search_rect = QRectF()
        self.update()
        # Where the search left the camera is worth keeping, but only once.
        # Announcing it per keystroke would re-encrypt and rewrite the vault on
        # every character typed, which is what view_state_changed costs.
        self.view_state_changed.emit(self.get_view_state())

    def is_search_active(self) -> bool:
        return self._search_active

    def search_query(self) -> str:
        return self._search_query

    def set_search_query(self, text: str, focus: bool = True):
        """Replace the query and jump to the best match."""
        self._search_query = str(text or "")
        self._search_replace = False
        self._recompute_search()
        if focus and self._search_hits:
            self._focus_search_hit()
        self.update()

    def _recompute_search(self):
        self._search_hits = self._rank_search_hits(self._search_query)
        self._search_index = 0 if self._search_hits else -1
        self._search_eps = {h["ep"] for h in self._search_hits}

    def _rank_search_hits(self, query: str) -> list:
        """Every tenant and connection matching *query*, best first."""
        if not (query or "").strip():
            return []
        hits = []
        for tenant in self._all_tenants():
            score = fuzzy_match(query, tenant)
            if score is None:
                continue
            count = sum(1 for c in self._connections if isinstance(c, dict)
                        and (c.get("tenant", "Default") or "Default") == tenant)
            plural = "" if count == 1 else "s"
            hits.append({
                "ep": make_ep("tenant", tenant), "kind": "tenant",
                "label": tenant, "tenant": tenant, "conn": None,
                "sub": f"{count} connection{plural}",
                # A tenant edges out its own members on an equal score: typing
                # a customer name should land on the customer, not one machine.
                "score": score * 1.05,
            })
        for conn in self._connections:
            if not isinstance(conn, dict):
                continue
            cid = str(conn.get("id", ""))
            if not cid:
                continue
            best = match_record(query, conn)
            if best is None:
                continue
            owner = str(conn.get("tenant", "Default") or "Default")
            host = str(conn.get("host", "") or "")
            proto = str(conn.get("protocol", "") or "").upper()
            hits.append({
                "ep": make_ep("conn", cid), "kind": "conn", "conn": conn,
                "label": (str(conn.get("name", "") or "").rstrip().rstrip("●• ")
                          or host or cid),
                "tenant": owner,
                "sub": "  ·  ".join(x for x in (host, proto, owner) if x),
                "score": best,
            })
        # Label breaks ties so the list never reshuffles between identical runs.
        hits.sort(key=lambda h: (-h["score"], h["label"].lower()))
        return hits

    def _current_search_hit(self) -> dict | None:
        if 0 <= self._search_index < len(self._search_hits):
            return self._search_hits[self._search_index]
        return None

    def _step_search(self, delta: int):
        """Move to the next/previous result, wrapping at both ends."""
        if not self._search_hits:
            return
        self._search_index = ((self._search_index + delta)
                              % len(self._search_hits))
        self._focus_search_hit()
        self.update()

    def _focus_search_hit(self):
        """Reveal, select and centre the focused result."""
        hit = self._current_search_hit()
        if hit is None:
            return
        self._highlighted_conn = None
        # Hub mode keeps un-revealed tenants out of the layout entirely, so the
        # reveal has to happen *before* we ask where the node sits — otherwise
        # we would be centring on the position it had while hidden, which is
        # no position at all.
        if not self.is_free_mode and hit["tenant"] not in self._visible_tenants:
            self._visible_tenants.add(hit["tenant"])
            self._layout_cache = None
        if hit["kind"] == "tenant":
            self._selected_node = hit["tenant"]
            self._selected_conn = None
        else:
            self._selected_node = hit["tenant"]
            self._selected_conn = str(hit["conn"].get("id", ""))

        nodes = self._layout_nodes()
        node = nodes.get(hit["ep"])
        if node is None and hit["kind"] == "conn":
            # Branches are switched off — the connection sits on its tile.
            node = nodes.get(make_ep("tenant", hit["tenant"]))
        if node is None:
            return
        self._center_on_logical(node[0], node[1])

    def _center_on_logical(self, px: float, py: float):
        """Put the logical point (*px*, *py*) under the middle of the canvas.

        Zoom is nudged up to a readable level but never pulled back down — a
        user who deliberately zoomed in stays zoomed in.
        """
        self._zoom = max(self._min_zoom,
                         min(self._max_zoom, max(self._zoom, 0.85)))
        w, h = max(self.width(), 1), max(self.height(), 1)
        cx, cy = self._scene_center()
        # Sit the result slightly below centre so the find bar never covers it.
        self._pan = QPointF(w / 2 - (cx + (px - cx) * self._zoom),
                            h / 2 + 30 - (cy + (py - cy) * self._zoom))

    def _commit_search(self, open_it: bool = False):
        """Enter: keep the focused result selected and put the bar away.

        With *open_it* (Ctrl+Enter) a connection is opened outright, the same
        thing double-clicking its node does.
        """
        hit = self._current_search_hit()
        if hit is not None:
            self._focus_search_hit()
        self.close_search()
        if hit is None:
            return
        if hit["kind"] == "conn":
            if open_it:
                self.connection_activated.emit(dict(hit["conn"]))
            else:
                self.connection_clicked.emit(dict(hit["conn"]))
        else:
            self.tenant_clicked.emit(hit["tenant"])

    def _search_row_at(self, pos: QPointF) -> int | None:
        for rect, index in self._search_row_rects:
            if rect.contains(pos):
                return index
        return None

    def _handle_search_key(self, event) -> bool:
        """Feed a key to the find bar.  True when the bar consumed it."""
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)

        if key == Qt.Key.Key_Escape:
            self.close_search()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._commit_search(open_it=ctrl)
            return True
        if key in (Qt.Key.Key_Down, Qt.Key.Key_Tab):
            self._step_search(1)
            return True
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Backtab):
            self._step_search(-1)
            return True
        if key == Qt.Key.Key_Backspace:
            if ctrl or self._search_replace:
                self._search_query = ""          # Ctrl+Backspace clears it
            else:
                self._search_query = self._search_query[:-1]
            self._search_replace = False
            self._recompute_search()
            if self._search_hits:
                self._focus_search_hit()
            self.update()
            return True
        if ctrl and key == Qt.Key.Key_A:
            self._search_replace = True          # "select all", ready to retype
            self.update()
            return True

        text = event.text()
        if text and text.isprintable() and not ctrl and not alt:
            if self._search_replace:
                self._search_query = ""
                self._search_replace = False
            self._search_query += text
            self._recompute_search()
            if self._search_hits:
                self._focus_search_hit()
            self.update()
            return True
        return False

    # ── Node Size Helpers ──────────────────────────────────────────

    def _node_base_color(self, tn: str) -> QColor:
        """The colour a tenant and its branches are drawn in.

        The fully-themed dashboards ignore per-tenant colours on purpose and
        draw everything in the skin's accent, so this is the one place that
        decision lives.
        """
        accent = self._skin.accent
        if accent is not None:
            return QColor(*accent)
        return QColor(self._tenant_colors.get(tn, "#a78bfa"))

    def _tile_compact(self) -> bool:
        """Should tenant tiles be drawn as a single compact row?

        "auto" means compact whenever the branches are on — the count badge
        and icon strip only duplicate what the branch nodes already show.
        """
        mode = self._gs["tile_detail"]
        if mode == "compact":
            return True
        if mode == "full":
            return False
        return bool(self._gs["show_branches"])

    def _tile_size(self, name: str, num_connections: int) -> tuple[float, float]:
        """Logical tile size for *name*, honouring the compact setting."""
        if not self._tile_compact():
            return self._node_size(num_connections)
        key = ("tile", name, num_connections, self._dashboard_theme)
        cached = self._branch_cache.get(key)
        if cached is not None:
            return cached
        fam = "Courier New" if self._skin.mono else "Segoe UI"
        fm = QFontMetrics(QFont(fam, COMPACT_TILE_FONT_PT, QFont.Weight.DemiBold))
        count_fm = QFontMetrics(QFont(fam, 9))
        # dot + gap + name + gap + count chip + padding, with the same slack the
        # branch pills use so nothing elides the moment you zoom out.
        chip_w = count_fm.horizontalAdvance(str(num_connections)) + 16
        w = 16 + 8 + 8 + fm.horizontalAdvance(name) * 1.08 + 10 + chip_w + 14
        size = (max(COMPACT_TILE_MIN_W, min(w, COMPACT_TILE_MAX_W)), COMPACT_TILE_H)
        self._branch_cache[key] = size
        return size

    @staticmethod
    def _node_size(num_connections: int) -> tuple[float, float]:
        """Return (width, height) scaled by number of connections.

        1 connection  → 120 × 70
        10+ connections → 200 × 110
        """
        t = min(max(num_connections, 1), 10) / 10.0
        cw = 150 + t * 80   # 150..230
        ch = 90 + t * 60    # 90..150
        return cw, ch

    # ── Main Paint ─────────────────────────────────────────────────

    def paintEvent(self, event):
        if self._gl_backed:
            # QOpenGLWidget only composites what is drawn inside paintGL();
            # the base paintEvent is what routes there.  Bypassing it (as a
            # QPainter here would) leaves the framebuffer untouched.
            super().paintEvent(event)
        else:
            self._paint()

    def paintGL(self):
        self._paint()

    def _paint(self):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        w, h = self.width(), self.height()
        self._view_rect = QRectF(-VIEW_MARGIN, -VIEW_MARGIN,
                                 w + 2 * VIEW_MARGIN, h + 2 * VIEW_MARGIN)
        self._tenant_state_memo.clear()

        if self._gl_backed:
            p.fillRect(0, 0, w, h, QColor(self._theme.get("bg_primary", "#0a0b10")))

        self._draw_scene(p, w, h)

        # Build tenant data
        tenants = {}
        for conn in self._connections:
            t = conn.get("tenant", "Default") or "Default"
            tenants.setdefault(t, []).append(conn)

        if not tenants:
            self._draw_empty_state(p, w, h)
            if self._search_active:
                self._draw_search_overlay(p, w, h)
            p.end()
            return

        cx, cy = self._scene_center()
        free = self.is_free_mode

        if free or self._show_all_connections:
            # Free-floating mode never collapses — the whole estate is on screen.
            visible_tenants = list(tenants.keys())
        else:
            visible_tenants = [t for t in tenants.keys() if t in self._visible_tenants]

        # Hub mode, nothing selected → just the orb and its hint.
        if not visible_tenants:
            self._node_rects = []
            self._conn_rects = []
            self._link_rects = []
            self._ep_geom = {}
            ocx, ocy = self._to_screen(cx, cy)
            self._draw_center_orb(p, ocx, ocy)
            self._draw_collapsed_hint(p, ocx, ocy, w)
            if self._search_active:
                self._draw_search_overlay(p, w, h)
            p.end()
            return

        # Logical tenant positions (cached — recomputed on resize/data/option change)
        positions = dict(self._compute_layout(visible_tenants, cx, cy, w, h, tenants))
        for tn in visible_tenants:
            pinned = self._pinned_position(tn)
            if pinned is not None:
                positions[tn] = pinned

        _tinted = self._skin.accent is not None   # one accent for everything
        node_scale = self._zoom * float(self._gs["node_scale"])
        show_branches = bool(self._gs["show_branches"])

        _tenant_color = self._node_base_color

        # ── Pre-compute branch geometry (logical space) ────────────
        branches: dict = {}
        if show_branches:
            for tn, (tx, ty) in positions.items():
                if not self._tenant_expanded(tn):
                    continue        # collapsed: the tile stands for its machines
                cw, ch = self._tile_size(tn, len(tenants[tn]))
                cw *= float(self._gs["node_scale"])
                ch *= float(self._gs["node_scale"])
                # In hub mode the first branch points away from the orb so the
                # spoke has a clear run in; in free mode just start at the top.
                start = (math.atan2(ty - cy, tx - cx) if not free
                         else -math.pi / 2)
                branches[tn] = self._branch_positions(
                    tn, tenants[tn], tx, ty, cw, ch, start)

        # ── 0. Where every linkable node sits, in screen space ─────
        #
        # Built before anything is drawn so links, the live link preview and
        # hit-testing all read the same geometry.  A connection endpoint falls
        # back to its tenant tile when branches are hidden, so turning branches
        # off dims a link rather than losing it.
        ep_geom: dict = {}
        for tn, (tx, ty) in positions.items():
            sx, sy = self._to_screen(tx, ty)
            tw, th = self._tile_size(tn, len(tenants[tn]))
            ep_geom[make_ep("tenant", tn)] = (
                sx, sy, tw * node_scale / 2, th * node_scale / 2)
        for tn, blist in branches.items():
            for conn, bx, by in blist:
                cid = str(conn.get("id", ""))
                if not cid:
                    continue
                sx, sy = self._to_screen(bx, by)
                bw = self._branch_logical_width(conn) * node_scale
                ep_geom[make_ep("conn", cid)] = (
                    sx, sy, bw / 2, BRANCH_H * node_scale / 2)
        for conn in self._connections:
            if not isinstance(conn, dict):
                continue
            cid = str(conn.get("id", ""))
            if not cid or make_ep("conn", cid) in ep_geom:
                continue
            owner = make_ep("tenant", conn.get("tenant", "Default") or "Default")
            if owner in ep_geom:
                ep_geom[make_ep("conn", cid)] = ep_geom[owner]
        self._ep_geom = ep_geom

        # ── 0b. Quiet patches of ground under each cluster ─────────
        self._draw_cluster_pools(p, positions, branches, tenants, node_scale)
        focus = self._hover_focus(tenants)

        # ── 1. Custom links between any two nodes (furthest back) ──
        self._link_rects = []
        self._pending_link_labels = []
        for i, link in enumerate(self._tenant_links):
            a, b = link.get("a"), link.get("b")
            ga, gb = ep_geom.get(a), ep_geom.get(b)
            if ga is None or gb is None:
                continue
            # Two connections on a hidden branch collapse onto the same tile;
            # a link from a node to itself is not worth drawing.
            if ga[0] == gb[0] and ga[1] == gb[1]:
                continue
            ax, ay, ahw, ahh = ga
            bx, by, bhw, bhh = gb
            # Anchor the ends on the node borders, not the hidden centres.
            ax, ay = self._edge_point(ax, ay, ahw, ahh, bx, by)
            bx, by = self._edge_point(bx, by, bhw, bhh, ax, ay)
            fade = 1.0
            if (focus is not None and normalize_ep(a) not in focus.members
                    and normalize_ep(b) not in focus.members):
                fade = focus.faded
            if fade < 1.0:
                p.save()
                p.setOpacity(fade)
            hit = self._draw_tenant_link(p, ax, ay, bx, by, link,
                                         selected=(i == self._selected_link),
                                         hovered=(i == self._hovered_link))
            if fade < 1.0:
                p.restore()
            self._link_rects.append((hit, i))

        # ── 2. Hub spokes (orb → tenant), hub mode only ────────────
        if not free and self._gs["show_hub_spokes"]:
            ocx, ocy = self._to_screen(cx, cy)
            for tn, (tx, ty) in positions.items():
                ztx, zty = self._to_screen(tx, ty)
                if not self._segment_visible(ocx, ocy, ztx, zty):
                    continue
                fade = focus.fade(make_ep("tenant", tn)) if focus is not None else 1.0
                if fade < 1.0:
                    p.save()
                    p.setOpacity(fade)
                self._draw_flow_line(p, ocx, ocy, ztx, zty, _tenant_color(tn))
                if fade < 1.0:
                    p.restore()

        # ── 3. Tenant → connection branch lines ────────────────────
        if show_branches:
            for tn, (tx, ty) in positions.items():
                ztx, zty = self._to_screen(tx, ty)
                base = _tenant_color(tn)
                # Everything a hover is not about fades back together.
                fade = focus.faded if focus is not None else 1.0
                # All of a tenant's idle lines share a pen, so they go down
                # as one path per colour: one drawPath instead of one per
                # machine.  They only overlap under the tile that covers
                # them, so the union looks the same as drawing them singly.
                batches: dict = {}
                lit: list = []
                for conn, bx, by in branches.get(tn, ()):
                    zbx, zby = self._to_screen(bx, by)
                    if not self._segment_visible(ztx, zty, zbx, zby):
                        continue
                    proto = str(conn.get("protocol", "ssh") or "ssh").lower()
                    lc = base if _tinted else QColor(
                        PROTOCOL_COLORS.get(proto, base.name()))
                    cid = str(conn.get("id", ""))
                    if focus is not None and focus.lights_line(tn, cid):
                        lit.append((lc, zbx, zby))
                        continue
                    if cid in self._active_conn_ids:
                        # Live sessions carry particles: still drawn singly.
                        if fade < 1.0:
                            p.save()
                            p.setOpacity(fade)
                        self._draw_flow_line(
                            p, ztx, zty, zbx, zby, lc,
                            width=1.2, glow=False, particles=2)
                        if fade < 1.0:
                            p.restore()
                        continue
                    batch = batches.get(lc.rgb())
                    if batch is None:
                        batch = batches[lc.rgb()] = (lc, QPainterPath())
                    self._append_line_path(batch[1], ztx, zty, zbx, zby)
                if batches:
                    if fade < 1.0:
                        p.save()
                        p.setOpacity(fade)
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    # Every casing first, then every colour, so a tenant's
                    # own lines never cut through each other.
                    casing = self._casing_pen(1.25)
                    for lc, path in batches.values():
                        p.setPen(casing)
                        p.drawPath(path)
                    for lc, path in batches.values():
                        p.setPen(self._round_pen(lc, BRANCH_LINE_ALPHA, 1.25))
                        p.drawPath(path)
                    if fade < 1.0:
                        p.restore()
                for k, (lc, zbx, zby) in enumerate(lit):
                    self._draw_lit_line(p, self._line_path(ztx, zty, zbx, zby),
                                        lc, focus.level, k * 0.173)

        # ── 4. Centre orb (hub mode only) ──────────────────────────
        if not free:
            ocx, ocy = self._to_screen(cx, cy)
            fade = 1.0 - (1.0 - focus.faded) * 0.5 if focus is not None else 1.0
            if fade < 1.0:
                p.save()
                p.setOpacity(fade)
            self._draw_center_orb(p, ocx, ocy)
            if fade < 1.0:
                p.restore()
        else:
            self._center_orb_rect = QRectF()

        # ── 5. Tenant tiles ────────────────────────────────────────
        self._node_rects = []
        self._drawn_nodes = {}
        for tn, (tx, ty) in positions.items():
            ztx, zty = self._to_screen(tx, ty)
            ep = make_ep("tenant", tn)
            fade = focus.fade(ep) if focus is not None else 1.0
            if fade < 1.0:
                p.save()
                p.setOpacity(fade)
            rect = self._draw_glass_node(
                p, ztx, zty, tn, tenants[tn],
                self._tenant_colors.get(tn, "#a78bfa"),
                tn == self._selected_node,
                (tn == self._hovered_node
                 or ep == self._link_hover_target),
                tn in self._active_tenants,
                node_scale)
            self._node_rects.append((rect, tn))
            if self.collapsing_active() and not self._tenant_expanded(tn):
                # The chip rides along with a lifted tile.
                lifted = self._lifted(p, ztx, zty,
                                      self._hover_lift(ep, HOVER_LIFT_TILE))
                self._draw_collapse_badge(p, rect, len(tenants[tn]))
                if lifted:
                    p.restore()
            if fade < 1.0:
                p.restore()

        # ── 6. Connection branch nodes (on top) ────────────────────
        self._conn_rects = []
        if show_branches:
            # A lifted pill grows past its neighbours, so it is drawn after
            # them -- the most-hovered last -- while hit-testing keeps the
            # layout order.
            later = []
            for tn, (tx, ty) in positions.items():
                base = _tenant_color(tn)
                anchor = self._to_screen(tx, ty)
                peek = (focus.level if focus is not None and focus.kind == "tenant"
                        and focus.tenant == tn else 0.0)
                for conn, bx, by in branches.get(tn, ()):
                    zbx, zby = self._to_screen(bx, by)
                    cid = str(conn.get("id", ""))
                    ep = make_ep("conn", cid)
                    args = (p, zbx, zby, conn, base,
                            cid in self._active_conn_ids,
                            cid == self._hovered_conn,
                            cid == self._selected_conn,
                            node_scale, anchor, peek)
                    fade = focus.fade(ep) if focus is not None else 1.0
                    amount = self._hover_amount(ep)
                    if amount > 0.0:
                        rect = self._draw_connection_node(*args, draw=False)
                        later.append((amount, args, fade))
                    else:
                        rect = self._draw_faded_conn(args, fade)
                    self._conn_rects.append((rect, conn, tn))
            later.sort(key=lambda item: item[0])
            for _amount, args, fade in later:
                self._draw_faded_conn(args, fade)

        # ── 6b. Rings from nodes the cursor just arrived on ────────
        self._draw_ripples(p)

        # ── 7. Link annotations, above everything they cross ───────
        self._draw_link_labels(p)

        # ── 8. Live link-drawing preview ───────────────────────────
        if self._link_source and self._link_cursor is not None:
            src = ep_geom.get(self._link_source)
            if src:
                sx, sy = src[0], src[1]
                ex, ey = self._link_cursor.x(), self._link_cursor.y()
                target = ep_geom.get(self._link_hover_target or "")
                if target:
                    ex, ey = target[0], target[1]
                pen = QPen(QColor(255, 255, 255, 190), 2)
                pen.setStyle(Qt.PenStyle.CustomDashLine)
                pen.setDashPattern([5, 4])
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(self._line_path(sx, sy, ex, ey, wobble=False))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(255, 255, 255, 200)))
                p.drawEllipse(QPointF(ex, ey), 4, 4)

        # ── 9. Relationship lens ───────────────────────────────────
        # Over the finished scene, because dimming what is unrelated is half
        # of what makes the relationship readable.
        if self._lens != LENS_NONE:
            self._draw_relation_overlay(p, w, h, tenants)

        self._draw_hover_card(p, w, h)

        if self._link_mode:
            self._draw_link_mode_banner(p, w)

        # Above everything — the spotlight dims the scene it sits over.
        if self._search_active:
            self._draw_search_overlay(p, w, h)

        p.end()

    # ── Ambient Background ─────────────────────────────────────────

    def _draw_ambient_glow(self, p, w, h):
        """Soft diffused radial glow behind the scene."""
        cx, cy = w / 2, h / 2 - 40
        r = max(w, h) * 0.5
        g = QRadialGradient(QPointF(cx, cy), r)
        # Breathing pulse, in the window theme's own accent so Nebula glows
        # violet and Tokyo Night blue; the house theme glows RPMote cyan.
        pulse = 0.5 + 0.15 * math.sin(self._time * 0.8)
        base = QColor(self._theme.get("accent", "#22d3ee"))
        mid, deep = base.darker(150), base.darker(320)
        g.setColorAt(0, QColor(base.red(), base.green(), base.blue(), int(35 * pulse)))
        g.setColorAt(0.4, QColor(mid.red(), mid.green(), mid.blue(), int(18 * pulse)))
        g.setColorAt(0.7, QColor(deep.red(), deep.green(), deep.blue(), int(12 * pulse)))
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(g))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(0, 0, w, h)

    # ── Matrix Rain ────────────────────────────────────────────────

    def _draw_matrix_rain(self, p, w: int, h: int):
        """Full-canvas digital rain — the Matrix / glitch background."""
        # Solid black canvas
        p.fillRect(0, 0, w, h, QColor(0, 0, 0))

        # Lazy-init if columns not yet created
        if not self._matrix_rain or self._matrix_rain_w != w:
            self._init_matrix_rain(w, h)

        char_h = 16
        col_w  = 16
        rain_font = QFont("Courier New", 9, QFont.Weight.Bold)
        p.setFont(rain_font)
        p.setPen(Qt.PenStyle.NoPen)

        for col in self._matrix_rain:
            x      = col["x"]
            head_y = col["y"]
            chars  = col["chars"]
            length = col["length"]

            for j, ch in enumerate(chars):
                char_y = head_y - j * char_h
                if char_y < -char_h or char_y > h + char_h:
                    continue

                if j == 0:
                    # Head — bright white-green flash
                    color = QColor(200, 255, 210, 255)
                elif j == 1:
                    color = QColor(100, 255, 130, 230)
                else:
                    # Exponential fade toward dark green
                    fade  = max(0.0, 1.0 - (j / length) ** 0.7)
                    alpha = int(220 * fade)
                    green = int(80 + 175 * fade)
                    color = QColor(0, green, int(20 * fade), alpha)

                p.setPen(color)
                p.drawText(
                    QRectF(x - col_w // 2, char_y - char_h, col_w, char_h),
                    Qt.AlignmentFlag.AlignCenter,
                    ch,
                )

        # Subtle dark scanline overlay — makes it feel like a CRT
        scan_alpha = 18
        scan_color = QColor(0, 0, 0, scan_alpha)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(scan_color))
        y = 0
        while y < h:
            p.drawRect(0, y, w, 1)
            y += 3

        # Faint green vignette to add depth
        vg = QRadialGradient(QPointF(w / 2, h / 2), max(w, h) * 0.7)
        vg.setColorAt(0,   QColor(0, 0, 0, 0))
        vg.setColorAt(0.7, QColor(0, 0, 0, 0))
        vg.setColorAt(1.0, QColor(0, 0, 0, 120))
        p.setBrush(QBrush(vg))
        p.drawRect(0, 0, w, h)

    # ── Dot Wave ───────────────────────────────────────────────────

    def _draw_dotwave(self, p: QPainter, w: int, h: int):
        """Animated 3-D sine-wave dot grid rendered with software perspective.

        Faithfully recreates the Three.js DottedSurface component:
          · 30 × 45 particle grid driven by two orthogonal sine waves
          · Perspective camera at (0, 355, 1100), FOV 60°
          · Near-fade: giants near the camera dissolve smoothly
          · Far-fade: fog blends distant dots into the darkness
          · Painter's algorithm ensures near dots overdraw far ones
        """
        # ── Deep-space background ────────────────────────────────────
        p.fillRect(0, 0, w, h, QColor(3, 7, 18))

        # ── Grid constants ───────────────────────────────────────────
        AMOUNTX: int   = 30
        AMOUNTY: int   = 45
        SEP:     float = 115.0
        count:   float = self._dw_count

        # ── Camera (mirrors Three.js PerspectiveCamera config) ────────
        CAM_Y:   float = 355.0
        CAM_Z:   float = 1100.0
        fov_rad: float = math.radians(60)
        f:       float = (h / 2.0) / math.tan(fov_rad / 2.0)
        cx_s:    float = w / 2.0
        cy_s:    float = h / 2.0

        # ── Project every grid point ──────────────────────────────────
        # Each entry: (depth, screen_x, screen_y, dot_size, alpha)
        dots: list = []

        for iy in range(AMOUNTY):
            z3d   = iy * SEP - (AMOUNTY * SEP) / 2.0
            depth = CAM_Z - z3d
            if depth < 5.0:          # clip behind / at camera
                continue

            for ix in range(AMOUNTX):
                x3d = ix * SEP - (AMOUNTX * SEP) / 2.0

                # Two-frequency sine wave — identical to Three.js animate()
                y3d = (math.sin((ix + count) * 0.3) * 50.0 +
                       math.sin((iy + count) * 0.5) * 50.0)

                # Perspective divide
                sx = x3d            / depth * f + cx_s
                sy = -(y3d - CAM_Y) / depth * f + cy_s

                # Coarse screen-space cull (skip fully invisible dots)
                if sx < -30 or sx > w + 30 or sy < -30 or sy > h + 30:
                    continue

                # Size attenuated by depth (sizeAttenuation: true in Three.js)
                REF  = 650.0
                size = max(1.0, min(20.0, 7.0 * REF / depth))

                # Alpha: fade in near the camera (avoid giant blobs) AND
                #        fade out in the distance (fog effect, matching Three.js fog)
                near_t = min(1.0, (depth - 5.0)   / 120.0)   # 0→1 over 5..125
                far_t  = max(0.0, 1.0 - (depth - 1800.0) / 2200.0)  # 1→0 over 1800..4000
                alpha  = int(200 * near_t * far_t)
                if alpha < 8:
                    continue

                dots.append((depth, sx, sy, size, alpha))

        # ── Painter's algorithm: draw far dots first ──────────────────
        dots.sort(reverse=True)          # largest depth (farthest) first
        p.setPen(Qt.PenStyle.NoPen)

        for _, sx, sy, size, alpha in dots:
            r = size / 2.0

            # Bright near-white core (matches Three.js vertex color 200,200,200)
            p.setBrush(QBrush(QColor(200, 218, 255, min(255, alpha + 55))))
            p.drawEllipse(QPointF(sx, sy), r * 0.38, r * 0.38)

            # Soft electric-blue outer bloom
            p.setBrush(QBrush(QColor(88, 166, 255, max(4, alpha // 4))))
            p.drawEllipse(QPointF(sx, sy), r, r)

        # ── Depth vignette — adds cinematic depth to the scene ────────
        vg = QRadialGradient(QPointF(cx_s, cy_s), max(w, h) * 0.75)
        vg.setColorAt(0.0, QColor(0, 0, 0,  0))
        vg.setColorAt(0.6, QColor(0, 0, 0,  0))
        vg.setColorAt(1.0, QColor(0, 0, 0, 80))
        p.setBrush(QBrush(vg))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(0, 0, w, h)

    # ── Aurora ─────────────────────────────────────────────────────

    def _draw_aurora(self, p: QPainter, w: int, h: int):
        """Aurora borealis — software approximation of the AnoAI GLSL shader.

        The original uses Three.js ShaderMaterial with:
          · fbm (3-octave fractal Brownian motion) noise field
          · 35 cosine-driven streamers: v = p + cos(i²+(t+p.x*0.08)*0.025 + i*vec2(13,11))*3.5
          · Per-streamer colour: (0.1+0.3*sin, 0.3+0.5*cos, 0.7+0.3*sin) → teal/cyan/blue
          · Intensity: exp(sin(i²+t*0.8));  thinness: smoothstep(0,1,i/35)*0.6
          · Tonemapping: tanh(pow(o/100, 1.6)) * 1.5

        QPainter translation:
          · Additive CompositionMode_Plus recreates HDR glow accumulation
          · 20 animated bezier streamers, each rendered in 3 width passes
          · Colour + intensity formulas lifted directly from the shader
          · Ambient horizon bands reproduce the fbm background field
          · Top/bottom depth gradients simulate the shader's vignette
        """
        t = self._aurora_time

        # ── Deep-space background ────────────────────────────────────
        p.fillRect(0, 0, w, h, QColor(2, 8, 16))

        # Additive blending — the core of aurora glow accumulation
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)

        # ── Horizon ambient glow bands (fbm background approximation) ─
        p.setPen(Qt.PenStyle.NoPen)
        for bi in range(4):
            by  = h * (0.38 + bi * 0.07)
            bh  = h * 0.14
            ba  = int(18 + 12 * math.sin(t * 0.3 + bi * 1.3))
            bg_g = QLinearGradient(0.0, by - bh * 0.5, 0.0, by + bh)
            bg_g.setColorAt(0.00, QColor(0, 0,   0,   0))
            bg_g.setColorAt(0.45, QColor(6, 200, 190, max(0, ba)))
            bg_g.setColorAt(1.00, QColor(0, 0,   0,   0))
            p.setBrush(QBrush(bg_g))
            p.drawRect(0, int(by - bh * 0.5), w, int(bh * 1.5))

        # ── 20 animated streamers ────────────────────────────────────
        NUM      = 20
        band_top = h * 0.18
        band_bot = h * 0.82
        span     = band_bot - band_top

        for i in range(NUM):
            fi = i / (NUM - 1)

            # Colour — lifted from shader's auroraColors formula
            r = max(0, min(255, int((0.10 + 0.30 * abs(math.sin(i * 0.20 + t * 0.40))) * 255)))
            g = max(0, min(255, int((0.30 + 0.50 * abs(math.cos(i * 0.30 + t * 0.50))) * 255)))
            b = max(0, min(255, int((0.70 + 0.30 * abs(math.sin(i * 0.40 + t * 0.30))) * 255)))

            # Intensity — shader: exp(sin(i*i + t*0.8))
            intensity = math.exp(math.sin(i * i * 0.035 + t * 0.80))
            intensity = max(0.05, min(3.8, intensity))

            # Thinness — shader: smoothstep(0,1,i/35)*0.6
            thin = fi * 0.65 + 0.08

            # ── Bezier streamer shape ─────────────────────────────────
            # Control points mirror the shader's cos-field equations:
            #   v = p + cos(i²+(t)*0.025 + i*[13,11])*3.5
            y_b  = band_top + span * (0.3 + 0.6 * math.sin(i * 0.74 + t * 0.05))

            ph1  = i * i * 0.020 + t * 0.025 + i * 13.0
            ph2  = i * i * 0.020 + t * 0.025 + i * 11.0

            sx   = -w * 0.02
            sy   =  y_b + math.sin(i * 0.90 + t * 0.08)  * span * 0.25
            c1x  =  w  * 0.30
            c1y  =  y_b + math.cos(ph1)                    * span * 0.45
            c1y +=         math.sin(t * 0.50 + i * 2.10)  * span * 0.08
            c2x  =  w  * 0.70
            c2y  =  y_b + math.cos(ph2)                    * span * 0.40
            c2y +=         math.cos(t * 0.40 - i * 1.80)  * span * 0.09
            ex   =  w  * 1.02
            ey   =  y_b + math.cos(i * 0.70 + t * 0.09)  * span * 0.22

            path = QPainterPath()
            path.moveTo(sx, sy)
            path.cubicTo(c1x, c1y, c2x, c2y, ex, ey)

            base_a = int(min(200, 85 * intensity * thin))
            if base_a < 4:
                continue

            p.setBrush(Qt.BrushStyle.NoBrush)

            # Pass 1 — wide outer glow (additive haze)
            pen1 = QPen(QColor(r, g, b, max(3, base_a // 5)),
                        max(3.0, 55.0 * thin * min(intensity, 2.5)))
            pen1.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen1)
            p.drawPath(path)

            # Pass 2 — medium glow
            pen2 = QPen(QColor(r, g, b, max(8, base_a // 2)),
                        max(1.5, 16.0 * thin * min(intensity, 2.0)))
            pen2.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen2)
            p.drawPath(path)

            # Pass 3 — bright luminous core
            pen3 = QPen(QColor(min(255, r + 90), min(255, g + 60), min(255, b + 15),
                               min(255, base_a * 2)),
                        max(0.8, 3.5 * thin))
            pen3.setCapStyle(Qt.PenCapStyle.FlatCap)
            p.setPen(pen3)
            p.drawPath(path)

        # ── Restore normal blending ──────────────────────────────────
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        p.setPen(Qt.PenStyle.NoPen)

        # Top — deep space fade (keeps branding area clean)
        tg = QLinearGradient(0.0, 0.0, 0.0, h * 0.18)
        tg.setColorAt(0.0, QColor(2, 8, 16, 220))
        tg.setColorAt(1.0, QColor(0, 0,  0,   0))
        p.setBrush(QBrush(tg))
        p.drawRect(0, 0, w, int(h * 0.18))

        # Bottom — horizon fade
        bg_b = QLinearGradient(0.0, h * 0.76, 0.0, float(h))
        bg_b.setColorAt(0.0, QColor(0, 0,   0,   0))
        bg_b.setColorAt(1.0, QColor(2, 8,  16, 235))
        p.setBrush(QBrush(bg_b))
        p.drawRect(0, int(h * 0.76), w, h)

    # ── Empty State ────────────────────────────────────────────────

    #: The host can show its own welcome card instead of this hint.
    show_empty_hint = True

    def _draw_empty_state(self, p, w, h):
        if not self.show_empty_hint:
            return
        p.setPen(QColor(148, 163, 184, 200))
        p.setFont(QFont("Segoe UI", 16, QFont.Weight.Light))
        p.drawText(QRectF(0, h * 0.35, w, 30), Qt.AlignmentFlag.AlignCenter,
                   "No connections yet")
        p.setFont(QFont("Segoe UI", 11))
        p.setPen(QColor(148, 163, 184, 170))
        p.drawText(QRectF(0, h * 0.35 + 34, w, 24), Qt.AlignmentFlag.AlignCenter,
                   "Press  +  or  Ctrl+N  to create one")

    def _draw_collapsed_hint(self, p, cx, cy, w):
        text = "Click the mote to expand all connections"
        font = QFont("Segoe UI", 10)
        p.setFont(font)
        if self._dashboard_theme != "default":
            # Matrix rain and aurora move behind this line; a soft scrim
            # keeps it readable without boxing it in on the plain theme.
            fm = QFontMetrics(font)
            tw = fm.horizontalAdvance(text) + 28
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(8, 10, 18, 170))
            p.drawRoundedRect(QRectF((w - tw) / 2, cy + 95, tw, 22), 11, 11)
        p.setPen(QColor(148, 163, 184, 200))
        p.drawText(QRectF(0, cy + 95, w, 22), Qt.AlignmentFlag.AlignCenter, text)

    # ── Center Orb ─────────────────────────────────────────────────

    def _draw_center_orb(self, p, cx, cy):
        """Frosted glass orb with glow halo and branding.

        *cx*/*cy* are **screen** coordinates, so the orb pans and zooms with
        the rest of the scene.
        """
        skin = self._skin
        _mono = skin.mono
        z = self._zoom
        pulse = (1.0 + 0.08 * math.sin(self._time * 1.5)
                 if self._gs["animate"] else 1.0)
        halo_r = 68 * pulse * z

        # Outer glow halo (animated)
        hg = QRadialGradient(QPointF(cx, cy), halo_r)
        hg.setColorAt(0,   QColor(*skin.orb_halo[0]))
        hg.setColorAt(0.5, QColor(*skin.orb_halo[1]))
        hg.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(hg))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), halo_r, halo_r)

        # Frosted glass circle.  The gradient is anchored to the orb, so the
        # cache key has to include where the orb currently sits.
        inner_r = 44 * z
        orb_key = (round(cx), round(cy), round(inner_r, 1), self._dashboard_theme)
        if self._orb_glass_gradient is None or self._orb_glass_key != orb_key:
            self._orb_glass_key = orb_key
            glass = QRadialGradient(QPointF(cx - 8 * z, cy - 12 * z), inner_r * 1.1)
            glass.setColorAt(0,   QColor(*skin.orb_glass[0]))
            glass.setColorAt(0.7, QColor(*skin.orb_glass[1]))
            glass.setColorAt(1.0, QColor(*skin.orb_glass[2]))
            self._orb_glass_gradient = glass

        border_col = QColor(*skin.orb_border)
        p.setBrush(QBrush(self._orb_glass_gradient))
        p.setPen(QPen(border_col, 1.5))
        p.drawEllipse(QPointF(cx, cy), inner_r, inner_r)

        # The mote: a soft glow in the skin's colour around a white-hot
        # core, with four faint rays so it reads as the ✦ in the wordmark.
        # It breathes with the halo.
        mote = QColor(skin.mote)
        mr, mgn, mb = mote.red(), mote.green(), mote.blue()
        glow_r = 26 * pulse * z
        glow = QRadialGradient(QPointF(cx, cy), glow_r)
        glow.setColorAt(0.0,  QColor(mr, mgn, mb, 210))
        glow.setColorAt(0.45, QColor(mr, mgn, mb, 80))
        glow.setColorAt(1.0,  QColor(mr, mgn, mb, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPointF(cx, cy), glow_r, glow_r)
        ray_l, ray_w = 18 * z, 2.2 * z
        p.setBrush(QColor(255, 255, 255, 140))
        for horizontal in (True, False):
            ray = QPainterPath()
            if horizontal:
                ray.moveTo(cx - ray_l, cy); ray.lineTo(cx, cy - ray_w)
                ray.lineTo(cx + ray_l, cy); ray.lineTo(cx, cy + ray_w)
            else:
                ray.moveTo(cx, cy - ray_l); ray.lineTo(cx + ray_w, cy)
                ray.lineTo(cx, cy + ray_l); ray.lineTo(cx - ray_w, cy)
            ray.closeSubpath()
            p.drawPath(ray)
        core_r = 9 * z
        core = QRadialGradient(QPointF(cx - core_r * 0.25, cy - core_r * 0.3),
                               core_r * 1.25)
        core.setColorAt(0.0, QColor(255, 255, 255))
        core.setColorAt(0.5, QColor(235, 252, 255))
        core.setColorAt(1.0, mote)
        p.setBrush(QBrush(core))
        p.drawEllipse(QPointF(cx, cy), core_r, core_r)

        # Branding below orb
        if not getattr(self, '_show_all_connections', False):
            name_col    = QColor(*skin.text)
            sub_col     = QColor(*skin.text_dim)
            p.setPen(name_col)
            p.setFont(QFont("Courier New" if _mono else "Segoe UI",
                            max(7, int(13 * z)), QFont.Weight.Bold))
            p.drawText(QRectF(cx - 70 * z, cy + 50 * z, 140 * z, 22 * z),
                       Qt.AlignmentFlag.AlignCenter, "RPMote")
            p.setPen(sub_col)
            p.setFont(QFont("Courier New" if _mono else "Segoe UI",
                            max(6, int(9 * z))))
            p.drawText(QRectF(cx - 100 * z, cy + 70 * z, 200 * z, 18 * z),
                       Qt.AlignmentFlag.AlignCenter, "Remote Connection Manager")
        self._center_orb_rect = QRectF(cx - inner_r, cy - inner_r,
                                       inner_r * 2, inner_r * 2)

    # ── Connection Branches ────────────────────────────────────────

    def _status_allowance(self) -> float:
        """Logical px a pill must set aside for its reachability dot."""
        if not (self._ping_enabled and self._gs.get("show_status", True)):
            return 0.0
        return STATUS_DOT_R * 2 + 7.0

    def _branch_logical_width(self, conn: dict) -> float:
        """Pill width in *logical* px (i.e. at zoom 1.0).

        Layout and rendering both derive from this single number, so rings can
        never plan for a narrower node than the one actually drawn — which is
        what made labels collide when zoomed out.  The status dot is part of
        that budget: switching probing on widens the pill rather than letting
        the dot land on top of the hostname.
        """
        label = self._branch_label(conn)
        allowance = self._status_allowance()
        key = (label, self._dashboard_theme, allowance)
        cached = self._branch_cache.get(key)
        if cached is not None:
            return cached
        font = QFont("Courier New" if self._skin.mono
                     else "Segoe UI", BRANCH_FONT_PT, BRANCH_FONT_WEIGHT)
        fm = QFontMetrics(font)
        pad, dot = 9.0, BRANCH_DOT_R
        if label:
            text_w = min(float(fm.horizontalAdvance(label)), BRANCH_W - 34)
            # Font advances do not scale perfectly linearly once hinting kicks
            # in at small point sizes, so carry a little slack; without it the
            # label gets needlessly elided the moment you zoom out.
            w = pad * 2 + dot * 2 + 6 + text_w * 1.10 + 4 + allowance
        else:
            w = pad * 2 + dot * 2
        w = max(w, 26.0)
        self._branch_cache[key] = w
        return w

    def _branch_slot(self, conns: list) -> float:
        """Widest pill in this tenant — the angular slot every ring must honour."""
        if not conns:
            return 26.0
        return max(self._branch_logical_width(c) for c in conns)

    def _branch_rings(self, count: int, cw: float, ch: float,
                      slot_w: float = BRANCH_W) -> list:
        """Concentric ring plan for *count* branches: [(radius, items), ...].

        The first ring clears the tile's *corner* (not just its half-width) plus
        half a pill, so nodes never sit on top of the tenant card.  Each ring
        then holds as many nodes as its circumference can fit.
        """
        if count <= 0:
            return []
        # `branch_spread` only ever pushes branches further out; the base radius
        # is already the tightest arrangement that cannot overlap.
        spread = max(float(self._gs["branch_spread"]), 1.0)
        base = math.hypot(cw, ch) / 2 + slot_w / 2 + 16
        r = base * spread
        step = (BRANCH_H + 22) * spread
        slot = slot_w + 16
        rings, left = [], count
        while left > 0:
            cap = max(2, int((2 * math.pi * r) // slot))
            take = min(cap, left)
            rings.append((r, take))
            left -= take
            r += max(step, BRANCH_H + 10)
        return rings

    def _branch_positions(self, tenant: str, conns: list, tx: float, ty: float,
                          cw: float, ch: float, start_angle: float) -> list:
        """Logical (conn, x, y) for every branch node of one tenant."""
        out = []
        rings = self._branch_rings(len(conns), cw, ch, self._branch_slot(conns))
        idx = 0
        for ring_i, (radius, take) in enumerate(rings):
            # Odd rings sit a half-slot around so nodes stagger between rings.
            a0 = start_angle + (math.pi / max(take, 1) if ring_i % 2 else 0.0)
            for k in range(take):
                if idx >= len(conns):
                    break
                conn = conns[idx]
                idx += 1
                ang = a0 + 2 * math.pi * k / max(take, 1)
                bx = tx + radius * math.cos(ang)
                by = ty + radius * math.sin(ang)
                override = self._conn_positions.get(str(conn.get("id", "")))
                if override:
                    bx, by = tx + override[0], ty + override[1]
                out.append((conn, bx, by))
        return out

    def _branch_label(self, conn: dict) -> str:
        mode = self._gs["branch_labels"]
        name = str(conn.get("name", "") or "").rstrip().rstrip("●• ")
        host = str(conn.get("host", "") or "")
        if mode == "none":
            return ""
        if mode == "host":
            return host or name
        if mode == "name_host":
            return f"{name}  ·  {host}" if host else name
        return name or host

    def _draw_status_dot(self, p, cx: float, cy: float, state: str,
                         radius: float, scale: float = 1.0):
        """Filled state dot with a soft halo, drawn over a node's corner.

        A checking state breathes so a slow sweep looks busy rather than stuck.
        """
        color = self._status_color(state)
        if color is None:
            return
        r = max(radius * scale, 1.6)
        alpha = 255
        if state == STATE_CHECKING and self._gs["animate"]:
            alpha = int(150 + 105 * abs(math.sin(self._time * 3.0)))

        halo = QRadialGradient(QPointF(cx, cy), r * 2.6)
        halo.setColorAt(0.0, QColor(color.red(), color.green(), color.blue(), 110))
        halo.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(halo))
        p.drawEllipse(QPointF(cx, cy), r * 2.6, r * 2.6)

        # A dark collar keeps the dot legible over a pale tile border.
        p.setBrush(QBrush(QColor(12, 14, 24, 220)))
        p.drawEllipse(QPointF(cx, cy), r + 1.3, r + 1.3)
        p.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(), alpha)))
        p.drawEllipse(QPointF(cx, cy), r, r)

    def _status_border(self, state: str, fallback: QColor,
                       width: float) -> tuple[QColor, float]:
        """Border colour/width for a node, tinted by reachability when asked."""
        if not self._gs.get("status_rings", True):
            return fallback, width
        color = self._status_color(state)
        if color is None or state == STATE_UP:
            # Green is the resting case; tinting every healthy node just adds
            # noise, so only trouble states change the outline.
            return fallback, width
        return QColor(color.red(), color.green(), color.blue(), 200), max(width, 1.6)

    # ── Viewport culling ───────────────────────────────────────────

    def _rect_visible(self, rect: QRectF, halo: float = 0.0) -> bool:
        """Does *rect* (grown by its widest glow) touch the viewport?"""
        if halo:
            rect = rect.adjusted(-halo, -halo, halo, halo)
        return self._view_rect.intersects(rect)

    def _segment_visible(self, x1, y1, x2, y2) -> bool:
        """Could a flow line between these points touch the viewport?

        The chord's bounding box is grown by the curve's bow (~18 px) plus
        the glow pen, which also keeps the box from collapsing: QRectF's
        ``intersects`` treats a zero-width or zero-height rect as empty and
        never overlapping, so a perfectly vertical or horizontal branch (the
        first spoke in free mode, or any of a four-spoke tenant's) would
        otherwise be culled while its endpoints were plainly on screen.
        """
        pad = SEGMENT_CULL_PAD
        return self._view_rect.intersects(
            QRectF(QPointF(x1, y1), QPointF(x2, y2)).normalized()
            .adjusted(-pad, -pad, pad, pad))

    def _pill_text(self, fam: str, label: str, ideal: int,
                   text_w: float) -> tuple:
        """(font, elided label) for a pill, measured once per distinct key."""
        key = (fam, label, ideal, int(text_w))
        hit = self._pill_text_cache.get(key)
        if hit is not None:
            return hit
        font = QFont(fam, ideal, BRANCH_FONT_WEIGHT)
        fm = QFontMetrics(font)
        elided = ""
        # Point sizes are quantised, so the scaled font can still measure a
        # shade wider than the geometry budget.  Step down a point (rather than
        # chopping the hostname) before falling back to an ellipsis.
        for size in (ideal, ideal - 1, ideal - 2):
            if size < BRANCH_FONT_MIN:
                break
            trial = QFont(fam, size, BRANCH_FONT_WEIGHT)
            tfm = QFontMetrics(trial)
            if tfm.horizontalAdvance(label) <= text_w:
                font, fm, elided = trial, tfm, label
                break
        else:
            elided = fm.elidedText(label, Qt.TextElideMode.ElideMiddle,
                                   int(text_w))
        if not elided:
            elided = fm.elidedText(label, Qt.TextElideMode.ElideMiddle,
                                   int(text_w))
        self._pill_text_cache[key] = (font, elided)
        return font, elided

    def _draw_connection_node(self, p, x: float, y: float, conn: dict,
                              tenant_color: QColor, is_active: bool,
                              is_hovered: bool, is_selected: bool,
                              scale: float, anchor: tuple | None = None,
                              peek: float = 0.0, draw: bool = True) -> QRectF:
        """Small frosted pill for a single connection hanging off its tenant.

        *anchor* is the tenant's screen centre and *peek* how strongly a
        hovered tenant is asking its zoomed-out dots to show their names.
        With *draw* False only the hit rect is worked out.
        """
        proto = str(conn.get("protocol", "ssh") or "ssh").lower()
        pc = QColor(PROTOCOL_COLORS.get(proto, tenant_color.name()))
        if self._skin.pill_accent is not None:
            pc = QColor(*self._skin.pill_accent)

        label = self._branch_label(conn)
        h = BRANCH_H * scale
        pad = 9.0 * scale
        dot = BRANCH_DOT_R * scale
        zoomed_out = scale < BRANCH_LABEL_MIN_SCALE
        if zoomed_out:
            # Too far out for text — collapse to a dot so the estate stays legible.
            label = ""
        # Geometry is *always* the logical width times the current scale, so the
        # drawn pill matches exactly what _branch_rings laid out.
        w = (self._branch_logical_width(conn) * scale if label
             else max(pad * 2 + dot * 2, 14.0))

        rect = QRectF(x - w / 2, y - h / 2, w, h)
        # The hit rect is all an off-screen pill needs; the halo is the
        # widest thing drawn around it.
        if not draw or not self._rect_visible(rect, halo=w * 0.75):
            return rect

        ep = make_ep("conn", str(conn.get("id", "")))
        is_hovered = is_hovered or ep in self._hover_targets()
        hover = self._hover_amount(ep)
        lift = self._hover_lift(ep, HOVER_LIFT_DOT if zoomed_out else HOVER_LIFT_PILL)
        if hover > 0.0:
            # A soft glow blooms under it, wider than it is tall like the pill.
            self._draw_bloom(p, rect, pc, hover, 0.55 if label else 1.1, 1.0)
        lifted = self._lifted(p, x, y, lift)

        # Active pulse halo — the one part that moves every frame.
        if is_active:
            pulse = 0.7 + 0.3 * math.sin(self._time * 2.5) if self._gs["animate"] else 1.0
            hg = QRadialGradient(QPointF(x, y), w * 0.75)
            hg.setColorAt(0, QColor(pc.red(), pc.green(), pc.blue(), int(60 * pulse)))
            hg.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(hg))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(x, y), w * 0.75, h * 1.1)
        elif is_selected:
            hg = QRadialGradient(QPointF(x, y), w * 0.65)
            hg.setColorAt(0, QColor(pc.red(), pc.green(), pc.blue(), 42))
            hg.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(hg))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(x, y), w * 0.65, h * 0.95)

        state = self.conn_state(conn.get("id", ""))
        # A checking dot breathes, so it cannot live in the static picture.
        live_dot = state == STATE_CHECKING and self._gs["animate"]
        dpr = float(self.devicePixelRatioF())
        key = (label, pc.rgb(), self._dashboard_theme, round(scale, 3),
               bool(is_active), bool(is_selected), bool(is_hovered),
               state, live_dot, dpr, self._status_allowance())
        pm = self._pill_pixmap_cache.get(key)
        if pm is None:
            if len(self._pill_pixmap_cache) > PILL_PIXMAP_CACHE_MAX:
                self._pill_pixmap_cache.clear()
            pm = self._render_pill(label, pc, w, h, pad, dot, scale,
                                   is_active, is_selected, is_hovered,
                                   state, not live_dot, dpr,
                                   HOVER_OVERSAMPLE if is_hovered else 1.0)
            self._pill_pixmap_cache[key] = pm
        # Blit on the device-pixel grid so text stays crisp.
        m = PILL_PIXMAP_MARGIN
        bx = round((rect.left() - m) * dpr) / dpr
        by = round((rect.top() - m) * dpr) / dpr
        p.drawPixmap(QPointF(bx, by), pm)

        if live_dot:
            self._draw_pill_status_dot(p, rect, y, label, pad, scale, state)
        self._draw_hover_accent(p, rect, pc, ep, is_hovered, h / 2)
        if lifted:
            p.restore()

        shown = QRectF(x - w * lift / 2, y - h * lift / 2, w * lift, h * lift)
        self._drawn_nodes[ep] = (shown, pc, shown.height() / 2)
        if zoomed_out and state in (STATE_DOWN, STATE_DEGRADED):
            # The dots someone is looking for when they zoom out to scan the
            # estate: those keep a readable name.
            self._draw_problem_tag(p, x, shown.top() - 2.0, conn, state, dpr)
        elif zoomed_out and max(hover, peek) > 0.0:
            # Any other dot says who it is when it, or its tenant, is hovered.
            self._draw_peek_tag(p, shown, conn, pc, max(hover, peek), anchor, dpr)
        return rect

    def _draw_faded_conn(self, args: tuple, fade: float) -> QRectF:
        """_draw_connection_node at *fade* opacity (a hover elsewhere)."""
        if fade >= 1.0:
            return self._draw_connection_node(*args)
        p = args[0]
        p.save()
        p.setOpacity(fade)
        try:
            return self._draw_connection_node(*args)
        finally:
            p.restore()

    #: Name tags for problem machines at low zoom, by (text, state, dpr).
    _problem_tag_cache: dict = {}

    def _draw_problem_tag(self, p, x: float, bottom: float, conn: dict, state: str,
                          dpr: float):
        """A screen-sized name tag above a down (red) or degraded (amber) dot."""
        name = (self._branch_label(conn) or str(conn.get("host", "") or "")).strip()
        if not name:
            return
        key = (name, state, dpr)
        pm = self._problem_tag_cache.get(key)
        if pm is None:
            if len(self._problem_tag_cache) > 512:
                self._problem_tag_cache.clear()
            font = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
            fm = QFontMetrics(font)
            text = fm.elidedText(name, Qt.TextElideMode.ElideRight, 150)
            w = fm.horizontalAdvance(text) + 14
            h = fm.height() + 4
            pm = QPixmap(int(math.ceil((w + 2) * dpr)), int(math.ceil((h + 2) * dpr)))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            pp = QPainter(pm)
            pp.setRenderHint(QPainter.RenderHint.Antialiasing)
            pp.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            border = QColor("#f87171") if state == STATE_DOWN else QColor("#fbbf24")
            box = QRectF(1, 1, w, h)
            # A dark scrim under the text: readable over every theme,
            # animated ones included.
            pp.setPen(QPen(border, 1.0))
            pp.setBrush(QColor(10, 12, 20, 225))
            pp.drawRoundedRect(box, h / 2, h / 2)
            pp.setFont(font)
            pp.setPen(QColor("#f8fafc"))
            pp.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
            pp.end()
            self._problem_tag_cache[key] = pm
        w = pm.width() / dpr
        h = pm.height() / dpr
        bx = round((x - w / 2) * dpr) / dpr
        by = round((bottom - h) * dpr) / dpr
        p.drawPixmap(QPointF(bx, by), pm)

    def _draw_pill_status_dot(self, p, rect: QRectF, y: float, label: str,
                              pad: float, scale: float, state: str):
        """Reachability dot mirrors the protocol dot across the pill.  A
        dot-only pill (zoomed right out) has no room inside, so its state
        goes on the shoulder instead."""
        if self._status_allowance() and label:
            self._draw_status_dot(p, rect.right() - pad - STATUS_DOT_R * scale,
                                  y, state, STATUS_DOT_R, scale)
        else:
            self._draw_status_dot(p, rect.right() - 1.0, rect.top() + 1.0,
                                  state, STATUS_DOT_R * 0.8, scale)

    def _render_pill(self, label: str, pc: QColor, w: float, h: float,
                     pad: float, dot: float, scale: float,
                     is_active: bool, is_selected: bool, is_hovered: bool,
                     state: str, with_dot: bool, dpr: float,
                     oversample: float = 1.0) -> QPixmap:
        """Paint one pill body into a pixmap: glass, border (tinted by
        *state*), protocol dot, label and -- unless it animates -- the
        status dot.  Same primitives the pill was always drawn with, just
        drawn once.  *oversample* > 1 keeps a hovered (enlarged) pill sharp."""
        m = PILL_PIXMAP_MARGIN
        pw, ph = w + 2 * m, h + 2 * m
        res = dpr * oversample
        pm = QPixmap(int(math.ceil(pw * res)), int(math.ceil(ph * res)))
        pm.setDevicePixelRatio(res)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        rect = QRectF(m, m, w, h)
        y = rect.center().y()

        text_w = max(
            w - (pad * 2 + dot * 2 + 6 * scale) - self._status_allowance() * scale,
            0.0)
        fam = "Courier New" if self._skin.mono else "Segoe UI"
        # The floor has to stay below the geometry's own floor: a font that
        # stops shrinking while the pill keeps shrinking overflows its box.
        ideal = max(BRANCH_FONT_MIN, min(11, int(round(BRANCH_FONT_PT * scale))))
        if label and text_w > 8:
            font, elided = self._pill_text(fam, label, ideal, text_w)
        else:
            font, elided = None, ""

        # Glass pill -- one gradient per theme in object space, mapped onto
        # each pill's rect by the painter, instead of a fresh one per pill.
        bgk = ("pill", self._dashboard_theme)
        bg = self._node_gradient_cache.get(bgk)
        if bg is None:
            bg = QLinearGradient(0, 0, 0, 1)
            bg.setCoordinateMode(QLinearGradient.CoordinateMode.ObjectBoundingMode)
            bg.setColorAt(0, QColor(*self._skin.pill_bg[0]))
            bg.setColorAt(1, QColor(*self._skin.pill_bg[1]))
            self._node_gradient_cache[bgk] = bg
        if is_active:
            border, bw = QColor(pc.red(), pc.green(), pc.blue(), 235), 1.6
        elif is_selected:
            border, bw = QColor(pc.red(), pc.green(), pc.blue(), 215), 1.5
        elif is_hovered:
            border, bw = QColor(pc.red(), pc.green(), pc.blue(), 175), 1.3
        else:
            border, bw = QColor(pc.red(), pc.green(), pc.blue(), 135), 1.2
        border, bw = self._status_border(state, border, bw)

        # Shadow and dark rim first, so the glass reads as a solid object on
        # any backdrop rather than a tint of whatever is animating behind it.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(*NODE_SHADOW)))
        p.drawRoundedRect(rect.translated(0, 1.2), h / 2, h / 2)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(*NODE_RIM), bw + 2.0))
        p.drawRoundedRect(rect, h / 2, h / 2)

        p.setBrush(QBrush(bg))
        p.setPen(QPen(border, bw))
        p.drawRoundedRect(rect, h / 2, h / 2)

        # Protocol dot
        dx = rect.left() + pad + dot
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(pc.red(), pc.green(), pc.blue(), 45)))
        p.drawEllipse(QPointF(dx, y), dot * 1.9, dot * 1.9)
        p.setBrush(QBrush(pc))
        p.drawEllipse(QPointF(dx, y), dot, dot)

        # Label
        if elided:
            p.setFont(font)
            if self._skin.pill_text is not None:
                tcol = QColor(*self._skin.pill_text)
            elif is_selected or is_hovered or is_active:
                tcol = QColor(255, 255, 255)
            else:
                tcol = QColor(228, 231, 240)
            p.setPen(tcol)
            tx0 = dx + dot + 6 * scale
            p.drawText(QRectF(tx0, rect.top(), rect.right() - tx0 - pad * 0.6, h),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       elided)

        if with_dot:
            self._draw_pill_status_dot(p, rect, y, label, pad, scale, state)
        p.end()
        return pm

    # ── Flow Lines ─────────────────────────────────────────────────

    def _line_path(self, x1, y1, x2, y2, wobble: bool = True) -> QPainterPath:
        """Build the edge geometry for the user's chosen line style."""
        path = QPainterPath()
        self._append_line_path(path, x1, y1, x2, y2, wobble)
        return path

    def _append_line_path(self, path: QPainterPath, x1, y1, x2, y2,
                          wobble: bool = True) -> None:
        """Add one edge to *path* (which may already hold others)."""
        style = self._gs["line_style"]
        path.moveTo(x1, y1)
        dx, dy = x2 - x1, y2 - y1

        if style == "straight":
            path.lineTo(x2, y2)
        elif style == "elbow":
            # Right-angled routing: travel the dominant axis first, with a small
            # rounded corner so it still reads as a designed diagram.
            if abs(dx) >= abs(dy):
                mx = x1 + dx * 0.5
                path.lineTo(mx, y1)
                path.lineTo(mx, y2)
            else:
                my = y1 + dy * 0.5
                path.lineTo(x1, my)
                path.lineTo(x2, my)
            path.lineTo(x2, y2)
        else:  # curved (default)
            t_off = (math.sin(self._time * 0.9 + x2 * 0.01) * 18
                     if (wobble and self._gs["animate"]) else 0.0)
            path.cubicTo(x1 + dx * 0.3 + t_off, y1 + dy * 0.1,
                         x1 + dx * 0.7 - t_off, y1 + dy * 0.9,
                         x2, y2)

    def _round_pen(self, color: QColor, alpha: int, width: float) -> QPen:
        """A round-capped pen, built once per (colour, alpha, width).

        Hundreds of branch lines share a handful of colours; rebuilding a
        pen with its cap and join for each of them every frame added up."""
        key = (color.rgb(), alpha, width)
        pen = self._pen_cache.get(key)
        if pen is None:
            pen = QPen(QColor(color.red(), color.green(), color.blue(), alpha), width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            self._pen_cache[key] = pen
        return pen

    def _draw_flow_line(self, p, x1, y1, x2, y2, color, width: float = 1.5,
                        glow: bool = True, particles: int = 3):
        """Edge with a soft glow, a dark casing and (optionally) travelling
        particles."""
        path = self._line_path(x1, y1, x2, y2)

        p.setBrush(Qt.BrushStyle.NoBrush)
        if glow:
            p.setPen(self._round_pen(color, 22, width * 4))
            p.drawPath(path)

        p.setPen(self._casing_pen(width))
        p.drawPath(path)
        p.setPen(self._round_pen(color, FLOW_LINE_ALPHA, width))
        p.drawPath(path)

        if not (self._gs["particles"] and self._gs["animate"]) or particles <= 0:
            return

        for i in range(particles):
            t = (i / particles + self._time * 0.12) % 1.0
            pt = path.pointAtPercent(max(0.0, min(t, 1.0)))
            pr = 3.5 - i * 0.5
            sprite = self._particle_sprite(color, pr)
            half = sprite.width() / sprite.devicePixelRatio() / 2
            p.drawPixmap(QPointF(pt.x() - half, pt.y() - half), sprite)

    def _particle_sprite(self, color: QColor, pr: float) -> QPixmap:
        """A glowing dot, rendered once per (colour, size) and blitted.

        A hundred-odd particles a frame each used to build a radial
        gradient and fill two ellipses; the picture never changes, only
        where it sits on the line."""
        dpr = float(self.devicePixelRatioF())
        key = (color.rgb(), pr, dpr)
        pm = self._sprite_cache.get(key)
        if pm is not None:
            return pm
        r = pr * 3
        size = int(math.ceil(2 * r + 2))
        pm = QPixmap(int(math.ceil(size * dpr)), int(math.ceil(size * dpr)))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        sp = QPainter(pm)
        sp.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(size / 2, size / 2)
        pg = QRadialGradient(c, r)
        pg.setColorAt(0, QColor(color.red(), color.green(), color.blue(), 140))
        pg.setColorAt(1.0, QColor(color.red(), color.green(), color.blue(), 0))
        sp.setPen(Qt.PenStyle.NoPen)
        sp.setBrush(QBrush(pg))
        sp.drawEllipse(c, r, r)
        sp.setBrush(QBrush(QColor(color.red(), color.green(), color.blue(), 200)))
        sp.drawEllipse(c, pr, pr)
        sp.end()
        self._sprite_cache[key] = pm
        return pm

    # ── Custom Tenant ↔ Tenant Links ───────────────────────────────

    def _link_color(self, link: dict) -> QColor:
        """Explicit colour if the user set one, else blend the two tenants'."""
        if link.get("color"):
            c = QColor(link["color"])
            if c.isValid():
                return c
        if self._skin.link_color is not None:
            return QColor(*self._skin.link_color)
        ca = QColor(self._tenant_colors.get(link.get("a"), "#a78bfa"))
        cb = QColor(self._tenant_colors.get(link.get("b"), "#a78bfa"))
        return QColor((ca.red() + cb.red()) // 2,
                      (ca.green() + cb.green()) // 2,
                      (ca.blue() + cb.blue()) // 2)

    @staticmethod
    def _edge_point(cx: float, cy: float, hw: float, hh: float,
                    tx: float, ty: float) -> tuple[float, float]:
        """Where the ray (cx,cy)->(tx,ty) leaves the hw x hh box around c."""
        dx, dy = tx - cx, ty - cy
        if abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return cx, cy
        sx = hw / abs(dx) if abs(dx) > 1e-6 else float("inf")
        sy = hh / abs(dy) if abs(dy) > 1e-6 else float("inf")
        t = min(sx, sy, 1.0)
        return cx + dx * t, cy + dy * t

    def _draw_tenant_link(self, p, x1, y1, x2, y2, link: dict,
                          selected: bool, hovered: bool) -> QPainterPath:
        """Draw one purely-visual tenant relationship and return its hit path."""
        col = self._link_color(link)
        path = self._line_path(x1, y1, x2, y2, wobble=False)

        if selected:
            alpha, width = 235, 2.6
        elif hovered:
            alpha, width = 200, 2.2
        else:
            alpha, width = 150, 1.8

        style = link.get("style", "dashed")
        pen = QPen(QColor(col.red(), col.green(), col.blue(), alpha), width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        if style == "dashed":
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([6, 4])
        elif style == "dotted":
            pen.setStyle(Qt.PenStyle.CustomDashLine)
            pen.setDashPattern([1, 3])

        # Halo so the link stays readable over busy backgrounds.
        halo = QPen(QColor(col.red(), col.green(), col.blue(),
                           70 if selected else 26), width * 3.5)
        halo.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(halo)
        p.drawPath(path)
        p.setPen(pen)
        p.drawPath(path)

        # End caps mark it as a deliberate, user-drawn relationship.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(col.red(), col.green(), col.blue(), alpha)))
        cap = 3.2 if not selected else 4.2
        p.drawEllipse(QPointF(x1, y1), cap, cap)
        p.drawEllipse(QPointF(x2, y2), cap, cap)

        # The label is drawn later, on top of the nodes — a user-written
        # annotation is no use hidden behind a tenant tile it happens to cross.
        label = str(link.get("label", "") or "")
        if label:
            # Keep the whole path: where along it the label can sit without
            # covering a node is only knowable once the nodes are laid out.
            self._pending_link_labels.append((path, label, QColor(col)))

        stroker = QPainterPathStroker()
        stroker.setWidth(14)
        return stroker.createStroke(path)

    #: Positions along a link to try for its label, midpoint first.
    _LABEL_STOPS = (0.5, 0.38, 0.62, 0.28, 0.72, 0.18, 0.82, 0.1, 0.9)

    def _place_link_label(self, path: QPainterPath, tw: float, th: float) -> QRectF:
        """Find a spot on *path* where a tw x th label covers nothing.

        A link often runs straight over a third tenant, and burying either the
        label or that tenant's name is bad.  Slide the label along the path
        instead, preferring the midpoint and giving up gracefully.
        """
        tiles = [r for r, _tn in self._node_rects]
        pills = [r for r, _c, _tn in self._conn_rects]

        def box_at(t: float) -> QRectF:
            pt = path.pointAtPercent(t)
            return QRectF(pt.x() - tw / 2, pt.y() - th / 2, tw, th)

        for obstacles in (tiles + pills, tiles):
            for t in self._LABEL_STOPS:
                box = box_at(t)
                if not any(box.intersects(o) for o in obstacles):
                    return box
        return box_at(0.5)

    def _draw_link_labels(self, p):
        """Second pass: link annotations sit above every node."""
        if not self._pending_link_labels:
            return
        font = QFont("Courier New" if self._skin.mono
                     else "Segoe UI", 8)
        fm = QFontMetrics(font)
        for path, label, col in self._pending_link_labels:
            tw = fm.horizontalAdvance(label) + 12
            th = fm.height() + 4
            box = self._place_link_label(path, tw, th)
            p.setBrush(QBrush(QColor(16, 18, 30, 235)))
            p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 150), 1))
            p.drawRoundedRect(box, th / 2, th / 2)
            p.setFont(font)
            p.setPen(QColor(220, 223, 236))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)
        self._pending_link_labels = []

    # ── Glass Tenant Node ──────────────────────────────────────────

    def _tile_layout(self, name: str, num: int, scale: float,
                     compact: bool) -> tuple:
        """Everything about a tile's box that does not depend on where it is.

        Measuring the word-wrapped name is the expensive part; a tile's
        layout only changes with its name, count, zoom or theme, so it is
        measured once per distinct key rather than 40 times a frame.
        """
        key = (name, num, round(scale, 3), compact, self._dashboard_theme)
        hit = self._tile_layout_cache.get(key)
        if hit is not None:
            return hit
        cw, ch = self._tile_size(name, num)
        cw *= scale
        ch *= scale
        if compact:
            # Fixed single row — nothing to pre-measure.
            pad_x = 12.0 * scale
            content_w = cw - 2 * pad_x
            name_size = max(6, min(14, int(round(COMPACT_TILE_FONT_PT * scale))))
            name_font = QFont("Segoe UI", name_size, QFont.Weight.DemiBold)
            name_h = ch
            count_h = 0.0
            pad_y = 0.0
        else:
            # ── Pre-measure layout so the tile is always tall enough ──
            pad_x = max(8.0, cw * 0.07)
            content_w = cw - 2 * pad_x
            name_size = max(9, min(12, int((9 + num * 0.3) * max(min(scale, 1.25), 0.9))))
            name_font = QFont("Segoe UI", name_size, QFont.Weight.DemiBold)
            fm = QFontMetrics(name_font)
            measured = fm.boundingRect(0, 0, int(content_w), 0,
                                       Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap,
                                       name)
            name_h = max(20.0, float(measured.height()) + 6)
            count_h = max(14.0, ch * 0.18)
            icon_h = 18.0
            pad_y = max(5.0, ch * 0.08)
            min_ch = pad_y * 2 + name_h + count_h + icon_h
            ch = max(ch, min_ch)
            pad_y = max(5.0, ch * 0.08)   # recalc with final ch
        layout = (cw, ch, pad_x, content_w, name_size, name_font,
                  name_h, count_h, pad_y)
        self._tile_layout_cache[key] = layout
        return layout

    def _draw_glass_node(self, p, x, y, name, connections, hex_color,
                         is_selected=False, is_hovered=False,
                         is_active=False, scale=1.0) -> QRectF:
        """Frosted glass card for a tenant group with dynamic sizing."""
        c = QColor(hex_color)
        num = len(connections)
        active_green = QColor(74, 222, 128)  # #4ade80

        compact = self._tile_compact()
        (cw, ch, pad_x, content_w, name_size, name_font,
         name_h, count_h, pad_y) = self._tile_layout(name, num, scale, compact)
        content_top = y - ch / 2 + pad_y
        # ─────────────────────────────────────────────────────────────

        rect = QRectF(x - cw / 2, y - ch / 2, cw, ch)
        # Off-screen: the hit rect is all anyone needs.  The active halo is
        # the widest thing drawn around a tile.
        if not self._rect_visible(rect, halo=cw * 0.9):
            return rect

        skin = self._skin

        # ── Glow colour source ──────────────────────────────────────
        # A themed skin glows in its accent; the default in the tenant's
        # brand colour.  (Matrix glows a touch brighter than it draws.)
        if skin.mono:
            gc = QColor(*(skin.hover or skin.accent))
        elif skin.accent is not None:
            gc = QColor(*skin.accent)
        else:
            gc = c

        # ── Active connection glow (pulsing halo) ──────────────────
        if is_active:
            pulse = (0.7 + 0.3 * math.sin(self._time * 2.5)
                     if self._gs["animate"] else 1.0)
            ar, ag_, ab, aa = skin.active_glow
            ag_col = QColor(ar, ag_, ab, int(aa * pulse))
            active_glow = QRadialGradient(QPointF(x, y), cw * 0.9)
            active_glow.setColorAt(0,   ag_col)
            active_glow.setColorAt(0.5, QColor(ag_col.red(), ag_col.green(), ag_col.blue(), int(ag_col.alpha() * 0.4)))
            active_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(active_glow))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(x, y), cw * 0.9, ch * 1.0)

        # ── Selection glow, and the hover bloom ────────────────────
        if is_selected:
            sel_glow = QRadialGradient(QPointF(x, y), cw * 0.8)
            sel_glow.setColorAt(0,   QColor(gc.red(), gc.green(), gc.blue(), 60))
            sel_glow.setColorAt(0.6, QColor(gc.red(), gc.green(), gc.blue(), 22))
            sel_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            p.setBrush(QBrush(sel_glow))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(x, y), cw * 0.8, ch * 0.9)
        ep = make_ep("tenant", name)
        hover = self._hover_amount(ep)
        lift = self._hover_lift(ep, HOVER_LIFT_TILE)
        if hover > 0.0:
            self._draw_bloom(p, rect, gc, hover, 0.32, 0.6)
        shown = QRectF(x - cw * lift / 2, y - ch * lift / 2, cw * lift, ch * lift)
        self._drawn_nodes[ep] = (shown, gc, 14.0 * lift)
        lifted = self._lifted(p, x, y, lift)

        state = self.tenant_state(name)
        # A pulsing border or a breathing status dot cannot be a still
        # picture; everything else about a tile is, so it is painted once.
        live = is_active or (state == STATE_CHECKING and self._gs["animate"])
        body_args = (x, y, rect, name, num, c, gc, compact, cw, ch, pad_x,
                     content_w, name_size, name_font, name_h, count_h,
                     content_top, is_selected, is_hovered, is_active, scale,
                     state)
        if live:
            self._paint_tile_body(p, *body_args)
            self._draw_hover_accent(p, rect, gc, ep, is_hovered, 14)
            if lifted:
                p.restore()
            return rect

        dpr = float(self.devicePixelRatioF())
        key = (name, num, hex_color, self._dashboard_theme, round(scale, 3),
               compact, bool(is_selected), bool(is_hovered), state, dpr)
        pm = self._tile_pixmap_cache.get(key)
        # The soft outer glow (cw*0.6 x ch*0.7 radii) is the widest still
        # part of a tile.
        pw = cw * 1.2 + 2 * PILL_PIXMAP_MARGIN
        ph = ch * 1.4 + 2 * PILL_PIXMAP_MARGIN
        if pm is None:
            if len(self._tile_pixmap_cache) > PILL_PIXMAP_CACHE_MAX:
                self._tile_pixmap_cache.clear()
            res = dpr * (HOVER_OVERSAMPLE if is_hovered else 1.0)
            pm = QPixmap(int(math.ceil(pw * res)), int(math.ceil(ph * res)))
            pm.setDevicePixelRatio(res)
            pm.fill(Qt.GlobalColor.transparent)
            pp = QPainter(pm)
            pp.setRenderHint(QPainter.RenderHint.Antialiasing)
            pp.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            # Same drawing code, same coordinates: shift the painter so the
            # tile's centre lands in the middle of the pixmap.
            pp.translate(pw / 2 - x, ph / 2 - y)
            self._paint_tile_body(pp, *body_args)
            pp.end()
            self._tile_pixmap_cache[key] = pm
        bx = round((x - pw / 2) * dpr) / dpr
        by = round((y - ph / 2) * dpr) / dpr
        p.drawPixmap(QPointF(bx, by), pm)
        self._draw_hover_accent(p, rect, gc, ep, is_hovered, 14)
        if lifted:
            p.restore()
        return rect

    def _paint_tile_body(self, p, x, y, rect, name, num, c, gc, compact,
                         cw, ch, pad_x, content_w, name_size, name_font,
                         name_h, count_h, content_top, is_selected,
                         is_hovered, is_active, scale, state):
        """The still part of a tenant tile: glow, glass, border, text,
        icons and status dot.  Drawn straight to the canvas for tiles that
        animate, otherwise once into the tile's cached pixmap."""
        skin = self._skin
        _mono = skin.mono
        _tinted = skin.accent is not None

        # ── Soft outer glow ────────────────────────────────────────
        glow = QRadialGradient(QPointF(x, y), cw * 0.6)
        glow.setColorAt(0,   QColor(gc.red(), gc.green(), gc.blue(), skin.tile_glow_alpha))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setBrush(QBrush(glow))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(x, y), cw * 0.6, ch * 0.7)

        # ── Glass card background ──────────────────────────────────
        ng_cache = self._node_gradient_cache
        bgk = ('bg', is_selected, self._dashboard_theme)   # theme-aware cache key
        if bgk not in ng_cache:
            glass_bg = QLinearGradient(0, 0, 1, 1)
            glass_bg.setCoordinateMode(QLinearGradient.CoordinateMode.ObjectBoundingMode)
            stops = skin.tile_bg_sel if is_selected else skin.tile_bg
            glass_bg.setColorAt(0,   QColor(*stops[0]))
            glass_bg.setColorAt(0.5, QColor(*stops[1]))
            glass_bg.setColorAt(1,   QColor(*stops[2]))
            ng_cache[bgk] = glass_bg
        # ── Shadow and dark rim (see NODE_RIM) ──────────────────────
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(*NODE_SHADOW)))
        p.drawRoundedRect(rect.translated(0, 1.5), 14, 14)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(*NODE_RIM), 3.2))
        p.drawRoundedRect(rect, 14, 14)

        p.setBrush(QBrush(ng_cache[bgk]))

        # ── Border ─────────────────────────────────────────────────
        if _tinted:
            if is_active:
                pulse_a = (0.7 + 0.3 * math.sin(self._time * 2.5)
                           if self._gs["animate"] else 1.0)
                br, bg_, bb, ba = skin.border_active
                border_col   = QColor(br, bg_, bb, int(ba * pulse_a))
                border_width = 1.8
            elif is_selected:
                border_col   = QColor(*skin.border_sel)
                border_width = 1.6
            elif is_hovered:
                border_col   = QColor(*skin.border_hover)
                border_width = 1.2
            else:
                border_col   = QColor(*skin.border_idle)
                border_width = 1.0
        else:
            if is_active:
                border_alpha = 130 if is_selected else 100
                border_width = 1.8 if is_selected else 1.4
                border_col   = QColor(74, 222, 128, border_alpha)
            else:
                border_alpha = 165 if is_selected else (135 if is_hovered else 105)
                border_width = 1.6 if is_selected else 1.2
                border_col   = QColor(c.red(), c.green(), c.blue(), border_alpha)
        p.setPen(QPen(border_col, border_width))
        p.drawRoundedRect(rect, 14, 14)

        # ── Top-edge highlight ─────────────────────────────────────
        highlight_rect = QRectF(rect.x() + 1, rect.y() + 1, rect.width() - 2, 1)
        hr, hg_, hb, ha_sel, ha_idle = skin.highlight
        hl_col = QColor(hr, hg_, hb, ha_sel if is_selected else ha_idle)
        p.setBrush(QBrush(hl_col))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(highlight_rect, 1, 1)

        # ── Tenant name ────────────────────────────────────────────
        if _mono:
            name_font = QFont("Courier New", name_size, QFont.Weight.Bold)
        name_col = QColor(*skin.text)

        if compact:
            self._draw_compact_tile_content(
                p, rect, name, num, name_font, name_col, gc, scale)
            self._draw_status_dot(p, rect.right() - 8 * scale,
                                  rect.top() + 7 * scale,
                                  state, STATUS_TILE_DOT_R * 0.9, scale)
            return

        p.setPen(name_col)
        p.setFont(name_font)
        name_rect = QRectF(x - cw / 2 + pad_x, content_top, content_w, name_h)
        p.drawText(name_rect,
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                   name)

        # ── Connection count badge ─────────────────────────────────
        count_text  = f"{num} connection{'s' if num != 1 else ''}"
        count_col   = QColor(*skin.count_text)
        count_size  = max(8, min(10, int(8 * max(min(scale, 1.2), 0.9))))
        count_font  = QFont("Courier New" if _mono else "Segoe UI", count_size)
        p.setPen(count_col)
        p.setFont(count_font)
        count_top   = content_top + name_h
        count_rect  = QRectF(x - cw / 2 + pad_x, count_top, content_w, count_h)
        p.drawText(count_rect, Qt.AlignmentFlag.AlignCenter, count_text)

        # Mini device icons (inline row below the badge)
        icon_count = min(num, 5)
        icon_start_x = x - (icon_count * 14) / 2
        p.setFont(QFont("Segoe UI", 9))
        for i in range(icon_count):
            ix = icon_start_x + i * 14
            iy = count_top + count_h
            # Tiny glow behind each icon (use gc so dotwave gets blue glow)
            p.setBrush(QBrush(QColor(gc.red(), gc.green(), gc.blue(), 25)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(ix + 6, iy + 5), 7, 7)
            p.drawPixmap(QPointF(ix + 1.5, iy + 1.5), _mini_monitor(p))

        self._draw_status_dot(p, rect.right() - 8 * scale, rect.top() + 8 * scale,
                              state, STATUS_TILE_DOT_R, scale)

    def _draw_compact_tile_content(self, p, rect: QRectF, name: str, num: int,
                                   name_font: QFont, name_col: QColor,
                                   accent: QColor, scale: float):
        """One row: tenant dot, name, connection count.

        Used when the branch nodes already spell out what is inside the tenant,
        so the count badge and icon strip would only repeat themselves.
        """
        pad = 12.0 * scale
        dot_r = 4.5 * scale
        count_text = str(num)

        tkey = ("chip", num, round(scale, 3), self._dashboard_theme)
        chip = self._tile_text_cache.get(tkey)
        if chip is None:
            count_font = QFont("Courier New" if self._skin.mono
                               else "Segoe UI", max(6, min(11, int(round(9 * scale)))))
            cfm = QFontMetrics(count_font)
            chip = (count_font,
                    cfm.horizontalAdvance(count_text) + 14 * scale,
                    cfm.height() + 4 * scale)
            self._tile_text_cache[tkey] = chip
        count_font, chip_w, chip_h = chip

        # Tenant dot
        dx = rect.left() + pad + dot_r
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(accent.red(), accent.green(), accent.blue(), 55)))
        p.drawEllipse(QPointF(dx, rect.center().y()), dot_r * 2.0, dot_r * 2.0)
        p.setBrush(QBrush(accent))
        p.drawEllipse(QPointF(dx, rect.center().y()), dot_r, dot_r)

        # Count chip, right-aligned
        chip = QRectF(rect.right() - pad - chip_w,
                      rect.center().y() - chip_h / 2, chip_w, chip_h)
        p.setBrush(QBrush(QColor(accent.red(), accent.green(), accent.blue(), 38)))
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 90), 1))
        p.drawRoundedRect(chip, chip_h / 2, chip_h / 2)
        p.setFont(count_font)
        p.setPen(QColor(*self._skin.chip_text))
        p.drawText(chip, Qt.AlignmentFlag.AlignCenter, count_text)

        # Name fills whatever is left between the two
        text_left = dx + dot_r + 8 * scale
        text_w = max(chip.left() - 8 * scale - text_left, 0.0)
        if text_w > 6:
            nkey = ("name", name, name_font.family(), name_font.pointSize(),
                    int(text_w))
            shown = self._tile_text_cache.get(nkey)
            if shown is None:
                shown = QFontMetrics(name_font).elidedText(
                    name, Qt.TextElideMode.ElideRight, int(text_w))
                self._tile_text_cache[nkey] = shown
            p.setFont(name_font)
            p.setPen(name_col)
            p.drawText(QRectF(text_left, rect.top(), text_w, rect.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                       shown)

    # ── Mouse Interaction ──────────────────────────────────────────

    # ── Hit Testing ────────────────────────────────────────────────

    def _tenant_at(self, pos: QPointF) -> str | None:
        for rect, tenant_name in reversed(self._node_rects):
            if rect.contains(pos):
                return tenant_name
        return None

    def _conn_at(self, pos: QPointF):
        """Return (conn_dict, tenant) for the branch node under *pos*."""
        for rect, conn, tenant in reversed(self._conn_rects):
            if rect.contains(pos):
                return conn, tenant
        return None

    def _ep_at(self, pos: QPointF) -> str | None:
        """Endpoint reference for whatever linkable node is under *pos*."""
        tenant = self._tenant_at(pos)
        if tenant:
            return make_ep("tenant", tenant)
        hit = self._conn_at(pos)
        if hit:
            cid = str(hit[0].get("id", ""))
            if cid:
                return make_ep("conn", cid)
        return None

    def _link_at(self, pos: QPointF) -> int | None:
        for hit_path, index in reversed(self._link_rects):
            if hit_path.contains(pos):
                return index
        return None

    # ── Mouse Interaction ──────────────────────────────────────────

    def mousePressEvent(self, event):
        if self._search_active:
            pos = event.position()
            row = self._search_row_at(pos)
            if row is not None:
                self._search_index = row
                self._commit_search()
                event.accept()
                return
            if self._search_rect.contains(pos):
                event.accept()          # a click on the bar itself does nothing
                return
            # Anywhere else means the user is done searching; close, then let
            # the click land on the canvas as it normally would.
            self.close_search()

        pos = event.position()
        btn = event.button()
        self._press_button = btn

        # Right / middle button always starts a pan.  Whether it *was* a pan or
        # just a click is decided on release (see mouseReleaseEvent).
        if btn in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            self._panning = True
            self._pan_anchor = QPointF(pos)
            self._pan_origin = QPointF(self._pan)
            self._pan_moved = False
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            event.accept()
            return

        if btn != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        self._highlighted_conn = None
        alt = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)

        # ── Drawing a visual link (tenant or connection) ───────────
        if alt or self._link_mode:
            ep = self._ep_at(pos)
            if self._link_source and ep and ep != self._link_source:
                self.add_link(self._link_source, ep)
                self._link_source = None
                self._link_cursor = None
                self._link_hover_target = None
            elif ep:
                self._link_source = ep
                self._link_cursor = QPointF(pos)
            else:
                self._link_source = None
                self._link_cursor = None
            self.update()
            event.accept()
            return

        # ── Centre orb toggles expand/collapse (hub mode only) ─────
        if not self.is_free_mode and self._center_orb_rect.contains(pos):
            restored = self._restore_tenant_view()
            self._show_all_connections = not self._show_all_connections
            if self._show_all_connections:
                self._visible_tenants = {
                    (c.get("tenant", "Default") or "Default")
                    for c in self._connections if isinstance(c, dict)
                }
                if self._selected_node and self._selected_node not in self._visible_tenants:
                    self._selected_node = None
                # Branch rings make an expanded estate far larger than the
                # viewport, so frame it instead of dumping tiles off-screen.
                self._layout_cache = None
                if not restored:
                    self.fit_to_view()
            else:
                self._visible_tenants = set()
                self._selected_node = None
                if not restored:
                    self.reset_view()
            self.update()
            self.view_state_changed.emit(self.get_view_state())
            return

        # ── Branch node ────────────────────────────────────────────
        hit = self._conn_at(pos)
        if hit:
            conn, tenant = hit
            self._selected_conn = str(conn.get("id", ""))
            self._selected_node = tenant
            self._conn_drag_target = conn
            self._drag_started = False
            self._drag_offset = QPointF(0, 0)
            for rect, c, _t in self._conn_rects:
                if c is conn:
                    self._drag_offset = QPointF(pos.x() - rect.center().x(),
                                                pos.y() - rect.center().y())
                    break
            self.update()
            return

        # ── Tenant tile — start a potential drag ───────────────────
        for rect, tenant_name in reversed(self._node_rects):
            if rect.contains(pos):
                self._drag_target = tenant_name
                self._drag_started = False
                self._drag_offset = QPointF(pos.x() - rect.center().x(),
                                            pos.y() - rect.center().y())
                self._selected_node = tenant_name
                self._selected_conn = None
                self._selected_link = None
                self.update()
                return

        # ── A custom link ──────────────────────────────────────────
        li = self._link_at(pos)
        if li is not None:
            self._selected_link = li
            self._selected_node = None
            self._selected_conn = None
            self.update()
            return

        # ── Empty canvas: deselect, and left-drag pans too ─────────
        self._selected_node = None
        self._selected_conn = None
        self._selected_link = None
        self._panning = True
        self._pan_anchor = QPointF(pos)
        self._pan_origin = QPointF(self._pan)
        self._pan_moved = False
        self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
        self.update()

    def mouseMoveEvent(self, event):
        pos = event.position()

        # ── Panning ────────────────────────────────────────────────
        if self._panning:
            pan_buttons = (Qt.MouseButton.RightButton
                           | Qt.MouseButton.MiddleButton
                           | Qt.MouseButton.LeftButton)
            if not (event.buttons() & pan_buttons):
                # A release we never saw (focus loss, a menu stealing the
                # grab): stop panning rather than gluing the graph to the mouse.
                self._panning = False
                self._pan_moved = False
                self.setCursor(QCursor(Qt.CursorShape.CrossCursor if self._link_mode
                                       else Qt.CursorShape.ArrowCursor))
                return
            delta = pos - self._pan_anchor
            if abs(delta.x()) + abs(delta.y()) > 3:
                self._pan_moved = True
            self._pan = QPointF(self._pan_origin.x() + delta.x(),
                                self._pan_origin.y() + delta.y())
            self.update()
            return

        # ── Live link preview ──────────────────────────────────────
        if self._link_source is not None:
            self._link_cursor = QPointF(pos)
            ep = self._ep_at(pos)
            self._link_hover_target = ep if (ep and ep != self._link_source) else None
            self.update()
            return

        # ── Dragging a branch node ─────────────────────────────────
        if self._conn_drag_target and event.buttons() & Qt.MouseButton.LeftButton:
            tenant = self._conn_drag_target.get("tenant", "Default") or "Default"
            if not self._drag_started:
                self._freeze_branch_positions(tenant)
            self._drag_started = True
            lx, ly = self._to_logical(pos.x() - self._drag_offset.x(),
                                      pos.y() - self._drag_offset.y())
            anchor = self._tenant_logical_pos(tenant)
            if anchor:
                cid = str(self._conn_drag_target.get("id", ""))
                self._conn_positions[cid] = (lx - anchor[0], ly - anchor[1])
                self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
                self.update()
            return

        # ── Dragging a tenant tile ─────────────────────────────────
        if self._drag_target and event.buttons() & Qt.MouseButton.LeftButton:
            if not self._drag_started:
                self._freeze_tenant_positions()
            self._drag_started = True
            lx, ly = self._to_logical(pos.x() - self._drag_offset.x(),
                                      pos.y() - self._drag_offset.y())
            self._pin(self._drag_target, lx, ly)
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            self.update()
            return

        # ── Hover ──────────────────────────────────────────────────
        hovered_node = self._tenant_at(pos)
        hit = self._conn_at(pos) if hovered_node is None else None
        hovered_conn = str(hit[0].get("id", "")) if hit else None
        hovered_link = (self._link_at(pos)
                        if (hovered_node is None and hovered_conn is None) else None)

        changed = (hovered_node != self._hovered_node
                   or hovered_conn != self._hovered_conn
                   or hovered_link != self._hovered_link)
        self._hovered_node = hovered_node
        self._hovered_conn = hovered_conn
        self._hovered_link = hovered_link

        if changed:
            # The hover card shows this now; a screen reader still reads it.
            self.setToolTip("")
            self.setAccessibleDescription(
                self._hover_tooltip(hovered_node, hovered_conn))
            if self._link_mode:
                shape = Qt.CursorShape.CrossCursor
            elif hovered_node or hovered_conn or hovered_link is not None:
                shape = Qt.CursorShape.PointingHandCursor
            else:
                shape = Qt.CursorShape.ArrowCursor
            self.setCursor(QCursor(shape))
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hovered_node = None
        self._hovered_conn = None
        self._hovered_link = None
        if not self._gs["animate"]:
            self._hover_levels.clear()
        self.setToolTip("")
        self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        pos = event.position()
        btn = event.button()

        # ── End of a pan (or a plain right-click) ──────────────────
        if btn in (Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
            was_pan = self._pan_moved
            self._panning = False
            self._pan_moved = False
            self.setCursor(QCursor(Qt.CursorShape.CrossCursor if self._link_mode
                                   else Qt.CursorShape.ArrowCursor))
            if was_pan:
                self.view_state_changed.emit(self.get_view_state())
            elif btn == Qt.MouseButton.RightButton:
                self._show_context_menu(pos)
            self.update()
            return

        if btn != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return

        # Alt-drag released over another tenant completes the link.
        if self._link_source is not None:
            ep = self._ep_at(pos)
            if ep and ep != self._link_source:
                self.add_link(self._link_source, ep)
                self._link_source = None
                self._link_cursor = None
                self._link_hover_target = None
                self.update()
            return

        if self._panning:
            was_pan = self._pan_moved
            self._panning = False
            self._pan_moved = False
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            if was_pan:
                self.view_state_changed.emit(self.get_view_state())
            return

        if self._conn_drag_target:
            if self._drag_started:
                self.conn_positions_changed.emit(
                    {k: list(v) for k, v in self._conn_positions.items()})
            else:
                self.connection_clicked.emit(dict(self._conn_drag_target))
            self._conn_drag_target = None
            self._drag_started = False
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            self.update()
            return

        if self._drag_target:
            if self._drag_started:
                self.node_positions_changed.emit(self._position_snapshot())
            else:
                if self.collapsing_active():
                    self.toggle_tenant_expanded(self._drag_target)
                else:
                    self.focus_tenant(self._drag_target)
                self.tenant_clicked.emit(self._drag_target)
            self._drag_target = None
            self._drag_started = False
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            self.update()
            return

        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._conn_at(event.position())
            if hit:
                self.connection_activated.emit(dict(hit[0]))
                event.accept()
                return
            li = self._link_at(event.position())
            if li is not None:
                self._rename_link(li)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event):
        """Zoom toward the cursor so the point under it stays put."""
        delta = event.angleDelta().y()
        if delta == 0:
            event.accept()
            return
        old = self._zoom
        minimum = 0.001 if self._tenant_camera is not None else self._min_zoom
        new = max(minimum,
                  min(self._max_zoom, old * (1.12 if delta > 0 else 1 / 1.12)))
        if abs(new - old) < 0.0001:
            event.accept()
            return
        pos = event.position()
        lx, ly = self._to_logical(pos.x(), pos.y())
        self._zoom = new
        nx, ny = self._to_screen(lx, ly)
        self._pan = QPointF(self._pan.x() + (pos.x() - nx),
                            self._pan.y() + (pos.y() - ny))
        self.update()
        self.view_state_changed.emit(self.get_view_state())
        event.accept()

    def keyPressEvent(self, event):
        key = event.key()
        ctrl = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        # Ctrl+F is handled here rather than as a window-level QShortcut so it
        # only fires while the canvas has focus — the SSH terminal keeps its
        # own Ctrl+F search.
        if ctrl and key == Qt.Key.Key_F:
            self.open_search()
            event.accept()
            return
        # The find bar eats plain typing, so it gets first refusal on every key
        # ahead of the single-letter canvas shortcuts below.
        if self._search_active and self._handle_search_key(event):
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            if self._link_source is not None or self._link_mode:
                self._link_source = None
                self._link_cursor = None
                self._link_hover_target = None
                self.set_link_mode(False)
                self.link_mode_changed.emit(False)
                event.accept()
                return
            # A lens is a mode too, and Esc is where a user reaches to leave one.
            if self.clear_relation_lens():
                event.accept()
                return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._selected_link is not None:
                self.remove_tenant_link(self._selected_link)
                event.accept()
                return
        if key == Qt.Key.Key_Home:
            self.reset_view()
            event.accept()
            return
        if key == Qt.Key.Key_F:
            self.fit_to_view()
            event.accept()
            return
        step = 60
        if key == Qt.Key.Key_Left:
            self.pan_by(step, 0)
        elif key == Qt.Key.Key_Right:
            self.pan_by(-step, 0)
        elif key == Qt.Key.Key_Up:
            self.pan_by(0, step)
        elif key == Qt.Key.Key_Down:
            self.pan_by(0, -step)
        else:
            super().keyPressEvent(event)
            return
        self.view_state_changed.emit(self.get_view_state())
        event.accept()

    def _hover_tooltip(self, tenant: str | None, conn_id: str | None) -> str:
        """What the cursor is over, plus its reachability if we are probing."""
        if conn_id:
            conn = self._conn_by_id(conn_id)
            if conn is None:
                return ""
            lines = [self._ep_label(make_ep("conn", conn_id))]
            host = str(conn.get("host", "") or "")
            port = conn.get("port", "")
            if host:
                lines.append(f"{host}:{port}" if port else host)
            proto = str(conn.get("protocol", "") or "").upper()
            owner = str(conn.get("tenant", "Default") or "Default")
            lines.append(f"{proto}  ·  {owner}" if proto else owner)
            if self._ping_enabled:
                lines.append(describe_status(self.conn_status(conn_id)))
            related = self._relationship_lines(conn_id)
            if related:
                lines.append("")
                lines.extend(related)
            return "\n".join(x for x in lines if x is not None)

        if tenant:
            count = sum(1 for c in self._connections
                        if isinstance(c, dict)
                        and (c.get("tenant", "Default") or "Default") == tenant)
            lines = [tenant, f"{count} connection{'s' if count != 1 else ''}"]
            if self._ping_enabled:
                state = self.tenant_state(tenant)
                lines.append(STATE_LABELS.get(state, state))
            return "\n".join(lines)
        return ""

    def _tenant_logical_pos(self, tenant: str):
        """Current logical position of a tenant tile, or None if not laid out."""
        for rect, tn in self._node_rects:
            if tn == tenant:
                return self._to_logical(rect.center().x(), rect.center().y())
        return None

    # ── Context Menus ──────────────────────────────────────────────

    def _show_context_menu(self, pos: QPointF):
        tenant = self._tenant_at(pos)
        if tenant:
            self._tenant_context_menu(pos, tenant)
            return
        hit = self._conn_at(pos)
        if hit:
            self._connection_context_menu(pos, hit[0], hit[1])
            return
        li = self._link_at(pos)
        if li is not None:
            self._link_context_menu(pos, li)
            return
        self._canvas_context_menu(pos)

    def _tenant_context_menu(self, pos: QPointF, tenant: str):
        menu = QMenu(self)
        gp = self.mapToGlobal(pos.toPoint())

        act_open = menu.addAction("Open in Sidebar")
        act_new = menu.addAction("New Connection Here…")
        menu.addSeparator()

        self_ep = make_ep("tenant", tenant)
        link_menu = menu.addMenu("Link To")
        others = sorted(t for t in self._all_tenants() if t != tenant)
        linked = {self._other_end(l, self_ep) for l in self._tenant_links
                  if self_ep in (l.get("a"), l.get("b"))}
        if others:
            for other in others:
                a = link_menu.addAction(other)
                a.setCheckable(True)
                a.setChecked(make_ep("tenant", other) in linked)
                a.setData(("link", other))
        else:
            link_menu.addAction("No other tenants").setEnabled(False)
        link_menu.addSeparator()
        act_link_from = link_menu.addAction("Start Link From Here…")

        act_color = menu.addAction("Set Tenant Colour…")

        menu.addSeparator()
        tenant_ids = [str(c.get("id", "")) for c in self._connections
                      if isinstance(c, dict)
                      and (c.get("tenant", "Default") or "Default") == tenant]
        if self.collapsing_active():
            act_expand = menu.addAction(
                "Collapse This Tenant" if self._tenant_expanded(tenant)
                else f"Open “{tenant}”  ({len(tenant_ids)} machines)")
        else:
            act_expand = None
        act_check = menu.addAction("Check Reachability Now")
        act_check.setEnabled(bool(tenant_ids))
        wakeable_ids = [str(c.get("id", "")) for c in self._connections
                        if isinstance(c, dict)
                        and (c.get("tenant", "Default") or "Default") == tenant
                        and is_wakeable(c)]
        act_wake = menu.addAction(icons.icon("power"), f"Wake All in “{tenant}”")
        act_wake.setEnabled(bool(wakeable_ids))
        if not act_wake.isEnabled():
            act_wake.setToolTip(
                "No connection in this tenant has a MAC address saved.")
        act_restart_setup = menu.addAction("Restart Server…")
        # The estate is where you pick the machines; opening them side by
        # side and typing once is the point of having picked them here.
        ssh_ids = [str(c.get("id", "")) for c in self._connections
                   if isinstance(c, dict)
                   and (c.get("tenant", "Default") or "Default") == tenant
                   and str(c.get("protocol", "ssh") or "ssh").lower() == "ssh"]
        act_broadcast = menu.addAction(icons.icon("broadcast"), "Broadcast to Tenant…")
        act_broadcast.setEnabled(bool(ssh_ids))
        if not act_broadcast.isEnabled():
            act_broadcast.setToolTip("No SSH connections in this tenant.")

        menu.addSeparator()
        act_reset = menu.addAction("Reset Position")
        act_reset.setEnabled(self.has_custom_position(tenant))
        act_reset_branch = menu.addAction("Reset Branch Positions")
        act_reset_branch.setEnabled(
            any(str(c.get("id", "")) in self._conn_positions
                for c in self._connections
                if (c.get("tenant", "Default") or "Default") == tenant))
        if not self.is_free_mode:
            menu.addSeparator()
            act_hide = menu.addAction("Hide From Graph")
        else:
            act_hide = None

        chosen = menu.exec(gp)
        if chosen is None:
            return
        data = chosen.data()
        if isinstance(data, tuple) and data[0] == "link":
            other = make_ep("tenant", data[1])
            existing = self._find_link(self_ep, other)
            if existing is None:
                self.add_link(self_ep, other)
            else:
                self.remove_tenant_link(existing)
        elif chosen is act_link_from:
            self.begin_link_from(self_ep)
        elif act_expand is not None and chosen is act_expand:
            self.toggle_tenant_expanded(tenant)
        elif chosen is act_check:
            self.check_now_requested.emit(tenant_ids)
        elif chosen is act_wake:
            self.wake_requested.emit(wakeable_ids)
        elif chosen is act_restart_setup:
            self.restart_setup_requested.emit("tenant", tenant)
        elif chosen is act_broadcast:
            self.broadcast_tenant_requested.emit(tenant)
        elif chosen is act_open:
            self.tenant_clicked.emit(tenant)
        elif chosen is act_new:
            self.new_connection_requested.emit(tenant)
        elif chosen is act_color:
            self.tenant_color_change_requested.emit(tenant)
        elif chosen is act_reset:
            self._unpin(tenant)
            self._layout_cache = None
            self.update()
            self.node_positions_changed.emit(self._position_snapshot())
        elif chosen is act_reset_branch:
            ids = {str(c.get("id", "")) for c in self._connections
                   if (c.get("tenant", "Default") or "Default") == tenant}
            for cid in ids:
                self._conn_positions.pop(cid, None)
            self.update()
            self.conn_positions_changed.emit(
                {k: list(v) for k, v in self._conn_positions.items()})
        elif act_hide is not None and chosen is act_hide:
            if self._focused_tenant == tenant:
                self._restore_tenant_view()
            self._visible_tenants.discard(tenant)
            self._show_all_connections = False
            if self._selected_node == tenant:
                self._selected_node = None
            self._layout_cache = None
            self.update()
            self.view_state_changed.emit(self.get_view_state())

    def _connection_context_menu(self, pos: QPointF, conn: dict, tenant: str):
        menu = QMenu(self)
        gp = self.mapToGlobal(pos.toPoint())
        name = str(conn.get("name", "") or "connection")
        act_open = menu.addAction(f"Open  “{name}”")
        cid = str(conn.get("id", ""))
        self_ep = make_ep("conn", cid)

        menu.addSeparator()
        act_check = menu.addAction("Check Reachability Now")
        act_check.setEnabled(bool(cid))
        state = self.conn_state(cid)
        if self._ping_enabled and state in _VISIBLE_STATES:
            status = menu.addAction(f"   {STATE_LABELS.get(state, state)}")
            status.setEnabled(False)
        act_wake = menu.addAction(icons.icon("power"), "Wake (Wake-on-LAN)")
        act_wake.setEnabled(is_wakeable(conn))
        if not act_wake.isEnabled():
            act_wake.setToolTip(
                "Add this machine's MAC address to the connection to wake it.")
        act_restart = menu.addAction("Restart…")
        # For anything RPMote can already open a shell on, a restart needs no
        # setup at all — so offer that directly rather than hiding it behind
        # the "no restart server configured" message.
        proto = str(conn.get("protocol", "") or "").strip().lower()
        act_restart_direct = menu.addAction("Restart Directly over SSH…")
        act_restart_direct.setEnabled(
            proto in DIRECT_PROTOCOLS and bool(str(conn.get("host", "") or "").strip()))
        if not act_restart_direct.isEnabled():
            act_restart_direct.setToolTip(
                "Needs an SSH or SFTP connection with a hostname.")
        act_restart_setup = menu.addAction("Restart Server for This Connection…")

        menu.addSeparator()
        link_menu = menu.addMenu("Links")
        act_link_from = link_menu.addAction("Start Link From Here…")
        existing = [(i, l) for i, l in enumerate(self._tenant_links)
                    if self_ep in (l.get("a"), l.get("b"))]
        if existing:
            link_menu.addSeparator()
            for index, link in existing:
                other = self._other_end(link, self_ep)
                a = link_menu.addAction(f"Remove link to “{self._ep_label(other)}”")
                a.setData(("unlink", index))
        link_menu.addSeparator()
        siblings = [c for c in self._connections
                    if isinstance(c, dict)
                    and (c.get("tenant", "Default") or "Default") == tenant
                    and str(c.get("id", "")) != cid]
        if siblings:
            sib_menu = link_menu.addMenu(f"Link To in “{tenant}”")
            for sibling in siblings:
                sid = str(sibling.get("id", ""))
                a = sib_menu.addAction(self._ep_label(make_ep("conn", sid)))
                a.setData(("linkto", make_ep("conn", sid)))

        menu.addSeparator()
        act_reset = menu.addAction("Reset Position")
        act_reset.setEnabled(cid in self._conn_positions)
        act_tenant = menu.addAction(f"Open Tenant “{tenant}” in Sidebar")

        chosen = menu.exec(gp)
        if chosen is None:
            return
        data = chosen.data()
        if isinstance(data, tuple) and data[0] == "unlink":
            self.remove_tenant_link(data[1])
        elif isinstance(data, tuple) and data[0] == "linkto":
            self.add_link(self_ep, data[1])
        elif chosen is act_link_from:
            self.begin_link_from(self_ep)
        elif chosen is act_check:
            self.check_now_requested.emit([cid])
        elif chosen is act_wake:
            self.wake_requested.emit([cid])
        elif chosen is act_restart:
            self.restart_requested.emit(dict(conn))
        elif chosen is act_restart_direct:
            self.restart_direct_requested.emit(dict(conn))
        elif chosen is act_restart_setup:
            self.restart_setup_requested.emit("connection", cid)
        elif chosen is act_open:
            self.connection_activated.emit(dict(conn))
        elif chosen is act_reset:
            self._conn_positions.pop(cid, None)
            self.update()
            self.conn_positions_changed.emit(
                {k: list(v) for k, v in self._conn_positions.items()})
        elif chosen is act_tenant:
            self.tenant_clicked.emit(tenant)

    def _link_context_menu(self, pos: QPointF, index: int):
        if not (0 <= index < len(self._tenant_links)):
            return
        link = self._tenant_links[index]
        self._selected_link = index
        self.update()

        menu = QMenu(self)
        gp = self.mapToGlobal(pos.toPoint())
        menu.addAction(f"{self._ep_label(link['a'])}  ↔  "
                       f"{self._ep_label(link['b'])}").setEnabled(False)
        menu.addSeparator()
        act_color = menu.addAction("Colour…")
        act_auto = menu.addAction("Use Tenant Colours")
        act_auto.setEnabled(bool(link.get("color")))

        style_menu = menu.addMenu("Style")
        style_actions = {}
        for st in LINK_STYLES:
            a = style_menu.addAction(st.capitalize())
            a.setCheckable(True)
            a.setChecked(link.get("style", "dashed") == st)
            style_actions[a] = st

        act_label = menu.addAction("Label…")
        menu.addSeparator()
        act_delete = menu.addAction("Delete Link")

        chosen = menu.exec(gp)
        if chosen is None:
            return
        if chosen in style_actions:
            link["style"] = style_actions[chosen]
            self.update()
            self.tenant_links_changed.emit(self.get_tenant_links())
        elif chosen is act_color:
            initial = QColor(link["color"]) if link.get("color") else self._link_color(link)
            col = QColorDialog.getColor(initial, self, "Link Colour")
            if col.isValid():
                link["color"] = col.name()
                self.update()
                self.tenant_links_changed.emit(self.get_tenant_links())
        elif chosen is act_auto:
            link["color"] = ""
            self.update()
            self.tenant_links_changed.emit(self.get_tenant_links())
        elif chosen is act_label:
            self._rename_link(index)
        elif chosen is act_delete:
            self.remove_tenant_link(index)

    def _rename_link(self, index: int):
        if not (0 <= index < len(self._tenant_links)):
            return
        link = self._tenant_links[index]
        text, ok = QInputDialog.getText(
            self, "Link Label",
            f"Label for {self._ep_label(link['a'])} ↔ {self._ep_label(link['b'])}:",
            text=str(link.get("label", "") or ""))
        if ok:
            link["label"] = text.strip()
            self.update()
            self.tenant_links_changed.emit(self.get_tenant_links())

    def _canvas_context_menu(self, pos: QPointF):
        menu = QMenu(self)
        gp = self.mapToGlobal(pos.toPoint())

        layout_menu = menu.addMenu("Layout")
        layout_actions = {}
        for key, label in (("hub", "Hub  (RPMote centre)"),
                           ("free", "Free Floating")):
            a = layout_menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(self._gs["layout_mode"] == key)
            layout_actions[a] = ("layout_mode", key)
        if self.is_free_mode:
            layout_menu.addSeparator()
            for key, label in (("grid", "Arrange: Grid"),
                               ("scatter", "Arrange: Organic Scatter")):
                a = layout_menu.addAction(label)
                a.setCheckable(True)
                a.setChecked(self._gs["free_arrangement"] == key)
                layout_actions[a] = ("free_arrangement", key)

        line_menu = menu.addMenu("Line Style")
        for key in LINE_STYLES:
            a = line_menu.addAction(key.capitalize())
            a.setCheckable(True)
            a.setChecked(self._gs["line_style"] == key)
            layout_actions[a] = ("line_style", key)

        act_branch = menu.addAction("Show Connection Branches")
        act_branch.setCheckable(True)
        act_branch.setChecked(self._gs["show_branches"])
        act_particles = menu.addAction("Animated Particles")
        act_particles.setCheckable(True)
        act_particles.setChecked(self._gs["particles"])

        act_status = menu.addAction("Reachability Dots")
        act_status.setCheckable(True)
        act_status.setChecked(self._gs.get("show_status", True))
        act_status.setEnabled(self._ping_enabled)

        menu.addSeparator()
        act_ping = menu.addAction("Ping Connections")
        act_ping.setCheckable(True)
        act_ping.setChecked(self._ping_enabled)
        act_check_all = menu.addAction("Check All Now")
        act_check_all.setEnabled(bool(self._connections))
        act_wake_all = menu.addAction(icons.icon("power"), "Wake All Machines")
        act_wake_all.setEnabled(
            any(is_wakeable(c) for c in self._connections if isinstance(c, dict)))
        if not act_wake_all.isEnabled():
            act_wake_all.setToolTip(
                "No connection has a MAC address saved to wake.")

        menu.addSeparator()
        if self.collapsing_active():
            act_expand_all = menu.addAction("Open Every Tenant")
            act_collapse_all = menu.addAction("Collapse Every Tenant")
        else:
            act_expand_all = act_collapse_all = None
        act_tidy = menu.addAction("Tidy Up Layout")
        act_tidy.setEnabled(self.is_free_mode)
        if not self.is_free_mode:
            act_tidy.setToolTip("Free-floating layout only.")

        menu.addSeparator()
        lens_menu = menu.addMenu("Relationship Lens")
        lens_actions = {}
        for kind in LENSES:
            act = lens_menu.addAction(LENS_LABELS[kind])
            act.setCheckable(True)
            act.setChecked(kind == self._lens)
            if kind != LENS_NONE:
                view = self.relation_view(kind)
                act.setEnabled(not view.empty)
                act.setToolTip(view.summary())
            lens_actions[act] = kind

        menu.addSeparator()
        act_link_mode = menu.addAction("Link Mode  (draw links between nodes)")
        act_link_mode.setCheckable(True)
        act_link_mode.setChecked(self._link_mode)
        act_clear_links = menu.addAction("Clear All Links")
        act_clear_links.setEnabled(bool(self._tenant_links))

        menu.addSeparator()
        act_find = menu.addAction("Find Node…\tCtrl+F")
        act_find.setEnabled(bool(self._connections))
        act_fit = menu.addAction("Fit to View")
        act_reset_view = menu.addAction("Reset View  (100%, centred)")
        act_reset_pos = menu.addAction("Reset All Node Positions")
        act_reset_pos.setEnabled(bool(self._custom_positions or self._legacy_positions
                                      or self._conn_positions))
        menu.addSeparator()
        act_settings = menu.addAction("Graph Settings…")

        chosen = menu.exec(gp)
        if chosen is None:
            return
        if chosen in lens_actions:
            self.set_relation_lens(lens_actions[chosen])
        elif act_expand_all is not None and chosen is act_expand_all:
            if not self.expand_all_tenants():
                # Opening everything makes the estate far larger than the view.
                QTimer.singleShot(0, self.fit_to_view)
        elif act_collapse_all is not None and chosen is act_collapse_all:
            self.collapse_all_tenants()
        elif chosen is act_tidy:
            self.tidy_layout()
        elif chosen in layout_actions:
            key, value = layout_actions[chosen]
            self.set_graph_option(key, value)
        elif chosen is act_branch:
            self.set_graph_option("show_branches", not self._gs["show_branches"])
        elif chosen is act_particles:
            self.set_graph_option("particles", not self._gs["particles"])
        elif chosen is act_status:
            self.set_graph_option("show_status", not self._gs.get("show_status", True))
        elif chosen is act_ping:
            self.ping_toggle_requested.emit(not self._ping_enabled)
        elif chosen is act_check_all:
            self.check_now_requested.emit([])
        elif chosen is act_wake_all:
            self.wake_requested.emit([])
        elif chosen is act_link_mode:
            self.set_link_mode(not self._link_mode)
            self.link_mode_changed.emit(self._link_mode)
        elif chosen is act_clear_links:
            self.clear_tenant_links()
        elif chosen is act_find:
            self.open_search()
        elif chosen is act_fit:
            self.fit_to_view()
        elif chosen is act_reset_view:
            self.reset_view()
        elif chosen is act_reset_pos:
            self.reset_all_positions()
        elif chosen is act_settings:
            self.settings_dialog_requested.emit()

    # ── Bulk Resets ────────────────────────────────────────────────

    def reset_all_positions(self):
        """Throw away every hand-placed tenant tile and branch node."""
        had = bool(self._custom_positions or self._legacy_positions
                   or self._conn_positions)
        self._custom_positions = {}
        self._legacy_positions = {}
        self._conn_positions = {}
        self._layout_cache = None
        self.update()
        if had:
            self.node_positions_changed.emit({})
            self.conn_positions_changed.emit({})

    # ── Helpers ────────────────────────────────────────────────────

    def _known_endpoints(self) -> set:
        """Every endpoint a link may legitimately point at right now."""
        eps = {make_ep("tenant", t) for t in self._all_tenants()}
        for conn in self._connections:
            if isinstance(conn, dict) and conn.get("id") is not None:
                eps.add(make_ep("conn", str(conn.get("id"))))
        return eps

    def _conn_by_id(self, conn_id: str) -> dict | None:
        target = str(conn_id)
        for conn in self._connections:
            if isinstance(conn, dict) and str(conn.get("id", "")) == target:
                return conn
        return None

    def _ep_label(self, ep: str) -> str:
        """Human name for an endpoint, for menus and the link-mode banner."""
        kind, key = parse_ep(ep)
        if kind == "tenant":
            return key
        conn = self._conn_by_id(key)
        if conn is None:
            return key
        return (str(conn.get("name", "") or "").rstrip().rstrip("●• ")
                or str(conn.get("host", "") or key))

    def _ep_owner_tenant(self, ep: str) -> str | None:
        """Tenant an endpoint belongs to — itself, or a connection's owner."""
        kind, key = parse_ep(ep)
        if kind == "tenant":
            return key
        conn = self._conn_by_id(key)
        if conn is None:
            return None
        return str(conn.get("tenant", "Default") or "Default")

    def _all_tenants(self) -> set:
        return {(c.get("tenant", "Default") or "Default")
                for c in self._connections if isinstance(c, dict)}

    @staticmethod
    def _other_end(link: dict, ep: str) -> str:
        return link.get("b") if link.get("a") == ep else link.get("a")

    def _find_link(self, a: str, b: str) -> int | None:
        for i, l in enumerate(self._tenant_links):
            if {l.get("a"), l.get("b")} == {a, b}:
                return i
        return None

    def _draw_collapse_badge(self, p, rect: QRectF, count: int):
        """A "click to open" chip on a tenant that is holding its machines back.

        Without it a collapsed tenant is indistinguishable from a tenant with
        nothing in it, and the estate looks emptier than it is. The tile's own
        pill already shows how many machines it holds, so the chip just says
        there is more inside.
        """
        text = "open" if count else ""
        font = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
        fm = QFontMetrics(font)
        glyph = fm.height() - 4
        gap = 3 if text else 0
        tw = fm.horizontalAdvance(text) + gap + glyph + 14
        th = fm.height() + 4
        box = QRectF(rect.right() - tw / 2 - 6, rect.bottom() - th / 2, tw, th)
        accent = QColor(self._theme.get("accent", "#a78bfa"))
        p.setBrush(QBrush(QColor(16, 18, 30, 236)))
        p.setPen(QPen(accent, 1.0))
        p.drawRoundedRect(box, th / 2, th / 2)
        inner = box.adjusted(7, 0, -7, 0)
        if text:
            p.setFont(font)
            p.setPen(accent)
            p.drawText(inner, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        chevron = QRectF(inner.right() - glyph, box.center().y() - glyph / 2, glyph, glyph)
        icons.icon("chevron-right", accent.name(), stroke=2.5).paint(p, chevron.toRect())

    # ── Relationship Lens Overlay ──────────────────────────────────
    #
    # Deliberately not drawn in a reachability colour: a relationship is not a
    # health state, and an admin should never have to work out whether an amber
    # line means "degraded" or "shared".

    LENS_ACCENT = "#7dd3fc"     # ordinary relationship
    LENS_CROSS = "#f472b6"      # spans two tenants — the noteworthy case

    def _lens_geom(self, conn_id: str):
        """Where a connection sits on screen, or its tenant tile if collapsed."""
        geom = self._ep_geom.get(make_ep("conn", str(conn_id)))
        if geom is not None:
            return geom
        conn = self._conn_by_id(str(conn_id))
        if conn is None:
            return None
        owner = make_ep("tenant", conn.get("tenant", "Default") or "Default")
        return self._ep_geom.get(owner)

    def _clear_of_nodes(self, x: float, y: float, limit: int = 14) -> tuple:
        """Slide a hub off any node it landed on.

        The centroid of a tenant's own machines is, almost by definition, the
        tenant tile — so left alone every hub would be stamped on top of the
        one node whose name you most need to read.
        """
        step = 11.0
        for _ in range(limit):
            for sx, sy, hw, hh in self._ep_geom.values():
                if abs(x - sx) <= hw + 12 and abs(y - sy) <= hh + 12:
                    y = sy + hh + 12 + step
                    break
            else:
                break
        return x, y

    def _draw_relation_overlay(self, p, w: int, h: int, tenants: dict):
        """Dim the estate, draw what the lens found, re-light what took part."""
        view = self.relation_view()
        scrim = QColor(self._theme.get("bg_primary", "#0a0b10"))
        scrim.setAlpha(190)
        p.fillRect(0, 0, w, h, scrim)

        chips = []
        for group in view.groups:
            chips.extend(self._draw_relation_group(p, group))
        for edge in view.edges:
            self._draw_relation_edge(p, edge)
        self._relight_participants(p, view, tenants)
        # Chips last: a hub label buried under a tenant tile is no label at all.
        for chip in chips:
            self._draw_lens_chip(p, *chip)
        self._draw_lens_banner(p, w, h, view)

    def _draw_relation_group(self, p, group) -> list:
        """Spokes from every member to one labelled hub.

        A hub rather than every pair on purpose: fifty machines on a subnet are
        1,225 lines drawn pairwise and fifty drawn this way, and only one of
        those two pictures can be read.
        """
        points = []
        for cid in group.members:
            geom = self._lens_geom(cid)
            if geom is not None:
                points.append((geom[0], geom[1]))
        if len(points) < 2:
            return []
        # Members collapsed onto one tenant tile share a pixel; without this a
        # group of twelve would pile twelve spokes on the same spot.
        unique = []
        for x, y in points:
            if all(abs(x - ux) > 0.5 or abs(y - uy) > 0.5 for ux, uy in unique):
                unique.append((x, y))
        if len(unique) < 2:
            return []

        hx = sum(x for x, _ in unique) / len(unique)
        hy = sum(y for _, y in unique) / len(unique)
        hx, hy = self._clear_of_nodes(hx, hy)
        colour = QColor(self.LENS_CROSS if group.cross_tenant else self.LENS_ACCENT)

        pen = QPen(QColor(colour), 1.3)
        pen.setStyle(Qt.PenStyle.CustomDashLine)
        pen.setDashPattern([6, 5])
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for x, y in unique:
            p.drawLine(QPointF(x, y), QPointF(hx, hy))

        p.setPen(Qt.PenStyle.NoPen)
        glow = QColor(colour)
        glow.setAlpha(70)
        p.setBrush(QBrush(glow))
        p.drawEllipse(QPointF(hx, hy), 9.0, 9.0)
        p.setBrush(QBrush(colour))
        p.drawEllipse(QPointF(hx, hy), 4.0, 4.0)
        return [(hx, hy, f"{group.label}  ·  {len(group.members)}", colour)]

    def _draw_relation_edge(self, p, edge):
        """One directed relationship, arrowhead on the thing acted upon."""
        a = self._lens_geom(edge.source)
        b = self._lens_geom(edge.target)
        if a is None or b is None:
            return
        ax, ay = self._edge_point(a[0], a[1], a[2], a[3], b[0], b[1])
        bx, by = self._edge_point(b[0], b[1], b[2], b[3], a[0], a[1])
        if abs(ax - bx) < 1.0 and abs(ay - by) < 1.0:
            return                      # both ends collapsed onto one tile
        colour = QColor(self.LENS_CROSS if edge.cross_tenant else self.LENS_ACCENT)
        p.setPen(QPen(colour, 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(ax, ay), QPointF(bx, by))

        angle = math.atan2(by - ay, bx - ax)
        size = 9.0
        head = QPolygonF([
            QPointF(bx, by),
            QPointF(bx - size * math.cos(angle - 0.42),
                    by - size * math.sin(angle - 0.42)),
            QPointF(bx - size * math.cos(angle + 0.42),
                    by - size * math.sin(angle + 0.42)),
        ])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(colour))
        p.drawPolygon(head)

    def _relight_participants(self, p, view, tenants: dict):
        """Re-draw every node the lens has something to say about."""
        node_scale = self._zoom * float(self._gs["node_scale"])
        show_branches = bool(self._gs["show_branches"])
        drawn: set = set()
        for cid in sorted(view.participants()):
            conn = self._conn_by_id(cid)
            if conn is None:
                continue
            tenant = conn.get("tenant", "Default") or "Default"
            ep = make_ep("conn", cid) if show_branches else make_ep("tenant", tenant)
            geom = self._ep_geom.get(ep)
            if geom is None:
                ep = make_ep("tenant", tenant)
                geom = self._ep_geom.get(ep)
            if geom is None or ep in drawn:
                continue
            drawn.add(ep)
            kind, key = parse_ep(ep)
            if kind == "tenant":
                self._draw_glass_node(
                    p, geom[0], geom[1], key, tenants.get(key, []),
                    self._tenant_colors.get(key, "#a78bfa"),
                    key == self._selected_node, key == self._hovered_node,
                    key in self._active_tenants, node_scale)
            else:
                self._draw_connection_node(
                    p, geom[0], geom[1], conn, self._node_base_color(tenant),
                    cid in self._active_conn_ids, cid == self._hovered_conn,
                    cid == self._selected_conn, node_scale)

    def _draw_lens_chip(self, p, x: float, y: float, text: str, colour: QColor):
        """The little label that says what a hub is a hub of."""
        font = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(text) + 16
        th = fm.height() + 7
        box = QRectF(x - tw / 2, y + 11, tw, th)
        p.setBrush(QBrush(QColor(14, 16, 28, 232)))
        p.setPen(QPen(QColor(colour), 1.1))
        p.drawRoundedRect(box, th / 2, th / 2)
        p.setFont(font)
        p.setPen(QColor(colour))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_lens_banner(self, p, w: int, h: int, view):
        """What the lens is showing, pinned clear of the find bar."""
        msg = f"{view.summary()}   —   Esc to clear"
        font = QFont("Segoe UI", 9, QFont.Weight.DemiBold)
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(msg) + 26
        th = fm.height() + 12
        box = QRectF((w - tw) / 2, h - th - 16, tw, th)
        accent = QColor(self.LENS_CROSS if view.cross_tenant_count()
                        else self.LENS_ACCENT)
        p.setBrush(QBrush(QColor(14, 16, 28, 238)))
        p.setPen(QPen(accent, 1.2))
        p.drawRoundedRect(box, th / 2, th / 2)
        p.setFont(font)
        p.setPen(QColor(226, 228, 240))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, msg)

    # ── Spotlight Search Overlay ───────────────────────────────────

    def _draw_search_overlay(self, p, w: int, h: int):
        """Dim what did not match, re-light what did, then draw the find bar."""
        if self._search_query.strip():
            # A scrim over the finished scene, with the matches painted again
            # on top: a hit then reads as *lit* rather than merely outlined,
            # which is what makes one node findable among forty.
            scrim = QColor(self._theme.get("bg_primary", "#0a0b10"))
            scrim.setAlpha(198)
            p.fillRect(0, 0, w, h, scrim)
            self._draw_search_highlights(p)
        self._draw_search_panel(p, w, h)

    def _draw_search_highlights(self, p):
        """Re-draw every matched node above the scrim, focused one ringed."""
        node_scale = self._zoom * float(self._gs["node_scale"])
        show_branches = bool(self._gs["show_branches"])
        tenants: dict = {}
        for conn in self._connections:
            if isinstance(conn, dict):
                tenants.setdefault(
                    conn.get("tenant", "Default") or "Default", []).append(conn)

        current = self._current_search_hit()
        current_ep = current["ep"] if current else None
        focus_geom = None
        drawn: set = set()
        for hit in self._search_hits:
            ep = hit["ep"]
            if hit["kind"] == "conn" and not show_branches:
                ep = make_ep("tenant", hit["tenant"])   # collapsed onto its tile
            geom = self._ep_geom.get(ep)
            if geom is None:
                continue                                # not on screen right now
            focused = (hit["ep"] == current_ep)
            if focused:
                focus_geom = geom
            if ep in drawn:
                continue
            drawn.add(ep)
            sx, sy = geom[0], geom[1]
            kind, key = parse_ep(ep)
            if kind == "tenant":
                self._draw_glass_node(
                    p, sx, sy, key, tenants.get(key, []),
                    self._tenant_colors.get(key, "#a78bfa"),
                    key == self._selected_node, focused,
                    key in self._active_tenants, node_scale)
            else:
                conn = self._conn_by_id(key)
                if conn is None:
                    continue
                self._draw_connection_node(
                    p, sx, sy, conn, self._node_base_color(hit["tenant"]),
                    key in self._active_conn_ids, focused,
                    key == self._selected_conn, node_scale)
        # Drawn last so the ring is never buried under a neighbouring match.
        if focus_geom is not None:
            self._draw_search_focus_ring(p, *focus_geom)

    def _draw_search_focus_ring(self, p, sx: float, sy: float,
                                hw: float, hh: float):
        """Breathing accent ring around the result the camera is sitting on."""
        pulse = ((0.5 + 0.5 * math.sin(self._time * 3.4))
                 if self._gs["animate"] else 1.0)
        accent = QColor(self._theme.get("accent", "#a78bfa"))
        pad = 9.0 + 3.0 * pulse
        box = QRectF(sx - hw - pad, sy - hh - pad,
                     (hw + pad) * 2, (hh + pad) * 2)
        r = min(box.height() / 2, 18.0)
        p.setBrush(Qt.BrushStyle.NoBrush)
        glow = QColor(accent)
        glow.setAlpha(int(55 + 55 * pulse))
        p.setPen(QPen(glow, 7))
        p.drawRoundedRect(box, r, r)
        p.setPen(QPen(accent, 1.8))
        p.drawRoundedRect(box, r, r)

    def _draw_search_panel(self, p, w: int, h: int):
        """The find bar and its ranked result list, pinned to the top."""
        accent = QColor(self._theme.get("accent", "#a78bfa"))
        text_c = QColor(self._theme.get("text_primary", "#e2e4f0"))
        muted = QColor(self._theme.get("text_secondary", "#94a3b8"))
        query = self._search_query
        hits = self._search_hits

        bar_font = QFont("Segoe UI", 11)
        row_font = QFont("Segoe UI", 9, QFont.Weight.DemiBold)
        sub_font = QFont("Segoe UI", 8)
        bar_fm, row_fm, sub_fm = (QFontMetrics(bar_font), QFontMetrics(row_font),
                                  QFontMetrics(sub_font))

        pw = max(300.0, min(520.0, w - 40.0))
        px = (w - pw) / 2
        py = 16.0
        bar_h = bar_fm.height() + 22.0
        shown = hits[:SEARCH_MAX_ROWS]
        row_h = row_fm.height() + sub_fm.height() + 13.0
        # The panel grows its own footer rather than letting the overflow line
        # spill onto the keyboard hint underneath it.
        overflow = sub_fm.height() + 6.0 if len(hits) > len(shown) else 0.0
        if shown:
            list_h = len(shown) * row_h + 12.0 + overflow
        elif query.strip():
            list_h = row_fm.height() + 22.0        # the "no matches" line
        else:
            list_h = 0.0

        panel = QRectF(px, py, pw, bar_h + list_h)
        self._search_rect = panel
        self._search_row_rects = []

        p.setBrush(QBrush(QColor(14, 16, 28, 244)))
        p.setPen(QPen(QColor(accent.red(), accent.green(), accent.blue(), 165), 1.3))
        p.drawRoundedRect(panel, 13, 13)

        # ── Magnifier ─────────────────────────────────────────────
        gx, gy = px + 20.0, py + bar_h / 2
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(muted, 1.6))
        p.drawEllipse(QPointF(gx, gy - 1), 5.0, 5.0)
        p.drawLine(QPointF(gx + 3.7, gy + 2.7), QPointF(gx + 7.5, gy + 6.5))

        # ── Query, caret and the n-of-m counter ───────────────────
        count = ""
        if query.strip():
            count = (f"{self._search_index + 1} of {len(hits)}" if hits
                     else "no matches")
        p.setFont(bar_font)
        count_w = bar_fm.horizontalAdvance(count) + (14.0 if count else 0.0)
        text_x = px + 36.0
        text_w = pw - 36.0 - 16.0 - count_w
        shown_q = query or "Find a tenant or connection…"
        p.setPen(text_c if query else muted)
        p.drawText(QRectF(text_x, py, text_w, bar_h),
                   int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                   bar_fm.elidedText(shown_q, Qt.TextElideMode.ElideLeft,
                                     int(max(text_w, 10))))
        if query:
            qw = min(bar_fm.horizontalAdvance(query), text_w)
            if self._search_replace:
                # "Selected", the way focusing a find bar selects what is there.
                sel = QColor(accent)
                sel.setAlpha(70)
                p.fillRect(QRectF(text_x - 2, py + 7, qw + 4, bar_h - 14), sel)
            elif (not self._gs["animate"]) or int(self._time * 2) % 2 == 0:
                p.setPen(QPen(accent, 1.6))
                p.drawLine(QPointF(text_x + qw + 2, py + 9),
                           QPointF(text_x + qw + 2, py + bar_h - 9))
        if count:
            p.setFont(bar_font)
            p.setPen(muted)
            p.drawText(QRectF(px + pw - count_w - 12, py, count_w, bar_h),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                       count)

        if list_h <= 0:
            self._draw_search_hint(p, panel, muted)
            return

        sep = QColor(accent)
        sep.setAlpha(60)
        p.setPen(QPen(sep, 1))
        p.drawLine(QPointF(px + 12, py + bar_h), QPointF(px + pw - 12, py + bar_h))

        if not shown:
            p.setFont(row_font)
            p.setPen(muted)
            p.drawText(QRectF(px, py + bar_h, pw, list_h),
                       int(Qt.AlignmentFlag.AlignCenter),
                       f"Nothing matches “{query.strip()}”")
            self._draw_search_hint(p, panel, muted)
            return

        top = py + bar_h + 6.0
        for i, hit in enumerate(shown):
            row = QRectF(px + 6, top + i * row_h, pw - 12, row_h - 2)
            self._search_row_rects.append((row, i))
            if i == self._search_index:
                fill = QColor(accent)
                fill.setAlpha(52)
                p.setBrush(QBrush(fill))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(row, 8, 8)

            dot = (QColor(self._tenant_colors.get(hit["tenant"], "#a78bfa"))
                   if hit["kind"] == "tenant" else
                   QColor(PROTOCOL_COLORS.get(
                       str((hit["conn"] or {}).get("protocol", "ssh") or "ssh").lower(),
                       "#a78bfa")))
            p.setBrush(QBrush(dot))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(row.left() + 14, row.center().y()), 4.0, 4.0)

            # A reachability dot on the right turns the result list into a
            # status list, which is the whole point of leaving this open.
            right = row.right() - 10
            if self._ping_enabled and self._gs.get("show_status", True):
                state = (self.tenant_state(hit["tenant"]) if hit["kind"] == "tenant"
                         else self.conn_state(str((hit["conn"] or {}).get("id", ""))))
                sc = self._status_color(state)
                if sc is not None:
                    p.setBrush(QBrush(sc))
                    p.setPen(Qt.PenStyle.NoPen)
                    p.drawEllipse(QPointF(right - 4, row.center().y()), 3.6, 3.6)
                    right -= 16

            tx = row.left() + 26
            tw = max(right - tx, 20)
            p.setFont(row_font)
            p.setPen(text_c)
            p.drawText(QRectF(tx, row.top() + 5, tw, row_fm.height()),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       row_fm.elidedText(hit["label"],
                                         Qt.TextElideMode.ElideRight, int(tw)))
            p.setFont(sub_font)
            p.setPen(muted)
            p.drawText(QRectF(tx, row.top() + 4 + row_fm.height(),
                              tw, sub_fm.height()),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       sub_fm.elidedText(hit["sub"],
                                         Qt.TextElideMode.ElideRight, int(tw)))

        if overflow:
            p.setFont(sub_font)
            p.setPen(muted)
            p.drawText(QRectF(px, panel.bottom() - overflow - 2, pw, overflow),
                       int(Qt.AlignmentFlag.AlignCenter),
                       f"+{len(hits) - len(shown)} more  ·  ↓ walks all "
                       f"{len(hits)}")
        self._draw_search_hint(p, panel, muted)

    def _draw_search_hint(self, p, panel: QRectF, muted: QColor):
        """One quiet line under the panel spelling out the keys."""
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(muted)
        p.drawText(QRectF(panel.left(), panel.bottom() + 14, panel.width(), 16),
                   int(Qt.AlignmentFlag.AlignCenter),
                   "↑↓ step   ·   Enter go to   ·   Ctrl+Enter open   ·   Esc close")

    def _draw_link_mode_banner(self, p, w: int):
        """Small heads-up strip while link mode is armed."""
        msg = ("Link mode — click any tenant or connection, then another to "
               "join them"
               if self._link_source is None
               else f"Linking from “{self._ep_label(self._link_source)}” — "
                    f"click the second node  (Esc to cancel)")
        font = QFont("Segoe UI", 9, QFont.Weight.DemiBold)
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(msg) + 26
        th = fm.height() + 12
        box = QRectF((w - tw) / 2, 14, tw, th)
        p.setBrush(QBrush(QColor(18, 20, 34, 235)))
        p.setPen(QPen(QColor(167, 139, 250, 150), 1.2))
        p.drawRoundedRect(box, th / 2, th / 2)
        p.setFont(font)
        p.setPen(QColor(226, 228, 240))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, msg)
