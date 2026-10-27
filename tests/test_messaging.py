import asyncio
import sqlite3
from contextlib import closing
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    PeerFloodError,
    RPCError,
    UsernameNotOccupiedError,
    UserPrivacyRestrictedError,
)
from telethon.tl.functions.messages import SendMessageRequest
from telethon.tl.types import User

from core.account_manager import AccountManager
from core.database import init_db
from core.diagnostics import preflight
from core.messenger import MessageEngine
from core.validation import validate_message
from tests.support import DatabaseCase, account_payload


class MessagingTests(DatabaseCase):
    async def setup_campaign(self, names):
        campaign_id = await self.db.create_campaign("Сообщения", "messages")
        self.db = self.db.for_campaign(campaign_id)
        await self.db.bulk_insert_targets(names)
        self.events = []
        self.reports = []
        self.engine = MessageEngine(
            self.db, lambda *args: self.events.append(args), complete_callback=self.reports.append
        )
        self.engine._wait = AsyncMock()
        return self.engine

    def manager(self, account, mapping=None):
        manager = AsyncMock()
        manager.account = account
        mapping = mapping or {}
        manager.resolve_recipient.side_effect = lambda name: User(
            id=mapping.get(name, 10000 + sum(ord(c) for c in name))
        )
        manager.send_direct_message.return_value = {"success": True, "message_id": 7}
        return manager

    async def test_preflight_ignores_unavailable_account_when_another_can_send(self):
        await self.setup_campaign(["alice"])
        restricted, active, disabled = await self.add_account(1), await self.add_account(2), await self.add_account(3)
        await self.db.update_account_status(restricted["id"], "restricted")
        await self.db.toggle_account_enabled(disabled["id"], False)

        selected_ids = [restricted["id"], active["id"], disabled["id"]]
        report = await preflight(self.db, selected_ids, "", mode="messages", message="Hello")

        self.assertTrue(report["ok"], report)
        restricted_check = next(row for row in report["checks"] if row["label"] == restricted["phone"])
        self.assertFalse(restricted_check["ok"])
        availability = next(row for row in report["checks"] if row["label"] == "Доступность отправки")
        self.assertTrue(availability["ok"])

        engine = MessageEngine(self.db, lambda *args: None)
        manager = self.manager(active)
        with patch("core.messenger.AccountManager", return_value=manager):
            await engine.start([restricted, active, disabled], "Hello", 10, 10)
        manager.send_direct_message.assert_awaited_once()
        self.assertEqual((await self.db.get_targets_stats())["sent"], 1)

    async def test_connection_failure_skips_account_and_sends_with_connected_account(self):
        engine = await self.setup_campaign(["alice"])
        first, second = await self.add_account(1), await self.add_account(2)
        failed_manager = AsyncMock()
        failed_manager.connect.side_effect = OSError("network unavailable")
        good_manager = self.manager(second)
        with patch("core.messenger.AccountManager", side_effect=[failed_manager, good_manager]):
            await asyncio.wait_for(engine.start([first, second], "Hello", 10, 10), 3)

        failed_manager.connect.assert_awaited_once()
        failed_manager.disconnect.assert_awaited_once()
        good_manager.send_direct_message.assert_awaited_once()
        self.assertEqual((await self.db.get_targets_stats())["sent"], 1)
        self.assertTrue(any(row[0] == "WARN" and first["phone"] in row[1] for row in self.events))
        self.assertTrue(any(row["action"] == "CONNECT_FAILED" for row in await self.db.get_logs()))

    async def test_all_connection_failures_keep_queue_restartable(self):
        engine = await self.setup_campaign(["alice"])
        account = await self.add_account()
        failed_manager = AsyncMock()
        failed_manager.connect.side_effect = OSError("network unavailable")
        with patch("core.messenger.AccountManager", return_value=failed_manager):
            await engine.start([account], "Hello", 10, 10)

        self.assertEqual((await self.db.get_targets())[0]["status"], "pending")
        self.assertEqual(self.reports[-1]["outcome"], "failed")
        self.assertFalse(engine.running)

        good_manager = self.manager(account)
        with patch("core.messenger.AccountManager", return_value=good_manager):
            await asyncio.wait_for(engine.start([account], "Hello", 10, 10), 3)
        good_manager.send_direct_message.assert_awaited_once()
        self.assertEqual((await self.db.get_targets_stats())["sent"], 1)

    async def test_exhausted_daily_quota_is_visible_while_task_waits(self):
        engine = await self.setup_campaign(["alice", "bobby"])
        account = await self.add_account()
        with closing(sqlite3.connect(self.root / "inviter.db")) as connection:
            connection.execute(
                "UPDATE accounts SET daily_limit=1,sent_today=1,last_message_reset_date=? WHERE id=?",
                (date.today().isoformat(), account["id"]),
            )
            connection.commit()
        statuses = []
        engine.status_callback = statuses.append

        async def stop_after_wait(_seconds):
            engine.stop()

        engine._wait = AsyncMock(side_effect=stop_after_wait)
        with patch("core.messenger.AccountManager", return_value=self.manager(account)):
            await asyncio.wait_for(engine.start([account], "Hello", 10, 10), 3)

        self.assertTrue(any("Дневной лимит первых сообщений исчерпан" in value for value in statuses))
        self.assertEqual((await self.db.get_targets_stats())["pending"], 2)
        self.assertEqual((await self.db.get_targets_stats())["sent"], 0)

    async def test_ten_accounts_share_queue_without_duplicates(self):
        names = [f"person_{i:04d}" for i in range(20)]
        engine = await self.setup_campaign(names)
        accounts = [await self.add_account(i + 1) for i in range(10)]
        assignments = []
        managers = {}
        mapping = {name: index + 1000 for index, name in enumerate(names)}

        def factory(account, db):
            manager = self.manager(account, mapping)

            async def send(user, text, random_id):
                assignments.append((account["id"], user.id, text, random_id))
                return {"success": True}

            manager.send_direct_message.side_effect = send
            managers[account["id"]] = manager
            return manager

        with patch("core.messenger.AccountManager", side_effect=factory):
            # Allow cold Windows SQLite/DPAPI I/O; this is a correctness test.
            # An accidental real scheduling wait (60+ seconds) still times out.
            await asyncio.wait_for(engine.start(accounts, "Тестовый текст\nВторая строка", 60, 90), 30)
        self.assertEqual(len(assignments), 20)
        self.assertEqual(len({row[1] for row in assignments}), 20)
        self.assertEqual(len({row[0] for row in assignments}), 10)
        self.assertEqual(len({row[3] for row in assignments}), 20)
        self.assertTrue(all(row[2] == "Тестовый текст\nВторая строка" for row in assignments))
        self.assertEqual(engine._wait.await_count, 19)
        self.assertTrue(all(60 <= call.args[0] <= 90 for call in engine._wait.await_args_list))
        self.assertEqual((await self.db.get_targets_stats())["sent"], 20)
        for account in accounts:
            self.assertEqual((await self.db.get_account(account["id"]))["sent_today"], 2)
            managers[account["id"]].disconnect.assert_awaited_once()

    async def test_same_telegram_id_under_two_usernames_sent_once(self):
        engine = await self.setup_campaign(["alice", "alias"])
        account = await self.add_account()
        manager = self.manager(account, {"alice": 123, "alias": 123})
        with patch("core.messenger.AccountManager", return_value=manager):
            await engine.start([account], "Hello", 10, 10)
        manager.send_direct_message.assert_awaited_once()
        stats = await self.db.get_targets_stats()
        self.assertEqual((stats["sent"], stats["skipped"]), (1, 1))

    async def test_unknown_username_is_logged_skipped_and_does_not_block_next_or_restart(self):
        engine = await self.setup_campaign(["missing_user", "alice"])
        account = await self.add_account()
        manager = self.manager(account)

        async def resolve(username):
            if username == "missing_user":
                raise UsernameNotOccupiedError(None)
            return User(id=123 if username == "alice" else 456)

        manager.resolve_recipient.side_effect = resolve
        with patch("core.messenger.AccountManager", return_value=manager):
            await engine.start([account], "Hello", 10, 10)
            self.assertEqual((await self.db.get_targets_stats())["sent"], 1)
            self.assertEqual((await self.db.get_targets_stats())["skipped"], 1)
            self.assertEqual((await self.db.get_account(account["id"]))["status"], "active")
            self.assertEqual(manager.send_direct_message.await_count, 1)
            # not_found paces the queue before the next claim
            self.assertEqual(engine._wait.await_count, 1)
            self.assertTrue(2 <= engine._wait.await_args_list[0].args[0] <= 5)
            logs = await self.db.get_logs()
            self.assertTrue(any(row["action"] == "MESSAGE_NOT_FOUND" for row in logs))
            await self.db.bulk_insert_targets(["bobby"])
            await engine.start([account], "Hello", 10, 10)
        self.assertEqual(manager.send_direct_message.await_count, 2)
        self.assertEqual((await self.db.get_targets_stats())["sent"], 2)

    async def test_transient_resolve_errors_requeue_target_and_continue(self):
        engine = await self.setup_campaign(["alice", "bobby"])
        account = await self.add_account()
        manager = self.manager(account)
        failures = {
            "alice": [asyncio.TimeoutError(), ConnectionError("reset")],
            "bobby": [OSError("net"), RPCError(None, "x", 500)],
        }

        async def resolve(username):
            if failures[username]:
                raise failures[username].pop(0)
            return User(id=123 if username == "alice" else 456)

        manager.resolve_recipient.side_effect = resolve
        with patch("core.messenger.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "Hello", 10, 10), 15)
        stats = await self.db.get_targets_stats()
        self.assertEqual((stats["sent"], stats["pending"]), (2, 0))
        self.assertEqual(self.reports[0]["outcome"], "completed")
        self.assertEqual(engine._wait.await_count, 5)  # 4 retry back-offs + 1 pause between recipients
        logs = await self.db.get_logs()
        self.assertEqual(sum(row["action"] == "RESOLVE_RETRY" for row in logs), 4)
        self.assertEqual((await self.db.get_account(account["id"]))["status"], "active")

    async def test_network_outage_across_targets_stops_safely_and_keeps_rest_pending(self):
        names = [f"person_{i}" for i in range(7)]
        engine = await self.setup_campaign(names)
        account = await self.add_account()
        manager = self.manager(account)
        manager.resolve_recipient.side_effect = asyncio.TimeoutError()
        with patch("core.messenger.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "Hello", 10, 10), 15)
        # Earlier targets exhaust their own retries (marked error); the global streak counts
        # distinct failing targets and stops the run at the MAX_TRANSIENT_FAILURES-th one.
        given_up = engine.MAX_TRANSIENT_FAILURES - 1
        self.assertEqual(
            manager.resolve_recipient.await_count, given_up * engine.MAX_RESOLVE_RETRIES + 1
        )
        manager.send_direct_message.assert_not_awaited()
        stats = await self.db.get_targets_stats()
        self.assertEqual(stats["pending"], 7 - given_up)
        self.assertEqual(self.reports[0]["outcome"], "failed")
        self.assertEqual((await self.db.get_account(account["id"]))["status"], "active")

    async def test_target_exhausting_resolve_retries_is_marked_error_and_queue_continues(self):
        engine = await self.setup_campaign(["alice", "bobby"])
        account = await self.add_account()
        manager = self.manager(account)

        async def resolve(username):
            if username == "alice":
                raise asyncio.TimeoutError()
            return User(id=456)

        manager.resolve_recipient.side_effect = resolve
        with patch("core.messenger.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "Hello", 10, 10), 15)
        rows = {row["username"]: row["status"] for row in await self.db.get_targets()}
        self.assertEqual(rows, {"alice": "error", "bobby": "sent"})
        alice_attempts = [
            call for call in manager.resolve_recipient.await_args_list if call.args[0] == "alice"
        ]
        self.assertEqual(len(alice_attempts), engine.MAX_RESOLVE_RETRIES)
        self.assertEqual(self.reports[0]["outcome"], "completed")
        self.assertEqual(self.reports[0]["errors"], 1)
        self.assertEqual(engine._resolve_retries, {})
        logs = await self.db.get_logs()
        self.assertEqual(sum(row["action"] == "RESOLVE_RETRY" for row in logs), engine.MAX_RESOLVE_RETRIES - 1)
        self.assertTrue(any(row["action"] == "RESOLVE_FAILED" for row in logs))
        # last wait before bobby is the 2-5 s pause after the give-up
        self.assertTrue(any(2 <= call.args[0] <= 5 for call in engine._wait.await_args_list))

    async def test_permanent_resolve_rpc_is_not_found_and_continues(self):
        engine = await self.setup_campaign(["ghost", "alice"])
        account = await self.add_account()
        manager = self.manager(account)

        async def resolve(username):
            if username == "ghost":
                raise RPCError(None, "PEER_ID_INVALID", 400)
            return User(id=123)

        manager.resolve_recipient.side_effect = resolve
        with patch("core.messenger.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "Hello", 10, 10), 3)
        rows = {row["username"]: row["status"] for row in await self.db.get_targets()}
        self.assertEqual(rows, {"ghost": "not_found", "alice": "sent"})
        self.assertEqual(manager.resolve_recipient.await_count, 2)
        self.assertTrue(2 <= engine._wait.await_args_list[0].args[0] <= 5)
        self.assertEqual(self.reports[0]["outcome"], "completed")

    async def test_global_journal_survives_clear_and_other_campaign(self):
        engine = await self.setup_campaign(["alice"])
        account = await self.add_account()
        manager = self.manager(account)
        with patch("core.messenger.AccountManager", return_value=manager):
            await engine.start([account], "Hello", 10, 10)
        await self.db.clear_targets()
        second = self.db.for_campaign(await self.db.create_campaign("Вторая", "messages"))
        await second.bulk_insert_targets(["alice"])
        self.assertEqual((await second.get_targets())[0]["status"], "already_sent")
        engine = MessageEngine(second, lambda *args: None)
        engine._wait = AsyncMock()
        with patch("core.messenger.AccountManager", return_value=manager):
            await engine.start([account], "Hello again", 10, 10)
        self.assertEqual(manager.send_direct_message.await_count, 1)
        self.assertEqual((await second.get_targets())[0]["status"], "already_sent")
        self.assertEqual(engine._wait.await_count, 1)

    async def test_parallel_reservations_unique_user_id(self):
        await self.setup_campaign(["alice", "alias"])
        accounts = [await self.add_account(i + 1) for i in range(2)]
        targets = [await self.db.claim_next_pending_target() for _ in range(2)]
        reservations = await asyncio.gather(
            *(self.db.reserve_delivery(t["id"], a["id"], 123) for t, a in zip(targets, accounts, strict=True))
        )
        self.assertEqual(sum(value is not None for value in reservations), 1)

    async def test_sent_commit_is_atomic_and_idempotent(self):
        await self.setup_campaign(["alice"])
        account = await self.add_account()
        target = await self.db.claim_next_pending_target()
        await self.db.reserve_delivery(target["id"], account["id"], 123)
        await self.db.finish_delivery(target["id"], "sent", 7)
        with self.assertRaises(RuntimeError):
            await self.db.finish_delivery(target["id"], "sent", 7)
        current = await self.db.get_account(account["id"])
        self.assertEqual((current["sent_today"], current["added_today"]), (1, 0))

    async def test_reused_target_id_cannot_overwrite_old_delivery(self):
        await self.setup_campaign(["alice"])
        account = await self.add_account()
        target = await self.db.claim_next_pending_target()
        await self.db.reserve_delivery(target["id"], account["id"], 123)
        await self.db.finish_delivery(target["id"], "sent", 7)
        await self.db.clear_targets()
        await self.db.bulk_insert_targets(["bobby"])
        target = await self.db.claim_next_pending_target()
        await self.db.reserve_delivery(target["id"], account["id"], 456)
        await self.db.finish_delivery(target["id"], "sent", 8)
        with closing(sqlite3.connect(self.root / "inviter.db")) as conn:
            self.assertEqual(
                dict(conn.execute("SELECT username,message_id FROM message_deliveries")), {"alice": 7, "bobby": 8}
            )

    async def test_pause_during_resolution_prevents_send(self):
        engine = await self.setup_campaign(["alice"])
        account = await self.add_account()
        manager = self.manager(account)
        reached = asyncio.Event()

        async def resolve(username):
            engine.pause()
            reached.set()
            return User(id=123)

        manager.resolve_recipient.side_effect = resolve
        with patch("core.messenger.AccountManager", return_value=manager):
            task = asyncio.create_task(engine.start([account], "Hello", 10, 10))
            await asyncio.wait_for(reached.wait(), 3)
            await asyncio.sleep(0.1)
            manager.send_direct_message.assert_not_called()
            self.assertEqual((await self.db.get_targets())[0]["status"], "pending")
            engine.stop()
            await asyncio.wait_for(task, 3)

    async def test_empty_send_response_is_not_success(self):
        manager = AccountManager(account_payload())
        manager.client = AsyncMock(return_value=None)
        result = await manager.send_direct_message(User(id=123), "Hello", 321)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "uncertain")

    async def test_timeout_is_not_retried_and_stops_queue(self):
        engine = await self.setup_campaign(["alice", "bobby"])
        account = await self.add_account()
        manager = self.manager(account)
        manager.send_direct_message.return_value = {"success": False, "error": "uncertain"}
        with patch("core.messenger.AccountManager", return_value=manager):
            await engine.start([account], "Hello", 10, 10)
        manager.send_direct_message.assert_awaited_once()
        self.assertEqual(await self.db.delivery_exists("alice"), "uncertain")
        rows = {row["username"]: row["status"] for row in await self.db.get_targets()}
        self.assertEqual(rows, {"alice": "uncertain", "bobby": "pending"})
        await self.db.reset_failed_targets(["error"])
        self.assertEqual((await self.db.get_targets(status="uncertain"))[0]["username"], "alice")
        self.assertEqual(self.reports[0]["outcome"], "failed")
        self.assertTrue(any(row["action"] == "MESSAGE_UNCERTAIN" for row in await self.db.get_logs()))

    async def test_cancellation_during_send_requires_manual_review(self):
        engine = await self.setup_campaign(["alice"])
        account = await self.add_account()
        manager = self.manager(account)
        entered = asyncio.Event()

        async def send(*args):
            entered.set()
            await asyncio.Event().wait()

        manager.send_direct_message.side_effect = send
        with patch("core.messenger.AccountManager", return_value=manager):
            task = asyncio.create_task(engine.start([account], "Hello", 10, 10))
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual((await self.db.get_targets())[0]["status"], "uncertain")
        manager.disconnect.assert_awaited_once()

    async def test_restart_recovers_reserved_as_uncertain_not_pending(self):
        await self.setup_campaign(["alice"])
        account = await self.add_account()
        target = await self.db.claim_next_pending_target()
        await self.db.reserve_delivery(target["id"], account["id"], 123)
        await init_db()
        await init_db()
        self.assertEqual((await self.db.get_targets())[0]["status"], "uncertain")
        self.assertEqual(await self.db.delivery_exists("alice"), "uncertain")

    def per_account_managers(self, accounts, **overrides):
        managers = {account["id"]: self.manager(account) for account in accounts}
        for account_id, configure in overrides.items():
            configure(managers[account_id])
        return managers, (lambda account, db: managers[account["id"]])

    async def test_peer_flood_pauses_only_that_account_and_rotates(self):
        engine = await self.setup_campaign(["alice", "bobby"])
        accounts = [await self.add_account(i + 1) for i in range(2)]
        managers, factory = self.per_account_managers(accounts)
        managers[accounts[0]["id"]].send_direct_message.return_value = {"success": False, "error": "peer_flood"}
        with patch("core.messenger.AccountManager", side_effect=factory):
            await asyncio.wait_for(engine.start(accounts, "Hello", 10, 10), 5)
        managers[accounts[0]["id"]].send_direct_message.assert_awaited_once()
        self.assertEqual(managers[accounts[1]["id"]].send_direct_message.await_count, 2)
        flooded = await self.db.get_account(accounts[0]["id"])
        # Timed 24h cooldown like the inviter, not a permanent "restricted" status.
        self.assertEqual(flooded["status"], "flood")
        remaining = (datetime.fromisoformat(flooded["flood_until"]) - datetime.now()).total_seconds()
        self.assertGreater(remaining, 23 * 3600)
        self.assertEqual((await self.db.get_targets_stats())["sent"], 2)
        self.assertEqual(self.reports[0]["outcome"], "completed")
        self.assertTrue(
            any(row[0] == "WARN" and accounts[0]["phone"] in row[1] and "остальные" in row[1] for row in self.events)
        )
        self.assertTrue(any(row["action"] == "MESSAGE_ACCOUNT_PAUSED" for row in await self.db.get_logs()))

    async def test_peer_flood_on_last_usable_account_stops_and_keeps_targets_pending(self):
        engine = await self.setup_campaign(["alice", "bobby"])
        account = await self.add_account()
        manager = self.manager(account)
        manager.send_direct_message.return_value = {"success": False, "error": "peer_flood"}
        with patch("core.messenger.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "Hello", 10, 10), 5)
        manager.send_direct_message.assert_awaited_once()
        self.assertEqual((await self.db.get_account(account["id"]))["status"], "flood")
        self.assertEqual((await self.db.get_targets_stats())["pending"], 2)
        self.assertIsNone(await self.db.delivery_exists("alice"))
        self.assertEqual(self.reports[0]["outcome"], "failed")
        self.assertTrue(any(row[0] == "ERROR" and "длительной паузе" in row[1] for row in self.events))

    async def test_flood_wait_on_send_rotates_to_second_account(self):
        engine = await self.setup_campaign(["alice", "bobby"])
        accounts = [await self.add_account(i + 1) for i in range(2)]
        managers, factory = self.per_account_managers(accounts)
        managers[accounts[0]["id"]].send_direct_message.return_value = {
            "success": False,
            "error": "flood",
            "flood_seconds": 3600 * 3,
        }
        with patch("core.messenger.AccountManager", side_effect=factory):
            await asyncio.wait_for(engine.start(accounts, "Hello", 10, 10), 5)
        managers[accounts[0]["id"]].send_direct_message.assert_awaited_once()
        self.assertEqual(managers[accounts[1]["id"]].send_direct_message.await_count, 2)
        self.assertEqual((await self.db.get_account(accounts[0]["id"]))["status"], "flood")
        self.assertEqual((await self.db.get_targets_stats())["sent"], 2)
        self.assertEqual(self.reports[0]["outcome"], "completed")

    async def test_flood_wait_on_resolve_rotates_to_second_account(self):
        engine = await self.setup_campaign(["alice", "bobby"])
        accounts = [await self.add_account(i + 1) for i in range(2)]
        managers, factory = self.per_account_managers(accounts)
        managers[accounts[0]["id"]].resolve_recipient.side_effect = FloodWaitError(None, capture=3 * 3600)
        with patch("core.messenger.AccountManager", side_effect=factory):
            await asyncio.wait_for(engine.start(accounts, "Hello", 10, 10), 5)
        managers[accounts[0]["id"]].send_direct_message.assert_not_awaited()
        self.assertEqual(managers[accounts[1]["id"]].send_direct_message.await_count, 2)
        self.assertEqual((await self.db.get_account(accounts[0]["id"]))["status"], "flood")
        self.assertEqual((await self.db.get_targets_stats())["sent"], 2)
        self.assertEqual(self.reports[0]["outcome"], "completed")

    async def test_unauthorized_account_is_dropped_and_other_account_continues(self):
        engine = await self.setup_campaign(["alice", "bobby"])
        accounts = [await self.add_account(i + 1) for i in range(2)]
        managers, factory = self.per_account_managers(accounts)
        managers[accounts[0]["id"]].resolve_recipient.side_effect = AuthKeyUnregisteredError(None)
        with patch("core.messenger.AccountManager", side_effect=factory):
            await asyncio.wait_for(engine.start(accounts, "Hello", 10, 10), 5)
        self.assertEqual((await self.db.get_account(accounts[0]["id"]))["status"], "unauthorized")
        self.assertEqual(managers[accounts[1]["id"]].send_direct_message.await_count, 2)
        self.assertEqual((await self.db.get_targets_stats())["sent"], 2)
        self.assertEqual(self.reports[0]["outcome"], "completed")

    async def test_unauthorized_on_send_rotates_and_last_account_stops(self):
        engine = await self.setup_campaign(["alice", "bobby"])
        accounts = [await self.add_account(i + 1) for i in range(2)]
        managers, factory = self.per_account_managers(accounts)
        for manager in managers.values():
            manager.send_direct_message.return_value = {"success": False, "error": "unauthorized"}
        with patch("core.messenger.AccountManager", side_effect=factory):
            await asyncio.wait_for(engine.start(accounts, "Hello", 10, 10), 5)
        for account in accounts:
            self.assertEqual((await self.db.get_account(account["id"]))["status"], "unauthorized")
        self.assertEqual((await self.db.get_targets_stats())["pending"], 2)
        self.assertIsNone(await self.db.delivery_exists("alice"))
        self.assertEqual(self.reports[0]["outcome"], "failed")

    async def test_rpc_rejection_on_send_is_error_and_campaign_continues(self):
        engine = await self.setup_campaign(["alice", "bobby"])
        account = await self.add_account()
        manager = self.manager(account)
        manager.send_direct_message.side_effect = [
            {"success": False, "error": "error", "message": "RPCError: SOME_REJECTION"},
            {"success": True, "message_id": 8},
        ]
        with patch("core.messenger.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "Hello", 10, 10), 3)
        self.assertEqual(manager.send_direct_message.await_count, 2)
        rows = {row["username"]: row["status"] for row in await self.db.get_targets()}
        self.assertEqual(rows, {"alice": "error", "bobby": "sent"})
        # Definite rejection: no delivery stays reserved/uncertain, so the target can be retried later.
        self.assertIsNone(await self.db.delivery_exists("alice"))
        self.assertEqual(self.reports[0]["outcome"], "completed")
        self.assertEqual(self.reports[0]["errors"], 1)
        self.assertTrue(
            any(row["action"] == "MESSAGE_ERROR" and "SOME_REJECTION" in row["message"] for row in await self.db.get_logs())
        )
        self.assertEqual(await self.db.reset_failed_targets(["error"]), 1)

    async def test_deactivated_recipient_on_send_is_skipped_and_campaign_continues(self):
        engine = await self.setup_campaign(["alice", "bobby"])
        account = await self.add_account()
        manager = self.manager(account)
        manager.send_direct_message.side_effect = [
            {"success": False, "error": "skip", "message": "RPCError: USER_DEACTIVATED"},
            {"success": True, "message_id": 8},
        ]
        with patch("core.messenger.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "Hello", 10, 10), 3)
        rows = {row["username"]: row["status"] for row in await self.db.get_targets()}
        self.assertEqual(rows, {"alice": "not_found", "bobby": "sent"})
        self.assertIsNone(await self.db.delivery_exists("alice"))
        self.assertTrue(any(row["action"] == "MESSAGE_SKIP" for row in await self.db.get_logs()))
        self.assertEqual(self.reports[0]["outcome"], "completed")

    async def test_real_rpc_rejection_from_account_manager_does_not_stop_campaign(self):
        engine = await self.setup_campaign(["alice", "bobby"])
        account = await self.add_account()
        real = AccountManager(account_payload())
        calls = []

        async def request(req, *args, **kwargs):
            calls.append(req)
            if len(calls) == 1:
                raise RPCError(None, "MESSAGE_EMPTY_OR_WHATEVER", 400)
            return SimpleNamespace(id=9)

        real.client = AsyncMock(side_effect=request)
        manager = self.manager(account)
        manager.send_direct_message.side_effect = real.send_direct_message
        with patch("core.messenger.AccountManager", return_value=manager):
            await asyncio.wait_for(engine.start([account], "Hello", 10, 10), 3)
        rows = {row["username"]: row["status"] for row in await self.db.get_targets()}
        self.assertEqual(rows, {"alice": "error", "bobby": "sent"})
        self.assertEqual(self.reports[0]["outcome"], "completed")

    async def test_expired_flood_status_is_cleared(self):
        engine = await self.setup_campaign([])
        account = await self.add_account()
        engine.account_managers[account["id"]] = self.manager(account)
        await self.db.set_flood_wait(account["id"], 10)
        from datetime import datetime, timedelta

        await self.db.update_account(
            account["id"], {"flood_until": (datetime.now() - timedelta(seconds=1)).isoformat()}
        )
        self.assertEqual((await engine._pick_available_account([account]))["status"], "active")

    async def test_private_recipient_is_not_tried_by_other_accounts(self):
        engine = await self.setup_campaign(["alice"])
        account = await self.add_account()
        manager = self.manager(account)
        manager.send_direct_message.return_value = {"success": False, "error": "private"}
        with patch("core.messenger.AccountManager", return_value=manager):
            await engine.start([account], "Hello", 10, 10)
            await self.db.reset_failed_targets(["private"])
            await engine.start([account], "Hello", 10, 10)
        manager.send_direct_message.assert_awaited_once()
        self.assertEqual(await self.db.delivery_exists("alice"), "blocked")
        self.assertTrue(any(row["action"] == "MESSAGE_PRIVATE" for row in await self.db.get_logs()))

    async def test_disabled_and_exhausted_account_not_picked(self):
        engine = await self.setup_campaign([])
        account = await self.add_account()
        engine.account_managers[account["id"]] = self.manager(account)
        await self.db.toggle_account_enabled(account["id"], False)
        self.assertIsNone(await engine._pick_available_account([account]))
        await self.db.toggle_account_enabled(account["id"], True)
        with closing(sqlite3.connect(self.root / "inviter.db")) as conn:
            conn.execute("UPDATE accounts SET sent_today=40")
            conn.commit()
        self.assertIsNone(await engine._pick_available_account([account]))

    async def test_message_counters_rollover(self):
        await self.setup_campaign([])
        account = await self.add_account()
        with closing(sqlite3.connect(self.root / "inviter.db")) as conn:
            conn.execute("UPDATE accounts SET sent_today=40,last_message_reset_date='2000-01-01'")
            conn.commit()
        await self.db.reset_message_counters()
        self.assertEqual((await self.db.get_account(account["id"]))["sent_today"], 0)

    async def test_message_preflight_does_not_need_chat_or_admin_rights(self):
        await self.setup_campaign(["alice"])
        account = await self.add_account()
        manager = self.manager(account)
        with patch("core.diagnostics.AccountManager", return_value=manager):
            result = await preflight(self.db, [account["id"]], "", live=True, mode="messages", message="Hello")
        self.assertTrue(result["ok"])
        manager.check_admin_rights.assert_not_called()
        manager.send_direct_message.assert_not_called()

    async def test_campaign_kind_cannot_change_after_results(self):
        await self.setup_campaign(["alice"])
        target = await self.db.claim_next_pending_target()
        await self.db.mark_target(target["id"], "not_found")
        with self.assertRaises(ValueError):
            await self.db.save_campaign_settings({"operation_mode": "invite"})
        self.assertEqual(await self.db.get_campaign_kind(), "messages")

    async def test_direct_request_uses_literal_text_and_persisted_random_id(self):
        manager = AccountManager(account_payload())
        manager.client = AsyncMock(return_value=SimpleNamespace(id=7))
        result = await manager.send_direct_message(User(id=123), "<b>Текст</b>\nВторая строка", 321)
        request = manager.client.call_args.args[0]
        self.assertIsInstance(request, SendMessageRequest)
        self.assertEqual(request.message, "<b>Текст</b>\nВторая строка")
        self.assertEqual(request.random_id, 321)
        self.assertTrue(result["success"])

    async def test_message_errors_are_classified(self):
        manager = AccountManager(account_payload())
        for exception, expected in (
            (FloodWaitError(None, capture=42), "flood"),
            (PeerFloodError(None), "peer_flood"),
            (UserPrivacyRestrictedError(None), "private"),
            (RPCError(None, "PRIVACY_PREMIUM_REQUIRED", 403), "private"),
            (RPCError(None, "USER_IS_BLOCKED", 403), "private"),
            (RPCError(None, "PEER_ID_INVALID", 400), "skip"),
            (RPCError(None, "INPUT_USER_DEACTIVATED", 400), "skip"),
            (RPCError(None, "SOMETHING_UNKNOWN", 400), "error"),
            (RPCError(None, "INTERNAL", 500), "error"),
            (OSError("network"), "uncertain"),
            (asyncio.TimeoutError(), "uncertain"),
        ):
            manager.client = AsyncMock(side_effect=exception)
            self.assertEqual((await manager.send_direct_message(User(id=123), "Hello", 321))["error"], expected)

    async def test_non_user_bot_and_self_cannot_be_recipient(self):
        manager = AccountManager(account_payload())
        for user in (User(id=123, bot=True), User(id=123, is_self=True), User(id=123, deleted=True)):
            manager.client = AsyncMock()
            manager.client.get_entity.return_value = user
            with self.assertRaises(ValueError):
                await manager.resolve_recipient("alice")

    async def test_message_validation_preserves_text_and_counts_utf16(self):
        self.assertEqual(validate_message(" Hello\n "), " Hello\n ")
        for text in ("  ", "a" * 4097, "😀" * 2049):
            with self.assertRaises(ValueError):
                validate_message(text)
