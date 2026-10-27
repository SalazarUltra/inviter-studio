"""Shared record budget of the multi-account parsers: fair shares that follow the accounts still working."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.tl.types import User

from core.chat_authors import ChatAuthorsEngine
from core.parser import MAX_RUN_RECORDS, MIN_GRANT, ParserEngine
from tests.support import DatabaseCase
from tests.test_chat_authors import FakeHistoryClient, message
from tests.test_parsing import channel

REAL_SLEEP = asyncio.sleep


async def yielding_sleep(_delay):
    await REAL_SLEEP(0.002)  # keeps the workers interleaving without long pauses


def make_ctx(budget, working, **extra):
    values = {"budget": budget, "outstanding": 0, "holders": 0, "working": set(working)}
    return SimpleNamespace(**(values | extra))


class BudgetPoolTests(unittest.TestCase):
    def test_workers_share_the_remaining_budget_fairly(self):
        ctx = make_ctx(MAX_RUN_RECORDS, {1, 2, 3})
        grants = [ParserEngine._reserve(ctx, 10_000) for _ in range(3)]
        self.assertEqual(grants, [6667, 6667, 6666])
        self.assertEqual((ctx.budget, ctx.outstanding, ctx.holders), (0, MAX_RUN_RECORDS, 3))

    def test_single_worker_takes_everything_it_asks_for(self):
        ctx = make_ctx(MAX_RUN_RECORDS, {1})
        self.assertEqual(ParserEngine._reserve(ctx, 10_000), 10_000)
        self.assertEqual(ctx.budget, 10_000)

    def test_finished_accounts_leave_their_share_to_the_others(self):
        ctx = make_ctx(1000, {1, 2})
        first = ParserEngine._reserve(ctx, 1000)
        self.assertEqual(first, 500)
        ctx.working.discard(2)  # the second account finished without using anything
        self.assertEqual(ParserEngine._reserve(ctx, 1000 - first, holding=True), 500)
        self.assertEqual((ctx.budget, ctx.holders), (0, 1))

    def test_unused_reservation_returns_and_holder_is_released(self):
        ctx = make_ctx(1000, {1, 2})
        granted = ParserEngine._reserve(ctx, 1000)
        ParserEngine._release(ctx, granted, 120, holding=True)
        self.assertEqual((ctx.budget, ctx.outstanding, ctx.holders), (880, 0, 0))

    def test_grant_is_never_smaller_than_a_page_and_never_above_what_is_left(self):
        ctx = make_ctx(150, {1, 2, 3})
        self.assertEqual(ParserEngine._reserve(ctx, 5000), MIN_GRANT)
        self.assertEqual(ParserEngine._reserve(ctx, 5000), 50)  # only 50 left
        self.assertEqual(ParserEngine._reserve(ctx, 5000), 0)
        self.assertEqual(ctx.budget, 0)

    def test_request_is_limited_by_what_the_source_needs(self):
        ctx = make_ctx(20_000, {1, 2})
        self.assertEqual(ParserEngine._reserve(ctx, 300), 300)


class GrowTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_a_holder_that_is_still_reading_and_takes_what_it_returns(self):
        ctx = make_ctx(0, {1, 2}, outstanding=400, holders=2, growing=0)
        engine = ParserEngine.__new__(ParserEngine)

        async def other_account_finishes():
            await REAL_SLEEP(0.05)
            ParserEngine._release(ctx, 200, 50, holding=True)
            ctx.working.discard(2)  # ... and its worker has no sources left

        with patch("core.parser.GROW_WAIT_SECONDS", 0.01):
            granted, _ = await asyncio.gather(engine._grow(ctx, 300), other_account_finishes())
        self.assertEqual(granted, 150)
        self.assertEqual(ctx.growing, 0)

    async def test_holders_that_all_wait_do_not_deadlock(self):
        ctx = make_ctx(0, {1, 2}, outstanding=400, holders=2, growing=0)
        engine = ParserEngine.__new__(ParserEngine)
        with patch("core.parser.GROW_WAIT_SECONDS", 0.01):
            results = await asyncio.wait_for(asyncio.gather(engine._grow(ctx, 300), engine._grow(ctx, 300)), 5)
        self.assertEqual(results, [0, 0])
        self.assertEqual(ctx.growing, 0)

    async def test_gives_up_after_the_wait_limit_when_an_idle_account_never_starts(self):
        ctx = make_ctx(300, {1, 2}, outstanding=100, holders=1, growing=0)  # account 2 never reserves
        engine = ParserEngine.__new__(ParserEngine)
        with patch("core.parser.GROW_WAIT_SECONDS", 0.001), patch("core.parser.GROW_WAIT_STEPS", 5):
            granted = await engine._grow(ctx, 300)
        self.assertEqual(granted, 150)  # then takes its fair share instead of waiting forever


class AuthorsDynamicBudgetTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.first, self.second = await self.add_account(1), await self.add_account(2)
        for patcher in (
            patch("core.chat_authors.asyncio.sleep", new=yielding_sleep),
            patch("core.parser.GROW_WAIT_STEPS", 5000),  # the fake sleep is short: keep the real time limit
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def history(self, user_id, count, entity_id):
        return FakeHistoryClient(
            messages=[message(number, user_id=user_id) for number in range(count, 0, -1)],
            users=[User(id=user_id, username=f"author_{user_id}")],
            entity=channel(entity_id),
        )

    async def run_authors(self, clients, sources, limit):
        def factory(account, db=None):
            return AsyncMock(client=clients[account["id"]])

        with patch("core.chat_authors.AccountManager", side_effect=factory):
            return await asyncio.wait_for(
                ChatAuthorsEngine(self.db).start([self.first, self.second], sources, limit), 20
            )

    async def test_budget_left_by_a_short_source_goes_to_the_account_still_reading(self):
        big, small = self.history(10, 400, 101), self.history(20, 50, 102)
        with patch("core.chat_authors.MAX_RUN_RECORDS", 400):
            result = await self.run_authors({self.first["id"]: big, self.second["id"]: small}, ["big", "small"], 400)
        rows, _ = await self.db.get_parse_results(result["run_id"])
        counts = {item["user_id"]: item["message_count"] for item in rows}
        # An equal split would stop the big source at 200 messages; the 150 the small one did not need are reused.
        self.assertEqual(counts, {10: 350, 20: 50})
        self.assertEqual(result["status"], "partial")

    async def test_failed_account_returns_its_share_to_the_other_one(self):
        from telethon.errors import FloodWaitError

        flooded = self.history(10, 400, 101)
        flooded.fail_offset, flooded.failure = 0, FloodWaitError(None, 90)
        healthy = self.history(20, 400, 102)
        with patch("core.chat_authors.MAX_RUN_RECORDS", 300):
            result = await self.run_authors(
                {self.first["id"]: flooded, self.second["id"]: healthy}, ["flooded", "healthy"], 400
            )
        rows, _ = await self.db.get_parse_results(result["run_id"])
        # The flooded account read nothing and its reservation is back: the healthy one can use the whole budget.
        self.assertEqual(sum(item["message_count"] for item in rows), 300)
        self.assertEqual({item["user_id"] for item in rows}, {20})

    async def test_budget_is_never_exceeded_with_many_accounts(self):
        third = await self.add_account(3)
        clients = {
            self.first["id"]: self.history(10, 400, 101),
            self.second["id"]: self.history(20, 400, 102),
            third["id"]: self.history(30, 400, 103),
        }

        def factory(account, db=None):
            return AsyncMock(client=clients[account["id"]])

        with (
            patch("core.chat_authors.MAX_RUN_RECORDS", 500),
            patch("core.chat_authors.AccountManager", side_effect=factory),
        ):
            result = await asyncio.wait_for(
                ChatAuthorsEngine(self.db).start([self.first, self.second, third], ["a", "b", "c"], 400), 20
            )
        rows, _ = await self.db.get_parse_results(result["run_id"])
        total = sum(item["message_count"] for item in rows)
        self.assertEqual(total, 500)
        self.assertEqual({item["user_id"] for item in rows}, {10, 20, 30})  # nobody was starved


if __name__ == "__main__":
    unittest.main()
