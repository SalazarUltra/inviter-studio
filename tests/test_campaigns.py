import asyncio
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from core.backup import apply_pending_restore, automatic_backup, create_backup, stage_restore, validate_backup
from core.diagnostics import preflight
from tests.support import DatabaseCase


class CampaignTests(DatabaseCase):
    async def test_logs_and_reports_are_scoped(self):
        other = self.db.for_campaign(await self.db.create_campaign("Вторая"))
        await self.db.add_log(None, "alice", "CHECK", "first")
        await other.add_log(None, "bobby", "CHECK", "second")
        self.assertEqual((await self.db.get_logs(limit=None))[0]["message"], "first")
        self.assertEqual((await other.get_recent_logs())[0]["message"], "second")
        await other.clear_logs()
        self.assertEqual(len(await self.db.get_logs()), 1)
        await other.bulk_insert_targets(["bobby"])
        self.assertEqual((await other.get_campaign_report())[0]["count"], 1)
        self.assertEqual(await self.db.get_campaign_report(), [])

    async def test_preflight_handles_midnight_without_mutating_counters(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        with closing(sqlite3.connect(self.root / "inviter.db")) as conn, conn:
            conn.execute("UPDATE accounts SET added_today=40, last_reset_date='2000-01-01'")
        report = await preflight(self.db, [account["id"]], "my_chat")
        self.assertTrue(report["ok"])
        self.assertEqual((await self.db.get_account(account["id"]))["added_today"], 40)

    async def test_same_username_independent_in_two_campaigns(self):
        other = self.db.for_campaign(await self.db.create_campaign("Вторая"))
        await self.db.bulk_insert_targets(["alice", "bobby"])
        await other.bulk_insert_targets(["alice"])
        target = await self.db.claim_next_pending_target()
        await self.db.mark_target(target["id"], "error")
        await other.reset_failed_targets()
        self.assertEqual((await other.get_targets())[0]["status"], "pending")
        self.assertEqual((await self.db.get_targets_stats())["errors"], 1)
        await other.clear_targets()
        self.assertEqual((await self.db.get_targets_stats())["total"], 2)

    async def test_cross_campaign_cannot_mark_or_record_success(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        target = await self.db.claim_next_pending_target()
        other = self.db.for_campaign(await self.db.create_campaign("Другая"))
        await other.mark_target(target["id"], "added")
        with self.assertRaises(RuntimeError):
            await other.record_success(target["id"], account["id"])
        self.assertEqual((await self.db.get_targets())[0]["status"], "processing")
        self.assertEqual((await self.db.get_account(account["id"]))["added_today"], 0)

    async def test_campaign_settings_isolated_global_ai_shared(self):
        other = self.db.for_campaign(await self.db.create_campaign("Вторая"))
        await self.db.set_setting("target_chat", "first_chat")
        await other.set_setting("target_chat", "other_chat")
        await self.db.set_setting("ai_memory_enabled", "0")
        self.assertEqual(await self.db.get_setting("target_chat"), "first_chat")
        self.assertEqual(await other.get_setting("target_chat"), "other_chat")
        self.assertEqual(await other.get_setting("ai_memory_enabled"), "0")

    async def test_search_pagination_literal_wildcards_and_status(self):
        await self.db.bulk_insert_targets([f"person_{i:04d}" for i in range(205)])
        rows = await self.db.get_targets(limit=100, offset=100)
        first = await self.db.get_targets(limit=100)
        self.assertFalse({r["id"] for r in rows} & {r["id"] for r in first})
        self.assertEqual(await self.db.count_targets(search="PERSON_"), 205)
        self.assertEqual(await self.db.count_targets(search="%"), 0)
        self.assertEqual(await self.db.count_targets(search="person_0204"), 1)

    async def test_run_history_isolated_and_restart_marks_interrupted(self):
        run = await self.db.begin_run("my_chat")
        await self.db.finish_run(run, {"outcome": "completed", "added": 2})
        other = self.db.for_campaign(await self.db.create_campaign("Вторая"))
        self.assertEqual(await other.get_runs(), [])
        await other.begin_run("other_chat")
        from core.database import init_db

        await init_db()
        self.assertEqual((await other.get_runs())[0]["outcome"], "interrupted")
        self.assertEqual((await self.db.get_runs())[0]["outcome"], "completed")

    async def test_backup_preserves_wal_changes_and_restore_keeps_recovery_copy(self):
        await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        copy = await asyncio.to_thread(create_backup, self.root / "copy.db")
        await self.db.bulk_insert_targets(["bobby"])
        await asyncio.to_thread(stage_restore, copy)
        self.assertEqual((await self.db.get_targets_stats())["total"], 2)
        await asyncio.to_thread(apply_pending_restore)
        self.assertEqual((await self.db.get_targets_stats())["total"], 1)
        copies = list((self.root / "backups").glob("*.db"))
        self.assertEqual(len(copies), 1)
        with closing(sqlite3.connect(copies[0])) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0], 2)

    async def test_invalid_backup_cannot_replace_database(self):
        bad = self.root / "bad.db"
        bad.write_bytes(b"not a database")
        with self.assertRaises(sqlite3.DatabaseError):
            stage_restore(bad)
        self.assertFalse((self.root / "restore-pending.db").exists())
        with self.assertRaises(ValueError):
            create_backup(self.root / "inviter.db")

    async def test_legacy_backup_protects_secrets_without_changing_source(self):
        await self.add_account()
        with closing(sqlite3.connect(self.root / "inviter.db")) as conn:
            conn.execute("UPDATE accounts SET api_hash=?", ("a" * 32,))
            conn.commit()
        copy = create_backup(self.root / "copy.db")
        with closing(sqlite3.connect(copy)) as conn:
            secret = conn.execute("SELECT api_hash FROM accounts").fetchone()[0]
            if os.name == "nt":
                self.assertTrue(secret.startswith("dpapi:"))
        with closing(sqlite3.connect(self.root / "inviter.db")) as conn:
            self.assertEqual(conn.execute("SELECT api_hash FROM accounts").fetchone()[0], "a" * 32)

    async def test_automatic_backup_daily_and_retention(self):
        await self.db.bulk_insert_targets(["alice"])
        automatic_backup()
        automatic_backup()
        self.assertEqual(len(list((self.root / "backups").glob("auto-*.db"))), 1)
        for days in range(1, 10):
            date = datetime.now() - timedelta(days=days)
            create_backup(self.root / "backups" / f"auto-{date:%Y%m%d}-010101.db")
        automatic_backup()
        self.assertEqual(len(list((self.root / "backups").glob("auto-*.db"))), 7)
        for copy in (self.root / "backups").glob("*.db"):
            validate_backup(copy)

    async def test_offline_preview_never_connects_or_mutates_queue(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        with patch("core.diagnostics.AccountManager") as manager:
            report = await preflight(self.db, [account["id"]], "my_chat")
        manager.assert_not_called()
        self.assertTrue(report["ok"])
        self.assertEqual((await self.db.get_targets())[0]["status"], "pending")

    async def test_diagnostics_disabled_account_and_zero_quota(self):
        account = await self.add_account()
        await self.db.toggle_account_enabled(account["id"], False)
        await self.db.bulk_insert_targets(["alice"])
        report = await preflight(self.db, [account["id"]], "my_chat")
        self.assertFalse(report["ok"])

    async def test_live_preflight_disconnects_and_does_not_invite(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        manager = AsyncMock()
        manager.check_admin_rights.return_value = {"ok": True}
        with patch("core.diagnostics.AccountManager", return_value=manager):
            report = await preflight(self.db, [account["id"]], "my_chat", live=True)
        self.assertTrue(report["ok"])
        manager.disconnect.assert_awaited_once()
        manager.add_user_to_chat.assert_not_called()

    async def test_preflight_masks_connection_secrets_and_disconnects(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        manager = AsyncMock()
        manager.connect.side_effect = RuntimeError("secret-password")
        with patch("core.diagnostics.AccountManager", return_value=manager):
            report = await preflight(self.db, [account["id"]], "my_chat", live=True)
        self.assertFalse(report["ok"])
        self.assertNotIn("secret-password", str(report))
        manager.disconnect.assert_awaited_once()
