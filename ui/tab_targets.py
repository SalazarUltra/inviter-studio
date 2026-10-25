from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from core.database import Database
from core.export import write_targets_csv, write_targets_xlsx
from core.user_feedback import explain_error
from core.validation import parse_usernames
from ui.campaigns import CampaignBar
from ui.components import empty_state, page_intro
from ui.dispatch import post_ui
from ui.hints import tip
from ui.recipient_editor import RecipientEditor
from ui.theme import COLORS, FONTS, apply_button_role, get_density_preset, init_fonts, style_card, style_tree

STATUS_LABELS = {
    "pending": ("Ожидает", COLORS["warning"]),
    "added": ("Добавлен", COLORS["success"]),
    "error": ("Ошибка", COLORS["error"]),
    "not_found": ("Не найден", COLORS["muted"]),
    "private": ("Закрытый", COLORS["warning"]),
    "already_in": ("Уже в чате", COLORS["accent"]),
    "processing": ("В процессе", COLORS["warning"]),
    "sent": ("Отправлено", COLORS["success"]),
    "already_sent": ("Без повтора", COLORS["muted"]),
    "uncertain": ("Проверить", COLORS["warning"]),
}
FILTER_MAP = {
    "Все": "all",
    "Ожидает": "pending",
    "Добавлен": "added",
    "Ошибки": "error",
    "Не найден": "not_found",
    "Закрытый": "private",
    "Уже в чате": "already_in",
    "В процессе": "processing",
    "Отправленное сообщение": "sent",
    "Повтор отключён": "already_sent",
    "Требует проверки": "uncertain",
}


def parse_txt(filepath: str) -> list[str]:
    with open(filepath, "r", encoding="utf-8-sig") as file:
        return parse_usernames(file.read())


