"""Offer a verified GitHub release update with explicit user consent."""

from __future__ import annotations

import os
import subprocess
import tempfile
from tkinter import messagebox

import customtkinter as ctk

from core import updates
from core.version import VERSION
from ui.dispatch import post_ui
from ui.theme import COLORS, FONTS, apply_button_role


class UpdateDialog(ctk.CTkToplevel):
    def __init__(self, app, release: dict):
        super().__init__(app)
        self.app = app
        self.release = release
        self.downloading = False
        self.title("Доступно обновление")
        self.geometry("520x420")
        self.minsize(460, 380)
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            self,
            text=f"Inviter Studio {release['version']}",
            font=FONTS["title"],
            text_color=COLORS["accent"],
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(18, 4))
        ctk.CTkLabel(
            self,
            text=f"Установлена версия {VERSION}. Что нового:",
            font=FONTS["small"],
            text_color=COLORS["muted"],
        ).grid(row=0, column=0, sticky="sw", padx=20, pady=(0, 8))
        notes = ctk.CTkTextbox(self, font=FONTS["small"], wrap="word", state="disabled")
        notes.grid(row=1, column=0, sticky="nsew", padx=20)
        text = release.get("notes") or "Описание обновления — на странице релиза."
        notes.configure(state="normal")
        notes.insert("1.0", text[:4000])
        notes.configure(state="disabled")

        self.progress = ctk.CTkProgressBar(self, height=8)
        self.progress.set(0)
        self.progress_label = ctk.CTkLabel(self, text="", font=FONTS["small"], text_color=COLORS["muted"])
        self.progress.grid_remove()
        self.progress_label.grid_remove()

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", padx=20, pady=(10, 16))
        buttons.grid_columnconfigure(0, weight=1)
        self.download_button = ctk.CTkButton(
            buttons, text="Скачать и установить", height=36, command=self.start_download
        )
        self.download_button.grid(row=0, column=0, sticky="ew")
        apply_button_role(self.download_button, "primary")
        self.later_button = ctk.CTkButton(buttons, text="Позже", width=110, height=36, command=self.close)
        self.later_button.grid(row=0, column=1, padx=(8, 0))
        apply_button_role(self.later_button, "secondary")
        self.skip_var = ctk.BooleanVar(value=False)
        self.skip_check = ctk.CTkCheckBox(
            self,
            text="Не проверять обновления автоматически",
            variable=self.skip_var,
            font=FONTS["small"],
        )
        self.skip_check.grid(row=4, column=0, sticky="w", padx=20, pady=(0, 12))
        self.after(80, self._focus)

    def _focus(self):
        if self.winfo_exists():
            self.lift()
            self.focus_set()

    def close(self):
        if self.downloading:
            return
        if self.skip_var.get():
            self.app.run_coro(self.app.db.set_setting("update_check_enabled", "0"))
        self.destroy()

    def start_download(self):
        if self.downloading:
            return
        self.downloading = True
        self.skip_check.configure(state="disabled")
        self.download_button.configure(state="disabled", text="Загрузка…")
        self.later_button.configure(state="disabled")
        self.progress.grid(row=2, column=0, sticky="ew", padx=20, pady=(10, 2))
        self.progress_label.grid(row=2, column=0, sticky="w", padx=20, pady=(24, 0))
        folder = os.path.join(tempfile.gettempdir(), "InviterStudioUpdate")

        def progress(done, total):
            def render():
                if not self.winfo_exists():
                    return
                if total:
                    self.progress.set(min(done / total, 1.0))
                    self.progress_label.configure(text=f"Загружено {done // (1 << 20)} из {total // (1 << 20)} МБ")

            post_ui(self, render)

        future = self.app.run_coro(
            updates.download_update(self.release["setup_url"], self.release.get("checksum_url") or "", folder, progress)
        )
        future.add_done_callback(self._downloaded)

    def _downloaded(self, result):
        def render():
            if not self.winfo_exists():
                return
            if result.cancelled() or result.exception():
                self.downloading = False
                self.download_button.configure(state="normal", text="Скачать и установить")
                self.later_button.configure(state="normal")
                self.skip_check.configure(state="normal")
                self.progress_label.configure(text="")
                messagebox.showerror(
                    "Обновление", str(result.exception()) if not result.cancelled() else "Загрузка отменена"
                )
                return
            installer = result.result()
            self.destroy()
            if messagebox.askyesno(
                "Обновление готово",
                "Установщик загружен и проверен по SHA-256.\nЗакрыть программу и запустить обновление?",
                parent=self.app,
            ):
                self.app.start_installer_after_close(str(installer))

        post_ui(self, render)


def open_downloaded_folder() -> None:
    folder = os.path.join(tempfile.gettempdir(), "InviterStudioUpdate")
    if os.path.isdir(folder):
        subprocess.Popen(["explorer", folder])
