import os
import sys
import ctypes
import ctypes.wintypes  # noqa: F401 — registers ctypes.wintypes (RECT, etc.)
import logging
import subprocess
import tempfile
import time
import threading
import winreg
from dataclasses import dataclass, field
from pathlib import Path
from PyQt6.QtWidgets import (
    QPushButton, QWidget, QVBoxLayout, QHBoxLayout, QFrame, QStackedWidget,
    QMessageBox, QLabel, QGroupBox, QFormLayout, QComboBox, QCheckBox,
    QScrollArea, QApplication, QAbstractSpinBox, QLineEdit, QPlainTextEdit,
    QTextEdit, QDialog, QDialogButtonBox
)
from PyQt6.QtCore import Qt, QTimer, QEvent, QSize, pyqtSignal
from PyQt6.QtGui import QFont, QColor, QGuiApplication

from app import icons
from app.widgets import set_variant


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
advapi32 = ctypes.windll.advapi32

# On 64-bit Windows an HWND is 64 bits; ctypes defaults an unprototyped call to
# a 32-bit ``int`` return, which silently truncates the handle. The window
# functions whose *result* we compare or reparent through must return a real
# pointer, or "did the reparent take?" is asked against a mangled handle.
for _fn, _restype, _argtypes in (
    (user32.SetParent, ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_void_p]),
    (user32.GetParent, ctypes.c_void_p, [ctypes.c_void_p]),
    (user32.GetAncestor, ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_uint]),
    (user32.GetClientRect, ctypes.wintypes.BOOL,
     [ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.RECT)]),
    (user32.GetWindowRect, ctypes.wintypes.BOOL,
     [ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.RECT)]),
    (user32.MapWindowPoints, ctypes.c_int,
     [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]),
    (user32.SetWindowPos, ctypes.wintypes.BOOL,
     [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
      ctypes.c_int, ctypes.c_int, ctypes.c_uint]),
    (user32.MoveWindow, ctypes.wintypes.BOOL,
     [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
      ctypes.c_int, ctypes.wintypes.BOOL]),
    (user32.GetWindowLongPtrW, ctypes.c_ssize_t, [ctypes.c_void_p, ctypes.c_int]),
    (user32.SetWindowLongPtrW, ctypes.c_ssize_t,
     [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]),
):
    _fn.restype = _restype
    _fn.argtypes = _argtypes

# Everything the RDP path does ends up in the crash log under this name: the
# file mstsc was handed, what went into Credential Manager, every dialog mstsc
# put up and what it said.  An RDP session that fails used to leave no trace
# at all, which made "the gateway does not work" impossible to answer.
_LOG = logging.getLogger("rpmote.rdp")
GWL_STYLE = -16
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_POPUP = 0x80000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
GW_OWNER = 4
GA_ROOT = 2
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
VK_CONTROL = 0x11
VK_MENU = 0x12   # Alt
VK_END = 0x23
VK_CAPITAL = 0x14   # CapsLock
VK_NUMLOCK = 0x90
VK_SCROLL = 0x91    # ScrollLock
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
INPUT_KEYBOARD = 1
ULONG_PTR = ctypes.c_size_t
MAPVK_VK_TO_VSC = 0
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20

# VK codes that map to extended keys (require bit 24 set in WM_KEYDOWN lParam).
# These are the "grey" navigation/modifier keys that use the E0 scan-code prefix.
_EXTENDED_VK: frozenset = frozenset({
    0x21,  # VK_PRIOR  – Page Up
    0x22,  # VK_NEXT   – Page Down
    0x23,  # VK_END
    0x24,  # VK_HOME
    0x25,  # VK_LEFT
    0x26,  # VK_UP
    0x27,  # VK_RIGHT
    0x28,  # VK_DOWN
    0x2D,  # VK_INSERT
    0x2E,  # VK_DELETE
    0x5B,  # VK_LWIN
    0x5C,  # VK_RWIN
    0x5D,  # VK_APPS
    0x6F,  # VK_DIVIDE – numpad /
    0x90,  # VK_NUMLOCK
    0xA3,  # VK_RCONTROL
    0xA5,  # VK_RMENU  – right Alt
})


# Widgets that are legitimately consuming keystrokes when they hold focus.  The
# embedded session never steals focus away from one of these (e.g. the sidebar
# search box) even though it is otherwise entitled to the keyboard.
_TEXT_ENTRY_WIDGETS = (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox)


def _should_claim_rdp_focus(focus_widget, focus_is_own: bool) -> bool:
    """Decide whether the embedded session may take Qt keyboard focus.

    ``focus_widget`` is the application-wide focus widget (``None`` when no
    widget holds it) and ``focus_is_own`` says whether it already belongs to the
    embedded RDP widget's own subtree.  Pure helper so the rules stay testable
    without a live, activated Qt window.
    """
    if focus_widget is None:
        return True
    if focus_is_own:
        # Already ours — e.g. the user clicked one of the session's buttons.
        return False
    # Never interrupt typing elsewhere in the app.
    return not isinstance(focus_widget, _TEXT_ENTRY_WIDGETS)


# ── Local resource redirection (per connection) ──────────────────────────
#
# Which local devices an RDP session may use. Asked once, the first time a
# connection is opened, then stored on the connection record so every later
# session for that device connects straight through with the same answer.
RDP_RESOURCE_KEY = "rdp_resources"

# (field, label, tooltip) — also drives the order shown in the dialog.
RDP_RESOURCE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("clipboard", "Clipboard", "Copy and paste text and files between this PC and the remote session."),
    ("printers", "Printers", "Make the printers installed on this PC available in the remote session."),
    ("drives", "Drives", "Expose this PC's drives to the remote session. Leave off unless you need to move files."),
    ("audio", "Audio", "Play the remote session's sound through this PC."),
    ("smartcards", "Smart cards", "Share smart card readers attached to this PC with the remote session."),
)

_RDP_RESOURCE_DEFAULTS: dict[str, bool] = {
    "clipboard": True,
    "printers": False,
    "drives": False,
    "audio": True,
    "smartcards": False,
}


def normalize_rdp_resources(saved) -> dict:
    """Coerce a stored resource selection into a complete, boolean dict.

    Tolerates missing keys, unknown extras and JSON round-trip artefacts (the
    vault stores whatever it was given), so a partial or hand-edited record can
    never break a connection.
    """
    result = dict(_RDP_RESOURCE_DEFAULTS)
    if isinstance(saved, dict):
        for key in result:
            if key in saved:
                result[key] = bool(saved[key])
    return result


def has_saved_rdp_resources(connection_info: dict) -> bool:
    """True once the user has answered the local-resources prompt for this device."""
    return isinstance(connection_info, dict) and isinstance(
        connection_info.get(RDP_RESOURCE_KEY), dict
    )


def rdp_resource_lines(resources) -> list[str]:
    """Render a resource selection as .rdp redirection directives."""
    r = normalize_rdp_resources(resources)
    lines = [
        f"redirectclipboard:i:{int(r['clipboard'])}",
        f"redirectprinters:i:{int(r['printers'])}",
        f"redirectdrives:i:{int(r['drives'])}",
        f"redirectsmartcards:i:{int(r['smartcards'])}",
        # 0 = play on this computer, 2 = do not play.
        f"audiomode:i:{0 if r['audio'] else 2}",
    ]
    if r["drives"]:
        # mstsc needs the explicit device list as well as the redirect flag.
        lines.append("drivestoredirect:s:*")
    return lines


# ── RD Gateway (per connection) ──────────────────────────────────────────
#
# The gateway itself — what a connection stores about it, how it is written
# into a .rdp file, and how mstsc's complaints about it are read — lives in
# rdp_gateway.py, where none of it needs a window.  The names are re-exported
# here because the connection dialog and the tests have always found them
# here.

from .rdp_gateway import (  # noqa: E402,F401 — re-exported
    RDP_GATEWAY_BYPASS_KEY,
    RDP_GATEWAY_DEFAULT_PORT,
    RDP_GATEWAY_HOST_KEY,
    RDP_GATEWAY_PASSWORD_KEY,
    RDP_GATEWAY_SHARE_CREDS_KEY,
    RDP_GATEWAY_USERNAME_KEY,
    account_has_domain,
    describe_gateway_route,
    explain_mstsc_message,
    gateway_credential_targets,
    gateway_is_enabled,
    gateway_login,
    is_gateway_message,
    normalize_gateway_host,
    normalize_rdp_gateway,
    rdp_gateway_lines,
    seed_gateway_credentials,
    split_gateway_address,
)
from .windows_credentials import (  # noqa: E402
    CredentialReport, release_credentials, store_credentials,
)


# ── One .rdp file, three launch paths ────────────────────────────────────
#
# The embedded (hosted mstsc) session and both ways to open an external
# window each hand mstsc a file.  They used to write three
# separate ones and drifted — different authentication levels, different
# encodings, a gateway block that one of them could forget.  Everything a
# session needs to *reach* the machine is now written by one function; the
# callers add only what differs, which is how the desktop is displayed.


@dataclass
class RDPCredentialState:
    """What was put in Credential Manager for this launch, and therefore
    whether mstsc can be expected to connect without asking."""

    desktop: CredentialReport = field(default_factory=CredentialReport)
    gateway: CredentialReport = field(default_factory=CredentialReport)
    gateway_host: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def desktop_ready(self) -> bool:
        return self.desktop.ok

    @property
    def gateway_ready(self) -> bool:
        """True when there is no gateway, or its login is stored."""
        return not self.gateway_host or self.gateway.ok

    @property
    def will_prompt(self) -> bool:
        return not (self.desktop_ready and self.gateway_ready)

    def release(self):
        """Take this launch's logins back out of Credential Manager.

        Called once the mstsc they were written for has exited — not
        before, since a dropped link makes mstsc re-read them to reconnect.
        Two open sessions to the same host share the same entries, so an
        entry is only removed when the last launch using it lets go.
        """
        for report in (self.desktop, self.gateway):
            if not report.attempted:
                continue
            _release_shared(report)
            report.writes = []


# Launches currently relying on each Credential Manager entry, so a session
# closing does not pull the login out from under another one to the same
# host.  Keyed by (target, type); guarded because the watcher threads for
# external mstsc windows release from off the GUI thread.
_CRED_USERS: dict[tuple[str, int], int] = {}
_CRED_USERS_LOCK = threading.Lock()


def _hold_shared(report: CredentialReport):
    with _CRED_USERS_LOCK:
        for w in report.writes:
            if w.ok:
                key = (w.target.lower(), w.cred_type)
                _CRED_USERS[key] = _CRED_USERS.get(key, 0) + 1


def _release_shared(report: CredentialReport):
    last = CredentialReport()
    with _CRED_USERS_LOCK:
        for w in report.writes:
            if not w.ok:
                continue
            key = (w.target.lower(), w.cred_type)
            left = _CRED_USERS.get(key, 1) - 1
            if left <= 0:
                _CRED_USERS.pop(key, None)
                last.writes.append(w)
            else:
                _CRED_USERS[key] = left
    release_credentials(last)


def release_when_exited(proc, credentials: RDPCredentialState | None,
                        rdp_path: str | None = None):
    """Release an external launch's credentials and file when mstsc exits.

    mstsc can outlive its launcher widget. A failed launch has no process
    to wait for, so its credentials and file can be released immediately.
    """
    def _release():
        if proc is not None:
            try:
                proc.wait()
            except Exception:
                pass
        if credentials is not None:
            credentials.release()
        if rdp_path:
            try:
                Path(rdp_path).unlink(missing_ok=True)
            except OSError:
                _LOG.warning("Could not remove temporary RDP file %s", rdp_path)

    if proc is None:
        _release()
    else:
        threading.Thread(target=_release, name="rdp-cred-release", daemon=True).start()


def _rdp_desktop_targets(host: str, port: int | str | None) -> list[str]:
    """Credential Manager names for the desktop itself.

    ``TERMSRV/<host>`` is what mstsc asks for; a non-default port has been
    seen looked up with the port appended, so that spelling is added too.
    """
    target = _rdp_termsrv_target(host, port)
    if not target:
        return []
    bare = _rdp_termsrv_target(host, 3389)
    return [target] if target == bare else [target, bare]


def prepare_rdp_credentials(connection_info: dict) -> RDPCredentialState:
    """Put every login this session needs where mstsc will find it.

    Done before mstsc starts, every time: the vault is the source of truth,
    and a password changed there has to reach Credential Manager before the
    next connect or the old one keeps being offered.
    """
    host = str(connection_info.get("host", "") or "").strip()
    port = connection_info.get("port", 3389)
    user = str(connection_info.get("username", "") or "").strip()
    password = str(connection_info.get("password", "") or "")
    gateway = normalize_rdp_gateway(connection_info)

    state = RDPCredentialState(gateway_host=gateway["host"])
    if host and user and password:
        state.desktop = store_credentials(_rdp_desktop_targets(host, port), user, password)
        if not state.desktop.ok:
            state.warnings.append(
                f"Could not save the desktop login for mstsc ({state.desktop.failure}); "
                "Windows will ask for it.")
    elif host:
        state.warnings.append("No username or password saved; Windows will ask for them.")

    if gateway["host"]:
        state.gateway = seed_gateway_credentials(gateway, user, password)
        gw_user, gw_password = gateway_login(gateway, user, password)
        if not gw_user or not gw_password:
            state.warnings.append(
                f"No login saved for RD Gateway {gateway['host']}; Windows will ask for it.")
        elif not state.gateway.ok:
            state.warnings.append(
                f"Could not save the RD Gateway login for mstsc ({state.gateway.failure}); "
                "Windows will ask for it.")
        elif not account_has_domain(gw_user):
            state.warnings.append(
                f"The gateway login \"{gw_user}\" has no domain. If the gateway rejects it, "
                "save the account as DOMAIN\\user.")

    _hold_shared(state.desktop)
    _hold_shared(state.gateway)
    _LOG.info(
        "credentials prepared | host=%s desktop=%s gateway=%s gateway_ok=%s warnings=%s",
        host, state.desktop.targets_written or "none", gateway["host"] or "none",
        state.gateway_ready, state.warnings or "none")
    return state


def build_rdp_session_lines(connection_info: dict, credentials: RDPCredentialState | None,
                            resources=None) -> list[str]:
    """Everything a .rdp file needs to *reach* the machine.

    Address, login, security negotiation, the gateway, local resources and
    reconnection — the parts that must be the same whichever window the
    desktop ends up in.  Display settings are the caller's.
    """
    host = str(connection_info.get("host", "") or "").strip()
    port = connection_info.get("port", 3389)
    try:
        port = int(port) if port not in (None, "") else 3389
    except (TypeError, ValueError):
        port = 3389
    user = str(connection_info.get("username", "") or "").strip()
    gateway = normalize_rdp_gateway(connection_info)
    prompt = 1
    if credentials is not None and credentials.desktop_ready:
        prompt = 0

    lines = [
        f"full address:s:{host}:{port}",
        f"username:s:{user}",
        # Ask for the password only when nothing was stored for mstsc to use.
        # With a gateway, mstsc consults Credential Manager for the gateway
        # on its own and prompts if that comes back empty; forcing a prompt
        # here as well would mean two.
        f"prompt for credentials:i:{prompt}",
        "enablecredsspsupport:i:1",
        "negotiate security layer:i:1",
        # 0 = connect even when the desktop's certificate cannot be verified.
        # Nearly every customer server has a self-signed one; the warning is
        # noise, and the gateway's certificate is checked by mstsc regardless.
        "authentication level:i:0",
        # Windows key combinations go to the remote session while it has focus.
        "keyboardhook:i:1",
        "autoreconnection enabled:i:1",
        "autoreconnect max retries:i:20",
    ]
    # Connect from anywhere, if this machine is behind a gateway.
    lines.extend(rdp_gateway_lines(gateway))
    # Whatever the user chose for this device — asked once, then reused.
    if resources is None:
        resources = connection_info.get(RDP_RESOURCE_KEY)
    lines.extend(rdp_resource_lines(resources))
    return lines


