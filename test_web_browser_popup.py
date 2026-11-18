"""Regression tests for the way back from a browser popup tab.

A NAS/iDRAC console that opens a window (target=_blank, window.open) gets a
new tab in the main window. That tab used to carry the console's own name and
its back button walked page history — which a fresh popup has none of — so the
user was left on a tab that looked like the console, with no control that led
back to the console.

Now the popup tab knows which tab opened it: it shows a "Back to <name>"
button, is named after its page, and closing it lands on its opener.
"""

import http.server
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import app.vault as vault_mod

_TMP = Path(tempfile.mkdtemp(prefix="rpmote_popup_test_"))
vault_mod.use_vault_directory(_TMP)

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
# WebEngine reads argv[0] to locate QtWebEngineProcess; an empty argv list
# aborts the process the moment a profile is built.
_APP = QApplication.instance() or QApplication(sys.argv[:1] or ["rpmote-tests"])

from app.web_browser import WebBrowserTab, _ensure_webengine

NAS_NAME = "Home NAS"
CHILD_TITLE = "File Station - Home NAS - Synology NAS"

MAIN_PAGE = (
    "<html><head><title>Home NAS - Synology NAS</title></head><body>"
    "<a id='popup' href='/child' target='_blank'>open</a>"
    "</body></html>"
).encode()
CHILD_PAGE = (
    f"<html><head><title>{CHILD_TITLE}</title></head><body>"
    "<a id='deeper' href='/deeper'>deeper</a></body></html>"
).encode()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = MAIN_PAGE if self.path == "/" else CHILD_PAGE
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def pump(predicate, timeout=15.0) -> bool:
    """Spin the Qt event loop until *predicate* holds (queued signals land)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _APP.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    _APP.processEvents()
    return bool(predicate())


@unittest.skipUnless(_ensure_webengine(), "PyQt6-WebEngine is not available")
class PopupTabWayBackTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(_TMP, ignore_errors=True)

    def setUp(self):
        # Re-assert our own directory: another test module imported later in
        # the same process would otherwise still own the module-level globals.
        vault_mod.use_vault_directory(_TMP)
        for f in (vault_mod.VAULT_FILE, vault_mod.SALT_FILE,
                  vault_mod.HASH_FILE, vault_mod.KEY_FILE):
            if f.exists():
                f.unlink()
        from app.vault import Vault
        from app.main_window import MainWindow

        self.vault = Vault()
        self.vault.setup("popup-test-pw")
        self.vault.add_connection(self._conn())
        self.win = MainWindow(self.vault)
        self.win.resize(1400, 850)
        self.win.show()
        self.addCleanup(self._teardown)
        pump(lambda: False, 0.2)

    def _teardown(self):
        for i in range(self.win.tabs.count() - 1, 0, -1):
            widget = self.win.tabs.widget(i)
            if hasattr(widget, "cleanup"):
                try:
                    widget.cleanup()
                except RuntimeError:
                    pass
        self.win.reachability.shutdown()
        self.win.close()
        self.win.deleteLater()
        _APP.processEvents()

    def _conn(self):
        # port 0 puts the tab in URL mode, so host is used verbatim.
        return {
            "name": NAS_NAME,
            "tenant": "Default",
            "protocol": "synology",
            "host": f"http://127.0.0.1:{self.port}/",
            "port": 0,
            "username": "",
            "password": "",
        }

    def _titles(self):
        return [self.win.tabs.tabText(i) for i in range(self.win.tabs.count())]

    def _open_console(self):
        """Open the NAS console tab the way the sidebar does and wait for it."""
        self.win._open_connection(dict(self.vault.connections[0]))
        tab = self.win.tabs.currentWidget()
        self.assertIsInstance(tab, WebBrowserTab)
        loaded = []
        tab._page.loadFinished.connect(lambda ok: loaded.append(ok))
        self.assertTrue(pump(lambda: bool(loaded)), "console page never loaded")
        return tab

    def _open_popup_from_page(self, console):
        """Click the console page's target=_blank link and wait for its tab."""
        before = self.win.tabs.count()
        console._page.runJavaScript("document.getElementById('popup').click();")
        self.assertTrue(pump(lambda: self.win.tabs.count() > before),
                        "the popup never became a tab")
        child = self.win.tabs.currentWidget()
        self.assertIsInstance(child, WebBrowserTab)
        self.assertIsNot(child, console)
        return child

    def _add_popup(self, console):
        """A popup tab wired up exactly as the createWindow path does it,
        without waiting on Chromium — for the tests about tab bookkeeping."""
        conn = dict(console.connection_info)
        conn["host"] = f"http://127.0.0.1:{self.port}/child"
        child = WebBrowserTab(conn, _shared_profile=console._profile,
                              _skip_navigate=True, opener=console)
        self.win._add_browser_child_tab(child)
        return child

    # ── The way back ────────────────────────────────────────────────

    def test_popup_opened_by_the_page_offers_a_way_back_to_the_console(self):
        console = self._open_console()
        child = self._open_popup_from_page(console)

        self.assertIs(child.opener_tab, console)
        self.assertIsNotNone(child.btn_back_to_opener)
        self.assertEqual(child.btn_back_to_opener.text(), f"Back to {NAS_NAME}")
        self.assertIsNone(console.btn_back_to_opener,
                          "the console tab itself was not opened by anything")

        child.btn_back_to_opener.click()
        self.assertIs(self.win.tabs.currentWidget(), console)

    def test_popup_tab_is_named_after_its_page(self):
        console = self._open_console()
        child = self._open_popup_from_page(console)
        idx = self.win.tabs.indexOf(child)

        self.assertTrue(
            pump(lambda: "File Station" in self.win.tabs.tabText(idx)),
            f"popup tab never picked up its page title: {self._titles()}")
        console_text = self.win.tabs.tabText(self.win.tabs.indexOf(console))
        # Tabs carry the protocol as an icon now, not an emoji in the text.
        self.assertEqual(console_text, NAS_NAME)
        self.assertFalse(self.win.tabs.tabIcon(self.win.tabs.indexOf(console)).isNull())
        self.assertTrue(self.win.tabs.tabText(idx).startswith(f"{NAS_NAME} ↗ "))
        self.assertNotEqual(self.win.tabs.tabText(idx), console_text)
        self.assertIn(CHILD_TITLE, self.win.tabs.tabToolTip(idx))
        self.assertIn(f"Back to {NAS_NAME}", self.win.tabs.tabToolTip(idx))

    def test_popup_history_buttons_only_light_up_once_there_is_history(self):
        console = self._open_console()
        self.assertFalse(console.btn_back.isEnabled())
        self.assertFalse(console.btn_forward.isEnabled())

        child = self._open_popup_from_page(console)
        loaded = []
        child._page.loadFinished.connect(lambda ok: loaded.append(ok))
        self.assertTrue(pump(lambda: bool(loaded)))
        self.assertFalse(child.btn_back.isEnabled(),
                         "a fresh popup has no page to go back to")

        child._page.runJavaScript("document.getElementById('deeper').click();")
        self.assertTrue(pump(lambda: child.btn_back.isEnabled()),
                        "◀ stayed dark after navigating inside the popup")

    # ── Closing ─────────────────────────────────────────────────────

    def test_closing_the_popup_lands_on_its_opener_not_its_neighbour(self):
        console = self._open_console()
        first = self._add_popup(console)
        second = self._add_popup(console)
        self.assertIs(self.win.tabs.currentWidget(), second)
        # Tab order is console, first, second: QTabWidget on its own would
        # fall back to `first`, the neighbour.
        self.assertEqual(self.win.tabs.indexOf(first), self.win.tabs.indexOf(second) - 1)

        self.win._close_tab(self.win.tabs.indexOf(second))
        self.assertIs(self.win.tabs.currentWidget(), console)
        self.assertEqual(self.win.tabs.indexOf(second), -1)
        self.assertGreaterEqual(self.win.tabs.indexOf(first), 0)

    def test_disconnecting_the_popup_lands_on_its_opener(self):
        console = self._open_console()
        child = self._add_popup(console)
        self.assertIs(self.win.tabs.currentWidget(), child)

        child._disconnect()          # the toolbar's ✕ Disconnect
        pump(lambda: self.win.tabs.indexOf(child) < 0, 2.0)
        self.assertEqual(self.win.tabs.indexOf(child), -1)
        self.assertIs(self.win.tabs.currentWidget(), console)

    def test_closing_a_popup_in_the_background_does_not_steal_focus(self):
        console = self._open_console()
        child = self._add_popup(console)
        self.win.tabs.setCurrentIndex(0)          # the user is on the dashboard

        self.win._close_tab(self.win.tabs.indexOf(child))
        self.assertEqual(self.win.tabs.currentIndex(), 0)

    def test_closing_the_console_takes_the_way_back_away_from_its_popups(self):
        console = self._open_console()
        child = self._add_popup(console)
        self.assertTrue(child.btn_back_to_opener.isVisibleTo(child))

        self.win._close_tab(self.win.tabs.indexOf(console))
        self.assertIsNone(child.opener_tab)
        self.assertFalse(child.btn_back_to_opener.isVisibleTo(child))
        self.assertGreaterEqual(self.win.tabs.indexOf(child), 0,
                                "the popup itself stays open")

    def test_way_back_when_the_console_vanished_underneath_the_button(self):
        """The window learns the opener is gone the moment the button is
        pressed, even when the close did not go through the tab bar."""
        console = self._open_console()
        child = self._add_popup(console)
        self.win.tabs.removeTab(self.win.tabs.indexOf(console))   # bypass _close_tab

        child.btn_back_to_opener.click()
        self.assertIs(self.win.tabs.currentWidget(), child)
        self.assertIsNone(child.opener_tab)
        self.assertFalse(child.btn_back_to_opener.isVisibleTo(child))

    # ── Labels ──────────────────────────────────────────────────────

    def test_long_console_names_are_shortened_on_the_button(self):
        console = WebBrowserTab({**self._conn(), "name": "A Rather Long Customer NAS Name"},
                                _skip_navigate=True)
        self.addCleanup(console.cleanup)
        child = WebBrowserTab(console.connection_info, _skip_navigate=True, opener=console)
        self.addCleanup(child.cleanup)
        label = child.opener_label()
        self.assertTrue(label.endswith("…"))
        self.assertLessEqual(len(label), WebBrowserTab.OPENER_LABEL_MAX)
        self.assertEqual(child.btn_back_to_opener.text(), f"Back to {label}")

    def test_unnamed_console_falls_back_to_the_protocol_label(self):
        console = WebBrowserTab({**self._conn(), "name": ""}, _skip_navigate=True)
        self.addCleanup(console.cleanup)
        child = WebBrowserTab(console.connection_info, _skip_navigate=True, opener=console)
        self.addCleanup(child.cleanup)
        self.assertEqual(child.btn_back_to_opener.text(), "Back to NAS")


if __name__ == "__main__":
    unittest.main()
