"""Per-theme palettes for the dashboard's nodes, pills and centre orb.

A theme's *background* is painted by ``app.dashboard_backgrounds``; this
module holds everything drawn on top of it: the tints a tenant tile, a
connection pill and the orb take on so they sit naturally on that
background.  The dashboard looks its skin up once per frame and reads
colours from it, so adding a theme is a table entry here plus a background
painter -- no branching in the draw code.

Colours are plain ``(r, g, b, a)`` tuples so the table has no Qt
dependency and can be imported by the theme registry and tests alike.

Glass is kept near-opaque (alpha 228+) and idle borders at alpha 120 or
so on every skin: the animated backdrops are bright and busy, and a
translucent tile with a whisper of a border simply vanished into them.
"""

from __future__ import annotations

from dataclasses import dataclass

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]


@dataclass(frozen=True)
class Skin:
    key: str
    #: The one colour every tenant and branch is drawn in.  ``None`` keeps
    #: the per-tenant brand colours (the default dashboard).
    accent: RGB | None
    #: A lighter accent for hovered borders.
    hover: RGB | None
    #: Monospaced type and neon text everywhere (Matrix).
    mono: bool
    #: Tile / orb name text, and the orb's dimmer subtitle.
    text: RGBA
    text_dim: RGBA
    #: "N connections" under a full tile, and the count chip on a compact one.
    count_text: RGBA
    chip_text: RGBA
    #: Pill label colour; ``None`` uses the resting grey / highlighted white.
    pill_text: RGBA | None
    #: Force every pill's protocol colour to this (Matrix); ``None`` keeps
    #: the per-protocol colours.
    pill_accent: RGB | None
    #: Custom link colour when the user set none; ``None`` blends the two
    #: tenants' brand colours.
    link_color: RGB | None
    #: The mote at the orb's heart: its glow and rim take this colour, the
    #: core is always white-hot.
    mote: str
    #: Alpha of the soft glow that sits under every tile.
    tile_glow_alpha: int
    #: Glass gradients: three stops for a tile (resting and selected), two
    #: for a pill, three for the orb.
    tile_bg: tuple[RGBA, RGBA, RGBA]
    tile_bg_sel: tuple[RGBA, RGBA, RGBA]
    pill_bg: tuple[RGBA, RGBA]
    orb_glass: tuple[RGBA, RGBA, RGBA]
    #: The orb's animated halo (centre, mid) and its rim.
    orb_halo: tuple[RGBA, RGBA]
    orb_border: RGBA
    #: Pulsing halo under a tile with a live session.
    active_glow: RGBA
    #: Tile borders.  Alpha of ``border_active`` is scaled by the pulse.
    border_active: RGBA
    border_sel: RGBA
    border_hover: RGBA
    border_idle: RGBA
    #: Top-edge highlight: colour plus its alpha when selected / resting.
    highlight: tuple[int, int, int, int, int]


_TEXT = (232, 233, 240, 255)
_TEXT_DIM = (160, 174, 194, 215)
_CHIP_TEXT = (198, 203, 220, 255)
#: RPMote cyan -- the mote on the default skin, where there is no single
#: accent to borrow.
_MOTE = "#22d3ee"


def _mix(a: RGB, b: RGB, t: float) -> RGB:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def _lift(c: RGB, n: int) -> RGB:
    return tuple(min(255, v + n) for v in c)  # type: ignore[return-value]


def _rgba(c: RGB, a: int) -> RGBA:
    return (c[0], c[1], c[2], a)


