import asyncio
from datetime import date, timedelta
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from telethon.tl.types import User

from core.inviter import InviterEngine
from core.messenger import MessageEngine
from core.pacing import (
    PACE_FIELDS,
    aggressive_warnings,
    daily_cap_reached,
    daily_cap_status,
    detect_preset,
    format_warning_block,
    gate_accounts,
    parse_daily_cap,
    preset_values,
    split_warmup_ready,
    warmup_gate_error,
)
from core.validation import MESSAGES_MIN_INTERVAL, validate_interval
from tests.support import DatabaseCase

EXPECTED = {
    "messages": {
        "safe": ("90", "180", "3"),
        "normal": ("60", "120", "5"),
        "fast": ("45", "90", "7"),
    },
    "invite": {
        "safe": ("40", "90", "3"),
        "normal": ("20", "45", "5"),
        "fast": ("15", "30", "8"),
    },
}


class PresetTests(TestCase):
    def test_preset_values_match_spec(self):
        for mode, presets in EXPECTED.items():
            for key, (low, high, consecutive) in presets.items():
                values = preset_values(mode, key)
                self.assertEqual(set(values), set(PACE_FIELDS))
                self.assertEqual(
                    (values["interval_min"], values["interval_max"], values["protection_consecutive"]),
                    (low, high, consecutive),
                )
                self.assertLessEqual(int(values["protection_break_min"]), int(values["protection_break_max"]))
                self.assertGreaterEqual(int(values["protection_break_min"]), 1)

    def test_presets_pass_interval_validation(self):
        for mode, presets in EXPECTED.items():
            for key in presets:
                values = preset_values(mode, key)
                validate_interval(mode, int(values["interval_min"]), int(values["interval_max"]))
        self.assertGreaterEqual(int(preset_values("messages", "fast")["interval_min"]), MESSAGES_MIN_INTERVAL)

    def test_detect_preset_roundtrip_and_custom(self):
        for mode, presets in EXPECTED.items():
            for key in presets:
                self.assertEqual(detect_preset(mode, preset_values(mode, key)), key)
        values = preset_values("messages", "normal")
        values["interval_max"] = "121"
        self.assertEqual(detect_preset("messages", values), "custom")
        # The same numbers are a different preset (or custom) in the other mode.
        self.assertEqual(detect_preset("invite", preset_values("messages", "normal")), "custom")
        self.assertEqual(detect_preset("messages", {**values, "interval_max": "abc"}), "custom")
        self.assertEqual(detect_preset("messages", {}), "custom")

    def test_unknown_preset_rejected(self):
        with self.assertRaises(ValueError):
            preset_values("messages", "turbo")


class WarningTests(TestCase):
    def test_messages_thresholds(self):
        self.assertEqual(aggressive_warnings("messages", 60, 8), [])
        self.assertEqual(len(aggressive_warnings("messages", 59, 8)), 1)
        self.assertIn("PeerFlood", aggressive_warnings("messages", 45, 5)[0])

    def test_invite_thresholds(self):
        self.assertEqual(aggressive_warnings("invite", 20, 8), [])
        self.assertEqual(len(aggressive_warnings("invite", 19, 5)), 1)

    def test_consecutive_threshold_and_combination(self):
        self.assertEqual(len(aggressive_warnings("invite", 30, 9)), 1)
        self.assertEqual(len(aggressive_warnings("messages", 45, 9)), 2)
        block = format_warning_block(aggressive_warnings("messages", 45, 9))
        self.assertTrue(block.startswith("ВНИМАНИЕ"))
        self.assertEqual(format_warning_block([]), "")

    def test_preset_warnings(self):
        for mode in EXPECTED:
            for key in ("safe", "normal"):
                values = preset_values(mode, key)
                self.assertEqual(
                    aggressive_warnings(mode, int(values["interval_min"]), int(values["protection_consecutive"])), []
                )
        fast = preset_values("messages", "fast")
        self.assertTrue(aggressive_warnings("messages", int(fast["interval_min"]), int(fast["protection_consecutive"])))


