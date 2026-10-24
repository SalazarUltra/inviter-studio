"""Notifications are local, optional, bounded and do not break task completion."""

import ctypes
import unittest
from unittest.mock import Mock, patch

from ui.notifications import NotifyIconData, WindowsNotifications, utf16_clip


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.root = Mock()
        self.root.after.return_value = "timer"
        self.notifier = WindowsNotifications(self.root)
        self.shell = Mock(return_value=True)

    def prepare(self):
        self.notifier._data = NotifyIconData()
        self.notifier._shell = self.shell

    def test_disabled_makes_no_windows_call(self):
        self.notifier.enabled = False
        with patch.object(self.notifier, "_prepare") as prepare:
            self.assertFalse(self.notifier.show("Итог", "Проверка"))
            prepare.assert_not_called()

    def test_other_platform_uses_existing_in_app_results(self):
        with patch("ui.notifications.sys.platform", "linux"), patch.object(self.notifier, "_prepare") as prepare:
            self.assertFalse(self.notifier.show("Итог", "Проверка"))
            prepare.assert_not_called()

    def test_notification_bounded_and_cleanup_removes_icon(self):
        with (
            patch("ui.notifications.sys.platform", "win32"),
            patch.object(self.notifier, "_prepare", side_effect=self.prepare),
        ):
            self.assertTrue(self.notifier.show("Я" * 100, "Т" * 300, warning=True))
            data = self.notifier._data
            self.assertEqual(len(data.szInfoTitle), 63)
            self.assertEqual(len(data.szInfo), 255)
            self.assertEqual(data.dwInfoFlags, 0x82)
            self.assertEqual(self.shell.call_args.args[0], 1)
            self.notifier.close()
            self.assertEqual(self.shell.call_args.args[0], 2)
            self.root.after_cancel.assert_called_once_with("timer")
            self.assertIsNone(self.notifier._data)

    def test_shell_failure_is_nonfatal(self):
        with (
            patch("ui.notifications.sys.platform", "win32"),
            patch.object(self.notifier, "_prepare", side_effect=OSError("test")),
        ):
            self.assertFalse(self.notifier.show("Итог", "Проверка"))
        self.assertIsNone(self.notifier._data)

    def test_emoji_truncation_does_not_split_utf16(self):
        self.assertEqual(utf16_clip("а😀б", 2), "а")
        self.assertEqual(utf16_clip("а😀б", 3), "а😀")
        self.assertEqual(utf16_clip("а\0б", 5), "аб")

    @unittest.skipUnless(__import__("sys").platform == "win32", "Windows ABI")
    def test_native_structure_size(self):
        self.assertEqual(ctypes.sizeof(NotifyIconData), 976 if ctypes.sizeof(ctypes.c_void_p) == 8 else 956)
