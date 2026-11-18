"""
RPMote Theme Engine — window palettes.

"Mote" is the RPMote house theme and the default.  The rest are carried
over unchanged; each one maps to a dashboard skin by its
``dashboard_theme`` key (plain themes share the default skin).
"""

from app.resource_paths import get_qss_resource_url

# ── Contrast helpers ─────────────────────────────────────────────
# Buttons are painted in the theme's accent, and most accents are mid-bright
# cyans, teals and violets on which white text reads at well under the 4.5:1
# floor.  Rather than hand-tune an ink per theme, pick it from the accent's
# own luminance so any theme (including a user-added one) gets legible
# buttons for free.

def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    """WCAG relative luminance of a ``#rrggbb`` colour, 0 (black) to 1."""
    h = color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g)
            + 0.0722 * _srgb_to_linear(b))


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two ``#rrggbb`` colours (1 to 21)."""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


#: Near-black ink for buttons whose accent is too light for white text.
BUTTON_INK_DARK = "#0b0e14"
BUTTON_INK_LIGHT = "#ffffff"


def button_text_color(theme: dict) -> str:
    """The ink a theme's buttons are lettered in.

    A theme may say outright with ``button_text``; otherwise it is white or
    near-black, whichever stays more readable across the resting *and*
    hovered face -- the same ink for both, so nothing flips under the
    pointer.
    """
    explicit = theme.get("button_text")
    if explicit:
        return explicit
    faces = (theme["accent"], theme.get("accent_hover", theme["accent"]))

    def worst(ink: str) -> float:
        return min(contrast_ratio(ink, face) for face in faces)

    return (BUTTON_INK_DARK if worst(BUTTON_INK_DARK) > worst(BUTTON_INK_LIGHT)
            else BUTTON_INK_LIGHT)

# ── House theme ──────────────────────────────────────────────────
# Deep navy-black with the RPMote cyan.  ``accent`` is the button face
# (lettered in dark ink, see ``button_text_color``); the brighter
# ``tab_active`` / cursor cyan is the same colour the app icon and
# dashboard mote glow in.
MOTE_THEME = {
    "name": "Mote",
    "bg_primary": "#070b12",
    "bg_secondary": "#0c121b",
    "bg_tertiary": "#131b27",
    "bg_input": "#04070c",
    "bg_hover": "#1a2533",
    "bg_selected": "#0b3a47",
    "bg_terminal": "#04070c",
    "text_primary": "#eef5f8",
    "text_secondary": "#8ea3b4",
    "text_muted": "#647e95",
    "accent": "#0aa3c2",
    "accent_hover": "#22c5e3",
    "accent_pressed": "#0b86a0",
    "success": "#10b981",
    "warning": "#f59e0b",
    "error": "#ef4444",
    "info": "#3b82f6",
    "border": "#1a2431",
    "border_light": "#2c3b4c",
    "scrollbar_bg": "#070b12",
    "scrollbar_handle": "#1a2533",
    "tab_active": "#22d3ee",
    "tab_inactive": "#0c121b",
    "sidebar_bg": "#05080d",
    "sidebar_item_hover": "#0c121b",
    "terminal_fg": "#eef5f8",
    "terminal_cursor": "#22d3ee",
    "bg_gradient": "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #070b12, stop:1 #03060a)",
    "glass_bg": "rgba(7, 11, 18, 0.8)",
    "glass_border": "1px solid rgba(255, 255, 255, 0.05)",
    "aura_1": "rgba(34, 211, 238, 0.14)",
    "aura_2": "rgba(8, 47, 73, 0.12)",
}


DARK_THEME = {
    "name": "Dark",
    "bg_primary": "#0a0b10",
    "bg_secondary": "#14151f",
    "bg_tertiary": "#1c1d29",
    "bg_input": "#0f1018",
    "bg_hover": "#252736",
    "bg_selected": "#312e81",
    "bg_terminal": "#000000",
    "text_primary": "#f8fafc",
    "text_secondary": "#94a3b8",
    "text_muted": "#687c99",
    # Indigo one step up from #6366f1, which sat at the luminance where
    # neither white nor dark ink reads on a button; the old accent is now
    # the pressed face.
    "accent": "#7c7ff5",
    "accent_hover": "#979dfb",
    "accent_pressed": "#6366f1",
    "success": "#10b981",
    "warning": "#f59e0b",
    "error": "#ef4444",
    "info": "#3b82f6",
    "border": "#212533",
    "border_light": "#334155",
    "scrollbar_bg": "#0a0b10",
    "scrollbar_handle": "#1c1d29",
    "tab_active": "#6366f1",
    "tab_inactive": "#14151f",
    "sidebar_bg": "#08090d",
    "sidebar_item_hover": "#14151f",
    "terminal_fg": "#f1f5f9",
    "terminal_cursor": "#6366f1",
    "bg_gradient": "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0a0b10, stop:1 #050608)",
    "glass_bg": "rgba(10, 11, 16, 0.8)",
    "glass_border": "1px solid rgba(255, 255, 255, 0.05)",
    "aura_1": "rgba(99, 102, 241, 0.15)",
    "aura_2": "rgba(30, 27, 75, 0.1)",
}


