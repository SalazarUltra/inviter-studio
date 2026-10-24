"""Small explanatory text and hover tips that do not interfere with editing."""

import tkinter as tk

import customtkinter as ctk

from ui.theme import COLORS, FONTS


def wrap_label(label, minimum=200):
    """Reflow once per width change; avoid nested scrollbar redraws during Configure."""
    label._wrap_job = None
    label._wrap_width = None

    def resized(event):
        width = max(minimum, int(event.width / label._get_widget_scaling()))
        if width == label._wrap_width:
            return
        label._wrap_width = width
        if label._wrap_job is not None:
            label.after_cancel(label._wrap_job)

        def apply():
            label._wrap_job = None
            if label.winfo_exists():
                label.configure(wraplength=label._wrap_width)

        label._wrap_job = label.after(80, apply)

    def cleanup(_event=None):
        if label._wrap_job is not None:
            label.after_cancel(label._wrap_job)
            label._wrap_job = None

    label.bind("<Configure>", resized, add="+")
    label.bind("<Destroy>", cleanup, add="+")


def hint(parent, text, padx=16):
    label = ctk.CTkLabel(
        parent, text=text, font=FONTS["small"], text_color=COLORS["muted"], justify="left", anchor="w", wraplength=480
    )
    label.pack(fill="x", padx=padx, pady=(0, 10))
    wrap_label(label)
    return label


class ControlTooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.job = None
        self.window = None
        widget.bind("<Enter>", self.schedule, add="+")
        for event in ("<Leave>", "<ButtonPress>", "<FocusOut>", "<Destroy>"):
            widget.bind(event, self.hide, add="+")

    def schedule(self, _event=None):
        self.hide()
        self.job = self.widget.after(550, self.show)

    def show(self):
        self.job = None
        if (
            not self.widget.winfo_exists()
            or not self.widget.winfo_viewable()
            or getattr(self.widget._root(), "_closing", False)
        ):
            return
        # Native Toplevel avoids delayed theme/icon callbacks on a short-lived tooltip.
        self.window = tk.Toplevel(self.widget, takefocus=False, bg=COLORS["border"])
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        scale = self.widget._get_widget_scaling()
        tk.Label(
            self.window,
            text=self.text,
            justify="left",
            bg=COLORS["elevated"],
            fg=COLORS["text"],
            font=("Segoe UI", 10),
            wraplength=int(320 * scale),
            padx=12,
            pady=10,
        ).pack(padx=1, pady=1)
        self.window.update_idletasks()
        owner = self.widget.winfo_toplevel()
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        x = max(
            owner.winfo_rootx() + 8,
            min(self.widget.winfo_rootx(), owner.winfo_rootx() + owner.winfo_width() - width - 8),
        )
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        if y + height > owner.winfo_rooty() + owner.winfo_height():
            y = max(owner.winfo_rooty() + 8, self.widget.winfo_rooty() - height - 6)
        self.window.geometry(f"+{x}+{y}")
        self.window.deiconify()

    def hide(self, _event=None):
        if self.job is not None:
            try:
                self.widget.after_cancel(self.job)
            except tk.TclError:
                pass
            self.job = None
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None


def tip(widget, text):
    previous = getattr(widget, "_help_tip", None)
    if previous is not None:
        previous.text = text
        return previous
    widget._help_tip = ControlTooltip(widget, text)
    return widget._help_tip
