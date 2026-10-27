from __future__ import annotations

import asyncio
import json
from tkinter import messagebox

import customtkinter as ctk

from core.database import Database
from core.diagnostics import estimate_messages_eta, format_report, message_usage_today, preflight
from core.inviter import describe_next_available
from core.message_template import SAMPLE_NAME, has_placeholders, preview_message
from core.messenger import send_test_message
from core.pacing import (
    PACE_KEYS,
    PACE_LABELS,
    aggressive_warnings,
    daily_cap_status,
    describe_skipped_warmup,
    detect_preset,
    format_warning_block,
    gate_accounts,
    pace_label,
    parse_daily_cap,
    preset_values,
    split_warmup_ready,
    warmup_gate_error,
)
from core.scheduler import TaskScheduler
from core.scheduling import (
    ALL_DAYS,
    DEFAULT_SCHEDULE_DAYS,
    SMART_TZ_CHOICES,
    SMART_TZ_LOCAL,
    WEEKDAY_SHORT,
    describe_schedule_days,
    format_schedule_days,
    parse_schedule_days,
    smart_tz_label,
    validate_smart_tz,
)
from core.user_feedback import explain_error
from core.validation import (
    MESSAGES_DEFAULT_INTERVAL,
    MESSAGES_MIN_INTERVAL,
    normalize_username,
    time_minutes,
    validate_interval,
    validate_message,
)
from ui.campaigns import CampaignBar, show_report
from ui.components import page_intro
from ui.dispatch import post_ui
from ui.entry_utils import paste_text, setup_entry
from ui.hints import hint, tip, wrap_label
from ui.task_recipients import TaskRecipients
from ui.template_dialog import TemplateDialog
from ui.theme import COLORS, FONTS, UI, apply_button_role, init_fonts, style_card, style_tree

INVITE_ORDER_CHOICES = {"Перемешивать": "shuffle", "Как в списке": "as_is"}


def normalize_chat(value: str) -> str:
    return normalize_username(value) if value.strip() else ""


