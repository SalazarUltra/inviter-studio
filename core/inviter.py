import asyncio
import random
from datetime import datetime, timedelta
from typing import Callable

from core.account_manager import AccountManager
from core.database import Database
from core.user_feedback import explain_error
from core.validation import time_minutes


class InviterEngine:
    def __init__(
        self,
        db: Database,
        log_callback: Callable[[str, str], None],
        progress_callback: Callable[[int, int], None] | None = None,
        status_callback: Callable[[str], None] | None = None,
        fatal_callback: Callable[[str], None] | None = None,
        complete_callback: Callable[[dict], None] | None = None,
    ):
        self.db = db
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

                if not stats["pending"]:
                    self.log_callback("INFO", "Список исчерпан. Задача завершена.")
                    break
                account = await self._pick_available_account(accounts)
                if not account:
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
                    self.log_callback("INFO", "Аккаунты на перерыве или достигли лимита. Ожидание...")
                    await self._wait(30)
                    continue
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
                result = await mgr.add_user_to_chat(chat, username)

                if result["success"]:
                    await self.db.record_success(target["id"], account["id"])
                    claimed_target = None
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
                elif result.get("error") in ("not_found", "private", "already_in"):
                    self._session_counts["skipped"] += 1
                    await self.db.mark_target(
                        target["id"],
                        result["error"],
                        None,
                        str(result.get("message") or result["error"]),
                    )
                    await self.db.add_log(
                        account["phone"],
                        username,
                        "SKIP",
                        str(result.get("message") or result["error"]),
                    )
                    self.log_callback(
                        "INFO",
                        f"@{target['username']} пропущен: "
                        + {
                            "not_found": "никнейм не найден",
                            "private": "запрет получателя",
                            "already_in": "уже в чате",
                        }[result["error"]],
                    )
                elif result.get("flood_seconds"):
                    await self.db.mark_target(target["id"], "pending")
                    await self.db.set_flood_wait(account["id"], int(result["flood_seconds"]))
                    await self.db.add_log(
                        account["phone"],
                        username,
                        "FLOOD_WAIT",
                        f"Telegram просит подождать {result['flood_seconds']} сек",
                    )
                    self.log_callback("FLOOD", f"Telegram просит подождать {result['flood_seconds']} сек")
                elif result.get("error") in {"peer_flood", "unauthorized", "need_admin"}:
                    await self.db.mark_target(target["id"], "pending")
                    claimed_target = None
                    error = result["error"]
                    if error == "need_admin":
                        raise RuntimeError("Нет прав администратора в целевом чате")
                    await self.db.update_account_status(
                        account["id"], "restricted" if error == "peer_flood" else "unauthorized"
                    )
                    reason = (
                        "Telegram отклонил действие с аккаунтом"
                        if error == "peer_flood"
                        else "Telegram отозвал сохранённый вход"
                    )
                    self.log_callback("ERROR", f"Аккаунт {account['phone']}: {reason}")
                else:
                    self._session_counts["errors"] += 1
                    mapped = {
                        "private": "private",
                        "already_in": "already_in",
                        "not_found": "not_found",
                    }
                    status = mapped.get(result.get("error"), "error")
                    await self.db.mark_target(
                        target["id"],
                        status,
                        None,
                        str(result.get("message") or "Telegram отклонил действие; проверьте аккаунт и получателя"),
                    )
                    await self.db.add_log(
                        account["phone"],
                        username,
                        "ADD_ERROR",
                        str(result.get("message") or "Telegram отклонил действие; проверьте аккаунт и получателя"),
                    )
                    self.log_callback("ERROR", f"@{username}: действие не выполнено; подробности в журнале")

                claimed_target = None
                stats = await self.db.get_targets_stats()
                if self.progress_callback:
                    self.progress_callback(stats["added"], stats["total"])
                if not stats["pending"]:
                    break
                if self.session_adds <= self.WARM_UP_COUNT:
                    wait_time = self._get_smart_interval(interval_min, interval_max) * self.WARM_UP_MULTIPLIER
                    self.log_callback(
                        "INFO",
                        f"Тёплый старт ({self.session_adds}/{self.WARM_UP_COUNT}): {wait_time} сек",
                    )
                else:
                    wait_time = self._get_smart_interval(interval_min, interval_max)
                self.log_callback("INFO", f"Ожидание {wait_time} сек...")
                await self._wait(wait_time)
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
                await self.db.update_account_status(fresh["id"], "active")
                fresh["status"] = "active"

            if rotation_break_until and datetime.fromisoformat(rotation_break_until) > now:
                continue
            if int(fresh.get("is_enabled", 1)) != 1:
                continue
            safe_limit = self.get_safe_daily_limit(fresh)
            if int(fresh.get("added_today") or 0) >= int(safe_limit):
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

        start = time_minutes(str(from_str))
        end = time_minutes(str(to_str))

        now_min = now.hour * 60 + now.minute
        if start == end:
            return True
        if start < end:
            return start <= now_min <= end
        # Overnight window, e.g. 22:00 -> 06:00
        return now_min >= start or now_min <= end

    @staticmethod
    def get_safe_daily_limit(account: dict) -> int:
        age = int(account.get("account_age_days") or 0)
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
