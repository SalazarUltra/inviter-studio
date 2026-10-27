"""Second-message history verification and dialog catch-up after reconnect."""

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from telethon.errors import FloodWaitError
from telethon.tl.types import InputPeerUser, PeerUser, User

from core.account_manager import AccountManager
from core.followups import ReplyFollowups
from core.messenger import MessageEngine
from tests.support import account_payload
from tests.test_followups import FollowupFixtures, incoming


class VerifyFixtures(FollowupFixtures):
    async def ready_followup(self, name="alice", account=None, user_id=123):
        # Recipients can only be added while the task is stopped.
        if await self.db.get_campaign_kind() == "messages":
            await self.db.set_setting("task_status", "stopped")
        account, _ = await self.seed(name, account, user_id=user_id)
        await self.db.set_setting("task_status", "running")
        self.assertTrue(await self.db.receive_followup_reply(account["id"], user_id, 11))
        return account

    async def tick_engine(self, accounts, **manager_values):
        engine = MessageEngine(self.db, lambda *args: None)
        engine.running = True
        engine._streaks = {}
        engine._session_counts = {"followup_sent": 0, "errors": 0}
        engine._followup_failed = False
        engine.stop = Mock()
        engine.VERIFY_DELAY = 0
        engine.logs = []
        engine.log_callback = lambda level, text: engine.logs.append((level, text))
        managers = {}
        for account in accounts if isinstance(accounts, list) else [accounts]:
            manager = SimpleNamespace(
                followup_history=AsyncMock(return_value={"last_id": 10, "reply_id": None}),
                followup_peer=AsyncMock(return_value=InputPeerUser(123, 456)),
                send_direct_message=AsyncMock(return_value={"success": True, "message_id": 30}),
            )
            for key, value in manager_values.items():
                setattr(manager, key, value)
            managers[account["id"]] = manager
        engine.account_managers = managers
        return engine, managers


