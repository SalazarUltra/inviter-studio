"""Reply detection and second-message delivery owned by the active campaign."""

import asyncio
import time
from datetime import datetime

from telethon import events
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerFloodError,
    UserIsBlockedError,
    UserPrivacyRestrictedError,
    YouBlockedUserError,
)

from core.account_manager import is_followup_reply
from core.user_feedback import explain_error


class ReplyFollowups:
    def __init__(self, engine):
        self.engine, self.db = engine, engine.db
        self.handlers = []
        self.callbacks = set()
        self.next_poll = self.next_tick = 0
        self.wake = False
        self.closed = False

    def attach(self):
        for account_id, manager in self.engine.account_managers.items():

            async def handler(event, account=account_id):
                task = asyncio.current_task()
                self.callbacks.add(task)
                try:
                    if self.closed or not self.engine.running or not event.is_private:
                        return
                    message = event.message
                    user_id = getattr(message, "sender_id", None)
                    if user_id and is_followup_reply(message, user_id, 1):
                        if await self.db.receive_followup_reply(account, user_id, message.id):
                            self.wake = True
                            await self.engine._progress()
                except Exception as exc:
                    self.engine.log_callback(
                        "ERROR", "Ошибка учёта ответа: " + explain_error(exc) + " Ожидание сохранено"
                    )
                finally:
                    self.callbacks.discard(task)

            manager.client.add_event_handler(handler, events.NewMessage(incoming=True))
            self.handlers.append((manager.client, handler))

    async def close(self):
        self.closed = True
        for client, handler in self.handlers:
            client.remove_event_handler(handler)
        if self.callbacks:
            await asyncio.gather(*list(self.callbacks), return_exceptions=True)

    async def active(self):
        # Only keep the run alive for work one of its connected accounts owns.
        # Preflight explains any remaining rows whose owner was not selected.
        return bool(await self.db.get_followups(account_ids=list(self.engine.account_managers), limit=1))

    async def tick(self):
        if self.closed or not self.engine.running or self.engine.paused:
            return
        now = time.monotonic()
        if now < self.next_tick and not self.wake:
            return
        self.next_tick, self.wake = now + 1, False
        if not await self.engine._is_within_schedule():
            return
        account_ids = list(self.engine.account_managers)
        waiting = await self.db.get_followups(states=("waiting",), account_ids=account_ids, limit=1)
        # A ready delivery can be deferred by its account's quota. Keep checking
        # other recipients for replies received while the app was closed.
        if waiting and now >= self.next_poll:
            self.next_poll = now + 5
            row = waiting[0]
            await self.db.followup_checked(row["username"], row["checked_id"])
            account = await self.db.get_account(row["account_id"])
            if (
                not account
                or not account.get("is_enabled")
                or account.get("status") in {"restricted", "inactive", "unauthorized"}
                or account.get("flood_until")
                and datetime.fromisoformat(account["flood_until"]) > datetime.now()
            ):
                return
            try:
                result = await self.engine.account_managers[row["account_id"]].followup_history(row)
                if result["reply_id"]:
                    await self.db.receive_followup_reply(row["account_id"], row["user_id"], result["reply_id"])
                    await self.engine._progress()
                await self.db.followup_checked(row["username"], result["last_id"])
            except (UserIsBlockedError, UserPrivacyRestrictedError, YouBlockedUserError, ChatWriteForbiddenError):
                await self.db.block_followup(row["username"])
                await self.engine._progress()
            except (FloodWaitError, PeerFloodError, AuthKeyUnregisteredError) as exc:
                await self.restriction(row["account_id"], exc)
                return
            except Exception as exc:
                # Keep waiting on transient history failures; never invent a reply.
                self.engine.log_callback("WARN", f"Не удалось проверить ответ @{row['username']}: {explain_error(exc)}")
        if not self.engine.running or self.engine.paused:
            return
        await self.db.reset_message_counters()
        for row in await self.db.get_followups(states=("ready",), account_ids=account_ids, limit=100, per_account=True):
            account = await self.db.get_account(row["account_id"])
            if not account:
                continue
            rr_index = self.engine._rr_index
            try:
                available = await self.engine._pick_available_account([account], followup=True)
            finally:
                self.engine._rr_index = rr_index
            if not available:
                continue
            manager = self.engine.account_managers[account["id"]]
            try:
                peer = await manager.followup_peer(row)
            except (UserIsBlockedError, UserPrivacyRestrictedError, YouBlockedUserError, ChatWriteForbiddenError):
                await self.db.block_followup(row["username"])
                await self.engine._progress()
                continue
            except (FloodWaitError, PeerFloodError, AuthKeyUnregisteredError) as exc:
                await self.restriction(account["id"], exc)
                return
            except Exception as exc:
                self.engine.log_callback(
                    "WARN", f"Не удалось проверить переписку @{row['username']}: {explain_error(exc)}"
                )
                return
            if peer is None:
                await self.db.block_followup(row["username"])
                await self.engine._progress()
                continue
            if not self.engine.running or self.engine.paused or not await self.engine._is_within_schedule():
                return
            claimed = await self.db.claim_followup(row["username"])
            if not claimed:
                continue
            try:
                # Recheck after reservation; a stop here must not start another RPC.
                if not self.engine.running or self.engine.paused:
                    await self.db.finish_followup(row["username"], "ready")
                    return
                result = await manager.send_direct_message(peer, claimed["text"], claimed["random_id"])
                if result.get("success"):
                    await self.db.finish_followup(row["username"], "sent", result.get("message_id"))
                    self.engine._session_counts["followup_sent"] += 1
                    self.engine.log_callback("SUCCESS", f"Второе сообщение @{row['username']} отправлено после ответа")
                    await self.engine._record_streak(account["id"])
                elif result.get("error") == "private":
                    await self.db.finish_followup(row["username"], "blocked")
                elif result.get("error") in {"flood", "peer_flood", "unauthorized"}:
                    await self.db.finish_followup(row["username"], "ready")
                    if result["error"] == "flood":
                        await self.db.set_flood_wait(account["id"], result["flood_seconds"])
                    else:
                        await self.db.update_account_status(
                            account["id"], "restricted" if result["error"] == "peer_flood" else "unauthorized"
                        )
                    if result["error"] == "flood":
                        reason = f"Telegram просит подождать {result['flood_seconds']} сек"
                    elif result["error"] == "peer_flood":
                        reason = "Telegram отклонил отправку с этого аккаунта"
                    else:
                        reason = "Telegram отозвал сохранённый вход"
                    self.engine.log_callback("WARN", f"{reason}. Кампания остановлена; второе сообщение отложено")
                    self.engine.stop()
                else:
                    await self.db.finish_followup(row["username"], "uncertain")
                    self.engine._session_counts["errors"] += 1
                    self.engine._followup_failed = True
                    self.engine.log_callback(
                        "ERROR", "Исход второго сообщения неизвестен. Автоматического повтора не будет"
                    )
                    self.engine.stop()
                await self.engine._progress()
            except BaseException:
                # A failed/cancelled RPC or commit must never cause a second automatic attempt.
                await self.db.recover_followups()
                raise
            return

    async def restriction(self, account_id, exc):
        if isinstance(exc, FloodWaitError):
            await self.db.set_flood_wait(account_id, max(1, exc.seconds))
        else:
            await self.db.update_account_status(
                account_id, "restricted" if isinstance(exc, PeerFloodError) else "unauthorized"
            )
        self.engine.log_callback("WARN", explain_error(exc) + " Кампания остановлена; второе сообщение отложено")
        self.engine.stop()
