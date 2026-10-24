from __future__ import annotations

import json
import webbrowser
from datetime import datetime
from tkinter import messagebox

import customtkinter as ctk

from core.account_manager import AccountManager
from core.database import Database
from core.user_feedback import explain_error
from core.validation import validate_account
from ui.components import empty_state, page_intro
from ui.dispatch import post_ui
from ui.entry_utils import setup_entry
from ui.hints import hint, tip
from ui.theme import COLORS, FONTS, apply_button_role, get_density_preset, init_fonts, style_card, style_tree

STATUS_COLORS = {
    "active": COLORS["success"],
    "flood": COLORS["warning"],
    "error": COLORS["error"],
    "inactive": COLORS["muted"],
    "restricted": COLORS["error"],
    "unauthorized": COLORS["warning"],
}

AUTH_STATUS_TEXT = {
    "invalid_code": "Неверный код входа. Проверьте код в Telegram.",
    "code_expired": "Срок действия кода истёк. Запросите новый код.",
    "unauthorized": "Telegram не подтвердил вход. Запросите новый код или проверьте пароль.",
    "code_required": "Введите код входа из Telegram.",
}


class AccountsTab(ctk.CTkFrame):
    def __init__(self, master, db: Database, run_coro, log_callback, refresh_task_accounts):
        super().__init__(master, fg_color=COLORS["bg"])
        init_fonts()
        self.db = db
        self.run_coro = run_coro
        self.log_callback = log_callback
        self.refresh_task_accounts = refresh_task_accounts
        self.auth_manager: AccountManager | None = None
        self.pending_account_payload: dict | None = None
        self.get_code_btn: ctk.CTkButton | None = None
        self._code_request_inflight = False
        self._auth_future = None
        self.form_visible = False
        self.proxy_visible = False
        self._density_mode = "normal"
        self._all_accounts = []
        self.search_var = ctk.StringVar()
        self.filter_var = ctk.StringVar(value="Все")

        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.pack(fill="both", expand=True)
        self.container.grid_rowconfigure(0, weight=1)
        self.container.grid_columnconfigure(0, weight=1)
        self.container.grid_columnconfigure(1, weight=0)

        self.left_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        self.left_frame.grid(row=0, column=0, sticky="nsew", padx=(16, 8), pady=16)

        page_intro(
            self.left_frame,
            "Аккаунты",
            "Ваши подключения к Telegram. Всё необходимое — на одной карточке.",
            "ПОДКЛЮЧЕНИЯ",
        )
        top = ctk.CTkFrame(
            self.left_frame,
            fg_color=COLORS["surface"],
            border_color=COLORS["border"],
            border_width=1,
            corner_radius=12,
        )
        top.pack(fill="x", padx=0, pady=(0, 10))
        style_card(top, color=COLORS["accent"])
        ctk.CTkButton(
            top,
            text="+ Добавить аккаунт",
            command=self.show_add_form,
            corner_radius=12,
            height=36,
            font=FONTS["subhead"],
            fg_color=COLORS["accent"],
            hover_color=COLORS["warning"],
            text_color=COLORS["text"],
        ).pack(side="left", padx=16, pady=12)
        filters = ctk.CTkFrame(self.left_frame, fg_color="transparent")
        filters.pack(fill="x", pady=(0, 8))
        filters.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(filters, text="Телефон:", font=FONTS["small"]).grid(row=0, column=0, padx=(0, 8))
        self.search_entry = ctk.CTkEntry(filters, textvariable=self.search_var, height=30)
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.filter_menu = ctk.CTkOptionMenu(
            filters,
            values=["Все", "Включённые", "Выключенные", "Нужна проверка"],
            variable=self.filter_var,
            width=150,
            height=30,
        )
        self.filter_menu.grid(row=0, column=2, sticky="e")
        self._small_filter = None

        def fit_filters(event):
            small = event.width / self._get_widget_scaling() < 480
            if small != self._small_filter:
                self._small_filter = small
                self.filter_menu.grid(
                    row=1 if small else 0, column=1 if small else 2, sticky="e", pady=(6, 0) if small else 0
                )

        filters.bind("<Configure>", fit_filters, add="+")

        self.table = ctk.CTkScrollableFrame(self.left_frame, fg_color=COLORS["surface"], corner_radius=12)
        self.table.pack(fill="both", expand=True, padx=0, pady=(0, 10))

        self.right_frame = ctk.CTkFrame(
            self.container,
            fg_color=COLORS["surface"],
            border_color=COLORS["border"],
            border_width=1,
            corner_radius=12,
            width=340,
        )
        style_card(self.right_frame, color=COLORS["accent"])

        self.refresh_table()
        self.search_var.trace_add("write", lambda *_: self._render_accounts(self._all_accounts))
        self.filter_var.trace_add("write", lambda *_: self._render_accounts(self._all_accounts))

    def _safe_after(self, callback) -> None:
        post_ui(self, callback)

    def _open_api_site(self) -> None:
        url = "https://my.telegram.org/apps"
        if not webbrowser.open(url):
            messagebox.showinfo("Официальный сайт Telegram", url)

    def _show_api_instructions(self):
        from ui.campaigns import show_report

        return show_report(
            self,
            "Где получить данные Telegram",
            (
                "1. Откройте my.telegram.org/apps кнопкой «Получить API ID и API Hash». Войдите по своему номеру Telegram с кодом страны. Код для сайта Telegram присылает в приложение Telegram.\n\n"
                "2. Выберите API development tools. Если приложение уже создано, откройте его — API ID и API Hash уже указаны там.\n\n"
                "3. Для нового приложения заполните форму: App title — название, например My Desktop Client; Short name — 5–32 латинские буквы и цифры, например mydesktopclient; Platform — Desktop; Description — кратко опишите своё приложение. Если собственного сайта нет, поле URL можно оставить пустым, когда форма это допускает. Нажмите Create application.\n\n"
                "4. Скопируйте App api_id (число) в поле API ID и App api_hash (32 символа) в API Hash. В поле «Телефон» этой программы укажите номер подключаемого аккаунта в международном формате, например +380…. Это не токен BotFather и не ключ AI-провайдера.\n\n"
                "5. Нажмите «Получить код» в программе. Введите новый код входа, присланный Telegram. Код для входа на сайт и код для этой программы — разные запросы. При включённой двухэтапной защите Telegram дополнительно запросит ваш пароль 2FA.\n\n"
                "API ID и API Hash не меняются при каждом запуске. Не публикуйте API Hash, коды входа, пароль и файлы сессии. После успешного входа сессия сохраняется на этом ПК.\n\n"
                "Официальная инструкция: https://core.telegram.org/api/obtaining_api_id"
            ),
        )

    def show_add_form(self) -> None:
        if self.form_visible:
            return
        self.form_visible = True
        self.container.grid_columnconfigure(1, minsize=int(320 * self._get_widget_scaling()))
        self.right_frame.grid(row=0, column=1, sticky="nsew", padx=(0, 16), pady=16)
        self._build_add_form()
        style_tree(self.right_frame)

    def hide_form(self) -> None:
        self.form_visible = False
        self.container.grid_columnconfigure(1, minsize=0)
        if self._auth_future and not self._auth_future.done():
            self._auth_future.cancel()
        if self.auth_manager:
            manager = self.auth_manager
            self.auth_manager = None
            self.run_coro(manager.disconnect())
        self.pending_account_payload = None
        self.right_frame.grid_forget()
        for widget in self.right_frame.winfo_children():
            widget.destroy()

    def _toggle_proxy_section(self, toggle_btn: ctk.CTkButton) -> None:
        if self.proxy_visible:
            self.proxy_frame.pack_forget()
            toggle_btn.configure(text="Прокси (необязательно)")
            self.proxy_visible = False
        else:
            self.proxy_frame.pack(fill="x", padx=8, pady=(4, 0))
            toggle_btn.configure(text="▼  Прокси (необязательно)")
            self.proxy_visible = True

    def _build_add_form(self) -> None:
        for widget in self.right_frame.winfo_children():
            widget.destroy()

        header = ctk.CTkFrame(self.right_frame, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(16, 0))
        ctk.CTkLabel(header, text="Новый аккаунт", font=FONTS["subhead"], text_color=COLORS["text"]).pack(side="left")
        ctk.CTkButton(
            header,
            text="✕",
            width=28,
            height=28,
            fg_color="transparent",
            hover_color=COLORS["border"],
            text_color=COLORS["muted"],
            font=ctk.CTkFont(size=14),
            command=self.hide_form,
            corner_radius=12,
        ).pack(side="right")
        ctk.CTkFrame(self.right_frame, fg_color=COLORS["border"], height=1).pack(fill="x", padx=16, pady=12)

        scroll = ctk.CTkScrollableFrame(self.right_frame, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        def add_field(label: str, placeholder: str = "", show: str = ""):
            ctk.CTkLabel(scroll, text=label, font=FONTS["small"], text_color=COLORS["muted"]).pack(
                anchor="w", padx=8, pady=(8, 2)
            )
            entry = ctk.CTkEntry(
                scroll,
                placeholder_text=placeholder,
                show=show,
                height=36,
                font=FONTS["body"],
                fg_color=COLORS["bg"],
                border_color=COLORS["border"],
                text_color=COLORS["text"],
                corner_radius=12,
            )
            entry.pack(fill="x", padx=8, pady=(0, 4))
            setup_entry(entry)
            return entry

        self.new_phone_entry = add_field("Телефон", "+79990000000")
        self.new_api_id_entry = add_field("API ID", "12345678")
        self.new_api_hash_entry = add_field("API Hash", "abcdef1234...", show="*")
        hint(
            scroll,
            "Нет API ID и API Hash? Получите их на официальном my.telegram.org → API development tools. Это данные приложения Telegram, а не токен бота и не ключ AI.",
            padx=8,
        )
        self.api_site_button = ctk.CTkButton(
            scroll, text="Получить API ID и API Hash ↗", height=30, command=self._open_api_site
        )
        apply_button_role(self.api_site_button, "secondary")
        self.api_site_button.pack(fill="x", padx=8, pady=(0, 8))
        self.api_instructions_button = ctk.CTkButton(
            scroll, text="Пошагово: где взять данные", height=30, command=self._show_api_instructions
        )
        apply_button_role(self.api_instructions_button, "ghost")
        self.api_instructions_button.pack(fill="x", padx=8, pady=(0, 8))
        api_help = ctk.CTkButton(
            scroll,
            text="Как подключить аккаунт?",
            height=28,
            command=lambda: self.winfo_toplevel().show_help("accounts"),
        )
        apply_button_role(api_help, "ghost")
        api_help.pack(anchor="w", padx=8, pady=(0, 8))
        self.new_limit_entry = add_field("Дневной лимит для каждого режима")
        self.new_limit_entry.insert(0, "40")
        tip(
            self.new_limit_entry,
            "Максимум на аккаунт в день отдельно для сообщений и приглашений. 0 запрещает действия; доступный лимит может быть меньше заданного.",
        )
        tip(self.new_phone_entry, "Телефон вашего Telegram в международном формате с + и кодом страны.")
        tip(
            self.new_api_id_entry,
            "Числовой API ID вашего приложения на my.telegram.org. Это не код входа и не токен бота.",
        )
        tip(
            self.new_api_hash_entry,
            "API Hash из того же Telegram-приложения: 32 символа. Вставка через Ctrl+V доступна и в скрытом поле.",
        )

        self.proxy_visible = False
        proxy_toggle = ctk.CTkButton(
            scroll,
            text="Прокси (необязательно)",
            fg_color="transparent",
            hover_color=COLORS["border"],
            text_color=COLORS["muted"],
            anchor="w",
            font=FONTS["small"],
            command=lambda: self._toggle_proxy_section(proxy_toggle),
            corner_radius=12,
            height=30,
        )
        proxy_toggle.pack(fill="x", padx=8, pady=(8, 0))
        self.proxy_frame = ctk.CTkFrame(scroll, fg_color="transparent")

        def add_proxy_field(label: str, show: str = ""):
            ctk.CTkLabel(self.proxy_frame, text=label, font=FONTS["small"], text_color=COLORS["muted"]).pack(
                anchor="w", padx=8, pady=(8, 2)
            )
            entry = ctk.CTkEntry(
                self.proxy_frame,
                show=show,
                height=36,
                font=FONTS["body"],
                fg_color=COLORS["bg"],
                border_color=COLORS["border"],
                text_color=COLORS["text"],
                corner_radius=12,
            )
            entry.pack(fill="x", padx=8, pady=(0, 4))
            setup_entry(entry)
            return entry

        self.proxy_type_entry = add_proxy_field("Тип (SOCKS5 / HTTP)")
        self.proxy_host_entry = add_proxy_field("Хост")
        self.proxy_port_entry = add_proxy_field("Порт")
        self.proxy_login_entry = add_proxy_field("Логин")
        self.proxy_pass_entry = add_proxy_field("Пароль", show="*")
        hint(
            scroll,
            "Ключ AmneziaVPN вставляется в приложение AmneziaVPN на этом компьютере. "
            "Здесь можно указать только отдельный прокси SOCKS5 или HTTP для Telegram-аккаунта. "
            "VPN компьютера может направлять соединения всей программы через туннель; проверьте настройки маршрутизации VPN.",
        )

        ctk.CTkFrame(scroll, fg_color=COLORS["border"], height=1).pack(fill="x", padx=8, pady=12)
        self.step_label = ctk.CTkLabel(
            scroll,
            text="Шаг 1: заполните данные и нажмите «Получить код»",
            font=FONTS["small"],
            text_color=COLORS["muted"],
            wraplength=280,
        )
        self.step_label.pack(anchor="w", padx=8, pady=(0, 8))

        self.get_code_btn = ctk.CTkButton(
            scroll,
            text="Получить код",
            height=40,
            corner_radius=12,
            font=FONTS["subhead"],
            fg_color=COLORS["accent"],
            hover_color=COLORS["warning"],
            text_color=COLORS["bg"],
            command=self._get_code,
        )
        self.get_code_btn.pack(fill="x", padx=8, pady=(0, 4))

        self.code_section = ctk.CTkFrame(scroll, fg_color="transparent")
        self.code_entry = ctk.CTkEntry(
            self.code_section,
            placeholder_text="Введите код из Telegram",
            height=36,
            font=FONTS["body"],
            fg_color=COLORS["bg"],
            border_color=COLORS["accent"],
            text_color=COLORS["text"],
            corner_radius=12,
        )
        self.code_entry.pack(fill="x", padx=8, pady=(0, 4))
        setup_entry(self.code_entry)
        ctk.CTkButton(
            self.code_section,
            text="Подтвердить",
            height=40,
            corner_radius=12,
            font=FONTS["subhead"],
            fg_color=COLORS["success"],
            hover_color=COLORS["warning"],
            text_color=COLORS["text"],
            command=self._confirm_code,
        ).pack(fill="x", padx=8, pady=(0, 4))

        self.twofa_section = ctk.CTkFrame(scroll, fg_color="transparent")
        self.twofa_entry = ctk.CTkEntry(
            self.twofa_section,
            placeholder_text="Пароль двухфакторной защиты",
            show="*",
            height=36,
            font=FONTS["body"],
            fg_color=COLORS["bg"],
            border_color=COLORS["accent"],
            text_color=COLORS["text"],
            corner_radius=12,
        )
        self.twofa_entry.pack(fill="x", padx=8, pady=(0, 4))
        setup_entry(self.twofa_entry)
        ctk.CTkButton(
            self.twofa_section,
            text="Подтвердить 2FA",
            height=40,
            corner_radius=12,
            font=FONTS["subhead"],
            fg_color=COLORS["accent"],
            hover_color=COLORS["warning"],
            text_color=COLORS["text"],
            command=self._confirm_2fa,
        ).pack(fill="x", padx=8, pady=(0, 4))

        ctk.CTkButton(
            scroll,
            text="Отмена",
            height=36,
            corner_radius=12,
            font=FONTS["subhead"],
            fg_color=COLORS["border"],
            command=self.hide_form,
        ).pack(fill="x", padx=8, pady=(12, 8))

    def _collect_form_payload(self) -> dict:
        phone = self.new_phone_entry.get().strip()
        payload = {
            "phone": phone,
            "api_id": self.new_api_id_entry.get().strip(),
            "api_hash": self.new_api_hash_entry.get().strip(),
            "session_name": f"session_{phone.replace('+', '')}",
            "daily_limit": int(self.new_limit_entry.get().strip() or "40"),
            "proxy_type": None,
            "proxy_host": None,
            "proxy_port": None,
            "proxy_login": None,
            "proxy_password": None,
        }
        if self.proxy_visible:
            payload["proxy_type"] = self.proxy_type_entry.get().strip().upper() or None
            payload["proxy_host"] = self.proxy_host_entry.get().strip() or None
            payload["proxy_port"] = (
                int(self.proxy_port_entry.get().strip()) if self.proxy_port_entry.get().strip() else None
            )
            payload["proxy_login"] = self.proxy_login_entry.get().strip() or None
            payload["proxy_password"] = self.proxy_pass_entry.get().strip() or None
        return validate_account(payload)

    def _get_code(self) -> None:
        if self._code_request_inflight:
            return
        try:
            payload = self._collect_form_payload()
            if not payload["phone"] or not payload["api_id"] or not payload["api_hash"]:
                messagebox.showerror("Ошибка", "Телефон, API ID и API Hash обязательны.")
                self.step_label.configure(text="Ошибка: заполните обязательные поля")
                return
        except Exception as exc:  # noqa: BLE001
            reason = explain_error(exc)
            messagebox.showerror("Ошибка", reason)
            self.step_label.configure(text=f"Ошибка: {reason}")
            return

        self._code_request_inflight = True
        if self.get_code_btn:
            self.get_code_btn.configure(state="disabled", text="Отправка...")
        self.step_label.configure(text="Запрашиваем код в Telegram...")

        self.pending_account_payload = payload
        old_manager = self.auth_manager
        self.auth_manager = AccountManager(payload)
        proxy_data = {
            "proxy_type": payload.get("proxy_type"),
            "proxy_host": payload.get("proxy_host"),
            "proxy_port": payload.get("proxy_port"),
            "proxy_login": payload.get("proxy_login"),
            "proxy_password": payload.get("proxy_password"),
        }
        manager = self.auth_manager

        async def request():
            if old_manager:
                await old_manager.disconnect()
            return await manager.send_code(payload["phone"], payload["api_id"], payload["api_hash"], proxy_data)

        future = self.run_coro(request())
        self._auth_future = future

        def done(f):
            if not self.form_visible:
                self._code_request_inflight = False
                return
            try:
                _ = f.result()
            except Exception as exc:  # noqa: BLE001
                reason = explain_error(exc)
                err_text = f"Не удалось отправить код: {reason}"
                self._safe_after(lambda reason=reason: self.step_label.configure(text=f"Ошибка: {reason}"))
                self._safe_after(
                    lambda reason=reason: messagebox.showerror("Ошибка", f"Не удалось отправить код:\n{reason}")
                )
                self._safe_after(lambda: self.log_callback("ERROR", err_text))
            else:
                self._safe_after(lambda: self.step_label.configure(text="Шаг 2: введите код из Telegram"))
                self._safe_after(lambda: self.code_section.pack(fill="x", padx=0, pady=(4, 0)))
            finally:
                self._code_request_inflight = False
                self._safe_after(
                    lambda: (
                        self.get_code_btn.configure(state="normal", text="Получить код") if self.get_code_btn else None
                    )
                )

        future.add_done_callback(done)

    def _save_account(self) -> None:
        if not self.pending_account_payload:
            return
        fut_save = self.run_coro(self.db.add_account(self.pending_account_payload))

        def done_save(result):
            try:
                result.result()
            except Exception as exc:
                self._safe_after(lambda exc=exc: messagebox.showerror("Ошибка сохранения", explain_error(exc)))
                return
            if self.auth_manager:
                self.run_coro(self.auth_manager.disconnect())
                self.auth_manager = None
            self._safe_after(self.refresh_table)
            self._safe_after(self.hide_form)
            self._safe_after(lambda: self.log_callback("SUCCESS", "Аккаунт успешно добавлен и авторизован"))

        fut_save.add_done_callback(done_save)

    def _confirm_code(self) -> None:
        if not self.auth_manager or not self.pending_account_payload:
            return
        code = self.code_entry.get().strip()
        if not code:
            messagebox.showwarning("Внимание", "Введите код.")
            return
        fut = self.run_coro(
            self.auth_manager.authorize(self.pending_account_payload["phone"], code=code, password=None)
        )

        def done(f):
            try:
                result = f.result()
            except Exception as exc:  # noqa: BLE001
                self._safe_after(lambda exc=exc: messagebox.showerror("Ошибка", explain_error(exc)))
                return
            if result["status"] == "ok":
                self._safe_after(self._save_account)
            elif result["status"] == "password_required":
                self._safe_after(lambda: self.twofa_section.pack(fill="x", padx=0, pady=(4, 0)))
            else:
                self._safe_after(
                    lambda: messagebox.showerror(
                        "Ошибка",
                        AUTH_STATUS_TEXT.get(result["status"], "Telegram не подтвердил вход. Повторите попытку."),
                    )
                )

        fut.add_done_callback(done)

    def _confirm_2fa(self) -> None:
        if not self.auth_manager or not self.pending_account_payload:
            return
        password = self.twofa_entry.get()
        if not password:
            messagebox.showwarning("Внимание", "Введите пароль 2FA.")
            return
        fut = self.run_coro(
            self.auth_manager.authorize(self.pending_account_payload["phone"], code=None, password=password)
        )

        def done(f):
            try:
                result = f.result()
            except Exception as exc:  # noqa: BLE001
                self._safe_after(lambda exc=exc: messagebox.showerror("Ошибка", explain_error(exc)))
                return
            if result["status"] == "ok":
                self._safe_after(self._save_account)
            else:
                self._safe_after(
                    lambda: messagebox.showerror(
                        "Ошибка",
                        AUTH_STATUS_TEXT.get(result["status"], "Telegram не подтвердил вход. Повторите попытку."),
                    )
                )

        fut.add_done_callback(done)

    def _status_text(self, account: dict) -> str:
        if int(account.get("is_enabled", 1)) == 0:
            return "Не выбран для задач"
        status = account.get("status") or "inactive"
        if status == "flood" and account.get("flood_until"):
            try:
                delta = datetime.fromisoformat(account["flood_until"]) - datetime.now()
                seconds = max(int(delta.total_seconds()), 0)
            except Exception:
                return "Ожидание Telegram"
            if not seconds:
                return "Пауза истекла · готов к проверке"
            return f"Ожидание ({(seconds + 59) // 60} мин)"
        mapping = {
            "active": "Готов",
            "error": "Ошибка",
            "inactive": "Неактивен",
            "restricted": "Отправка требует проверки",
            "unauthorized": "Нужен вход",
        }
        return mapping.get(status, status)

    def refresh_table(self) -> None:
        fut = self.run_coro(self.db.get_accounts())

        def done(f):
            try:
                accounts = f.result()
            except Exception:
                return
            self._safe_after(lambda: self._render_accounts(accounts))
            self._safe_after(self.refresh_task_accounts)

        fut.add_done_callback(done)

    def _render_accounts(self, accounts: list[dict]) -> None:
        self._all_accounts = accounts
        query = self.search_var.get().strip().casefold()
        selected = self.filter_var.get()
        all_count = len(accounts)
        accounts = [
            account
            for account in accounts
            if query in account["phone"].casefold()
            and (
                selected == "Все"
                or (selected == "Включённые" and account.get("is_enabled", 1))
                or (selected == "Выключенные" and not account.get("is_enabled", 1))
                or (selected == "Нужна проверка" and account.get("status") != "active")
            )
        ]
        density = get_density_preset(self._density_mode)
        for widget in self.table.winfo_children():
            widget.destroy()

        if not accounts:
            if all_count:
                empty_state(self.table, "Нет совпадений", "Измените телефон или выберите фильтр «Все».", "accounts")
                return
            empty_state(
                self.table,
                "Подключите первый аккаунт",
                "Добавьте Telegram-аккаунт, чтобы начать работу.",
                "accounts",
                ("Добавить аккаунт", self.show_add_form),
            )
            style_tree(self.table)
            return
        for account in accounts:
            card = ctk.CTkFrame(
                self.table,
                fg_color=COLORS["surface"],
                border_color=COLORS["border"],
                border_width=1,
                corner_radius=12,
            )
            card.pack(fill="x", padx=16, pady=(0, density["card_pad_y"]))
            style_card(card, color=COLORS["border"])

            proxy_text = "-"
            if account.get("proxy_type"):
                proxy_text = (
                    f"{account['proxy_type']} {account.get('proxy_host') or ''}:{account.get('proxy_port') or ''}"
                )

            status = account.get("status") or "inactive"
            status_color = STATUS_COLORS.get(status, COLORS["muted"])
            if not int(account.get("is_enabled", 1)):
                status_color = COLORS["muted"]
            content = ctk.CTkFrame(card, fg_color="transparent")
            content.pack(fill="x", padx=12, pady=(6, 2))
            ctk.CTkLabel(content, text=account["phone"], anchor="w", font=FONTS["subhead"]).pack(side="left")
            switch = ctk.CTkSwitch(content, text="Использовать", width=110, font=FONTS["small"])
            tip(switch, "Разрешить использовать аккаунт в задачах. Сам по себе переключатель ничего не запускает.")
            if int(account.get("is_enabled", 1)) == 1:
                switch.select()
            else:
                switch.deselect()
            switch.configure(command=lambda aid=account["id"], sw=switch: self.toggle_account(aid, bool(sw.get())))
            switch.pack(side="right")
            device_text = "Устройство не задано"
            if account.get("device_profile"):
                try:
                    device = json.loads(account["device_profile"])
                    device_text = str(device.get("device_model", "Устройство не задано"))
                except (ValueError, TypeError, AttributeError):
                    pass
            ctk.CTkLabel(
                card,
                text=f"{self._status_text(account)} • {device_text} • Прокси: {proxy_text}",
                anchor="w",
                text_color=status_color,
                font=FONTS["small"],
                wraplength=440,
            ).pack(fill="x", padx=12, pady=(0, 4))
            actions = ctk.CTkFrame(card, fg_color="transparent")
            actions.pack(fill="x", padx=12, pady=(0, density["card_pad_y"]))
            ctk.CTkLabel(
                actions,
                text=f"Сегодня: приглашений {account.get('added_today') or 0}; сообщений {account.get('sent_today') or 0}"
                f" / лимит {account.get('daily_limit', 40)} на режим",
                font=FONTS["small"],
                text_color=COLORS["muted"],
            ).pack(anchor="w", pady=(0, 5))
            ctk.CTkButton(
                actions,
                text="Проверить",
                width=70,
                height=density["list_item_h"],
                corner_radius=12,
                fg_color=COLORS["surface"],
                hover_color=COLORS["bg"],
                command=lambda aid=account["id"]: self.test_account(aid),
                text_color=COLORS["accent"],
                border_color=COLORS["accent"],
                border_width=1,
                font=FONTS["small"],
            ).pack(side="left", padx=(0, 6))
            ctk.CTkButton(
                actions,
                text="Изменить",
                width=70,
                height=density["list_item_h"],
                corner_radius=12,
                fg_color=COLORS["surface"],
                hover_color=COLORS["bg"],
                command=lambda a=account: self.open_edit_modal(a),
                text_color=COLORS["text"],
                border_color=COLORS["border"],
                border_width=1,
                font=FONTS["small"],
            ).pack(side="left")
            ctk.CTkButton(
                actions,
                text="Удалить",
                width=70,
                height=density["list_item_h"],
                corner_radius=12,
                fg_color=COLORS["surface"],
                hover_color=COLORS["bg"],
                command=lambda aid=account["id"]: self.delete_account(aid),
                text_color=COLORS["error"],
                border_color=COLORS["error"],
                border_width=1,
                font=FONTS["small"],
            ).pack(side="left", padx=6)

        style_tree(self.table)

    def on_density_change(self, mode: str) -> None:
        self._density_mode = "compact" if mode == "compact" else "normal"
        self.refresh_table()

    def toggle_account(self, account_id: int, enabled: bool) -> None:
        fut = self.run_coro(self.db.toggle_account_enabled(account_id, enabled))
        fut.add_done_callback(lambda _: self._safe_after(self.refresh_table))

    def test_account(self, account_id: int) -> None:
        async def worker() -> str:
            account = await self.db.get_account(account_id)
            if not account:
                return "Аккаунт не найден"
            mgr = AccountManager(account)
            try:
                await mgr.connect()
                authorized = await mgr.client.is_user_authorized() if mgr.client else False
                me = await mgr.client.get_me() if mgr.client else None
            finally:
                await mgr.disconnect()
            if authorized and me:
                result = f"Вход и соединение работают: {me.first_name} @{me.username or ''}".strip()
                if account.get("status") == "restricted":
                    result += (
                        "\n\nTelegram ранее отклонил отправку с этого аккаунта. "
                        "Проверка входа не подтверждает возможность отправлять личные сообщения "
                        "и не снимает ограничение. Проверьте состояние аккаунта в Telegram. "
                        "После устранения причины откройте «Изменить» и выберите «Готов»."
                    )
                return result
            return "Сессия не активна"

        fut = self.run_coro(worker())

        def done(f):
            try:
                result = f.result()
            except Exception as exc:  # noqa: BLE001
                result = "Ошибка: " + explain_error(exc)
            self._safe_after(lambda: messagebox.showinfo("Тест аккаунта", result))

        fut.add_done_callback(done)

    def delete_account(self, account_id: int) -> None:
        if not messagebox.askyesno("Подтверждение", "Удалить аккаунт?"):
            return

        async def remove():
            account = await self.db.get_account(account_id)
            if account and AccountManager.is_session_busy(account["phone"], account.get("session_name")):
                raise RuntimeError("Сначала остановите операции с этим аккаунтом")
            await self.db.delete_account(account_id)

        fut = self.run_coro(remove())

        def done(result):
            if result.cancelled():
                return
            error = result.exception()
            if error is not None:
                self._safe_after(lambda error=error: messagebox.showerror("Аккаунт не удалён", explain_error(error)))
                return
            self._safe_after(self.refresh_table)
            self._safe_after(lambda: self.log_callback("INFO", "Аккаунт и его локальная сессия удалены"))

        fut.add_done_callback(done)

    async def _save_account_edits(self, account_id: int, payload: dict) -> None:
        current = await self.db.get_account(account_id)
        if not current:
            raise ValueError("Аккаунт уже удалён. Обновите список аккаунтов.")
        deadline = current.get("flood_until")
        if (
            deadline
            and datetime.fromisoformat(deadline) > datetime.now()
            and "status" in payload
            and payload["status"] != "flood"
        ):
            until = datetime.fromisoformat(deadline).strftime("%d.%m.%Y %H:%M:%S")
            raise ValueError(f"Telegram назначил паузу до {until}. Дождитесь её окончания; вручную снять паузу нельзя.")
        network_changed = any(
            key in payload and payload[key] != current.get(key)
            for key in ("proxy_type", "proxy_host", "proxy_port", "proxy_login", "proxy_password")
        )
        if network_changed and AccountManager.is_session_busy(current["phone"], current.get("session_name")):
            raise ValueError("Сначала остановите задачу, парсинг или прогрев этого аккаунта, затем измените прокси.")
        await self.db.update_account(account_id, payload)

    def open_edit_modal(self, account: dict) -> None:
        modal = ctk.CTkToplevel(self)
        modal.title("Редактировать аккаунт")
        modal.geometry("480x620")
        modal.minsize(420, 420)
        modal.configure(fg_color=COLORS["bg"])
        modal.transient(self.winfo_toplevel())
        modal.grab_set()
        form = ctk.CTkScrollableFrame(modal, fg_color="transparent")
        form.pack(fill="both", expand=True, padx=8, pady=(8, 0))

        def field(label, value, **options):
            ctk.CTkLabel(form, text=label).pack(anchor="w", padx=8, pady=(10, 2))
            entry = ctk.CTkEntry(form, **options)
            setup_entry(entry)
            entry.insert(0, str(value or ""))
            entry.pack(fill="x", padx=8)
            return entry

        limit_entry = field("Лимит действий в день (0 — не отправлять)", str(account.get("daily_limit", 40)))
        proxy_type = field("Тип прокси: SOCKS5 / HTTP (необязательно)", account.get("proxy_type"))
        proxy_host = field("Адрес сервера прокси", account.get("proxy_host"))
        proxy_port = field("Порт прокси", account.get("proxy_port"))
        proxy_login = field("Логин прокси (если требуется)", account.get("proxy_login"))
        proxy_password = field("Пароль прокси (если требуется)", account.get("proxy_password"), show="*")
        hint(
            form,
            "Оставьте тип пустым для работы без прокси. Прокси меняется после остановки операций аккаунта.",
            padx=8,
        )

        ctk.CTkLabel(form, text="Состояние (менять после проверки аккаунта)").pack(anchor="w", padx=8, pady=(10, 2))
        statuses = {
            "Готов": "active",
            "Неактивен": "inactive",
            "Ожидание Telegram": "flood",
            "Отправка требует проверки": "restricted",
            "Нужен вход": "unauthorized",
            "Ошибка": "error",
        }
        status_menu = ctk.CTkOptionMenu(form, values=list(statuses))
        status_menu.set(
            next((label for label, value in statuses.items() if value == account.get("status")), "Неактивен")
        )
        status_menu.pack(fill="x", padx=8)
        hint(form, "Назначенная Telegram пауза сохраняется до указанного срока.", padx=8)

        def save():
            try:
                payload = {
                    "daily_limit": int(limit_entry.get().strip() or "40"),
                    "proxy_type": proxy_type.get().strip().upper() or None,
                    "proxy_host": proxy_host.get().strip() or None,
                    "proxy_port": int(proxy_port.get().strip()) if proxy_port.get().strip() else None,
                    "proxy_login": proxy_login.get().strip() or None,
                    "proxy_password": proxy_password.get() or None,
                }
                selected_status = statuses[status_menu.get()]
                if selected_status != (account.get("status") or "inactive"):
                    payload["status"] = selected_status
                validate_account({**account, **payload})
            except ValueError as exc:
                messagebox.showerror("Ошибка", explain_error(exc), parent=modal)
                return
            fut = self.run_coro(self._save_account_edits(account["id"], payload))

            def done(result):
                try:
                    result.result()
                except Exception as exc:
                    self._safe_after(lambda exc=exc: messagebox.showerror("Ошибка", explain_error(exc)))
                    return
                self._safe_after(self.refresh_table)
                self._safe_after(modal.destroy)

            fut.add_done_callback(done)

        ctk.CTkButton(
            modal,
            text="Сохранить изменения",
            command=save,
            corner_radius=12,
            height=36,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["warning"],
            text_color=COLORS["text"],
        ).pack(pady=14)
        style_tree(modal)
