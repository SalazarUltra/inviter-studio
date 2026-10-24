"""
Вкладка управления прогревом аккаунтов.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError
from datetime import datetime

import customtkinter as ctk

from core.account_manager import AccountManager
from core.user_feedback import explain_error
from core.warmup_engine import WarmupEngine
from ui.components import empty_state, page_intro
from ui.dispatch import post_ui
from ui.hints import tip
from ui.theme import COLORS, FONTS, init_fonts, style_card, style_tree

WARMUP_ACTION_LABELS = {
    "go_online": "Статус онлайн",
    "read_channel": "Чтение чата",
    "react_to_post": "Реакция на запись",
    "view_profile": "Просмотр профиля",
    "send_message": "Сообщение в чат",
}


class TabWarmup(ctk.CTkFrame):
    MAX_LOG_LINES = 600
    TONE_LABELS = {"neutral": "Нейтральный", "friendly": "Дружелюбный", "expert": "Экспертный"}

    def __init__(self, parent, db, run_coro, async_loop):
        super().__init__(parent, fg_color=COLORS["bg"])
        init_fonts()
        self.db = db
        self.run_coro = run_coro
        self.async_loop = async_loop
        self.warmup_engine: WarmupEngine | None = None
        self.warmup_future = None
        self.accounts_map: dict[str, dict] = {}
        self.channel_vars: dict[int, ctk.BooleanVar] = {}
        self.channel_rows: dict[int, dict] = {}
        self.current_account_id: int | None = None
        self.ai_memory_enabled = ctk.BooleanVar(value=True)
        self.ai_tone_var = ctk.StringVar(value="Нейтральный")
        self._status_refresh_job = None
        self._ai_refresh_job = None
        self._build_ui()
        self._load_ai_memory_setting()
        self._load_ai_behavior_settings()
        self._load_accounts()

    def _safe_after(self, callback) -> None:
        post_ui(self, callback)

    def _build_ui(self):
        page_intro(
            self,
            "Прогрев аккаунтов",
            "Действия одного аккаунта в выбранных чатах по плану на несколько дней.",
            "ПРОГРЕВ АККАУНТОВ",
        )
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=16, pady=(4, 12))
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        main.grid_columnconfigure(0, weight=1)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(0, weight=1)

        left = ctk.CTkScrollableFrame(
            main, fg_color=COLORS["surface"], border_color=COLORS["border"], border_width=1, corner_radius=12
        )
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        style_card(left, color=COLORS["accent"])

        ctk.CTkLabel(left, text="НАСТРОЙКИ", font=FONTS["label"], text_color=COLORS["muted"]).pack(
            anchor="w", padx=16, pady=(16, 8)
        )
        ctk.CTkLabel(left, text="Аккаунт", font=FONTS["body"], text_color=COLORS["muted"]).pack(
            anchor="w", padx=16, pady=(0, 4)
        )
        self.account_combo = ctk.CTkComboBox(
            left,
            values=["Загрузка..."],
            height=36,
            font=FONTS["body"],
            fg_color=COLORS["bg"],
            border_color=COLORS["border"],
            button_color=COLORS["accent"],
            dropdown_fg_color=COLORS["surface"],
            text_color=COLORS["text"],
            corner_radius=12,
            command=self._on_account_selected,
        )
        self.account_combo.pack(fill="x", padx=16, pady=(0, 12))
        tip(
            self.account_combo,
            "Для прогрева выберите один аккаунт. Это отдельная задача; для отправки готового текста прогрев не обязателен.",
        )

        ctk.CTkLabel(left, text="Уровень прогрева", font=FONTS["body"], text_color=COLORS["muted"]).pack(
            anchor="w", padx=16, pady=(0, 4)
        )
        self.level_var = ctk.StringVar(value="medium")
        for label, value in [
            ("Лёгкий · 3 дня / чтение и реакции", "light"),
            ("Средний · 7 дней / сообщения в чатах", "medium"),
            ("Интенсивный · 14 дней / диалоги AI", "intensive"),
        ]:
            ctk.CTkRadioButton(
                left,
                text=label,
                variable=self.level_var,
                value=value,
                font=FONTS["small"],
                text_color=COLORS["text"],
                fg_color=COLORS["accent"],
                hover_color=COLORS["accent"],
            ).pack(anchor="w", padx=24, pady=2)

        ctk.CTkLabel(left, text="Тематика сообщений", font=FONTS["body"], text_color=COLORS["muted"]).pack(
            anchor="w", padx=16, pady=(12, 4)
        )
        self.topic_entry = ctk.CTkEntry(
            left,
            placeholder_text="крипто, инвестиции, финансы...",
            height=36,
            font=FONTS["body"],
            fg_color=COLORS["bg"],
            border_color=COLORS["border"],
            text_color=COLORS["text"],
            corner_radius=12,
        )
        self.topic_entry.pack(fill="x", padx=16, pady=(0, 8))

        memory_frame = ctk.CTkFrame(left, fg_color="transparent")
        memory_frame.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(
            memory_frame,
            text="Память диалога AI",
            font=FONTS["body"],
            text_color=COLORS["muted"],
        ).pack(side="left")
        self.memory_switch = ctk.CTkSwitch(
            memory_frame,
            text="",
            variable=self.ai_memory_enabled,
            command=self._on_ai_memory_toggle,
            fg_color=COLORS["border"],
            progress_color=COLORS["accent"],
        )
        self.memory_switch.pack(side="right")

        ctk.CTkLabel(left, text="Выбранные чаты", font=FONTS["label"], text_color=COLORS["muted"]).pack(
            anchor="w", padx=16, pady=(8, 4)
        )
        channels_header = ctk.CTkFrame(left, fg_color="transparent")
        channels_header.pack(fill="x", padx=16, pady=(0, 6))
        ctk.CTkLabel(
            channels_header, text="Каналы в которых состоит аккаунт", font=FONTS["small"], text_color=COLORS["muted"]
        ).pack(side="left")
        ctk.CTkButton(
            channels_header,
            text="Обновить",
            width=90,
            height=28,
            corner_radius=12,
            fg_color=COLORS["border"],
            hover_color=COLORS["bg"],
            text_color=COLORS["accent"],
            border_color=COLORS["accent"],
            border_width=1,
            font=FONTS["small"],
            command=self._sync_channels,
        ).pack(side="right")

        self.channels_scroll = ctk.CTkScrollableFrame(
            left,
            fg_color=COLORS["bg"],
            border_color=COLORS["border"],
            border_width=1,
            corner_radius=12,
            height=160,
        )
        self.channels_scroll.pack(fill="x", padx=16, pady=(0, 8))

        memory_actions = ctk.CTkFrame(left, fg_color="transparent")
        memory_actions.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkButton(
            memory_actions,
            text="🧹 Очистить память аккаунта",
            height=30,
            corner_radius=12,
            font=FONTS["small"],
            fg_color=COLORS["surface"],
            border_color=COLORS["border"],
            border_width=1,
            text_color=COLORS["muted"],
            command=self._clear_memory_for_account,
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(
            memory_actions,
            text="🧽 Очистить память выбранных",
            height=30,
            corner_radius=12,
            font=FONTS["small"],
            fg_color=COLORS["surface"],
            border_color=COLORS["accent"],
            border_width=1,
            text_color=COLORS["accent"],
            command=self._clear_memory_for_selected_channels,
        ).pack(side="left", fill="x", expand=True, padx=(4, 0))

        ctk.CTkLabel(left, text="Стиль и частота AI", font=FONTS["label"], text_color=COLORS["muted"]).pack(
            anchor="w", padx=16, pady=(4, 4)
        )
        ai_behavior = ctk.CTkFrame(
            left, fg_color=COLORS["bg"], border_color=COLORS["border"], border_width=1, corner_radius=12
        )
        ai_behavior.pack(fill="x", padx=16, pady=(0, 8))

        tone_row = ctk.CTkFrame(ai_behavior, fg_color="transparent")
        tone_row.pack(fill="x", padx=10, pady=(8, 6))
        ctk.CTkLabel(tone_row, text="Тон", font=FONTS["small"], text_color=COLORS["muted"]).pack(side="left")
        self.ai_tone_menu = ctk.CTkOptionMenu(
            tone_row,
            values=list(self.TONE_LABELS.values()),
            variable=self.ai_tone_var,
            height=30,
            width=150,
            font=FONTS["small"],
        )
        self.ai_tone_menu.pack(side="right")

        limit_row = ctk.CTkFrame(ai_behavior, fg_color="transparent")
        limit_row.pack(fill="x", padx=10, pady=(0, 6))
        ctk.CTkLabel(limit_row, text="Лимит/час", font=FONTS["small"], text_color=COLORS["muted"]).pack(side="left")
        self.ai_hourly_entry = ctk.CTkEntry(limit_row, width=80, height=30, font=FONTS["small"])
        self.ai_hourly_entry.pack(side="right")
        self.ai_hourly_entry.insert(0, "6")

        cooldown_row = ctk.CTkFrame(ai_behavior, fg_color="transparent")
        cooldown_row.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(cooldown_row, text="Пауза в чате (мин)", font=FONTS["small"], text_color=COLORS["muted"]).pack(
            side="left"
        )
        self.ai_cooldown_entry = ctk.CTkEntry(cooldown_row, width=80, height=30, font=FONTS["small"])
        self.ai_cooldown_entry.pack(side="right")
        self.ai_cooldown_entry.insert(0, "12")

        ctk.CTkButton(
            ai_behavior,
            text="Сохранить AI-параметры",
            height=30,
            corner_radius=12,
            font=FONTS["small"],
            fg_color=COLORS["surface"],
            hover_color=COLORS["border"],
            border_color=COLORS["accent"],
            border_width=1,
            text_color=COLORS["accent"],
            command=self._save_ai_behavior_settings,
        ).pack(fill="x", padx=10, pady=(0, 10))

        btn_frame = footer
        self.btn_start = ctk.CTkButton(
            btn_frame,
            text="Запустить прогрев",
            height=44,
            corner_radius=12,
            font=FONTS["subhead"],
            fg_color=COLORS["accent"],
            hover_color=COLORS["error"],
            text_color=COLORS["text"],
            command=self._start_warmup,
        )
        self.btn_start.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.btn_stop = ctk.CTkButton(
            btn_frame,
            text="Остановить",
            height=36,
            corner_radius=12,
            font=FONTS["body"],
            fg_color=COLORS["surface"],
            hover_color=COLORS["border"],
            text_color=COLORS["muted"],
            border_color=COLORS["border"],
            border_width=1,
            command=self._stop_warmup,
        )
        self.btn_stop.pack(side="left", fill="x", expand=True)
        tip(
            self.btn_start,
            "Начать действия в выбранных чатах от имени одного аккаунта. Некоторые уровни включают отправку сообщений.",
        )
        tip(self.btn_stop, "Остановить прогрев. После закрытия программы он автоматически не возобновляется.")
        self._set_warmup_controls(False)

        right = ctk.CTkFrame(
            main, fg_color=COLORS["surface"], border_color=COLORS["border"], border_width=1, corner_radius=12
        )
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        style_card(right, color=COLORS["accent"])
        ctk.CTkLabel(right, text="СТАТУС", font=FONTS["label"], text_color=COLORS["muted"]).pack(
            anchor="w", padx=16, pady=(16, 8)
        )

        progress_block = ctk.CTkFrame(
            right, fg_color=COLORS["bg"], border_color=COLORS["border"], border_width=1, corner_radius=12
        )
        progress_block.pack(fill="x", padx=16, pady=(0, 12))
        self.status_account = ctk.CTkLabel(
            progress_block, text="Аккаунт не выбран", font=FONTS["subhead"], text_color=COLORS["text"]
        )
        self.status_account.pack(anchor="w", padx=12, pady=(10, 2))
        self.status_label = ctk.CTkLabel(
            progress_block, text="Ожидание", font=FONTS["body"], text_color=COLORS["muted"]
        )
        self.status_label.pack(anchor="w", padx=12, pady=(0, 8))
        self.progress_bar = ctk.CTkProgressBar(
            progress_block, progress_color=COLORS["accent"], fg_color=COLORS["border"], height=6, corner_radius=12
        )
        self.progress_bar.pack(fill="x", padx=12, pady=(0, 8))
        self.progress_bar.set(0)
        stats_row = ctk.CTkFrame(progress_block, fg_color="transparent")
        stats_row.pack(fill="x", padx=12, pady=(0, 10))
        self.day_label = ctk.CTkLabel(stats_row, text="День: —", font=FONTS["small"], text_color=COLORS["muted"])
        self.day_label.pack(side="left")
        self.actions_label = ctk.CTkLabel(
            stats_row, text="Действий сегодня: —", font=FONTS["small"], text_color=COLORS["muted"]
        )
        self.actions_label.pack(side="right")

        ai_stats_block = ctk.CTkFrame(
            right,
            fg_color=COLORS["bg"],
            border_color=COLORS["border"],
            border_width=1,
            corner_radius=12,
        )
        ai_stats_block.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(ai_stats_block, text="Использование AI", font=FONTS["label"], text_color=COLORS["muted"]).pack(
            anchor="w", padx=10, pady=(8, 2)
        )
        ai_row = ctk.CTkFrame(ai_stats_block, fg_color="transparent")
        ai_row.pack(fill="x", padx=10, pady=(0, 8))
        self.ai_tokens_label = ctk.CTkLabel(ai_row, text="Токенов: 0", font=FONTS["small"], text_color=COLORS["muted"])
        self.ai_tokens_label.pack(side="left")
        self.ai_cost_label = ctk.CTkLabel(
            ai_row, text="Стоимость: см. кабинет провайдера", font=FONTS["small"], text_color=COLORS["success"]
        )
        self.ai_cost_label.pack(side="right")

        ctk.CTkLabel(right, text="История действий", font=FONTS["label"], text_color=COLORS["muted"]).pack(
            anchor="w", padx=16, pady=(4, 4)
        )
        self.log_box = ctk.CTkTextbox(
            right,
            fg_color=COLORS["bg"],
            text_color=COLORS["text"],
            font=ctk.CTkFont(family="Consolas", size=11),
            border_color=COLORS["border"],
            border_width=1,
            corner_radius=12,
            state="normal",
        )
        self.log_box.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.log_box.tag_config("SUCCESS", foreground=COLORS["success"])
        self.log_box.tag_config("ERROR", foreground=COLORS["error"])
        self.log_box.tag_config("INFO", foreground=COLORS["muted"])
        self.log_box.tag_config("WARN", foreground=COLORS["warning"])

    def _load_ai_memory_setting(self) -> None:
        fut = self.run_coro(self.db.get_setting("ai_memory_enabled", "1"))

        def done(f):
            try:
                value = str(f.result() or "1")
            except Exception:
                value = "1"

            def apply_setting():
                self.ai_memory_enabled.set(value == "1")

            self._safe_after(apply_setting)

        fut.add_done_callback(done)

    def _on_ai_memory_toggle(self) -> None:
        enabled = bool(self.ai_memory_enabled.get())
        fut = self.run_coro(self.db.set_setting("ai_memory_enabled", "1" if enabled else "0"))

        def done(result):
            def render():
                self._log_operation(result, f"Память AI {'включена' if enabled else 'выключена'}")
                if not result.cancelled() and result.exception():
                    self._load_ai_memory_setting()

            self._safe_after(render)

        fut.add_done_callback(done)

    def _load_ai_behavior_settings(self) -> None:
        async def load_all():
            tone = await self.db.get_setting("ai_style_tone", "neutral")
            hourly = await self.db.get_setting("ai_hourly_limit", "6")
            cooldown = await self.db.get_setting("ai_chat_cooldown_min", "12")
            return {
                "tone": str(tone or "neutral").strip().lower(),
                "hourly": str(hourly or "6").strip(),
                "cooldown": str(cooldown or "12").strip(),
            }

        fut = self.run_coro(load_all())

        def done(f):
            try:
                data = f.result()
            except Exception:
                data = {"tone": "neutral", "hourly": "6", "cooldown": "12"}

            def apply():
                tone = data.get("tone", "neutral")
                if tone not in ("neutral", "friendly", "expert"):
                    tone = "neutral"
                self.ai_tone_var.set(self.TONE_LABELS[tone])
                self.ai_hourly_entry.delete(0, "end")
                self.ai_hourly_entry.insert(0, data.get("hourly", "6"))
                self.ai_cooldown_entry.delete(0, "end")
                self.ai_cooldown_entry.insert(0, data.get("cooldown", "12"))

            self._safe_after(apply)

        fut.add_done_callback(done)

    def _save_ai_behavior_settings(self) -> None:
        labels_to_keys = {label: key for key, label in self.TONE_LABELS.items()}
        tone = labels_to_keys.get(self.ai_tone_var.get(), "neutral")
        try:
            hourly_limit = int(self.ai_hourly_entry.get().strip() or "6")
            cooldown_min = int(self.ai_cooldown_entry.get().strip() or "12")
        except Exception:
            self._log("ERROR", "Лимиты должны быть целыми числами")
            return

        if not 1 <= hourly_limit <= 30 or not 1 <= cooldown_min <= 180:
            self._log("ERROR", "Укажите лимит от 1 до 30 сообщений в час и паузу от 1 до 180 минут")
            return

        async def save_all():
            await self.db.set_setting("ai_style_tone", tone)
            await self.db.set_setting("ai_hourly_limit", str(hourly_limit))
            await self.db.set_setting("ai_chat_cooldown_min", str(cooldown_min))

        fut = self.run_coro(save_all())

        def done(result):
            self._safe_after(lambda: self._log_operation(result, "AI-параметры сохранены"))

        fut.add_done_callback(done)

    def _load_accounts(self):
        fut = self.run_coro(self.db.get_accounts())

        def done(f):
            try:
                accounts = f.result()
            except Exception:
                self._safe_after(lambda: self.account_combo.configure(values=["Загрузка..."]))
                self._safe_after(lambda: self.account_combo.set("Загрузка..."))
                self._safe_after(lambda: self.after(2000, self._load_accounts))
                return

            def apply_accounts():
                current_value = self.account_combo.get().strip()
                states = {
                    "cold": "Не прогревался",
                    "warming": "В процессе",
                    "paused": "На паузе",
                    "ready": "Готов",
                    "error": "Ошибка",
                }
                self.accounts_map = {
                    f"{a['phone']} · {states.get(a.get('warmup_status') or 'cold', 'Ожидание') if a.get('is_enabled', 1) else 'Отключён'}": a
                    for a in accounts
                }
                values = list(self.accounts_map.keys())
                if not values:
                    self.account_combo.configure(values=["Нет аккаунтов"])
                    self.account_combo.set("Нет аккаунтов")
                    self.btn_start.configure(state="disabled")
                    return
                self.account_combo.configure(values=values)
                if current_value in self.accounts_map:
                    self.account_combo.set(current_value)
                    self._on_account_selected(current_value)
                else:
                    default = next(
                        (key for key, item in self.accounts_map.items() if item.get("is_enabled", 1)), values[0]
                    )
                    self.account_combo.set(default)
                    self._on_account_selected(default)

            self._safe_after(apply_accounts)

        fut.add_done_callback(done)

    def _on_account_selected(self, value):
        account = self.accounts_map.get(value)
        if not account:
            return
        self.current_account_id = int(account["id"])
        self.status_account.configure(text=account["phone"])
        if not self.warmup_future or self.warmup_future.done():
            self.btn_start.configure(state="normal" if account.get("is_enabled", 1) else "disabled")
        self._load_session(account["id"])
        self._populate_channels_list(account["id"])
        self._load_warmup_logs(account["id"])

    def _load_session(self, account_id: int) -> None:
        fut = self.run_coro(self.db.get_warmup_session(account_id))

        def done(f):
            try:
                session = f.result()
            except Exception:
                session = None
            if session:
                self._safe_after(lambda: self._update_progress_ui(session))
            else:
                self._safe_after(
                    lambda: self._update_progress_ui(
                        {"status": "idle", "day_current": 0, "day_total": 7, "actions_today": 0}
                    )
                )

        fut.add_done_callback(done)

    def _load_warmup_logs(self, account_id: int) -> None:
        fut = self.run_coro(self.db.get_warmup_logs(account_id, limit=120))

        def done(f):
            try:
                logs = f.result()
            except Exception:
                logs = []
            self._safe_after(lambda: self._render_warmup_logs(logs))

        fut.add_done_callback(done)

    def _render_warmup_logs(self, logs: list[dict]) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        if not logs:
            self.log_box.insert("end", "[--:--:--]  Нет сохранённых записей\n", "INFO")
            self.log_box.see("end")
            return

        for entry in reversed(logs):
            try:
                dt = datetime.fromisoformat(str(entry.get("created_at") or ""))
                time_str = dt.strftime("%H:%M:%S")
            except Exception:
                time_str = "--:--:--"

            action = WARMUP_ACTION_LABELS.get(str(entry.get("action") or ""), "Действие")
            detail = str(entry.get("detail") or "")
            status = str(entry.get("status") or "").lower()
            if status == "success":
                tag = "SUCCESS"
                icon = "[УСПЕХ]"
            elif status == "error":
                tag = "ERROR"
                icon = "[ОШИБКА]"
            else:
                tag = "WARN"
                icon = "[ВНИМАНИЕ]"
            line = f"[{time_str}]  {icon} {action}: {detail}\n"
            self.log_box.insert("end", line, tag)

        self.log_box.see("end")
        self._trim_log_lines()

    def _sync_channels(self):
        selected = self.account_combo.get()
        account = self.accounts_map.get(selected)
        if not account:
            return
        self._log("INFO", "Загрузка каналов аккаунта...")

        async def do_sync():
            mgr = AccountManager(account, self.db)
            try:
                await mgr.connect()
                dialogs = await mgr.get_joined_dialogs()
            finally:
                await mgr.disconnect()
            await self.db.save_account_channels(account["id"], dialogs)
            return dialogs

        future = asyncio.run_coroutine_threadsafe(do_sync(), self.async_loop)

        def on_done():
            try:
                dialogs = future.result(timeout=30)
                self._safe_after(lambda: self._populate_channels_list(account["id"]))
                self._safe_after(lambda: self._log("SUCCESS", f"Загружено {len(dialogs)} каналов/групп"))
            except Exception as exc:  # noqa: BLE001
                self._safe_after(lambda exc=exc: self._log("ERROR", "Ошибка: " + explain_error(exc)))

        future.add_done_callback(lambda _: on_done())

    def _populate_channels_list(self, account_id: int):
        fut = self.run_coro(self.db.get_account_channels(account_id))

        def done(f):
            try:
                channels = f.result()
            except Exception:
                channels = []
            self._safe_after(lambda: self._render_channels(channels))

        fut.add_done_callback(done)

    def _render_channels(self, channels: list[dict]) -> None:
        for w in self.channels_scroll.winfo_children():
            w.destroy()
        self.channel_vars.clear()
        self.channel_rows.clear()
        if not channels:
            empty_state(
                self.channels_scroll,
                "Выберите свои чаты",
                "Обновите список диалогов аккаунта, затем отметьте нужные чаты.",
                "targets",
            )
            return
        for ch in channels:
            row = ctk.CTkFrame(self.channels_scroll, fg_color="transparent")
            row.pack(fill="x", pady=1)
            var = ctk.BooleanVar(value=bool(ch.get("is_selected", 0)))
            self.channel_vars[ch["id"]] = var
            self.channel_rows[ch["id"]] = ch
            icon = "📢" if ch.get("channel_type") == "channel" else "👥"
            title = ch.get("title") or ch.get("username") or f"ID:{ch.get('channel_id')}"
            members_count = int(ch.get("members_count") or 0)
            members = f"  {members_count:,}" if members_count else ""
            ctk.CTkCheckBox(
                row,
                text=f"{icon} {title}{members}",
                variable=var,
                font=FONTS["small"],
                text_color=COLORS["text"],
                fg_color=COLORS["accent"],
                hover_color=COLORS["warning"],
                checkmark_color=COLORS["bg"],
                command=lambda c=ch, v=var: self._toggle_channel(c, v),
            ).pack(anchor="w", padx=4)

        style_tree(self.channels_scroll)

    def _toggle_channel(self, channel: dict, var: ctk.BooleanVar):
        future = self.run_coro(self.db.toggle_channel_selected(channel["id"], bool(var.get())))
        future.add_done_callback(lambda result: self._safe_after(lambda: self._log_operation(result, None)))

    def _log_operation(self, result, success):
        if result.cancelled():
            return
        if result.exception():
            self._log("ERROR", "Изменения не сохранены: " + str(result.exception()))
        elif success:
            self._log("SUCCESS", success)

    def _clear_memory_for_account(self) -> None:
        account_id = self.current_account_id
        if not account_id:
            self._log("WARN", "Сначала выберите аккаунт")
            return
        fut = self.run_coro(self.db.clear_ai_memory_for_account(account_id))

        def done(result):
            self._safe_after(lambda: self._log_operation(result, "Память AI очищена для аккаунта"))

        fut.add_done_callback(done)

    def _clear_memory_for_selected_channels(self) -> None:
        account_id = self.current_account_id
        if not account_id:
            self._log("WARN", "Сначала выберите аккаунт")
            return
        selected_keys: list[str] = []
        for row_id, var in self.channel_vars.items():
            if not bool(var.get()):
                continue
            ch = self.channel_rows.get(row_id) or {}
            username = str(ch.get("username") or "").strip()
            channel_id = str(ch.get("channel_id") or "").strip()
            if username:
                selected_keys.append(username)
            elif channel_id:
                selected_keys.append(channel_id)
        if not selected_keys:
            self._log("WARN", "Нет отмеченных каналов для очистки памяти")
            return
        fut = self.run_coro(self.db.clear_ai_memory_for_chat_keys(account_id, selected_keys))

        def done(f):
            try:
                deleted = int(f.result() or 0)
            except Exception as exc:
                self._safe_after(lambda exc=exc: self._log("ERROR", "Память не очищена: " + explain_error(exc)))
                return
            self._safe_after(
                lambda: self._log("SUCCESS", f"Память очищена для {len(selected_keys)} чатов (сообщений: {deleted})")
            )

        fut.add_done_callback(done)

    def _update_progress_ui(self, session: dict):
        day = int(session.get("day_current") or 0)
        total = int(session.get("day_total") or 7)
        progress = min(day / total, 1.0) if total > 0 else 0
        self.progress_bar.set(progress)
        self.day_label.configure(text=f"День: {day}/{total}")
        self.actions_label.configure(text=f"Действий сегодня: {int(session.get('actions_today') or 0)}")
        status_map = {
            "idle": ("Ожидание", COLORS["muted"]),
            "running": ("Идёт прогрев", COLORS["success"]),
            "paused": ("На паузе", COLORS["warning"]),
            "error": ("Ошибка", COLORS["error"]),
            "completed": ("Завершён", COLORS["success"]),
        }
        text, color = status_map.get(session.get("status", "idle"), ("Неизвестно", COLORS["muted"]))
        self.status_label.configure(text=text, text_color=color)

    def _log(self, level: str, message: str):
        def append_line():
            time_str = datetime.now().strftime("%H:%M:%S")
            tag = level.upper()
            icons = {
                "SUCCESS": "[УСПЕХ]",
                "ERROR": "[ОШИБКА]",
                "INFO": "[СВЕДЕНИЕ]",
                "WARN": "[ВНИМАНИЕ]",
            }
            line = f"[{time_str}]  {icons.get(tag, '')} {message}\n"
            self.log_box.configure(state="normal")
            self.log_box.insert("end", line, tag)
            self.log_box.see("end")
            self.log_box.configure(state="normal")
            self._trim_log_lines()

        self._safe_after(append_line)

    def _trim_log_lines(self) -> None:
        try:
            current_lines = int(float(self.log_box.index("end-1c").split(".")[0]))
        except Exception:
            return
        if current_lines <= self.MAX_LOG_LINES:
            return
        to_remove = current_lines - self.MAX_LOG_LINES
        self.log_box.delete("1.0", f"{to_remove + 1}.0")

    def _set_warmup_controls(self, running: bool) -> None:
        self.account_combo.configure(state="disabled" if running else "normal")
        if running:
            self.btn_start.configure(state="disabled", text="Прогрев запущен")
            self.btn_stop.configure(state="normal")
        else:
            self.btn_start.configure(state="normal", text="Запустить прогрев")
            self.btn_stop.configure(state="disabled")

    def _update_ai_stats(self):
        if self.warmup_engine and self.warmup_engine.ai_writer:
            stats = self.warmup_engine.ai_writer.get_session_stats()
            self.ai_tokens_label.configure(text=f"Токенов: {int(stats.get('tokens', 0)):,}")
            self.ai_cost_label.configure(text=f"Стоимость: {stats.get('cost_formatted', '$0.0000')}")
        if self.warmup_engine and self.warmup_engine.running:
            if self._ai_refresh_job:
                try:
                    self.after_cancel(self._ai_refresh_job)
                except Exception:
                    pass
            self._ai_refresh_job = self.after(10000, self._update_ai_stats)

    def _start_warmup(self):
        if self.winfo_toplevel().parsing_tab.busy():
            self._log("WARN", "Завершите или остановите парсинг перед прогревом")
            return
        if self.warmup_future and not self.warmup_future.done():
            self._log("WARN", "Прогрев уже выполняется")
            return
        selected = self.account_combo.get()
        account = self.accounts_map.get(selected)
        if not account:
            return
        if AccountManager.is_session_busy(account["phone"], account.get("session_name")):
            self._log(
                "WARN", "Этот аккаунт уже используется. Дождитесь завершения задачи, парсинга или другой операции."
            )
            return
        self.current_account_id = int(account["id"])
        level = self.level_var.get()
        topic = self.topic_entry.get().strip() or "общее"
        self.warmup_engine = WarmupEngine(self.db, self._log)
        future = self.run_coro(self.warmup_engine.start(account, level, topic))
        self.warmup_future = future
        self._set_warmup_controls(True)
        self.btn_start.configure(text="Подключаемся к аккаунту…")
        self._log("INFO", f"Подключаем аккаунт {account['phone']} для прогрева…")

        def report_started():
            if future.done():
                return
            if self.warmup_engine.client is None:
                self.after(200, report_started)
                return
            self.btn_start.configure(text="Прогрев выполняется")
            self._log("INFO", f"Подключение выполнено; прогрев начат для {account['phone']}")

        self.after(200, report_started)

        def done(f):
            try:
                _ = f.result()
            except FutureCancelledError:
                # The engine re-raises cancellation after logging "приостановлен".
                pass
            except Exception as exc:  # noqa: BLE001
                self._safe_after(lambda exc=exc: self._log("ERROR", f"Ошибка прогрева: {exc}"))
            finally:
                self._safe_after(lambda: self._set_warmup_controls(False))

        future.add_done_callback(done)
        if self._ai_refresh_job:
            try:
                self.after_cancel(self._ai_refresh_job)
            except Exception:
                pass
        self._ai_refresh_job = self.after(10000, self._update_ai_stats)
        if self._status_refresh_job:
            try:
                self.after_cancel(self._status_refresh_job)
            except Exception:
                pass
        self._status_refresh_job = self.after(30000, self._refresh_status)

    def _stop_warmup(self):
        if self.warmup_engine:
            self.async_loop.call_soon_threadsafe(self.warmup_engine.stop)
            self.btn_stop.configure(state="disabled")
            self.btn_start.configure(state="disabled", text="Останавливаем прогрев...")
            self._log("INFO", "Завершение прогрева...")

    def _refresh_status(self):
        selected = self.account_combo.get()
        account = self.accounts_map.get(selected)
        if account and self.warmup_engine and self.warmup_engine.running:
            self._load_session(account["id"])
            if self._status_refresh_job:
                try:
                    self.after_cancel(self._status_refresh_job)
                except Exception:
                    pass
            self._status_refresh_job = self.after(30000, self._refresh_status)
        elif self.warmup_engine and not self.warmup_engine.running:
            self._set_warmup_controls(False)

    def destroy(self):
        if self._status_refresh_job:
            try:
                self.after_cancel(self._status_refresh_job)
            except Exception:
                pass
            self._status_refresh_job = None
        if self._ai_refresh_job:
            try:
                self.after_cancel(self._ai_refresh_job)
            except Exception:
                pass
            self._ai_refresh_job = None
        super().destroy()
