from __future__ import annotations

import json
from datetime import datetime

import customtkinter as ctk

from ui.artwork import studio_art
from ui.dispatch import post_ui
from ui.icons import icon
from ui.tab_logs import display_log_message
from ui.theme import COLORS, FONTS, apply_button_role, get_density_preset, init_fonts, style_card, style_tree


class TabDashboard(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color=COLORS["bg"])
        init_fonts()
        self.app = app
        self._refresh_job = None
        self._refreshing = False
        self._last_accounts_sig = None
        self._last_logs_sig = None
        self._density_mode = "normal"
        self._build_ui()
        self._start_auto_refresh()

    def _build_ui(self):
        self.scroll = ctk.CTkScrollableFrame(self, fg_color=COLORS["bg"], corner_radius=0)
        self.scroll.pack(fill="both", expand=True)
        content = self.scroll
        heading = ctk.CTkFrame(content, fg_color="transparent")
        heading.pack(fill="x", padx=18, pady=(10, 16))
        ctk.CTkLabel(heading, text="Ваше рабочее пространство", font=FONTS["heading"]).pack(side="left")
        self.clock_label = ctk.CTkLabel(heading, text="", font=FONTS["small"], text_color=COLORS["muted"])
        self.clock_label.pack(side="right")
        self.support_button = ctk.CTkButton(
            heading,
            text="Поддержать проект",
            height=32,
            width=174,
            image=icon("support", COLORS["accent"], 16),
            command=self.app.show_support,
        )
        apply_button_role(self.support_button, "secondary")
        self.support_button.pack(side="right", padx=(0, 12))
        self._update_clock()
        hero = ctk.CTkFrame(content, fg_color="#191F19", corner_radius=18, border_width=1, border_color="#343C26")
        hero._studio_styled = True
        hero.pack(fill="x", padx=18, pady=(0, 20))
        hero.grid_columnconfigure(0, weight=1)
        copy = ctk.CTkFrame(hero, fg_color="transparent")
        copy.grid(row=0, column=0, sticky="nsew", padx=26, pady=24)
        ctk.CTkLabel(copy, text="TELEGRAM. В ВАШЕМ РИТМЕ.", font=FONTS["label"], text_color=COLORS["accent"]).pack(
            anchor="w"
        )
        ctk.CTkLabel(copy, text="Начните с трёх шагов", font=FONTS["heading"], justify="left", anchor="w").pack(
            anchor="w", pady=(10, 8)
        )
        ctk.CTkLabel(
            copy,
            text="1. Подключите свой Telegram-аккаунт.\n2. Вставьте список людей в «Аудитории».\n3. Выберите режим и настройте «Задачу».",
            font=FONTS["body"],
            text_color="#BAC1B1",
            justify="left",
        ).pack(anchor="w")
        cta = ctk.CTkButton(
            copy,
            text="Пошаговая инструкция",
            width=220,
            height=40,
            image=icon("arrow", COLORS["bg"], 16),
            compound="right",
            command=lambda: self.app.show_help("start"),
        )
        apply_button_role(cta, "primary")
        cta.pack(anchor="w", pady=(18, 0))
        self.hero_art = ctk.CTkLabel(hero, text="", image=studio_art(330, 218))
        self.hero_art.grid(row=0, column=1, sticky="e", padx=(0, 22), pady=18)
        self.bind("<Configure>", self._resize_hero, add="+")
        section = ctk.CTkFrame(content, fg_color="transparent")

        ctk.CTkLabel(section, text="Начните с нужного инструмента", font=FONTS["subhead"]).pack(side="left")
        ctk.CTkLabel(section, text="РАБОЧЕЕ ПРОСТРАНСТВО", font=FONTS["label"], text_color=COLORS["subtle"]).pack(
            side="right"
        )
        tools = ctk.CTkFrame(content, fg_color="transparent")

        tools.grid_columnconfigure((0, 1, 2), weight=1, uniform="tools")
        for column, (key, title, description) in enumerate(
            (
                ("accounts", "Аккаунты", "Подключение и управление"),
                ("targets", "Аудитория", "Списки и результаты"),
                ("ai", "AI-студия", "Модели и генерация"),
            )
        ):
            card = ctk.CTkFrame(
                tools, fg_color=COLORS["surface"], corner_radius=14, border_width=1, border_color=COLORS["border"]
            )
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 0 if column == 2 else 5))
            line = ctk.CTkFrame(card, fg_color="transparent")
            line.pack(fill="x", padx=16, pady=(14, 6))
            ctk.CTkLabel(line, text="", image=icon(key, COLORS["accent"], 22), width=28).pack(side="left")
            button = ctk.CTkButton(
                line,
                text=title,
                image=icon("arrow", COLORS["muted"], 15),
                compound="right",
                anchor="w",
                height=32,
                fg_color="transparent",
                command=lambda tab=key: self.app.switch_tab(tab),
            )
            apply_button_role(button, "ghost")
            button.configure(text_color=COLORS["text"])
            button.pack(side="left", fill="x", expand=True)
            ctk.CTkLabel(card, text=description, font=FONTS["small"], text_color=COLORS["muted"], anchor="w").pack(
                fill="x", padx=16, pady=(0, 16)
            )
        stats_title = ctk.CTkFrame(content, fg_color="transparent")
        stats_title.pack(fill="x", padx=18, pady=(0, 10))
        ctk.CTkLabel(stats_title, text="Сейчас в пространстве", font=FONTS["subhead"]).pack(side="left")
        ctk.CTkLabel(stats_title, text="●  Данные программы", font=FONTS["small"], text_color=COLORS["accent"]).pack(
            side="right"
        )
        top_row = ctk.CTkFrame(content, fg_color="transparent")
        top_row.pack(fill="x", padx=18, pady=(0, 14))
        top_row.grid_columnconfigure((0, 1, 2), weight=1, uniform="stats")
        self.card_accounts = self._make_stat_card(top_row, "АККАУНТЫ", col=0)
        self.card_task = self._make_stat_card(top_row, "РЕЗУЛЬТАТЫ", col=1)
        self.card_warmup = self._make_stat_card(top_row, "ПРОГРЕВ", col=2)
        section.pack(fill="x", padx=18, pady=(4, 12))
        tools.pack(fill="x", padx=18, pady=(0, 22))
        progress_block = ctk.CTkFrame(content, fg_color=COLORS["surface"], corner_radius=14)
        progress_block.pack(fill="x", padx=18, pady=(0, 14))
        progress_header = ctk.CTkFrame(progress_block, fg_color="transparent")
        progress_header.pack(fill="x", padx=18, pady=(14, 10))
        ctk.CTkLabel(progress_header, text="Прогресс аудитории", font=FONTS["subhead"]).pack(side="left")
        self.progress_percent = ctk.CTkLabel(
            progress_header, text="0%", font=FONTS["subhead"], text_color=COLORS["accent"]
        )
        self.progress_percent.pack(side="right")
        self.main_progress = ctk.CTkProgressBar(progress_block, height=6)
        self.main_progress.pack(fill="x", padx=18, pady=(0, 10))
        self.main_progress.set(0)
        self.progress_label = ctk.CTkLabel(
            progress_block, text="Нет активной задачи", font=FONTS["small"], text_color=COLORS["muted"]
        )
        self.progress_label.pack(anchor="w", padx=18, pady=(0, 14))
        bottom = ctk.CTkFrame(content, fg_color="transparent")
        bottom.pack(fill="x", padx=18, pady=(0, 20))
        bottom.grid_columnconfigure((0, 1), weight=1, uniform="details")
        left_block = ctk.CTkFrame(bottom, fg_color=COLORS["surface"], corner_radius=14)
        left_block.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        ctk.CTkLabel(left_block, text="Ваши аккаунты", font=FONTS["subhead"]).pack(anchor="w", padx=18, pady=(16, 10))
        self.accounts_scroll = ctk.CTkScrollableFrame(left_block, fg_color="transparent", height=170)
        self.accounts_scroll.pack(fill="both", expand=True, padx=8, pady=(0, 12))
        right_block = ctk.CTkFrame(bottom, fg_color=COLORS["surface"], corner_radius=14)
        right_block.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        log_header = ctk.CTkFrame(right_block, fg_color="transparent")
        log_header.pack(fill="x", padx=18, pady=(16, 10))
        ctk.CTkLabel(log_header, text="Последние действия", font=FONTS["subhead"]).pack(side="left")
        link = ctk.CTkButton(
            log_header, text="Журнал ↗", width=90, height=26, command=lambda: self.app.switch_tab("logs")
        )
        apply_button_role(link, "ghost")
        link.pack(side="right")
        self.recent_log = ctk.CTkTextbox(right_block, fg_color=COLORS["input"], font=FONTS["mono"], height=170)
        self.recent_log.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        self.recent_log.configure(state="disabled")
        for tag, color in (
            ("SUCCESS", COLORS["success"]),
            ("ERROR", COLORS["error"]),
            ("WARN", COLORS["warning"]),
            ("INFO", COLORS["muted"]),
            ("TIME", COLORS["subtle"]),
        ):
            self.recent_log.tag_config(tag, foreground=color)

    def _resize_hero(self, event):
        if event.widget is not self:
            return
        if event.width / self._get_widget_scaling() < 880:
            self.hero_art.grid_remove()
        else:
            self.hero_art.grid()

    def _safe_after(self, callback) -> None:
        post_ui(self, callback)

    def _make_stat_card(self, parent, title: str, col: int) -> ctk.CTkFrame:
        card = ctk.CTkFrame(
            parent,
            fg_color=COLORS["surface"],
            border_color=COLORS["border"],
            border_width=1,
            corner_radius=12,
        )
        card.grid(
            row=0,
            column=col,
            sticky="nsew",
            padx=(0 if col == 0 else 4, 4 if col < 2 else 0),
            pady=0,
        )
        style_card(card, color=COLORS["accent"])
        ctk.CTkLabel(
            card,
            text=title,
            font=FONTS["label"],
            text_color=COLORS["muted"],
        ).pack(anchor="w", padx=14, pady=(12, 4))
        content = ctk.CTkFrame(card, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        card.content = content  # type: ignore[attr-defined]
        return card

    def _update_clock(self):
        if not self.winfo_exists():
            return
        now = datetime.now().strftime("%d.%m.%Y  %H:%M:%S")
        self.clock_label.configure(text=now)
        self.after(1000, self._update_clock)

    def _start_auto_refresh(self):
        if self.app._closing:
            return
        if self._refresh_job:
            try:
                self.after_cancel(self._refresh_job)
            except Exception:
                pass
        self._refresh()
        self._refresh_job = self.after(5000, self._start_auto_refresh)

    def _refresh(self):
        if self.app._closing:
            return
        if self._refreshing:
            return
        self._refreshing = True
        future = self.app.run_coro(self._fetch_all_data())

        def on_done():
            try:
                data = future.result(timeout=8)
                self._safe_after(lambda: self._update_ui(data))
            except Exception:
                pass
            finally:
                self._refreshing = False

        future.add_done_callback(lambda _: on_done())

    async def _fetch_all_data(self) -> dict:
        db = self.app.db
        accounts = await db.get_accounts()
        active = [a for a in accounts if a.get("status") == "active" and a.get("is_enabled", 1)]
        flood = [a for a in accounts if a.get("status") == "flood"]
        disabled = [a for a in accounts if not a.get("is_enabled", 1)]
        stats = await db.get_targets_stats()
        logs = await db.get_recent_logs(limit=20)
        warmup_sessions = []
        for acc in accounts:
            session = await db.get_warmup_session(acc["id"])
            if session and session.get("status") == "running":
                warmup_sessions.append(
                    {
                        "phone": acc["phone"],
                        "day_current": session.get("day_current", 0),
                        "day_total": session.get("day_total", 7),
                    }
                )
        ai_configs = await db.get_ai_configs()
        total_ai_cost = sum(float(c.get("total_cost_usd") or 0.0) for c in ai_configs)
        return {
            "accounts": accounts,
            "active_count": len(active),
            "flood_count": len(flood),
            "disabled_count": len(disabled),
            "stats": stats,
            "logs": logs,
            "campaign_id": db.campaign_id,
            "campaign_kind": await db.get_campaign_kind(),
            "warmup_sessions": warmup_sessions,
            "ai_configs": ai_configs,
            "total_ai_cost": total_ai_cost,
        }

    def _update_ui(self, data: dict):
        if data["campaign_id"] != self.app.db.campaign_id:
            return
        self._update_accounts_card(data)
        self._update_task_card(data)
        self._update_warmup_card(data)
        self._update_progress(data)
        self._update_accounts_list(data["accounts"], data["campaign_kind"])
        self._update_recent_log(data["logs"])
        style_tree(self)

    def _update_accounts_card(self, data: dict):
        for w in self.card_accounts.content.winfo_children():
            w.destroy()
        total = len(data["accounts"])
        active = data["active_count"]
        flood = data["flood_count"]
        ctk.CTkLabel(
            self.card_accounts.content,
            text=str(total),
            font=FONTS["stat"],
            text_color=COLORS["text"],
        ).pack(anchor="w")
        ctk.CTkLabel(
            self.card_accounts.content,
            text="аккаунтов",
            font=FONTS["small"],
            text_color=COLORS["muted"],
        ).pack(anchor="w", pady=(0, 6))
        details = [(f"{active} активных", COLORS["success"])]
        if flood:
            details.append((f"Пауза Telegram: {flood}", COLORS["warning"]))
        if data["disabled_count"]:
            details.append((f"{data['disabled_count']} отключён", COLORS["muted"]))
        for text, color in details:
            ctk.CTkLabel(
                self.card_accounts.content,
                text=text,
                font=FONTS["small"],
                text_color=color,
            ).pack(anchor="w")

    def _update_task_card(self, data: dict):
        for w in self.card_task.content.winfo_children():
            w.destroy()
        stats = data["stats"]
        added = stats.get("added", 0) + stats.get("sent", 0)
        total = stats.get("total", 0)
        pending = stats.get("pending", 0)
        errors = stats.get("errors", 0)
        ctk.CTkLabel(
            self.card_task.content,
            text=str(added),
            font=FONTS["stat"],
            text_color=COLORS["success"] if added > 0 else COLORS["text"],
        ).pack(anchor="w")
        ctk.CTkLabel(
            self.card_task.content,
            text=f"успешно из {total}",
            font=FONTS["small"],
            text_color=COLORS["muted"],
        ).pack(anchor="w", pady=(0, 6))
        if pending > 0:
            ctk.CTkLabel(
                self.card_task.content,
                text=f"⏳  {pending} ожидает",
                font=FONTS["small"],
                text_color=COLORS["warning"],
            ).pack(anchor="w")
        if errors > 0:
            ctk.CTkLabel(
                self.card_task.content,
                text=f"{errors} ошибок",
                font=FONTS["small"],
                text_color=COLORS["error"],
            ).pack(anchor="w")
        if total == 0:
            ctk.CTkLabel(
                self.card_task.content,
                text="Список не загружен",
                font=FONTS["small"],
                text_color=COLORS["muted"],
            ).pack(anchor="w")

    def _update_warmup_card(self, data: dict):
        for w in self.card_warmup.content.winfo_children():
            w.destroy()
        sessions = data["warmup_sessions"]
        if not sessions:
            ctk.CTkLabel(
                self.card_warmup.content,
                text="—",
                font=FONTS["stat"],
                text_color=COLORS["muted"],
            ).pack(anchor="w")
            ctk.CTkLabel(
                self.card_warmup.content,
                text="прогрев не запущен",
                font=FONTS["small"],
                text_color=COLORS["muted"],
            ).pack(anchor="w")
            return
        ctk.CTkLabel(
            self.card_warmup.content,
            text=str(len(sessions)),
            font=FONTS["stat"],
            text_color=COLORS["accent"],
        ).pack(anchor="w")
        ctk.CTkLabel(
            self.card_warmup.content,
            text="в прогреве",
            font=FONTS["small"],
            text_color=COLORS["muted"],
        ).pack(anchor="w", pady=(0, 6))
        for s in sessions[:2]:
            day = s["day_current"]
            total = s["day_total"]
            phone_short = s["phone"][-4:]
            ctk.CTkLabel(
                self.card_warmup.content,
                text=f"...{phone_short}  День {day}/{total}",
                font=FONTS["small"],
                text_color=COLORS["warning"],
            ).pack(anchor="w")
        if data["total_ai_cost"] > 0:
            cost = data["total_ai_cost"]
            color = COLORS["success"] if cost < 0.01 else COLORS["warning"]
            ctk.CTkLabel(
                self.card_warmup.content,
                text=f"AI: ${cost:.4f}",
                font=FONTS["small"],
                text_color=color,
            ).pack(anchor="w", pady=(4, 0))

    def _update_progress(self, data: dict):
        stats = data["stats"]
        added = stats.get("added", 0) + stats.get("sent", 0)
        total = stats.get("total", 0)
        if total > 0:
            pct = (added + stats.get("skipped", 0) + stats.get("errors", 0)) / total
            self.main_progress.set(pct)
            self.progress_percent.configure(text=f"{int(pct * 100)}%")
            self.progress_label.configure(
                text=f"Успешно {added} из {total}  •  Осталось: {stats.get('pending', 0)}  •  Ошибок: {stats.get('errors', 0)}"
            )
        else:
            self.main_progress.set(0)
            self.progress_percent.configure(text="0%")
            self.progress_label.configure(text="Список не загружен")

    def _update_accounts_list(self, accounts: list, kind="invite"):
        counter_key = "sent_today" if kind == "messages" else "added_today"
        density = get_density_preset(self._density_mode)
        signature = tuple(
            (
                a.get("id"),
                a.get("status"),
                int(a.get("is_enabled", 1) or 0),
                int(a.get(counter_key, 0) or 0),
                int(a.get("daily_limit", 40)),
                a.get("device_profile") or "",
            )
            for a in accounts
        )
        if signature == self._last_accounts_sig:
            return
        self._last_accounts_sig = signature
        for w in self.accounts_scroll.winfo_children():
            w.destroy()
        if not accounts:
            ctk.CTkLabel(
                self.accounts_scroll,
                text="Нет аккаунтов",
                font=FONTS["body"],
                text_color=COLORS["muted"],
            ).pack(pady=16)
            return
        status_map = {
            "active": ("A", COLORS["success"]),
            "flood": ("F", COLORS["warning"]),
            "error": ("E", COLORS["error"]),
            "inactive": ("I", COLORS["muted"]),
            "restricted": ("R", COLORS["error"]),
            "unauthorized": ("!", COLORS["warning"]),
        }
        status_text_map = {
            "active": "Активен",
            "flood": "Пауза Telegram",
            "error": "Ошибка",
            "inactive": "Неактивен",
            "restricted": "Отправка требует проверки",
            "unauthorized": "Нужен вход",
        }
        for acc in accounts:
            card = ctk.CTkFrame(
                self.accounts_scroll,
                fg_color=COLORS["bg"],
                border_color=COLORS["border"],
                border_width=1,
                corner_radius=12,
            )
            card.pack(fill="x", pady=(0, density["card_pad_y"]))
            row1 = ctk.CTkFrame(card, fg_color="transparent")
            row1.pack(fill="x", padx=12, pady=(density["row_pad_y"] * 2, density["row_pad_y"]))
            status = acc.get("status", "inactive")
            if not acc.get("is_enabled", 1):
                status = "inactive"
            dot, color = status_map.get(status, ("I", COLORS["muted"]))
            status_text = status_text_map.get(status, "Неизвестно")
            ctk.CTkLabel(
                row1,
                text=f"{dot}  {acc['phone']}",
                font=FONTS["subhead"],
                text_color=color,
            ).pack(side="left")
            added_today = int(acc.get(counter_key, 0) or 0)
            limit = int(acc.get("daily_limit", 40))
            limit_color = COLORS["error"] if added_today >= limit else COLORS["text"]
            ctk.CTkLabel(
                row1,
                text=f"{added_today}/{limit}",
                font=FONTS["subhead"],
                text_color=limit_color,
            ).pack(side="right")
            row2 = ctk.CTkFrame(card, fg_color="transparent")
            row2.pack(fill="x", padx=12, pady=(0, density["row_pad_y"] * 2))
            device = ""
            if acc.get("device_profile"):
                try:
                    d = json.loads(acc["device_profile"])
                    device = d.get("device_model", "")
                except Exception:
                    pass
            ctk.CTkLabel(
                row2,
                text=f"Устройство: {device}" if device else "Устройство: Неизвестно",
                font=FONTS["label"],
                text_color=COLORS["muted"],
            ).pack(side="left")
            ctk.CTkLabel(
                row2,
                text=status_text,
                font=FONTS["label"],
                text_color=color,
            ).pack(side="right")

    def _update_recent_log(self, logs: list):
        signature = tuple((entry.get("created_at"), entry.get("action"), entry.get("message")) for entry in logs[:15])
        if signature == self._last_logs_sig:
            return
        self._last_logs_sig = signature
        self.recent_log.configure(state="normal")
        self.recent_log.delete("1.0", "end")
        if not logs:
            self.recent_log.insert("end", "Нет записей\n", "INFO")
            return
        icons = {
            "ADD_SUCCESS": ("[УСПЕХ]", "SUCCESS"),
            "ADD_ERROR": ("[ОШИБКА]", "ERROR"),
            "FLOOD_WAIT": ("[ПАУЗА]", "WARN"),
            "SKIP": ("[ПРОПУСК]", "INFO"),
            "CONNECT": ("[СВЯЗЬ]", "INFO"),
            "INFO": ("[СВЕДЕНИЕ]", "INFO"),
            "SUCCESS": ("[УСПЕХ]", "SUCCESS"),
            "ERROR": ("[ОШИБКА]", "ERROR"),
            "WARN": ("[ВНИМАНИЕ]", "WARN"),
        }
        for log in logs[:15]:
            try:
                dt = datetime.fromisoformat(log.get("created_at", ""))
                time_str = dt.strftime("%H:%M")
            except Exception:
                time_str = "--:--"
            action = log.get("action", "INFO")
            icon, tag = icons.get(action, ("•", "INFO"))
            msg = display_log_message(log.get("message", ""))[:60]
            self.recent_log.insert("end", f"[{time_str}] ", "TIME")
            self.recent_log.insert("end", f"{icon} {msg}\n", tag)
        self.recent_log.see("end")
        self.recent_log.configure(state="disabled")

    def destroy(self):
        if self._refresh_job:
            try:
                self.after_cancel(self._refresh_job)
            except Exception:
                pass
            self._refresh_job = None
        super().destroy()

    def on_density_change(self, mode: str) -> None:
        self._density_mode = "compact" if mode == "compact" else "normal"
        self._last_accounts_sig = None
        self._last_logs_sig = None
        self._refresh()
