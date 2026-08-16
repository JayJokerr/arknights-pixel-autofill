import unittest
from unittest import mock

from arknights_pixel import automation


class AdminStartupTests(unittest.TestCase):
    def test_elevated_process_continues_without_restarting(self):
        with (
            mock.patch.object(automation, "is_running_as_admin", return_value=True),
            mock.patch.object(automation, "restart_as_admin") as restart,
        ):
            self.assertTrue(automation.require_admin_before_startup())
        restart.assert_not_called()

    def test_unelevated_process_restarts_and_then_exits(self):
        with (
            mock.patch.object(automation, "is_running_as_admin", return_value=False),
            mock.patch.object(automation, "restart_as_admin", return_value=True) as restart,
            mock.patch.object(automation.ctypes.windll.user32, "MessageBoxW") as message_box,
        ):
            self.assertFalse(automation.require_admin_before_startup())
        restart.assert_called_once_with()
        message_box.assert_not_called()

    def test_source_restart_preserves_arguments(self):
        with (
            mock.patch.object(automation.sys, "frozen", False, create=True),
            mock.patch.object(automation.sys, "executable", r"C:\Python\python.exe"),
            mock.patch.object(
                automation.sys,
                "argv",
                [r"Z:\app folder\arknights_pixel_qt.py", "--sample", "a b"],
            ),
            mock.patch.object(automation.os, "getcwd", return_value=r"Z:\app folder"),
            mock.patch.object(
                automation.ctypes.windll.shell32,
                "ShellExecuteW",
                return_value=42,
            ) as shell_execute,
        ):
            self.assertTrue(automation.restart_as_admin())

        call = shell_execute.call_args.args
        self.assertEqual("runas", call[1])
        self.assertEqual(r"C:\Python\python.exe", call[2])
        self.assertIn('"Z:\\app folder\\arknights_pixel_qt.py"', call[3])
        self.assertIn('"a b"', call[3])
        self.assertEqual(r"Z:\app folder", call[4])


if __name__ == "__main__":
    unittest.main()
