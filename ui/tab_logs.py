from __future__ import annotations

import asyncio
import re
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox

import customtkinter as ctk

from core.database import Database
from core.export import write_logs, write_logs_xlsx
from core.user_feedback import explain_error
from ui.campaigns import CampaignBar
from ui.components import page_intro
from ui.dispatch import post_ui
from ui.theme import COLORS, FONTS, init_fonts, style_card

LOG_COLORS = {
    "SUCCESS": COLORS["success"],
    "ERROR": COLORS["error"],
    "FLOOD": COLORS["warning"],
    "INFO": COLORS["muted"],
    "WARN": COLORS["warning"],
    "SKIP": COLORS["accent"],
}
ICONS = {
    "SUCCESS": "[УСПЕХ]",
    "ERROR": "[ОШИБКА]",
    "FLOOD": "[ПАУЗА]",
    "INFO": "[СВЕДЕНИЕ]",
    "WARN": "[ВНИМАНИЕ]",
    "SKIP": "[ПРОПУСК]",
}
ACTION_LABELS = {
    "FOLLOWUP_WAIT": "Ожидание ответа для второго сообщения",
    "FOLLOWUP_REPLY": "Получен ответ на первое сообщение",
    "FOLLOWUP_SENT": "Второе сообщение отправлено",
    "FOLLOWUP_BLOCKED": "Второе сообщение недоступно",
    "FOLLOWUP_UNCERTAIN": "Второе сообщение требует проверки",
    "FOLLOWUP_CANCELLED": "Второе сообщение отменено",
    "CONNECT": "Рабочее соединение открыто",
    "CONNECT_FAILED": "Не удалось подключить аккаунт",
    "DISCONNECT": "Рабочее соединение закрыто",
    "ADD_SUCCESS": "Пользователь добавлен в чат",
    "MESSAGE_SENT": "Сообщение отправлено",
    "ADD_ERROR": "Ошибка приглашения",
    "FLOOD_WAIT": "Telegram потребовал паузу",
    "SKIP": "Получатель пропущен",
    "AUDIENCE_EDIT": "Список получателей изменён",
    "MESSAGE_PRIVATE": "Личные сообщения закрыты",
    "MESSAGE_NOT_FOUND": "Получатель не найден",
    "MESSAGE_UNCERTAIN": "Результат отправки требует проверки",
    "MESSAGE_STOPPED": "Отправка остановлена Telegram",
    "MESSAGE_SKIP": "Получатель недоступен",
    "MESSAGE_ERROR": "Telegram отклонил отправку",
    "MESSAGE_ACCOUNT_PAUSED": "Аккаунт приостановлен",
    "RESOLVE_RETRY": "Повторная проверка получателя",
    "RESOLVE_FAILED": "Получатель не удалось проверить",
}
MESSAGE_LABELS = {
    "Account connected": "Рабочее соединение открыто; аккаунт сохранён",
    "Account disconnected": "Рабочее соединение закрыто; аккаунт сохранён",
    "Added to chat": "Добавлен в чат",
    "private": "Действие запрещено для этого получателя",
    "not_found": "Пользователь не найден",
    "already_in": "Пользователь уже в чате",
}
LOG_FILTERS = {
    "Все события": None,
    "Ошибки и ожидание": {
        "ADD_ERROR",
        "FLOOD_WAIT",
        "MESSAGE_UNCERTAIN",
        "MESSAGE_STOPPED",
        "MESSAGE_ERROR",
        "MESSAGE_ACCOUNT_PAUSED",
        "RESOLVE_RETRY",
        "RESOLVE_FAILED",
        "CONNECT_FAILED",
        "FOLLOWUP_UNCERTAIN",
    },
    "Ожидание ответа": {"FOLLOWUP_WAIT", "FOLLOWUP_REPLY"},
    "Успешные действия": {"ADD_SUCCESS", "MESSAGE_SENT", "FOLLOWUP_SENT"},
    "Пропуски": {
        "SKIP",
        "MESSAGE_PRIVATE",
        "MESSAGE_NOT_FOUND",
        "MESSAGE_SKIP",
        "FOLLOWUP_BLOCKED",
        "FOLLOWUP_CANCELLED",
    },
}


def display_log_message(message):
    value = str(message or "")
    if value.startswith("PeerFlood:"):
        return value.replace("PeerFlood:", "Telegram отклонил отправку:", 1)
    if value.startswith("FloodWait:"):
        return value.replace("FloodWait:", "Telegram просит подождать:", 1)
    match = re.fullmatch(r"Flood for (\d+) sec", value)
    if match:
        return f"Telegram просит подождать {match.group(1)} сек"
    return MESSAGE_LABELS.get(value, value)


def format_log(item):
    action = str(item.get("action") or "")
    return f"[{item.get('created_at') or ''}] {ACTION_LABELS.get(action, action)} · " + " · ".join(
        value
        for value in (
            item.get("account_phone") or "",
            "@" + item["username"] if item.get("username") else "",
            display_log_message(item.get("message")),
        )
        if value
    )


