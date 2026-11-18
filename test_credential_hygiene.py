"""Nothing RPMote puts outside the vault may outlive its use.

Covers the fixes from the credential audit: Credential Manager entries are
session-scoped, released when their mstsc exits, shared between launches to
the same host, and swept up by comment on start-up and exit; the master
password has a real floor; the vault body is written atomically; secrets go
to the clipboard tagged for exclusion from history and are cleared again;
the NAS autofill asks before typing a password into a page on some other
host; and the idle lock covers the window -- and only the window -- and opens only for the master
password.
"""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtWidgets import QApplication, QDialog

QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
_APP = QApplication.instance() or QApplication(sys.argv[:1] or ["rpmote-tests"])

import app.vault as vault_mod
from app import idle_lock, secure_clipboard, windows_credentials
from app.rdp_launcher import (
    RDPCredentialState, _CRED_USERS, prepare_rdp_credentials, release_when_exited,
)
from app.vault import MIN_MASTER_PASSWORD_LEN, Vault, VaultError
from app.windows_credentials import (
    CREDENTIAL_COMMENT, CRED_PERSIST_SESSION, CRED_TYPE_DOMAIN_PASSWORD,
    CRED_TYPE_GENERIC, MemoryCredentialStore, purge_saved_credentials,
    release_credentials, store_credentials,
)

PASSWORD = "correct-horse-battery"