NEBULA_THEME = {
    "name": "Nebula",
    "bg_primary": "#05060f",
    "bg_secondary": "rgba(15, 17, 28, 0.7)", # Translucent glass
    "bg_tertiary": "rgba(25, 27, 45, 0.5)",
    "bg_input": "rgba(0, 0, 0, 0.4)",
    "bg_hover": "rgba(255, 255, 255, 0.08)",
    "bg_selected": "rgba(139, 92, 246, 0.3)",
    "bg_terminal": "rgba(5, 6, 12, 0.8)",
    "text_primary": "#ffffff",
    "text_secondary": "#94a3b8",
    "text_muted": "#667a97",
    "accent": "#a78bfa", # Vibrant purple
    "accent_hover": "#c4b5fd",
    "accent_pressed": "#8b5cf6",
    "success": "#10b981",
    "warning": "#f59e0b",
    "error": "#ef4444",
    "info": "#3b82f6",
    "border": "rgba(255, 255, 255, 0.12)",
    "border_light": "rgba(255, 255, 255, 0.05)",
    "scrollbar_bg": "transparent",
    "scrollbar_handle": "rgba(139, 92, 246, 0.4)",
    "tab_active": "#a78bfa",
    "tab_inactive": "rgba(15, 17, 28, 0.5)",
    "sidebar_bg": "rgba(5, 6, 12, 0.6)",
    "sidebar_item_hover": "rgba(255, 255, 255, 0.05)",
    "terminal_fg": "#f8fafc",
    "terminal_cursor": "#a78bfa",
    "bg_gradient": "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #05060f, stop:0.5 #1e1b4b, stop:1 #0f172a)",
    "glass_bg": "rgba(10, 10, 25, 0.65)",
    "glass_border": "1px solid rgba(255, 255, 255, 0.15)",
    "aura_1": "rgba(99, 102, 241, 0.2)",
    "aura_2": "rgba(167, 139, 250, 0.15)",
}

CATPPUCCIN_MOCHA = {
    "name": "Catppuccin",
    "bg_primary": "#1e1e2e",
    "bg_secondary": "#181825",
    "bg_tertiary": "#11111b",
    "bg_input": "#11111b",
    "bg_hover": "#313244",
    "bg_selected": "#45475a",
    "bg_terminal": "#1e1e2e",
    "text_primary": "#cdd6f4",
    "text_secondary": "#bac2de",
    "text_muted": "#85889f",
    "accent": "#cba6f7", # Mauve
    "accent_hover": "#f5c2e7", # Flamingo
    "accent_pressed": "#89b4fa", # Blue
    "success": "#a6e3a1",
    "warning": "#f9e2af",
    "error": "#f38ba8",
    "info": "#89dceb",
    "border": "#313244",
    "border_light": "#45475a",
    "scrollbar_bg": "#1e1e2e",
    "scrollbar_handle": "#313244",
    "tab_active": "#cba6f7",
    "tab_inactive": "#181825",
    "sidebar_bg": "#11111b",
    "sidebar_item_hover": "#1e1e2e",
    "terminal_fg": "#cdd6f4",
    "terminal_cursor": "#f5e0dc",
    "bg_gradient": "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1e1e2e, stop:1 #11111b)",
    "glass_bg": "rgba(30,30,46,0.8)",
    "glass_border": "1px solid rgba(203,166,247,0.2)",
    "aura_1": "rgba(203,166,247,0.1)",
    "aura_2": "rgba(137,180,250,0.1)",
}

TOKYO_NIGHT = {
    "name": "Tokyo Night",
    "bg_primary": "#1a1b26",
    "bg_secondary": "#16161e",
    "bg_tertiary": "#1f2335",
    "bg_input": "#16161e",
    "bg_hover": "#292e42",
    "bg_selected": "#3b4261",
    "bg_terminal": "#1a1b26",
    "text_primary": "#a9b1d6",
    "text_secondary": "#9aa5ce",
    "text_muted": "#7b83ac",
    "accent": "#7aa2f7",
    "accent_hover": "#7dcfff",
    "accent_pressed": "#bb9af7",
    "success": "#9ece6a",
    "warning": "#e0af68",
    "error": "#f7768e",
    "info": "#2ac3de",
    "border": "#24283b",
    "border_light": "#414868",
    "scrollbar_bg": "#1a1b26",
    "scrollbar_handle": "#24283b",
    "tab_active": "#7aa2f7",
    "tab_inactive": "#16161e",
    "sidebar_bg": "#16161e",
    "sidebar_item_hover": "#1a1b26",
    "terminal_fg": "#c0caf5",
    "terminal_cursor": "#c0caf5",
    "bg_gradient": "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1a1b26, stop:1 #16161e)",
    "glass_bg": "rgba(26, 27, 38, 0.85)",
    "glass_border": "1px solid rgba(122, 162, 247, 0.2)",
    "aura_1": "rgba(122, 162, 247, 0.1)",
    "aura_2": "rgba(187, 154, 247, 0.1)",
}

MATRIX_GLITCH_THEME = {
    "name": "Matrix",
    "dashboard_theme": "matrix",          # activates digital-rain in DashboardWidget
    # A mellow, cool-biased phosphor green rather than pure #00ff41 neon: the
    # near-blacks carry a hint of green so the text never has to fight a
    # pure-black canvas, and the mint accent sits closer to teal than lime.
    "bg_primary": "#050a08",
    "bg_secondary": "#08110c",
    "bg_tertiary": "#0c1811",
    "bg_input": "#060c09",
    "bg_hover": "#10221a",
    "bg_selected": "#153326",
    "bg_terminal": "#050a08",
    "text_primary": "#a3e4bc",
    "text_secondary": "#6fc48f",
    "text_muted": "#4a8962",
    "accent": "#5cd68f",
    "accent_hover": "#7ee2a6",
    "accent_pressed": "#45b876",
    "success": "#5cd68f",
    "warning": "#e8c95a",
    "error": "#e06c6c",
    "info": "#5cc8d6",
    "border": "#12281c",
    "border_light": "#1f4230",
    "scrollbar_bg": "#050a08",
    "scrollbar_handle": "#153326",
    "tab_active": "#5cd68f",
    "tab_inactive": "#08110c",
    "sidebar_bg": "#050a08",
    "sidebar_item_hover": "#08110c",
    "terminal_fg": "#a3e4bc",
    "terminal_cursor": "#7ee2a6",
    "bg_gradient": "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #050a08, stop:1 #08110c)",
    "glass_bg": "rgba(6, 14, 10, 0.88)",
    "glass_border": "1px solid rgba(92, 214, 143, 0.22)",
    "aura_1": "rgba(92, 214, 143, 0.05)",
    "aura_2": "rgba(60, 160, 140, 0.04)",
}