class TargetsTab(ctk.CTkFrame):
    def __init__(self, master, db: Database, run_coro, log_callback):
        super().__init__(master, fg_color=COLORS["bg"])
        init_fonts()
        self.db = db
        self.run_coro = run_coro
        self.log_callback = log_callback
        self.current_filter = ctk.StringVar(value="Все")
        self._density_mode = "normal"
        self.page = 0
        self.page_size = 100
        self._query_generation = 0

        self.intro = page_intro(
            self, "Аудитория", "Загрузите список пользователей и следите за каждым результатом.", "ПОЛУЧАТЕЛИ"
        )
        self.campaign_bar = CampaignBar(self)
        self.campaign_bar.pack(fill="x", padx=20, pady=(0, 4))
        editor_row = ctk.CTkFrame(self, fg_color="transparent")
        editor_row.pack(fill="x", padx=16, pady=(6, 0))
        self.edit_button = ctk.CTkButton(
            editor_row,
            text="Вставить / изменить список",
            command=self.edit_list,
            width=240,
            height=36,
        )
        apply_button_role(self.edit_button, "primary")
        self.edit_button.pack(side="left", padx=(0, 12))
        tip(
            self.edit_button,
            "Открыть всех ожидающих получателей выбранной кампании для вставки, изменения или удаления строк.",
        )
        self.editor_status = ctk.CTkLabel(
            editor_row,
            text="Никнеймы по одному на строку · Ctrl+V",
            anchor="w",
            font=FONTS["small"],
            text_color=COLORS["muted"],
        )
        self.editor_status.pack(side="left", fill="x", expand=True)
        self.editor_status.bind(
            "<Configure>",
            lambda event: self.editor_status.configure(
                wraplength=max(180, int(event.width / self._get_widget_scaling()))
            ),
        )
        self._editor = None
        top = ctk.CTkFrame(
            self, fg_color=COLORS["surface"], border_color=COLORS["border"], border_width=1, corner_radius=12
        )
        top.pack(fill="x", padx=10, pady=(10, 6))
        style_card(top, color=COLORS["accent"])

        btn_load = ctk.CTkButton(
            top,
            text="Импорт TXT",
            command=self.load_txt,
            corner_radius=12,
            height=36,
            font=FONTS["subhead"],
        )
        apply_button_role(btn_load, "secondary")
        btn_load.configure(width=110)
        btn_load.grid(row=0, column=0, sticky="ew", padx=5, pady=8)
        top.grid_columnconfigure(0, weight=1)
        btn_shuffle = ctk.CTkButton(
            top,
            text="Перемешать",
            command=self.shuffle_targets,
            corner_radius=12,
            height=36,
            font=FONTS["subhead"],
        )
        apply_button_role(btn_shuffle, "accent-line")
        btn_shuffle.configure(width=110)
        btn_shuffle.grid(row=0, column=1, sticky="ew", padx=5, pady=8)
        top.grid_columnconfigure(1, weight=1)
        btn_reset = ctk.CTkButton(
            top,
            text="Повторить ошибки",
            command=self.reset_failed,
            corner_radius=12,
            height=36,
            font=FONTS["subhead"],
        )
        apply_button_role(btn_reset, "accent-line")
        btn_reset.configure(width=110)
        btn_reset.grid(row=0, column=2, sticky="ew", padx=5, pady=8)
        top.grid_columnconfigure(2, weight=1)
        btn_clear = ctk.CTkButton(
            top,
            text="Очистить всё",
            command=self.clear_list,
            corner_radius=12,
            height=36,
            font=FONTS["subhead"],
        )
        apply_button_role(btn_clear, "danger")
        btn_clear.configure(width=110)
        btn_clear.grid(row=0, column=3, sticky="ew", padx=5, pady=8)
        top.grid_columnconfigure(3, weight=1)
        btn_export = ctk.CTkButton(
            top,
            text="Экспорт",
            command=self.export_csv,
            corner_radius=12,
            height=36,
            font=FONTS["subhead"],
        )
        apply_button_role(btn_export, "secondary")
        btn_export.configure(width=110)
        btn_export.grid(row=0, column=4, sticky="ew", padx=5, pady=8)
        top.grid_columnconfigure(4, weight=1)
        for button, text in (
            (
                btn_load,
                "Добавить получателей из UTF-8 TXT к существующей очереди. По одному никнейму на строку; повторы не добавляются.",
            ),
            (btn_shuffle, "Изменить порядок только ожидающих получателей. Результаты обработанных людей сохраняются."),
            (
                btn_reset,
                "Вернуть «Ошибка», «Не найден» и «Закрытый» в ожидающие. Защита личных сообщений от повторов остаётся.",
            ),
            (
                btn_clear,
                "Удалить всю аудиторию выбранной кампании, включая результаты. Для удаления только ожидающих используйте редактор.",
            ),
            (
                btn_export,
                "Сохранить всю аудиторию и результаты в Excel (XLSX) или CSV, независимо от страницы и выбранного фильтра. Формат выбирается в окне сохранения.",
            ),
        ):
            tip(button, text)

        ctk.CTkLabel(top, text="Фильтр:").grid(row=1, column=0, padx=8, pady=(0, 8), sticky="e")
        ctk.CTkOptionMenu(
            top,
            values=list(FILTER_MAP),
            variable=self.current_filter,
            command=lambda _: self.search_targets(),
            width=130,
        ).grid(row=1, column=1, columnspan=2, padx=5, pady=(0, 8), sticky="w")
        self.status_help_button = ctk.CTkButton(
            top,
            text="Что значат статусы?",
            height=28,
            width=180,
            command=lambda: self.winfo_toplevel().show_help("statuses"),
        )
        apply_button_role(self.status_help_button, "ghost")
        self.status_help_button.grid(row=1, column=3, columnspan=2, padx=8, pady=(0, 8), sticky="e")

        search_row = ctk.CTkFrame(self, fg_color="transparent")
        search_row.pack(fill="x", padx=16, pady=(0, 6))
        self.search_entry = ctk.CTkEntry(search_row, placeholder_text="Поиск по username", height=30)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.search_entry.bind("<Return>", lambda _: self.search_targets())
        ctk.CTkButton(search_row, text="Найти", width=75, height=30, command=self.search_targets).pack(
            side="left", padx=3
        )
        ctk.CTkButton(search_row, text="‹", width=32, height=30, command=lambda: self.change_page(-1)).pack(
            side="left", padx=3
        )
        self.page_label = ctk.CTkLabel(search_row, text="1 / 1", width=110, font=FONTS["small"])
        self.page_label.pack(side="left", padx=3)
        ctk.CTkButton(search_row, text="›", width=32, height=30, command=lambda: self.change_page(1)).pack(
            side="left", padx=3
        )

        self.stats_frame = ctk.CTkFrame(
            self,
            fg_color=COLORS["surface"],
            border_color=COLORS["border"],
            border_width=1,
            corner_radius=12,
        )
        self.stats_frame.pack(fill="x", padx=16, pady=(0, 8))
        style_card(self.stats_frame, color=COLORS["border"])
        self.stats_value_labels: dict[str, ctk.CTkLabel] = {}
        for key, title, color in [
            ("total", "ВСЕГО", COLORS["text"]),
            ("pending", "ОЖИДАЕТ", COLORS["warning"]),
            ("added", "УСПЕШНО", COLORS["success"]),
            ("errors", "ОШИБОК", COLORS["error"]),
        ]:
            block = ctk.CTkFrame(self.stats_frame, fg_color="transparent")
            block.pack(side="left", fill="x", expand=True, padx=10, pady=10)
            value_label = ctk.CTkLabel(
                block,
                text="0",
                font=FONTS["stat"],
                text_color=color,
            )
            value_label.pack(anchor="center")
            ctk.CTkLabel(
                block,
                text=title,
                font=FONTS["label"],
                text_color=COLORS["muted"],
            ).pack(anchor="center")
            self.stats_value_labels[key] = value_label

        self.progress = ctk.CTkProgressBar(self)
        self.progress.pack(fill="x", padx=10, pady=(0, 8))
        self.progress.set(0)

        self.table = ctk.CTkScrollableFrame(
            self,
            fg_color=COLORS["surface"],
            corner_radius=12,
            border_color=COLORS["border"],
            border_width=1,
        )
        self.table.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        style_card(self.table, color=COLORS["border"])

        self.refresh_stats()
        self.refresh_table()
        self._short_layout = None
        self.bind("<Configure>", self.fit_height, add="+")

    def fit_height(self, event):
        short = event.height / self._get_widget_scaling() < 580
        if short == self._short_layout:
            return
        self._short_layout = short
        if short:
            self.intro.pack_forget()
            self.stats_frame.pack_forget()
            self.campaign_bar.pack_configure(pady=(8, 4))
        else:
            self.intro.pack(fill="x", padx=20, pady=(18, 18), before=self.campaign_bar)
            self.stats_frame.pack(fill="x", padx=16, pady=(0, 8), before=self.progress)
            self.campaign_bar.pack_configure(pady=(0, 4))

    def _safe_after(self, callback) -> None:
        post_ui(self, callback)

    def _render_rows(self, rows: list[dict]) -> None:
        density = get_density_preset(self._density_mode)
        for widget in self.table.winfo_children():
            widget.destroy()

        if not rows:
            empty_state(
                self.table,
                "Здесь появится ваша аудитория",
                "Вставьте никнеймы в редактор или импортируйте TXT со списком username.",
                "targets",
                ("Вставить никнеймы", self.edit_list),
            )
            style_tree(self.table)
            return
        header = ctk.CTkFrame(self.table, fg_color="transparent")
        header.pack(fill="x", pady=(0, 4))
        header.grid_columnconfigure(4, weight=1)
        widths = (160, 110, 80, 160, 120)
        for idx, title in enumerate(["Юзернейм", "Статус", "Аккаунт", "Дата обработки", "Примечание"]):
            ctk.CTkLabel(header, text=title, width=widths[idx], anchor="w", font=FONTS["small"]).grid(
                row=0, column=idx, padx=6, sticky="w"
            )

        for item in rows:
            row = ctk.CTkFrame(self.table, fg_color=COLORS["bg"])
            row.pack(fill="x", pady=density["row_pad_y"])
            row.grid_columnconfigure(4, weight=1)
            status_text, status_color = STATUS_LABELS.get(item["status"], (item["status"], COLORS["muted"]))
            ctk.CTkLabel(row, text=f"@{item['username']}", width=160, wraplength=160, anchor="w").grid(
                row=0, column=0, padx=6, pady=density["row_pad_y"], sticky="w"
            )
            ctk.CTkLabel(
                row, text=status_text, text_color=status_color, width=110, anchor="w", font=FONTS["small"]
            ).grid(row=0, column=1, padx=6, pady=density["row_pad_y"], sticky="w")
            ctk.CTkLabel(row, text=str(item.get("added_by_account_id") or "-"), width=80, anchor="w").grid(
                row=0, column=2, padx=6, pady=density["row_pad_y"], sticky="w"
            )
            ctk.CTkLabel(row, text=self.format_time(item.get("processed_at") or "-"), width=160, anchor="w").grid(
                row=0, column=3, padx=6, pady=density["row_pad_y"], sticky="w"
            )
            ctk.CTkLabel(
                row, text=(item.get("error_message") or "-")[:300], width=120, wraplength=180, anchor="w"
            ).grid(row=0, column=4, padx=6, pady=density["row_pad_y"], sticky="w")

        style_tree(self.table)

    def begin_audience_import(self) -> None:
        app = self.winfo_toplevel()
        app._audience_imports += 1
        app.task_tab.update_readiness()

    def finish_audience_import(self) -> None:
        app = self.winfo_toplevel()
        app._audience_imports = max(0, app._audience_imports - 1)
        app.task_tab.update_readiness()

    def on_density_change(self, mode: str) -> None:
        self._density_mode = "compact" if mode == "compact" else "normal"
        self.refresh_table()

    def format_time(self, iso_str: str) -> str:
        if not iso_str or iso_str == "-":
            return "-"
        try:
            dt = datetime.fromisoformat(iso_str)
            return dt.strftime("%d.%m.%Y %H:%M")
        except Exception:
            return iso_str

    def edit_list(self):
        if self._editor is not None and self._editor.winfo_exists():
            self._editor.lift()
            return self._editor
        app = self.winfo_toplevel()
        if app.scheduler.is_running() or app.task_tab._starting:
            messagebox.showwarning("Задача выполняется", "Сначала остановите задачу, затем измените список")
            return None
        self._editor = RecipientEditor(self)
        return self._editor

    def load_txt(self) -> None:
        if not self._ensure_task_stopped("импортировать получателей"):
            return
        path = filedialog.askopenfilename(title="Выберите txt", filetypes=[("Text files", "*.txt")])
        if not path:
            return
        try:
            usernames = parse_txt(path)
        except (OSError, UnicodeError, ValueError) as exc:
            messagebox.showerror("Ошибка импорта", explain_error(exc))
            return
        if not usernames:
            messagebox.showwarning("Пусто", "В файле не найдено юзернеймов.")
            return

        self.begin_audience_import()
        try:
            fut = self.run_coro(self.db.bulk_insert_targets(usernames))
        except Exception:
            self.finish_audience_import()
            raise

        def done(f):
            def render():
                try:
                    inserted = f.result()
                except Exception as exc:  # noqa: BLE001
                    messagebox.showerror("Ошибка", explain_error(exc))
                else:
                    self.refresh_stats()
                    self.refresh_table()
                    self.log_callback("INFO", f"Импортировано {inserted} записей из TXT")
                finally:
                    self.finish_audience_import()

            self._safe_after(render)

        fut.add_done_callback(done)

    def clear_list(self) -> None:
        if not self._ensure_task_stopped("очистить аудиторию"):
            return
        if not messagebox.askyesno(
            "Очистить аудиторию",
            "Удалить всех получателей выбранной кампании, включая результаты?\n"
            "Настройки, журнал и защита от повторных сообщений сохранятся.\n"
            "Ожидания вторых сообщений тоже сохранятся. Для их отмены используйте кнопку в «Задаче».\n"
            "Только ожидающих можно удалить через «Вставить / изменить список».",
        ):
            return
        fut = self.run_coro(self.db.clear_targets())

        def done(result):
            if result.cancelled():
                return
            if result.exception():
                self._safe_after(lambda: messagebox.showerror("Список не очищен", str(result.exception())))
                return
            self._safe_after(self.refresh_stats)
            self._safe_after(self.refresh_table)

        fut.add_done_callback(done)

    def reset_failed(self) -> None:
        if self.winfo_toplevel().scheduler.is_running() or self.winfo_toplevel().task_tab._starting:
            messagebox.showwarning("Задача выполняется", "Остановите задачу перед возвратом получателей в очередь")
            return
        fut = self.run_coro(self.db.reset_failed_targets())
        fut.add_done_callback(
            lambda result: self._safe_after(
                lambda: self.finish_operation(result, "Получатели с ошибками возвращены в очередь")
            )
        )

    def shuffle_targets(self) -> None:
        if not self._ensure_task_stopped("перемешать список"):
            return
        fut = self.run_coro(self.db.shuffle_pending_targets())
        fut.add_done_callback(
            lambda result: self._safe_after(lambda: self.finish_operation(result, "Список перемешан"))
        )

    def finish_operation(self, result, message):
        if result.cancelled():
            return
        if result.exception():
            messagebox.showerror("Операция не выполнена", str(result.exception()))
            return
        self.refresh_stats()
        self.refresh_table()
        self.log_callback("INFO", message)

    def _ensure_task_stopped(self, action: str) -> bool:
        app = self.winfo_toplevel()
        if app.scheduler.is_running() or app.task_tab._starting:
            messagebox.showwarning("Задача выполняется", f"Сначала остановите задачу, затем можно {action}.")
            return False
        return True

    def export_csv(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Экспорт аудитории",
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")],
        )
        if not path:
            return

        async def export():
            rows = list(reversed(await self.db.get_targets(status="all", limit=None)))
            if Path(path).suffix.lower() == ".xlsx":
                accounts = await self.db.get_accounts()
                phones = {account["id"]: account.get("phone") or "" for account in accounts}
                await asyncio.to_thread(write_targets_xlsx, path, rows, phones)
            else:
                await asyncio.to_thread(write_targets_csv, path, rows)

        fut = self.run_coro(export())

        def done(result):
            try:
                result.result()
            except Exception as exc:
                self._safe_after(lambda exc=exc: messagebox.showerror("Ошибка экспорта", explain_error(exc)))
                return
            self._safe_after(lambda: messagebox.showinfo("Готово", "Файл экспортирован"))

        fut.add_done_callback(done)

    def refresh_stats(self) -> None:
        fut = self.run_coro(self.db.get_targets_stats())

        def done(f):
            try:
                stats = f.result()
            except Exception:
                return
            total = max(stats["total"], 1)
            done_count = stats["added"] + stats.get("sent", 0) + stats["errors"] + stats["skipped"]
            progress = min(done_count / total, 1.0)
            self._safe_after(lambda: self.stats_value_labels["total"].configure(text=str(stats["total"])))
            self._safe_after(lambda: self.stats_value_labels["pending"].configure(text=str(stats["pending"])))
            self._safe_after(
                lambda: self.stats_value_labels["added"].configure(text=str(stats["added"] + stats.get("sent", 0)))
            )
            self._safe_after(lambda: self.stats_value_labels["errors"].configure(text=str(stats["errors"])))
            self._safe_after(lambda: self.progress.set(progress))

        fut.add_done_callback(done)

    def search_targets(self):
        self.page = 0
        self.refresh_table()

    def change_page(self, direction):
        self.page = max(0, self.page + direction)
        self.refresh_table()

    def refresh_table(self) -> None:
        current = FILTER_MAP.get(self.current_filter.get(), "all")
        search = self.search_entry.get()
        db = self.db
        page = self.page
        self._query_generation += 1
        generation = self._query_generation

        async def fetch():
            count = await db.count_targets(current, search)
            pages = max(1, (count + self.page_size - 1) // self.page_size)
            selected = min(page, pages - 1)
            rows = await db.get_targets(current, self.page_size, search, selected * self.page_size)
            return rows, count, pages, selected

        fut = self.run_coro(fetch())

        def done(f):
            try:
                rows, count, pages, selected = f.result()
            except Exception:
                return

            def render():
                if self.db is not db or generation != self._query_generation:
                    return
                self.page = selected
                self.page_label.configure(text=f"{selected + 1} / {pages} · {count}")
                self._render_rows(rows)

            self._safe_after(render)

        fut.add_done_callback(done)