class FollowupVerifyTests(VerifyFixtures):
    async def row_of(self, name):
        states = ("waiting", "ready", "sending", "sent", "uncertain", "blocked", "cancelled")
        return next(item for item in await self.db.get_followups(states=states) if item["username"] == name)

    async def uncertain_engine(self, verdicts, reason="timeout", seq=1):
        account = await self.ready_followup(f"user{seq}", await self.add_account(seq), 1000 + seq)
        self.name = f"user{seq}"
        verify = AsyncMock(side_effect=verdicts) if isinstance(verdicts, list) else AsyncMock(return_value=verdicts)
        engine, managers = await self.tick_engine(
            account,
            send_direct_message=AsyncMock(return_value={"success": False, "error": "uncertain", "reason": reason}),
            verify_sent_message=verify,
        )
        return account, engine, managers[account["id"]], ReplyFollowups(engine)

    async def test_found_in_history_marks_sent_for_every_uncertain_reason(self):
        for seq, reason in enumerate(("timeout", "empty", "server", "network"), 1):
            with self.subTest(reason=reason):
                account, engine, manager, followups = await self.uncertain_engine(
                    {"found": True, "message_id": 77}, reason, seq
                )
                await followups.tick()
                row = await self.row_of(self.name)
                self.assertEqual((row["state"], row["message_id"]), ("sent", 77))
                self.assertEqual(engine._session_counts, {"followup_sent": 1, "errors": 0})
                self.assertFalse(engine._followup_failed)
                manager.send_direct_message.assert_awaited_once()
                args = manager.verify_sent_message.await_args.args
                self.assertEqual(args[1], "Second message")
                self.assertEqual(args[2], row["random_id"])
                self.assertIsInstance(args[3], datetime)
                self.assertIsNotNone(args[3].tzinfo)
                self.assertTrue(any("сообщение найдено" in text for _, text in engine.logs))
                engine.stop.assert_not_called()

    async def test_found_on_second_history_check_after_short_wait(self):
        _, engine, manager, followups = await self.uncertain_engine(
            [{"found": False}, {"found": True, "message_id": 5}]
        )
        await followups.tick()
        self.assertEqual(manager.verify_sent_message.await_count, 2)
        self.assertEqual((await self.db.get_followup_stats()), {"sent": 1})

    async def test_absent_returns_to_ready_once_then_uncertain_without_stopping(self):
        account, engine, manager, followups = await self.uncertain_engine({"found": False})
        await followups.tick()
        # First absence: the row is ready again, one resend is allowed, the campaign keeps running.
        self.assertEqual(await self.db.get_followup_stats(), {"ready": 1})
        self.assertEqual(engine._session_counts["errors"], 0)
        self.assertTrue(any("Повтор позже" in text for _, text in engine.logs))
        # The resend is paused for a while, so an immediate tick does not send again.
        followups.next_tick = 0
        await followups.tick()
        self.assertEqual(manager.send_direct_message.await_count, 1)
        # After the pause the second attempt uses a fresh random_id; absence again -> uncertain.
        first_random_id = manager.verify_sent_message.await_args.args[2]
        followups.retry_after.clear()
        followups.next_tick = 0
        await followups.tick()
        self.assertEqual(manager.send_direct_message.await_count, 2)
        self.assertNotEqual(manager.verify_sent_message.await_args.args[2], first_random_id)
        self.assertEqual(await self.db.get_followup_stats(), {"uncertain": 1})
        self.assertEqual(engine._session_counts["errors"], 1)
        self.assertTrue(engine._followup_failed)
        self.assertTrue(any(level == "ERROR" and "после повтора" in text for level, text in engine.logs))
        # Terminal: never a third attempt.
        followups.retry_after.clear()
        followups.next_tick = 0
        await followups.tick()
        self.assertEqual(manager.send_direct_message.await_count, 2)
        engine.stop.assert_not_called()

    async def test_unreadable_history_is_uncertain_without_retry(self):
        for seq, verdict in enumerate(({"found": None}, RuntimeError("history down")), 1):
            with self.subTest(verdict=verdict):
                _, engine, manager, followups = await self.uncertain_engine([verdict, verdict], seq=seq)
                await followups.tick()
                self.assertEqual((await self.row_of(self.name))["state"], "uncertain")
                manager.send_direct_message.assert_awaited_once()
                self.assertEqual(engine._session_counts["errors"], 1)
                self.assertTrue(any("неизвестен" in text for _, text in engine.logs))
                engine.stop.assert_not_called()

    async def test_manager_without_history_api_stays_uncertain(self):
        account = await self.ready_followup()
        engine, managers = await self.tick_engine(
            account, send_direct_message=AsyncMock(return_value={"success": False, "error": "uncertain"})
        )
        await ReplyFollowups(engine).tick()
        self.assertEqual(await self.db.get_followup_stats(), {"uncertain": 1})

    async def test_other_rows_and_accounts_continue_after_uncertain_second(self):
        first = await self.ready_followup()
        second = await self.ready_followup("bobby", await self.add_account(2), user_id=456)
        engine, managers = await self.tick_engine(
            [first, second], verify_sent_message=AsyncMock(return_value={"found": False})
        )
        managers[first["id"]].send_direct_message.return_value = {"success": False, "error": "uncertain"}
        followups = ReplyFollowups(engine)
        for _ in range(3):
            followups.next_tick = 0
            await followups.tick()
        self.assertEqual(managers[second["id"]].send_direct_message.await_count, 1)
        self.assertEqual((await self.db.get_followup_stats()).get("sent"), 1)
        self.assertEqual((await self.db.get_followup_stats()).get("ready"), 1)  # alice waits for her retry
        engine.stop.assert_not_called()


def dialog(user_id, top, date=None, access_hash=1):
    entity = User(id=user_id, access_hash=access_hash)
    return SimpleNamespace(entity=entity, message=top, date=date or datetime.now(timezone.utc), is_user=True)


def row(user_id, first=10, checked=0, name=None):
    return {
        "user_id": user_id,
        "access_hash": 1,
        "first_message_id": first,
        "checked_id": checked,
        "username": name or f"user{user_id}",
    }


def mocked_client(dialogs, history=None):
    async def iter_dialogs(limit=None):
        for item in dialogs[:limit]:
            yield item

    async def iter_messages(peer, **kwargs):
        for message in (history or {}).get(peer.user_id, []):
            if message.id > kwargs.get("min_id", 0):
                yield message

    return SimpleNamespace(iter_dialogs=Mock(side_effect=iter_dialogs), iter_messages=Mock(side_effect=iter_messages))


