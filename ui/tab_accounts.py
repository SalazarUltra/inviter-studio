from __future__ import annotations

import json
import webbrowser
from datetime import datetime
from tkinter import messagebox

import customtkinter as ctk

from core.account_display import account_headline, profile_from_user
from core.account_manager import AccountManager
from core.database import Database, effective_account_age_days, is_flood_blocking
from core.inviter import InviterEngine
from core.user_feedback import explain_error
from core.validation import validate_account
from ui.components import empty_state, page_intro, set_page_intro
from ui.dispatch import post_ui
from ui.entry_utils import setup_entry
from ui.hints import hint, tip
from ui.i18n import t
from ui.summary_card import SummaryCard
from ui.theme import COLORS, FONTS, apply_button_role, get_density_preset, init_fonts, style_card, style_tree

ACCOUNT_FILTERS = ("all", "enabled", "disabled", "check")
FILTER_DEBOUNCE_MS = 220

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
        self._save_inflight = False
        self._confirm_inflight = False
        self._auth_future = None
        self.form_visible = False
        self.proxy_visible = False
        self._density_mode = "normal"
        self._all_accounts = []
        self._filter_code = "all"
        self._filter_job = None
        self.search_var = ctk.StringVar()
        self.filter_var = ctk.StringVar(value=t("accounts.filter.all"))

        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.pack(fill="both", expand=True)
        self.container.grid_rowconfigure(0, weight=1)
        self.container.grid_columnconfigure(0, weight=1)
        self.container.grid_columnconfigure(1, weight=0)

        self.left_frame = ctk.CTkFrame(self.container, fg_color="transparent")
        self.left_frame.grid(row=0, column=0, sticky="nsew", padx=(16, 8), pady=16)

        self.intro = page_intro(
            self.left_frame,
            t("accounts.title"),
            t("accounts.desc"),
            t("accounts.kicker"),
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
        self.add_button = ctk.CTkButton(
            top,
            text=t("accounts.add"),
            command=self.show_add_form,
            corner_radius=12,
            height=36,
            font=FONTS["subhead"],
        )
        apply_button_role(self.add_button, "primary")
        self.add_button.pack(side="left", padx=16, pady=12)
        filters = ctk.CTkFrame(self.left_frame, fg_color="transparent")
        filters.pack(fill="x", pady=(0, 8))
        filters.grid_columnconfigure(1, weight=1)
        self.phone_label = ctk.CTkLabel(filters, text=t("accounts.phone"), font=FONTS["small"])
        self.phone_label.grid(row=0, column=0, padx=(0, 8))
        self.search_entry = ctk.CTkEntry(filters, textvariable=self.search_var, height=30)
        self.search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.filter_menu = ctk.CTkOptionMenu(
            filters,
            values=self._filter_labels(),
            variable=self.filter_var,
            width=150,
            height=30,
            command=self._on_filter_change,
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

        self.summary_card = SummaryCard(self.left_frame, get_db=lambda: self.db, run_coro=run_coro)
        self.summary_card.pack(side="bottom", fill="x", pady=(0, 4))
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
        self.search_var.trace_add("write", lambda *_: self._schedule_filter_render())

    def _filter_labels(self) -> list[str]:
        return [t(f"accounts.filter.{key}") for key in ACCOUNT_FILTERS]

    def _on_filter_change(self, label: str) -> None:
        labels = self._filter_labels()
        try:
            self._filter_code = ACCOUNT_FILTERS[labels.index(label)]
        except ValueError:
            self._filter_code = "all"
        self._schedule_filter_render()

    def _schedule_filter_render(self) -> None:
        if self._filter_job is not None:
            try:
                self.after_cancel(self._filter_job)
            except Exception:
                pass
        self._filter_job = self.after(FILTER_DEBOUNCE_MS, lambda: self._render_accounts(self._all_accounts))

    def _account_matches(self, account: dict, query: str) -> bool:
        haystack = " ".join(
            str(account.get(key) or "")
            for key in ("phone", "tg_first_name", "tg_last_name", "tg_username")
        ).casefold()
        if query and query not in haystack:
            return False
        code = self._filter_code
        if code == "enabled":
            return bool(account.get("is_enabled", 1))
        if code == "disabled":
            return not account.get("is_enabled", 1)
        if code == "check":
            return account.get("status") != "active"
        return True

    def _safe_after(self, callback) -> None:
        post_ui(self, callback)

    def _open_api_site(self) -> None:
        url = "https://my.telegram.org/apps"
        if not webbrowser.open(url):
            messagebox.showinfo(t("accounts.official_site"), url)

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
            toggle_btn.configure(text=t("accounts.proxy"))
            self.proxy_visible = False
        else:
            self.proxy_frame.pack(fill="x", padx=8, pady=(4, 0))
            toggle_btn.configure(text="▼  " + t("accounts.proxy"))
            self.proxy_visible = True

    def _build_add_form(self) -> None:
        for widget in self.right_frame.winfo_children():
            widget.destroy()

        header = ctk.CTkFrame(self.right_frame, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(16, 0))
        ctk.CTkLabel(header, text=t("accounts.new"), font=FONTS["subhead"], text_color=COLORS["text"]).pack(side="left")
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
            scroll, text=t("accounts.api_site"), height=30, command=self._open_api_site
        )
        apply_button_role(self.api_site_button, "secondary")
        self.api_site_button.pack(fill="x", padx=8, pady=(0, 8))
        self.api_instructions_button = ctk.CTkButton(
            scroll, text=t("accounts.api_steps"), height=30, command=self._show_api_instructions
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
        self.new_age_entry = add_field("Возраст аккаунта, дней")
        self.new_age_entry.insert(0, "0")
        tip(
            self.new_age_entry,
            "Сколько дней аккаунту Telegram. Новые аккаунты (до 7 дней) ограничены 10 действиями в день, до 30 дней — 20. Если оставить 0, возраст считается с момента добавления в программу.",
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
            text=t("accounts.proxy"),
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
            text=t("accounts.step1"),
            font=FONTS["small"],
            text_color=COLORS["muted"],
            wraplength=280,
        )
        self.step_label.pack(anchor="w", padx=8, pady=(0, 8))

        self.get_code_btn = ctk.CTkButton(
            scroll,
            text=t("accounts.get_code"),
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
            text=t("accounts.confirm"),
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
            text=t("accounts.confirm_2fa"),
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
            text=t("btn.cancel"),
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
            "account_age_days": int(self.new_age_entry.get().strip() or "0"),
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
            self.get_code_btn.configure(state="disabled", text=t("accounts.sending"))
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
                        self.get_code_btn.configure(state="normal", text=t("accounts.get_code"))
                        if self.get_code_btn
                        else None
                    )
                )

        future.add_done_callback(done)

    def _save_account(self) -> None:
        if not self.pending_account_payload or self._save_inflight:
            return
        self._save_inflight = True
        payload = self.pending_account_payload
        # AccountManager fills device_profile on the payload while connecting.
        fut_save = self.run_coro(self.db.save_authorized_account(payload))

        def done_save(result):
            self._save_inflight = False
            try:
                _account_id, created = result.result()
            except Exception as exc:
                self._safe_after(lambda exc=exc: messagebox.showerror("Ошибка сохранения", explain_error(exc)))
                return
            if self.auth_manager:
                self.run_coro(self.auth_manager.disconnect())
                self.auth_manager = None
            message = "Аккаунт успешно добавлен и авторизован" if created else f"Вход обновлён: {payload['phone']}"
            self._safe_after(self.refresh_table)
            self._safe_after(self.hide_form)
            self._safe_after(lambda: self.log_callback("SUCCESS", message))
            if not created:
                self._safe_after(
                    lambda: messagebox.showinfo(
                        "Вход обновлён", f"Аккаунт {payload['phone']} уже был в списке. Вход обновлён, аккаунт готов."
                    )
                )

        fut_save.add_done_callback(done_save)

    def _confirm_code(self) -> None:
        if not self.auth_manager or not self.pending_account_payload:
            return
        code = self.code_entry.get().strip()
        if not code:
            messagebox.showwarning("Внимание", "Введите код.")
            return
        if self._confirm_inflight:
            return
        self._confirm_inflight = True
        fut = self.run_coro(
            self.auth_manager.authorize(self.pending_account_payload["phone"], code=code, password=None)
        )

        def done(f):
            self._confirm_inflight = False
            try:
                result = f.result()
            except Exception as exc:  # noqa: BLE001
                self._safe_after(lambda exc=exc: messagebox.showerror("Ошибка", explain_error(exc)))
                return
            if result["status"] == "ok":
                if self.pending_account_payload is not None:
                    self.pending_account_payload.update(
                        {
                            "tg_first_name": result.get("tg_first_name") or "",
                            "tg_last_name": result.get("tg_last_name") or "",
                            "tg_username": result.get("tg_username") or "",
                        }
                    )
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
        if self._confirm_inflight:
            return
        self._confirm_inflight = True
        fut = self.run_coro(
            self.auth_manager.authorize(self.pending_account_payload["phone"], code=None, password=password)
        )

        def done(f):
            self._confirm_inflight = False
            try:
                result = f.result()
            except Exception as exc:  # noqa: BLE001
                self._safe_after(lambda exc=exc: messagebox.showerror("Ошибка", explain_error(exc)))
                return
            if result["status"] == "ok":
                if self.pending_account_payload is not None:
                    self.pending_account_payload.update(
                        {
                            "tg_first_name": result.get("tg_first_name") or "",
                            "tg_last_name": result.get("tg_last_name") or "",
                            "tg_username": result.get("tg_username") or "",
                        }
                    )
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
            return t("status.disabled")
        status = account.get("status") or "inactive"
        if status == "flood" and account.get("flood_until"):
            try:
                delta = datetime.fromisoformat(account["flood_until"]) - datetime.now()
                seconds = max(int(delta.total_seconds()), 0)
            except Exception:
                return t("status.flood")
            if not seconds:
                return t("status.flood_expired")
            if seconds >= 3600:
                return t("status.flood_hours", h=seconds // 3600, min=seconds % 3600 // 60)
            return t("status.flood_wait", min=(seconds + 59) // 60)
        if status == "active":
            return t("status.ready")
        key = f"status.{status}"
        text = t(key)
        return text if text != key else status

    def refresh_table(self) -> None:
        fut = self.run_coro(self.db.get_accounts())

        def done(f):
            try:
                accounts = f.result()
            except Exception:
                return
            self._safe_after(lambda: self._render_accounts(accounts))
            self._safe_after(self.refresh_task_accounts)
            self._safe_after(self.summary_card.refresh_accounts)

        fut.add_done_callback(done)

    def retranslate(self) -> None:
        """Re-render labels after a language switch (no restart)."""
        set_page_intro(
            self.intro,
            title=t("accounts.title"),
            description=t("accounts.desc"),
            kicker=t("accounts.kicker"),
        )
        self.add_button.configure(text=t("accounts.add"))
        self.phone_label.configure(text=t("accounts.phone"))
        labels = self._filter_labels()
        self.filter_menu.configure(values=labels)
        self.filter_var.set(labels[ACCOUNT_FILTERS.index(self._filter_code)])
        self.summary_card.retranslate()
        self._render_accounts(self._all_accounts)

    def _render_accounts(self, accounts: list[dict]) -> None:
        self._filter_job = None
        self._all_accounts = accounts
        query = self.search_var.get().strip().casefold()
        all_count = len(accounts)
        accounts = [account for account in accounts if self._account_matches(account, query)]
        density = get_density_preset(self._density_mode)
        for widget in self.table.winfo_children():
            widget.destroy()

        if not accounts:
            if all_count:
                empty_state(self.table, t("accounts.no_match"), t("accounts.no_match_hint"), "accounts")
                return
            empty_state(
                self.table,
                t("accounts.empty_title"),
                t("accounts.empty_text"),
                "accounts",
                (t("accounts.empty_add"), self.show_add_form),
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
            ctk.CTkLabel(content, text=account_headline(account), anchor="w", font=FONTS["subhead"]).pack(side="left")
            switch = ctk.CTkSwitch(content, text=t("accounts.use"), width=110, font=FONTS["small"])
            tip(switch, t("accounts.use_tip"))
            if int(account.get("is_enabled", 1)) == 1:
                switch.select()
            else:
                switch.deselect()
            switch.configure(command=lambda aid=account["id"], sw=switch: self.toggle_account(aid, bool(sw.get())))
            switch.pack(side="right")
            device_text = t("accounts.device_unset")
            if account.get("device_profile"):
                try:
                    device = json.loads(account["device_profile"])
                    device_text = str(device.get("device_model") or t("accounts.device_unset"))
                except (ValueError, TypeError, AttributeError):
                    pass
            ctk.CTkLabel(
                card,
                text=f"{self._status_text(account)} • {device_text} • {t('accounts.proxy_label', value=proxy_text)}",
                anchor="w",
                text_color=status_color,
                font=FONTS["small"],
                wraplength=440,
            ).pack(fill="x", padx=12, pady=(0, 4))
            actions = ctk.CTkFrame(card, fg_color="transparent")
            actions.pack(fill="x", padx=12, pady=(0, density["card_pad_y"]))
            ctk.CTkLabel(
                actions,
                text=t(
                    "accounts.today",
                    invites=account.get("added_today") or 0,
                    sent=account.get("sent_today") or 0,
                    limit=account.get("daily_limit", 40),
                    safe=InviterEngine.get_safe_daily_limit(account),
                    age=effective_account_age_days(account),
                ),
                font=FONTS["small"],
                text_color=COLORS["muted"],
            ).pack(anchor="w", pady=(0, 5))
            ctk.CTkButton(
                actions,
                text=t("accounts.test"),
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
                text=t("accounts.edit"),
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
                text=t("accounts.delete"),
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
            account = await self.db.clear_expired_flood(account)
            mgr = AccountManager(account)
            try:
                await mgr.connect()
                authorized = await mgr.client.is_user_authorized() if mgr.client else False
                me = await mgr.client.get_me() if mgr.client else None
            finally:
                await mgr.disconnect()
            if not authorized or not me:
                return "Сессия не активна"
            profile = profile_from_user(me)
            await self.db.update_account_profile(
                account_id,
                first_name=profile["tg_first_name"],
                last_name=profile["tg_last_name"],
                username=profile["tg_username"],
            )
            name = " ".join(part for part in (profile["tg_first_name"], profile["tg_last_name"]) if part).strip()
            handle = f"@{profile['tg_username']}" if profile["tg_username"] else ""
            result = f"Вход и соединение работают: {name} {handle}".strip()
            previous = account.get("status") or "inactive"
            # Successful login check clears soft warnings; an active Telegram pause stays until it expires.
            if previous == "flood" and is_flood_blocking(account):
                result += "\n\nВход в порядке, но Telegram ещё держит паузу — статус «Пауза» остаётся до её окончания."
            elif previous in {"restricted", "unauthorized", "error", "inactive"}:
                await self.db.update_account_status(account_id, "active")
                result += "\n\nСтатус обновлён: Готов. Если Telegram снова ограничит отправку, метка вернётся сама."
            elif previous != "active":
                await self.db.update_account_status(account_id, "active")
                result += "\n\nСтатус обновлён: Готов."
            return result

        fut = self.run_coro(worker())

        def done(f):
            try:
                result = f.result()
            except Exception as exc:  # noqa: BLE001
                result = "Ошибка: " + explain_error(exc)
            self._safe_after(self.refresh_table)
            self._safe_after(lambda: messagebox.showinfo("Тест аккаунта", result))

        fut.add_done_callback(done)

    def delete_account(self, account_id: int) -> None:
        if not messagebox.askyesno(t("accounts.delete_title"), t("accounts.delete_confirm")):
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
        shown_age = effective_account_age_days(account)
        age_entry = field("Возраст аккаунта, дней (до 7 — лимит 10, до 30 — 20)", str(shown_age))
        hint(
            form,
            f"Доступный лимит сейчас: {InviterEngine.get_safe_daily_limit(account)} из заданных {account.get('daily_limit', 40)}.",
            padx=8,
        )
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
        status_codes = ("active", "inactive", "flood", "restricted", "unauthorized", "error")
        status_labels = {
            "active": t("status.ready"),
            "inactive": t("status.inactive"),
            "flood": t("status.flood"),
            "restricted": t("status.restricted"),
            "unauthorized": t("status.unauthorized"),
            "error": t("status.error"),
        }
        statuses = {status_labels[code]: code for code in status_codes}
        status_menu = ctk.CTkOptionMenu(form, values=[status_labels[code] for code in status_codes])
        status_menu.set(status_labels.get(account.get("status"), t("status.inactive")))
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
                age_text = age_entry.get().strip() or "0"
                if int(age_text) != shown_age:  # keep derived age automatic unless the user changed it
                    payload["account_age_days"] = int(age_text)
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
            text=t("accounts.save_changes"),
            command=save,
            corner_radius=12,
            height=36,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color=COLORS["accent"],
            hover_color=COLORS["warning"],
            text_color=COLORS["text"],
        ).pack(pady=14)
        style_tree(modal)
