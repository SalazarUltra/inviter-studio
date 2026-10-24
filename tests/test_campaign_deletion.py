import sqlite3
from contextlib import closing

from core.database import init_db
from tests.support import DatabaseCase


class CampaignDeletionTests(DatabaseCase):
    async def test_delete_is_scoped_cancels_followup_and_keeps_global_data(self):
        account = await self.add_account()
        other = self.db.for_campaign(await self.db.create_campaign("Keep", "messages"))
        await other.bulk_insert_targets(["bobby"])
        await other.set_setting("message_text", "Keep")
        await self.db.bulk_insert_targets(["alice"])
        target = (await self.db.get_targets())[0]
        await self.db.set_setting("message_text", "Remove")
        await self.db.add_log(None, "alice", "TEST", "Remove")
        ai_id = await self.db.add_ai_config("Keep", "openai", "fake-key", "gpt-6-luna")
        await self.db.create_parse_run(account["id"], ["@test_group"])
        with closing(sqlite3.connect(self.path_patch.new)) as connection, connection:
            connection.execute(
                "INSERT INTO message_deliveries(username,user_id,campaign_id,target_id,account_id,random_id,state) VALUES ('alice',123,1,?,?,1,'sent')",
                (target["id"], account["id"]),
            )
            connection.execute(
                "INSERT INTO message_followups(username,user_id,access_hash,campaign_id,target_id,account_id,text,state) VALUES ('alice',123,456,1,?,?,'Remove','waiting')",
                (target["id"], account["id"]),
            )
        selected = await self.db.delete_campaign()
        self.assertEqual(selected, other.campaign_id)
        self.assertEqual([row["id"] for row in await self.db.get_campaigns()], [other.campaign_id])
        self.assertEqual(await self.db.get_targets(), [])
        self.assertEqual(await self.db.get_logs(), [])
        self.assertIsNone(await self.db.get_setting("message_text"))
        self.assertEqual(await other.get_setting("message_text"), "Keep")
        self.assertEqual(len(await other.get_targets()), 1)
        self.assertEqual((await self.db.get_ai_configs())[0]["id"], ai_id)
        self.assertIsNotNone(await self.db.get_account(account["id"]))
        self.assertEqual(await other.delivery_exists("alice"), "sent")
        with closing(sqlite3.connect(self.path_patch.new)) as connection:
            self.assertEqual(
                connection.execute("SELECT state,text FROM message_followups").fetchone(), ("cancelled", "")
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM parse_runs").fetchone()[0], 1)

    async def test_last_campaign_gets_new_identity_and_does_not_reappear_after_restart(self):
        await self.db.set_setting("message_text", "Remove")
        with closing(sqlite3.connect(self.path_patch.new)) as connection, connection:
            connection.execute("INSERT INTO settings(key,value) VALUES ('message_text','Legacy')")
        identifier = await self.db.delete_campaign()
        self.assertNotEqual(identifier, 1)
        await init_db()
        campaigns = await self.db.get_campaigns()
        self.assertEqual([row["id"] for row in campaigns], [identifier])
        self.assertEqual(await self.db.get_setting("last_campaign_id"), str(identifier))
        self.assertIsNone(await self.db.get_setting("message_text"))
        self.assertEqual(await self.db.for_campaign(identifier).get_targets(), [])

    async def test_running_and_paused_campaigns_cannot_be_deleted(self):
        for state in ("running", "paused"):
            await self.db.set_setting("task_status", state)
            with self.assertRaises(RuntimeError):
                await self.db.delete_campaign()
            self.assertEqual(len(await self.db.get_campaigns()), 1)
        await self.db.set_setting("task_status", "stopped")
        await self.db.bulk_insert_targets(["alice"])
        await self.db.claim_next_pending_target()
        with self.assertRaises(RuntimeError):
            await self.db.delete_campaign()

    async def test_stale_handles_cannot_repopulate_deleted_campaign(self):
        await self.db.delete_campaign()
        for operation in (
            self.db.delete_campaign(),
            self.db.get_campaign_kind(),
            self.db.rename_campaign("Restore"),
            self.db.copy_campaign_settings("Restore"),
            self.db.bulk_insert_targets(["alice"]),
            self.db.replace_pending_targets(["alice"], []),
            self.db.set_setting("message_text", "Restore"),
            self.db.save_campaign_settings({"message_text": "Restore"}),
            self.db.begin_run("test"),
        ):
            with self.assertRaises(ValueError):
                await operation
