"""Reply detection and second-message delivery owned by the active campaign.

Delivery rules for a second message:
* it is sent only after the person's *new* reply, and never instantly: the reply
  stores ``ready_at`` (now + random 30-180 s, persisted in the database so a
  restart does not shorten it);
* it keeps at least ``engine.interval_min`` seconds after the last message sent;
* waiting rows older than ``followup_wait_hours`` expire ("Истекло ожидание ответа");
* waiting/ready rows survive an app restart (the app must be open to deliver; the
  database never cancels them on startup) and resume when the campaign is started;
* the recipient's first name is stored with the row so ``{имя}`` renders in the follow-up;
* Telegram limits (flood / peer flood / revoked login) pause only the affected
  account. They never stop the whole campaign.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

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
from core.followup_store import FOLLOWUP_WAIT_HOURS_DEFAULT, clamp_wait_hours
from core.inviter import PEER_FLOOD_COOLDOWN_SECONDS
from core.message_template import render_message
from core.user_feedback import explain_error

EXPIRE_CHECK_SECONDS = 30
# Reply polling: a small batch of the longest-unchecked waiting rows per tick, at most
# POLL_PER_ACCOUNT for each connected account, spaced out to stay flood-safe.
POLL_INTERVAL_SECONDS = 5
POLL_MAX_PER_TICK = 10
POLL_PER_ACCOUNT = 5
POLL_ROW_DELAY_SECONDS = 0.4
RESTRICTED_STATUSES = {"restricted", "inactive", "unauthorized"}

# An uncertain second message that the history shows as absent goes back to "ready" once, after a pause.
FOLLOWUP_VERIFY_REQUEUES = 1
FOLLOWUP_RETRY_DELAY_SECONDS = 30

# Dialog catch-up: replies that arrived while the app was offline. One account per tick, at most
# CATCHUP_DIALOG_LIMIT recent dialogs and CATCHUP_MAX_HISTORY extra history reads per pass, spaced out.
# The first pass for an account runs right after connect; later passes repeat every CATCHUP_INTERVAL_SECONDS
# (0 disables the periodic part).
CATCHUP_INTERVAL_SECONDS = 600
CATCHUP_DIALOG_LIMIT = 100
CATCHUP_MAX_HISTORY = 15
CATCHUP_HISTORY_DELAY_SECONDS = 0.5
CATCHUP_MAX_ROWS = 500


class ReplyFollowups:
    def __init__(self, engine):
        self.engine, self.db = engine, engine.db
        self.handlers = []
        self.callbacks = set()
        self.next_poll = self.next_tick = self.next_expire = 0
        self.wake = False
        self.closed = False
        self.verify_requeues = {}  # username -> requeues after an uncertain send absent from history
        self.retry_after = {}  # username -> monotonic time before which a requeued row is not resent
        self.catchup_next = {}  # account_id -> monotonic time of the next catch-up pass
        self.catchup_interval = CATCHUP_INTERVAL_SECONDS
        self.catchup_done = set()

    def attach(self):
        # Every connected account gets a catch-up pass as soon as the first tick runs.
        self.catchup_next = {account_id: 0 for account_id in self.engine.account_managers}
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

    async def expire(self):
        """Cancel waiting second messages that outlived ``followup_wait_hours``."""
        hours = clamp_wait_hours(await self.db.get_setting("followup_wait_hours", str(FOLLOWUP_WAIT_HOURS_DEFAULT)))
        expired = await self.db.expire_followups(hours)
        if expired:
            self.engine.log_callback(
                "INFO", f"Истекло ожидание ответа ({hours} ч): вторых сообщений отменено — {expired}"
            )
            await self.engine._progress()
        return expired

    async def tick(self):
        if self.closed or not self.engine.running or self.engine.paused:
            return
        now = time.monotonic()
        if now < self.next_tick and not self.wake:
            return
        self.next_tick, self.wake = now + 1, False
        if now >= self.next_expire:
            self.next_expire = now + EXPIRE_CHECK_SECONDS
            try:
                await self.expire()
            except Exception as exc:
                self.engine.log_callback("WARN", "Не удалось проверить срок ожидания ответов: " + explain_error(exc))
        if not await self.engine._is_within_schedule():
            return
        account_ids = list(self.engine.account_managers)
        await self._poll_waiting(now, account_ids)
        if not self.engine.running or self.engine.paused:
            return
        await self._catch_up(now, account_ids)
        if not self.engine.running or self.engine.paused:
            return
        await self._send_ready(account_ids)

    async def _account_usable(self, account):
        if (
            not account
            or not account.get("is_enabled")
            or account.get("status") in RESTRICTED_STATUSES
            or account.get("flood_until")
            and datetime.fromisoformat(account["flood_until"]) > datetime.now()
        ):
            return False
        return True

    async def _poll_waiting(self, now, account_ids):
        # A ready delivery can be deferred by its account's quota. Keep checking
        # other recipients for replies received while the app was closed.
        if now < self.next_poll:
            return
        # Oldest-checked rows first, a few per connected account, so every account's
        # queue advances without a burst of history requests.
        waiting = await self.db.get_followups(
            states=("waiting",),
            account_ids=account_ids,
            limit=POLL_MAX_PER_TICK,
            per_account=True,
            per_account_limit=POLL_PER_ACCOUNT,
        )
        if not waiting:
            return
        self.next_poll = now + POLL_INTERVAL_SECONDS
        accounts, restricted = {}, set()
        for index, row in enumerate(waiting):
            if not self.engine.running or self.engine.paused or self.closed:
                return
            account_id = row["account_id"]
            await self.db.followup_checked(row["username"], row["checked_id"])
            if account_id in restricted:
                continue
            if account_id not in accounts:
                accounts[account_id] = await self.db.get_account(account_id)
            if not await self._account_usable(accounts[account_id]):
                restricted.add(account_id)
                continue
            if index:
                await asyncio.sleep(POLL_ROW_DELAY_SECONDS)
            try:
                result = await self.engine.account_managers[account_id].followup_history(row)
                if result["reply_id"]:
                    await self.db.receive_followup_reply(account_id, row["user_id"], result["reply_id"])
                    await self.engine._progress()
                await self.db.followup_checked(row["username"], result["last_id"])
            except (UserIsBlockedError, UserPrivacyRestrictedError, YouBlockedUserError, ChatWriteForbiddenError):
                await self.db.block_followup(row["username"])
                await self.engine._progress()
            except (FloodWaitError, PeerFloodError, AuthKeyUnregisteredError) as exc:
                # Only this account stops polling for the tick; the others and the campaign continue.
                restricted.add(account_id)
                await self.restriction(account_id, exc)
            except Exception as exc:
                # Keep waiting on transient history failures; never invent a reply.
                self.engine.log_callback("WARN", f"Не удалось проверить ответ @{row['username']}: {explain_error(exc)}")

    @staticmethod
    def _oldest_first_message(rows):
        """Local time of the oldest first message among ``rows`` (minus a margin), or None."""
        oldest = None
        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"])
            except (KeyError, TypeError, ValueError):
                return None  # unknown age: do not cut the dialog scan short
            oldest = created if oldest is None else min(oldest, created)
        return oldest - timedelta(minutes=10) if oldest else None

    async def _catch_up(self, now, account_ids):
        """Find replies missed while offline by scanning recent private dialogs (one account per tick).

        Runs once right after connect and then every ``catchup_interval`` seconds. The scan is bounded
        (dialog count, history reads, spacing) so it stays flood-safe, and a Telegram limit only pauses
        the affected account. Found replies go through the same ``receive_followup_reply`` path as live
        and polled ones.
        """
        due = [a for a in account_ids if a in self.catchup_next and now >= self.catchup_next[a]]
        if not due:
            return
        account_id = due[0]
        interval = self.catchup_interval
        self.catchup_next[account_id] = now + interval if interval and interval > 0 else float("inf")
        manager = self.engine.account_managers[account_id]
        catchup = getattr(manager, "followup_catchup", None)
        if catchup is None:
            return
        account = await self.db.get_account(account_id)
        if not await self._account_usable(account):
            self.catchup_next[account_id] = now + 60  # try again soon, once the account is usable
            return
        rows = await self.db.get_followups(states=("waiting",), account_ids=[account_id], limit=CATCHUP_MAX_ROWS)
        if not rows:
            return
        phone = account.get("phone", account_id)
        try:
            result = await catchup(
                rows,
                dialog_limit=CATCHUP_DIALOG_LIMIT,
                max_history=CATCHUP_MAX_HISTORY,
                history_delay=CATCHUP_HISTORY_DELAY_SECONDS,
                since=self._oldest_first_message(rows),
            )
        except (FloodWaitError, PeerFloodError, AuthKeyUnregisteredError) as exc:
            await self.restriction(account_id, exc)
            return
        except Exception as exc:
            # Never invent a reply; the 1-by-1 history poll keeps working.
            self.engine.log_callback(
                "WARN", f"Не удалось проверить пропущенные ответы (аккаунт {phone}): {explain_error(exc)}"
            )
            return
        by_user = {row["user_id"]: row for row in rows}
        found = 0
        for user_id, reply_id in (result.get("replies") or {}).items():
            row = by_user.get(user_id)
            if row and await self.db.receive_followup_reply(account_id, user_id, reply_id):
                found += 1
                self.engine.log_callback(
                    "INFO", f"Найден пропущенный ответ @{row['username']} (получен, пока приложение было закрыто)"
                )
        for user_id, last_id in (result.get("checked") or {}).items():
            row = by_user.get(user_id)
            if row and isinstance(last_id, int):
                await self.db.followup_checked(row["username"], last_id)
        if found:
            self.wake = True
            await self.engine._progress()
        if found or account_id not in self.catchup_done:
            self.catchup_done.add(account_id)
            self.engine.log_callback(
                "INFO",
                f"Проверка пропущенных ответов (аккаунт {phone}): диалогов просмотрено {result.get('dialogs', 0)}, "
                f"запросов истории {result.get('history', 0)}, найдено ответов {found}",
            )

    async def _gap_elapsed(self):
        """Keep the campaign's minimum interval since the most recent message sent."""
        gap = float(getattr(self.engine, "interval_min", 0) or 0)
        if gap <= 0:
            return True
        last = await self.db.last_message_time()
        if not last:
            return True
        return (datetime.now() - datetime.fromisoformat(last)).total_seconds() >= gap

    async def _send_ready(self, account_ids):
        await self.db.reset_message_counters()
        due_at = datetime.now().isoformat(timespec="microseconds")
        rows = await self.db.get_followups(
            states=("ready",), account_ids=account_ids, limit=100, per_account=True, due_at=due_at
        )
        if not rows or not await self._gap_elapsed():
            return
        for row in rows:
            if time.monotonic() < self.retry_after.get(row["username"], 0):
                continue  # requeued after an unconfirmed send: wait before the second attempt
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
                continue
            except Exception as exc:
                self.engine.log_callback(
                    "WARN", f"Не удалось проверить переписку @{row['username']}: {explain_error(exc)}"
                )
                continue
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
                # {имя} uses the first name stored at the first delivery («друг» when unknown).
                text = render_message(claimed["text"], claimed.get("first_name") or getattr(peer, "first_name", None))
                if not text.strip() or len(text.encode("utf-16-le")) // 2 > 4096:
                    text = claimed["text"]
                send_started = datetime.now(timezone.utc)
                result = await manager.send_direct_message(peer, text, claimed["random_id"])
                error = result.get("error")
                if result.get("success"):
                    await self.db.finish_followup(row["username"], "sent", result.get("message_id"))
                    self.verify_requeues.pop(row["username"], None)
                    self.engine._session_counts["followup_sent"] += 1
                    self.engine.log_callback("SUCCESS", f"Второе сообщение @{row['username']} отправлено после ответа")
                    await self.engine._record_streak(account["id"])
                elif error == "uncertain":
                    # Timeout / empty answer / server error: the message may or may not have been stored.
                    await self._settle_uncertain(row, account, manager, peer, text, claimed["random_id"], send_started)
                elif error == "private":
                    await self.db.finish_followup(row["username"], "blocked")
                elif error in {"flood", "peer_flood", "unauthorized"}:
                    # Only this account pauses; the row stays ready for a later attempt.
                    await self.db.finish_followup(row["username"], "ready")
                    if error == "flood":
                        seconds = max(1, int(result.get("flood_seconds") or 1))
                        await self.db.set_flood_wait(account["id"], seconds)
                        reason = f"Telegram просит подождать {seconds} сек"
                    elif error == "peer_flood":
                        await self.db.set_flood_wait(account["id"], PEER_FLOOD_COOLDOWN_SECONDS)
                        reason = (
                            "Telegram отклонил отправку с этого аккаунта; "
                            f"пауза {PEER_FLOOD_COOLDOWN_SECONDS // 3600} ч"
                        )
                    else:
                        await self.db.update_account_status(account["id"], "unauthorized")
                        reason = "Telegram отозвал сохранённый вход"
                    self.engine.log_callback(
                        "WARN",
                        f"Аккаунт {account['phone']}: {reason}. Второе сообщение отложено, кампания продолжается",
                    )
                    await self.engine._progress()
                    continue
                else:
                    # Unknown outcome: never retry this row, but other work continues.
                    await self.db.finish_followup(row["username"], "uncertain")
                    self.engine._session_counts["errors"] += 1
                    self.engine._followup_failed = True
                    self.engine.log_callback(
                        "ERROR",
                        f"Исход второго сообщения @{row['username']} неизвестен. "
                        "Автоматического повтора не будет; проверьте переписку вручную",
                    )
                await self.engine._progress()
            except BaseException:
                # A failed/cancelled RPC or commit must never cause a second automatic attempt.
                await self.db.recover_followups()
                raise
            return

    async def _verify_followup(self, manager, peer, text, random_id, since):
        """Read the chat history after an uncertain send, like the first message does.

        Returns ``{"found": True, "message_id"}``, ``{"found": False}`` (history read, message absent
        after a short wait) or ``{"found": None}`` (history could not be read).
        """
        verify = getattr(manager, "verify_sent_message", None)
        if verify is None:
            return {"found": None}
        reading_failed = False
        for attempt in range(getattr(self.engine, "VERIFY_ATTEMPTS", 2)):
            if attempt:
                await asyncio.sleep(getattr(self.engine, "VERIFY_DELAY", 3.0))
            if self.closed or (not self.engine.running and getattr(self.engine, "_stop_requested", False)):
                return {"found": None}
            try:
                verdict = await verify(peer, text, random_id, since)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - verification must never break the campaign
                verdict = {"found": None}
            found = verdict.get("found") if isinstance(verdict, dict) else None
            if found is True:
                return {"found": True, "message_id": verdict.get("message_id")}
            if found is None:
                reading_failed = True
        return {"found": None} if reading_failed else {"found": False}

    async def _settle_uncertain(self, row, account, manager, peer, text, random_id, since):
        """Resolve an uncertain second send from the history.

        Found -> sent. Absent -> ``ready`` again once (after a pause), then ``uncertain``.
        Unreadable -> ``uncertain`` right away (a blind retry could duplicate the message).
        Only this row is affected; the campaign keeps running.
        """
        username = row["username"]
        verdict = await self._verify_followup(manager, peer, text, random_id, since)
        if verdict["found"] is True:
            self.verify_requeues.pop(username, None)
            await self.db.finish_followup(
                username,
                "sent",
                verdict.get("message_id"),
                detail=(
                    "FOLLOWUP_SENT",
                    "Второе сообщение отправлено (подтверждено по истории переписки после неопределённого ответа)",
                ),
            )
            self.engine._session_counts["followup_sent"] += 1
            self.engine.log_callback(
                "INFO", f"@{username}: ответ Telegram на второе сообщение был неопределённым, но сообщение найдено"
            )
            await self.engine._record_streak(account["id"])
            return
        if verdict["found"] is False:
            requeues = self.verify_requeues.get(username, 0)
            if requeues < FOLLOWUP_VERIFY_REQUEUES:
                self.verify_requeues[username] = requeues + 1
                self.retry_after[username] = time.monotonic() + FOLLOWUP_RETRY_DELAY_SECONDS
                await self.db.finish_followup(
                    username,
                    "ready",
                    detail=(
                        "FOLLOWUP_RETRY",
                        "Неопределённый ответ, в переписке второго сообщения нет; повтор позже",
                    ),
                )
                self.engine.log_callback(
                    "WARN",
                    f"@{username}: второе сообщение не доставлено (проверено по истории). "
                    "Повтор позже, кампания продолжается",
                )
                return
            self.verify_requeues.pop(username, None)
            await self.db.finish_followup(
                username,
                "uncertain",
                detail=(
                    "FOLLOWUP_UNCERTAIN",
                    "Повторная отправка второго сообщения тоже не подтверждена; в переписке его нет. Повтора не будет",
                ),
            )
            self.engine._session_counts["errors"] += 1
            self.engine._followup_failed = True
            self.engine.log_callback(
                "ERROR",
                f"@{username}: второе сообщение не подтверждено после повтора. "
                "Автоматического повтора не будет; проверьте переписку вручную",
            )
            return
        # Could not read the history: never retry blindly.
        self.verify_requeues.pop(username, None)
        await self.db.finish_followup(username, "uncertain")
        self.engine._session_counts["errors"] += 1
        self.engine._followup_failed = True
        self.engine.log_callback(
            "ERROR",
            f"Исход второго сообщения @{username} неизвестен: историю переписки проверить не удалось. "
            "Автоматического повтора не будет; проверьте переписку вручную",
        )

    async def restriction(self, account_id, exc):
        """Pause only the affected account; the campaign and other accounts keep running."""
        if isinstance(exc, FloodWaitError):
            await self.db.set_flood_wait(account_id, max(1, exc.seconds))
        elif isinstance(exc, PeerFloodError):
            await self.db.set_flood_wait(account_id, PEER_FLOOD_COOLDOWN_SECONDS)
        else:
            await self.db.update_account_status(account_id, "unauthorized")
        self.engine.log_callback(
            "WARN", explain_error(exc) + " Аккаунт поставлен на паузу; второе сообщение отложено, кампания продолжается"
        )
