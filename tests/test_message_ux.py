import random
import sqlite3
import unittest
from contextlib import closing
from datetime import date
from unittest.mock import AsyncMock, patch

from telethon.tl.types import User

from core.diagnostics import estimate_messages_eta, message_usage_today, preflight
from core.message_template import FALLBACK_NAME, has_placeholders, preview_message, render_message
from core.messenger import MessageEngine, send_test_message
from core.private_bridge import (
    CONSERVATIVE_INTERVAL_MIN,
    CONSERVATIVE_MAX_CONSECUTIVE,
    conservative_settings,
    create_private_dm_campaign,
)
from core.validation import MESSAGES_MIN_INTERVAL, validate_interval
from tests.support import DatabaseCase


class TemplateRenderingTests(unittest.TestCase):
    def test_name_placeholders_in_both_languages_and_cases(self):
        self.assertEqual(render_message("Привет, {имя}! {Name}", "Анна"), "Привет, Анна! Анна")
        self.assertEqual(render_message("Hi {name}", "Bob"), "Hi Bob")
        self.assertEqual(render_message("Hi { имя }", "Bob"), "Hi Bob")

    def test_missing_name_uses_fallback(self):
        self.assertEqual(render_message("Привет, {имя}", None), f"Привет, {FALLBACK_NAME}")
        self.assertEqual(render_message("Привет, {имя}", "   "), f"Привет, {FALLBACK_NAME}")
        self.assertEqual(render_message("Привет, {имя}", None, fallback=""), "Привет, ")

    def test_spin_picks_one_option_per_call(self):
        seen = {render_message("{а|б|в}", None, rng=random.Random(seed)) for seed in range(40)}
        self.assertEqual(seen, {"а", "б", "в"})
        for _ in range(20):
            self.assertIn(render_message("Привет! {раз|два}", None), {"Привет! раз", "Привет! два"})

    def test_multiple_groups_are_independent_and_empty_option_allowed(self):
        results = {render_message("{a|b} {c|d} x{|!}", None, rng=random.Random(seed)) for seed in range(80)}
        self.assertTrue(all(len(text.split()) == 3 for text in results))
        self.assertGreater(len(results), 4)
        self.assertIn("a c x", results)
        self.assertIn("b d x!", results)

    def test_name_inside_option_and_name_is_never_interpreted(self):
        self.assertEqual(
            render_message("{Привет, {имя}|Здравствуйте, {имя}}", "Ли", rng=random.Random(1)).count("Ли"), 1
        )
        # A hostile first name must not become a template of its own.
        self.assertEqual(render_message("Привет, {имя}", "{a|b}"), "Привет, {a|b}")
        self.assertEqual(render_message("{имя}", "x\ny\x00z"), "x y z")

    def test_other_braces_are_untouched(self):
        text = "Группа {ссылка} и {просто текст}"
        self.assertEqual(render_message(text, "Ли"), text)
        self.assertFalse(has_placeholders(text))
        self.assertTrue(has_placeholders("{имя}"))
        self.assertTrue(has_placeholders("{a|b}"))

    def test_preview_uses_fake_name(self):
        self.assertIn("Алексей", preview_message("Привет, {имя}"))


class SoftIntervalTests(unittest.TestCase):
    def test_messages_below_floor_is_rejected_with_clear_text(self):
        with self.assertRaises(ValueError) as caught:
            validate_interval("messages", 20, 40)
        self.assertIn(str(MESSAGES_MIN_INTERVAL), str(caught.exception))
        self.assertIn("PeerFlood", str(caught.exception))
        with self.assertRaises(ValueError):
            validate_interval("messages", MESSAGES_MIN_INTERVAL - 1, 120)

    def test_messages_at_or_above_floor_is_accepted(self):
        validate_interval("messages", MESSAGES_MIN_INTERVAL, MESSAGES_MIN_INTERVAL)
        validate_interval("messages", 60, 120)
        validate_interval("messages", 60, 86400)

    def test_invite_floor_and_bounds_are_unchanged(self):
        validate_interval("invite", 10, 40)
        validate_interval("invite", 20, 300)
        for low, high in ((9, 40), (40, 20), (20, 301)):
            with self.assertRaises(ValueError):
                validate_interval("invite", low, high)
        with self.assertRaises(ValueError):
            validate_interval("messages", 60, 86401)
        with self.assertRaises(ValueError):
            validate_interval("messages", 120, 60)


