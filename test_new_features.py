"""Recently Deleted, shared credentials, backups, session history, importers,
Go to, Settings, the Overview card, and the window's button colours."""

import base64
import csv
import hashlib
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import app.vault as vault_mod

from PyQt6.QtWidgets import QApplication, QPushButton

from app.vault import Vault, TRASH_DAYS

_APP = QApplication.instance() or QApplication([])

PASSWORD = "correct-horse-battery"


class _VaultCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="rpmote_features_")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        vault_mod.use_vault_directory(self.root)
        self.vault = Vault()
        self.vault.setup(PASSWORD)

    def add(self, name, **extra):
        data = {"name": name, "host": f"{name.lower()}.example", "protocol": "ssh",
                "tenant": "Contoso", "username": "admin", "password": "pw"}
        data.update(extra)
        return self.vault.add_connection(data)


class RecentlyDeletedTests(_VaultCase):
    def test_a_deleted_connection_can_be_restored_with_its_id(self):
        cid = self.add("DC01")
        self.vault.delete_connection(cid)
        self.assertEqual(self.vault.connections, [])
        self.assertEqual([e["connection"]["id"] for e in self.vault.trash], [cid])
        self.assertEqual(self.vault.restore_from_trash([cid]), 1)
        self.assertEqual([c["id"] for c in self.vault.connections], [cid])
        self.assertEqual(self.vault.trash, [])

    def test_permanent_delete_skips_the_bin(self):
        cid = self.add("DC01")
        self.vault.delete_connection(cid, permanent=True)
        self.assertEqual(self.vault.trash, [])

    def test_deleting_a_tenant_keeps_every_connection_in_the_bin(self):
        self.add("A")
        self.add("B")
        self.add("C", tenant="Fabrikam")
        self.assertEqual(self.vault.delete_connections_by_tenant("Contoso"), 2)
        self.assertEqual(len(self.vault.trash), 2)
        self.assertEqual([c["name"] for c in self.vault.connections], ["C"])

    def test_old_deletions_are_forgotten(self):
        cid = self.add("DC01")
        self.vault.delete_connection(cid)
        self.vault._data["trash"][0]["deleted_at"] = time.time() - (TRASH_DAYS + 1) * 86400
        self.vault._prune_trash()
        self.assertEqual(self.vault.trash, [])

    def test_the_bin_survives_a_lock(self):
        cid = self.add("DC01")
        self.vault.delete_connection(cid)
        again = Vault()
        self.assertTrue(again.unlock(PASSWORD))
        self.assertEqual(len(again.trash), 1)

    def test_purge(self):
        a, b = self.add("A"), self.add("B")
        self.vault.delete_connections([a, b])
        self.assertEqual(self.vault.purge_trash([a]), 1)
        self.assertEqual(self.vault.purge_trash(), 1)
        self.assertEqual(self.vault.trash, [])


class SharedCredentialTests(_VaultCase):
    def test_a_connection_signs_in_with_the_shared_login(self):
        cred = self.vault.add_credential({"name": "Contoso admin", "username": "CONTOSO\\admin",
                                          "password": "one"})
        cid = self.add("DC01", username="", password="", credential_id=cred)
        conn = next(c for c in self.vault.connections if c["id"] == cid)
        resolved = self.vault.resolve_credentials(conn)
        self.assertEqual(resolved["username"], "CONTOSO\\admin")
        self.assertEqual(resolved["password"], "one")
        self.assertEqual(resolved["credential_name"], "Contoso admin")
        self.assertEqual(conn["password"], "")          # the record itself is untouched

    def test_rotating_the_password_reaches_every_connection(self):
        cred = self.vault.add_credential({"name": "x", "username": "u", "password": "old"})
        ids = [self.add(n, password="", credential_id=cred) for n in ("A", "B")]
        self.vault.update_credential(cred, {"password": "new"})
        for conn in self.vault.connections:
            self.assertIn(conn["id"], ids)
            self.assertEqual(self.vault.resolve_credentials(conn)["password"], "new")

    def test_age_moves_only_when_the_secret_changes(self):
        cred = self.vault.add_credential({"name": "x", "username": "u", "password": "pw"})
        stamp = self.vault.get_credential(cred)["updated_at"]
        time.sleep(0.01)
        self.vault.update_credential(cred, {"name": "renamed"})
        self.assertEqual(self.vault.get_credential(cred)["updated_at"], stamp)
        self.vault.update_credential(cred, {"password": "pw2"})
        self.assertGreater(self.vault.get_credential(cred)["updated_at"], stamp)

    def test_deleting_a_credential_leaves_connections_working(self):
        cred = self.vault.add_credential({"name": "x", "username": "u", "password": "pw"})
        cid = self.add("A", username="", password="", credential_id=cred)
        self.assertEqual(self.vault.delete_credential(cred), 1)
        conn = next(c for c in self.vault.connections if c["id"] == cid)
        self.assertNotIn("credential_id", conn)
        self.assertEqual((conn["username"], conn["password"]), ("u", "pw"))