DOTWAVE_THEME = {
    "name": "Dot Wave",
    "dashboard_theme": "dotwave",          # activates animated 3-D dot-wave
    "bg_primary": "#030712",
    "bg_secondary": "#0d1117",
    "bg_tertiary": "#161b22",
    "bg_input": "#0d1117",
    "bg_hover": "#1c2128",
    "bg_selected": "#0c2d48",
    "bg_terminal": "#030712",
    "text_primary": "#e6edf3",
    "text_secondary": "#8b949e",
    "text_muted": "#6f7a88",
    "accent": "#58a6ff",               # Electric blue
    "accent_hover": "#79c0ff",
    "accent_pressed": "#388bfd",
    "success": "#3fb950",
    "warning": "#d29922",
    "error": "#f85149",
    "info": "#79c0ff",
    "border": "#21262d",
    "border_light": "#30363d",
    "scrollbar_bg": "#0d1117",
    "scrollbar_handle": "#21262d",
    "tab_active": "#58a6ff",
    "tab_inactive": "#0d1117",
    "sidebar_bg": "#010409",
    "sidebar_item_hover": "#0d1117",
    "terminal_fg": "#e6edf3",
    "terminal_cursor": "#58a6ff",
    "bg_gradient": "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #030712, stop:1 #0d1117)",
    "glass_bg": "rgba(13, 17, 23, 0.85)",
    "glass_border": "1px solid rgba(88, 166, 255, 0.15)",
    "aura_1": "rgba(88, 166, 255, 0.06)",
    "aura_2": "rgba(56, 139, 253, 0.04)",
}

AURORA_THEME = {
    "name": "Aurora",
    "dashboard_theme": "aurora",           # activates aurora-borealis animated background
    "bg_primary":        "#020a12",
    "bg_secondary":      "#061420",
    "bg_tertiary":       "#0b1e2e",
    "bg_input":          "#061420",
    "bg_hover":          "#102234",
    "bg_selected":       "#0d3348",
    "bg_terminal":       "#020a12",
    "text_primary":      "#d8f0f8",        # pale ice-blue
    "text_secondary":    "#7ecdc8",        # aurora teal
    "text_muted":        "#4e8098",
    "accent":            "#06d6ca",        # aurora teal
    "accent_hover":      "#34e8df",
    "accent_pressed":    "#04aaa0",
    "success":           "#3ecf8e",
    "warning":           "#f59e0b",
    "error":             "#f87171",
    "info":              "#38bdf8",
    "border":            "#0e2636",
    "border_light":      "#1c3a4a",
    "scrollbar_bg":      "#061420",
    "scrollbar_handle":  "#0e2636",
    "tab_active":        "#06d6ca",
    "tab_inactive":      "#061420",
    "sidebar_bg":        "#010810",
    "sidebar_item_hover":"#061420",
    "terminal_fg":       "#d8f0f8",
    "terminal_cursor":   "#06d6ca",
    "bg_gradient":       "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #020a12, stop:1 #061420)",
    "glass_bg":          "rgba(6, 20, 32, 0.88)",
    "glass_border":      "1px solid rgba(6, 214, 202, 0.20)",
    "aura_1":            "rgba(6, 214, 202, 0.07)",
    "aura_2":            "rgba(34, 211, 238, 0.04)",
}


# ── Animated-background themes (see app.dashboard_backgrounds) ─────
#
# Each one pairs a window palette with a "dashboard_theme" key that picks
# the dashboard's background painter and node skin.

SYNTHWAVE_THEME = {
    "name": "Synthwave",
    "dashboard_theme": "synthwave",        # scrolling retro grid under a striped sun
    "bg_primary":        "#0c021c",
    "bg_secondary":      "#170733",
    "bg_tertiary":       "#230d48",
    "bg_input":          "#12052a",
    "bg_hover":          "#2c1259",
    "bg_selected":       "#4a1466",
    "bg_terminal":       "#0c021c",
    "text_primary":      "#fbe9ff",        # lilac white
    "text_secondary":    "#c493e0",
    "text_muted":        "#9168b7",
    "accent":            "#ff2d95",        # hot pink
    "accent_hover":      "#ff69b4",
    "accent_pressed":    "#d61a78",
    "success":           "#22d3ee",        # cyan is the second neon
    "warning":           "#fbbf24",
    "error":             "#fb7185",
    "info":              "#22d3ee",
    "border":            "#2c1259",
    "border_light":      "#45207a",
    "scrollbar_bg":      "#170733",
    "scrollbar_handle":  "#2c1259",
    "tab_active":        "#ff2d95",
    "tab_inactive":      "#170733",
    "sidebar_bg":        "#090116",
    "sidebar_item_hover":"#170733",
    "terminal_fg":       "#fbe9ff",
    "terminal_cursor":   "#ff2d95",
    "bg_gradient":       "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0c021c, stop:1 #170733)",
    "glass_bg":          "rgba(23, 7, 51, 0.88)",
    "glass_border":      "1px solid rgba(255, 45, 149, 0.22)",
    "aura_1":            "rgba(255, 45, 149, 0.08)",
    "aura_2":            "rgba(34, 211, 238, 0.05)",
}

CIRCUIT_THEME = {
    "name": "Circuit",
    "dashboard_theme": "circuit",          # gold PCB traces with pulses of current
    "bg_primary":        "#050a07",
    "bg_secondary":      "#0b1410",
    "bg_tertiary":       "#122019",
    "bg_input":          "#070f0a",
    "bg_hover":          "#182a21",
    "bg_selected":       "#2a3a14",
    "bg_terminal":       "#050a07",
    "text_primary":      "#f3f0dc",        # warm off-white
    "text_secondary":    "#b9b48a",
    "text_muted":        "#708061",
    "accent":            "#facc15",        # gold
    "accent_hover":      "#fde047",
    "accent_pressed":    "#ca8a04",
    "success":           "#4ade80",
    "warning":           "#fb923c",
    "error":             "#f87171",
    "info":              "#fde68a",
    "border":            "#1a2a20",
    "border_light":      "#2d4232",
    "scrollbar_bg":      "#0b1410",
    "scrollbar_handle":  "#1a2a20",
    "tab_active":        "#facc15",
    "tab_inactive":      "#0b1410",
    "sidebar_bg":        "#030705",
    "sidebar_item_hover":"#0b1410",
    "terminal_fg":       "#f3f0dc",
    "terminal_cursor":   "#facc15",
    "bg_gradient":       "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #050a07, stop:1 #0b1410)",
    "glass_bg":          "rgba(11, 20, 16, 0.88)",
    "glass_border":      "1px solid rgba(250, 204, 21, 0.20)",
    "aura_1":            "rgba(250, 204, 21, 0.06)",
    "aura_2":            "rgba(74, 222, 128, 0.04)",
}

