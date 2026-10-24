import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.backup import create_backup, validate_backup
from core.database import Database, init_db
from core.export import write_logs
from tests.support import DatabaseCase
from ui.tab_accounts import AccountsTab
from ui.tab_logs import format_log, matches_log


class TemplateAndCampaignTests(DatabaseCase):
    async def test_templates_preserve_literal_text_and_are_shared_between_campaigns(self):
        text = "Привет 😀\nhttps://t.me/example\n**обычный текст**"
        key = await self.db.save_message_template("Встреча", text)
        other = self.db.for_campaign(await self.db.create_campaign("Другая", "messages"))
        rows = await other.get_message_templates()
        self.assertEqual((rows[0]["id"], rows[0]["text"]), (key, text))
        await self.db.save_message_template("Новое название", "Новый текст", key)
        self.assertEqual((await Database().get_message_templates())[0]["text"], "Новый текст")
        await other.delete_message_template(key)
        self.assertEqual(await self.db.get_message_templates(), [])

    async def test_invalid_template_and_duplicate_name_leave_original_unchanged(self):
        key = await self.db.save_message_template("Welcome", "Original")
        for name, text in (
            ("welcome", "Other"),
            ("", "text"),
            ("x" * 81, "text"),
            ("Valid", " "),
            ("Valid", "😀" * 2049),
        ):
            with self.assertRaises(ValueError):
                await self.db.save_message_template(name, text)
        with self.assertRaises(ValueError):
            await self.db.save_message_template("Missing", "text", key + 100)
        self.assertEqual(
            [(row["name"], row["text"]) for row in await self.db.get_message_templates()], [("Welcome", "Original")]
        )

    async def test_template_survives_idempotent_startup_and_backup(self):
        await self.db.save_message_template("Сохранённый", "Длинный текст\n" * 100)
        await init_db()
        await init_db()
        rows = await self.db.get_message_templates()
        self.assertEqual(len(rows), 1)
        backup = create_backup(self.root / "copy.db")
        validate_backup(backup)
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(conn.execute("SELECT text FROM message_templates").fetchone()[0], rows[0]["text"])

    async def test_copy_settings_preserves_kind_but_not_targets_logs_running_state_or_global_settings(self):
        campaign_id = await self.db.create_campaign("Источник", "messages")
        source = self.db.for_campaign(campaign_id)
        await source.save_campaign_settings(
            {"message_text": "Текст", "interval_min": "120", "selected_accounts": "[1]"}
        )
        await source.bulk_insert_targets(["alice"])
        await source.add_log(None, "alice", "MESSAGE_SENT", "История")
        await source.set_setting("task_status", "running")
        run_id = await source.begin_run("")
        copied_id = await source.copy_campaign_settings("Копия")
        copied = self.db.for_campaign(copied_id)
        self.assertEqual(await copied.get_campaign_kind(), "messages")
        self.assertEqual(await copied.get_setting("message_text"), "Текст")
        self.assertEqual(await copied.get_setting("interval_min"), "120")
        self.assertEqual(await copied.get_setting("task_status", "stopped"), "stopped")
        self.assertEqual(await copied.count_targets(), 0)
        self.assertEqual(await copied.get_logs(), [])
        self.assertEqual(await copied.get_runs(), [])
        self.assertEqual((await source.get_runs())[0]["id"], run_id)
        await copied.set_setting("message_text", "Копия изменена")
        self.assertEqual(await source.get_setting("message_text"), "Текст")

    async def test_rename_invalid_copy_and_missing_source_are_safe(self):
        await self.db.rename_campaign("  Новое название  ")
        self.assertEqual((await self.db.get_campaigns())[0]["name"], "Новое название")
        count = len(await self.db.get_campaigns())
        for name in ("", "x" * 81):
            with self.assertRaises(ValueError):
                await self.db.copy_campaign_settings(name)
        with self.assertRaises(ValueError):
            await self.db.for_campaign(9999).copy_campaign_settings("Missing")
        self.assertEqual(len(await self.db.get_campaigns()), count)


