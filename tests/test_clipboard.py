import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import customtkinter as ctk

from ui.entry_utils import (
    CLIPBOARD_TAG,
    _select_all,
    install_clipboard_shortcuts,
    paste_text,
    shortcut_action,
    show_context_menu,
)


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.root = ctk.CTk()
        self.root.geometry("400x250+20+20")
        self.root.attributes("-alpha", 0)
        install_clipboard_shortcuts(self.root)
        self.addCleanup(self.close_root)

    def close_root(self):
        for timer_id in self.root.tk.call("after", "info"):
            self.root.tk.call("after", "cancel", timer_id)
        self.root.destroy()

    def field(self, constructor=ctk.CTkEntry):
        widget = constructor(self.root)
        widget.pack(fill="x")
        self.root.update()
        native = getattr(widget, "_entry", getattr(widget, "_textbox", widget))
        native.focus_force()
        self.root.update()
        return widget, native

    def test_shortcuts_follow_physical_keys_and_russian_keysyms(self):
        for code, keysym, expected in (
            (86, "Cyrillic_em", "paste"),
            (67, "Cyrillic_es", "copy"),
            (65, "Cyrillic_ef", "select"),
            (88, "Cyrillic_che", "cut"),
        ):
            self.assertEqual(shortcut_action(SimpleNamespace(keycode=code, keysym=keysym, state=4)), expected)
        self.assertIsNone(shortcut_action(SimpleNamespace(keycode=86, keysym="v", state=12)))

    def test_ctrl_v_pastes_exactly_once_in_entries_and_comboboxes(self):
        for constructor in (ctk.CTkEntry, ctk.CTkComboBox):
            widget, native = self.field(constructor)
            native.delete(0, "end")
            native.clipboard_get = Mock(return_value="Тестовый текст")
            self.assertEqual(native.bindtags()[0], CLIPBOARD_TAG)
            native.event_generate("<KeyPress>", keycode=86, state=4)
            self.root.update()
            self.assertEqual(widget.get(), "Тестовый текст")
            native.clipboard_get.assert_called_once()

    def test_multiline_paste_and_replacement_preserve_line_breaks(self):
        widget, native = self.field(ctk.CTkTextbox)
        native.clipboard_get = Mock(return_value="Первая строка\r\nВторая строка 😀")
        native.event_generate("<KeyPress>", keycode=86, state=4)
        self.root.update()
        self.assertEqual(widget.get("1.0", "end-1c"), "Первая строка\nВторая строка 😀")
        _select_all(None, widget)
        native.clipboard_get.return_value = "Замена\nДругая строка"
        native.event_generate("<<Paste>>")
        self.root.update()
        self.assertEqual(widget.get("1.0", "end-1c"), "Замена\nДругая строка")

    def test_partial_entry_selection_replaced_at_selection_start(self):
        widget, native = self.field()
        widget.insert(0, "abcd")
        native.selection_range(1, 3)
        native.icursor(4)
        native.clipboard_get = Mock(return_value="XY")
        paste_text(widget)
        self.assertEqual(widget.get(), "aXYd")

    def test_paste_into_unfocused_placeholder_restores_password_mask(self):
        widget = ctk.CTkEntry(self.root, placeholder_text="sk-…", show="*")
        widget.pack(fill="x")
        self.root.update()
        self.assertEqual(widget.get(), "")
        self.assertTrue(widget._placeholder_text_active)
        widget._entry.clipboard_get = Mock(return_value="fake-secret-key")
        paste_text(widget)
        self.assertEqual(widget.get(), "fake-secret-key")
        self.assertEqual(widget._entry.cget("show"), "*")

    def test_readonly_and_empty_clipboard_do_not_delete_text(self):
        widget, native = self.field(ctk.CTkTextbox)
        widget.insert("1.0", "Сохранённый текст")
        _select_all(None, widget)
        native.clipboard_get = Mock(return_value="")
        paste_text(widget)
        self.assertEqual(widget.get("1.0", "end-1c"), "Сохранённый текст")
        widget.configure(state="disabled")
        native.clipboard_get.return_value = "Новый текст"
        paste_text(widget)
        self.assertEqual(widget.get("1.0", "end-1c"), "Сохранённый текст")

    def test_context_menu_pastes_into_multiline_field(self):
        widget, native = self.field(ctk.CTkTextbox)
        native.clipboard_get = Mock(return_value="Из меню\nВторая строка")
        with patch.object(tk.Menu, "tk_popup"), patch.object(tk.Menu, "grab_release"):
            show_context_menu(SimpleNamespace(x_root=10, y_root=10), widget)
        native._inviter_clipboard_menu.invoke(2)
        self.assertEqual(widget.get("1.0", "end-1c"), "Из меню\nВторая строка")

    def test_copy_cut_select_all_without_changing_system_clipboard(self):
        widget, native = self.field()
        widget.insert(0, "Скопировать")
        native.clipboard_clear = Mock()
        native.clipboard_append = Mock()
        native.event_generate("<KeyPress>", keycode=65, state=4)
        native.event_generate("<KeyPress>", keycode=67, state=4)
        self.root.update()
        native.clipboard_append.assert_called_with("Скопировать")
        native.event_generate("<KeyPress>", keycode=88, state=4)
        self.root.update()
        self.assertEqual(widget.get(), "")
