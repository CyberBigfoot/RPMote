"""Lock preferences apply live, preserve legacy timeouts, and protect open dialogs."""

import ctypes
import os
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QMainWindow, QPushButton

from app.idle_lock import IdleLock
from app.lock_options import LockOptionsMenu, LockOptionsWidget
from app.lock_policy import (IDLE_LOCK_SETTING, LOCK_ON_MINIMIZE, LOCK_ON_SLEEP,
                             LOCK_ON_WINDOWS_LOCK, normalize_lock_settings, update_window_lock_settings)
from app.windows_lock_events import WindowsLockEvents, WM_POWERBROADCAST, WM_WTSSESSION_CHANGE

APP = QApplication.instance() or QApplication([])


class FakeVault:
    is_unlocked = True

    def __init__(self):
        self.settings = {IDLE_LOCK_SETTING: 15, "other_setting": "keep me"}
        self.fail_write = False
        self.locked_calls = 0

    def update_settings(self, settings):
        if self.fail_write:
            raise OSError("vault save failed")
        self.settings.update(settings)

    def verify_password(self, password):
        return password == "correct-master-password"

    def lock(self):
        self.locked_calls += 1
        self.is_unlocked = False


@pytest.fixture
def window():
    widget = QMainWindow()
    widget.resize(900, 650)
    widget.vault = FakeVault()
    widget.idle_lock = IdleLock(widget.vault, widget, settings=widget.vault.settings)
    widget._show_settings = mock.Mock()
    widget.show()
    APP.processEvents()
    yield widget
    widget.idle_lock.shutdown()
    widget.close()
    widget.deleteLater()
    APP.processEvents()


@pytest.mark.parametrize("value,expected", [(0, 0), (7, 7), (-2, 0), (99999, 1440), (None, 15), ("bad", 15), (float('inf'), 15)])
def test_timeout_normalization_keeps_existing_values(value, expected):
    settings = normalize_lock_settings({IDLE_LOCK_SETTING: value})
    assert settings[IDLE_LOCK_SETTING] == expected
    assert not settings[LOCK_ON_MINIMIZE]
    assert not settings[LOCK_ON_WINDOWS_LOCK]
    assert not settings[LOCK_ON_SLEEP]


def test_boolean_settings_do_not_treat_false_strings_as_enabled():
    settings = normalize_lock_settings({LOCK_ON_MINIMIZE: "false", LOCK_ON_WINDOWS_LOCK: "true", LOCK_ON_SLEEP: "0"})
    assert not settings[LOCK_ON_MINIMIZE] and settings[LOCK_ON_WINDOWS_LOCK] and not settings[LOCK_ON_SLEEP]


def test_presets_custom_minutes_and_events_emit_only_user_changes():
    control = LockOptionsWidget({IDLE_LOCK_SETTING: 7})
    changes = []
    control.changed.connect(changes.append)
    try:
        assert control.preset.currentText() == "Custom…"
        assert control.minutes.value() == 7
        control.preset.setCurrentIndex(control.preset.findData(30))
        assert changes[-1] == {IDLE_LOCK_SETTING: 30}
        control.preset.setCurrentIndex(control.preset.count() - 1)
        control.minutes.setValue(9)
        assert changes[-1] == {IDLE_LOCK_SETTING: 9}
        control.triggers[LOCK_ON_MINIMIZE].setChecked(True)
        assert changes[-1] == {LOCK_ON_MINIMIZE: True}
        control.preset.setCurrentIndex(control.preset.findData(0))
        assert changes[-1] == {IDLE_LOCK_SETTING: 0}
    finally:
        control.deleteLater()


def test_changes_are_persisted_and_applied_live(window):
    seen = []
    window.idle_lock.settings_changed.connect(seen.append)
    update_window_lock_settings(window, {IDLE_LOCK_SETTING: 7, LOCK_ON_SLEEP: True})
    assert window.idle_lock.minutes == 7
    assert window.idle_lock._timer.interval() == 7 * 60000
    assert window.vault.settings[LOCK_ON_SLEEP]
    assert window.vault.settings['other_setting'] == 'keep me'
    assert seen[-1][IDLE_LOCK_SETTING] == 7


def test_failed_persistence_does_not_change_the_live_policy(window):
    window.vault.fail_write = True
    with pytest.raises(OSError):
        update_window_lock_settings(window, {IDLE_LOCK_SETTING: 0, LOCK_ON_SLEEP: True})
    assert window.idle_lock.minutes == 15
    assert not window.idle_lock._settings[LOCK_ON_SLEEP]


def test_never_stops_idle_timer_but_keeps_enabled_lock_events(window):
    update_window_lock_settings(window, {IDLE_LOCK_SETTING: 0, LOCK_ON_WINDOWS_LOCK: True})
    assert not window.idle_lock._timer.isActive()
    window.idle_lock._system_event("windows_lock")
    APP.processEvents()
    assert window.idle_lock.is_locked
    assert window.idle_lock._screen.reason == "windows_lock"
    assert 'Windows session' in window.idle_lock._screen.subtitle.text()
    assert window.vault.is_unlocked


def test_disabled_events_do_not_lock_the_app(window):
    window.idle_lock._system_event("windows_lock")
    window.idle_lock._system_event("sleep")
    window.showMinimized()
    APP.processEvents()
    assert not window.idle_lock.is_locked


def test_minimize_rule_locks_and_restoring_does_not_unlock(window):
    update_window_lock_settings(window, {IDLE_LOCK_SETTING: 0, LOCK_ON_MINIMIZE: True})
    window.showMinimized()
    APP.processEvents()
    assert window.idle_lock.is_locked
    assert window.idle_lock._screen.reason == 'minimized'
    window.showNormal()
    APP.processEvents()
    assert window.idle_lock.is_locked
    screen = window.idle_lock._screen
    screen.password.setText('correct-master-password')
    screen._try_unlock()
    assert not window.idle_lock.is_locked


