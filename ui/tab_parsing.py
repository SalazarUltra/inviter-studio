"""Participant collection with explicit access checks, saved runs and local segments."""

import asyncio
import os
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from core.account_manager import AccountManager
from core.chat_authors import (
    MAX_HISTORY_MESSAGES,
    PERIOD_CHOICES,
    ChatAuthorsEngine,
    filter_author_stats,
    history_period,
)
from core.export import write_parsed
from core.parser import (
    ACTIVITY_LABELS,
    MAX_PER_SOURCE,
    ParserEngine,
    access_error,
    filter_members,
    group_members,
    parse_sources,
    unique_members,
)
from core.user_feedback import explain_error
from core.validation import normalize_username
from ui.campaigns import CampaignBar
from ui.components import page_intro
from ui.dispatch import post_ui
from ui.entry_utils import paste_text
from ui.hints import hint, tip, wrap_label
from ui.theme import COLORS, FONTS, apply_button_role, style_tree

RUN_LABELS = {
    "running": "Получение",
    "completed": "Завершено",
    "partial": "Часть списка",
    "failed": "Ошибка",
    "stopped": "Остановлено",
    "interrupted": "Прервано",
}
ROLE_CHOICES = {"Все роли": "all", "Создатели и админы": "admins", "Обычные участники": "members"}
ACTIVITY_CHOICES = {
    "Все статусы": "all",
    "Онлайн": "online",
    "Недавно / на неделе": "recent",
    "Недавно": "recently",
    "На этой неделе": "week",
    "За 7 дней: точное время": "exact_week",
    "В этом месяце": "month",
    "Есть точное время": "offline",
    "Не видно": "unknown",
}
GROUP_CHOICES = {
    "Без группировки": "none",
    "По группам": "source",
    "По ролям": "role",
    "По активности": "activity",
    "По никнейму": "username",
    "По Premium": "premium",
    "По числу сообщений": "messages",
}
MODE_CHOICES = {"Участники группы": "members", "Авторы сообщений": "authors"}
MIN_MESSAGES = {
    "Любое число": 0,
    "От 2 сообщений": 2,
    "От 5 сообщений": 5,
    "От 10 сообщений": 10,
    "От 20 сообщений": 20,
    "От 100 сообщений": 100,
}
LAST_POST = {
    "Любая дата сообщения": None,
    "Писал за последний день": 1,
    "Писал за 7 дней": 7,
    "Писал за 30 дней": 30,
    "Писал за 90 дней": 90,
}
EXPORT_FORMATS = {"CSV · таблица / Excel": "csv", "TXT · никнеймы": "txt", "JSON · данные": "json"}


