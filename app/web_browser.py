"""
RPMote Web Browser Tab — Reusable embedded Chromium browser for web-based
management consoles (iDRAC, NAS DSM, etc.).

Certificate exceptions require approval for the exact server and certificate.
"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit, QLabel,
    QSizePolicy, QApplication, QMessageBox,
)
from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal, PYQT_VERSION_STR
from PyQt6.QtGui import QFont
import json
import sys

from app import icons, totp
from app.secure_clipboard import copy_secret
from app.browser_trust import CertificatePrompts, CertificateTrustStore

# ── Lazy WebEngine imports ──────────────────────────────────────────
_QWebEngineView = None
_QWebEnginePage = None
_QWebEngineProfile = None
_QWebEngineScript = None
_webengine_error: str | None = None


def _ensure_webengine():
    """Lazy-import WebEngine modules. Returns True on success."""
    global _QWebEngineView, _QWebEnginePage, _QWebEngineProfile, _QWebEngineScript, _webengine_error
    if _QWebEngineView is not None:
        return True
    if _webengine_error is not None:
        return False
    try:
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript
        _QWebEngineView = QWebEngineView
        _QWebEnginePage = QWebEnginePage
        _QWebEngineProfile = QWebEngineProfile
        _QWebEngineScript = QWebEngineScript
        return True
    except Exception as exc:
        # ImportError on a missing/mismatched .pyd, but a WebEngine built for a
        # different Qt minor can fail in other ways too — never let it kill the tab.
        _webengine_error = f"{type(exc).__name__}: {exc}"
        return False


# ── Shared persistent profiles ──────────────────────────────────────
# Chromium locks the on-disk directory behind a named QWebEngineProfile. A
# second profile built with the same storage name while the first is still
# alive never gets that lock, and every page created on it stays blank — which
# is what happened when a NAS tab was closed and reopened: removeTab() does not
# destroy the old tab, so its profile kept holding the lock. Build one profile
# per storage name and reuse it; that also keeps cookies and the logged-in
# session shared between tabs of the same protocol.
_profiles: dict = {}


def _shared_profile_for(storage_name: str):
    """Return the one long-lived profile for this storage name."""
    profile = _profiles.get(storage_name)
    if profile is not None:
        try:
            profile.storageName()      # touch it: RuntimeError if C++ side is gone
        except RuntimeError:
            profile = None
    if profile is None:
        # Deliberately unparented — the profile has to outlive every page built
        # on it, and pages are owned by tabs that come and go.
        profile = _QWebEngineProfile(storage_name)
        _profiles[storage_name] = profile
    return profile


def _make_ssl_page(profile, parent_tab):
    """Create a QWebEnginePage with explicit certificate approval and
    handles new-window requests (target=_blank, window.open) by opening a
    brand-new browser tab in the main window."""
    class _SslPage(_QWebEnginePage):
        def __init__(self, profile, parent):
            super().__init__(profile, parent)
            parent_tab._connect_certificate_handler(self)

        def createWindow(self, window_type):
            """Intercept new-window requests: return a lightweight capture
            page so Chromium navigates it, then open a real tab once the
            target URL is known."""
            try:
                # Lightweight page on the SAME profile (shares cookies/session).
                # It is NOT attached to any view — Qt adopts and navigates it.
                class _CapturePage(_QWebEnginePage):
                    _captured = False

                shared_profile = self.profile()
                cap = _CapturePage(shared_profile, parent_tab)
                parent_tab._connect_certificate_handler(cap)

                def _on_url(url):
                    if cap._captured:
                        return
                    url_str = url.toString()
                    if not url_str or url_str == "about:blank":
                        return
                    cap._captured = True
                    try:
                        cap.urlChanged.disconnect(_on_url)
                    except Exception:
                        pass
                    # Build a URL-mode connection (port=0 → host IS the URL)
                    conn = dict(parent_tab.connection_info)
                    conn["host"] = url_str
                    conn["port"] = 0
                    # The child keeps a handle on this tab so it can offer a
                    # way back: a popup has no page history, so its own ◀
                    # button can never lead to the console that opened it.
                    child = WebBrowserTab(conn, _shared_profile=shared_profile,
                                          opener=parent_tab)
                    parent_tab.open_in_new_tab.emit(child)
                    # Cleanup
                    try:
                        parent_tab._capture_pages.remove(cap)
                    except ValueError:
                        pass
                    cap.deleteLater()

                cap.urlChanged.connect(_on_url)
                # prevent garbage collection
                parent_tab._capture_pages.append(cap)
                return cap
            except Exception:
                pass
            return super().createWindow(window_type)

    return _SslPage(profile, parent_tab)


# ── Protocol branding presets ───────────────────────────────────────


def _web_url(host: str, port: int) -> str:
    """Any web console: a pasted URL is used as is; otherwise https, with
    plain http only for port 80."""
    host = str(host or "").strip()
    if host.startswith(("http://", "https://")):
        return host
    if not port or port == 443:
        return f"https://{host}"
    if port == 80:
        return f"http://{host}"
    return f"https://{host}:{port}"


BROWSER_PRESETS = {
    "web": {
        "label":      "Web",
        "color":      "#22d3ee",  # cyan
        "icon":       "globe",
        "default_port": 443,
        "url_builder": _web_url,
    },
    "idrac": {
        "label":      "iDRAC",
        "color":      "#f97316",  # orange
        "icon":       "server",
        "default_port": 443,
        "url_builder": lambda host, port: (
            # Port 0 means host is a full URL — use directly
            (host if host.startswith(("http://", "https://")) else f"https://{host}")
            if not port
            else (f"https://{host}" if port == 443 else f"https://{host}:{port}")
        ),
    },
    "synology": {
        "label":      "NAS",
        "color":      "#4cb5ae",  # teal
        "icon":       "nas",
        "default_port": 5001,
        "url_builder": lambda host, port: (
            # Port 0 means host is a full URL — use directly
            (host if host.startswith(("http://", "https://")) else f"https://{host}")
            if not port
            # QuickConnect IDs have no dots — route through quickconnect.to
            else (
                f"https://quickconnect.to/{host}"
                if "." not in host and ":" not in host
                else (f"https://{host}" if port == 5001 else f"https://{host}:{port}")
            )
        ),
    },
}


class WebBrowserTab(QWidget):
    """Embedded Chromium browser tab for web-based management consoles."""

    session_closed = pyqtSignal()
    open_in_new_tab = pyqtSignal(object)  # emits a new WebBrowserTab widget
    # A popup tab asking the window to show the tab that opened it (emits
    # that opener tab). Only ever emitted by tabs built with an opener.
    return_to_opener = pyqtSignal(object)
    # The page's <title>, so a popup tab can be told apart from the console
    # it came from — both would otherwise carry the same connection name.
    title_changed = pyqtSignal(str)

    # Longest opener name the "Back to …" button will spell out before it is
    # shortened; a long connection name must not crowd the URL bar out.
    OPENER_LABEL_MAX = 20

    def __init__(self, connection: dict, parent=None, _skip_navigate: bool = False,
                 _shared_profile=None, opener=None, certificate_trust=None):
        super().__init__(parent)
        self._certificate_trust = (certificate_trust
                                   or getattr(opener, "_certificate_trust", None)
                                   or CertificateTrustStore())
        self._certificate_prompts = CertificatePrompts(self._certificate_trust, self)
        self.connection_info = dict(connection)
        self._host = connection.get("host", "")
        self._protocol = connection.get("protocol", "idrac").lower()
        self._preset = BROWSER_PRESETS.get(self._protocol, BROWSER_PRESETS["idrac"])
        self._port = int(connection.get("port", self._preset["default_port"]))
        # Host the console's own first load ended up on (after any redirect
        # the NAS or QuickConnect did on the way in); see _saved_login_hosts.
        self._landing_host = ""
        self.web_view = None
        self._page = None
        self._profile = None
        self._capture_pages = []
        self._shared_profile = _shared_profile
        # The WebBrowserTab whose page opened this one (target=_blank,
        # window.open), or None for a tab the user opened themselves.
        self.opener_tab = opener
        self.btn_back_to_opener = None

        if not _ensure_webengine():
            self._setup_error_ui()
            return

        self._setup_ui()
        if not _skip_navigate:
            self._navigate()

    # ── Error Fallback ───────────────────────────────────────────────

    def _connect_certificate_handler(self, page):
        # certificateError is a Qt 6 signal, not a virtual method. Retain a
        # copy because the user answers after this signal has returned.
        from PyQt6.QtWebEngineCore import QWebEngineCertificateError
        prompts = self._certificate_prompts
        page.certificateError.connect(
            lambda error: prompts.handle(QWebEngineCertificateError(error)))

    def _setup_error_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        p = self._preset

        if getattr(sys, "frozen", False):
            # An installed build has no pip and no way for the user to add the
            # package — Qt WebEngine simply was not bundled. Telling them to run
            # pip here sends them chasing a fix that cannot work.
            advice = (
                "<p style='color:#94a3b8;font-size:13px;'>This installed build "
                "was packaged without Qt WebEngine, so there is nothing to "
                "install on this machine.</p>"
                "<p style='color:#94a3b8;font-size:13px;'>It needs a new build "
                "produced from an environment that has "
                f"<b>PyQt6-WebEngine {PYQT_VERSION_STR}</b> installed "
                "(<code style='color:#4ade80;'>python build.py --installer</code> "
                "verifies this before packaging).</p>"
            )
        else:
            # Running from source: the user can genuinely fix this. WebEngine
            # must match the running PyQt6 minor exactly, so quote that version.
            advice = (
                f"<p style='color:#94a3b8;font-size:13px;'>This build runs PyQt6 "
                f"<b>{PYQT_VERSION_STR}</b>; WebEngine must match that version:<br>"
                f"<code style='color:#4ade80;'>pip install "
                f"PyQt6-WebEngine=={PYQT_VERSION_STR}</code></p>"
            )

        err = QLabel(
            f"<h2 style='color:{p['color']};'>{p['label']} Browser Unavailable</h2>"
            "<p style='color:#94a3b8;font-size:13px;'>"
            "PyQt6-WebEngine is required but could not be loaded.</p>"
            + advice
            + f"<p style='color:#94a3b8;font-size:12px;'>Error: <span style='color:#f87171;'>"
            f"{_webengine_error}</span></p>"
        )
        err.setWordWrap(True)
        layout.addWidget(err)
        layout.addStretch()

    # ── UI ───────────────────────────────────────────────────────────

    def _setup_ui(self):
        p = self._preset
        color = p["color"]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toolbar
        toolbar = QWidget()
        toolbar.setFixedHeight(38)
        toolbar.setStyleSheet(
            "background: rgba(30,34,52,0.85); "
            "border-bottom: 1px solid rgba(255,255,255,0.08);"
        )
        tb = QHBoxLayout(toolbar)
        tb.setContentsMargins(8, 2, 8, 2)
        tb.setSpacing(6)

        btn_css = f"""
            QPushButton {{
                background: transparent; color: #cdd6f4;
                border: 1px solid rgba(255,255,255,0.10); border-radius: 6px;
                font-size: 14px; padding: 2px 8px;
            }}
            QPushButton:hover {{
                background: rgba({self._hex_to_rgba(color, 0.18)});
                border-color: {color};
            }}
            QPushButton:disabled {{
                color: rgba(205,214,244,0.25);
                border-color: rgba(255,255,255,0.05);
            }}
        """

        if self.opener_tab is not None:
            # This tab was opened by another console tab. Its own ◀ walks page
            # history, which a fresh popup has none of, so the one thing the
            # user is most likely to want — getting back to the console that
            # opened it — needs a button of its own, and it goes first.
            self.btn_back_to_opener = QPushButton(
                icons.icon("chevron-left", "#d1fae5"), f"Back to {self.opener_label()}")
            self.btn_back_to_opener.setFixedHeight(28)
            self.btn_back_to_opener.setStyleSheet(
                f"QPushButton {{ background: rgba({self._hex_to_rgba(color, 0.16)}); "
                f"color: #d1fae5; border: 1px solid rgba({self._hex_to_rgba(color, 0.40)}); "
                "border-radius: 6px; font-size: 12px; padding: 2px 10px; font-weight: 600; }"
                f"QPushButton:hover {{ background: rgba({self._hex_to_rgba(color, 0.28)}); }}"
            )
            self.btn_back_to_opener.setToolTip(
                "Switch back to the tab this window was opened from. "
                "This tab stays open; close it to return for good.")
            self.btn_back_to_opener.clicked.connect(self._go_back_to_opener)
            tb.addWidget(self.btn_back_to_opener)

        from app import icons as _icons
        for icon_name, tip, slot_name in [("chevron-left", "Back", "back"),
                                          ("chevron-right", "Forward", "forward"),
                                          ("refresh", "Refresh", "reload")]:
            btn = QPushButton()
            btn.setIcon(_icons.icon(icon_name))
            btn.setFixedSize(30, 28)
            btn.setStyleSheet(btn_css)
            btn.setToolTip(tip)
            setattr(self, f"btn_{slot_name}", btn)
            tb.addWidget(btn)

        self.url_bar = QLineEdit()
        self.url_bar.setReadOnly(True)
        self.url_bar.setStyleSheet(
            "background: rgba(255,255,255,0.06); color: #94a3b8; "
            "border: 1px solid rgba(255,255,255,0.08); border-radius: 6px; "
            "padding: 2px 8px; font-size: 12px; font-family: 'Segoe UI';"
        )
        tb.addWidget(self.url_bar, 1)

        badge = QLabel(p["label"])
        badge.setStyleSheet(
            f"color: {color}; font-size: 11px; font-weight: 700; "
            f"padding: 1px 8px; border-radius: 8px; "
            f"background: rgba({self._hex_to_rgba(color, 0.12)}); "
            f"border: 1px solid rgba({self._hex_to_rgba(color, 0.30)});"
        )
        tb.addWidget(badge)

        btn_disc = QPushButton("Disconnect")
        btn_disc.setIcon(_icons.icon("x", "#f87171"))
        btn_disc.setFixedHeight(28)
        btn_disc.setStyleSheet(
            "QPushButton { background: rgba(239,68,68,0.15); color: #f87171; "
            "border: 1px solid rgba(239,68,68,0.30); border-radius: 6px; "
            "font-size: 12px; padding: 2px 10px; font-weight: 600; }"
            "QPushButton:hover { background: rgba(239,68,68,0.30); }"
        )
        btn_disc.clicked.connect(self._disconnect)
        tb.addWidget(btn_disc)

        btn_reconnect = QPushButton("Reconnect")
        btn_reconnect.setIcon(_icons.icon("refresh"))
        btn_reconnect.setFixedHeight(28)
        btn_reconnect.setStyleSheet(btn_css)
        btn_reconnect.clicked.connect(self._reconnect)
        tb.addWidget(btn_reconnect)

        if self._protocol in ("synology", "idrac", "web"):
            self.btn_nas_autofill = QPushButton("Paste Saved Login")
            self.btn_nas_autofill.setFixedHeight(28)
            self.btn_nas_autofill.setStyleSheet(
                f"QPushButton {{ background: rgba({self._hex_to_rgba(color, 0.16)}); color: #d1fae5; "
                f"border: 1px solid rgba({self._hex_to_rgba(color, 0.40)}); border-radius: 6px; "
                "font-size: 12px; padding: 2px 10px; font-weight: 600; }"
                f"QPushButton:hover {{ background: rgba({self._hex_to_rgba(color, 0.28)}); }}"
            )
            self.btn_nas_autofill.setToolTip("Paste username/password saved in this connection")
            self.btn_nas_autofill.clicked.connect(self._manual_paste_saved_login)
            tb.addWidget(self.btn_nas_autofill)

            self.btn_nas_copy = QPushButton("Copy Login")
            self.btn_nas_copy.setFixedHeight(28)
            self.btn_nas_copy.setStyleSheet(btn_css)
            self.btn_nas_copy.setToolTip("Copy saved username/password to clipboard")
            self.btn_nas_copy.clicked.connect(self._copy_saved_login)
            tb.addWidget(self.btn_nas_copy)

            if totp.has_totp(self.connection_info):
                # Shows the live code as its own label so it can be read off
                # and typed by hand when the console's 2FA box is one this
                # cannot find — a rotating code is useless once it is stale.
                self.btn_totp = QPushButton(icons.icon("shield-check", "#d1fae5"), "——— ———")
                self.btn_totp.setFixedHeight(28)
                self.btn_totp.setStyleSheet(
                    f"QPushButton {{ background: rgba({self._hex_to_rgba(color, 0.16)}); "
                    "color: #d1fae5; font-family: 'MesloLGS Nerd Font Mono', monospace; "
                    f"border: 1px solid rgba({self._hex_to_rgba(color, 0.40)}); "
                    "border-radius: 6px; font-size: 12px; padding: 2px 10px; "
                    "font-weight: 700; }"
                    f"QPushButton:hover {{ background: rgba({self._hex_to_rgba(color, 0.28)}); }}"
                    "QPushButton:disabled { color: #f87171; font-weight: 600; }"
                )
                self.btn_totp.setToolTip(
                    "Fill the 2-step verification code on the page and copy it "
                    "to the clipboard")
                self.btn_totp.clicked.connect(self._use_totp_code)
                tb.addWidget(self.btn_totp)

                self._totp_timer = QTimer(self)
                self._totp_timer.setInterval(1000)
                self._totp_timer.timeout.connect(self._refresh_totp_button)
                self._totp_timer.start()
                self._refresh_totp_button()

        layout.addWidget(toolbar)

        # Web view
        if self._shared_profile is not None:
            self._profile = self._shared_profile
        else:
            self._profile = _shared_profile_for(p["label"].lower())
        self.web_view = _QWebEngineView()
        self.web_view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.web_view, 1)
        self._attach_new_page()

        # Signals — routed through _view_action so a click that lands after
        # cleanup() has torn the view down is a no-op rather than a crash.
        self.btn_back.clicked.connect(lambda: self._view_action("back"))
        self.btn_forward.clicked.connect(lambda: self._view_action("forward"))
        self.btn_reload.clicked.connect(lambda: self._view_action("reload"))

    def _view_action(self, name: str):
        if self.web_view is None:
            return
        try:
            getattr(self.web_view, name)()
        except RuntimeError:
            pass

    def _update_history_buttons(self):
        """Grey out ◀ / ▶ when there is nowhere for them to go, so a popup
        tab's dead Back button is not mistaken for the way back to its
        opener — that is what the "Back to …" button is for."""
        if self._page is None:
            return
        try:
            history = self._page.history()
            self.btn_back.setEnabled(history.canGoBack())
            self.btn_forward.setEnabled(history.canGoForward())
        except RuntimeError:
            pass

    # ── Opener (popup) handling ──────────────────────────────────────

    def opener_label(self) -> str:
        """The name of the tab that opened this one, short enough for a button."""
        opener = self.opener_tab
        if opener is None:
            return ""
        info = getattr(opener, "connection_info", None) or {}
        preset = getattr(opener, "_preset", None) or self._preset
        name = str(info.get("name", "") or "").strip() or preset["label"]
        if len(name) > self.OPENER_LABEL_MAX:
            name = name[: self.OPENER_LABEL_MAX - 1].rstrip() + "…"
        return name

    def _go_back_to_opener(self):
        if self.opener_tab is None:
            return
        self.return_to_opener.emit(self.opener_tab)

    def opener_closed(self):
        """The tab that opened this one is gone: there is nothing to go back
        to, so the button must not keep promising there is."""
        self.opener_tab = None
        if self.btn_back_to_opener is not None:
            try:
                self.btn_back_to_opener.hide()
            except RuntimeError:
                pass

    def _attach_new_page(self):
        """Put a fresh page on the shared profile into the view."""
        page = _make_ssl_page(self._profile, self)
        self.web_view.setPage(page)
        page.urlChanged.connect(self._on_url_changed)
        page.loadFinished.connect(self._on_page_loaded)
        page.titleChanged.connect(self._on_title_changed)
        self._page = page
        self._update_history_buttons()
        return page

    def _detach_page_signals(self, page):
        if page is None:
            return
        for signal_name, slot in (("urlChanged", self._on_url_changed),
                                  ("loadFinished", self._on_page_loaded),
                                  ("titleChanged", self._on_title_changed)):
            try:
                getattr(page, signal_name).disconnect(slot)
            except (TypeError, RuntimeError):
                pass

    def _build_saved_login_injector_js(self, user: str = None, pw: str = None,
                                       code: str = ""):
        """Script that types the saved credentials into whatever page is up.

        Each of the three values is optional and independent, because the
        consoles this drives ask for them on separate screens: the username
        and password on one, then — once those are accepted — the six-digit
        code on another. Passing only a code fills only the code.

        The script evaluates to ``{user, pass, code, needsCode}``: which of
        the three the page took, and whether it showed a 2FA box that went
        unfilled for want of a code.
        """
        if user is None:
            user = str(self.connection_info.get("username", "") or "").strip()
        if pw is None:
            pw = str(self.connection_info.get("password", "") or "").strip()
        js_user = json.dumps(user)
        js_pw = json.dumps(pw)
        js_code = json.dumps(str(code or "").strip())
        return f"""
