"""«После завершения запустить» — picks the follower campaign of the open campaign (campaign queue)."""

import customtkinter as ctk

from core.campaign_queue import ON_ALWAYS, ON_SUCCESS, get_link, set_link
from ui.dispatch import post_ui
from ui.hints import tip
from ui.theme import COLORS, FONTS

NONE_LABEL = "Нет"
TRIGGER_LABELS = {ON_SUCCESS: "только если завершена успешно", ON_ALWAYS: "в любом случае (кроме остановки)"}


class QueuePicker(ctk.CTkFrame):
    """Saves immediately; the next campaign is only offered (with a confirmation) when the current one finishes."""

    def __init__(self, parent):
        super().__init__(parent, fg_color="transparent")
        self.app = self.winfo_toplevel()
        self.choices = {NONE_LABEL: None}
        self._applying = False
        self._db = None
        ctk.CTkLabel(self, text="После завершения запустить:", font=FONTS["small"], text_color=COLORS["muted"]).pack(
            side="left", padx=(0, 6)
        )
        self.value = ctk.StringVar(value=NONE_LABEL)
        self.menu = ctk.CTkOptionMenu(
            self, variable=self.value, values=[NONE_LABEL], command=lambda _value: self.save(), width=230, height=28
        )
        self.menu.pack(side="left", padx=(0, 8))
        self.trigger = ctk.StringVar(value=TRIGGER_LABELS[ON_SUCCESS])
        self.trigger_menu = ctk.CTkOptionMenu(
            self,
            variable=self.trigger,
            values=list(TRIGGER_LABELS.values()),
            command=lambda _value: self.save(),
            width=250,
            height=28,
        )
        self.trigger_menu.pack(side="left", padx=(0, 8))
        self.status = ctk.CTkLabel(self, text="", font=FONTS["small"], text_color=COLORS["muted"])
        self.status.pack(side="left")
        tip(
            self.menu,
            "Очередь кампаний: когда эта кампания закончится, программа предложит запустить выбранную. "
            "Запуск всегда с вашим подтверждением; остановленная вами кампания очередь не продолжает.",
        )
        tip(self.trigger_menu, "«Успешно» — задача дошла до конца без сбоя. «В любом случае» — также после ошибки.")
        if not hasattr(self.app, "campaign_bars"):
            self.app.campaign_bars = []
        self.app.campaign_bars.append(self)  # refreshed together with the campaign bars
        self.refresh()

    def refresh(self):
        db = self.app.db
        self._db = db

        async def load():
            return await db.get_campaigns(), await get_link(db)

        future = self.app.run_coro(load())

        def done(result):
            if result.cancelled() or result.exception():
                return
            campaigns, link = result.result()

            def render():
                if self._db is not db or not self.winfo_exists():
                    return
                self.choices = {NONE_LABEL: None} | {
                    f"{row['id']} · {row['name']}": row["id"] for row in campaigns if row["id"] != db.campaign_id
                }
                self._applying = True
                self.menu.configure(values=list(self.choices))
                self.value.set(next((name for name, key in self.choices.items() if key == link["next_id"]), NONE_LABEL))
                self.trigger.set(TRIGGER_LABELS[link["on"]])
                self._applying = False
                self.status.configure(text="")

            post_ui(self, render)

        future.add_done_callback(done)

    def save(self):
        if self._applying:
            return
        db = self.app.db
        next_id = self.choices.get(self.value.get())
        trigger = next((key for key, text in TRIGGER_LABELS.items() if text == self.trigger.get()), ON_SUCCESS)
        future = self.app.run_coro(set_link(db, next_id, trigger))

        def done(result):
            def render():
                if not self.winfo_exists():
                    return
                if result.cancelled() or result.exception():
                    self.status.configure(
                        text=str(result.exception()) if not result.cancelled() else "Не сохранено",
                        text_color=COLORS["error"],
                    )
                    self.refresh()
                    return
                self.status.configure(text="Сохранено", text_color=COLORS["muted"])

            post_ui(self, render)

        future.add_done_callback(done)
