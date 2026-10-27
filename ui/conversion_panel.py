"""Dashboard section «Конверсия»: invites and messages by campaign and period, with a per-account table."""

from __future__ import annotations

import time
from datetime import datetime

import customtkinter as ctk

from core.conversion import PERIODS, conversion_report, format_rate
from ui.dispatch import post_ui
from ui.hints import tip
from ui.i18n import t
from ui.theme import COLORS, FONTS, apply_button_role

AUTO_REFRESH_SECONDS = 30
# (i18n key, column) of the per-account table header.
ACCOUNT_HEADERS = (
    ("conv.col.account", 0),
    ("conv.success", 1),
    ("conv.skipped", 2),
    ("conv.errors", 3),
    ("conv.already_in", 4),
    ("conv.sent", 5),
    ("conv.col.private", 6),
    ("conv.uncertain", 7),
    ("conv.replies", 8),
    ("conv.col.followup", 9),
)
INVITE_TILES = (
    ("success", "conv.success"),
    ("skipped", "conv.skipped"),
    ("errors", "conv.errors"),
    ("already_in", "conv.already_in"),
)
MESSAGE_TILES = (
    ("sent", "conv.sent"),
    ("private", "conv.private"),
    ("uncertain", "conv.uncertain"),
    ("replies", "conv.replies"),
    ("followup_sent", "conv.followup_sent"),
)


def all_campaigns_label() -> str:
    return t("conv.all_campaigns")


def period_title(key: str) -> str:
    """Localized period name; periods without a translation keep the core (Russian) title."""
    title = t(f"conv.period.{key}")
    return title if title != f"conv.period.{key}" else PERIODS.get(key, key)


def period_titles() -> dict[str, str]:
    return {key: period_title(key) for key in PERIODS}


def share(count: int, value) -> str:
    """«12 · 57.1%»."""
    return f"{count} · {format_rate(value)}"


