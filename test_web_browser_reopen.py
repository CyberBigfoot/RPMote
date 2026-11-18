"""Regression tests for closing and reopening a NAS/iDRAC browser tab.

The tab used to render a blank white page the second time it was opened:
every tab built its own QWebEngineProfile("nas"), and because closing a tab
only removes it from the QTabWidget (it is never destroyed), the old profile
kept Chromium's lock on the on-disk profile directory. The replacement profile
never acquired that lock and no page created on it ever loaded.
"""

import http.server
import sys
import threading
import unittest

from PyQt6.QtCore import Qt, QThread
from PyQt6.QtWidgets import QApplication, QTabWidget

QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
# WebEngine reads argv[0] to locate QtWebEngineProcess; an empty argv list
# aborts the process the moment a profile is built.
_APP = QApplication.instance() or QApplication(sys.argv[:1] or ["rpmote-tests"])

from app.web_browser import WebBrowserTab, _ensure_webengine, _shared_profile_for

MARKER = "rpmote-nas-marker"
PAGE = f"<html><body><h1>{MARKER}</h1></body></html>".encode()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


# A healthy tab paints a localhost page in well under a second. The broken
# build took 7-13s to struggle through the same load, so this budget catches a
# tab that never paints; test_close_and_reopen_reuses_the_same_profile is the
# deterministic guard on the underlying cause.
LOAD_BUDGET_MS = 10000


def _wait(predicate, timeout_ms=20000):
    """Spin the Qt event loop until predicate() is true or the timeout hits."""
    elapsed = 0
    while not predicate() and elapsed < timeout_ms:
        _APP.processEvents()
        QThread.msleep(25)
        elapsed += 25
    return predicate()


@unittest.skipUnless(_ensure_webengine(), "PyQt6-WebEngine is not available")
class WebBrowserTabReopenTest(unittest.TestCase):
    """Open, close and reopen browser tabs the way the main window does."""

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

    def setUp(self):
        self.tabs = QTabWidget()
        self.tabs.resize(900, 600)
        self.tabs.show()
        self.opened = []
        self.addCleanup(self._teardown)

    def _teardown(self):
        for tab in self.opened:
            try:
                tab.cleanup()
            except RuntimeError:
                pass
        self.tabs.close()
        self.tabs.deleteLater()
        _APP.processEvents()

    def _conn(self):
        # port 0 puts the tab in URL mode, so host is used verbatim.
        return {
            "name": "NAS",
            "protocol": "synology",
            "host": f"http://127.0.0.1:{self.port}/",
            "port": 0,
        }

    def _open_tab(self):
        tab = WebBrowserTab(self._conn())
        self.opened.append(tab)
        self.tabs.addTab(tab, "NAS")
        self.tabs.setCurrentWidget(tab)
        return tab

    def _close_tab(self, tab):
        """Exactly what MainWindow._close_tab does: cleanup, then removeTab."""
        tab.cleanup()
        self.tabs.removeTab(self.tabs.indexOf(tab))
        _APP.processEvents()

    def _assert_rendered(self, tab, label):
        loaded = []
        tab._page.loadFinished.connect(lambda ok: loaded.append(ok))
        self.assertTrue(
            _wait(lambda: bool(loaded), LOAD_BUDGET_MS),
            f"{label}: nothing painted within {LOAD_BUDGET_MS}ms — this is the white tab",
        )
        self.assertTrue(loaded[-1], f"{label}: load reported failure")

        html = []
        tab._page.toHtml(html.append)
        self.assertTrue(_wait(lambda: bool(html), 10000), f"{label}: no HTML came back")
        self.assertIn(MARKER, html[-1], f"{label}: the page rendered empty")

    def test_reopen_after_close_still_renders(self):
        first = self._open_tab()
        self._assert_rendered(first, "first open")

        self._close_tab(first)

        second = self._open_tab()
        self._assert_rendered(second, "reopen after close")

    def test_third_open_still_renders(self):
        for round_number in range(3):
            tab = self._open_tab()
            self._assert_rendered(tab, f"open #{round_number + 1}")
            self._close_tab(tab)

    def test_reconnect_button_reloads(self):
        tab = self._open_tab()
        self._assert_rendered(tab, "initial load")
        tab._reconnect()
        self._assert_rendered(tab, "after reconnect")

    def test_tabs_share_one_profile(self):
        first = self._open_tab()
        second = self._open_tab()
        self.assertIs(first._profile, second._profile)
        self.assertIs(first._profile, _shared_profile_for("nas"))

    def test_close_and_reopen_reuses_the_same_profile(self):
        """The invariant behind the white tab: a second QWebEngineProfile with
        the same storage name never wins the lock on the profile directory, so
        exactly one may ever be built."""
        first = self._open_tab()
        profile = first._profile
        self._close_tab(first)

        second = self._open_tab()
        self.assertIs(
            second._profile, profile,
            "reopening built a second profile for the same storage name",
        )

    def test_cleanup_is_idempotent(self):
        """_disconnect() cleans up and then the close handler cleans up again."""
        tab = self._open_tab()
        self._assert_rendered(tab, "initial load")
        tab.cleanup()
        tab.cleanup()
        self.assertIsNone(tab.web_view)
        self.assertIsNone(tab._page)


if __name__ == "__main__":
    unittest.main()