class PrivateBridgeSettingsTests(DatabaseCase):
    def test_conservative_preset_raises_pace(self):
        result = conservative_settings(
            {"interval_min": "20", "interval_max": "40", "protection_consecutive": "10", "protection_break_min": "2"}
        )
        self.assertEqual(int(result["interval_min"]), CONSERVATIVE_INTERVAL_MIN)
        self.assertGreaterEqual(int(result["interval_max"]), 120)
        self.assertEqual(int(result["protection_consecutive"]), CONSERVATIVE_MAX_CONSECUTIVE)
        self.assertLessEqual(int(result["protection_break_min"]), int(result["protection_break_max"]))

    def test_conservative_preset_keeps_slower_user_values(self):
        result = conservative_settings({"interval_min": "300", "interval_max": "400", "protection_consecutive": "2"})
        self.assertEqual((result["interval_min"], result["interval_max"]), ("300", "400"))
        self.assertEqual(result["protection_consecutive"], "2")
        self.assertEqual(conservative_settings({})["interval_min"], str(CONSERVATIVE_INTERVAL_MIN))

    async def test_bridge_campaign_gets_conservative_settings(self):
        await self.db.bulk_insert_targets(["alice", "bobby"])
        for _name in ("alice", "bobby"):
            target = await self.db.claim_next_pending_target()
            await self.db.mark_target(target["id"], "private")
        await self.db.save_campaign_settings(
            {"interval_min": "20", "interval_max": "40", "protection_consecutive": "8", "protection_break_min": "5"}
        )
        info = await create_private_dm_campaign(self.db, "Привет {ссылка}")
        copied = self.db.for_campaign(info["campaign_id"])
        self.assertGreaterEqual(int(await copied.get_setting("interval_min")), 60)
        self.assertLessEqual(int(await copied.get_setting("protection_consecutive")), 3)


class CombinedCapTests(DatabaseCase):
    async def setup_engine(self):
        self.db = self.db.for_campaign(await self.db.create_campaign("Сообщения", "messages"))
        account = await self.add_account()
        engine = MessageEngine(self.db, lambda *args: None)
        engine.account_managers = {account["id"]: object()}
        return engine, account

    async def pick(self, engine, account, **values):
        stored = {**account, "daily_limit": 10, "account_age_days": 45, **values}
        with patch.object(engine.db, "get_account", new=AsyncMock(return_value=stored)):
            return (
                await engine._pick_available_account([account]),
                await engine._pick_available_account([account], followup=True),
            )

    async def test_below_combined_cap_both_available(self):
        engine, account = await self.setup_engine()
        first, second = await self.pick(engine, account, sent_today=5, followups_today=4)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)

    async def test_followups_consume_first_message_budget(self):
        engine, account = await self.setup_engine()
        first, second = await self.pick(engine, account, sent_today=6, followups_today=4)
        self.assertIsNone(first)
        self.assertIsNone(second)

    async def test_first_messages_consume_followup_budget(self):
        engine, account = await self.setup_engine()
        first, second = await self.pick(engine, account, sent_today=10, followups_today=0)
        self.assertIsNone(first)
        self.assertIsNone(second)

    async def test_daily_used_ignores_missing_counters(self):
        self.assertEqual(MessageEngine.daily_used({}), 0)
        self.assertEqual(MessageEngine.daily_used({"sent_today": None, "followups_today": 3}), 3)


class EngineRenderingTests(DatabaseCase):
    async def test_each_recipient_gets_own_name_and_variant(self):
        self.db = self.db.for_campaign(await self.db.create_campaign("Сообщения", "messages"))
        await self.db.bulk_insert_targets(["alice", "bobby"])
        account = await self.add_account()
        sent = []
        engine = MessageEngine(self.db, lambda *args: None)
        engine._wait = AsyncMock()
        manager = AsyncMock()
        manager.account = account
        names = {"alice": "Алиса", "bobby": None}
        manager.resolve_recipient.side_effect = lambda name: User(id=sum(map(ord, name)), first_name=names[name])

        async def send(user, text, random_id):
            sent.append(text)
            return {"success": True, "message_id": 5}

        manager.send_direct_message.side_effect = send
        with patch("core.messenger.AccountManager", return_value=manager):
            await engine.start([account], "{Привет|Здравствуйте}, {имя}!", 60, 60)
        self.assertEqual(len(sent), 2)
        self.assertTrue(any(text.endswith(", Алиса!") for text in sent))
        self.assertTrue(any(text.endswith(f", {FALLBACK_NAME}!") for text in sent))
        self.assertTrue(all("{" not in text for text in sent))

    async def test_text_that_renders_empty_is_rejected_before_start(self):
        self.db = self.db.for_campaign(await self.db.create_campaign("Сообщения", "messages"))
        await self.db.bulk_insert_targets(["alice"])
        account = await self.add_account()
        engine = MessageEngine(self.db, lambda *args: None)
        with self.assertRaises(ValueError):
            await engine.start([account], "{|}", 60, 60)

    async def test_test_send_goes_to_saved_messages_with_sample_name(self):
        account = await self.add_account()
        manager = AsyncMock()
        with patch("core.messenger.AccountManager", return_value=manager):
            rendered = await send_test_message(self.db, account["id"], "Привет, {имя}! {раз|раз}")
        self.assertEqual(rendered, "Привет, Алексей! раз")
        manager.connect.assert_awaited_once()
        manager.send_saved_message.assert_awaited_once_with(rendered)
        manager.disconnect.assert_awaited_once()
        manager.send_direct_message.assert_not_called()

    async def test_test_send_disconnects_on_failure(self):
        account = await self.add_account()
        manager = AsyncMock()
        manager.send_saved_message.side_effect = OSError("offline")
        with patch("core.messenger.AccountManager", return_value=manager):
            with self.assertRaises(OSError):
                await send_test_message(self.db, account["id"], "Привет")
        manager.disconnect.assert_awaited_once()


