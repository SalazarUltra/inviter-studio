"""Optional project support window shared by first launch and persistent buttons."""

import tkinter as tk

import customtkinter as ctk

from core.support import SUPPORT_NETWORKS, WALLET_ADDRESS
from ui.hints import hint, wrap_label
from ui.icons import icon
from ui.theme import COLORS, FONTS, apply_button_role, style_tree


class SupportDialog(ctk.CTkToplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Поддержать проект · Inviter Studio")
        self.geometry("620x560")
        self.minsize(500, 450)
        self.configure(fg_color=COLORS["bg"])
        self.transient(app)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.bind("<Escape>", lambda _event: self.destroy())
        body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        body.grid(row=0, column=0, sticky="nsew", padx=12, pady=(12, 0))
        heading = ctk.CTkFrame(body, fg_color="transparent")
        heading.pack(fill="x", padx=12, pady=(10, 12))
        ctk.CTkLabel(
            heading,
            text="",
            image=icon("support", COLORS["accent"], 34),
            width=58,
            height=58,
            fg_color=COLORS["accent_soft"],
            corner_radius=14,
        ).pack(side="left", padx=(0, 14))
        ctk.CTkLabel(
            heading, text="ПОДДЕРЖАТЬ ПРОЕКТ\nInviter Studio", justify="left", font=FONTS["heading"], anchor="w"
        ).pack(side="left", fill="x", expand=True)
        intro = ctk.CTkLabel(
            body,
            text="Если приложение оказалось полезным, вы можете поддержать его развитие донатом. "
            "Спасибо за вашу поддержку!",
            font=FONTS["body"],
            justify="left",
            anchor="w",
        )
        intro.pack(fill="x", padx=12, pady=(0, 12))
        wrap_label(intro)
        networks = ctk.CTkFrame(body, fg_color=COLORS["surface"], corner_radius=12)
        networks.pack(fill="x", padx=12, pady=(0, 14))
        for title, assets in SUPPORT_NETWORKS:
            row = ctk.CTkFrame(networks, fg_color="transparent")
            row.pack(fill="x", padx=14, pady=8)
            ctk.CTkLabel(row, text=title, font=FONTS["subhead"], anchor="w").pack(side="left")
            ctk.CTkLabel(row, text=assets, font=FONTS["small"], text_color=COLORS["accent"]).pack(side="right")
        ctk.CTkLabel(body, text="Адрес кошелька", font=FONTS["subhead"], anchor="w").pack(fill="x", padx=12)
        self.address_var = ctk.StringVar(value=WALLET_ADDRESS)
        self.address_entry = ctk.CTkEntry(
            body,
            textvariable=self.address_var,
            state="readonly",
            height=42,
            font=("Consolas", 14),
            fg_color=COLORS["surface"],
            border_color=COLORS["border"],
        )
        self.address_entry.pack(fill="x", padx=12, pady=(6, 8))
        hint(body, "При переводе выберите нужную сеть: Ethereum (ERC-20) или BNB Smart Chain (BEP-20).", padx=12)
        hint(body, "Донат добровольный и не требуется для работы программы.", padx=12)
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=1, column=0, sticky="ew", padx=24, pady=(6, 16))
        self.status = ctk.CTkLabel(
            footer, text="", font=FONTS["small"], text_color=COLORS["muted"], anchor="w", justify="left"
        )
        self.status.pack(fill="x", pady=(0, 6))
        wrap_label(self.status)
        self.copy_button = ctk.CTkButton(
            footer, text="Скопировать адрес кошелька", height=40, command=self.copy_address
        )
        apply_button_role(self.copy_button, "primary")
        self.copy_button.pack(fill="x", pady=(0, 8))
        self.close_button = ctk.CTkButton(footer, text="Закрыть", height=32, command=self.destroy)
        apply_button_role(self.close_button, "secondary")
        self.close_button.pack(fill="x")
        style_tree(self)

    def copy_address(self):
        try:
            self.clipboard_clear()
            self.clipboard_append(WALLET_ADDRESS)
        except tk.TclError:
            self.copy_button.configure(text="Скопировать адрес кошелька")
            self.status.configure(
                text="Не удалось скопировать адрес. Выделите его и нажмите Ctrl+C.", text_color=COLORS["error"]
            )
            return
        self.copy_button.configure(text="Адрес скопирован ✓")
        self.status.configure(text="Адрес скопирован. Выберите нужную сеть при переводе.", text_color=COLORS["success"])
