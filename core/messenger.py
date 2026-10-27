"""One shared, persistent recipient queue for all selected accounts."""

import asyncio
import random
from datetime import datetime, timedelta, timezone

from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    PeerFloodError,
    RPCError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)

from core.account_manager import AccountManager, classify_recipient_rpc, rpc_error_text
from core.followups import ReplyFollowups
from core.inviter import PEER_FLOOD_COOLDOWN_SECONDS, InviterEngine
from core.message_template import SAMPLE_NAME, render_message
from core.user_feedback import explain_error
from core.validation import validate_message


class MessageEngine(InviterEngine):
    PACE_MODE = "messages"
    # Consecutive transient resolve failures across *different* recipients (network outage)
    # before the whole run stops.
    MAX_TRANSIENT_FAILURES = 5
    # Transient resolve failures for the SAME recipient before that recipient is marked "error".
    MAX_RESOLVE_RETRIES = 3
    # Set by shutdown(): app close must not cancel durable waiting/ready second messages.
    keep_followups = False

    def shutdown(self) -> None:
        """Stop because the application is closing; waiting follow-ups survive for the next start."""
        self.keep_followups = True
        self.stop()

    # Uncertain send, history clearly shows nothing was delivered: requeue this many times, then "error".
    MAX_VERIFY_REQUEUES = 1
    # History checks after an uncertain send (the second one follows a short wait).
    VERIFY_ATTEMPTS = 2
    VERIFY_DELAY = 3.0

    async def start(self, accounts, text, interval_min, interval_max):
        validate_message(text)
        validate_message(render_message(text, SAMPLE_NAME))
        if self.running:
            raise RuntimeError("Задача уже выполняется")
        if not accounts or not 10 <= interval_min <= interval_max <= 86400:
            raise ValueError("Выберите аккаунты и интервал 10–86400 секунд")
        # Claim the engine synchronously: a concurrent start() must not pass the
        # guard above across the awaits inside the try block.
        self.running = True
        self._task = asyncio.current_task()
        self.paused = False
        self._started_at = self.start_time = datetime.now()
        self._session_counts = {"added": 0, "sent": 0, "followup_sent": 0, "skipped": 0, "errors": 0}
        self._rr_index = 0
        self.interval_min = interval_min  # follow-ups keep the same minimum gap since the last send
        self._streaks = {}
        self._resolve_retries = {}
        self._verify_requeues = {}
        self.followup_text = None
        self.followups = None
        self._followup_failed = False
        claimed = reserved = run_id = None
        outcome = "completed"
        try:
            if self._stop_requested:
                return
            if await self.db.get_campaign_kind() != "messages":
                raise ValueError("Выберите кампанию личных сообщений")
            self.followup_text = (
                await self.db.get_setting("followup_text", "")
                if await self.db.get_setting("followup_enabled", "0") == "1"
                else None
            )
            if self.followup_text is not None:
                validate_message(render_message(validate_message(self.followup_text), SAMPLE_NAME))
            await self.db.recover_message_deliveries()
            await self.db.recover_followups()
            await self.db.reset_processing_to_pending()
            run_id = await self.db.begin_run("Личные сообщения")
            await self.db.reset_message_counters()
            await self._load_daily_cap()
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
            transient_failures = 0
            last_transient_target = None
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
                if await self._daily_cap_hold():
                    continue
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
                    long_until = self._long_cooldown_until(usable_accounts)
                    if long_until:
                        raise RuntimeError(
                            "Все доступные аккаунты на длительной паузе Telegram до "
                            + long_until.strftime("%d.%m %H:%M")
                            + ". Запустите задачу позже; получатели остались в очереди"
                        )
                    quota_available = any(
                        a.get("is_enabled")
                        and a.get("status") not in {"restricted", "unauthorized", "inactive"}
                        and self.daily_used(a) < self.get_safe_daily_limit(a)
                        for a in usable_accounts
                    )
                    reason = (
                        "Дневной лимит первых сообщений исчерпан (он общий с вторыми). "
                        "Задача ждёт его сброса; ответы на отправленные сообщения продолжают отслеживаться."
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
                    transient_failures, last_transient_target = 0, None
                    self._resolve_retries.pop(target["id"], None)
                    claimed = None
                    await self._finish_not_found(
                        account,
                        target,
                        "Никнейм не найден или не принадлежит действующему пользователю; очередь продолжается",
                    )
                    continue
                except FloodWaitError as exc:
                    await self.db.mark_target(claimed, "pending")
                    claimed = None
                    await self._pause_account(account, target, "flood", max(1, exc.seconds))
                    continue
                except (PeerFloodError, AuthKeyUnregisteredError) as exc:
                    await self.db.mark_target(claimed, "pending")
                    claimed = None
                    await self._pause_account(
                        account, target, "peer_flood" if isinstance(exc, PeerFloodError) else "unauthorized"
                    )
                    continue
                except (asyncio.TimeoutError, ConnectionError, OSError, RPCError) as exc:
                    if isinstance(exc, RPCError) and (
                        classify_recipient_rpc(exc) is not None or getattr(exc, "code", None) == 400
                    ):
                        # Permanent recipient-level rejection (invalid peer, deactivated, ...).
                        transient_failures, last_transient_target = 0, None
                        self._resolve_retries.pop(target["id"], None)
                        claimed = None
                        await self._finish_not_found(
                            account,
                            target,
                            f"Telegram отклонил получателя ({rpc_error_text(exc)}); очередь продолжается",
                        )
                        continue
                    # Transient network/RPC failure: retried per recipient. One recipient that keeps
                    # failing is marked "error" without burning the whole campaign; only repeated
                    # failures across different recipients (an outage) stop the run safely.
                    target_id = target["id"]
                    retries = self._resolve_retries.get(target_id, 0) + 1
                    detail = explain_error(exc)
                    if target_id != last_transient_target:
                        transient_failures += 1
                        last_transient_target = target_id
                    outage = transient_failures >= self.MAX_TRANSIENT_FAILURES
                    if retries >= self.MAX_RESOLVE_RETRIES:
                        self._resolve_retries.pop(target_id, None)
                        await self.db.mark_target(
                            claimed,
                            "error",
                            error_message=f"Не удалось проверить получателя после {retries} попыток: {detail}",
                        )
                        claimed = None
                        self._session_counts["errors"] += 1
                        await self.db.add_log(
                            account["phone"],
                            target["username"],
                            "RESOLVE_FAILED",
                            f"Не удалось проверить получателя за {retries} попыток ({detail}); получатель пропущен",
                        )
                        self.log_callback(
                            "ERROR",
                            f"@{target['username']}: {retries} попытки не удались ({detail}); "
                            "получатель пропущен, очередь продолжается",
                        )
                        await self._progress()
                        if not outage:
                            await self._wait(random.uniform(2, 5))
                    else:
                        self._resolve_retries[target_id] = retries
                        await self.db.mark_target(claimed, "pending")
                        claimed = None
                        await self.db.add_log(
                            account["phone"],
                            target["username"],
                            "RESOLVE_RETRY",
                            f"Временная ошибка сети или Telegram ({detail}); получатель остался в очереди",
                        )
                        self.log_callback(
                            "WARN",
                            f"@{target['username']}: временная ошибка ({detail}). Повтор позже "
                            f"({retries}/{self.MAX_RESOLVE_RETRIES})",
                        )
                        if not outage:
                            await self._wait(min(30, 5 * retries))
                    if outage:
                        raise RuntimeError(
                            "Несколько ошибок сети подряд. Проверьте интернет и прокси; получатели остались в очереди."
                        ) from None
                    continue
                transient_failures, last_transient_target = 0, None
                self._resolve_retries.pop(target["id"], None)
                if not self.running or self.paused:
                    await self.db.mark_target(claimed, "pending")
                    claimed = None
                    continue
                try:
                    rendered = validate_message(render_message(text, getattr(user, "first_name", None)))
                except ValueError as exc:
                    await self.db.mark_target(
                        claimed,
                        "error",
                        error_message=f"Текст после подстановки {{имя}} и вариантов не подходит: {exc}",
                    )
                    claimed = None
                    self._session_counts["errors"] += 1
                    self.log_callback("ERROR", f"@{target['username']}: текст после подстановки некорректен ({exc})")
                    await self._progress()
                    continue
                random_id = await self.db.reserve_delivery(
                    claimed,
                    account["id"],
                    user.id,
                    followup_text=self.followup_text,
                    access_hash=getattr(user, "access_hash", None),
                    first_name=getattr(user, "first_name", None),
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
                send_started = datetime.now(timezone.utc)
                result = await manager.send_direct_message(user, rendered, random_id)
                if result.get("success"):
                    await self.db.finish_delivery(reserved, "sent", result.get("message_id"))
                    claimed = reserved = None
                    self._session_counts["sent"] += 1
                    self._daily_done = await self.db.add_campaign_daily_success()
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
                elif result.get("error") in {"skip", "error"}:
                    # Definite Telegram rejection: the message was not sent. Only this recipient
                    # is affected; the campaign continues.
                    detail = str(result.get("message") or "")
                    await self.db.release_delivery(reserved)
                    reserved = None
                    if result["error"] == "skip":
                        await self.db.mark_target(
                            claimed,
                            "not_found",
                            error_message=detail or "Аккаунт получателя удалён или недоступен",
                        )
                        self._session_counts["skipped"] += 1
                        await self.db.add_log(
                            account["phone"],
                            target["username"],
                            "MESSAGE_SKIP",
                            f"Получатель недоступен ({detail}); очередь продолжается",
                        )
                        self.log_callback("SKIP", f"@{target['username']} недоступен ({detail}); идём дальше")
                    else:
                        await self.db.mark_target(
                            claimed,
                            "error",
                            error_message=f"Telegram отклонил отправку: {detail}" if detail else "Отправка отклонена",
                        )
                        self._session_counts["errors"] += 1
                        await self.db.add_log(
                            account["phone"],
                            target["username"],
                            "MESSAGE_ERROR",
                            f"Telegram отклонил отправку ({detail}); сообщение не отправлено, очередь продолжается",
                        )
                        self.log_callback(
                            "ERROR", f"@{target['username']}: Telegram отклонил отправку ({detail}); идём дальше"
                        )
                elif result.get("error") in {"flood", "peer_flood", "unauthorized"}:
                    await self.db.release_delivery(reserved)
                    reserved = None
                    await self.db.mark_target(claimed, "pending")
                    await self._pause_account(account, target, result["error"], result.get("flood_seconds"))
                else:
                    # Unknown outcome (timeout, empty answer, 5xx, dropped connection): look at the
                    # chat history before deciding. One uncertain recipient never stops the run.
                    verdict = await self._verify_uncertain_send(
                        manager, user, rendered, random_id, send_started, result
                    )
                    if verdict["found"] is True:
                        await self.db.finish_delivery(reserved, "sent", verdict.get("message_id"))
                        claimed = reserved = None
                        self._verify_requeues.pop(target["id"], None)
                        self._session_counts["sent"] += 1
                        await self.db.add_log(
                            account["phone"],
                            target["username"],
                            "MESSAGE_SENT",
                            "Сообщение отправлено (подтверждено по истории переписки после неопределённого ответа)",
                        )
                        self.log_callback(
                            "INFO", f"@{target['username']}: ответ Telegram был неопределённым, но сообщение найдено"
                        )
                        await self._record_streak(account["id"])
                    elif verdict["found"] is False:
                        # History was read and the message is clearly absent: nothing was delivered.
                        await self.db.release_delivery(reserved)
                        reserved = None
                        requeues = self._verify_requeues.get(target["id"], 0)
                        if requeues < self.MAX_VERIFY_REQUEUES:
                            self._verify_requeues[target["id"]] = requeues + 1
                            await self.db.mark_target(claimed, "pending")
                            claimed = None
                            await self.db.add_log(
                                account["phone"],
                                target["username"],
                                "MESSAGE_RETRY",
                                "Неопределённый ответ, в переписке сообщения нет; получатель возвращён в очередь",
                            )
                            self.log_callback(
                                "WARN",
                                f"@{target['username']}: сообщение не доставлено (проверено по истории). "
                                "Повтор позже, кампания продолжается",
                            )
                        else:
                            self._verify_requeues.pop(target["id"], None)
                            await self.db.mark_target(
                                claimed,
                                "error",
                                error_message="Отправка не подтверждена: Telegram не ответил, сообщения в переписке нет",
                            )
                            claimed = None
                            self._session_counts["errors"] += 1
                            await self.db.add_log(
                                account["phone"],
                                target["username"],
                                "MESSAGE_ERROR",
                                "Повторная отправка тоже не подтверждена; сообщения в переписке нет",
                            )
                            self.log_callback("ERROR", f"@{target['username']}: сообщение не доставлено; идём дальше")
                    else:
                        # Could not verify either way: never auto-retry (would risk a duplicate),
                        # but keep the campaign and its follow-ups running.
                        await self.db.finish_delivery(reserved, "uncertain")
                        claimed = reserved = None
                        self._verify_requeues.pop(target["id"], None)
                        self._session_counts["errors"] += 1
                        await self.db.add_log(
                            account["phone"],
                            target["username"],
                            "MESSAGE_UNCERTAIN",
                            "Результат неизвестен. Проверьте переписку вручную; автоматического повтора не будет",
                        )
                        self.log_callback(
                            "ERROR",
                            f"@{target['username']}: результат отправки неизвестен. "
                            "Проверьте переписку вручную; кампания продолжается",
                        )
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
            summary = {
                **self._session_counts,
                "outcome": outcome,
                "duration": str(datetime.now() - self._started_at).split(".")[0],
                "followup_waiting": 0,
                "followup_cancelled": 0,
            }
            try:
                if self.followups:
                    await self.followups.close()
                if reserved is not None:
                    await self.db.finish_delivery(reserved, "uncertain")
                    self._session_counts["errors"] += 1
                elif claimed is not None:
                    await self.db.mark_target(claimed, "pending")
                await self.db.set_setting("task_status", "stopped")
                # Closing the app keeps waiting/ready second messages; a user stop or normal finish cancels them.
                cancelled_followups = (
                    0
                    if self.keep_followups
                    else await self.db.cancel_followups(
                        "Задача завершена или остановлена; второе сообщение отменено и после ответа не отправится"
                    )
                )
                followup_stats = await self.db.get_followup_stats()
                summary["followup_waiting"] = sum(followup_stats.get(key, 0) for key in ("waiting", "ready"))
                summary["followup_cancelled"] = cancelled_followups
                if run_id is not None:
                    await self.db.finish_run(run_id, summary)
                await self._progress()
                self.log_callback(
                    "INFO",
                    f"Первых сообщений: {summary['sent']}; вторых после ответа: {summary['followup_sent']}; "
                    f"пропущено: {summary['skipped']}; ошибок: {summary['errors']}",
                )
            except Exception as exc:  # noqa: BLE001
                # Cleanup failures must not swallow the completion callback.
                self.log_callback("ERROR", "Ошибка завершения задачи: " + explain_error(exc))
            finally:
                await self._disconnect_all()
                self.running = self.paused = False
                self._task = None
            if self.complete_callback:
                self.complete_callback(summary)

    async def _verify_uncertain_send(self, manager, user, text, random_id, since, result):
        """Check the chat history after an uncertain send.

        Returns ``{"found": True, "message_id": ...}`` (delivered), ``{"found": False}`` (clearly absent
        after a short wait) or ``{"found": None}`` (could not tell).
        """
        reading_failed = False
        for attempt in range(self.VERIFY_ATTEMPTS):
            if attempt:
                await asyncio.sleep(self.VERIFY_DELAY)
            if not self.running and self._stop_requested:
                return {"found": None}
            try:
                verdict = await manager.verify_sent_message(user, text, random_id, since)
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

    async def _finish_not_found(self, account, target, detail):
        await self.db.mark_target(
            target["id"], "not_found", error_message="Получатель не найден или не является пользователем"
        )
        self._session_counts["skipped"] += 1
        await self.db.add_log(account["phone"], target["username"], "MESSAGE_NOT_FOUND", detail)
        self.log_callback("SKIP", f"@{target['username']} не найден; переходим к следующему получателю")
        await self._progress()
        await self._wait(random.uniform(2, 5))

    async def _pause_account(self, account, target, kind, flood_seconds=None):
        """Take ONE account out of rotation; the other accounts keep working.

        The recipient must already be back in the queue. The run only stops later, when no
        usable account is left (see the account-selection branch of the main loop).
        """
        if kind == "flood":
            seconds = max(1, int(flood_seconds or 1))
            await self.db.set_flood_wait(account["id"], seconds)
            reason = f"Telegram просит подождать {seconds} сек"
            outcome = f"Пауза {seconds} сек"
        elif kind == "peer_flood":
            await self.db.set_flood_wait(account["id"], PEER_FLOOD_COOLDOWN_SECONDS)
            reason = "Telegram ограничил отправку с этого аккаунта"
            outcome = f"Пауза {PEER_FLOOD_COOLDOWN_SECONDS // 3600} ч"
        else:
            await self.db.update_account_status(account["id"], "unauthorized")
            reason = "Telegram отозвал сохранённый вход"
            outcome = "Аккаунт исключён из ротации"
        self._streaks.pop(account["id"], None)
        self.log_callback(
            "WARN", f"Аккаунт {account['phone']}: {reason}. {outcome}; остальные аккаунты продолжают работу"
        )
        await self.db.add_log(
            account["phone"],
            target["username"],
            "MESSAGE_ACCOUNT_PAUSED",
            f"{reason}. {outcome}; получатель остался в очереди",
        )

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

    @staticmethod
    def daily_used(account: dict) -> int:
        """First messages + follow-ups sent today (they share a single daily budget)."""
        return int(account.get("sent_today") or 0) + int(account.get("followups_today") or 0)

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
                account = await self.db.clear_expired_flood(account)
                if account.get("status") == "flood":
                    continue
            # One combined daily budget per account: first messages and
            # reply-triggered follow-ups both count against the safe limit.
            if self.daily_used(account) >= self.get_safe_daily_limit(account):
                continue
            self._rr_index = (index + 1) % len(accounts)
            return account
        return None


async def send_test_message(db, account_id, text):
    """Render ``text`` with a sample name and send it to the account's own «Избранное»."""
    account = await db.get_account(account_id)
    if not account:
        raise ValueError("Аккаунт не найден")
    rendered = validate_message(render_message(validate_message(text), SAMPLE_NAME))
    manager = AccountManager(account, db)
    try:
        await manager.connect()
        await manager.send_saved_message(rendered)
    finally:
        await manager.disconnect()
    return rendered