def outgoing(user_id, message_id):
    return SimpleNamespace(id=message_id, peer_id=PeerUser(user_id), sender_id=1, out=True, action=None)


class CatchupManagerTests(FollowupFixtures):
    def manager(self, client):
        manager = AccountManager(account_payload())
        manager.client = client
        return manager

    async def test_catchup_uses_top_message_then_history_and_skips_unrelated_dialogs(self):
        client = mocked_client(
            [
                dialog(123, incoming(123, 15)),  # reply is the top message: no history request
                SimpleNamespace(entity=SimpleNamespace(id=5), message=None, date=None),
                dialog(999, incoming(999, 40)),  # nobody waits for this person
                dialog(456, outgoing(456, 30)),  # our own message is on top; reply is earlier
                dialog(789, incoming(789, 12)),  # nothing newer than the already checked id
            ],
            history={456: [incoming(456, 22), outgoing(456, 30)]},
        )
        result = await self.manager(client).followup_catchup(
            [row(123), row(456), row(789, checked=12)], history_delay=0
        )
        self.assertEqual(result["replies"], {123: 15, 456: 22})
        self.assertEqual(result["history"], 1)
        client.iter_messages.assert_called_once()
        self.assertEqual(client.iter_messages.call_args.kwargs["min_id"], 10)
        self.assertEqual(client.iter_dialogs.call_args.kwargs["limit"], 100)

    async def test_catchup_ignores_old_edited_and_own_messages(self):
        client = mocked_client(
            [dialog(123, outgoing(123, 50)), dialog(456, incoming(456, 9))],
            history={123: [outgoing(123, 30), outgoing(123, 50)]},
        )
        result = await self.manager(client).followup_catchup([row(123), row(456)], history_delay=0)
        self.assertEqual(result["replies"], {})
        # How far the chat was read is reported so the poll does not repeat it.
        self.assertEqual(result["checked"], {123: 50})

    async def test_catchup_history_reads_are_capped_and_scan_stops_at_old_dialogs(self):
        rows = [row(1000 + i) for i in range(5)]
        dialogs = [dialog(1000 + i, outgoing(1000 + i, 30)) for i in range(5)]
        history = {1000 + i: [incoming(1000 + i, 20)] for i in range(5)}
        result = await self.manager(mocked_client(dialogs, history)).followup_catchup(
            rows, max_history=2, history_delay=0
        )
        self.assertEqual((len(result["replies"]), result["history"]), (2, 2))
        old = datetime.now(timezone.utc) - timedelta(days=3)
        client = mocked_client([dialog(1000, outgoing(1000, 30), date=old)], history)
        result = await self.manager(client).followup_catchup(
            [row(1000)], since=datetime.now() - timedelta(hours=1), history_delay=0
        )
        self.assertEqual((result["dialogs"], result["history"]), (0, 0))
        client.iter_messages.assert_not_called()

    async def test_catchup_spaces_history_reads_and_limits_dialogs(self):
        from unittest.mock import patch

        dialogs = [dialog(1000 + i, outgoing(1000 + i, 30)) for i in range(3)]
        history = {1000 + i: [incoming(1000 + i, 20)] for i in range(3)}
        client = mocked_client(dialogs, history)
        with patch("core.account_manager.asyncio.sleep", new=AsyncMock()) as sleep:
            await self.manager(client).followup_catchup(
                [row(1000 + i) for i in range(3)], dialog_limit=50, history_delay=0.7
            )
        self.assertEqual([call.args for call in sleep.await_args_list], [(0.7,), (0.7,)])  # between reads only
        self.assertEqual(client.iter_dialogs.call_args.kwargs["limit"], 50)

    async def test_catchup_flood_is_raised_and_empty_input_makes_no_requests(self):
        client = SimpleNamespace(iter_dialogs=Mock(side_effect=FloodWaitError(request=None, capture=30)))
        with self.assertRaises(FloodWaitError):
            await self.manager(client).followup_catchup([row(123)])
        client = mocked_client([])
        self.assertEqual((await self.manager(client).followup_catchup([]))["replies"], {})
        client.iter_dialogs.assert_not_called()
        self.assertEqual((await AccountManager(account_payload()).followup_catchup([row(1)]))["dialogs"], 0)