PLEXUS_THEME = {
    "name": "Plexus",
    "dashboard_theme": "plexus",           # drifting nodes joined by fading lines
    "bg_primary":        "#080518",
    "bg_secondary":      "#110c2a",
    "bg_tertiary":       "#1a1340",
    "bg_input":          "#0c0820",
    "bg_hover":          "#231a50",
    "bg_selected":       "#33206e",
    "bg_terminal":       "#080518",
    "text_primary":      "#f1ecff",
    "text_secondary":    "#b8a6e6",
    "text_muted":        "#8070b3",
    "accent":            "#c084fc",        # violet
    "accent_hover":      "#d8b4fe",
    "accent_pressed":    "#a855f7",
    "success":           "#34d399",
    "warning":           "#fbbf24",
    "error":             "#fb7185",
    "info":              "#a5b4fc",
    "border":            "#1e1646",
    "border_light":      "#32266a",
    "scrollbar_bg":      "#110c2a",
    "scrollbar_handle":  "#1e1646",
    "tab_active":        "#c084fc",
    "tab_inactive":      "#110c2a",
    "sidebar_bg":        "#060412",
    "sidebar_item_hover":"#110c2a",
    "terminal_fg":       "#f1ecff",
    "terminal_cursor":   "#c084fc",
    "bg_gradient":       "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #080518, stop:1 #110c2a)",
    "glass_bg":          "rgba(17, 12, 42, 0.88)",
    "glass_border":      "1px solid rgba(192, 132, 252, 0.20)",
    "aura_1":            "rgba(192, 132, 252, 0.08)",
    "aura_2":            "rgba(120, 70, 220, 0.05)",
}

WARP_THEME = {
    "name": "Warp",
    "dashboard_theme": "warp",             # a starfield streaking past at speed
    "bg_primary":        "#02040c",
    "bg_secondary":      "#080e1e",
    "bg_tertiary":       "#0f182e",
    "bg_input":          "#050914",
    "bg_hover":          "#16223e",
    "bg_selected":       "#0f3552",
    "bg_terminal":       "#02040c",
    "text_primary":      "#eaf6ff",        # star white
    "text_secondary":    "#8fb8d8",
    "text_muted":        "#597ba8",
    "accent":            "#38bdf8",        # sky blue
    "accent_hover":      "#7dd3fc",
    "accent_pressed":    "#0ea5e9",
    "success":           "#4ade80",
    "warning":           "#fbbf24",
    "error":             "#f87171",
    "info":              "#bae6fd",
    "border":            "#132038",
    "border_light":      "#22375a",
    "scrollbar_bg":      "#080e1e",
    "scrollbar_handle":  "#132038",
    "tab_active":        "#38bdf8",
    "tab_inactive":      "#080e1e",
    "sidebar_bg":        "#010309",
    "sidebar_item_hover":"#080e1e",
    "terminal_fg":       "#eaf6ff",
    "terminal_cursor":   "#38bdf8",
    "bg_gradient":       "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #02040c, stop:1 #080e1e)",
    "glass_bg":          "rgba(8, 14, 30, 0.88)",
    "glass_border":      "1px solid rgba(56, 189, 248, 0.20)",
    "aura_1":            "rgba(56, 189, 248, 0.07)",
    "aura_2":            "rgba(230, 244, 255, 0.03)",
}

EMBER_THEME = {
    "name": "Ember",
    "dashboard_theme": "ember",            # sparks rising from a bed of coals
    "bg_primary":        "#0e0604",
    "bg_secondary":      "#1a0c08",
    "bg_tertiary":       "#27130c",
    "bg_input":          "#140905",
    "bg_hover":          "#331a10",
    "bg_selected":       "#4d2412",
    "bg_terminal":       "#0e0604",
    "text_primary":      "#fff1e6",        # firelit white
    "text_secondary":    "#d9a98c",
    "text_muted":        "#a86d53",
    "accent":            "#fb923c",        # orange
    "accent_hover":      "#fdba74",
    "accent_pressed":    "#ea580c",
    "success":           "#a3e635",
    "warning":           "#facc15",
    "error":             "#ef4444",
    "info":              "#fdba74",
    "border":            "#2e1810",
    "border_light":      "#4a281a",
    "scrollbar_bg":      "#1a0c08",
    "scrollbar_handle":  "#2e1810",
    "tab_active":        "#fb923c",
    "tab_inactive":      "#1a0c08",
    "sidebar_bg":        "#0a0403",
    "sidebar_item_hover":"#1a0c08",
    "terminal_fg":       "#fff1e6",
    "terminal_cursor":   "#fb923c",
    "bg_gradient":       "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0e0604, stop:1 #1a0c08)",
    "glass_bg":          "rgba(26, 12, 8, 0.88)",
    "glass_border":      "1px solid rgba(251, 146, 60, 0.22)",
    "aura_1":            "rgba(251, 146, 60, 0.08)",
    "aura_2":            "rgba(220, 50, 40, 0.05)",
}

#: Every selectable theme, in the order the View menu and Ctrl+T cycle them.
ALL_THEMES = [
    MOTE_THEME, DARK_THEME, NEBULA_THEME, CATPPUCCIN_MOCHA, TOKYO_NIGHT,
    MATRIX_GLITCH_THEME, DOTWAVE_THEME, AURORA_THEME,
    SYNTHWAVE_THEME, CIRCUIT_THEME, PLEXUS_THEME, WARP_THEME, EMBER_THEME,
]

