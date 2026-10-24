import asyncio
import csv
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.errors import AuthKeyUnregisteredError, ChannelPrivateError, FloodWaitError
from telethon.tl.functions.messages import GetHistoryRequest
from telethon.tl.types import (
    Chat,
    ChatPhotoEmpty,
    Message,
    MessageActionEmpty,
    MessageFwdHeader,
    MessageService,
    PeerChannel,
    PeerUser,
    User,
)

from core.chat_authors import ChatAuthorsEngine, filter_author_stats, history_period
from core.database import init_db
from core.export import write_parsed
from core.parser import filter_members, group_members, unique_members
from tests.support import DatabaseCase
from tests.test_parsing import channel, row

NOW = datetime.now(timezone.utc)


def message(number, user_id=1, when=None, **kwargs):
    return Message(
        id=number,
        peer_id=PeerChannel(101),
        from_id=PeerUser(user_id),
        date=when or NOW - timedelta(hours=1),
        message="SECRET_BODY_DO_NOT_SAVE",
        **kwargs,
    )


class FakeHistoryClient:
    """The authors mode may only resolve sources and read normal accessible history."""

    def __init__(
        self,
        messages=None,
        users=None,
        entity=None,
        *,
        fail_offset=None,
        failure=None,
        block_offset=None,
        repeat=False,
        ignore_date=False,
    ):
        self.messages = messages if messages is not None else [message(3), message(2), message(1, 2)]
        self.users = (
            users
            if users is not None
            else [User(id=1, username="alice", phone="PRIVATE_PHONE", access_hash=42), User(id=2, username="bobby")]
        )
        self.entity = entity or channel(left=True)
        self.requests, self.references = [], []
        self.fail_offset, self.failure = fail_offset, failure
        self.block_offset, self.repeat, self.ignore_date = block_offset, repeat, ignore_date
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def get_entity(self, reference):
        self.references.append(reference)
        if reference == "missing_group":
            raise ChannelPrivateError(None)
        return self.entity

    async def __call__(self, request):
        self.requests.append(request)
        if not isinstance(request, GetHistoryRequest):
            raise AssertionError(f"Unexpected API method: {type(request).__name__}")
        assert 1 <= request.limit <= 100 and request.hash == 0
        assert not request.min_id and not request.max_id and not request.add_offset
        if request.offset_id == self.block_offset:
            self.entered.set()
            await self.release.wait()
        if request.offset_id == self.fail_offset:
            raise self.failure or ChannelPrivateError(request)
        available = (
            self.messages
            if self.repeat
            else [item for item in self.messages if not request.offset_id or item.id < request.offset_id]
        )
        if request.offset_date and not self.ignore_date:
            available = [item for item in available if item.date < request.offset_date]
        return SimpleNamespace(messages=available[: request.limit], users=self.users)