def build_external_rdp_lines(connection_info: dict, credentials: RDPCredentialState,
                             resources=None, *, fullscreen: bool,
                             multimon: bool = False) -> list[str]:
    lines = build_rdp_session_lines(connection_info, credentials, resources)
    # Keep the remote desktop size fixed when moving between displays.
    lines.append("dynamic resolution:i:0")
    if multimon:
        lines.append("use multimon:i:1")
    lines.append(f"screen mode id:i:{2 if fullscreen or multimon else 1}")
    if not fullscreen and not multimon:
        lines.append("smart sizing:i:1")
    lines.extend([
        "connection type:i:7",
        "networkautodetect:i:1",
        "bandwidthautodetect:i:1",
        "enable font smoothing:i:1",
        "disable wallpaper:i:0",
        "disable themes:i:0",
        "bitmapcachepersistenable:i:0",
        "videoplaybackmode:i:1",
    ])
    return lines


def write_rdp_file(lines: list[str]) -> str:
    """Write a .rdp file the way mstsc writes its own: UTF-16 with a BOM.

    mstsc reads ANSI too, but a hostname or username outside the local code
    page then comes out wrong, and a file mstsc itself would have produced is
    the one it has no opinions about.
    """
    fd, path = tempfile.mkstemp(suffix=".rdp")
    os.close(fd)
    with open(path, "w", encoding="utf-16") as handle:
        handle.write("\n".join(lines) + "\n")
    _LOG.info("rdp file written | path=%s | %s", path,
              " ; ".join(l for l in lines if not l.startswith("password")))
    return path


#: The Remote Desktop client's own disconnect reasons (its event 1026), for
#: the ones that are not a person closing the session.
_DISCONNECT_REASONS = {
    260: "the computer name could not be looked up",
    264: "the connection timed out",
    516: "nothing accepted the connection",
    520: "the computer was not found",
    772: "sending to the computer failed",
    1028: "receiving from the computer failed",
    1288: "the computer name could not be looked up",
    1796: "the connection timed out",
    2055: "the sign-in failed",
    2056: "license negotiation failed",
    2308: "the connection was cut",
    2312: "licensing timed out",
}
#: Reasons that mean the network between here and the computer let it down.
_NETWORK_REASONS = {264, 772, 1028, 1796, 2308}


def rdp_client_outcome(pid, since: float) -> tuple[bool, int | None]:
    """(signed in?, disconnect reason) the Remote Desktop client logged for mstsc *pid*.

    Windows records each connection in its RDPClient/Operational log under
    the mstsc process that made it; no administrator rights are needed.
    (False, None) when there is nothing to read.
    """
    try:
        import re
        import win32evtlog

        stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(max(0.0, since - 5)))
        query = (f"*[System[(EventID=1026 or EventID=1027) and Execution[@ProcessID={int(pid)}]"
                 f" and TimeCreated[@SystemTime>='{stamp}']]]")
        handle = win32evtlog.EvtQuery("Microsoft-Windows-TerminalServices-RDPClient/Operational",
                                      win32evtlog.EvtQueryChannelPath, query)
        signed_in, reason = False, None
        while (batch := win32evtlog.EvtNext(handle, 16)):
            for event in batch:
                xml = win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml)
                event_id = re.search(r"<EventID>(\d+)</EventID>", xml)
                if event_id and event_id.group(1) == "1027":
                    signed_in = True
                value = re.search(r"<Data Name='Value'>(\d+)</Data>", xml)
                if event_id and event_id.group(1) == "1026" and value:
                    reason = int(value.group(1))
        return signed_in, reason
    except Exception:
        return False, None


def describe_disconnect(signed_in: bool, reason: int | None, host: str) -> str:
    """Why the Remote Desktop client said a connection ended ("" when it said nothing useful)."""
    meaning = _DISCONNECT_REASONS.get(reason or 0)
    if not meaning:
        return ""
    if not signed_in:
        return f"Could not connect to {host}: {meaning} (Windows reason {reason})."
    text = f"Signed in to {host}, then {meaning} (Windows reason {reason})."
    if reason in _NETWORK_REASONS:
        text += " Over a VPN that is often a tunnel losing large packets."
    return text


def _log_rdp_launch(connection_info: dict, mode: str, path: str, pid: int | None):
    gateway = normalize_rdp_gateway(connection_info)
    _LOG.info(
        "mstsc launched | mode=%s host=%s:%s user=%s gateway=%s bypass_local=%s "
        "share_creds=%s pid=%s file=%s",
        mode, connection_info.get("host", ""), connection_info.get("port", 3389),
        connection_info.get("username", ""), gateway["host"] or "none",
        gateway["bypass_local"], gateway["use_for_remote"], pid, path)


