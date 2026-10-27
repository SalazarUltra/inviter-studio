import asyncio
import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.errors import AuthKeyUnregisteredError, ChatAdminRequiredError, FloodWaitError
from telethon.tl.functions.channels import GetFullChannelRequest, GetParticipantsRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.tl.types import (
    Channel,
    ChannelParticipant,
    ChannelParticipantsRecent,
    ChannelParticipantsSearch,
    Chat,
    ChatParticipant,
    ChatParticipantCreator,
    ChatParticipants,
    ChatParticipantsForbidden,
    ChatPhotoEmpty,
    User,
    UserProfilePhoto,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

from core.database import init_db
from core.export import write_parsed
from core.parser import ParserEngine, filter_members, group_members, member_row, parse_sources, unique_members
from tests.support import DatabaseCase

NOW = datetime.now(timezone.utc)


def row(user_id=1, source_id=-100101, **changes):
    result = member_row(User(id=user_id, username=f"person_{user_id}", first_name="Имя"), None, source_id)
    return {**result, "source_title": "Группа", **changes}


def channel(number=101, **changes):
    return Channel(
        id=number,
        title=f"Группа {number}",
        photo=ChatPhotoEmpty(),
        date=NOW,
        username=f"group_{number}",
        megagroup=True,
        **changes,
    )


class FakeClient:
    """Only normal list requests are accepted; joining, history and searches fail the test."""

    def __init__(
        self,
        entity=None,
        users=None,
        *,
        allowed=True,
        hidden=False,
        count=None,
        fail_offset=None,
        failure=None,
        repeat=False,
        block_offset=None,
        recent_cap=None,
    ):
        self.entity = entity or channel()
        self.users = users if users is not None else [User(id=1, username="alice"), User(id=2, username="bobby")]
        self.allowed, self.hidden = allowed, hidden
        self.count = len(self.users) if count is None else count
        self.fail_offset, self.failure = fail_offset, failure
        self.repeat, self.block_offset = repeat, block_offset
        self.recent_cap, self.searches = recent_cap, []
        self.requests, self.references = [], []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def get_entity(self, reference):
        self.references.append(reference)
        if reference == "missing_group":
            raise ValueError("Группа не найдена")
        return self.entity

    async def __call__(self, request):
        self.requests.append(request)
        if isinstance(request, GetFullChannelRequest):
            return SimpleNamespace(
                full_chat=SimpleNamespace(
                    participants_hidden=self.hidden, can_view_participants=self.allowed, participants_count=self.count
                )
            )
        if isinstance(request, GetFullChatRequest):
            return SimpleNamespace(
                full_chat=SimpleNamespace(
                    participants=ChatParticipants(
                        self.entity.id, [ChatParticipant(user.id, 9, NOW) for user in self.users], 1
                    )
                ),
                users=self.users,
            )
        if not isinstance(request, GetParticipantsRequest):
            raise AssertionError(f"Unexpected Telegram request: {type(request).__name__}")
        assert isinstance(request.filter, (ChannelParticipantsRecent, ChannelParticipantsSearch))
        assert 1 <= request.limit <= 200 and request.hash == 0
        if isinstance(request.filter, ChannelParticipantsSearch):
            self.searches.append(request.filter.q)
            pool = [user for user in self.users if request.filter.q in (user.username or "").lower()]
            users = pool[request.offset : request.offset + request.limit]
            return SimpleNamespace(participants=[ChannelParticipant(user.id, NOW) for user in users], users=users)
        if self.recent_cap is not None:
            visible = self.users[: self.recent_cap]
            users = visible[request.offset : request.offset + request.limit]
            return SimpleNamespace(participants=[ChannelParticipant(user.id, NOW) for user in users], users=users)
        if request.offset == self.block_offset:
            self.entered.set()
            await self.release.wait()
        if request.offset == self.fail_offset:
            raise self.failure or ChatAdminRequiredError(request)
        offset = 0 if self.repeat else request.offset
        users = self.users[offset : offset + request.limit]
        return SimpleNamespace(participants=[ChannelParticipant(user.id, NOW) for user in users], users=users)


class ParsingDataTests(unittest.TestCase):
    def test_sources_normalize_links_ids_and_deduplicate(self):
        self.assertEqual(
            parse_sources("@GROUP_ONE\nhttps://t.me/group_one\n\n-100101\n-100101"), ["group_one", -100101]
        )

    def test_sources_reject_invites_users_bad_ids_empty_and_excess(self):
        for text in (
            "",
            "https://t.me/+private",
            "t.me/joinchat/private",
            "https://evil.org/group_one",
            "12345",
            "-0",
            str(-(2**64)),
            "\n".join(f"group_{i}" for i in range(11)),
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_sources(text)

    def test_profile_only_contains_visible_needed_fields(self):
        user = User(
            id=99,
            username="ALICE",
            first_name="Алиса",
            phone="private-phone",
            access_hash=1234,
            premium=True,
            photo=UserProfilePhoto(photo_id=4, dc_id=2),
        )
        result = member_row(user, ChatParticipantCreator(99), -45)
        self.assertEqual(
            (result["username"], result["role"], result["is_premium"], result["has_photo"]),
            ("alice", "creator", True, True),
        )
        self.assertNotIn("phone", result)
        self.assertNotIn("access_hash", result)
        self.assertIsNone(member_row(User(id=1, is_self=True), None, -45))
        self.assertIsNone(member_row(None, None, -45))

    def test_approximate_status_is_not_converted_to_timestamp(self):
        for status, label in (
            (UserStatusOnline(NOW), "online"),
            (UserStatusRecently(), "recently"),
            (UserStatusLastWeek(), "week"),
            (UserStatusLastMonth(), "month"),
        ):
            with self.subTest(status=label):
                result = member_row(User(id=1, status=status), None, -1)
                self.assertEqual(result["activity"], label)
                self.assertIsNone(result["last_seen"])
        result = member_row(User(id=1, status=UserStatusOffline(NOW.replace(tzinfo=None))), None, -1)
        self.assertEqual(datetime.fromisoformat(result["last_seen"]), NOW)

    def test_default_filters_and_optional_visibility(self):
        rows = [row(1), row(2, username=""), row(3, is_bot=True), row(4, is_deleted=True)]
        self.assertEqual([item["user_id"] for item in filter_members(rows)], [1])
        self.assertEqual(len(filter_members(rows, only_username=False, exclude_bots=False, exclude_deleted=False)), 4)

    def test_filters_combine_source_roles_premium_photo_and_unicode_search(self):
        rows = [
            row(1, first_name="Анна", role="admin", is_premium=True, has_photo=True),
            row(2, source_id=-202, role="member"),
        ]
        self.assertEqual(
            [
                item["user_id"]
                for item in filter_members(
                    rows, query="АНН", role="admins", premium=True, photo=True, source_id=-100101
                )
            ],
            [1],
        )
        self.assertEqual([item["user_id"] for item in filter_members(rows, role="members")], [2])
        self.assertFalse(filter_members(rows, source_id=-999))

    def test_activity_filters_do_not_assume_unknown_or_approximate_exact_time(self):
        rows = [
            row(1, activity="unknown"),
            row(2, activity="recently"),
            row(3, activity="offline", last_seen=(NOW - timedelta(days=2)).isoformat()),
            row(4, activity="offline", last_seen=(NOW - timedelta(days=20)).isoformat()),
            row(5, activity="offline", last_seen=(NOW + timedelta(days=2)).isoformat()),
        ]
        self.assertEqual([item["user_id"] for item in filter_members(rows, activity="exact_week")], [3])
        self.assertEqual([item["user_id"] for item in filter_members(rows, activity="recent")], [2, 3])
        self.assertEqual([item["user_id"] for item in filter_members(rows, activity="unknown")], [1])

    def test_dedup_keeps_same_title_sources_roles_and_available_username(self):
        rows = [row(1, username="", role="admin"), row(1, source_id=-202, role="member")]
        people = unique_members(rows)
        self.assertEqual(len(people), 1)
        self.assertEqual(people[0]["username"], "person_1")
        self.assertEqual(people[0]["source_ids"], [-100101, -202])
        self.assertEqual(len(people[0]["sources"]), 2)
        self.assertEqual([item["role"] for item in people[0]["memberships"]], ["admin", "member"])
        self.assertEqual(sorted(group_members(rows + rows, "source").values()), [1, 1])
        self.assertEqual(group_members(rows, "role"), {"Администратор": 1, "Участник": 1})

    def test_exports_keep_unicode_provenance_and_formula_cells_safe(self):
        people = unique_members(
            [row(1, first_name="=HYPERLINK(1)", last_name="Иванов"), row(1, source_id=-202), row(2, username="")]
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for format_name in ("csv", "json", "txt"):
                write_parsed(root / format_name, people, format_name)
            self.assertEqual((root / "txt").read_text(encoding="utf-8"), "@person_1\n")
            data = json.loads((root / "json").read_text(encoding="utf-8"))
            self.assertEqual(len(data), 2)
            self.assertEqual(len(data[0]["memberships"]), 2)
            with (root / "csv").open(encoding="utf-8-sig", newline="") as handle:
                data = list(csv.DictReader(handle))
            self.assertEqual(data[0]["first_name"], "'=HYPERLINK(1)")
            self.assertEqual(data[0]["last_name"], "Иванов")
            self.assertIn("-202", data[0]["source_ids"])

    def test_export_failure_preserves_previous_file_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "result.csv"
            path.write_text("previous", encoding="utf-8")
            with patch("core.export.os.replace", side_effect=OSError("full disk")), self.assertRaises(OSError):
                write_parsed(path, unique_members([row()]), "csv")
            self.assertEqual(path.read_text(), "previous")
            self.assertEqual(list(Path(folder).iterdir()), [path])


class ParsingDatabaseTests(DatabaseCase):
    async def test_saved_results_are_global_idempotent_and_survive_restart(self):
        run_id = await self.db.create_parse_run(1, ["group_one", "group_two"])
        await self.db.save_parse_source(run_id, "group_one", {"source_id": -101, "title": "Один"}, "running", "")
        await self.db.save_parse_source(run_id, "group_two", {"source_id": -202, "title": "Два"}, "completed", "")
        rows = [row(1, source_id=-101), row(1, source_id=-202), row(2, source_id=-101)]
        await self.db.save_parsed_members(run_id, rows + rows)
        await init_db()
        other_id = await self.db.create_campaign("Другая")
        runs = await self.db.for_campaign(other_id).get_parse_runs()
        self.assertEqual((runs[0]["people_count"], runs[0]["status"]), (2, "interrupted"))
        saved, sources = await self.db.get_parse_results(run_id)
        self.assertEqual(len(saved), 3)
        self.assertEqual(sources[0]["status"], "partial")
        self.assertEqual({item["source_title"] for item in saved}, {"Один", "Два"})

    async def test_delete_running_rejected_and_finished_deletes_only_parser(self):
        await self.db.bulk_insert_targets(["alice"])
        run_id = await self.db.create_parse_run(1, ["group_one"])
        await self.db.save_parsed_members(run_id, [row()])
        with self.assertRaises(ValueError):
            await self.db.delete_parse_run(run_id)
        await self.db.finish_parse_run(run_id, "stopped", "Остановлено")
        await self.db.delete_parse_run(run_id)
        self.assertEqual(await self.db.get_parse_results(run_id), ([], []))
        self.assertEqual((await self.db.get_targets_stats())["total"], 1)

    async def test_source_error_update_preserves_title_and_received_count(self):
        run_id = await self.db.create_parse_run(1, ["group_one"])
        await self.db.save_parse_source(run_id, "group_one", {"source_id": -101, "title": "Имя"}, "running", "", 200)
        await self.db.save_parse_source(run_id, "group_one", {"source_id": -101}, "failed", "Нет доступа")
        _, sources = await self.db.get_parse_results(run_id)
        self.assertEqual((sources[0]["title"], sources[0]["fetched"]), ("Имя", 200))


class ParsingEngineTests(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.account = await self.add_account()
        self.engine = ParserEngine(self.db)
        for name in ("PAGE_DELAY", "SEARCH_DELAY"):
            patcher = patch(f"core.parser.{name}", 0)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def collect(self, client, sources=None, limit=500):
        manager = AsyncMock()
        manager.client = client
        with patch("core.parser.AccountManager", return_value=manager):
            result = await self.engine.start(self.account, sources or ["group_one"], limit)
        manager.disconnect.assert_awaited_once()
        return result, await self.db.get_parse_results(result["run_id"])

    async def test_public_group_access_without_joining_is_allowed_when_api_allows(self):
        client = FakeClient(channel(left=True))
        result, (rows, sources) = await self.collect(client)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(rows), 2)
        self.assertEqual(sources[0]["membership"], "not_joined")
        self.assertTrue(
            all(isinstance(item, (GetFullChannelRequest, GetParticipantsRequest)) for item in client.requests)
        )

    async def test_private_group_requires_membership_before_list_requests(self):
        entity = channel(left=True)
        entity.username = None
        client = FakeClient(entity)
        result, (rows, sources) = await self.collect(client, [-100101])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(rows or client.requests)
        self.assertIn("вступления", sources[0]["detail"])

    async def test_hidden_members_rejected_for_member_allowed_for_creator(self):
        client = FakeClient(hidden=True)
        result, (rows, _) = await self.collect(client)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(rows)
        self.assertFalse(any(isinstance(item, GetParticipantsRequest) for item in client.requests))
        result, (rows, _) = await self.collect(FakeClient(channel(creator=True), hidden=True))
        self.assertEqual(len(rows), 2)

    async def test_broadcast_subscription_does_not_grant_participant_access(self):
        result, (rows, sources) = await self.collect(FakeClient(channel(broadcast=True), allowed=False))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(rows)
        self.assertIn("администратора", sources[0]["detail"])

    async def test_basic_group_uses_available_full_chat_members(self):
        entity = Chat(45, "Обычная", ChatPhotoEmpty(), 2, NOW, 1)
        client = FakeClient(entity)
        result, (rows, sources) = await self.collect(client)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(sources[0]["source_id"], -45)
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(client.requests), 1)

    async def test_basic_forbidden_is_not_reported_as_empty_success(self):
        client = FakeClient(Chat(45, "Обычная", ChatPhotoEmpty(), 0, NOW, 1))
        with patch.object(
            FakeClient,
            "__call__",
            new=AsyncMock(
                return_value=SimpleNamespace(full_chat=SimpleNamespace(participants=ChatParticipantsForbidden(45)))
            ),
        ):
            result, (rows, sources) = await self.collect(client)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(rows)
        self.assertIn("не открыл", sources[0]["detail"])

    async def test_limit_pagination_and_same_chat_alias_do_not_duplicate(self):
        client = FakeClient(users=[User(id=i, username=f"person_{i}") for i in range(1, 251)])
        result, (rows, sources) = await self.collect(client, ["group_one", -1000000000101], 220)
        self.assertEqual((result["status"], len(rows), len(sources)), ("partial", 220, 1))
        self.assertEqual(
            [(item.offset, item.limit) for item in client.requests if isinstance(item, GetParticipantsRequest)],
            [(0, 200), (200, 20)],
        )

    async def test_repeating_page_stops_and_does_not_inflate_records(self):
        client = FakeClient(users=[User(id=i, username=f"person_{i}") for i in range(1, 201)], count=1000, repeat=True)
        result, (rows, _) = await self.collect(client)
        self.assertEqual((result["status"], len(rows)), ("partial", 200))
        recent = [
            item
            for item in client.requests
            if isinstance(item, GetParticipantsRequest) and isinstance(item.filter, ChannelParticipantsRecent)
        ]
        self.assertEqual(len(recent), 2)

    async def test_source_error_allows_other_sources_and_records_partial(self):
        result, (rows, sources) = await self.collect(FakeClient(), ["missing_group", "group_one"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(rows), 2)
        self.assertEqual([item["status"] for item in sources], ["failed", "completed"])

    async def test_flood_wait_stops_all_sources_and_saves_required_pause(self):
        client = FakeClient(fail_offset=0, failure=FloodWaitError(None, 125))
        result, (rows, sources) = await self.collect(client, ["group_one", "group_two"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(client.references, ["group_one"])
        self.assertFalse(rows)
        self.assertIn("125", sources[0]["detail"])
        self.assertIsNotNone((await self.db.get_account(self.account["id"]))["flood_until"])

    async def test_stop_preserves_committed_page_and_finalizes_source(self):
        client = FakeClient(users=[User(id=i, username=f"person_{i}") for i in range(1, 251)], block_offset=200)
        manager = AsyncMock(client=client)
        with patch("core.parser.AccountManager", return_value=manager):
            task = asyncio.create_task(self.engine.start(self.account, ["group_one"], 500))
            await asyncio.wait_for(client.entered.wait(), 5)
            self.engine.stop()
            result = await asyncio.wait_for(task, 5)
        rows, sources = await self.db.get_parse_results(result["run_id"])
        self.assertEqual((result["status"], len(rows), sources[0]["status"]), ("stopped", 200, "stopped"))
        self.assertIsNone(self.engine._task)
        manager.disconnect.assert_awaited_once()

    async def test_check_does_not_create_run_or_collect_large_list(self):
        client = FakeClient()
        manager = AsyncMock(client=client)
        with patch("core.parser.AccountManager", return_value=manager):
            info = await self.engine.check(self.account, "group_one")
        self.assertEqual(info["title"], "Группа 101")
        self.assertEqual(await self.db.get_parse_runs(), [])
        self.assertEqual(client.requests[-1].limit, 1)
        manager.disconnect.assert_awaited_once()

    async def test_later_page_error_keeps_received_rows_and_reports_partial(self):
        client = FakeClient(users=[User(id=i, username=f"person_{i}") for i in range(1, 251)], fail_offset=200)
        result, (rows, sources) = await self.collect(client)
        self.assertEqual((result["status"], len(rows), sources[0]["fetched"]), ("partial", 200, 200))

    async def test_account_revocation_stops_remaining_sources(self):
        client = FakeClient(fail_offset=0, failure=AuthKeyUnregisteredError(None))
        result, _ = await self.collect(client, ["group_one", "group_two"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(client.references, ["group_one"])

    async def test_global_budget_bounds_sources(self):
        client = FakeClient(users=[User(id=i, username=f"person_{i}") for i in range(1, 9)])
        with patch("core.parser.MAX_RUN_RECORDS", 3):
            result, (rows, _) = await self.collect(client, ["group_one", "group_two"])
        self.assertEqual((result["status"], len(rows)), ("partial", 3))
        self.assertEqual(client.references, ["group_one"])

    async def test_budget_also_counts_saved_pages_of_failed_sources(self):
        client = FakeClient(users=[User(id=i, username=f"person_{i}") for i in range(1, 251)], fail_offset=200)
        with (
            patch("core.parser.MAX_RUN_RECORDS", 205),
            patch.object(client, "get_entity", new=AsyncMock(side_effect=[channel(101), channel(102)])),
        ):
            result, (rows, sources) = await self.collect(client, ["group_one", "group_two"])
        self.assertEqual((result["status"], len(rows)), ("partial", 205))
        self.assertEqual([item["fetched"] for item in sources], [200, 5])
        self.assertEqual(
            [item.limit for item in client.requests if isinstance(item, GetParticipantsRequest)], [200, 5, 5]
        )

    async def test_invalid_limits_do_not_create_run_or_connect(self):
        for limit in (0, 5001):
            with self.assertRaises(ValueError):
                await self.engine.start(self.account, ["group_one"], limit)
        self.assertEqual(await self.db.get_parse_runs(), [])
