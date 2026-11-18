"""
RPMote Graph Settings — live customisation panel for the dashboard topology.

Every control applies immediately to the live dashboard so the user can see
what a setting does before committing to it; Close simply dismisses the panel.
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QLabel,
    QComboBox, QCheckBox, QSlider, QPushButton, QDialogButtonBox, QMessageBox,
    QSpinBox, QDoubleSpinBox, QScrollArea, QWidget, QFrame,
)
from PyQt6.QtCore import Qt, pyqtSignal

from app.dashboard import DEFAULT_GRAPH_SETTINGS
from app.widgets import fit_to_screen, set_variant

#: Shared label column, so every section's fields start at the same x.
_LABEL_WIDTH = 130


def _form(box) -> QFormLayout:
    form = QFormLayout(box)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setHorizontalSpacing(12)
    form.setVerticalSpacing(8)
    return form


def _label(text: str) -> QLabel:
    label = QLabel(text)
    label.setFixedWidth(_LABEL_WIDTH)
    label.setMinimumHeight(30)
    label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    label.setProperty("role", "form-label")
    return label


#: (stored value, label shown to the user)
LAYOUT_CHOICES = [
    ("hub", "Hub — RPMote orb in the centre"),
    ("free", "Free floating — no centre, tenants spread out"),
]
ARRANGEMENT_CHOICES = [
    ("grid", "Grid — even rows and columns"),
    ("scatter", "Organic scatter — loosely placed"),
]
LINE_CHOICES = [
    ("curved", "Curved"),
    ("straight", "Straight"),
    ("elbow", "Elbow (right angles)"),
]
TILE_CHOICES = [
    ("auto", "Auto — compact while branches are shown"),
    ("full", "Full — name, count and device icons"),
    ("compact", "Compact — a single row"),
]
COLLAPSE_CHOICES = [
    ("auto", "Auto — collapse once the estate is large"),
    ("always", "Always — a tenant opens only when clicked"),
    ("never", "Never — every machine on screen at once"),
]
LABEL_CHOICES = [
    ("name", "Connection name"),
    ("name_host", "Name and host"),
    ("host", "Host only"),
    ("none", "No labels — dots only"),
]
#: Only the animated themes (Matrix, Aurora, Synthwave…) have a scene to dim.
BACKGROUND_CHOICES = [
    ("calm", "Calm — dimmed and slowed, the graph stands out"),
    ("balanced", "Balanced"),
    ("vivid", "Vivid — the scene at full strength"),
    ("plain", "Plain — no animated scene"),
]


class _Slider(QSlider):
    """Horizontal slider working in hundredths so it can carry a float."""

    def __init__(self, lo: float, hi: float, value: float, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setRange(int(lo * 100), int(hi * 100))
        self.setValue(int(round(value * 100)))
        self.setSingleStep(5)
        self.setPageStep(10)

    def value_f(self) -> float:
        return self.value() / 100.0

    def set_value_f(self, value: float):
        self.setValue(int(round(value * 100)))


class GraphSettingsDialog(QDialog):
    """Live-editing panel for how the dashboard graph looks and behaves."""

    reset_positions_requested = pyqtSignal()
    clear_links_requested = pyqtSignal()
    ping_settings_changed = pyqtSignal(dict)
    check_now_requested = pyqtSignal()

    def __init__(self, dashboard, parent=None, ping_settings=None):
        super().__init__(parent)
        self.dashboard = dashboard
        self._loading = False
        self._ping_settings = dict(ping_settings) if ping_settings else None
        self.setWindowTitle("Graph Settings")
        self.setMinimumWidth(520)

        # Everything scrolls except the buttons, which stay on screen however
        # short the display: this dialog used to be taller than a laptop.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        root = QVBoxLayout(body)
        root.setContentsMargins(20, 16, 20, 12)
        root.setSpacing(12)

        intro = QLabel(
            "Changes apply straight away.  Right-drag anywhere on the graph to "
            "pan, Alt-drag from one node to another to link them — tenants and "
            "individual connections alike."
        )
        intro.setWordWrap(True)
        intro.setProperty("role", "hint")
        root.addWidget(intro)

        # ── Layout ─────────────────────────────────────────────────
        layout_box = QGroupBox("Layout")
        lf = _form(layout_box)
        self.cmb_layout = self._combo(LAYOUT_CHOICES)
        self.cmb_arrangement = self._combo(ARRANGEMENT_CHOICES)
        self.sld_spacing = _Slider(0.5, 2.5, 1.0)
        self.lbl_spacing = QLabel()
        lf.addRow(_label("Mode"), self.cmb_layout)
        lf.addRow(_label("Arrangement"), self.cmb_arrangement)
        lf.addRow(_label("Tenant spacing"), self._with_value(self.sld_spacing, self.lbl_spacing))
        root.addWidget(layout_box)

        # ── Connections ────────────────────────────────────────────
        conn_box = QGroupBox("Connections")
        cf = _form(conn_box)
        self.chk_branches = QCheckBox("Show each connection branching off its tenant")
        self.cmb_labels = self._combo(LABEL_CHOICES)
        self.cmb_collapse = self._combo(COLLAPSE_CHOICES)
        self.cmb_collapse.setToolTip(
            "Free-floating layout only — hub mode already opens one tenant at "
            "a time.")
        self.sld_spread = _Slider(1.0, 2.5, 1.0)
        self.lbl_spread = QLabel()
        cf.addRow(_label(""), self.chk_branches)
        cf.addRow(_label("Hide until opened"), self.cmb_collapse)
        cf.addRow(_label("Branch labels"), self.cmb_labels)
        cf.addRow(_label("Branch spread"), self._with_value(self.sld_spread, self.lbl_spread))
        root.addWidget(conn_box)

        # ── Appearance ─────────────────────────────────────────────
        look_box = QGroupBox("Appearance")
        af = _form(look_box)
        self.cmb_background = self._combo(BACKGROUND_CHOICES)
        self.cmb_background.setToolTip(
            "How strongly an animated theme's scene shows behind the graph.")
        self.cmb_lines = self._combo(LINE_CHOICES)
        self.cmb_tiles = self._combo(TILE_CHOICES)
        self.sld_nodes = _Slider(0.6, 1.6, 1.0)
        self.lbl_nodes = QLabel()
        self.chk_spokes = QCheckBox("Draw lines from the RPMote orb to each tenant")
        self.chk_particles = QCheckBox("Animated particles along the lines")
        self.chk_animate = QCheckBox("Animation (uncheck to save CPU)")
        af.addRow(_label("Background"), self.cmb_background)
        af.addRow(_label("Line style"), self.cmb_lines)
        af.addRow(_label("Tenant tiles"), self.cmb_tiles)
        af.addRow(_label("Tenant tile size"), self._with_value(self.sld_nodes, self.lbl_nodes))
        af.addRow(_label(""), self.chk_spokes)
        af.addRow(_label(""), self.chk_particles)
        af.addRow(_label(""), self.chk_animate)
        root.addWidget(look_box)

        # ── Reachability ───────────────────────────────────────────
        # Only offered when the host actually owns a probe engine; the dialog
        # stays usable on its own in tests and previews.
        if self._ping_settings is not None:
            ping_box = QGroupBox("Reachability")
            pf = _form(ping_box)
            self.chk_ping = QCheckBox(
                "Check whether each connection is reachable")
            self.spn_interval = QSpinBox()
            self.spn_interval.setRange(5, 3600)
            self.spn_interval.setSuffix(" s")
            self.spn_timeout = QDoubleSpinBox()
            self.spn_timeout.setRange(0.5, 15.0)
            self.spn_timeout.setSingleStep(0.5)
            self.spn_timeout.setSuffix(" s")
            self.chk_icmp = QCheckBox(
                "Fall back to ping, to tell a stopped service from a dead host")
            self.chk_status_dots = QCheckBox("Show status dots on the graph")
            # Stays available with continuous probing off: a single sweep at
            # startup is a different, cheaper thing to want.
            self.chk_login_sweep = QCheckBox(
                "Check everything once, as soon as the vault is unlocked")
            self.btn_check_now = QPushButton("Check all now")
            self.btn_check_now.setToolTip("Probe every connection once (Ctrl+Shift+H)")
            for spin in (self.spn_interval, self.spn_timeout):
                spin.setMaximumWidth(140)
            pf.addRow(_label(""), self.chk_ping)
            pf.addRow(_label("Check every"), self.spn_interval)
            pf.addRow(_label("Give up after"), self.spn_timeout)
            pf.addRow(_label(""), self.chk_icmp)
            pf.addRow(_label(""), self.chk_status_dots)
            pf.addRow(_label(""), self.chk_login_sweep)
            pf.addRow(_label(""), self.btn_check_now)
            root.addWidget(ping_box)
        else:
            self.chk_ping = None

        # ── Housekeeping ───────────────────────────────────────────
        tools_box = QGroupBox("Reset")
        tools = QHBoxLayout(tools_box)
        self.btn_reset_pos = QPushButton("Reset node positions")
        self.btn_clear_links = QPushButton("Clear custom links")
        self.btn_defaults = QPushButton("Restore defaults")
        for b in (self.btn_reset_pos, self.btn_clear_links, self.btn_defaults):
            tools.addWidget(b)
        set_variant(self.btn_clear_links, "ghost")
        tools.addStretch()
        root.addWidget(tools_box)
        root.addStretch(1)

        footer = QFrame()
        footer.setObjectName("dialog_footer")
        footer_row = QHBoxLayout(footer)
        footer_row.setContentsMargins(20, 10, 20, 12)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        footer_row.addWidget(buttons)
        outer.addWidget(footer)
        self.resize(560, 860)

        # ── Wiring ─────────────────────────────────────────────────
        self.cmb_layout.currentIndexChanged.connect(self._apply)
        self.cmb_arrangement.currentIndexChanged.connect(self._apply)
        self.cmb_labels.currentIndexChanged.connect(self._apply)
        self.cmb_lines.currentIndexChanged.connect(self._apply)
        self.cmb_background.currentIndexChanged.connect(self._apply)
        self.cmb_tiles.currentIndexChanged.connect(self._apply)
        self.cmb_collapse.currentIndexChanged.connect(self._apply)
        for chk in (self.chk_branches, self.chk_spokes,
                    self.chk_particles, self.chk_animate):
            chk.toggled.connect(self._apply)
        for sld in (self.sld_spacing, self.sld_spread, self.sld_nodes):
            sld.valueChanged.connect(self._apply)

        if self.chk_ping is not None:
            self.chk_ping.toggled.connect(self._apply_ping)
            self.chk_icmp.toggled.connect(self._apply_ping)
            self.chk_login_sweep.toggled.connect(self._apply_ping)
            self.spn_interval.valueChanged.connect(self._apply_ping)
            self.spn_timeout.valueChanged.connect(self._apply_ping)
            self.chk_status_dots.toggled.connect(self._apply)
            self.btn_check_now.clicked.connect(self.check_now_requested.emit)
            self._load_ping(self._ping_settings)

        self.btn_reset_pos.clicked.connect(self._reset_positions)
        self.btn_clear_links.clicked.connect(self._clear_links)
        self.btn_defaults.clicked.connect(self._restore_defaults)

        self.load_from(dashboard.get_graph_settings())
        fit_to_screen(self)

    # ── Small builders ─────────────────────────────────────────────

    @staticmethod
    def _combo(choices) -> QComboBox:
        cmb = QComboBox()
        for value, label in choices:
            cmb.addItem(label, value)
        return cmb

    @staticmethod
    def _with_value(slider: _Slider, label: QLabel):
        from PyQt6.QtWidgets import QWidget
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        label.setMinimumWidth(42)
        label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(slider, 1)
        row.addWidget(label)
        return holder

    @staticmethod
    def _select(cmb: QComboBox, value):
        idx = cmb.findData(value)
        cmb.setCurrentIndex(idx if idx >= 0 else 0)

    # ── State ──────────────────────────────────────────────────────

    def load_from(self, gs: dict):
        """Fill every widget from *gs* without triggering an apply loop."""
        self._loading = True
        try:
            self._select(self.cmb_layout, gs.get("layout_mode", "hub"))
            self._select(self.cmb_arrangement, gs.get("free_arrangement", "grid"))
            self._select(self.cmb_labels, gs.get("branch_labels", "name"))
            self._select(self.cmb_lines, gs.get("line_style", "curved"))
            self._select(self.cmb_background, gs.get("background", "calm"))
            self._select(self.cmb_tiles, gs.get("tile_detail", "auto"))
            self._select(self.cmb_collapse, gs.get("collapse_tenants", "auto"))
            self.chk_branches.setChecked(bool(gs.get("show_branches", True)))
            self.chk_spokes.setChecked(bool(gs.get("show_hub_spokes", True)))
            self.chk_particles.setChecked(bool(gs.get("particles", True)))
            self.chk_animate.setChecked(bool(gs.get("animate", True)))
            self.sld_spacing.set_value_f(float(gs.get("cluster_spacing", 1.0)))
            self.sld_spread.set_value_f(float(gs.get("branch_spread", 1.0)))
            self.sld_nodes.set_value_f(float(gs.get("node_scale", 1.0)))
            if self.chk_ping is not None:
                self.chk_status_dots.setChecked(bool(gs.get("show_status", True)))
        finally:
            self._loading = False
        self._sync_enabled()

    def _load_ping(self, settings: dict):
        self._loading = True
        try:
            self.chk_ping.setChecked(bool(settings.get("enabled", False)))
            self.spn_interval.setValue(int(settings.get("interval", 30)))
            self.spn_timeout.setValue(float(settings.get("timeout", 2.0)))
            self.chk_icmp.setChecked(bool(settings.get("icmp_fallback", True)))
            self.chk_login_sweep.setChecked(
                bool(settings.get("check_on_login", True)))
        finally:
            self._loading = False
        self._sync_enabled()

    def _apply_ping(self):
        if self._loading:
            return
        self._sync_enabled()
        self.ping_settings_changed.emit({
            "enabled": self.chk_ping.isChecked(),
            "interval": self.spn_interval.value(),
            "timeout": round(self.spn_timeout.value(), 2),
            "icmp_fallback": self.chk_icmp.isChecked(),
            "check_on_login": self.chk_login_sweep.isChecked(),
        })

    def _collect(self) -> dict:
        return {
            "layout_mode": self.cmb_layout.currentData(),
            "free_arrangement": self.cmb_arrangement.currentData(),
            "branch_labels": self.cmb_labels.currentData(),
            "line_style": self.cmb_lines.currentData(),
            "background": self.cmb_background.currentData(),
            "tile_detail": self.cmb_tiles.currentData(),
            "collapse_tenants": self.cmb_collapse.currentData(),
            "show_branches": self.chk_branches.isChecked(),
            "show_hub_spokes": self.chk_spokes.isChecked(),
            "particles": self.chk_particles.isChecked(),
            "animate": self.chk_animate.isChecked(),
            "cluster_spacing": round(self.sld_spacing.value_f(), 2),
            "branch_spread": round(self.sld_spread.value_f(), 2),
            "node_scale": round(self.sld_nodes.value_f(), 2),
            **({"show_status": self.chk_status_dots.isChecked()}
               if self.chk_ping is not None else {}),
        }

    def _sync_enabled(self):
        free = self.cmb_layout.currentData() == "free"
        self.cmb_arrangement.setEnabled(free)
        self.sld_spacing.setEnabled(free)
        self.chk_spokes.setEnabled(not free)
        branches = self.chk_branches.isChecked()
        self.cmb_labels.setEnabled(branches)
        self.cmb_collapse.setEnabled(branches and free)
        self.sld_spread.setEnabled(branches)
        self.chk_particles.setEnabled(self.chk_animate.isChecked())
        if self.chk_ping is not None:
            probing = self.chk_ping.isChecked()
            for widget in (self.spn_interval, self.spn_timeout, self.chk_icmp,
                           self.chk_status_dots):
                widget.setEnabled(probing)
        self.lbl_spacing.setText(f"{self.sld_spacing.value_f():.2f}×")
        self.lbl_spread.setText(f"{self.sld_spread.value_f():.2f}×")
        self.lbl_nodes.setText(f"{self.sld_nodes.value_f():.2f}×")

    # ── Actions ────────────────────────────────────────────────────

    def _apply(self):
        if self._loading:
            return
        self._sync_enabled()
        self.dashboard.set_graph_settings(self._collect(), emit=True)

    def _reset_positions(self):
        self.dashboard.reset_all_positions()
        self.reset_positions_requested.emit()

    def _clear_links(self):
        if not self.dashboard.get_tenant_links():
            QMessageBox.information(self, "Clear Custom Links",
                                    "There are no custom links to clear.")
            return
        confirm = QMessageBox.question(
            self, "Clear Custom Links",
            "Remove every link you have drawn between nodes?\n"
            "This only affects the picture — no connection is changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirm == QMessageBox.StandardButton.Yes:
            self.dashboard.clear_tenant_links()
            self.clear_links_requested.emit()

    def _restore_defaults(self):
        self.load_from(DEFAULT_GRAPH_SETTINGS)
        self.dashboard.set_graph_settings(dict(DEFAULT_GRAPH_SETTINGS), emit=True)