(() => {{
  const USER = {js_user};
  const PASS = {js_pw};
  const CODE = {js_code};
  if (!USER && !PASS && !CODE) return false;

  const has = (txt, arr) => arr.some((k) => txt.includes(k));
  const userKw = ['user', 'username', 'login', 'account', 'email', 'userid'];
  const passKw = ['pass', 'password', 'passwd', 'pwd', 'passcode'];
  // A 2FA box is a plain text input like a username box is, so the words
  // that tell them apart have to be checked for on both.
  const otpKw = ['otp', 'one-time', 'onetime', 'one_time', 'totp', 'mfa', '2fa',
                 'two-factor', 'twofactor', 'verification', 'verifycode',
                 'authcode', 'auth_code', 'security code', 'securitycode',
                 'token'];

  const isVisible = (el) => {{
    if (!el) return false;
    if (el.type === 'hidden') return false;
    const style = window.getComputedStyle ? window.getComputedStyle(el) : null;
    if (style && (style.visibility === 'hidden' || style.display === 'none')) return false;
    return !!(el.offsetParent || el.getClientRects().length);
  }};

  const setVal = (el, val) => {{
    if (!el || val === undefined || val === null || val === '') return false;
    try {{ el.removeAttribute('readonly'); el.removeAttribute('disabled'); }} catch (_) {{}}
    try {{ el.focus(); }} catch (_) {{}}

    const proto = Object.getPrototypeOf(el) || window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set ||
                   Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (setter) setter.call(el, val);
    else el.value = val;

    try {{ el.setAttribute('value', val); }} catch (_) {{}}
    try {{ el.dispatchEvent(new InputEvent('input', {{ bubbles: true, data: String(val), inputType: 'insertText' }})); }}
    catch (_) {{ el.dispatchEvent(new Event('input', {{ bubbles: true }})); }}
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    el.dispatchEvent(new Event('keyup', {{ bubbles: true }}));
    el.dispatchEvent(new Event('blur', {{ bubbles: true }}));
    return true;
  }};

  const collectRoots = () => {{
    const roots = [];
    const seen = new Set();
    const add = (r) => {{
      if (!r || seen.has(r)) return;
      seen.add(r);
      roots.push(r);
    }};
    add(document);
    const stack = [document];
    while (stack.length) {{
      const root = stack.pop();
      const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
      for (const n of all) {{
        if (n && n.shadowRoot) {{
          add(n.shadowRoot);
          stack.push(n.shadowRoot);
        }}
      }}
    }}
    return roots;
  }};

  const allInputs = (roots) => {{
    const out = [];
    for (const r of roots) {{
      const list = r.querySelectorAll ? r.querySelectorAll('input') : [];
      for (const el of list) out.push(el);
    }}
    return out;
  }};

  const inputText = (el) => {{
    const id = (el.id || '').toLowerCase();
    const name = (el.name || '').toLowerCase();
    const ph = (el.getAttribute('placeholder') || '').toLowerCase();
    const al = (el.getAttribute('aria-label') || '').toLowerCase();
    const title = (el.getAttribute('title') || '').toLowerCase();
    const cls = (el.className || '').toLowerCase();
    const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
    return `${{id}} ${{name}} ${{ph}} ${{al}} ${{title}} ${{cls}} ${{ac}}`;
  }};

  // Inputs that can never take typed text. Without this a checkbox with no
  // name — DSM's "Don't ask again on this device" — scores as a username
  // box on visibility alone.
  const notText = ['hidden', 'checkbox', 'radio', 'submit', 'button', 'reset',
                   'file', 'image', 'range', 'color'];

  const scoreUser = (el) => {{
    if (!el) return -1;
    const t = (el.type || '').toLowerCase();
    if (t === 'password' || notText.includes(t)) return -1;
    let score = 0;
    const text = inputText(el);
    if (has(text, userKw)) score += 12;
    if (text.includes('current-password')) score -= 20;
    // Never mistake the 2FA box for the username box: on the second screen
    // it is the only text input on the page, so it would otherwise win.
    if (has(text, otpKw)) score -= 30;
    if (t === 'email' || t === 'text' || t === 'tel' || t === '') score += 4;
    if (isVisible(el)) score += 3;
    return score;
  }};

  const scoreOtp = (el) => {{
    if (!el) return -1;
    const t = (el.type || '').toLowerCase();
    if (notText.includes(t)) return -1;
    let score = 0;
    const text = inputText(el);
    if (text.includes('one-time-code')) score += 20;
    if (has(text, otpKw)) score += 14;
    // A short numeric box on a login page is a code box even when nothing
    // is named helpfully — which is most of them.
    const max = parseInt(el.getAttribute('maxlength') || '0', 10);
    if (max >= 4 && max <= 8) score += 6;
    if ((el.getAttribute('inputmode') || '').toLowerCase() === 'numeric') score += 4;
    if (t === 'number') score += 3;
    if (has(text, passKw) && !has(text, otpKw)) score -= 12;
    if (has(text, userKw) && !has(text, otpKw)) score -= 12;
    if (isVisible(el)) score += 3;
    return score;
  }};

  const scorePass = (el) => {{
    if (!el) return -1;
    const t = (el.type || '').toLowerCase();
    if (notText.includes(t)) return -1;
    let score = 0;
    const text = inputText(el);
    if (t === 'password') score += 15;
    if (has(text, passKw)) score += 10;
    if (text.includes('current-password')) score += 8;
    // The 2FA box is the only visible text input on its screen. With no
    // password signal at all it must not be filled by default, or the
    // saved password ends up sitting in the verification-code box.
    if (has(text, otpKw)) score -= 30;
    if (isVisible(el)) score += 3;
    return score;
  }};

  // ``floor`` is the score a candidate has to beat. Visibility alone is
  // worth 3, so a floor of 3 means "only with a real signal".
  const pickBest = (arr, scorer, floor = 0) => {{
    const ranked = arr
      .map((el) => ({{ el, score: scorer(el) }}))
      .filter((x) => x.score > floor)
      .sort((a, b) => b.score - a.score);
    return ranked.length ? ranked[0].el : null;
  }};

  const clickNextIfNeeded = (userApplied, passApplied, roots) => {{
    if (!userApplied || passApplied) return false;
    const selectors = [
      "button[type='submit']",
      "button[id*='next' i]",
      "button[name*='next' i]",
      "button[class*='next' i]",
      "button[aria-label*='next' i]",
      "input[type='submit']",
      "a[role='button']"
    ];
    for (const r of roots) {{
      for (const s of selectors) {{
        const nodes = r.querySelectorAll ? r.querySelectorAll(s) : [];
        for (const el of nodes) {{
          if (!isVisible(el)) continue;
          const txt = (el.innerText || el.textContent || el.value || '').toLowerCase().trim();
          if (txt && !has(txt, ['next', 'continue', 'sign in', 'log in', 'submit', 'ok'])) continue;
          try {{ el.click(); return true; }} catch (_) {{}}
        }}
      }}
    }}
    return false;
  }};

  const fill = () => {{
    const roots = collectRoots();
    const inputs = allInputs(roots);
    let u = false, p = false, o = false;
    if (USER || PASS) {{
      u = setVal(pickBest(inputs, scoreUser), USER);
      // A password only goes where something says "password": a
      // type=password box or a name that says so. Never on visibility.
      p = setVal(pickBest(inputs, scorePass, 3), PASS);
    }}
    const otpBox = pickBest(inputs, scoreOtp);
    if (CODE) {{
      // Deliberately not submitted: a wrong code counts against the
      // account's lockout, so the last press stays the user's.
      o = setVal(otpBox, CODE);
    }}
    clickNextIfNeeded(u, p, roots);
    // A box that calls itself a 2FA box (not merely a short numeric one),
    // left empty because there was no code to give: the one thing worth
    // telling the user, since nothing on the page will.
    const needsCode = !CODE && !!otpBox && scoreOtp(otpBox) >= 14;
    return {{ user: u, pass: p, code: o, needsCode: needsCode }};
  }};

  return fill();
}})();
"""

    # ── Navigation ───────────────────────────────────────────────────

    def _navigate(self):
        if self.web_view is None:
            return
        url = self._start_url()
        self._landing_host = ""
        self.url_bar.setText(url)
        self.web_view.setUrl(QUrl(url))

    def _start_url(self) -> str:
        """The URL this connection opens, as the preset builds it."""
        try:
            return str(self._preset["url_builder"](self._host, self._port) or "")
        except Exception:
            return ""

    def _on_url_changed(self, url: QUrl):
        self.url_bar.setText(url.toString())
        self._update_history_buttons()

    def _on_page_loaded(self, ok: bool):
        self._update_history_buttons()
        # Wherever the console's own first load settles is the console: a
        # QuickConnect ID lands on *.quickconnect.to, a NAS may bounce to
        # its DNS name. Only the first successful load counts; a link the
        # user follows afterwards must not widen the set.
        if ok and not self._landing_host:
            self._landing_host = self._current_page_host()

    def _on_title_changed(self, title: str):
        self.title_changed.emit(str(title or "").strip())

    def _manual_paste_saved_login(self):
        self._attempt_saved_login_fill()

    # ── Where the login is about to go ────────────────────────────────

    def _current_page_host(self) -> str:
        """Host of the page on screen, lower-cased; "" if there is none."""
        page = self._page
        if page is None:
            return ""
        try:
            return str(page.url().host() or "").strip().lower()
        except RuntimeError:
            return ""

    @staticmethod
    def _bare_host(value: str) -> str:
        """Reduce a saved host, host:port or full URL to its lower-cased host."""
        value = str(value or "").strip()
        if not value:
            return ""
        if "://" not in value:
            value = f"https://{value}"
        try:
            host = QUrl(value).host()
        except Exception:
            host = ""
        if not host:
            # Not something QUrl could parse (e.g. a QuickConnect ID with a
            # stray character); fall back to the text before any port/path.
            host = value.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
        return host.strip().lower().strip("[]")

    def _saved_login_hosts(self) -> set:
        """Hosts the saved login may be typed into without asking.

        The host exactly as saved, the host of the URL it opens (a full URL,
        a host:port or a QuickConnect ID all differ from the bare name), and
        wherever the console's first load landed.
        """
        hosts = set()
        try:
            landing = self._landing_host
        except (AttributeError, RuntimeError):
            landing = ""
        for candidate in (self._host, self._start_url(), landing):
            bare = self._bare_host(candidate)
            if bare:
                hosts.add(bare)
        return hosts

    _QUICKCONNECT_SUFFIXES = (".quickconnect.to", ".quickconnect.cn")

    @classmethod
    def _quickconnect_id(cls, host: str) -> str:
        """The QuickConnect ID a quickconnect.to name serves, else "".

        Synology hands one ID out under several names — ``id.quickconnect.to``
        first, then a bounce to a relay (``id.us6.quickconnect.to``) or a
        direct name (``id.direct.quickconnect.to``). The ID is always the
        first label.
        """
        host = str(host or "").strip().lower()
        for suffix in cls._QUICKCONNECT_SUFFIXES:
            if host.endswith(suffix):
                return host[:-len(suffix)].split(".", 1)[0]
        return ""

    def _saved_login_quickconnect_ids(self) -> set:
        """QuickConnect IDs the saved login belongs to, if it has any."""
        ids = set()
        for host in self._saved_login_hosts():
            qc_id = self._quickconnect_id(host)
            if qc_id:
                ids.add(qc_id)
        # A bare ID opens https://quickconnect.to/<id>, so the ID is the
        # saved host itself rather than a label of any host.
        if self._bare_host(self._start_url()) in ("quickconnect.to", "quickconnect.cn"):
            bare_id = str(self._host or "").strip().lower()
            if bare_id and "." not in bare_id:
                ids.add(bare_id)
        return ids

    def _page_matches_connection(self) -> bool:
        """True when the page belongs to the host this login was saved for.

        The console's own pages can only be on that host, so any other one
        is somewhere a link led — a vendor's help site, a redirect — and not
        somewhere the saved password should be typed without asking.
        """
        current = self._current_page_host().strip("[]")
        allowed = self._saved_login_hosts()
        if not allowed or not current:
            return True     # nothing to compare yet (page still loading)
        if current in allowed:
            return True
        # A QuickConnect ID is served from a relay or direct name under
        # quickconnect.to, never from the name it was saved under: the same
        # ID on any of its names is still the same NAS.
        current_id = self._quickconnect_id(current)
        if current_id and current_id in self._saved_login_quickconnect_ids():
            return True
        return False

    def _confirm_fill_elsewhere(self) -> bool:
        """Ask before filling a page that is not the saved host."""
        current = self._current_page_host() or "an unknown site"
        answer = QMessageBox.question(
            self, "Different site",
            f"This page is on <b>{current}</b>, but the saved login belongs to "
            f"<b>{self._bare_host(self._start_url()) or self._host}</b>."
            "<br><br>Fill it in here anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    # ── Two-step verification ────────────────────────────────────────

    def _refresh_totp_button(self):
        """Retitle the button with the code that is valid this second."""
        button = getattr(self, "btn_totp", None)
        if button is None:
            return
        try:
            current = totp.code_for_connection(self.connection_info)
        except RuntimeError:      # the C++ side went away mid-tick
            return
        if current is None:
            button.setEnabled(False)
            button.setText("2FA secret unreadable")
            button.setToolTip(
                "The saved 2-step verification secret could not be decoded. "
                "Edit the connection and paste it again.")
            return
        code, seconds = current
        half = len(code) // 2
        button.setEnabled(True)
        button.setText(f"{code[:half]} {code[half:]} · {seconds:>2}s")

    def _use_totp_code(self):
        """Put the current code in the page's 2FA box and on the clipboard."""
        current = totp.code_for_connection(self.connection_info)
        if current is None:
            return
        code, _seconds = current

        # Gone from the clipboard roughly when it stops being valid.
        copy_secret(code, clear_after_ms=60_000)

        if self._page is not None:
            if not self._page_matches_connection() and not self._confirm_fill_elsewhere():
                return
            try:
                self._page.runJavaScript(
                    self._build_saved_login_injector_js(user="", pw="", code=code))
            except Exception:
                pass

    def _copy_saved_login(self):
        user = str(self.connection_info.get("username", "") or "").strip()
        pw = str(self.connection_info.get("password", "") or "").strip()
        if not user and not pw:
            return
        text = f"Username: {user}\nPassword: {pw}"
        current = totp.code_for_connection(self.connection_info)
        if current is not None:
            code, seconds = current
            text += f"\nCode: {code}  (valid {seconds}s)"
        if not copy_secret(text):
            return
        self.url_bar.setText(
            f"{self.url_bar.text()}   •   Saved login copied (clipboard clears in 30s)")

    def _attempt_saved_login_fill(self):
        user = str(self.connection_info.get("username", "") or "").strip()
        pw = str(self.connection_info.get("password", "") or "").strip()
        current = totp.code_for_connection(self.connection_info)
        code = current[0] if current else ""
        if not user and not pw and not code:
            return

        if self._page is None:
            return
        if not self._page_matches_connection() and not self._confirm_fill_elsewhere():
            return
        # One press fills whichever of the three boxes this screen is showing,
        # so the same button works on the login page and the 2FA page after it.
        script = self._build_saved_login_injector_js(user=user, pw=pw, code=code)
        try:
            self._page.runJavaScript(script, self._on_saved_login_filled)
        except Exception:
            pass

    def _on_saved_login_filled(self, result):
        """Say the one thing the fill could not do: give a code it does not have.

        A NAS with 2-step verification on stops at a code box, and a connection
        without a stored secret has nothing to put there. The button appears to
        do nothing at that point, so this is where the reason gets shown.
        """
        if not isinstance(result, dict) or not result.get("needsCode"):
            return
        if totp.has_totp(self.connection_info):
            return
        try:
            self.url_bar.setText(
                f"{self.url_bar.text()}   •   This page wants a 2-step code — "
                "none is saved. Edit the connection and add the secret.")
        except RuntimeError:
            pass

    # ── Lifecycle ────────────────────────────────────────────────────

    def _disconnect(self):
        self.cleanup()
        self.session_closed.emit()

    def _reconnect(self):
        if self.web_view is None or not _ensure_webengine():
            return
        self._certificate_prompts.close()
        self._certificate_prompts.deleteLater()
        self._certificate_prompts = CertificatePrompts(self._certificate_trust, self)
        old_page = self._page
        self._detach_page_signals(old_page)
        # Attach the replacement before discarding the old page so the view is
        # never left pointing at a deleted one.
        self._attach_new_page()
        if old_page is not None:
            try:
                old_page.deleteLater()
            except RuntimeError:
                pass
        self._navigate()

    def cleanup(self):
        """Tear the browser down for good. Safe to call more than once — the
        disconnect button and the tab-close handler both reach it."""
        prompts = getattr(self, "_certificate_prompts", None)
        if prompts is not None:
            prompts.close()
        timer = getattr(self, "_totp_timer", None)
        if timer is not None:
            try:
                timer.stop()
            except RuntimeError:
                pass

        page, self._page = self._page, None
        view, self.web_view = self.web_view, None

        for cap in self._capture_pages:
            try:
                cap.deleteLater()
            except RuntimeError:
                pass
        self._capture_pages = []

        if page is not None:
            self._detach_page_signals(page)
            try:
                page.setUrl(QUrl("about:blank"))   # drop the live connection
            except RuntimeError:
                pass
        # Queue the view first: it still points at the page, so the page must
        # not be destroyed while a live view holds a pointer to it.
        if view is not None:
            try:
                view.deleteLater()
            except RuntimeError:
                pass
        if page is not None:
            try:
                page.deleteLater()
            except RuntimeError:
                pass
        # self._profile is shared process-wide and is deliberately left alive.

    def suspend(self):
        """WebEngine handles its own sockets — nothing to do."""
        pass

    def resume(self):
        """Reload the page after system wake to restore session."""
        if self.web_view:
            try:
                self.web_view.reload()
            except Exception:
                pass

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _hex_to_rgba(hex_color: str, alpha: float) -> str:
        """Convert '#rrggbb' → 'r,g,b,alpha' for use in rgba()."""
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"{r},{g},{b},{alpha}"
