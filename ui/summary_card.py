"""Opt-in settings card: short run summary to Telegram Saved Messages via one chosen account."""

from __future__ import annotations

import customtkinter as ctk

from core.summary import (
    MODE_DAILY,
    MODE_TASK,
    SUMMARY_ACCOUNT_KEY,
    SUMMARY_ENABLED_KEY,
    SUMMARY_MODE_KEY,
    build_summary_text,
    normalize_mode,
    send_to_saved,
)
from ui.dispatch import post_ui
from ui.hints import hint, wrap_label
from ui.i18n import get_language, t
from ui.theme import COLORS, FONTS, apply_button_role, style_card


class SummaryCard(ctk.CTkFrame):
    def __init__(self, master, get_db, run_coro):
        super().__init__(master, fg_color=COLORS["surface"], border_color=COLORS["border"], border_width=1)
        self.get_db = get_db
        self.run_coro = run_coro
        self._accounts: dict[str, int] = {}
        self._loading = True
        style_card(self, color=COLORS["accent"])
        self.title_label = ctk.CTkLabel(self, text="", font=FONTS["subhead"], anchor="w")
        self.title_label.pack(fill="x", padx=16, pady=(12, 4))
        self.enabled_var = ctk.BooleanVar(value=False)
        self.enable_check = ctk.CTkCheckBox(
            self, text="", variable=self.enabled_var, command=self._save, font=FONTS["body"]
        )
        self.enable_check.pack(anchor="w", padx=16, pady=(0, 6))
        self.hint_label = hint(self, "")
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(0, 6))
        self.mode_caption = ctk.CTkLabel(row, text="", font=FONTS["small"])
        self.mode_caption.pack(side="left")
        self.mode_var = ctk.StringVar()
        self.mode_menu = ctk.CTkOptionMenu(
            row, values=[""], variable=self.mode_var, width=170, height=30, command=lambda _v: self._save()
        )
        self.mode_menu.pack(side="left", padx=(6, 12))
        self.account_caption = ctk.CTkLabel(row, text="", font=FONTS["small"])
        self.account_caption.pack(side="left")
        self.account_var = ctk.StringVar()
        self.account_menu = ctk.CTkOptionMenu(
            row, values=[""], variable=self.account_var, width=170, height=30, command=lambda _v: self._save()
        )
        self.account_menu.pack(side="left", padx=6)
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=(0, 12))
        self.test_button = ctk.CTkButton(actions, text="", height=30, width=160, command=self._send_sample)
        apply_button_role(self.test_button, "secondary")
        self.test_button.pack(side="left")
        self.status_label = ctk.CTkLabel(
            actions, text="", font=FONTS["small"], text_color=COLORS["muted"], anchor="w", justify="left"
        )
        self.status_label.pack(side="left", padx=10, fill="x", expand=True)
        wrap_label(self.status_label)
        self.retranslate()
        self.load()

    # -- helpers -------------------------------------------------------------------------
    def _mode_labels(self) -> dict[str, str]:
        return {MODE_TASK: t("summary.card.mode_task"), MODE_DAILY: t("summary.card.mode_daily")}

    def _current_mode(self) -> str:
        for key, label in self._mode_labels().items():
            if label == self.mode_var.get():
                return key
        return MODE_TASK

    def _selected_account_id(self) -> int:
        return self._accounts.get(self.account_var.get(), 0)

    def _say(self, text: str, error: bool = False) -> None:
        self.status_label.configure(text=text, text_color=COLORS["error"] if error else COLORS["muted"])

    def retranslate(self) -> None:
        mode = self._current_mode() if self.mode_var.get() else MODE_TASK
        self.title_label.configure(text=t("summary.card.title"))
        self.enable_check.configure(text=t("summary.card.enable"))
        self.hint_label.configure(text=t("summary.card.hint"))
        self.mode_caption.configure(text=t("summary.card.mode"))
        self.account_caption.configure(text=t("summary.card.account"))
        self.test_button.configure(text=t("summary.card.test"))
        labels = self._mode_labels()
        self.mode_menu.configure(values=list(labels.values()))
        self.mode_var.set(labels[mode])
        if not self._accounts:
            self.account_var.set(t("summary.card.no_accounts"))

    def set_accounts(self, accounts: list[dict], selected_id: int = 0) -> None:
        """Fill the account menu (phones are shown only in this local window, never in the summary)."""
        self._accounts = {str(item.get("phone") or item.get("id")): int(item["id"]) for item in accounts}
        names = list(self._accounts) or [t("summary.card.no_accounts")]
        self.account_menu.configure(values=names)
        chosen = next((name for name, key in self._accounts.items() if key == selected_id), None)
        self.account_var.set(chosen or names[0])

    # -- persistence ---------------------------------------------------------------------
    def load(self) -> None:
        db = self.get_db()

        async def read():
            values = await db.get_settings_map([SUMMARY_ENABLED_KEY, SUMMARY_MODE_KEY, SUMMARY_ACCOUNT_KEY])
            return values, await db.get_enabled_accounts()

        future = self.run_coro(read())

        def done(result):
            if result.cancelled() or result.exception():
                return

            def render():
                if not self.winfo_exists():
                    return
                values, accounts = result.result()
                try:
                    selected = int(values.get(SUMMARY_ACCOUNT_KEY) or 0)
                except ValueError:
                    selected = 0
                self._loading = True
                self.enabled_var.set(values.get(SUMMARY_ENABLED_KEY) == "1")
                self.mode_var.set(self._mode_labels()[normalize_mode(values.get(SUMMARY_MODE_KEY))])
                self.set_accounts(accounts, selected)
                self._loading = False

            post_ui(self, render)

        future.add_done_callback(done)

    def refresh_accounts(self) -> None:
        self.load()

    def _save(self) -> None:
        if self._loading:
            return
        enabled = self.enabled_var.get()
        account_id = self._selected_account_id()
        if enabled and not account_id:
            self.enabled_var.set(False)
            self._say(t("summary.card.pick"), error=True)
            return
        mode = self._current_mode()
        db = self.get_db()

        async def write():
            await db.set_setting(SUMMARY_ENABLED_KEY, "1" if enabled else "0")
            await db.set_setting(SUMMARY_MODE_KEY, mode)
            if account_id:
                await db.set_setting(SUMMARY_ACCOUNT_KEY, str(account_id))

        self.run_coro(write())
        self._say(t("summary.card.saved"))

    def _send_sample(self) -> None:
        account_id = self._selected_account_id()
        if not account_id:
            self._say(t("summary.card.pick"), error=True)
            return
        lang = get_language()
        text = build_summary_text({"sent": 12, "added": 3, "errors": 1, "skipped": 2}, lang=lang, outcome="completed")
        text += "\n" + t("summary.sample", lang)
        db = self.get_db()
        self.test_button.configure(state="disabled")
        future = self.run_coro(send_to_saved(db, account_id, text))

        def done(result):
            def render():
                if not self.winfo_exists():
                    return
                self.test_button.configure(state="normal")
                failed = result.cancelled() or result.exception() is not None
                self._say(t("summary.card.failed" if failed else "summary.card.sent"), error=failed)

            post_ui(self, render)

        future.add_done_callback(done)
