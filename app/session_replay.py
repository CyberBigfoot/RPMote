"""Watching a session back, exactly as it looked at the time.

The recording keeps the raw ANSI, and RPMote already has an emulator and a
renderer that turn raw ANSI into a screen — so replay is the same screen the
session had, not a transcript of it.  Colours, progress bars, `htop`, an
editor someone opened by accident: all of it comes back.

Seeking rebuilds the screen by replaying from the start rather than trying to
run the emulator backwards.  A terminal's state is the sum of everything that
came before it — you cannot un-scroll a region or un-set a colour — and
feeding a few megabytes through the emulator is fast enough that the honest
way is also the quick one.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QSlider, QSplitter, QVBoxLayout, QWidget,
)

from app import session_log
from app.screen_buffer import ScreenBuffer
from app.terminal_view import TerminalView
from app.themes import DARK_THEME

SPEEDS = (0.5, 1.0, 2.0, 4.0, 8.0)

#: Gaps longer than this are skipped: nobody wants to watch the four minutes
#: someone spent reading the output before typing the next command.
IDLE_LIMIT = 2.0


def format_time(seconds) -> str:
    seconds = max(0, int(seconds or 0))
    return f"{seconds // 60:d}:{seconds % 60:02d}"

from app import icons

class ReplayPlayer:
    """The playback logic, with no widgets in it.

    Kept separate so the interesting parts — where a seek lands, what the
    screen holds after it, when playback is finished — can be exercised
    without a window or a clock.
    """

    def __init__(self, events=None, rows=24, cols=80):
        self.buffer = ScreenBuffer(rows=rows, cols=cols, scrollback_limit=20000)
        self._events = []
        self._index = 0
        self.load(events or [])

    # ── Loading ─────────────────────────────────────────────────────

    def load(self, events, rows=None, cols=None):
        self._events = [event for event in (events or []) if event[1] == "o"]
        if rows or cols:
            self.buffer.resize(rows or self.buffer.rows, cols or self.buffer.cols)
        self.rewind()

    @property
    def events(self) -> list:
        return list(self._events)

    @property
    def count(self) -> int:
        return len(self._events)

    @property
    def index(self) -> int:
        return self._index

    @property
    def finished(self) -> bool:
        return self._index >= len(self._events)

    @property
    def duration(self) -> float:
        return float(self._events[-1][0]) if self._events else 0.0

    @property
    def position(self) -> float:
        if not self._events or self._index <= 0:
            return 0.0
        return float(self._events[min(self._index, len(self._events)) - 1][0])

    # ── Moving through it ───────────────────────────────────────────

    def rewind(self):
        # RIS is the emulator's own "forget everything" — screen, scrollback,
        # colours, modes — so rewinding is a sequence, not a new buffer the
        # view would then have to be told about.
        self.buffer.feed("\x1bc")
        self._index = 0

    def step(self) -> bool:
        """Play the next chunk. False when there is nothing left."""
        if self.finished:
            return False
        self.buffer.feed(self._events[self._index][2])
        self._index += 1
        return True

    def seek(self, index: int):
        """Rebuild the screen as it was after *index* chunks."""
        index = max(0, min(int(index), len(self._events)))
        self.rewind()
        for event in self._events[:index]:
            self.buffer.feed(event[2])
        self._index = index

    def seek_time(self, seconds: float):
        """Seek to the first chunk at or after *seconds*."""
        target = float(seconds or 0)
        index = 0
        for position, event in enumerate(self._events):
            if float(event[0]) > target:
                break
            index = position + 1
        self.seek(index)

    def next_delay(self, speed=1.0, idle_limit=IDLE_LIMIT) -> float:
        """Seconds to wait before the next chunk, dead air clamped."""
        if self.finished:
            return 0.0
        speed = float(speed) if speed and float(speed) > 0 else 1.0
        previous = (float(self._events[self._index - 1][0])
                    if self._index else 0.0)
        gap = max(0.0, float(self._events[self._index][0]) - previous)
        if idle_limit:
            gap = min(gap, float(idle_limit))
        return gap / speed


class SessionReplayDialog(QDialog):
    """Pick a recording and watch it."""

    def __init__(self, parent=None, initial=None, theme=None, root=None):
        super().__init__(parent)
        self.setWindowTitle("Session Recordings")
        self.setMinimumSize(980, 620)
        self.theme = theme or DARK_THEME
        self._root = Path(root) if root is not None else session_log.SESSIONS_DIR
        self._speed = 1.0
        self._playing = False
        self.player = ReplayPlayer()

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._tick)

        self._build_ui()
        self.reload(select=initial)

    # ── Layout ──────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)

        self.list = QListWidget()
        self.list.setMinimumWidth(280)
        self.list.currentRowChanged.connect(self._on_selected)
        splitter.addWidget(self.list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        self.view = TerminalView()
        self.view.set_terminal_font(QFont("Consolas", 10))
        self.view.set_colors(self.theme.get("text_primary", "#e2e8f0"),
                             self.theme.get("bg_terminal", "#0a0e18"))
        self.view.set_buffer(self.player.buffer)
        self.view.set_placeholder("Choose a recording to play it back.")
        self.view.set_cursor_blink(False)
        right_layout.addWidget(self.view, 1)

        controls = QHBoxLayout()
        self.btn_play = QPushButton("Play")
        self.btn_play.setIcon(icons.icon("play"))
        self.btn_play.setFixedWidth(90)
        self.btn_play.clicked.connect(self.toggle_play)
        controls.addWidget(self.btn_play)

        self.btn_restart = QPushButton("⟲")
        self.btn_restart.setFixedWidth(36)
        self.btn_restart.setToolTip("Back to the start")
        self.btn_restart.clicked.connect(self.restart)
        controls.addWidget(self.btn_restart)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.slider.sliderMoved.connect(self._on_scrub)
        controls.addWidget(self.slider, 1)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setStyleSheet("color: #94a3b8; font-size: 11px;")
        controls.addWidget(self.time_label)

        self.speed_combo = QComboBox()
        for speed in SPEEDS:
            self.speed_combo.addItem(f"{speed:g}×", speed)
        self.speed_combo.setCurrentIndex(SPEEDS.index(1.0))
        self.speed_combo.currentIndexChanged.connect(self._on_speed)
        controls.addWidget(self.speed_combo)

        right_layout.addLayout(controls)

        self.status = QLabel("")
        self.status.setStyleSheet("color: #94a3b8; font-size: 11px;")
        right_layout.addWidget(self.status)

        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)

        footer = QHBoxLayout()
        footer.addStretch()
        btn_folder = QPushButton("Open folder")
        btn_folder.clicked.connect(self._open_folder)
        footer.addWidget(btn_folder)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        footer.addWidget(btn_close)
        layout.addLayout(footer)

    # ── The list ────────────────────────────────────────────────────

    def reload(self, select=None):
        self._recordings = session_log.list_recordings(self._root)
        self.list.clear()
        chosen = 0
        for position, row in enumerate(self._recordings):
            label = f"{row['name']}\n{session_log.format_size(row['size'])}"
            item = QListWidgetItem(label)
            item.setToolTip(str(row["path"]))
            self.list.addItem(item)
            if select is not None and Path(select) == row["path"]:
                chosen = position
        if self._recordings:
            self.list.setCurrentRow(chosen)
        else:
            self.status.setText(
                "No recordings yet — every SSH session records itself.")

    def _on_selected(self, row: int):
        if not (0 <= row < len(self._recordings)):
            return
        self.open_recording(self._recordings[row]["path"])

    def open_recording(self, path) -> bool:
        self.pause()
        try:
            header, events = session_log.read_cast(path)
        except session_log.CastError as exc:
            self.status.setText(str(exc))
            return False

        rows = int(header.get("height") or 24)
        cols = int(header.get("width") or 80)
        self.player.load(events, rows=rows, cols=cols)
        self.view.set_buffer(self.player.buffer)
        self.view.set_placeholder("")
        self.slider.setMaximum(max(0, self.player.count))
        self.slider.setValue(0)
        self.status.setText(
            f"{Path(path).name} · {header.get('title', '')} · "
            f"{format_time(self.player.duration)}")
        self._refresh()
        return True

    # ── Playing ─────────────────────────────────────────────────────

    def toggle_play(self):
        self.pause() if self._playing else self.play()

    def play(self):
        if not self.player.count:
            return
        if self.player.finished:
            self.player.rewind()
        self._playing = True
        self.btn_play.setText("Pause")
        self.btn_play.setIcon(icons.icon("pause"))
        self._schedule()

    def pause(self):
        self._playing = False
        self._timer.stop()
        self.btn_play.setText("Play")
        self.btn_play.setIcon(icons.icon("play"))

    def restart(self):
        self.player.rewind()
        self._refresh()

    def _schedule(self):
        if not self._playing or self.player.finished:
            self.pause()
            return
        delay = self.player.next_delay(self._speed)
        self._timer.start(max(0, int(delay * 1000)))

    def _tick(self):
        # Play everything that would have arrived in the same instant, so a
        # burst of output does not take one frame per chunk to appear.
        for _ in range(64):
            if not self.player.step():
                break
            if self.player.next_delay(self._speed) > 0.005:
                break
        self._refresh()
        self._schedule()

    def _on_speed(self, index: int):
        self._speed = self.speed_combo.itemData(index) or 1.0

    def _on_scrub(self, value: int):
        self.pause()
        self.player.seek(value)
        self._refresh(move_slider=False)

    def _refresh(self, move_slider=True):
        self.view.refresh()
        if move_slider:
            self.slider.blockSignals(True)
            self.slider.setValue(self.player.index)
            self.slider.blockSignals(False)
        self.time_label.setText(
            f"{format_time(self.player.position)} / "
            f"{format_time(self.player.duration)}")

    def _open_folder(self):
        from PyQt6.QtCore import QUrl
        from PyQt6.QtGui import QDesktopServices
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._root)))

    def closeEvent(self, event):
        self.pause()
        super().closeEvent(event)
