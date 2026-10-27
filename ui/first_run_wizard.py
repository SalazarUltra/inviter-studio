"""Short guided dialog for a brand-new install. Never shown to users who already have accounts."""

from __future__ import annotations

import webbrowser

import customtkinter as ctk

from core.first_run import API_SITE_URL, WIZARD_DONE_KEY, WIZARD_STEPS
from ui.hints import wrap_label
from ui.i18n import subscribe, t
from ui.theme import COLORS, FONTS, apply_button_role, style_tree


class FirstRunWizard(ctk.CTkToplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.step = 0
        self.configure(fg_color=COLORS["bg"])
        self.geometry("560x400")
        self.minsize(480, 360)
        self.transient(app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.bind("<Escape>", lambda _event: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.progress_label = ctk.CTkLabel(self, text="", font=FONTS["label"], text_color=COLORS["accent"], anchor="w")
        self.progress_label.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 0))
        body = ctk.CTkFrame(self, fg_color=COLORS["surface"], corner_radius=12)
        body.grid(row=1, column=0, sticky="nsew", padx=20, pady=12)
        self.title_label = ctk.CTkLabel(body, text="", font=FONTS["heading"], anchor="w", justify="left")
        self.title_label.pack(fill="x", padx=18, pady=(18, 8))
        self.text_label = ctk.CTkLabel(
            body, text="", font=FONTS["body"], text_color=COLORS["muted"], anchor="nw", justify="left", wraplength=460
        )
        self.text_label.pack(fill="x", padx=18)
        wrap_label(self.text_label)
        self.action_button = ctk.CTkButton(body, text="", height=36, command=self._action)
        apply_button_role(self.action_button, "secondary")
        self.action_button.pack(anchor="w", padx=18, pady=(16, 18))
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 16))
        self.remember_var = ctk.BooleanVar(value=True)
        self.remember_check = ctk.CTkCheckBox(footer, text="", variable=self.remember_var, font=FONTS["small"])
        self.remember_check.pack(side="left")
        self.next_button = ctk.CTkButton(footer, text="", width=110, height=34, command=self._next)
        apply_button_role(self.next_button, "primary")
        self.next_button.pack(side="right")
        self.back_button = ctk.CTkButton(footer, text="", width=90, height=34, command=self._back)
        apply_button_role(self.back_button, "secondary")
        self.back_button.pack(side="right", padx=(0, 8))
        self.skip_button = ctk.CTkButton(footer, text="", width=110, height=34, command=self.close)
        apply_button_role(self.skip_button, "ghost")
        self.skip_button.pack(side="right", padx=(0, 8))
        self._unsubscribe = subscribe(lambda _lang: self.render())
        self.render()
        style_tree(self)

    def render(self) -> None:
        if not self.winfo_exists():
            return
        step_id, _tab = WIZARD_STEPS[self.step]
        last = self.step == len(WIZARD_STEPS) - 1
        self.title(t("wizard.title"))
        self.progress_label.configure(text=t("wizard.progress", n=self.step + 1, total=len(WIZARD_STEPS)).upper())
        self.title_label.configure(text=t(f"wizard.{step_id}.title"))
        self.text_label.configure(text=t(f"wizard.{step_id}.text"))
        self.action_button.configure(text=t(f"wizard.{step_id}.button"))
        self.remember_check.configure(text=t("wizard.dont_show"))
        self.next_button.configure(text=t("btn.done") if last else t("btn.next"))
        self.back_button.configure(text=t("btn.back"), state="disabled" if self.step == 0 else "normal")
        self.skip_button.configure(text=t("btn.skip"))

    def _action(self) -> None:
        _step_id, tab = WIZARD_STEPS[self.step]
        if tab is None:
            if not webbrowser.open(API_SITE_URL):
                self.text_label.configure(text=API_SITE_URL)
            return
        self.app.switch_tab(tab)
        if tab == "accounts" and self.step == 1:
            self.app.accounts_tab.show_add_form()

    def _back(self) -> None:
        if self.step > 0:
            self.step -= 1
            self.render()

    def _next(self) -> None:
        if self.step < len(WIZARD_STEPS) - 1:
            self.step += 1
            self.render()
        else:
            self.close()

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self._unsubscribe()
        if self.remember_var.get():
            self.app.run_coro(self.app.db.set_setting(WIZARD_DONE_KEY, "1"))
        self.destroy()