class WarmupGateTests(TestCase):
    accounts = [
        {"id": 1, "phone": "+1", "warmup_status": "ready"},
        {"id": 2, "phone": "+2", "warmup_status": "cold"},
        {"id": 3, "phone": "+3"},
        {"id": 4, "phone": "+4", "warmup_status": "warming"},
        {"id": 5, "phone": "+5", "warmup_status": None},
    ]

    def test_split(self):
        ready, skipped = split_warmup_ready(self.accounts)
        self.assertEqual([a["id"] for a in ready], [1])
        self.assertEqual([a["id"] for a in skipped], [2, 3, 4, 5])

    def test_gate_off_keeps_everyone(self):
        allowed, skipped = gate_accounts(self.accounts, False)
        self.assertEqual(len(allowed), 5)
        self.assertEqual(skipped, [])

    def test_gate_on_and_error_text(self):
        allowed, skipped = gate_accounts(self.accounts, True)
        self.assertEqual([a["id"] for a in allowed], [1])
        _, none_ready = gate_accounts(self.accounts[1:], True)
        message = warmup_gate_error(none_ready)
        self.assertIn("+2 (не прогревался)", message)
        self.assertIn("+4 (прогревается)", message)


class DailyCapHelperTests(TestCase):
    def test_parse(self):
        self.assertEqual(parse_daily_cap(""), 0)
        self.assertEqual(parse_daily_cap(" 25 "), 25)
        for bad in ("-1", "abc", "1.5", "100001"):
            with self.assertRaises(ValueError):
                parse_daily_cap(bad)

    def test_reached_and_status(self):
        self.assertFalse(daily_cap_reached(100, 0))
        self.assertFalse(daily_cap_reached(4, 5))
        self.assertTrue(daily_cap_reached(5, 5))
        self.assertEqual(daily_cap_status(1, 0), "")
        self.assertIn("1/5", daily_cap_status(1, 5, "messages"))
        self.assertIn("достигнут", daily_cap_status(5, 5, "invite"))


class DailyCounterStorageTests(DatabaseCase):
    async def test_counter_is_per_campaign_and_day(self):
        other = self.db.for_campaign(await self.db.create_campaign("Другая", "invite"))
        self.assertEqual(await self.db.get_campaign_daily_count(), 0)
        self.assertEqual(await self.db.add_campaign_daily_success(), 1)
        self.assertEqual(await self.db.add_campaign_daily_success(), 2)
        self.assertEqual(await self.db.get_campaign_daily_count(), 2)
        self.assertEqual(await other.get_campaign_daily_count(), 0)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.assertEqual(await self.db.get_campaign_daily_count(tomorrow), 0)

    async def test_settings_are_campaign_scoped_and_saved(self):
        await self.db.save_campaign_settings({"campaign_daily_cap": "7", "require_warmup_ready": "1"})
        other = self.db.for_campaign(await self.db.create_campaign("Другая", "invite"))
        self.assertEqual(await self.db.get_setting("campaign_daily_cap"), "7")
        self.assertEqual(await self.db.get_setting("require_warmup_ready"), "1")
        self.assertIsNone(await other.get_setting("campaign_daily_cap"))


def invite_manager(account):
    manager = AsyncMock()
    manager.account = account
    manager.check_admin_rights.return_value = {"ok": True}
    manager.add_user_to_chat.return_value = {"success": True}
    return manager