def derive_skin(key: str, accent: RGB, deep: RGB,
                hover: RGB | None = None) -> Skin:
    """A skin in the Dot Wave / Aurora mould, tinted from two colours.

    *accent* is the theme's signature colour and *deep* the darkest colour
    of its background.  Every glass tint is *deep* nudged a few percent
    toward *accent*, which is how the hand-tuned skins were arrived at.
    """
    hover = hover or _lift(_mix(accent, (255, 255, 255), 0.25), 0)
    return Skin(
        key=key,
        accent=accent,
        hover=hover,
        mono=False,
        text=_TEXT,
        text_dim=_TEXT_DIM,
        count_text=_rgba(accent, 205),
        chip_text=_CHIP_TEXT,
        pill_text=None,
        pill_accent=None,
        link_color=None,
        mote="#%02x%02x%02x" % hover,
        tile_glow_alpha=18,
        tile_bg=(_rgba(_lift(_mix(deep, accent, 0.07), 6), 228),
                 _rgba(_lift(_mix(deep, accent, 0.05), 4), 236),
                 _rgba(_lift(_mix(deep, accent, 0.03), 2), 244)),
        tile_bg_sel=(_rgba(_lift(_mix(deep, accent, 0.11), 8), 238),
                     _rgba(_lift(_mix(deep, accent, 0.08), 6), 246),
                     _rgba(_lift(_mix(deep, accent, 0.05), 4), 252)),
        pill_bg=(_rgba(_lift(_mix(deep, accent, 0.08), 8), 244),
                 _rgba(_lift(_mix(deep, accent, 0.04), 4), 250)),
        orb_glass=(_rgba(_lift(_mix(deep, accent, 0.09), 10), 225),
                   _rgba(_lift(_mix(deep, accent, 0.05), 6), 242),
                   _rgba(_lift(_mix(deep, accent, 0.02), 2), 252)),
        orb_halo=(_rgba(accent, 62), _rgba(_mix(accent, deep, 0.2), 26)),
        orb_border=_rgba(accent, 44),
        active_glow=_rgba(accent, 50),
        border_active=_rgba(accent, 200),
        border_sel=_rgba(accent, 185),
        border_hover=_rgba(hover, 160),
        border_idle=_rgba(accent, 120),
        highlight=(accent[0], accent[1], accent[2], 26, 14),
    )


DEFAULT_SKIN = Skin(
    key="default",
    accent=None,
    hover=None,
    mono=False,
    text=_TEXT,
    text_dim=_TEXT_DIM,
    count_text=(160, 174, 194, 210),
    chip_text=_CHIP_TEXT,
    pill_text=None,
    pill_accent=None,
    link_color=None,
    mote=_MOTE,
    tile_glow_alpha=18,
    tile_bg=((30, 33, 55, 228), (25, 28, 48, 236), (20, 22, 40, 244)),
    tile_bg_sel=((40, 43, 70, 238), (35, 38, 63, 246), (28, 30, 52, 252)),
    pill_bg=((34, 37, 60, 244), (23, 25, 43, 250)),
    orb_glass=((35, 38, 58, 220), (22, 24, 40, 240), (15, 16, 28, 250)),
    orb_halo=((34, 211, 238, 50), (8, 145, 178, 22)),
    orb_border=(255, 255, 255, 25),
    active_glow=(74, 222, 128, 40),
    # The default tile border is built from the tenant's own colour in the
    # draw code; these four are never read for it.
    border_active=(74, 222, 128, 100),
    border_sel=(255, 255, 255, 90),
    border_hover=(255, 255, 255, 65),
    border_idle=(255, 255, 255, 50),
    highlight=(255, 255, 255, 20, 15),
)

MATRIX_SKIN = Skin(
    key="matrix",
    # Mellow mint (see MATRIX_GLITCH_THEME): cool-biased and well short of
    # full-saturation neon, so a screen full of it doesn't glare.
    accent=(92, 214, 143),
    hover=(126, 226, 166),
    mono=True,
    text=(163, 228, 188, 255),
    text_dim=(111, 196, 143, 190),
    count_text=(111, 196, 143, 170),
    chip_text=(163, 228, 188, 255),
    pill_text=(163, 228, 188, 255),
    pill_accent=(92, 214, 143),
    link_color=(92, 214, 143),
    mote="#7ee2a6",
    tile_glow_alpha=20,
    tile_bg=((8, 22, 15, 232), (6, 16, 11, 240), (4, 10, 7, 246)),
    tile_bg_sel=((12, 34, 23, 240), (9, 24, 16, 246), (6, 15, 10, 252)),
    pill_bg=((9, 27, 18, 244), (5, 14, 9, 250)),
    orb_glass=((9, 26, 18, 225), (6, 15, 10, 242), (3, 7, 5, 252)),
    orb_halo=((92, 214, 143, 60), (60, 160, 110, 26)),
    orb_border=(92, 214, 143, 45),
    active_glow=(92, 214, 143, 50),
    border_active=(92, 214, 143, 200),
    border_sel=(92, 214, 143, 185),
    border_hover=(92, 214, 143, 160),
    border_idle=(80, 170, 120, 125),
    highlight=(126, 226, 166, 26, 15),
)

