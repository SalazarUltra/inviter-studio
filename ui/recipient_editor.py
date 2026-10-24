"""A campaign-scoped editor for the waiting audience, with explicit persistence."""

from tkinter import messagebox

import customtkinter as ctk

from core.user_feedback import explain_error
from core.validation import parse_usernames
from ui.dispatch import post_ui
from ui.entry_utils import paste_text
from ui.theme import COLORS, FONTS, apply_button_role, style_tree


class RecipientEditor(ctk.CTkToplevel):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.app = owner.winfo_toplevel()
        self.db = owner.db
        self.expected = None
        self.original = ""
        self.busy = False
        self.title("Список никнеймов")
        self.geometry("720x540")
        self.minsize(560, 420)
        self.transient(self.app)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        ctk.CTkLabel(self, text="Список получателей", font=FONTS["title"]).grid(
            row=0, column=0, padx=20, pady=(18, 6), sticky="w"
        )
        self.help_label = ctk.CTkLabel(
            self,
            text=f"Кампания №{self.db.campaign_id} · Только ожидающие получатели.\n"
            "Один @username или ссылка t.me на строку.\nПосле редактирования нажмите «Сохранить список».",
            font=FONTS["small"],
            text_color=COLORS["muted"],
            justify="left",
            anchor="w",
            wraplength=660,
        )
        self.help_label.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 12))
        self.help_label.bind(
            "<Configure>",
            lambda event: self.help_label.configure(
                wraplength=max(300, int(event.width / self.help_label._get_widget_scaling()))
            ),
        )
        self.textbox = ctk.CTkTextbox(self, font=FONTS["body"], wrap="none", state="disabled")
        self.textbox.grid(row=2, column=0, sticky="nsew", padx=20)
        self.textbox._textbox.bind("<<Modified>>", self.changed, add="+")
        self.status = ctk.CTkLabel(
            self,
            text="Загрузка очереди…",
            anchor="w",
            justify="left",
            wraplength=660,
            font=FONTS["small"],
            text_color=COLORS["muted"],
        )
        self.status.grid(row=3, column=0, sticky="ew", padx=20, pady=8)
        self.status.bind(
            "<Configure>",
            lambda event: self.status.configure(
                wraplength=max(300, int(event.width / self.status._get_widget_scaling()))
            ),
        )
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=4, column=0, sticky="ew", padx=20, pady=(0, 16))
        buttons.grid_columnconfigure(2, weight=1)
        self.paste_button = ctk.CTkButton(buttons, text="Вставить", width=90, command=lambda: paste_text(self.textbox))
        self.paste_button.grid(row=0, column=0, padx=(0, 6))
        self.clear_button = ctk.CTkButton(buttons, text="Очистить поле", width=120, command=self.clear_text)
        self.clear_button.grid(row=0, column=1, padx=(0, 6))
        self.save_button = ctk.CTkButton(buttons, text="Сохранить список", width=150, command=self.save)
        self.save_button.grid(row=0, column=2, sticky="e", padx=(6, 6))
        self.cancel_button = ctk.CTkButton(buttons, text="Отмена", width=80, command=self.close)
        self.cancel_button.grid(row=0, column=3)
        style_tree(self)
        for button in (self.paste_button, self.clear_button, self.cancel_button):
            apply_button_role(button, "secondary")
        apply_button_role(self.save_button, "primary")
        self.set_enabled(False)
        self.after(50, self.activate)
        future = owner.run_coro(self.db.get_pending_list())

        def loaded(result):
            def render():
                if not self.winfo_exists():
                    return
                if result.cancelled() or result.exception():
                    self.status.configure(
                        text="Не удалось загрузить очередь. Закройте редактор и попробуйте снова.",
                        text_color=COLORS["error"],
                    )
                    return
                self.expected = result.result()
                self.original = "\n".join(f"@{row['username']}" for row in self.expected)
                self.textbox.configure(state="normal")
                self.textbox.insert("1.0", self.original)
                self.set_enabled(True)
                self.changed()
                self.textbox.focus_set()

            post_ui(owner, render)

        future.add_done_callback(loaded)

    def activate(self):
        if self.winfo_exists():
            self.lift()
            self.grab_set()

    def set_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.textbox.configure(state=state)
        for button in (self.paste_button, self.clear_button, self.save_button):
            button.configure(state=state)
        self.cancel_button.configure(state="disabled" if self.busy else "normal")

    def changed(self, _event=None):
        self.textbox._textbox.edit_modified(False)
        if self.expected is None or self.busy:
            return
        try:
            count = len(parse_usernames(self.textbox.get("1.0", "end-1c")))
            self.status.configure(
                text=f"Уникальных никнеймов: {count}. Повторы объединяются при сохранении.", text_color=COLORS["muted"]
            )
        except ValueError as exc:
            self.status.configure(text=explain_error(exc), text_color=COLORS["error"])

    def clear_text(self):
        if not self.busy and self.expected is not None:
            self.textbox.delete("1.0", "end")
            self.changed()

    def close(self):
        if self.busy:
            return
        if self.expected is not None and self.textbox.get("1.0", "end-1c") != self.original:
            if not messagebox.askyesno(
                "Несохранённый список", "Закрыть редактор без сохранения изменений?", parent=self
            ):
                return
        self.destroy()

    def save(self):
        if self.busy or self.expected is None:
            return
        if self.app.db.campaign_id != self.db.campaign_id:
            self.status.configure(text="Выбрана другая кампания. Откройте редактор заново.", text_color=COLORS["error"])
            return
        if self.app.scheduler.is_running() or self.app.task_tab._starting:
            self.status.configure(text="Сначала остановите задачу, затем измените список.", text_color=COLORS["error"])
            return
        try:
            usernames = parse_usernames(self.textbox.get("1.0", "end-1c"))
        except ValueError as exc:
            self.status.configure(text=explain_error(exc), text_color=COLORS["error"])
            return
        if not usernames and self.expected:
            if not messagebox.askyesno(
                "Очистить очередь?",
                "Удалить всех ожидающих получателей этой кампании?\n"
                "Результаты обработанных получателей и защита от повторов сохранятся.",
                parent=self,
            ):
                return
        self.busy = True
        self.set_enabled(False)
        self.status.configure(text="Сохранение…", text_color=COLORS["muted"])
        future = self.owner.run_coro(self.db.replace_pending_targets(usernames, self.expected))

        def saved(result):
            def render():
                if not self.winfo_exists():
                    return
                if result.cancelled() or result.exception():
                    self.busy = False
                    self.set_enabled(True)
                    self.status.configure(
                        text="Список не сохранён: "
                        + (str(result.exception()) if not result.cancelled() else "операция отменена"),
                        text_color=COLORS["error"],
                    )
                    return
                counts = result.result()
                text = f"Список сохранён · Ожидает: {counts['pending']} · Добавлено: {counts['inserted']} · Удалено: {counts['removed']}"
                if counts["processed"]:
                    text += f" · Уже обработано: {counts['processed']}"
                self.destroy()
                self.owner.refresh_stats()
                self.owner.refresh_table()
                self.owner.log_callback("INFO", text)
                self.owner.editor_status.configure(text=text)

            post_ui(self.owner, render)

        future.add_done_callback(saved)