class TaskTab(ctk.CTkFrame):
    def __init__(self, master, db: Database, run_coro, scheduler: TaskScheduler, log_callback):
        super().__init__(master, fg_color=COLORS["bg"])
        init_fonts()
        self.db = db
        self.run_coro = run_coro
        self.scheduler = scheduler
        self.log_callback = log_callback
        self.account_vars: dict[int, ctk.BooleanVar] = {}
        self._account_labels = {}
        self._accounts_info: dict[int, dict] = {}
        self.chat_values: list[str] = []
        self._counter_inflight = False
        self._saved_account_ids = []
        self._last_stats = None
        self._template_dialog = None
        self._starting = False
        self._loading_campaign = True
        self._load_error = False
        self._draft_job = None
        self._drafts = {}
        self._followup_stats = {}
        self._draft_lock = asyncio.Lock()
        self._section_cards = []
        self._section_buttons = []
        self._step = 0
        self.app = self.winfo_toplevel()
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        intro = page_intro(
            self,
            "Настройка задачи",
            "Выберите режим, аудиторию, аккаунты и интервал между действиями.",
            "INVITE STUDIO",
        )
        intro.pack_forget()
        intro.grid(row=0, column=0, sticky="ew")
        self.readiness_label = intro.winfo_children()[-1]
        self.readiness_label.pack_configure(fill="x")
        self._intro_labels = [
            widget
            for widget in intro.winfo_children()
            if isinstance(widget, ctk.CTkLabel) and widget is not self.readiness_label
        ]
        self._compact_header = None
        wrap_label(self.readiness_label)
        sections = ctk.CTkFrame(self, fg_color="transparent")
        sections.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 8))
        CampaignBar(sections).pack(fill="x", pady=(0, 6))
        section_links = ctk.CTkFrame(sections, fg_color="transparent")
        section_links.pack(fill="x")
        for index, title in enumerate(("Получатели", "Сообщение / чат", "Аккаунты и темп", "Проверка")):
            button = ctk.CTkButton(
                section_links,
                text=f"{index + 1:02}  {title}",
                width=120,
                height=30,
                command=lambda position=index: self._scroll_section(position),
            )
            apply_button_role(button, "ghost")
            button.configure(font=FONTS["small"])
            button.pack(side="left", padx=(0, 4))
            self._section_buttons.append(button)
        self.advanced_var = ctk.BooleanVar(value=False)
        self.settings_frame = ctk.CTkScrollableFrame(self, fg_color=COLORS["bg"], corner_radius=12)
        self.settings_frame.grid(row=2, column=0, sticky="nsew", padx=8, pady=4)
        self._build_layout()
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=16, pady=(8, 6))
        buttons = ctk.CTkFrame(footer, fg_color="transparent")
        self._footer_buttons = buttons
        actions = ctk.CTkFrame(footer, fg_color="transparent")
        self._task_action_buttons = []
        self._footer_actions = actions
        actions.pack(fill="x", pady=(0, 6))
        self.back_btn = ctk.CTkButton(
            buttons, text="Назад", width=90, height=UI["btn_h_lg"], command=lambda: self._scroll_section(self._step - 1)
        )
        apply_button_role(self.back_btn, "secondary")
        self.back_btn.pack(side="left", padx=(0, 6))
        for title, command in (
            ("Предпросмотр", lambda: self.check_task(False)),
            ("Проверить подключение", lambda: self.check_task(True)),
            ("Сохранить настройки", self.save_settings),
        ):
            button = ctk.CTkButton(actions, text=title, height=30, width=155, command=command)
            self._task_action_buttons.append(button)
            apply_button_role(button, "secondary")
            button.pack(side="left", padx=(0, 6))
            tip(
                button,
                {
                    "Предпросмотр": "Проверка настроек и очереди без подключения к Telegram. Ничего не отправляет.",
                    "Проверить подключение": "Проверяет сессии, прокси и для приглашений — права в чате. Ничего не отправляет.",
                    "Сохранить настройки": "Сохранить параметры выбранной кампании для следующего запуска. Текст сообщения сохраняется автоматически.",
                }[title],
            )
        buttons.pack(fill="x", pady=(0, 8))
        self.start_btn = ctk.CTkButton(
            buttons,
            text="Запустить задачу",
            height=UI["btn_h_lg"],
            corner_radius=12,
            font=FONTS["heading"],
            command=self._primary_action,
        )
        self.start_btn.pack(side="left", fill="x", expand=True)
        self.pause_btn = ctk.CTkButton(
            buttons,
            text="Пауза",
            height=UI["btn_h_lg"],
            corner_radius=12,
            font=FONTS["heading"],
            command=self.pause_or_resume,
        )
        self.pause_btn.pack(side="left", fill="x", expand=True, padx=8)
        self.stop_btn = ctk.CTkButton(
            buttons,
            text="Остановить",
            height=UI["btn_h_lg"],
            corner_radius=12,
            font=FONTS["heading"],
            command=self.stop_task,
        )
        self.stop_btn.pack(side="left", fill="x", expand=True)
        apply_button_role(self.start_btn, "primary")
        apply_button_role(self.pause_btn, "accent-line")
        apply_button_role(self.stop_btn, "danger")
        tip(self.start_btn, "Начать реальные сообщения или приглашения выбранной кампании после проверки подключения.")
        tip(self.pause_btn, "Приостановить следующие действия. Для возобновления нажмите «Продолжить».")
        tip(
            self.stop_btn,
            "Завершить текущую задачу. Ожидающие остаются в очереди; уже отправленный запрос может завершиться.",
        )

        progress_frame = ctk.CTkFrame(footer, fg_color="transparent")
        self._progress_frame = progress_frame
        progress_frame.pack(fill="x")
        self.progress_top = ctk.CTkLabel(
            progress_frame,
            text="Готовы начать?",
            font=FONTS["subhead"],
            text_color=COLORS["success"],
        )
        self.progress_top.pack(side="left")
        self.progress_bottom = ctk.CTkLabel(
            progress_frame,
            text="Ошибок: 0 | Осталось: 0",
            font=FONTS["small"],
            text_color=COLORS["muted"],
        )
        self.progress_bottom.pack(side="right")
        self.status_label = ctk.CTkLabel(
            footer, text="Настройте задачу и нажмите «Запустить».", text_color=COLORS["muted"]
        )
        self.status_label.configure(font=FONTS["small"], height=20)
        self.status_label.pack(anchor="w", pady=(2, 0))
        # Live invite details (next free account, per-account results); shown only while running.
        self.next_label = ctk.CTkLabel(footer, text="", text_color=COLORS["muted"], anchor="w", justify="left")
        self.next_label.configure(font=FONTS["small"])
        self._build_protection_accounts()
        self.review_card = self._block("04  /  Проверьте перед запуском")
        hint(
            self.review_card,
            "Проверьте получателей, текст и аккаунты. Отправка начнётся только по кнопке «Запустить задачу».",
        )
        self.review_box = ctk.CTkTextbox(
            self.review_card, height=310, wrap="word", font=FONTS["body"], state="disabled"
        )
        self.review_box.pack(fill="x", padx=16, pady=(0, 12))
        self._wizard_groups = [
            [self._section_cards[0]],
            [self.details_card],
            [self._section_cards[4], self._section_cards[1], self._section_cards[2], self._section_cards[3]],
            [self.review_card],
        ]
        self.toggle_advanced()

        self.refresh_chats()
        self.refresh_accounts()
        self.update_counter()
        self.load_campaign_settings()
        self.bind("<Configure>", self.fit_header, add="+")

    def fit_header(self, event):
        compact = event.height / self._get_widget_scaling() < 580
        if compact == self._compact_header:
            return
        self._compact_header = compact
        self.recipients.textbox.configure(height=85 if compact else 125)
        for widget in self._intro_labels:
            if compact:
                widget.pack_forget()
            else:
                widget.pack(anchor="w", before=self.readiness_label)

    def _block(self, title: str) -> ctk.CTkFrame:
        block = ctk.CTkFrame(
            self.settings_frame,
            fg_color=COLORS["surface"],
            border_color=COLORS["border"],
            border_width=1,
            corner_radius=12,
        )
        block.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_2"]))
        self._section_cards.append(block)
        style_card(block, color=COLORS["border"])
        ctk.CTkLabel(
            block,
            text=title,
            font=FONTS["label"],
            text_color=COLORS["muted"],
        ).pack(anchor="w", padx=UI["space_4"], pady=(UI["space_3"], UI["space_1"]))
        return block

    def _scroll_section(self, index):
        self._step = max(0, min(index, 3))
        for card in [*self._section_cards, self.details_card]:
            card.pack_forget()
        for card in self._wizard_groups[self._step]:
            if card in self._section_cards[2:4] and not self.advanced_var.get():
                continue
            card.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_2"]))
        self.settings_frame._parent_canvas.yview_moveto(0)
        for position, button in enumerate(self._section_buttons):
            button.configure(
                fg_color=COLORS["accent_soft"] if position == self._step else "transparent",
                text_color=COLORS["accent"] if position == self._step else COLORS["muted"],
            )
        self.back_btn.configure(state="normal" if self._step else "disabled")
        if not self.scheduler.is_running() and not self._starting:
            self.status_label.configure(
                text=(
                    "Вставьте получателей и нажмите «Сохранить список».",
                    "Заполните сообщение или адрес группы для приглашения.",
                    "Отметьте аккаунты, задайте интервал и при необходимости расписание.",
                    "Проверьте данные. Только «Запустить задачу» начнёт отправку.",
                )[self._step]
            )
        if self._step == 3:
            self.refresh_review()
        self._update_buttons_state()

    def _primary_action(self):
        if self._load_error:
            self.load_campaign_settings()
            return
        if self._loading_campaign:
            return
        if self._starting or self.scheduler.is_running():
            return
        if self._step < 3:
            self._scroll_section(self._step + 1)
        else:
            self.start_task()

    def refresh_review(self):
        stats = self._last_stats or {}
        lines = [
            f"Режим: {self.mode_var.get()}",
            f"В сохранённой очереди: {stats.get('pending', 0)} получателей",
            f"Выбрано аккаунтов: {sum(var.get() for var in self.account_vars.values())}",
            f"Интервал: {self.interval_min_entry.get()}–{self.interval_max_entry.get()} секунд",
            "Расписание: "
            + (
                f"{self.schedule_from_entry.get()}–{self.schedule_to_entry.get()}, " + self._schedule_days_text()
                if self.schedule_enabled.get()
                else "выключено"
            ),
            f"Перерыв после {self.consecutive_entry.get()} действий: {self.break_min_entry.get()}–{self.break_max_entry.get()} минут",
        ]
        selected_names = [
            self._account_labels.get(key, str(key)) for key, var in self.account_vars.items() if var.get()
        ]
        lines.insert(3, "Аккаунты: " + (", ".join(selected_names) or "не выбраны"))
        lines.insert(5, "Темп: " + pace_label(self.current_pace()))
        try:
            cap = parse_daily_cap(self.daily_cap_entry.get())
        except ValueError:
            cap = 0
        lines.insert(6, f"Дневной лимит кампании: {cap if cap else 'выключен'}")
        if self.require_warmup_var.get():
            chosen = [
                info
                for key, info in self._accounts_info.items()
                if self.account_vars.get(key) and self.account_vars[key].get()
            ]
            ready, skipped = split_warmup_ready(chosen)
            line = f"Только прогретые аккаунты: включено ({len(ready)} из {len(chosen)} подходят)"
            if skipped:
                line += "\nБудут пропущены: " + describe_skipped_warmup(skipped)
            lines.insert(7, line)
        warning_block = format_warning_block(self._pace_warnings())
        if warning_block:
            lines.insert(0, warning_block + "\n")
        if self.operation_mode() == "messages":
            eta = self._eta_line(stats.get("pending", 0))
            if eta:
                lines.extend(["", eta])
            lines.extend(["", "Первое сообщение:", self.message_box.get("1.0", "end-1c") or "Не заполнено"])
            if self.followup_enabled.get():
                lines.extend(
                    [
                        "",
                        "Второе сообщение — только после ответа:",
                        self.followup_box.get("1.0", "end-1c") or "Не заполнено",
                    ]
                )
        else:
            lines.extend(["", "Чат для приглашения: " + (self.chat_entry.get() or "Не указан")])
        try:
            self.task_values()
        except ValueError as exc:
            lines.extend(["", "Нужно исправить: " + str(exc)])
        if self.recipients.dirty():
            lines.extend(
                ["", "Есть несохранённые изменения получателей. Вернитесь на шаг 1 и нажмите «Сохранить список»."]
            )
        elif self.recipients.loading or self.recipients.busy or self.recipients.expected is None:
            lines.extend(["", "Дождитесь загрузки или сохранения списка получателей."])
        elif not stats.get("pending", 0) and not any(self._followup_stats.get(key, 0) for key in ("waiting", "ready")):
            lines.extend(["", "В очереди нет получателей. Добавьте их на шаге 1."])
        lines.extend(["", "Перед отправкой программа проверит подключение и доступные лимиты."])
        text = "\n".join(lines)
        if text == self.review_box.get("1.0", "end-1c"):
            return
        position = self.review_box.yview()[0]
        self.review_box.configure(state="normal")
        self.review_box.delete("1.0", "end")
        self.review_box.insert("1.0", text)
        self.review_box.yview_moveto(position)
        self.review_box.configure(state="disabled")

    def _used_today(self, account: dict) -> int:
        if self.operation_mode() == "messages":
            return sum(message_usage_today(account))  # first + follow-ups share one budget
        return account.get("added_today") or 0

    def _eta_line(self, pending: int) -> str:
        """Approximate days and today's remaining capacity for the selected accounts (messages mode)."""
        selected = [
            self._accounts_info[key]
            for key, var in self.account_vars.items()
            if var.get() and key in self._accounts_info
        ]
        if not selected:
            return ""
        try:
            low = int(self.interval_min_entry.get().strip())
            high = int(self.interval_max_entry.get().strip())
        except ValueError:
            return ""
        return estimate_messages_eta(selected, pending, low, max(low, high), self._schedule_hours())

    def _schedule_hours(self) -> float | None:
        if not self.schedule_enabled.get():
            return None
        try:
            start = time_minutes(self.schedule_from_entry.get().strip())
            end = time_minutes(self.schedule_to_entry.get().strip())
        except ValueError:
            return None
        return ((end - start) % 1440 or 1440) / 60

    def _build_layout(self) -> None:
        chat_block = self._block("01  /  Режим и получатели")
        self.mode_var = ctk.StringVar(value="Приглашения")
        self.mode_menu = ctk.CTkOptionMenu(
            chat_block,
            values=["Приглашения", "Личные сообщения"],
            variable=self.mode_var,
            command=lambda _: self.on_mode_change(),
            height=36,
        )
        self.mode_menu.pack(fill="x", padx=UI["space_4"], pady=(0, 8))
        self.mode_hint = hint(chat_block, "")
        audience_row = ctk.CTkFrame(chat_block, fg_color="transparent")
        audience_row.pack(fill="x", padx=16, pady=(0, 8))
        self.audience_button = ctk.CTkButton(
            audience_row,
            text="Открыть аудиторию",
            width=180,
            height=30,
            command=lambda: self.app._switch_tab("targets"),
        )
        apply_button_role(self.audience_button, "secondary")
        self.audience_button.pack(side="left")
        tip(
            self.audience_button,
            "Результаты и статусы каждого получателя. Список этой кампании общий для выбранных аккаунтов.",
        )
        self.recipients = TaskRecipients(self, chat_block)
        self.recipients.pack(fill="x", padx=16, pady=(0, 12))
        # Keep the editable list above secondary navigation on small windows.
        self.mode_hint.pack_forget()
        audience_row.pack_forget()
        audience_row.pack(fill="x", padx=16, pady=(0, 8))
        self.mode_hint.pack(fill="x", padx=16, pady=(0, 10))
        self.details_card = self._block("02  /  Текст сообщения или чат назначения")
        self._section_cards.pop()
        self.chat_controls = ctk.CTkFrame(self.details_card, fg_color="transparent")
        self.chat_controls.pack(fill="x")
        chat_row = ctk.CTkFrame(self.chat_controls, fg_color="transparent")
        chat_row.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_2"]))
        chat_row.grid_columnconfigure(0, weight=1)
        self.chat_entry = ctk.CTkEntry(
            chat_row,
            placeholder_text="@chatname или https://t.me/chatname",
            height=UI["input_h"],
            font=FONTS["body"],
            fg_color=COLORS["bg"],
            border_color=COLORS["border"],
            text_color=COLORS["text"],
            corner_radius=12,
        )
        self.chat_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        setup_entry(self.chat_entry)
        tip(
            self.chat_entry,
            "Публичный @адрес группы или канала для прямого приглашения. Приватную invite-ссылку можно отправить текстом в режиме личных сообщений.",
        )
        self.save_chat_btn = ctk.CTkButton(
            chat_row,
            text="+ Сохранить",
            width=100,
            height=UI["input_h"],
            corner_radius=12,
            font=FONTS["subhead"],
            command=self._save_chat,
        )
        apply_button_role(self.save_chat_btn, "accent-line")
        self.save_chat_btn.grid(row=0, column=1, padx=(0, 6))
        self.delete_chat_btn = ctk.CTkButton(
            chat_row,
            text="Удалить",
            width=40,
            height=UI["input_h"],
            corner_radius=12,
            font=FONTS["subhead"],
            command=self._delete_selected_chat,
        )
        apply_button_role(self.delete_chat_btn, "danger")
        self.delete_chat_btn.grid(row=0, column=2)

        ctk.CTkLabel(
            self.chat_controls, text="Сохранённые чаты:", font=FONTS["small"], text_color=COLORS["muted"]
        ).pack(anchor="w", padx=UI["space_4"], pady=(0, UI["space_1"]))
        self.saved_chats_frame = ctk.CTkFrame(self.chat_controls, fg_color="transparent")
        self.saved_chats_frame.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_2"]))

        self.message_frame = ctk.CTkFrame(self.details_card, fg_color="transparent")
        message_header = ctk.CTkFrame(self.message_frame, fg_color="transparent")
        message_header.pack(fill="x", padx=16)
        ctk.CTkLabel(message_header, text="Первое сообщение", font=FONTS["small"]).pack(side="left")
        self.message_counter = ctk.CTkLabel(
            message_header, text="0 / 4096", width=90, font=FONTS["small"], text_color=COLORS["muted"]
        )
        self.message_counter.pack(side="left", padx=8)
        self.paste_message_btn = ctk.CTkButton(
            message_header, text="Вставить", width=100, height=28, command=lambda: paste_text(self.message_box)
        )
        apply_button_role(self.paste_message_btn, "secondary")
        self.paste_message_btn.pack(side="right")
        self.template_button = ctk.CTkButton(
            message_header, text="Шаблоны…", width=110, height=28, command=self.open_templates
        )
        apply_button_role(self.template_button, "secondary")
        self.template_button.pack(side="right", padx=(0, 8))
        tip(
            self.template_button,
            "Сохранить часто используемый текст или вставить готовый шаблон. Отправка не запускается.",
        )
        self.message_box = ctk.CTkTextbox(self.message_frame, height=140, wrap="word", font=FONTS["body"])
        self.message_box.bind("<<Modified>>", self._message_modified, add="+")
        self.message_box.pack(fill="x", padx=16, pady=(6, 4))
        hint(
            self.message_frame,
            "Подстановки: {имя} или {name} — имя получателя (если неизвестно — «друг»); "
            "{привет|здравствуйте|добрый день} — случайный вариант для каждого сообщения. "
            "Так тексты отличаются, а рассылка выглядит менее шаблонной.",
        )
        preview_row = ctk.CTkFrame(self.message_frame, fg_color="transparent")
        preview_row.pack(fill="x", padx=16, pady=(0, 4))
        self.preview_message_btn = ctk.CTkButton(
            preview_row, text="Предпросмотр", width=130, height=28, command=self.show_message_preview
        )
        apply_button_role(self.preview_message_btn, "secondary")
        self.preview_message_btn.pack(side="left", padx=(0, 8))
        tip(
            self.preview_message_btn,
            f"Показать текст так, как увидит получатель по имени «{SAMPLE_NAME}». Ничего не отправляется.",
        )
        self.test_message_btn = ctk.CTkButton(
            preview_row, text="Тест в Избранное", width=150, height=28, command=self.send_test_to_saved
        )
        apply_button_role(self.test_message_btn, "accent-line")
        self.test_message_btn.pack(side="left")
        tip(
            self.test_message_btn,
            "Отправить готовый текст себе в «Избранное» через первый выбранный аккаунт. "
            "Получателям из списка ничего не уходит; перед отправкой будет вопрос.",
        )
        self.preview_label = ctk.CTkLabel(
            self.message_frame,
            text="",
            font=FONTS["small"],
            text_color=COLORS["text"],
            justify="left",
            anchor="w",
            wraplength=480,
        )
        wrap_label(self.preview_label)
        self.preview_label.pack(fill="x", padx=16, pady=(0, 8))
        self.followup_enabled = ctk.BooleanVar(value=False)
        self.followup_switch = ctk.CTkSwitch(
            self.message_frame,
            text="Второе сообщение — только после ответа",
            variable=self.followup_enabled,
            command=self.toggle_followup,
        )
        self.followup_switch.pack(anchor="w", padx=16, pady=(4, 8))
        self.followup_frame = ctk.CTkFrame(self.message_frame, fg_color="transparent")
        header = ctk.CTkFrame(self.followup_frame, fg_color="transparent")
        header.pack(fill="x", padx=16)
        ctk.CTkLabel(header, text="Второе сообщение", font=FONTS["small"]).pack(side="left")
        self.followup_counter = ctk.CTkLabel(header, text="0 / 4096", font=FONTS["small"])
        self.followup_counter.pack(side="left", padx=8)
        self.paste_followup_btn = ctk.CTkButton(
            header, text="Вставить", width=100, height=28, command=lambda: paste_text(self.followup_box)
        )
        apply_button_role(self.paste_followup_btn, "secondary")
        self.paste_followup_btn.pack(side="right")
        self.followup_box = ctk.CTkTextbox(self.followup_frame, height=125, wrap="word", font=FONTS["body"])
        self.followup_box.pack(fill="x", padx=16, pady=(6, 8))
        self.followup_box.bind("<<Modified>>", self._message_modified, add="+")
        hint(
            self.followup_frame,
            "Отправляется один раз с того же аккаунта после нового входящего сообщения. "
            "Старые сообщения, реакции и прочтения не считаются ответом. Пауза и расписание действуют.\n"
            "Дневной лимит общий: первые и вторые сообщения вместе не превышают дневной лимит аккаунта "
            "(например, 40 первых — вторым места не останется).\n"
            "Держите задачу запущенной, пока ждёте ответы. После выхода запустите её вручную. "
            "Изменение текста применяется к новым первым сообщениям; уже ожидающие сохраняют прежний второй текст.",
        )
        wait_row = ctk.CTkFrame(self.followup_frame, fg_color="transparent")
        wait_row.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(wait_row, text="Ждать ответ, часов", font=FONTS["body"]).pack(side="left")
        self.followup_wait_entry = ctk.CTkEntry(wait_row, width=70, height=32)
        setup_entry(self.followup_wait_entry)
        self.followup_wait_entry.insert(0, "24")
        self.followup_wait_entry.pack(side="left", padx=(8, 8))
        ctk.CTkLabel(wait_row, text="1–168. Без ответа второе сообщение отменяется", font=FONTS["small"]).pack(
            side="left"
        )
        self.followup_status = hint(self.message_frame, "Вторые сообщения: ожиданий нет")
        self.cancel_followups_btn = ctk.CTkButton(
            self.message_frame, text="Отменить ожидающие вторые сообщения", height=30, command=self.cancel_followups
        )
        apply_button_role(self.cancel_followups_btn, "secondary")
        # Shown by update_counter only while waiting/ready follow-ups exist;
        # re-packed before the note to keep the original order.
        self.followup_note = ctk.CTkLabel(
            self.message_frame,
            text="Первое сообщение каждому — один раз. Интервал первых сообщений общий для всех аккаунтов.",
            justify="left",
            font=FONTS["small"],
            wraplength=500,
            text_color=COLORS["muted"],
        )
        self.followup_note.pack(anchor="w", padx=16, pady=(0, 10))
        interval_block = self._block("Интервал между действиями")
        pace_row = ctk.CTkFrame(interval_block, fg_color="transparent")
        pace_row.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_2"]))
        ctk.CTkLabel(pace_row, text="Темп:", font=FONTS["body"]).pack(side="left", padx=(0, 8))
        self.pace_buttons: dict[str, ctk.CTkButton] = {}
        for key in PACE_KEYS:
            button = ctk.CTkButton(
                pace_row,
                text=PACE_LABELS[key],
                width=110,
                height=30,
                command=lambda value=key: self.apply_pace_preset(value),
            )
            apply_button_role(button, "secondary")
            button.pack(side="left", padx=(0, 6))
            self.pace_buttons[key] = button
        tip(
            self.pace_buttons["safe"],
            "Редкие действия и частые перерывы: самый низкий риск ограничений. Заполняет интервал и перерывы.",
        )
        tip(self.pace_buttons["normal"], "Рекомендуемый баланс скорости и безопасности. Заполняет интервал и перерывы.")
        tip(
            self.pace_buttons["fast"],
            "Быстрее, но выше риск PeerFlood и ограничений аккаунта. Заполняет интервал и перерывы.",
        )
        self.pace_label = ctk.CTkLabel(pace_row, text="Выбрано: Свой", font=FONTS["small"], text_color=COLORS["muted"])
        self.pace_label.pack(side="left", padx=(6, 0))
        self.pace_warning = hint(interval_block, "")
        self.pace_warning.configure(text_color=COLORS["error"])
        self.pace_warning.pack_forget()
        intervals = ctk.CTkFrame(interval_block, fg_color="transparent")
        intervals.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_3"]))
        ctk.CTkLabel(intervals, text="Мин. интервал (сек)", font=FONTS["body"]).pack(side="left")
        self.interval_min_entry = ctk.CTkEntry(intervals, width=80, height=36)
        setup_entry(self.interval_min_entry)
        self.interval_min_entry.insert(0, "20")
        self.interval_min_entry.pack(side="left", padx=(8, 20))
        ctk.CTkLabel(intervals, text="Макс. интервал (сек)", font=FONTS["body"]).pack(side="left")
        self.interval_max_entry = ctk.CTkEntry(intervals, width=80, height=36)
        setup_entry(self.interval_max_entry)
        self.interval_max_entry.insert(0, "40")
        self.interval_max_entry.pack(side="left", padx=8)
        hint(
            interval_block,
            "Случайная пауза от минимума до максимума. Для фиксированной паузы задайте одинаковые числа.",
        )
        hint(
            interval_block,
            "В режиме приглашений ночью и поздним вечером пауза может автоматически увеличиваться. "
            "Время определяется по системным часам компьютера (не по московскому времени).",
        )
        hint(
            interval_block,
            "В приглашениях пауза считается отдельно для каждого аккаунта: пока один отдыхает, остальные работают. "
            "Первые действия каждого аккаунта идут с удвоенной паузой (тёплый старт).",
        )
        hint(
            interval_block,
            f"Личные сообщения: минимум {MESSAGES_MIN_INTERVAL} сек, рекомендуем "
            f"{MESSAGES_DEFAULT_INTERVAL[0]}–{MESSAGES_DEFAULT_INTERVAL[1]}. Быстрая отправка незнакомым людям "
            "ведёт к PeerFlood — Telegram блокирует рассылку с аккаунта примерно на сутки.",
        )
        tip(
            self.interval_min_entry,
            "Нижняя граница паузы в секундах. В личных сообщениях интервал общий для всех аккаунтов.",
        )
        tip(
            self.interval_max_entry,
            "Верхняя граница паузы. Например, 60 и 60 — примерно одно новое личное сообщение в минуту на всю очередь.",
        )
        cap_row = ctk.CTkFrame(interval_block, fg_color="transparent")
        cap_row.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_2"]))
        ctk.CTkLabel(cap_row, text="Дневной лимит кампании (0 — выкл.)", font=FONTS["body"]).pack(side="left")
        self.daily_cap_entry = ctk.CTkEntry(cap_row, width=80, height=36)
        setup_entry(self.daily_cap_entry)
        self.daily_cap_entry.insert(0, "0")
        self.daily_cap_entry.pack(side="left", padx=8)
        tip(
            self.daily_cap_entry,
            "Максимум успешных первых действий за день по всей кампании: добавленных людей или первых сообщений. "
            "Достигнув лимита, задача ждёт следующего дня. 0 — без лимита.",
        )
        hint(
            interval_block,
            "Общий лимит кампании на календарный день (по времени ПК) поверх дневных лимитов аккаунтов. "
            "Вторые сообщения после ответа в него не входят.",
        )
        for entry in (self.interval_min_entry, self.interval_max_entry):
            entry.bind("<KeyRelease>", lambda _event: self._refresh_pace(), add="+")

        schedule_block = self._block("Время работы")
        self.schedule_enabled = ctk.BooleanVar(value=False)
        schedule_row = ctk.CTkFrame(schedule_block, fg_color="transparent")
        schedule_row.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_2"]))
        self.schedule_switch = ctk.CTkSwitch(
            schedule_row,
            text="Работать по расписанию",
            variable=self.schedule_enabled,
            command=self.on_schedule_toggle,
        )
        self.schedule_switch.pack(side="left")
        self.schedule_time_frame = ctk.CTkFrame(schedule_block, fg_color="transparent")
        self.schedule_time_frame.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_3"]))
        ctk.CTkLabel(self.schedule_time_frame, text="С", font=FONTS["body"]).pack(side="left")
        self.schedule_from_entry = ctk.CTkEntry(self.schedule_time_frame, width=70, height=36)
        setup_entry(self.schedule_from_entry)
        self.schedule_from_entry.insert(0, "13:00")
        self.schedule_from_entry.pack(side="left", padx=(6, 10))
        ctk.CTkLabel(self.schedule_time_frame, text="До", font=FONTS["body"]).pack(side="left")
        self.schedule_to_entry = ctk.CTkEntry(self.schedule_time_frame, width=70, height=36)
        setup_entry(self.schedule_to_entry)
        self.schedule_to_entry.insert(0, "21:00")
        self.schedule_to_entry.pack(side="left", padx=(6, 0))
        self.schedule_days_frame = ctk.CTkFrame(schedule_block, fg_color="transparent")
        self.schedule_day_vars = [ctk.BooleanVar(value=True) for _ in WEEKDAY_SHORT]
        self.schedule_day_checks = []
        ctk.CTkLabel(self.schedule_days_frame, text="Дни:", font=FONTS["body"]).pack(side="left", padx=(0, 6))
        for name, var in zip(WEEKDAY_SHORT, self.schedule_day_vars, strict=True):
            check = ctk.CTkCheckBox(
                self.schedule_days_frame,
                text=name,
                variable=var,
                width=52,
                checkbox_width=20,
                checkbox_height=20,
                command=self._schedule_days_changed,
            )
            check.pack(side="left", padx=(0, 4))
            self.schedule_day_checks.append(check)
        self.schedule_hint = hint(
            schedule_block,
            "Время и дни недели — по часам этого компьютера. Вне выбранных дней и часов задача ждёт, как вне "
            "расписания. Расписание действует при открытой программе и запущенной задаче.",
        )
        self.smart_tz_row = ctk.CTkFrame(schedule_block, fg_color="transparent")
        self.smart_tz_row.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_2"]))
        ctk.CTkLabel(self.smart_tz_row, text="Умный интервал считать по:", font=FONTS["body"]).pack(
            side="left", padx=(0, 8)
        )
        self.smart_tz_menu = ctk.CTkOptionMenu(
            self.smart_tz_row, values=list(SMART_TZ_CHOICES), width=220, command=lambda _: self.update_readiness()
        )
        self.smart_tz_menu.set(smart_tz_label(SMART_TZ_LOCAL))
        self.smart_tz_menu.pack(side="left")
        self.smart_tz_hint = hint(
            schedule_block,
            "Умный интервал (только приглашения) удлиняет паузы ночью и поздним вечером. Час берётся по выбранным "
            "часам: «Время этого компьютера» (по умолчанию) или Москва. Базовые интервалы выше не меняются.",
        )

    def _build_protection_accounts(self) -> None:
        protection_block = self._block("Перерывы и ротация")
        protection_row = ctk.CTkFrame(protection_block, fg_color="transparent")
        protection_row.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_3"]))
        ctk.CTkLabel(protection_row, text="Перерыв после", font=FONTS["body"]).pack(side="left")
        self.consecutive_entry = ctk.CTkEntry(protection_row, width=60, height=36)
        setup_entry(self.consecutive_entry)
        self.consecutive_entry.insert(0, "5")
        self.consecutive_entry.pack(side="left", padx=6)
        ctk.CTkLabel(
            protection_row,
            text="действий | Мин. пауза (мин)",
            font=FONTS["body"],
        ).pack(side="left", padx=(8, 0))
        self.break_min_entry = ctk.CTkEntry(protection_row, width=60, height=36)
        setup_entry(self.break_min_entry)
        self.break_min_entry.insert(0, "5")
        self.break_min_entry.pack(side="left", padx=6)
        ctk.CTkLabel(protection_row, text="Макс. перерыв", font=FONTS["body"]).pack(side="left", padx=(8, 0))
        self.break_max_entry = ctk.CTkEntry(protection_row, width=60, height=36)
        setup_entry(self.break_max_entry)
        self.break_max_entry.insert(0, "15")
        self.break_max_entry.pack(side="left", padx=6)
        for entry in (self.consecutive_entry, self.break_min_entry, self.break_max_entry):
            entry.bind("<KeyRelease>", lambda _event: self._refresh_pace(), add="+")
        hint(
            protection_block, "Перерыв для аккаунта после указанного числа успешных действий. Длительность — в минутах."
        )
        order_row = ctk.CTkFrame(protection_block, fg_color="transparent")
        order_row.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_3"]))
        ctk.CTkLabel(order_row, text="Порядок приглашений", font=FONTS["body"]).pack(side="left")
        self.order_menu = ctk.CTkOptionMenu(order_row, values=list(INVITE_ORDER_CHOICES), width=200)
        self.order_menu.set(next(iter(INVITE_ORDER_CHOICES)))
        self.order_menu.pack(side="left", padx=8)
        hint(
            protection_block,
            "«Как в списке» сохраняет порядок импорта из парсинга (сначала недавно активные). «Перемешивать» — случайный порядок при каждом запуске.",
        )

        accounts_block = self._block("03  /  Аккаунты для задачи")
        self.advanced_switch = ctk.CTkSwitch(
            accounts_block, text="Расписание и перерывы", variable=self.advanced_var, command=self.toggle_advanced
        )
        self.advanced_switch.pack(anchor="w", padx=16, pady=(0, 8))
        tip(
            self.advanced_switch,
            "Показать дополнительные настройки. Скрытые настройки продолжают действовать; их значения видны на шаге «Проверка».",
        )
        select_row = ctk.CTkFrame(accounts_block, fg_color="transparent")
        select_row.pack(fill="x", padx=16, pady=(0, 6))
        self.select_all_button = ctk.CTkButton(
            select_row, text="Выбрать все", width=120, height=28, command=lambda: self.select_accounts(True)
        )
        self.select_all_button.pack(side="left", padx=(0, 8))
        self.select_none_button = ctk.CTkButton(
            select_row, text="Снять выбор", width=120, height=28, command=lambda: self.select_accounts(False)
        )
        self.select_none_button.pack(side="left")
        for button in (self.select_all_button, self.select_none_button):
            apply_button_role(button, "secondary")
        self.require_warmup_var = ctk.BooleanVar(value=False)
        self.require_warmup_check = ctk.CTkCheckBox(
            accounts_block,
            text="Запускать только с прогретыми аккаунтами",
            variable=self.require_warmup_var,
            command=self.update_readiness,
        )
        self.require_warmup_check.pack(anchor="w", padx=16, pady=(0, 8))
        tip(
            self.require_warmup_check,
            "Аккаунты, у которых прогрев не завершён (статус не «Готов»), при запуске пропускаются. "
            "Если прогретых нет — запуск остановится с пояснением.",
        )
        self.accounts_box = ctk.CTkFrame(accounts_block, fg_color=COLORS["surface"])
        self.accounts_box.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_3"]))
        hint(
            accounts_block,
            "Отметьте, какие аккаунты участвуют. Счётчик рядом с телефоном: успешно сегодня / заданный дневной лимит.",
        )

    def update_counter(self):
        if self.app._closing:
            return
        if self._counter_inflight:
            self.after(2000, self.update_counter)
            return
        self._counter_inflight = True
        db = self.db

        async def counters():
            try:
                cap = parse_daily_cap(await db.get_setting("campaign_daily_cap", "0"))
            except ValueError:
                cap = 0
            today = await db.get_campaign_daily_count() if cap else 0
            return await db.get_targets_stats(), await db.get_followup_stats(), (cap, today)

        future = self.run_coro(counters())

        def done(f):
            try:
                stats, followups, (cap, today) = f.result()

                def render():
                    if self.db is not db:
                        return
                    self._last_stats = stats
                    self._followup_stats = followups
                    self.followup_status.configure(
                        text=f"Вторые сообщения · Ждём ответ: {followups.get('waiting', 0)} · "
                        f"Готовы: {followups.get('ready', 0)} · Отправлено: {followups.get('sent', 0)} · "
                        f"Недоступно: {followups.get('blocked', 0)} · Проверить: {followups.get('uncertain', 0)} · "
                        f"Отменено: {followups.get('cancelled', 0)}"
                    )
                    pending_cancel = followups.get("waiting", 0) + followups.get("ready", 0)
                    if pending_cancel and not self.cancel_followups_btn.winfo_manager():
                        self.cancel_followups_btn.pack(anchor="w", padx=16, pady=(0, 8), before=self.followup_note)
                    elif not pending_cancel and self.cancel_followups_btn.winfo_manager():
                        self.cancel_followups_btn.pack_forget()
                    self.update_readiness()
                    self.progress_top.configure(
                        text=f"Успешно: {stats['added'] + stats.get('sent', 0)} из {stats['total']}"
                    )
                    cap_text = f" | Лимит дня: {today}/{cap}" if cap else ""
                    self.progress_bottom.configure(
                        text=f"Пропущено: {stats.get('skipped', 0)} | Ошибок: {stats['errors']} | "
                        f"Осталось: {stats['pending']}{cap_text}"
                    )

                self._safe_after(render)
            except Exception:
                pass
            finally:
                self._counter_inflight = False

        future.add_done_callback(done)
        self.after(2000, self.update_counter)

    def toggle_advanced(self):
        self._scroll_section(self._step)
        self.update_readiness()

    def select_accounts(self, selected):
        if self.scheduler.is_running() or self._starting:
            return
        for var in self.account_vars.values():
            var.set(selected)
        self.update_readiness()

    def open_templates(self):
        if self._template_dialog is None or not self._template_dialog.winfo_exists():
            self._template_dialog = TemplateDialog(self)
        self._template_dialog.lift()
        return self._template_dialog

    def update_readiness(self):
        if not hasattr(self, "readiness_label") or self._loading_campaign:
            return
        stats = self._last_stats or {}
        selected = sum(var.get() for var in self.account_vars.values())
        pending = stats.get("pending", 0)
        prefix = f"Ожидает: {pending} · Аккаунтов: {selected} · "
        if self.scheduler.is_running():
            text = "Задача на паузе" if self.scheduler.engine.paused else "Задача выполняется"
        elif getattr(self.app, "_audience_imports", 0):
            text = "Дождитесь окончания импорта получателей"
        elif not selected:
            text = "Отметьте аккаунты в блоке «Аккаунты»"
        elif not pending and not (
            self.followup_enabled.get() and any(self._followup_stats.get(k, 0) for k in ("waiting", "ready"))
        ):
            text = "Вставьте @никнеймы в список получателей и нажмите «Сохранить список»"
        else:
            try:
                self.task_values()
            except ValueError as exc:
                text = str(exc)
            else:
                text = "Данные заполнены → шаг 4 «Проверка»"
        if not self.advanced_var.get() and self.schedule_enabled.get():
            text += f" · Расписание {self.schedule_from_entry.get()}–{self.schedule_to_entry.get()} включено"
        self.readiness_label.configure(text=prefix + text)
        if self._step == 3:
            self.refresh_review()

    def _safe_after(self, callback) -> None:
        post_ui(self, callback)

    def on_schedule_toggle(self) -> None:
        # Repack in reading order: time window, weekdays, hint, then the smart-interval zone.
        for widget in (
            self.schedule_time_frame,
            self.schedule_days_frame,
            self.schedule_hint,
            self.smart_tz_row,
            self.smart_tz_hint,
        ):
            widget.pack_forget()
        if self.schedule_enabled.get():
            self.schedule_time_frame.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_2"]))
            self.schedule_days_frame.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_3"]))
        self.schedule_hint.pack(fill="x", padx=16, pady=(0, 10))
        self.smart_tz_row.pack(fill="x", padx=UI["space_4"], pady=(0, UI["space_2"]))
        self.smart_tz_hint.pack(fill="x", padx=16, pady=(0, 10))

    def _schedule_days_value(self) -> str:
        """Weekdays to store; «no day ticked» is an error only while the schedule is switched on."""
        try:
            return self.selected_schedule_days()
        except ValueError:
            if self.schedule_enabled.get():
                raise
            return DEFAULT_SCHEDULE_DAYS

    def _schedule_days_text(self) -> str:
        try:
            return describe_schedule_days(self.selected_schedule_days())
        except ValueError:
            return "дни не выбраны"

    def _schedule_days_changed(self) -> None:
        if not self._loading_campaign:
            self.update_readiness()

    def selected_schedule_days(self) -> str:
        """Canonical «0,1,4» value of the weekday checkboxes (every day when all are ticked)."""
        return format_schedule_days({index for index, var in enumerate(self.schedule_day_vars) if var.get()})

    def set_schedule_days(self, value) -> None:
        try:
            days = parse_schedule_days(value)
        except ValueError:
            days = ALL_DAYS
        for index, var in enumerate(self.schedule_day_vars):
            var.set(index in days)

    def refresh_chats(self) -> None:
        fut = self.run_coro(self.db.get_chats())

        def done(f):
            try:
                chats = f.result()
            except Exception:
                return
            self.chat_values = [item["username"] for item in chats]
            self._safe_after(lambda: self._populate_saved_chats(chats))

        fut.add_done_callback(done)

    def _populate_saved_chats(self, chats: list[dict]) -> None:
        for widget in self.saved_chats_frame.winfo_children():
            widget.destroy()
        if not chats:
            ctk.CTkLabel(
                self.saved_chats_frame, text="Нет сохранённых чатов", font=FONTS["small"], text_color=COLORS["muted"]
            ).pack(anchor="w", padx=6, pady=6)
            return
        for item in chats:
            username = item["username"]
            ctk.CTkButton(
                self.saved_chats_frame,
                text=f"@{username}",
                height=30,
                corner_radius=12,
                fg_color=COLORS["surface"],
                hover_color=COLORS["border"],
                text_color=COLORS["text"],
                anchor="w",
                command=lambda u=username: self._on_saved_chat_click(u),
            ).pack(fill="x", padx=4, pady=2)
        self._update_buttons_state()

    def _on_saved_chat_click(self, chat_username: str) -> None:
        self.chat_entry.delete(0, "end")
        self.chat_entry.insert(0, chat_username)

    def _save_chat(self) -> None:
        try:
            raw = normalize_chat(self.chat_entry.get())
        except ValueError as exc:
            messagebox.showerror("Ошибка", explain_error(exc))
            return
        if not raw:
            return
        fut = self.run_coro(self.db.add_chat(raw, raw))
        fut.add_done_callback(lambda _: self._safe_after(self.refresh_chats))

    def _delete_selected_chat(self) -> None:
        try:
            raw = normalize_chat(self.chat_entry.get())
        except ValueError as exc:
            messagebox.showerror("Ошибка", explain_error(exc))
            return
        if not raw:
            return
        fut = self.run_coro(self.db.delete_chat(raw))
        fut.add_done_callback(lambda _: self._safe_after(self.refresh_chats))

    def refresh_accounts(self) -> None:
        fut = self.run_coro(self.db.get_enabled_accounts())

        def done(f):
            try:
                accounts = f.result()
            except Exception:
                return
            self._safe_after(lambda: self._render_accounts(accounts))

        fut.add_done_callback(done)

    def _render_accounts(self, accounts: list[dict]) -> None:
        old_values = {acc_id: var.get() for acc_id, var in self.account_vars.items()}
        self.account_vars = {}
        self.account_checkboxes = []
        for widget in self.accounts_box.winfo_children():
            widget.destroy()
        enabled_accounts = [acc for acc in accounts if int(acc.get("is_enabled", 1)) == 1]
        self._accounts_info = {acc["id"]: acc for acc in enabled_accounts}
        if not enabled_accounts:
            ctk.CTkLabel(self.accounts_box, text="Нет добавленных аккаунтов").pack(anchor="w", padx=8, pady=8)
            self._update_buttons_state()
            return
        for account in enabled_accounts:
            var = ctk.BooleanVar(
                value=old_values.get(
                    account["id"], account["id"] in self._saved_account_ids if self._saved_account_ids else True
                )
            )
            self.account_vars[account["id"]] = var
            self._account_labels[account["id"]] = account["phone"]
            checkbox = ctk.CTkCheckBox(
                self.accounts_box,
                text=f"{account['phone']} ({self._used_today(account)}/{account.get('daily_limit', 40)})",
                variable=var,
                command=self.update_readiness,
            )
            checkbox.pack(anchor="w", padx=8, pady=3)
            checkbox.configure(state="disabled" if self.scheduler.is_running() or self._starting else "normal")
            self.account_checkboxes.append(checkbox)

        style_tree(self.accounts_box)
        self.update_readiness()
        self._update_buttons_state()

    def set_progress(self, added: int, total: int) -> None:
        self.progress_top.configure(text=f"Успешно: {added} из {total}")

    def set_details(self, details: dict) -> None:
        """Richer live progress from the invite engine (pending/skipped/errors + next free account)."""
        if not self.scheduler.is_running():
            self._hide_details()
            return
        self.progress_top.configure(text=f"Успешно: {details.get('success', 0)} из {details.get('total', 0)}")
        self.progress_bottom.configure(
            text=f"Пропущено: {details.get('skipped', 0)} | Ошибок: {details.get('errors', 0)} | "
            f"Осталось: {details.get('pending', 0)}"
        )
        line = describe_next_available(details)
        per_account = [
            f"{item['phone']}: +{item['added']}" for item in details.get("accounts", []) if item.get("added")
        ]
        if per_account:
            line = (line + "\n" if line else "") + "Добавлено по аккаунтам: " + ", ".join(per_account)
        cap_line = daily_cap_status(details.get("daily_done", 0), details.get("daily_cap", 0), "invite")
        if cap_line:
            line = (line + "\n" if line else "") + cap_line
        if not line:
            self._hide_details()
            return
        self.next_label.configure(text=line)
        if not self.next_label.winfo_manager():
            self.next_label.pack(anchor="w", pady=(0, 2))

    def _hide_details(self) -> None:
        if hasattr(self, "next_label") and self.next_label.winfo_manager():
            self.next_label.pack_forget()

    def set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _update_buttons_state(self) -> None:
        running = self.scheduler.is_running()
        self.recipients.set_enabled()
        locked = running or self._starting
        input_state = "disabled" if locked else "normal"
        for widget in (
            self.chat_entry,
            self.save_chat_btn,
            self.delete_chat_btn,
            self.message_box,
            self.paste_message_btn,
            self.template_button,
            self.test_message_btn,
            self.interval_min_entry,
            self.interval_max_entry,
            self.schedule_switch,
            self.schedule_from_entry,
            self.schedule_to_entry,
            self.smart_tz_menu,
            *self.schedule_day_checks,
            self.advanced_switch,
            self.consecutive_entry,
            self.break_min_entry,
            self.break_max_entry,
            self.order_menu,
            self.daily_cap_entry,
            self.require_warmup_check,
            *self.pace_buttons.values(),
            *getattr(self, "_task_action_buttons", []),
            *getattr(self, "account_checkboxes", []),
        ):
            widget.configure(state=input_state)
        for widget in self.saved_chats_frame.winfo_children():
            if isinstance(widget, ctk.CTkButton):
                widget.configure(state=input_state)
        followup_state = "disabled" if running or self._starting else "normal"
        for widget in (
            self.followup_switch,
            self.followup_box,
            self.paste_followup_btn,
            self.cancel_followups_btn,
            self.followup_wait_entry,
        ):
            widget.configure(state=followup_state)
        for name in ("select_all_button", "select_none_button"):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(state="disabled" if running or self._starting else "normal")
        self.mode_menu.configure(state="disabled" if running or self._starting else "normal")
        paused = bool(getattr(getattr(self.scheduler, "engine", None), "paused", False))
        if running:
            self.start_btn.configure(
                state="disabled",
                text="Задача на паузе" if paused else "Задача запущена",
            )
            self.pause_btn.configure(state="normal", text="Продолжить" if paused else "Пауза")
            self.stop_btn.configure(state="normal")
        else:
            self.start_btn.configure(
                state="disabled" if self._starting else "normal",
                text="Проверяем перед запуском..."
                if self._starting
                else (
                    ("Далее: сообщение / чат", "Далее: аккаунты и темп", "Далее: проверка", "Запустить задачу")[
                        self._step
                    ]
                ),
            )
            self.pause_btn.configure(state="disabled", text="Пауза")
            self.stop_btn.configure(state="disabled")
        if not running and self._loading_campaign:
            self.start_btn.configure(
                state="normal" if self._load_error else "disabled",
                text="Повторить загрузку" if self._load_error else "Загружаем настройки…",
            )
        self._footer_actions.pack_forget()
        if self._step == 3 or running:
            self._footer_actions.pack(fill="x", pady=(0, 6), before=self._footer_buttons)
        self.pause_btn.pack_forget()
        self.stop_btn.pack_forget()
        if running:
            self.pause_btn.pack(side="left", fill="x", expand=True, padx=8)
            self.stop_btn.pack(side="left", fill="x", expand=True)
        if hasattr(self, "_progress_frame"):
            self._progress_frame.pack_forget()
            if running:
                self._progress_frame.pack(fill="x", before=self.status_label)
            else:
                self._hide_details()
        tip(
            self.start_btn,
            "Начать реальные сообщения или приглашения после проверки подключения."
            if self._step == 3
            else "Перейти к следующему шагу. Сообщения и приглашения не отправляются.",
        )

    def set_invite_order(self, key: str) -> None:
        """Mirror a stored invite order into the menu so a later save does not overwrite it."""
        self.order_menu.set(
            next((title for title, value in INVITE_ORDER_CHOICES.items() if value == key), "Перемешивать")
        )

    def operation_mode(self):
        return "messages" if self.mode_var.get() == "Личные сообщения" else "invite"

    def on_mode_change(self):
        if self.operation_mode() == "messages":
            self.mode_hint.configure(
                text="Отправляет ваш текст людям в личную переписку. Ссылку на чат вставьте в текст сообщения."
            )
            self.chat_controls.pack_forget()
            self.message_frame.pack(fill="x")
            self._soften_message_interval()
        else:
            self.mode_hint.configure(
                text="Добавляет людей прямо в группу или канал. Выбранным аккаунтам нужны права администратора с правом приглашения."
            )
            self.message_frame.pack_forget()
            self.chat_controls.pack(fill="x")
        self._refresh_pace()
        self.refresh_accounts()

    def _pace_fields(self) -> dict[str, str]:
        return {
            "interval_min": self.interval_min_entry.get(),
            "interval_max": self.interval_max_entry.get(),
            "protection_consecutive": self.consecutive_entry.get(),
            "protection_break_min": self.break_min_entry.get(),
            "protection_break_max": self.break_max_entry.get(),
        }

    def current_pace(self) -> str:
        """Preset key matching the entered numbers, or «custom»."""
        return detect_preset(self.operation_mode(), self._pace_fields())

    def _pace_warnings(self) -> list[str]:
        try:
            low = int(self.interval_min_entry.get().strip())
            consecutive = int(self.consecutive_entry.get().strip())
        except ValueError:
            return []
        return aggressive_warnings(self.operation_mode(), low, consecutive)

    def _refresh_pace(self) -> None:
        """Highlight the active preset («Свой» when the numbers match none) and flag risky values."""
        if not hasattr(self, "pace_label"):
            return
        active = self.current_pace()
        for key, button in self.pace_buttons.items():
            if key == active:
                button.configure(fg_color=COLORS["accent_soft"], text_color=COLORS["accent"])
            else:
                apply_button_role(button, "secondary")
        self.pace_label.configure(text=f"Выбрано: {pace_label(active)}")
        warnings = self._pace_warnings()
        if warnings:
            self.pace_warning.configure(text="Внимание: " + " ".join(warnings))
            if not self.pace_warning.winfo_manager():
                self.pace_warning.pack(fill="x", padx=16, pady=(0, 10), after=self.pace_label.master)
        elif self.pace_warning.winfo_manager():
            self.pace_warning.pack_forget()
        if not self._loading_campaign:
            self.update_readiness()

    def apply_pace_preset(self, key: str) -> None:
        if self.scheduler.is_running() or self._starting:
            return
        values = preset_values(self.operation_mode(), key)
        for field, entry in (
            ("interval_min", self.interval_min_entry),
            ("interval_max", self.interval_max_entry),
            ("protection_consecutive", self.consecutive_entry),
            ("protection_break_min", self.break_min_entry),
            ("protection_break_max", self.break_max_entry),
        ):
            entry.delete(0, "end")
            entry.insert(0, values[field])
        self._refresh_pace()
        self.set_status(f"Темп «{PACE_LABELS[key]}» применён. Нажмите «Сохранить настройки», чтобы запомнить его.")

    def _soften_message_interval(self) -> None:
        """Switching to direct messages with an invite-style fast pace: offer the safe default."""
        if self._loading_campaign:
            return
        try:
            low = int(self.interval_min_entry.get().strip())
        except ValueError:
            return
        if low >= MESSAGES_MIN_INTERVAL:
            return
        for entry, value in zip(
            (self.interval_min_entry, self.interval_max_entry), MESSAGES_DEFAULT_INTERVAL, strict=True
        ):
            entry.delete(0, "end")
            entry.insert(0, str(value))
        self.set_status(
            f"Для личных сообщений интервал изменён на {MESSAGES_DEFAULT_INTERVAL[0]}–"
            f"{MESSAGES_DEFAULT_INTERVAL[1]} сек (минимум {MESSAGES_MIN_INTERVAL})."
        )
        self._refresh_pace()

    def show_message_preview(self) -> None:
        text = self.message_box.get("1.0", "end-1c")
        if not text.strip():
            self.preview_label.configure(text="Введите текст сообщения.")
            return
        note = "" if has_placeholders(text) else "\n(Подстановок нет — текст уйдёт без изменений.)"
        self.preview_label.configure(text=f"Предпросмотр для «{SAMPLE_NAME}»:\n{preview_message(text)}{note}")

    def send_test_to_saved(self) -> None:
        if self._starting or self.scheduler.is_running():
            messagebox.showwarning("Задача выполняется", "Остановите задачу: аккаунт нужен для тестовой отправки.")
            return
        text = self.message_box.get("1.0", "end-1c")
        try:
            validate_message(text)
        except ValueError as exc:
            messagebox.showerror("Тест в Избранное", explain_error(exc))
            return
        selected = [key for key, var in self.account_vars.items() if var.get()]
        if not selected:
            messagebox.showerror("Тест в Избранное", "Отметьте аккаунт, через который отправить тест.")
            return
        account_id = selected[0]
        phone = self._account_labels.get(account_id, str(account_id))
        if not messagebox.askyesno(
            "Тест в Избранное",
            f"Отправить текст в «Избранное» аккаунта {phone}?\n"
            f"Имя получателя подставится как «{SAMPLE_NAME}». Никому из списка сообщение не уйдёт.",
            parent=self.app,
        ):
            return
        self._starting = True
        self._update_buttons_state()
        self.set_status(f"Тестовая отправка в «Избранное» ({phone})…")
        db = self.db
        future = self.run_coro(send_test_message(db, account_id, text))

        def done(result):
            def render():
                self._starting = False
                self._update_buttons_state()
                if result.cancelled():
                    self.set_status("Тестовая отправка отменена")
                elif result.exception():
                    self.set_status("Тест не отправлен")
                    messagebox.showerror("Тест в Избранное", explain_error(result.exception()))
                else:
                    self.set_status("Тест отправлен в «Избранное»")
                    self.preview_label.configure(text=f"Отправлено в «Избранное»:\n{result.result()}")

            self._safe_after(render)

        future.add_done_callback(done)

    def task_values(self):
        if self._loading_campaign:
            raise ValueError("Дождитесь загрузки настроек кампании")
        mode = self.operation_mode()
        chat = normalize_chat(self.chat_entry.get()) if mode == "invite" else ""
        if mode == "invite" and not chat:
            raise ValueError("Укажите чат")
        message = self.message_box.get("1.0", "end-1c")
        if mode == "messages":
            validate_message(message)
            if self.followup_enabled.get():
                try:
                    validate_message(self.followup_box.get("1.0", "end-1c"))
                except ValueError as exc:
                    raise ValueError("Второе сообщение: " + str(exc)) from None
        followup_wait_hours = self.followup_wait_entry.get().strip() or "24"
        if self.followup_enabled.get() and not (followup_wait_hours.isdigit() and 1 <= int(followup_wait_hours) <= 168):
            raise ValueError("«Ждать ответ, часов»: целое число от 1 до 168")
        try:
            interval_min = int(self.interval_min_entry.get().strip())
            interval_max = int(self.interval_max_entry.get().strip())
        except ValueError:
            raise ValueError("Введите минимальный и максимальный интервал целыми числами, в секундах.") from None
        validate_interval(mode, interval_min, interval_max)
        selected_ids = [account_id for account_id, var in self.account_vars.items() if var.get()]
        if not selected_ids:
            raise ValueError("Выберите хотя бы один аккаунт")
        settings = {
            "target_chat": chat,
            "operation_mode": mode,
            "message_text": message,
            "followup_enabled": "1" if self.followup_enabled.get() else "0",
            "followup_text": self.followup_box.get("1.0", "end-1c"),
            "followup_wait_hours": followup_wait_hours if followup_wait_hours.isdigit() else "24",
            "interval_min": str(interval_min),
            "interval_max": str(interval_max),
            "schedule_enabled": "1" if self.schedule_enabled.get() else "0",
            "schedule_from": self.schedule_from_entry.get().strip() or "13:00",
            "schedule_to": self.schedule_to_entry.get().strip() or "21:00",
            "schedule_days": self._schedule_days_value(),
            "smart_interval_tz": validate_smart_tz(SMART_TZ_CHOICES.get(self.smart_tz_menu.get(), SMART_TZ_LOCAL)),
            "protection_consecutive": self.consecutive_entry.get().strip() or "5",
            "protection_break_min": self.break_min_entry.get().strip() or "5",
            "protection_break_max": self.break_max_entry.get().strip() or "15",
            "invite_order": INVITE_ORDER_CHOICES.get(self.order_menu.get(), "shuffle"),
            "campaign_daily_cap": str(parse_daily_cap(self.daily_cap_entry.get())),
            "require_warmup_ready": "1" if self.require_warmup_var.get() else "0",
        }
        if settings["schedule_enabled"] == "1":
            time_minutes(settings["schedule_from"])
            time_minutes(settings["schedule_to"])
        for key in ("protection_consecutive", "protection_break_min", "protection_break_max"):
            try:
                value = int(settings[key])
            except ValueError:
                raise ValueError("Введите число действий и длительность перерывов целыми числами.") from None
            if not 1 <= value <= 10000:
                raise ValueError("Настройки перерывов должны быть положительными числами до 10000")
        if int(settings["protection_break_min"]) > int(settings["protection_break_max"]):
            raise ValueError("Минимальный перерыв не может быть больше максимального")
        settings["selected_accounts"] = json.dumps(selected_ids)
        return chat, interval_min, interval_max, selected_ids, settings

    def load_campaign_settings(self):
        self._loading_campaign = True
        self._load_error = False
        self._update_buttons_state()
        self.recipients.refresh()
        db = self.db
        defaults = {
            "target_chat": "",
            "interval_min": "20",
            "interval_max": "40",
            "selected_accounts": "[]",
            "message_text": "",
            "followup_text": "",
            "followup_enabled": "0",
            "followup_wait_hours": "24",
            "schedule_enabled": "0",
            "schedule_from": "13:00",
            "schedule_to": "21:00",
            "schedule_days": DEFAULT_SCHEDULE_DAYS,
            "smart_interval_tz": SMART_TZ_LOCAL,
            "protection_consecutive": "5",
            "protection_break_min": "5",
            "protection_break_max": "15",
            "invite_order": "shuffle",
            "campaign_daily_cap": "0",
            "require_warmup_ready": "0",
        }

        async def load():
            try:
                kind = await db.get_campaign_kind()
            except ValueError:
                # Startup may briefly hold the deleted default scope before
                # App restores the last remaining campaign.
                return None
            if kind == "messages":
                # Softer defaults for direct messages (PeerFlood risk).
                defaults["interval_min"], defaults["interval_max"] = map(str, MESSAGES_DEFAULT_INTERVAL)
            values = {key: await db.get_setting(key, value) for key, value in defaults.items()}
            values["kind"] = kind
            return values

        future = self.run_coro(load())

        def done(result):
            def render():
                if self.db is not db:
                    return
                if result.cancelled() or result.exception() or result.result() is None:
                    self._load_error = True
                    self.status_label.configure(text="Не удалось загрузить настройки. Нажмите «Повторить загрузку».")
                    self._update_buttons_state()
                    return
                values = result.result()
                self.mode_var.set("Личные сообщения" if values["kind"] == "messages" else "Приглашения")
                self.message_box.delete("1.0", "end")
                draft = self._drafts.get(db.campaign_id, (db, values))[1]
                self.message_box.insert("1.0", draft["message_text"])
                self.message_box._textbox.edit_modified(False)
                self.followup_box.configure(state="normal")
                self.followup_box.delete("1.0", "end")
                self.followup_box.insert("1.0", draft["followup_text"])
                self.followup_box._textbox.edit_modified(False)
                self.followup_enabled.set(draft["followup_enabled"] == "1")
                self.toggle_followup()
                self._followup_stats = {}
                self.update_message_count()
                self.on_mode_change()
                for key, entry in (
                    ("target_chat", self.chat_entry),
                    ("interval_min", self.interval_min_entry),
                    ("interval_max", self.interval_max_entry),
                    ("followup_wait_hours", self.followup_wait_entry),
                    ("schedule_from", self.schedule_from_entry),
                    ("schedule_to", self.schedule_to_entry),
                    ("protection_consecutive", self.consecutive_entry),
                    ("protection_break_min", self.break_min_entry),
                    ("protection_break_max", self.break_max_entry),
                    ("campaign_daily_cap", self.daily_cap_entry),
                ):
                    entry.delete(0, "end")
                    entry.insert(0, values[key])
                self.require_warmup_var.set(values["require_warmup_ready"] == "1")
                self._refresh_pace()
                self.order_menu.set(
                    next(
                        (title for title, key in INVITE_ORDER_CHOICES.items() if key == values["invite_order"]),
                        "Перемешивать",
                    )
                )
                selected = json.loads(values["selected_accounts"] or "[]")
                self._saved_account_ids = selected
                for key, var in self.account_vars.items():
                    var.set(key in selected if selected else True)
                self.schedule_enabled.set(values["schedule_enabled"] == "1")
                self.set_schedule_days(values["schedule_days"])
                self.smart_tz_menu.set(smart_tz_label(values["smart_interval_tz"]))
                self.on_schedule_toggle()
                if self.schedule_enabled.get():
                    self.advanced_var.set(True)
                    self.toggle_advanced()
                self._loading_campaign = False
                self._update_buttons_state()
                self.update_readiness()

            self._safe_after(render)

        future.add_done_callback(done)

    def _message_modified(self, _event=None):
        text_widget = getattr(_event, "widget", self.message_box._textbox)
        text_widget = getattr(text_widget, "_textbox", text_widget)
        if not text_widget.edit_modified():
            return
        text_widget.edit_modified(False)
        self.update_message_count()
        if self._loading_campaign or self.app._closing:
            return
        self.capture_draft()
        self.update_readiness()
        if self._draft_job is not None:
            self.after_cancel(self._draft_job)
        self._draft_job = self.after(750, self.flush_draft)

    def update_message_count(self):
        length = len(self.message_box.get("1.0", "end-1c").encode("utf-16-le")) // 2
        self.message_counter.configure(
            text=f"{length} / 4096", text_color=COLORS["error"] if length > 4096 else COLORS["muted"]
        )
        length = len(self.followup_box.get("1.0", "end-1c").encode("utf-16-le")) // 2
        self.followup_counter.configure(
            text=f"{length} / 4096", text_color=COLORS["error"] if length > 4096 else COLORS["muted"]
        )

    def toggle_followup(self):
        if self.followup_enabled.get():
            self.followup_frame.pack(fill="x", after=self.followup_switch)
        else:
            self.followup_frame.pack_forget()
        if not self._loading_campaign:
            self.flush_draft()
            self.update_readiness()

    def cancel_followups(self):
        if self.scheduler.is_running() or self._starting:
            return
        if not messagebox.askyesno(
            "Отменить ожидание?",
            "Отменить все ожидающие вторые сообщения этой кампании?\nПервые сообщения и журнал отправок сохранятся.",
            parent=self.app,
        ):
            return
        db = self.db
        future = self.run_coro(db.cancel_followups())

        def done(result):
            def render():
                if result.cancelled() or result.exception():
                    messagebox.showerror(
                        "Вторые сообщения", "Не удалось отменить ожидания. Остановите задачу и повторите."
                    )
                elif self.db is db:
                    self.followup_status.configure(text=f"Ожидания второго сообщения отменены: {result.result()}")
                    self.app.targets_tab.refresh_table()

            self._safe_after(render)

        future.add_done_callback(done)

    def capture_draft(self):
        if not self._loading_campaign:
            self._drafts[self.db.campaign_id] = (
                self.db,
                {
                    "message_text": self.message_box.get("1.0", "end-1c"),
                    "followup_text": self.followup_box.get("1.0", "end-1c"),
                    "followup_enabled": "1" if self.followup_enabled.get() else "0",
                },
            )
        return list(self._drafts.values())

    def flush_draft(self):
        if self._draft_job is not None:
            self.after_cancel(self._draft_job)
            self._draft_job = None
        snapshots = self.capture_draft()
        if snapshots:
            self.run_coro(self.save_drafts(snapshots))

    async def save_drafts(self, snapshots):
        # Serialize writes so an older debounce cannot overwrite the text saved at exit.
        async with self._draft_lock:
            available = {row["id"] for row in await self.db.get_campaigns()}
            for db, values in snapshots:
                if db.campaign_id not in available:
                    continue
                for key, value in values.items():
                    await db.set_setting(key, value)

    def save_settings(self):
        try:
            *_, settings = self.task_values()
        except ValueError as exc:
            messagebox.showerror("Настройки", explain_error(exc))
            return
        db = self.db

        async def save():
            async with self._draft_lock:
                await db.save_campaign_settings(settings)

        future = self.run_coro(save())
        future.add_done_callback(
            lambda f: (
                self._safe_after(lambda: self.set_status("Настройки кампании сохранены"))
                if not f.cancelled() and not f.exception()
                else None
            )
        )

    def check_task(self, live=False):
        if not self.recipients.ready():
            self._scroll_section(0)
            return
        if self._starting or self.scheduler.is_running():
            messagebox.showwarning("Задача выполняется", "Остановите задачу перед диагностикой")
            return
        try:
            chat, low, high, ids, settings = self.task_values()
        except ValueError as exc:
            messagebox.showerror("Проверка", explain_error(exc))
            return
        db = self.db
        schedule_hours = self._schedule_hours()
        self._starting = True
        self._update_buttons_state()
        future = self.run_coro(
            preflight(
                db,
                ids,
                chat,
                live,
                mode=settings["operation_mode"],
                message=settings["message_text"],
                followup_enabled=settings["followup_enabled"] == "1",
                interval_min=low,
                interval_max=high,
                schedule_hours=schedule_hours,
            )
        )

        def done(result):
            def render():
                self._starting = False
                self._update_buttons_state()
                if result.cancelled():
                    return
                if result.exception():
                    messagebox.showerror("Проверка", explain_error(result.exception()))
                    return
                text = format_report(result.result())
                text += f"\n\nИнтервал: {low}–{high} сек. Расписание: " + (
                    f"{settings['schedule_from']}–{settings['schedule_to']}"
                    if settings["schedule_enabled"] == "1"
                    else "выключено"
                )
                text += "\nТемп: " + pace_label(self.current_pace())
                cap = int(settings["campaign_daily_cap"])
                text += f"\nДневной лимит кампании: {cap if cap else 'выключен'}"
                if settings["require_warmup_ready"] == "1":
                    _, gated = split_warmup_ready(
                        [self._accounts_info[key] for key in ids if key in self._accounts_info]
                    )
                    text += "\nТолько прогретые аккаунты: включено"
                    if gated:
                        text += ". Будут пропущены: " + describe_skipped_warmup(gated)
                warning_block = format_warning_block(
                    aggressive_warnings(settings["operation_mode"], low, int(settings["protection_consecutive"]))
                )
                if warning_block:
                    text += "\n\n" + warning_block
                if settings["operation_mode"] == "messages":
                    text += "\n\nТекст сообщения:\n" + settings["message_text"]
                    if settings["followup_enabled"] == "1":
                        text += "\n\nВторое сообщение — только после ответа:\n" + settings["followup_text"]
                text += "\nПриглашения и сообщения не отправлялись."
                show_report(self, "Диагностика кампании", text)

            self._safe_after(render)

        future.add_done_callback(done)

    def start_task(self) -> None:
        if getattr(self.app, "_audience_imports", 0):
            messagebox.showwarning(
                "Импорт списка выполняется",
                "Дождитесь окончания импорта получателей, затем запускайте задачу.",
            )
            return
        if not self.recipients.ready():
            self._scroll_section(0)
            return
        if self.app.parsing_tab.busy():
            messagebox.showwarning("Получение участников", "Завершите или остановите парсинг перед запуском задачи")
            return
        if self._starting or self.scheduler.is_running():
            messagebox.showwarning("Внимание", "Задача уже запущена.")
            return
        try:
            chat, interval_min, interval_max, selected_ids, settings = self.task_values()
        except ValueError as exc:
            messagebox.showerror("Ошибка запуска", explain_error(exc))
            return
        warnings = aggressive_warnings(
            settings["operation_mode"], interval_min, int(settings["protection_consecutive"])
        )
        if warnings and not messagebox.askyesno(
            "Агрессивные настройки темпа",
            format_warning_block(warnings)
            + "\n\nРекомендуем нажать «Безопасный» или «Обычный» в блоке интервала. Всё равно запустить?",
            icon="warning",
            parent=self.app,
        ):
            return
        self.start_btn.configure(state="disabled")
        self._starting = True
        self._update_buttons_state()
        db = self.db
        require_warmup = settings["require_warmup_ready"] == "1"

        async def prepare():
            marked_running = False
            try:
                accounts = await db.get_accounts_by_ids(selected_ids)
                if not accounts:
                    raise ValueError("Выбранные аккаунты не найдены. Обновите список и выберите доступный аккаунт")
                accounts, skipped = gate_accounts(accounts, require_warmup)
                if skipped:
                    notice = "Пропущены не прогретые аккаунты: " + describe_skipped_warmup(skipped)
                    self._safe_after(lambda text=notice: self.log_callback("WARN", text))
                if not accounts:
                    raise ValueError(warmup_gate_error(skipped))
                report = await preflight(
                    db,
                    [account["id"] for account in accounts],
                    chat,
                    live=True,
                    mode=settings["operation_mode"],
                    message=settings["message_text"],
                    followup_enabled=settings["followup_enabled"] == "1",
                )
                if not report["ok"]:
                    raise ValueError(format_report(report))
                async with self._draft_lock:
                    await db.save_campaign_settings({**settings, "task_status": "running"})
                marked_running = True
                self.scheduler.start(
                    accounts,
                    chat,
                    interval_min,
                    interval_max,
                    mode=settings["operation_mode"],
                    message=settings["message_text"],
                )
            except Exception:
                if marked_running and not self.scheduler.is_running():
                    await db.set_setting("task_status", "stopped")
                raise

        future = self.run_coro(prepare())

        def done(result):
            self._starting = False
            try:
                result.result()
            except Exception as exc:
                self._safe_after(lambda exc=exc: messagebox.showerror("Ошибка запуска", explain_error(exc)))
            else:
                self._safe_after(lambda: self.log_callback("INFO", "Задача запущена"))
                self._safe_after(lambda: self.set_status("Выполняется..."))
            self._safe_after(self._update_buttons_state)

        future.add_done_callback(done)

    def pause_or_resume(self) -> None:
        if not self.scheduler.is_running():
            messagebox.showwarning("Внимание", "Нет активной задачи.")
            return
        if self.pause_btn.cget("text") == "Продолжить":
            self.scheduler.resume()
            self.run_coro(self.db.set_setting("task_status", "running"))
            self.log_callback("INFO", "Задача продолжена")
            self.set_status("Выполняется...")
            self.pause_btn.configure(text="Пауза")
        else:
            self.scheduler.pause()
            self.run_coro(self.db.set_setting("task_status", "paused"))
            self.log_callback("INFO", "Задача на паузе")
            self.set_status("Пауза")
            self.pause_btn.configure(text="Продолжить")

    def stop_task(self) -> None:
        if not self.scheduler.is_running() or getattr(self, "_stop_confirming", False):
            return
        if self.operation_mode() != "messages" or not self.followup_enabled.get():
            self._stop_task_now()
            return
        # Stopping cancels every waiting second message: ask first, but never block the UI thread.
        self._stop_confirming = True
        db = self.db
        future = self.run_coro(db.get_followup_stats())

        def done(result):
            def render():
                self._stop_confirming = False
                pending = 0
                if not result.cancelled() and not result.exception():
                    stats = result.result()
                    pending = stats.get("waiting", 0) + stats.get("ready", 0)
                if pending > 0 and not messagebox.askyesno(
                    "Остановить задачу?",
                    f"Остановить задачу? Ожидание вторых сообщений ({pending}) будет отменено.",
                    parent=self.app,
                ):
                    return
                self._stop_task_now()

            self._safe_after(render)

        future.add_done_callback(done)

    def _stop_task_now(self) -> None:
        if not self.scheduler.is_running():
            return
        self.scheduler.stop()
        self.start_btn.configure(state="disabled", text="Останавливаем задачу...")
        self.run_coro(self.db.set_setting("task_status", "stopped"))
        self.log_callback("INFO", "Задача остановлена")
        self.set_status("Остановлено")
        self._update_buttons_state()

    def show_summary(self, stats: dict) -> None:
        msg = (
            "Задача завершена\n\n"
            f"Добавлено: {stats.get('added', 0)}\n"
            f"Первых сообщений: {stats.get('sent', 0)}\n"
            f"Вторых сообщений после ответа: {stats.get('followup_sent', 0)}\n"
            f"Вторых сообщений отменено после завершения: {stats.get('followup_cancelled', 0)}\n"
            f"Пропущено: {stats.get('skipped', 0)}\n"
            f"Ошибок: {stats.get('errors', 0)}\n"
            f"Время работы: {stats.get('duration', '00:00:00')}"
        )
        accounts = stats.get("accounts") or []
        if accounts:
            msg += "\n\nПо аккаунтам:\n" + "\n".join(
                f"{item['phone']}: добавлено {item['added']}, пропущено {item['skipped']}, ошибок {item['errors']}"
                for item in accounts
            )
        outcome = stats.get("outcome", "completed")
        title = {"completed": "Итог", "stopped": "Остановлено", "failed": "Ошибка задачи"}.get(outcome, "Итог")
        msg = msg.replace("Задача завершена", title, 1)
        self._update_buttons_state()
        messagebox.showinfo(title, msg)