def matches_log(item, query, group):
    actions = LOG_FILTERS.get(group)
    return (actions is None or item.get("action") in actions) and query.strip().casefold() in format_log(
        item
    ).casefold()


class LogsTab(ctk.CTkFrame):
    MAX_LOG_LINES = 2000

    def __init__(self, master, db: Database, run_coro):
        super().__init__(master, fg_color=COLORS["bg"])
        init_fonts()
        self.db = db
        self.run_coro = run_coro
        self._query_generation = 0
        self.search_var = ctk.StringVar()
        self.filter_var = ctk.StringVar(value="Все события")
        self._filter_job = None

        page_intro(
            self,
            "Журнал действий",
            "События выбранной кампании. Результат каждого человека — в «Аудитории».",
            "ЖУРНАЛ СОБЫТИЙ",
        )
        CampaignBar(self).pack(fill="x", padx=20, pady=(0, 4))
        self.progress = ctk.CTkProgressBar(
            self,
            progress_color=COLORS["accent"],
            fg_color=COLORS["muted"],
            height=4,
            corner_radius=12,
        )
        self.progress.pack(fill="x", padx=16, pady=(8, 0))
        self.progress.set(0)

        content = ctk.CTkFrame(self, fg_color=COLORS["bg"])
        content.pack(fill="both", expand=True, padx=6, pady=6)

        top = ctk.CTkFrame(content, fg_color=COLORS["surface"])
        top.pack(fill="x", padx=10, pady=(10, 6))
        style_card(top, color=COLORS["accent"])

        ctk.CTkButton(
            top,
            text="Очистить журнал",
            width=120,
            command=self.clear_logs,
            corner_radius=12,
            fg_color=COLORS["surface"],
            hover_color=COLORS["bg"],
            border_color=COLORS["error"],
            border_width=1,
            text_color=COLORS["error"],
            height=36,
            font=FONTS["subhead"],
        ).pack(side="left", padx=8, pady=8)
        ctk.CTkButton(
            top,
            text="Экспорт журнала",
            width=140,
            command=self.export_logs,
            corner_radius=12,
            fg_color=COLORS["accent"],
            hover_color=COLORS["warning"],
            text_color=COLORS["text"],
            height=36,
            font=FONTS["subhead"],
        ).pack(side="left", padx=8, pady=8)
        self.refresh_button = ctk.CTkButton(top, text="Обновить", width=100, height=36, command=self.refresh_saved_logs)
        self.refresh_button.pack(side="right", padx=8, pady=8)
        filters = ctk.CTkFrame(content, fg_color="transparent")
        filters.pack(fill="x", padx=10, pady=(0, 6))
        ctk.CTkLabel(filters, text="Поиск:", width=50).pack(side="left", padx=(0, 8))
        self.search_entry = ctk.CTkEntry(filters, textvariable=self.search_var, height=30)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.filter_menu = ctk.CTkOptionMenu(
            filters, values=list(LOG_FILTERS), variable=self.filter_var, width=180, height=30
        )
        self.filter_menu.pack(side="right")
        self.result_label = ctk.CTkLabel(
            content,
            text="Последние 2000 сохранённых событий. Экспорт включает весь журнал.",
            font=FONTS["small"],
            text_color=COLORS["muted"],
        )
        self.result_label.pack(anchor="w", padx=10, pady=(0, 6))

        self.log_box = ctk.CTkTextbox(
            content,
            corner_radius=12,
            wrap="word",
            fg_color=COLORS["surface"],
            border_color=COLORS["border"],
            border_width=1,
            text_color=COLORS["text"],
            font=FONTS["mono"],
        )
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        style_card(self.log_box, color=COLORS["border"])
        self.log_box.configure(state="disabled")
        self.log_box.bind("<Button-3>", self.show_log_context_menu)

        text_widget = self.log_box._textbox
        for level, color in LOG_COLORS.items():
            text_widget.tag_config(level, foreground=color)
        text_widget.tag_config("TIME", foreground=COLORS["muted"])
        self.search_var.trace_add("write", self.schedule_filter)
        self.filter_var.trace_add("write", self.schedule_filter)

    def schedule_filter(self, *_args):
        if self._filter_job is not None:
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(180, self.refresh_saved_logs)

    def refresh_saved_logs(self) -> None:
        if self._filter_job is not None:
            self.after_cancel(self._filter_job)
            self._filter_job = None
        db = self.db
        query, group = self.search_var.get(), self.filter_var.get()
        self._query_generation += 1
        generation = self._query_generation
        future = self.run_coro(db.get_logs(limit=self.MAX_LOG_LINES))

        def done(result):
            try:
                rows = result.result()
            except Exception:
                return

            def render():
                if self.db is not db or generation != self._query_generation:
                    return
                self.clear_view()
                visible = [row for row in rows if matches_log(row, query, group)]
                self.result_label.configure(
                    text=f"Показано: {len(visible)} из {len(rows)} загруженных событий · Экспорт: весь журнал"
                )
                if not visible:
                    self.log_box.insert(
                        "end",
                        "Нет подходящих событий. Измените поиск или фильтр."
                        if rows
                        else "Пока нет сохранённых событий этой кампании.",
                        "INFO",
                    )
                for item in reversed(visible):
                    action = str(item.get("action") or "")
                    level = {
                        "ADD_SUCCESS": "SUCCESS",
                        "MESSAGE_SENT": "SUCCESS",
                        "FOLLOWUP_SENT": "SUCCESS",
                        "FOLLOWUP_BLOCKED": "SKIP",
                        "FOLLOWUP_CANCELLED": "SKIP",
                        "FOLLOWUP_UNCERTAIN": "ERROR",
                        "ADD_ERROR": "ERROR",
                        "FLOOD_WAIT": "FLOOD",
                        "SKIP": "SKIP",
                        "MESSAGE_PRIVATE": "SKIP",
                        "MESSAGE_NOT_FOUND": "SKIP",
                        "MESSAGE_UNCERTAIN": "ERROR",
                        "MESSAGE_STOPPED": "FLOOD",
                        "MESSAGE_SKIP": "SKIP",
                        "MESSAGE_ERROR": "ERROR",
                        "MESSAGE_ACCOUNT_PAUSED": "FLOOD",
                        "RESOLVE_FAILED": "ERROR",
                        "CONNECT_FAILED": "ERROR",
                    }.get(action, "INFO")
                    self.log_box.insert(
                        "end",
                        format_log(item) + "\n",
                        level,
                    )
                self._trim_log_lines()
                self.log_box.see("end")
                self.log_box.configure(state="disabled")

            post_ui(self, render)

        future.add_done_callback(done)

    def add_log_line(self, level: str, message: str) -> None:
        allowed = {
            "Ошибки и ожидание": {"ERROR", "FLOOD", "WARN"},
            "Ожидание ответа": {"INFO"},
            "Успешные действия": {"SUCCESS"},
            "Пропуски": {"SKIP"},
        }.get(self.filter_var.get())
        if (
            allowed is not None and level not in allowed
        ) or self.search_var.get().strip().casefold() not in message.casefold():
            return
        self.log_box.configure(state="normal")
        stamp = datetime.now().strftime("%H:%M:%S")
        icon = ICONS.get(level, "[СВЕДЕНИЕ]")
        self.log_box.insert("end", f"[{stamp}]  ", "TIME")
        self.log_box.insert("end", f"{icon} {message}\n", level if level in LOG_COLORS else "INFO")
        self._trim_log_lines()
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def clear_view(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")

    def _trim_log_lines(self) -> None:
        try:
            current_lines = int(float(self.log_box.index("end-1c").split(".")[0]))
        except Exception:
            return
        if current_lines <= self.MAX_LOG_LINES:
            return
        to_remove = current_lines - self.MAX_LOG_LINES
        self.log_box.delete("1.0", f"{to_remove + 1}.0")

    def show_log_context_menu(self, event) -> None:
        menu = tk.Menu(self.log_box, tearoff=0)
        menu.add_command(
            label="Копировать",
            command=lambda: self.log_box.event_generate("<<Copy>>"),
        )
        menu.add_command(
            label="Выделить всё",
            command=lambda: self.log_box._textbox.tag_add("sel", "1.0", "end"),
        )
        menu.tk_popup(event.x_root, event.y_root)

    def clear_logs(self) -> None:
        if not messagebox.askyesno(
            "Очистить журнал",
            "Удалить сохранённые события выбранной кампании?\n"
            "Получатели, результаты и защита от повторных сообщений сохранятся.",
        ):
            return
        fut = self.run_coro(self.db.clear_logs())

        def done(result):
            if not result.cancelled() and result.exception() is None:
                post_ui(self, self.refresh_saved_logs)

        fut.add_done_callback(done)

    def export_logs(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Сохранить логи",
            defaultextension=".txt",
            filetypes=[("Книга Excel", "*.xlsx"), ("Текстовый файл", "*.txt")],
        )
        if not path:
            return

        async def export():
            logs = list(reversed(await self.db.get_logs(limit=None)))
            if path.casefold().endswith(".xlsx"):
                await asyncio.to_thread(write_logs_xlsx, path, logs, ACTION_LABELS, display_log_message)
            else:
                await asyncio.to_thread(write_logs, path, logs, format_log)

        fut = self.run_coro(export())

        def done(result):
            try:
                result.result()
            except Exception as exc:
                post_ui(self, lambda exc=exc: messagebox.showerror("Ошибка экспорта", explain_error(exc)))
                return
            post_ui(self, lambda: messagebox.showinfo("Готово", "Логи экспортированы"))

        fut.add_done_callback(done)
