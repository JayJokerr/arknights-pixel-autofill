import unittest
from unittest import mock

from arknights_pixel import automation as app_module


class MouseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.mouse = app_module.GameMouse()
        self.mouse.target_hwnd = 123

    def test_target_coordinates_are_clamped_inside_client(self):
        with (
            mock.patch.object(app_module.win32gui, "IsWindow", return_value=True),
            mock.patch.object(app_module.win32gui, "GetClientRect", return_value=(0, 0, 1280, 720)),
            mock.patch.object(
                app_module.win32gui,
                "ClientToScreen",
                side_effect=lambda _hwnd, point: (100 + point[0], 200 + point[1]),
            ),
        ):
            self.assertEqual((102, 202), self.mouse._clamp_to_target(-5000, -5000))
            self.assertEqual((1377, 917), self.mouse._clamp_to_target(5000, 5000))

    def test_clearing_target_releases_cursor_clip(self):
        self.mouse.cursor_clipped = True
        with mock.patch.object(self.mouse, "release_cursor") as release:
            self.mouse.set_target(None)
        release.assert_called_once_with()

    def test_relative_input_is_only_enabled_for_native_unity(self):
        with mock.patch.object(app_module.win32gui, "GetClassName", return_value="QtRenderSurface"):
            self.mouse.set_target(123)
        self.assertFalse(self.mouse.relative_compat)

        with mock.patch.object(app_module.win32gui, "GetClassName", return_value="UnityWndClass"):
            self.mouse.set_target(456)
        self.assertTrue(self.mouse.relative_compat)


class PainterSurfaceStabilityTests(unittest.TestCase):
    def test_resolution_change_invalidates_active_coordinates(self):
        painter = app_module.AutoPainter(mock.Mock())
        painter.active_surface = (123, 100, 200, 1280, 720)
        with (
            mock.patch.object(app_module.win32gui, "IsWindow", return_value=True),
            mock.patch.object(app_module, "get_client_info", return_value=(100, 200, 1600, 900)),
        ):
            with self.assertRaisesRegex(RuntimeError, "分辨率已经变化"):
                painter._ensure_surface_stable()

    def test_paint_lock_rejects_second_fill_task(self):
        host = mock.Mock()
        painter = app_module.AutoPainter(host)
        painter.paint_lock.acquire()
        try:
            painter.paint([[0]], {}, .05, True)
        finally:
            painter.paint_lock.release()
        host.thread_status.assert_called_once()
        self.assertTrue(host.thread_status.call_args.kwargs["error"])


if __name__ == "__main__":
    unittest.main()