# ANSI terminal color palettes
TERMINAL_COLORS_DARK = {
    "black": "#1e293b",
    "red": "#ff4d4d",
    "green": "#00ff99",
    "yellow": "#ffff66",
    "blue": "#3399ff",
    "magenta": "#ff66ff",
    "cyan": "#00ffff",
    "white": "#e2e8f0",
    "bright_black": "#64748b",
    "bright_red": "#ff8080",
    "bright_green": "#66ffb3",
    "bright_yellow": "#ffff99",
    "bright_blue": "#66b3ff",
    "bright_magenta": "#ff99ff",
    "bright_cyan": "#66ffff",
    "bright_white": "#ffffff",
}

TERMINAL_COLORS_LIGHT = {
    "black": "#1a1b2e",
    "red": "#dc2626",
    "green": "#16a34a",
    "yellow": "#ca8a04",
    "blue": "#2563eb",
    "magenta": "#9333ea",
    "cyan": "#0891b2",
    "white": "#e8e9f0",
    "bright_black": "#6b6e85",
    "bright_red": "#ef4444",
    "bright_green": "#22c55e",
    "bright_yellow": "#eab308",
    "bright_blue": "#3b82f6",
    "bright_magenta": "#a855f7",
    "bright_cyan": "#06b6d4",
    "bright_white": "#ffffff",
}


def _qcolor(value, fallback="#6366f1"):
    from PyQt6.QtGui import QColor

    text = str(value or "").strip()
    if text.startswith("rgba(") or text.startswith("rgb("):
        try:
            parts = [float(x) for x in text[text.index("(") + 1:text.index(")")].split(",")]
            r, g, b = (int(round(v)) for v in parts[:3])
            alpha = parts[3] if len(parts) > 3 else 1.0
            return QColor(r, g, b, int(round(255 * (alpha if alpha <= 1 else alpha / 255))))
        except Exception:
            return QColor(fallback)
    color = QColor(text)
    return color if color.isValid() else QColor(fallback)


def rgba(value, alpha: float) -> str:
    """*value* (hex or rgba) at a new opacity, for hovers and soft fills."""
    c = _qcolor(value)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {max(0.0, min(1.0, alpha)):.3f})"


def solid(value) -> str:
    """A hex colour for places (SVG strokes) that cannot take rgba()."""
    return _qcolor(value).name()


def on_color(value) -> str:
    """Readable text on a filled *value* background (white or near-black).

    Whichever contrasts more (WCAG), so mid-bright cyans, teals and
    violets -- where white sits under 4.5:1 -- get dark ink.
    """
    face = solid(value)
    return (BUTTON_INK_DARK if contrast_ratio(BUTTON_INK_DARK, face)
            > contrast_ratio(BUTTON_INK_LIGHT, face) else BUTTON_INK_LIGHT)


def accent_rgba(alpha: float = 1.0) -> str:
    """The current theme's accent, with *alpha*, for widget style sheets.

    The main window copies the accent into the application palette
    (Highlight), so screens that build their own style sheets follow the
    theme instead of a purple fixed in the code.
    """
    from PyQt6.QtGui import QGuiApplication, QPalette
    app = QGuiApplication.instance()
    color = (app.palette().color(QPalette.ColorRole.Highlight) if app is not None
             else QColor("#8b5cf6"))
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha:.2f})"


def primary_ink() -> str:
    """The ink primary (accent-filled) buttons are lettered in, for icons
    that sit on them.

    The main window keeps the application palette's HighlightedText at
    :func:`button_text_color` of the current theme, so this matches the
    button's own label.
    """
    from PyQt6.QtGui import QGuiApplication, QPalette
    app = QGuiApplication.instance()
    if app is None:
        return BUTTON_INK_LIGHT
    return app.palette().color(QPalette.ColorRole.HighlightedText).name()


def transparent_container_stylesheet(theme: dict) -> str:
    """For the window's content area, which lets the background aura show.

    It used to be the bare declaration ``background: transparent;`` — and a
    style sheet without a selector applies to every widget under it, ahead
    of the application style sheet whatever its specificity. Every primary,
    danger and checked button in a tab lost its fill. The transparency stays;
    the button roles are restated here, where they outrank it.
    """
    t = theme
    accent = t['accent']
    on_accent = button_text_color(t)
    return f"""
    * {{ background: transparent; }}
    QPushButton:checked {{ background-color: {rgba(accent, 0.22)}; }}
    QPushButton[variant="primary"] {{
        background-color: {accent};
        border: 1px solid {accent};
        color: {on_accent};
    }}
    QPushButton[variant="primary"]:hover {{
        background-color: {t['accent_hover']};
        border-color: {t['accent_hover']};
    }}
    QPushButton[variant="primary"]:pressed {{ background-color: {t['accent_pressed']}; }}
    QPushButton[variant="primary"]:disabled {{
        background-color: {rgba(accent, 0.30)};
        border-color: transparent;
        color: {rgba(on_accent, 0.55)};
    }}
    QPushButton[variant="danger"] {{
        background-color: #dc2626;
        border: 1px solid #dc2626;
        color: #ffffff;
    }}
    QPushButton[variant="danger"]:hover {{ background-color: #ef4444; border-color: #ef4444; }}
    QPushButton[variant="success"] {{
        background-color: #15803d;
        border: 1px solid #15803d;
        color: #ffffff;
    }}
    QPushButton[variant="success"]:hover {{ background-color: #16a34a; border-color: #16a34a; }}
    """


