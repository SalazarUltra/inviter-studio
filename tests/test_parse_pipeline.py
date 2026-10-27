"""Parse -> audience -> invite pipeline: identity carry, ordering, exclusions, ledger, resume, large groups."""

import asyncio
import sqlite3
from contextlib import closing
from unittest.mock import AsyncMock, patch

from telethon.errors import ChatAdminRequiredError
from telethon.tl.functions.channels import GetParticipantsRequest
from telethon.tl.types import User

from core.database import chat_key, init_db, order_audience, user_keys
from core.inviter import InviterEngine
from core.parser import SEARCH_QUERIES, ParserEngine, audience_members, pending_sources, unique_members
from tests.support import DatabaseCase
from tests.test_parsing import FakeClient, row


class ParserCase(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.account = await self.add_account()
        self.engine = ParserEngine(self.db)
        for name in ("PAGE_DELAY", "SEARCH_DELAY"):
            patcher = patch(f"core.parser.{name}", 0)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def run_engine(self, client, call):
        manager = AsyncMock()
        manager.client = client
        with patch("core.parser.AccountManager", return_value=manager):
            return await call(self.engine)


class LargeGroupSearchTests(ParserCase):
    async def test_search_passes_fill_the_gap_when_recent_list_stalls(self):
        users = [User(id=i, username=f"person_{i}") for i in range(1, 301)]
        client = FakeClient(users=users, count=300, recent_cap=50)
        result = await self.run_engine(client, lambda engine: engine.start(self.account, ["group_one"], 500))
        rows, sources = await self.db.get_parse_results(result["run_id"])
        self.assertEqual(
            (result["status"], len(rows), len({item["user_id"] for item in rows})), ("completed", 300, 300)
        )
        self.assertIn("e", client.searches)
        self.assertEqual(sources[0]["status"], "completed")
        self.assertGreater(sources[0]["search_pos"], 0)

    async def test_no_search_when_recent_list_reaches_the_advertised_count(self):
        client = FakeClient(users=[User(id=i, username=f"person_{i}") for i in range(1, 41)])
        await self.run_engine(client, lambda engine: engine.start(self.account, ["group_one"], 500))
        self.assertEqual(client.searches, [])

    async def test_no_search_when_limit_already_reached(self):
        client = FakeClient(
            users=[User(id=i, username=f"person_{i}") for i in range(1, 301)], count=300, recent_cap=300
        )
        result = await self.run_engine(client, lambda engine: engine.start(self.account, ["group_one"], 100))
        rows, _ = await self.db.get_parse_results(result["run_id"])
        self.assertEqual((len(rows), client.searches), (100, []))

    async def test_search_alphabet_covers_latin_cyrillic_and_digits(self):
        self.assertTrue(set("az09яа") <= set(SEARCH_QUERIES))
        self.assertEqual(len(SEARCH_QUERIES), len(set(SEARCH_QUERIES)))

    async def test_search_requests_are_flood_safe_paced(self):
        users = [User(id=i, username=f"person_{i}") for i in range(1, 31)]
        client = FakeClient(users=users, count=30, recent_cap=5)
        with patch("core.parser.asyncio.sleep", new=AsyncMock()) as pause, patch("core.parser.SEARCH_DELAY", 1.5):
            await self.run_engine(client, lambda engine: engine.start(self.account, ["group_one"], 500))
        self.assertIn(1.5, [call.args[0] for call in pause.await_args_list])


class ResumeTests(ParserCase):
    async def test_resume_continues_partial_source_without_duplicates(self):
        users = [User(id=i, username=f"person_{i}") for i in range(1, 251)]
        first = await self.run_engine(
            FakeClient(users=users, fail_offset=200),
            lambda engine: engine.start(self.account, ["group_one"], 500),
        )
        run = await self.db.get_parse_run(first["run_id"])
        sources = await self.db.get_parse_sources(first["run_id"])
        self.assertEqual((first["status"], run["limit_per_source"]), ("partial", 500))
        self.assertEqual(pending_sources(run, sources), ["group_one"])

        client = FakeClient(users=users)
        second = await self.run_engine(client, lambda engine: engine.resume(self.account, first["run_id"]))
        rows, sources = await self.db.get_parse_results(first["run_id"])
        self.assertEqual((second["run_id"], second["status"]), (first["run_id"], "completed"))
        self.assertEqual((len(rows), len({item["user_id"] for item in rows})), (250, 250))
        self.assertEqual(sources[0]["status"], "completed")
        offsets = [item.offset for item in client.requests if isinstance(item, GetParticipantsRequest)]
        self.assertEqual(offsets[0], 200)  # already saved people are not requested again
        self.assertEqual(
            pending_sources(
                await self.db.get_parse_run(first["run_id"]), await self.db.get_parse_sources(first["run_id"])
            ),
            [],
        )
        self.assertEqual((await self.db.get_parse_run(first["run_id"]))["status"], "completed")

    async def test_resume_only_requeues_incomplete_sources(self):
        run_id = await self.db.create_parse_run(self.account["id"], ["one", "two", "three"], limit_per_source=100)
        await self.db.save_parse_source(run_id, "one", {"source_id": -1, "title": "1"}, "completed", "", 5)
        await self.db.save_parse_source(run_id, "two", {"source_id": -2, "title": "2"}, "failed", "x", 3)
        run = await self.db.get_parse_run(run_id)
        sources = await self.db.get_parse_sources(run_id)
        self.assertEqual(pending_sources(run, sources), ["two", "three"])
        await self.db.save_parse_source(run_id, "two", {"source_id": -2}, "partial", "", 100)  # limit reached
        self.assertEqual(pending_sources(run, await self.db.get_parse_sources(run_id)), ["three"])

    async def test_resume_rejects_running_authors_and_finished_runs(self):
        run_id = await self.db.create_parse_run(self.account["id"], ["one"])
        with self.assertRaises(ValueError):
            await self.db.reopen_parse_run(run_id)
        await self.db.finish_parse_run(run_id, "interrupted", "x")
        await self.db.save_parse_source(run_id, "one", {"source_id": -1}, "completed", "", 1)
        with self.assertRaises(ValueError):
            await ParserEngine(self.db).resume(self.account, run_id)
        authors = await self.db.create_parse_run(self.account["id"], ["one"], mode="authors")
        await self.db.finish_parse_run(authors, "partial", "x")
        with self.assertRaises(ValueError):
            await ParserEngine(self.db).resume(self.account, authors)

    async def test_interrupted_run_is_resumable_after_restart(self):
        users = [User(id=i, username=f"person_{i}") for i in range(1, 6)]
        run_id = await self.db.create_parse_run(self.account["id"], ["group_one"], limit_per_source=500)
        await self.db.save_parse_source(
            run_id, "group_one", {"source_id": -1000000000101, "title": "G"}, "running", "", 2
        )
        await self.db.save_parsed_members(run_id, [row(1, source_id=-1000000000101), row(2, source_id=-1000000000101)])
        await init_db()  # simulated restart marks the run interrupted
        self.assertEqual((await self.db.get_parse_run(run_id))["status"], "interrupted")
        result = await self.run_engine(FakeClient(users=users), lambda engine: engine.resume(self.account, run_id))
        rows, _ = await self.db.get_parse_results(run_id)
        self.assertEqual((result["status"], len(rows)), ("completed", 5))

    async def test_resume_failure_keeps_run_resumable(self):
        run_id = await self.db.create_parse_run(self.account["id"], ["group_one"], limit_per_source=500)
        await self.db.finish_parse_run(run_id, "failed", "x")
        client = FakeClient(fail_offset=0, failure=ChatAdminRequiredError(None))
        result = await self.run_engine(client, lambda engine: engine.resume(self.account, run_id))
        self.assertNotEqual(result["status"], "completed")
        run = await self.db.get_parse_run(run_id)
        self.assertEqual(pending_sources(run, await self.db.get_parse_sources(run_id)), ["group_one"])


class ChatMembersTests(ParserCase):
    async def test_collect_member_keys_reads_without_storing(self):
        users = [User(id=i, username=f"person_{i}" if i % 2 else None) for i in range(1, 11)]
        client = FakeClient(users=users)
        found = await self.run_engine(client, lambda engine: engine.collect_member_keys(self.account, "group_one"))
        self.assertEqual(found["user_ids"], set(range(1, 11)))
        self.assertEqual(found["usernames"], {f"person_{i}" for i in (1, 3, 5, 7, 9)})
        self.assertFalse(found["partial"])
        self.assertEqual(await self.db.get_parse_runs(), [])

    async def test_collect_member_keys_accepts_numeric_chat_id(self):
        client = FakeClient()
        await self.run_engine(client, lambda engine: engine.collect_member_keys(self.account, "-100101"))
        self.assertEqual(client.references, [-100101])


class AudienceImportTests(DatabaseCase):
    def members(self, count=5):
        return [
            {
                "username": f"person_{i}",
                "user_id": 1000 + i,
                "activity": "recently",
                "source": "Группа",
                "parse_seq": 100 - i,
            }
            for i in range(1, count + 1)
        ]

    async def test_identity_is_stored_and_username_only_import_still_works(self):
        result = await self.db.import_audience(self.members(2))
        await self.db.bulk_insert_targets(["plain_name"])
        self.assertEqual(result["inserted"], 2)
        targets = {item["username"]: item for item in await self.db.get_targets()}
        self.assertEqual(
            (targets["person_1"]["user_id"], targets["person_1"]["activity"], targets["person_1"]["source"]),
            (1001, "recently", "Группа"),
        )
        self.assertIsNone(targets["plain_name"]["user_id"])

    async def test_existing_username_gains_user_id(self):
        await self.db.bulk_insert_targets(["person_1"])
        result = await self.db.import_audience(self.members(1))
        self.assertEqual((result["inserted"], result["duplicates"]), (0, 1))
        self.assertEqual((await self.db.get_targets())[0]["user_id"], 1001)

    async def test_imported_order_is_preserved_and_follows_existing_queue(self):
        await self.db.bulk_insert_targets(["early"], preserve_order=True)
        names = ["zeta", "alpha", "mike"]
        await self.db.import_audience([{"username": name, "user_id": 5} for name in names], preserve_order=True)
        self.assertEqual([item["username"] for item in await self.db.get_pending_list()], ["early", *names])

    async def test_order_helpers(self):
        people = [
            {"username": "a", "activity": "month", "parse_seq": 1},
            {"username": "b", "activity": "online", "parse_seq": 3},
            {"username": "c", "activity": "offline", "last_seen": "2026-10-01T00:00:00+00:00", "parse_seq": 2},
            {"username": "d", "activity": "offline", "last_seen": "2026-10-20T00:00:00+00:00", "parse_seq": 4},
        ]
        self.assertEqual([p["username"] for p in order_audience(people, "imported")], ["a", "c", "b", "d"])
        self.assertEqual([p["username"] for p in order_audience(people, "activity")], ["b", "d", "c", "a"])
        self.assertEqual(sorted(p["username"] for p in order_audience(people, "shuffle")), ["a", "b", "c", "d"])
        self.assertEqual([p["username"] for p in people], ["a", "b", "c", "d"])

    async def test_exclude_chat_members_by_id_and_username(self):
        members = self.members(4)
        result = await self.db.import_audience(members, exclude_user_ids={1001}, exclude_usernames={"PERSON_2", "@x"})
        self.assertEqual((result["inserted"], result["excluded"]), (2, 2))
        self.assertEqual({item["username"] for item in await self.db.get_targets()}, {"person_3", "person_4"})

    async def test_ledger_marks_invited_people_for_the_same_chat_only(self):
        await self.db.import_audience(self.members(3))
        target = await self.db.claim_next_pending_target()
        await self.db.record_success(target["id"], 1, chat="https://t.me/MyChat")
        self.assertEqual(await self.db.count_invited("@mychat"), 1)
        self.assertEqual(await self.db.get_invited_keys("mychat"), {"id:1001", "u:person_1"})
        other = await self.db.create_campaign("Другая")
        scoped = self.db.for_campaign(other)
        skipped = await scoped.import_audience(self.members(3), chat="mychat", skip_invited=True)
        self.assertEqual((skipped["inserted"], skipped["skipped_invited"]), (2, 1))
        free = await scoped.import_audience(
            [{"username": "person_1", "user_id": 1001}], chat="otherchat", skip_invited=True
        )
        self.assertEqual((free["inserted"], free["skipped_invited"]), (1, 0))  # another chat: not invited there
        disabled = await self.db.for_campaign(await self.db.create_campaign("Третья")).import_audience(
            self.members(3), chat="mychat", skip_invited=False
        )
        self.assertEqual((disabled["inserted"], disabled["skipped_invited"]), (3, 0))

    async def test_ledger_matches_username_only_targets_and_survives_name_only_imports(self):
        await self.db.bulk_insert_targets(["alice"])
        target = await self.db.claim_next_pending_target()
        await self.db.record_success(target["id"], 1, chat="mychat")
        other = self.db.for_campaign(await self.db.create_campaign("Другая"))
        result = await other.import_audience([{"username": "Alice", "user_id": 77}], chat="mychat", skip_invited=True)
        self.assertEqual(result["skipped_invited"], 1)

    async def test_record_success_without_chat_writes_no_ledger(self):
        await self.db.bulk_insert_targets(["alice"])
        target = await self.db.claim_next_pending_target()
        await self.db.record_success(target["id"], 1)
        with closing(sqlite3.connect(self.root / "inviter.db")) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM invited_ledger").fetchone()[0], 0)

    async def test_running_campaign_rejects_import(self):
        await self.db.set_setting("task_status", "running")
        with self.assertRaises(RuntimeError):
            await self.db.import_audience(self.members(1))

    async def test_invalid_usernames_in_dict_import_raise_like_plain_import(self):
        with self.assertRaises(ValueError):
            await self.db.import_audience([{"username": "../bad", "user_id": 1}])

    async def test_legacy_targets_table_is_rebuilt_with_identity_columns(self):
        with closing(sqlite3.connect(self.root / "legacy.db")) as connection:
            connection.executescript(
                "CREATE TABLE targets(id INTEGER PRIMARY KEY, username TEXT UNIQUE, status TEXT DEFAULT 'pending', "
                "added_by_account_id INTEGER, error_message TEXT, processed_at TEXT, created_at TEXT, sort_order INTEGER DEFAULT 0);"
                "INSERT INTO targets(username,created_at) VALUES('old_user','2026-01-01');"
            )
            connection.commit()
        with patch("core.database.DB_PATH", str(self.root / "legacy.db")):
            await init_db()
            targets = await self.db.get_targets()
            await self.db.import_audience(self.members(1))
            after = await self.db.get_targets()
        self.assertEqual([item["username"] for item in targets], ["old_user"])
        self.assertEqual({item["username"]: item["user_id"] for item in after}, {"old_user": None, "person_1": 1001})

    def test_keys_and_audience_members_helpers(self):
        self.assertEqual(chat_key("https://t.me/MyChat?x=1"), "mychat")
        self.assertEqual(chat_key("-1001234567890"), chat_key("1234567890"))
        self.assertEqual(chat_key(""), "")
        self.assertEqual(user_keys("@Bob", 5), ["id:5", "u:bob"])
        people = unique_members(
            [{**row(1), "parse_seq": 9}, {**row(1, source_id=-2), "parse_seq": 4}, row(2, username="")]
        )
        members, skipped = audience_members(people)
        self.assertEqual(
            [(item["username"], item["user_id"], item["parse_seq"]) for item in members], [("person_1", 1, 4)]
        )
        self.assertEqual(skipped["no_username"], 1)


class InviterPipelineTests(DatabaseCase):
    def engine(self):
        self.reports = []
        return InviterEngine(self.db, lambda *args: None, complete_callback=self.reports.append)

    async def run_engine(self, engine, account, manager):
        manager.account = account
        manager.check_admin_rights.return_value = {"ok": True}
        engine._wait = AsyncMock()
        engine._schedule_account_next = lambda *args, **kwargs: 0  # no per-account interval in unit tests
        with patch("core.inviter.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "mychat", 10, 10), 10)

    async def test_user_id_is_passed_to_account_manager_and_ledger_is_written(self):
        account = await self.add_account()
        await self.db.import_audience([{"username": "alice", "user_id": 42}, {"username": "bobby"}])
        manager = AsyncMock()
        manager.add_user_to_chat.return_value = {"success": True}
        await self.run_engine(self.engine(), account, manager)
        calls = {call.args[1]: call.kwargs for call in manager.add_user_to_chat.await_args_list}
        self.assertEqual(calls, {"alice": {"user_id": 42}, "bobby": {}})
        self.assertEqual(await self.db.get_invited_keys("mychat"), {"id:42", "u:alice", "u:bobby"})

    async def test_as_is_order_is_kept_and_default_still_shuffles(self):
        account = await self.add_account()
        names = [f"person_{i:02d}" for i in range(15, 0, -1)]
        await self.db.import_audience([{"username": name, "user_id": 1} for name in names], preserve_order=True)
        await self.db.set_setting("invite_order", "as_is")
        await self.db.set_setting("protection_consecutive", "1000")
        manager = AsyncMock()
        manager.add_user_to_chat.return_value = {"success": True}
        await self.run_engine(self.engine(), account, manager)
        self.assertEqual([call.args[1] for call in manager.add_user_to_chat.await_args_list], names)

        other = self.db.for_campaign(await self.db.create_campaign("Случайная"))
        await other.set_setting("protection_consecutive", "1000")
        await other.import_audience([{"username": name} for name in names], preserve_order=True)
        engine = InviterEngine(other, lambda *args: None)
        manager = AsyncMock()
        manager.add_user_to_chat.return_value = {"success": True}
        await self.run_engine(engine, account, manager)
        self.assertNotEqual([call.args[1] for call in manager.add_user_to_chat.await_args_list], names)

    async def test_invalid_invite_order_is_rejected(self):
        with self.assertRaises(ValueError):
            await self.db.save_campaign_settings({"invite_order": "random"})
        await self.db.save_campaign_settings({"invite_order": "as_is"})
        self.assertEqual(await self.db.get_setting("invite_order"), "as_is")


class AccountManagerResolveTests(DatabaseCase):
    async def test_user_id_resolved_first_then_falls_back_to_username(self):
        from core.account_manager import AccountManager

        account = await self.add_account()
        manager = AccountManager(account, self.db)
        manager.client = AsyncMock()
        user = User(id=42, username="alice")
        manager.client.get_entity.side_effect = [user, ValueError("no")]
        with patch.object(manager, "get_chat_entity", new=AsyncMock(side_effect=ValueError("stop")), create=True):
            result = await manager.add_user_to_chat("group", "alice", user_id=42)
        self.assertEqual(result["error"], "need_admin")  # reached chat resolution after resolving by id
        self.assertEqual(manager.client.get_entity.await_args_list[0].args[0].user_id, 42)

        manager.client.get_entity.reset_mock()
        manager.client.get_entity.side_effect = [ValueError("not cached"), user]
        with patch.object(manager, "get_chat_entity", new=AsyncMock(side_effect=ValueError("stop")), create=True):
            result = await manager.add_user_to_chat("group", "alice", user_id=42)
        self.assertEqual(result["error"], "need_admin")
        self.assertEqual(manager.client.get_entity.await_args_list[1].args[0], "alice")
