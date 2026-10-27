import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.errors import (
    ChatAdminRequiredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    InputUserDeactivatedError,
    PeerFloodError,
    UserBannedInChannelError,
    UserBotError,
    UserChannelsTooMuchError,
    UserKickedError,
    UserNotMutualContactError,
    UserNotParticipantError,
    UserPrivacyRestrictedError,
)
from telethon.tl.types import Channel, User

from core.account_manager import AccountManager
from core.inviter import ACCOUNT_FAILURE_LIMIT, MAX_TRANSIENT_RETRIES, InviterEngine
from core.warmup_actions import WarmupActions
from core.warmup_engine import MAX_CONSECUTIVE_FAILURES, WarmupEngine
from tests.support import DatabaseCase, account_payload


class EngineTests(DatabaseCase):
    def engine(self):
        self.events = []
        self.reports = []
        return InviterEngine(self.db, lambda *args: self.events.append(args), complete_callback=self.reports.append)

    async def test_disabled_account_is_never_picked(self):
        account = await self.add_account()
        engine = self.engine()
        engine.account_managers[account["id"]] = SimpleNamespace()
        await self.db.toggle_account_enabled(account["id"], False)
        self.assertIsNone(await engine._pick_available_account([account]))

    async def test_zero_daily_limit_is_respected(self):
        account = await self.add_account()
        await self.db.update_account(account["id"], {"daily_limit": 0})
        engine = self.engine()
        engine.account_managers[account["id"]] = SimpleNamespace()
        self.assertIsNone(await engine._pick_available_account([account]))

    async def test_stored_account_age_lifts_new_account_cap(self):
        account = await self.add_account()  # support payload: age 45, limit 40
        self.assertEqual(account["account_age_days"], 45)
        self.assertEqual(InviterEngine.get_safe_daily_limit(account), 40)
        young = await self.db.add_account({**account_payload(2), "account_age_days": 3})
        self.assertEqual(InviterEngine.get_safe_daily_limit(await self.db.get_account(young)), 10)

    async def test_account_age_is_editable(self):
        account = await self.add_account()
        await self.db.update_account(account["id"], {"account_age_days": 10})
        fresh = await self.db.get_account(account["id"])
        self.assertEqual((fresh["account_age_days"], InviterEngine.get_safe_daily_limit(fresh)), (10, 20))
        with self.assertRaises(ValueError):
            await self.db.update_account(account["id"], {"account_age_days": -1})

    async def test_zero_age_is_derived_from_created_at(self):
        key = await self.db.add_account({**account_payload(3), "account_age_days": 0})
        fresh = await self.db.get_account(key)
        self.assertEqual(fresh["account_age_days"], 0)
        self.assertEqual(InviterEngine.get_safe_daily_limit(fresh), 10)
        old = (datetime.now() - timedelta(days=45)).isoformat(timespec="seconds")
        with closing(sqlite3.connect(self.root / "inviter.db")) as connection:
            connection.execute("UPDATE accounts SET created_at=? WHERE id=?", (old, key))
            connection.commit()
        derived = await self.db.get_account(key)
        self.assertEqual(derived["account_age_days"], 45)
        self.assertEqual(InviterEngine.get_safe_daily_limit(derived), 40)
        self.assertEqual((await self.db.get_accounts())[0]["account_age_days"], 45)
        # Manual value overrides derivation; raw rows without a stored age are derived too.
        await self.db.update_account(key, {"account_age_days": 8})
        self.assertEqual((await self.db.get_account(key))["account_age_days"], 8)
        self.assertEqual(InviterEngine.get_safe_daily_limit({"daily_limit": 40, "created_at": old}), 40)
        self.assertEqual(InviterEngine.get_safe_daily_limit({"daily_limit": 40, "created_at": None}), 10)

    async def test_flood_is_respected_and_expiry_restores_account(self):
        account = await self.add_account()
        engine = self.engine()
        engine.account_managers[account["id"]] = SimpleNamespace()
        await self.db.set_flood_wait(account["id"], 60)
        self.assertIsNone(await engine._pick_available_account([account]))
        await self.db.update_account(
            account["id"], {"flood_until": (datetime.now() - timedelta(seconds=1)).isoformat()}
        )
        self.assertEqual((await engine._pick_available_account([account]))["status"], "active")

    async def test_stop_interrupts_long_wait(self):
        engine = self.engine()
        engine.running = True
        task = asyncio.create_task(engine._wait(3600))
        await asyncio.sleep(0)
        engine.stop()
        await asyncio.wait_for(task, 1)

    async def test_invalid_schedule_does_not_allow_work(self):
        engine = self.engine()
        await self.db.set_setting("schedule_enabled", "1")
        await self.db.set_setting("schedule_from", "99:99")
        self.assertFalse(await engine._is_within_schedule())

    async def test_overnight_schedule(self):
        engine = self.engine()
        await self.db.set_setting("schedule_enabled", "1")
        await self.db.set_setting("schedule_from", "22:00")
        await self.db.set_setting("schedule_to", "06:00")
        with patch("core.inviter.datetime") as clock:
            clock.now.return_value = datetime(2026, 1, 1, 23, 30)
            self.assertTrue(await engine._is_within_schedule())
            clock.now.return_value = datetime(2026, 1, 1, 12, 0)
            self.assertFalse(await engine._is_within_schedule())

    async def test_normal_completion_updates_queue_counter_and_disconnects(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        manager = AsyncMock()
        manager.account = account
        manager.check_admin_rights.return_value = {"ok": True}
        manager.add_user_to_chat.return_value = {"success": True}
        engine = self.engine()
        with patch("core.inviter.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "group", 10, 10), 3)
        self.assertEqual((await self.db.get_targets_stats())["added"], 1)
        self.assertEqual((await self.db.get_account(account["id"]))["added_today"], 1)
        manager.disconnect.assert_awaited_once()
        self.assertFalse(engine.running)
        self.assertEqual(self.reports[0]["outcome"], "completed")

    async def test_connection_error_always_disconnects_and_reports_failure(self):
        account = await self.add_account()
        manager = AsyncMock()
        manager.account = account
        manager.connect.side_effect = OSError("offline")
        engine = self.engine()
        with patch("core.inviter.AccountManager", return_value=manager):
            await engine.start([account], "group", 10, 10)
        manager.disconnect.assert_awaited_once()
        self.assertFalse(engine.running)
        self.assertEqual(self.reports[0]["outcome"], "failed")

    async def test_invite_skips_unavailable_account_and_uses_eligible_account(self):
        bad, good = await self.add_account(1), await self.add_account(2)
        await self.db.bulk_insert_targets(["alice"])
        failed = AsyncMock()
        failed.connect.side_effect = OSError("offline")
        eligible = AsyncMock()
        eligible.account = good
        eligible.check_admin_rights.return_value = {"ok": True}
        eligible.add_user_to_chat.return_value = {"success": True}
        engine = self.engine()
        with patch("core.inviter.AccountManager", side_effect=[failed, eligible]):
            await asyncio.wait_for(engine.start([bad, good], "group", 10, 10), 3)
        failed.disconnect.assert_awaited_once()
        eligible.add_user_to_chat.assert_awaited_once_with("group", "alice")
        self.assertEqual((await self.db.get_targets_stats())["added"], 1)
        self.assertEqual(self.reports[0]["outcome"], "completed")

    def manager_for(self, account, *results):
        manager = AsyncMock()
        manager.account = account
        manager.check_admin_rights.return_value = {"ok": True}
        manager.add_user_to_chat.side_effect = list(results) if len(results) > 1 else None
        if len(results) == 1:
            manager.add_user_to_chat.return_value = results[0]
        return manager

    async def run_engine(self, accounts, managers, **kwargs):
        engine = self.engine()
        engine._wait = AsyncMock()
        engine._schedule_account_next = lambda *args, **kwargs: 0  # no per-account interval in unit tests
        with patch("core.inviter.AccountManager", side_effect=managers):
            await asyncio.wait_for(engine.start(accounts, "group", 10, 10), 5)
        return engine

    async def test_peer_flood_sets_timed_cooldown_not_permanent_restriction(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        manager = self.manager_for(account, {"success": False, "error": "peer_flood"})
        await self.run_engine([account], [manager])
        self.assertEqual((await self.db.get_targets_stats())["pending"], 1)
        stored = await self.db.get_account(account["id"])
        self.assertEqual(stored["status"], "flood")
        remaining = datetime.fromisoformat(stored["flood_until"]) - datetime.now()
        self.assertGreater(remaining, timedelta(hours=23))
        self.assertEqual(self.reports[0]["outcome"], "failed")
        self.assertTrue(any("длительной паузе" in str(args[1]) for args in self.events))
        self.assertIsNone(await self.engine_pick(stored))

    async def engine_pick(self, account):
        engine = self.engine()
        engine.account_managers[account["id"]] = SimpleNamespace()
        return await engine._pick_available_account([account])

    async def test_need_admin_on_one_account_drops_it_and_continues(self):
        bad, good = await self.add_account(1), await self.add_account(2)
        await self.db.bulk_insert_targets(["alice"])
        no_rights = self.manager_for(bad, {"success": False, "error": "need_admin", "message": "нет прав"})
        working = self.manager_for(good, {"success": True})
        engine = await self.run_engine([bad, good], [no_rights, working])
        self.assertEqual(self.reports[0]["outcome"], "completed")
        self.assertEqual((await self.db.get_targets_stats())["added"], 1)
        no_rights.disconnect.assert_awaited_once()
        working.add_user_to_chat.assert_awaited_once_with("group", "alice")
        self.assertTrue(any(level == "WARN" and "исключён из ротации" in text for level, text in self.events))
        self.assertFalse(engine.account_managers)

    async def test_need_admin_on_all_accounts_fails_task(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        manager = self.manager_for(account, {"success": False, "error": "need_admin"})
        await self.run_engine([account], [manager])
        self.assertEqual(self.reports[0]["outcome"], "failed")
        self.assertEqual((await self.db.get_targets_stats())["pending"], 1)

    async def test_flood_wait_clears_claimed_target(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        manager = self.manager_for(
            account,
            {"success": False, "error": "flood", "flood_seconds": 7200},
        )
        await self.run_engine([account], [manager])
        self.assertEqual((await self.db.get_targets())[0]["status"], "pending")
        self.assertFalse(any("Незавершённая цель" in str(args[1]) for args in self.events))

    async def test_transient_error_is_requeued_then_succeeds(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        manager = self.manager_for(
            account,
            {"success": False, "error": "network", "message": "Сбой"},
            {"success": False, "error": "uncertain"},
            {"success": True},
        )
        await self.run_engine([account], [manager])
        self.assertEqual(manager.add_user_to_chat.await_count, 3)
        self.assertEqual((await self.db.get_targets())[0]["status"], "added")
        self.assertEqual(self.reports[0]["errors"], 0)

    async def test_transient_error_marked_error_after_retry_limit(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        fail = {"success": False, "error": "not_connected"}
        manager = self.manager_for(account, *([fail] * (MAX_TRANSIENT_RETRIES + 1)))
        await self.run_engine([account], [manager])
        self.assertEqual(manager.add_user_to_chat.await_count, MAX_TRANSIENT_RETRIES + 1)
        target = (await self.db.get_targets())[0]
        self.assertEqual(target["status"], "error")
        self.assertIn("Попытки исчерпаны", target["error_message"])
        self.assertEqual(self.reports[0]["errors"], 1)

    async def test_recipient_skip_is_final_and_not_retried(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice", "bobby"])
        manager = self.manager_for(account, {"success": True})
        outcomes = {
            "alice": {"success": False, "error": "skip", "message": "Это бот"},
            "bobby": {"success": False, "error": "private", "message": "Только контакты"},
        }
        manager.add_user_to_chat.side_effect = lambda chat, username, **kw: outcomes[username]  # queue is shuffled
        await self.run_engine([account], [manager])
        rows = {row["username"]: row for row in await self.db.get_targets()}
        self.assertEqual({name: row["status"] for name, row in rows.items()}, {"alice": "not_found", "bobby": "private"})
        self.assertEqual(manager.add_user_to_chat.await_count, 2)
        self.assertEqual(self.reports[0]["skipped"], 2)
        self.assertEqual(self.reports[0]["errors"], 0)

    async def test_add_user_to_chat_maps_telegram_errors(self):
        cases = [
            (UserPrivacyRestrictedError(None), "private"),
            (UserNotMutualContactError(None), "private"),
            (UserChannelsTooMuchError(None), "skip"),
            (UserKickedError(None), "skip"),
            (UserBotError(None), "skip"),
            (InputUserDeactivatedError(None), "skip"),
            (UserBannedInChannelError(None), "account_banned"),
            (ChatWriteForbiddenError(None), "need_admin"),
            (ChatAdminRequiredError(None), "need_admin"),
            (PeerFloodError(None), "peer_flood"),
            (ConnectionError("reset"), "network"),
            (OSError("down"), "network"),
            (asyncio.TimeoutError(), "uncertain"),
        ]
        for exc, expected in cases:
            with self.subTest(error=type(exc).__name__):
                manager = AccountManager(account_payload())
                user = User(id=5, access_hash=1, first_name="A")
                chat = Channel(id=7, title="Chat", photo=None, date=None, access_hash=1, megagroup=True)
                client = AsyncMock()
                client.get_entity.side_effect = [user, chat]
                client.get_permissions.side_effect = UserNotParticipantError(None)
                client.side_effect = exc
                manager.client = client
                result = await manager.add_user_to_chat("group", "alice")
                self.assertFalse(result["success"])
                self.assertEqual(result["error"], expected)
                self.assertFalse(result.get("flood_seconds"))
                if expected not in {"peer_flood"}:
                    self.assertTrue(result.get("message"))
                    self.assertRegex(result["message"], "[А-Яа-я]")

    async def test_account_banned_uses_cooldown_and_requeues(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        manager = self.manager_for(account, {"success": False, "error": "account_banned", "message": "бан"})
        await self.run_engine([account], [manager])
        self.assertEqual((await self.db.get_targets())[0]["status"], "pending")
        self.assertEqual((await self.db.get_account(account["id"]))["status"], "flood")

    async def test_consecutive_failures_trigger_account_break(self):
        account = await self.add_account()
        names = [f"user{i}" for i in range(ACCOUNT_FAILURE_LIMIT + 1)]
        await self.db.bulk_insert_targets(names)
        failures = [{"success": False, "error": "unknown", "message": "сбой"}] * ACCOUNT_FAILURE_LIMIT
        manager = self.manager_for(account, *failures, {"success": True})
        engine = self.engine()
        engine._wait = AsyncMock()
        engine._schedule_account_next = lambda *args, **kwargs: 0

        calls = 0
        original = engine._pick_available_account

        async def pick(accounts):
            nonlocal calls
            calls += 1
            if calls > ACCOUNT_FAILURE_LIMIT + 1:
                engine.stop()
                return None
            return await original(accounts)

        engine._pick_available_account = pick
        with patch("core.inviter.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "group", 10, 10), 5)
        stored = await self.db.get_account(account["id"])
        self.assertIsNotNone(stored["rotation_break_until"])
        self.assertGreater(datetime.fromisoformat(stored["rotation_break_until"]), datetime.now())
        self.assertEqual(manager.add_user_to_chat.await_count, ACCOUNT_FAILURE_LIMIT)
        self.assertTrue(any(level == "WARN" and "неудач подряд" in text for level, text in self.events))

    async def test_success_resets_failure_streak(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets([f"user{i}" for i in range(2 * ACCOUNT_FAILURE_LIMIT - 1)])
        fail = {"success": False, "error": "unknown", "message": "сбой"}
        results = [fail] * (ACCOUNT_FAILURE_LIMIT - 1) + [{"success": True}] + [fail] * (ACCOUNT_FAILURE_LIMIT - 1)
        manager = self.manager_for(account, *results)
        await self.run_engine([account], [manager])
        self.assertEqual(manager.add_user_to_chat.await_count, len(results))
        self.assertIsNone((await self.db.get_account(account["id"]))["rotation_break_until"])

    async def test_reset_failed_targets_is_selective(self):
        await self.db.bulk_insert_targets(["aaa1", "bbb2", "ccc3", "ddd4", "eee5"])
        for username, status in (
            ("aaa1", "error"),
            ("bbb2", "uncertain"),
            ("ccc3", "private"),
            ("ddd4", "not_found"),
            ("eee5", "added"),
        ):
            target = next(row for row in await self.db.get_targets() if row["username"] == username)
            await self.db.mark_target(target["id"], status, None, "msg")
        self.assertEqual(await self.db.reset_failed_targets(), 2)
        statuses = {row["username"]: row["status"] for row in await self.db.get_targets()}
        self.assertEqual(
            statuses, {"aaa1": "pending", "bbb2": "pending", "ccc3": "private", "ddd4": "not_found", "eee5": "added"}
        )
        self.assertEqual(await self.db.reset_failed_targets(["private"]), 1)
        self.assertEqual(await self.db.reset_failed_targets([]), 0)
        with self.assertRaises(ValueError):
            await self.db.reset_failed_targets(["added"])
        statuses = {row["username"]: row["status"] for row in await self.db.get_targets()}
        self.assertEqual(statuses["ccc3"], "pending")
        self.assertEqual(statuses["ddd4"], "not_found")

    async def test_warmup_no_selection_fails_without_default_actions(self):
        account = await self.add_account()
        manager = AsyncMock()
        engine = WarmupEngine(self.db, lambda *_: None)
        engine._execute_action = AsyncMock()
        with patch("core.warmup_engine.AccountManager", return_value=manager):
            with self.assertRaises(ValueError):
                await engine.start(account, "medium", "test")
        engine._execute_action.assert_not_called()
        manager.disconnect.assert_awaited_once()
        self.assertFalse(engine.running)

    async def test_warmup_stops_immediately_during_action(self):
        account = await self.add_account()
        await self.db.save_account_channels(account["id"], [{"id": -1001, "username": "group_one"}])
        row = (await self.db.get_account_channels(account["id"]))[0]
        await self.db.toggle_channel_selected(row["id"], True)
        manager = AsyncMock()
        engine = WarmupEngine(self.db, lambda *_: None)
        entered = asyncio.Event()

        async def action(*_):
            entered.set()
            await asyncio.sleep(3600)

        engine._execute_action = action
        with patch("core.warmup_engine.AccountManager", return_value=manager):
            task = asyncio.create_task(engine.start(account, "medium", "test"))
            await asyncio.wait_for(entered.wait(), 3)
            engine.stop()
            # Cancellation must propagate: the task ends cancelled, not "successfully".
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        self.assertFalse(engine.running)
        manager.disconnect.assert_awaited_once()
        self.assertEqual((await self.db.get_warmup_session(account["id"]))["status"], "paused")

    async def _select_warmup_channel(self, account):
        await self.db.save_account_channels(account["id"], [{"id": -1001, "username": "group_one"}])
        row = (await self.db.get_account_channels(account["id"]))[0]
        await self.db.toggle_channel_selected(row["id"], True)

    async def test_warmup_stops_with_error_after_consecutive_failures(self):
        account = await self.add_account()
        await self._select_warmup_channel(account)
        engine = WarmupEngine(self.db, lambda *_: None)
        engine._execute_action = AsyncMock(return_value={"success": False, "detail": "нет доступа"})
        engine._sleep_interruptible = AsyncMock(return_value=True)
        with patch("core.warmup_engine.AccountManager", return_value=AsyncMock()):
            with self.assertRaises(RuntimeError):
                await engine.start(account, "medium", "test")
        self.assertEqual(engine._execute_action.await_count, MAX_CONSECUTIVE_FAILURES)
        session = await self.db.get_warmup_session(account["id"])
        self.assertEqual(session["status"], "error")
        self.assertFalse(engine.running)

    async def test_warmup_success_resets_failure_streak(self):
        account = await self.add_account()
        await self._select_warmup_channel(account)
        engine = WarmupEngine(self.db, lambda *_: None)
        fail = {"success": False, "detail": "нет доступа"}
        ok = {"success": True, "detail": "готово"}
        # 9 failures, 1 success, then 10 failures: only the final streak stops the run.
        results = [fail] * 9 + [ok] + [fail] * MAX_CONSECUTIVE_FAILURES
        engine._execute_action = AsyncMock(side_effect=results)
        engine._sleep_interruptible = AsyncMock(return_value=True)
        with patch("core.warmup_engine.AccountManager", return_value=AsyncMock()):
            with self.assertRaises(RuntimeError):
                await engine.start(account, "medium", "test")
        self.assertEqual(engine._execute_action.await_count, len(results))
        self.assertEqual((await self.db.get_warmup_session(account["id"]))["actions_total"], 1)

    async def _finished_days_session(self, account, actions_total):
        await self._select_warmup_channel(account)
        session_id = await self.db.create_warmup_session(account["id"], "light", "test", 3)
        await self.db.update_warmup_session(
            session_id,
            day_current=4,
            actions_total=actions_total,
            last_reset_date=datetime.now().strftime("%Y-%m-%d"),
        )

    async def test_warmup_not_completed_without_any_success(self):
        account = await self.add_account()
        await self._finished_days_session(account, 0)
        engine = WarmupEngine(self.db, lambda *_: None)
        engine._execute_action = AsyncMock()
        with patch("core.warmup_engine.AccountManager", return_value=AsyncMock()):
            with self.assertRaises(RuntimeError):
                await engine.start(account, "light", "test")
        session = await self.db.get_warmup_session(account["id"])
        self.assertEqual(session["status"], "error")
        self.assertEqual(session["day_current"], 0)
        self.assertNotEqual((await self.db.get_account(account["id"]))["warmup_status"], "ready")

    async def test_warmup_completes_when_days_elapsed_with_successes(self):
        account = await self.add_account()
        await self._finished_days_session(account, 5)
        engine = WarmupEngine(self.db, lambda *_: None)
        with patch("core.warmup_engine.AccountManager", return_value=AsyncMock()):
            await engine.start(account, "light", "test")
        self.assertEqual((await self.db.get_warmup_session(account["id"]))["status"], "completed")

    async def test_warmup_throttle_works_when_memory_disabled(self):
        account = await self.add_account()
        await self.db.record_ai_send(account["id"], "group_one")
        client = AsyncMock()
        client.get_entity.return_value = SimpleNamespace(username="group_one", title="One")
        ai = SimpleNamespace(db=self.db, generate_message=AsyncMock())
        actions = WarmupActions(
            client, ai, lambda *_: None, account_id=account["id"], memory_enabled=False, hourly_limit=1
        )
        result = await actions.send_message_to_chat("group_one")
        self.assertFalse(result["success"])
        ai.generate_message.assert_not_called()
        client.send_message.assert_not_called()

    async def test_warmup_flood_reaches_engine(self):
        client = AsyncMock()
        client.get_entity.side_effect = FloodWaitError(request=None, capture=30)
        actions = WarmupActions(client, None, lambda *_: None)
        with self.assertRaises(FloodWaitError):
            await actions.read_channel("group")

    async def test_report_counts_only_current_session(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice", "bobby"])
        historical = await self.db.claim_next_pending_target()
        await self.db.record_success(historical["id"], account["id"])
        manager = AsyncMock()
        manager.account = account
        manager.check_admin_rights.return_value = {"ok": True}
        manager.add_user_to_chat.return_value = {"success": True}
        engine = self.engine()
        with patch("core.inviter.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "group", 10, 10), 3)
        self.assertEqual(self.reports[0]["added"], 1)
        self.assertEqual((await self.db.get_targets_stats())["added"], 2)
