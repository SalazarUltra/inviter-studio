import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon.errors import AuthKeyUnregisteredError, FloodWaitError, PeerFloodError
from telethon.tl.types import InputPeerUser, PeerChannel, PeerUser, UpdateMessageID, User

from core.account_manager import AccountManager, is_followup_reply
from core.database import _connect, init_db
from core.diagnostics import preflight
from core.followup_store import clamp_wait_hours
from core.followups import ReplyFollowups
from core.messenger import MessageEngine
from tests.support import DatabaseCase, account_payload


def incoming(user_id=123, message_id=11, **values):
    return SimpleNamespace(
        **(
            {"id": message_id, "peer_id": PeerUser(user_id), "sender_id": user_id, "out": False, "action": None}
            | values
        )
    )


class FollowupFixtures(DatabaseCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Most tests exercise delivery, not the human-like post-reply delay.
        self.set_reply_delay(0, 0)

    def set_reply_delay(self, low, high):
        delay = patch("core.followup_store.FOLLOWUP_DELAY_RANGE", (low, high))
        delay.start()
        self.addCleanup(delay.stop)

    async def seed(self, name="alice", account=None, user_id=123, state="sent", message_id=10):
        if await self.db.get_campaign_kind() != "messages":
            self.db = self.db.for_campaign(await self.db.create_campaign("Replies", "messages"))
        account = account or await self.add_account()
        await self.db.bulk_insert_targets([name])
        target = await self.db.claim_next_pending_target()
        await self.db.reserve_delivery(
            target["id"], account["id"], user_id, followup_text="Second message", access_hash=456
        )
        if state:
            await self.db.finish_delivery(target["id"], state, message_id)
        return account, target


class FollowupDatabaseTests(FollowupFixtures):
    async def test_second_message_shares_daily_budget_with_first_messages(self):
        account, _ = await self.seed()
        await self.db.set_setting("task_status", "running")
        await self.db.receive_followup_reply(account["id"], 123, 11)
        await self.db.claim_followup("alice")
        await self.db.finish_followup("alice", "sent", 20)
        engine = MessageEngine(self.db, lambda *args: None)
        engine.account_managers = {account["id"]: object()}
        account_at_limit = {**account, "daily_limit": 10, "sent_today": 10, "account_age_days": 45}
        with patch.object(engine.db, "get_account", new=AsyncMock(return_value=account_at_limit)):
            self.assertIsNone(await engine._pick_available_account([account]))
            self.assertIsNone(await engine._pick_available_account([account], followup=True))
        # Follow-ups use their own counter, but it counts against the same combined daily cap.
        self.assertEqual((await self.db.get_account(account["id"]))["sent_today"], 1)
        await self.db.set_setting("task_status", "stopped")
        await self.db.bulk_insert_targets(["bobby"])
        with patch.object(engine.db, "get_account", new=AsyncMock(return_value=account)):
            self.assertIsNotNone(await engine._pick_available_account([account]))

    async def test_restricted_account_followup_does_not_hide_other_accounts_history_reply(self):
        first, _ = await self.seed()
        second = await self.add_account(2)
        await self.seed("bobby", second, user_id=456)
        await self.db.set_setting("task_status", "running")
        await self.db.receive_followup_reply(first["id"], 123, 11)
        await self.db.update_account(first["id"], {"status": "restricted"})
        engine = MessageEngine(self.db, lambda *args: None)
        engine.running = True
        engine._streaks = {}
        engine._session_counts = {"followup_sent": 0}
        blocked = SimpleNamespace(send_direct_message=AsyncMock())
        available = SimpleNamespace(
            followup_history=AsyncMock(return_value={"last_id": 21, "reply_id": 21}),
            followup_peer=AsyncMock(return_value=InputPeerUser(456, 456)),
            send_direct_message=AsyncMock(return_value={"success": True, "message_id": 30}),
        )
        engine.account_managers = {first["id"]: blocked, second["id"]: available}
        await ReplyFollowups(engine).tick()
        available.followup_history.assert_awaited_once()
        available.send_direct_message.assert_awaited_once()
        blocked.send_direct_message.assert_not_awaited()
        self.assertEqual(await self.db.get_followup_stats(), {"ready": 1, "sent": 1})

    async def test_confirmed_first_and_new_reply_required(self):
        account, _ = await self.seed()
        await self.db.set_setting("task_status", "running")
        row = (await self.db.get_followups())[0]
        self.assertEqual((row["state"], row["first_message_id"], row["text"]), ("waiting", 10, "Second message"))
        for aid, uid, mid in [
            (account["id"], 123, 9),
            (account["id"], 123, 10),
            (999, 123, 11),
            (account["id"], 999, 11),
        ]:
            self.assertFalse(await self.db.receive_followup_reply(aid, uid, mid))
        self.assertTrue(await self.db.receive_followup_reply(account["id"], 123, 11))
        self.assertFalse(await self.db.receive_followup_reply(account["id"], 123, 12))
        self.assertEqual((await self.db.get_followups())[0]["reply_id"], 11)

    async def test_unconfirmed_first_never_enables_second(self):
        account, _ = await self.seed(message_id=None)
        self.assertEqual(await self.db.get_followups(), [])
        self.assertFalse(await self.db.receive_followup_reply(account["id"], 123, 11))
        self.assertEqual((await self.db.get_followup_stats())["uncertain"], 1)

    async def test_private_first_never_enables_second(self):
        account, _ = await self.seed(state="private")
        self.assertEqual(await self.db.get_followups(), [])
        self.assertFalse(await self.db.receive_followup_reply(account["id"], 123, 11))
        self.assertEqual((await self.db.get_followup_stats())["blocked"], 1)

    async def test_uncertain_first_and_release_do_not_leave_waiting(self):
        _, target = await self.seed(state=None)
        await self.db.release_delivery(target["id"])
        self.assertEqual(await self.db.get_followup_stats(), {})
        await self.db.reserve_delivery(
            target["id"], (await self.db.get_accounts())[0]["id"], 123, followup_text="Second", access_hash=456
        )
        await self.db.finish_delivery(target["id"], "uncertain")
        self.assertEqual(await self.db.get_followups(), [])

    async def test_atomic_claim_counts_and_no_second_claim(self):
        account, _ = await self.seed()
        await self.db.set_setting("task_status", "running")
        await self.db.receive_followup_reply(account["id"], 123, 11)
        claims = await asyncio.gather(self.db.claim_followup("alice"), self.db.claim_followup("alice"))
        self.assertEqual(sum(row is not None for row in claims), 1)
        await self.db.finish_followup("alice", "sent", 20)
        self.assertIsNone(await self.db.claim_followup("alice"))
        with self.assertRaises(RuntimeError):
            await self.db.finish_followup("alice", "sent", 20)
        self.assertEqual((await self.db.get_account(account["id"]))["sent_today"], 1)
        self.assertEqual(await self.db.delivery_exists("alice"), "sent")
        self.assertTrue(any(row["action"] == "FOLLOWUP_SENT" for row in await self.db.get_logs()))

    async def test_scope_and_clear_keep_the_original_delivery(self):
        account, _ = await self.seed()
        other = self.db.for_campaign(await self.db.create_campaign("Other", "messages"))
        self.assertFalse(await other.receive_followup_reply(account["id"], 123, 11))
        await self.db.clear_targets()
        self.assertEqual(len(await self.db.get_followups()), 1)
        self.assertEqual(await self.db.delivery_exists("alice"), "sent")
        self.assertEqual(await other.get_followup_stats(), {})

    async def test_restart_cancels_waiting_and_marks_sending_uncertain(self):
        account, _ = await self.seed()
        second_account = await self.add_account(2)
        second, _ = await self.seed("bobby", account=second_account, user_id=456)
        await self.db.set_setting("task_status", "running")
        self.assertTrue(await self.db.receive_followup_reply(account["id"], 123, 11))
        self.assertTrue(await self.db.receive_followup_reply(second["id"], 456, 11))
        await self.db.claim_followup("bobby")
        await init_db()
        await init_db()
        states = await self.db.get_followup_stats()
        self.assertEqual(states["cancelled"], 1)
        self.assertEqual(states["uncertain"], 1)
        self.assertFalse(await self.db.receive_followup_reply(account["id"], 123, 12))

    async def test_cancel_does_not_remove_first_or_other_campaign(self):
        await self.seed()
        await self.db.set_setting("task_status", "running")
        with self.assertRaises(RuntimeError):
            await self.db.cancel_followups()
        await self.db.set_setting("task_status", "stopped")
        self.assertEqual(await self.db.cancel_followups(), 1)
        self.assertEqual(await self.db.cancel_followups(), 0)
        self.assertEqual(await self.db.delivery_exists("alice"), "sent")
        self.assertEqual((await self.db.get_targets())[0]["status"], "sent")

    async def test_legacy_database_adds_table_without_changing_first_delivery(self):
        account, target = await self.seed()
        from core.database import _connect

        async with _connect() as connection:
            await connection.execute("DROP TABLE message_followups")
            await connection.commit()
        await init_db()
        await init_db()
        self.assertEqual(await self.db.get_followup_stats(), {})
        self.assertEqual(await self.db.delivery_exists("alice"), "sent")
        self.assertEqual((await self.db.get_account(account["id"]))["sent_today"], 1)
        self.assertEqual((await self.db.get_targets())[0]["id"], target["id"])

    async def test_preflight_can_resume_waiting_without_first_queue(self):
        account, _ = await self.seed()
        await self.db.update_account(account["id"], {"sent_today": 40, "daily_limit": 40})
        self.assertFalse((await preflight(self.db, [account["id"]], "", mode="messages", message="Hi"))["ok"])
        self.assertTrue(
            (await preflight(self.db, [account["id"]], "", mode="messages", message="Hi", followup_enabled=True))["ok"]
        )
        self.assertFalse((await preflight(self.db, [], "", mode="messages", message="Hi", followup_enabled=True))["ok"])

    async def test_preflight_requires_available_original_followup_account(self):
        owner, _ = await self.seed()
        other = await self.add_account(2)
        await self.db.update_account(owner["id"], {"status": "restricted"})
        report = await preflight(
            self.db,
            [owner["id"], other["id"]],
            "",
            mode="messages",
            message="Hi",
            followup_enabled=True,
        )
        self.assertFalse(report["ok"])
        availability = next(row for row in report["checks"] if row["label"] == "Доступность отправки")
        self.assertFalse(availability["ok"])
        self.assertIn("закреплены", availability["detail"])

    async def test_preflight_requires_followup_owner_to_be_selected(self):
        _, _ = await self.seed()
        other = await self.add_account(2)
        report = await preflight(
            self.db,
            [other["id"]],
            "",
            mode="messages",
            message="Hi",
            followup_enabled=True,
        )
        self.assertFalse(report["ok"])
        availability = next(row for row in report["checks"] if row["label"] == "Доступность отправки")
        self.assertFalse(availability["ok"])
        self.assertIn("Выберите исходные аккаунты", availability["detail"])

    async def test_preflight_warns_if_saved_followups_are_disabled(self):
        account, _ = await self.seed()
        report = await preflight(self.db, [account["id"]], "", mode="messages", message="Hi")
        self.assertFalse(report["ok"])
        followups = next(row for row in report["checks"] if row["label"] == "Вторые сообщения")
        self.assertFalse(followups["ok"])
        self.assertIn("отмените очередь", followups["detail"])

    async def test_text_snapshot_and_bounded_candidates(self):
        account, _ = await self.seed()
        await self.seed("bobby", account, user_id=456)
        await self.db.set_setting("task_status", "running")
        await self.db.receive_followup_reply(account["id"], 123, 11)
        await self.db.receive_followup_reply(account["id"], 456, 11)
        await self.db.set_setting("followup_text", "New text")
        rows = await self.db.get_followups(states=("ready",), account_ids=[account["id"]], limit=1, per_account=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "Second message")
        self.assertEqual(await self.db.get_followups(account_ids=[]), [])
        self.assertEqual(await self.db.get_followup_stats(account_ids=[]), {})


class FollowupEngineTests(FollowupFixtures):
    async def configure(self, seed=False):
        if seed:
            account, _ = await self.seed()
        else:
            self.db = self.db.for_campaign(await self.db.create_campaign("Replies", "messages"))
            account = await self.add_account()
            await self.db.bulk_insert_targets(["alice"])
        await self.db.save_campaign_settings({"followup_enabled": "1", "followup_text": "Second message"})
        self.engine = MessageEngine(self.db, lambda *args: None)
        self.handlers = []
        self.manager = SimpleNamespace(
            account=account,
            connect=AsyncMock(),
            disconnect=AsyncMock(),
            resolve_recipient=AsyncMock(return_value=User(id=123, access_hash=456)),
            send_direct_message=AsyncMock(return_value={"success": True, "message_id": 10}),
            followup_peer=AsyncMock(return_value=InputPeerUser(123, 456)),
            followup_history=AsyncMock(return_value={"last_id": 10, "reply_id": None}),
            client=SimpleNamespace(
                add_event_handler=lambda callback, event: self.handlers.append(callback),
                remove_event_handler=lambda callback: self.handlers.remove(callback),
            ),
        )
        return account

    async def wait_for(self, check):
        for _ in range(150):
            if await check():
                return
            await asyncio.sleep(0.025)
        self.fail("Engine state timed out")

    async def wait_first(self):
        await self.wait_for(lambda: self.has_state("waiting"))

    async def has_state(self, state):
        return bool((await self.db.get_followup_stats()).get(state))

    async def emit(self, message=None):
        for handler in list(self.handlers):
            await handler(SimpleNamespace(is_private=True, message=message or incoming()))

    async def run_engine(self, account):
        self.patch_manager = patch("core.messenger.AccountManager", return_value=self.manager)
        self.patch_manager.start()
        self.addCleanup(self.patch_manager.stop)
        self.patch_time = patch.object(self.db, "last_message_time", new=AsyncMock(return_value=None))
        self.patch_time.start()
        self.addCleanup(self.patch_time.stop)
        task = asyncio.create_task(self.engine.start([account], "First message", 10, 10))

        async def cleanup():
            self.engine.stop()
            if not task.done():
                await asyncio.wait_for(task, 5)

        self.addAsyncCleanup(cleanup)
        return task

    async def test_live_reply_sends_exactly_one_second_with_same_account(self):
        account = await self.configure()
        task = await self.run_engine(account)
        await self.wait_first()
        await asyncio.gather(self.emit(), self.emit(incoming(message_id=12)))
        await asyncio.wait_for(task, 5)
        self.assertEqual(self.manager.send_direct_message.await_count, 2)
        self.assertEqual(self.manager.send_direct_message.await_args.args[1], "Second message")
        self.assertIsInstance(self.manager.send_direct_message.await_args.args[0], InputPeerUser)
        self.assertEqual((await self.db.get_account(account["id"]))["sent_today"], 1)
        self.assertEqual(self.handlers, [])

    async def test_no_reply_does_not_send_second_and_stop_cancels_wait(self):
        account = await self.configure()
        task = await self.run_engine(account)
        await self.wait_first()
        await self.emit(incoming(message_id=9))
        await asyncio.sleep(0.15)
        self.engine.stop()
        await asyncio.wait_for(task, 5)
        self.assertEqual(self.manager.send_direct_message.await_count, 1)
        self.assertTrue(await self.has_state("cancelled"))

    async def test_pause_defers_ready_second_until_resume(self):
        account = await self.configure()
        task = await self.run_engine(account)
        await self.wait_first()
        self.engine.pause()
        await self.emit()
        await asyncio.sleep(0.15)
        self.assertEqual(self.manager.send_direct_message.await_count, 1)
        self.engine.resume()
        await asyncio.wait_for(task, 5)
        self.assertEqual(self.manager.send_direct_message.await_count, 2)

    async def test_restart_never_sends_followup_after_campaign_was_stopped(self):
        account = await self.configure(seed=True)
        await self.db.set_setting("followup_text", "Changed after first")
        await self.db.set_setting("task_status", "stopped")
        self.assertEqual(await self.db.cancel_followups("Задача завершена"), 1)
        self.manager.followup_history.return_value = {"last_id": 11, "reply_id": 11}
        task = await self.run_engine(account)
        await asyncio.wait_for(task, 5)
        self.manager.send_direct_message.assert_not_awaited()
        self.assertTrue(await self.has_state("cancelled"))

    async def test_known_block_prevents_second_rpc(self):
        account = await self.configure()
        self.manager.followup_peer.return_value = None
        task = await self.run_engine(account)
        await self.wait_first()
        await self.emit()
        await asyncio.wait_for(task, 5)
        self.assertEqual(self.manager.send_direct_message.await_count, 1)
        self.assertTrue(await self.has_state("blocked"))

    async def test_private_error_is_terminal_and_never_retried(self):
        account = await self.configure()
        self.manager.send_direct_message.side_effect = [
            {"success": True, "message_id": 10},
            {"success": False, "error": "private"},
        ]
        task = await self.run_engine(account)
        await self.wait_first()
        await self.emit()
        await asyncio.wait_for(task, 5)
        self.assertTrue(await self.has_state("blocked"))
        self.assertIsNone(await self.db.claim_followup("alice"))

    async def test_flood_pauses_account_but_does_not_stop_campaign(self):
        account = await self.configure()
        self.manager.send_direct_message.side_effect = [
            {"success": True, "message_id": 10},
            {"success": False, "error": "flood", "flood_seconds": 60},
        ]
        task = await self.run_engine(account)
        await self.wait_first()
        await self.emit()

        async def flooded():
            return (await self.db.get_account(account["id"]))["status"] == "flood"

        await self.wait_for(flooded)
        await asyncio.sleep(0.1)
        self.assertFalse(task.done())
        self.assertTrue(self.engine.running)
        self.assertTrue(await self.has_state("ready"))
        self.assertEqual(self.manager.send_direct_message.await_count, 2)
        self.engine.stop()
        await asyncio.wait_for(task, 5)
        self.assertTrue(await self.has_state("cancelled"))

    async def test_run_finishes_normally_when_all_waiting_expired(self):
        account = await self.configure(seed=True)
        async with _connect() as connection:
            old = (datetime.now() - timedelta(hours=30)).isoformat(timespec="seconds")
            await connection.execute("UPDATE message_followups SET created_at=?", (old,))
            await connection.commit()
        summaries = []
        self.engine.complete_callback = summaries.append
        task = await self.run_engine(account)
        await asyncio.wait_for(task, 5)
        self.assertEqual(summaries[0]["outcome"], "completed")
        self.assertEqual(await self.db.get_followup_stats(), {"cancelled": 1})

    async def test_uncertain_second_is_not_automatically_retried(self):
        account = await self.configure()
        self.manager.send_direct_message.side_effect = [
            {"success": True, "message_id": 10},
            {"success": False, "error": "uncertain"},
        ]
        task = await self.run_engine(account)
        await self.wait_first()
        await self.emit()
        await asyncio.wait_for(task, 5)
        self.assertTrue(await self.has_state("uncertain"))
        await init_db()
        self.assertIsNone(await self.db.claim_followup("alice"))

    async def test_cancel_during_second_send_keeps_uncertain(self):
        account = await self.configure(seed=True)
        await self.db.set_setting("task_status", "running")
        await self.db.receive_followup_reply(account["id"], 123, 11)
        started = asyncio.Event()

        async def sending(*args):
            started.set()
            await asyncio.Event().wait()

        self.manager.send_direct_message.side_effect = sending
        task = await self.run_engine(account)
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(await self.has_state("uncertain"))

    async def test_followup_daily_limit_defers_second(self):
        account = await self.configure(seed=True)
        await self.db.set_setting("task_status", "running")
        await self.db.receive_followup_reply(account["id"], 123, 11)
        from datetime import date

        from core.database import _connect

        async with _connect() as connection:
            # The reply-triggered follow-up consumes its own daily counter (limit 40),
            # already spent today; the midnight reset must not clear it mid-day.
            await connection.execute(
                "UPDATE accounts SET followups_today=40, followup_reset_date=?", (date.today().isoformat(),)
            )
            await connection.commit()
        task = await self.run_engine(account)
        await asyncio.sleep(0.3)
        self.engine.stop()
        await asyncio.wait_for(task, 5)
        self.manager.send_direct_message.assert_not_called()
        self.assertTrue(await self.has_state("cancelled"))

    async def test_exhausted_combined_quota_defers_second(self):
        account = await self.configure(seed=True)
        await self.db.set_setting("task_status", "running")
        await self.db.receive_followup_reply(account["id"], 123, 11)
        from datetime import date

        from core.database import _connect

        async with _connect() as connection:
            # First messages already used the whole combined daily budget: the follow-up waits.
            await connection.execute(
                "UPDATE accounts SET sent_today=40, last_message_reset_date=?", (date.today().isoformat(),)
            )
            await connection.commit()
        task = await self.run_engine(account)
        await asyncio.sleep(0.3)
        self.engine.stop()
        await asyncio.wait_for(task, 5)
        self.manager.send_direct_message.assert_not_called()
        self.assertEqual((await self.db.get_account(account["id"]))["followups_today"], 0)

    async def test_second_message_sends_while_combined_quota_remains(self):
        account = await self.configure(seed=True)
        await self.db.set_setting("task_status", "running")
        await self.db.receive_followup_reply(account["id"], 123, 11)
        from datetime import date

        from core.database import _connect

        async with _connect() as connection:
            await connection.execute(
                "UPDATE accounts SET sent_today=39, last_message_reset_date=?", (date.today().isoformat(),)
            )
            await connection.commit()
        task = await self.run_engine(account)
        await asyncio.wait_for(task, 5)
        self.assertEqual(self.manager.send_direct_message.await_count, 1)
        fresh = await self.db.get_account(account["id"])
        self.assertEqual((fresh["sent_today"], fresh["followups_today"]), (39, 1))
        self.assertTrue(await self.has_state("sent"))


class FollowupReliabilityTests(FollowupFixtures):
    async def tick_engine(self, account, **manager_values):
        engine = MessageEngine(self.db, lambda *args: None)
        engine.running = True
        engine._streaks = {}
        engine._session_counts = {"followup_sent": 0, "errors": 0}
        engine._followup_failed = False
        engine.stop = Mock()
        manager = SimpleNamespace(
            followup_history=AsyncMock(return_value={"last_id": 10, "reply_id": None}),
            followup_peer=AsyncMock(return_value=InputPeerUser(123, 456)),
            send_direct_message=AsyncMock(return_value={"success": True, "message_id": 30}),
        )
        for key, value in manager_values.items():
            setattr(manager, key, value)
        engine.account_managers = {account["id"]: manager}
        return engine, manager

    async def ready_followup(self):
        account, _ = await self.seed()
        await self.db.set_setting("task_status", "running")
        self.assertTrue(await self.db.receive_followup_reply(account["id"], 123, 11))
        return account

    async def test_ready_at_delays_second_message_30_to_180_seconds(self):
        self.set_reply_delay(30, 180)
        account = await self.ready_followup()
        row = (await self.db.get_followups(states=("ready",)))[0]
        ready_at = datetime.fromisoformat(row["ready_at"])
        elapsed = (ready_at - datetime.now()).total_seconds()
        self.assertTrue(29 <= elapsed <= 180, elapsed)
        now = datetime.now().isoformat(timespec="microseconds")
        self.assertEqual(await self.db.get_followups(states=("ready",), due_at=now), [])
        later = (datetime.now() + timedelta(seconds=181)).isoformat(timespec="microseconds")
        self.assertEqual(len(await self.db.get_followups(states=("ready",), due_at=later)), 1)
        engine, manager = await self.tick_engine(account)
        await ReplyFollowups(engine).tick()
        manager.send_direct_message.assert_not_awaited()
        self.assertEqual((await self.db.get_followup_stats())["ready"], 1)
        # Once the delay has elapsed (and only then) the same row is delivered.
        async with _connect() as connection:
            past = (datetime.now() - timedelta(seconds=1)).isoformat(timespec="microseconds")
            await connection.execute("UPDATE message_followups SET ready_at=?", (past,))
            await connection.commit()
        await ReplyFollowups(engine).tick()
        manager.send_direct_message.assert_awaited_once()

    async def test_delayed_row_does_not_block_due_row_of_same_account(self):
        account, _ = await self.seed()
        await self.seed("bobby", account, user_id=456)
        await self.db.set_setting("task_status", "running")
        await self.db.receive_followup_reply(account["id"], 123, 11)
        await self.db.receive_followup_reply(account["id"], 456, 11)
        async with _connect() as connection:
            future = (datetime.now() + timedelta(hours=1)).isoformat(timespec="microseconds")
            await connection.execute("UPDATE message_followups SET ready_at=? WHERE username='alice'", (future,))
            await connection.commit()
        engine, manager = await self.tick_engine(account)
        await ReplyFollowups(engine).tick()
        manager.send_direct_message.assert_awaited_once()
        self.assertEqual((await self.db.get_followup_stats())["sent"], 1)

    async def test_minimum_gap_since_last_send_is_enforced(self):
        account = await self.ready_followup()
        engine, manager = await self.tick_engine(account)
        engine.interval_min = 60
        recent = datetime.now().isoformat()
        with patch.object(self.db, "last_message_time", new=AsyncMock(return_value=recent)):
            await ReplyFollowups(engine).tick()
        manager.send_direct_message.assert_not_awaited()
        old = (datetime.now() - timedelta(seconds=61)).isoformat()
        with patch.object(self.db, "last_message_time", new=AsyncMock(return_value=old)):
            await ReplyFollowups(engine).tick()
        manager.send_direct_message.assert_awaited_once()

    async def test_restriction_pauses_account_without_stopping_engine(self):
        account, _ = await self.seed()
        engine, _ = await self.tick_engine(account)
        followups = ReplyFollowups(engine)
        await followups.restriction(account["id"], FloodWaitError(request=None, capture=120))
        stored = await self.db.get_account(account["id"])
        self.assertEqual(stored["status"], "flood")
        remaining = (datetime.fromisoformat(stored["flood_until"]) - datetime.now()).total_seconds()
        self.assertTrue(60 < remaining <= 120, remaining)
        await followups.restriction(account["id"], PeerFloodError(request=None))
        stored = await self.db.get_account(account["id"])
        remaining = (datetime.fromisoformat(stored["flood_until"]) - datetime.now()).total_seconds()
        self.assertTrue(23 * 3600 < remaining <= 24 * 3600, remaining)
        self.assertEqual(stored["status"], "flood")
        await followups.restriction(account["id"], AuthKeyUnregisteredError(request=None))
        self.assertEqual((await self.db.get_account(account["id"]))["status"], "unauthorized")
        engine.stop.assert_not_called()

    async def test_peer_flood_and_uncertain_send_do_not_stop_engine(self):
        account = await self.ready_followup()
        engine, manager = await self.tick_engine(
            account, send_direct_message=AsyncMock(return_value={"success": False, "error": "peer_flood"})
        )
        await ReplyFollowups(engine).tick()
        stored = await self.db.get_account(account["id"])
        self.assertEqual(stored["status"], "flood")
        remaining = (datetime.fromisoformat(stored["flood_until"]) - datetime.now()).total_seconds()
        self.assertTrue(23 * 3600 < remaining <= 24 * 3600, remaining)
        self.assertEqual((await self.db.get_followup_stats())["ready"], 1)
        engine.stop.assert_not_called()
        # An unknown outcome is terminal for this row only.
        await self.db.update_account(account["id"], {"status": "active", "flood_until": None})
        manager.send_direct_message.return_value = {"success": False, "error": "uncertain"}
        logs = []
        engine.log_callback = lambda level, text: logs.append((level, text))
        await ReplyFollowups(engine).tick()
        self.assertEqual((await self.db.get_followup_stats())["uncertain"], 1)
        self.assertTrue(any(level == "ERROR" for level, _ in logs))
        engine.stop.assert_not_called()

    async def test_flood_during_history_poll_does_not_stop_engine_or_other_accounts(self):
        first, _ = await self.seed()
        second = await self.add_account(2)
        await self.seed("bobby", second, user_id=456)
        await self.db.set_setting("task_status", "running")
        await self.db.receive_followup_reply(second["id"], 456, 11)
        engine, flooded = await self.tick_engine(
            first, followup_history=AsyncMock(side_effect=FloodWaitError(request=None, capture=90))
        )
        ready = SimpleNamespace(
            followup_peer=AsyncMock(return_value=InputPeerUser(456, 456)),
            send_direct_message=AsyncMock(return_value={"success": True, "message_id": 31}),
        )
        engine.account_managers[second["id"]] = ready
        # Make the flooded account's waiting row the one polled.
        async with _connect() as connection:
            await connection.execute("UPDATE message_followups SET checked_at='' WHERE username='alice'")
            await connection.commit()
        await ReplyFollowups(engine).tick()
        self.assertEqual((await self.db.get_account(first["id"]))["status"], "flood")
        ready.send_direct_message.assert_awaited_once()
        engine.stop.assert_not_called()

    async def test_waiting_expires_after_ttl_with_reason(self):
        account, _ = await self.seed()
        await self.seed("bobby", account, user_id=456)
        async with _connect() as connection:
            old = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
            await connection.execute("UPDATE message_followups SET created_at=? WHERE username='alice'", (old,))
            await connection.commit()
        engine, manager = await self.tick_engine(account)
        await ReplyFollowups(engine).tick()
        states = {row["username"]: row["state"] for row in await self.db.get_followups(states=("waiting", "cancelled"))}
        self.assertEqual(states, {"alice": "cancelled", "bobby": "waiting"})
        self.assertTrue(any("Истекло ожидание ответа" in (row["message"] or "") for row in await self.db.get_logs()))
        engine.stop.assert_not_called()

    async def test_ttl_setting_is_clamped_and_respected(self):
        account, _ = await self.seed()
        async with _connect() as connection:
            old = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
            await connection.execute("UPDATE message_followups SET created_at=?", (old,))
            await connection.commit()
        await self.db.save_campaign_settings({"followup_wait_hours": "3"})
        engine, _ = await self.tick_engine(account)
        self.assertEqual(await ReplyFollowups(engine).expire(), 0)
        await self.db.save_campaign_settings({"followup_wait_hours": "1"})
        self.assertEqual(await ReplyFollowups(engine).expire(), 1)
        # Out-of-range and invalid values fall back to the allowed range.
        self.assertEqual(clamp_wait_hours("0"), 1)
        self.assertEqual(clamp_wait_hours("9999"), 168)
        self.assertEqual(clamp_wait_hours("bad"), 24)

class FollowupProtocolTests(DatabaseCase):
    async def test_peer_check_prevents_known_block_or_privacy_restriction(self):
        manager = AccountManager(account_payload())
        row = {"user_id": 123, "access_hash": 456}
        for flags in [{"blocked": True}, {"contact_require_premium": True}]:
            manager.client = AsyncMock(return_value=SimpleNamespace(full_user=SimpleNamespace(**flags)))
            self.assertIsNone(await manager.followup_peer(row))
        manager.client = AsyncMock(return_value=SimpleNamespace(full_user=SimpleNamespace(blocked=False)))
        self.assertEqual((await manager.followup_peer(row)).user_id, 123)

    async def test_missing_peer_check_result_never_assumes_writable(self):
        manager = AccountManager(account_payload())
        manager.client = AsyncMock(return_value=None)
        with self.assertRaises(RuntimeError):
            await manager.followup_peer({"user_id": 123, "access_hash": 456})

    async def test_reply_filter_excludes_outgoing_groups_services_and_old(self):
        self.assertTrue(is_followup_reply(incoming(), 123, 10))
        for message in [
            incoming(message_id=9),
            incoming(out=True),
            incoming(user_id=456),
            incoming(peer_id=PeerChannel(123)),
            incoming(action=object()),
        ]:
            self.assertFalse(is_followup_reply(message, 123, 10))

    async def test_random_id_mapping_confirms_first_message_id(self):
        manager = AccountManager(account_payload())
        manager.client = AsyncMock(
            return_value=SimpleNamespace(
                updates=[UpdateMessageID(id=99, random_id=3), UpdateMessageID(id=10, random_id=42)]
            )
        )
        self.assertEqual((await manager.send_direct_message(User(id=123), "Hi", 42))["message_id"], 10)

    async def test_history_is_scoped_and_ignores_old_and_outgoing(self):
        manager = AccountManager(account_payload())

        async def messages(*args, **kwargs):
            for message in [incoming(message_id=9), incoming(message_id=11, out=True), incoming(message_id=12)]:
                yield message

        manager.client = SimpleNamespace(iter_messages=Mock(side_effect=messages))
        result = await manager.followup_history(
            {"user_id": 123, "access_hash": 456, "checked_id": 10, "first_message_id": 10}
        )
        self.assertEqual(result, {"last_id": 12, "reply_id": 12})
        self.assertEqual(manager.client.iter_messages.call_args.kwargs, {"min_id": 10, "reverse": True, "limit": 30})