DOTWAVE_SKIN = Skin(
    key="dotwave",
    accent=(88, 166, 255),
    hover=(121, 192, 255),
    mono=False,
    text=_TEXT,
    text_dim=_TEXT_DIM,
    count_text=(88, 166, 255, 205),
    chip_text=_CHIP_TEXT,
    pill_text=None,
    pill_accent=None,
    link_color=None,
    mote="#79c0ff",
    tile_glow_alpha=18,
    tile_bg=((13, 18, 38, 228), (10, 14, 30, 236), (7, 10, 22, 244)),
    tile_bg_sel=((18, 28, 52, 238), (13, 21, 42, 246), (9, 15, 32, 252)),
    pill_bg=((16, 24, 46, 244), (9, 14, 28, 250)),
    orb_glass=((20, 28, 48, 225), (13, 18, 32, 242), (8, 12, 22, 252)),
    orb_halo=((88, 166, 255, 65), (56, 139, 253, 28)),
    orb_border=(88, 166, 255, 45),
    active_glow=(88, 166, 255, 50),
    border_active=(88, 166, 255, 200),
    border_sel=(88, 166, 255, 185),
    border_hover=(121, 192, 255, 160),
    border_idle=(88, 166, 255, 120),
    highlight=(88, 166, 255, 28, 16),
)

AURORA_SKIN = Skin(
    key="aurora",
    accent=(6, 214, 202),
    hover=(34, 232, 220),
    mono=False,
    text=_TEXT,
    text_dim=_TEXT_DIM,
    count_text=(6, 214, 202, 205),
    chip_text=_CHIP_TEXT,
    pill_text=None,
    pill_accent=None,
    link_color=None,
    mote="#22e8dc",
    tile_glow_alpha=18,
    tile_bg=((9, 20, 34, 228), (7, 15, 26, 236), (4, 10, 18, 244)),
    tile_bg_sel=((14, 30, 48, 238), (10, 22, 36, 246), (6, 15, 26, 252)),
    pill_bg=((11, 25, 40, 244), (5, 13, 23, 250)),
    orb_glass=((14, 28, 44, 225), (8, 18, 30, 242), (4, 10, 20, 252)),
    orb_halo=((6, 214, 202, 62), (6, 182, 212, 26)),
    orb_border=(6, 214, 202, 42),
    active_glow=(6, 214, 202, 50),
    border_active=(6, 214, 202, 200),
    border_sel=(6, 214, 202, 185),
    border_hover=(34, 232, 220, 160),
    border_idle=(6, 214, 202, 120),
    highlight=(6, 214, 202, 26, 14),
)

# ── The newer skins are all derived: accent + darkest background colour ──

SYNTHWAVE_SKIN = derive_skin("synthwave", accent=(255, 45, 149),
                             deep=(12, 2, 28), hover=(255, 105, 180))
CIRCUIT_SKIN = derive_skin("circuit", accent=(250, 204, 21),
                           deep=(5, 10, 7), hover=(253, 224, 71))
PLEXUS_SKIN = derive_skin("plexus", accent=(192, 132, 252),
                          deep=(8, 5, 24), hover=(216, 180, 254))
WARP_SKIN = derive_skin("warp", accent=(56, 189, 248),
                        deep=(2, 4, 12), hover=(125, 211, 252))
EMBER_SKIN = derive_skin("ember", accent=(251, 146, 60),
                         deep=(14, 6, 4), hover=(253, 186, 116))

SKINS: dict[str, Skin] = {
    s.key: s for s in (
        DEFAULT_SKIN, MATRIX_SKIN, DOTWAVE_SKIN, AURORA_SKIN,
        SYNTHWAVE_SKIN, CIRCUIT_SKIN, PLEXUS_SKIN, WARP_SKIN, EMBER_SKIN,
    )
}


def skin_for(key: str | None) -> Skin:
    """The skin for a ``dashboard_theme`` key; unknown keys get the default."""
    return SKINS.get(key or "default", DEFAULT_SKIN)
