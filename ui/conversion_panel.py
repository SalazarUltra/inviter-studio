"""Dashboard section «Конверсия»: invites and messages by campaign and period, with a per-account table."""

from __future__ import annotations

import time
from datetime import datetime

import customtkinter as ctk

from core.conversion import PERIODS, conversion_report, format_rate
from ui.dispatch import post_ui
from ui.hints import tip
from ui.theme import COLORS, FONTS, apply_button_role

ALL_CAMPAIGNS = "Все кампании"
AUTO_REFRESH_SECONDS = 30
ACCOUNT_HEADERS = (
    ("Аккаунт", 0),
    ("Успешно", 1),
    ("Пропущено", 2),
    ("Ошибки", 3),
    ("Уже в чате", 4),
    ("Отправлено", 5),
    ("Приватных", 6),
    ("Неясно", 7),
    ("Ответили", 8),
    ("Вторых", 9),
)


def share(count: int, value) -> str:
    """«12 · 57.1%»."""
    return f"{count} · {format_rate(value)}"


class ConversionPanel(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color=COLORS["surface"], corner_radius=14)
        self.app = app
        self.db = None
        self._campaigns = {ALL_CAMPAIGNS: None}
        self._selected_id = "current"  # «current» follows the open campaign; None — all; int — a fixed one
        self._loading = False
        self._rerun = False
        self._last_refresh = 0.0
        self._follow_signature = None
        self._build()
        self.reload()

    # -- layout -----------------------------------------------------------------------------------------
    def _build(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(16, 6))
        ctk.CTkLabel(header, text="Конверсия", font=FONTS["subhead"]).pack(side="left")
        self.refresh_button = ctk.CTkButton(header, text="Обновить", width=100, height=28, command=self.reload)
        apply_button_role(self.refresh_button, "secondary")
        self.refresh_button.pack(side="right")
        self.period_var = ctk.StringVar(value=PERIODS["7d"])
        self.period_menu = ctk.CTkOptionMenu(
            header,
            variable=self.period_var,
            values=list(PERIODS.values()),
            command=lambda _value: self.refresh(),
            width=120,
            height=28,
        )
        self.period_menu.pack(side="right", padx=(0, 8))
        self.campaign_var = ctk.StringVar(value="")
        self.campaign_menu = ctk.CTkOptionMenu(
            header,
            variable=self.campaign_var,
            values=[ALL_CAMPAIGNS],
            command=self._campaign_chosen,
            width=230,
            height=28,
        )
        self.campaign_menu.pack(side="right", padx=(0, 8))
        tip(self.campaign_menu, "Кампания для отчёта. «Все кампании» — суммарно по всей программе.")
        tip(
            self.period_menu,
            "Период считается по времени итога: когда человек добавлен/пропущен, сообщение отправлено.",
        )
        self.note = ctk.CTkLabel(
            self, text="", font=FONTS["small"], text_color=COLORS["muted"], anchor="w", justify="left", wraplength=900
        )
        self.note.pack(fill="x", padx=18, pady=(0, 8))

        self.invites_title = ctk.CTkLabel(self, text="Приглашения", font=FONTS["label"], text_color=COLORS["muted"])
        self.invites_title.pack(anchor="w", padx=18)
        self.invites_row = ctk.CTkFrame(self, fg_color="transparent")
        self.invites_row.pack(fill="x", padx=18, pady=(4, 10))
        self.invites_row.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="inv")
        self.invite_tiles = {
            key: self._tile(self.invites_row, column, title)
            for column, (key, title) in enumerate(
                (("success", "Успешно"), ("skipped", "Пропущено"), ("errors", "Ошибки"), ("already_in", "Уже в чате"))
            )
        }
        self.invites_note = ctk.CTkLabel(self, text="", font=FONTS["small"], text_color=COLORS["muted"], anchor="w")
        self.invites_note.pack(fill="x", padx=18, pady=(0, 10))

        self.messages_title = ctk.CTkLabel(self, text="Сообщения", font=FONTS["label"], text_color=COLORS["muted"])
        self.messages_title.pack(anchor="w", padx=18)
        self.messages_row = ctk.CTkFrame(self, fg_color="transparent")
        self.messages_row.pack(fill="x", padx=18, pady=(4, 10))
        self.messages_row.grid_columnconfigure((0, 1, 2, 3, 4), weight=1, uniform="msg")
        self.message_tiles = {
            key: self._tile(self.messages_row, column, title)
            for column, (key, title) in enumerate(
                (
                    ("sent", "Отправлено"),
                    ("private", "Приватные"),
                    ("uncertain", "Неясно"),
                    ("replies", "Ответили"),
                    ("followup_sent", "Вторых отправлено"),
                )
            )
        }
        self.messages_note = ctk.CTkLabel(self, text="", font=FONTS["small"], text_color=COLORS["muted"], anchor="w")
        self.messages_note.pack(fill="x", padx=18, pady=(0, 10))

        ctk.CTkLabel(self, text="По аккаунтам", font=FONTS["label"], text_color=COLORS["muted"]).pack(
            anchor="w", padx=18
        )
        self.table = ctk.CTkFrame(self, fg_color="transparent")
        self.table.pack(fill="x", padx=18, pady=(4, 6))
        self.footer = ctk.CTkLabel(self, text="", font=FONTS["small"], text_color=COLORS["subtle"], anchor="w")
        self.footer.pack(fill="x", padx=18, pady=(0, 14))

    def _tile(self, parent, column, title):
        card = ctk.CTkFrame(
            parent, fg_color=COLORS["bg"], corner_radius=10, border_width=1, border_color=COLORS["border"]
        )
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 4, 4))
        ctk.CTkLabel(card, text=title, font=FONTS["small"], text_color=COLORS["muted"]).pack(
            anchor="w", padx=12, pady=(8, 0)
        )
        value = ctk.CTkLabel(card, text="—", font=FONTS["subhead"])
        value.pack(anchor="w", padx=12, pady=(0, 8))
        return value

    # -- data -------------------------------------------------------------------------------------------
    def reload(self):
        """Refresh the campaign list, then the report."""
        db = self.app.db
        future = self.app.run_coro(db.get_campaigns())

        def done(result):
            def render():
                if not self.winfo_exists() or result.cancelled() or result.exception():
                    return
                self._campaigns = {ALL_CAMPAIGNS: None} | {
                    f"{row['id']} · {row['name']}": row["id"] for row in result.result()
                }
                self.campaign_menu.configure(values=list(self._campaigns))
                self.refresh()

            post_ui(self, render)

        future.add_done_callback(done)

    def _campaign_chosen(self, name):
        self._selected_id = self._campaigns.get(name)
        self.refresh()

    def _current_campaign(self):
        return self.app.db.campaign_id if self._selected_id == "current" else self._selected_id

    def refresh(self):
        if self._loading:
            self._rerun = True  # the choice changed while a report was being built
            return
        campaign_id = self._current_campaign()
        period = next((key for key, title in PERIODS.items() if title == self.period_var.get()), "7d")
        self.campaign_var.set(
            next((name for name, key in self._campaigns.items() if key == campaign_id), ALL_CAMPAIGNS)
        )
        self._loading = True
        self.refresh_button.configure(state="disabled")
        db = self.app.db
        future = self.app.run_coro(conversion_report(db, campaign_id, period))

        def done(result):
            def render():
                self._loading = False
                if not self.winfo_exists():
                    return
                self.refresh_button.configure(state="normal")
                if self._rerun:
                    self._rerun = False
                    self.refresh()
                    return
                if result.cancelled() or result.exception():
                    self.footer.configure(
                        text="Не удалось построить отчёт: "
                        + (str(result.exception()) if result.exception() else "операция отменена"),
                        text_color=COLORS["error"],
                    )
                    return
                self.show(result.result())

            post_ui(self, render)

        future.add_done_callback(done)

    def tick(self):
        """Called on every dashboard refresh: follow the open campaign and refresh now and then."""
        if not self.winfo_exists():
            return
        signature = self.app.db.campaign_id
        changed = self._selected_id == "current" and signature != self._follow_signature
        if changed or time.monotonic() - self._last_refresh > AUTO_REFRESH_SECONDS:
            self.refresh()

    # -- rendering --------------------------------------------------------------------------------------
    def show(self, report):
        self._last_refresh = time.monotonic()
        self._follow_signature = self.app.db.campaign_id
        invites, messages = report["invites"], report["messages"]
        kind = {"invite": "приглашения", "messages": "личные сообщения"}.get(report["kind"], "приглашения и сообщения")
        period = PERIODS.get(report["period"], report["period"]).lower()
        self.note.configure(text=f"{report['campaign_name']} · {kind} · период: {period}")
        for key, label in self.invite_tiles.items():
            label.configure(text=share(invites[key], invites["rates"][key]))
        self.invites_note.configure(
            text=(
                f"Итог по {invites['total']} людям · в журнале приглашений чата: {invites['ledger']}"
                if invites["total"] or invites["ledger"]
                else "Нет приглашений за период"
                + (" (кампания в режиме сообщений)" if report["kind"] == "messages" else "")
            )
        )
        rates = messages["rates"]
        for key, label in self.message_tiles.items():
            if key == "replies":
                label.configure(text=share(messages["replies"], rates["replies"]))
            elif key == "followup_sent":
                label.configure(text=share(messages["followup_sent"], rates["followup_sent"]))
            else:
                label.configure(text=share(messages[key], rates[key]))
        self.messages_note.configure(
            text=(
                f"Попыток: {messages['attempts']} · доля ответивших — от отправленных, вторых — от ответивших · "
                f"ждут отправки второго: {messages['followup_ready']}"
                if messages["attempts"] or messages["replies"]
                else "Нет сообщений за период"
                + (" (кампания в режиме приглашений)" if report["kind"] == "invite" else "")
            )
        )
        self._fill_table(report["accounts"])
        self.footer.configure(
            text=(
                "Обновлено " + datetime.now().strftime("%H:%M:%S") + ". Пропущено — закрытые профили и не найденные "
                "никнеймы; по аккаунтам пропуски и ошибки берутся из журнала."
            ),
            text_color=COLORS["subtle"],
        )

    def _fill_table(self, accounts):
        for widget in self.table.winfo_children():
            widget.destroy()
        for title, column in ACCOUNT_HEADERS:
            ctk.CTkLabel(self.table, text=title, font=FONTS["label"], text_color=COLORS["muted"], anchor="w").grid(
                row=0, column=column, sticky="w", padx=(0, 14), pady=(0, 4)
            )
        if not accounts:
            ctk.CTkLabel(
                self.table, text="За выбранный период действий аккаунтов нет", font=FONTS["small"], anchor="w"
            ).grid(row=1, column=0, columnspan=len(ACCOUNT_HEADERS), sticky="w")
            return
        for index, account in enumerate(accounts, 1):
            invites, messages = account["invites"], account["messages"]
            values = (
                account["phone"],
                share(invites["success"], invites["rates"]["success"]),
                share(invites["skipped"], invites["rates"]["skipped"]),
                share(invites["errors"], invites["rates"]["errors"]),
                share(invites["already_in"], invites["rates"]["already_in"]),
                share(messages["sent"], messages["rates"]["sent"]),
                share(messages["private"], messages["rates"]["private"]),
                share(messages["uncertain"], messages["rates"]["uncertain"]),
                share(messages["replies"], messages["rates"]["replies"]),
                share(messages["followup_sent"], messages["rates"]["followup_sent"]),
            )
            for column, text in enumerate(values):
                ctk.CTkLabel(self.table, text=text, font=FONTS["small"], anchor="w").grid(
                    row=index, column=column, sticky="w", padx=(0, 14), pady=1
                )
