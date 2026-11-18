"""
Session History — who was connected where, and for how long.

Pick a period (and optionally a tenant): the window lists every session and
adds them up per tenant, which is the number an MSP needs for a timesheet or
an invoice. Export writes the same rows to CSV with decimal hours.
"""

from __future__ import annotations

from datetime import datetime, time as dtime, timedelta

from PyQt6.QtCore import QDate, Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDateEdit, QDialog, QDialogButtonBox,
    QFileDialog, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton,
    QSplitter, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from app import icons
from app.dialogs import confirm
from app.session_history import format_duration
from app.widgets import fit_to_screen, set_variant

PERIODS = ("Today", "Last 7 days", "This week", "This month", "Last month",
           "Last 90 days", "Custom")


def period_bounds(name: str, today: datetime | None = None):
    """(start, end) datetimes for a named period; end is exclusive."""
    now = today or datetime.now()
    midnight = datetime.combine(now.date(), dtime.min)
    if name == "Today":
        return midnight, midnight + timedelta(days=1)
    if name == "Last 7 days":
        return midnight - timedelta(days=6), midnight + timedelta(days=1)
    if name == "This week":
        start = midnight - timedelta(days=midnight.weekday())
        return start, start + timedelta(days=7)
    if name == "This month":
        start = midnight.replace(day=1)
        nxt = (start + timedelta(days=32)).replace(day=1)
        return start, nxt
    if name == "Last month":
        this_month = midnight.replace(day=1)
        start = (this_month - timedelta(days=1)).replace(day=1)
        return start, this_month
    if name == "Last 90 days":
        return midnight - timedelta(days=89), midnight + timedelta(days=1)
    return None