class BackupTests(_VaultCase):
    def test_backup_and_restore(self):
        from app import backups
        self.add("Before")
        first = backups.create_backup("test")
        self.assertIsNotNone(first)
        self.add("After")
        backups.restore_backup(first)
        restored = Vault()
        self.assertTrue(restored.unlock(PASSWORD))
        self.assertEqual([c["name"] for c in restored.connections], ["Before"])
        # The restore backed up what it replaced, so it can be undone too.
        reasons = [b.reason for b in backups.list_backups()]
        self.assertIn("before restore", reasons)

    def test_only_the_newest_are_kept(self):
        from app import backups
        for _ in range(backups.KEEP + 3):
            backups.create_backup("bulk")
        self.assertEqual(len(backups.list_backups()), backups.KEEP)

    def test_one_daily_backup_a_day(self):
        from app import backups
        self.assertIsNotNone(backups.daily_backup_if_due())
        self.assertIsNone(backups.daily_backup_if_due())


class SessionHistoryTests(_VaultCase):
    def test_sessions_are_timed_and_totalled_per_tenant(self):
        from app import session_history
        history = session_history.SessionHistory(self.vault)
        clock = [1000.0]
        original = session_history._now
        session_history._now = lambda: clock[0]
        self.addCleanup(setattr, session_history, "_now", original)
        a = history.start({"id": "a", "name": "DC01", "tenant": "Contoso", "protocol": "rdp"})
        clock[0] += 600
        history.end(a)
        b = history.start({"id": "b", "name": "fw", "tenant": "Fabrikam", "protocol": "ssh"})
        clock[0] += 120
        history.end(b)
        entries = history.entries()
        self.assertEqual(len(entries), 2)
        totals = history.totals_by_tenant(entries)
        self.assertEqual(round(totals["Contoso"]), 600)
        self.assertEqual(round(totals["Fabrikam"]), 120)
        self.assertEqual(history.recent_ids()[:2], ["b", "a"])

        out = self.root / "time.csv"
        history.export_csv(str(out), entries)
        rows = list(csv.reader(out.open(encoding="utf-8-sig")))
        self.assertGreaterEqual(len(rows), 3)
        self.assertIn("Contoso", sum(rows, []))


class ImporterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="rpmote_import_")
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_rdm_shaped_csv(self):
        from app import importers
        path = self.dir / "rdm.csv"
        path.write_text(
            "Name,Host,ConnectionType,Group,Username,Password\n"
            "DC01,10.0.0.10,RDPConfiguredConnection,Contoso\\Servers,CONTOSO\\admin,pw1\n"
            "fw,10.0.0.1:2222,SSH Shell,Contoso,root,\n"
            "portal,https://portal.contoso.com,WebBrowser,Contoso,,\n", encoding="utf-8")
        records = {r["name"]: r for r in importers.parse_csv(path)}
        self.assertEqual(records["DC01"]["protocol"], "rdp")
        self.assertEqual(records["fw"]["protocol"], "ssh")
        self.assertEqual((records["fw"]["host"], records["fw"]["port"]), ("10.0.0.1", 2222))
        self.assertEqual(records["portal"]["protocol"], "web")
        self.assertTrue(records["DC01"]["tenant"].startswith("Contoso"))

    def test_rdp_file_written_by_mstsc(self):
        from app import importers
        path = self.dir / "SQL01.rdp"
        path.write_text("full address:s:sql01.contoso.local:3390\r\nusername:s:admin\r\n"
                        "domain:s:CONTOSO\r\ngatewayhostname:s:gw.contoso.com\r\n",
                        encoding="utf-16")
        record = importers.parse_rdp_file(path)
        self.assertEqual((record["host"], record["port"]), ("sql01.contoso.local", 3390))
        self.assertIn("admin", record["username"])
        self.assertEqual(record["name"], "SQL01")

    def _gcm(self, text, password="mR3m", iterations=1000):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        salt, nonce = os.urandom(16), os.urandom(16)
        key = PBKDF2HMAC(algorithm=hashes.SHA1(), length=32, salt=salt,
                         iterations=iterations).derive(password.encode())
        return base64.b64encode(salt + nonce + AESGCM(key).encrypt(nonce, text.encode(), salt)).decode()

    def test_mremoteng_with_its_default_encryption(self):
        from app import importers
        xml = (f'<?xml version="1.0" encoding="utf-8"?>'
               f'<mrng:Connections xmlns:mrng="http://mremoteng.org" Name="Connections" '
               f'EncryptionEngine="AES" BlockCipherMode="GCM" KdfIterations="1000" '
               f'Protected="{self._gcm("ThisIsNotProtected")}" ConfVersion="2.6">'
               f'<Node Name="Fabrikam" Type="Container">'
               f'<Node Name="FAB-DC01" Type="Connection" Protocol="RDP" Hostname="10.1.0.10" '
               f'Port="3389" Username="admin" Domain="FABRIKAM" Password="{self._gcm("s3cret!")}"/>'
               f'<Node Name="FAB-web" Type="Connection" Protocol="SSH2" Hostname="10.1.0.20" Port="22"/>'
               f'</Node></mrng:Connections>')
        path = self.dir / "confCons.xml"
        path.write_text(xml, encoding="utf-8")
        self.assertFalse(importers.mremoteng_needs_password(path))
        records = {r["name"]: r for r in importers.parse_mremoteng(path)}
        self.assertEqual(records["FAB-DC01"]["password"], "s3cret!")
        self.assertEqual(records["FAB-DC01"]["tenant"], "Fabrikam")
        self.assertEqual(records["FAB-web"]["protocol"], "ssh")

    def test_mremoteng_with_a_master_password(self):
        from app import importers
        xml = (f'<Connections Name="Connections" EncryptionEngine="AES" BlockCipherMode="GCM" '
               f'KdfIterations="1000" Protected="{self._gcm("ThisIsProtected", "hunter2")}">'
               f'<Node Name="X" Type="Connection" Protocol="RDP" Hostname="h" '
               f'Password="{self._gcm("pw", "hunter2")}"/></Connections>')
        path = self.dir / "protected.xml"
        path.write_text(xml, encoding="utf-8")
        self.assertTrue(importers.mremoteng_needs_password(path))
        self.assertEqual(importers.parse_mremoteng(path, "hunter2")[0]["password"], "pw")

    def test_legacy_cbc_mremoteng(self):
        from app import importers
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        def cbc(text, password="mR3m"):
            key = hashlib.md5(password.encode()).digest()
            iv = os.urandom(16)
            padder = padding.PKCS7(128).padder()
            data = padder.update(text.encode()) + padder.finalize()
            enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
            return base64.b64encode(iv + enc.update(data) + enc.finalize()).decode()
        xml = (f'<Connections Name="Connections" Protected="{cbc("ThisIsNotProtected")}" '
               f'ConfVersion="2.5"><Node Name="Old" Type="Container"><Node Name="OLD-SRV" '
               f'Type="Connection" Protocol="RDP" Hostname="old.local" Username="u" '
               f'Password="{cbc("legacy-pw")}"/></Node></Connections>')
        path = self.dir / "legacy.xml"
        path.write_text(xml, encoding="utf-8")
        self.assertEqual(importers.parse_mremoteng(path)[0]["password"], "legacy-pw")


