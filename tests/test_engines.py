import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.errors import FloodWaitError

from core.inviter import InviterEngine
from core.warmup_actions import WarmupActions
from core.warmup_engine import WarmupEngine
from tests.support import DatabaseCase


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

    async def test_peer_flood_preserves_target_and_restricts_account(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        manager = AsyncMock()
        manager.account = account
        manager.check_admin_rights.return_value = {"ok": True}
        manager.add_user_to_chat.return_value = {"success": False, "error": "peer_flood"}
        engine = self.engine()
        engine._wait = AsyncMock()
        with patch("core.inviter.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "group", 10, 10), 3)
        self.assertEqual((await self.db.get_targets_stats())["pending"], 1)
        self.assertEqual((await self.db.get_account(account["id"]))["status"], "restricted")
        self.assertEqual(self.reports[0]["outcome"], "failed")

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
