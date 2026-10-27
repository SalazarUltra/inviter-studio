from __future__ import annotations

import webbrowser
from tkinter import messagebox

import customtkinter as ctk

from core.ai_pricing import RATES_AS_OF, format_usd, resolve_stored_cost
from core.ai_providers import CATALOG_CHECKED, PROVIDERS, validate_connection
from core.ai_writer import AIWriter
from core.user_feedback import explain_error
from ui.campaigns import show_report
from ui.components import empty_state, page_intro, set_page_intro
from ui.dispatch import post_ui
from ui.entry_utils import paste_text, setup_entry
from ui.hints import hint, tip
from ui.i18n import t
from ui.theme import COLORS, FONTS, apply_button_role, init_fonts, style_card, style_tree


class TabAISettings(ctk.CTkFrame):
    PROVIDERS = PROVIDERS

    def __init__(self, parent, db, run_coro):
        super().__init__(parent, fg_color=COLORS["bg"])
        init_fonts()
        self.db = db
        self.run_coro = run_coro
        self._preview_inflight = False
        self._save_inflight = False
        self._editing_id = None
        self._key_visible = False
        self.provider_var = ctk.StringVar(value="openai")
        self.provider_label_var = ctk.StringVar(value=self.PROVIDERS["openai"]["name"])
        self.model_var = ctk.StringVar(value=self.PROVIDERS["openai"]["models"][0])
        self.cards_frame: ctk.CTkScrollableFrame | None = None
        self.total_label: ctk.CTkLabel | None = None
        self._build_ui()
        self.refresh_configs()

    def _safe_after(self, callback) -> None:
        post_ui(self, callback)

    def _build_ui(self) -> None:
        self.intro = page_intro(self, t("ai.title"), t("ai.desc"), t("ai.kicker"))
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        main.grid_columnconfigure(0, weight=1, uniform="ai")
        main.grid_columnconfigure(1, weight=1, uniform="ai")
        main.grid_rowconfigure(0, weight=1)
        left = ctk.CTkFrame(main, fg_color=COLORS["surface"], corner_radius=14)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        ctk.CTkLabel(left, text="Подключить провайдера", font=FONTS["subhead"]).pack(anchor="w", padx=20, pady=(18, 4))
        hint(left, "1. Получите ключ → 2. Сохраните → 3. Активируйте", padx=20)
        self.add_button = ctk.CTkButton(left, text="Сохранить подключение", height=42, command=self._add_config)
        apply_button_role(self.add_button, "primary")
        self.add_button.pack(side="bottom", fill="x", padx=20, pady=(8, 18))
        self.status_label = hint(left, "Сохранение не выполняет запрос к AI и не проверяет ключ.", padx=20)
        self.status_label.pack_configure(side="bottom")
        form = ctk.CTkScrollableFrame(left, fg_color="transparent", corner_radius=0)
        form.pack(fill="both", expand=True, padx=12, pady=(0, 0))

        def label(text):
            ctk.CTkLabel(form, text=text, font=FONTS["small"], text_color=COLORS["muted"]).pack(
                anchor="w", padx=8, pady=(8, 5)
            )

        label("1. Провайдер")
        self.provider_menu = ctk.CTkOptionMenu(
            form,
            values=[info["name"] for info in self.PROVIDERS.values()],
            variable=self.provider_label_var,
            command=self._on_provider_change,
            height=40,
        )
        self.provider_menu.pack(fill="x", padx=8, pady=(0, 8))
        links = ctk.CTkFrame(form, fg_color="transparent")
        links.pack(fill="x", padx=8, pady=(0, 8))
        links.grid_columnconfigure((0, 1), weight=1, uniform="links")
        for column, (title, target) in enumerate((("Получить ключ ↗", "keys_url"), ("Модели ↗", "docs_url"))):
            button = ctk.CTkButton(
                links, text=title, width=0, height=30, command=lambda key=target: self._open_provider_page(key)
            )
            apply_button_role(button, "secondary")
            button.grid(row=0, column=column, sticky="ew", padx=(0, 6))
        label("2. API-ключ AI-провайдера")
        self.key_entry = ctk.CTkEntry(
            form, placeholder_text=self.PROVIDERS["openai"]["placeholder"], show="*", height=40
        )
        self.key_entry.pack(fill="x", padx=8, pady=(0, 12))
        key_actions = ctk.CTkFrame(form, fg_color="transparent")
        key_actions.pack(fill="x", padx=8, pady=(0, 10))
        self.paste_button = ctk.CTkButton(
            key_actions, text="Вставить ключ", width=120, height=30, command=lambda: paste_text(self.key_entry)
        )
        self.paste_button.pack(side="left", padx=(0, 6))
        self.show_key_button = ctk.CTkButton(
            key_actions, text="Показать", width=90, height=30, command=self._toggle_key
        )
        self.show_key_button.pack(side="left")
        for button in (self.paste_button, self.show_key_button):
            apply_button_role(button, "secondary")
        label("3. Модель")
        self.model_menu = ctk.CTkComboBox(
            form, values=self.PROVIDERS["openai"]["models"], variable=self.model_var, height=40
        )
        self.model_menu.pack(fill="x", padx=8, pady=(0, 8))
        label("4. Название подключения для себя")
        self.name_entry = ctk.CTkEntry(form, height=40, placeholder_text="Например, основной AI")
        self.name_entry.pack(fill="x", padx=8, pady=(0, 8))
        setup_entry(self.name_entry)
        self.provider_steps = hint(form, self.PROVIDERS["openai"]["steps"], padx=8)
        self.model_advice = hint(form, self.PROVIDERS["openai"]["advice"], padx=8)
        hint(
            form,
            f"Подборка проверена {CATALOG_CHECKED}. Можно вписать другой ID текстовой модели вручную. Доступ зависит от вашего API-аккаунта.",
            padx=8,
        )
        self.cancel_edit_button = ctk.CTkButton(
            form, text="Отменить редактирование", height=30, command=self._reset_form
        )
        hint(
            form,
            "Здесь нужен ключ AI-провайдера, а не Telegram API ID / API Hash. После сохранения нажмите «Активировать» справа. Проверка с примером — отдельный запрос по тарифу провайдера.",
            padx=8,
        )
        tip(
            self.key_entry,
            "Ключ выбранного AI-провайдера. Проверочный запрос может быть платным; сообщения в Telegram не отправляются.",
        )
        setup_entry(self.key_entry)
        right = ctk.CTkFrame(main, fg_color=COLORS["surface"], corner_radius=14)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        ctk.CTkLabel(right, text="Мои подключения", font=FONTS["subhead"]).pack(anchor="w", padx=20, pady=(18, 4))
        hint(right, "Сохраните → проверьте пример → активируйте для прогрева.", padx=20)
        self.total_label = ctk.CTkLabel(
            right,
            text="Использовано: 0 токенов",
            font=FONTS["small"],
            text_color=COLORS["muted"],
            wraplength=300,
            justify="left",
        )
        self.total_label.pack(side="bottom", anchor="w", padx=20, pady=(8, 18))
        self.cards_frame = ctk.CTkScrollableFrame(right, fg_color="transparent")
        self.cards_frame.pack(fill="both", expand=True, padx=12, pady=(0, 4))

    def _on_provider_change(self, provider: str) -> None:
        provider = next((key for key, info in self.PROVIDERS.items() if info["name"] == provider), provider)
        previous = self.provider_var.get()
        self.provider_var.set(provider)
        info = self.PROVIDERS[provider]
        self.provider_label_var.set(info["name"])
        self.model_var.set(info["models"][0])
        self.model_menu.configure(values=info["models"])
        self.key_entry.configure(placeholder_text=info["placeholder"])
        self.provider_steps.configure(text=info["steps"])
        self.model_advice.configure(text=info["advice"])
        if previous != provider:
            self.key_entry.delete(0, "end")
            self._hide_key()
            self.status_label.configure(text="Провайдер изменён. Вставьте его API-ключ заново.")

    def _open_provider_page(self, target: str) -> None:
        if not webbrowser.open(self.PROVIDERS[self.provider_var.get()][target]):
            messagebox.showinfo("Открыть сайт", self.PROVIDERS[self.provider_var.get()][target])

    def _hide_key(self) -> None:
        self._key_visible = False
        self.key_entry.configure(show="*")
        self.show_key_button.configure(text="Показать")

    def _toggle_key(self) -> None:
        if not self._key_visible:
            self._key_visible = True
            self.key_entry.configure(show="")
            self.show_key_button.configure(text="Скрыть")
        else:
            self._hide_key()

    def _reset_form(self) -> None:
        self._editing_id = None
        self.name_entry.delete(0, "end")
        self.key_entry.delete(0, "end")
        self._hide_key()
        self.add_button.configure(text="Сохранить подключение")
        self.cancel_edit_button.pack_forget()
        self.status_label.configure(text="Сохранение не выполняет запрос к AI и не проверяет ключ.")

    def _edit_config(self, config: dict) -> None:
        if self._save_inflight:
            return
        self._reset_form()
        self._on_provider_change(config["provider"])
        self._editing_id = config["id"]
        self.name_entry.insert(0, config["name"])
        self.model_var.set(config["model"])
        self.key_entry.insert(0, config["api_key"])
        self.add_button.configure(text="Сохранить изменения")
        self.cancel_edit_button.pack(fill="x", padx=8, pady=8)
        self.status_label.configure(text="Редактирование слева. Измените модель или ключ и сохраните.")
        self.name_entry.focus_set()

    def retranslate(self) -> None:
        set_page_intro(
            self.intro,
            title=t("ai.title"),
            description=t("ai.desc"),
            kicker=t("ai.kicker"),
        )

    def _add_config(self) -> None:
        if self._save_inflight:
            return
        name = self.name_entry.get().strip()
        provider = self.provider_var.get().strip()
        model = self.model_var.get().strip()
        api_key = self.key_entry.get().strip()
        try:
            name, api_key, model = validate_connection(name, provider, api_key, model)
        except ValueError as exc:
            messagebox.showwarning("Проверьте подключение", explain_error(exc))
            return
        self._save_inflight = True
        self._set_form_state("disabled")
        operation = (
            self.db.update_ai_config(self._editing_id, name, provider, api_key, model)
            if self._editing_id is not None
            else self.db.add_ai_config(name, provider, api_key, model)
        )
        fut = self.run_coro(self._safe_config_write(operation))

        def done(result):
            def render():
                self._save_inflight = False
                self._set_form_state("normal")
                if result.cancelled() or result.exception():
                    messagebox.showerror(
                        "AI-подключение",
                        "Не удалось сохранить подключение. Проверьте доступ к папке данных и повторите. Введённые поля сохранены на экране.",
                    )
                    return
                self._reset_form()
                self.status_label.configure(
                    text="Сохранено. Проверьте пример и нажмите «Активировать» справа. Изменения уже активного подключения используются при следующем запуске прогрева."
                )
                self.refresh_configs()

            self._safe_after(render)

        fut.add_done_callback(done)

    @staticmethod
    async def _safe_config_write(operation):
        try:
            return await operation
        except Exception:
            # App.run_coro also reports failures globally; never pass storage
            # exceptions containing a credential to that error reporter.
            raise RuntimeError("Не удалось сохранить изменения AI-подключения. Проверьте папку данных.") from None

    def _set_form_state(self, state: str) -> None:
        for widget in (
            self.name_entry,
            self.provider_menu,
            self.model_menu,
            self.key_entry,
            self.add_button,
            self.paste_button,
            self.show_key_button,
            self.cancel_edit_button,
        ):
            widget.configure(state=state)

    def refresh_configs(self) -> None:
        fut = self.run_coro(self.db.get_ai_configs())

        def done(f):
            try:
                configs = f.result()
            except Exception:
                self._safe_after(
                    lambda: self.status_label.configure(
                        text="Не удалось загрузить подключения. Проверьте папку данных и откройте вкладку снова."
                    )
                )
                return
            self._safe_after(lambda: self._render_configs(configs))

        fut.add_done_callback(done)

    def _render_configs(self, configs: list[dict]) -> None:
        if not self.cards_frame or not self.total_label:
            return
        for w in self.cards_frame.winfo_children():
            w.destroy()

        if not configs:
            empty_state(
                self.cards_frame,
                "Выберите свой AI",
                "Сохраните подключение слева, затем активируйте его для AI-прогрева. Для обычной рассылки API не требуется.",
                "ai",
            )
        total_tokens = 0
        total_cost = 0.0
        any_estimate = False
        any_unknown = False
        for cfg in configs:
            total_tokens += int(cfg.get("total_tokens") or 0)
            amount, source = resolve_stored_cost(cfg)
            if amount is None:
                any_unknown = True
            else:
                total_cost += amount
                if source == "estimate":
                    any_estimate = True
            self._build_config_card(self.cards_frame, cfg)
        style_tree(self.cards_frame)
        if any_unknown and total_cost <= 0:
            cost_text = t("ai.cost_unknown")
        else:
            cost_text = format_usd(total_cost, estimated=any_estimate, unknown=t("ai.cost_unknown"))
            if any_estimate:
                cost_text = f"{cost_text} ({t('ai.cost_estimate_note', as_of=RATES_AS_OF)})"
        self.total_label.configure(text=t("ai.total_usage", tokens=f"{total_tokens:,}", cost=cost_text))

    def _build_config_card(self, parent, config: dict):
        is_active = bool(config.get("is_active"))
        tokens = int(config.get("total_tokens", 0) or 0)
        amount, source = resolve_stored_cost(config)
        cost_value = float(amount or 0.0)
        if amount is None:
            cost_label = t("ai.cost_unknown")
        else:
            cost_label = format_usd(amount, estimated=source == "estimate", unknown=t("ai.cost_unknown"))
            if source == "estimate":
                cost_label = f"{cost_label} ({t('ai.cost_estimate_note', as_of=RATES_AS_OF)})"
        card = ctk.CTkFrame(
            parent,
            fg_color=COLORS["surface"],
            border_color=COLORS["accent"] if is_active else COLORS["border"],
            border_width=2 if is_active else 1,
            corner_radius=12,
        )
        card.pack(fill="x", pady=(0, 8))
        style_card(card, color=COLORS["border"])
        header = ctk.CTkFrame(card, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(10, 4))
        status_dot = "АКТИВНЫЙ" if is_active else "СОХРАНЁН"
        ctk.CTkLabel(header, text=config["name"], font=FONTS["subhead"], text_color=COLORS["text"]).pack(side="left")
        ctk.CTkLabel(
            header, text=status_dot, font=FONTS["label"], text_color=COLORS["accent"] if is_active else COLORS["muted"]
        ).pack(side="right")
        provider_name = self.PROVIDERS.get(config["provider"], {}).get("name", config["provider"])
        hint(card, f"{provider_name}  •  {config['model']}", padx=12)
        if config["model"] not in self.PROVIDERS.get(config["provider"], {}).get("models", []):
            hint(
                card,
                "Модель вне текущей подборки. Проверьте её доступность у провайдера или выберите новую через «Изменить».",
                padx=12,
            )

        stats = ctk.CTkFrame(card, fg_color=COLORS["bg"], corner_radius=12)
        stats.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(
            stats,
            text=t("ai.tokens", tokens=f"{tokens:,}"),
            font=FONTS["small"],
            text_color=COLORS["muted"],
        ).pack(anchor="w", padx=8, pady=(6, 0))
        cost_color = (
            COLORS["success"] if cost_value < 0.01 else COLORS["warning"] if cost_value < 0.1 else COLORS["error"]
        )
        hint(stats, t("ai.cost", cost=cost_label), padx=8).configure(text_color=cost_color)

        btn_row = ctk.CTkFrame(card, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkButton(
            card,
            text="Проверить AI: 1 запрос по тарифу",
            height=30,
            command=lambda cfg=config: self._preview_config(cfg),
        ).pack(fill="x", padx=12, pady=(0, 8))
        if not is_active:
            ctk.CTkButton(
                btn_row,
                text="Активировать",
                width=100,
                height=30,
                corner_radius=12,
                fg_color=COLORS["surface"],
                hover_color=COLORS["bg"],
                text_color=COLORS["accent"],
                border_color=COLORS["accent"],
                border_width=1,
                font=FONTS["small"],
                command=lambda cid=config["id"]: self._activate_config(cid),
            ).pack(side="left", padx=(0, 6))
        else:
            ctk.CTkLabel(btn_row, text="Активная", font=FONTS["small"], text_color=COLORS["success"]).pack(
                side="left", padx=(0, 6)
            )
        ctk.CTkButton(
            btn_row, text="Изменить", width=80, height=30, command=lambda cfg=config: self._edit_config(cfg)
        ).pack(side="left", padx=(0, 6))
        ctk.CTkButton(
            btn_row,
            text="Удалить",
            width=75,
            height=30,
            corner_radius=12,
            fg_color=COLORS["surface"],
            hover_color=COLORS["bg"],
            text_color=COLORS["error"],
            border_color=COLORS["error"],
            border_width=1,
            font=FONTS["small"],
            command=lambda cid=config["id"]: self._delete_config(cid),
        ).pack(side="right")

    def _activate_config(self, config_id: int) -> None:
        fut = self.run_coro(self._safe_config_write(self.db.set_active_ai_config(config_id)))
        fut.add_done_callback(
            lambda result: self._configuration_done(
                result, "Подключение активно. Запустите или перезапустите прогрев, чтобы использовать его."
            )
        )

    def _configuration_done(self, result, success: str) -> None:
        def render():
            if result.cancelled() or result.exception():
                messagebox.showerror(
                    "AI-подключение", "Не удалось изменить подключение. Проверьте папку данных и повторите."
                )
                return
            self.status_label.configure(text=success)
            self.refresh_configs()

        self._safe_after(render)

    def _preview_config(self, config):
        if self._preview_inflight:
            messagebox.showinfo("Проверка AI", "Запрос уже выполняется. Дождитесь результата.")
            return
        context = ctk.CTkInputDialog(
            title="Предпросмотр AI",
            text="Введите тему или контекст. Будет выполнен один запрос к провайдеру\n"
            "с учётом его тарифа. В Telegram ничего не отправляется.",
        ).get_input()
        if not context:
            return
        writer = AIWriter(config, db=self.db)
        self._preview_inflight = True
        future = self.run_coro(writer.generate_message(context=context[:2000], memory_enabled=False))

        def done(result):
            def render():
                self._preview_inflight = False
                if result.cancelled():
                    return
                if result.exception():
                    messagebox.showerror("Проверка AI", str(result.exception()))
                else:
                    stats = writer.get_session_stats()
                    lines = [
                        result.result(),
                        "",
                        t(
                            "ai.preview_tokens",
                            total=int(stats.get("tokens") or 0),
                            input=int(stats.get("last_input_tokens") or 0),
                            output=int(stats.get("last_output_tokens") or 0),
                        ),
                    ]
                    reasoning = int(stats.get("last_reasoning_tokens") or 0)
                    if reasoning:
                        lines.append(t("ai.preview_reasoning", reasoning=reasoning))
                    cost_text = stats.get("last_cost_formatted") or t("ai.cost_unknown")
                    if stats.get("last_cost_source") == "estimate":
                        cost_text = f"{cost_text} ({t('ai.cost_estimate_note', as_of=RATES_AS_OF)})"
                    elif stats.get("last_cost_source") == "provider":
                        cost_text = cost_text  # billed by provider
                    lines.append(t("ai.preview_cost", cost=cost_text or t("ai.cost_unknown")))
                    if reasoning or int(stats.get("tokens") or 0) > 400:
                        lines.append(t("ai.preview_why_tokens"))
                    lines.append(t("ai.preview_not_sent"))
                    show_report(self, "Пример AI-сообщения", "\n".join(lines))
                    self.refresh_configs()

            self._safe_after(render)

        future.add_done_callback(done)

    def _delete_config(self, config_id: int) -> None:
        if self._save_inflight:
            return
        if not messagebox.askyesno("Подтверждение", "Удалить AI конфигурацию?"):
            return
        fut = self.run_coro(self._safe_config_write(self.db.delete_ai_config(config_id)))
        if self._editing_id == config_id:
            self._reset_form()
        fut.add_done_callback(
            lambda result: self._configuration_done(
                result, "Подключение удалено. При необходимости активируйте другое справа."
            )
        )
