import unittest
from unittest.mock import patch

from ui.app import App


class AccountNotificationTests(unittest.TestCase):
    def test_account_alerts_are_rate_limited_per_reason(self):
        callbacks = []
        app = type(
            "NotificationHarness",
            (),
            {"_account_notice_times": {}, "_safe_after": lambda self, callback: callbacks.append(callback)},
        )()
        with patch("ui.app.time.monotonic", side_effect=[12, 40, 103]):
            App._notify_account_state(app, "flood", "пауза")
            App._notify_account_state(app, "flood", "пауза")
            App._notify_account_state(app, "login", "войти")
        self.assertEqual(len(callbacks), 2)


if __name__ == "__main__":
    unittest.main()
