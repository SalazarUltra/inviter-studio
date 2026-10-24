"""Clipboard shortcuts on native Tk inputs, independent of keyboard layout."""

from __future__ import annotations

import sys
import tkinter as tk
from typing import Any

from ui.theme import COLORS

CLIPBOARD_TAG = "InviterClipboard"
VK_ACTIONS = {65: "select", 67: "copy", 86: "paste", 88: "cut"}
KEY_ACTIONS = {
    "a": "select",
    "c": "copy",
    "v": "paste",
    "x": "cut",
    "cyrillic_ef": "select",
    "cyrillic_es": "copy",
    "cyrillic_em": "paste",
    "cyrillic_che": "cut",
}


def _native(widget):
    return getattr(widget, "_entry", getattr(widget, "_textbox", widget))


def _editable(widget):
    return str(widget.cget("state")) == "normal"


def shortcut_action(event):
    if event.state & (0x8 | 0x20000):
        return None
    if sys.platform == "win32" and event.keycode in VK_ACTIONS:
        return VK_ACTIONS[event.keycode]
    return KEY_ACTIONS.get(str(event.keysym).lower())


def _select_all(_event, widget):
    widget = _native(widget)
    if isinstance(widget, tk.Text):
        widget.tag_add("sel", "1.0", "end-1c")
        widget.mark_set("insert", "end-1c")
    else:
        widget.selection_range(0, "end")
        widget.icursor("end")
    return "break"


def _copy(widget):
    widget = _native(widget)
    try:
        text = (
            widget.get("sel.first", "sel.last")
            if isinstance(widget, tk.Text)
            else widget.get()[widget.index("sel.first") : widget.index("sel.last")]
        )
    except tk.TclError:
        return "break"
    widget.clipboard_clear()
    widget.clipboard_append(text)
    return "break"


def _cut(widget):
    widget = _native(widget)
    if _editable(widget):
        _copy(widget)
        try:
            widget.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
    return "break"


def paste_text(widget):
    widget = _native(widget)
    if not _editable(widget):
        return "break"
    try:
        text = widget.clipboard_get()
    except tk.TclError:
        return "break"
    if not text:
        return "break"
    owner = getattr(widget, "master", None)
    if getattr(owner, "_entry", None) is widget and hasattr(owner, "insert"):
        # Use the public wrapper to remove an unfocused CTkEntry placeholder
        # and restore its password mask before inserting into the native input.
        owner.insert("insert", "")
    if isinstance(widget, tk.Text):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    try:
        start = widget.index("sel.first")
        widget.delete("sel.first", "sel.last")
        if isinstance(widget, tk.Text):
            widget.mark_set("insert", start)
        else:
            widget.icursor(start)
    except tk.TclError:
        pass
    widget.insert("insert", text)
    widget.focus_set()
    return "break"


def _control_key(event):
    action = shortcut_action(event)
    widget = event.widget
    if action == "paste":
        return paste_text(widget)
    if action == "copy":
        return _copy(widget)
    if action == "cut":
        return _cut(widget)
    if action == "select":
        return _select_all(event, widget)
    return None


def show_context_menu(event, widget):
    widget = _native(widget)
    widget.focus_set()
    menu = getattr(widget, "_inviter_clipboard_menu", None)
    if menu is None:
        menu = tk.Menu(widget, tearoff=0)
        widget._inviter_clipboard_menu = menu
    menu.delete(0, "end")
    menu.add_command(
        label="Вырезать", command=lambda: _cut(widget), state="normal" if _editable(widget) else "disabled"
    )
    menu.add_command(label="Копировать", command=lambda: _copy(widget))
    menu.add_command(
        label="Вставить", command=lambda: paste_text(widget), state="normal" if _editable(widget) else "disabled"
    )
    menu.add_separator()
    menu.add_command(label="Выделить всё", command=lambda: _select_all(None, widget))
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()
    return "break"


def install_clipboard_shortcuts(root):
    if getattr(root, "_clipboard_installed", False):
        return
    root._clipboard_installed = True
    root.bind_class(CLIPBOARD_TAG, "<Control-KeyPress>", _control_key)
    root.bind_class(CLIPBOARD_TAG, "<<Paste>>", lambda event: paste_text(event.widget))
    root.bind_class(CLIPBOARD_TAG, "<<Copy>>", lambda event: _copy(event.widget))
    root.bind_class(CLIPBOARD_TAG, "<<Cut>>", lambda event: _cut(event.widget))
    root.bind_class(CLIPBOARD_TAG, "<Shift-Insert>", lambda event: paste_text(event.widget))
    root.bind_class(CLIPBOARD_TAG, "<Control-Insert>", lambda event: _copy(event.widget))
    root.bind_class(CLIPBOARD_TAG, "<Button-3>", lambda event: show_context_menu(event, event.widget))

    def attach(widget):
        if isinstance(widget, (tk.Entry, tk.Text)) or widget.winfo_class() in {"TEntry", "TCombobox"}:
            tags = widget.bindtags()
            if CLIPBOARD_TAG not in tags:
                widget.bindtags((CLIPBOARD_TAG, *tags))

    def mapped(event):
        if isinstance(event.widget, tk.Misc):
            attach(event.widget)

    root.bind_all("<Map>", mapped, add="+")

    def walk(widget):
        attach(widget)
        for child in widget.winfo_children():
            walk(child)

    walk(root)


def setup_entry(entry: Any) -> None:
    entry.configure(fg_color=COLORS["surface"], border_color=COLORS["border"], text_color=COLORS["text"])
    entry.bind("<FocusIn>", lambda _event: entry.configure(border_color=COLORS["accent"]), add="+")
    entry.bind("<FocusOut>", lambda _event: entry.configure(border_color=COLORS["border"]), add="+")
