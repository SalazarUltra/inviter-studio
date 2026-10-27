"""Saved Messages summary: text builder privacy, daily accumulator and opt-in delivery."""

import json
import unittest

from core.summary import (
    SUMMARY_ACCOUNT_KEY,
    SUMMARY_ENABLED_KEY,
    SUMMARY_MODE_KEY,
    SUMMARY_PENDING_KEY,
    build_summary_text,
    counts_from_summary,
    deliver_task_summary,
    flush_daily,
    merge_pending,
)
from tests.support import DatabaseCase

ENGINE_SUMMARY = {
    "added": 4,
    "sent": 7,
    "followup_sent": 2,
    "skipped": 1,
    "errors": 3,
    "outcome": "completed",
    "duration": "0:12:34",
    "accounts": [{"phone": "+380501112233", "added": 4, "skipped": 1, "errors": 3}],
    "recipients": ["@alice", "@bob"],
}


class SummaryTextTests(unittest.TestCase):
    def test_counts_merge_first_and_second_messages(self):
        self.assertEqual(counts_from_summary(ENGINE_SUMMARY), {"sent": 9, "added": 4, "errors": 3, "skipped": 1})
        self.assertEqual(
            counts_from_summary({"sent": "x", "errors": -5}), {"sent": 0, "added": 0, "errors": 0, "skipped": 0}
        )

    def test_text_has_counts_and_no_private_data(self):
        text = build_summary_text(
            {**ENGINE_SUMMARY, **counts_from_summary(ENGINE_SUMMARY)},
            lang="ru",
            outcome="completed",
            duration="0:12:34",
        )
        for expected in ("Отправлено: 9", "Добавлено: 4", "Ошибок: 3", "Пропущено: 1", "Задача завершена", "0:12:34"):
            self.assertIn(expected, text)
        for secret in ("+380501112233", "380501112233", "@alice", "@bob", "alice"):
            self.assertNotIn(secret, text)

    def test_english_and_daily_header(self):
        text = build_summary_text({"sent": 5}, lang="en", tasks=2, day="2026-10-26")
        self.assertIn("summary for 2026-10-26", text)
        self.assertIn("Sent: 5", text)
        self.assertIn("Tasks: 2", text)
        self.assertIn("Errors: 0", text)

    def test_unknown_outcome_and_language_fall_back(self):
        text = build_summary_text({"sent": 1}, lang="de", outcome="weird")
        self.assertIn("Итог задачи", text)
        self.assertIn("Отправлено: 1", text)


class MergePendingTests(unittest.TestCase):
    def test_same_day_accumulates(self):
        flush, first = merge_pending(None, {"sent": 2, "errors": 1}, "2026-10-27")
        self.assertIsNone(flush)
        flush, second = merge_pending(json.dumps(first), {"sent": 3, "added": 1}, "2026-10-27")
        self.assertIsNone(flush)
        self.assertEqual(second, {"date": "2026-10-27", "tasks": 2, "sent": 5, "added": 1, "errors": 1, "skipped": 0})

    def test_new_day_returns_old_for_flush(self):
        _, old = merge_pending(None, {"sent": 4}, "2026-10-26")
        flush, current = merge_pending(old, {"sent": 1}, "2026-10-27")
        self.assertEqual(flush["date"], "2026-10-26")
        self.assertEqual(flush["sent"], 4)
        self.assertEqual((current["date"], current["sent"], current["tasks"]), ("2026-10-27", 1, 1))

    def test_carry_over_keeps_unsent_day(self):
        _, old = merge_pending(None, {"sent": 4}, "2026-10-26")
        flush, current = merge_pending(old, {"sent": 1}, "2026-10-27", carry_over=True)
        self.assertIsNone(flush)
        self.assertEqual((current["sent"], current["tasks"]), (5, 2))

    def test_corrupt_pending_is_ignored(self):
        flush, current = merge_pending("{not json", {"sent": 1}, "2026-10-27")
        self.assertIsNone(flush)
        self.assertEqual(current["sent"], 1)


class DeliveryTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sent = []
        self.fail = False

        async def sender(_db, account_id, text):
            if self.fail:
                raise RuntimeError("offline")
            self.sent.append((account_id, text))

        self.sender = sender

    async def configure(self, mode="task", enabled="1", account_id="7"):
        await self.db.set_setting(SUMMARY_ENABLED_KEY, enabled)
        await self.db.set_setting(SUMMARY_MODE_KEY, mode)
        await self.db.set_setting(SUMMARY_ACCOUNT_KEY, account_id)

    async def test_disabled_by_default_sends_nothing(self):
        self.assertEqual(await deliver_task_summary(self.db, ENGINE_SUMMARY, sender=self.sender), "disabled")
        await self.configure(enabled="0")
        self.assertEqual(await deliver_task_summary(self.db, ENGINE_SUMMARY, sender=self.sender), "disabled")
        self.assertEqual(self.sent, [])

    async def test_task_mode_sends_counters_only_via_selected_account(self):
        await self.configure()
        self.assertEqual(await deliver_task_summary(self.db, ENGINE_SUMMARY, sender=self.sender), "sent")
        ((account_id, text),) = self.sent
        self.assertEqual(account_id, 7)
        self.assertIn("Отправлено: 9", text)
        self.assertNotIn("+380", text)
        self.assertNotIn("@alice", text)

    async def test_task_mode_uses_saved_language(self):
        await self.configure()
        await self.db.set_setting("ui_lang", "en")
        await deliver_task_summary(self.db, ENGINE_SUMMARY, sender=self.sender)
        self.assertIn("Sent: 9", self.sent[0][1])

    async def test_empty_task_and_missing_account_are_skipped(self):
        await self.configure()
        empty = {"outcome": "stopped"}
        self.assertEqual(await deliver_task_summary(self.db, empty, sender=self.sender), "empty")
        await self.configure(account_id="")
        self.assertEqual(await deliver_task_summary(self.db, ENGINE_SUMMARY, sender=self.sender), "no_account")
        self.assertEqual(self.sent, [])

    async def test_send_failure_is_reported_not_raised(self):
        await self.configure()
        self.fail = True
        logs = []
        status = await deliver_task_summary(
            self.db, ENGINE_SUMMARY, sender=self.sender, log=lambda level, text: logs.append((level, text))
        )
        self.assertEqual(status, "failed")
        self.assertEqual(logs[0][0], "WARN")

    async def test_daily_mode_accumulates_then_flushes_next_day(self):
        await self.configure(mode="daily")
        first = await deliver_task_summary(self.db, ENGINE_SUMMARY, sender=self.sender, today="2026-10-26")
        second = await deliver_task_summary(self.db, ENGINE_SUMMARY, sender=self.sender, today="2026-10-26")
        self.assertEqual((first, second, self.sent), ("queued", "queued", []))
        self.assertEqual(await flush_daily(self.db, sender=self.sender, today="2026-10-26"), "nothing")
        self.assertEqual(await flush_daily(self.db, sender=self.sender, today="2026-10-27"), "sent")
        ((_account, text),) = self.sent
        self.assertIn("2026-10-26", text)
        self.assertIn("Отправлено: 18", text)
        self.assertIn("Задач: 2", text)
        self.assertEqual(await self.db.get_setting(SUMMARY_PENDING_KEY), "")
        self.assertEqual(await flush_daily(self.db, sender=self.sender, today="2026-10-27"), "nothing")

    async def test_daily_flush_failure_keeps_counters(self):
        await self.configure(mode="daily")
        await deliver_task_summary(self.db, ENGINE_SUMMARY, sender=self.sender, today="2026-10-26")
        self.fail = True
        self.assertEqual(await flush_daily(self.db, sender=self.sender, today="2026-10-27"), "failed")
        pending = json.loads(await self.db.get_setting(SUMMARY_PENDING_KEY))
        self.assertEqual(pending["sent"], 9)
        # A task finishing the next day while Telegram is unreachable keeps the old counters.
        status = await deliver_task_summary(self.db, ENGINE_SUMMARY, sender=self.sender, today="2026-10-27")
        self.assertEqual(status, "failed")
        pending = json.loads(await self.db.get_setting(SUMMARY_PENDING_KEY))
        self.assertEqual((pending["date"], pending["sent"], pending["tasks"]), ("2026-10-27", 18, 2))

    async def test_flush_does_nothing_when_disabled_or_task_mode(self):
        self.assertEqual(await flush_daily(self.db, sender=self.sender, today="2026-10-27"), "disabled")
        await self.configure(mode="task")
        self.assertEqual(await flush_daily(self.db, sender=self.sender, today="2026-10-27"), "disabled")


if __name__ == "__main__":
    unittest.main()
