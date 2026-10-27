import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.tl.types import Channel, User

from core.account_manager import AccountManager
from core.inviter import InviterEngine, describe_next_available
from core.private_bridge import (
    DEFAULT_TEMPLATE,
    build_message,
    chat_link,
    create_private_dm_campaign,
)
from tests.support import DatabaseCase, account_payload


def fake_manager(account):
    manager = AsyncMock()
    manager.account = account
    manager.check_admin_rights.return_value = {"ok": True}
    manager.add_user_to_chat.return_value = {"success": True}
    return manager


class ChatEntityCacheTests(DatabaseCase):
    def manager(self):
        manager = AccountManager(account_payload())
        manager.client = AsyncMock()
        self.chat_lookups = 0
        channel = Channel(id=2, title="test", photo=None, date=None)

        async def get_entity(value):
            if value == "group":
                self.chat_lookups += 1
                return channel
            return User(id=1)

        manager.client.get_entity.side_effect = get_entity
        manager.client.return_value = SimpleNamespace(missing_invitees=[])
        manager.client.get_permissions.side_effect = __import__("telethon").errors.UserNotParticipantError(
            request=None
        )
        return manager

    async def test_chat_is_resolved_once_for_many_invites(self):
        manager = self.manager()
        for name in ("alice", "bobby", "carol"):
            self.assertTrue((await manager.add_user_to_chat("group", name))["success"])
        self.assertEqual(self.chat_lookups, 1)

    async def test_admin_check_prefills_cache(self):
        manager = self.manager()
        manager.client.get_permissions.side_effect = None
        manager.client.get_permissions.return_value = SimpleNamespace(is_creator=True)
        self.assertTrue((await manager.check_admin_rights("group"))["ok"])
        manager.client.get_permissions.side_effect = __import__("telethon").errors.UserNotParticipantError(
            request=None
        )
        self.assertTrue((await manager.add_user_to_chat("group", "alice"))["success"])
        self.assertEqual(self.chat_lookups, 1)

    async def test_disconnect_and_invalidate_clear_cache(self):
        manager = self.manager()
        await manager.add_user_to_chat("group", "alice")
        manager.invalidate_chat_cache("group")
        await manager.add_user_to_chat("group", "bobby")
        self.assertEqual(self.chat_lookups, 2)
        await manager.disconnect()
        self.assertEqual(manager._chat_cache, {})

    async def test_export_invite_link_uses_cached_chat(self):
        manager = self.manager()
        manager.client.return_value = SimpleNamespace(link="https://t.me/+abc")
        self.assertEqual(await manager.export_invite_link("group"), "https://t.me/+abc")
        manager.client.return_value = SimpleNamespace(link=None)
        with self.assertRaises(RuntimeError):
            await manager.export_invite_link("group")


