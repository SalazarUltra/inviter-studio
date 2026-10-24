"""Shared page structure and purposeful empty states."""

import customtkinter as ctk

from ui.icons import icon
from ui.theme import COLORS, FONTS, apply_button_role


def page_intro(parent, title, description, kicker=None):
    header = ctk.CTkFrame(parent, fg_color="transparent")
    header.pack(fill="x", padx=20, pady=(18, 18))
    if kicker:
        ctk.CTkLabel(header, text=kicker, font=FONTS["label"], text_color=COLORS["accent"], height=20).pack(anchor="w")
    ctk.CTkLabel(header, text=title, font=FONTS["heading"], text_color=COLORS["text"]).pack(anchor="w")
    ctk.CTkLabel(
        header,
        text=description,
        font=FONTS["small"],
        text_color=COLORS["muted"],
        anchor="w",
        justify="left",
        wraplength=640,
    ).pack(anchor="w", pady=(4, 0))
    return header


def empty_state(parent, title, description, icon_name="layers", action=None):
    frame = ctk.CTkFrame(parent, fg_color="transparent")
    frame.pack(fill="x", padx=20, pady=32)
    ctk.CTkLabel(frame, text="", image=icon(icon_name, COLORS["subtle"], 34), height=42).pack(pady=(0, 12))
    ctk.CTkLabel(frame, text=title, font=FONTS["subhead"], text_color=COLORS["text"]).pack()
    ctk.CTkLabel(
        frame, text=description, font=FONTS["small"], text_color=COLORS["muted"], wraplength=280, justify="center"
    ).pack(pady=(6, 0))
    if action:
        label, command = action
        button = ctk.CTkButton(frame, text=label, command=command, height=36)
        apply_button_role(button, "secondary")
        button.pack(pady=(16, 0))
    return frame


class NavTooltip:
    """Expose icon-only navigation labels without taking keyboard focus."""

    def __init__(self, widget, title):
        self.widget = widget
        self.title = title
        self.job = None
        self.window = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<Button-1>", self.hide, add="+")

    def schedule(self, _event=None):
        self.hide()
        root = self.widget._root()
        if not root._rail_expanded and not root._closing:
            self.job = self.widget.after(450, self.show)

    def show(self):
        self.job = None
        if not self.widget.winfo_exists() or self.widget._root()._closing:
            return
        self.window = ctk.CTkToplevel(self.widget)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        ctk.CTkLabel(
            self.window, text=self.title, font=FONTS["small"], fg_color=COLORS["elevated"], corner_radius=8
        ).pack(padx=8, pady=4)
        self.window.geometry(
            f"+{self.widget.winfo_rootx() + self.widget.winfo_width() + 12}+{self.widget.winfo_rooty() + 4}"
        )
        self.window.deiconify()

    def hide(self, _event=None):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.window:
            self.window.destroy()
            self.window = None
