"""«После завершения запустить» — picks the follower campaign of the open campaign (campaign queue)."""

import customtkinter as ctk

from core.campaign_queue import ON_ALWAYS, ON_SUCCESS, get_auto_continue, get_link, set_auto_continue, set_link
from ui.dispatch import post_ui
from ui.hints import tip
from ui.i18n import t
from ui.theme import COLORS, FONTS


def none_label() -> str:
    return t("queue.none")


def auto_label() -> str:
    return t("queue.auto")


def trigger_labels() -> dict:
    """Localized trigger names keyed by the stored trigger value."""
    return {ON_SUCCESS: t("queue.trigger.success"), ON_ALWAYS: t("queue.trigger.always")}


class QueuePicker(ctk.CTkFrame):
    """Saves immediately; the next campaign is only offered (with a confirmation) when the current one finishes."""

    def __init__(self, parent):
        super().__init__(parent, fg_color="transparent")
        self.app = self.winfo_toplevel()
        self.choices = {none_label(): None}
        self._applying = False
        self._db = None
        self._last_triggers = trigger_labels()
        self.caption = ctk.CTkLabel(self, text=t("queue.label"), font=FONTS["small"], text_color=COLORS["muted"])
        self.caption.pack(side="left", padx=(0, 6))
        self.value = ctk.StringVar(value=none_label())
        self.menu = ctk.CTkOptionMenu(
            self, variable=self.value, values=[none_label()], command=lambda _value: self.save(), width=230, height=28
        )
        self.menu.pack(side="left", padx=(0, 8))
        self.trigger = ctk.StringVar(value=trigger_labels()[ON_SUCCESS])
        self.trigger_menu = ctk.CTkOptionMenu(
            self,
            variable=self.trigger,
            values=list(trigger_labels().values()),
            command=lambda _value: self.save(),
            width=250,
            height=28,
        )
        self.trigger_menu.pack(side="left", padx=(0, 8))
        self.auto_var = ctk.BooleanVar(value=False)
        self.auto_check = ctk.CTkCheckBox(
            self,
            text=auto_label(),
            variable=self.auto_var,
            command=self.save_auto,
            font=FONTS["small"],
            checkbox_width=18,
            checkbox_height=18,
        )
        self.auto_check.pack(side="left", padx=(0, 8))
        tip(self.auto_check, t("queue.auto_tip"))
        self.status = ctk.CTkLabel(self, text="", font=FONTS["small"], text_color=COLORS["muted"])
        self.status.pack(side="left")
        tip(self.menu, t("queue.menu_tip"))
        tip(self.trigger_menu, t("queue.trigger_tip"))
        if not hasattr(self.app, "campaign_bars"):
            self.app.campaign_bars = []
        self.app.campaign_bars.append(self)  # refreshed together with the campaign bars
        self.refresh()

    def refresh(self):
        db = self.app.db
        self._db = db

        async def load():
            return await db.get_campaigns(), await get_link(db), await get_auto_continue(db)

        future = self.app.run_coro(load())

        def done(result):
            if result.cancelled() or result.exception():
                return
            campaigns, link, auto = result.result()

            def render():
                if self._db is not db or not self.winfo_exists():
                    return
                self.choices = {none_label(): None} | {
                    f"{row['id']} · {row['name']}": row["id"] for row in campaigns if row["id"] != db.campaign_id
                }
                self._applying = True
                self.menu.configure(values=list(self.choices))
                self.value.set(
                    next((name for name, key in self.choices.items() if key == link["next_id"]), none_label())
                )
                self.trigger.set(trigger_labels()[link["on"]])
                self.auto_var.set(auto)
                self._applying = False
                self.status.configure(text="")

            post_ui(self, render)

        future.add_done_callback(done)

    def retranslate(self) -> None:
        """Relabel after a language switch; the stored link and trigger are kept (the lists reload)."""
        stored = next((key for key, text in self._last_triggers.items() if text == self.trigger.get()), ON_SUCCESS)
        self.caption.configure(text=t("queue.label"))
        self.auto_check.configure(text=auto_label())
        tip(self.auto_check, t("queue.auto_tip"))
        tip(self.menu, t("queue.menu_tip"))
        tip(self.trigger_menu, t("queue.trigger_tip"))
        self._last_triggers = trigger_labels()
        self.trigger_menu.configure(values=list(self._last_triggers.values()))
        self.trigger.set(self._last_triggers[stored])
        self.status.configure(text="")
        self.refresh()  # rebuilds the follower list with the localized «None» entry

    def save(self):
        if self._applying:
            return
        db = self.app.db
        next_id = self.choices.get(self.value.get())
        trigger = next((key for key, text in trigger_labels().items() if text == self.trigger.get()), ON_SUCCESS)
        future = self.app.run_coro(set_link(db, next_id, trigger))

        def done(result):
            def render():
                if not self.winfo_exists():
                    return
                if result.cancelled() or result.exception():
                    self.status.configure(
                        text=str(result.exception()) if not result.cancelled() else t("queue.not_saved"),
                        text_color=COLORS["error"],
                    )
                    self.refresh()
                    return
                self.status.configure(text=t("queue.saved"), text_color=COLORS["muted"])

            post_ui(self, render)

        future.add_done_callback(done)

    def save_auto(self):
        if self._applying:
            return
        enabled = bool(self.auto_var.get())
        runner = getattr(self.app, "queue_runner", None)
        if runner is not None and not enabled:
            runner.session_confirmed = False  # switching it off also forgets the «Да» given earlier in this session
        future = self.app.run_coro(set_auto_continue(self.app.db, enabled))

        def done(result):
            def render():
                if not self.winfo_exists():
                    return
                if result.cancelled() or result.exception():
                    self.auto_var.set(not enabled)
                    self.status.configure(text=t("queue.auto_not_saved"), text_color=COLORS["error"])
                    return
                for bar in getattr(self.app, "campaign_bars", []):
                    if bar is not self and hasattr(bar, "auto_var") and bar.winfo_exists():
                        bar.auto_var.set(enabled)
                self.status.configure(
                    text=t("queue.auto_on") if enabled else t("queue.auto_off"), text_color=COLORS["muted"]
                )

            post_ui(self, render)

        future.add_done_callback(done)
