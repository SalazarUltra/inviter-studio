import asyncio
import unittest
import zlib
from unittest.mock import AsyncMock, patch

from telethon.tl.types import User

from core.ab_test import (
    ABConfig,
    assign_variant,
    format_ab_stats,
    normalize_split,
    planned_split,
    variant_key,
)
from core.messenger import MessageEngine
from tests.support import DatabaseCase


class AssignmentTests(unittest.TestCase):
    def test_assignment_is_stable_and_order_independent(self):
        keys = [f"user_{i:04d}" for i in range(500)]
        first = {key: assign_variant(key, 50, "7") for key in keys}
        for _ in range(3):
            self.assertEqual({key: assign_variant(key, 50, "7") for key in reversed(keys)}, first)

    def test_pinned_values_do_not_change_between_releases(self):
        # Changing the hash would silently re-split running campaigns: these values are a contract.
        self.assertEqual(
            [assign_variant(key, 50, "1") for key in ("alice", "bobby", "carol_x", "id:12345", "dave_01")],
            ["B", "B", "B", "A", "A"],
        )

    def test_key_is_case_and_at_insensitive_and_falls_back_to_user_id(self):
        self.assertEqual(variant_key("@Alice"), variant_key("alice"))
        self.assertEqual(assign_variant(variant_key("@Alice")), assign_variant(variant_key("alice")))
        self.assertEqual(variant_key("", 12345), "id:12345")
        self.assertEqual(variant_key(None, "12345"), "id:12345")
        with self.assertRaises(ValueError):
            variant_key("", None)

    def test_split_is_respected(self):
        keys = [f"user_{i}" for i in range(4000)]
        for split in (50, 10, 80):
            share = sum(assign_variant(key, split, "7") == "A" for key in keys) / len(keys)
            self.assertAlmostEqual(share, split / 100, delta=0.04, msg=f"split {split}")
        self.assertEqual(planned_split(keys, 50, "7")["A"] + planned_split(keys, 50, "7")["B"], len(keys))

    def test_growing_the_split_only_moves_people_from_b_to_a(self):
        keys = [f"user_{i}" for i in range(1000)]
        low = {key for key in keys if assign_variant(key, 30, "3") == "A"}
        high = {key for key in keys if assign_variant(key, 60, "3") == "A"}
        self.assertTrue(low <= high)

    def test_salt_makes_campaigns_independent(self):
        keys = [f"user_{i}" for i in range(200)]
        self.assertNotEqual(
            [assign_variant(key, 50, "1") for key in keys], [assign_variant(key, 50, "2") for key in keys]
        )

    def test_split_validation(self):
        self.assertEqual(normalize_split(""), 50)
        self.assertEqual(normalize_split(" 35 "), 35)
        for bad in ("0", "100", "-5", "abc", "12.5", "٥٠"):
            with self.assertRaises(ValueError, msg=bad):
                normalize_split(bad)

    def test_disabled_config_always_uses_text_a(self):
        self.assertEqual(ABConfig("A text").choose("alice"), (None, "A text"))
        config = ABConfig("A text", "B text", 50, "1")
        variant, text = config.choose("alice")
        self.assertEqual((variant, text), ("B", "B text"))

    def test_format_stats(self):
        empty = {"A": {"sent": 0, "replies": 0}, "B": {"sent": 0, "replies": 0}, "untested": 3}
        self.assertEqual(format_ab_stats(empty), "")
        stats = {
            "A": {"sent": 10, "replies": 3},
            "B": {"sent": 8, "replies": 0},
            "untested": 0,
            "replies_tracked": True,
        }
        text = format_ab_stats(stats)
        self.assertIn("A — 10", text)
        self.assertIn("A — 3 (30%)", text)
        self.assertIn("B — 0 (0%)", text)
        self.assertIn("не отслеживаются", format_ab_stats({**stats, "replies_tracked": False}))


