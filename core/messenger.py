"""One shared, persistent recipient queue for all selected accounts."""

import asyncio
import random
from datetime import datetime, timedelta

from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    PeerFloodError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from core.account_manager import AccountManager
from core.followups import ReplyFollowups
from core.inviter import InviterEngine
from core.user_feedback import explain_error
from core.validation import validate_message


class MessageEngine(InviterEngine):
    async def start(self, accounts, text, interval_min, interval_max):
        validate_message(text)
        if self.running or not accounts or not 10 <= interval_min <= interval_max <= 86400:
            raise ValueError("Выберите аккаунты и интервал 10–86400 секунд")
        if await self.db.get_campaign_kind() != "messages":
            raise ValueError("Выберите кампанию личных сообщений")
        self.followup_text = (
            await self.db.get_setting("followup_text", "")
            if await self.db.get_setting("followup_enabled", "0") == "1"
            else None
        )
        if self.followup_text is not None:
            validate_message(self.followup_text)
        self.followups = None
        self._followup_failed = False
        self.running = not self._stop_requested
        self.paused = False
        self._task = asyncio.current_task()
        self._started_at = self.start_time = datetime.now()
        self._session_counts = {"added": 0, "sent": 0, "followup_sent": 0, "skipped": 0, "errors": 0}
        self._rr_index = 0
        streaks = {}
        self._streaks = streaks
        claimed = reserved = run_id = None
        outcome = "completed"
        try:
            if not self.running:
                return
            await self.db.recover_message_deliveries()
            await self.db.recover_followups()
            await self.db.reset_processing_to_pending()
            run_id = await self.db.begin_run("Личные сообщения")
            await self.db.reset_message_counters()
            self.max_consecutive = int(await self.db.get_setting("protection_consecutive", "5"))
            self.break_min = int(await self.db.get_setting("protection_break_min", "5"))
            self.break_max = int(await self.db.get_setting("protection_break_max", "15"))
            if min(self.max_consecutive, self.break_min) < 1 or self.break_max < self.break_min:
                raise ValueError("Некорректные настройки перерывов")
            for seed in accounts:
                if not self.running:
                    break
                account = await self.db.get_account(seed["id"])
                if (
                    not account
                    or not account.get("is_enabled")
                    or account.get("status") in {"restricted", "unauthorized", "inactive"}
                ):
                    continue
                manager = AccountManager(account, self.db)
                try:
                    await manager.connect()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await manager.disconnect()
                    is_unauthorized = isinstance(exc, AuthKeyUnregisteredError) or (
                        isinstance(exc, RuntimeError) and "Сессия не авторизована" in str(exc)
                    )
                    if is_unauthorized:
                        await self.db.update_account_status(account["id"], "unauthorized")
                    detail = explain_error(exc)
                    await self.db.add_log(account["phone"], None, "CONNECT_FAILED", detail)
                    self.log_callback("WARN", f"Аккаунт {account['phone']} пропущен: {detail}")
                    continue
                self.account_managers[account["id"]] = manager
                await self.db.add_log(account["phone"], None, "CONNECT", "Соединение готово к работе")
            if not self.account_managers and self.running:
                raise RuntimeError("Нет доступных авторизованных аккаунтов")
            if self.followup_text is not None and self.running:
                self.followups = ReplyFollowups(self)
                self.followups.attach()
            last_message = await self.db.last_message_time()
            if last_message and self.running:
                remaining = interval_min - (datetime.now() - datetime.fromisoformat(last_message)).total_seconds()
                if remaining > 0:
                    self.log_callback("INFO", "Сохраняется интервал после предыдущей отправки")
                    await self._wait(remaining)
            wait_reason = None
            while self.running:
                if self.paused:
                    await asyncio.sleep(0.25)
                    continue
                if not await self._is_within_schedule():
                    self.log_callback("INFO", "Вне расписания. Ожидание…")
                    await self._wait(60)
                    continue
                if self.followups:
                    await self.followups.tick()
                    if not self.running:
                        break
                stats = await self.db.get_targets_stats()
                if not stats["pending"]:
                    if self.followups and await self.followups.active():
                        if self.status_callback:
                            followup_stats = await self.db.get_followup_stats(account_ids=list(self.account_managers))
                            waiting_count = followup_stats.get("waiting", 0)
                            ready_count = followup_stats.get("ready", 0)
                            self.status_callback(
                                f"Первые сообщения завершены. Ждём ответ от {waiting_count} человек; "
                                f"вторых к отправке: {ready_count}. Приложение должно оставаться открытым"
                            )
                        await self._wait(1)
                        continue
                    break
                await self.db.reset_message_counters()
                account = await self._pick_available_account(accounts)
                if not account:
                    current = await self.db.get_accounts_by_ids(list(self.account_managers))
                    usable_accounts = [
                        a
                        for a in current
                        if a.get("is_enabled")
                        and a.get("status") not in {"restricted", "unauthorized", "inactive"}
                        and self.get_safe_daily_limit(a) > 0
                    ]
                    if not usable_accounts:
                        raise RuntimeError("Нет доступных аккаунтов: проверьте ограничения и дневные лимиты")
                    quota_available = any(
                        a.get("is_enabled")
                        and a.get("status") not in {"restricted", "unauthorized", "inactive"}
                        and int(a.get("sent_today") or 0) < self.get_safe_daily_limit(a)
                        for a in usable_accounts
                    )
                    reason = (
                        "Дневной лимит первых сообщений исчерпан. Задача ждёт его сброса; ответы для вторых сообщений продолжают отслеживаться."
                        if not quota_available
                        else "Аккаунты на паузе или перерыве. Задача подождёт и проверит доступность снова."
                    )
                    if reason != wait_reason:
                        self.log_callback("INFO", reason)
                        wait_reason = reason
                    if self.status_callback:
                        self.status_callback(reason)
                    await self._wait(30)
                    continue
                if wait_reason:
                    wait_reason = None
                    if self.status_callback:
                        self.status_callback("Выполняется...")
                if not self.running or self.paused:
                    continue
                target = await self.db.claim_next_pending_target()
                if not target:
                    continue
                claimed = target["id"]
                manager = self.account_managers[account["id"]]
                if await self.db.delivery_exists(target["username"]):
                    await self._skip_duplicate(target)
                    claimed = None
                    continue
                try:
                    user = await manager.resolve_recipient(target["username"])
                except (ValueError, UsernameInvalidError, UsernameNotOccupiedError):
                    await self.db.mark_target(
                        claimed, "not_found", error_message="Получатель не найден или не является пользователем"
                    )
                    self._session_counts["skipped"] += 1
                    await self.db.add_log(
                        account["phone"],
                        target["username"],
                        "MESSAGE_NOT_FOUND",
                        "Никнейм не найден или не принадлежит действующему пользователю; очередь продолжается",
                    )
                    self.log_callback("SKIP", f"@{target['username']} не найден; переходим к следующему получателю")
                    claimed = None
                    await self._progress()
                    continue
                except FloodWaitError as exc:
                    await self.db.mark_target(claimed, "pending")
                    claimed = None
                    await self.db.set_flood_wait(account["id"], max(1, exc.seconds))
                    self.log_callback("FLOOD", f"Telegram требует ожидание {exc.seconds} сек. Кампания остановлена.")
                    self.stop()
                    continue
                except (PeerFloodError, AuthKeyUnregisteredError) as exc:
                    await self.db.mark_target(claimed, "pending")
                    claimed = None
                    await self.db.update_account_status(
                        account["id"], "restricted" if isinstance(exc, PeerFloodError) else "unauthorized"
                    )
                    reason = (
                        "Telegram отклонил отправку с этого аккаунта"
                        if isinstance(exc, PeerFloodError)
                        else "Telegram отозвал сохранённый вход"
                    )
                    await self.db.add_log(account["phone"], target["username"], "MESSAGE_STOPPED", reason)
                    raise RuntimeError(f"{reason}. Кампания остановлена.") from None
                if not self.running or self.paused:
                    await self.db.mark_target(claimed, "pending")
                    claimed = None
                    continue
                random_id = await self.db.reserve_delivery(
                    claimed,
                    account["id"],
                    user.id,
                    followup_text=self.followup_text,
                    access_hash=getattr(user, "access_hash", None),
                )
                if random_id is None:
                    await self._skip_duplicate(target)
                    claimed = None
                    continue
                reserved = claimed
                if not self.running or self.paused:
                    await self.db.release_delivery(reserved)
                    reserved = None
                    await self.db.mark_target(claimed, "pending")
                    claimed = reserved = None
                    continue
                if self.status_callback:
                    self.status_callback(f"Сообщение @{target['username']} через {account['phone']}")
                result = await manager.send_direct_message(user, text, random_id)
                if result.get("success"):
                    await self.db.finish_delivery(reserved, "sent", result.get("message_id"))
                    claimed = reserved = None
                    self._session_counts["sent"] += 1
                    await self.db.add_log(account["phone"], target["username"], "MESSAGE_SENT", "Сообщение отправлено")
                    await self._record_streak(account["id"])
                elif result.get("error") == "private":
                    await self.db.finish_delivery(reserved, "private")
                    claimed = reserved = None
                    self._session_counts["skipped"] += 1
                    await self.db.add_log(
                        account["phone"],
                        target["username"],
                        "MESSAGE_PRIVATE",
                        "Получатель не принимает сообщения; повтор другим аккаунтом отключён",
                    )
                elif result.get("error") in {"flood", "peer_flood", "unauthorized"}:
                    await self.db.release_delivery(reserved)
                    reserved = None
                    await self.db.mark_target(claimed, "pending")
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
                    self.log_callback("WARN", f"{reason}. Кампания остановлена.")
                    await self.db.add_log(
                        account["phone"],
                        target["username"],
                        "MESSAGE_STOPPED",
                        f"{reason}. Получатель остался в очереди; кампания остановлена",
                    )
                    self.stop()
                else:
                    await self.db.finish_delivery(reserved, "uncertain")
                    claimed = reserved = None
                    self._session_counts["errors"] += 1
                    await self.db.add_log(
                        account["phone"],
                        target["username"],
                        "MESSAGE_UNCERTAIN",
                        "Результат неизвестен. Проверьте переписку вручную; автоматического повтора не будет",
                    )
                    self.log_callback("ERROR", "Результат отправки неизвестен. Проверьте переписку вручную.")
                    outcome = "failed"
                    self.stop()
                claimed = reserved = None
                await self._progress()
                if self.running and (await self.db.get_targets_stats())["pending"]:
                    await self._wait(random.uniform(interval_min, interval_max))
        except asyncio.CancelledError:
            outcome = "stopped"
            raise
        except Exception as exc:
            outcome = "failed"
            self.log_callback("ERROR", "Отправка остановлена: " + explain_error(exc))
            if self.fatal_callback:
                self.fatal_callback("Отправка остановлена. Проверьте журнал, аккаунты и состояние получателя.")
        finally:
            if self._followup_failed:
                outcome = "failed"
            if self._stop_requested and outcome != "failed":
                outcome = "stopped"
            try:
                if self.followups:
                    await self.followups.close()
                if reserved is not None:
                    await self.db.finish_delivery(reserved, "uncertain")
                    self._session_counts["errors"] += 1
                elif claimed is not None:
                    await self.db.mark_target(claimed, "pending")
                await self.db.set_setting("task_status", "stopped")
                cancelled_followups = await self.db.cancel_followups(
                    "Задача завершена или остановлена; второе сообщение отменено и после ответа не отправится"
                )
                followup_stats = await self.db.get_followup_stats()
                summary = {
                    **self._session_counts,
                    "outcome": outcome,
                    "duration": str(datetime.now() - self._started_at).split(".")[0],
                    "followup_waiting": sum(followup_stats.get(key, 0) for key in ("waiting", "ready")),
                    "followup_cancelled": cancelled_followups,
                }
                if run_id is not None:
                    await self.db.finish_run(run_id, summary)
                await self._progress()
                self.log_callback(
                    "INFO",
                    f"Первых сообщений: {summary['sent']}; вторых после ответа: {summary['followup_sent']}; "
                    f"пропущено: {summary['skipped']}; ошибок: {summary['errors']}",
                )
            finally:
                await self._disconnect_all()
                self.running = self.paused = False
                self._task = None
            if self.complete_callback:
                self.complete_callback(summary)

    async def _record_streak(self, account_id):
        self._streaks[account_id] = self._streaks.get(account_id, 0) + 1
        if self._streaks[account_id] >= self.max_consecutive:
            until = datetime.now() + timedelta(minutes=random.randint(self.break_min, self.break_max))
            await self.db.set_rotation_break(account_id, until.isoformat(timespec="seconds"))
            self._streaks[account_id] = 0

    async def _wait(self, seconds):
        if not getattr(self, "followups", None):
            return await super()._wait(seconds)
        remaining = max(0, seconds)
        while self.running and remaining > 0:
            if not self.paused:
                await self.followups.tick()
            chunk = min(0.25, remaining)
            await asyncio.sleep(chunk)
            if not self.paused:
                remaining -= chunk

    async def _skip_duplicate(self, target):
        await self.db.mark_target(
            target["id"], "already_sent", error_message="Получатель уже есть в общем журнале отправок"
        )
        self._session_counts["skipped"] += 1
        await self.db.add_log(
            None, target["username"], "SKIP", "Получатель уже есть в общем журнале личных сообщений; повтор отключён"
        )
        await self._progress()

    async def _progress(self):
        stats = await self.db.get_targets_stats()
        if self.progress_callback:
            self.progress_callback(stats["sent"], stats["total"])

    async def _pick_available_account(self, accounts, *, followup=False):
        now = datetime.now()
        for step in range(len(accounts)):
            index = (self._rr_index + step) % len(accounts)
            account = await self.db.get_account(accounts[index]["id"])
            if not account or account["id"] not in self.account_managers or not account.get("is_enabled"):
                continue
            if account.get("status") in {"restricted", "unauthorized", "inactive"}:
                continue
            if any(
                account.get(key) and datetime.fromisoformat(account[key]) > now
                for key in ("flood_until", "rotation_break_until")
            ):
                continue
            if account.get("status") == "flood":
                await self.db.update_account_status(account["id"], "active")
                account["status"] = "active"
            # The configured daily quota is the number of initial outreach
            # messages. A requested reply-triggered follow-up has its own
            # counter and must remain sendable once that quota is reached.
            if not followup and int(account.get("sent_today") or 0) >= self.get_safe_daily_limit(account):
                continue
            self._rr_index = (index + 1) % len(accounts)
            return account
        return None