class _MemoryCredentials(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.store = MemoryCredentialStore()
        previous = windows_credentials.use_credential_store(self.store)
        self.addCleanup(windows_credentials.use_credential_store, previous)
        _CRED_USERS.clear()
        self.addCleanup(_CRED_USERS.clear)


class _TempVault(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._tmp = Path(tempfile.mkdtemp(prefix="rpmote-hygiene-"))
        self._orig_dir = vault_mod.VAULT_DIR
        vault_mod.use_vault_directory(self._tmp)

    def tearDown(self):
        vault_mod.use_vault_directory(self._orig_dir)
        for f in self._tmp.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            self._tmp.rmdir()
        except OSError:
            pass
        super().tearDown()


# ── Credential Manager ────────────────────────────────────────────────


class CredentialPersistenceTests(unittest.TestCase):
    def test_entries_are_session_scoped_not_persisted(self):
        # The constant the real store writes; a persisted entry would survive
        # logoff and be readable without the vault indefinitely.
        self.assertEqual(CRED_PERSIST_SESSION, 1)
        import inspect
        source = inspect.getsource(windows_credentials.WindowsCredentialStore.write)
        self.assertIn("cred.Persist = CRED_PERSIST_SESSION", source)
        self.assertNotIn("cred.Persist = CRED_PERSIST_LOCAL_MACHINE", source)


class ReleaseTests(_MemoryCredentials):
    def test_release_removes_what_a_report_wrote(self):
        report = store_credentials(["TERMSRV/box", "TERMSRV/box:3390"], "u", "p")
        self.assertEqual(sorted(self.store.targets), ["TERMSRV/box", "TERMSRV/box:3390"])
        self.assertEqual(release_credentials(report), 2)
        self.assertEqual(self.store.targets, [])

    def test_release_skips_writes_that_failed(self):
        self.store.fail_targets = {"termsrv/nope"}
        report = store_credentials(["TERMSRV/nope", "TERMSRV/ok"], "u", "p")
        self.assertEqual(release_credentials(report), 1)
        self.assertEqual(self.store.targets, [])


class PurgeTests(_MemoryCredentials):
    def test_purge_removes_only_rpmote_entries(self):
        store_credentials(["TERMSRV/ours"], "u", "p")
        # Something mstsc or the user saved themselves.
        self.store.saved["TERMSRV/theirs"] = ("u", "p")
        self.store.kinds["TERMSRV/theirs"] = CRED_TYPE_GENERIC
        original_enumerate = self.store.enumerate
        self.store.enumerate = lambda: [
            (t, k, CREDENTIAL_COMMENT if t == "TERMSRV/ours" else "Saved by mstsc")
            for t, k, _ in original_enumerate()]
        self.assertEqual(purge_saved_credentials(), 1)
        self.assertEqual(self.store.targets, ["TERMSRV/theirs"])

    def test_purge_on_an_empty_store_is_quiet(self):
        self.assertEqual(purge_saved_credentials(), 0)


class LaunchLifecycleTests(_MemoryCredentials):
    CONN = {"host": "desk.example", "port": 3389, "username": "DOM\\tech",
            "password": "hunter2-secret"}

    def test_a_launch_holds_and_release_lets_go(self):
        state = prepare_rdp_credentials(dict(self.CONN))
        self.assertTrue(state.desktop_ready)
        self.assertEqual(self.store.targets, ["TERMSRV/desk.example"])
        state.release()
        self.assertEqual(self.store.targets, [])
        self.assertEqual(_CRED_USERS, {})

    def test_release_twice_is_harmless(self):
        state = prepare_rdp_credentials(dict(self.CONN))
        state.release()
        state.release()
        self.assertEqual(self.store.targets, [])

    def test_two_sessions_to_one_host_keep_the_login_until_the_last_closes(self):
        first = prepare_rdp_credentials(dict(self.CONN))
        second = prepare_rdp_credentials(dict(self.CONN))
        first.release()
        self.assertEqual(self.store.targets, ["TERMSRV/desk.example"],
                         "the other session may still need to reconnect")
        second.release()
        self.assertEqual(self.store.targets, [])

    def test_gateway_login_is_released_with_the_desktop(self):
        conn = dict(self.CONN, rdp_gateway="gw.example",
                    rdp_gateway_username="DOM\\tech", rdp_gateway_password="gw-pw")
        state = prepare_rdp_credentials(conn)
        self.assertTrue(state.gateway_ready)
        self.assertIn("gw.example", self.store.targets)
        self.assertEqual(self.store.kinds["gw.example"], CRED_TYPE_DOMAIN_PASSWORD)
        state.release()
        self.assertEqual(self.store.targets, [])

    def test_a_detached_window_releases_when_its_process_ends(self):
        state = prepare_rdp_credentials(dict(self.CONN))
        exited = threading.Event()
        released = threading.Event()

        class _Proc:
            def wait(self_inner):
                exited.wait(5)

        original = state.release

        def _release_and_flag():
            original()
            released.set()
        state.release = _release_and_flag

        release_when_exited(_Proc(), state)
        self.assertEqual(self.store.targets, ["TERMSRV/desk.example"],
                         "still needed while mstsc runs")
        exited.set()
        self.assertTrue(released.wait(5))
        self.assertEqual(self.store.targets, [])

    def test_a_launch_with_nothing_stored_releases_nothing(self):
        state = RDPCredentialState()
        state.release()      # no writes, no error
        self.assertEqual(self.store.targets, [])


# ── Vault ─────────────────────────────────────────────────────────────


class PasswordFloorTests(_TempVault):
    def test_the_floor_is_twelve(self):
        self.assertEqual(MIN_MASTER_PASSWORD_LEN, 12)

    def test_a_short_master_password_is_refused_at_setup(self):
        with self.assertRaises(VaultError) as ctx:
            Vault().setup("hunter22")
        self.assertIn("12", str(ctx.exception))
        self.assertFalse(Vault().is_setup)

    def test_a_short_new_password_is_refused_on_change(self):
        v = Vault()
        v.setup(PASSWORD)
        with self.assertRaises(VaultError):
            v.change_password(PASSWORD, "short-pw")
        self.assertTrue(Vault().unlock(PASSWORD), "the old password still works")

    def test_an_existing_short_password_still_opens_its_vault(self):
        # A vault made under the old six-character rule must not lock its
        # owner out; the floor applies when a password is *set*.
        v = Vault()
        v.setup(PASSWORD)
        v._write_password_material("abcdef")
        self.assertTrue(Vault().unlock("abcdef"))

    def test_export_gets_the_same_floor(self):
        v = Vault()
        v.setup(PASSWORD)
        target = self._tmp / "out.rpvault"
        with self.assertRaises(VaultError):
            v.export_connections(str(target), "a")
        self.assertFalse(target.exists())
        v.export_connections(str(target), "export-password-1")
        self.assertTrue(target.exists())


class AtomicSaveTests(_TempVault):
    def test_a_save_leaves_no_temp_file_and_a_readable_vault(self):
        v = Vault()
        v.setup(PASSWORD)
        v.add_connection({"name": "a", "protocol": "ssh", "host": "h"})
        self.assertFalse((self._tmp / "vault.enc.tmp").exists())
        again = Vault()
        self.assertTrue(again.unlock(PASSWORD))
        self.assertEqual(again.connections[0]["name"], "a")

    def test_a_failed_write_keeps_the_previous_vault(self):
        v = Vault()
        v.setup(PASSWORD)
        v.add_connection({"name": "kept", "protocol": "ssh", "host": "h"})
        before = vault_mod.VAULT_FILE.read_bytes()

        original = os.replace

        def _boom(src, dst):
            raise OSError("disk full")
        os.replace = _boom
        try:
            with self.assertRaises(OSError):
                v.add_connection({"name": "lost", "protocol": "ssh", "host": "h"})
        finally:
            os.replace = original
        self.assertEqual(vault_mod.VAULT_FILE.read_bytes(), before)


# ── Clipboard ─────────────────────────────────────────────────────────


class SecureClipboardTests(unittest.TestCase):
    def test_the_secret_is_copied_with_the_exclusion_formats(self):
        secure_clipboard.copy_secret("s3cret", clear_after_ms=0)
        mime = QApplication.clipboard().mimeData()
        self.assertEqual(mime.text(), "s3cret")
        for fmt, _ in secure_clipboard._EXCLUDE_FORMATS:
            self.assertTrue(mime.hasFormat(fmt), fmt)

    def test_clear_only_if_still_ours(self):
        secure_clipboard.copy_secret("s3cret", clear_after_ms=0)
        QApplication.clipboard().setText("something else the user copied")
        self.assertFalse(secure_clipboard.clear_if_still("s3cret"))
        self.assertEqual(QApplication.clipboard().text(), "something else the user copied")
        secure_clipboard.copy_secret("s3cret", clear_after_ms=0)
        self.assertTrue(secure_clipboard.clear_if_still("s3cret"))
        self.assertEqual(QApplication.clipboard().text(), "")


# ── Autofill origin ───────────────────────────────────────────────────


class _FakePage:
    def __init__(self, url):
        self._url = QUrl(url)

    def url(self):
        return self._url


class AutofillOriginTests(unittest.TestCase):
    def _tab(self, saved_host, page_url):
        from app.web_browser import WebBrowserTab
        tab = WebBrowserTab.__new__(WebBrowserTab)
        tab._host = saved_host
        tab._page = _FakePage(page_url) if page_url else None
        return tab

    def test_the_saved_host_matches_its_own_pages(self):
        tab = self._tab("nas.example", "https://nas.example:5001/#/signin")
        self.assertTrue(tab._page_matches_connection())

    def test_case_and_scheme_do_not_matter(self):
        tab = self._tab("NAS.Example", "http://nas.example/")
        self.assertTrue(tab._page_matches_connection())

    def test_another_host_does_not_match(self):
        tab = self._tab("nas.example", "https://help.vendor.example/login")
        self.assertFalse(tab._page_matches_connection())

    def test_a_page_still_loading_is_not_blocked(self):
        tab = self._tab("nas.example", "about:blank")
        self.assertTrue(tab._page_matches_connection())
        tab = self._tab("nas.example", None)
        self.assertTrue(tab._page_matches_connection())

    def _synology_tab(self, saved_host, page_url, port=5001, landing=""):
        from app.web_browser import BROWSER_PRESETS
        tab = self._tab(saved_host, page_url)
        tab._preset = BROWSER_PRESETS["synology"]
        tab._port = port
        tab._landing_host = landing
        return tab

    def test_a_host_saved_with_its_port_matches(self):
        tab = self._synology_tab("nas.example:5001", "https://nas.example:5001/#/signin")
        self.assertTrue(tab._page_matches_connection())

    def test_a_host_saved_as_a_full_url_matches(self):
        tab = self._synology_tab("https://nas.example:5001/", "https://nas.example:5001/#/signin",
                                 port=0)
        self.assertTrue(tab._page_matches_connection())

    def test_a_quickconnect_id_matches_its_relay_and_direct_names(self):
        tab = self._synology_tab("myquickid", "https://myquickid.direct.quickconnect.to:5001/")
        self.assertTrue(tab._page_matches_connection())
        tab = self._synology_tab("myquickid", "https://myquickid.us3.quickconnect.to/")
        self.assertTrue(tab._page_matches_connection())
        tab = self._synology_tab("myquickid", "https://quickconnect.to/myquickid")
        self.assertTrue(tab._page_matches_connection())
        tab = self._synology_tab("myquickid", "https://help.vendor.example/")
        self.assertFalse(tab._page_matches_connection())

    def test_a_quickconnect_name_matches_the_relay_it_bounces_to(self):
        # Saved as the full quickconnect.to name; the first load lands there
        # and the NAS then bounces to a regional relay for the same ID.
        tab = self._synology_tab("jpsys-syn-bckup2.quickconnect.to",
                                 "https://jpsys-syn-bckup2.us6.quickconnect.to/#/signin",
                                 landing="jpsys-syn-bckup2.quickconnect.to")
        self.assertTrue(tab._page_matches_connection())
        tab = self._synology_tab("https://myquickid.quickconnect.to/",
                                 "https://myquickid.direct.quickconnect.to:5001/", port=0)
        self.assertTrue(tab._page_matches_connection())

    def test_a_different_quickconnect_id_does_not_match(self):
        tab = self._synology_tab("myquickid", "https://otherid.us3.quickconnect.to/")
        self.assertFalse(tab._page_matches_connection())
        tab = self._synology_tab("myquickid.quickconnect.to",
                                 "https://otherid.quickconnect.to/")
        self.assertFalse(tab._page_matches_connection())
        tab = self._synology_tab("myquickid", "https://quickconnect.to.evil.example/")
        self.assertFalse(tab._page_matches_connection())

    def test_where_the_first_load_landed_matches(self):
        # The NAS redirected its IP to its DNS name on the way in.
        tab = self._synology_tab("192.168.1.5", "https://nas.lan:5001/#/signin",
                                 landing="nas.lan")
        self.assertTrue(tab._page_matches_connection())
        tab = self._synology_tab("192.168.1.5", "https://help.vendor.example/",
                                 landing="nas.lan")
        self.assertFalse(tab._page_matches_connection())

    def test_only_the_first_successful_load_is_the_landing_host(self):
        tab = self._synology_tab("192.168.1.5", "https://nas.lan:5001/")
        tab._update_history_buttons = lambda: None
        tab._on_page_loaded(False)
        self.assertEqual(tab._landing_host, "")
        tab._on_page_loaded(True)
        self.assertEqual(tab._landing_host, "nas.lan")
        tab._page = _FakePage("https://help.vendor.example/")
        tab._on_page_loaded(True)
        self.assertEqual(tab._landing_host, "nas.lan")


# ── Idle lock ─────────────────────────────────────────────────────────


class _VerifyingVault:
    def __init__(self, password, unlocked=True):
        self._password = password
        self.is_unlocked = unlocked
        self.locked_calls = 0

    def verify_password(self, entered):
        return entered == self._password

    def lock(self):
        self.locked_calls += 1
        self.is_unlocked = False


class IdleLockTests(unittest.TestCase):
    def setUp(self):
        from PyQt6.QtWidgets import QMainWindow
        self.window = QMainWindow()
        # The cover is a child of the window, so it is only visible
        # while the window is.
        self.window.show()
        self.addCleanup(self.window.close)
        self.vault = _VerifyingVault(PASSWORD)

    def test_the_setting_is_clamped(self):
        self.assertEqual(idle_lock.idle_lock_minutes({}), 15)
        self.assertEqual(idle_lock.idle_lock_minutes({"idle_lock_minutes": "9"}), 9)
        self.assertEqual(idle_lock.idle_lock_minutes({"idle_lock_minutes": -3}), 0)
        self.assertEqual(idle_lock.idle_lock_minutes({"idle_lock_minutes": "x"}), 15)
        self.assertEqual(idle_lock.idle_lock_minutes({"idle_lock_minutes": 99999}), 1440)

    def test_zero_minutes_never_arms_the_timer(self):
        lock = idle_lock.IdleLock(self.vault, self.window, minutes=0)
        self.assertFalse(lock._timer.isActive())
        lock.touch()
        self.assertFalse(lock._timer.isActive())

    def test_activity_rearms_the_timer(self):
        lock = idle_lock.IdleLock(self.vault, self.window, minutes=5)
        self.assertTrue(lock._timer.isActive())
        self.assertEqual(lock._timer.interval(), 5 * 60_000)
        lock.set_minutes(0)
        self.assertFalse(lock._timer.isActive())
        lock.set_minutes(2)
        self.assertTrue(lock._timer.isActive())

    def test_locking_covers_and_the_right_password_uncovers(self):
        lock = idle_lock.IdleLock(self.vault, self.window, minutes=5)
        lock.lock_now()
        self.assertTrue(lock.is_locked)
        screen = lock._screen
        self.assertTrue(screen.isVisible())
        self.assertFalse(lock._timer.isActive(), "no countdown while covered")

        screen.password.setText("wrong-password-1")
        screen._try_unlock()
        self.assertTrue(lock.is_locked)
        self.assertIn("not correct", screen.error.text())

        screen.password.setText(PASSWORD)
        screen._try_unlock()
        self.assertFalse(lock.is_locked)
        self.assertTrue(screen.accepted)
        self.assertFalse(screen.isVisible())
        self.assertTrue(lock._timer.isActive(), "countdown resumes once open")

    def test_escape_and_close_do_not_uncover(self):
        from PyQt6.QtTest import QTest
        lock = idle_lock.IdleLock(self.vault, self.window, minutes=5)
        lock.lock_now()
        screen = lock._screen
        QTest.keyClick(screen.password, Qt.Key.Key_Escape)
        self.assertTrue(lock.is_locked)
        self.assertTrue(screen.isVisible())
        screen.close()
        self.assertTrue(screen.isVisible())
        screen.password.setText(PASSWORD)
        screen._try_unlock()

    def test_the_cover_is_the_window_and_no_bigger(self):
        """It is part of the window, so it is full screen exactly when the
        window is, hides with it when minimised, and never floats over
        other programs the way a stay-on-top window would."""
        self.window.resize(900, 600)
        self.window.show()
        lock = idle_lock.IdleLock(self.vault, self.window, minutes=5)
        lock.lock_now()
        screen = lock._screen
        self.assertFalse(screen.isWindow(), "a child, not a top-level window")
        self.assertIs(screen.parentWidget(), self.window)
        self.assertFalse(screen.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertEqual(screen.geometry(), self.window.rect())
        # ...and it follows the window when that changes size.
        self.window.resize(1100, 720)
        _APP.processEvents()
        self.assertEqual(screen.geometry(), self.window.rect())
        self.assertEqual(screen.size().width(), 1100)
        screen.password.setText(PASSWORD)
        screen._try_unlock()

    def test_nothing_behind_the_cover_takes_input_while_locked(self):
        from PyQt6.QtGui import QAction
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QPushButton
        self.window.resize(800, 500)
        button = QPushButton("Danger", self.window)
        button.setGeometry(50, 50, 120, 30)
        clicks = []
        button.clicked.connect(lambda: clicks.append(1))
        fired = []
        action = QAction("Hidden", self.window)
        action.setShortcut("Ctrl+Shift+9")
        action.triggered.connect(lambda: fired.append(1))
        self.window.addAction(action)
        self.window.show()
        _APP.processEvents()

        lock = idle_lock.IdleLock(self.vault, self.window, minutes=5)
        lock.lock_now()
        screen = lock._screen
        _APP.processEvents()

        # A click on where the button is lands on the cover; a click sent
        # straight at the button is swallowed by the lock's filter.
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.assertEqual(clicks, [])
        # A window shortcut the menu bar would normally answer to.
        QTest.keyClick(screen.password, Qt.Key.Key_9,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(fired, [])
        # Typing still reaches the cover's own field.
        QTest.keyClicks(screen.password, "abc")
        self.assertEqual(screen.password.text(), "abc")

        screen.password.setText(PASSWORD)
        screen._try_unlock()
        _APP.processEvents()
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        self.assertEqual(clicks, [1], "input flows again once unlocked")
        QTest.keyClick(button, Qt.Key.Key_9,
                       Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(fired, [1])

    def test_a_prompt_over_the_cover_can_still_be_answered(self):
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QPushButton
        self.window.show()
        lock = idle_lock.IdleLock(self.vault, self.window, minutes=5)
        lock.lock_now()
        # Something that needs an answer -- a host-key prompt, say -- that
        # appears while the screen is locked.
        prompt = QDialog(self.window)
        prompt.setModal(True)
        ok = QPushButton("OK", prompt)
        answered = []
        ok.clicked.connect(lambda: answered.append(1))
        prompt.show()
        _APP.processEvents()
        QTest.mouseClick(ok, Qt.MouseButton.LeftButton)
        self.assertEqual(answered, [1])
        prompt.close()
        self.assertTrue(lock.is_locked)
        lock._screen.password.setText(PASSWORD)
        lock._screen._try_unlock()

    def test_repeated_wrong_guesses_are_slowed(self):
        lock = idle_lock.IdleLock(self.vault, self.window, minutes=5)
        lock.lock_now()
        screen = lock._screen
        for _ in range(idle_lock.LockScreen.MAX_ATTEMPTS_BEFORE_DELAY):
            screen.password.setText("nope-nope-nope")
            screen._try_unlock()
        self.assertFalse(screen.password.isEnabled())
        screen._allow_retry()
        screen.password.setText(PASSWORD)
        screen._try_unlock()
        self.assertFalse(lock.is_locked)

    def test_nothing_to_protect_means_no_cover(self):
        vault = _VerifyingVault(PASSWORD, unlocked=False)
        lock = idle_lock.IdleLock(vault, self.window, minutes=5)
        lock.lock_now()
        self.assertFalse(lock.is_locked)

    def test_locking_twice_keeps_one_cover(self):
        lock = idle_lock.IdleLock(self.vault, self.window, minutes=5)
        lock.lock_now()
        first = lock._screen
        lock.lock_now()
        self.assertIs(lock._screen, first)
        first.password.setText(PASSWORD)
        first._try_unlock()

    def test_quitting_from_the_cover_locks_the_vault(self):
        lock = idle_lock.IdleLock(self.vault, self.window, minutes=5)
        lock.lock_now()
        lock._screen.quit_requested.emit()
        self.assertEqual(self.vault.locked_calls, 1)
        self.assertFalse(lock.is_locked)


if __name__ == "__main__":
    unittest.main()