class PerAccountPacingTests(DatabaseCase):
    def engine(self, **kwargs):
        self.events, self.reports, self.details = [], [], []
        return InviterEngine(
            self.db,
            lambda *args: self.events.append(args),
            complete_callback=self.reports.append,
            details_callback=self.details.append,
            **kwargs,
        )

    async def test_busy_account_does_not_block_other_accounts(self):
        first, second = await self.add_account(1), await self.add_account(2)
        await self.db.bulk_insert_targets(["alice", "bobby", "carol", "dmitri"])
        managers = [fake_manager(first), fake_manager(second)]
        engine = self.engine()
        engine._get_smart_interval = lambda *_: 3600
        waits = []

        async def stop_on_wait(seconds):
            waits.append(seconds)
            engine.stop()

        engine._wait = AsyncMock(side_effect=stop_on_wait)
        with patch("core.inviter.AccountManager", side_effect=managers), patch("core.inviter.MIN_ACTION_GAP", 0):
            await asyncio.wait_for(engine.start([first, second], "group", 10, 10), 5)
        # Both accounts acted back to back; only then everyone is cooling down (bounded wait chunk).
        self.assertEqual([m.add_user_to_chat.await_count for m in managers], [1, 1])
        self.assertEqual(len(waits), 1)
        self.assertLessEqual(waits[0], 30)
        self.assertEqual((await self.db.get_targets_stats())["pending"], 2)

    async def test_account_in_interval_is_not_picked(self):
        account = await self.add_account()
        engine = self.engine()
        engine.account_managers[account["id"]] = SimpleNamespace()
        engine._next_allowed[account["id"]] = datetime.now() + timedelta(seconds=45)
        self.assertIsNone(await engine._pick_available_account([account]))
        self.assertIsNotNone(engine._soonest_ready)
        engine._next_allowed[account["id"]] = datetime.now() - timedelta(seconds=1)
        self.assertEqual((await engine._pick_available_account([account]))["id"], account["id"])
        self.assertIsNone(engine._soonest_ready)

    async def test_warm_up_is_per_account(self):
        first, second = await self.add_account(1), await self.add_account(2)
        engine = self.engine()
        engine._get_smart_interval = lambda *_: 10
        stat = engine._account_stat(first)
        stat["actions"] = 1
        self.assertEqual(engine._schedule_account_next(first, 10, 10), 20)
        stat["actions"] = engine.WARM_UP_COUNT + 1
        self.assertEqual(engine._schedule_account_next(first, 10, 10), 10)
        # A fresh account still warms up although the first one is long past it.
        engine._account_stat(second)["actions"] = 1
        self.assertEqual(engine._schedule_account_next(second, 10, 10), 20)
        self.assertGreater(engine._next_allowed[second["id"]], datetime.now() + timedelta(seconds=15))

    async def test_task_wide_gap_separates_back_to_back_actions(self):
        first, second = await self.add_account(1), await self.add_account(2)
        await self.db.bulk_insert_targets(["alice", "bobby"])
        engine = self.engine()
        engine._get_smart_interval = lambda *_: 3600
        gaps = []
        real_wait = engine._wait

        async def record_wait(seconds):
            gaps.append(seconds)
            await real_wait(0)

        engine._wait = record_wait
        with patch(
            "core.inviter.AccountManager", side_effect=[fake_manager(first), fake_manager(second)]
        ), patch("core.inviter.MIN_ACTION_GAP", 2.0):
            await asyncio.wait_for(engine.start([first, second], "group", 10, 10), 8)
        self.assertEqual(self.reports[0]["outcome"], "completed")
        self.assertTrue(any(0 < gap <= 2.0 for gap in gaps))

    async def test_details_and_final_report_have_per_account_counts(self):
        first, second = await self.add_account(1), await self.add_account(2)
        await self.db.bulk_insert_targets(["alice", "bobby", "carol"])
        managers = [fake_manager(first), fake_manager(second)]
        managers[1].add_user_to_chat.return_value = {"success": False, "error": "not_found"}
        engine = self.engine()
        engine._get_smart_interval = lambda *_: 0
        with patch("core.inviter.AccountManager", side_effect=managers), patch("core.inviter.MIN_ACTION_GAP", 0):
            await asyncio.wait_for(engine.start([first, second], "group", 10, 10), 5)
        summary = self.reports[0]
        by_phone = {item["phone"]: item for item in summary["accounts"]}
        self.assertEqual(sum(item["added"] for item in by_phone.values()), summary["added"])
        self.assertEqual(sum(item["skipped"] for item in by_phone.values()), summary["skipped"])
        self.assertGreaterEqual(summary["added"], 1)
        self.assertTrue(by_phone[first["phone"]]["added"] >= 1)
        last = self.details[-1]
        self.assertEqual({"pending", "skipped", "errors", "success", "accounts", "next_account"} - set(last), set())
        self.assertEqual(last["pending"], 0)
        self.assertEqual(last["total"], 3)
        self.assertEqual(last["success"], summary["added"])
        # The final log shows per-account lines.
        self.assertTrue(any(first["phone"] in str(event[1]) and "добавлено" in str(event[1]) for event in self.events))

    async def test_build_details_reports_next_available_account(self):
        first, second = await self.add_account(1), await self.add_account(2)
        engine = self.engine()
        engine.account_managers = {first["id"]: SimpleNamespace(account=first), second["id"]: SimpleNamespace(account=second)}
        now = datetime.now()
        engine._next_allowed = {
            first["id"]: now + timedelta(seconds=90),
            second["id"]: now + timedelta(seconds=20),
        }
        details = engine.build_details({"total": 5, "pending": 4, "added": 1, "skipped": 0, "errors": 0})
        self.assertEqual(details["next_account"], second["phone"])
        self.assertIn(details["next_in"], range(19, 22))
        text = describe_next_available(details)
        self.assertIn(second["phone"], text)
        self.assertIn("сек", text)
        self.assertEqual(describe_next_available({"next_account": "+1", "next_in": 0}), "Ближайший аккаунт: +1 — готов сейчас")
        self.assertEqual(describe_next_available({}), "")

    async def test_details_callback_failure_does_not_stop_run(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice"])
        engine = self.engine()
        engine.details_callback = lambda _details: 1 / 0
        with patch("core.inviter.AccountManager", return_value=fake_manager(account)), patch(
            "core.inviter.MIN_ACTION_GAP", 0
        ):
            await asyncio.wait_for(engine.start([account], "group", 10, 10), 5)
        self.assertEqual(self.reports[0]["outcome"], "completed")


class PrivateBridgeTests(DatabaseCase):
    async def make_private(self, names):
        await self.db.bulk_insert_targets(names)
        for _ in names:
            target = await self.db.claim_next_pending_target()
            await self.db.mark_target(target["id"], "private", None, "закрыт")

    def test_chat_link_variants(self):
        self.assertEqual(chat_link("@MyGroup"), "https://t.me/mygroup")
        self.assertEqual(chat_link("t.me/mygroup"), "https://t.me/mygroup")
        self.assertEqual(chat_link("https://t.me/+AbCd123"), "https://t.me/+AbCd123")
        self.assertEqual(chat_link("t.me/joinchat/XyZ"), "https://t.me/joinchat/XyZ")
        self.assertEqual(chat_link(""), "")
        self.assertEqual(chat_link("не ссылка!"), "")

    def test_build_message(self):
        self.assertEqual(
            build_message(DEFAULT_TEMPLATE, "@mygroup"),
            "Здравствуйте! Приглашаю вас в нашу группу: https://t.me/mygroup",
        )
        self.assertEqual(build_message("Заходи", "@mygroup"), "Заходи\nhttps://t.me/mygroup")
        self.assertEqual(
            build_message("Тут https://t.me/mygroup", "@mygroup"), "Тут https://t.me/mygroup"
        )
        with self.assertRaises(ValueError):
            build_message(DEFAULT_TEMPLATE, "")

    async def test_creates_messages_campaign_from_private_targets(self):
        await self.add_account()
        await self.make_private(["alice", "bobby"])
        await self.db.bulk_insert_targets(["carol"])  # pending: must not be copied
        await self.db.set_setting("interval_min", "33")
        await self.db.set_setting("selected_accounts", "[1]")
        info = await create_private_dm_campaign(self.db, "Привет https://t.me/mygroup")
        self.assertEqual((info["count"], info["inserted"], info["pending"]), (2, 2, 2))
        new_db = self.db.for_campaign(info["campaign_id"])
        self.assertEqual(await new_db.get_campaign_kind(), "messages")
        self.assertEqual(
            sorted(row["username"] for row in await new_db.get_targets("pending", limit=None)), ["alice", "bobby"]
        )
        self.assertEqual(await new_db.get_setting("message_text"), "Привет https://t.me/mygroup")
        self.assertEqual(await new_db.get_setting("operation_mode"), "messages")
        # A copied fast pace (33 s) is raised to the conservative DM preset.
        self.assertEqual(await new_db.get_setting("interval_min"), "60")
        self.assertGreaterEqual(int(await new_db.get_setting("interval_max")), 120)
        self.assertLessEqual(int(await new_db.get_setting("protection_consecutive")), 3)
        self.assertEqual(await new_db.get_setting("selected_accounts"), "[1]")
        # The source campaign keeps its own results.
        self.assertEqual(await self.db.count_targets("private"), 2)

    async def test_ledger_prevents_duplicate_first_message(self):
        account = await self.add_account()
        await self.make_private(["alice", "bobby"])
        other = self.db.for_campaign(await self.db.create_campaign("ЛС", "messages"))
        await other.bulk_insert_targets(["bobby"])
        target = await other.claim_next_pending_target()
        await other.reserve_delivery(target["id"], account["id"], 77)
        await other.finish_delivery(target["id"], "sent", 5)
        info = await create_private_dm_campaign(self.db, "Заходи https://t.me/mygroup")
        self.assertEqual((info["count"], info["pending"]), (2, 1))
        new_db = self.db.for_campaign(info["campaign_id"])
        self.assertEqual((await new_db.get_targets("already_sent", limit=None))[0]["username"], "bobby")

    async def test_requires_private_targets_and_invite_campaign(self):
        with self.assertRaises(ValueError):
            await create_private_dm_campaign(self.db, "Привет https://t.me/mygroup")
        messages = self.db.for_campaign(await self.db.create_campaign("ЛС", "messages"))
        with self.assertRaises(ValueError):
            await create_private_dm_campaign(messages, "Привет https://t.me/mygroup")
        await self.make_private(["alice"])
        with self.assertRaises(ValueError):
            await create_private_dm_campaign(self.db, "   ")
        self.assertEqual(len(await self.db.get_campaigns()), 2)
