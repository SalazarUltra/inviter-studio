import asyncio
import json
from tkinter import filedialog, messagebox

import customtkinter as ctk

from core.backup import create_backup, stage_restore
from ui.dispatch import post_ui
from ui.hints import hint, tip
from ui.theme import FONTS, apply_button_role, style_tree


def show_report(owner, title, text):
    dialog = ctk.CTkToplevel(owner.winfo_toplevel())
    dialog.title(title)
    dialog.geometry("700x460")
    dialog.minsize(500, 320)
    dialog.transient(owner.winfo_toplevel())
    box = ctk.CTkTextbox(dialog, wrap="word", font=FONTS["body"])
    box.pack(fill="both", expand=True, padx=16, pady=16)
    box.insert("1.0", text)
    box.configure(state="disabled")
    style_tree(dialog)
    dialog.after(50, dialog.lift)
    return dialog


class CampaignBar(ctk.CTkFrame):
    def __init__(self, parent):
        super().__init__(parent, fg_color="transparent")
        self.app = self.winfo_toplevel()
        self.choices = {}
        self.value = ctk.StringVar(value="Основная кампания")
        self.menu = ctk.CTkOptionMenu(
            self, variable=self.value, values=["Основная кампания"], command=self.select, width=230, height=30
        )
        self.menu.pack(side="left", padx=(0, 6))
        self.manage_button = ctk.CTkButton(self, text="⋯", width=32, height=30, command=self.manage)
        apply_button_role(self.manage_button, "secondary")
        self.manage_button.pack(side="left", padx=(0, 6))
        tip(self.manage_button, "Переименовать кампанию, скопировать настройки или удалить кампанию.")
        tip(
            self.menu,
            "Кампания — отдельный список людей, режим, настройки и результаты. Выбирайте одну и ту же кампанию в «Аудитории» и «Задаче».",
        )
        for title, command, width, side in (
            ("+ Кампания", self.create, 110, "left"),
            ("Отчёт", self.history, 85, "left"),
            ("Копии данных", self.data_dialog, 120, "right"),
        ):
            button = ctk.CTkButton(self, text=title, width=width, height=30, command=command)
            apply_button_role(button, "secondary")
            button.pack(side=side, padx=3)
            tip(
                button,
                {
                    "+ Кампания": "Создать отдельную очередь и выбрать действие: личные сообщения или прямые приглашения в чат.",
                    "Отчёт": "Итоги выбранной кампании по аккаунтам и история последних запусков.",
                    "Копии данных": "Сохранить или восстановить базу. Telegram-сессии в копию базы не входят.",
                }[title],
            )
        self.delete_button = ctk.CTkButton(self, text="Удалить", width=75, height=30, command=self.delete)
        apply_button_role(self.delete_button, "danger")
        self.delete_button.pack(side="left", padx=3)
        tip(
            self.delete_button,
            "Удалить выбранную кампанию, её аудиторию, настройки, журнал и отчёты. Аккаунты останутся.",
        )
        if not hasattr(self.app, "campaign_bars"):
            self.app.campaign_bars = []
        self.app.campaign_bars.append(self)
        self.refresh()

    def refresh(self):
        future = self.app.run_coro(self.app.db.get_campaigns())

        def done(result):
            if result.cancelled() or result.exception():
                return

            def render():
                self.choices = {f"{row['id']} · {row['name']}": row["id"] for row in result.result()}
                self.menu.configure(values=list(self.choices))
                self.value.set(next((name for name, key in self.choices.items() if key == self.app.db.campaign_id), ""))

            post_ui(self, render)

        future.add_done_callback(done)

    def select(self, name):
        if name in self.choices:
            self.app.select_campaign(self.choices[name])
        self.refresh()

    def manage(self):
        if self.app.scheduler.is_running() or self.app.task_tab._starting:
            messagebox.showwarning("Задача выполняется", "Остановите задачу перед изменением кампании")
            return None
        db = self.app.db
        dialog = ctk.CTkToplevel(self.app)
        dialog.title("Управление кампанией")
        dialog.geometry("480x355")
        dialog.transient(self.app)
        ctk.CTkLabel(dialog, text=f"Кампания №{db.campaign_id}", font=FONTS["heading"]).pack(
            anchor="w", padx=20, pady=(18, 8)
        )
        name = ctk.CTkEntry(dialog, placeholder_text="Название кампании")
        name.pack(fill="x", padx=20, pady=8)
        current = self.value.get().split(" · ", 1)[-1]
        name.insert(0, current)
        hint(
            dialog,
            "Копируются только сохранённые настройки. Новая кампания получит пустую аудиторию и историю. Исходная кампания сохранится; защита от повторов остаётся общей.",
            padx=20,
        )
        status = ctk.CTkLabel(dialog, text="", font=FONTS["small"], wraplength=420)
        status.pack(fill="x", padx=20)
        actions = ctk.CTkFrame(dialog, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=10)
        buttons = []

        def submit(copy):
            if (
                self.app.scheduler.is_running()
                or self.app.task_tab._starting
                or self.app.db.campaign_id != db.campaign_id
            ):
                status.configure(text="Остановите задачу и откройте нужную кампанию заново.")
                return
            for button in buttons:
                button.configure(state="disabled")
            future = self.app.run_coro(
                db.copy_campaign_settings(name.get()) if copy else db.rename_campaign(name.get())
            )

            def done(result):
                def render():
                    if not dialog.winfo_exists():
                        return
                    if result.cancelled() or result.exception():
                        for button in buttons:
                            button.configure(state="normal")
                        status.configure(
                            text=str(result.exception()) if not result.cancelled() else "Операция отменена"
                        )
                        return
                    dialog.destroy()
                    if copy:
                        self.app.select_campaign(result.result())
                    for bar in self.app.campaign_bars:
                        bar.refresh()

                post_ui(self, render)

            future.add_done_callback(done)

        for title, copy in (("Переименовать", False), ("Создать копию настроек", True)):
            button = ctk.CTkButton(actions, text=title, width=205, command=lambda value=copy: submit(value))
            button.pack(side="left", fill="x", expand=True, padx=(0, 6))
            buttons.append(button)
        dialog.name_entry = name
        dialog.rename_button, dialog.copy_button = buttons
        dialog.delete_button = ctk.CTkButton(
            dialog, text="Удалить эту кампанию", height=32, command=lambda: self.delete(dialog)
        )
        apply_button_role(dialog.delete_button, "danger")
        dialog.delete_button.pack(fill="x", padx=20, pady=(0, 16))
        style_tree(dialog)
        return dialog

    def delete(self, owner=None):
        task = self.app.task_tab
        if self.app.scheduler.is_running() or task._starting:
            messagebox.showwarning(
                "Кампания выполняется",
                "Сначала остановите задачу и дождитесь завершения текущего действия.",
                parent=self.app,
            )
            return
        if task.recipients.busy:
            messagebox.showwarning("Список сохраняется", "Дождитесь сохранения списка получателей.", parent=self.app)
            return
        db = self.app.db
        current = self.value.get().split(" · ", 1)[-1]
        if not messagebox.askyesno(
            "Удалить кампанию?",
            f"Удалить «{current}»?\n\n"
            "Будут удалены аудитория, настройки, журнал и отчёты этой кампании, включая несохранённые изменения. Ожидание вторых сообщений будет отменено.\n\n"
            "Аккаунты, AI-подключения, результаты парсинга и другие кампании останутся. Защита от повторной отправки сохранится. Если это последняя кампания, появится новая пустая.\n\n"
            "Отменить удаление можно только восстановлением заранее сделанной копии базы.",
            parent=owner or self.app,
        ):
            return
        if self.app.db is not db or self.app.scheduler.is_running() or task._starting:
            return
        task._starting = True
        task._update_buttons_state()

        async def remove():
            async with task._draft_lock:
                return await db.delete_campaign()

        future = self.app.run_coro(remove())

        def done(result):
            def render():
                task._starting = False
                task._update_buttons_state()
                if result.cancelled() or result.exception():
                    messagebox.showerror(
                        "Удаление кампании",
                        str(result.exception()) if not result.cancelled() else "Операция отменена",
                        parent=self.app,
                    )
                    return
                if owner is not None and owner.winfo_exists():
                    owner.destroy()
                self.app.select_campaign(result.result(), discard_current=True)
                self.app.set_status("Кампания удалена. Открыта другая кампания.")

            post_ui(self, render)

        future.add_done_callback(done)

    def create(self):
        if self.app.scheduler.is_running() or self.app.task_tab._starting:
            messagebox.showwarning("Задача выполняется", "Остановите задачу перед созданием новой кампании")
            return
        dialog = ctk.CTkToplevel(self)
        dialog.title("Новая кампания")
        dialog.geometry("440x350")
        dialog.transient(self.app)
        name = ctk.CTkEntry(dialog, placeholder_text="Название кампании", height=38)
        name.pack(fill="x", padx=20, pady=(20, 12))
        hint(dialog, "Название — любое. Режим определяет, что будет делать эта кампания.", padx=20)
        mode = ctk.StringVar(value=self.app.task_tab.mode_var.get())
        ctk.CTkOptionMenu(dialog, values=["Приглашения", "Личные сообщения"], variable=mode, height=38).pack(
            fill="x", padx=20, pady=8
        )
        hint(
            dialog,
            "Личные сообщения — отправка текста людям.\nПриглашения — добавление людей прямо в чат с правами администратора.",
            padx=20,
        )

        def submit():
            kind = "messages" if mode.get() == "Личные сообщения" else "invite"
            future = self.app.run_coro(self.app.db.create_campaign(name.get(), kind))

            def done(result):
                if result.cancelled():
                    return

                def render():
                    if result.exception():
                        messagebox.showerror("Кампания", str(result.exception()))
                        return
                    dialog.destroy()
                    self.app.select_campaign(result.result())

                post_ui(self, render)

            future.add_done_callback(done)

        ctk.CTkButton(dialog, text="Создать кампанию", height=38, command=submit).pack(fill="x", padx=20, pady=12)
        style_tree(dialog)
        dialog.after(50, dialog.lift)

    def history(self):
        db = self.app.db

        async def fetch():
            return await db.get_runs(), await db.get_campaign_report()

        future = self.app.run_coro(fetch())

        def done(result):
            if result.cancelled() or result.exception():
                return
            runs, accounts = result.result()
            labels = {
                "completed": "Завершено",
                "stopped": "Остановлено",
                "failed": "Ошибка",
                "running": "Выполняется",
                "interrupted": "Прервано при закрытии",
            }
            statuses = {
                "added": "Добавлено",
                "pending": "Ожидает",
                "error": "Ошибок",
                "private": "Закрытый",
                "already_in": "Уже в чате",
                "not_found": "Не найден",
                "processing": "В обработке",
                "sent": "Отправлено",
                "already_sent": "Повтор отключён",
                "uncertain": "Требует проверки",
            }
            blocks = (
                [
                    "Результаты текущей очереди\n"
                    + "\n".join(
                        f"{row['phone'] or 'Без аккаунта'} · {statuses.get(row['status'], row['status'])}: {row['count']}"
                        for row in accounts
                    )
                ]
                if accounts
                else []
            )
            for row in runs:
                summary = json.loads(row["summary"] or "{}")
                block = (
                    f"{row['started_at']} · {row['chat']} · {labels.get(row['outcome'], row['outcome'])}\n"
                    f"Добавлено: {summary.get('added', 0)}; первых сообщений: {summary.get('sent', 0)}; "
                    f"вторых после ответа: {summary.get('followup_sent', 0)}; пропущено: {summary.get('skipped', 0)}; "
                    f"ошибок: {summary.get('errors', 0)}; время: {summary.get('duration', '—')}"
                )
                per_account = summary.get("accounts") or []
                if per_account:
                    block += "\nПо аккаунтам: " + "; ".join(
                        f"{item.get('phone', '?')} +{item.get('added', 0)}" for item in per_account
                    )
                blocks.append(block)
            post_ui(self, lambda: show_report(self, "История кампании", "\n\n".join(blocks) or "Запусков пока нет"))

        future.add_done_callback(done)

    def data_dialog(self):
        dialog = ctk.CTkToplevel(self)
        dialog.title("Резервные копии")
        dialog.geometry("560x270")
        dialog.transient(self.app)
        ctk.CTkLabel(
            dialog,
            text="Копия базы: кампании, аккаунты, настройки, история.\n"
            "Telegram-сессии остаются в папке sessions / profile.\n"
            "Зашифрованные ключи доступны исходному пользователю Windows.\n"
            "Не отправляйте .db и папку profile другим людям — это доступ к аккаунтам.\n"
            "При запуске сохраняются последние 7 автоматических копий.",
            justify="left",
            wraplength=500,
        ).pack(padx=20, pady=20)
        ctk.CTkButton(dialog, text="Сохранить копию…", command=self.backup).pack(fill="x", padx=20, pady=5)
        ctk.CTkButton(dialog, text="Восстановить при следующем запуске…", command=self.restore).pack(
            fill="x", padx=20, pady=5
        )
        style_tree(dialog)
        dialog.after(50, dialog.lift)

    def backup(self):
        if not messagebox.askyesno(
            "Личные данные",
            "Файл копии содержит аккаунты и ключи API (в зашифрованном виде).\n"
            "Не публикуйте его в GitHub и не передавайте посторонним.\n\n"
            "Сохранить копию только для себя?",
        ):
            return
        path = filedialog.asksaveasfilename(
            title="Сохранить резервную копию", defaultextension=".db", filetypes=[("SQLite", "*.db")]
        )
        if path:
            self._operation(asyncio.to_thread(create_backup, path), "Резервная копия сохранена")

    def restore(self):
        if (
            self.app.scheduler.is_running()
            or self.app.task_tab._starting
            or (self.app.warmup_tab.warmup_future and not self.app.warmup_tab.warmup_future.done())
        ):
            messagebox.showwarning(
                "Сначала остановите задачи", "Остановите приглашения и прогрев перед восстановлением"
            )
            return
        path = filedialog.askopenfilename(title="Выберите копию", filetypes=[("SQLite", "*.db")])
        if path and messagebox.askyesno(
            "Восстановление",
            "При следующем запуске база будет заменена выбранной копией. Текущая база будет сохранена. Продолжить?",
        ):
            self._operation(
                asyncio.to_thread(stage_restore, path), "Копия проверена. Перезапустите программу для восстановления."
            )

    def _operation(self, coroutine, success):
        future = self.app.run_coro(coroutine)

        def done(result):
            if result.cancelled():
                return
            error = result.exception()
            post_ui(
                self,
                lambda: (
                    messagebox.showerror("Ошибка копирования", str(error))
                    if error
                    else messagebox.showinfo("Готово", success)
                ),
            )

        future.add_done_callback(done)
