"""Inline editing of the campaign's pending recipients on the task screen."""

from tkinter import messagebox

import customtkinter as ctk

from core.user_feedback import explain_error
from core.validation import parse_usernames, recipient_line
from ui.dispatch import post_ui
from ui.entry_utils import paste_text
from ui.hints import hint, wrap_label
from ui.theme import COLORS, FONTS, apply_button_role


class TaskRecipients(ctk.CTkFrame):
    def __init__(self, owner, parent):
        super().__init__(parent, fg_color="transparent")
        self.owner, self.app, self.db = owner, owner.app, owner.db
        self.expected = None
        self.original = ""
        self.busy = self.loading = False
        self._generation = 0
        ctk.CTkLabel(self, text="Кому отправлять · список получателей", font=FONTS["subhead"]).pack(anchor="w")
        hint(
            self,
            "Один @никнейм на строку, например @alice и @bobby. Можно вставлять ссылки t.me.\n"
            "После изменений нажмите «Сохранить список». Этот же список доступен в «Аудитории».",
            padx=0,
        )
        self.textbox = ctk.CTkTextbox(self, height=125, font=FONTS["body"], wrap="none", state="disabled")
        self.textbox.pack(fill="x", pady=(0, 6))
        self.textbox._textbox.bind("<<Modified>>", self.changed, add="+")
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x")
        self.paste_button = ctk.CTkButton(
            actions, text="Вставить", width=95, height=30, command=lambda: paste_text(self.textbox)
        )
        self.clear_button = ctk.CTkButton(actions, text="Очистить поле", width=125, height=30, command=self.clear)
        self.reload_button = ctk.CTkButton(actions, text="Обновить", width=100, height=30, command=self.reload)
        self.save_button = ctk.CTkButton(actions, text="Сохранить список", width=160, height=30, command=self.save)
        for button in (self.paste_button, self.clear_button, self.reload_button, self.save_button):
            apply_button_role(button, "primary" if button is self.save_button else "secondary")
            button.pack(side="left", padx=(0, 6))
        self.status = ctk.CTkLabel(
            self,
            text="Загрузка списка…",
            font=FONTS["small"],
            anchor="w",
            justify="left",
            text_color=COLORS["muted"],
            wraplength=600,
        )
        self.status.pack(fill="x", pady=(6, 0))
        wrap_label(self.status)
        self.set_enabled()

    def dirty(self):
        return self.expected is not None and self.textbox.get("1.0", "end-1c") != self.original

    def can_leave(self):
        if self.busy:
            messagebox.showwarning("Список сохраняется", "Дождитесь сохранения списка получателей.", parent=self.app)
            return False
        return not self.dirty() or messagebox.askyesno(
            "Несохранённый список",
            "Продолжить без сохранения изменений списка получателей?\n"
            "Чтобы применить список, сначала нажмите «Сохранить список».",
            parent=self.app,
        )

    def ready(self):
        if self.loading or self.busy or self.expected is None:
            messagebox.showwarning(
                "Список получателей",
                "Дождитесь загрузки или сохранения списка. При ошибке загрузки нажмите «Обновить».",
                parent=self.app,
            )
            return False
        if self.dirty():
            messagebox.showwarning(
                "Сохраните список",
                "Список получателей изменён. Нажмите «Сохранить список», затем повторите действие.",
                parent=self.app,
            )
            return False
        return True

    def set_enabled(self):
        running = self.app.scheduler.is_running() or self.owner._starting
        enabled = not (running or self.busy or self.loading) and self.expected is not None
        state = "normal" if enabled else "disabled"
        self.textbox.configure(state=state)
        for button in (self.paste_button, self.clear_button, self.save_button):
            button.configure(state=state)
        self.reload_button.configure(state="disabled" if running or self.busy or self.loading else "normal")

    def changed(self, _event=None):
        self.textbox._textbox.edit_modified(False)
        if self.expected is None or self.busy or self.loading:
            return
        try:
            count = len(parse_usernames(self.textbox.get("1.0", "end-1c")))
            self.status.configure(
                text=f"Уникальных никнеймов: {count} · "
                + ("Есть изменения — нажмите «Сохранить список»" if self.dirty() else "Список сохранён"),
                text_color=COLORS["muted"],
            )
        except ValueError as exc:
            self.status.configure(text=explain_error(exc), text_color=COLORS["error"])

    def clear(self):
        if str(self.textbox._textbox.cget("state")) == "normal":
            self.textbox.delete("1.0", "end")
            self.changed()

    def reload(self, force=False):
        if self.busy or (self.loading and self.db is self.owner.db):
            return
        if not force and not self.can_leave():
            return
        self.db = self.owner.db
        db = self.db
        self._generation += 1
        generation = self._generation
        self.expected = None
        self.loading = True
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self.set_enabled()
        self.status.configure(text="Загрузка списка…", text_color=COLORS["muted"])
        future = self.owner.run_coro(db.get_pending_list())

        def loaded(result):
            def render():
                if not self.winfo_exists() or generation != self._generation or self.owner.db is not db:
                    return
                self.loading = False
                if result.cancelled() or result.exception():
                    self.set_enabled()
                    self.status.configure(
                        text="Не удалось загрузить список. Нажмите «Обновить».", text_color=COLORS["error"]
                    )
                    return
                self.show_rows(result.result())

            post_ui(self.owner, render)

        future.add_done_callback(loaded)

    def refresh(self):
        if self.owner.db is not self.db:
            self.reload(force=True)
        elif not self.dirty() and not self.busy and not self.loading:
            self.reload(force=True)

    def show_rows(self, rows):
        self.expected = rows
        self.original = "\n".join(recipient_line(row["username"]) for row in rows)
        self.textbox.configure(state="normal")
        self.textbox.delete("1.0", "end")
        self.textbox.insert("1.0", self.original)
        self.set_enabled()
        self.changed()

    def save(self):
        if self.busy or self.loading or self.expected is None:
            return
        if self.owner.db is not self.db or self.app.scheduler.is_running() or self.owner._starting:
            self.status.configure(
                text="Сначала остановите задачу и выберите нужную кампанию.", text_color=COLORS["error"]
            )
            return
        try:
            usernames = parse_usernames(self.textbox.get("1.0", "end-1c"))
        except ValueError as exc:
            self.status.configure(text=explain_error(exc), text_color=COLORS["error"])
            return
        if (
            not usernames
            and self.expected
            and not messagebox.askyesno(
                "Очистить очередь?",
                "Удалить всех ожидающих получателей этой кампании?\n"
                "Обработанные результаты и защита от повторов сохранятся.",
                parent=self.app,
            )
        ):
            return
        self.busy = True
        self.set_enabled()
        self.status.configure(text="Сохранение списка…", text_color=COLORS["muted"])
        db, expected = self.db, self.expected

        async def persist():
            counts = await db.replace_pending_targets(usernames, expected)
            try:
                return counts, await db.get_pending_list()
            except Exception:
                return counts, None

        future = self.owner.run_coro(persist())

        def saved(result):
            def render():
                if not self.winfo_exists():
                    return
                self.busy = False
                if result.cancelled() or result.exception():
                    self.set_enabled()
                    self.status.configure(
                        text="Список не сохранён: "
                        + (str(result.exception()) if not result.cancelled() else "операция отменена"),
                        text_color=COLORS["error"],
                    )
                    return
                counts, rows = result.result()
                if rows is not None:
                    self.show_rows(rows)
                else:
                    self.expected = None
                    self.set_enabled()
                text = f"Список сохранён · Ожидает: {counts['pending']} · Уже обработано: {counts['processed']}"
                if rows is None:
                    text += " · Не удалось обновить поле, нажмите «Обновить»"
                self.status.configure(text=text, text_color=COLORS["success"])
                self.app.targets_tab.refresh_stats()
                self.app.targets_tab.refresh_table()
                self.owner._last_stats = None
                self.owner.log_callback("INFO", text)

            post_ui(self.owner, render)

        future.add_done_callback(saved)
