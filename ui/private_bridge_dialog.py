"""«Написать закрытым со ссылкой»: private recipients -> new messages campaign with the group link."""

from __future__ import annotations

import customtkinter as ctk

from core.private_bridge import (
    DEFAULT_TEMPLATE,
    build_message,
    chat_link,
    create_private_dm_campaign,
    export_invite_link,
)
from core.user_feedback import explain_error
from core.validation import normalize_username
from ui.dispatch import post_ui
from ui.entry_utils import setup_entry
from ui.hints import hint, tip
from ui.theme import COLORS, FONTS, apply_button_role, style_tree


class PrivateBridgeDialog(ctk.CTkToplevel):
    def __init__(self, targets_tab, count: int):
        super().__init__(targets_tab)
        self.targets_tab = targets_tab
        self.app = targets_tab.winfo_toplevel()
        self.db = targets_tab.db
        self.count = count
        self.busy = False
        self.title("Написать закрытым со ссылкой")
        self.geometry("560x560")
        self.minsize(480, 460)
        self.transient(self.app)
        ctk.CTkLabel(self, text="Написать закрытым со ссылкой", font=FONTS["heading"]).pack(
            anchor="w", padx=20, pady=(16, 4)
        )
        hint(
            self,
            f"Закрытых получателей: {count}. Им нельзя добавить людей в чат напрямую — "
            "отправим личное сообщение со ссылкой на группу.",
            padx=20,
        )
        task = self.app.task_tab
        self.chat_raw = task.chat_entry.get().strip()
        ctk.CTkLabel(self, text="Ссылка на группу", font=FONTS["small"], text_color=COLORS["muted"]).pack(
            anchor="w", padx=20
        )
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(2, 6))
        row.grid_columnconfigure(0, weight=1)
        self.link_entry = ctk.CTkEntry(row, placeholder_text="https://t.me/имя или https://t.me/+…", height=36)
        self.link_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        setup_entry(self.link_entry)
        self.link_entry.insert(0, chat_link(self.chat_raw))
        self.export_button = ctk.CTkButton(row, text="Создать ссылку", width=140, height=36, command=self.export_link)
        apply_button_role(self.export_button, "secondary")
        self.export_button.grid(row=0, column=1)
        tip(
            self.export_button,
            "Выпустить invite-ссылку через выбранный аккаунт-администратор. Нужен сохранённый @адрес группы. "
            "Если не получится — вставьте ссылку вручную.",
        )
        if not self._exportable():
            self.export_button.configure(state="disabled")
        ctk.CTkLabel(
            self,
            text="Текст сообщения · {ссылка} заменится ссылкой",
            font=FONTS["small"],
            text_color=COLORS["muted"],
        ).pack(anchor="w", padx=20)
        self.message_box = ctk.CTkTextbox(self, height=150, wrap="word", font=FONTS["body"])
        self.message_box.pack(fill="x", padx=20, pady=(2, 8))
        self.message_box.insert("1.0", DEFAULT_TEMPLATE)
        hint(
            self,
            "Будет создана новая кампания «Личные сообщения» с этими получателями и настройками аккаунтов/интервала "
            "из текущей. Текст можно проверить на вкладке «Задача» — отправка начнётся только по кнопке запуска. "
            "Люди, которым вы уже писали, повторно не получат сообщение.",
            padx=20,
        )
        self.status = ctk.CTkLabel(
            self, text="", font=FONTS["small"], text_color=COLORS["muted"], wraplength=500, justify="left", anchor="w"
        )
        self.status.pack(fill="x", padx=20, pady=(0, 6))
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(0, 16))
        self.create_button = ctk.CTkButton(actions, text="Создать кампанию", height=38, command=self.submit)
        apply_button_role(self.create_button, "primary")
        self.create_button.pack(side="left", fill="x", expand=True, padx=(0, 6))
        cancel = ctk.CTkButton(actions, text="Отмена", height=38, width=100, command=self.destroy)
        apply_button_role(cancel, "secondary")
        cancel.pack(side="left")
        style_tree(self)
        self.after(50, self.lift)

    def _exportable(self) -> bool:
        try:
            normalize_username(self.chat_raw)
        except ValueError:
            return False
        return True

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.create_button.configure(state=state)
        self.export_button.configure(state="disabled" if busy or not self._exportable() else "normal")
        self.status.configure(text=text, text_color=COLORS["muted"])

    def _selected_account_ids(self) -> list[int]:
        task = self.app.task_tab
        selected = [key for key, var in task.account_vars.items() if var.get()]
        return selected or list(task.account_vars)

    def export_link(self) -> None:
        if self.busy:
            return
        if self.app.scheduler.is_running() or self.app.task_tab._starting:
            self.status.configure(text="Остановите задачу: аккаунт нужен для создания ссылки.")
            return
        try:
            chat = normalize_username(self.chat_raw)
        except ValueError as exc:
            self.status.configure(text=explain_error(exc))
            return
        self._set_busy(True, "Создаём ссылку через аккаунт…")
        db = self.db
        future = self.app.run_coro(export_invite_link(db, self._selected_account_ids(), chat))

        def done(result):
            def render():
                if not self.winfo_exists():
                    return
                self._set_busy(False)
                if result.cancelled() or result.exception():
                    error = "Операция отменена" if result.cancelled() else explain_error(result.exception())
                    self.status.configure(text=error, text_color=COLORS["error"])
                    return
                self.link_entry.delete(0, "end")
                self.link_entry.insert(0, result.result())
                self.status.configure(text="Ссылка создана и подставлена.", text_color=COLORS["success"])

            post_ui(self, render)

        future.add_done_callback(done)

    def submit(self) -> None:
        if self.busy:
            return
        if self.app.scheduler.is_running() or self.app.task_tab._starting:
            self.status.configure(text="Остановите задачу перед созданием кампании.", text_color=COLORS["error"])
            return
        try:
            message = build_message(self.message_box.get("1.0", "end-1c"), self.link_entry.get())
        except ValueError as exc:
            self.status.configure(text=explain_error(exc), text_color=COLORS["error"])
            return
        self._set_busy(True, "Создаём кампанию…")
        db = self.db
        future = self.app.run_coro(create_private_dm_campaign(db, message))

        def done(result):
            def render():
                if not self.winfo_exists():
                    return
                if result.cancelled() or result.exception():
                    self._set_busy(False)
                    error = "Операция отменена" if result.cancelled() else explain_error(result.exception())
                    self.status.configure(text=error, text_color=COLORS["error"])
                    return
                info = result.result()
                self.destroy()
                self.app.select_campaign(info["campaign_id"])
                for bar in getattr(self.app, "campaign_bars", []):
                    bar.refresh()
                if self.app.db.campaign_id == info["campaign_id"]:
                    self.app._switch_tab("task")
                self.app.set_status(
                    f"Создана кампания личных сообщений: в очереди {info['pending']} из {info['count']} закрытых. "
                    "Проверьте текст и запустите."
                )

            post_ui(self, render)

        future.add_done_callback(done)