class CatchupFollowupsTests(VerifyFixtures):
    async def test_missed_reply_becomes_ready_and_second_message_is_sent(self):
        account, _ = await self.seed()
        await self.db.set_setting("task_status", "running")
        catchup = AsyncMock(return_value={"replies": {123: 15}, "checked": {123: 15}, "dialogs": 3, "history": 1})
        engine, managers = await self.tick_engine(account, followup_catchup=catchup)
        followups = ReplyFollowups(engine)
        followups.catchup_next = {account["id"]: 0}
        await followups.tick()
        catchup.assert_awaited_once()
        passed = catchup.await_args.args[0]
        self.assertEqual([item["username"] for item in passed], ["alice"])
        self.assertEqual(catchup.await_args.kwargs["dialog_limit"], 100)
        self.assertIsInstance(catchup.await_args.kwargs["since"], datetime)
        row_ = (await self.db.get_followups(states=("sent",)))[0]
        self.assertEqual(row_["reply_id"], 15)
        managers[account["id"]].send_direct_message.assert_awaited_once()
        texts = [text for _, text in engine.logs]
        self.assertTrue(any("Найден пропущенный ответ @alice" in text for text in texts))
        self.assertTrue(any("Проверка пропущенных ответов" in text and "найдено ответов 1" in text for text in texts))

    async def test_catchup_runs_once_then_periodically_and_can_be_disabled(self):
        account, _ = await self.seed()
        await self.db.set_setting("task_status", "running")
        catchup = AsyncMock(return_value={"replies": {}, "checked": {}, "dialogs": 1, "history": 0})
        engine, _ = await self.tick_engine(account, followup_catchup=catchup)
        followups = ReplyFollowups(engine)
        followups.catchup_next = {account["id"]: 0}
        for _ in range(3):
            followups.next_tick = 0
            await followups.tick()
        self.assertEqual(catchup.await_count, 1)  # the next pass is a full interval away
        followups.catchup_next[account["id"]] = time.monotonic() - 1
        followups.next_tick = 0
        await followups.tick()
        self.assertEqual(catchup.await_count, 2)
        followups.catchup_interval = 0
        followups.catchup_next[account["id"]] = 0
        followups.next_tick = 0
        await followups.tick()
        self.assertEqual(catchup.await_count, 3)
        followups.next_tick = 0
        await followups.tick()
        self.assertEqual(catchup.await_count, 3)  # periodic part disabled
        # Quiet passes are logged once (first pass), not on every interval.
        self.assertEqual(sum("Проверка пропущенных ответов" in text for _, text in engine.logs), 1)

    async def test_attach_schedules_every_connected_account_and_only_one_account_per_tick(self):
        first, _ = await self.seed()
        second = await self.add_account(2)
        await self.seed("bobby", second, user_id=456)
        await self.db.set_setting("task_status", "running")
        catchup = AsyncMock(return_value={"replies": {}, "checked": {}, "dialogs": 0, "history": 0})
        engine, managers = await self.tick_engine([first, second], followup_catchup=catchup)
        for manager in managers.values():
            manager.client = SimpleNamespace(add_event_handler=Mock(), remove_event_handler=Mock())
        followups = ReplyFollowups(engine)
        followups.attach()
        self.assertEqual(set(followups.catchup_next), {first["id"], second["id"]})
        await followups.tick()
        self.assertEqual(catchup.await_count, 1)  # flood-safe: one account per tick
        followups.next_tick = 0
        await followups.tick()
        self.assertEqual(catchup.await_count, 2)
        self.assertEqual(
            {call.args[0][0]["account_id"] for call in catchup.await_args_list}, {first["id"], second["id"]}
        )

    async def test_catchup_skips_accounts_without_waiting_rows_or_when_unusable(self):
        account, _ = await self.seed()
        await self.db.set_setting("task_status", "running")
        await self.db.update_account(account["id"], {"status": "restricted"})
        catchup = AsyncMock()
        engine, _ = await self.tick_engine(account, followup_catchup=catchup)
        followups = ReplyFollowups(engine)
        followups.catchup_next = {account["id"]: 0}
        await followups.tick()
        catchup.assert_not_awaited()
        self.assertGreater(followups.catchup_next[account["id"]], time.monotonic())  # retried soon
        await self.db.update_account(account["id"], {"status": "active"})
        self.assertTrue(await self.db.receive_followup_reply(account["id"], 123, 11))
        followups.catchup_next = {account["id"]: 0}
        followups.next_tick = 0
        await followups.tick()
        catchup.assert_not_awaited()  # no waiting row left to look for

    async def test_flood_during_catchup_pauses_only_that_account(self):
        first, _ = await self.seed()
        second = await self.add_account(2)
        await self.seed("bobby", second, user_id=456)
        await self.db.set_setting("task_status", "running")
        engine, managers = await self.tick_engine(
            [first, second],
            followup_catchup=AsyncMock(
                return_value={"replies": {456: 15}, "checked": {456: 15}, "dialogs": 1, "history": 0}
            ),
        )
        managers[first["id"]].followup_catchup = AsyncMock(side_effect=FloodWaitError(request=None, capture=90))
        followups = ReplyFollowups(engine)
        followups.catchup_next = {first["id"]: 0, second["id"]: 1e12}
        await followups.tick()
        self.assertEqual((await self.db.get_account(first["id"]))["status"], "flood")
        engine.stop.assert_not_called()
        self.assertTrue(any("кампания продолжается" in text for _, text in engine.logs))
        followups.catchup_next[second["id"]] = 0
        followups.next_tick = 0
        await followups.tick()
        self.assertEqual((await self.db.get_followups(states=("sent",)))[0]["username"], "bobby")
        self.assertEqual((await self.db.get_followups(states=("waiting",)))[0]["username"], "alice")

    async def test_catchup_error_keeps_waiting_and_logs_warning(self):
        account, _ = await self.seed()
        await self.db.set_setting("task_status", "running")
        engine, _ = await self.tick_engine(account, followup_catchup=AsyncMock(side_effect=RuntimeError("boom")))
        followups = ReplyFollowups(engine)
        followups.catchup_next = {account["id"]: 0}
        await followups.tick()
        self.assertEqual(await self.db.get_followup_stats(), {"waiting": 1})
        self.assertTrue(any(level == "WARN" and "пропущенные ответы" in text for level, text in engine.logs))
        engine.stop.assert_not_called()

    async def test_stale_or_foreign_reply_ids_are_rejected_by_receive_path(self):
        account, _ = await self.seed()
        await self.db.set_setting("task_status", "running")
        catchup = AsyncMock(return_value={"replies": {123: 9, 555: 40}, "checked": {}, "dialogs": 2, "history": 0})
        engine, _ = await self.tick_engine(account, followup_catchup=catchup)
        followups = ReplyFollowups(engine)
        followups.catchup_next = {account["id"]: 0}
        await followups.tick()
        self.assertEqual(await self.db.get_followup_stats(), {"waiting": 1})  # 9 is not newer than the first message