class LogFilterTests(unittest.TestCase):
    def test_export_uses_readable_labels_and_failed_format_keeps_existing_file(self):
        row = {"action": "DISCONNECT", "message": "Account disconnected", "created_at": "2026-10-22"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "журнал.txt"
            write_logs(str(path), [row], format_log)
            contents = path.read_text(encoding="utf-8")
            self.assertIn("Рабочее соединение закрыто", contents)
            self.assertIn("аккаунт сохранён", contents)
            self.assertNotIn("DISCONNECT", contents)
            with self.assertRaises(RuntimeError):
                write_logs(str(path), [row], lambda _: (_ for _ in ()).throw(RuntimeError("test")))
            self.assertEqual(path.read_text(encoding="utf-8"), contents)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_search_matches_translated_action_phone_username_and_message_case_insensitively(self):
        row = {
            "action": "MESSAGE_PRIVATE",
            "account_phone": "+380500000000",
            "username": "Alice",
            "message": "Причина",
            "created_at": "2026-10-14",
        }
        for query in ("личные сообщения", "+3805", "@ALICE", "причина"):
            self.assertTrue(matches_log(row, query, "Все события"))
        self.assertFalse(matches_log(row, "bobby", "Все события"))
        self.assertTrue(matches_log(row, "", "Пропуски"))
        self.assertFalse(matches_log(row, "", "Успешные действия"))
        self.assertIn("Личные сообщения закрыты", format_log(row))

    def test_unknown_outcomes_and_restrictions_are_in_error_filter(self):
        for action in ("MESSAGE_UNCERTAIN", "MESSAGE_STOPPED", "FLOOD_WAIT", "ADD_ERROR", "CONNECT_FAILED"):
            self.assertTrue(matches_log({"action": action}, "", "Ошибки и ожидание"))
        self.assertFalse(matches_log({"action": "MESSAGE_SENT"}, "", "Ошибки и ожидание"))
        self.assertFalse(matches_log({"action": "FOLLOWUP_WAIT"}, "", "Ошибки и ожидание"))
        self.assertTrue(matches_log({"action": "FOLLOWUP_WAIT"}, "", "Ожидание ответа"))
        self.assertTrue(matches_log({"action": "FOLLOWUP_REPLY"}, "", "Ожидание ответа"))
        self.assertIn("CUSTOM", format_log({"action": "CUSTOM"}))

    def test_old_connection_and_telegram_error_logs_are_readable(self):
        failed_connect = format_log({"action": "CONNECT_FAILED", "message": "Проверьте интернет"})
        self.assertIn("Не удалось подключить аккаунт", failed_connect)
        old_disconnect = format_log({"action": "DISCONNECT", "message": "Account disconnected"})
        self.assertIn("Рабочее соединение закрыто", old_disconnect)
        self.assertIn("аккаунт сохранён", old_disconnect)
        self.assertNotIn("Аккаунт отключён", old_disconnect)
        for old_message in ("PeerFlood: Telegram ограничил отправку", "FloodWait: Telegram требует ожидание 42 сек"):
            rendered = format_log({"action": "MESSAGE_STOPPED", "message": old_message})
            self.assertNotIn("PeerFlood", rendered)
            self.assertNotIn("FloodWait", rendered)


class AccountEditTests(DatabaseCase):
    async def edit(self, account_id, payload):
        await AccountsTab._save_account_edits(SimpleNamespace(db=self.db), account_id, payload)

    async def test_manual_status_change_cannot_cancel_telegram_pause(self):
        account = await self.add_account()
        await self.db.set_flood_wait(account["id"], 120)
        before = await self.db.get_account(account["id"])
        with self.assertRaisesRegex(ValueError, "Telegram назначил паузу"):
            await self.edit(account["id"], {"status": "active", "daily_limit": 12})
        after = await self.db.get_account(account["id"])
        self.assertEqual(after["status"], "flood")
        self.assertEqual(after["flood_until"], before["flood_until"])
        self.assertEqual(after["daily_limit"], before["daily_limit"])
        await self.edit(account["id"], {"daily_limit": 12})
        self.assertEqual((await self.db.get_account(account["id"]))["flood_until"], before["flood_until"])
        await self.db.update_account(
            account["id"], {"flood_until": (datetime.now() - timedelta(seconds=1)).isoformat()}
        )
        await self.edit(account["id"], {"status": "active"})
        self.assertEqual((await self.db.get_account(account["id"]))["status"], "active")

    async def test_proxy_edits_require_idle_session_and_preserve_password(self):
        account = await self.add_account()
        proxy = {
            "proxy_type": "SOCKS5",
            "proxy_host": "127.0.0.1",
            "proxy_port": 1080,
            "proxy_login": "user",
            "proxy_password": " secret ",
        }
        with patch("ui.tab_accounts.AccountManager.is_session_busy", return_value=True):
            with self.assertRaisesRegex(ValueError, "Сначала остановите"):
                await self.edit(account["id"], proxy)
            self.assertIsNone((await self.db.get_account(account["id"]))["proxy_host"])
            await self.edit(account["id"], {"daily_limit": 10})
        with patch("ui.tab_accounts.AccountManager.is_session_busy", return_value=False):
            await self.edit(account["id"], proxy)
        saved = await self.db.get_account(account["id"])
        for key, value in proxy.items():
            self.assertEqual(saved[key], value)
        self.assertEqual(saved["daily_limit"], 10)