class EtaTests(DatabaseCase):
    def accounts(self, **values):
        base = {"id": 1, "is_enabled": 1, "status": "active", "daily_limit": 40, "account_age_days": 60}
        return [{**base, **values}]

    def test_fits_today(self):
        text = estimate_messages_eta(self.accounts(), 10, 60, 120)
        self.assertIn("хватит сегодня", text)
        self.assertIn("сегодня можно ещё 40 из 40", text)

    def test_multiple_days_and_shared_usage(self):
        today = date.today().isoformat()
        accounts = self.accounts(
            sent_today=10,
            last_message_reset_date=today,
            followups_today=5,
            followup_reset_date=today,
        )
        text = estimate_messages_eta(accounts, 100, 60, 120)
        self.assertIn("~3 дн.", text)  # 25 today + 40 + 35
        self.assertIn("сегодня можно ещё 25 из 40", text)
        self.assertIn("первых 10, вторых 5", text)

    def test_stale_counters_do_not_count(self):
        accounts = self.accounts(sent_today=40, last_message_reset_date="2000-01-01")
        self.assertEqual(message_usage_today(accounts[0]), (0, 0))
        self.assertIn("сегодня можно ещё 40", estimate_messages_eta(accounts, 5, 60, 60))

    def test_interval_can_be_the_bottleneck(self):
        # 10 accounts x 40/day, but one send per 600 s with a 1-hour window -> 6 per day.
        accounts = [
            {"id": index, "is_enabled": 1, "status": "active", "daily_limit": 40, "account_age_days": 60}
            for index in range(10)
        ]
        text = estimate_messages_eta(accounts, 12, 600, 600, schedule_hours=1)
        self.assertIn("темп ≈ 6 в день", text)
        self.assertIn("~2 дн.", text)

    def test_no_usable_accounts(self):
        self.assertIn("нет аккаунтов", estimate_messages_eta(self.accounts(status="restricted"), 5, 60, 120))
        self.assertIn("нет аккаунтов", estimate_messages_eta([], 5, 60, 120))

    def test_zero_remaining_starts_tomorrow(self):
        today = date.today().isoformat()
        accounts = self.accounts(sent_today=40, last_message_reset_date=today)
        self.assertIn("со следующего дня", estimate_messages_eta(accounts, 50, 60, 120))

    async def test_preflight_reports_eta_for_messages_only(self):
        self.db = self.db.for_campaign(await self.db.create_campaign("Сообщения", "messages"))
        await self.db.bulk_insert_targets(["alice"])
        account = await self.add_account()
        report = await preflight(
            self.db, [account["id"]], "", mode="messages", message="Hi", interval_min=60, interval_max=120
        )
        eta = next(row for row in report["checks"] if row["label"] == "Оценка срока")
        self.assertTrue(eta["ok"])
        self.assertFalse(eta["blocking"])
        self.assertTrue(report["ok"])
        silent = await preflight(self.db, [account["id"]], "", mode="messages", message="Hi")
        self.assertFalse(any(row["label"] == "Оценка срока" for row in silent["checks"]))