class GotoPaletteTests(unittest.TestCase):
    def palette(self, **kwargs):
        from app.goto_palette import GotoPalette
        conns = [
            {"id": "1", "name": "DC01", "host": "10.0.0.10", "tenant": "Contoso", "protocol": "rdp"},
            {"id": "2", "name": "web01", "host": "10.0.0.20", "tenant": "Contoso", "protocol": "ssh"},
            {"id": "3", "name": "FAB-DC01", "host": "10.1.0.10", "tenant": "Fabrikam", "protocol": "rdp"},
        ]
        palette = GotoPalette(conns, **kwargs)
        self.addCleanup(palette.deleteLater)
        return palette

    def texts(self, palette):
        from app.goto_palette import _ROLE
        rows = (palette.list.item(i).data(_ROLE) or {} for i in range(palette.list.count()))
        return [row.get("label", "") for row in rows]

    def test_typing_narrows_to_matching_connections(self):
        palette = self.palette()
        palette.input.setText("fab dc")
        shown = " | ".join(self.texts(palette))
        self.assertIn("FAB-DC01", shown)
        self.assertNotIn("web01", shown)

    def test_a_tenant_and_a_machine_can_be_typed_together(self):
        palette = self.palette()
        palette.input.setText("contoso web")
        labels = self.texts(palette)
        self.assertIn("web01", labels)
        self.assertNotIn("FAB-DC01", labels)

    def test_enter_opens_the_top_result(self):
        palette = self.palette()
        opened = []
        palette.open_requested.connect(lambda conn, new: opened.append((conn["id"], new)))
        palette.input.setText("web01")
        palette._activate()
        self.assertEqual(opened, [("2", False)])


class _WindowCase(_VaultCase):
    def window(self):
        from app.main_window import MainWindow
        self.add("DC01", protocol="rdp")
        win = MainWindow(self.vault)
        self.addCleanup(win.deleteLater)
        return win


class SettingsTests(_WindowCase):
    def test_settings_is_in_the_file_menu_and_factory_reset_is_not(self):
        win = self.window()
        file_menu = next(a.menu() for a in win.menuBar().actions()
                         if a.menu() and a.menu().title() == "&File")
        labels = [a.text().replace("&", "") for a in file_menu.actions()]
        self.assertTrue(any(label.startswith("Settings") for label in labels), labels)
        self.assertFalse(any("Factory Reset" in label for label in labels), labels)
        settings = next(a for a in file_menu.actions() if a.text().replace("&", "").startswith("Settings"))
        self.assertEqual(settings.shortcut().toString(), "Ctrl+,")

    def test_troubleshooting_lives_under_help(self):
        win = self.window()
        help_menu = next(a.menu() for a in win.menuBar().actions()
                         if a.menu() and a.menu().title() == "&Help")
        trouble = next(a.menu() for a in help_menu.actions() if a.menu())
        labels = " | ".join(a.text().replace("&", "") for a in trouble.actions())
        self.assertIn("ActiveX", labels)
        self.assertIn("Repair", labels)
        self.assertIn("Log Folder", labels)

    def test_pages_apply_as_they_change(self):
        from app.settings_dialog import SettingsDialog, PAGES
        from app.idle_lock import IDLE_LOCK_SETTING
        from app import host_keys
        win = self.window()
        host_keys.bind_vault(self.vault)
        dialog = SettingsDialog(win)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.stack.count(), len(PAGES))
        dialog.show_page("security")
        self.assertEqual(dialog.stack.currentIndex(), 3)

        dialog.auto_lock.setValue(7)
        self.assertEqual(self.vault.settings[IDLE_LOCK_SETTING], 7)

        dialog.density.setCurrentIndex(dialog.density.findData("compact"))
        self.assertEqual(win.side_panel.density, "compact")
        self.assertEqual(self.vault.settings.get("sidebar_density"), "compact")

        index = dialog.host_key_policy.findData(host_keys.POLICY_STRICT)
        dialog.host_key_policy.setCurrentIndex(index)
        self.assertEqual(host_keys.store().policy, host_keys.POLICY_STRICT)

        dialog.rdp_tabs.setChecked(not dialog.rdp_tabs.isChecked())
        self.assertEqual(win.act_rdp_embedded.isChecked(), dialog.rdp_tabs.isChecked())


class OverviewCardTests(_WindowCase):
    def test_no_welcome_flash_and_a_card_tall_enough_for_its_rows(self):
        win = self.window()
        panel = win.home_panel
        # Built straight from the first data, not after a debounce.
        self.assertFalse(panel._welcome)
        self.assertEqual(panel.title.text(), "Overview")
        win.resize(1200, 800)
        win.show()
        self.addCleanup(win.hide)
        for _ in range(5):
            _APP.processEvents()
        self.assertGreater(panel.height(), 150)

    def test_first_run_gets_the_welcome(self):
        from app.home_panel import HomePanel
        panel = HomePanel()
        self.addCleanup(panel.deleteLater)
        panel.set_data(connections=[], statuses={}, recent_ids=[])
        self.assertTrue(panel._welcome)


