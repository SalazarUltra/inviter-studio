from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from concurrent.futures import Future
from tkinter import messagebox

import customtkinter as ctk

from core.database import Database
from core.scheduler import TaskScheduler
from core.support import SUPPORT_SEEN_KEY
from core.user_feedback import explain_error
from core.version import VERSION
from ui.components import NavTooltip
from ui.dispatch import UIDispatcher, post_ui
from ui.entry_utils import install_clipboard_shortcuts
from ui.help_dialog import HelpDialog
from ui.hints import tip
from ui.icons import icon
from ui.notifications import WindowsNotifications
from ui.support_dialog import SupportDialog
from ui.tab_accounts import AccountsTab
from ui.tab_ai_settings import TabAISettings
from ui.tab_dashboard import TabDashboard
from ui.tab_logs import LogsTab
from ui.tab_parsing import ParsingTab
from ui.tab_targets import TargetsTab
from ui.tab_task import TaskTab
from ui.tab_warmup import TabWarmup
from ui.theme import APP_PROFILES, COLORS, FONTS, apply_button_role, init_fonts, style_tree
from ui.update_dialog import UpdateDialog


class App(ctk.CTk):
    def __init__(self, async_loop: asyncio.AbstractEventLoop, *, show_startup_support=True):
        super().__init__()
        install_clipboard_shortcuts(self)
        self.ui_dispatcher = UIDispatcher()
        self._closing = False
        self.async_loop = async_loop
        self.db = Database()
        # Prevent a task from starting while a recipient import is still in flight.
        self._audience_imports = 0
        self._targets_refresh_job = None
        self._accounts_refresh_job = None
        self._rail_expanded = None
        self._help_dialog = None
        self._support_dialog = None
        self._support_intro_handled = False
        self._support_seen_future = None
        self._support_intro_job = None
        self.notifications = WindowsNotifications(self)
        self._notification_save_future = None
        self._notification_preference_changed = False
        self._account_notice_times = {}
        self.title(f"Inviter Studio {VERSION}")
        self.geometry("1280x800")
        self.minsize(900, 600)
        self.configure(fg_color=COLORS["bg"])
        init_fonts()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_shell()
        self.scheduler = TaskScheduler(
            loop=self.async_loop,
            db=self.db,
            log_callback=self.add_log,
            progress_callback=self.on_progress,
            status_callback=self.on_status,
            fatal_callback=self.on_fatal,
            complete_callback=self.on_complete,
        )
        self.logs_tab = LogsTab(self.page_frames["logs"], db=self.db, run_coro=self.run_coro)
        self.dashboard_tab = TabDashboard(self.page_frames["dashboard"], app=self)
        self.task_tab = TaskTab(
            self.page_frames["task"],
            db=self.db,
            run_coro=self.run_coro,
            scheduler=self.scheduler,
            log_callback=self.add_log,
        )
        self.accounts_tab = AccountsTab(
            self.page_frames["accounts"],
            db=self.db,
            run_coro=self.run_coro,
            log_callback=self.add_log,
            refresh_task_accounts=self.task_tab.refresh_accounts,
        )
        self.targets_tab = TargetsTab(
            self.page_frames["targets"], db=self.db, run_coro=self.run_coro, log_callback=self.add_log
        )
        self.warmup_tab = TabWarmup(
            self.page_frames["warmup"], db=self.db, run_coro=self.run_coro, async_loop=self.async_loop
        )
        self.ai_tab = TabAISettings(self.page_frames["ai"], db=self.db, run_coro=self.run_coro)
        self.parsing_tab = ParsingTab(self.page_frames["parsing"], app=self)
        for tab in (
            self.logs_tab,
            self.dashboard_tab,
            self.task_tab,
            self.accounts_tab,
            self.targets_tab,
            self.warmup_tab,
            self.ai_tab,
            self.parsing_tab,
        ):
            tab.pack(fill="both", expand=True, padx=12, pady=8)
        style_tree(self)
        self._load_profile_setting()
        self._load_density_setting()
        self._load_notification_setting()
        self._load_last_campaign()
        self._switch_tab("dashboard")
        self._resize_shell()
        self.bind("<Configure>", self._on_shell_configure, add="+")
        self.bind("<F1>", lambda _event: self.show_help())
        for index, key in enumerate(self.page_frames, 1):
            self.bind(f"<Control-Key-{index}>", lambda event, tab=key: self._switch_tab(tab))
        self._drain_ui()
        self._poll_ui()
        self._update_installer = None
        if show_startup_support:
            self._support_intro_job = self.after(1800, self._load_support_intro)
        self._maybe_check_updates()

    def _build_shell(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.sidebar = ctk.CTkFrame(self, width=208, corner_radius=0, fg_color="#141619")
        self.sidebar.grid(row=0, column=0, sticky="ns")
        self.sidebar.pack_propagate(False)
        brand = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand.pack(fill="x", padx=14, pady=(14, 16))
        self.brand_mark = ctk.CTkLabel(
            brand,
            text="I",
            width=40,
            height=40,
            corner_radius=12,
            fg_color=COLORS["accent"],
            text_color=COLORS["bg"],
            font=FONTS["title"],
        )
        self.brand_mark.pack(side="left")
        self.brand_text = ctk.CTkLabel(brand, text="Inviter Studio", font=FONTS["brand"])
        self.brand_text.pack(side="left", padx=(10, 0))
        self.nav_heading = ctk.CTkLabel(
            self.sidebar, text="ИНСТРУМЕНТЫ", font=FONTS["label"], text_color=COLORS["subtle"], anchor="w"
        )
        self.nav_heading.pack(fill="x", padx=22, pady=(0, 10))
        self.nav_buttons = {}
        self.nav_lines = {}
        self.tab_name_to_key = {}
        self.nav_titles = {
            "dashboard": "Обзор",
            "accounts": "Аккаунты",
            "targets": "Аудитория",
            "task": "Задача",
            "warmup": "Прогрев",
            "ai": "AI-студия",
            "logs": "Журнал",
            "parsing": "Парсинг",
        }
        self.current_tab_key = "dashboard"
        for key, title in self.nav_titles.items():
            self.tab_name_to_key.update({title: key, title.lower(): key, key: key})
            button = ctk.CTkButton(
                self.sidebar,
                text=title,
                image=icon(key),
                compound="left",
                anchor="w",
                height=42,
                width=164,
                corner_radius=10,
                border_width=0,
                fg_color="transparent",
                hover_color=COLORS["elevated"],
                text_color=COLORS["muted"],
                font=FONTS["body"],
                command=lambda tab=key: self._switch_tab(tab),
            )
            button._studio_styled = True
            button.pack(fill="x", padx=12, pady=2)
            self.nav_buttons[key] = button
            NavTooltip(button, title)
        self.tab_name_to_key.update({"Главная": "dashboard", "Список": "targets", "Логи": "logs", "AI": "ai"})
        self.rail_footer = ctk.CTkFrame(self.sidebar, fg_color=COLORS["surface"], corner_radius=12)
        self.rail_footer.pack(side="bottom", fill="x", padx=12, pady=12)
        ctk.CTkLabel(
            self.rail_footer,
            text="TG",
            width=36,
            height=36,
            corner_radius=10,
            fg_color=COLORS["elevated"],
            font=FONTS["label"],
            text_color=COLORS["accent"],
        ).pack(side="left", padx=8, pady=8)
        self.footer_brand = ctk.CTkLabel(
            self.rail_footer, text="Помощь · F1", font=FONTS["small"], text_color=COLORS["muted"], cursor="hand2"
        )
        self.footer_brand.pack(side="left")
        self.footer_brand.bind("<Button-1>", lambda _event: self.show_help("start"))
        self.support_button = ctk.CTkButton(
            self.sidebar,
            text="Поддержать проект",
            image=icon("support", COLORS["accent"]),
            height=36,
            width=164,
            command=self.show_support,
            anchor="w",
        )
        apply_button_role(self.support_button, "secondary")
        self.support_button.pack(side="bottom", fill="x", padx=12, pady=(6, 0))
        NavTooltip(self.support_button, "Поддержать проект · адрес и сети для доната")
        workspace = ctk.CTkFrame(self, fg_color=COLORS["bg"], corner_radius=0)
        workspace.grid(row=0, column=1, sticky="nsew")
        workspace.grid_columnconfigure(0, weight=1)
        workspace.grid_rowconfigure(1, weight=1)
        header = ctk.CTkFrame(workspace, fg_color=COLORS["bg"], corner_radius=0, height=72)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        breadcrumb = ctk.CTkFrame(header, fg_color="transparent")
        breadcrumb.grid(row=0, column=0, sticky="w", padx=26, pady=18)
        self.breadcrumb_label = ctk.CTkLabel(
            breadcrumb, text="Рабочее пространство  /  Обзор", font=FONTS["small"], text_color=COLORS["muted"]
        )
        self.breadcrumb_label.pack(anchor="w")
        settings = ctk.CTkFrame(header, fg_color="transparent")
        settings.grid(row=0, column=1, sticky="e", padx=20, pady=16)
        self.ui_profile_var = ctk.StringVar(value="Кастом")
        self.ui_density_var = ctk.StringVar(value="Обычный")
        self._active_profile_name = "Кастом"
        self._active_density_mode = "normal"
        self.profile_menu = ctk.CTkOptionMenu(
            settings,
            values=list(APP_PROFILES),
            variable=self.ui_profile_var,
            width=112,
            height=32,
            font=FONTS["small"],
            command=self._on_profile_change,
        )
        self.profile_menu.pack(side="left", padx=(0, 8))
        NavTooltip(
            self.profile_menu,
            "Профиль меняет настройки AI-студии: тон ответов и ограничения диалогов. "
            "«Кастом» оставляет ваши текущие настройки без изменений.",
        )
        self.density_menu = ctk.CTkOptionMenu(
            settings,
            values=["Обычный", "Компактный"],
            variable=self.ui_density_var,
            width=116,
            height=32,
            font=FONTS["small"],
            command=self._on_density_change,
        )
        self.density_menu.pack(side="left", padx=(0, 12))
        NavTooltip(
            self.density_menu,
            "Плотность меняет отступы и высоту строк в таблицах разделов «Обзор», "
            "«Аккаунты» и «Аудитория». Настройка сохраняется для следующего запуска.",
        )
        self.update_button = ctk.CTkButton(
            settings, text="Обновления", width=96, height=32, font=FONTS["small"], command=self.check_updates_now
        )
        apply_button_role(self.update_button, "secondary")
        self.update_button.pack(side="left", padx=(0, 8))
        tip(
            self.update_button,
            "Проверить обновления Inviter Studio на GitHub. Установка запускается только с вашего согласия; "
            "файл проверяется по SHA-256. Установленная программа проверяет обновления и автоматически при запуске.",
        )
        self.help_button = ctk.CTkButton(settings, text="?", width=32, height=32, command=self.show_help)
        apply_button_role(self.help_button, "secondary")
        self.help_button.pack(side="left", padx=(0, 10))
        tip(self.help_button, "Помощь по текущему разделу и пошаговый запуск. Можно открыть клавишей F1.")
        new_task = ctk.CTkButton(
            settings,
            text="Настроить задачу",
            image=icon("arrow", COLORS["bg"], 16),
            compound="right",
            width=164,
            height=34,
            command=lambda: self._switch_tab("task"),
        )
        apply_button_role(new_task, "primary")
        new_task.pack(side="left")
        tip(new_task, "Открыть настройки выбранной кампании. Отправка начнётся только после «Запустить задачу».")
        self.content = ctk.CTkFrame(workspace, fg_color=COLORS["bg"], corner_radius=0)
        self.content.grid(row=1, column=0, sticky="nsew")
        self.page_frames = {
            key: ctk.CTkFrame(self.content, fg_color=COLORS["bg"], corner_radius=0) for key in self.nav_titles
        }
        status_bar = ctk.CTkFrame(workspace, fg_color=COLORS["bg"], corner_radius=0)
        status_bar.grid(row=2, column=0, sticky="ew", padx=26, pady=(4, 12))
        self.status_dot = ctk.CTkLabel(
            status_bar, text="●", text_color=COLORS["accent"], width=16, font=("Segoe UI", 10)
        )
        self.status_dot.pack(side="left")
        self.status_text = ctk.CTkLabel(
            status_bar, text="Готов к работе", font=FONTS["small"], text_color=COLORS["muted"], anchor="w"
        )
        self.status_text.pack(side="left", fill="x", expand=True, padx=6)
        self.status_text.bind(
            "<Configure>", lambda event: self.status_text.configure(wraplength=max(200, event.width - 16))
        )
        self.notifications_var = ctk.BooleanVar(value=True)
        self.notifications_switch = ctk.CTkSwitch(
            status_bar,
            text="Уведомления Windows",
            variable=self.notifications_var,
            command=self._toggle_notifications,
            font=FONTS["small"],
        )
        self.notifications_switch.pack(side="right")
        tip(
            self.notifications_switch,
            "Уведомлять о завершении задачи и парсинга. Без текстов переписки и номеров аккаунтов. Windows может скрыть уведомление в режиме «Не беспокоить». Выбор сохраняется.",
        )

    def _maybe_check_updates(self) -> None:
        """Silent GitHub check in the installed app only; source runs and smoke tests stay offline."""
        if not getattr(sys, "frozen", False) or self._closing or "--smoke-test" in sys.argv:
            return
        self._check_updates(silent=True, respect_opt_out=True)

    def check_updates_now(self) -> None:
        """Manual check from the header button; explains itself when unavailable."""
        if getattr(self, "_update_checking", False):
            return
        self._check_updates(silent=False, respect_opt_out=False)

    def _check_updates(self, *, silent: bool, respect_opt_out: bool) -> None:
        self._update_checking = True

        def busy():
            if hasattr(self, "update_button"):
                self.update_button.configure(state="disabled", text="Проверяем…")

        self._safe_after(busy)

        async def check():
            from core import updates

            if respect_opt_out and await self.db.get_setting("update_check_enabled", "1") == "0":
                return None
            return await updates.fetch_latest_release()

        future = self.run_coro(check())

        def done(result):
            def show():
                self._update_checking = False
                if hasattr(self, "update_button"):
                    self.update_button.configure(state="normal", text="Обновления")
                if self._closing:
                    return
                if result.cancelled():
                    return
                if result.exception():
                    # Silence auto-checks: only the manual button explains failures.
                    if not silent:
                        messagebox.showwarning("Обновления", explain_error(result.exception()))
                    return
                release = result.result()
                if release:
                    UpdateDialog(self, release)
                elif not silent:
                    self.set_status("У вас последняя версия Inviter Studio.", COLORS["success"])
                    self.notify(
                        "Обновления",
                        "У вас последняя версия Inviter Studio.",
                    )

            self._safe_after(show)

        future.add_done_callback(done)

    def start_installer_after_close(self, installer_path: str) -> None:
        """Run the verified installer right after the Tk window is destroyed."""
        self._update_installer = installer_path
        self._on_close()

    def show_help(self, topic=None):
        if self._closing:
            return None
        selected = topic or ("start" if self.current_tab_key == "dashboard" else self.current_tab_key)
        if self._help_dialog is not None and self._help_dialog.winfo_exists():
            self._help_dialog.show_topic(selected)
            self._help_dialog.lift()
        else:
            self._help_dialog = HelpDialog(self, selected)
        self._help_dialog.lift()
        self._help_dialog.focus_set()
        return self._help_dialog

    def _load_support_intro(self):
        self._support_intro_job = None
        if self._closing or self._support_intro_handled:
            return
        future = self.run_coro(self.db.get_setting(SUPPORT_SEEN_KEY, "0"))

        def loaded(result):
            if result.cancelled() or result.exception():
                return
            if result.result() != "1":
                self._safe_after(self._show_support_intro)

        future.add_done_callback(loaded)

    def _show_support_intro(self):
        if self._closing or self._support_intro_handled:
            return
        if self.state() == "iconic" or self.grab_current() is not None:
            self._support_intro_job = self.after(1000, self._show_support_intro)
            return
        self.show_support()

    def show_support(self):
        if self._closing:
            return None
        if self._support_dialog is not None and self._support_dialog.winfo_exists():
            self._support_dialog.lift()
            return self._support_dialog
        self._support_dialog = SupportDialog(self)
        self._support_intro_handled = True
        if self._support_intro_job is not None:
            self.after_cancel(self._support_intro_job)
            self._support_intro_job = None
        self._support_seen_future = self.run_coro(self.db.set_setting(SUPPORT_SEEN_KEY, "1"))
        return self._support_dialog

    def _on_shell_configure(self, event):
        if event.widget is self:
            self._resize_shell()

    def _resize_shell(self):
        expanded = self.winfo_width() / self._get_window_scaling() >= 1180
        if self._rail_expanded == expanded:
            return
        self._rail_expanded = expanded
        self.support_button.configure(
            text="Поддержать проект" if expanded else "",
            anchor="w" if expanded else "center",
            width=164 if expanded else 48,
        )
        self.sidebar.configure(width=208 if expanded else 72)
        if expanded:
            self.brand_text.pack(side="left", padx=(10, 0))
            self.nav_heading.pack(fill="x", padx=22, pady=(0, 10), before=self.nav_buttons["dashboard"])
            self.footer_brand.pack(side="left")
        else:
            self.brand_text.pack_forget()
            self.nav_heading.pack_forget()
            self.footer_brand.pack_forget()
        for key, button in self.nav_buttons.items():
            button.configure(
                text=self.nav_titles[key] if expanded else "",
                anchor="w" if expanded else "center",
                width=164 if expanded else 48,
            )
        self.breadcrumb_label.configure(
            text=f"{'Рабочее пространство  /  ' if expanded else ''}{self.nav_titles[self.current_tab_key]}"
        )

    def run_coro(self, coro) -> Future:
        if self._closing:
            coro.close()
            future = Future()
            future.cancel()
            return future
        future = asyncio.run_coroutine_threadsafe(coro, self.async_loop)

        def observe(result):
            if not result.cancelled() and result.exception() is not None:
                error = explain_error(result.exception())
                self.ui_dispatcher.post(lambda: self.add_log("ERROR", error))

        future.add_done_callback(observe)
        return future

    def _safe_after(self, callback) -> None:
        post_ui(self, callback)

    def add_log(self, level: str, text: str) -> None:
        self._safe_after(lambda: self.logs_tab.add_log_line(level, text))
        level_color = {
            "SUCCESS": COLORS["success"],
            "ERROR": COLORS["error"],
            "FLOOD": COLORS["warning"],
            "WARN": COLORS["warning"],
            "INFO": COLORS["muted"],
            "SKIP": COLORS["muted"],
        }.get(level, COLORS["muted"])
        self._safe_after(lambda: self.set_status(text, level_color))
        lowered = str(text).casefold()
        if level in {"FLOOD", "WARN", "ERROR"}:
            if "telegram просит подождать" in lowered or "telegram требует ожидание" in lowered:
                self._notify_account_state(
                    "flood",
                    "Telegram попросил сделать паузу. Проверьте журнал и состояние аккаунта перед продолжением.",
                )
            elif any(
                phrase in lowered
                for phrase in ("войдите в аккаунт заново", "подключите аккаунт заново", "отозвал сохранённый вход")
            ):
                self._notify_account_state(
                    "login",
                    "Сохранённый вход Telegram больше не действует. Откройте раздел «Аккаунты» и подключите аккаунт заново.",
                )
            elif (
                "telegram отклонил действие с этого аккаунта" in lowered
                or "telegram отклонил отправку с этого аккаунта" in lowered
            ):
                self._notify_account_state(
                    "restricted",
                    "Telegram отклонил действие аккаунта. Проверьте состояние аккаунта в Telegram и журнал программы.",
                )

    def _notify_account_state(self, kind: str, text: str) -> None:
        now = time.monotonic()
        previous = self._account_notice_times.get(kind)
        if previous is not None and now - previous < 90:
            return
        self._account_notice_times[kind] = now
        self._safe_after(lambda: self.notify("Telegram: нужна проверка аккаунта", text, warning=True))

    def on_progress(self, added: int, total: int) -> None:
        self._safe_after(lambda: self.task_tab.set_progress(added, total))
        if self.current_tab_key in {"targets", "dashboard"}:
            self._safe_after(self.targets_tab.refresh_stats)
        if self.current_tab_key == "targets":
            self._safe_after(self._schedule_targets_refresh)
        if self.current_tab_key == "accounts":
            self._safe_after(self._schedule_accounts_refresh)

    def _schedule_targets_refresh(self) -> None:
        if self._targets_refresh_job:
            try:
                self.after_cancel(self._targets_refresh_job)
            except Exception:
                pass
        self._targets_refresh_job = self.after(800, self.targets_tab.refresh_table)

    def _schedule_accounts_refresh(self) -> None:
        if self._accounts_refresh_job:
            try:
                self.after_cancel(self._accounts_refresh_job)
            except Exception:
                pass
        self._accounts_refresh_job = self.after(1200, self.accounts_tab.refresh_table)

    def on_status(self, text: str) -> None:
        self._safe_after(lambda: self.task_tab.set_status(text))
        self._safe_after(lambda: self.set_status(text))

    def set_status(self, text: str, color: str = COLORS["muted"]) -> None:
        self.status_dot.configure(text_color=color)
        self.status_text.configure(text=text, text_color=color)

    def select_campaign(self, campaign_id, *, discard_current=False):
        if self._closing:
            return
        if campaign_id == self.db.campaign_id:
            return
        if self.scheduler.is_running() or self.task_tab._starting:
            messagebox.showwarning(
                "Кампания выполняется", "Остановите задачу или дождитесь проверки перед переключением"
            )
            return
        if not discard_current and not self.task_tab.recipients.can_leave():
            return
        if discard_current:
            self.task_tab._drafts.pop(self.db.campaign_id, None)
            if self.task_tab._draft_job is not None:
                self.task_tab.after_cancel(self.task_tab._draft_job)
                self.task_tab._draft_job = None
        else:
            self.task_tab.flush_draft()
        # Replace the scope object: in-flight requests retain the previous campaign.
        self.db = self.db.for_campaign(campaign_id)
        self.scheduler.db = self.db
        for tab in (
            self.task_tab,
            self.targets_tab,
            self.dashboard_tab,
            self.logs_tab,
            self.accounts_tab,
            self.warmup_tab,
            self.ai_tab,
            self.parsing_tab,
        ):
            tab.db = self.db
        self.targets_tab.page = 0
        self.targets_tab.editor_status.configure(text="Никнеймы по одному на строку · Ctrl+V")
        self.targets_tab.refresh_table()
        self.targets_tab.refresh_stats()
        self.task_tab.load_campaign_settings()
        if self.current_tab_key == "logs":
            self.logs_tab.refresh_saved_logs()
        self.dashboard_tab._refresh()
        for bar in self.campaign_bars:
            bar.refresh()

    def _load_last_campaign(self):
        async def load():
            value = await self.db.get_setting("last_campaign_id", "1")
            campaigns = await self.db.get_campaigns()
            try:
                selected = int(value)
            except (ValueError, TypeError):
                selected = 1
            return selected if any(row["id"] == selected for row in campaigns) else campaigns[0]["id"]

        future = self.run_coro(load())

        def done(result):
            if not result.cancelled() and not result.exception():
                self._safe_after(lambda: self.select_campaign(result.result()))

        future.add_done_callback(done)

    def _switch_tab(self, key: str) -> None:
        if key not in self.page_frames:
            return
        self.current_tab_key = key
        if key == "task":
            self.task_tab.recipients.refresh()
        if key == "logs":
            self.logs_tab.refresh_saved_logs()
        if key == "warmup":
            self.warmup_tab._load_accounts()
        if key == "parsing":
            self.parsing_tab.refresh_accounts()
            if not self.parsing_tab.busy():
                self.parsing_tab.refresh_history()
        if key == "targets":
            self.targets_tab.refresh_table()
            self.targets_tab.refresh_stats()
        for tab_key, frame in self.page_frames.items():
            if tab_key == key:
                frame.place(relx=0, rely=0, relwidth=1, relheight=1)
                frame.lift()
            else:
                frame.place_forget()
        for tab_key, button in self.nav_buttons.items():
            active = tab_key == key
            color = COLORS["accent"] if active else COLORS["muted"]
            button.configure(
                text_color=color,
                image=icon(tab_key, color),
                fg_color=COLORS["accent_soft"] if active else "transparent",
            )
        self.breadcrumb_label.configure(
            text=f"{'Рабочее пространство  /  ' if self._rail_expanded else ''}{self.nav_titles[key]}"
        )
        style_tree(self.page_frames[key])

    def switch_tab(self, tab_name: str) -> None:
        key = self.tab_name_to_key.get(tab_name) or self.tab_name_to_key.get(tab_name.lower(), tab_name)
        if key in self.page_frames:
            self._switch_tab(key)

    def _on_density_change(self, value: str) -> None:
        mode = "compact" if value == "Компактный" else "normal"
        previous_mode = self._active_density_mode

        async def save_density():
            await self.db.set_setting("ui_density", mode)
            await self.db.set_setting("ui_profile_preset", "Кастом")

        future = self.run_coro(save_density())

        def done(result):
            def apply():
                if result.cancelled() or result.exception():
                    self.ui_density_var.set("Компактный" if previous_mode == "compact" else "Обычный")
                    self.set_status("Не удалось сохранить плотность интерфейса. Попробуйте ещё раз.", COLORS["error"])
                    return
                self._active_density_mode = mode
                self._active_profile_name = "Кастом"
                self.ui_profile_var.set("Кастом")
                if not self._apply_density(mode):
                    return
                label = "Компактный" if mode == "compact" else "Обычный"
                self.set_status(f"Плотность интерфейса: {label}. Настройка сохранена.", COLORS["accent"])

            self._safe_after(apply)

        future.add_done_callback(done)

    def _on_profile_change(self, value: str) -> None:
        profile_name = value if value in APP_PROFILES else "Кастом"
        profile = APP_PROFILES.get(profile_name)
        if not profile:
            previous_profile = self._active_profile_name
            future = self.run_coro(self.db.set_setting("ui_profile_preset", "Кастом"))

            def custom_done(result):
                def apply():
                    if result.cancelled() or result.exception():
                        self.ui_profile_var.set(previous_profile)
                        self.set_status("Не удалось сохранить выбор профиля. Попробуйте ещё раз.", COLORS["error"])
                        return
                    self._active_profile_name = "Кастом"
                    self.set_status(
                        "Выбран «Кастом»: текущие настройки AI-студии оставлены без изменений.",
                        COLORS["accent"],
                    )

                self._safe_after(apply)

            future.add_done_callback(custom_done)
            return

        previous_profile = self._active_profile_name
        previous_mode = self._active_density_mode

        async def save_profile_settings():
            await self.db.set_setting("ui_profile_preset", profile_name)
            for key, val in profile.items():
                await self.db.set_setting(key, val)

        fut = self.run_coro(save_profile_settings())

        def done(result):
            if result.cancelled() or result.exception():

                def show_save_error():
                    self.ui_profile_var.set(previous_profile)
                    self.ui_density_var.set("Компактный" if previous_mode == "compact" else "Обычный")
                    self.set_status(
                        "Не удалось сохранить профиль полностью. Проверьте настройки AI-студии.", COLORS["error"]
                    )

                self._safe_after(show_save_error)
                return
            mode = profile.get("ui_density", "normal")

            def apply_profile():
                self._active_profile_name = profile_name
                self._active_density_mode = mode
                self.ui_profile_var.set(profile_name)
                self.ui_density_var.set("Компактный" if mode == "compact" else "Обычный")
                density_applied = self._apply_density(mode)
                if hasattr(self, "warmup_tab"):
                    if hasattr(self.warmup_tab, "_load_ai_memory_setting"):
                        self.warmup_tab._load_ai_memory_setting()
                    if hasattr(self.warmup_tab, "_load_ai_behavior_settings"):
                        self.warmup_tab._load_ai_behavior_settings()
                effects = {
                    "Оператор": "тон общения, память, лимит диалогов и паузу между чатами",
                    "Аналитик": "экспертный тон, память, более строгий лимит и паузу между чатами",
                    "Compact Ops": "нейтральный тон, лимиты AI-диалогов и компактные таблицы",
                }.get(profile_name, "настройки AI-студии")
                if density_applied:
                    self.set_status(f"Профиль «{profile_name}» применён: {effects}.", COLORS["accent"])

            self._safe_after(apply_profile)

        fut.add_done_callback(done)

    def _load_profile_setting(self) -> None:
        fut = self.run_coro(self.db.get_setting("ui_profile_preset", "Кастом"))

        def done(f):
            try:
                profile_name = str(f.result() or "Кастом")
            except Exception:
                profile_name = "Кастом"

            def apply():
                loaded_name = profile_name if profile_name in APP_PROFILES else "Кастом"
                self._active_profile_name = loaded_name
                self.ui_profile_var.set(loaded_name)

            self._safe_after(apply)

        fut.add_done_callback(done)

    def _load_density_setting(self) -> None:
        fut = self.run_coro(self.db.get_setting("ui_density", "normal"))

        def done(f):
            try:
                mode = str(f.result() or "normal")
            except Exception:
                mode = "normal"

            def apply():
                loaded_mode = "compact" if mode == "compact" else "normal"
                self._active_density_mode = loaded_mode
                self.ui_density_var.set("Компактный" if loaded_mode == "compact" else "Обычный")
                self._apply_density(loaded_mode)

            self._safe_after(apply)

        fut.add_done_callback(done)

    def _apply_density(self, mode: str) -> bool:
        tabs = (
            ("Аудитория", getattr(self, "targets_tab", None)),
            ("Аккаунты", getattr(self, "accounts_tab", None)),
            ("Обзор", getattr(self, "dashboard_tab", None)),
        )
        failed = []
        for name, tab in tabs:
            if tab and hasattr(tab, "on_density_change"):
                try:
                    tab.on_density_change(mode)
                except Exception:
                    logging.getLogger(__name__).exception("Не удалось изменить плотность раздела «%s»", name)
                    failed.append(name)
        if failed:
            self.set_status(
                f"Плотность сохранена, но не обновлены разделы: {', '.join(failed)}. Перейдите в раздел ещё раз.",
                COLORS["error"],
            )
            return False
        return True

    def on_fatal(self, text: str) -> None:
        self._safe_after(lambda: messagebox.showerror("Критическая ошибка", text))

    def on_complete(self, summary: dict) -> None:
        def show():
            outcome = summary.get("outcome", "completed")
            title = {
                "completed": "Задача завершена",
                "failed": "Задача требует внимания",
                "stopped": "Задача остановлена",
            }.get(outcome, "Итог задачи")
            self.notify(
                title,
                "Откройте Inviter Studio: итог доступен в задаче, подробности — в журнале.",
                warning=outcome == "failed",
            )
            self.task_tab.show_summary(summary)

        self._safe_after(show)

    def notify(self, title, text, *, warning=False):
        if not self._closing:
            self.notifications.show(title, text, warning=warning)

    def _load_notification_setting(self):
        future = self.run_coro(self.db.get_setting("desktop_notifications", "1"))

        def done(result):
            def render():
                if result.cancelled() or result.exception() or self._notification_preference_changed:
                    return
                enabled = result.result() != "0"
                self.notifications.enabled = enabled
                self.notifications_var.set(enabled)

            self._safe_after(render)

        future.add_done_callback(done)

    def _toggle_notifications(self):
        self._notification_preference_changed = True
        enabled = self.notifications_var.get()
        self.notifications.enabled = enabled
        if not enabled:
            self.notifications.close()
        self.notifications_switch.configure(state="disabled")
        future = self.run_coro(self.db.set_setting("desktop_notifications", "1" if enabled else "0"))
        self._notification_save_future = future

        def done(result):
            def render():
                self.notifications_switch.configure(state="normal")
                if result.cancelled() or result.exception():
                    self.set_status("Выбор уведомлений не сохранён. Повторите переключение.")

            self._safe_after(render)

        future.add_done_callback(done)

    def _poll_ui(self) -> None:
        if self._closing:
            return
        if self.current_tab_key in {"targets", "dashboard"}:
            self.targets_tab.refresh_stats()
        self.after(5000, self._poll_ui)

    def _drain_ui(self) -> None:
        def on_error(exc):
            logging.getLogger(__name__).error("Ошибка обновления интерфейса: %s", exc)

        self.ui_dispatcher.drain(on_error)
        if not self.ui_dispatcher.closed:
            self.after(25, self._drain_ui)

    async def _shutdown_async(self, drafts=None) -> None:
        if self._notification_save_future:
            await asyncio.wrap_future(self._notification_save_future)
        if self._support_seen_future:
            try:
                await asyncio.wrap_future(self._support_seen_future)
            except Exception:
                logging.getLogger(__name__).warning("Не удалось сохранить отметку показа поддержки")
        export_future = self.parsing_tab.export_future
        if export_future and not export_future.done():
            await asyncio.wrap_future(export_future)
        if drafts:
            await self.task_tab.save_drafts(drafts)
        await self.db.set_setting("last_campaign_id", str(self.db.campaign_id))
        self.ui_dispatcher.closed = True
        self.scheduler.engine.stop()
        self.parsing_tab.stop()
        if self.warmup_tab.warmup_engine:
            self.warmup_tab.warmup_engine.stop()
        workers = [
            (self.scheduler.engine, self.scheduler.future),
            (self.warmup_tab.warmup_engine, getattr(self.warmup_tab, "warmup_future", None)),
            (self.parsing_tab.engine, self.parsing_tab.future),
        ]
        pending = [
            getattr(engine, "_task", None) or asyncio.wrap_future(future)
            for engine, future in workers
            if future and not future.done()
        ]
        if pending:
            done, unfinished = await asyncio.wait(pending, timeout=10)
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        current = asyncio.current_task()
        tasks = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Release login/test/sync clients too, including an unfinished login form.
        from core.account_manager import AccountManager

        managers = list(AccountManager._session_owners.values())
        await asyncio.gather(*(mgr.disconnect() for mgr in managers), return_exceptions=True)

    def _on_close(self) -> None:
        if self._closing:
            return
        if not self.task_tab.recipients.can_leave():
            return
        for dialog in (getattr(self.targets_tab, "_editor", None), getattr(self.task_tab, "_template_dialog", None)):
            if dialog is not None and dialog.winfo_exists():
                dialog.close()
                if dialog.winfo_exists():
                    return
        active = self.scheduler.is_running() or bool(
            getattr(self.warmup_tab, "warmup_future", None) and not self.warmup_tab.warmup_future.done()
        )
        active = active or self.parsing_tab.busy()
        if active and not messagebox.askyesno("Выход", "Задача выполняется. Закрыть приложение?"):
            return
        self._closing = True
        self.set_status("Завершение фоновых операций...")
        drafts = self.task_tab.capture_draft()
        self.task_tab.message_box.configure(state="disabled")
        self.task_tab.followup_box.configure(state="disabled")
        future = asyncio.run_coroutine_threadsafe(self._shutdown_async(drafts), self.async_loop)

        def finish():
            if not future.done():
                self.after(50, finish)
                return
            try:
                future.result()
            except Exception as exc:
                logging.getLogger(__name__).error("Ошибка завершения: %s", exc)
                self._closing = False
                dispatcher_was_closed = self.ui_dispatcher.closed
                self.ui_dispatcher.closed = False
                if dispatcher_was_closed:
                    self._drain_ui()
                self.task_tab.message_box.configure(state="normal")
                self.task_tab.followup_box.configure(state="normal")
                messagebox.showerror(
                    "Данные не сохранены",
                    "Не удалось сохранить данные перед выходом. "
                    "Окно оставлено открытым: скопируйте текст и повторите сохранение.\n" + explain_error(exc),
                )
                self.task_tab.update_counter()
                self._poll_ui()
                return
            self.ui_dispatcher.closed = True
            self.notifications.close()
            self.async_loop.call_soon_threadsafe(self.async_loop.stop)
            # Cancel widget/scaling timers before removing their Tcl commands.
            for timer_id in self.tk.call("after", "info"):
                self.tk.call("after", "cancel", timer_id)
            self.destroy()
            # The app is fully closed by now, so the installer will not meet a
            # running instance; detached launch lets it show its own wizard.
            if self._update_installer:
                os.startfile(self._update_installer)  # noqa: PTH110 - Windows shell launch

        self.after(50, finish)
