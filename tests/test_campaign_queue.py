import unittest

from core.campaign_queue import (
    ON_ALWAYS,
    ON_SUCCESS,
    get_link,
    normalize_trigger,
    outcome_continues,
    plan_next,
    set_link,
)
from tests.support import DatabaseCase


class QueueRulesTests(unittest.TestCase):
    def test_trigger_rules(self):
        self.assertTrue(outcome_continues(ON_SUCCESS, "completed"))
        self.assertFalse(outcome_continues(ON_SUCCESS, "failed"))
        self.assertTrue(outcome_continues(ON_ALWAYS, "failed"))
        for trigger in (ON_SUCCESS, ON_ALWAYS):
            self.assertFalse(outcome_continues(trigger, "stopped"))  # the user stopped it: never continue
        self.assertEqual(normalize_trigger("junk"), ON_SUCCESS)


class CampaignQueueTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.first_id = (await self.db.get_campaigns())[0]["id"]
        self.second_id = await self.db.create_campaign("Вторая", "invite")
        self.third_id = await self.db.create_campaign("Третья", "messages")
        self.first, self.second, self.third = (
            self.db.for_campaign(i) for i in (self.first_id, self.second_id, self.third_id)
        )

    async def add_pending(self, db, count=2):
        await db.bulk_insert_targets([f"person{db.campaign_id}x{n}" for n in range(count)])

    async def test_link_defaults_and_roundtrip(self):
        self.assertEqual(await get_link(self.first), {"next_id": None, "on": ON_SUCCESS})
        await set_link(self.first, self.second_id, ON_ALWAYS)
        self.assertEqual(await get_link(self.first), {"next_id": self.second_id, "on": ON_ALWAYS})
        self.assertEqual((await get_link(self.second))["next_id"], None)  # per campaign
        await set_link(self.first, None, ON_SUCCESS)
        self.assertEqual(await get_link(self.first), {"next_id": None, "on": ON_SUCCESS})

    async def test_rejects_self_missing_loops_and_bad_trigger(self):
        with self.assertRaises(ValueError):
            await set_link(self.first, self.first_id)
        with self.assertRaises(ValueError):
            await set_link(self.first, 9999)
        with self.assertRaises(ValueError):
            await set_link(self.first, self.second_id, "sometimes")
        await set_link(self.first, self.second_id)
        await set_link(self.second, self.third_id)
        with self.assertRaises(ValueError):  # third -> first would close the circle first -> second -> third
            await set_link(self.third, self.first_id)

    async def test_copied_campaign_does_not_inherit_the_queue(self):
        await set_link(self.first, self.second_id, ON_ALWAYS)
        copy = self.db.for_campaign(await self.first.copy_campaign_settings("Копия"))
        self.assertEqual(await get_link(copy), {"next_id": None, "on": ON_SUCCESS})

    async def test_deleting_the_next_campaign_clears_links_to_it(self):
        await set_link(self.first, self.second_id)
        await self.second.delete_campaign()
        self.assertIsNone((await get_link(self.first))["next_id"])

    async def test_plan_offers_next_after_success_only_when_it_has_people(self):
        await set_link(self.first, self.second_id, ON_SUCCESS)
        self.assertEqual((await plan_next(self.first, {"outcome": "completed"}))["action"], "blocked")  # nobody queued
        await self.add_pending(self.second)
        plan = await plan_next(self.first, {"outcome": "completed"})
        self.assertEqual(
            (plan["action"], plan["next_id"], plan["name"], plan["pending"]), ("offer", self.second_id, "Вторая", 2)
        )
        self.assertIn("«Вторая»", plan["message"])
        for outcome in ("failed", "stopped"):
            self.assertEqual((await plan_next(self.first, {"outcome": outcome}))["action"], "none")

    async def test_always_continues_after_failure_but_never_after_stop(self):
        await set_link(self.first, self.second_id, ON_ALWAYS)
        await self.add_pending(self.second, 1)
        self.assertEqual((await plan_next(self.first, {"outcome": "failed"}))["action"], "offer")
        self.assertEqual((await plan_next(self.first, {"outcome": "stopped"}))["action"], "none")

    async def test_no_link_means_nothing_to_offer(self):
        self.assertEqual((await plan_next(self.first, {"outcome": "completed"}))["action"], "none")

    async def test_chain_guard_blocks_a_campaign_already_run_in_this_queue(self):
        await set_link(self.first, self.second_id)
        await self.add_pending(self.second)
        plan = await plan_next(self.first, {"outcome": "completed"}, chain=[self.second_id, self.first_id])
        self.assertEqual(plan["action"], "blocked")

    async def test_following_a_deleted_campaign_is_blocked_not_started(self):
        await set_link(self.first, self.second_id)
        await self.add_pending(self.second)
        await self.second.delete_campaign()
        await self.first.set_setting("queue_next_campaign_id", str(self.second_id))  # a stale value
        plan = await plan_next(self.first, {"outcome": "completed"})
        self.assertEqual(plan["action"], "blocked")


if __name__ == "__main__":
    unittest.main()
