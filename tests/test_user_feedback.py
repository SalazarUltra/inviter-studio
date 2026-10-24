import unittest

from telethon.errors import AuthKeyUnregisteredError, FloodWaitError, PeerFloodError, UsernameNotOccupiedError

from core.user_feedback import explain_error


class UserFeedbackTests(unittest.TestCase):
    def test_telegram_errors_are_actionable_russian(self):
        cases = (
            (FloodWaitError(None, capture=42), "42 сек"),
            (PeerFloodError(None), "Telegram отклонил"),
            (AuthKeyUnregisteredError(None), "Подключите аккаунт заново"),
            (UsernameNotOccupiedError(None), "Никнейм не найден"),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                result = explain_error(error)
                self.assertIn(expected, result)
                self.assertNotIn(type(error).__name__, result)

    def test_known_russian_validation_and_unknown_english_errors(self):
        self.assertEqual(explain_error(ValueError("Введите текст сообщения")), "Введите текст сообщения")
        self.assertEqual(explain_error(RuntimeError("PeerFlood: Telegram отказал")), "отказ Telegram: Telegram отказал")
        self.assertNotIn("socket", explain_error(OSError("socket connect failed")))
