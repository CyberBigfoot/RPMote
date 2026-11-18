"""Cron schedule templates are reviewed/copied, never executed as shell input."""

import os
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDialog

from app.bash_commands_library import get_bash_command_library
from app.cron_library import get_cron_command_library
from app.cron_template_dialog import CronTemplateDialog
from app.command_palette import CommandPalette, palette_entries
from app.snippets_panel import SnippetsPanel

APP = QApplication.instance() or QApplication([])


def test_cron_examples_and_management_commands_are_searchable():
    entries = get_bash_command_library()
    templates = [e for e in entries if e.get("copy_only")]
    assert len(templates) >= 25
    assert all(e["category"] == "Cron jobs" for e in templates)
    assert any("PostgreSQL" in e["name"] for e in templates)
    assert any("flock" in e["name"] for e in templates)
    assert any(e["command"] == "crontab -e" and e["category"] == "Cron management" for e in entries)


def test_date_percent_signs_are_escaped_in_cron_but_not_shell_commands():
    entries = get_cron_command_library()
    for entry in entries:
        if entry.get("copy_only") and "/usr/bin/date " in entry["command"]:
            assert "\\%F" in entry["command"]
    backup = next(e for e in entries if e["name"] == "Back up my crontab")
    assert "date +%Y" in backup["command"]


def test_palette_keeps_copy_only_metadata_and_never_emits_schedule():
    entries = palette_entries(None)
    entry = next(e for e in entries if e.get("copy_only"))
    palette = CommandPalette([entry])
    emitted = []
    palette.command_chosen.connect(emitted.append)
    try:
        assert "Cron template" in palette.preview_text(entry)
        with mock.patch.object(CronTemplateDialog, "exec", return_value=QDialog.DialogCode.Rejected):
            palette._accept_current()
        assert emitted == []
    finally:
        palette.deleteLater()


def test_library_opens_template_without_executing_it():
    class Vault:
        snippets = []
    panel = SnippetsPanel(Vault())
    emitted = []
    panel.execute_requested.connect(emitted.append)
    try:
        panel.bash_category.setCurrentText("Cron jobs")
        item = panel.bash_list.item(1)
        assert item.data(Qt.ItemDataRole.UserRole)["copy_only"]
        with mock.patch.object(CronTemplateDialog, "exec", return_value=QDialog.DialogCode.Rejected):
            panel._on_bash_item_clicked(item)
        assert emitted == []
    finally:
        panel.deleteLater()


def test_copy_uses_the_edited_entry_and_open_crontab_is_an_explicit_action():
    entry = next(e for e in get_cron_command_library() if e.get("copy_only") and e.get("requires_root"))
    called = []
    dialog = CronTemplateDialog(entry, open_crontab=called.append)
    try:
        dialog.editor.setPlainText("0 2 * * * /srv/backup.sh")
        dialog.copy_entry()
        assert QApplication.clipboard().text() == "0 2 * * * /srv/backup.sh\n"
        assert called == []
        dialog.btn_open.click()
        assert called == ["sudo crontab -e"]
    finally:
        dialog.deleteLater()
