"""
RPMote ANSI Parser — Converts ANSI escape codes to Qt rich text formatting.
Supports SGR codes: colors (8/16/256/truecolor), bold, italic, underline, dim, reverse.
"""

import re
from PyQt6.QtGui import QColor, QTextCharFormat, QFont
from app.themes import TERMINAL_COLORS_DARK, TERMINAL_COLORS_LIGHT, DARK_THEME

# ANSI CSI sequence: ESC [ [?>=]? [0-9;]* [A-Za-z]
ANSI_CSI_RE = re.compile(r'\x1b\[([?>=]?[0-9;]*)([A-Za-z])')
# ANSI OSC sequence: ESC ] [0-9;]* ; text (ST | BEL)
ANSI_OSC_RE = re.compile(r'\x1b][0-9;]*;[^\x07\x1b]*(\x07|\x1b\\)')
# General ESC sequences for character sets, etc.
ANSI_ESC_RE = re.compile(r'\x1b[()][A-B0-2]')

# Standard color name mapping (index → name)
COLOR_NAMES = [
    "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"
]
BRIGHT_COLOR_NAMES = [
    "bright_black", "bright_red", "bright_green", "bright_yellow",
    "bright_blue", "bright_magenta", "bright_cyan", "bright_white"
]

# 256-color palette (indices 0-255)
def _build_256_palette():
    """Build the standard 256-color xterm palette."""
    palette = []
    # 0-7: standard colors
    std = [
        "#000000", "#cc0000", "#00cc00", "#cccc00",
        "#0000cc", "#cc00cc", "#00cccc", "#cccccc"
    ]
    palette.extend(std)
    # 8-15: bright colors
    bright = [
        "#555555", "#ff5555", "#55ff55", "#ffff55",
        "#5555ff", "#ff55ff", "#55ffff", "#ffffff"
    ]
    palette.extend(bright)
    # 16-231: 6×6×6 color cube
    for r in range(6):
        for g in range(6):
            for b in range(6):
                rv = 55 + 40 * r if r else 0
                gv = 55 + 40 * g if g else 0
                bv = 55 + 40 * b if b else 0
                palette.append(f"#{rv:02x}{gv:02x}{bv:02x}")
    # 232-255: grayscale ramp
    for i in range(24):
        v = 8 + 10 * i
        palette.append(f"#{v:02x}{v:02x}{v:02x}")
    return palette

PALETTE_256 = _build_256_palette()