class SessionHistoryDialog(QDialog):
    def __init__(self, history, parent=None):
        super().__init__(parent)
        self.history = history
        self.setWindowTitle("Session History")
        self.setWindowIcon(icons.icon("history"))
        self.setMinimumSize(720, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        intro = QLabel("Every session RPMote opened, with how long it stayed open — "
                       "totals per tenant are ready for a timesheet or an invoice.")
        intro.setWordWrap(True)
        intro.setProperty("role", "hint")
        layout.addWidget(intro)

        filters = QHBoxLayout()
        self.period = QComboBox()
        self.period.addItems(PERIODS)
        self.period.setCurrentText("This month")
        filters.addWidget(QLabel("Period"))
        filters.addWidget(self.period)
        self.date_from = QDateEdit()
        self.date_to = QDateEdit()
        for edit in (self.date_from, self.date_to):
            edit.setCalendarPopup(True)
            edit.setDisplayFormat("yyyy-MM-dd")
        self.date_from.setDate(QDate.currentDate().addDays(-30))
        self.date_to.setDate(QDate.currentDate())
        filters.addWidget(self.date_from)
        filters.addWidget(QLabel("to"))
        filters.addWidget(self.date_to)
        filters.addSpacing(12)
        filters.addWidget(QLabel("Tenant"))
        self.tenant = QComboBox()
        self.tenant.setMinimumWidth(160)
        filters.addWidget(self.tenant, 1)
        layout.addLayout(filters)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.totals = QTreeWidget()
        self.totals.setHeaderLabels(["Tenant", "Time", "Sessions"])
        self.totals.setRootIsDecorated(False)
        self.totals.setAlternatingRowColors(True)
        self.totals.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.sessions = QTreeWidget()
        self.sessions.setHeaderLabels(["Started", "Tenant", "Connection", "Protocol", "Duration"])
        self.sessions.setRootIsDecorated(False)
        self.sessions.setAlternatingRowColors(True)
        self.sessions.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.sessions.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        splitter.addWidget(self.totals)
        splitter.addWidget(self.sessions)
        splitter.setSizes([260, 560])
        layout.addWidget(splitter, 1)

        self.summary = QLabel("")
        self.summary.setProperty("role", "hint")
        layout.addWidget(self.summary)

        row = QHBoxLayout()
        self.btn_export = QPushButton("Export CSV…")
        self.btn_export.setIcon(icons.icon("export"))
        set_variant(self.btn_export, "primary")
        self.btn_export.clicked.connect(self._export)
        self.btn_clear = QPushButton("Clear history…")
        self.btn_clear.setIcon(icons.icon("trash", "#f87171"))
        set_variant(self.btn_clear, "ghost")
        self.btn_clear.clicked.connect(self._clear)
        row.addWidget(self.btn_export)
        row.addWidget(self.btn_clear)
        row.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

        self.period.currentTextChanged.connect(self._on_period)
        self.date_from.dateChanged.connect(self._reload)
        self.date_to.dateChanged.connect(self._reload)
        self.tenant.currentTextChanged.connect(self._reload)
        self._loading_tenants = False
        self._on_period(self.period.currentText())
        self.resize(940, 560)
        fit_to_screen(self)

    # ── data ──────────────────────────────────────────────────────

    def _range(self):
        bounds = period_bounds(self.period.currentText())
        if bounds is None:
            start = datetime.combine(self.date_from.date().toPyDate(), dtime.min)
            end = datetime.combine(self.date_to.date().toPyDate(), dtime.min) + timedelta(days=1)
            return start, end
        return bounds

    def _on_period(self, name: str):
        custom = name == "Custom"
        self.date_from.setEnabled(custom)
        self.date_to.setEnabled(custom)
        bounds = period_bounds(name)
        if bounds is not None:
            self.date_from.blockSignals(True)
            self.date_to.blockSignals(True)
            self.date_from.setDate(QDate(bounds[0].year, bounds[0].month, bounds[0].day))
            last = bounds[1] - timedelta(days=1)
            self.date_to.setDate(QDate(last.year, last.month, last.day))
            self.date_from.blockSignals(False)
            self.date_to.blockSignals(False)
        self._reload()

    def _current_entries(self):
        start, end = self._range()
        entries = self.history.entries(since=start.timestamp(), until=end.timestamp())
        tenant = self.tenant.currentText()
        if tenant and tenant != "All tenants":
            entries = [e for e in entries if e.get("tenant") == tenant]
        return entries

    def _reload(self, *_args):
        if self._loading_tenants:
            return
        start, end = self._range()
        all_entries = self.history.entries(since=start.timestamp(), until=end.timestamp())
        tenants = sorted({e.get("tenant", "Default") for e in all_entries}, key=str.casefold)
        current = self.tenant.currentText()
        self._loading_tenants = True
        self.tenant.clear()
        self.tenant.addItem("All tenants")
        self.tenant.addItems(tenants)
        if current in tenants:
            self.tenant.setCurrentText(current)
        self._loading_tenants = False

        entries = self._current_entries()
        self.totals.clear()
        counts: dict[str, int] = {}
        for e in entries:
            counts[e.get("tenant", "Default")] = counts.get(e.get("tenant", "Default"), 0) + 1
        totals = self.history.totals_by_tenant(entries)
        for tenant, seconds in totals.items():
            item = QTreeWidgetItem([tenant, format_duration(seconds), str(counts.get(tenant, 0))])
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.totals.addTopLevelItem(item)

        self.sessions.clear()
        for e in entries:
            started = datetime.fromtimestamp(float(e.get("start", 0)))
            duration = format_duration(e.get("duration", 0)) + (" (open)" if e.get("open") else "")
            item = QTreeWidgetItem([started.strftime("%Y-%m-%d %H:%M"), e.get("tenant", ""),
                                    e.get("name", ""), str(e.get("protocol", "")).upper(), duration])
            item.setIcon(2, icons.protocol_icon(e.get("protocol", "ssh")))
            self.sessions.addTopLevelItem(item)

        total = sum(totals.values())
        self.summary.setText(
            f"{len(entries)} session{'s' if len(entries) != 1 else ''} · "
            f"{format_duration(total)} in total "
            f"({start:%Y-%m-%d} – {(end - timedelta(days=1)):%Y-%m-%d})")
        self.btn_export.setEnabled(bool(entries))

    # ── actions ───────────────────────────────────────────────────

    def _export(self):
        start, end = self._range()
        tenant = self.tenant.currentText()
        suffix = "" if tenant in ("", "All tenants") else f"-{tenant}"
        default = f"rpmote-sessions-{start:%Y%m%d}-{(end - timedelta(days=1)):%Y%m%d}{suffix}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export session history", default,
                                              "CSV files (*.csv)")
        if not path:
            return
        try:
            self.history.export_csv(path, self._current_entries())
        except OSError as exc:
            QMessageBox.warning(self, "Export", f"Could not write the file:\n{exc}")
            return
        QMessageBox.information(self, "Export", f"Saved {path}")

    def _clear(self):
        if not confirm(self, "Clear session history",
                       "Delete the whole session history?", "Clear history",
                       danger=True,
                       informative="Recents are kept. Export first if you need the times."):
            return
        self.history.clear()
        self._reload()