class InviteDailyCapTests(DatabaseCase):
    async def run_engine(self, cap, names, today_before=0):
        accounts = [await self.add_account(1), await self.add_account(2)]
        await self.db.bulk_insert_targets(names)
        await self.db.save_campaign_settings({"campaign_daily_cap": str(cap)})
        for _ in range(today_before):
            await self.db.add_campaign_daily_success()
        statuses, events = [], []
        engine = InviterEngine(self.db, lambda *args: events.append(args), status_callback=statuses.append)
        engine._get_smart_interval = lambda *_: 0
        waits = []

        async def stop_on_wait(seconds):
            waits.append(seconds)
            engine.stop()

        engine._wait = AsyncMock(side_effect=stop_on_wait)
        managers = [invite_manager(accounts[0]), invite_manager(accounts[1])]
        with patch("core.inviter.AccountManager", side_effect=managers), patch("core.inviter.MIN_ACTION_GAP", 0):
            await asyncio.wait_for(engine.start(accounts, "group", 10, 10), 5)
        return engine, managers, statuses, events, waits

    async def test_invites_stop_at_cap_and_status_is_shown(self):
        engine, managers, statuses, events, waits = await self.run_engine(2, ["alice", "bobby", "carol", "dmitri"])
        self.assertEqual(sum(m.add_user_to_chat.await_count for m in managers), 2)
        stats = await self.db.get_targets_stats()
        self.assertEqual((stats["added"], stats["pending"]), (2, 2))
        self.assertEqual(await self.db.get_campaign_daily_count(), 2)
        self.assertTrue(any("Дневной лимит кампании достигнут: 2/2" in value for value in statuses))
        self.assertTrue(any("следующего дня" in row[1] for row in events))
        self.assertEqual(waits, [30])
        details = engine.build_details()
        self.assertEqual((details["daily_cap"], details["daily_done"]), (2, 2))

    async def test_counter_persists_between_runs(self):
        _, managers, *_ = await self.run_engine(3, ["alice", "bobby", "carol"], today_before=2)
        self.assertEqual(sum(m.add_user_to_chat.await_count for m in managers), 1)
        self.assertEqual((await self.db.get_targets_stats())["pending"], 2)

    async def test_cap_zero_is_unlimited(self):
        _, managers, _, _, waits = await self.run_engine(0, ["alice", "bobby", "carol"])
        self.assertEqual(sum(m.add_user_to_chat.await_count for m in managers), 3)
        self.assertEqual((await self.db.get_targets_stats())["pending"], 0)
        self.assertEqual(waits, [])


class MessageDailyCapTests(DatabaseCase):
    async def run_engine(self, cap, names, today_before=0):
        campaign_id = await self.db.create_campaign("Сообщения", "messages")
        self.db = self.db.for_campaign(campaign_id)
        await self.db.bulk_insert_targets(names)
        await self.db.save_campaign_settings({"campaign_daily_cap": str(cap)})
        for _ in range(today_before):
            await self.db.add_campaign_daily_success()
        account = await self.add_account()
        statuses = []
        engine = MessageEngine(self.db, lambda *args: None, status_callback=statuses.append)

        async def stop_on_wait(seconds):
            if seconds == 30:
                engine.stop()

        engine._wait = AsyncMock(side_effect=stop_on_wait)
        manager = AsyncMock()
        manager.account = account
        manager.resolve_recipient.side_effect = lambda name: User(id=10000 + sum(ord(c) for c in name))
        manager.send_direct_message.return_value = {"success": True, "message_id": 7}
        manager.verify_sent_message.return_value = {"found": None}
        with patch("core.messenger.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "Hello", 10, 10), 5)
        return manager, statuses

    async def test_messages_stop_at_cap(self):
        manager, statuses = await self.run_engine(2, ["alice", "bobby", "carol"])
        self.assertEqual(manager.send_direct_message.await_count, 2)
        stats = await self.db.get_targets_stats()
        self.assertEqual((stats["sent"], stats["pending"]), (2, 1))
        self.assertEqual(await self.db.get_campaign_daily_count(), 2)
        self.assertTrue(any("2/2 сообщений" in value for value in statuses))

    async def test_previous_runs_today_count_toward_cap(self):
        manager, _ = await self.run_engine(2, ["alice", "bobby"], today_before=2)
        manager.send_direct_message.assert_not_awaited()
        self.assertEqual((await self.db.get_targets_stats())["pending"], 2)

    async def test_cap_off_sends_everything(self):
        manager, _ = await self.run_engine(0, ["alice", "bobby", "carol"])
        self.assertEqual(manager.send_direct_message.await_count, 3)
