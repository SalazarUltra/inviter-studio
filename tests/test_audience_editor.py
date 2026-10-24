import unittest

from core.database import Database
from core.validation import parse_usernames
from tests.support import DatabaseCase


class RecipientParsingTests(unittest.TestCase):
    def test_pasted_lines_links_bom_and_duplicates(self):
        self.assertEqual(
            parse_usernames("\ufeff@Alice\r\n\r\n# comment\nhttps://t.me/Bobby\nalice\n"), ["alice", "bobby"]
        )

    def test_invalid_line_reports_position_before_any_write(self):
        with self.assertRaisesRegex(ValueError, "Строка 3"):
            parse_usernames("alice\n\nbad name\ncharlie")

    def test_empty_field_means_empty_list(self):
        self.assertEqual(parse_usernames("  \n# comment\r\n"), [])


class AudienceEditorTests(DatabaseCase):
    async def test_edit_removes_adds_reorders_and_preserves_ids_and_outcomes(self):
        await self.db.bulk_insert_targets(["alice", "bobby", "charlie"])
        before = {row["username"]: row for row in await self.db.get_targets()}
        await self.db.mark_target(before["alice"]["id"], "sent", error_message="confirmed")
        expected = await self.db.get_pending_list()
        result = await self.db.replace_pending_targets(["delta", "charlie", "charlie", "alice"], expected)
        self.assertEqual(result, {"pending": 2, "removed": 1, "inserted": 1, "processed": 1})
        rows = {row["username"]: row for row in await Database().get_targets()}
        self.assertEqual(set(rows), {"alice", "charlie", "delta"})
        self.assertEqual(
            rows["alice"],
            before["alice"]
            | {"status": "sent", "error_message": "confirmed", "processed_at": rows["alice"]["processed_at"]},
        )
        self.assertEqual(rows["charlie"]["id"], before["charlie"]["id"])
        self.assertEqual([row["username"] for row in await self.db.get_pending_list()], ["delta", "charlie"])
        self.assertEqual((await self.db.claim_next_pending_target())["username"], "delta")

    async def test_clear_preserves_results_other_campaign_and_delivery_ledger(self):
        account = await self.add_account()
        await self.db.bulk_insert_targets(["alice", "bobby"])
        target = await self.db.claim_next_pending_target()
        await self.db.reserve_delivery(target["id"], account["id"], 88001)
        await self.db.finish_delivery(target["id"], "sent", message_id=10)
        other_id = await self.db.create_campaign("Other", "messages")
        other = self.db.for_campaign(other_id)
        await other.bulk_insert_targets(["other_user"])
        result = await self.db.replace_pending_targets([], await self.db.get_pending_list())
        self.assertEqual(result["removed"], 1)
        self.assertEqual((await self.db.get_targets())[0]["status"], "sent")
        self.assertEqual(await self.db.delivery_exists("alice"), "sent")
        self.assertEqual(await other.count_targets(), 1)
        logs = await self.db.get_logs()
        self.assertTrue(any(row["action"] == "AUDIENCE_EDIT" for row in logs))

    async def test_changed_snapshot_rejected_without_losing_new_import(self):
        await self.db.bulk_insert_targets(["alice"])
        expected = await self.db.get_pending_list()
        await self.db.bulk_insert_targets(["bobby"])
        with self.assertRaisesRegex(RuntimeError, "Очередь изменилась"):
            await self.db.replace_pending_targets(["charlie"], expected)
        self.assertEqual({row["username"] for row in await self.db.get_targets()}, {"alice", "bobby"})

    async def test_running_or_paused_queue_cannot_be_edited(self):
        await self.db.bulk_insert_targets(["alice"])
        expected = await self.db.get_pending_list()
        for state in ("running", "paused"):
            await self.db.set_setting("task_status", state)
            with self.assertRaisesRegex(RuntimeError, "остановите"):
                await self.db.replace_pending_targets([], expected)
        self.assertEqual(await self.db.count_targets(), 1)

    async def test_running_campaign_cannot_import_or_shuffle_queue(self):
        await self.db.bulk_insert_targets(["alice", "bobby"])
        for state in ("running", "paused"):
            await self.db.set_setting("task_status", state)
            with self.assertRaisesRegex(RuntimeError, "остановите"):
                await self.db.bulk_insert_targets(["charlie"])
            with self.assertRaisesRegex(RuntimeError, "остановите"):
                await self.db.shuffle_pending_targets()
        self.assertEqual({row["username"] for row in await self.db.get_targets()}, {"alice", "bobby"})

    async def test_message_campaign_import_marks_previously_sent_as_no_repeat(self):
        campaign = self.db.for_campaign(await self.db.create_campaign("Messages", "messages"))
        account = await self.add_account()
        await campaign.bulk_insert_targets(["alice"])
        target = await campaign.claim_next_pending_target()
        await campaign.reserve_delivery(target["id"], account["id"], 88001)
        await campaign.finish_delivery(target["id"], "sent", message_id=10)
        await campaign.clear_targets()

        await campaign.bulk_insert_targets(["alice", "bobby"])
        rows = {row["username"]: row for row in await campaign.get_targets()}
        self.assertEqual(rows["alice"]["status"], "already_sent")
        self.assertIn("повтор отключён", rows["alice"]["error_message"].lower())
        self.assertEqual(rows["bobby"]["status"], "pending")
        self.assertEqual([row["username"] for row in await campaign.get_pending_list()], ["bobby"])

    async def test_processing_target_blocks_replacement_even_without_status_setting(self):
        await self.db.bulk_insert_targets(["alice", "bobby"])
        await self.db.claim_next_pending_target()
        with self.assertRaisesRegex(RuntimeError, "остановите"):
            await self.db.replace_pending_targets([], await self.db.get_pending_list())
        self.assertEqual(await self.db.count_targets(), 2)

    async def test_invalid_input_does_not_partially_remove_existing_queue(self):
        await self.db.bulk_insert_targets(["alice"])
        with self.assertRaises(ValueError):
            await self.db.replace_pending_targets(["bobby", "bad name"], await self.db.get_pending_list())
        self.assertEqual([row["username"] for row in await self.db.get_targets()], ["alice"])
