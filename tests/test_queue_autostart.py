"""Campaign queue: the confirmation is asked once per session, or never with «Автозапуск следующей»."""

import unittest
from unittest.mock import MagicMock, patch

from core.campaign_queue import (
    get_auto_continue,
    needs_confirmation,
    parse_auto_continue,
    plan_next,
    set_auto_continue,
    set_link,
)
from tests.support import DatabaseCase
from ui.queue_runner import QueueRunner

PLAN = {"action": "offer", "next_id": 2, "name": "Вторая", "pending": 3, "message": "Следующая: «Вторая»."}


class AutoContinueRulesTests(unittest.TestCase):
    def test_setting_values(self):
        for value in ("1", "true", "YES", " on "):
            self.assertTrue(parse_auto_continue(value), value)
        for value in (None, "", "0", "off", "no", "junk"):
            self.assertFalse(parse_auto_continue(value), value)

    def test_confirmation_is_skipped_by_the_setting_or_by_an_earlier_yes(self):
        self.assertTrue(needs_confirmation(False, False))
        self.assertFalse(needs_confirmation(True, False))
        self.assertFalse(needs_confirmation(False, True))


class QueueRunnerConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.app = MagicMock(_closing=False)
        self.runner = QueueRunner(self.app)
        patcher = patch.object(self.runner, "start")
        self.start = patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, answer):
        return patch("ui.queue_runner.messagebox.askyesno", return_value=answer)

    def test_first_completion_asks_then_later_ones_start_without_a_dialog(self):
        with self.ask(True) as dialog:
            self.runner.handle(PLAN, [1])
            self.assertEqual(dialog.call_count, 1)
            self.assertTrue(self.runner.session_confirmed)
            self.runner.handle(PLAN, [1, 2])
            self.runner.handle(PLAN, [1, 2, 3])
            self.assertEqual(dialog.call_count, 1)
        self.assertEqual(self.start.call_count, 3)
        self.assertEqual(self.runner.chain, [1, 2, 3])

    def test_no_stops_the_queue_and_keeps_asking_next_time(self):
        with self.ask(False) as dialog:
            self.runner.handle(PLAN, [1])
            self.assertFalse(self.runner.session_confirmed)
            self.runner.handle(PLAN, [1])
            self.assertEqual(dialog.call_count, 2)
        self.start.assert_not_called()
        self.assertEqual(self.runner.chain, [])

    def test_persistent_setting_never_asks_and_says_what_it_does(self):
        with self.ask(False) as dialog:
            self.runner.handle(PLAN, [1], auto_continue=True)
        dialog.assert_not_called()
        self.start.assert_called_once_with(2, 0)
        self.assertIn("Автозапуск", self.app.set_status.call_args[0][0])
        self.assertFalse(self.runner.session_confirmed)

    def test_blocked_and_empty_plans_still_stop_even_with_auto_continue(self):
        self.runner.chain = [1]
        blocked = {**PLAN, "action": "blocked", "message": "В кампании нет получателей."}
        with self.ask(True) as dialog:
            self.runner.session_confirmed = True
            self.runner.handle(blocked, [1], auto_continue=True)
            self.assertEqual(self.runner.chain, [])
            self.runner.chain = [1]
            self.runner.handle({**PLAN, "action": "none"}, [1], auto_continue=True)
        dialog.assert_not_called()
        self.start.assert_not_called()
        self.assertEqual(self.runner.chain, [])
        self.app.notify.assert_called_once()
        self.assertTrue(self.app.notify.call_args.kwargs["warning"])


class QueueAutoContinueDatabaseTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.first_id = (await self.db.get_campaigns())[0]["id"]
        self.second_id = await self.db.create_campaign("Вторая", "invite")
        self.first, self.second = self.db.for_campaign(self.first_id), self.db.for_campaign(self.second_id)

    async def test_setting_is_global_persistent_and_off_by_default(self):
        self.assertFalse(await get_auto_continue(self.first))
        await set_auto_continue(self.first, True)
        self.assertTrue(await get_auto_continue(self.second))  # not tied to one campaign
        self.assertTrue(await get_auto_continue(self.db))
        await set_auto_continue(self.second, False)
        self.assertFalse(await get_auto_continue(self.first))

    async def test_auto_continue_does_not_change_what_plan_next_allows(self):
        await set_link(self.first, self.second_id)
        await set_auto_continue(self.first, True)
        self.assertEqual((await plan_next(self.first, {"outcome": "completed"}))["action"], "blocked")  # nobody queued
        await self.second.bulk_insert_targets(["alice_one", "bobby_two"])
        self.assertEqual((await plan_next(self.first, {"outcome": "completed"}))["action"], "offer")
        self.assertEqual((await plan_next(self.first, {"outcome": "stopped"}))["action"], "none")  # Stop is final
        loop = await plan_next(self.first, {"outcome": "completed"}, chain=[self.second_id])
        self.assertEqual(loop["action"], "blocked")  # cycles are still refused


if __name__ == "__main__":
    unittest.main()