class ConversionPanel(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color=COLORS["surface"], corner_radius=14)
        self.app = app
        self.db = None
        self._campaigns = {all_campaigns_label(): None}
        self._selected_id = "current"  # «current» follows the open campaign; None — all; int — a fixed one
        self._loading = False
        self._rerun = False
        self._last_refresh = 0.0
        self._follow_signature = None
        self._report = None  # last rendered report, re-rendered after a language switch
        self._build()
        self.reload()

    # -- layout -----------------------------------------------------------------------------------------
    def _build(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(16, 6))
        self.title_label = ctk.CTkLabel(header, text=t("conv.title"), font=FONTS["subhead"])
        self.title_label.pack(side="left")
        self.refresh_button = ctk.CTkButton(header, text=t("conv.refresh"), width=100, height=28, command=self.reload)
        apply_button_role(self.refresh_button, "secondary")
        self.refresh_button.pack(side="right")
        self.period_var = ctk.StringVar(value=period_title("7d"))
        self.period_menu = ctk.CTkOptionMenu(
            header,
            variable=self.period_var,
            values=list(period_titles().values()),
            command=lambda _value: self.refresh(),
            width=120,
            height=28,
        )
        self.period_menu.pack(side="right", padx=(0, 8))
        self.campaign_var = ctk.StringVar(value="")
        self.campaign_menu = ctk.CTkOptionMenu(
            header,
            variable=self.campaign_var,
            values=[all_campaigns_label()],
            command=self._campaign_chosen,
            width=230,
            height=28,
        )
        self.campaign_menu.pack(side="right", padx=(0, 8))
        tip(self.campaign_menu, t("conv.campaign_tip"))
        tip(self.period_menu, t("conv.period_tip"))
        self.note = ctk.CTkLabel(
            self, text="", font=FONTS["small"], text_color=COLORS["muted"], anchor="w", justify="left", wraplength=900
        )
        self.note.pack(fill="x", padx=18, pady=(0, 8))

        self.invites_title = ctk.CTkLabel(self, text=t("conv.invites"), font=FONTS["label"], text_color=COLORS["muted"])
        self.invites_title.pack(anchor="w", padx=18)
        self.invites_row = ctk.CTkFrame(self, fg_color="transparent")
        self.invites_row.pack(fill="x", padx=18, pady=(4, 10))
        self.invites_row.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="inv")
        self.tile_captions = {}  # i18n key -> caption label, relabelled on a language switch
        self.invite_tiles = {
            key: self._tile(self.invites_row, column, title_key) for column, (key, title_key) in enumerate(INVITE_TILES)
        }
        self.invites_note = ctk.CTkLabel(self, text="", font=FONTS["small"], text_color=COLORS["muted"], anchor="w")
        self.invites_note.pack(fill="x", padx=18, pady=(0, 10))

        self.messages_title = ctk.CTkLabel(
            self, text=t("conv.messages"), font=FONTS["label"], text_color=COLORS["muted"]
        )
        self.messages_title.pack(anchor="w", padx=18)
        self.messages_row = ctk.CTkFrame(self, fg_color="transparent")
        self.messages_row.pack(fill="x", padx=18, pady=(4, 10))
        self.messages_row.grid_columnconfigure((0, 1, 2, 3, 4), weight=1, uniform="msg")
        self.message_tiles = {
            key: self._tile(self.messages_row, column, title_key)
            for column, (key, title_key) in enumerate(MESSAGE_TILES)
        }
        self.messages_note = ctk.CTkLabel(self, text="", font=FONTS["small"], text_color=COLORS["muted"], anchor="w")
        self.messages_note.pack(fill="x", padx=18, pady=(0, 10))

        self.accounts_title = ctk.CTkLabel(
            self, text=t("conv.by_account"), font=FONTS["label"], text_color=COLORS["muted"]
        )
        self.accounts_title.pack(anchor="w", padx=18)
        self.table = ctk.CTkFrame(self, fg_color="transparent")
        self.table.pack(fill="x", padx=18, pady=(4, 6))
        self.footer = ctk.CTkLabel(self, text="", font=FONTS["small"], text_color=COLORS["subtle"], anchor="w")
        self.footer.pack(fill="x", padx=18, pady=(0, 14))

    def _tile(self, parent, column, title_key):
        card = ctk.CTkFrame(
            parent, fg_color=COLORS["bg"], corner_radius=10, border_width=1, border_color=COLORS["border"]
        )
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 4, 4))
        caption = ctk.CTkLabel(card, text=t(title_key), font=FONTS["small"], text_color=COLORS["muted"])
        caption.pack(anchor="w", padx=12, pady=(8, 0))
        self.tile_captions.setdefault(title_key, []).append(caption)
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
                self._campaigns = {all_campaigns_label(): None} | {
                    f"{row['id']} · {row['name']}": row["id"] for row in result.result()
                }
                self.campaign_menu.configure(values=list(self._campaigns))
                self.refresh()

            post_ui(self, render)

        future.add_done_callback(done)

    def _campaign_chosen(self, name):
        self._selected_id = self._campaigns.get(name)
        self.refresh()

    def _period_key(self) -> str:
        return next((key for key, title in period_titles().items() if title == self.period_var.get()), "7d")

    def retranslate(self) -> None:
        """Relabel the panel after a language switch; the selected period and campaign are kept."""
        period = self._period_key()
        self.title_label.configure(text=t("conv.title"))
        self.refresh_button.configure(text=t("conv.refresh"))
        self.period_menu.configure(values=list(period_titles().values()))
        self.period_var.set(period_title(period))
        tip(self.campaign_menu, t("conv.campaign_tip"))
        tip(self.period_menu, t("conv.period_tip"))
        self.invites_title.configure(text=t("conv.invites"))
        self.messages_title.configure(text=t("conv.messages"))
        self.accounts_title.configure(text=t("conv.by_account"))
        for title_key, captions in self.tile_captions.items():
            for caption in captions:
                caption.configure(text=t(title_key))
        # Campaign names keep their text; only the «all campaigns» entry is localized, so reload the list.
        self.reload()
        if self._report is not None:
            self.show(self._report)

    def _current_campaign(self):
        return self.app.db.campaign_id if self._selected_id == "current" else self._selected_id

    def refresh(self):
        if self._loading:
            self._rerun = True  # the choice changed while a report was being built
            return
        campaign_id = self._current_campaign()
        period = self._period_key()
        self.campaign_var.set(
            next((name for name, key in self._campaigns.items() if key == campaign_id), all_campaigns_label())
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
                        text=t(
                            "conv.report_failed",
                            error=str(result.exception()) if result.exception() else t("conv.cancelled"),
                        ),
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
        self._report = report
        invites, messages = report["invites"], report["messages"]
        kind = t(f"conv.kind.{report['kind']}") if report["kind"] in ("invite", "messages") else t("conv.kind.both")
        period = period_title(report["period"]).lower()
        name = all_campaigns_label() if report["campaign_name"] == "Все кампании" else report["campaign_name"]
        self.note.configure(text=t("conv.note", campaign=name, kind=kind, period=period))
        for key, label in self.invite_tiles.items():
            label.configure(text=share(invites[key], invites["rates"][key]))
        self.invites_note.configure(
            text=(
                t("conv.invites_total", total=invites["total"], ledger=invites["ledger"])
                if invites["total"] or invites["ledger"]
                else t("conv.invites_none")
                + (t("conv.invites_none_messages_mode") if report["kind"] == "messages" else "")
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
                t("conv.messages_detail", attempts=messages["attempts"], ready=messages["followup_ready"])
                if messages["attempts"] or messages["replies"]
                else t("conv.messages_none")
                + (t("conv.messages_none_invite_mode") if report["kind"] == "invite" else "")
            )
        )
        self._fill_table(report["accounts"])
        self.footer.configure(
            text=t("conv.updated", time=datetime.now().strftime("%H:%M:%S")),
            text_color=COLORS["subtle"],
        )

    def _fill_table(self, accounts):
        for widget in self.table.winfo_children():
            widget.destroy()
        for title_key, column in ACCOUNT_HEADERS:
            ctk.CTkLabel(
                self.table, text=t(title_key), font=FONTS["label"], text_color=COLORS["muted"], anchor="w"
            ).grid(row=0, column=column, sticky="w", padx=(0, 14), pady=(0, 4))
        if not accounts:
            ctk.CTkLabel(self.table, text=t("conv.no_accounts"), font=FONTS["small"], anchor="w").grid(
                row=1, column=0, columnspan=len(ACCOUNT_HEADERS), sticky="w"
            )
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
