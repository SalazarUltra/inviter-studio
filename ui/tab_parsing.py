"""Participant collection with explicit access checks, saved runs and local segments."""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from telethon.errors import FloodWaitError

from core import account_manager as account_core
from core.account_manager import AccountManager
from core.chat_authors import (
    MAX_HISTORY_MESSAGES,
    PERIOD_CHOICES,
    ChatAuthorsEngine,
    filter_author_stats,
    history_period,
)
from core.database import is_flood_blocking, order_audience
from core.export import txt_lines_with_ids, write_parsed
from core.parse_compare import describe_difference, only_new, user_id_set
from core.parser import (
    ACCOUNT_ERRORS,
    ACTIVITY_LABELS,
    MAX_PER_SOURCE,
    ParserEngine,
    access_error,
    audience_members,
    filter_members,
    group_members,
    parse_sources,
    pending_sources,
    unique_members,
)
from core.user_feedback import explain_error
from core.validation import is_id_recipient, normalize_username
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
RESUMABLE_STATUSES = {"partial", "failed", "interrupted", "stopped"}
AUDIENCE_ORDERS = {
    "Как получено (свежие первыми)": "imported",
    "По активности": "activity",
    "Перемешать": "shuffle",
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
EXPORT_FORMATS = {
    "XLSX · Excel": "xlsx",
    "CSV · таблица": "csv",
    "TXT · никнеймы": "txt",
    "TXT · никнеймы и ID": "txt_ids",
    "JSON · данные": "json",
}
EXPORT_EXTENSIONS = {"txt_ids": "txt"}
NO_COMPARE = "Выберите прошлый запуск"
FILTER_KEYS = (
    "only_username",
    "exclude_bots",
    "exclude_deleted",
    "exclude_scam",
    "exclude_fake",
    "exclude_restricted",
    "premium",
    "photo",
)
FILTER_DEFAULTS = set(FILTER_KEYS) - {"premium", "photo"}
FILTER_PREFS_SETTING = "parser_filter_prefs"
FILTER_DEBOUNCE_MS = 280


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
        self._resume_pending = []
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
        self._filter_job = None
        self.multi_vars = {}  # account id -> BooleanVar of the «several accounts» checklist
        self.multi_checks = []
        self.compare_runs = {}  # menu label -> saved run offered for «new since previous run»
        self._compare_ids = None  # Telegram ids of the previous run (None until loaded)
        self._compare_loading = False
        self._compare_generation = 0
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
        self.multi_var = ctk.BooleanVar(value=False)
        self.multi_switch = ctk.CTkSwitch(
            self.account_bar,
            text="Несколько аккаунтов",
            variable=self.multi_var,
            command=self.toggle_multi_accounts,
        )
        self.multi_switch.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        tip(
            self.multi_switch,
            "Источники делятся между отмеченными аккаунтами по кругу (1-й — первому, 2-й — второму…). Если у одного "
            "аккаунта пауза Telegram (FloodWait) или ограничение, его оставшиеся источники передаются другим, а "
            "этот аккаунт ждёт окончания паузы. Лимиты Telegram не обходятся. Режим «Авторы сообщений» "
            "использует один аккаунт.",
        )
        self.multi_frame = ctk.CTkFrame(self.account_bar, fg_color=COLORS["surface"], border_width=1)
        self.multi_frame.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(6, 0))
        self.multi_frame.grid_remove()
        self.multi_hint = ctk.CTkLabel(
            self.account_bar,
            text="",
            font=FONTS["small"],
            text_color=COLORS["muted"],
            anchor="w",
            justify="left",
        )
        self.multi_hint.grid(row=3, column=0, columnspan=4, sticky="ew")
        self.multi_hint.grid_remove()
        wrap_label(self.multi_hint)
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
        self.resume_button = ctk.CTkButton(
            results, text="Продолжить", width=190, height=30, state="disabled", command=self.resume_run
        )
        self.resume_button.pack(anchor="w", padx=16, pady=(0, 8))
        tip(
            self.resume_button,
            "Дополучает группы, которые не были завершены (ошибка, остановка, закрытие программы). Уже сохранённые люди не дублируются.",
        )
        compare = ctk.CTkFrame(results, fg_color="transparent")
        self.compare_row = compare
        compare.pack(fill="x", padx=16, pady=(0, 8))
        compare.grid_columnconfigure(1, weight=1)
        self.compare_var = ctk.BooleanVar(value=False)
        self.compare_check = ctk.CTkCheckBox(
            compare,
            text="Новые с прошлого запуска",
            variable=self.compare_var,
            command=self.compare_toggled,
        )
        self.compare_check.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.compare_menu = ctk.CTkOptionMenu(
            compare, values=[NO_COMPARE], command=lambda _: self.compare_toggled(), state="disabled"
        )
        self.compare_menu.grid(row=0, column=1, sticky="ew")
        tip(
            self.compare_check,
            "Показывает, сохраняет и передаёт в аудиторию только тех людей, чьего Telegram ID не было в выбранном "
            "прошлом запуске. Сравнение идёт по ID, а не по никнейму; фильтры применяются дополнительно.",
        )
        self.compare_label = hint(results, "", padx=16)
        self.compare_label.pack_forget()
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
        self.filter_vars = {key: ctk.BooleanVar(value=key in FILTER_DEFAULTS) for key in FILTER_KEYS}
        checks = ctk.CTkFrame(filters, fg_color="transparent")
        checks.pack(fill="x", padx=16, pady=(0, 8))
        for index, (key, text) in enumerate(
            (
                ("only_username", "С никнеймом"),
                ("exclude_bots", "Без ботов"),
                ("exclude_deleted", "Без удалённых"),
                ("exclude_scam", "Без скама"),
                ("exclude_fake", "Без фейков"),
                ("exclude_restricted", "Без ограниченных"),
                ("premium", "Premium отмечен"),
                ("photo", "Фото видно"),
            )
        ):
            ctk.CTkCheckBox(checks, text=text, variable=self.filter_vars[key], command=self.filter_toggled).grid(
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
        for format_name in ("xlsx", "csv", "json", "txt", "txt_ids"):
            button = ctk.CTkButton(
                exports,
                text="Экспорт TXT+ID" if format_name == "txt_ids" else f"Экспорт {format_name.upper()}",
                width=120,
                command=lambda value=format_name: self.export(value),
            )
            button.pack(side="left", padx=(0, 8))
            self.export_buttons[format_name] = button
        tip(
            self.export_buttons["txt_ids"],
            "TXT, где у людей без никнейма вместо @имени записан Telegram ID (id:123456789). "
            "Такой файл можно импортировать в «Аудиторию».",
        )
        self.import_button = ctk.CTkButton(exports, text="В аудиторию…", width=150, command=self.to_audience)
        self.import_button.pack(side="left")
        options_row = ctk.CTkFrame(export, fg_color="transparent")
        options_row.pack(fill="x", padx=16, pady=(0, 8))
        self.include_admins_var = ctk.BooleanVar(value=False)
        self.include_admins_check = ctk.CTkCheckBox(
            options_row, text="Включая админов", variable=self.include_admins_var, width=140
        )
        self.include_admins_check.pack(side="left")
        tip(
            self.include_admins_check,
            "По умолчанию создатели и администраторы групп в аудиторию не попадают. Боты и удалённые пропускаются всегда.",
        )
        self.include_id_only_var = ctk.BooleanVar(value=False)
        self.include_id_only_check = ctk.CTkCheckBox(
            options_row, text="Включая людей без никнейма (по ID)", variable=self.include_id_only_var
        )
        self.include_id_only_check.pack(side="left", padx=(16, 0))
        tip(
            self.include_id_only_check,
            "Добавляет людей, у которых есть Telegram ID, но нет @никнейма (снимите фильтр «С никнеймом»). "
            "Пригласить такого человека получится, только если аккаунт «знает» его (общая группа или контакт); "
            "иначе Telegram не отдаст пользователя по ID, и он будет пропущен. Для личных сообщений нужен @никнейм.",
        )
        audience = ctk.CTkFrame(export, fg_color="transparent")
        audience.pack(fill="x", padx=16, pady=(0, 8))
        audience.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(audience, text="Порядок:", font=FONTS["small"]).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.audience_order_menu = ctk.CTkOptionMenu(audience, values=list(AUDIENCE_ORDERS), width=260)
        self.audience_order_menu.grid(row=0, column=1, sticky="w", pady=(0, 6))
        self.skip_invited_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            audience, text="Пропустить уже приглашённых в чат кампании", variable=self.skip_invited_var
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.exclude_chat_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            audience, text="Исключить участников чата (нужен аккаунт)", variable=self.exclude_chat_var
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.exclude_chat_entry = ctk.CTkEntry(audience, placeholder_text="@чат; пусто — чат из настроек кампании")
        self.exclude_chat_entry.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.delete_button = ctk.CTkButton(export, text="Удалить этот запуск", width=170, command=self.delete_run)
        apply_button_role(self.delete_button, "danger")
        self.delete_button.pack(anchor="w", padx=16, pady=(0, 8))
        hint(
            export,
            "Выгружается весь отфильтрованный сегмент, независимо от страницы. XLSX/CSV/JSON сохраняют ID и источники; TXT — никнеймы. Один Telegram ID — одна строка. Получение списка не означает согласия людей на сообщения.",
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
            self.resume_button,
            *self.export_buttons.values(),
        ):
            apply_button_role(button, "secondary")
        self.search_var.trace_add("write", lambda *_: self.schedule_filters())
        self.mode_changed()
        self.refresh_accounts()
        self.restore_filter_prefs()
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
            self.multi_switch,
            *self.multi_checks,
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
        self.update_resume_button(busy)

    def update_resume_button(self, busy=None):
        busy = self.busy() if busy is None else busy
        count = len(self._resume_pending)
        self.resume_button.configure(
            text=f"Продолжить ({count})" if count else "Продолжить",
            state="normal" if count and not busy else "disabled",
        )

    def resume_run(self):
        run = next((row for row in self.runs.values() if row["id"] == self.selected_run), None)
        if run is None or not self._resume_pending:
            return
        run_id = run["id"]
        multi = self.multi_mode()
        original = next((key for key, row in self.accounts.items() if row["id"] == run.get("account_id")), None)
        if original and original != self.account_menu.get() and not multi:
            answer = messagebox.askyesnocancel(
                "Продолжить получение",
                f"Исходный запуск шёл с аккаунтом {original}.\nПродолжить с ним? «Нет» — взять выбранный сейчас аккаунт.",
            )
            if answer is None:
                return
            if answer:
                self.account_menu.set(original)
                self.account_changed(original)

        async def collect():
            account = await self.selected_accounts() if multi else await self.selected_account()
            self.engine = ParserEngine(
                self.db, progress=lambda text: self._safe_after(lambda: self.status.configure(text=text))
            )
            return await self.engine.resume(account, run_id)

        self.operate(collect(), self._run_finished, activity="Продолжаем получение...", multi=multi)

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
                self.rebuild_multi_accounts()

            self._safe_after(render)

        future.add_done_callback(done)

    def multi_mode(self) -> bool:
        """True while «Несколько аккаунтов» is on and at least one account is ticked."""
        return bool(self.multi_var.get()) and any(var.get() for var in self.multi_vars.values())

    def toggle_multi_accounts(self):
        if self.multi_var.get():
            self.multi_frame.grid()
            self.multi_hint.grid()
            self.rebuild_multi_accounts()
        else:
            self.multi_frame.grid_remove()
            self.multi_hint.grid_remove()

    def rebuild_multi_accounts(self):
        """Checklist of enabled accounts; ticks survive a refresh, the first account is ticked by default."""
        previous = {account_id: var.get() for account_id, var in self.multi_vars.items()}
        for widget in self.multi_frame.winfo_children():
            widget.destroy()
        self.multi_vars, self.multi_checks = {}, []
        for index, row in enumerate(self.accounts.values()):
            var = ctk.BooleanVar(value=previous.get(row["id"], not previous and index == 0))
            check = ctk.CTkCheckBox(
                self.multi_frame, text=row["phone"], variable=var, width=150, command=self.multi_selection_changed
            )
            check.grid(row=index // 3, column=index % 3, sticky="w", padx=10, pady=4)
            self.multi_vars[row["id"]] = var
            self.multi_checks.append(check)
        self.multi_selection_changed()

    def multi_selection_changed(self):
        count = sum(1 for var in self.multi_vars.values() if var.get())
        self.multi_hint.configure(
            text=(
                f"Отмечено аккаунтов: {count}. Источники делятся между ними по кругу; при паузе Telegram у одного "
                "аккаунта его источники берут остальные."
                if count
                else "Отметьте хотя бы один аккаунт или выключите режим."
            )
        )

    async def selected_accounts(self):
        """Fresh, usable accounts ticked in the multi-account list (blocked ones are skipped with a note)."""
        chosen = [account_id for account_id, var in self.multi_vars.items() if var.get()]
        if not chosen:
            raise ValueError("Отметьте хотя бы один аккаунт")
        usable, skipped = [], []
        for account_id in chosen:
            account = await self.db.get_account(account_id)
            if not account or not account.get("is_enabled"):
                skipped.append(f"{account['phone'] if account else account_id}: выключен или удалён")
                continue
            account = await self.db.clear_expired_flood(account)
            if is_flood_blocking(account):
                skipped.append(f"{account['phone']}: пауза Telegram")
                continue
            usable.append(account)
        if not usable:
            raise ValueError("Все отмеченные аккаунты недоступны: " + "; ".join(skipped))
        if skipped:
            self.progress_note("Пропущены аккаунты — " + "; ".join(skipped))
        return usable

    def progress_note(self, text):
        self._safe_after(lambda: self.status.configure(text=text, text_color=COLORS["muted"]))

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
        account = await self.db.clear_expired_flood(account)
        if is_flood_blocking(account):
            raise ValueError("Аккаунт ожидает окончания паузы Telegram. Повторите позже")
        return account

    def account_conflict(self):
        """Why the selected parse account cannot be used right now (None if it is free).

        Tasks and warm-up on OTHER accounts do not block parsing.
        """
        if self.busy():
            return "Дождитесь завершения текущей проверки или остановите парсинг"
        return self._account_busy_reason(self._selected_account_id)

    def accounts_conflict(self):
        """Same check for every ticked account of the multi-account list (first problem wins)."""
        if self.busy():
            return "Дождитесь завершения текущей проверки или остановите парсинг"
        for account_id, var in self.multi_vars.items():
            if not var.get():
                continue
            reason = self._account_busy_reason(account_id)
            if reason:
                phone = next((row["phone"] for row in self.accounts.values() if row["id"] == account_id), account_id)
                return f"{phone}: {reason}"
        return None

    def _account_busy_reason(self, account_id):
        task_tab = self.app.task_tab
        if self.app.scheduler.is_running() or task_tab._starting:
            selected = {key for key, var in task_tab.account_vars.items() if var.get()}
            if account_id in selected:
                return "Этот аккаунт участвует в запущенной задаче. Выберите другой аккаунт или остановите задачу"
        warmup = self.app.warmup_tab
        if warmup.warmup_future and not warmup.warmup_future.done() and warmup.current_account_id == account_id:
            return "Этот аккаунт сейчас на прогреве. Выберите другой аккаунт или остановите прогрев"
        account = next((row for row in self.accounts.values() if row["id"] == account_id), None)
        if account and account_core.AccountManager.is_session_busy(account["phone"], account.get("session_name")):
            return "Этот аккаунт уже используется другой операцией. Дождитесь её завершения или выберите другой"
        return None

    def operate(self, coroutine, callback, activity="Проверяем доступ...", multi=False):
        conflict = self.accounts_conflict() if multi else self.account_conflict()
        if conflict:
            coroutine.close()
            messagebox.showwarning("Аккаунт занят", conflict)
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
            sources = self.sources()
        except ValueError as exc:
            self.status.configure(text=explain_error(exc), text_color=COLORS["error"])
            return

        async def check():
            account = await self.selected_account()
            engine = ChatAuthorsEngine(self.db) if authors else ParserEngine(self.db)
            results = []
            for index, source in enumerate(sources):
                try:
                    results.append((source, await engine.check(account, source), ""))
                except Exception as exc:
                    results.append((source, None, access_error(exc)))
                    if isinstance(exc, (FloodWaitError, *ACCOUNT_ERRORS)):
                        # The account itself is limited: the remaining sources would fail for the same reason.
                        for rest in sources[index + 1 :]:
                            results.append((rest, None, "Не проверено: проверка остановлена"))
                        break
            return results

        def show(results):
            ok = [item for item in results if item[1] is not None]
            lines = [
                f"✓ OK · {info['title']} ({'уже в чате' if info['membership'] == 'joined' else 'без вступления'})"
                if info is not None
                else f"✗ FAIL · {source} · {error}"
                for source, info, error in results
            ]
            report = f"Проверка доступа: доступно {len(ok)} из {len(results)}\n" + "\n".join(lines)
            self.source_report.configure(state="normal")
            self.source_report.delete("1.0", "end")
            self.source_report.insert("1.0", report)
            self.source_report.configure(state="disabled")
            self.details_var.set(True)
            self.toggle_details()
            what = "история доступна" if authors else "список доступен"
            if len(ok) == len(results):
                text = (
                    f"{ok[0][1]['title']}: {what} этому аккаунту. "
                    if len(results) == 1
                    else f"Проверено источников: {len(results)}. Везде {what} этому аккаунту. "
                ) + "Вступление для проверки не потребовалось."
            else:
                failed = [line for line in lines if line.startswith("✗")]
                text = (
                    f"Проверено источников: {len(results)} · {what}: {len(ok)} · недоступно: {len(failed)}.\n"
                    + "\n".join(failed[:3])
                    + ("\n…" if len(failed) > 3 else "")
                )
            if len(results) > 1 or len(ok) != len(results):
                text += "\nПодробный отчёт: «Результаты и сохранение» → «Отчёт об источниках»."
            color = COLORS["success"] if len(ok) == len(results) else COLORS["error"]
            self.status.configure(text=text, text_color=color)

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

        multi = self.multi_mode()

        async def collect():
            account = await self.selected_accounts() if multi else await self.selected_account()
            engine_class = ChatAuthorsEngine if authors else ParserEngine
            self.engine = engine_class(
                self.db, progress=lambda text: self._safe_after(lambda: self.status.configure(text=text))
            )
            if authors:
                return await self.engine.start(account, sources, limit, since=since, until=until)
            return await self.engine.start(account, sources, limit)

        self.operate(
            collect(),
            self._run_finished,
            activity="Получаем сообщения..." if authors else "Получаем участников...",
            multi=multi,
        )

    def _run_finished(self, result):
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
                    self._resume_pending = []
                    self.update_resume_button()
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
                    self.update_compare_options()
                    self.apply_filters()

            self._safe_after(render)

        future.add_done_callback(done)

    def update_compare_options(self):
        """Offer the other saved runs for «Новые с прошлого запуска»; default = the nearest older run."""
        options = {
            label: row
            for label, row in self.runs.items()
            if row["id"] != self.selected_run and row["status"] != "running"
        }
        self.compare_runs = options
        enabled = bool(options) and self.selected_run is not None
        self.compare_menu.configure(values=list(options) or [NO_COMPARE], state="normal" if enabled else "disabled")
        self.compare_check.configure(state="normal" if enabled else "disabled")
        if self.compare_menu.get() not in options:
            older = [label for label, row in options.items() if self.selected_run and row["id"] < self.selected_run]
            self.compare_menu.set(older[0] if older else next(iter(options), NO_COMPARE))
        if not enabled:
            self.compare_var.set(False)
        self.compare_toggled()

    def compare_toggled(self):
        """(Re)load the previous run's Telegram ids; every view then shows only people absent from it."""
        self._compare_generation += 1
        generation = self._compare_generation
        previous = self.compare_runs.get(self.compare_menu.get())
        self._compare_ids = None
        if not self.compare_var.get() or previous is None:
            self._compare_loading = False
            if previous is None:
                self.compare_var.set(False)
            self.compare_label.pack_forget()
            self.apply_filters()
            return
        self._compare_loading = True
        self.compare_label.configure(text="Загрузка прошлого запуска для сравнения…")
        self.compare_label.pack(fill="x", padx=16, pady=(0, 10), after=self.compare_row)
        self.apply_filters()
        future = self.app.run_coro(self.db.get_parse_user_ids(previous["id"]))

        def done(result):
            def render():
                if generation != self._compare_generation:
                    return
                self._compare_loading = False
                if result.cancelled() or result.exception():
                    self.compare_var.set(False)
                    self.compare_label.pack_forget()
                    self.status.configure(text="Не удалось загрузить прошлый запуск для сравнения.")
                else:
                    self._compare_ids = result.result()
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
        self._resume_pending = []
        self.update_resume_button()
        self.update_compare_options()
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
                self._resume_pending = (
                    pending_sources(row, sources)
                    if row["mode"] == "members" and row["status"] in RESUMABLE_STATUSES
                    else []
                )
                self.update_resume_button()
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

    def destroy(self):
        if self._filter_job is not None:
            try:
                self.after_cancel(self._filter_job)
            except Exception:
                pass
            self._filter_job = None
        super().destroy()

    def schedule_filters(self):
        """Debounce typing in the search box: recompute once after the user pauses."""
        if self._filter_job is not None:
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(FILTER_DEBOUNCE_MS, self.apply_filters)

    def filter_toggled(self):
        self.apply_filters()
        self.save_filter_prefs()

    def save_filter_prefs(self):
        prefs = json.dumps({key: bool(var.get()) for key, var in self.filter_vars.items()})
        future = self.app.run_coro(self.db.set_setting(FILTER_PREFS_SETTING, prefs))
        future.add_done_callback(lambda result: result.cancelled() or result.exception())

    def restore_filter_prefs(self):
        future = self.app.run_coro(self.db.get_setting(FILTER_PREFS_SETTING, ""))

        def done(result):
            def render():
                if result.cancelled() or result.exception() or not result.result():
                    return
                try:
                    prefs = json.loads(result.result())
                except (TypeError, ValueError):
                    return
                if not isinstance(prefs, dict):
                    return
                for key, var in self.filter_vars.items():
                    if isinstance(prefs.get(key), bool):
                        var.set(prefs[key])
                self.apply_filters()

            self._safe_after(render)

        future.add_done_callback(done)

    def apply_filters(self):
        if not hasattr(self, "table"):
            return
        if self._filter_job is not None:
            self.after_cancel(self._filter_job)
            self._filter_job = None
        options = {key: var.get() for key, var in self.filter_vars.items()}
        options.update(
            query=self.search_var.get(),
            role=ROLE_CHOICES[self.role_menu.get()],
            activity=ACTIVITY_CHOICES[self.activity_menu.get()],
            source_id=getattr(self, "source_choices", {}).get(self.source_filter.get()),
        )
        matched = filter_members(self.rows, **options)
        comparing = bool(getattr(self, "compare_var", None) and self.compare_var.get())
        if comparing:
            # «Новые с прошлого запуска»: only people whose id is absent from the chosen earlier run.
            # While that run loads nothing is shown, so a partial list can never be exported.
            matched = only_new(matched, self._compare_ids) if self._compare_ids is not None else []
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
                + (" Показаны только новые с прошлого запуска." if comparing and not self._compare_loading else "")
            )
        )
        if comparing and self._compare_ids is not None and not self._compare_loading:
            self.compare_label.configure(text=describe_difference(user_id_set(self.rows), self._compare_ids))
            self.compare_label.pack(fill="x", padx=16, pady=(0, 10), after=self.compare_row)

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
        ready = bool(self.filtered) and not self._results_loading and not self._exporting and not self._compare_loading
        names_count = len({row["username"] for row in self.filtered if row["username"]})
        lines_count = len(txt_lines_with_ids(self.filtered))
        format_name = EXPORT_FORMATS[self.export_format.get()]
        count = {"txt": names_count, "txt_ids": lines_count}.get(format_name, len(self.filtered))
        apply_button_role(self.save_file_button, "primary" if ready and count else "secondary")
        self.save_file_button.configure(
            state="normal" if ready and count else "disabled",
            text="Сохранение файла…" if self._exporting else f"Сохранить файл на ПК · {count}",
        )
        self.export_format.configure(state="disabled" if self._exporting else "normal")
        for name, button in self.export_buttons.items():
            needed = {"txt": names_count, "txt_ids": lines_count}.get(name, 1)
            button.configure(state="normal" if ready and needed else "disabled")

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
        if self._exporting or self._results_loading or self._compare_loading:
            return
        if not self.filtered:
            self.status.configure(text="Выберите сохранённый результат. Если фильтры скрыли всех людей, измените их.")
            return
        if format_name == "txt" and not any(row["username"] for row in self.filtered):
            self.status.configure(
                text="TXT содержит только никнеймы. В этом сегменте их нет; выберите TXT с ID, XLSX, CSV или JSON"
            )
            return
        if format_name == "txt_ids" and not txt_lines_with_ids(self.filtered):
            self.status.configure(text="В этом сегменте нет ни никнеймов, ни Telegram ID")
            return
        extension = EXPORT_EXTENSIONS.get(format_name, format_name)
        initial_folder = self.last_export_path.parent if self.last_export_path else Path.home() / "Downloads"
        if not initial_folder.is_dir():
            initial_folder = Path.home()
        path = filedialog.asksaveasfilename(
            title="Сохранить результат парсинга на компьютер",
            defaultextension="." + extension,
            filetypes=[(extension.upper(), "*." + extension)],
            initialdir=str(initial_folder),
            initialfile=f"inviter_{self._result_mode}_run-{self.selected_run}_{datetime.now():%Y%m%d_%H%M%S}.{extension}",
            confirmoverwrite=True,
        )
        if not path:
            return
        rows = list(self.filtered)  # filtered is rebuilt, never mutated: a shallow snapshot is enough
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
        if self._compare_loading or self._results_loading:
            self.status.configure(text="Дождитесь загрузки результата и сравнения с прошлым запуском")
            return
        include_id_only = bool(self.include_id_only_var.get())
        members, skipped = audience_members(
            self.filtered, include_admins=bool(self.include_admins_var.get()), include_id_only=include_id_only
        )
        if not members:
            self.status.configure(
                text="В сегменте нет подходящих получателей для аудитории"
                + (
                    " (люди без никнейма не добавлены: включите «Включая людей без никнейма (по ID)»)"
                    if skipped["no_username"] and not include_id_only
                    else ""
                )
                + (f" (админов пропущено: {skipped['admins']})" if skipped["admins"] else "")
            )
            return
        db = self.app.db
        order = AUDIENCE_ORDERS[self.audience_order_menu.get()]
        skip_invited = bool(self.skip_invited_var.get())
        exclude = bool(self.exclude_chat_var.get())
        entry_chat = self.exclude_chat_entry.get().strip()
        if exclude:
            conflict = self.account_conflict()
            if conflict:
                messagebox.showwarning("Аккаунт занят", conflict)
                return
        ordered = order_audience(members, order)
        admins_note = f"\nСоздатели и админы пропущены: {skipped['admins']}." if skipped["admins"] else ""
        id_count = sum(1 for item in ordered if is_id_recipient(item.get("username")))
        id_note = (
            f"\nИз них без никнейма (по Telegram ID): {id_count}. Они подходят только для приглашений в чат, "
            "и аккаунт сможет пригласить такого человека, только если Telegram уже «знает» его "
            "(общий чат, контакт или недавняя переписка)."
            if id_count
            else ""
        )
        extras = (
            "\nУчастники чата будут исключены: аккаунту потребуется прочитать список чата." if exclude else ""
        ) + ("\nУже приглашённые в чат кампании будут пропущены." if skip_invited else "")
        if not messagebox.askyesno(
            "В аудиторию",
            f"Добавить {len(ordered)} получателей в кампанию №{db.campaign_id}?{id_note}{admins_note}{extras}\nЭто не запускает отправку. Используйте список для общения с людьми, которые ожидают ваше сообщение.",
        ):
            return
        targets_tab = self.app.targets_tab

        async def work():
            chat = entry_chat or (await db.get_setting("target_chat", "") or "")
            found = None
            if exclude:
                if not chat:
                    raise ValueError("Укажите чат для исключения участников или задайте чат в настройках кампании")
                reference = chat.strip().lstrip("@")
                reference = int(reference) if reference.lstrip("-").isdigit() else normalize_username(reference)
                account = await self.selected_account()
                self.engine = ParserEngine(
                    db, progress=lambda text: self._safe_after(lambda: self.status.configure(text=text))
                )
                found = await self.engine.collect_member_keys(account, reference)
            stats = await db.import_audience(
                ordered,
                preserve_order=True,
                chat=chat,
                skip_invited=skip_invited,
                exclude_user_ids=found["user_ids"] if found else (),
                exclude_usernames=found["usernames"] if found else (),
            )
            if stats["inserted"]:
                # Parsed order is meaningful (recent first), so the engine must not reshuffle it.
                await db.set_setting("invite_order", "shuffle" if order == "shuffle" else "as_is")
            return stats, found, order

        targets_tab.begin_audience_import()
        if exclude:
            self.set_busy(True, "Сверяем чат...")
            self.status.configure(text="Читаем участников чата, чтобы не приглашать тех, кто уже внутри…")
        try:
            future = self.app.run_coro(work())
        except Exception:
            if exclude:
                self.set_busy(False)
            targets_tab.finish_audience_import()
            raise
        if exclude:
            self.future = future

        def done(result):
            def render():
                try:
                    if exclude:
                        self.set_busy(False)
                    if result.cancelled():
                        self.status.configure(text="Не удалось добавить аудиторию")
                        return
                    if result.exception():
                        self.status.configure(
                            text="Не удалось добавить аудиторию: " + access_error(result.exception()),
                            text_color=COLORS["error"],
                        )
                        return
                    stats, found, used_order = result.result()
                    text = f"Кампания №{db.campaign_id}: добавлено {stats['inserted']}, повторов в очереди {stats['duplicates']}"
                    if stats.get("id_only"):
                        text += f" (из них по ID без никнейма: {stats['id_only']})"
                    if stats.get("no_username"):
                        text += (
                            f", пропущено без никнейма (в кампании сообщений нужен @никнейм): {stats['no_username']}"
                        )
                    if stats["skipped_invited"]:
                        text += f", уже приглашены ранее: {stats['skipped_invited']}"
                    if found is not None:
                        text += f", уже в чате «{found['title']}»: {stats['excluded']}"
                        if found["partial"]:
                            text += " (список чата получен не полностью)"
                    if stats["inserted"]:
                        text += ". Порядок приглашений: " + ("случайный" if used_order == "shuffle" else "как в списке")
                        self.app.task_tab.set_invite_order("shuffle" if used_order == "shuffle" else "as_is")
                    self.status.configure(text=text, text_color=COLORS["muted"])
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
