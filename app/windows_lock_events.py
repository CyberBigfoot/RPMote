"""Windows session and power notifications for the app's screen-lock policy.

WTS registration is limited to this window's Windows session. Notifications
never unlock the vault or lock Windows itself.
https://learn.microsoft.com/en-us/windows/win32/termserv/wm-wtssession-change
https://learn.microsoft.com/en-us/windows/win32/power/wm-powerbroadcast-messages
"""

import ctypes
import logging
import sys
from ctypes import wintypes

from PyQt6.QtCore import QAbstractNativeEventFilter
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import QApplication

WM_WTSSESSION_CHANGE = 0x02B1
WM_POWERBROADCAST = 0x0218
WTS_SESSION_LOCK = 0x7
POWER_SLEEP_EVENTS = frozenset((0x4, 0x6, 0x7, 0x12))


class WindowsLockEvents(QAbstractNativeEventFilter):
    def __init__(self, window, callback):
        super().__init__()
        self.window = window
        self.callback = callback
        self.hwnd = 0
        self.registered = False
        self.installed = False
        self.error = ""
        self._dll = None

    def start(self):
        if self.installed or sys.platform != "win32" or QGuiApplication.platformName() != "windows":
            return
        app = QApplication.instance()
        if app is None:
            return
        try:
            self.hwnd = int(self.window.winId())
            self._dll = ctypes.WinDLL("wtsapi32", use_last_error=True)
            self._dll.WTSRegisterSessionNotification.argtypes = (wintypes.HWND, wintypes.DWORD)
            self._dll.WTSRegisterSessionNotification.restype = wintypes.BOOL
            self._dll.WTSUnRegisterSessionNotification.argtypes = (wintypes.HWND,)
            self._dll.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
            self.registered = bool(self._dll.WTSRegisterSessionNotification(self.hwnd, 0))
            if not self.registered:
                self.error = "Windows lock notifications could not be enabled on this system."
                logging.getLogger(__name__).warning("WTS registration failed: %s", ctypes.get_last_error())
            app.installNativeEventFilter(self)
            self.installed = True
        except (OSError, RuntimeError, AttributeError) as exc:
            self.error = "Windows lock notifications are unavailable on this system."
            logging.getLogger(__name__).warning("Lock notifications unavailable: %s", exc)

    def stop(self):
        app = QApplication.instance()
        if self.installed and app is not None:
            app.removeNativeEventFilter(self)
        self.installed = False
        if self.registered and self._dll is not None:
            try:
                self._dll.WTSUnRegisterSessionNotification(self.hwnd)
            except (OSError, RuntimeError):
                pass
        self.registered = False
        self.hwnd = 0

    def dispatch(self, hwnd, message, parameter):
        if not self.hwnd or hwnd != self.hwnd:
            return
        if message == WM_WTSSESSION_CHANGE and parameter == WTS_SESSION_LOCK:
            self.callback("windows_lock")
        elif message == WM_POWERBROADCAST and parameter in POWER_SLEEP_EVENTS:
            self.callback("sleep")

    def nativeEventFilter(self, event_type, message):
        # Session and power notifications are sent to this top-level window;
        # system-wide dispatcher messages are unrelated to this subscription.
        if bytes(event_type) != b"windows_generic_MSG":
            return False, 0
        try:
            msg = wintypes.MSG.from_address(int(message))
            self.dispatch(int(msg.hWnd or 0), int(msg.message), int(msg.wParam))
        except (RuntimeError, TypeError, ValueError, AttributeError):
            pass
        return False, 0