class ManualResolveTests(DatabaseCase):
    async def make_uncertain(self, names=("alice",), followup=False):
        self.db = self.db.for_campaign(await self.db.create_campaign("Сообщения", "messages"))
        account = await self.add_account()
        await self.db.bulk_insert_targets(list(names))
        for index, _ in enumerate(names):
            target = await self.db.claim_next_pending_target()
            await self.db.reserve_delivery(
                target["id"],
                account["id"],
                1000 + index,
                followup_text="Second" if followup else None,
                access_hash=7 if followup else None,
            )
            await self.db.finish_delivery(target["id"], "uncertain")
        return account

    def ledger(self):
        with closing(sqlite3.connect(self.root / "inviter.db")) as conn:
            return dict(conn.execute("SELECT username,state FROM message_deliveries"))

    def followup_states(self):
        with closing(sqlite3.connect(self.root / "inviter.db")) as conn:
            return dict(conn.execute("SELECT username,state FROM message_followups"))

    async def statuses(self):
        return {row["username"]: row["status"] for row in await self.db.get_targets()}

    async def test_marked_delivered_becomes_sent_and_stays_blocked(self):
        await self.make_uncertain(("alice", "bobby"))
        self.assertEqual(await self.db.resolve_uncertain_deliveries(["alice"], True), 1)
        self.assertEqual(await self.statuses(), {"alice": "sent", "bobby": "uncertain"})
        self.assertEqual(self.ledger(), {"alice": "sent", "bobby": "uncertain"})
        target = next(row for row in await self.db.get_targets() if row["username"] == "alice")
        self.assertIn("вручную", target["error_message"])
        self.assertEqual(await self.db.delivery_exists("alice"), "sent")

    async def test_marked_not_delivered_returns_to_queue_and_can_be_sent_again(self):
        await self.make_uncertain(("alice",), followup=True)
        self.assertEqual(self.followup_states(), {"alice": "uncertain"})
        self.assertEqual(await self.db.resolve_uncertain_deliveries(["@Alice"], False), 1)
        self.assertEqual(await self.statuses(), {"alice": "pending"})
        self.assertEqual(self.ledger(), {})
        self.assertEqual(self.followup_states(), {})
        target = await self.db.claim_next_pending_target()
        self.assertIsNotNone(await self.db.reserve_delivery(target["id"], 1, 5))

    async def test_delivered_cancels_unconfirmed_followup(self):
        await self.make_uncertain(("alice",), followup=True)
        await self.db.resolve_uncertain_deliveries(["alice"], True)
        self.assertEqual(self.followup_states(), {"alice": "cancelled"})

    async def test_only_uncertain_rows_are_touched_and_calls_are_idempotent(self):
        await self.make_uncertain(("alice",))
        await self.db.bulk_insert_targets(["carol"])
        self.assertEqual(await self.db.resolve_uncertain_deliveries(["carol", "ghost1"], False), 0)
        self.assertEqual(await self.db.resolve_uncertain_deliveries(["alice"], True), 1)
        self.assertEqual(await self.db.resolve_uncertain_deliveries(["alice"], False), 0)
        self.assertEqual(await self.statuses(), {"alice": "sent", "carol": "pending"})
        self.assertEqual(await self.db.resolve_uncertain_deliveries([], True), 0)

    async def test_bulk_resolve_and_log(self):
        await self.make_uncertain(("alice", "bobby", "carol"))
        names = [row["username"] for row in await self.db.get_targets("uncertain", None)]
        self.assertEqual(await self.db.resolve_uncertain_deliveries(names, True), 3)
        self.assertEqual(set((await self.statuses()).values()), {"sent"})
        self.assertTrue(any(row["action"] == "MESSAGE_RESOLVED" for row in await self.db.get_logs()))

    async def test_invite_campaign_is_rejected(self):
        await self.db.bulk_insert_targets(["alice"])
        with self.assertRaises(ValueError):
            await self.db.resolve_uncertain_deliveries(["alice"], True)

    async def test_retry_never_requeues_uncertain_in_messages_campaign(self):
        await self.make_uncertain(("alice",))
        self.assertEqual(await self.db.reset_failed_targets(), 0)
        self.assertEqual(await self.db.reset_failed_targets(["uncertain", "error"]), 0)
        self.assertEqual(await self.statuses(), {"alice": "uncertain"})

    async def test_retry_still_requeues_uncertain_in_invite_campaign(self):
        await self.db.bulk_insert_targets(["alice"])
        target = await self.db.claim_next_pending_target()
        await self.db.mark_target(target["id"], "uncertain")
        self.assertEqual(await self.db.reset_failed_targets(), 1)


class RetryDialogChoicesTests(unittest.TestCase):
    def test_uncertain_choice_hidden_for_messages(self):
        from ui.tab_targets import retry_choices

        self.assertIn("uncertain", [item[0] for item in retry_choices(False)])
        self.assertNotIn("uncertain", [item[0] for item in retry_choices(True)])
        self.assertIn("error", [item[0] for item in retry_choices(True)])


if __name__ == "__main__":
    unittest.main()
