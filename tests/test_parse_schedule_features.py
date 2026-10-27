"""Multi-account parsing, «new since previous run», id-only audience lines, smart-interval time zone, weekday gate."""

import asyncio
import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telethon.errors import FloodWaitError
from telethon.tl.types import User

from core.audience_import import load_audience_file, parse_audience_csv, parse_audience_text
from core.export import parsed_txt_content, txt_lines_with_ids, write_parsed, write_targets_csv
from core.inviter import InviterEngine
from core.parse_compare import describe_difference, only_new, user_id_set
from core.parser import ParserEngine, audience_members, split_sources_round_robin, unique_members
from core.scheduling import (
    SMART_TZ_LOCAL,
    SMART_TZ_MOSCOW,
    normalize_smart_tz,
    parse_schedule_days,
    smart_hour,
    validate_smart_tz,
    weekday_allowed,
)
from core.validation import normalize_recipient, parse_usernames, recipient_label, recipient_line, recipient_user_id
from tests.support import DatabaseCase
from tests.test_parsing import FakeClient, channel, row


class SmartTimezoneTests(DatabaseCase):
    def engine(self, tz=SMART_TZ_LOCAL):
        engine = InviterEngine(self.db, lambda *args: None)
        engine.smart_tz = tz
        return engine

    def test_moscow_hour_is_taken_from_the_utc_moment(self):
        moment = datetime(2026, 1, 5, 21, 30, tzinfo=timezone.utc)  # 00:30 next day in Moscow
        self.assertEqual(smart_hour(SMART_TZ_MOSCOW, moment), 0)
        self.assertEqual(smart_hour("Europe/Moscow", datetime(2026, 7, 1, 9, 0, tzinfo=timezone.utc)), 12)

    def test_local_hour_is_the_wall_clock_of_a_naive_time(self):
        self.assertEqual(smart_hour(SMART_TZ_LOCAL, datetime(2026, 1, 5, 14, 5)), 14)
        self.assertEqual(smart_hour("", datetime(2026, 1, 5, 3, 0)), 3)

    def test_unknown_zone_means_local_and_strict_validation_rejects_it(self):
        self.assertEqual(normalize_smart_tz("Asia/Nowhere"), SMART_TZ_LOCAL)
        self.assertEqual(validate_smart_tz("Europe/Moscow"), SMART_TZ_MOSCOW)
        with self.assertRaises(ValueError):
            validate_smart_tz("Asia/Nowhere")

    def test_smart_interval_follows_the_chosen_zone(self):
        # 22:00 UTC is 01:00 in Moscow (night, x2.5) but 22:00 in the naive-UTC "computer" zone (evening, x1.5).
        moment = datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc)
        with patch("core.inviter.datetime") as clock:
            clock.now.return_value = moment
            moscow = self.engine(SMART_TZ_MOSCOW)._get_smart_interval(100, 100)
            self.assertEqual(moscow, 250)
        with patch("core.inviter.datetime") as clock:
            clock.now.return_value = datetime(2026, 1, 5, 12, 0)  # naive local noon
            self.assertEqual(self.engine(SMART_TZ_LOCAL)._get_smart_interval(100, 100), 100)
            self.assertEqual(self.engine(SMART_TZ_MOSCOW)._get_smart_interval(100, 100) > 0, True)

    async def test_start_loads_the_zone_from_campaign_settings(self):
        account = await self.add_account()
        await self.db.save_campaign_settings({"smart_interval_tz": "Europe/Moscow"})
        self.assertEqual(await self.db.get_setting("smart_interval_tz"), "Europe/Moscow")
        engine = self.engine()
        manager = AsyncMock()
        manager.account = account
        manager.check_admin_rights.return_value = {"ok": True}
        engine._wait = AsyncMock()
        with patch("core.inviter.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "group", 10, 10), 5)
        self.assertEqual(engine.smart_tz, SMART_TZ_MOSCOW)


class WeekdayGateTests(DatabaseCase):
    def engine(self):
        self.logs = []
        return InviterEngine(self.db, lambda level, text: self.logs.append((level, text)))

    async def configure(self, days):
        await self.db.set_setting("schedule_enabled", "1")
        await self.db.set_setting("schedule_from", "09:00")
        await self.db.set_setting("schedule_to", "18:00")
        if days is not None:
            await self.db.set_setting("schedule_days", days)

    async def test_selected_weekdays_gate_the_task_like_outside_hours(self):
        await self.configure("0,2,4")  # Mon, Wed, Fri
        engine = self.engine()
        with patch("core.inviter.datetime") as clock:
            clock.now.return_value = datetime(2026, 1, 5, 12, 0)  # Monday
            self.assertTrue(await engine._is_within_schedule())
            clock.now.return_value = datetime(2026, 1, 6, 12, 0)  # Tuesday
            self.assertFalse(await engine._is_within_schedule())
            clock.now.return_value = datetime(2026, 1, 7, 8, 0)  # Wednesday, before the window
            self.assertFalse(await engine._is_within_schedule())
        self.assertEqual(sum("не рабочий день" in text for _, text in self.logs), 1)

    async def test_default_is_every_day(self):
        await self.configure(None)
        engine = self.engine()
        with patch("core.inviter.datetime") as clock:
            for day in range(5, 12):  # a full week
                clock.now.return_value = datetime(2026, 1, day, 12, 0)
                self.assertTrue(await engine._is_within_schedule())

    async def test_gate_is_ignored_when_the_schedule_is_disabled(self):
        await self.configure("0")
        await self.db.set_setting("schedule_enabled", "0")
        with patch("core.inviter.datetime") as clock:
            clock.now.return_value = datetime(2026, 1, 6, 12, 0)
            self.assertTrue(await self.engine()._is_within_schedule())

    async def test_corrupt_days_pause_the_task_with_a_warning(self):
        await self.configure("monday")
        engine = self.engine()
        with patch("core.inviter.datetime") as clock:
            clock.now.return_value = datetime(2026, 1, 5, 12, 0)
            self.assertFalse(await engine._is_within_schedule())
        self.assertTrue(any(level == "WARN" for level, _ in self.logs))

    async def test_settings_validation_and_bitmask(self):
        await self.db.save_campaign_settings({"schedule_days": "0,1,2"})
        self.assertEqual(await self.db.get_setting("schedule_days"), "0,1,2")
        for bad in ("7", "", "x", "mask:0"):
            if bad == "":
                continue  # empty means every day
            with self.assertRaises(ValueError, msg=bad):
                parse_schedule_days(bad)
        self.assertEqual(parse_schedule_days("mask:5"), frozenset({0, 2}))
        self.assertEqual(parse_schedule_days(None), frozenset(range(7)))
        self.assertEqual(parse_schedule_days([6, 5]), frozenset({5, 6}))
        self.assertTrue(weekday_allowed("5,6", datetime(2026, 1, 10)))  # Saturday
        self.assertFalse(weekday_allowed("5,6", datetime(2026, 1, 12)))  # Monday


class ParseDiffTests(DatabaseCase):
    def test_only_new_keeps_people_absent_from_the_previous_run(self):
        without_id = {**row(4), "user_id": None}
        rows = [row(1), row(2), row(3, source_id=-100102), row(3), without_id]
        fresh = only_new(rows, {1, 2})
        self.assertEqual([item["user_id"] for item in fresh], [3, 3, None])
        self.assertEqual(len(unique_members(fresh)), 2)
        self.assertEqual(only_new(rows, set(user_id_set(rows))), [rows[4]])  # nothing proves the id-less row was seen

    def test_description_counts_new_old_and_gone(self):
        self.assertEqual(describe_difference({1, 2, 3}, {2, 3, 4, 5}), "Новых: 1 · было в прошлом запуске: 2 · ушло: 2")

    async def test_previous_run_ids_come_from_the_database(self):
        account = await self.add_account()
        first = await self.run_parse(account, [User(id=1, username="alice"), User(id=2, username="bobby")])
        second = await self.run_parse(account, [User(id=2, username="bobby"), User(id=3, username="carol")])
        before = await self.db.get_parse_user_ids(first)
        rows, _ = await self.db.get_parse_results(second)
        self.assertEqual(before, {1, 2})
        self.assertEqual([item["user_id"] for item in only_new(rows, before)], [3])
        self.assertEqual(await self.db.get_parse_user_ids(999), set())

    async def run_parse(self, account, users):
        manager = AsyncMock(client=FakeClient(users=users))
        with patch("core.parser.AccountManager", return_value=manager), patch("core.parser.PAGE_DELAY", 0):
            result = await ParserEngine(self.db).start(account, ["group_one"], 100)
        return result["run_id"]


class IdAudienceLineTests(DatabaseCase):
    def test_id_lines_are_recipients(self):
        self.assertEqual(parse_usernames("id:123456789\n@alice"), ["id:123456789", "alice"])
        self.assertEqual(normalize_recipient("ID: 77"), "id:77")
        self.assertEqual(recipient_user_id("id:77"), 77)
        self.assertIsNone(recipient_user_id("alice"))
        self.assertEqual((recipient_label("id:77"), recipient_label("alice")), ("ID 77", "@alice"))
        self.assertEqual((recipient_line("id:77"), recipient_line("alice")), ("id:77", "@alice"))
        with self.assertRaises(ValueError):
            parse_usernames("id:0")

    def test_text_accepts_numbers_ids_links_and_pairs(self):
        items = parse_audience_text("# list\n123456789\nid:555\n@alice\nhttps://t.me/bobby\ncarol;42\n")
        self.assertEqual(
            [(item["username"], item["user_id"]) for item in items],
            [("id:123456789", 123456789), ("id:555", 555), ("alice", None), ("bobby", None), ("carol", 42)],
        )
        with self.assertRaises(ValueError):
            parse_audience_text("@alice\nnot a recipient!!")

    def test_csv_export_can_be_imported_back(self):
        items = parse_audience_csv("user_id,username,status\n10,alice,pending\n11,,pending\n,bobby,pending\n")
        self.assertEqual(
            [(item["username"], item["user_id"]) for item in items],
            [("alice", 10), ("id:11", 11), ("bobby", None)],
        )
        self.assertEqual([item["username"] for item in parse_audience_csv("@alice\nid:5\n")], ["alice", "id:5"])

    def test_files_are_loaded_by_suffix(self):
        with tempfile.TemporaryDirectory() as folder:
            text, table = Path(folder, "a.txt"), Path(folder, "b.csv")
            text.write_text("id:9\n", encoding="utf-8")
            table.write_text("user_id,username\n8,\n", encoding="utf-8")
            self.assertEqual(load_audience_file(str(text))[0]["username"], "id:9")
            self.assertEqual(load_audience_file(str(table))[0]["username"], "id:8")

    async def test_import_keeps_id_only_targets_and_dedups_by_user_id(self):
        stats = await self.db.import_audience(
            [
                {"username": "id:42", "user_id": 42},
                {"username": "alice", "user_id": 42},
                {"username": "id:77", "user_id": 77},
            ]
        )
        self.assertEqual((stats["inserted"], stats["id_only"]), (2, 1))
        targets = {item["username"]: item["user_id"] for item in await self.db.get_targets(limit=None)}
        self.assertEqual(targets, {"alice": 42, "id:77": 77})
        again = await self.db.import_audience(
            [{"username": "id:77", "user_id": 77}, {"username": "id:42", "user_id": 42}]
        )
        self.assertEqual((again["inserted"], again["duplicates"]), (0, 2))  # already queued, also under @alice

    async def test_messages_campaign_skips_id_only_people(self):
        messages = self.db.for_campaign(await self.db.create_campaign("Сообщения", "messages"))
        stats = await messages.import_audience([{"username": "id:42", "user_id": 42}, {"username": "alice"}])
        self.assertEqual((stats["inserted"], stats["no_username"]), (1, 1))
        self.assertEqual([item["username"] for item in await messages.get_targets(limit=None)], ["alice"])
        with self.assertRaises(ValueError):
            await messages.replace_pending_targets(["id:42"], await messages.get_pending_list())

    async def test_replace_pending_stores_the_id_for_invites(self):
        await self.db.replace_pending_targets(["id:42", "alice"], await self.db.get_pending_list())
        targets = {item["username"]: item["user_id"] for item in await self.db.get_targets(limit=None)}
        self.assertEqual(targets, {"id:42": 42, "alice": None})

    def test_parse_rows_without_username_become_id_lines_on_request(self):
        rows = [row(1), {**row(2), "username": ""}, {**row(3), "username": "", "is_bot": 1}]
        members, skipped = audience_members(rows)
        self.assertEqual([item["username"] for item in members], ["person_1"])
        members, skipped = audience_members(rows, include_id_only=True)
        self.assertEqual([item["username"] for item in members], ["person_1", "id:2"])
        self.assertEqual(skipped["id_only"], 1)
        self.assertEqual(txt_lines_with_ids(unique_members(rows)), ["@person_1", "id:2", "id:3"])

    def test_exports_carry_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "audience.csv")
            write_targets_csv(str(path), [{"username": "id:5", "user_id": 5, "status": "pending"}])
            with open(path, encoding="utf-8-sig", newline="") as handle:
                table = list(csv.DictReader(handle))
            self.assertEqual((table[0]["username"], table[0]["user_id"]), ("id:5", "5"))
            lines = Path(folder, "people.txt")
            write_parsed(lines, [row(1), {**row(2), "username": ""}], "txt_ids")
            self.assertEqual(lines.read_text(encoding="utf-8").split(), ["@person_1", "id:2"])
            self.assertEqual(
                parsed_txt_content([row(1), {**row(2), "username": ""}], "txt_ids").split(),
                ["@person_1", "id:2"],
            )
            self.assertEqual(parsed_txt_content([row(1), {**row(2), "username": ""}], "txt").strip(), "@person_1")

    async def test_inviter_passes_the_id_of_an_id_only_target(self):
        account = await self.add_account()
        await self.db.import_audience([{"username": "id:42", "user_id": 42}])
        manager = AsyncMock()
        manager.account = account
        manager.check_admin_rights.return_value = {"ok": True}
        manager.add_user_to_chat.return_value = {"success": True}
        engine = InviterEngine(self.db, lambda *args: None)
        engine._wait = AsyncMock()
        engine._schedule_account_next = lambda *args, **kwargs: 0
        with patch("core.inviter.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "mychat", 10, 10), 10)
        call = manager.add_user_to_chat.await_args
        self.assertEqual((call.args[1], call.kwargs), ("id:42", {"user_id": 42}))
        self.assertEqual(await self.db.get_invited_keys("mychat"), {"id:42"})


class MultiAccountParsingTests(DatabaseCase):
    class GroupsClient(FakeClient):
        """Every reference is a different group, so sources can move between accounts."""

        def __init__(self, groups, **options):
            super().__init__(**options)
            self.groups = groups

        async def get_entity(self, reference):
            self.references.append(reference)
            number, self.users = self.groups[reference]
            self.entity = channel(number)
            self.count = len(self.users)
            return self.entity

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.first, self.second = await self.add_account(1), await self.add_account(2)
        for name in ("PAGE_DELAY", "SEARCH_DELAY"):
            patcher = patch(f"core.parser.{name}", 0)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.groups = {
            f"group_{number}": (100 + number, [User(id=number * 10 + 1, username=f"member_{number}")])
            for number in (1, 2, 3, 4)
        }

    def run_with(self, clients, sources, accounts=None):
        def factory(account, db=None):
            return AsyncMock(client=clients[account["id"]])

        async def call():
            with patch("core.parser.AccountManager", side_effect=factory):
                return await ParserEngine(self.db).start(accounts or [self.first, self.second], sources, 100)

        return asyncio.wait_for(call(), 10)

    def test_round_robin_split(self):
        self.assertEqual(
            split_sources_round_robin(["a", "b", "c", "d", "e"], [1, 2]), {1: ["a", "c", "e"], 2: ["b", "d"]}
        )
        self.assertEqual(split_sources_round_robin(["a"], [1, 2]), {1: ["a"], 2: []})
        self.assertEqual(split_sources_round_robin(["a", "b"], [7, 7]), {7: ["a", "b"]})

    async def test_sources_are_shared_between_accounts(self):
        clients = {
            self.first["id"]: self.GroupsClient(self.groups),
            self.second["id"]: self.GroupsClient(self.groups),
        }
        result = await self.run_with(clients, ["group_1", "group_2", "group_3", "group_4"])
        rows, sources = await self.db.get_parse_results(result["run_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(clients[self.first["id"]].references, ["group_1", "group_3"])
        self.assertEqual(clients[self.second["id"]].references, ["group_2", "group_4"])
        self.assertEqual(sorted(item["user_id"] for item in rows), [11, 21, 31, 41])
        self.assertEqual([item["status"] for item in sources], ["completed"] * 4)

    async def test_flood_wait_hands_the_sources_over_to_the_other_account(self):
        flooded = self.GroupsClient(self.groups, fail_offset=0, failure=FloodWaitError(None, 90))
        healthy = self.GroupsClient(self.groups)
        result = await self.run_with(
            {self.first["id"]: flooded, self.second["id"]: healthy}, ["group_1", "group_2", "group_3"]
        )
        rows, _ = await self.db.get_parse_results(result["run_id"])
        self.assertEqual(flooded.references, ["group_1"])  # stopped at the first request
        self.assertCountEqual(healthy.references, ["group_2", "group_1", "group_3"])
        self.assertEqual(sorted(item["user_id"] for item in rows), [11, 21, 31])
        self.assertIn("Аккаунты", result["detail"])

    async def test_flood_of_the_only_account_still_ends_the_run(self):
        flooded = self.GroupsClient(self.groups, fail_offset=0, failure=FloodWaitError(None, 90))
        result = await self.run_with({self.first["id"]: flooded}, ["group_1", "group_2"], accounts=[self.first])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(flooded.references, ["group_1"])

    async def test_single_account_list_behaves_like_a_single_account(self):
        client = self.GroupsClient(self.groups)
        result = await self.run_with({self.first["id"]: client}, ["group_1", "group_2"], accounts=[self.first])
        rows, _ = await self.db.get_parse_results(result["run_id"])
        self.assertEqual((result["status"], sorted(item["user_id"] for item in rows)), ("completed", [11, 21]))
        self.assertEqual(client.references, ["group_1", "group_2"])


if __name__ == "__main__":
    unittest.main()