class CatchupEngineTests(FollowupFixtures):
    async def test_engine_connect_triggers_catchup_that_delivers_second_message(self):
        import asyncio
        from unittest.mock import patch

        account, _ = await self.seed()
        await self.db.save_campaign_settings({"followup_enabled": "1", "followup_text": "Second message"})
        engine = MessageEngine(self.db, lambda *args: None)
        manager = SimpleNamespace(
            account=account,
            connect=AsyncMock(),
            disconnect=AsyncMock(),
            followup_peer=AsyncMock(return_value=InputPeerUser(123, 456)),
            followup_history=AsyncMock(return_value={"last_id": 10, "reply_id": None}),
            followup_catchup=AsyncMock(
                return_value={"replies": {123: 11}, "checked": {123: 11}, "dialogs": 4, "history": 0}
            ),
            send_direct_message=AsyncMock(return_value={"success": True, "message_id": 31}),
            client=SimpleNamespace(add_event_handler=Mock(), remove_event_handler=Mock()),
        )
        with (
            patch("core.messenger.AccountManager", return_value=manager),
            patch.object(self.db, "last_message_time", new=AsyncMock(return_value=None)),
        ):
            await asyncio.wait_for(engine.start([account], "First message", 10, 10), 10)
        manager.followup_catchup.assert_awaited_once()
        manager.send_direct_message.assert_awaited_once()
        self.assertEqual(await self.db.get_followup_stats(), {"sent": 1})