class AnsiState:
    """Tracks the current ANSI text formatting state."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.bold = False
        self.dim = False
        self.italic = False
        self.underline = False
        self.strikethrough = False
        self.reverse = False
        self.fg_color = None   # QColor or None (default)
        self.bg_color = None   # QColor or None (default)


class AnsiParser:
    """Parses ANSI escape sequences and produces QTextCharFormat objects."""

    def __init__(self, theme=None):
        self.theme = theme or DARK_THEME
        # Assuming most Project Lumina themes are dark/premium. 
        # We check the name or default to dark.
        is_light = self.theme.get("name") == "Light" # Fallback for backwards compatibility if needed
        self.dark_mode = not is_light
        self.state = AnsiState()
        self._update_palette()

    def _update_palette(self):
        """Select the appropriate terminal color palette."""
        self.ansi_colors = TERMINAL_COLORS_DARK if self.dark_mode else TERMINAL_COLORS_LIGHT

    def set_dark_mode(self, dark: bool):
        self.dark_mode = dark
        self._update_palette()

    def _color_from_index(self, index: int) -> QColor:
        """Convert a color index (0-255) to a QColor."""
        if index < 8:
            return QColor(self.ansi_colors[COLOR_NAMES[index]])
        elif index < 16:
            return QColor(self.ansi_colors[BRIGHT_COLOR_NAMES[index - 8]])
        elif index < 256:
            return QColor(PALETTE_256[index])
        return QColor(self.theme.get("terminal_fg", "#ffffff"))

    def _process_sgr(self, params: list[int]):
        """Process a single SGR (Select Graphic Rendition) sequence."""
        i = 0
        while i < len(params):
            p = params[i]
            if p == 0:
                self.state.reset()
            elif p == 1:
                self.state.bold = True
            elif p == 2:
                self.state.dim = True
            elif p == 3:
                self.state.italic = True
            elif p == 4:
                self.state.underline = True
            elif p == 7:
                self.state.reverse = True
            elif p == 9:
                self.state.strikethrough = True
            elif p == 21 or p == 22:
                self.state.bold = False
                self.state.dim = False
            elif p == 23:
                self.state.italic = False
            elif p == 24:
                self.state.underline = False
            elif p == 27:
                self.state.reverse = False
            elif p == 29:
                self.state.strikethrough = False
            # Foreground colors
            elif 30 <= p <= 37:
                self.state.fg_color = QColor(self.ansi_colors[COLOR_NAMES[p - 30]])
            elif p == 38:
                # Extended foreground color
                if i + 1 < len(params):
                    if params[i + 1] == 5 and i + 2 < len(params):
                        # 256-color: ESC[38;5;{n}m
                        self.state.fg_color = self._color_from_index(params[i + 2])
                        i += 2
                    elif params[i + 1] == 2 and i + 4 < len(params):
                        # Truecolor: ESC[38;2;{r};{g};{b}m
                        r, g, b = params[i + 2], params[i + 3], params[i + 4]
                        self.state.fg_color = QColor(r, g, b)
                        i += 4
            elif p == 39:
                self.state.fg_color = None
            elif p == 49:
                self.state.bg_color = None
            elif 90 <= p <= 97:
                self.state.fg_color = QColor(self.ansi_colors[BRIGHT_COLOR_NAMES[p - 90]])
            # Background colors
            elif 40 <= p <= 47:
                self.state.bg_color = QColor(self.ansi_colors[COLOR_NAMES[p - 40]])
            elif p == 48:
                if i + 1 < len(params):
                    if params[i + 1] == 5 and i + 2 < len(params):
                        self.state.bg_color = self._color_from_index(params[i + 2])
                        i += 2
                    elif params[i + 1] == 2 and i + 4 < len(params):
                        r, g, b = params[i + 2], params[i + 3], params[i + 4]
                        self.state.bg_color = QColor(r, g, b)
                        i += 4
            elif 100 <= p <= 107:
                self.state.bg_color = QColor(self.ansi_colors[BRIGHT_COLOR_NAMES[p - 100]])
            i += 1

    def get_format(self) -> QTextCharFormat:
        """Build a QTextCharFormat reflecting the current ANSI state."""
        fmt = QTextCharFormat()

        fg = self.state.fg_color
        bg = self.state.bg_color

        if self.state.reverse:
            fg, bg = bg, fg

        if fg:
            if self.state.dim:
                fg = QColor(fg.red() // 2, fg.green() // 2, fg.blue() // 2)
            fmt.setForeground(fg)
        else:
            default_fg = QColor(self.theme.get("terminal_fg", "#ffffff"))
            if self.state.dim:
                default_fg = QColor(
                    default_fg.red() // 2, default_fg.green() // 2, default_fg.blue() // 2
                )
            fmt.setForeground(default_fg)

        if bg:
            fmt.setBackground(bg)

        if self.state.bold:
            fmt.setFontWeight(QFont.Weight.Bold)

        if self.state.italic:
            fmt.setFontItalic(True)

        if self.state.underline:
            fmt.setFontUnderline(True)

        if self.state.strikethrough:
            fmt.setFontStrikeOut(True)

        return fmt

    def parse_and_format(self, text: str) -> list[tuple[str, QTextCharFormat]]:
        """Parse text with ANSI codes and return list of (text, format) tuples."""
        # 1. Pre-strip OSC sequences and other ESC sequences we don't handle
        text = ANSI_OSC_RE.sub('', text)
        text = ANSI_ESC_RE.sub('', text)
        
        # We don't check for CSI codes here because SSHTerminal already splits them
        # However, for safety, if we see one, we process it and move on.
        segments = []
        last_end = 0
        
        for match in ANSI_CSI_RE.finditer(text):
            start = match.start()
            if start > last_end:
                plain = text[last_end:start]
                if plain:
                    segments.append((plain, self.get_format()))
            
            # Update state if it's an SGR
            if match.group(2) == 'm':
                param_str = match.group(1)
                try:
                    params = [int(p) for p in param_str.split(';') if p.isdigit()]
                    if not params: params = [0]
                    self._process_sgr(params)
                except:
                    self.state.reset()
            
            last_end = match.end()
            
        if last_end < len(text):
            remaining = text[last_end:]
            if remaining:
                segments.append((remaining, self.get_format()))
                
        return segments

    def strip_ansi(self, text: str) -> str:
        """Remove all ANSI escape sequences from text."""
        text = ANSI_OSC_RE.sub('', text)
        text = ANSI_ESC_RE.sub('', text)
        return ANSI_CSI_RE.sub('', text)