class RDPLocalResourcesDialog(QDialog):
    """First-run prompt asking what the remote session may use from this PC."""

    def __init__(self, connection_info: dict, resources: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Local Resources")
        self.setMinimumWidth(420)
        self.setModal(True)
        self._boxes: dict[str, QCheckBox] = {}
        self._setup_ui(connection_info, normalize_rdp_resources(resources))

    def _setup_ui(self, connection_info: dict, resources: dict):
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 24, 24, 24)

        name = connection_info.get("name") or connection_info.get("host") or "this connection"
        title = QLabel("Local Resources")
        title.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        layout.addWidget(title)

        subtitle = QLabel(f"Choose what {name} can use from this PC.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #94a3b8; font-size: 12px;")
        layout.addWidget(subtitle)

        group = QGroupBox()
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(8)
        for field, label, tooltip in RDP_RESOURCE_FIELDS:
            box = QCheckBox(label)
            box.setToolTip(tooltip)
            box.setChecked(bool(resources.get(field)))
            group_layout.addWidget(box)
            self._boxes[field] = box
        layout.addWidget(group)

        hint = QLabel(
            "RPMote remembers this for this connection and won't ask again. "
            "Change it any time with the Local Resources button on the session tab."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #64748b; font-size: 11px; font-style: italic;")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Connect")
        set_variant(buttons.button(QDialogButtonBox.StandardButton.Ok), "primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict:
        return {field: box.isChecked() for field, box in self._boxes.items()}


def _resolve_local_resources(
    parent_widget,
    connection_info: dict,
    on_chosen,
    force_prompt: bool = False,
) -> dict | None:
    """Return the local-resource selection to connect with.

    Prompts when the connection has no stored answer yet (or when the user asked
    to change it), writes the answer onto ``connection_info`` and hands it to
    ``on_chosen`` so the caller can persist it. Returns None if the prompt was
    cancelled, meaning the caller should not connect.
    """
    saved = connection_info.get(RDP_RESOURCE_KEY) if isinstance(connection_info, dict) else None
    if has_saved_rdp_resources(connection_info) and not force_prompt:
        return normalize_rdp_resources(saved)

    dialog = RDPLocalResourcesDialog(connection_info, saved, parent_widget)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None

    chosen = dialog.values()
    connection_info[RDP_RESOURCE_KEY] = chosen
    if on_chosen:
        on_chosen(dict(chosen))
    return chosen


_EMBEDDED_RDP_SUPPORT_CACHE: tuple[bool, str] | None = None


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


class TOKEN_ELEVATION_STRUCT(ctypes.Structure):
    _fields_ = [("TokenIsElevated", ctypes.c_ulong)]


user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
user32.MapVirtualKeyW.restype = ctypes.c_uint
user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = ctypes.c_uint
user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetFocus.argtypes = []
user32.GetFocus.restype = ctypes.c_void_p
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.restype = ctypes.c_bool
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.ShowWindow.restype = ctypes.c_bool
user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
user32.BringWindowToTop.restype = ctypes.c_bool
user32.SetFocus.argtypes = [ctypes.c_void_p]
user32.SetFocus.restype = ctypes.c_void_p
user32.SetActiveWindow.argtypes = [ctypes.c_void_p]
user32.SetActiveWindow.restype = ctypes.c_void_p
user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
user32.GetAncestor.restype = ctypes.c_void_p


def embedded_rdp_supported() -> tuple[bool, str]:
    """Preflight whether embedded RDP is safe to attempt in this runtime.

    NOTE:
    Avoid spawning a subprocess via sys.executable for probing. In frozen builds
    (PyInstaller), sys.executable points to the app EXE; launching it with
    "-c" can start a second app instance (triggering another unlock prompt)
    instead of running inline probe code.
    """
    global _EMBEDDED_RDP_SUPPORT_CACHE
    if _EMBEDDED_RDP_SUPPORT_CACHE is not None:
        return _EMBEDDED_RDP_SUPPORT_CACHE

    app = QApplication.instance()
    if app is None:
        _EMBEDDED_RDP_SUPPORT_CACHE = (False, "qapplication-missing")
        return _EMBEDDED_RDP_SUPPORT_CACHE

    # Keep preflight conservative and safe: only verify Qt ActiveX bindings can
    # be imported in this process. Avoid creating/probing COM controls here,
    # which can cause native instability in some environments.
    QAxWidget, reason = _get_rax_control()
    if not QAxWidget:
        _EMBEDDED_RDP_SUPPORT_CACHE = (False, reason)
        return _EMBEDDED_RDP_SUPPORT_CACHE

    _EMBEDDED_RDP_SUPPORT_CACHE = (True, "ok")
    return _EMBEDDED_RDP_SUPPORT_CACHE


def _window_title(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(512)
    try:
        user32.GetWindowTextW(ctypes.c_void_p(int(hwnd)), buf, 512)
        return buf.value or ""
    except Exception:
        return ""


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    try:
        user32.GetClassNameW(ctypes.c_void_p(int(hwnd)), buf, 256)
        return buf.value or ""
    except Exception:
        return ""


# ── What mstsc has on screen ─────────────────────────────────────────────
#
# While it connects, mstsc puts up ordinary Win32 dialogs (class ``#32770``)
# and, on Windows 10 and later, a XAML-hosted credential prompt.  None of
# them may be adopted into the canvas as if they were the desktop — a
# transient dialog parented into the tab leaves a dead child HWND and a blank
# panel when it closes.  They are also not all the same thing, and treating
# them as one ("waiting for credentials") is how a gateway that rejected the
# certificate was reported as a password problem.

DIALOG_CREDENTIALS = "credentials"     # Windows Security — asking for a login
DIALOG_PROGRESS = "progress"           # "Connecting to: … Securing remote connection"
DIALOG_SECURITY_WARNING = "warning"    # publisher / certificate — has a Connect or Yes button
DIALOG_MESSAGE = "message"             # an error or notice with an OK button
DIALOG_UNKNOWN = "unknown"             # a dialog, but none of the above


def _text_has(text: str, *needles: str) -> bool:
    lowered = (text or "").lower()
    return any(n in lowered for n in needles)


def classify_mstsc_dialog(title: str, class_name: str,
                          children: list[tuple[str, str]] | None = None) -> str | None:
    """Say what kind of dialog an mstsc top-level window is, or None if it is
    not a dialog at all (the desktop window, say).

    ``children`` is ``[(class, text), ...]`` for the dialog's controls, which
    is what tells a progress box from an error box: both are ``#32770``
    windows titled "Remote Desktop Connection".  Pure so it can be tested
    against window trees that were seen once and written down.
    """
    t = (title or "").strip().lower()
    c = (class_name or "").strip().lower()
    children = children or []

    if "credential dialog" in c or "xaml" in c:
        return DIALOG_CREDENTIALS
    if c != "#32770":
        return None
    if _text_has(t, "windows security", "your credentials did not work",
                 "enter your credentials"):
        return DIALOG_CREDENTIALS

    buttons = [txt.replace("&", "").strip().lower()
               for cls, txt in children if cls.lower() == "button"]
    statics = " ".join(txt for cls, txt in children
                       if cls.lower() in ("static", "syslink", "richedit20w", "edit"))
    has_progress_bar = any(cls.lower() == "msctls_progress32" for cls, _ in children)

    if has_progress_bar or _text_has(statics, "connecting to", "initiating remote connection",
                                     "securing remote connection", "configuring remote session",
                                     "estimating connection quality"):
        return DIALOG_PROGRESS
    if "connect" in buttons or "yes" in buttons:
        return DIALOG_SECURITY_WARNING
    if _text_has(t, "security warning", "unknown publisher"):
        return DIALOG_SECURITY_WARNING
    if "ok" in buttons or "close" in buttons:
        return DIALOG_MESSAGE
    if _text_has(statics, "can't connect", "cannot connect", "could not", "unable to",
                 "failed", "error", "not authorized", "denied"):
        return DIALOG_MESSAGE
    if _text_has(t, "credentials", "password", "logon", "login", "authentication",
                 "certificate"):
        return DIALOG_CREDENTIALS
    if not t or "remote desktop connection" in t:
        return DIALOG_UNKNOWN
    return DIALOG_UNKNOWN


def dialog_message_text(children: list[tuple[str, str]]) -> str:
    """The words on a dialog, in reading order, buttons left out."""
    parts = []
    for cls, txt in children:
        if cls.lower() == "button":
            continue
        txt = (txt or "").strip()
        if txt and txt not in parts:
            parts.append(txt)
    return " ".join(parts)


def _is_probable_rdp_auth_dialog(title: str, class_name: str) -> bool:
    """True for any mstsc dialog that must never be adopted as the desktop.

    Title-and-class only, for the callers (and tests) that have no control
    tree; ``classify_mstsc_dialog`` is the one that says which dialog.
    """
    return classify_mstsc_dialog(title, class_name) is not None


@dataclass
class MstscDialog:
    """One dialog mstsc currently has up, read as far as Win32 lets us."""

    hwnd: int
    kind: str
    title: str = ""
    text: str = ""


def _dialog_children(hwnd: int) -> list[tuple[str, str]]:
    """``[(class, text), ...]`` for every control in a dialog.

    Standard dialog controls answer ``GetWindowText`` across processes, which
    is enough for mstsc's own dialogs; the XAML credential prompt does not,
    and is recognised by its class instead.
    """
    children: list[tuple[str, str]] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    text_buf = ctypes.create_unicode_buffer(2048)
    class_buf = ctypes.create_unicode_buffer(256)

    def _callback(child, _lparam):
        try:
            user32.GetClassNameW(ctypes.c_void_p(child), class_buf, 256)
            user32.GetWindowTextW(ctypes.c_void_p(child), text_buf, 2048)
            children.append((class_buf.value or "", text_buf.value or ""))
        except Exception:
            pass
        return True

    try:
        user32.EnumChildWindows(ctypes.c_void_p(int(hwnd)), WNDENUMPROC(_callback), 0)
    except Exception:
        pass
    return children


def _inspect_mstsc_dialog(hwnd: int, title: str, class_name: str) -> MstscDialog | None:
    children = _dialog_children(hwnd)
    kind = classify_mstsc_dialog(title, class_name, children)
    if kind is None:
        return None
    return MstscDialog(hwnd=int(hwnd), kind=kind, title=title or "",
                       text=dialog_message_text(children))


def _choose_best_mstsc_main_window(candidates: list[dict]) -> int | None:
    """Choose the actual mstsc desktop window, never a credentials dialog.

    ``candidates`` items contain: hwnd, title, class, visible, area, and
    ``owned`` for a window that belongs to another process but is owned by
    one of mstsc's (the credential prompt lives in CredentialUIBroker).
    This pure helper is intentionally testable because the real Windows window
    tree is difficult to reproduce in CI.

    Only a visible window with a size is ever chosen.  mstsc creates its
    desktop container before it connects and shows it only once the session
    is up; adopting it while hidden — which is what happened whenever a
    gateway session sat on a credential prompt — put a blank panel in the
    tab and reported it ready.
    """
    best_hwnd = None
    best_score = -1
    for item in candidates:
        hwnd = int(item.get("hwnd") or 0)
        if not hwnd:
            continue
        title = str(item.get("title") or "")
        cls = str(item.get("class") or "")
        if _is_probable_rdp_auth_dialog(title, cls):
            continue
        if item.get("owned") or not item.get("visible") or int(item.get("area") or 0) <= 0:
            continue
        if "tsc_popup_parent" in cls.lower():
            # The zero-size window mstsc owns its popups through.
            continue

        area = int(item.get("area") or 0)
        visible_bonus = 10_000_000 if item.get("visible") else 0
        c = cls.lower()
        t = title.lower()
        class_bonus = 0
        if "tscshellcontainer" in c:
            class_bonus += 50_000_000
        elif "uimainclass" in c or "mstsc" in c or "rdp" in c:
            class_bonus += 20_000_000
        elif c == "#32770":
            # Non-auth dialogs are still a poor embedding target.
            class_bonus -= 20_000_000
        title_bonus = 5_000_000 if "remote desktop" in t else 0
        score = area + visible_bonus + class_bonus + title_bonus
        if score > best_score:
            best_score = score
            best_hwnd = hwnd
    return best_hwnd


def _window_owner_pid(hwnd: int) -> int:
    """The process that owns the window this one is owned by, or 0."""
    try:
        owner = user32.GetWindow(ctypes.c_void_p(int(hwnd)), GW_OWNER)
        if not owner:
            return 0
        proc_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(owner), ctypes.byref(proc_id))
        return int(proc_id.value)
    except Exception:
        return 0


def _enum_windows_for_pid(pid: int) -> list[dict]:
    """Every top-level window that is mstsc's, or is owned by one of mstsc's.

    The second kind matters: on current Windows the "Windows Security"
    credential prompt is drawn by CredentialUIBroker.exe, a separate process,
    and is tied to mstsc only through window ownership.  Looking at mstsc's
    own windows alone, a session waiting on that prompt looked like a session
    with nothing on screen.
    """
    candidates: list[dict] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    rect = ctypes.wintypes.RECT()

    def _callback(hwnd, _lparam):
        proc_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        owned = False
        if proc_id.value != pid:
            if not user32.IsWindowVisible(hwnd) or _window_owner_pid(int(hwnd)) != pid:
                return True
            owned = True
        visible = bool(user32.IsWindowVisible(hwnd))
        if user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            w = max(0, rect.right - rect.left)
            h = max(0, rect.bottom - rect.top)
        else:
            w = h = 0
        candidates.append(
            {
                "hwnd": int(hwnd),
                "title": _window_title(int(hwnd)),
                "class": _window_class(int(hwnd)),
                "visible": visible,
                "area": w * h,
                "owned": owned,
            }
        )
        return True

    user32.EnumWindows(WNDENUMPROC(_callback), 0)
    return candidates


def _find_rdp_dialog_for_pid(pid: int) -> MstscDialog | None:
    """The dialog mstsc currently has up for this process, if any.

    A credential prompt or an error box wins over the progress box when both
    are showing, because that is the one that needs the user.
    """
    found: list[MstscDialog] = []
    for item in _enum_windows_for_pid(pid):
        if not item.get("visible"):
            continue
        dialog = _inspect_mstsc_dialog(
            int(item.get("hwnd") or 0), str(item.get("title") or ""),
            str(item.get("class") or ""))
        if dialog and dialog.hwnd:
            found.append(dialog)
    if not found:
        return None
    priority = {DIALOG_CREDENTIALS: 0, DIALOG_MESSAGE: 1, DIALOG_SECURITY_WARNING: 2,
                DIALOG_UNKNOWN: 3, DIALOG_PROGRESS: 4}
    found.sort(key=lambda d: priority.get(d.kind, 9))
    return found[0]


def _find_main_window_for_pid(pid: int) -> int | None:
    return _choose_best_mstsc_main_window(_enum_windows_for_pid(pid))

def _get_rax_control() -> tuple[object | None, str]:
    """Return (QAxWidget class, reason)."""
    def _prime_ax_dlls():
        candidates = []

        # Frozen onefile/onedir extraction base
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            base = Path(sys._MEIPASS)
            candidates.extend([
                base,
                base / "PyQt6" / "Qt6" / "bin",
            ])

        # Dev/venv runtime fallback
        try:
            import PyQt6

            candidates.append(Path(PyQt6.__file__).resolve().parent / "Qt6" / "bin")
        except Exception:
            pass

        for d in candidates:
            if not d.exists():
                continue
            try:
                os.add_dll_directory(str(d))
            except Exception:
                pass
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")

        # Explicitly preload Ax DLLs to avoid late loader failures.
        for d in candidates:
            for dll in ("Qt6AxBase.dll", "Qt6AxContainer.dll"):
                p = d / dll
                if p.exists():
                    try:
                        ctypes.WinDLL(str(p))
                    except Exception:
                        pass

    _prime_ax_dlls()

    try:
        from PyQt6.QAxContainer import QAxWidget
        return QAxWidget, "ok"
    except Exception as e:
        return None, f"qt_import_failed: {e}"


def _rdp_termsrv_target(host: str, port: int | str | None) -> str:
    host = (host or "").strip()
    if not host:
        return ""
    try:
        p = int(port) if port is not None else 3389
    except Exception:
        p = 3389
    if p != 3389:
        return f"TERMSRV/{host}:{p}"
    return f"TERMSRV/{host}"


def _suppress_rdp_security_warning():
    r"""Set registry key to suppress the 'Unknown remote connection' publisher warning.

    Writes HKCU\Software\Microsoft\Terminal Server Client\AuthenticationLevelOverride = 0
    which tells mstsc to skip the unsigned .rdp file trust check.
    """
    try:
        key = winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Terminal Server Client",
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "AuthenticationLevelOverride", 0, winreg.REG_DWORD, 0)
        winreg.CloseKey(key)
    except Exception:
        pass


def _auto_dismiss_rdp_security_warning(pid: int):
    """Spawn a background thread that watches for the mstsc security warning
    dialog belonging to the given PID, checks Clipboard, then clicks Connect.

    Must be called AFTER subprocess.Popen so the PID exists.
    """
    BM_SETCHECK = 0x00F1
    BM_CLICK    = 0x00F5
    BST_CHECKED = 0x0001
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _try_dismiss() -> bool:
        """Find the security dialog owned by our mstsc PID and auto-dismiss."""
        candidates: list[int] = []
        title_buf  = ctypes.create_unicode_buffer(512)
        proc_id    = ctypes.c_ulong()

        def _enum_top(hwnd, _lp):
            if not user32.IsWindowVisible(hwnd):
                return True
            # Get the PID that owns this window.
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
            if proc_id.value != pid:
                return True
            user32.GetWindowTextW(hwnd, title_buf, 512)
            title = title_buf.value.lower()
            # The dialog has several possible titles across Windows versions.
            # mstsc also keeps a 0x0 popup-parent window titled "Remote
            # Desktop Connection" — same title, no buttons — so every match
            # is kept and the one with a Connect button is the warning.
            if any(k in title for k in (
                "security warning", "remote desktop connection",
                "unknown remote", "caution"
            )):
                candidates.append(int(hwnd))
            return True

        user32.EnumWindows(WNDENUMPROC(_enum_top), 0)
        if not candidates:
            return False

        child_buf = ctypes.create_unicode_buffer(512)
        connect_hwnd   = None
        clipboard_hwnd = None
        other_hwnds    = []

        for found_hwnd in candidates:
            # Enumerate every child control.
            children: list[tuple[int, str]] = []

            def _enum_child(child_hwnd, _lp):
                user32.GetWindowTextW(child_hwnd, child_buf, 512)
                children.append((int(child_hwnd), child_buf.value))
                return True

            user32.EnumChildWindows(ctypes.c_void_p(found_hwnd), WNDENUMPROC(_enum_child), 0)

            connect_hwnd   = None
            clipboard_hwnd = None
            other_hwnds    = []
            for hwnd_val, text in children:
                t = text.lower().replace("&", "").strip()
                if "clipboard" in t:
                    clipboard_hwnd = hwnd_val
                elif any(k in t for k in ("smart cards", "webauthn", "printers")):
                    other_hwnds.append(hwnd_val)
                elif t == "connect":
                    connect_hwnd = hwnd_val
            if connect_hwnd:
                break
        if not connect_hwnd:
            return False

        # Check Clipboard.
        if clipboard_hwnd:
            user32.SendMessageW(ctypes.c_void_p(clipboard_hwnd), BM_SETCHECK, BST_CHECKED, 0)

        # Uncheck others.
        BST_UNCHECKED = 0x0000
        for hw in other_hwnds:
            user32.SendMessageW(ctypes.c_void_p(hw), BM_SETCHECK, BST_UNCHECKED, 0)

        # Click Connect.
        if connect_hwnd:
            user32.PostMessageW(ctypes.c_void_p(connect_hwnd), BM_CLICK, 0, 0)
            return True

        return False

    def _poll():
        # Give mstsc a moment to fully start before we start looking.
        time.sleep(1.0)
        # Then poll every 200 ms for up to 60 seconds.
        for _ in range(300):
            try:
                if _try_dismiss():
                    return
            except Exception:
                pass
            time.sleep(0.2)

    t = threading.Thread(target=_poll, daemon=True, name="rdp-dismiss")
    t.start()


class EmbeddedRDPWidget(QWidget):
    """Host an embedded RDP session using Microsoft ActiveX when available."""

    session_closed = pyqtSignal()
    resources_changed = pyqtSignal(dict)
    external_opened = pyqtSignal(object)

    def __init__(self, connection_info, parent=None):
        super().__init__(parent)
        self.connection_info = connection_info
        self._close_emitted = False
        # Stability mode: keep "embedded in app" UX by hosting mstsc window
        # inside the tab, while avoiding direct ActiveX crashes on some systems.
        self._force_compat_mode = True
        self.rdp_ax = None
        self._sending_secure_attention = False
        self._app_key_filter_installed = False
        self._fallback_mode = False
        self._fallback_reason = ""
        self._rdp_proc = None
        self._rdp_file_path = None
        self._embedded_hwnd = None
        self._attach_timer = QTimer(self)
        self._attach_timer.setInterval(200)
        self._attach_timer.timeout.connect(self._try_attach_mstsc_window)
        self._attach_attempts = 0
        self._fallback_resize_timer = QTimer(self)
        self._fallback_resize_timer.setInterval(500)
        self._fallback_resize_timer.timeout.connect(self._tick_fallback_resize)
        self._last_embed_size = (0, 0)
        self._last_fallback_desktop = (0, 0)
        # Session resolution follows a change to the Qt panel size. Native
        # pixels can change just from moving between displays with different
        # scaling; that only needs a placement repair, never a reconnect.
        self._last_fallback_canvas_size = (0, 0)
        self._last_fallback_canvas_dpr = None
        self._pending_fallback_canvas_size = None
        self._fallback_reconnect_timer = QTimer(self)
        self._fallback_reconnect_timer.setSingleShot(True)
        self._fallback_reconnect_timer.setInterval(700)
        self._fallback_reconnect_timer.timeout.connect(self._relaunch_fallback_for_resize)
        self._last_rdp_size = (0, 0)
        self._active_resize_timer = QTimer(self)
        self._active_resize_timer.setInterval(500)
        self._active_resize_timer.timeout.connect(self._tick_active_resize)
        
        # Coalesced "put Qt keyboard focus back on the RDP canvas" request.
        # Several triggers (tab shown, window activated, mstsc adopted) can ask
        # for this at once; a single-shot timer collapses them into one pass and
        # defers it until Qt has finished settling focus for the current event.
        self._focus_claim_timer = QTimer(self)
        self._focus_claim_timer.setSingleShot(True)
        self._focus_claim_timer.setInterval(0)
        self._focus_claim_timer.timeout.connect(self._claim_keyboard_focus)

        # Debounce timer for resize events
        self._sync_debounce_timer = QTimer(self)
        self._sync_debounce_timer.setSingleShot(True)
        self._sync_debounce_timer.setInterval(200)
        self._sync_debounce_timer.timeout.connect(self._sync_rdp_resolution)

        # Health monitor — detects when mstsc exits unexpectedly and auto-reconnects
        self._user_disconnected = False
        self._process_health_timer = QTimer(self)
        self._process_health_timer.setInterval(2000)
        self._process_health_timer.timeout.connect(self._check_process_health)

        # What was seeded for the launch in progress, and the last dialog mstsc
        # showed — so a message box is raised and logged once, not five times
        # a second for as long as it stays open, and so the status bar can say
        # what mstsc said rather than guess.
        self._credentials: RDPCredentialState | None = None
        self._last_dialog_hwnd: int | None = None
        self._last_dialog_kind: str | None = None
        self._last_dialog_text: str = ""
        # Whether the desktop has been embedded once (so the attach timer is a
        # watchdog now), and how many times reparenting the current window has
        # failed in a row.
        self._adopt_done = False
        self._adopt_failures = 0

        self._setup_ui()
        if self.connection_info.get("host"):
            QTimer.singleShot(0, self.connect_session)

    def _install_app_key_filter(self):
        if self._app_key_filter_installed:
            return
        app = QApplication.instance()
        if app is None:
            return
        app.installEventFilter(self)
        self._app_key_filter_installed = True

    def _remove_app_key_filter(self):
        if not self._app_key_filter_installed:
            return
        app = QApplication.instance()
        if app is not None:
            try:
                app.removeEventFilter(self)
            except Exception:
                pass
        self._app_key_filter_installed = False

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.status = QLabel("Preparing embedded RDP...")
        self.status.setStyleSheet("color: #94a3b8; font-size: 12px;")
        layout.addWidget(self.status)

        self.canvas = QFrame()
        self.canvas.setStyleSheet("background-color: #05070d; border: 1px solid rgba(148,163,184,0.2);")
        self.canvas.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        canvas_layout = QVBoxLayout(self.canvas)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.canvas, 1)

        btn_row = QHBoxLayout()
        self.btn_connect = QPushButton("Connect Embedded")
        self.btn_connect.clicked.connect(self.connect_session)
        btn_row.addWidget(self.btn_connect)

        self.btn_disconnect = QPushButton("Disconnect")
        self.btn_disconnect.clicked.connect(self.disconnect_session)
        self.btn_disconnect.setEnabled(False)
        btn_row.addWidget(self.btn_disconnect)

        self.btn_reconnect = QPushButton("Reconnect")
        self.btn_reconnect.clicked.connect(self.reconnect_session)
        self.btn_reconnect.setEnabled(True)
        btn_row.addWidget(self.btn_reconnect)

        self.btn_resources = QPushButton("Local Resources")
        self.btn_resources.setToolTip(
            "Choose which of this PC's devices the remote session can use.\n"
            "Saved per connection; changing it reconnects the session."
        )
        self.btn_resources.clicked.connect(self._edit_local_resources)
        btn_row.addWidget(self.btn_resources)

        self.chk_fit = QCheckBox("Fit to panel")
        self.chk_fit.setChecked(True)
        self.chk_fit.toggled.connect(self._apply_fit_mode)
        btn_row.addWidget(self.chk_fit)

        self.btn_fullscreen = QPushButton("Full Screen")
        self.btn_fullscreen.setCheckable(True)
        self.btn_fullscreen.clicked.connect(self._toggle_fullscreen)
        btn_row.addWidget(self.btn_fullscreen)

        self.btn_cad = QPushButton("Open External Window")
        self.btn_cad.clicked.connect(self._send_ctrl_alt_del)
        btn_row.addWidget(self.btn_cad)

        btn_row.addStretch()
        layout.addLayout(btn_row)

        supported, reason = embedded_rdp_supported()
        if self._force_compat_mode:
            supported = False
            reason = "forced-compat-mode"
        if not supported:
            self._fallback_mode = True
            self._fallback_reason = reason
            self._install_app_key_filter()
            self.status.setText(
                "Embedded RDP compatibility mode enabled (hosted mstsc window)."
            )
            self.btn_connect.setText("Connect Embedded (Compat)")
            self.btn_cad.setText("Open External Window")
            self.canvas.installEventFilter(self)
            self._apply_fit_mode()
            return

        QAxWidget, ax_reason = _get_rax_control()
        if not QAxWidget:
            self._fallback_mode = True
            self._fallback_reason = ax_reason
            self._install_app_key_filter()
            self.status.setText(
                "Embedded RDP compatibility mode enabled (hosted mstsc window)."
            )
            self.btn_connect.setText("Connect Embedded (Compat)")
            self.btn_cad.setText("Open External Window")
            self.canvas.installEventFilter(self)
            self._apply_fit_mode()
            return

        try:
            self.rdp_ax = QAxWidget(self.canvas)
            self.rdp_ax.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            control_ok = False
            for progid in ("MsTscAx.MsTscAx.9", "MsTscAx.MsTscAx.8", "MsTscAx.MsTscAx"):
                if self.rdp_ax.setControl(progid):
                    control_ok = True
                    break

            if not control_ok:
                self.rdp_ax = None
                self.status.setText(
                    "Embedded RDP unavailable: Microsoft RDP ActiveX exists but could not be initialized in embedded mode."
                )
                self.btn_connect.setEnabled(False)
                return

            canvas_layout.addWidget(self.rdp_ax)
            self.status.setText("Embedded RDP ready.")
            self.canvas.installEventFilter(self)
            self._apply_fit_mode()
        except Exception as e:
            self.rdp_ax = None
            self.status.setText(f"Embedded RDP init failed: {e}")
            self.btn_connect.setEnabled(False)

    def connect_session(self):
        if self._close_emitted:
            return
        if self._fallback_mode:
            self._connect_fallback_mstsc()
            return

        if not self.rdp_ax:
            return
        host = self.connection_info.get("host", "")
        user = self.connection_info.get("username", "")
        if not host:
            QMessageBox.warning(self, "RDP", "Host is required.")
            return
        try:
            # FIX: Use properties for better credibility and password support
            self.rdp_ax.setProperty("Server", host)
            if user:
                self.rdp_ax.setProperty("UserName", user)
            
            pw = self.connection_info.get("password", "")
            # The control takes the password directly (ClearTextPassword
            # below); nothing needs to go through Credential Manager for it.

            # FIX: Automatic login configuration
            adv = self.rdp_ax.querySubObject("AdvancedSettings7")
            if adv:
                adv.setProperty("EnableCredSspSupport", True)
                if pw:
                    adv.setProperty("ClearTextPassword", pw)
                adv.setProperty("AuthenticationLevel", 2)
                adv.setProperty("PromptForCredentials", False)
                adv.setProperty("EnableStatusPresenter", True)
                adv.setProperty("SmartSizing", True)
                # Route Windows key combos (including Ctrl+Alt+End) to remote.
                # 0=local, 1=remote, 2=remote only when full-screen.
                adv.setProperty("KeyboardHookMode", 1)

            self._apply_gateway_to_activex()

            # Explicitly set the initial resolution to match the canvas
            # Apply DPI scaling so wide monitors get the correct aspect ratio
            logical_w = max(64, self.canvas.width() - 4)
            logical_h = max(64, self.canvas.height() - 4)
            ratio = 1.0
            win = self.window().windowHandle() if self.window() else None
            if win and win.screen():
                ratio = win.screen().devicePixelRatio()
            else:
                screen = QGuiApplication.primaryScreen()
                ratio = screen.devicePixelRatio() if screen else 1.0
            w = max(64, int(round(logical_w * ratio)))
            h = max(64, int(round(logical_h * ratio)))
            self.rdp_ax.setProperty("DesktopWidth", w)
            self.rdp_ax.setProperty("DesktopHeight", h)
            self._last_rdp_size = (w, h)

            self._apply_fit_mode()
            self.rdp_ax.dynamicCall("Connect()")
            self._active_resize_timer.start()
            QTimer.singleShot(350, self._sync_rdp_resolution)
            self.status.setText(f"Connecting to {host}...")
            self.btn_connect.setEnabled(False)
            self.btn_disconnect.setEnabled(True)
        except Exception as e:
            QMessageBox.critical(self, "RDP", f"Embedded connect failed: {e}")

    def _apply_gateway_to_activex(self):
        """Point the ActiveX control at this connection's RD Gateway.

        The control keeps its gateway settings on a TransportSettings
        sub-object rather than on AdvancedSettings, and which version of that
        interface exists depends on the mstsc that shipped with the machine —
        so the newest is tried first and anything missing is skipped.  A
        control that cannot be told about the gateway is not worth failing the
        connection over; the compatibility path, which is what actually runs
        on most machines, writes the same settings into the .rdp file.
        """
        gateway = normalize_rdp_gateway(self.connection_info)
        if not gateway["host"] or not self.rdp_ax:
            return
        try:
            transport = None
            for interface in ("TransportSettings4", "TransportSettings3",
                              "TransportSettings2", "TransportSettings"):
                transport = self.rdp_ax.querySubObject(interface)
                if transport:
                    break
            if not transport:
                return
            from .rdp_gateway import (GATEWAY_CREDS_PASSWORD_NTLM, GATEWAY_USAGE_ALWAYS,
                                      GATEWAY_USAGE_IF_DIRECT_FAILS)

            transport.setProperty("GatewayHostname", gateway["host"])
            transport.setProperty(
                "GatewayUsageMethod",
                GATEWAY_USAGE_IF_DIRECT_FAILS if gateway["bypass_local"] else GATEWAY_USAGE_ALWAYS)
            transport.setProperty("GatewayProfileUsageMethod", 1)
            transport.setProperty("GatewayCredsSource", GATEWAY_CREDS_PASSWORD_NTLM)
            if gateway["username"]:
                transport.setProperty("GatewayUsername", gateway["username"])
            if gateway["password"]:
                transport.setProperty("GatewayPassword", gateway["password"])
            # The control's name for "use my RD Gateway credentials for the
            # remote computer": 1 means share the one set, 0 means ask twice.
            transport.setProperty(
                "GatewayCredSharing", 1 if gateway["use_for_remote"] else 0)
        except Exception:
            pass

    def disconnect_session(self, emit_close: bool = True):
        if self._fallback_mode:
            self._disconnect_fallback_mstsc()
            if emit_close and not self._close_emitted:
                self._close_emitted = True
                self.session_closed.emit()
            return

        if self.rdp_ax:
            self._active_resize_timer.stop()
            try:
                self.rdp_ax.dynamicCall("Disconnect()")
            except Exception:
                pass
        self.status.setText("Disconnected.")
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)
        if emit_close and not self._close_emitted:
            self._close_emitted = True
            self.session_closed.emit()

    def reconnect_session(self):
        self._close_emitted = False
        self.disconnect_session(emit_close=False)
        self.connect_session()

    def cleanup(self):
        self._close_emitted = True
        self._remove_app_key_filter()
        self.disconnect_session(emit_close=False)
        self._release_credentials()

    def suspend(self):
        """Stop all timers and disconnect RDP before system sleep."""
        self._attach_timer.stop()
        self._fallback_resize_timer.stop()
        self._fallback_reconnect_timer.stop()
        self._active_resize_timer.stop()
        self._sync_debounce_timer.stop()
        self._focus_claim_timer.stop()
        self._process_health_timer.stop()
        if not self._fallback_mode and self.rdp_ax:
            try:
                self.rdp_ax.dynamicCall("Disconnect()")
            except Exception:
                pass
        self.status.setText("Suspended")

    def resume(self):
        """Reconnect RDP after system wake."""
        if not self._close_emitted:
            self.reconnect_session()

    def closeEvent(self, event):
        self._remove_app_key_filter()
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self._fallback_mode:
            QTimer.singleShot(0, self._bring_rdp_auth_dialog_to_front)
            QTimer.singleShot(50, self._resize_embedded_window)
            # Becoming the current tab must also hand the keyboard back to the
            # session; nothing else will, because clicks go to mstsc's HWND.
            self._focus_claim_timer.start()

    def eventFilter(self, obj, event):
        # Clicking inside the hosted session activates this top-level window
        # (Windows routes activation to the child's root owner) even though Qt
        # never sees the click itself. That activation is our cue to take the
        # keyboard back.
        if (
            self._fallback_mode
            and event.type() == QEvent.Type.WindowActivate
            and isinstance(obj, QWidget)
            and obj.window() is self.window()
        ):
            self._focus_claim_timer.start()

        if (
            self._fallback_mode
            and not self._sending_secure_attention
            and event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease)
        ):
            if self._should_forward_fallback_key_event(obj):
                if self._forward_key_event_to_fallback(event):
                    return True

        if obj is self.canvas:
            if event.type() == QEvent.Type.Resize:
                if self._fallback_mode:
                    self._resize_embedded_window()
                    self._queue_fallback_resize()
                else:
                    if self.rdp_ax:
                        self.rdp_ax.setGeometry(0, 0, self.canvas.width(), self.canvas.height())
                    self._apply_fit_mode()
                    self._sync_debounce_timer.start()
            elif event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.FocusIn):
                self._focus_remote_input()
        return super().eventFilter(obj, event)

    def _should_forward_fallback_key_event(self, obj) -> bool:
        if not self._embedded_hwnd or not self.isVisible():
            return False
        if not self._rdp_proc or self._rdp_proc.poll() is not None:
            return False

        # Forward only while this launcher window is active.
        top = self.window()
        if not top or not top.isActiveWindow():
            return False

        # If focus is on a Qt widget in this subtree, we own the key event.
        fw = QApplication.focusWidget()
        if fw is not None and (fw is self or self.isAncestorOf(fw)):
            return True

        # No Qt widget holds focus at all, so Qt routes the key event to the
        # top-level window. Nothing else is going to consume it and the visible
        # embedded session is what the user is looking at, so it takes the key.
        if fw is None and obj is top:
            return True

        # Also allow direct events from this widget/canvas (or descendants).
        if obj is self or obj is self.canvas:
            return True
        if isinstance(obj, QWidget) and (obj is self or self.isAncestorOf(obj)):
            return True

        return False

    def _claim_keyboard_focus(self):
        """Move Qt keyboard focus onto the RDP canvas.

        Compat mode delivers keystrokes by forwarding Qt key events to the
        hosted mstsc window, and ``_should_forward_fallback_key_event()`` only
        allows that while focus sits inside this widget.  Clicks inside the
        session land on mstsc's own HWND — a child window owned by another
        process — so Qt never sees them and never moves focus here by itself.
        Without an explicit claim the session stays keyboard-dead until the user
        happens to click one of our Qt buttons, which is why pressing
        "Reconnect" looked like it was fixing the keyboard.
        """
        if not self._fallback_mode or not self.isVisible():
            return
        fw = QApplication.focusWidget()
        focus_is_own = fw is not None and (fw is self or self.isAncestorOf(fw))
        if not _should_claim_rdp_focus(fw, focus_is_own):
            return
        self.canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    def _focus_remote_input(self):
        if self._fallback_mode:
            # Claim Qt focus first: key forwarding is gated on it and the
            # auth-dialog branch below returns early. Setting the Qt focus
            # widget does not disturb a credential prompt, which is a separate
            # top-level window owned by mstsc.
            self._claim_keyboard_focus()
            # If mstsc is showing a credential/security/error dialog (common
            # after an incorrect password), bring that real top-level dialog
            # back instead of focusing a stale embedded HWND. This prevents the
            # tab from appearing as a blank/white canvas after the user clicks
            # away and returns.
            if self._bring_rdp_auth_dialog_to_front():
                return
            if not self._embedded_hwnd:
                return
            # Prefer the dedicated input-handler window — the same target the
            # key forwarder posts to — and fall back to the last child only when
            # mstsc has not created it yet.
            target = self._find_mstsc_input_hwnd()
            if not target:
                target = self._embedded_hwnd
                children = self._enum_child_windows(self._embedded_hwnd)
                if children:
                    target = children[-1]
            top = self.window()
            if top is not None:
                try:
                    top_h = ctypes.c_void_p(int(top.winId()))
                    user32.SetActiveWindow(top_h)
                except Exception:
                    pass
            h = ctypes.c_void_p(int(target))
            try:
                user32.SetFocus(h)
                user32.SetActiveWindow(h)
            except Exception:
                pass
            return

        if self.rdp_ax:
            self.rdp_ax.setFocus()

    def _restore_window_activation(self, adopted_hwnd: int):
        """Re-activate this app's window after adopting the mstsc window.

        mstsc takes the foreground when it launches; once its window is
        reparented into the canvas that activation is left dangling, so the
        session would sit there ignoring the keyboard until the user clicked
        something.  Only reclaim the foreground when it still belongs to us or
        to the window we just adopted — never steal it from an application the
        user switched to while the session was connecting.
        """
        if not self.isVisible():
            return
        try:
            fg = int(user32.GetForegroundWindow() or 0)
            if fg:
                fg_pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), ctypes.byref(fg_pid))
                own_pid = int(kernel32.GetCurrentProcessId() or 0)
                is_adopted_tree = fg == int(adopted_hwnd) or bool(
                    user32.IsChild(ctypes.c_void_p(int(adopted_hwnd)), ctypes.c_void_p(fg))
                )
                if int(fg_pid.value) != own_pid and not is_adopted_tree:
                    return
            top = self.window()
            if top is not None:
                top.activateWindow()
        except Exception:
            pass

    def _current_mstsc_dialog(self) -> MstscDialog | None:
        """Whatever dialog the hosted mstsc has up right now, or None."""
        if not self._fallback_mode or not self._rdp_proc or self._rdp_proc.poll() is not None:
            return None
        try:
            return _find_rdp_dialog_for_pid(int(self._rdp_proc.pid))
        except Exception:
            return None

    def _rdp_auth_dialog_hwnd(self) -> int | None:
        dialog = self._current_mstsc_dialog()
        return dialog.hwnd if dialog else None

    def _describe_dialog(self, dialog: MstscDialog) -> str:
        """The status line for a dialog, in words that lead somewhere."""
        gateway = normalize_rdp_gateway(self.connection_info)
        if dialog.kind == DIALOG_PROGRESS:
            return describe_gateway_route(gateway, self.connection_info.get("host", ""))
        if dialog.kind == DIALOG_CREDENTIALS:
            where = ""
            if gateway["host"] and self._credentials and not self._credentials.gateway_ready:
                where = f" for RD Gateway {gateway['host']}"
            return (f"Windows is asking for credentials{where}. Complete the prompt, "
                    "or click Reconnect to start over.")
        if dialog.kind == DIALOG_SECURITY_WARNING:
            return "mstsc is asking whether to trust the connection — answer its prompt."
        if dialog.kind == DIALOG_MESSAGE:
            explained = explain_mstsc_message(dialog.text)
            if not explained:
                return "mstsc reported a problem — see its dialog, then click Reconnect."
            who = "RD Gateway" if (gateway["host"] and is_gateway_message(dialog.text)) \
                else "Connection"
            return f"{who}: {explained}"
        return "mstsc has a dialog open — answer it, or click Reconnect to start over."

    def _note_mstsc_dialog(self, dialog: MstscDialog) -> bool:
        """Record a dialog and say what it is; True the first time it is seen.

        A progress box is noted quietly.  A prompt, a warning or an error is
        logged with its text — the text is the diagnosis for a gateway that
        does not connect, and nothing else records it.
        """
        first_time = (dialog.hwnd != self._last_dialog_hwnd
                      or dialog.kind != self._last_dialog_kind
                      or (dialog.kind == DIALOG_MESSAGE and dialog.text != self._last_dialog_text))
        self._last_dialog_hwnd = dialog.hwnd
        self._last_dialog_kind = dialog.kind
        self._last_dialog_text = dialog.text
        if first_time:
            level = logging.WARNING if dialog.kind == DIALOG_MESSAGE else logging.INFO
            _LOG.log(level, "mstsc dialog | kind=%s title=%r text=%r host=%s gateway=%s",
                     dialog.kind, dialog.title, dialog.text,
                     self.connection_info.get("host", ""),
                     normalize_rdp_gateway(self.connection_info)["host"] or "none")
        self.status.setText(self._describe_dialog(dialog))
        self.btn_connect.setEnabled(False)
        self.btn_disconnect.setEnabled(True)
        return first_time

    def _raise_dialog(self, dialog: MstscDialog):
        try:
            h = ctypes.c_void_p(int(dialog.hwnd))
            user32.ShowWindow(h, 5)  # SW_SHOW
            user32.BringWindowToTop(h)
            user32.SetForegroundWindow(h)
        except Exception:
            pass

    def _bring_rdp_auth_dialog_to_front(self) -> bool:
        """Bring whatever mstsc is asking back in front of the app.

        Called when the tab is shown or clicked: a prompt that went behind the
        main window looks exactly like a session that hung.  The progress box
        is left alone — it needs nothing from the user, and pulling it forward
        while they read something else is not help.
        """
        dialog = self._current_mstsc_dialog()
        if not dialog:
            return False
        self._note_mstsc_dialog(dialog)
        if dialog.kind != DIALOG_PROGRESS:
            self._raise_dialog(dialog)
        return True

    def _force_focus_embedded_target(self, diag: list[str] | None = None) -> bool:
        if diag is None:
            diag = []
        if not self._fallback_mode or not self._embedded_hwnd:
            diag.append("force_focus skipped: not fallback/no embedded hwnd")
            return False
        try:
            target = int(self._embedded_hwnd)
            children = self._enum_child_windows(target)
            if children:
                target = int(children[-1])

            fg = int(user32.GetForegroundWindow() or 0)
            cur_tid = int(kernel32.GetCurrentThreadId() or 0)
            tgt_tid = int(user32.GetWindowThreadProcessId(ctypes.c_void_p(target), None) or 0)
            fg_tid = int(user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), None) or 0) if fg else 0
            root = int(user32.GetAncestor(ctypes.c_void_p(target), GA_ROOT) or 0)

            diag.append(
                f"force_focus target=0x{target:08X} root=0x{root:08X} cur_tid={cur_tid} tgt_tid={tgt_tid} fg_tid={fg_tid}"
            )

            attached_cur = False
            attached_fg = False
            if cur_tid and tgt_tid and cur_tid != tgt_tid:
                attached_cur = bool(user32.AttachThreadInput(cur_tid, tgt_tid, True))
                diag.append(f"AttachThreadInput(cur->target)={attached_cur}")
            if fg_tid and tgt_tid and fg_tid != tgt_tid:
                attached_fg = bool(user32.AttachThreadInput(fg_tid, tgt_tid, True))
                diag.append(f"AttachThreadInput(fg->target)={attached_fg}")

            try:
                if root:
                    sfw = bool(user32.SetForegroundWindow(ctypes.c_void_p(root)))
                    diag.append(f"SetForegroundWindow(root)={sfw}")
                    btt = bool(user32.BringWindowToTop(ctypes.c_void_p(root)))
                    diag.append(f"BringWindowToTop(root)={btt}")
                sfo = bool(user32.SetFocus(ctypes.c_void_p(target)))
                diag.append(f"SetFocus(target)={sfo}")
                saw = bool(user32.SetActiveWindow(ctypes.c_void_p(target)))
                diag.append(f"SetActiveWindow(target)={saw}")
            finally:
                if attached_fg:
                    user32.AttachThreadInput(fg_tid, tgt_tid, False)
                if attached_cur:
                    user32.AttachThreadInput(cur_tid, tgt_tid, False)

            return True
        except Exception as e:
            diag.append(f"force_focus exception: {e}")
            return False

    def _sync_toggle_key_states(self):
        """Synchronize host NumLock / CapsLock / ScrollLock state into the embedded session.

        When mstsc is first embedded, its internal toggle-key state may differ from the
        host.  Sending a synthetic down+up for each key that is currently ON on the host
        brings the remote into alignment before the user starts typing.
        """
        if not self._embedded_hwnd:
            return
        target = self._find_mstsc_input_hwnd() or self._embedded_hwnd
        h = ctypes.c_void_p(int(target))
        for vk in (VK_NUMLOCK, VK_CAPITAL, VK_SCROLL):
            # GetKeyState low-order bit == 1 means the key is toggled ON.
            if user32.GetKeyState(vk) & 0x0001:
                sc = int(user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) or 0)
                lp_down = 1 | ((sc & 0xFF) << 16)
                lp_up = lp_down | (1 << 30) | (1 << 31)
                user32.PostMessageW(h, WM_KEYDOWN, vk, lp_down)
                user32.PostMessageW(h, WM_KEYUP, vk, lp_up)

    def _find_mstsc_input_hwnd(self) -> int | None:
        """Return the IHWindowClass input-handler window inside the embedded mstsc tree.

        mstsc routes keyboard input through a dedicated IHWindowClass child window.
        Sending key messages only to that window (rather than to every window in the
        hierarchy) prevents duplicate key delivery and toggle-key state corruption.
        """
        if not self._embedded_hwnd:
            return None
        found: list[int] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        buf = ctypes.create_unicode_buffer(256)

        def _cb(hwnd, _):
            user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 256)
            if buf.value == "IHWindowClass":
                found.append(int(hwnd))
            return True

        user32.EnumChildWindows(ctypes.c_void_p(self._embedded_hwnd), WNDENUMPROC(_cb), 0)
        return found[0] if found else None

    def _forward_key_event_to_fallback(self, event):
        if not self._embedded_hwnd:
            return False

        # Only redirect Win32 focus when it is not already inside the embedded
        # window tree.  Calling SetFocus/SetActiveWindow on every keystroke
        # thrashes the focus state and can cause the Qt top-level to lose its
        # "active" status, making _should_forward_fallback_key_event() drop the
        # very next event.
        cur_focus = int(user32.GetFocus() or 0)
        if cur_focus == 0 or not bool(
            user32.IsChild(ctypes.c_void_p(self._embedded_hwnd), ctypes.c_void_p(cur_focus))
        ):
            self._focus_remote_input()

        vk = int(getattr(event, "nativeVirtualKey", lambda: 0)() or 0)
        if vk <= 0:
            return False

        is_alt = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier) or vk == VK_MENU
        down_msg = WM_SYSKEYDOWN if is_alt else WM_KEYDOWN
        up_msg = WM_SYSKEYUP if is_alt else WM_KEYUP
        msg = down_msg if event.type() == QEvent.Type.KeyPress else up_msg

        # Build lParam per WM_KEYDOWN / WM_KEYUP specification:
        #  bits  0-15: repeat count (must be 1, not 0)
        #  bits 16-23: OEM scan code
        #  bit     24: extended-key flag (right-side modifiers, nav cluster, numpad /)
        #  bit     29: context code (1 if Alt is held)
        #  bit     30: previous key-state (1 on key-up / auto-repeat down)
        #  bit     31: transition state (1 on key-up)
        sc = int(getattr(event, "nativeScanCode", lambda: 0)() or 0)
        if not sc:
            sc = int(user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) or 0)
        lparam = 1 | ((sc & 0xFF) << 16)
        if vk in _EXTENDED_VK:
            lparam |= 1 << 24
        if is_alt:
            lparam |= 1 << 29
        if event.type() == QEvent.Type.KeyRelease:
            lparam |= (1 << 30) | (1 << 31)

        # Send only to the dedicated mstsc input window (IHWindowClass).
        # Sending to every child window in the hierarchy multiplies each
        # keystroke N times and causes toggle-key corruption (NumLock, CapsLock).
        target = self._find_mstsc_input_hwnd() or self._embedded_hwnd
        user32.PostMessageW(ctypes.c_void_p(int(target)), msg, vk, lparam)
        return True

    def _sync_rdp_resolution(self):
        """Update the RDP session resolution to match the current canvas size."""
        if not self.rdp_ax or self._fallback_mode:
            return
            
        # Matches the initial connect logic: container minus small buffer
        logical_w = max(64, self.canvas.width() - 4)
        logical_h = max(64, self.canvas.height() - 4)

        # Use the widget's screen when available so HiDPI scaling is correct.
        ratio = 1.0
        win = self.window().windowHandle() if self.window() else None
        if win and win.screen():
            ratio = win.screen().devicePixelRatio()
        else:
            screen = QGuiApplication.primaryScreen()
            ratio = screen.devicePixelRatio() if screen else 1.0

        w = max(64, int(round(logical_w * ratio)))
        h = max(64, int(round(logical_h * ratio)))
        if (w, h) == self._last_rdp_size:
            return
        
        scale = int(ratio * 100)

        try:
            self.rdp_ax.setProperty("DesktopWidth", w)
            self.rdp_ax.setProperty("DesktopHeight", h)
            # We rely on SmartSizing for the final fit, but we update the
            # backing resolution so text/UI elements are the correct size
            # for this window.
            self.rdp_ax.dynamicCall("UpdateSessionDisplaySettings(uint, uint, uint, uint, uint, uint, uint)",
                                   w, h, w, h, 0, scale, scale)
            self._last_rdp_size = (w, h)
        except Exception:
            pass

    def _tick_active_resize(self):
        """Backstop resize sync for ActiveX sessions when Qt resize events are missed."""
        if not self.rdp_ax or self._fallback_mode:
            self._active_resize_timer.stop()
            return
        self._sync_rdp_resolution()

    def _toggle_fullscreen(self):
        top = self.window()
        if self.btn_fullscreen.isChecked():
            top.showFullScreen()
            self.btn_fullscreen.setText("Exit Full Screen")
        else:
            top.showNormal()
            self.btn_fullscreen.setText("Full Screen")
        
        # Trigger resolution sync after transition
        QTimer.singleShot(250, self._sync_rdp_resolution)
        # Also force-resize the fallback hosted window after transition
        QTimer.singleShot(300, self._resize_embedded_window)
        QTimer.singleShot(800, self._resize_embedded_window)
        if self._fallback_mode and self._rdp_proc and self._rdp_proc.poll() is None:
            # Fullscreen transitions can change the final canvas size after Qt's
            # first resize event. Schedule a relaunch pass to keep desktop size
            # aligned with the visible panel and eliminate letterboxing.
            QTimer.singleShot(900, self._relaunch_fallback_for_resize)

    def _apply_fit_mode(self):
        fit_enabled = getattr(self, "chk_fit", None) and self.chk_fit.isChecked()

        if self._fallback_mode:
            if fit_enabled:
                self._resize_embedded_window()
            return

        if not self.rdp_ax:
            return

        # Native ActiveX: SmartSizing controls whether remote desktop scales
        # to fit the hosting widget while connected.
        try:
            adv = self.rdp_ax.querySubObject("AdvancedSettings7")
            if adv is not None:
                adv.setProperty("SmartSizing", bool(fit_enabled))
        except Exception:
            pass

    def _local_resources(self, force_prompt: bool = False) -> dict | None:
        """Resolve this connection's local-resource choice, prompting on first use."""
        return _resolve_local_resources(
            self,
            self.connection_info,
            self.resources_changed.emit,
            force_prompt=force_prompt,
        )

    def _edit_local_resources(self):
        """Let the user revisit the choice; reconnect so mstsc picks it up."""
        if self._local_resources(force_prompt=True) is None:
            return
        if self._rdp_proc and self._rdp_proc.poll() is None:
            self.status.setText("Applying local resource changes...")
            self.reconnect_session()
        else:
            self.status.setText("Local resources saved. Connect to apply them.")

    def _connect_fallback_mstsc(self):
        # First use of this connection asks what the session may reach on this
        # PC; every later connect reuses the stored answer without prompting.
        if self._local_resources() is None:
            self.status.setText("Connection cancelled — no local resources chosen.")
            self.btn_connect.setEnabled(True)
            self.btn_disconnect.setEnabled(False)
            return
        self._launch_fallback_mstsc(None, None, reconnecting=False)

    def _launch_fallback_mstsc(self, width: int | None, height: int | None, reconnecting: bool):
        host = self.connection_info.get("host", "")
        port = self.connection_info.get("port", 3389)
        user = self.connection_info.get("username", "")
        pw = self.connection_info.get("password", "")
        if not host:
            QMessageBox.warning(self, "RDP", "Host is required.")
            return

        try:
            # Match remote desktop dimensions to the embedded canvas. Prefer a
            # DPI-aware target to avoid under-sized sessions that leave bars.
            if width is None or height is None:
                width, height = self._desired_fallback_desktop_size()
            else:
                width = max(640, int(width))
                height = max(480, int(height))
            fit_enabled = bool(self.chk_fit.isChecked()) if hasattr(self, "chk_fit") else True
            # Seed once per launch; a resize relaunch reuses what was stored.
            if self._credentials is None or not reconnecting:
                if self._credentials is not None:
                    self._credentials.release()
                self._credentials = prepare_rdp_credentials(self.connection_info)

            lines = build_rdp_session_lines(self.connection_info, self._credentials)
            lines.extend([
                "screen mode id:i:1",
                f"desktopwidth:i:{width}",
                f"desktopheight:i:{height}",
                "dynamic resolution:i:1",
                "session bpp:i:32",
                "use multimon:i:0",
                f"smart sizing:i:{1 if fit_enabled else 0}",
            ])
            rdp_path = write_rdp_file(lines)

            self._rdp_file_path = rdp_path
            self._last_fallback_desktop = (width, height)
            self._last_fallback_canvas_size = self._logical_canvas_size()
            self._last_fallback_canvas_dpr = float(self.canvas.devicePixelRatioF())
            self._pending_fallback_canvas_size = None
            self._user_disconnected = False
            self._last_dialog_hwnd = None
            self._last_dialog_kind = None
            self._rdp_proc = subprocess.Popen(["mstsc", rdp_path])
            # Its events in Windows' RDP client log carry this pid: how it ended is read there.
            self._rdp_launch = (self._rdp_proc.pid, time.time())
            _log_rdp_launch(self.connection_info, "embedded-compat", rdp_path, self._rdp_proc.pid)
            _auto_dismiss_rdp_security_warning(self._rdp_proc.pid)
            self._attach_attempts = 0
            self._adopt_done = False
            self._adopt_failures = 0
            self._attach_timer.setInterval(200)
            self._attach_timer.start()
            if reconnecting:
                self.status.setText(
                    f"Resizing embedded compatibility session to {width}x{height}..."
                )
            else:
                status = describe_gateway_route(self.connection_info, host)
                if self._credentials.warnings:
                    status += "  " + self._credentials.warnings[0]
                self.status.setText(status)
            self.btn_connect.setEnabled(False)
            self.btn_disconnect.setEnabled(True)
        except Exception as e:
            _LOG.exception("mstsc launch failed | host=%s", host)
            QMessageBox.critical(self, "RDP", f"Failed to launch mstsc: {e}")

    def _relaunch_fallback_for_resize(self):
        """Hosted mstsc cannot always live-resize; relaunch at new desktop size."""
        self._pending_fallback_canvas_size = None
        if not self._fallback_mode:
            return
        if not self._rdp_proc or self._rdp_proc.poll() is not None:
            return
        if not self.canvas.isVisible() or self.window().isMinimized():
            return
        if self._accept_fallback_dpi_change() or not self._fallback_canvas_resized():
            # A queued resize can be cancelled by returning to the original
            # size, or reached by an adoption/fullscreen/DPI placement repair.
            return
        if self._rdp_auth_dialog_hwnd():
            # Never terminate/relaunch mstsc while Windows is asking for
            # credentials. A tab switch or resize during a bad-password prompt
            # used to kill the prompt and leave the embedded panel blank.
            return

        fit_enabled = bool(self.chk_fit.isChecked()) if hasattr(self, "chk_fit") else True
        if not fit_enabled:
            return

        new_w, new_h = self._desired_fallback_desktop_size()
        old_w, old_h = self._last_fallback_desktop

        # Relaunch for both grow and shrink when the size change is meaningful.
        # This keeps remote desktop dimensions aligned with the visible panel.
        dw = abs(new_w - old_w)
        dh = abs(new_h - old_h)
        if dw < 20 and dh < 20:
            return

        self._disconnect_fallback_mstsc()
        self._launch_fallback_mstsc(new_w, new_h, reconnecting=True)

    def _logical_canvas_size(self) -> tuple[int, int]:
        return self.canvas.width(), self.canvas.height()

    def _fallback_canvas_resized(self) -> bool:
        if self._last_fallback_canvas_size == (0, 0):
            return False
        width, height = self._logical_canvas_size()
        old_width, old_height = self._last_fallback_canvas_size
        # Ignore the one logical pixel of rounding a DPI transition can leave.
        return abs(width - old_width) > 1 or abs(height - old_height) > 1

    def _accept_fallback_dpi_change(self) -> bool:
        """Treat Qt's DPI-driven geometry adjustment as placement, not resize."""
        ratio = float(self.canvas.devicePixelRatioF())
        previous = self._last_fallback_canvas_dpr
        self._last_fallback_canvas_dpr = ratio
        if previous is None or ratio == previous:
            return False
        self._last_fallback_canvas_size = self._logical_canvas_size()
        self._pending_fallback_canvas_size = None
        self._fallback_reconnect_timer.stop()
        return True

    def _queue_fallback_resize(self):
        """Coalesce real panel resizes, ignoring movement and DPI-only changes."""
        current = self._logical_canvas_size()
        if (self._accept_fallback_dpi_change() or not self._fallback_canvas_resized()
                or self.window().isMinimized()):
            self._fallback_reconnect_timer.stop()
            self._pending_fallback_canvas_size = None
            return
        if not self._rdp_proc or self._rdp_proc.poll() is not None:
            return
        if not self.chk_fit.isChecked() or self._rdp_auth_dialog_hwnd():
            return
        if current != self._pending_fallback_canvas_size:
            self._pending_fallback_canvas_size = current
            self._fallback_reconnect_timer.start()

    def _desired_fallback_desktop_size(self) -> tuple[int, int]:
        """Use the same native pixels for the desktop and its hosting window."""
        width, height = self._native_canvas_size()
        return max(640, width), max(480, height)

    def _native_canvas_size(self) -> tuple[int, int]:
        """Measure in the Win32 coordinate space used by SetWindowPos.

        Qt widths/heights are device-independent pixels. Passing them directly
        to user32 leaves mstsc occupying only part of a 125/150/200% panel.
        Measuring the native client area also follows monitor/DPI changes
        that do not produce a change to the Qt logical size.
        """
        rect = ctypes.wintypes.RECT()
        if user32.GetClientRect(ctypes.c_void_p(int(self.canvas.winId())),
                                ctypes.byref(rect)):
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w > 0 and h > 0:
                return w, h
        ratio = float(self.canvas.devicePixelRatioF())
        return (max(1, round(self.canvas.width() * ratio)),
                max(1, round(self.canvas.height() * ratio)))

    def _window_fills_parent(self, hwnd: int, parent_hwnd: int,
                             size: tuple[int, int]) -> bool:
        """Check actual bounds, including the offset, rather than our last request."""
        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
            return False
        user32.MapWindowPoints(None, ctypes.c_void_p(parent_hwnd),
                               ctypes.byref(rect), 2)
        return (rect.left, rect.top, rect.right, rect.bottom) == (0, 0, *size)

    def _try_attach_mstsc_window(self):
        if not self._rdp_proc:
            self._attach_timer.stop()
            return

        if self._rdp_proc.poll() is not None:
            self._attach_timer.stop()
            self._fallback_resize_timer.stop()
            self.status.setText(self._how_it_ended() or "Embedded compatibility session ended.")
            self.btn_connect.setEnabled(True)
            self.btn_disconnect.setEnabled(True)
            self._rdp_proc = None
            self._embedded_hwnd = None
            return

        dialog = self._current_mstsc_dialog()
        if dialog:
            # Never adopt a dialog into the canvas; keep polling for the real
            # desktop window.  Say what the dialog is — through a gateway the
            # progress box alone can sit there for ten seconds, and an error
            # box behind the app is the difference between "it hung" and
            # "the gateway's certificate is not trusted".
            self._attach_attempts = 0
            first_time = self._note_mstsc_dialog(dialog)
            if first_time and dialog.kind != DIALOG_PROGRESS:
                # Raise it once, when it appears. Every 200 ms would fight the
                # user for the foreground while they type into it.
                if self.window() and self.window().isActiveWindow():
                    self._raise_dialog(dialog)
            return
        self._last_dialog_hwnd = None
        self._last_dialog_kind = None

        # A qualifying top-level mstsc window means one of: the desktop has
        # just appeared; mstsc re-created it after connecting; or our last
        # reparent did not hold.  A window we already embedded is a *child* of
        # the canvas, and EnumWindows never returns children — so once a
        # reparent takes, this goes quiet on its own, and if mstsc lets the
        # window float free again it shows back up here to be re-adopted.
        hwnd = _find_main_window_for_pid(self._rdp_proc.pid)
        if hwnd:
            if self._adopt_window(hwnd):
                self._embedded_hwnd = hwnd
                self._adopt_failures = 0
                self._on_desktop_embedded(hwnd)
            else:
                self._embedded_hwnd = hwnd
                self._adopt_failures += 1
                if self._adopt_failures == 1:
                    self.status.setText("Docking the remote session into the tab...")
                elif self._adopt_failures == 4:
                    # Past the transient window; back off so a genuinely stuck
                    # reparent does not strip and restore the frame several
                    # times a second.
                    self._attach_timer.setInterval(800)
                if self._adopt_failures > 30:
                    self._attach_timer.stop()
                    _LOG.error("gave up docking mstsc | hwnd=%s host=%s",
                               hwnd, self.connection_info.get("host", ""))
                    self.status.setText(
                        "The remote session opened in its own window — this PC "
                        "would not dock it into the tab. It is still connected; "
                        "use it there, or click Reconnect to try docking again.")
            return

        # No stray top-level window.  Either the desktop is not up yet, or it
        # is already embedded and the timer is just watching.
        if self._adopt_done:
            return
        self._attach_attempts += 1
        if self._attach_attempts > 150:
            self._attach_timer.stop()
            self.status.setText(
                "Could not embed mstsc window automatically. Session is running externally."
            )

    def _on_desktop_embedded(self, hwnd: int):
        """Settle a freshly embedded mstsc desktop into the canvas.

        Runs every time a window is (re-)adopted so sizing and focus are
        re-established after a window swap; the one-time layout/relaunch/focus
        volley and the switch to a slow watchdog interval happen only once.
        """
        self._restore_window_activation(hwnd)
        self._resize_embedded_window()
        if not self._fallback_resize_timer.isActive():
            self._fallback_resize_timer.start()
        if not self._process_health_timer.isActive():
            self._process_health_timer.start()
        self.status.setText("Embedded RDP ready (compatibility host mode).")
        if self._adopt_done:
            return
        self._adopt_done = True
        # Keep the timer alive as a light watchdog rather than stopping it, so
        # a window mstsc later floats free is pulled back in.
        self._attach_timer.setInterval(600)
        _LOG.info("mstsc desktop adopted | hwnd=%s host=%s", hwnd,
                  self.connection_info.get("host", ""))
        # mstsc rebuilds its internal layout when the RDP session connects.
        for delay in (500, 1500, 3000):
            QTimer.singleShot(delay, self._resize_embedded_window)
        # If the initial launch happened before final layout sizing, relaunch
        # once at the settled canvas size so the remote desktop fills the panel.
        for delay in (900, 2200):
            QTimer.singleShot(delay, self._relaunch_fallback_for_resize)
        # mstsc creates its input-handler window a beat after the desktop
        # window appears, and the Qt top-level is usually still inactive right
        # after SetParent, so one attempt is not enough.
        for delay in (80, 400, 1200, 2500):
            QTimer.singleShot(delay, self._focus_remote_input)
        # Sync host NumLock/CapsLock/ScrollLock into the embedded session once
        # mstsc has finished its own RDP keyboard-state handshake.
        QTimer.singleShot(1200, self._sync_toggle_key_states)

    def _adopt_window(self, hwnd: int) -> bool:
        """Reparent an mstsc top-level window into the canvas.

        Returns True only when the window is verifiably a child of the canvas
        afterwards.  Cross-process ``SetParent`` can fail silently — it
        returns NULL and leaves the window a top-level — and the old code
        stripped the caption and moved the window to (0,0) at canvas size
        regardless of whether the reparent took.  On the failure path that
        produced exactly what a gateway session showed: a frameless mstsc
        window floating at the screen origin with a blank tab behind it.
        Verifying, and reporting the failure, is what lets the watchdog try
        again instead of declaring success.

        ``WS_CHILD`` is set *before* ``SetParent`` because a window still
        carrying ``WS_POPUP`` can refuse the new parent.
        """
        canvas_hwnd = int(self.canvas.winId())
        h = ctypes.c_void_p(int(hwnd))
        # WS_POPUP sets the sign bit of a 32-bit style. Normalize before bit
        # operations so Python's sign extension cannot overflow ctypes when
        # we clear it and add WS_CHILD on a 64-bit host.
        original_style = user32.GetWindowLongPtrW(h, GWL_STYLE) & 0xFFFFFFFF
        style = original_style
        style &= ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU)
        style |= (WS_CHILD | WS_VISIBLE)
        user32.SetWindowLongPtrW(h, GWL_STYLE, style)

        user32.SetParent(h, ctypes.c_void_p(canvas_hwnd))
        err = kernel32.GetLastError()
        parent = user32.GetParent(h)
        if int(parent or 0) != canvas_hwnd:
            # The reparent did not take. Put the window's frame back so it is
            # left as a normal, usable top-level window rather than the
            # frameless WS_CHILD-with-no-parent blob at the screen origin that
            # the old code produced on this path.
            user32.SetWindowLongPtrW(h, GWL_STYLE, original_style)
            user32.SetWindowPos(
                h, 0, 0, 0, 0, 0,
                SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
                | 0x0001 | 0x0002)  # SWP_NOSIZE | SWP_NOMOVE
            _LOG.warning(
                "SetParent did not take | hwnd=%s class=%r lasterror=%s "
                "parent=%s canvas=%s", hwnd, _window_class(hwnd), err,
                int(parent or 0), canvas_hwnd)
            return False

        native_w, native_h = self._native_canvas_size()
        user32.SetWindowPos(
            h, 0, 0, 0,
            native_w,
            native_h,
            SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
        return True

    def _resize_embedded_window(self):
        if not self._embedded_hwnd:
            return
        canvas_hwnd = int(self.canvas.winId())
        # An unsuccessful adoption must never move an external window to the
        # screen origin. The attach watchdog owns retries on that path.
        if int(user32.GetParent(ctypes.c_void_p(self._embedded_hwnd)) or 0) != canvas_hwnd:
            return
        w, h = self._native_canvas_size()
        # Keep resize lightweight; aggressive invalidation causes repaint storms
        # with native hosted mstsc and can make the Qt UI appear to flicker/hang.
        resized = user32.SetWindowPos(
            ctypes.c_void_p(self._embedded_hwnd),
            0,
            0,
            0,
            w,
            h,
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
        if not resized:
            _LOG.warning("mstsc resize failed | hwnd=%s target=%sx%s error=%s",
                         self._embedded_hwnd, w, h, kernel32.GetLastError())
            return
        self._last_embed_size = (w, h)
        self._force_fill_window_tree(self._embedded_hwnd)

    def _enum_child_windows(self, parent_hwnd: int) -> list[int]:
        children: list[int] = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _callback(hwnd, _lparam):
            children.append(int(hwnd))
            return True

        user32.EnumChildWindows(ctypes.c_void_p(parent_hwnd), WNDENUMPROC(_callback), 0)
        return children

    def _force_fill_window_tree(self, root_hwnd: int):
        """Repair the desktop surfaces, leaving dialogs and scrollbars alone.

        EnumChildWindows includes *all descendants*, not just direct children.
        Moving each one relative to every ancestor repeatedly could shrink
        the display surface again as mstsc rebuilt its layout after login.
        """
        surface_classes = {
            "tscshellcontainerclass", "tscuicontainerclass", "uimainclass",
            "uicontainerclass", "opcontainerclass", "opwindowclass",
            "ihwindowclass",
        }
        stack = [int(root_hwnd)]
        seen: set[int] = set()

        while stack:
            parent = stack.pop()
            if parent in seen:
                continue
            seen.add(parent)

            rect = ctypes.wintypes.RECT()
            if user32.GetClientRect(ctypes.c_void_p(parent), ctypes.byref(rect)):
                pw = max(1, rect.right - rect.left)
                ph = max(1, rect.bottom - rect.top)
            else:
                continue

            for child in self._enum_child_windows(parent):
                if int(user32.GetParent(ctypes.c_void_p(child)) or 0) != parent:
                    continue
                if _window_class(child).lower() not in surface_classes:
                    continue
                if not self._window_fills_parent(child, parent, (pw, ph)):
                    user32.MoveWindow(ctypes.c_void_p(child), 0, 0, pw, ph, True)
                stack.append(child)

    def _tick_fallback_resize(self):
        """Backstop resize sync for hosted mstsc when Qt resize events are missed."""
        if not self._embedded_hwnd or not self._rdp_proc or self._rdp_proc.poll() is not None:
            self._fallback_resize_timer.stop()
            return
        canvas_hwnd = int(self.canvas.winId())
        if int(user32.GetParent(ctypes.c_void_p(self._embedded_hwnd)) or 0) != canvas_hwnd:
            return
        cur = self._native_canvas_size()
        size_changed = cur != self._last_embed_size
        if size_changed or not self._window_fills_parent(self._embedded_hwnd, canvas_hwnd, cur):
            self._resize_embedded_window()
        else:
            # mstsc can recreate/shrink its inner surface without resizing the
            # outer host. Repair that too, but only move windows that drifted.
            self._force_fill_window_tree(self._embedded_hwnd)
        # Qt's logical size is the source of reconnect requests. The native
        # measurement above exists only to keep the hosted window filling
        # the panel, including after a DPI change while moving the app.
        self._queue_fallback_resize()

    def _release_credentials(self):
        """Let go of this launch's Credential Manager entries, if any."""
        creds, self._credentials = self._credentials, None
        if creds is not None:
            creds.release()

    def _disconnect_fallback_mstsc(self):
        self._user_disconnected = True
        if self._rdp_proc is not None:
            _LOG.info("mstsc disconnect requested | pid=%s host=%s",
                      getattr(self._rdp_proc, "pid", None), self.connection_info.get("host", ""))
        self._attach_timer.stop()
        self._fallback_resize_timer.stop()
        self._fallback_reconnect_timer.stop()
        self._process_health_timer.stop()
        self._embedded_hwnd = None
        if self._rdp_proc and self._rdp_proc.poll() is None:
            try:
                self._rdp_proc.terminate()
            except Exception:
                pass
        self._rdp_proc = None
        self._release_credentials()
        if self._rdp_file_path and os.path.exists(self._rdp_file_path):
            try:
                os.remove(self._rdp_file_path)
            except Exception:
                pass
        self._rdp_file_path = None
        self.status.setText("Disconnected.")
        self.btn_connect.setEnabled(True)
        self.btn_disconnect.setEnabled(False)

    def _how_it_ended(self) -> str:
        """Windows' own account of how the last mstsc connection ended ("" when it has none).

        Read once per launch, from the RDP client's event log, and logged.
        """
        launch = getattr(self, "_rdp_launch", None)
        if not launch:
            return ""
        cached = getattr(self, "_rdp_ended", None)
        if cached and cached[0] == launch:
            return cached[1]
        signed_in, reason = rdp_client_outcome(*launch)
        host = str(self.connection_info.get("host", "") or "")
        if signed_in or reason is not None:
            _LOG.warning("rdp client | pid=%s signed_in=%s disconnect_reason=%s host=%s",
                         launch[0], signed_in, reason, host)
        text = describe_disconnect(signed_in, reason, host)
        self._rdp_ended = (launch, text)
        return text

    def _check_process_health(self):
        """Periodic check: if mstsc exited unexpectedly, auto-reconnect."""
        if not self._fallback_mode:
            self._process_health_timer.stop()
            return
        if self._user_disconnected:
            self._process_health_timer.stop()
            return
        if self._rdp_proc and self._rdp_proc.poll() is None:
            return  # Still running — all good
        # Process exited unexpectedly. If no desktop HWND is currently embedded,
        # this is usually the user cancelling a credentials / bad-password flow;
        # do not auto-reconnect with the same bad saved password into a loop.
        had_embedded_hwnd = bool(self._embedded_hwnd)
        exit_code = self._rdp_proc.poll() if self._rdp_proc else None
        _LOG.warning("mstsc exited | code=%s had_desktop=%s last_dialog=%s %r host=%s",
                     exit_code, had_embedded_hwnd, self._last_dialog_kind,
                     self._last_dialog_text, self.connection_info.get("host", ""))
        ended = self._how_it_ended()
        self._process_health_timer.stop()
        self._fallback_resize_timer.stop()
        self._fallback_reconnect_timer.stop()
        self._embedded_hwnd = None
        self._rdp_proc = None
        self._release_credentials()
        if self._rdp_file_path and os.path.exists(self._rdp_file_path):
            try:
                os.remove(self._rdp_file_path)
            except Exception:
                pass
        self._rdp_file_path = None
        if not had_embedded_hwnd:
            # Say why, if mstsc said: the error box it showed is gone now, and
            # the status bar is the only place its text survives.
            if self._last_dialog_kind == DIALOG_MESSAGE and self._last_dialog_text:
                gateway = normalize_rdp_gateway(self.connection_info)
                who = "RD Gateway" if (gateway["host"] and is_gateway_message(self._last_dialog_text)) \
                    else "Connection failed"
                self.status.setText(
                    f"{who}: {explain_mstsc_message(self._last_dialog_text)}  "
                    "Click Reconnect to try again.")
            elif ended:
                self.status.setText(f"{ended}  Click Reconnect to try again.")
            else:
                self.status.setText(
                    "mstsc closed before the desktop appeared (credential prompt "
                    "cancelled or connection refused). Click Reconnect to try again.")
            self.btn_connect.setEnabled(True)
            self.btn_disconnect.setEnabled(False)
            return
        self.status.setText("Remote session lost — reconnecting in 3s...")
        self.btn_connect.setEnabled(False)
        self.btn_disconnect.setEnabled(True)
        QTimer.singleShot(3000, self._auto_reconnect)

    def _auto_reconnect(self):
        """Automatically reconnect after unexpected mstsc exit."""
        if self._user_disconnected or self._close_emitted:
            return
        self._close_emitted = False
        self.status.setText("Reconnecting...")
        self._connect_fallback_mstsc()

    def _send_ctrl_alt_del(self):
        """Send remote secure-attention equivalent (Ctrl+Alt+End)."""
        if self._fallback_mode:
            self._open_external_window()
            return

        diag: list[str] = []
        if self._send_ctrl_alt_end_best_effort(diag):
            self.status.setText("Sent Ctrl+Alt+End to remote session.")
            self._emit_cad_diagnostics(diag, success=True)
            return
        self._emit_cad_diagnostics(diag, success=False)
        self.status.setText(self._build_cad_failure_status(diag))

    def _build_cad_failure_status(self, diag: list[str]) -> str:
        if not diag:
            return "Could not send Ctrl+Alt+End (no diagnostics)."

        text = "\n".join(diag)
        if (
            "mode=fallback" in text
            and "postmessage posted_count=" in text
            and "SendInput(use_scancode=False) sent=0/6" in text
            and "SendInput(use_scancode=True) sent=0/6" in text
        ):
            return (
                "Could not send Ctrl+Alt+End (Windows blocked synthetic secure-attention "
                "in embedded compatibility mode)."
            )

        tail = diag[-1]
        return f"Could not send Ctrl+Alt+End ({tail})."

    def _open_external_window(self):
        if self._close_emitted:
            return
        host = self.connection_info.get("host", "")
        if not host:
            self.status.setText("Host is required to open an external window.")
            return

        credentials = None
        path = None
        try:
            credentials = prepare_rdp_credentials(self.connection_info)
            lines = build_external_rdp_lines(self.connection_info, credentials,
                                             fullscreen=True)
            path = write_rdp_file(lines)
            proc = subprocess.Popen(["mstsc", path])
        except Exception:
            release_when_exited(None, credentials, path)
            _LOG.exception("external mstsc handoff failed | host=%s", host)
            self.status.setText("Could not open an external Remote Desktop window.")
            return

        _log_rdp_launch(self.connection_info, "external-handoff", path, proc.pid)
        release_when_exited(proc, credentials, path)
        # Transfer session tracking before closing the embedded tab.
        self.external_opened.emit(proc)
        self.disconnect_session()

    def _emit_cad_diagnostics(self, diag: list[str], success: bool):
        prefix = "[RDP CAD] SUCCESS" if success else "[RDP CAD] FAILURE"
        print(prefix)
        if not diag:
            print("[RDP CAD] (no diagnostics captured)")
            return
        for line in diag:
            print(f"[RDP CAD] {line}")

    def _is_process_elevated(self, pid: int) -> tuple[bool | None, str | None]:
        if pid <= 0:
            return None, "invalid pid"
        proc_handle = None
        token_handle = None
        try:
            proc_handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not proc_handle:
                return None, f"OpenProcess failed ({ctypes.get_last_error()})"

            token_handle = ctypes.c_void_p()
            if not advapi32.OpenProcessToken(ctypes.c_void_p(proc_handle), TOKEN_QUERY, ctypes.byref(token_handle)):
                return None, f"OpenProcessToken failed ({ctypes.get_last_error()})"

            elevation = TOKEN_ELEVATION_STRUCT()
            out_len = ctypes.c_ulong(0)
            ok = advapi32.GetTokenInformation(
                token_handle,
                TokenElevation,
                ctypes.byref(elevation),
                ctypes.sizeof(elevation),
                ctypes.byref(out_len),
            )
            if not ok:
                return None, f"GetTokenInformation failed ({ctypes.get_last_error()})"
            return bool(elevation.TokenIsElevated), None
        except Exception as e:
            return None, str(e)
        finally:
            try:
                if token_handle:
                    kernel32.CloseHandle(token_handle)
            except Exception:
                pass
            try:
                if proc_handle:
                    kernel32.CloseHandle(ctypes.c_void_p(proc_handle))
            except Exception:
                pass

    def _append_sendinput_context(self, diag: list[str]):
        try:
            current_pid = os.getpid()
        except Exception:
            current_pid = -1

        fg = int(user32.GetForegroundWindow() or 0)
        fg_pid = ctypes.c_ulong(0)
        if fg:
            try:
                user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), ctypes.byref(fg_pid))
            except Exception:
                fg_pid = ctypes.c_ulong(0)

        diag.append(f"app_pid={current_pid} foreground_pid={int(fg_pid.value)}")

        app_elev, app_elev_err = self._is_process_elevated(int(current_pid))
        if app_elev_err:
            diag.append(f"app_elevated=unknown ({app_elev_err})")
        else:
            diag.append(f"app_elevated={app_elev}")

        target_pid = 0
        if self._fallback_mode and self._rdp_proc and self._rdp_proc.poll() is None:
            target_pid = int(self._rdp_proc.pid)
        elif int(fg_pid.value) > 0:
            target_pid = int(fg_pid.value)

        if target_pid > 0:
            target_elev, target_elev_err = self._is_process_elevated(target_pid)
            if target_elev_err:
                diag.append(f"target_pid={target_pid} elevated=unknown ({target_elev_err})")
            else:
                diag.append(f"target_pid={target_pid} elevated={target_elev}")
            if app_elev is not None and target_elev is not None and app_elev != target_elev:
                diag.append("integrity mismatch detected (likely UIPI block for SendInput)")

    def _send_ctrl_alt_end_best_effort(self, diag: list[str] | None = None) -> bool:
        if diag is None:
            diag = []
        diag.append(f"mode={'fallback' if self._fallback_mode else 'activex'}")

        # Strategy 1 (ActiveX): use SendKeys for Ctrl+Alt+End.
        if not self._fallback_mode and self.rdp_ax is not None:
            # keyUp array + keyData array (VK_*): down Ctrl, down Alt, down End,
            # then release End, release Alt, release Ctrl.
            key_up = [False, False, False, True, True, True]
            key_data = [VK_CONTROL, VK_MENU, VK_END, VK_END, VK_MENU, VK_CONTROL]
            for sig in (
                "SendKeys(int, QVariant, QVariant)",
                "SendKeys(int, VARIANT, VARIANT)",
            ):
                try:
                    result = self.rdp_ax.dynamicCall(sig, len(key_data), key_up, key_data)
                    diag.append(f"{sig} -> {result!r}")
                    if isinstance(result, bool) and result:
                        diag.append("activeX SendKeys reported success")
                        return True
                    if isinstance(result, int) and result != 0:
                        diag.append("activeX SendKeys reported non-zero success code")
                        return True
                except Exception as e:
                    diag.append(f"{sig} exception: {e}")

            # Some client builds expose only SendRemoteAction; keep as low-confidence
            # attempt, but do not treat a void return as success.
            for call in (
                ("SendRemoteAction(int)", [0]),
                ("SendRemoteAction(RemoteSessionActionType)", [0]),
            ):
                try:
                    result = self.rdp_ax.dynamicCall(call[0], *call[1])
                    diag.append(f"{call[0]} -> {result!r}")
                    if isinstance(result, bool) and result:
                        diag.append("activeX SendRemoteAction reported success")
                        return True
                    if isinstance(result, int) and result != 0:
                        diag.append("activeX SendRemoteAction reported non-zero success code")
                        return True
                except Exception as e:
                    diag.append(f"{call[0]} exception: {e}")

        # Strategy 2 (fallback-hosted mstsc): targeted window-message path.
        # IMPORTANT: PostMessage can report success even when remote session ignores
        # the sequence, so this is diagnostic/assistive only, not authoritative.
        if self._fallback_mode and self._embedded_hwnd:
            if self._send_ctrl_alt_end_via_postmessage(diag):
                diag.append("postmessage path posted (non-authoritative)")
            else:
                diag.append("postmessage path failed")

        # Strategy 3: synthesize Ctrl+Alt+End using VK-based SendInput.
        self._focus_remote_input()
        if self._send_ctrl_alt_end_via_sendinput(use_scancode=False, diag=diag):
            diag.append("sendinput vk path succeeded")
            return True
        diag.append("sendinput vk path failed")

        # Strategy 4: synthesize with scan-codes (works on some keyboard layouts).
        time.sleep(0.02)
        self._focus_remote_input()
        if self._send_ctrl_alt_end_via_sendinput(use_scancode=True, diag=diag):
            diag.append("sendinput scancode path succeeded")
            return True
        diag.append("sendinput scancode path failed")
        diag.append("all CAD strategies exhausted")

        return False

    def _send_ctrl_alt_end_via_sendinput(self, use_scancode: bool = False, diag: list[str] | None = None) -> bool:
        if diag is None:
            diag = []

        if self._fallback_mode and self._embedded_hwnd:
            self._force_focus_embedded_target(diag)

        def _key_input(vk: int, key_up: bool = False, extended: bool = False) -> INPUT:
            flags = 0
            if extended:
                flags |= KEYEVENTF_EXTENDEDKEY
            if key_up:
                flags |= KEYEVENTF_KEYUP
            if use_scancode:
                scan = int(user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) or 0)
                flags |= KEYEVENTF_SCANCODE
                return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, scan, flags, 0, 0))
            return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, 0, flags, 0, 0))

        sequence = (
            _key_input(VK_CONTROL, False),
            _key_input(VK_MENU, False),
            _key_input(VK_END, False, extended=True),
            _key_input(VK_END, True, extended=True),
            _key_input(VK_MENU, True),
            _key_input(VK_CONTROL, True),
        )
        arr = (INPUT * len(sequence))(*sequence)
        self._sending_secure_attention = True
        try:
            ctypes.set_last_error(0)
            sent = user32.SendInput(len(sequence), arr, ctypes.sizeof(INPUT))
            diag.append(f"SendInput(use_scancode={use_scancode}) sent={int(sent)}/{len(sequence)}")
            if int(sent) != len(sequence):
                err = ctypes.get_last_error()
                diag.append(f"SendInput GetLastError={int(err)}")
                self._append_sendinput_context(diag)
                diag.append("SendInput reported partial/failed injection")
                return False
            # SendInput success only means local injection succeeded. Require the
            # remote target to be foreground to avoid false "sent" status.
            fg_ok = self._remote_target_is_foreground(diag)
            diag.append(f"foreground_check={fg_ok}")
            return fg_ok
        except Exception as e:
            diag.append(f"SendInput exception: {e}")
            return False
        finally:
            self._sending_secure_attention = False

    def _remote_target_is_foreground(self, diag: list[str] | None = None) -> bool:
        if diag is None:
            diag = []
        try:
            fg = int(user32.GetForegroundWindow() or 0)
            diag.append(f"foreground_hwnd=0x{fg:08X}")
            if fg <= 0:
                diag.append("no foreground hwnd")
                return False

            if self._fallback_mode and self._embedded_hwnd:
                root = int(self._embedded_hwnd)
                diag.append(f"embedded_hwnd=0x{root:08X}")
                if fg == root:
                    diag.append("foreground matches embedded root")
                    return True
                if bool(user32.IsChild(ctypes.c_void_p(root), ctypes.c_void_p(fg))):
                    diag.append("foreground is embedded child")
                    return True

                # As a backup, verify foreground belongs to mstsc process.
                if self._rdp_proc and self._rdp_proc.poll() is None:
                    pid = ctypes.c_ulong()
                    user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), ctypes.byref(pid))
                    same_pid = int(pid.value) == int(self._rdp_proc.pid)
                    diag.append(
                        f"foreground_pid={int(pid.value)} mstsc_pid={int(self._rdp_proc.pid)} same_pid={same_pid}"
                    )
                    return same_pid
                return False

            if self.rdp_ax is not None:
                try:
                    ax_hwnd = int(self.rdp_ax.winId())
                    diag.append(f"rdp_ax_hwnd=0x{ax_hwnd:08X}")
                    if fg == ax_hwnd:
                        diag.append("foreground matches ActiveX host hwnd")
                        return True
                    is_child = bool(user32.IsChild(ctypes.c_void_p(ax_hwnd), ctypes.c_void_p(fg)))
                    diag.append(f"foreground_is_child_of_ax={is_child}")
                    return is_child
                except Exception as e:
                    diag.append(f"foreground ActiveX check exception: {e}")
                    return False

            diag.append("no known remote target handle available")
            return False
        except Exception as e:
            diag.append(f"GetForegroundWindow/target check exception: {e}")
            return False

    def _send_ctrl_alt_end_via_postmessage(self, diag: list[str] | None = None) -> bool:
        if diag is None:
            diag = []
        if not self._embedded_hwnd:
            diag.append("postmessage skipped: no embedded hwnd")
            return False

        def _lparam(vk: int, key_up: bool = False, extended: bool = False, alt_context: bool = False) -> int:
            # WM_KEY* lParam bits:
            #  0-15 repeat count (1)
            # 16-23 scan code
            #    24 extended-key flag
            #    29 context code (ALT down)
            #    30 previous key state
            #    31 transition state (1 on key-up)
            scan = int(user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) or 0) & 0xFF
            lp = 1 | (scan << 16)
            if extended:
                lp |= 1 << 24
            if alt_context:
                lp |= 1 << 29
            if key_up:
                lp |= 1 << 30
                lp |= 1 << 31
            return lp

        try:
            # Send only to the mstsc input window (IHWindowClass); fall back to the
            # root if not found.  Sending to every child window in the hierarchy
            # causes the Ctrl+Alt+End sequence to be processed multiple times.
            input_hwnd = self._find_mstsc_input_hwnd()
            targets: list[int] = [self._embedded_hwnd]
            if input_hwnd and input_hwnd != self._embedded_hwnd:
                targets.append(input_hwnd)
            diag.append(f"postmessage targets={len(targets)}")
            posted_any = False
            posted_count = 0
            for hwnd in targets:
                h = ctypes.c_void_p(hwnd)
                posted = (
                    bool(user32.PostMessageW(h, WM_KEYDOWN, VK_CONTROL, _lparam(VK_CONTROL, key_up=False)))
                    and bool(user32.PostMessageW(h, WM_SYSKEYDOWN, VK_MENU, _lparam(VK_MENU, key_up=False, alt_context=True)))
                    and bool(user32.PostMessageW(h, WM_KEYDOWN, VK_END, _lparam(VK_END, key_up=False, extended=True, alt_context=True)))
                    and bool(user32.PostMessageW(h, WM_KEYUP, VK_END, _lparam(VK_END, key_up=True, extended=True, alt_context=True)))
                    and bool(user32.PostMessageW(h, WM_SYSKEYUP, VK_MENU, _lparam(VK_MENU, key_up=True, alt_context=True)))
                    and bool(user32.PostMessageW(h, WM_KEYUP, VK_CONTROL, _lparam(VK_CONTROL, key_up=True)))
                )
                if posted:
                    posted_count += 1
                posted_any = posted_any or posted
            diag.append(f"postmessage posted_count={posted_count}/{len(targets)}")
            if not posted_any:
                try:
                    err = ctypes.get_last_error()
                    diag.append(f"postmessage GetLastError={err}")
                except Exception:
                    pass
            return posted_any
        except Exception as e:
            diag.append(f"postmessage exception: {e}")
            return False

