import asyncio
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from unittest.mock import patch

from core.database import init_db
from tests.support import DatabaseCase


class DatabaseTests(DatabaseCase):
    async def test_parallel_claims_never_duplicate(self):
        await self.db.bulk_insert_targets([f"person_{i}" for i in range(20)])
        rows = await asyncio.gather(*(self.db.claim_next_pending_target() for _ in range(25)))
        claimed = [row for row in rows if row]
        self.assertEqual(len(claimed), 20)
        self.assertEqual(len({row["id"] for row in claimed}), 20)
        self.assertTrue(all(row["status"] == "processing" for row in claimed))

    async def test_import_normalizes_deduplicates_and_rejects_bad_batch(self):
        self.assertEqual(await self.db.bulk_insert_targets(["@Alice", "alice", "https://t.me/Bobby"]), 2)
        self.assertEqual(await self.db.bulk_insert_targets(["alice"]), 0)
        with self.assertRaises(ValueError):
            await self.db.bulk_insert_targets(["charlie", "../../bad"])
        self.assertEqual((await self.db.get_targets_stats())["total"], 2)

    async def test_success_is_atomic_and_idempotent(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        target = await self.db.claim_next_pending_target()
        await self.db.record_success(target["id"], account["id"])
        with self.assertRaises(RuntimeError):
            await self.db.record_success(target["id"], account["id"])
        fresh = await self.db.get_account(account["id"])
        self.assertEqual(fresh["added_today"], 1)
        self.assertEqual(fresh["consecutive_adds"], 1)
        self.assertEqual((await self.db.get_targets_stats())["added"], 1)

    async def test_success_rolls_counter_over_at_midnight(self):
        account = await self.add_account()
        with closing(sqlite3.connect(self.root / "inviter.db")) as db, db:
            db.execute("UPDATE accounts SET added_today=40, last_reset_date='2000-01-01'")
        await self.db.bulk_insert_targets(["alice"])
        target = await self.db.claim_next_pending_target()
        await self.db.record_success(target["id"], account["id"])
        self.assertEqual((await self.db.get_account(account["id"]))["added_today"], 1)

    async def test_stats_categories_do_not_overlap(self):
        names = ["added", "private", "not_found", "already_in", "error", "pending", "processing"]
        await self.db.bulk_insert_targets([f"user_{name}" for name in names])
        for row in await self.db.get_targets():
            await self.db.mark_target(row["id"], row["username"].removeprefix("user_"))
        stats = await self.db.get_targets_stats()
        self.assertEqual(stats["total"], 7)
        self.assertEqual(sum(stats[key] for key in ("added", "skipped", "errors", "pending", "processing")), 7)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["skipped"], 3)

    async def test_startup_recovers_processing_and_stale_states(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        await self.db.claim_next_pending_target()
        await self.db.set_setting("task_status", "running")
        session_id = await self.db.create_warmup_session(account["id"], "medium", "test", 7)
        await self.db.update_warmup_session(session_id, status="running")
        await init_db()
        self.assertEqual((await self.db.get_targets_stats())["pending"], 1)
        self.assertEqual(await self.db.get_setting("task_status"), "stopped")
        self.assertEqual((await self.db.get_warmup_session(account["id"]))["status"], "paused")

    async def test_channel_sync_preserves_selection(self):
        account = await self.add_account()
        channels = [{"id": -100123, "username": "group_one", "title": "One", "type": "group"}]
        await self.db.save_account_channels(account["id"], channels)
        channel = (await self.db.get_account_channels(account["id"]))[0]
        await self.db.toggle_channel_selected(channel["id"], True)
        await self.db.save_account_channels(account["id"], channels)
        self.assertEqual(len(await self.db.get_selected_channels(account["id"])), 1)

    async def test_invalid_activation_keeps_existing_config(self):
        key = await self.db.add_ai_config("test", "openai", "fake-key", "test-model")
        await self.db.set_active_ai_config(key)
        with self.assertRaises(ValueError):
            await self.db.set_active_ai_config(999)
        self.assertEqual((await self.db.get_active_ai_config())["id"], key)

    async def test_credentials_round_trip_and_are_encrypted_on_windows(self):
        account = await self.add_account()
        self.assertEqual(account["api_hash"], "a" * 32)
        with closing(sqlite3.connect(self.root / "inviter.db")) as db, db:
            stored = db.execute("SELECT api_hash FROM accounts").fetchone()[0]
        if os.name == "nt":
            self.assertTrue(stored.startswith("dpapi:v1:"))
            self.assertNotIn("a" * 32, stored)
        await self.db.update_account(account["id"], {"api_hash": "b" * 32, "daily_limit": 0})
        self.assertEqual((await self.db.get_account(account["id"]))["api_hash"], "b" * 32)
        self.assertEqual((await self.db.get_account(account["id"]))["daily_limit"], 0)

    async def test_ai_throttle_survives_memory_clear(self):
        account = await self.add_account()
        await self.db.record_ai_send(account["id"], "chat")
        await self.db.clear_ai_memory_for_account(account["id"])
        count, last = await self.db.get_ai_send_limits(account["id"], "chat")
        self.assertEqual(count, 1)
        self.assertIsNotNone(last)
        self.assertEqual(await self.db.get_ai_messages(account["id"], "chat"), [])

    async def test_expired_ai_events_do_not_count(self):
        account = await self.add_account()
        await self.db.record_ai_send(account["id"], "chat")
        with closing(sqlite3.connect(self.root / "inviter.db")) as db, db:
            db.execute("UPDATE ai_send_events SET created_at=?", ((datetime.now() - timedelta(hours=2)).isoformat(),))
        self.assertEqual((await self.db.get_ai_send_limits(account["id"], "chat"))[0], 0)

    async def test_delete_cleans_children_and_only_exact_session(self):
        account = await self.add_account()
        sessions = self.root / "sessions"
        sessions.mkdir()
        owned = sessions / f"{account['session_name']}.session"
        other = sessions / "session_other.session"
        owned.write_bytes(b"fake session")
        other.write_bytes(b"other")
        await self.db.create_warmup_session(account["id"], "medium", "test", 7)
        await self.db.record_ai_send(account["id"], "chat")
        await self.db.delete_account(account["id"])
        self.assertFalse(owned.exists())
        self.assertTrue(other.exists())
        self.assertIsNone(await self.db.get_warmup_session(account["id"]))
        self.assertEqual((await self.db.get_ai_send_limits(account["id"], "chat"))[0], 0)

    async def test_legacy_schema_migrates_before_indexes(self):
        legacy = self.root / "legacy.db"
        with closing(sqlite3.connect(legacy)) as db, db:
            db.executescript("""
                CREATE TABLE accounts(id INTEGER PRIMARY KEY, phone TEXT, api_id TEXT, api_hash TEXT,
                    session_name TEXT, proxy_type TEXT, proxy_host TEXT, proxy_port INTEGER,
                    proxy_login TEXT, proxy_password TEXT, daily_limit INTEGER DEFAULT 40,
                    added_today INTEGER DEFAULT 0, last_reset_date TEXT, status TEXT, flood_until TEXT,
                    notes TEXT, created_at TEXT);
                CREATE TABLE targets(id INTEGER PRIMARY KEY, username TEXT UNIQUE, status TEXT,
                    added_by_account_id INTEGER, error_message TEXT, processed_at TEXT, created_at TEXT);
                INSERT INTO targets(username, status) VALUES('alice', 'pending');
            """)
        with patch("core.database.DB_PATH", str(legacy)):
            await init_db()
            await init_db()
            self.assertEqual((await self.db.get_targets_stats())["pending"], 1)
        with closing(sqlite3.connect(legacy)) as db, db:
            self.assertIn("sort_order", {row[1] for row in db.execute("PRAGMA table_info(targets)")})

    async def test_clearing_active_queue_is_rejected(self):
        await self.db.bulk_insert_targets(["alice"])
        await self.db.set_setting("task_status", "running")
        with self.assertRaises(RuntimeError):
            await self.db.clear_targets()
        self.assertEqual((await self.db.get_targets_stats())["total"], 1)

    async def test_daily_warmup_quota_persists(self):
        account = await self.add_account()
        key = await self.db.create_warmup_session(account["id"], "medium", "test", 7)
        await self.db.update_warmup_session(key, daily_action_limit=6)
        session = await self.db.get_warmup_session(account["id"])
        self.assertEqual(session["day_current"], 1)
        self.assertEqual(session["daily_action_limit"], 6)
