"""Live lock controls shared by Settings and the main-window lock menu."""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QActionGroup
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QInputDialog, QLabel,
    QMenu, QMessageBox, QSpinBox, QVBoxLayout, QWidget,
)

from app.lock_policy import (IDLE_LOCK_SETTING, LOCK_TIMEOUT_PRESETS, LOCK_TRIGGER_LABELS,
                             normalize_lock_settings, timeout_label, update_window_lock_settings)


class LockOptionsWidget(QWidget):
    changed = pyqtSignal(dict)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        values = normalize_lock_settings(settings)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.preset = QComboBox()
        for minutes in LOCK_TIMEOUT_PRESETS:
            self.preset.addItem(timeout_label(minutes), minutes)
        self.preset.addItem("Custom…", None)
        self.minutes = QSpinBox()
        self.minutes.setRange(0, 1440)
        self.minutes.setSuffix(" min")
        self.minutes.setSpecialValueText("Never")
        self.minutes.setMaximumWidth(140)
        self.minutes.setValue(values[IDLE_LOCK_SETTING])
        index = self.preset.findData(values[IDLE_LOCK_SETTING])
        self.preset.setCurrentIndex(index if index >= 0 else self.preset.count() - 1)
        self.minutes.setVisible(index < 0)
        self.minutes.setEnabled(index < 0)
        row = QHBoxLayout()
        row.addWidget(self.preset, 1)
        row.addWidget(self.minutes)
        form.addRow("Idle timeout", row)
        layout.addLayout(form)
        hint = QLabel("Time without keyboard or mouse input in this app. Never disables the idle timeout; the lock events below still apply.")
        hint.setWordWrap(True)
        hint.setProperty("role", "hint")
        layout.addWidget(hint)
        self.triggers = {}
        for key, label in LOCK_TRIGGER_LABELS.items():
            check = QCheckBox(label)
            check.setChecked(values[key])
            check.toggled.connect(lambda checked, option=key: self.changed.emit({option: checked}))
            self.triggers[key] = check
            layout.addWidget(check)
        self.preset.currentIndexChanged.connect(self._choose_preset)
        self.minutes.valueChanged.connect(self._minutes_changed)

    def _choose_preset(self, _index):
        value = self.preset.currentData()
        custom = value is None
        self.minutes.setVisible(custom)
        self.minutes.setEnabled(custom)
        if custom:
            self.minutes.setFocus()
            self.minutes.selectAll()
        else:
            self.minutes.blockSignals(True)
            self.minutes.setValue(value)
            self.minutes.blockSignals(False)
            self.changed.emit({IDLE_LOCK_SETTING: value})

    def _minutes_changed(self, minutes):
        # A custom input stays editable even if it happens to equal a preset.
        if self.preset.currentData() is not None:
            self.preset.blockSignals(True)
            self.preset.setCurrentIndex(self.preset.count() - 1)
            self.preset.blockSignals(False)
            self.minutes.setVisible(True)
            self.minutes.setEnabled(True)
        self.changed.emit({IDLE_LOCK_SETTING: minutes})


class LockOptionsMenu(QMenu):
    def __init__(self, window):
        super().__init__("Screen Lock &Options", window)
        self.window = window
        self.addAction("Lock screen now", lambda: window.idle_lock.lock_now())
        self.addSeparator()
        self.addSection("Idle timeout")
        group = QActionGroup(self)
        group.setExclusive(True)
        self.timeouts = {}
        for minutes in LOCK_TIMEOUT_PRESETS:
            action = self.addAction(timeout_label(minutes))
            action.setCheckable(True)
            group.addAction(action)
            action.triggered.connect(lambda _checked=False, value=minutes: self._apply({IDLE_LOCK_SETTING: value}))
            self.timeouts[minutes] = action
        self.custom = self.addAction("Custom timeout…", self._custom)
        self.custom.setCheckable(True)
        group.addAction(self.custom)
        self.addSeparator()
        self.triggers = {}
        for key, label in LOCK_TRIGGER_LABELS.items():
            action = self.addAction(label)
            action.setCheckable(True)
            action.triggered.connect(lambda checked, option=key: self._apply({option: checked}))
            self.triggers[key] = action
        self.addSeparator()
        self.addAction("More lock settings…", lambda: window._show_settings("security"))
        self.aboutToShow.connect(self.refresh)
        self.refresh()

    def refresh(self):
        values = normalize_lock_settings(self.window.vault.settings)
        for minutes, action in self.timeouts.items():
            action.setChecked(minutes == values[IDLE_LOCK_SETTING])
        value = values[IDLE_LOCK_SETTING]
        self.custom.setChecked(value not in LOCK_TIMEOUT_PRESETS)
        self.custom.setText("Custom timeout…" if value in LOCK_TIMEOUT_PRESETS else f"Custom timeout… ({value} min)")
        for key, action in self.triggers.items():
            action.setChecked(values[key])

    def _apply(self, changes):
        try:
            update_window_lock_settings(self.window, changes)
        except Exception as exc:
            QMessageBox.warning(self.window, "Lock settings", f"Could not save the lock settings.\n{exc}")
        self.refresh()

    def _custom(self):
        minutes, accepted = QInputDialog.getInt(self.window, "Custom screen-lock timeout",
            "Minutes without input (0 disables the idle timeout):",
            normalize_lock_settings(self.window.vault.settings)[IDLE_LOCK_SETTING], 0, 1440)
        if accepted:
            self._apply({IDLE_LOCK_SETTING: minutes})
        else:
            self.refresh()