class RDPLauncher(QWidget):
    """Launch Remote Desktop in a tab or in a standalone Windows client."""

    session_closed = pyqtSignal()
    resources_changed = pyqtSignal(dict)
    external_opened = pyqtSignal(object)

    def __init__(self, connection_info, parent=None, embedded_mode: bool = False):
        super().__init__(parent)
        self.connection_info = connection_info
        self.embedded_mode = embedded_mode
        self._embedded_widget = None
        self._setup_ui()

    def _setup_ui(self):
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)

        if self.embedded_mode:
            self._embedded_widget = EmbeddedRDPWidget(self.connection_info, self)
            self._embedded_widget.session_closed.connect(self.session_closed)
            self._embedded_widget.resources_changed.connect(self.resources_changed)
            self._embedded_widget.external_opened.connect(self.external_opened)
            self.main_layout.addWidget(self._embedded_widget)
            return

        # ─── HEADER ───
        header = QFrame()
        header.setFixedHeight(50)
        header.setStyleSheet("""
            QFrame {
                background-color: rgba(30, 41, 59, 0.95);
                border-bottom: 1px solid palette(highlight);
            }
        """)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 0, 20, 0)
        
        # Status "Light"
        self.status_icon = QLabel("●")
        self.status_icon.setStyleSheet("color: #10b981; font-size: 14px;") # Always green for "Ready"
        hl.addWidget(self.status_icon)

        # Title
        name = self.connection_info.get("name", "Remote Desktop")
        title = QLabel(f"  {name}")
        title.setStyleSheet("font-weight: bold; font-size: 14px; color: #f8fafc;")
        hl.addWidget(title)
        
        # Subtitle / Tag
        tag = QLabel("  (PREMIUM EXTERNAL)")
        tag.setStyleSheet("color: #94a3b8; font-size: 10px; font-weight: bold; margin-top: 2px;")
        hl.addWidget(tag)
        
        hl.addStretch()
        self.main_layout.addWidget(header)

        # ─── CONTENT AREA ───
        # Gradient background
        self.content_container = QWidget()
        self.content_container.setStyleSheet("background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #0f172a, stop:1 #1e293b);")
        cl = QVBoxLayout(self.content_container)
        cl.setContentsMargins(40, 40, 40, 40)
        cl.setSpacing(25)

        # 1. Info Card
        info_card = QFrame()
        info_card.setStyleSheet("""
            QFrame {
                background-color: rgba(255, 255, 255, 0.03);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 12px;
            }
            QLabel { color: #cbd5e1; font-size: 13px; }
        """)
        il = QFormLayout(info_card)
        il.setContentsMargins(20, 20, 20, 20)
        il.setVerticalSpacing(10)
        
        host_lbl = QLabel(self.connection_info.get("host", "Unknown Host"))
        host_lbl.setStyleSheet("font-family: 'MesloLGS Nerd Font Mono', monospace; color: #60a5fa; font-weight: bold;")
        il.addRow("Host Address:", host_lbl)
        
        user_lbl = QLabel(self.connection_info.get("username", "Prompt"))
        user_lbl.setStyleSheet("font-family: 'MesloLGS Nerd Font Mono', monospace; color: #f472b6;")
        il.addRow("Username:", user_lbl)

        # Only shown when there is one — a direct connection has nothing to say
        # here, and a gateway that silently stopped applying is worth spotting.
        gateway = normalize_rdp_gateway(self.connection_info)
        if gateway["host"]:
            notes = []
            if gateway["bypass_local"]:
                notes.append("bypassed for local addresses")
            if gateway["use_for_remote"]:
                notes.append("credentials shared with the desktop")
            gw_lbl = QLabel(
                gateway["host"] + (f"  ({', '.join(notes)})" if notes else ""))
            gw_lbl.setWordWrap(True)
            gw_lbl.setStyleSheet(
                "font-family: 'MesloLGS Nerd Font Mono', monospace; color: #a78bfa;")
            il.addRow("RD Gateway:", gw_lbl)

        cl.addWidget(info_card)

        # 2. Configuration Grid
        config_group = QGroupBox("SESSION CONFIGURATION")
        config_group.setStyleSheet("""
            QGroupBox {
                color: #94a3b8; font-weight: bold; font-size: 11px;
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 15px;
            }
        """)
        cgl = QVBoxLayout(config_group)
        cgl.setSpacing(15)
        
        # Display Settings
        display_layout = QHBoxLayout()
        self.multimon = QCheckBox("Use All Monitors")
        self.multimon.setChecked(True)
        self.multimon.setStyleSheet("color: #e2e8f0;")
        display_layout.addWidget(self.multimon)
        
        self.fullscreen = QCheckBox("Full Screen")
        self.fullscreen.setChecked(True)
        self.fullscreen.setStyleSheet("color: #e2e8f0;")
        display_layout.addWidget(self.fullscreen)
        cgl.addLayout(display_layout)
        
        # Resource Redirection — pre-filled from this device's stored answer.
        res_layout = QHBoxLayout()
        self.clipboard = QCheckBox("Clipboard Sync")
        self.clipboard.setStyleSheet("color: #e2e8f0;")
        res_layout.addWidget(self.clipboard)

        self.printers = QCheckBox("Redirect Printers")
        self.printers.setStyleSheet("color: #e2e8f0;")
        res_layout.addWidget(self.printers)

        self.drives = QCheckBox("Redirect Drives")
        self.drives.setStyleSheet("color: #e2e8f0;")
        res_layout.addWidget(self.drives)

        self.smartcards = QCheckBox("Smart Cards")
        self.smartcards.setStyleSheet("color: #e2e8f0;")
        res_layout.addWidget(self.smartcards)
        cgl.addLayout(res_layout)

        # Experience
        exp_layout = QHBoxLayout()
        self.audio = QCheckBox("Play Audio Remotely")
        self.audio.setStyleSheet("color: #e2e8f0;")
        exp_layout.addWidget(self.audio)

        self._seed_resource_boxes()

        self.admin = QCheckBox("Admin Session (/admin)")
        self.admin.setChecked(False)
        self.admin.setStyleSheet("color: #e2e8f0;")
        exp_layout.addWidget(self.admin)
        cgl.addLayout(exp_layout)
        
        cl.addWidget(config_group)

        # 3. Big Launch Button
        self.btn_launch = QPushButton("Launch Remote Desktop")
        self.btn_launch.setIcon(icons.icon("play", "#0b0e14"))
        self.btn_launch.setIconSize(QSize(20, 20))
        self.btn_launch.setFixedHeight(65)
        self.btn_launch.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_launch.setStyleSheet("""
            QPushButton {
                font-size: 16px; font-weight: 900; letter-spacing: 1px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0aa3c2, stop:1 #0b86a0);
                border-radius: 12px;
                color: #0b0e14;  /* dark ink: white sits at 3:1 on this cyan */
                border: 1px solid rgba(255,255,255,0.1);
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #22c5e3, stop:1 #0aa3c2);
                border: 1px solid rgba(255,255,255,0.3);
                margin-top: -2px; /* Lift effect */
            }
            QPushButton:pressed {
                background: #0b86a0;
                margin-top: 2px;
            }
        """)
        self.btn_launch.clicked.connect(self._launch_rdp)
        cl.addWidget(self.btn_launch)
        
        # Footer Hint
        hint = QLabel("Session will open in a new high-performance MSTSC window.")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: #64748b; font-size: 11px; font-style: italic;")
        cl.addWidget(hint)
        
        cl.addStretch()
        self.main_layout.addWidget(self.content_container)

    def _seed_resource_boxes(self):
        """Fill the redirection checkboxes from this device's stored answer."""
        resources = normalize_rdp_resources(self.connection_info.get(RDP_RESOURCE_KEY))
        self.clipboard.setChecked(resources["clipboard"])
        self.printers.setChecked(resources["printers"])
        self.drives.setChecked(resources["drives"])
        self.smartcards.setChecked(resources["smartcards"])
        self.audio.setChecked(resources["audio"])

    def _resource_values_from_ui(self) -> dict:
        return {
            "clipboard": self.clipboard.isChecked(),
            "printers": self.printers.isChecked(),
            "drives": self.drives.isChecked(),
            "smartcards": self.smartcards.isChecked(),
            "audio": self.audio.isChecked(),
        }

    def _resolve_launch_resources(self) -> dict | None:
        """Local resources for this launch, asking once per device.

        After the first answer the panel above governs, so anything the user
        changes there is remembered for the device too. Returns None when the
        first-run prompt was cancelled.
        """
        if not has_saved_rdp_resources(self.connection_info):
            chosen = _resolve_local_resources(
                self, self.connection_info, self.resources_changed.emit
            )
            if chosen is None:
                return None
            self._seed_resource_boxes()

        resources = self._resource_values_from_ui()
        if resources != normalize_rdp_resources(self.connection_info.get(RDP_RESOURCE_KEY)):
            self.connection_info[RDP_RESOURCE_KEY] = resources
            self.resources_changed.emit(dict(resources))
        return resources

    def _launch_rdp(self):
        """Builds the .rdp configuration and launches mstsc.exe."""
        try:
            resources = self._resolve_launch_resources()
            if resources is None:
                return
            credentials = prepare_rdp_credentials(self.connection_info)
            lines = build_external_rdp_lines(
                self.connection_info, credentials, resources,
                fullscreen=self.fullscreen.isChecked(), multimon=self.multimon.isChecked(),
            )
            path = write_rdp_file(lines)

            # Launch
            cmd = ["mstsc.exe", path]
            if self.admin.isChecked():
                cmd.append("/admin")

            _proc = subprocess.Popen(cmd)
            # Kept so the window can log how long this desktop was open.
            self.last_process = _proc
            _log_rdp_launch(self.connection_info, "external", path, _proc.pid)
            _auto_dismiss_rdp_security_warning(_proc.pid)
            release_when_exited(_proc, credentials, path)

            # Visual feedback
            self.status_icon.setStyleSheet("color: #34d399; font-size: 14px;") # Brighter green
            if credentials.warnings:
                self.status_icon.setToolTip("\n".join(credentials.warnings))

        except Exception as e:
            _LOG.exception("external mstsc launch failed | host=%s",
                           self.connection_info.get("host", ""))
            QMessageBox.critical(self, "Launch Error", f"Failed to launch Remote Desktop:\n{e}")

    def cleanup(self):
        """No helper processes to clean up."""
        if self._embedded_widget:
            self._embedded_widget.cleanup()

    def suspend(self):
        if self._embedded_widget:
            self._embedded_widget.suspend()

    def resume(self):
        if self._embedded_widget:
            self._embedded_widget.resume()