def test_sleep_rule_preserves_vault_and_requires_password_after_resume(window):
    update_window_lock_settings(window, {LOCK_ON_SLEEP: True})
    window.idle_lock._system_event('sleep')
    APP.processEvents()
    assert window.idle_lock.is_locked
    first = window.idle_lock._screen
    window.idle_lock._system_event('sleep')
    APP.processEvents()
    assert window.idle_lock._screen is first
    assert window.vault.locked_calls == 0
    first.password.setText('wrong')
    first._try_unlock()
    assert window.idle_lock.is_locked
    first.password.setText('correct-master-password')
    first._try_unlock()
    assert not window.idle_lock.is_locked


def test_menu_presets_and_cancelled_custom_timeout(window):
    menu = LockOptionsMenu(window)
    try:
        menu.timeouts[5].trigger()
        assert window.idle_lock.minutes == 5
        assert menu.timeouts[5].isChecked()
        menu.triggers[LOCK_ON_SLEEP].trigger()
        assert window.vault.settings[LOCK_ON_SLEEP]
        with mock.patch('app.lock_options.QInputDialog.getInt', return_value=(9, False)):
            menu._custom()
        assert window.idle_lock.minutes == 5
        with mock.patch('app.lock_options.QInputDialog.getInt', return_value=(9, True)):
            menu._custom()
        assert window.idle_lock.minutes == 9
        assert menu.custom.isChecked()
        assert '9 min' in menu.custom.text()
    finally:
        menu.deleteLater()


def test_an_open_modal_dialog_is_covered_without_losing_its_state(window):
    from PyQt6.QtWidgets import QLineEdit
    dialog = QDialog(window)
    dialog.setModal(True)
    dialog.resize(500, 400)
    draft = QLineEdit('unsaved draft', dialog)
    draft.setGeometry(20, 20, 300, 30)
    button = QPushButton('Change something', dialog)
    button.setGeometry(20, 70, 180, 35)
    clicked = []
    button.clicked.connect(lambda: clicked.append(True))
    dialog.show()
    APP.processEvents()
    try:
        window.idle_lock.lock_now()
        screen = window.idle_lock._screen
        APP.processEvents()
        assert dialog in screen._protected_modals
        assert screen._modal_covers[0].isVisible()
        assert screen._unlock_prompt.isVisible()
        assert APP.activeModalWidget() is screen._unlock_prompt
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert not clicked
        QTest.keyClicks(draft, 'cannot type')
        assert draft.text() == 'unsaved draft'
        screen._unlock_prompt.reject()
        assert window.idle_lock.is_locked and screen._unlock_prompt.isVisible()
        QTest.keyClicks(screen.password, 'correct-master-password')
        assert screen.password.text() == 'correct-master-password'
        screen._try_unlock()
        APP.processEvents()
        assert not window.idle_lock.is_locked
        assert dialog.isVisible() and draft.text() == 'unsaved draft'
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert clicked == [True]
    finally:
        dialog.close()
        dialog.deleteLater()


def test_shutdown_cancels_pending_events_and_removes_filters(window):
    update_window_lock_settings(window, {LOCK_ON_SLEEP: True})
    window.idle_lock._system_event('sleep')
    window.idle_lock.shutdown()
    APP.processEvents()
    assert not window.idle_lock.is_locked
    assert not window.idle_lock._timer.isActive()


def test_native_messages_only_apply_to_the_registered_window(window):
    seen = []
    native = WindowsLockEvents(window, seen.append)
    native.hwnd = 1234
    native.dispatch(9876, WM_WTSSESSION_CHANGE, 7)
    native.dispatch(1234, WM_WTSSESSION_CHANGE, 8)  # Windows unlock must never unlock the app.
    native.dispatch(1234, WM_POWERBROADCAST, 10)  # Unrelated power event.
    assert seen == []
    native.dispatch(1234, WM_WTSSESSION_CHANGE, 7)
    for code in (4, 6, 7, 18):
        native.dispatch(1234, WM_POWERBROADCAST, code)
    assert seen == ['windows_lock', 'sleep', 'sleep', 'sleep', 'sleep']


def test_native_message_pointer_uses_windows_pointer_sized_fields(window):
    from ctypes import wintypes
    seen = []
    native = WindowsLockEvents(window, seen.append)
    native.hwnd = 1234
    message = wintypes.MSG()
    message.hWnd = 1234
    message.message = WM_WTSSESSION_CHANGE
    message.wParam = 7
    assert native.nativeEventFilter(b'windows_generic_MSG', ctypes.addressof(message)) == (False, 0)
    assert seen == ['windows_lock']


def test_native_registration_is_for_this_session_and_is_unregistered(window):
    class Function:
        def __init__(self):
            self.calls = []
        def __call__(self, *args):
            self.calls.append(args)
            return 1
    register, unregister = Function(), Function()
    dll = SimpleNamespace(WTSRegisterSessionNotification=register, WTSUnRegisterSessionNotification=unregister)
    native = WindowsLockEvents(window, lambda _reason: None)
    try:
        with mock.patch('app.windows_lock_events.QGuiApplication.platformName', return_value='windows'), \
             mock.patch('app.windows_lock_events.ctypes.WinDLL', return_value=dll):
            native.start()
        assert native.registered
        assert register.calls == [(int(window.winId()), 0)]
        native.stop()
        assert unregister.calls == [(int(window.winId()),)]
        assert not native.installed and not native.registered
    finally:
        native.stop()
