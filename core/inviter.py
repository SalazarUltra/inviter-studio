import asyncio
import math
import random
import time
from datetime import datetime, timedelta
from typing import Callable

from core.account_manager import AccountManager
from core.database import Database, effective_account_age_days, is_flood_blocking
from core.user_feedback import explain_error
from core.validation import time_minutes

# Recipient-level results that end the target for good (result code -> stored target status).
FINAL_SKIP_STATUS = {
    "not_found": "not_found",
    "private": "private",
    "already_in": "already_in",
    "skip": "not_found",  # deactivated / bot / kicked / too many chats
}
# Account-level restrictions: timed cooldown instead of a permanent "restricted" status.
ACCOUNT_COOLDOWN_ERRORS = {"peer_flood", "account_banned"}
PEER_FLOOD_COOLDOWN_SECONDS = 24 * 3600
# Network blips / unknown outcomes: requeue a few times before giving up.
TRANSIENT_ERRORS = {"network", "uncertain", "not_connected"}
MAX_TRANSIENT_RETRIES = 3
# Consecutive non-success results on one account before it takes a short break.
ACCOUNT_FAILURE_LIMIT = 5
# A pause longer than this is reported instead of silently waited out.
LONG_COOLDOWN_SECONDS = 3600
# Task-wide safety gap (seconds) between two invite calls from any accounts. Per-account
# intervals do the real throttling; this only prevents bursts when several accounts are ready.
MIN_ACTION_GAP = 2.0
# Longest single wait while all accounts are cooling down between actions (keeps UI details fresh).
MAX_INTERVAL_WAIT_CHUNK = 30


def describe_next_available(details: dict) -> str:
    """Human-readable (Russian) line about when an account may act next; empty when unknown."""
    phone = details.get("next_account")
    if not phone:
        return ""
    seconds = int(details.get("next_in") or 0)
    if seconds <= 0:
        return f"Ближайший аккаунт: {phone} — готов сейчас"
    clock = str(details.get("next_available_at") or "")[11:19]
    suffix = f" (в {clock})" if clock else ""
    return f"Ближайший аккаунт: {phone} — через {seconds} сек{suffix}"


