"""User-owned Telegram bot notifications: Bot API client (mocked aiohttp), storage and channel routing."""

import asyncio
import unittest
from unittest.mock import patch

import aiohttp

from core import bot_notify
from core.bot_notify import (
    NOTIFY_CHANNEL_KEY,
    NOTIFY_TOKEN_KEY,
    BotNotifyError,
    clear_bot_token,
    detect_chat_id,
    load_bot_credentials,
    normalize_channel,
    safe_document_filename,
    save_bot_settings,
    send_document,
    send_document_to_bot,
    send_message,
    send_to_bot,
    validate_chat_id,
    validate_token,
)
from core.database import init_db
from core.secrets import PREFIX
from core.summary import (
    SUMMARY_ACCOUNT_KEY,
    SUMMARY_ENABLED_KEY,
    SUMMARY_MODE_KEY,
    SUMMARY_PENDING_KEY,
    deliver_task_summary,
    flush_daily,
)
from tests.support import DatabaseCase

TOKEN = "123456789:AAH-test_TOKEN_value_0123456789abcdef"
CHAT = "555000111"
ENGINE_SUMMARY = {
    "added": 4,
    "sent": 7,
    "followup_sent": 2,
    "skipped": 1,
    "errors": 3,
    "outcome": "completed",
    "duration": "0:12:34",
    "accounts": [{"phone": "+380501112233", "added": 4}],
    "recipients": ["@alice", "@bob"],
}


class FakeResponse:
    def __init__(self, status, data):
        self.status = status
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def json(self, content_type=None):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class FakeAiohttp:
    """Replaces ``aiohttp.ClientSession`` inside ``core.bot_notify``; records every request."""

    def __init__(self, status=200, data=None, error=None):
        self.status = status
        self.data = {"ok": True, "result": {}} if data is None else data
        self.error = error
        self.calls = []

    def session(self, *args, **kwargs):
        outer = self
        outer.timeout = kwargs.get("timeout")

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            def post(self, url, json=None, data=None, allow_redirects=True):
                outer.calls.append(
                    {"url": url, "json": json, "data": data, "allow_redirects": allow_redirects}
                )
                if outer.error:
                    raise outer.error
                return FakeResponse(outer.status, outer.data)

        return Session()

    def patch(self):
        return patch.object(bot_notify.aiohttp, "ClientSession", self.session)


class ValidationTests(unittest.TestCase):
    def test_token_and_chat_id_validation(self):
        self.assertEqual(validate_token(f"  {TOKEN} "), TOKEN)
        for bad in ("", "abc", "123:short", "no-colon-" + "a" * 40, TOKEN + " extra"):
            with self.assertRaises(BotNotifyError):
                validate_token(bad)
        self.assertEqual(validate_chat_id(" 42 "), "42")
        self.assertEqual(validate_chat_id("-100123456"), "-100123456")
        for bad in ("", "0", "abc", "12.5", "@name", "1" * 20):
            with self.assertRaises(BotNotifyError):
                validate_chat_id(bad)

    def test_channel_normalization(self):
        for value in ("off", "saved", "bot", "both"):
            self.assertEqual(normalize_channel(value), value)
        self.assertEqual(normalize_channel(" BOT "), "bot")
        for value in (None, "", "sms", 5):
            self.assertEqual(normalize_channel(value), "off")


class BotApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_posts_to_bot_api(self):
        fake = FakeAiohttp()
        with fake.patch():
            await send_message(TOKEN, CHAT, "Привет")
        ((call,),) = [(c,) for c in fake.calls]
        self.assertEqual(call["url"], f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(call["json"]["chat_id"], int(CHAT))
        self.assertEqual(call["json"]["text"], "Привет")
        self.assertFalse(call["allow_redirects"])
        self.assertEqual(fake.timeout.total, bot_notify.REQUEST_TIMEOUT)

    async def test_long_text_is_trimmed_and_empty_refused(self):
        fake = FakeAiohttp()
        with fake.patch():
            await send_message(TOKEN, CHAT, "x" * 9000)
            with self.assertRaises(BotNotifyError):
                await send_message(TOKEN, CHAT, "   ")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(fake.calls[0]["json"]["text"]), bot_notify.MAX_TEXT)

    async def test_http_errors_become_russian_messages_without_token(self):
        cases = {
            401: ({"ok": False, "description": "Unauthorized"}, "токен"),
            404: ({"ok": False, "description": "Not Found"}, "токен"),
            403: ({"ok": False, "description": "Forbidden: bot was blocked by the user"}, "«Запустить»"),
            400: ({"ok": False, "description": "Bad Request: chat not found"}, "Чат не найден"),
            429: ({"ok": False, "parameters": {"retry_after": 17}}, "17 сек"),
            409: ({"ok": False, "description": "Conflict: webhook"}, "webhook"),
            502: (None, "недоступен"),
        }
        for status, (data, expected) in cases.items():
            fake = FakeAiohttp(status=status, data=data if data is not None else {})
            with fake.patch(), self.assertRaises(BotNotifyError) as caught:
                await send_message(TOKEN, CHAT, "hi")
            message = str(caught.exception)
            self.assertIn(expected, message, status)
            self.assertNotIn(TOKEN, message)
            self.assertNotIn("AAH-test", message)

    async def test_ok_false_with_200_is_an_error(self):
        fake = FakeAiohttp(status=200, data={"ok": False, "description": "Bad Request: chat not found"})
        with fake.patch(), self.assertRaises(BotNotifyError):
            await send_message(TOKEN, CHAT, "hi")

    async def test_network_failures_never_leak_the_token(self):
        leaky = aiohttp.ClientConnectionError(f"Cannot connect to https://api.telegram.org/bot{TOKEN}/sendMessage")
        for error, expected in (
            (leaky, "api.telegram.org"),
            (asyncio.TimeoutError(), "не ответил"),
        ):
            fake = FakeAiohttp(error=error)
            with fake.patch(), self.assertRaises(BotNotifyError) as caught:
                await send_message(TOKEN, CHAT, "hi")
            self.assertIn(expected, str(caught.exception))
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)

    async def test_non_json_reply_is_an_error(self):
        fake = FakeAiohttp(status=200, data=ValueError("not json"))
        with fake.patch(), self.assertRaises(BotNotifyError):
            await send_message(TOKEN, CHAT, "hi")

    async def test_invalid_token_is_rejected_before_any_request(self):
        fake = FakeAiohttp()
        with fake.patch(), self.assertRaises(BotNotifyError):
            await send_message("bad", CHAT, "hi")
        self.assertEqual(fake.calls, [])

    async def test_send_document_uses_multipart_and_safe_filename(self):
        fake = FakeAiohttp()
        with fake.patch():
            await send_document(TOKEN, CHAT, "list@bad name!.txt", "@alice\n@bob\n", caption="Парсинг · 2")
        call = fake.calls[0]
        self.assertTrue(call["url"].endswith("/sendDocument"))
        self.assertIsNone(call["json"])
        self.assertIsNotNone(call["data"])
        self.assertFalse(call["allow_redirects"])
        self.assertEqual(fake.timeout.total, bot_notify.DOCUMENT_TIMEOUT)
        by_name = {}
        for options, headers, value in call["data"]._fields:
            by_name[options["name"]] = (options, headers, value)
        self.assertEqual(by_name["chat_id"][2], CHAT)
        self.assertEqual(by_name["caption"][2], "Парсинг · 2")
        options, headers, value = by_name["document"]
        self.assertEqual(options["filename"], safe_document_filename("list@bad name!.txt"))
        self.assertEqual(value, b"@alice\n@bob\n")
        self.assertEqual(headers.get("Content-Type"), "text/plain; charset=utf-8")

    async def test_send_document_rejects_empty_and_oversized(self):
        fake = FakeAiohttp()
        with fake.patch(), self.assertRaises(BotNotifyError):
            await send_document(TOKEN, CHAT, "empty.txt", "")
        with fake.patch(), self.assertRaises(BotNotifyError) as caught:
            await send_document(TOKEN, CHAT, "big.txt", b"x" * (bot_notify.MAX_DOCUMENT_BYTES + 1))
        self.assertIn("МБ", str(caught.exception))
        self.assertEqual(fake.calls, [])

    def test_safe_document_filename(self):
        self.assertEqual(safe_document_filename("a b/@c!.TXT"), "a_b_c_.TXT")
        self.assertTrue(safe_document_filename("list").endswith(".txt"))
        self.assertEqual(safe_document_filename(""), "parsed.txt")


class DetectChatTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def update(chat_id, text="hi", kind="private"):
        return {"update_id": 1, "message": {"text": text, "chat": {"id": chat_id, "type": kind}}}

    async def test_prefers_the_chat_that_wrote_start(self):
        updates = [self.update(111, "/start"), self.update(222, "hello"), self.update(-100500, "/start", "group")]
        fake = FakeAiohttp(data={"ok": True, "result": updates})
        with fake.patch():
            self.assertEqual(await detect_chat_id(TOKEN), "111")
        self.assertTrue(fake.calls[0]["url"].endswith("/getUpdates"))
        self.assertEqual(fake.calls[0]["json"]["timeout"], 0)

    async def test_falls_back_to_latest_private_chat(self):
        updates = [self.update(111), self.update(222), {"update_id": 3, "edited_message": {}}]
        fake = FakeAiohttp(data={"ok": True, "result": updates})
        with fake.patch():
            self.assertEqual(await detect_chat_id(TOKEN), "222")

    async def test_no_private_messages_gives_a_hint(self):
        for result in ([], [self.update(-1, "/start", "group")], None, [{"x": 1}], ["junk"]):
            fake = FakeAiohttp(data={"ok": True, "result": result})
            with fake.patch(), self.assertRaises(BotNotifyError) as caught:
                await detect_chat_id(TOKEN)
            self.assertIn("/start", str(caught.exception))


class StorageTests(DatabaseCase):
    async def test_token_is_protected_and_roundtrips(self):
        await save_bot_settings(self.db, token=TOKEN, chat_id=CHAT)
        stored = await self.db.get_setting(NOTIFY_TOKEN_KEY)
        self.assertTrue(stored.startswith(PREFIX))
        self.assertNotIn(TOKEN, stored)
        self.assertEqual(await load_bot_credentials(self.db), (TOKEN, CHAT))

    async def test_saving_without_token_keeps_the_saved_one(self):
        await save_bot_settings(self.db, token=TOKEN, chat_id=CHAT)
        await save_bot_settings(self.db, token="", chat_id="777")
        self.assertEqual(await load_bot_credentials(self.db), (TOKEN, "777"))

    async def test_invalid_input_is_not_stored(self):
        with self.assertRaises(BotNotifyError):
            await save_bot_settings(self.db, token="garbage", chat_id=CHAT)
        with self.assertRaises(BotNotifyError):
            await save_bot_settings(self.db, token=TOKEN, chat_id="abc")
        self.assertFalse(await self.db.get_setting(NOTIFY_TOKEN_KEY))

    async def test_clear_token_and_missing_credentials(self):
        with self.assertRaises(BotNotifyError):
            await load_bot_credentials(self.db)
        await save_bot_settings(self.db, token=TOKEN, chat_id=CHAT)
        await clear_bot_token(self.db)
        with self.assertRaises(BotNotifyError):
            await load_bot_credentials(self.db)

    async def test_send_to_bot_uses_stored_credentials(self):
        await save_bot_settings(self.db, token=TOKEN, chat_id=CHAT)
        fake = FakeAiohttp()
        with fake.patch():
            await send_to_bot(self.db, "Тест")
        self.assertIn(f"/bot{TOKEN}/sendMessage", fake.calls[0]["url"])
        self.assertEqual(fake.calls[0]["json"]["chat_id"], int(CHAT))

    async def test_send_to_bot_hides_unexpected_errors(self):
        await save_bot_settings(self.db, token=TOKEN, chat_id=CHAT)
        with patch.object(bot_notify, "send_message", side_effect=OSError(f"boom {TOKEN}")):
            with self.assertRaises(BotNotifyError) as caught:
                await send_to_bot(self.db, "x")
        self.assertNotIn(TOKEN, str(caught.exception))

    async def test_send_document_to_bot_uses_stored_credentials(self):
        await save_bot_settings(self.db, token=TOKEN, chat_id=CHAT)
        fake = FakeAiohttp()
        with fake.patch():
            await send_document_to_bot(self.db, "people.txt", "@a\n", caption="2 строки")
        self.assertIn(f"/bot{TOKEN}/sendDocument", fake.calls[0]["url"])
        self.assertIsNotNone(fake.calls[0]["data"])

    async def test_send_document_to_bot_hides_unexpected_errors(self):
        await save_bot_settings(self.db, token=TOKEN, chat_id=CHAT)
        with patch.object(bot_notify, "send_document", side_effect=OSError(f"boom {TOKEN}")):
            with self.assertRaises(BotNotifyError) as caught:
                await send_document_to_bot(self.db, "x.txt", "x")
        self.assertNotIn(TOKEN, str(caught.exception))

    async def test_init_db_protects_plaintext_token_and_maps_legacy_flag(self):
        await self.db.set_setting(NOTIFY_TOKEN_KEY, TOKEN)  # as if written by an older/manual build
        await self.db.set_setting(SUMMARY_ENABLED_KEY, "1")
        await init_db()
        stored = await self.db.get_setting(NOTIFY_TOKEN_KEY)
        self.assertTrue(stored.startswith(PREFIX))
        self.assertEqual(await self.db.get_setting(NOTIFY_CHANNEL_KEY), "saved")
        await init_db()  # idempotent: no double encryption, an existing channel is kept
        self.assertEqual(await self.db.get_setting(NOTIFY_TOKEN_KEY), stored)
        await self.db.set_setting(NOTIFY_CHANNEL_KEY, "both")
        await init_db()
        self.assertEqual(await self.db.get_setting(NOTIFY_CHANNEL_KEY), "both")


class ChannelRoutingTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.saved, self.bot = [], []
        self.saved_fails = self.bot_fails = False

        async def saved_sender(_db, account_id, text):
            if self.saved_fails:
                raise RuntimeError("offline")
            self.saved.append((account_id, text))

        async def bot_sender(_db, text):
            if self.bot_fails:
                raise BotNotifyError("Telegram не ответил за 15 сек. Проверьте интернет и повторите.")
            self.bot.append(text)

        self.kw = {"sender": saved_sender, "bot_sender": bot_sender}

    async def configure(self, channel, *, mode="task", account="7", bot=True):
        await self.db.set_setting(NOTIFY_CHANNEL_KEY, channel)
        await self.db.set_setting(SUMMARY_MODE_KEY, mode)
        await self.db.set_setting(SUMMARY_ACCOUNT_KEY, account)
        if bot:
            await save_bot_settings(self.db, token=TOKEN, chat_id=CHAT)

    async def test_off_sends_nothing_even_with_legacy_flag_absent(self):
        await self.configure("off")
        self.assertEqual(await deliver_task_summary(self.db, ENGINE_SUMMARY, **self.kw), "disabled")
        self.assertEqual((self.saved, self.bot), ([], []))

    async def test_saved_channel_keeps_working_without_bot(self):
        await self.configure("saved", bot=False)
        self.assertEqual(await deliver_task_summary(self.db, ENGINE_SUMMARY, **self.kw), "sent")
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.bot, [])

    async def test_legacy_enabled_flag_means_saved_messages(self):
        await self.db.set_setting(SUMMARY_ENABLED_KEY, "1")
        await self.db.set_setting(SUMMARY_ACCOUNT_KEY, "7")
        self.assertEqual(await deliver_task_summary(self.db, ENGINE_SUMMARY, **self.kw), "sent")
        self.assertEqual((len(self.saved), self.bot), (1, []))

    async def test_bot_channel_sends_only_through_bot(self):
        await self.configure("bot", account="")
        self.assertEqual(await deliver_task_summary(self.db, ENGINE_SUMMARY, **self.kw), "sent")
        self.assertEqual(self.saved, [])
        self.assertIn("Отправлено: 9", self.bot[0])

    async def test_both_sends_same_text_to_both_channels(self):
        await self.configure("both")
        self.assertEqual(await deliver_task_summary(self.db, ENGINE_SUMMARY, **self.kw), "sent")
        self.assertEqual(self.saved[0][1], self.bot[0])

    async def test_both_survives_one_failing_channel_and_logs_it(self):
        await self.configure("both")
        logs = []
        self.bot_fails = True
        status = await deliver_task_summary(
            self.db, ENGINE_SUMMARY, log=lambda level, text: logs.append(text), **self.kw
        )
        self.assertEqual((status, len(self.saved)), ("sent", 1))
        self.assertTrue(any("через бота" in line for line in logs))
        self.saved_fails, self.bot_fails = True, True
        self.assertEqual(await deliver_task_summary(self.db, ENGINE_SUMMARY, **self.kw), "failed")

    async def test_missing_setup_statuses(self):
        await self.configure("bot", bot=False)
        self.assertEqual(await deliver_task_summary(self.db, ENGINE_SUMMARY, **self.kw), "no_bot")
        await self.configure("saved", account="", bot=False)
        self.assertEqual(await deliver_task_summary(self.db, ENGINE_SUMMARY, **self.kw), "no_account")
        await self.configure("both", account="", bot=True)  # bot alone is enough for «both»
        self.assertEqual(await deliver_task_summary(self.db, ENGINE_SUMMARY, **self.kw), "sent")
        self.assertEqual((self.saved, len(self.bot)), ([], 1))

    async def test_text_is_counters_only(self):
        await self.configure("bot")
        await deliver_task_summary(self.db, {**ENGINE_SUMMARY, "phone": "+380501112233"}, **self.kw)
        text = self.bot[0]
        for secret in ("+380501112233", "380501112233", "@alice", "@bob", "alice", TOKEN, CHAT):
            self.assertNotIn(secret, text)

    async def test_daily_mode_flushes_through_bot(self):
        await self.configure("bot", mode="daily")
        first = await deliver_task_summary(self.db, ENGINE_SUMMARY, today="2026-10-26", **self.kw)
        self.assertEqual((first, self.bot), ("queued", []))
        self.assertEqual(await flush_daily(self.db, today="2026-10-27", **self.kw), "sent")
        self.assertIn("2026-10-26", self.bot[0])
        self.assertEqual(await self.db.get_setting(SUMMARY_PENDING_KEY), "")

    async def test_daily_flush_failure_keeps_counters_for_retry(self):
        await self.configure("bot", mode="daily")
        await deliver_task_summary(self.db, ENGINE_SUMMARY, today="2026-10-26", **self.kw)
        self.bot_fails = True
        self.assertEqual(await flush_daily(self.db, today="2026-10-27", **self.kw), "failed")
        self.assertTrue(await self.db.get_setting(SUMMARY_PENDING_KEY))
        self.bot_fails = False
        self.assertEqual(await flush_daily(self.db, today="2026-10-27", **self.kw), "sent")

    async def test_end_to_end_with_mocked_aiohttp_and_no_token_in_logs(self):
        await self.configure("bot", account="")
        fake = FakeAiohttp()
        with fake.patch():
            status = await deliver_task_summary(self.db, ENGINE_SUMMARY, sender=self.kw["sender"])
        self.assertEqual(status, "sent")
        self.assertEqual(fake.calls[0]["json"]["chat_id"], int(CHAT))
        self.assertNotIn("@alice", fake.calls[0]["json"]["text"])
        logs = []
        failing = FakeAiohttp(error=aiohttp.ClientConnectionError(f"https://api.telegram.org/bot{TOKEN}/sendMessage"))
        with failing.patch():
            status = await deliver_task_summary(
                self.db, ENGINE_SUMMARY, sender=self.kw["sender"], log=lambda level, text: logs.append(text)
            )
        self.assertEqual(status, "failed")
        self.assertTrue(logs)
        self.assertFalse(any(TOKEN in line for line in logs))


if __name__ == "__main__":
    unittest.main()