class ButtonColourTests(_WindowCase):
    def test_primary_buttons_inside_the_window_keep_their_fill(self):
        """The content area is transparent for the background, but a primary
        button inside it must still be filled with the accent."""
        win = self.window()
        from app.widgets import set_variant
        button = QPushButton("Go", win.main_content)
        set_variant(button, "primary")
        button.resize(120, 34)
        win.resize(900, 600)
        win.show()
        _APP.processEvents()
        image = button.grab().toImage()
        colour = image.pixelColor(10, image.height() // 2)
        accent = win._current_theme["accent"]
        from PyQt6.QtGui import QColor
        expected = QColor(accent)
        self.assertLess(abs(colour.red() - expected.red()) + abs(colour.green() - expected.green())
                        + abs(colour.blue() - expected.blue()), 40,
                        (colour.name(), accent))
        win.hide()


class IconSweepTests(unittest.TestCase):
    def test_glyphs_become_icons(self):
        from PyQt6.QtGui import QAction
        from app.widgets import iconize, split_glyph
        self.assertEqual(split_glyph("↻ Reconnect"), ("refresh", "Reconnect"))
        self.assertEqual(split_glyph("🗑️ Delete 3 items"), ("trash", "Delete 3 items"))
        self.assertEqual(split_glyph("＋📁"), ("folder-plus", ""))
        self.assertEqual(split_glyph("＋ Folder"), ("folder-plus", "Folder"))
        self.assertEqual(split_glyph("Plain"), (None, "Plain"))
        button = iconize(QPushButton("✕ Disconnect"))
        self.assertEqual(button.text(), "Disconnect")
        self.assertFalse(button.icon().isNull())
        action = iconize(QAction("🔄 Refresh"))
        self.assertEqual(action.text(), "Refresh")

    def test_file_type_icons(self):
        from app.icons import file_icon_name
        self.assertEqual(file_icon_name("deploy.sh"), "code")
        self.assertEqual(file_icon_name("id_ed25519.pub"), "key")
        self.assertEqual(file_icon_name("backup.tar.gz"), "archive")
        self.assertEqual(file_icon_name("README"), "file")
        self.assertEqual(file_icon_name(".bashrc"), "file")

    def test_file_browsers_list_this_pc_before_connecting(self):
        from app.sftp_browser import SFTPBrowser
        from app.ftp_browser import FTPBrowser
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "notes.txt").write_text("x", encoding="utf-8")
            Path(folder, "sub").mkdir()
            for cls in (SFTPBrowser, FTPBrowser):
                browser = cls({"name": "x", "host": "127.0.0.1", "port": 1, "username": "u"})
                self.addCleanup(browser.deleteLater)
                browser._local_cwd = folder
                _APP.processEvents()
                names = [browser.local_tree.topLevelItem(i).text(0)
                         for i in range(browser.local_tree.topLevelItemCount())]
                self.assertIn("notes.txt", names)        # no emoji in the name
                self.assertIn("sub", names)
                row = browser.local_tree.topLevelItem(names.index("sub"))
                self.assertFalse(row.icon(0).isNull())


class DashboardIdleTests(unittest.TestCase):
    def test_the_graph_stops_animating_behind_another_tab(self):
        from PyQt6.QtWidgets import QTabWidget, QWidget
        from app.dashboard import DashboardWidget, IDLE_POLL_MS
        tabs = QTabWidget()
        self.addCleanup(tabs.deleteLater)
        dash = DashboardWidget([], {})
        tabs.addTab(dash, "Dashboard")
        tabs.addTab(QWidget(), "Terminal")
        tabs.resize(640, 480)
        tabs.show()
        self.addCleanup(tabs.hide)
        dash._tick()
        self.assertEqual(dash._anim_timer.interval(), dash.frame_interval_ms)
        tabs.setCurrentIndex(1)
        before = dash._time
        dash._tick()
        self.assertEqual(dash._anim_timer.interval(), IDLE_POLL_MS)
        self.assertEqual(dash._time, before)          # no animation work done
        tabs.setCurrentIndex(0)
        _APP.processEvents()
        self.assertEqual(dash._anim_timer.interval(), dash.frame_interval_ms)


if __name__ == "__main__":
    unittest.main()