class InviterEngine:
    def __init__(
        self,
        db: Database,
        log_callback: Callable[[str, str], None],
        progress_callback: Callable[[int, int], None] | None = None,
        status_callback: Callable[[str], None] | None = None,
        fatal_callback: Callable[[str], None] | None = None,
        complete_callback: Callable[[dict], None] | None = None,
        details_callback: Callable[[dict], None] | None = None,
    ):
        self.db = db
        self.details_callback = details_callback
        self.log_callback = log_callback
        self.progress_callback = progress_callback
        self.status_callback = status_callback
        self.fatal_callback = fatal_callback
        self.complete_callback = complete_callback
        self.running = False
        self.paused = False
        self._stop_requested = False
        self._task = None
        self.account_managers: dict[int, AccountManager] = {}
        self._rr_index = 0
        self._started_at: datetime | None = None
        self.start_time: datetime | None = None
        self.max_consecutive = 5
        self.break_min = 5
        self.break_max = 15
        self.session_adds = 0
        self._session_counts = {"added": 0, "skipped": 0, "errors": 0}
        self._fail_streaks: dict[int, int] = {}
        self._transient_retries: dict[int, int] = {}
        # Per-account pacing: next allowed action time, action/result counters for warm-up and reports.
        self._next_allowed: dict[int, datetime] = {}
        self._account_stats: dict[int, dict] = {}
        self._soonest_ready: datetime | None = None
        self._soonest_account: str | None = None
        self._last_action_ts = float("-inf")
        self._last_stats: dict | None = None
        self.WARM_UP_COUNT = 3
        self.WARM_UP_MULTIPLIER = 2

    async def start(self, accounts: list[dict], chat: str, interval_min: int, interval_max: int) -> None:
        if self.running:
            raise RuntimeError("Задача уже выполняется")
        if not accounts or not chat or not 10 <= interval_min <= interval_max <= 300:
            raise ValueError("Укажите аккаунты, чат и интервал 10–300 секунд")
        # Claim the engine synchronously: a concurrent start() must not pass the
        # guard above across the awaits inside the try block.
        self.running = True
        self._task = asyncio.current_task()
        self.paused = False
        self._rr_index = 0
        self._started_at = self.start_time = datetime.now()
        self.session_adds = 0
        self._fail_streaks = {}
        self._transient_retries = {}
        self._next_allowed = {}
        self._account_stats = {}
        self._soonest_ready = None
        self._soonest_account = None
        self._last_action_ts = float("-inf")
        self._last_stats = None
        claimed_target = None
        self._session_counts = {"added": 0, "skipped": 0, "errors": 0}
        outcome = "completed"
        run_id = None

        try:
            if self._stop_requested:
                return
            if await self.db.get_campaign_kind() != "invite":
                raise ValueError("Выберите кампанию приглашений")
            run_id = await self.db.begin_run(chat)
            await self.db.reset_daily_counters_if_needed()
            await self.db.reset_processing_to_pending()
            self.max_consecutive = int(await self.db.get_setting("protection_consecutive", "5") or 5)
            self.break_min = int(await self.db.get_setting("protection_break_min", "5") or 5)
            self.break_max = int(await self.db.get_setting("protection_break_max", "15") or 15)
            if self.max_consecutive < 1 or self.break_min < 1 or self.break_max < 1:
                raise ValueError("Настройки перерыва и последовательных действий должны быть положительными")
            if self.break_min > self.break_max:
                self.break_min, self.break_max = self.break_max, self.break_min
            if (await self.db.get_setting("invite_order", "shuffle") or "shuffle") == "as_is":
                self.log_callback("INFO", "Порядок приглашений сохранён: люди добавляются как в списке")
            else:
                await self.db.shuffle_pending_targets(allow_running=True)
                self.log_callback("INFO", "Список перемешан для случайного порядка добавления")

            eligible_accounts = []
            for seed in accounts:
                if not self.running:
                    break
                acc = await self.db.get_account(seed["id"])
                if (
                    not acc
                    or int(acc.get("is_enabled", 1)) != 1
                    or acc.get("status") in {"restricted", "unauthorized", "inactive"}
                ):
                    continue
                mgr = AccountManager(acc, self.db)
                try:
                    await mgr.connect()
                    check = await mgr.check_admin_rights(chat)
                except asyncio.CancelledError:
                    await mgr.disconnect()
                    raise
                except Exception as exc:
                    await mgr.disconnect()
                    detail = explain_error(exc)
                    await self.db.add_log(acc["phone"], None, "CONNECT_FAILED", detail)
                    self.log_callback("WARN", f"Аккаунт {acc['phone']} пропущен: {detail}")
                    continue
                if not check["ok"]:
                    await mgr.disconnect()
                    detail = str(check.get("reason") or "Не подтверждено право приглашения")
                    await self.db.add_log(acc["phone"], None, "INVITE_RIGHTS_MISSING", detail)
                    self.log_callback("WARN", f"Аккаунт {acc['phone']} пропущен: {detail}")
                    continue
                self.account_managers[acc["id"]] = mgr
                eligible_accounts.append(acc)
                self.log_callback("INFO", f"Подключен аккаунт {acc['phone']}; право приглашения подтверждено")
                await self.db.add_log(
                    acc["phone"], None, "CONNECT", "Соединение готово; право приглашения подтверждено"
                )
            accounts = eligible_accounts
            if not self.account_managers and self.running:
                raise RuntimeError("Нет доступных аккаунтов с подтверждённым правом приглашения в этот чат")
            if self.running:
                self.log_callback("SUCCESS", f"Подготовлено аккаунтов с правом приглашения: {len(accounts)}")

            while self.running:
                if self.paused:
                    await asyncio.sleep(0.25)
                    continue
                if not await self._is_within_schedule():
                    self.log_callback("INFO", "Вне расписания. Ожидание...")
                    await self._wait(60)
                    continue

                await self.db.reset_daily_counters_if_needed()
                stats = await self.db.get_targets_stats()
                if self.progress_callback:
                    self.progress_callback(stats["added"], stats["total"])
                self._emit_details(stats)

                if not stats["pending"]:
                    self.log_callback("INFO", "Список исчерпан. Задача завершена.")
                    break
                account = await self._pick_available_account(accounts)
                if not account:
                    if await self._wait_for_account_interval():
                        continue
                    if not await self.db.get_enabled_accounts():
                        raise RuntimeError("Все аккаунты отключены")
                    usable = [await self.db.get_account(item["id"]) for item in accounts]
                    if not any(
                        item
                        and int(item.get("is_enabled", 1)) == 1
                        and item.get("status") not in {"restricted", "unauthorized", "inactive"}
                        and self.get_safe_daily_limit(item) > 0
                        for item in usable
                    ):
                        raise RuntimeError("Нет доступных аккаунтов: проверьте авторизацию и ограничения")
                    long_until = self._long_cooldown_until(usable)
                    if long_until:
                        raise RuntimeError(
                            "Все доступные аккаунты на длительной паузе Telegram до "
                            + long_until.strftime("%d.%m %H:%M")
                            + ". Запустите задачу позже"
                        )
                    self.log_callback("INFO", "Аккаунты на перерыве или достигли лимита. Ожидание...")
                    await self._wait(30)
                    continue
                if not self.running or self.paused:
                    continue
                gap = MIN_ACTION_GAP - (time.monotonic() - self._last_action_ts)
                if gap > 0:
                    await self._wait(gap)
                    if not self.running or self.paused:
                        continue
                target = await self.db.claim_next_pending_target()
                if not target:
                    continue
                claimed_target = target["id"]
                if not self.running or self.paused:
                    await self.db.mark_target(claimed_target, "pending")
                    claimed_target = None
                    continue

                username = target["username"]
                if self.status_callback:
                    self.status_callback(f"Добавление @{username} через {account['phone']}")

                mgr = self.account_managers[account["id"]]
                counts_before = dict(self._session_counts)
                self._last_action_ts = time.monotonic()
                if target.get("user_id"):
                    result = await mgr.add_user_to_chat(chat, username, user_id=target["user_id"])
                else:
                    result = await mgr.add_user_to_chat(chat, username)

                if result["success"]:
                    await self.db.record_success(target["id"], account["id"], chat=chat)
                    claimed_target = None
                    self._fail_streaks[account["id"]] = 0
                    self._transient_retries.pop(target["id"], None)
                    self.session_adds += 1
                    self._session_counts["added"] += 1
                    await self.db.add_log(account["phone"], username, "ADD_SUCCESS", "Добавлен в чат")
                    self.log_callback("SUCCESS", f"@{username} добавлен через {account['phone']}")
                    fresh = await self.db.get_account(account["id"])
                    if fresh and int(fresh.get("consecutive_adds") or 0) >= self.max_consecutive:
                        break_minutes = random.randint(self.break_min, self.break_max)
                        break_until = (datetime.now() + timedelta(minutes=break_minutes)).isoformat(timespec="seconds")
                        await self.db.set_rotation_break(account["id"], break_until)
                        await self.db.reset_consecutive(account["id"])
                        self.log_callback(
                            "INFO",
                            f"{account['phone']} уходит на перерыв {break_minutes} мин (ротация)",
                        )
                elif result.get("error") in FINAL_SKIP_STATUS:
                    # Recipient-level outcome: final, not retried and not the account's fault.
                    error = result["error"]
                    self._session_counts["skipped"] += 1
                    self._transient_retries.pop(target["id"], None)
                    message = str(result.get("message") or error)
                    await self.db.mark_target(target["id"], FINAL_SKIP_STATUS[error], None, message)
                    await self.db.add_log(account["phone"], username, "SKIP", message)
                    reason = {
                        "not_found": "никнейм не найден",
                        "already_in": "уже в чате",
                    }.get(error) or (message if result.get("message") else "запрет получателя")
                    self.log_callback("INFO", f"@{target['username']} пропущен: {reason}")
                elif result.get("flood_seconds"):
                    await self.db.mark_target(target["id"], "pending")
                    claimed_target = None
                    await self.db.set_flood_wait(account["id"], int(result["flood_seconds"]))
                    await self.db.add_log(
                        account["phone"],
                        username,
                        "FLOOD_WAIT",
                        f"Telegram просит подождать {result['flood_seconds']} сек",
                    )
                    self.log_callback("FLOOD", f"Telegram просит подождать {result['flood_seconds']} сек")
                    await self._record_account_failure(account)
                elif result.get("error") in ACCOUNT_COOLDOWN_ERRORS:
                    await self.db.mark_target(target["id"], "pending")
                    claimed_target = None
                    await self.db.set_flood_wait(account["id"], PEER_FLOOD_COOLDOWN_SECONDS)
                    reason = str(result.get("message") or "Telegram ограничил приглашения с этого аккаунта")
                    await self.db.add_log(account["phone"], username, "PEER_FLOOD", reason)
                    self.log_callback(
                        "WARN",
                        f"Аккаунт {account['phone']}: {reason}. "
                        f"Пауза {PEER_FLOOD_COOLDOWN_SECONDS // 3600} ч, остальные аккаунты продолжают работу",
                    )
                elif result.get("error") == "unauthorized":
                    await self.db.mark_target(target["id"], "pending")
                    claimed_target = None
                    await self.db.update_account_status(account["id"], "unauthorized")
                    self.log_callback("ERROR", f"Аккаунт {account['phone']}: Telegram отозвал сохранённый вход")
                elif result.get("error") == "need_admin":
                    await self.db.mark_target(target["id"], "pending")
                    claimed_target = None
                    accounts = await self._drop_account_without_rights(account, accounts, result)
                    if not accounts:
                        raise RuntimeError("Нет прав администратора в целевом чате ни у одного аккаунта")
                    continue
                elif result.get("error") in TRANSIENT_ERRORS:
                    claimed_target = None
                    await self._handle_transient_failure(target, account, result)
                    await self._record_account_failure(account)
                else:
                    self._session_counts["errors"] += 1
                    self._transient_retries.pop(target["id"], None)
                    message = str(result.get("message") or "Telegram отклонил действие; проверьте аккаунт и получателя")
                    await self.db.mark_target(target["id"], "error", None, message)
                    await self.db.add_log(account["phone"], username, "ADD_ERROR", message)
                    self.log_callback("ERROR", f"@{username}: действие не выполнено; подробности в журнале")
                    await self._record_account_failure(account)

                claimed_target = None
                stats = await self.db.get_targets_stats()
                if self.progress_callback:
                    self.progress_callback(stats["added"], stats["total"])
                self._record_account_result(account, counts_before)
                self._emit_details(stats)
                if not stats["pending"]:
                    break
                # Per-account pacing: only this account waits; others keep inviting meanwhile.
                self._schedule_account_next(account, interval_min, interval_max)
        except asyncio.CancelledError:
            outcome = "stopped"
            raise
        except Exception as exc:
            outcome = "failed"
            self.log_callback("ERROR", f"Задача завершилась с ошибкой: {explain_error(exc)}")
            if self.fatal_callback:
                self.fatal_callback(explain_error(exc))
        finally:
            if self._stop_requested and outcome != "failed":
                outcome = "stopped"
            summary = {
                **self._session_counts,
                "outcome": outcome,
                "duration": str(datetime.now() - self._started_at).split(".")[0],
                "accounts": self._account_summary(),
            }
            try:
                if claimed_target is not None:
                    # Cancelled requests may have reached Telegram; leave processing
                    # for startup recovery rather than pretending they failed.
                    self.log_callback("WARN", "Незавершённая цель будет восстановлена при следующем запуске")
                await self.db.set_setting("task_status", "stopped")
                if run_id is not None:
                    await self.db.finish_run(run_id, summary)
                await self._show_final_report(outcome)
            except Exception as exc:  # noqa: BLE001
                # Cleanup failures must not swallow the completion callback.
                self.log_callback("ERROR", "Ошибка завершения задачи: " + explain_error(exc))
            finally:
                await self._disconnect_all()
                self.running = False
                self.paused = False
                self._task = None
                if self.status_callback:
                    self.status_callback("Ошибка" if outcome == "failed" else "Остановлено")
            if self.complete_callback:
                self.complete_callback(summary)

    async def _record_account_failure(self, account: dict) -> None:
        """Circuit breaker: several non-success results in a row send the account on a short break."""
        account_id = account["id"]
        streak = self._fail_streaks.get(account_id, 0) + 1
        self._fail_streaks[account_id] = streak
        if streak < ACCOUNT_FAILURE_LIMIT:
            return
        self._fail_streaks[account_id] = 0
        minutes = random.randint(self.break_min, self.break_max)
        until = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        await self.db.set_rotation_break(account_id, until)
        await self.db.add_log(
            account["phone"], None, "ACCOUNT_BREAKER", f"{streak} неудач подряд; перерыв {minutes} мин"
        )
        self.log_callback(
            "WARN",
            f"{account['phone']}: {streak} неудач подряд — аккаунт уходит на перерыв {minutes} мин",
        )

    async def _handle_transient_failure(self, target: dict, account: dict, result: dict) -> None:
        """Requeue a target after a network blip/unknown outcome; give up after MAX_TRANSIENT_RETRIES."""
        target_id, username = target["id"], target["username"]
        attempts = self._transient_retries.get(target_id, 0) + 1
        detail = str(result.get("message") or "Сбой соединения с Telegram")
        if attempts <= MAX_TRANSIENT_RETRIES:
            self._transient_retries[target_id] = attempts
            await self.db.mark_target(target_id, "pending")
            await self.db.add_log(
                account["phone"], username, "ADD_RETRY", f"{detail} (повтор {attempts}/{MAX_TRANSIENT_RETRIES})"
            )
            self.log_callback(
                "WARN",
                f"@{username}: временный сбой, повтор {attempts}/{MAX_TRANSIENT_RETRIES}",
            )
            return
        self._transient_retries.pop(target_id, None)
        self._session_counts["errors"] += 1
        final = f"{detail}. Попытки исчерпаны ({MAX_TRANSIENT_RETRIES}); проверьте вручную и повторите"
        await self.db.mark_target(target_id, "error", None, final)
        await self.db.add_log(account["phone"], username, "ADD_ERROR", final)
        self.log_callback("ERROR", f"@{username}: попытки исчерпаны; подробности в журнале")

    async def _drop_account_without_rights(self, account: dict, accounts: list[dict], result: dict) -> list[dict]:
        """Remove one account that cannot invite into the chat; the other accounts keep working."""
        detail = str(result.get("message") or "Нет прав администратора с правом приглашения")
        await self.db.add_log(account["phone"], None, "NEED_ADMIN", detail)
        self.log_callback(
            "WARN",
            f"Аккаунт {account['phone']} исключён из ротации: {detail}",
        )
        mgr = self.account_managers.pop(account["id"], None)
        if mgr is not None:
            try:
                await mgr.disconnect()
            except Exception:  # noqa: BLE001
                pass
        self._fail_streaks.pop(account["id"], None)
        remaining = [item for item in accounts if item["id"] != account["id"]]
        self._rr_index = 0
        return remaining

    @staticmethod
    def _long_cooldown_until(accounts: list[dict | None]) -> datetime | None:
        """Earliest end of the pause when every usable account is stuck in a long Telegram pause."""
        now = datetime.now()
        deadlines = []
        for item in accounts:
            if (
                not item
                or int(item.get("is_enabled", 1)) != 1
                or item.get("status") in {"restricted", "unauthorized", "inactive"}
                or InviterEngine.get_safe_daily_limit(item) <= 0
            ):
                continue
            if not is_flood_blocking(item, now):
                return None
            deadline = datetime.fromisoformat(str(item["flood_until"]))
            if (deadline - now).total_seconds() <= LONG_COOLDOWN_SECONDS:
                return None
            deadlines.append(deadline)
        return min(deadlines) if deadlines else None

    def _account_stat(self, account: dict) -> dict:
        stat = self._account_stats.get(account["id"])
        if stat is None:
            stat = {
                "id": account["id"],
                "phone": account.get("phone") or str(account["id"]),
                "actions": 0,
                "added": 0,
                "skipped": 0,
                "errors": 0,
            }
            self._account_stats[account["id"]] = stat
        return stat

    def _record_account_result(self, account: dict, counts_before: dict) -> None:
        """Attribute the outcome of the action just finished to the account that performed it."""
        stat = self._account_stat(account)
        stat["actions"] += 1
        for key in ("added", "skipped", "errors"):
            stat[key] += max(0, self._session_counts.get(key, 0) - counts_before.get(key, 0))

    def _schedule_account_next(self, account: dict, interval_min: int, interval_max: int) -> int:
        """Set this account's own next-allowed time; warm-up applies to its first actions only."""
        stat = self._account_stat(account)
        wait_time = self._get_smart_interval(interval_min, interval_max)
        if stat["actions"] <= self.WARM_UP_COUNT:
            wait_time *= self.WARM_UP_MULTIPLIER
            self.log_callback(
                "INFO",
                f"{stat['phone']}: тёплый старт ({stat['actions']}/{self.WARM_UP_COUNT}), "
                f"пауза аккаунта {wait_time} сек",
            )
        else:
            self.log_callback("INFO", f"{stat['phone']}: следующее действие аккаунта через {wait_time} сек")
        self._next_allowed[account["id"]] = datetime.now() + timedelta(seconds=wait_time)
        return wait_time

    async def _wait_for_account_interval(self) -> bool:
        """Every otherwise-available account is cooling down: wait for the earliest (True if waited)."""
        ready = self._soonest_ready
        if ready is None:
            return False
        delay = min(max((ready - datetime.now()).total_seconds(), 0.25), MAX_INTERVAL_WAIT_CHUNK)
        if self.status_callback:
            self.status_callback(
                f"Ожидание интервала: {self._soonest_account or 'аккаунт'} свободен через {math.ceil(delay)} сек"
            )
        await self._wait(delay)
        return True

    def _account_summary(self) -> list[dict]:
        return [
            {key: item[key] for key in ("id", "phone", "actions", "added", "skipped", "errors")}
            for item in sorted(self._account_stats.values(), key=lambda row: (-row["added"], row["phone"]))
        ]

    def build_details(self, stats: dict | None = None) -> dict:
        """Snapshot for the UI: queue counters, session counters, per-account results, next free account."""
        stats = stats or self._last_stats or {}
        now = datetime.now()
        accounts = []
        for account_id, mgr in self.account_managers.items():
            known = self._account_stats.get(account_id, {})
            phone = known.get("phone") or (getattr(mgr, "account", None) or {}).get("phone") or str(account_id)
            ready_at = self._next_allowed.get(account_id)
            wait = max(0, math.ceil((ready_at - now).total_seconds())) if ready_at else 0
            accounts.append(
                {
                    "id": account_id,
                    "phone": phone,
                    "added": known.get("added", 0),
                    "skipped": known.get("skipped", 0),
                    "errors": known.get("errors", 0),
                    "next_in": wait,
                }
            )
        nearest = min(accounts, key=lambda row: row["next_in"]) if accounts else None
        next_at = (now + timedelta(seconds=nearest["next_in"])).isoformat(timespec="seconds") if nearest else None
        return {
            "total": stats.get("total", 0),
            "pending": stats.get("pending", 0),
            "processing": stats.get("processing", 0),
            "success": stats.get("added", 0),
            "skipped": stats.get("skipped", 0),
            "errors": stats.get("errors", 0),
            "session": dict(self._session_counts),
            "accounts": accounts,
            "next_account": nearest["phone"] if nearest else None,
            "next_in": nearest["next_in"] if nearest else 0,
            "next_available_at": next_at,
        }

    def _emit_details(self, stats: dict | None = None) -> None:
        if stats is not None:
            self._last_stats = stats
        if not self.details_callback:
            return
        try:
            self.details_callback(self.build_details(stats))
        except Exception:  # noqa: BLE001
            # UI formatting problems must never stop an invite run.
            pass

    async def _wait(self, seconds: float) -> None:
        remaining = max(0, seconds)
        while self.running and remaining > 0:
            chunk = min(0.25, remaining)
            await asyncio.sleep(chunk)
            if not self.paused:
                remaining -= chunk

    async def _pick_available_account(self, seed_accounts: list[dict]) -> dict | None:
        if not seed_accounts:
            return None

        now = datetime.now()
        self._soonest_ready = None
        self._soonest_account = None
        ordered = seed_accounts[self._rr_index :] + seed_accounts[: self._rr_index]
        for idx, acc in enumerate(ordered):
            fresh = await self.db.get_account(acc["id"])
            if not fresh or fresh["id"] not in self.account_managers:
                continue
            if fresh.get("status") in {"restricted", "unauthorized", "inactive"}:
                continue

            flood_until = fresh.get("flood_until")
            rotation_break_until = fresh.get("rotation_break_until")
            if fresh.get("status") == "flood":
                if flood_until and datetime.fromisoformat(flood_until) > now:
                    continue
                fresh = await self.db.clear_expired_flood(fresh)
                if fresh.get("status") == "flood":
                    continue

            if rotation_break_until and datetime.fromisoformat(rotation_break_until) > now:
                continue
            if int(fresh.get("is_enabled", 1)) != 1:
                continue
            safe_limit = self.get_safe_daily_limit(fresh)
            if int(fresh.get("added_today") or 0) >= int(safe_limit):
                continue
            # Per-account interval: an account that acted recently waits on its own clock
            # while the other accounts keep working.
            ready_at = self._next_allowed.get(fresh["id"])
            if ready_at is not None and ready_at > now:
                if self._soonest_ready is None or ready_at < self._soonest_ready:
                    self._soonest_ready = ready_at
                    self._soonest_account = fresh.get("phone") or str(fresh["id"])
                continue

            self._rr_index = (self._rr_index + idx + 1) % len(seed_accounts)
            return fresh
        return None

    async def _is_within_schedule(self) -> bool:
        settings = await self.db.get_settings_map(["schedule_enabled", "schedule_from", "schedule_to"])
        if settings["schedule_enabled"] != "1":
            return True
        from_str = settings["schedule_from"] or "00:00"
        to_str = settings["schedule_to"] or "23:59"
        now = datetime.now()

        try:
            start = time_minutes(str(from_str))
            end = time_minutes(str(to_str))
        except ValueError:
            # Corrupt/legacy values must not crash a running task, but also must
            # not silently allow work outside an intended window.
            self.log_callback(
                "WARN",
                f"Некорректное расписание «{from_str}–{to_str}». Задача на паузе до исправления настроек.",
            )
            return False

        now_min = now.hour * 60 + now.minute
        if start == end:
            return True
        if start < end:
            return start <= now_min <= end
        # Overnight window, e.g. 22:00 -> 06:00
        return now_min >= start or now_min <= end

    @staticmethod
    def get_safe_daily_limit(account: dict) -> int:
        age = effective_account_age_days(account)
        limit = int(account.get("daily_limit", 40))
        if age < 7:
            return min(limit, 10)
        if age < 30:
            return min(limit, 20)
        return limit

    def _get_smart_interval(self, base_min: int, base_max: int) -> int:
        hour = datetime.now().hour
        if 0 <= hour < 8:
            multiplier = 2.5
            period = "ночь"
        elif (8 <= hour < 10) or (21 <= hour < 23):
            multiplier = 1.5
            period = "утро/вечер"
        elif hour >= 23:
            multiplier = 2.0
            period = "поздний вечер"
        else:
            multiplier = 1.0
            period = "день"
        smart_min = max(int(base_min * multiplier), 1)
        smart_max = max(int(base_max * multiplier), smart_min)
        interval = random.randint(smart_min, smart_max)
        if multiplier > 1.0:
            if not getattr(self, "_smart_clock_logged", False):
                self._smart_clock_logged = True
                self.log_callback(
                    "INFO",
                    "Умный интервал считается по системным часам компьютера (не по московскому времени)",
                )
            self.log_callback(
                "INFO",
                f"Умный интервал ({period}): {interval} сек (x{multiplier} от базового)",
            )
        return interval

    async def _show_final_report(self, outcome: str = "completed") -> None:
        if not self.start_time:
            return
        stats = self._session_counts
        duration = int((datetime.now() - self.start_time).total_seconds())
        minutes = duration // 60
        seconds = duration % 60
        self.log_callback("INFO", "─" * 50)
        label = {
            "completed": "Задача завершена",
            "stopped": "Задача остановлена",
            "failed": "Задача завершилась с ошибкой",
        }[outcome]
        self.log_callback("ERROR" if outcome == "failed" else "INFO", label)
        self.log_callback("INFO", "📊 Итоги:")
        self.log_callback("SUCCESS", f"   Добавлено:  {stats.get('added', 0)}")
        self.log_callback("INFO", f"   Пропущено:  {stats.get('skipped', 0)}")
        self.log_callback("ERROR", f"   Ошибок:     {stats.get('errors', 0)}")
        self.log_callback("INFO", f"   Время:      {minutes} мин {seconds} сек")
        if stats.get("added", 0) > 0 and duration > 0:
            rate = stats["added"] / (duration / 60)
            self.log_callback("INFO", f"   Скорость:   {rate:.1f} чел/мин")
        for item in self._account_summary():
            self.log_callback(
                "INFO",
                f"   {item['phone']}: добавлено {item['added']}, пропущено {item['skipped']}, ошибок {item['errors']}",
            )
        self.log_callback("INFO", "─" * 50)

    async def _disconnect_all(self) -> None:
        for account_id, mgr in list(self.account_managers.items()):
            phone = None
            if mgr.account:
                phone = mgr.account.get("phone")
            try:
                await mgr.disconnect()
                if phone:
                    await self.db.add_log(phone, None, "DISCONNECT", "Рабочее соединение закрыто; аккаунт сохранён")
            except Exception:  # noqa: BLE001
                pass
            finally:
                self.account_managers.pop(account_id, None)

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def stop(self) -> None:
        self._stop_requested = True
        self.running = False
