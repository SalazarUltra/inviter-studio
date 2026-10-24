"""Reusable literal message drafts. Applying a template never starts a campaign."""

from tkinter import messagebox

import customtkinter as ctk

from ui.dispatch import post_ui
from ui.theme import COLORS, FONTS, apply_button_role, style_tree


class TemplateDialog(ctk.CTkToplevel):
    def __init__(self, task):
        super().__init__(task)
        self.task = task
        self.db = task.db
        self.selected_id = None
        self.templates = {}
        self.busy = False
        self.title("Шаблоны сообщений")
        self.geometry("700x560")
        self.minsize(580, 450)
        self.transient(task.app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)
        ctk.CTkLabel(self, text="Шаблоны сообщений", font=FONTS["heading"]).grid(
            row=0, column=0, sticky="w", padx=20, pady=(16, 8)
        )
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=1, column=0, sticky="ew", padx=20)
        top.grid_columnconfigure(0, weight=1)
        self.menu = ctk.CTkOptionMenu(top, values=["Новый шаблон"], command=self.choose)
        self.menu.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.new_button = ctk.CTkButton(top, text="Новый", width=90, command=self.new)
        self.new_button.grid(row=0, column=1)
        self.name = ctk.CTkEntry(self, placeholder_text="Название: например, приглашение на встречу")
        self.name.grid(row=2, column=0, sticky="ew", padx=20, pady=10)
        ctk.CTkLabel(
            self, text="Текст шаблона · Ctrl+V для вставки", font=FONTS["small"], text_color=COLORS["muted"]
        ).grid(row=3, column=0, sticky="w", padx=20)
        self.textbox = ctk.CTkTextbox(self, wrap="word", font=FONTS["body"])
        self.textbox.grid(row=4, column=0, sticky="nsew", padx=20, pady=(4, 6))
        self.textbox.insert("1.0", task.message_box.get("1.0", "end-1c"))
        self.original = ("", self.textbox.get("1.0", "end-1c"))
        self.last_choice = "Новый шаблон"
        self.status = ctk.CTkLabel(
            self,
            text="Общие для всех кампаний. «Использовать» вставляет текст в задачу без отправки.",
            font=FONTS["small"],
            text_color=COLORS["muted"],
            wraplength=540,
            justify="left",
            anchor="w",
        )
        self.status.grid(row=5, column=0, sticky="ew", padx=20, pady=6)
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=6, column=0, sticky="ew", padx=20, pady=(0, 16))
        actions.grid_columnconfigure(2, weight=1)
        self.save_button = ctk.CTkButton(actions, text="Сохранить шаблон", width=155, command=self.save)
        self.save_button.grid(row=0, column=0, padx=(0, 8))
        self.delete_button = ctk.CTkButton(actions, text="Удалить", width=90, command=self.delete)
        self.delete_button.grid(row=0, column=1, padx=(0, 8))
        self.use_button = ctk.CTkButton(actions, text="Использовать текст", width=155, command=self.use)
        self.use_button.grid(row=0, column=2, sticky="e")
        style_tree(self)
        apply_button_role(self.use_button, "primary")
        apply_button_role(self.save_button, "secondary")
        apply_button_role(self.delete_button, "danger")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.load()

    def close(self):
        if not self.busy and self.discard_changes():
            self.destroy()

    def discard_changes(self):
        changed = (self.name.get(), self.textbox.get("1.0", "end-1c")) != self.original
        return not changed or messagebox.askyesno(
            "Несохранённый шаблон", "Продолжить без сохранения изменений шаблона?", parent=self
        )

    def load(self, selected=None):
        future = self.task.run_coro(self.db.get_message_templates())

        def done(result):
            def render():
                if not self.winfo_exists():
                    return
                if result.cancelled() or result.exception():
                    self.status.configure(text="Не удалось загрузить шаблоны", text_color=COLORS["error"])
                    return
                self.templates = {f"{row['id']} · {row['name']}": row for row in result.result()}
                self.menu.configure(values=["Новый шаблон", *self.templates])
                if selected:
                    key = next((key for key, row in self.templates.items() if row["id"] == selected), None)
                    if key:
                        self.menu.set(key)
                        self.choose(key)
                elif self.selected_id is None:
                    self.menu.set("Новый шаблон")

            post_ui(self.task, render)

        future.add_done_callback(done)

    def new(self, force=False):
        if not force and not self.discard_changes():
            self.menu.set(self.last_choice)
            return
        self.selected_id = None
        self.menu.set("Новый шаблон")
        self.name.delete(0, "end")
        self.textbox.delete("1.0", "end")
        self.original = ("", "")
        self.last_choice = "Новый шаблон"
        self.status.configure(text="Введите название и текст, затем сохраните шаблон.", text_color=COLORS["muted"])

    def choose(self, key):
        if self.busy:
            return
        row = self.templates.get(key)
        if row is None:
            self.new()
            return
        if not self.discard_changes():
            self.menu.set(self.last_choice)
            return
        self.selected_id = row["id"]
        self.name.delete(0, "end")
        self.name.insert(0, row["name"])
        self.textbox.delete("1.0", "end")
        self.textbox.insert("1.0", row["text"])
        self.original = (row["name"], row["text"])
        self.last_choice = key
        self.status.configure(text="Можно изменить шаблон или вставить его текст в задачу.", text_color=COLORS["muted"])

    def operate(self, coro, callback):
        self.busy = True
        self.name.configure(state="disabled")
        self.textbox.configure(state="disabled")
        self.menu.configure(state="disabled")
        for button in (self.save_button, self.delete_button, self.use_button, self.new_button):
            button.configure(state="disabled")
        future = self.task.run_coro(coro)

        def done(result):
            def render():
                if not self.winfo_exists():
                    return
                self.busy = False
                self.name.configure(state="normal")
                self.textbox.configure(state="normal")
                self.menu.configure(state="normal")
                for button in (self.save_button, self.delete_button, self.use_button, self.new_button):
                    button.configure(state="normal")
                if result.cancelled() or result.exception():
                    self.status.configure(
                        text="Изменения не сохранены: "
                        + (str(result.exception()) if not result.cancelled() else "операция отменена"),
                        text_color=COLORS["error"],
                    )
                    return
                callback(result.result())

            post_ui(self.task, render)

        future.add_done_callback(done)

    def save(self):
        if self.busy:
            return

        def saved(key):
            self.selected_id = key
            self.original = (self.name.get(), self.textbox.get("1.0", "end-1c"))
            self.status.configure(text="Шаблон сохранён", text_color=COLORS["success"])
            self.load(key)

        self.operate(
            self.db.save_message_template(self.name.get(), self.textbox.get("1.0", "end-1c"), self.selected_id), saved
        )

    def delete(self):
        if self.busy or self.selected_id is None:
            return
        if messagebox.askyesno("Удалить шаблон", "Удалить выбранный шаблон? Текст в задаче сохранится.", parent=self):

            def deleted(_result):
                self.new(force=True)
                self.load()

            self.operate(self.db.delete_message_template(self.selected_id), deleted)

    def use(self):
        if self.busy:
            return
        if self.task.db.campaign_id != self.db.campaign_id or self.task.scheduler.is_running() or self.task._starting:
            self.status.configure(
                text="Остановите задачу и откройте шаблоны в нужной кампании заново.", text_color=COLORS["error"]
            )
            return
        text = self.textbox.get("1.0", "end-1c")
        if not text.strip():
            self.status.configure(text="Введите текст шаблона", text_color=COLORS["error"])
            return
        current = self.task.message_box.get("1.0", "end-1c")
        if current.strip() and current != text:
            if not messagebox.askyesno(
                "Заменить текст", "Заменить текущий текст сообщения текстом из этого окна?", parent=self
            ):
                return
        self.task.message_box.delete("1.0", "end")
        self.task.message_box.insert("1.0", text)
        self.task.flush_draft()
        self.destroy()