def generate_stylesheet(theme: dict) -> str:
    """Generate a complete Qt stylesheet from a theme dictionary."""
    from app import icons

    t = theme
    accent = t['accent']
    on_accent = button_text_color(t)
    tab_close_icon = get_qss_resource_url("app", "resources", "icons", "tab_close.svg")
    chevron_right_icon = icons.file_url("chevron-right", solid(t['text_secondary']))
    chevron_down_icon = icons.file_url("chevron-down", solid(t['text_secondary']))
    arrow_down = icons.file_url("chevron-down", solid(t['text_secondary']), stroke=2.5)
    arrow_up = icons.file_url("chevron-up", solid(t['text_secondary']), stroke=2.5)
    arrow_down_disabled = icons.file_url("chevron-down", solid(t['text_muted']), stroke=2.5)
    check_icon = icons.file_url("check", on_accent, stroke=3)
    btn_bg = rgba(t['text_primary'], 0.06)
    btn_bg_hover = rgba(t['text_primary'], 0.11)
    btn_bg_pressed = rgba(t['text_primary'], 0.04)
    btn_border = rgba(t['text_primary'], 0.14)
    accent_soft = rgba(accent, 0.22)
    hint = t['text_secondary']
    return f"""
    /* === Global === */
    QMainWindow, QDialog {{
        background-color: {t['bg_primary']};
        color: {t['text_primary']};
    }}

    QMainWindow {{
        background: {t.get('bg_gradient', t['bg_primary'])};
    }}

    /* A scroll area's viewport fills with the bare palette colour, which is
       grey under some themes; let the dialog or window behind it show. */
    QScrollArea {{ background: transparent; }}
    QScrollArea > QWidget#qt_scrollarea_viewport {{ background: transparent; }}

    .glass, #side_panel_container, #snippets_panel_container {{
        background-color: {t.get('glass_bg', t['bg_secondary'])};
        border: {t.get('glass_border', '1px solid ' + t['border'])};
        border-radius: 14px;
    }}

    QWidget {{
        color: {t['text_primary']};
        font-family: 'Segoe UI', 'Inter', sans-serif;
        font-size: 13px;
    }}

    /* Global Glass FX */
    *[glass="true"] {{
        background-color: {t['bg_secondary']};
        border: 1px solid {t['border']};
        border-radius: 14px;
    }}

    /* === Menu Bar === */
    QMenuBar {{
        background-color: {t['bg_secondary']};
        color: {t['text_primary']};
        border-bottom: 1px solid {t['border']};
        padding: 2px 0;
        font-family: 'Segoe UI', sans-serif;
    }}
    QMenuBar::item {{
        padding: 6px 14px;
        border-radius: 6px;
        margin: 2px;
    }}
    QMenuBar::item:selected {{
        background-color: {t['bg_hover']};
    }}
    QMenu {{
        background-color: {t['bg_secondary']};
        border: 1px solid {t['border_light']};
        border-radius: 10px;
        padding: 6px;
    }}
    QMenu::item {{
        padding: 7px 28px 7px 12px;
        border-radius: 6px;
    }}
    QMenu::item:selected {{
        background-color: {t['bg_hover']};
    }}
    QMenu::item:disabled {{
        color: {t['text_muted']};
    }}
    QMenu::icon {{
        padding-left: 8px;
    }}
    QMenu::separator {{
        height: 1px;
        background-color: {t['border']};
        margin: 5px 10px;
    }}

    /* === Tab Widget === */
    QTabWidget::pane {{
        border: none;
        background-color: {t['bg_primary']};
    }}
    QTabBar {{
        background-color: {t['bg_secondary']};
    }}
    QTabBar::tab {{
        background-color: {t['tab_inactive']};
        color: {t['text_secondary']};
        padding: 8px 16px;
        border: none;
        border-bottom: 2px solid transparent;
        margin-right: 2px;
        min-width: 110px;
        font-family: 'Segoe UI', sans-serif;
    }}
    QTabBar::tab:selected {{
        background-color: {t['bg_primary']};
        color: {t['text_primary']};
        border-bottom: 2px solid {accent};
        font-weight: bold;
    }}
    QTabBar::tab:hover {{
        background-color: {t['bg_hover']};
        color: {t['text_primary']};
    }}
    QTabBar::close-button {{
        image: url("{tab_close_icon}");
        background-color: transparent;
        border-radius: 4px;
        width: 16px;
        height: 16px;
        subcontrol-position: right;
        margin-right: 4px;
    }}
    QTabBar::close-button:hover {{
        background-color: #ef4444;
    }}
    QTabBar QToolButton {{
        background-color: {t['bg_secondary']};
        border: 1px solid {t['border']};
        border-radius: 6px;
    }}

    /* === Buttons ===
       Neutral by default; one [variant="primary"] per dialog carries the
       accent, so the main action is obvious at a glance. */
    QPushButton {{
        background-color: {btn_bg};
        color: {t['text_primary']};
        border: 1px solid {btn_border};
        padding: 7px 16px;
        border-radius: 8px;
        font-weight: 600;
        font-size: 13px;
        font-family: 'Segoe UI', sans-serif;
    }}
    QPushButton:hover {{
        background-color: {btn_bg_hover};
        border-color: {rgba(accent, 0.65)};
    }}
    QPushButton:pressed {{
        background-color: {btn_bg_pressed};
    }}
    QPushButton:checked {{
        background-color: {accent_soft};
        border-color: {accent};
    }}
    QPushButton:disabled {{
        background-color: transparent;
        color: {t['text_muted']};
        border-color: {t['border']};
    }}
    QPushButton:focus {{
        border-color: {accent};
    }}
    QPushButton[variant="primary"] {{
        background-color: {accent};
        border: 1px solid {accent};
        color: {on_accent};
    }}
    QPushButton[variant="primary"]:hover {{
        background-color: {t['accent_hover']};
        border-color: {t['accent_hover']};
    }}
    QPushButton[variant="primary"]:pressed {{
        background-color: {t['accent_pressed']};
    }}
    QPushButton[variant="primary"]:disabled {{
        background-color: {rgba(accent, 0.30)};
        border-color: transparent;
        color: {rgba(on_accent, 0.55)};
    }}
    QPushButton[variant="danger"] {{
        background-color: #dc2626;
        border: 1px solid #dc2626;
        color: #ffffff;
    }}
    QPushButton[variant="danger"]:hover {{
        background-color: #ef4444;
        border-color: #ef4444;
    }}
    QPushButton[variant="success"] {{
        background-color: #15803d;
        border: 1px solid #15803d;
        color: #ffffff;
    }}
    QPushButton[variant="success"]:hover {{
        background-color: #16a34a;
        border-color: #16a34a;
    }}
    QPushButton[variant="ghost"] {{
        background-color: transparent;
        border-color: transparent;
        color: {t['text_secondary']};
    }}
    QPushButton[variant="ghost"]:hover {{
        background-color: {t['bg_hover']};
        color: {t['text_primary']};
    }}
    QPushButton[variant="icon"] {{
        padding: 0px;
        background-color: transparent;
        border: 1px solid {btn_border};
    }}
    QPushButton[variant="icon"]:hover {{
        background-color: {btn_bg_hover};
        border-color: {accent};
    }}
    QPushButton[flat="true"] {{
        background-color: transparent;
        border-color: transparent;
        color: {t['text_secondary']};
    }}
    QPushButton[flat="true"]:hover {{
        background-color: {t['bg_hover']};
        color: {t['text_primary']};
    }}

    /* === Inputs === */
    QLineEdit, QAbstractSpinBox, QComboBox, QPlainTextEdit#notes_input {{
        background-color: {t['bg_input']};
        color: {t['text_primary']};
        border: 1px solid {t['border_light']};
        border-radius: 8px;
        padding: 7px 10px;
        font-size: 13px;
        selection-background-color: {accent};
        selection-color: {on_accent};
        placeholder-text-color: {t['text_muted']};
        font-family: 'Segoe UI', sans-serif;
    }}
    QLineEdit:hover, QAbstractSpinBox:hover, QComboBox:hover {{
        border-color: {rgba(t['text_primary'], 0.28)};
    }}
    QLineEdit:focus, QAbstractSpinBox:focus, QComboBox:focus, QPlainTextEdit#notes_input:focus {{
        border-color: {accent};
    }}
    /* Toolbar fields are pinned to a fixed height, and the 7px vertical
       padding above left 13px placeholders ("username", "password") with
       nowhere to sit -- their descenders were clipped.  Tighter vertical
       padding gives the text a full line; the sides keep theirs (the
       combo's room for its arrow included). */
    QToolBar QLineEdit, QToolBar QAbstractSpinBox, QToolBar QComboBox {{
        padding-top: 3px;
        padding-bottom: 3px;
    }}
    QLineEdit:disabled, QAbstractSpinBox:disabled, QComboBox:disabled {{
        color: {t['text_muted']};
        background-color: {rgba(t['text_primary'], 0.025)};
        border-color: {t['border']};
    }}
    QLineEdit[readOnly="true"] {{
        background-color: {rgba(t['text_primary'], 0.03)};
    }}
    QComboBox {{
        padding-right: 30px;
    }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: 26px;
        border: none;
    }}
    QComboBox::down-arrow {{
        image: url("{arrow_down}");
        width: 12px;
        height: 12px;
    }}
    QComboBox::down-arrow:disabled {{
        image: url("{arrow_down_disabled}");
    }}
    QComboBox::down-arrow:on {{
        top: 1px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {t['bg_secondary']};
        border: 1px solid {t['border_light']};
        border-radius: 8px;
        padding: 4px;
        selection-background-color: {t['bg_hover']};
        selection-color: {t['text_primary']};
        color: {t['text_primary']};
        outline: 0;
    }}
    QAbstractSpinBox {{
        padding-right: 24px;
    }}
    QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
        subcontrol-origin: border;
        width: 22px;
        border: none;
        background: transparent;
    }}
    QAbstractSpinBox::up-button {{
        subcontrol-position: top right;
        margin-top: 3px;
    }}
    QAbstractSpinBox::down-button {{
        subcontrol-position: bottom right;
        margin-bottom: 3px;
    }}
    QAbstractSpinBox::up-arrow {{
        image: url("{arrow_up}");
        width: 10px;
        height: 10px;
    }}
    QAbstractSpinBox::down-arrow {{
        image: url("{arrow_down}");
        width: 10px;
        height: 10px;
    }}
    QAbstractSpinBox::up-button:hover, QAbstractSpinBox::down-button:hover {{
        background: {btn_bg_hover};
        border-radius: 4px;
    }}

    QTextEdit, QPlainTextEdit {{
        background-color: {t['bg_terminal']};
        color: {t['terminal_fg']};
        border: none;
    }}

    /* === Check boxes and radio buttons === */
    QCheckBox, QRadioButton {{
        spacing: 8px;
        background: transparent;
    }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 16px;
        height: 16px;
        border: 1px solid {rgba(t['text_primary'], 0.32)};
        background-color: {t['bg_input']};
    }}
    QCheckBox::indicator {{
        border-radius: 4px;
    }}
    QRadioButton::indicator {{
        border-radius: 8px;
    }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
        border-color: {accent};
    }}
    QCheckBox::indicator:checked {{
        background-color: {accent};
        border-color: {accent};
        image: url("{check_icon}");
    }}
    QRadioButton::indicator:checked {{
        background-color: {t['bg_input']};
        border: 5px solid {accent};
    }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
        border-color: {t['border']};
        background-color: transparent;
    }}
    QCheckBox:disabled, QRadioButton:disabled {{
        color: {t['text_muted']};
    }}

    /* === Sliders === */
    QSlider::groove:horizontal {{
        height: 4px;
        background: {rgba(t['text_primary'], 0.16)};
        border-radius: 2px;
    }}
    QSlider::sub-page:horizontal {{
        background: {accent};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {t['text_primary']};
        border: 2px solid {accent};
        width: 12px;
        height: 12px;
        margin: -6px 0;
        border-radius: 8px;
    }}

    /* === Labels === */
    QLabel {{
        color: {t['text_primary']};
        background: transparent;
    }}
    QLabel[class="muted"] {{
        color: {t['text_secondary']};
        font-size: 11px;
    }}
    QLabel[role="form-label"] {{
        color: {t['text_secondary']};
    }}
    QLabel[role="hint"] {{
        color: {hint};
        font-size: 11px;
    }}

    /* === Scrollbars === */
    QScrollBar:vertical {{
        background: {t['scrollbar_bg']};
        width: 10px;
        margin: 0;
        border-radius: 5px;
    }}
    QScrollBar::handle:vertical {{
        background: {t['scrollbar_handle']};
        min-height: 30px;
        border-radius: 4px;
        margin: 1px 2px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {t['border_light']};
    }}
    QScrollBar:horizontal {{
        background: {t['scrollbar_bg']};
        height: 10px;
        margin: 0;
        border-radius: 5px;
    }}
    QScrollBar::handle:horizontal {{
        background: {t['scrollbar_handle']};
        min-width: 30px;
        border-radius: 4px;
        margin: 2px 1px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {t['border_light']};
    }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        width: 0px;
        height: 0px;
        border: none;
        background: none;
    }}
    /* Without these, Windows draws its dotted "page" texture in the track. */
    QScrollBar::add-page, QScrollBar::sub-page {{
        background: none;
    }}
    QAbstractScrollArea::corner {{
        background: transparent;
    }}

    /* === Tree/List Views === */
    QTreeWidget, QListWidget, QTableWidget, QTreeView, QListView, QTableView {{
        background-color: {t.get('glass_bg', t['bg_secondary'])};
        border: {t.get('glass_border', '1px solid ' + t['border'])};
        border-radius: 12px;
        outline: 0;
        padding: 6px;
        show-decoration-selected: 1;
        alternate-background-color: {rgba(t['text_primary'], 0.025)};
    }}
    QTreeWidget::item, QListWidget::item {{
        padding: 6px 8px;
        border: none;
        margin: 0;
    }}
    QTreeWidget::item:hover, QListWidget::item:hover {{
        background-color: {rgba(t['text_primary'], 0.05)};
    }}
    QTreeWidget::item:selected, QTreeWidget::branch:selected,
    QListWidget::item:selected, QTableWidget::item:selected {{
        background-color: {t['bg_selected']};
        color: {t['text_primary']};
    }}
    QTreeWidget::branch {{
        background-color: transparent;
    }}
    QTreeWidget::branch:has-children:closed {{
        image: url("{chevron_right_icon}");
    }}
    QTreeWidget::branch:has-children:open {{
        image: url("{chevron_down_icon}");
    }}
    QTreeWidget::branch:!has-children {{
        image: none;
        border-image: none;
    }}
    QTreeWidget::branch:has-siblings {{
        border-image: none;
    }}
    QTreeWidget::branch:!has-siblings {{
        border-image: none;
    }}
    QTreeWidget::branch:has-siblings:adjoins-item {{
        border-image: none;
    }}
    QTreeWidget::branch:!has-siblings:adjoins-item {{
        border-image: none;
    }}
    QHeaderView::section {{
        background-color: {t['bg_tertiary']};
        color: {t['text_secondary']};
        border: none;
        border-right: 1px solid {t['border']};
        padding: 8px;
        font-weight: 600;
        font-size: 12px;
    }}

    /* === Toolbar === */
    QToolBar {{
        background-color: {t['bg_secondary']};
        border-bottom: 1px solid {t['border']};
        padding: 4px 8px;
        spacing: 6px;
    }}
    QToolButton {{
        background-color: transparent;
        border: none;
        border-radius: 6px;
        padding: 6px 10px;
        color: {t['text_secondary']};
        font-weight: 500;
    }}
    QToolButton:hover {{
        background-color: {t['bg_hover']};
        color: {t['text_primary']};
    }}
    QToolButton:checked {{
        background-color: {t['bg_selected']};
        color: {t['text_primary']};
    }}
    QToolButton[variant="icon"] {{
        padding: 0px;
        border: 1px solid {btn_border};
    }}
    QToolButton[variant="icon"]:hover {{
        border-color: {accent};
    }}
    QToolButton::menu-indicator {{
        image: none;
        width: 0px;
    }}
    QToolButton#section_toggle {{
        color: {t['text_secondary']};
        font-weight: 600;
        padding: 4px 2px;
        text-align: left;
        border-radius: 6px;
    }}
    QToolButton#section_toggle:hover {{
        color: {t['text_primary']};
        background-color: transparent;
    }}
    QToolButton#section_toggle:checked {{
        background-color: transparent;
        color: {t['text_primary']};
    }}

    /* === Status Bar === */
    QStatusBar {{
        background-color: {t['bg_secondary']};
        color: {t['text_secondary']};
        border-top: 1px solid {t['border']};
        font-size: 12px;
        font-family: 'Segoe UI', sans-serif;
    }}
    QStatusBar QLabel {{
        color: {t['text_secondary']};
        padding: 0 6px;
    }}
    QStatusBar::item {{
        border: none;
    }}

    /* === Splitter === */
    QSplitter::handle {{
        background-color: {t['border']};
    }}
    QSplitter::handle:horizontal {{
        width: 1px;
    }}
    QSplitter::handle:vertical {{
        height: 1px;
    }}

    /* === Progress Bar === */
    QProgressBar {{
        background-color: {t['bg_tertiary']};
        border: none;
        border-radius: 4px;
        height: 6px;
        text-align: center;
    }}
    QProgressBar::chunk {{
        background-color: {accent};
        border-radius: 4px;
    }}

    /* === Dock Widget (Side Panel) === */
    QDockWidget {{
        titlebar-close-icon: none;
        titlebar-normal-icon: none;
        color: {t['text_primary']};
    }}
    QDockWidget::title {{
        background-color: {t['sidebar_bg']};
        padding: 10px 12px;
        border-bottom: 1px solid {t['border']};
        font-weight: 600;
        font-family: 'Segoe UI', sans-serif;
        letter-spacing: 0.3px;
    }}

    /* === Group Box === */
    QGroupBox {{
        border: 1px solid {t['border']};
        border-radius: 10px;
        margin-top: 12px;
        padding: 18px 12px 12px 12px;
        font-weight: 600;
        background-color: {rgba(t['text_primary'], 0.015)};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 10px;
        padding: 0 6px;
        color: {t['text_secondary']};
    }}
    QGroupBox[flat="true"] {{
        border: none;
        background: transparent;
        padding: 4px 0 0 0;
        margin-top: 0;
    }}
    QFrame#dialog_footer {{
        background-color: {t['bg_secondary']};
        border-top: 1px solid {t['border']};
    }}

    /* === Dialog Buttons === */
    QDialogButtonBox QPushButton {{
        min-width: 84px;
    }}

    /* === Tooltips === */
    QToolTip {{
        background-color: {t['bg_tertiary']};
        color: {t['text_primary']};
        border: 1px solid {t['border_light']};
        border-radius: 6px;
        padding: 6px 10px;
        font-size: 12px;
    }}
    """