class ParsingTab(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color=COLORS["bg"])
        self.app = app
        self.db = app.db
        self.future = None
        self.engine = None
        self.accounts = {}
        self._selected_account_id = None
        self.groups = {}
        self.runs = {}
        self.rows = []
        self.filtered = []
        self.selected_run = None
        self.page = 0
        self._query_generation = 0
        self._result_mode = "members"
        self._input_mode = "members"
        self._limits = {"members": "500", "authors": "2000"}
        self._exporting = False
        self.export_future = None
        self._results_loading = False
        self._export_path_generation = 0
        self.last_export_path = None
        self.export_buttons = {}
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        intro = page_intro(
            self,
            "Парсинг групп",
            "Получите участников или авторов доступных сообщений, отфильтруйте и выгрузите список.",
            "ДАННЫЕ TELEGRAM",
        )
        intro.pack_forget()
        intro.grid(row=0, column=0, sticky="ew")
        self._intro_labels = [widget for widget in intro.winfo_children() if isinstance(widget, ctk.CTkLabel)]
        self._compact_header = None
        self.bind("<Configure>", self.fit_header, add="+")
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 8))
        toolbar.grid_columnconfigure(1, weight=1)
        navigation = ctk.CTkFrame(toolbar, fg_color="transparent")
        navigation.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        self.view_buttons = {}
        for key, title in (("setup", "1. Настройка парсинга"), ("results", "2. Результаты и сохранение")):
            button = ctk.CTkButton(
                navigation, text=title, width=220, height=32, command=lambda value=key: self.show_view(value)
            )
            button.pack(side="left", padx=(0, 8))
            self.view_buttons[key] = button
        self.account_bar = ctk.CTkFrame(toolbar, fg_color="transparent")
        self.account_bar.grid(row=1, column=0, columnspan=4, sticky="ew")
        self.account_bar.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.account_bar, text="Аккаунт:").grid(row=0, column=0, padx=(0, 8))
        self.account_menu = ctk.CTkOptionMenu(
            self.account_bar, values=["Загрузка…"], width=220, command=self.account_changed
        )
        self.account_menu.grid(row=0, column=1, sticky="w")
        self.groups_button = ctk.CTkButton(
            self.account_bar, text="Мои группы", width=130, height=30, command=self.load_groups
        )
        self.groups_button.grid(row=0, column=2, padx=8)
        self.help_button = ctk.CTkButton(
            self.account_bar,
            text="Как получить доступ?",
            width=170,
            height=30,
            command=lambda: app.show_help("parsing"),
        )
        self.help_button.grid(row=0, column=3)
        save_bar = ctk.CTkFrame(toolbar, fg_color="transparent")
        self.save_bar = save_bar
        save_bar.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(0, 0))
        save_bar.grid_columnconfigure(0, weight=1)
        self.save_file_button = ctk.CTkButton(
            save_bar,
            text="Сохранить файл на ПК",
            width=255,
            height=34,
            state="disabled",
            command=lambda: self.export(EXPORT_FORMATS[self.export_format.get()]),
        )
        self.save_file_button.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.export_format = ctk.CTkOptionMenu(
            save_bar, values=list(EXPORT_FORMATS), width=180, height=34, command=lambda _: self.update_export_controls()
        )
        self.export_format.grid(row=0, column=1, padx=(0, 8))
        self.open_folder_button = ctk.CTkButton(
            save_bar, text="Открыть папку", width=140, height=34, state="disabled", command=self.open_saved_folder
        )
        self.open_folder_button.grid(row=0, column=2)
        self.export_notice = ctk.CTkLabel(
            toolbar,
            text="Файл на ПК создаётся кнопкой «Сохранить». Результаты также остаются в программе.",
            font=FONTS["small"],
            text_color=COLORS["muted"],
            anchor="w",
            height=24,
            width=1,
        )
        self.export_notice.grid(row=3, column=0, columnspan=4, sticky="ew", pady=(3, 0))
        tip(
            self.export_notice,
            "После парсинга результат хранится внутри программы. Нажмите «Сохранить файл на ПК», выберите папку и подтвердите сохранение в окне Windows. Затем «Открыть папку» покажет, где находится файл.",
        )
        self.body = ctk.CTkScrollableFrame(self, fg_color=COLORS["bg"])
        self.body.grid(row=2, column=0, sticky="nsew", padx=8)
        self.body.grid_columnconfigure(0, weight=1)
        self.results_body = ctk.CTkScrollableFrame(self, fg_color=COLORS["bg"])
        self.results_body.grid(row=2, column=0, sticky="nsew", padx=8)
        self.results_body.grid_columnconfigure(0, weight=1)
        self._card_parent = self.body
        self._card_row = 0
        config = self.card("1. Источники")
        self.mode_controls = ctk.CTkFrame(config, fg_color="transparent")
        self.mode_controls.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(self.mode_controls, text="Что получить:", font=FONTS["small"]).pack(side="left", padx=(0, 8))
        self.mode_menu = ctk.CTkOptionMenu(self.mode_controls, values=list(MODE_CHOICES), command=self.mode_changed)
        self.mode_menu.pack(side="left", fill="x", expand=True)
        self.author_options = ctk.CTkFrame(config, fg_color="transparent")
        self.author_options.pack(fill="x", padx=16, pady=(0, 8))
        self.author_options.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(self.author_options, text="Период истории", font=FONTS["small"]).grid(row=0, column=0, sticky="w")
        self.period_menu = ctk.CTkOptionMenu(
            self.author_options, values=list(PERIOD_CHOICES), command=lambda _: self.period_changed()
        )
        self.period_menu.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ctk.CTkLabel(self.author_options, text="С даты (ГГГГ-ММ-ДД)", font=FONTS["small"]).grid(
            row=2, column=0, sticky="w"
        )
        ctk.CTkLabel(self.author_options, text="По дату включительно", font=FONTS["small"]).grid(
            row=2, column=1, sticky="w"
        )
        self.date_from = ctk.CTkEntry(self.author_options, placeholder_text="2026-10-01")
        self.date_to = ctk.CTkEntry(self.author_options, placeholder_text="2026-10-14")
        self.date_from.grid(row=3, column=0, sticky="ew", padx=(0, 8))
        self.date_to.grid(row=3, column=1, sticky="ew")
        author_hint = ctk.CTkLabel(
            self.author_options,
            text="Считаются доступные обычные сообщения, включая медиа. Тексты не сохраняются. Даты — по времени компьютера. Найденный автор мог уже покинуть группу.",
            font=FONTS["small"],
            text_color=COLORS["muted"],
            anchor="w",
            justify="left",
        )
        author_hint.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        wrap_label(author_hint)
        hint(
            config,
            "Публичный @адрес или ссылка t.me — по одному на строку. Приватную группу выберите из «Моих групп». Программа не вступает в группы автоматически.",
        )
        own = ctk.CTkFrame(config, fg_color="transparent")
        own.pack(fill="x", padx=16, pady=(0, 8))
        self.group_menu = ctk.CTkOptionMenu(own, values=["Сначала нажмите «Мои группы»"], width=320)
        self.group_menu.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.add_group_button = ctk.CTkButton(own, text="Добавить", width=95, height=30, command=self.add_group)
        self.add_group_button.pack(side="left")
        self.source_box = ctk.CTkTextbox(config, height=90, wrap="none", font=FONTS["body"])
        self.source_box.pack(fill="x", padx=16, pady=(0, 8))
        editing = ctk.CTkFrame(config, fg_color="transparent")
        editing.pack(fill="x", padx=16, pady=(0, 8))
        self.paste_button = ctk.CTkButton(
            editing, text="Вставить", width=100, height=28, command=lambda: paste_text(self.source_box)
        )
        self.paste_button.pack(side="left", padx=(0, 8))
        self.clear_button = ctk.CTkButton(
            editing, text="Очистить поле", width=130, height=28, command=lambda: self.source_box.delete("1.0", "end")
        )
        self.clear_button.pack(side="left")
        limits = ctk.CTkFrame(config, fg_color="transparent")
        limits.pack(fill="x", padx=16, pady=(0, 12))
        self.limit_label = ctk.CTkLabel(limits, text="Лимит участников на группу:", font=FONTS["small"])
        self.limit_label.pack(side="left")
        self.limit_entry = ctk.CTkEntry(limits, width=80, height=30)
        self.limit_entry.insert(0, "500")
        self.limit_entry.pack(side="left", padx=8)
        tip(
            self.limit_entry,
            "До 5000 участников или 10000 сообщений на источник; до 10 источников и 20000 записей/сообщений за запуск. Лимит сообщений не равен числу людей.",
        )
        self._card_parent = self.results_body
        self._card_row = 0
        results = self.card("Сохранённые результаты")
        self.history_menu = ctk.CTkOptionMenu(results, values=["Запусков пока нет"], command=self.choose_run)
        self.history_menu.pack(fill="x", padx=16, pady=(0, 8))
        self.result_hint = hint(
            results, "Выберите сохранённый результат. Участники и авторы сообщений — разные режимы."
        )
        self.source_report = ctk.CTkTextbox(results, height=85, wrap="word", font=FONTS["small"], state="disabled")
        self.source_report.pack(fill="x", padx=16, pady=(0, 10))
        self.result_hint.pack_forget()
        self.source_report.pack_forget()
        result_actions = ctk.CTkFrame(results, fg_color="transparent")
        result_actions.pack(fill="x", padx=16, pady=(0, 8))
        self.filters_var = ctk.BooleanVar(value=False)
        self.filters_switch = ctk.CTkSwitch(
            result_actions, text="Фильтры и группировка", variable=self.filters_var, command=self.toggle_filters
        )
        self.filters_switch.pack(side="left")
        self.details_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            result_actions, text="Отчёт об источниках", variable=self.details_var, command=self.toggle_details
        ).pack(side="right")
        self.segment_label = hint(results, "Пока нет результатов. Получите список на экране «Настройка парсинга».")
        filters = self.card("Фильтры и группировка")
        self.filters_card = filters
        self.filter_vars = {
            key: ctk.BooleanVar(value=key in {"only_username", "exclude_bots", "exclude_deleted"})
            for key in ("only_username", "exclude_bots", "exclude_deleted", "premium", "photo")
        }
        checks = ctk.CTkFrame(filters, fg_color="transparent")
        checks.pack(fill="x", padx=16, pady=(0, 8))
        for index, (key, text) in enumerate(
            (
                ("only_username", "С никнеймом"),
                ("exclude_bots", "Без ботов"),
                ("exclude_deleted", "Без удалённых"),
                ("premium", "Premium отмечен"),
                ("photo", "Фото видно"),
            )
        ):
            ctk.CTkCheckBox(checks, text=text, variable=self.filter_vars[key], command=self.apply_filters).grid(
                row=index // 3, column=index % 3, sticky="w", padx=(0, 16), pady=5
            )
        options = ctk.CTkFrame(filters, fg_color="transparent")
        options.pack(fill="x", padx=16, pady=(0, 8))
        options.grid_columnconfigure((0, 1), weight=1)
        self.role_menu = ctk.CTkOptionMenu(options, values=list(ROLE_CHOICES), command=lambda _: self.apply_filters())
        self.role_menu.grid(row=0, column=0, sticky="ew", padx=(0, 8), pady=(0, 8))
        self.activity_menu = ctk.CTkOptionMenu(
            options, values=list(ACTIVITY_CHOICES), command=lambda _: self.apply_filters()
        )
        self.activity_menu.grid(row=0, column=1, sticky="ew", pady=(0, 8))
        self.source_filter = ctk.CTkOptionMenu(options, values=["Все группы"], command=lambda _: self.apply_filters())
        self.source_filter.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.grouping_menu = ctk.CTkOptionMenu(
            options, values=list(GROUP_CHOICES), command=lambda _: self.apply_filters()
        )
        self.grouping_menu.grid(row=1, column=1, sticky="ew")
        self.author_filters = ctk.CTkFrame(filters, fg_color="transparent")
        self.author_filters.pack(fill="x", padx=16, pady=(0, 8))
        self.author_filters.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkLabel(self.author_filters, text="Найденных сообщений автора", font=FONTS["small"]).grid(
            row=0, column=0, sticky="w"
        )
        ctk.CTkLabel(self.author_filters, text="Последнее найденное сообщение", font=FONTS["small"]).grid(
            row=0, column=1, sticky="w"
        )
        self.min_messages_menu = ctk.CTkOptionMenu(
            self.author_filters, values=list(MIN_MESSAGES), command=lambda _: self.apply_filters(), state="disabled"
        )
        self.min_messages_menu.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.last_post_menu = ctk.CTkOptionMenu(
            self.author_filters, values=list(LAST_POST), command=lambda _: self.apply_filters(), state="disabled"
        )
        self.last_post_menu.grid(row=1, column=1, sticky="ew")
        hint(
            filters,
            "Статус «недавно» приблизительный. Последнее сообщение и онлайн — разные показатели. Счётчики доступны у авторов; роли — у участников. Фильтры не делают запросов Telegram.",
        )
        search = ctk.CTkFrame(filters, fg_color="transparent")
        search.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(search, text="Имя, никнейм или ID:", font=FONTS["small"]).pack(side="left", padx=(0, 8))
        self.search_var = ctk.StringVar()
        self.search_entry = ctk.CTkEntry(search, textvariable=self.search_var)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.group_summary = hint(filters, "Сначала получите участников или выберите сохранённый запуск")
        self.table_card = self.card("Найденные люди")
        table_frame = ctk.CTkFrame(self.table_card, fg_color="transparent", height=240)
        table_frame.pack(fill="x", padx=16, pady=(0, 8))
        table_frame.pack_propagate(False)
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)
        style = ttk.Style(self)
        style.configure(
            "Parser.Treeview",
            background=COLORS["input"],
            fieldbackground=COLORS["input"],
            foreground=COLORS["text"],
            rowheight=28,
            font=("Segoe UI", 10),
        )
        style.map(
            "Parser.Treeview",
            background=[("selected", COLORS["accent_soft"])],
            foreground=[("selected", COLORS["accent"])],
        )
        self.table = ttk.Treeview(
            table_frame,
            columns=("id", "username", "name", "activity", "messages", "last_post", "sources"),
            show="headings",
            style="Parser.Treeview",
        )
        for key, title, width in (
            ("id", "Telegram ID", 100),
            ("username", "Никнейм", 160),
            ("name", "Имя", 180),
            ("activity", "Активность", 160),
            ("messages", "Сообщений", 100),
            ("last_post", "Последнее сообщение", 180),
            ("sources", "Группы", 250),
        ):
            self.table.heading(key, text=title)
            self.table.column(key, width=width, minwidth=70)
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical = ctk.CTkScrollbar(table_frame, command=self.table.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ctk.CTkScrollbar(table_frame, orientation="horizontal", command=self.table.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        pages = ctk.CTkFrame(self.table_card, fg_color="transparent")
        pages.pack(fill="x", padx=16, pady=(0, 10))
        ctk.CTkButton(pages, text="‹", width=30, command=lambda: self.change_page(-1)).pack(side="left")
        self.page_label = ctk.CTkLabel(pages, text="0 человек", font=FONTS["small"])
        self.page_label.pack(side="left", padx=12)
        ctk.CTkButton(pages, text="›", width=30, command=lambda: self.change_page(1)).pack(side="left")
        for widget in pages.winfo_children():
            if isinstance(widget, ctk.CTkButton):
                apply_button_role(widget, "secondary")
        export = self.card("Передать в кампанию или удалить результат")
        CampaignBar(export).pack(fill="x", padx=16, pady=(0, 8))
        exports = ctk.CTkFrame(export, fg_color="transparent")
        exports.pack(fill="x", padx=16, pady=(0, 8))
        for format_name in ("csv", "json", "txt"):
            button = ctk.CTkButton(
                exports,
                text=f"Экспорт {format_name.upper()}",
                width=130,
                command=lambda value=format_name: self.export(value),
            )
            button.pack(side="left", padx=(0, 8))
            self.export_buttons[format_name] = button
        self.import_button = ctk.CTkButton(exports, text="В аудиторию…", width=150, command=self.to_audience)
        self.import_button.pack(side="left")
        self.delete_button = ctk.CTkButton(export, text="Удалить этот запуск", width=170, command=self.delete_run)
        apply_button_role(self.delete_button, "danger")
        self.delete_button.pack(anchor="w", padx=16, pady=(0, 8))
        hint(
            export,
            "Выгружается весь отфильтрованный сегмент, независимо от страницы. CSV/JSON сохраняют ID и источники; TXT — никнеймы. Один Telegram ID — одна строка. Получение списка не означает согласия людей на сообщения.",
        )
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=16, pady=8)
        footer.grid_columnconfigure((0, 1, 2), weight=1)
        self.check_button = ctk.CTkButton(footer, text="Проверить доступ", height=36, command=self.check_access)
        self.start_button = ctk.CTkButton(footer, text="Получить участников", height=36, command=self.start)
        self.stop_button = ctk.CTkButton(footer, text="Остановить", height=36, command=self.stop, state="disabled")
        for column, button in enumerate((self.check_button, self.start_button, self.stop_button)):
            button.grid(row=0, column=column, sticky="ew", padx=4)
        self.status = ctk.CTkLabel(
            footer,
            text="Выберите аккаунт и источники. Для проверки вступать автоматически не требуется.",
            font=FONTS["small"],
            text_color=COLORS["muted"],
            anchor="w",
            justify="left",
        )
        self.status.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        wrap_label(self.status)
        style_tree(self)
        apply_button_role(self.start_button, "primary")
        apply_button_role(self.stop_button, "danger")
        for button in (
            self.groups_button,
            self.help_button,
            self.add_group_button,
            self.paste_button,
            self.clear_button,
            self.check_button,
            self.save_file_button,
            self.open_folder_button,
            self.import_button,
            *self.export_buttons.values(),
        ):
            apply_button_role(button, "secondary")
        self.search_var.trace_add("write", lambda *_: self.apply_filters())
        self.mode_changed()
        self.refresh_accounts()
        self.refresh_history()
        self.restore_export_path()
        self.toggle_filters()
        self.show_view("setup")

    def card(self, title):
        frame = ctk.CTkFrame(
            self._card_parent, fg_color=COLORS["surface"], border_width=1, border_color=COLORS["border"]
        )
        frame.grid(row=self._card_row, column=0, sticky="ew", padx=12, pady=(0, 10))
        self._card_row += 1
        ctk.CTkLabel(frame, text=title, font=FONTS["subhead"]).pack(anchor="w", padx=16, pady=(12, 8))
        return frame

    def show_view(self, view):
        self._view = view
        results = view == "results"
        self.body.grid_remove()
        self.results_body.grid_remove()
        (self.results_body if results else self.body).grid()
        for widget in (self.save_bar, self.export_notice):
            widget.grid() if results else widget.grid_remove()
        self.account_bar.grid_remove() if results else self.account_bar.grid()
        for button in (self.check_button, self.start_button):
            button.grid_remove() if results else button.grid()
        for key, button in self.view_buttons.items():
            apply_button_role(button, "secondary")
            button.configure(
                fg_color=COLORS["accent_soft"] if key == view else COLORS["surface"],
                text_color=COLORS["accent"] if key == view else COLORS["text"],
            )
        self._update_stop_visibility()

    def fit_header(self, event):
        compact = event.height / self._get_widget_scaling() < 580
        if compact == self._compact_header:
            return
        self._compact_header = compact
        for widget in self._intro_labels[:-1]:
            if compact:
                widget.pack_forget()
            else:
                widget.pack(anchor="w", before=self._intro_labels[-1])

    def _update_stop_visibility(self):
        if getattr(self, "_view", "setup") == "results" and not self.busy():
            self.stop_button.grid_remove()
        else:
            self.stop_button.grid()

    def toggle_filters(self):
        if self.filters_var.get():
            self.filters_card.grid()
        else:
            self.filters_card.grid_remove()

    def toggle_details(self):
        if self.details_var.get():
            self.result_hint.pack(fill="x", padx=16, pady=(0, 8))
            self.source_report.pack(fill="x", padx=16, pady=(0, 10))
        else:
            self.result_hint.pack_forget()
            self.source_report.pack_forget()

    def busy(self):
        return bool(self.future and not self.future.done())

    def mode_changed(self, _value=None):
        mode = MODE_CHOICES[self.mode_menu.get()]
        if mode != self._input_mode:
            self._limits[self._input_mode] = self.limit_entry.get()
            self.limit_entry.delete(0, "end")
            self.limit_entry.insert(0, self._limits[mode])
            self._input_mode = mode
        if mode == "authors":
            self.author_options.pack(fill="x", padx=16, pady=(0, 8), after=self.mode_controls)
        else:
            self.author_options.pack_forget()
        self.limit_label.configure(
            text="Лимит сообщений на источник:" if mode == "authors" else "Лимит участников на группу:"
        )
        self.start_button.configure(text="Получить авторов" if mode == "authors" else "Получить участников")
        self.period_changed()

    def period_changed(self):
        state = "normal" if not self.busy() and self.period_menu.get() == "Свои даты" else "disabled"
        self.date_from.configure(state=state)
        self.date_to.configure(state=state)

    def _safe_after(self, callback):
        post_ui(self, callback)

    def set_busy(self, busy, activity="Выполняется..."):
        for button in (
            self.groups_button,
            self.check_button,
            self.start_button,
            self.account_menu,
            self.add_group_button,
            self.limit_entry,
            self.source_box,
            self.paste_button,
            self.clear_button,
            self.mode_menu,
            self.period_menu,
        ):
            button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")
        if busy:
            self.stop_button.grid()
        else:
            self._update_stop_visibility()
        self.start_button.configure(
            text=activity
            if busy
            else ("Получить авторов" if MODE_CHOICES[self.mode_menu.get()] == "authors" else "Получить участников")
        )
        if busy:
            self.date_from.configure(state="disabled")
            self.date_to.configure(state="disabled")
        else:
            self.period_changed()

    def refresh_accounts(self):
        future = self.app.run_coro(self.db.get_enabled_accounts())

        def done(result):
            def render():
                if result.cancelled() or result.exception() or self.busy():
                    return
                previous = self.account_menu.get()
                self.accounts = {f"{row['id']} · {row['phone']}": row for row in result.result()}
                self.account_menu.configure(values=list(self.accounts) or ["Добавьте аккаунт в «Аккаунтах»"])
                value = (
                    previous
                    if previous in self.accounts
                    else next(iter(self.accounts), "Добавьте аккаунт в «Аккаунтах»")
                )
                self.account_menu.set(value)
                if value != previous:
                    self.account_changed(value)

            self._safe_after(render)

        future.add_done_callback(done)

    def account_changed(self, _value):
        self._selected_account_id = self.accounts.get(_value, {}).get("id")
        self.groups = {}
        self.group_menu.configure(values=["Сначала нажмите «Мои группы»"])
        self.group_menu.set("Сначала нажмите «Мои группы»")

    async def selected_account(self):
        account_id = self._selected_account_id
        if account_id is None:
            raise ValueError("Выберите добавленный Telegram-аккаунт")
        account = await self.db.get_account(account_id)
        if not account or not account.get("is_enabled"):
            raise ValueError("Аккаунт выключен или удалён")
        if account.get("flood_until") and datetime.fromisoformat(account["flood_until"]) > datetime.now():
            raise ValueError("Аккаунт ожидает окончания паузы Telegram. Повторите позже")
        return account

    def operate(self, coroutine, callback, activity="Проверяем доступ..."):
        if (
            self.busy()
            or self.app.scheduler.is_running()
            or self.app.task_tab._starting
            or (self.app.warmup_tab.warmup_future and not self.app.warmup_tab.warmup_future.done())
        ):
            coroutine.close()
            messagebox.showwarning("Аккаунт занят", "Остановите текущую задачу или дождитесь завершения проверки")
            return
        self.set_busy(True, activity)
        self.status.configure(text="Подключение и чтение данных Telegram…")
        self.future = self.app.run_coro(coroutine)

        def done(result):
            def render():
                self.set_busy(False)
                if result.cancelled():
                    self.status.configure(text="Операция остановлена")
                elif result.exception():
                    self.status.configure(text=access_error(result.exception()), text_color=COLORS["error"])
                    self.app.notify(
                        "Парсинг требует внимания",
                        "Откройте раздел «Парсинг»: там указана причина ошибки.",
                        warning=True,
                    )
                else:
                    callback(result.result())

            self._safe_after(render)

        self.future.add_done_callback(done)

    def load_groups(self):
        async def fetch():
            account = await self.selected_account()
            manager = AccountManager(account, self.db)
            try:
                await manager.connect()
                return await asyncio.wait_for(manager.get_joined_dialogs(), 60)
            finally:
                await manager.disconnect()

        def show(rows):
            self.groups = {
                f"{row['title']} · {row['id']} · {'Канал' if row['type'] == 'channel' else 'Группа'}": row
                for row in rows
            }
            self.group_menu.configure(values=list(self.groups) or ["Групп не найдено"])
            self.group_menu.set(next(iter(self.groups), "Групп не найдено"))
            self.status.configure(
                text=f"Доступных диалогов: {len(rows)}. Выберите и нажмите «Добавить».", text_color=COLORS["muted"]
            )

        self.operate(fetch(), show)

    def add_group(self):
        row = self.groups.get(self.group_menu.get())
        if row:
            self.source_box.insert(
                "end", ("\n" if self.source_box.get("1.0", "end-1c").strip() else "") + str(row["id"])
            )

    def sources(self):
        return parse_sources(self.source_box.get("1.0", "end-1c"))

    def check_access(self):
        authors = MODE_CHOICES[self.mode_menu.get()] == "authors"
        try:
            source = self.sources()[0]
        except ValueError as exc:
            self.status.configure(text=explain_error(exc), text_color=COLORS["error"])
            return

        async def check():
            account = await self.selected_account()
            engine = ChatAuthorsEngine(self.db) if authors else ParserEngine(self.db)
            return await engine.check(account, source)

        def show(info):
            self.status.configure(
                text=f"{info['title']}: {'история доступна' if authors else 'список доступен'} этому аккаунту. "
                + (
                    "Вступление для этого запроса не потребовалось."
                    if info["membership"] == "not_joined"
                    else "Аккаунт уже состоит в чате."
                )
                + " Проверен первый указанный источник.",
                text_color=COLORS["success"],
            )

        self.operate(check(), show)

    def start(self):
        authors = MODE_CHOICES[self.mode_menu.get()] == "authors"
        try:
            sources = self.sources()
            limit = int(self.limit_entry.get())
            maximum = MAX_HISTORY_MESSAGES if authors else MAX_PER_SOURCE
            if not 1 <= limit <= maximum:
                raise ValueError(f"Лимит на источник: от 1 до {maximum}")
            since, until = (
                history_period(self.period_menu.get(), self.date_from.get(), self.date_to.get())
                if authors
                else (None, None)
            )
        except ValueError as exc:
            self.status.configure(text="Проверьте адреса и числовой лимит: " + str(exc), text_color=COLORS["error"])
            return

        async def collect():
            account = await self.selected_account()
            engine_class = ChatAuthorsEngine if authors else ParserEngine
            self.engine = engine_class(
                self.db, progress=lambda text: self._safe_after(lambda: self.status.configure(text=text))
            )
            if authors:
                return await self.engine.start(account, sources, limit, since=since, until=until)
            return await self.engine.start(account, sources, limit)

        def finished(result):
            self.status.configure(
                text=RUN_LABELS.get(result["status"], result["status"])
                + ": "
                + (result["detail"] or "Результаты сохранены. Полнота списка зависит от Telegram."),
                text_color=COLORS["muted"],
            )
            self._results_loading = True
            self.rows = []
            self.apply_filters()
            self.refresh_history(result["run_id"])
            self.show_view("results")
            self.results_body._parent_canvas.yview_moveto(0)
            self.app.notify(
                "Парсинг: " + RUN_LABELS.get(result["status"], "завершён"),
                "Откройте «Парсинг → Результаты и сохранение». Список можно сохранить на ПК.",
                warning=result["status"] in {"failed", "partial", "interrupted"},
            )

        self.operate(collect(), finished, activity="Получаем сообщения..." if authors else "Получаем участников...")

    def stop(self):
        if self.engine and self.engine._task:
            self.app.async_loop.call_soon_threadsafe(self.engine.stop)
        elif self.future and not self.future.done():
            self.future.cancel()

    def refresh_history(self, selected=None):
        future = self.app.run_coro(self.db.get_parse_runs())

        def done(result):
            def render():
                if result.cancelled() or result.exception():
                    self._results_loading = False
                    self.status.configure(
                        text="Не удалось загрузить историю результатов. Откройте раздел «Парсинг» повторно."
                    )
                    self.update_export_controls()
                    return
                self.runs = {
                    f"{row['id']} · {row['started_at']} · {'Авторы' if row['mode'] == 'authors' else 'Участники'} · {RUN_LABELS.get(row['status'], row['status'])} · {row['people_count']} чел.": row
                    for row in result.result()
                }
                self.history_menu.configure(values=list(self.runs) or ["Запусков пока нет"])
                key = next(
                    (key for key, row in self.runs.items() if row["id"] == (selected or self.selected_run)),
                    next(iter(self.runs), None),
                )
                self.history_menu.set(key or "Запусков пока нет")
                if key:
                    self.choose_run(key)
                else:
                    self._query_generation += 1
                    self.selected_run, self.rows = None, []
                    self._results_loading = False
                    self.set_result_mode("members")
                    self.result_hint.configure(
                        text="Выберите сохранённый результат. Участники и авторы сообщений — разные режимы."
                    )
                    self.source_choices = {}
                    self.source_filter.configure(values=["Все группы"])
                    self.source_filter.set("Все группы")
                    self.source_report.configure(state="normal")
                    self.source_report.delete("1.0", "end")
                    self.source_report.configure(state="disabled")
                    self.apply_filters()

            self._safe_after(render)

        future.add_done_callback(done)

    def choose_run(self, key):
        row = self.runs.get(key)
        if row is None:
            return
        self.selected_run = row["id"]
        self._results_loading = True
        self.rows = []
        self.apply_filters()
        self._query_generation += 1
        generation = self._query_generation
        future = self.app.run_coro(self.db.get_parse_results(self.selected_run))

        def done(result):
            def render():
                if generation != self._query_generation:
                    return
                self._results_loading = False
                if result.cancelled() or result.exception():
                    self.status.configure(text="Не удалось загрузить результат. Выберите запуск ещё раз.")
                    self.update_export_controls()
                    return
                self.rows, sources = result.result()
                self.set_result_mode(row["mode"])
                self.result_hint.configure(
                    text=(
                        "Авторы доступных сообщений. Роли и текущее членство не проверялись. "
                        + f"Период UTC: {row['since'] or 'вся доступная история'} — {row['until'] or 'без верхней даты'}; лимит {row['scan_limit']} сообщений/источник."
                        if row["mode"] == "authors"
                        else "Доступный список участников на момент получения. Число сообщений в этом режиме не считается."
                    )
                )
                self.source_choices = {
                    f"{item['title'] or item['reference']} · {item['source_id']}": item["source_id"]
                    for item in sources
                    if item["source_id"] is not None
                }
                self.source_filter.configure(values=["Все группы", *self.source_choices])
                self.source_filter.set("Все группы")
                self.source_report.configure(state="normal")
                self.source_report.delete("1.0", "end")
                self.source_report.insert(
                    "1.0",
                    "\n".join(
                        f"{item['title'] or item['reference']}: {RUN_LABELS.get(item['status'], item['status'])} · {'Сообщений просмотрено' if row['mode'] == 'authors' else 'API'}: {item['fetched']} · {item['detail'] or ''}"
                        for item in sources
                    )
                    or "Нет сохранённых источников",
                )
                self.source_report.configure(state="disabled")
                self.apply_filters()

            self._safe_after(render)

        future.add_done_callback(done)

    def set_result_mode(self, mode):
        if mode != self._result_mode:
            self.min_messages_menu.set("Любое число")
            self.last_post_menu.set("Любая дата сообщения")
            self.role_menu.set("Все роли")
        self._result_mode = mode
        for menu in (self.min_messages_menu, self.last_post_menu):
            menu.configure(state="normal" if mode == "authors" else "disabled")
        self.role_menu.configure(state="disabled" if mode == "authors" else "normal")

    def apply_filters(self):
        if not hasattr(self, "table"):
            return
        options = {key: var.get() for key, var in self.filter_vars.items()}
        options.update(
            query=self.search_var.get(),
            role=ROLE_CHOICES[self.role_menu.get()],
            activity=ACTIVITY_CHOICES[self.activity_menu.get()],
            source_id=getattr(self, "source_choices", {}).get(self.source_filter.get()),
        )
        matched = filter_members(self.rows, **options)
        self.filtered = unique_members(matched)
        if self._result_mode == "authors":
            self.filtered = filter_author_stats(
                self.filtered,
                min_messages=MIN_MESSAGES[self.min_messages_menu.get()],
                last_days=LAST_POST[self.last_post_menu.get()],
            )
            selected_ids = {person["user_id"] for person in self.filtered}
            matched = [row for row in matched if row["user_id"] in selected_ids]
        by = GROUP_CHOICES[self.grouping_menu.get()]
        if by != "none":

            def group_key(row):
                if by == "source":
                    return tuple(row["sources"])
                if by == "role":
                    return tuple(row["roles"])
                if by == "username":
                    return not bool(row["username"])
                if by == "premium":
                    return not bool(row["is_premium"])
                if by == "messages":
                    return -row["message_count"]
                return row["activity"]

            self.filtered.sort(key=group_key)
        groups = group_members(matched, by)
        self.group_summary.configure(
            text=" · ".join(f"{key}: {count}" for key, count in groups.items()) or "Нет совпадений. Измените фильтры."
        )
        self.page = 0
        self.render_page()
        self.update_export_controls()
        total = len({row["user_id"] for row in self.rows})
        self.segment_label.configure(
            text=(
                "Загрузка результата…"
                if self._results_loading
                else f"Найдено: {total} · После фильтров: {len(self.filtered)} · В файл попадёт весь отфильтрованный список."
            )
        )

    def render_page(self):
        pages = max(1, (len(self.filtered) + 99) // 100)
        self.page = min(self.page, pages - 1)
        self.table.delete(*self.table.get_children())
        for row in self.filtered[self.page * 100 : (self.page + 1) * 100]:
            self.table.insert(
                "",
                "end",
                values=(
                    row["user_id"],
                    "@" + row["username"] if row["username"] else "—",
                    f"{row['first_name']} {row['last_name']}".strip(),
                    ACTIVITY_LABELS.get(row["activity"], row["activity"]),
                    row["message_count"] or "—",
                    datetime.fromisoformat(row["last_message_at"]).astimezone().strftime("%Y-%m-%d %H:%M")
                    if row["last_message_at"]
                    else "—",
                    " | ".join(row["sources"]),
                ),
            )
        self.page_label.configure(text=f"{self.page + 1} / {pages} · {len(self.filtered)} уникальных людей")

    def change_page(self, delta):
        self.page = max(0, self.page + delta)
        self.render_page()

    def update_export_controls(self):
        ready = bool(self.filtered) and not self._results_loading and not self._exporting
        names_count = len({row["username"] for row in self.filtered if row["username"]})
        format_name = EXPORT_FORMATS[self.export_format.get()]
        count = names_count if format_name == "txt" else len(self.filtered)
        apply_button_role(self.save_file_button, "primary" if ready and count else "secondary")
        self.save_file_button.configure(
            state="normal" if ready and count else "disabled",
            text="Сохранение файла…" if self._exporting else f"Сохранить файл на ПК · {count}",
        )
        self.export_format.configure(state="disabled" if self._exporting else "normal")
        for name, button in self.export_buttons.items():
            button.configure(state="normal" if ready and (name != "txt" or names_count) else "disabled")

    def show_export_path(self, path):
        self.last_export_path = Path(path).resolve()
        self.export_notice.configure(
            text=f"Последний файл: {self.last_export_path.name} · Папка: {self.last_export_path.parent}"
        )
        tip(self.export_notice, str(self.last_export_path))
        self.open_folder_button.configure(state="normal")

    def restore_export_path(self):
        generation = self._export_path_generation
        future = self.app.run_coro(self.db.get_setting("parser_last_export_path", ""))

        def done(result):
            def render():
                if (
                    not result.cancelled()
                    and not result.exception()
                    and result.result()
                    and generation == self._export_path_generation
                ):
                    try:
                        self.show_export_path(result.result())
                    except (OSError, ValueError):
                        pass

            self._safe_after(render)

        future.add_done_callback(done)

    def open_saved_folder(self):
        if not self.last_export_path:
            return
        try:
            exists = self.last_export_path.is_file()
        except OSError:
            exists = False
        if not exists:
            messagebox.showwarning(
                "Файл не найден",
                "Файл мог быть перемещён или удалён. Сохраните нужный результат заново.\n\n"
                + str(self.last_export_path),
            )
            return
        try:
            os.startfile(str(self.last_export_path.parent))
        except (OSError, AttributeError):
            messagebox.showerror(
                "Не удалось открыть папку",
                "Откройте эту папку через Проводник:\n\n" + str(self.last_export_path.parent),
            )

    def export(self, format_name):
        if self._exporting or self._results_loading:
            return
        if not self.filtered:
            self.status.configure(text="Выберите сохранённый результат. Если фильтры скрыли всех людей, измените их.")
            return
        if format_name == "txt" and not any(row["username"] for row in self.filtered):
            self.status.configure(text="TXT содержит только никнеймы. В этом сегменте их нет; выберите CSV или JSON")
            return
        initial_folder = self.last_export_path.parent if self.last_export_path else Path.home() / "Downloads"
        if not initial_folder.is_dir():
            initial_folder = Path.home()
        path = filedialog.asksaveasfilename(
            title="Сохранить результат парсинга на компьютер",
            defaultextension="." + format_name,
            filetypes=[(format_name.upper(), "*." + format_name)],
            initialdir=str(initial_folder),
            initialfile=f"inviter_{self._result_mode}_run-{self.selected_run}_{datetime.now():%Y%m%d_%H%M%S}.{format_name}",
            confirmoverwrite=True,
        )
        if not path:
            return
        rows = [dict(row) for row in self.filtered]
        path = Path(path).resolve()
        self._exporting = True
        self._export_path_generation += 1
        self.update_export_controls()

        async def save():
            await asyncio.to_thread(write_parsed, path, rows, format_name)
            try:
                await self.db.set_setting("parser_last_export_path", str(path))
            except Exception:
                return False
            return True

        future = self.app.run_coro(save())
        self.export_future = future

        def done(result):
            def render():
                self._exporting = False
                self.update_export_controls()
                if result.cancelled() or result.exception():
                    self.status.configure(
                        text="Файл не сохранён. Выберите доступную папку и повторите.", text_color=COLORS["error"]
                    )
                    messagebox.showerror(
                        "Не удалось сохранить файл",
                        "Проверьте доступ к папке и свободное место. Результаты парсинга остаются в программе.\n\n"
                        + str(path),
                    )
                    return
                self.show_export_path(path)
                text = f"Файл сохранён на ПК: {path.name}. Нажмите «Открыть папку», чтобы найти его."
                if not result.result():
                    text += " Папку не удалось запомнить для следующего запуска."
                self.status.configure(text=text, text_color=COLORS["success"])

            self._safe_after(render)

        future.add_done_callback(done)

    def to_audience(self):
        if self.app.scheduler.is_running() or self.app.task_tab._starting:
            messagebox.showwarning("Задача выполняется", "Остановите задачу перед добавлением получателей")
            return
        names = []
        for row in self.filtered:
            if row["is_bot"] or row["is_deleted"] or not row["username"]:
                continue
            try:
                names.append(normalize_username(row["username"]))
            except ValueError:
                continue
        names = list(dict.fromkeys(names))
        if not names:
            self.status.configure(text="В сегменте нет подходящих никнеймов людей для аудитории")
            return
        db = self.app.db
        if not messagebox.askyesno(
            "В аудиторию",
            f"Добавить {len(names)} никнеймов в кампанию №{db.campaign_id}?\nЭто не запускает отправку. Используйте список для общения с людьми, которые ожидают ваше сообщение.",
        ):
            return
        targets_tab = self.app.targets_tab
        targets_tab.begin_audience_import()
        try:
            future = self.app.run_coro(db.bulk_insert_targets(names))
        except Exception:
            targets_tab.finish_audience_import()
            raise

        def done(result):
            def render():
                try:
                    if result.cancelled() or result.exception():
                        self.status.configure(text="Не удалось добавить аудиторию")
                        return
                    self.status.configure(
                        text=f"Кампания №{db.campaign_id}: добавлено {result.result()}, повторов в очереди {len(names) - result.result()}"
                    )
                    self.app.targets_tab.refresh_table()
                    self.app.targets_tab.refresh_stats()
                finally:
                    targets_tab.finish_audience_import()

            self._safe_after(render)

        future.add_done_callback(done)

    def delete_run(self):
        if self.selected_run is None or self.busy():
            return
        run_id = self.selected_run
        if not messagebox.askyesno(
            "Удалить результаты",
            "Удалить выбранный запуск и его участников? Уже добавленная аудитория и файлы экспорта останутся.",
        ):
            return
        future = self.app.run_coro(self.db.delete_parse_run(run_id))

        def done(result):
            def render():
                if result.cancelled() or result.exception():
                    self.status.configure(text="Результат не удалён")
                    return
                self.selected_run = None
                self.refresh_history()

            self._safe_after(render)

        future.add_done_callback(done)
