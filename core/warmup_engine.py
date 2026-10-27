"""
Основной движок прогрева.
Управляет очерёдностью действий, лимитами и расписанием.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import Any

from telethon.errors import AuthKeyUnregisteredError, FloodWaitError, PeerFloodError

from core.account_manager import AccountManager
from core.ai_writer import AIWriter
from core.user_feedback import explain_error
from core.warmup_actions import WarmupActions

MAX_CONSECUTIVE_FAILURES = 10

WARMUP_LEVELS = {
    "light": {
        "days": 3,
        "actions_per_day": {"min": 5, "max": 10},
        "actions": ["go_online", "read_channel", "react_to_post"],
        "label": "Лёгкий (3 дня)",
    },
    "medium": {
        "days": 7,
        "actions_per_day": {"min": 8, "max": 15},
        "actions": ["go_online", "read_channel", "react_to_post", "view_profile", "send_message"],
        "label": "Средний (7 дней)",
    },
    "intensive": {
        "days": 14,
        "actions_per_day": {"min": 12, "max": 20},
        "actions": [
            "go_online",
            "read_channel",
            "react_to_post",
            "view_profile",
            "send_message",
            "send_message",
            "react_to_post",
        ],
        "label": "Интенсивный (14 дней)",
    },
}


class WarmupEngine:
    def __init__(self, db, log_callback):
        self.db = db
        self.log_callback = log_callback
        self.running = False
        self.current_account: dict[str, Any] | None = None
        self.client = None
        self.ai_writer: AIWriter | None = None
        self._task = None
        self._manager = None
        self._session_id = None
        self._stop_requested = False

    async def _sleep_interruptible(self, total_seconds: float, step: float = 1.0) -> bool:
        remaining = max(float(total_seconds), 0.0)
        while remaining > 0 and self.running:
            chunk = step if remaining > step else remaining
            await asyncio.sleep(chunk)
            remaining -= chunk
        return self.running

    async def start(self, account: dict[str, Any], level: str, topic: str, api_key: str | None = None) -> None:
        if self.running:
            raise RuntimeError("Прогрев уже выполняется")
        if self._stop_requested:
            return
        if level not in WARMUP_LEVELS:
            raise ValueError("Неизвестный уровень прогрева")
        if account.get("id"):
            account = await self.db.clear_expired_flood(account)
        if int(account.get("is_enabled", 1)) != 1 or account.get("status") in {
            "flood",
            "restricted",
            "unauthorized",
            "inactive",
        }:
            raise ValueError(
                "Аккаунт выключен в настройках или его отправка требует проверки. Откройте раздел «Аккаунты»."
            )
        self.running = True
        self.current_account = account
        self._task = asyncio.current_task()
        outcome = "paused"
        try:
            await self._run(account, level, topic)
            session = await self.db.get_warmup_session(account["id"])
            if session and session["status"] == "completed":
                outcome = "completed"
        except asyncio.CancelledError:
            self.log_callback("INFO", "Прогрев приостановлен")
            # Cancellation must propagate; swallowing it hides the task's real state.
            raise
        except FloodWaitError as exc:
            await self.db.set_flood_wait(account["id"], int(exc.seconds))
            self.log_callback("WARN", f"Telegram требует ожидание {exc.seconds} сек. Прогрев остановлен")
        except (PeerFloodError, AuthKeyUnregisteredError) as exc:
            await self.db.update_account_status(
                account["id"], "restricted" if isinstance(exc, PeerFloodError) else "unauthorized"
            )
            self.log_callback("ERROR", explain_error(exc) + " Прогрев остановлен.")
        except Exception:
            outcome = "error"
            raise
        finally:
            try:
                if self._session_id and outcome != "completed":
                    await self.db.update_warmup_session(self._session_id, status=outcome)
                    await self.db.set_account_warmup_status(account["id"], "paused")
            finally:
                if self._manager:
                    await self._manager.disconnect()
                self.client = None
                self.running = False
                self._task = None

    async def _run(self, account: dict[str, Any], level: str, topic: str) -> None:
        self.running = True
        self.current_account = account
        config = WARMUP_LEVELS.get(level, WARMUP_LEVELS["medium"])

        session = await self.db.get_warmup_session(account["id"])
        if not session or session.get("level") != level or session.get("topic") != topic:
            await self.db.create_warmup_session(
                account_id=account["id"],
                level=level,
                topic=topic,
                days=config["days"],
            )
            session = await self.db.get_warmup_session(account["id"])
        if not session:
            self.log_callback("ERROR", "Не удалось создать сессию прогрева")
            self.running = False
            return

        if session.get("status") == "completed":
            self.log_callback("INFO", "Этот прогрев уже завершён")
            return
        self._session_id = session["id"]
        mgr = AccountManager(account, self.db)
        self._manager = mgr
        await mgr.connect()
        self.client = mgr.client
        ai_config = await self.db.get_active_ai_config()
        self.ai_writer = AIWriter(config=ai_config or {}, topic=topic, db=self.db)
        memory_enabled_setting = await self.db.get_setting("ai_memory_enabled", "1")
        memory_enabled = str(memory_enabled_setting or "1") == "1"
        tone = str(await self.db.get_setting("ai_style_tone", "neutral") or "neutral").strip().lower()
        try:
            hourly_limit = int(await self.db.get_setting("ai_hourly_limit", "6") or 6)
        except Exception:
            hourly_limit = 6
        try:
            chat_cooldown_min = int(await self.db.get_setting("ai_chat_cooldown_min", "12") or 12)
        except Exception:
            chat_cooldown_min = 12
        try:
            reply_chance = int(await self.db.get_setting("ai_reply_chance", "100") or 100)
        except Exception:
            reply_chance = 100
        reply_chance = max(0, min(100, reply_chance))
        actions_obj = WarmupActions(
            self.client,
            self.ai_writer,
            self.log_callback,
            account_id=int(account["id"]),
            memory_enabled=memory_enabled,
            tone=tone,
            hourly_limit=hourly_limit,
            chat_cooldown_min=chat_cooldown_min,
            reply_chance=reply_chance,
        )
        self.log_callback(
            "INFO",
            (
                f"Память AI: {'включена' if memory_enabled else 'выключена'} | "
                f"Тон: {tone} | Лимит: {hourly_limit}/ч | Пауза чат: {chat_cooldown_min} мин | "
                f"Ответы: {reply_chance}%"
            ),
        )

        selected = await self.db.get_selected_channels(account["id"])
        channels = []
        for ch in selected:
            username = (ch.get("username") or "").strip()
            peer_id = int(ch.get("channel_id") or 0)
            if username:
                channels.append(username)
            elif peer_id < 0:
                channels.append(peer_id)
        if not channels:
            raise ValueError("Выберите каналы или группы для прогрева; для приватных групп обновите список")

        self.log_callback("INFO", f"🔥 Прогрев запущен: {account['phone']} | {config['label']}")
        await self.db.set_account_warmup_status(account["id"], "warming")
        await self.db.update_warmup_session(
            session["id"], status="running", started_at=session.get("started_at") or datetime.now().isoformat()
        )

        consecutive_failures = 0
        while self.running:
            fresh_account = await self.db.get_account(account["id"])
            if fresh_account:
                fresh_account = await self.db.clear_expired_flood(fresh_account)
            if (
                not fresh_account
                or int(fresh_account.get("is_enabled", 1)) != 1
                or fresh_account.get("status") in {"flood", "restricted", "unauthorized", "inactive"}
            ):
                self.log_callback(
                    "WARN", "Аккаунт выключен в настройках или его отправка требует проверки. Прогрев остановлен."
                )
                break
            session = await self.db.get_warmup_session(account["id"])
            if not session:
                break
            if int(session.get("day_current") or 0) < 1:
                session["day_current"] = 1
                await self.db.update_warmup_session(session["id"], day_current=1)
            today = datetime.now().strftime("%Y-%m-%d")
            if session.get("last_reset_date") != today:
                await self.db.reset_warmup_actions_today(session["id"])
                new_day = int(session.get("day_current") or 0) + 1
                await self.db.update_warmup_session(
                    session["id"],
                    day_current=new_day,
                    last_reset_date=today,
                    daily_action_limit=0,
                )
                self.log_callback("INFO", f"📅 День прогрева {new_day}/{config['days']}")
                session["day_current"] = new_day
                session["actions_today"] = 0
                session["daily_action_limit"] = 0

            if int(session.get("day_current") or 0) > int(config["days"]):
                if int(session.get("actions_total") or 0) < 1:
                    # Days elapsed with zero successes — not completed. Reset the day
                    # counter so the next start can retry the same level/topic.
                    await self.db.update_warmup_session(
                        session["id"],
                        day_current=0,
                        last_reset_date=None,
                        actions_today=0,
                    )
                    raise RuntimeError(
                        "Прогрев не завершён: за все дни не выполнено ни одного успешного действия. "
                        "Проверьте аккаунт, каналы и подключение, затем запустите снова."
                    )
                await self.db.update_warmup_session(
                    session["id"],
                    status="completed",
                    completed_at=datetime.now().isoformat(),
                )
                await self.db.set_account_warmup_status(account["id"], "ready")
                self.log_callback("SUCCESS", f"✅ Прогрев завершён! Аккаунт {account['phone']} готов к работе")
                break

            max_actions = int(session.get("daily_action_limit") or 0)
            if max_actions <= 0:
                max_actions = random.randint(config["actions_per_day"]["min"], config["actions_per_day"]["max"])
                await self.db.update_warmup_session(session["id"], daily_action_limit=max_actions)
            if int(session.get("actions_today") or 0) >= max_actions:
                next_day = (datetime.now() + timedelta(days=1)).replace(
                    hour=random.randint(9, 11),
                    minute=random.randint(0, 59),
                    second=0,
                    microsecond=0,
                )
                wait_seconds = (next_day - datetime.now()).total_seconds()
                self.log_callback(
                    "INFO",
                    f"💤 Лимит на сегодня достигнут. Следующие действия в {next_day.strftime('%H:%M')}",
                )
                if not await self._sleep_interruptible(wait_seconds, step=1.0):
                    break
                continue

            action_name = random.choice(config["actions"])
            channel = random.choice(channels)
            result = await self._execute_action(actions_obj, action_name, channel)
            status = "success" if result.get("success") else "error"
            await self.db.add_warmup_log(account["id"], action_name, result.get("detail", ""), status)

            if result.get("success"):
                self.log_callback("SUCCESS", f"✅ {result['detail']}")
                await self.db.update_warmup_session(
                    session["id"],
                    actions_today=int(session.get("actions_today") or 0) + 1,
                    actions_total=int(session.get("actions_total") or 0) + 1,
                    last_action_at=datetime.now().isoformat(),
                )
                consecutive_failures = 0
            else:
                self.log_callback("WARN", f"⚠️ {result.get('detail', 'Неизвестная ошибка')}")
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise RuntimeError(
                        f"Прогрев остановлен: {consecutive_failures} неудачных действий подряд. "
                        "Проверьте аккаунт, каналы, прокси и подключение, затем запустите снова."
                    )

            wait = random.randint(300, 1800)
            self.log_callback("INFO", f"⏳ Следующее действие через {wait // 60} мин")
            if not await self._sleep_interruptible(wait, step=1.0):
                break

    async def _execute_action(self, actions: WarmupActions, name: str, channel: str) -> dict[str, Any]:
        try:
            if name == "go_online":
                return await actions.go_online()
            if name == "read_channel":
                return await actions.read_channel(channel)
            if name == "react_to_post":
                return await actions.react_to_post(channel)
            if name == "view_profile":
                return await actions.view_profile(channel)
            if name == "send_message":
                return await actions.send_message_to_chat(channel)
            return {"success": False, "detail": f"Неизвестное действие: {name}"}
        except (FloodWaitError, PeerFloodError, AuthKeyUnregisteredError):
            raise
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "detail": explain_error(exc)}

    def stop(self) -> None:
        self._stop_requested = True
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()