class ABEngineTests(DatabaseCase):
    async def setup_campaign(self, names, *, ab=True, split="50", followups=False):
        campaign_id = await self.db.create_campaign("A/B", "messages")
        self.db = self.db.for_campaign(campaign_id)
        await self.db.bulk_insert_targets(names)
        settings = {
            "ab_enabled": "1" if ab else "0",
            "message_text_b": "Text B {имя}",
            "ab_split_a": split,
            # No rotation breaks: _wait is mocked, so a real-time break would make the engine spin.
            "protection_consecutive": "10000",
        }
        if followups:
            settings.update({"followup_enabled": "1", "followup_text": "Second"})
        await self.db.save_campaign_settings(settings)
        self.reports = []
        self.engine = MessageEngine(self.db, lambda *args: None, complete_callback=self.reports.append)
        self.engine._wait = AsyncMock()
        return self.engine

    def manager(self, sent, account=None):
        manager = AsyncMock()
        manager.account = account or {"phone": "+380500000000"}
        manager.resolve_recipient.side_effect = lambda name: User(
            id=zlib.crc32(name.encode()) + 1, access_hash=1, first_name="Ann"
        )

        async def send(user, text, random_id):
            sent.append((user.id, text))
            return {"success": True, "message_id": 7}

        manager.send_direct_message.side_effect = send
        return manager

    async def run_engine(self, accounts, text="Text A {имя}"):
        sent = []
        with patch("core.messenger.AccountManager", return_value=self.manager(sent)):
            await asyncio.wait_for(self.engine.start(accounts, text, 10, 10), 30)
        return sent

    async def test_each_recipient_gets_their_variant_and_it_is_stored(self):
        names = [f"person_{i:03d}" for i in range(40)]
        engine = await self.setup_campaign(names)
        account = await self.add_account()
        await self.db.update_account(account["id"], {"daily_limit": 1000})
        sent = await self.run_engine([await self.db.get_account(account["id"])])

        self.assertEqual(len(sent), 40)
        salt = str(self.db.campaign_id)
        expected = {name: assign_variant(name, 50, salt) for name in names}
        self.assertEqual(set(expected.values()), {"A", "B"})  # both groups are actually used
        stats = await self.db.get_ab_stats()
        self.assertEqual(stats["A"]["sent"], sum(v == "A" for v in expected.values()))
        self.assertEqual(stats["B"]["sent"], sum(v == "B" for v in expected.values()))
        self.assertEqual(stats["untested"], 0)
        self.assertFalse(stats["replies_tracked"])
        self.assertEqual(sum(1 for _, text in sent if text == "Text A Ann"), stats["A"]["sent"])
        self.assertEqual(sum(1 for _, text in sent if text == "Text B Ann"), stats["B"]["sent"])
        summary = self.reports[-1]
        self.assertEqual(summary["variants"], {"A": stats["A"]["sent"], "B": stats["B"]["sent"]})
        self.assertEqual(engine.ab.split_a, 50)

    async def test_split_setting_is_applied(self):
        names = [f"person_{i:03d}" for i in range(60)]
        await self.setup_campaign(names, split="90")
        account = await self.add_account()
        await self.db.update_account(account["id"], {"daily_limit": 1000})
        await self.run_engine([await self.db.get_account(account["id"])])
        stats = await self.db.get_ab_stats()
        self.assertGreater(stats["A"]["sent"], stats["B"]["sent"] * 3)

    async def test_without_ab_everyone_gets_text_a_and_no_variant(self):
        await self.setup_campaign(["alice", "bobby", "carol_x"], ab=False)
        account = await self.add_account()
        sent = await self.run_engine([account])
        self.assertTrue(all(text == "Text A Ann" for _, text in sent))
        stats = await self.db.get_ab_stats()
        self.assertEqual((stats["A"]["sent"], stats["B"]["sent"], stats["untested"]), (0, 0, 3))
        self.assertNotIn("variants", self.reports[-1])

    async def test_enabled_test_with_empty_text_b_does_not_start(self):
        await self.setup_campaign(["alice"])
        await self.db.save_campaign_settings({"message_text_b": "   "})
        account = await self.add_account()
        with patch("core.messenger.AccountManager", return_value=self.manager([])):
            await self.engine.start([account], "Text A", 10, 10)
        self.assertEqual(self.reports[-1]["outcome"], "failed")
        self.assertEqual((await self.db.get_targets_stats())["sent"], 0)

    async def test_restarted_engine_keeps_the_same_variant_for_a_recipient(self):
        names = [f"person_{i:03d}" for i in range(20)]
        await self.setup_campaign(names)
        account = await self.add_account()
        await self.db.update_account(account["id"], {"daily_limit": 1000})
        account = await self.db.get_account(account["id"])
        # First run sends ten people, the app "restarts", a new engine sends the rest.
        self.engine._wait = AsyncMock(side_effect=lambda _s: None)
        pending = await self.db.get_pending_list()
        await self.db.replace_pending_targets([row["username"] for row in pending[:10]], pending)
        first = await self.run_engine([account])
        await self.db.bulk_insert_targets(names)
        second_engine = MessageEngine(self.db, lambda *args: None)
        second_engine._wait = AsyncMock()
        sent = []
        with patch("core.messenger.AccountManager", return_value=self.manager(sent)):
            await asyncio.wait_for(second_engine.start([account], "Text A {имя}", 10, 10), 30)
        self.assertEqual(len(first) + len(sent), 20)
        salt = str(self.db.campaign_id)
        stored = await self.stored_variants()
        self.assertEqual(stored, {name: assign_variant(name, 50, salt) for name in names})

    async def stored_variants(self):
        from core.database import _connect

        async with _connect() as db:
            rows = await (
                await db.execute(
                    "SELECT username,variant FROM message_deliveries WHERE campaign_id=?", (self.db.campaign_id,)
                )
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    async def test_replies_are_counted_per_variant(self):
        names = [f"person_{i:03d}" for i in range(12)]
        await self.setup_campaign(names, followups=True)
        account = await self.add_account()
        salt = str(self.db.campaign_id)
        variants = {name: assign_variant(name, 50, salt) for name in names}
        await self.db.set_setting("task_status", "running")
        for index, name in enumerate(names):
            target = await self.db.claim_next_pending_target()
            self.assertEqual(target["username"], name)
            await self.db.reserve_delivery(
                target["id"],
                account["id"],
                1000 + index,
                followup_text="Second",
                access_hash=5,
                variant=variants[name],
            )
            await self.db.finish_delivery(target["id"], "sent", 10 + index)
        stats = await self.db.get_ab_stats()
        self.assertTrue(stats["replies_tracked"])
        self.assertEqual((stats["A"]["replies"], stats["B"]["replies"]), (0, 0))
        self.assertEqual(stats["A"]["sent"] + stats["B"]["sent"], 12)

        replied = names[:7]
        for index in range(len(replied)):
            self.assertTrue(await self.db.receive_followup_reply(account["id"], 1000 + index, 500))
        stats = await self.db.get_ab_stats()
        self.assertEqual(stats["A"]["replies"], sum(variants[n] == "A" for n in replied))
        self.assertEqual(stats["B"]["replies"], sum(variants[n] == "B" for n in replied))
        self.assertEqual(stats["A"]["replies"] + stats["B"]["replies"], 7)

    async def test_reserve_rejects_unknown_variant(self):
        await self.setup_campaign(["alice"])
        account = await self.add_account()
        target = await self.db.claim_next_pending_target()
        with self.assertRaises(ValueError):
            await self.db.reserve_delivery(target["id"], account["id"], 5, variant="C")
