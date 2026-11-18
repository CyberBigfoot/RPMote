"""The strip that appears above a session typing to more than one machine.

Broadcast has to be loud.  A bar across the top of the panes, a colour nobody
uses for anything calm, a list of exactly which machines are listening, and a
count of where the last thing went — all of it exists so that "I thought I was
on the test box" stops being a sentence anyone says.
"""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMenu, QPushButton, QWidget,
)

from app import icons

class BroadcastBar(QWidget):
    """Arm the group, see who is listening, and type once for all of them."""

    send_requested = pyqtSignal(str)

    def __init__(self, group, parent=None):
        super().__init__(parent)
        self.group = group
        self.setObjectName("broadcast_bar")
        self.setFixedHeight(40)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(8)

        self.toggle = QPushButton("Broadcast")
        self.toggle.setIcon(icons.icon("broadcast"))
        self.toggle.setCheckable(True)
        self.toggle.setFixedHeight(28)
        self.toggle.setMinimumWidth(120)
        self.toggle.setToolTip(
            "Send what you type to every listed pane at once.\n"
            "Off by default, and it stays visible while it is on.")
        self.toggle.toggled.connect(self._on_toggled)
        layout.addWidget(self.toggle)

        self.panes_button = QPushButton("Panes")
        self.panes_button.setIcon(icons.icon("chevron-down"))
        self.panes_button.setIconSize(QSize(12, 12))
        self.panes_button.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.panes_button.setFixedHeight(28)
        self.panes_button.setToolTip("Choose which panes are listening.")
        self.panes_button.clicked.connect(self._show_panes_menu)
        layout.addWidget(self.panes_button)

        self.input = QLineEdit()
        self.input.setFixedHeight(28)
        self.input.setPlaceholderText(
            "Type a command here to run it everywhere, then press Enter…")
        self.input.returnPressed.connect(self._on_return)
        layout.addWidget(self.input, 1)

        self.send_button = QPushButton("Send")
        self.send_button.setFixedHeight(28)
        self.send_button.clicked.connect(self._on_return)
        layout.addWidget(self.send_button)

        self.status = QLabel("")
        self.status.setStyleSheet("color: #fde68a; font-size: 11px;")
        layout.addWidget(self.status)

        self._apply_style()
        if group is not None:
            group.changed.connect(self.refresh)
        self.refresh()

    # ── Appearance ──────────────────────────────────────────────────

    def _apply_style(self):
        armed = bool(self.group is not None and self.group.armed)
        background = "#78350f" if armed else "rgba(120, 53, 15, 0.35)"
        border = "#f59e0b" if armed else "rgba(245, 158, 11, 0.4)"
        self.setStyleSheet(f"""
            #broadcast_bar {{
                background: {background};
                border-bottom: 1px solid {border};
            }}
            QPushButton {{
                font-size: 11px; padding: 2px 10px; color: #fde68a;
                background: rgba(0,0,0,0.25); border-radius: 6px;
                border: 1px solid rgba(245, 158, 11, 0.5);
            }}
            QPushButton:hover {{ background: rgba(0,0,0,0.4); }}
            QPushButton:checked {{ background: #b45309; color: #ffffff; }}
            QLineEdit {{
                font-size: 12px; padding: 2px 8px; color: #fff7ed;
                background: rgba(0,0,0,0.35); border-radius: 6px;
                border: 1px solid rgba(245, 158, 11, 0.5);
            }}
        """)

    def refresh(self):
        group = self.group
        if group is None:
            return
        rows = group.labels()
        listening = [label for _terminal, label, included, live in rows
                     if included and live]

        was_blocked = self.toggle.blockSignals(True)
        self.toggle.setChecked(bool(group.armed))
        self.toggle.blockSignals(was_blocked)

        self.toggle.setText("Broadcast ON" if group.armed else "Broadcast")
        self.panes_button.setText(f"Panes ({len(listening)}/{len(rows)})")
        enabled = bool(group.armed)
        self.input.setEnabled(enabled)
        self.send_button.setEnabled(enabled)
        if not group.armed:
            self.status.setText("")
        elif listening:
            self.status.setText("→ " + ", ".join(listening[:4])
                                + (f" +{len(listening) - 4}"
                                   if len(listening) > 4 else ""))
        else:
            self.status.setText("nothing is listening")
        self._apply_style()

    # ── Actions ─────────────────────────────────────────────────────

    def _on_toggled(self, checked: bool):
        if self.group is not None:
            self.group.set_armed(checked)
        self.refresh()
        if checked:
            self.input.setFocus()

    def _on_return(self):
        text = self.input.text().strip()
        if not text or self.group is None or not self.group.armed:
            return
        self.send_requested.emit(text)

    def clear_input(self):
        self.input.clear()

    def report(self, count: int):
        """Say where the last thing actually went."""
        self.status.setText(f"sent to {count} pane{'s' if count != 1 else ''}")

    def _show_panes_menu(self):
        group = self.group
        if group is None:
            return
        menu = QMenu(self)
        actions = {}
        for terminal, label, included, live in group.labels():
            action = menu.addAction(label if live else f"{label} (offline)")
            action.setCheckable(True)
            action.setChecked(included)
            action.setEnabled(live)
            actions[action] = terminal
        if not actions:
            empty = menu.addAction("No panes yet")
            empty.setEnabled(False)
        chosen = menu.exec(self.panes_button.mapToGlobal(
            self.panes_button.rect().bottomLeft()))
        if chosen in actions:
            group.set_included(actions[chosen], chosen.isChecked())
