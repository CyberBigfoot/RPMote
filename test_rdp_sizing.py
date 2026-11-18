"""Hosted RDP must fill native pixels and recover from mstsc layout resets."""
import ctypes
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QResizeEvent, QMoveEvent
from app import rdp_launcher as rdp


APP = QApplication.instance() or QApplication([])


def write_rect(values):
    def fill(hwnd, pointer):
        rect = ctypes.cast(pointer, ctypes.POINTER(ctypes.wintypes.RECT)).contents
        rect.left, rect.top, rect.right, rect.bottom = values
        return True
    return fill


class HostedRDPSizingTests(unittest.TestCase):
    def setUp(self):
        self.widget = rdp.EmbeddedRDPWidget({"host": ""})
        self.addCleanup(self.widget.deleteLater)
        self.addCleanup(self.widget.cleanup)
        self.widget.canvas.resize(800, 500)

    def hosted(self):
        self.widget._embedded_hwnd = 0x1234
        self.widget._rdp_proc = Mock()
        self.widget._rdp_proc.poll.return_value = None
        self.widget._last_fallback_canvas_size = self.widget._logical_canvas_size()
        self.widget._last_fallback_canvas_dpr = float(self.widget.canvas.devicePixelRatioF())
        return int(self.widget.canvas.winId())

    def test_native_client_size_wins_over_qt_logical_size_at_all_scales(self):
        for ratio in (1.0, 1.25, 1.5, 2.0):
            size = (round(800 * ratio), round(500 * ratio))
            with self.subTest(ratio=ratio), patch.object(
                    rdp.user32, "GetClientRect", side_effect=write_rect((0, 0, *size))):
                self.assertEqual(self.widget._native_canvas_size(), size)
                self.assertEqual(self.widget._desired_fallback_desktop_size(), size)

    def test_native_measurement_failure_uses_this_canvas_pixel_ratio(self):
        with patch.object(rdp.user32, "GetClientRect", return_value=False), \
                patch.object(self.widget.canvas, "devicePixelRatioF", return_value=1.5):
            self.assertEqual(self.widget._native_canvas_size(), (1200, 750))

    def test_minimum_remote_desktop_is_applied_after_pixel_conversion(self):
        with patch.object(self.widget, "_native_canvas_size", return_value=(600, 400)):
            self.assertEqual(self.widget._desired_fallback_desktop_size(), (640, 480))

    def test_adoption_sizes_the_host_in_native_pixels(self):
        canvas = int(self.widget.canvas.winId())
        with patch.object(rdp.user32, "GetWindowLongPtrW", return_value=rdp.WS_POPUP), \
                patch.object(rdp.user32, "SetWindowLongPtrW"), \
                patch.object(rdp.user32, "SetParent"), \
                patch.object(rdp.user32, "GetParent", return_value=canvas), \
                patch.object(rdp.user32, "SetWindowPos", return_value=True) as move, \
                patch.object(self.widget, "_native_canvas_size", return_value=(1200, 750)):
            self.assertTrue(self.widget._adopt_window(0x1234))
            args = move.call_args.args
            self.assertEqual(args[0].value, 0x1234)
            self.assertEqual(args[2:6], (0, 0, 1200, 750))

    def test_failed_adoption_restores_frame_without_moving_the_external_window(self):
        with patch.object(rdp.user32, "GetWindowLongPtrW", return_value=rdp.WS_CAPTION), \
                patch.object(rdp.user32, "SetWindowLongPtrW") as style, \
                patch.object(rdp.user32, "SetParent"), \
                patch.object(rdp.user32, "GetParent", return_value=None), \
                patch.object(rdp.user32, "SetWindowPos") as move:
            self.assertFalse(self.widget._adopt_window(0x1234))
            self.assertEqual(style.call_args.args[2], rdp.WS_CAPTION)
            self.assertEqual(move.call_args.args[-1] & 3, 3)  # NOMOVE | NOSIZE

    def test_popup_style_is_normalized_without_signed_ctypes_overflow(self):
        canvas = int(self.widget.canvas.winId())
        signed_popup_style = ctypes.c_int32(rdp.WS_POPUP | rdp.WS_CAPTION).value
        with patch.object(rdp.user32, "GetWindowLongPtrW", return_value=signed_popup_style), \
                patch.object(rdp.user32, "SetWindowLongPtrW") as style, \
                patch.object(rdp.user32, "SetParent"), \
                patch.object(rdp.user32, "GetParent", return_value=canvas), \
                patch.object(rdp.user32, "SetWindowPos", return_value=True), \
                patch.object(self.widget, "_native_canvas_size", return_value=(1200, 750)):
            self.assertTrue(self.widget._adopt_window(0x1234))
            new_style = style.call_args.args[2]
            self.assertEqual(new_style, rdp.WS_CHILD | rdp.WS_VISIBLE)
        self.assertEqual(rdp.user32.SetWindowLongPtrW.argtypes,
                         [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t])

    def test_resize_never_moves_an_unadopted_external_window(self):
        self.hosted()
        with patch.object(rdp.user32, "GetParent", return_value=None), \
                patch.object(rdp.user32, "SetWindowPos") as move:
            self.widget._resize_embedded_window()
            move.assert_not_called()

    def test_failed_resize_does_not_record_the_size_as_successful(self):
        canvas = self.hosted()
        with patch.object(rdp.user32, "GetParent", return_value=canvas), \
                patch.object(self.widget, "_native_canvas_size", return_value=(1200, 750)), \
                patch.object(rdp.user32, "SetWindowPos", return_value=False), \
                patch.object(self.widget, "_force_fill_window_tree") as fill:
            self.widget._resize_embedded_window()
            self.assertEqual(self.widget._last_embed_size, (0, 0))
            fill.assert_not_called()

    def test_successful_resize_updates_size_and_repairs_desktop_surfaces(self):
        canvas = self.hosted()
        with patch.object(rdp.user32, "GetParent", return_value=canvas), \
                patch.object(self.widget, "_native_canvas_size", return_value=(1200, 750)), \
                patch.object(rdp.user32, "SetWindowPos", return_value=True) as move, \
                patch.object(self.widget, "_force_fill_window_tree") as fill:
            self.widget._resize_embedded_window()
            self.assertEqual(move.call_args.args[2:6], (0, 0, 1200, 750))
            self.assertEqual(self.widget._last_embed_size, (1200, 750))
            fill.assert_called_once_with(0x1234)

    def test_watchdog_repairs_mstsc_shrink_without_reconnecting(self):
        canvas = self.hosted()
        self.widget._last_embed_size = (1200, 750)
        with patch.object(rdp.user32, "GetParent", return_value=canvas), \
                patch.object(self.widget, "_native_canvas_size", return_value=(1200, 750)), \
                patch.object(self.widget, "_window_fills_parent", return_value=False), \
                patch.object(self.widget, "_resize_embedded_window") as resize, \
                patch.object(self.widget._fallback_reconnect_timer, "start") as reconnect:
            self.widget._tick_fallback_resize()
            resize.assert_called_once()
            reconnect.assert_not_called()

    def test_watchdog_checks_inner_surface_when_outer_window_still_fills(self):
        canvas = self.hosted()
        self.widget._last_embed_size = (1200, 750)
        with patch.object(rdp.user32, "GetParent", return_value=canvas), \
                patch.object(self.widget, "_native_canvas_size", return_value=(1200, 750)), \
                patch.object(self.widget, "_window_fills_parent", return_value=True), \
                patch.object(self.widget, "_force_fill_window_tree") as fill, \
                patch.object(self.widget, "_resize_embedded_window") as resize:
            self.widget._tick_fallback_resize()
            fill.assert_called_once_with(0x1234)
            resize.assert_not_called()

    def test_monitor_scale_change_repairs_placement_without_reconnecting(self):
        canvas = self.hosted()
        self.widget._last_embed_size = (800, 500)
        with patch.object(rdp.user32, "GetParent", return_value=canvas), \
                patch.object(self.widget, "_native_canvas_size", return_value=(1200, 750)), \
                patch.object(self.widget, "_resize_embedded_window") as resize, \
                patch.object(self.widget, "_rdp_auth_dialog_hwnd", return_value=None), \
                patch.object(self.widget._fallback_reconnect_timer, "start") as reconnect:
            self.widget._tick_fallback_resize()
            resize.assert_called_once()
            reconnect.assert_not_called()

    def test_canvas_resize_notification_with_unchanged_size_does_not_reconnect(self):
        self.hosted()
        size = self.widget.canvas.size()
        with patch.object(self.widget, "_resize_embedded_window") as resize, \
                patch.object(self.widget._fallback_reconnect_timer, "start") as reconnect:
            self.widget.eventFilter(self.widget.canvas, QResizeEvent(size, size))
        resize.assert_called_once()
        reconnect.assert_not_called()

    def test_window_move_does_not_schedule_a_reconnect(self):
        self.hosted()
        with patch.object(self.widget._fallback_reconnect_timer, "start") as reconnect:
            self.widget.eventFilter(self.widget, QMoveEvent(self.widget.pos(), self.widget.pos()))
        reconnect.assert_not_called()

    def test_actual_logical_resize_queues_once_even_if_watchdog_repeats_it(self):
        self.hosted()
        with patch.object(self.widget, "_logical_canvas_size", return_value=(1000, 650)), \
                patch.object(self.widget, "_rdp_auth_dialog_hwnd", return_value=None), \
                patch.object(self.widget._fallback_reconnect_timer, "start") as reconnect:
            self.widget._queue_fallback_resize()
            self.widget._queue_fallback_resize()
        reconnect.assert_called_once()

    def test_dpi_geometry_adjustment_does_not_reconnect_and_later_resize_still_does(self):
        self.hosted()
        self.widget._last_fallback_canvas_dpr = 1.0
        with patch.object(self.widget.canvas, "devicePixelRatioF", return_value=1.5), \
                patch.object(self.widget, "_logical_canvas_size", return_value=(1200, 700)), \
                patch.object(self.widget._fallback_reconnect_timer, "start") as reconnect:
            self.widget._queue_fallback_resize()
        reconnect.assert_not_called()
        self.assertEqual(self.widget._last_fallback_canvas_size, (1200, 700))
        with patch.object(self.widget.canvas, "devicePixelRatioF", return_value=1.5), \
                patch.object(self.widget, "_logical_canvas_size", return_value=(1400, 800)), \
                patch.object(self.widget, "_rdp_auth_dialog_hwnd", return_value=None), \
                patch.object(self.widget._fallback_reconnect_timer, "start") as reconnect:
            self.widget._queue_fallback_resize()
        reconnect.assert_called_once()

    def test_one_pixel_dpi_rounding_does_not_schedule_reconnect(self):
        self.hosted()
        with patch.object(self.widget, "_logical_canvas_size", return_value=(801, 501)), \
                patch.object(self.widget._fallback_reconnect_timer, "start") as reconnect:
            self.widget._queue_fallback_resize()
        reconnect.assert_not_called()

    def test_returning_to_original_size_cancels_pending_reconnect(self):
        self.hosted()
        self.widget._pending_fallback_canvas_size = (1000, 650)
        with patch.object(self.widget._fallback_reconnect_timer, "stop") as stop:
            self.widget._queue_fallback_resize()
        stop.assert_called_once()
        self.assertIsNone(self.widget._pending_fallback_canvas_size)

    def test_delayed_relaunch_after_a_dpi_move_never_terminates_the_session(self):
        self.hosted()
        with patch.object(self.widget.canvas, "isVisible", return_value=True), \
                patch.object(self.widget, "_desired_fallback_desktop_size", return_value=(1600, 1000)), \
                patch.object(self.widget, "_disconnect_fallback_mstsc") as disconnect, \
                patch.object(self.widget, "_launch_fallback_mstsc") as launch:
            self.widget._relaunch_fallback_for_resize()
        disconnect.assert_not_called()
        launch.assert_not_called()

    def test_real_resize_relaunches_with_the_final_native_dimensions(self):
        self.hosted()
        self.widget._last_fallback_desktop = (800, 500)
        with patch.object(self.widget.canvas, "isVisible", return_value=True), \
                patch.object(self.widget, "_logical_canvas_size", return_value=(1000, 650)), \
                patch.object(self.widget, "_desired_fallback_desktop_size", return_value=(1500, 975)), \
                patch.object(self.widget, "_rdp_auth_dialog_hwnd", return_value=None), \
                patch.object(self.widget, "_disconnect_fallback_mstsc") as disconnect, \
                patch.object(self.widget, "_launch_fallback_mstsc") as launch:
            self.widget._relaunch_fallback_for_resize()
        disconnect.assert_called_once()
        launch.assert_called_once_with(1500, 975, reconnecting=True)

    def test_hidden_or_minimized_panel_does_not_relaunch(self):
        self.hosted()
        with patch.object(self.widget, "_logical_canvas_size", return_value=(1000, 650)), \
                patch.object(self.widget, "_disconnect_fallback_mstsc") as disconnect:
            self.widget._relaunch_fallback_for_resize()
        disconnect.assert_not_called()
        with patch.object(self.widget.canvas, "isVisible", return_value=True), \
                patch.object(self.widget, "isMinimized", return_value=True), \
                patch.object(self.widget, "_logical_canvas_size", return_value=(1000, 650)), \
                patch.object(self.widget, "_disconnect_fallback_mstsc") as disconnect:
            self.widget._relaunch_fallback_for_resize()
        disconnect.assert_not_called()

    def test_bad_password_prompt_and_fit_disabled_do_not_queue_reconnects(self):
        self.hosted()
        with patch.object(self.widget, "_logical_canvas_size", return_value=(1000, 650)), \
                patch.object(self.widget, "_rdp_auth_dialog_hwnd", return_value=999), \
                patch.object(self.widget._fallback_reconnect_timer, "start") as reconnect:
            self.widget._queue_fallback_resize()
        reconnect.assert_not_called()
        self.widget.chk_fit.setChecked(False)
        with patch.object(self.widget, "_logical_canvas_size", return_value=(1000, 650)), \
                patch.object(self.widget._fallback_reconnect_timer, "start") as reconnect:
            self.widget._queue_fallback_resize()
        reconnect.assert_not_called()

    def test_correct_size_at_wrong_offset_does_not_count_as_filling(self):
        with patch.object(rdp.user32, "GetWindowRect", side_effect=write_rect((3, 0, 1203, 750))), \
                patch.object(rdp.user32, "MapWindowPoints"):
            self.assertFalse(self.widget._window_fills_parent(1, 2, (1200, 750)))

    def test_surface_repair_visits_direct_children_once_and_leaves_dialogs_alone(self):
        parents = {20: 10, 30: 20, 40: 10, 50: 10}
        children = {10: [20, 30, 40, 50], 20: [30], 30: []}
        classes = {20: "UIMainClass", 30: "OPWindowClass", 40: "#32770", 50: "ScrollBar"}
        with patch.object(self.widget, "_enum_child_windows", side_effect=lambda h: children[h]), \
                patch.object(rdp.user32, "GetParent", side_effect=lambda h: parents[h.value]), \
                patch.object(rdp, "_window_class", side_effect=lambda h: classes[h]), \
                patch.object(rdp.user32, "GetClientRect", side_effect=write_rect((0, 0, 1200, 750))), \
                patch.object(self.widget, "_window_fills_parent", return_value=False), \
                patch.object(rdp.user32, "MoveWindow", return_value=True) as move:
            self.widget._force_fill_window_tree(10)
            self.assertEqual([call.args[0].value for call in move.call_args_list], [20, 30])

    def test_surfaces_that_already_fill_are_not_moved_or_repainted(self):
        with patch.object(self.widget, "_enum_child_windows", side_effect=lambda h: [20] if h == 10 else []), \
                patch.object(rdp.user32, "GetParent", return_value=10), \
                patch.object(rdp, "_window_class", return_value="UIMainClass"), \
                patch.object(rdp.user32, "GetClientRect", side_effect=write_rect((0, 0, 1200, 750))), \
                patch.object(self.widget, "_window_fills_parent", return_value=True), \
                patch.object(rdp.user32, "MoveWindow") as move:
            self.widget._force_fill_window_tree(10)
            move.assert_not_called()


if __name__ == "__main__":
    unittest.main()