class AuthorsDataTests(unittest.TestCase):
    def test_relative_and_all_periods_have_utc_bounds(self):
        self.assertEqual(history_period("Последние 7 дней", now=NOW), (NOW - timedelta(days=7), NOW))
        self.assertEqual(history_period("Вся доступная история", now=NOW), (None, NOW))

    def test_custom_period_includes_entire_last_local_day(self):
        since, until = history_period("Свои даты", "2026-10-01", "2026-10-02")
        self.assertEqual(since.astimezone().replace(tzinfo=None), datetime(2026, 10, 1))
        self.assertEqual(until.astimezone().replace(tzinfo=None), datetime(2026, 10, 3))
        self.assertEqual(since.tzinfo, timezone.utc)

    def test_invalid_periods_dates_and_naive_now_rejected(self):
        for args in (
            ("unknown",),
            ("Свои даты", "2026-02-30", "2026-10-01"),
            ("Свои даты", "2026-10-02", "2026-10-01"),
            ("Свои даты", "", ""),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                history_period(*args)
        with self.assertRaises(ValueError):
            history_period("Последние 7 дней", now=NOW.replace(tzinfo=None))

    def test_author_totals_merge_sources_once_and_keep_earliest_latest_dates(self):
        rows = [
            row(
                1,
                role="unknown",
                message_count=2,
                first_message_at=(NOW - timedelta(days=5)).isoformat(),
                last_message_at=(NOW - timedelta(days=2)).isoformat(),
            ),
            row(
                1,
                source_id=-202,
                role="unknown",
                message_count=3,
                first_message_at=(NOW - timedelta(days=7)).isoformat(),
                last_message_at=(NOW - timedelta(days=1)).isoformat(),
            ),
        ]
        people = unique_members(rows + rows)
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["message_count"], 5)
        self.assertEqual(people[0]["first_message_at"], rows[1]["first_message_at"])
        self.assertEqual(people[0]["last_message_at"], rows[1]["last_message_at"])
        self.assertEqual([item["message_count"] for item in people[0]["memberships"]], [2, 3])
        self.assertEqual(group_members(rows, "messages"), {"5–19 сообщений": 1})

    def test_message_count_and_post_date_filters_use_found_messages_not_online(self):
        people = unique_members(
            [
                row(1, message_count=5, last_message_at=(NOW - timedelta(days=2)).isoformat(), activity="unknown"),
                row(2, message_count=30, last_message_at=(NOW - timedelta(days=20)).isoformat(), activity="online"),
                row(3, message_count=1),
                row(4, message_count=8, last_message_at=(NOW + timedelta(days=1)).isoformat()),
            ]
        )
        self.assertEqual(
            [person["user_id"] for person in filter_author_stats(people, min_messages=5, last_days=7, now=NOW)], [1]
        )
        self.assertFalse(filter_author_stats(people, min_messages=50))

    def test_source_filter_changes_count_scope_before_author_filter(self):
        rows = [row(1, message_count=2), row(1, source_id=-202, message_count=3)]
        self.assertEqual(len(filter_author_stats(unique_members(rows), min_messages=5)), 1)
        self.assertFalse(filter_author_stats(unique_members(filter_members(rows, source_id=-202)), min_messages=5))

    def test_csv_json_include_author_counters_and_txt_keeps_names(self):
        people = unique_members(
            [
                row(
                    1,
                    role="unknown",
                    message_count=5,
                    first_message_at=(NOW - timedelta(days=2)).isoformat(),
                    last_message_at=NOW.isoformat(),
                )
            ]
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            for name in ("csv", "json", "txt"):
                write_parsed(path / name, people, name)
            with (path / "csv").open(encoding="utf-8-sig", newline="") as handle:
                saved = list(csv.DictReader(handle))[0]
            self.assertEqual(saved["message_count"], "5")
            self.assertEqual(saved["last_message_at"], NOW.isoformat())
            self.assertEqual(json.loads((path / "json").read_text(encoding="utf-8"))[0]["message_count"], 5)
            self.assertEqual((path / "txt").read_text(encoding="utf-8"), "@person_1\n")


class AuthorsDatabaseTests(DatabaseCase):
    async def test_repeated_page_snapshot_does_not_inflate_counter(self):
        run_id = await self.db.create_parse_run(1, ["group_one"], mode="authors", scan_limit=100)
        await self.db.save_parse_source(run_id, "group_one", {"source_id": -100101}, "running", "")
        profile = row(
            1,
            role="unknown",
            message_count=3,
            first_message_at=(NOW - timedelta(days=3)).isoformat(),
            last_message_at=NOW.isoformat(),
        )
        for _ in range(2):
            await self.db.save_parsed_authors(run_id, [profile], "group_one", 10)
        await self.db.save_parsed_authors(run_id, [{**profile, "message_count": 2, "username": ""}], "group_one", 5)
        saved, sources = await self.db.get_parse_results(run_id)
        self.assertEqual((saved[0]["message_count"], saved[0]["username"], sources[0]["fetched"]), (3, "person_1", 10))
        self.assertEqual((await self.db.get_parse_runs())[0]["mode"], "authors")

    async def test_parser_v1_migration_preserves_old_runs_and_members(self):
        with closing(sqlite3.connect(self.root / "inviter.db")) as connection, connection:
            connection.executescript(
                "DROP TABLE parse_runs; CREATE TABLE parse_runs(id INTEGER PRIMARY KEY,account_id INTEGER,sources TEXT,status TEXT,started_at TEXT,finished_at TEXT,detail TEXT); INSERT INTO parse_runs VALUES(77,1,'[]','completed','2026-01-01',NULL,'old'); DROP TABLE parsed_members; CREATE TABLE parsed_members(run_id INTEGER,source_id INTEGER,user_id INTEGER,username TEXT,first_name TEXT,last_name TEXT,role TEXT,is_bot INTEGER,is_deleted INTEGER,is_premium INTEGER,has_photo INTEGER,activity TEXT,last_seen TEXT,PRIMARY KEY(run_id,source_id,user_id)); INSERT INTO parsed_members VALUES(77,-101,1,'alice','','','member',0,0,0,0,'unknown',NULL);"
            )
        await init_db()
        await init_db()
        self.assertEqual((await self.db.get_parse_runs())[0]["mode"], "members")
        saved, _ = await self.db.get_parse_results(77)
        self.assertEqual(
            (saved[0]["username"], saved[0]["message_count"], saved[0]["last_message_at"]), ("alice", 0, None)
        )


class AuthorsEngineTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.account = await self.add_account()
        self.engine = ChatAuthorsEngine(self.db)

    async def collect(self, client, sources=None, limit=2000, **bounds):
        manager = AsyncMock(client=client)
        with patch("core.chat_authors.AccountManager", return_value=manager):
            result = await self.engine.start(self.account, sources or ["group_one"], limit, **bounds)
        manager.disconnect.assert_awaited_once()
        return result, await self.db.get_parse_results(result["run_id"])

    async def test_counts_dates_identity_and_no_message_body_phone_or_hash(self):
        client = FakeHistoryClient(
            messages=[
                message(3, when=NOW - timedelta(days=1)),
                message(2, when=NOW - timedelta(days=2)),
                message(1, 2, when=NOW - timedelta(days=3)),
            ]
        )
        result, (rows, sources) = await self.collect(client)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([person["message_count"] for person in rows], [2, 1])
        self.assertEqual(rows[0]["first_message_at"], (NOW - timedelta(days=2)).isoformat())
        self.assertEqual(rows[0]["last_message_at"], (NOW - timedelta(days=1)).isoformat())
        self.assertEqual(rows[0]["role"], "unknown")
        self.assertEqual(sources[0]["membership"], "not_joined")
        self.assertFalse(any(value in str(rows) for value in ("SECRET_BODY", "PRIVATE_PHONE", "access_hash")))
        self.assertTrue(all(isinstance(request, GetHistoryRequest) for request in client.requests))

    async def test_history_access_does_not_require_member_list(self):
        result, (rows, _) = await self.collect(FakeHistoryClient(entity=channel(broadcast=True)))
        self.assertEqual((result["status"], len(rows)), ("completed", 2))

    async def test_private_unjoined_source_rejected_without_request(self):
        entity = channel(left=True)
        entity.username = None
        client = FakeHistoryClient(entity=entity)
        result, (rows, _) = await self.collect(client, [-1000000000101])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(rows or client.requests)

    async def test_basic_chat_history_uses_no_full_member_list_request(self):
        client = FakeHistoryClient(entity=Chat(45, "Обычная", ChatPhotoEmpty(), 2, NOW, 1))
        result, (rows, _) = await self.collect(client)
        self.assertEqual((result["status"], rows[0]["source_id"]), ("completed", -45))

    async def test_period_bounds_are_inclusive_start_exclusive_end(self):
        since, until = NOW - timedelta(days=7), NOW
        client = FakeHistoryClient(
            messages=[
                message(4, 9, until),
                message(3, 1, until - timedelta(seconds=1)),
                message(2, 2, since),
                message(1, 3, since - timedelta(seconds=1)),
            ],
            users=[User(id=i, username=f"person_{i}") for i in (1, 2, 3, 9)],
            ignore_date=True,
        )
        result, (rows, _) = await self.collect(client, since=since, until=until)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([person["user_id"] for person in rows], [1, 2])
        run = (await self.db.get_parse_runs())[0]
        self.assertEqual((run["since"], run["until"]), (since.isoformat(), until.isoformat()))

    async def test_anonymous_channel_service_and_self_are_not_mapped_to_people(self):
        anonymous = message(5)
        anonymous.from_id, anonymous.post_author = PeerChannel(101), "Anonymous Admin"
        absent = message(4)
        absent.from_id = None
        service = MessageService(
            id=3, peer_id=PeerChannel(101), from_id=PeerUser(1), date=NOW, action=MessageActionEmpty()
        )
        forwarded = message(1, 2, fwd_from=MessageFwdHeader(date=NOW, from_id=PeerUser(999)))
        result, (rows, sources) = await self.collect(
            FakeHistoryClient(
                messages=[anonymous, absent, service, message(2, 1), forwarded],
                users=[User(id=1, is_self=True), User(id=2, username="bobby")],
            )
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual([(person["user_id"], person["message_count"]) for person in rows], [(2, 1)])
        self.assertIn("пропущено: 4", sources[0]["detail"])

    async def test_unknown_visible_sender_id_is_kept_without_profile_or_lookup(self):
        client = FakeHistoryClient(messages=[message(1, 333)], users=[])
        _, (rows, _) = await self.collect(client)
        self.assertEqual((rows[0]["user_id"], rows[0]["username"], rows[0]["message_count"]), (333, "", 1))
        self.assertEqual(client.references, ["group_one"])

    async def test_bots_deleted_and_no_username_can_be_filtered_after_collection(self):
        _, (rows, _) = await self.collect(
            FakeHistoryClient(
                messages=[message(4, 1), message(3, 2), message(2, 3), message(1, 4)],
                users=[
                    User(id=1, username="alice"),
                    User(id=2, bot=True, username="bobby"),
                    User(id=3, deleted=True, username="deleted_one"),
                    User(id=4),
                ],
            )
        )
        self.assertEqual(len(rows), 4)
        self.assertEqual([person["user_id"] for person in filter_members(rows)], [1])

    async def test_pagination_limit_and_alias_do_not_inflate_totals(self):
        client = FakeHistoryClient(messages=[message(300 - i) for i in range(250)])
        result, (rows, sources) = await self.collect(client, ["group_one", -1000000000101], limit=120)
        self.assertEqual((result["status"], rows[0]["message_count"], len(sources)), ("partial", 120, 1))
        self.assertEqual([(request.offset_id, request.limit) for request in client.requests], [(0, 100), (201, 20)])

    async def test_repeated_history_page_stops_without_duplicates(self):
        client = FakeHistoryClient(messages=[message(300 - i) for i in range(100)], repeat=True)
        result, (rows, sources) = await self.collect(client)
        self.assertEqual((result["status"], rows[0]["message_count"], sources[0]["fetched"]), ("partial", 100, 100))
        self.assertEqual(len(client.requests), 2)

    async def test_empty_accessible_history_is_success_with_no_authors(self):
        result, (rows, sources) = await self.collect(FakeHistoryClient(messages=[]))
        self.assertEqual((result["status"], rows, sources[0]["fetched"]), ("completed", [], 0))

    async def test_multi_source_identity_merges_counts_but_preserves_origins(self):
        client = FakeHistoryClient()
        with patch.object(client, "get_entity", new=AsyncMock(side_effect=[channel(101), channel(102)])):
            _, (rows, _) = await self.collect(client, ["group_one", "group_two"])
        people = unique_members(rows)
        self.assertEqual([(person["user_id"], person["message_count"]) for person in people], [(1, 4), (2, 2)])
        self.assertEqual(len(people[0]["memberships"]), 2)

    async def test_flood_wait_stops_other_sources_and_keeps_page(self):
        client = FakeHistoryClient(
            messages=[message(300 - i) for i in range(250)], fail_offset=201, failure=FloodWaitError(None, 123)
        )
        result, (rows, _) = await self.collect(client, ["group_one", "group_two"])
        self.assertEqual((result["status"], rows[0]["message_count"]), ("partial", 100))
        self.assertEqual(client.references, ["group_one"])
        self.assertIsNotNone((await self.db.get_account(self.account["id"]))["flood_until"])

    async def test_revoked_session_stops_without_other_sources(self):
        client = FakeHistoryClient(fail_offset=0, failure=AuthKeyUnregisteredError(None))
        result, _ = await self.collect(client, ["group_one", "group_two"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(client.references, ["group_one"])

    async def test_other_access_failure_can_continue_with_next_source(self):
        result, (rows, sources) = await self.collect(FakeHistoryClient(), ["missing_group", "group_one"])
        self.assertEqual((result["status"], len(rows), len(sources)), ("partial", 2, 2))

    async def test_global_budget_counts_saved_pages_even_after_source_error(self):
        client = FakeHistoryClient(messages=[message(300 - i) for i in range(250)], fail_offset=201)
        with (
            patch("core.chat_authors.MAX_RUN_RECORDS", 150),
            patch.object(client, "get_entity", new=AsyncMock(side_effect=[channel(101), channel(102), channel(103)])),
        ):
            result, (rows, sources) = await self.collect(client, ["group_one", "group_two", "group_three"])
        self.assertEqual((result["status"], sum(person["message_count"] for person in rows)), ("partial", 150))
        self.assertEqual([source["fetched"] for source in sources], [100, 50])

    async def test_stop_keeps_committed_counters_and_recovery_does_not_resume(self):
        client = FakeHistoryClient(messages=[message(300 - i) for i in range(250)], block_offset=201)
        manager = AsyncMock(client=client)
        with patch("core.chat_authors.AccountManager", return_value=manager):
            task = asyncio.create_task(self.engine.start(self.account, ["group_one"], 2000))
            await asyncio.wait_for(client.entered.wait(), 5)
            self.engine.stop()
            result = await asyncio.wait_for(task, 5)
        await init_db()
        rows, sources = await self.db.get_parse_results(result["run_id"])
        self.assertEqual(
            (result["status"], rows[0]["message_count"], sources[0]["fetched"], sources[0]["status"]),
            ("stopped", 100, 100, "stopped"),
        )
        manager.disconnect.assert_awaited_once()

    async def test_check_uses_one_history_record_and_creates_no_run(self):
        client = FakeHistoryClient()
        manager = AsyncMock(client=client)
        with patch("core.chat_authors.AccountManager", return_value=manager):
            await self.engine.check(self.account, "group_one")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.requests[0].limit, 1)
        self.assertEqual(await self.db.get_parse_runs(), [])
        manager.disconnect.assert_awaited_once()

    async def test_invalid_limits_bounds_sources_fail_before_creation(self):
        for options in (
            {"limit": 0},
            {"limit": 10001},
            {"limit": 10, "since": NOW, "until": NOW},
            {"limit": 10, "since": NOW.replace(tzinfo=None)},
        ):
            with self.assertRaises(ValueError):
                await self.engine.start(self.account, ["group_one"], **options)
        self.assertEqual(await self.db.get_parse_runs(), [])
