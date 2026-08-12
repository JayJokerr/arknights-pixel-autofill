import unittest
from unittest import mock

import arknights_pixel_autofill as app_module


class WindowSelectionTests(unittest.TestCase):
    def test_window_picker_lists_real_windows_and_excludes_tool_instances(self):
        windows = {
            10: ("明日方舟", 501, 1280, 720, "UnityWndClass"),
            11: ("雷电模拟器", 502, 1600, 900, "LDPlayerMainFrame"),
            12: ("明日方舟 24×24 像素画自动填色", 503, 1180, 760, "TkTopLevel"),
            13: ("tiny", 504, 320, 200, "Other"),
        }

        def enumerate_windows(callback, extra):
            for hwnd in windows:
                callback(hwnd, extra)

        with (
            mock.patch.object(app_module.os, "getpid", return_value=999),
            mock.patch.object(app_module.win32gui, "EnumWindows", side_effect=enumerate_windows),
            mock.patch.object(app_module.win32gui, "IsWindowVisible", return_value=True),
            mock.patch.object(app_module.win32gui, "GetWindowText", side_effect=lambda hwnd: windows[hwnd][0]),
            mock.patch.object(
                app_module.win32process,
                "GetWindowThreadProcessId",
                side_effect=lambda hwnd: (1, windows[hwnd][1]),
            ),
            mock.patch.object(
                app_module.win32gui,
                "GetClientRect",
                side_effect=lambda hwnd: (0, 0, windows[hwnd][2], windows[hwnd][3]),
            ),
            mock.patch.object(app_module.win32gui, "GetClassName", side_effect=lambda hwnd: windows[hwnd][4]),
        ):
            result = app_module.list_selectable_windows()

        self.assertEqual([10, 11], [item["hwnd"] for item in result])

    def test_selected_window_is_used_without_title_matching(self):
        target = {"hwnd": 77, "pid": 700, "title": "任意模拟器窗口"}
        with (
            mock.patch.object(app_module.win32gui, "IsWindow", return_value=True),
            mock.patch.object(app_module.win32gui, "GetWindowText", return_value="任意模拟器窗口"),
        ):
            hwnd, title = app_module.resolve_selected_window(target)
        self.assertEqual((77, "任意模拟器窗口"), (hwnd, title))

    def test_visual_candidates_include_shell_child_and_overlapping_surface(self):
        # Brand/class names do not influence discovery.  Include a cross-PID
        # child plus a same-process overlapping top-level render surface.
        windows = {
            70: ("任意安卓容器", 700, 1600, 950, (100, 80), "Shell", True),
            71: ("", 701, 1500, 844, (150, 130), "ChildRender", True),
            72: ("render", 702, 1536, 864, (120, 120), "SeparateRender", True),
            73: ("辅助面板", 700, 700, 700, (1700, 100), "Helper", True),
        }

        def enumerate_windows(callback, extra):
            for hwnd in windows:
                callback(hwnd, extra)

        with (
            mock.patch.object(app_module.win32gui, "IsWindow", side_effect=lambda hwnd: hwnd in windows),
            mock.patch.object(app_module.win32gui, "IsWindowVisible", side_effect=lambda hwnd: windows[hwnd][6]),
            mock.patch.object(app_module.win32gui, "GetWindowText", side_effect=lambda hwnd: windows[hwnd][0]),
            mock.patch.object(app_module.win32gui, "GetClassName", side_effect=lambda hwnd: windows[hwnd][5]),
            mock.patch.object(
                app_module.win32process, "GetWindowThreadProcessId",
                side_effect=lambda hwnd: (1, windows[hwnd][1]),
            ),
            mock.patch.object(
                app_module.win32gui, "GetClientRect",
                side_effect=lambda hwnd: (0, 0, windows[hwnd][2], windows[hwnd][3]),
            ),
            mock.patch.object(
                app_module.win32gui, "ClientToScreen",
                side_effect=lambda hwnd, point: (
                    windows[hwnd][4][0] + point[0], windows[hwnd][4][1] + point[1]
                ),
            ),
            mock.patch.object(app_module.win32gui, "EnumWindows", side_effect=enumerate_windows),
            mock.patch.object(
                app_module.win32gui, "EnumChildWindows",
                side_effect=lambda _host, callback, extra: callback(71, extra),
            ),
            mock.patch.object(app_module.win32gui, "GetWindow", return_value=0),
        ):
            candidates = app_module.list_visual_surface_candidates(70)

        self.assertEqual({70, 71}, {item["hwnd"] for item in candidates})
        # Merely overlapping the selected window is not a relationship: this
        # prevents an unrelated foreground app from becoming an input target.
        self.assertNotIn(72, {item["hwnd"] for item in candidates})
        self.assertNotIn(73, {item["hwnd"] for item in candidates})


if __name__ == "__main__":
    unittest.main()
