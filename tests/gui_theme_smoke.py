"""Offline GUI check: light/dark switch in the header (live, saved, independent of density) and the queue auto-start box."""

import asyncio
import json
import tempfile
import threading
import traceback
from pathlib import Path
from tkinter import ttk
from unittest.mock import patch

import customtkinter as ctk

from core import database
from core.campaign_queue import get_auto_continue
from core.database import Database, init_db
from ui import theme
from ui.app import App


def main():
    folder = Path(".audit/theme")
    folder.mkdir(parents=True, exist_ok=True)
    errors = []
    with (
        tempfile.TemporaryDirectory(dir=folder) as directory,
        patch.object(database, "DB_PATH", str(Path(directory) / "test.db")),
    ):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(init_db())
        db = Database()
        loop.run_until_complete(db.set_setting("ui_theme", "light"))  # a saved light theme must be restored at start
        loop.run_until_complete(db.set_setting("ui_density", "compact"))
        thread = threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()), daemon=True)
        thread.start()
        with (
            patch("core.account_manager.TelegramClient", side_effect=AssertionError("Live Telegram blocked")),
            patch("tkinter.messagebox.showerror", side_effect=lambda *args, **kw: errors.append(str(args))),
        ):
            app = App(loop)
            app.geometry("1100x800")

            def read(coro):
                return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=3)

            def scenario():
                yield lambda: theme.current_theme() == "light"
                assert ctk.get_appearance_mode() == "Light"
                assert app.theme_var.get() == "Светлая", app.theme_var.get()
                yield lambda: app._active_density_mode == "compact"
                # the Treeview of the parsing tab follows the theme too
                style = ttk.Style(app)
                light_background = style.lookup("Parser.Treeview", "background")
                assert light_background.upper() == theme.COLORS["input"][0], light_background
                # switch to dark from the header menu: saved, density untouched
                app.theme_menu.set("Тёмная")
                app._on_theme_change("Тёмная")
                yield lambda: read(db.get_setting("ui_theme")) == "dark"
                assert ctk.get_appearance_mode() == "Dark"
                assert style.lookup("Parser.Treeview", "background").upper() == theme.COLORS["input"][1]
                assert app._active_density_mode == "compact"
                assert read(db.get_setting("ui_density")) == "compact"
                # language and theme are independent controls
                app.set_ui_language("en", persist=True)
                yield lambda: app.theme_var.get() == "Dark"
                app._on_theme_change("Light")
                yield lambda: read(db.get_setting("ui_theme")) == "light"
                app.set_ui_language("ru")
                yield lambda: app.theme_var.get() == "Светлая"
                # a bad stored value falls back to dark without errors
                app.set_ui_theme("sepia")
                assert theme.current_theme() == "dark" and ctk.get_appearance_mode() == "Dark"
                # navigation and all tabs render in both themes
                for mode in ("light", "dark"):
                    app.set_ui_theme(mode)
                    for key in app.page_frames:
                        app._switch_tab(key)
                        app.update_idletasks()
                # queue picker: «Автозапуск следующей» is a persistent global setting
                app._switch_tab("task")
                picker = next(bar for bar in app.campaign_bars if hasattr(bar, "auto_check"))
                yield lambda: picker.winfo_exists()
                assert picker.auto_var.get() is False
                picker.auto_var.set(True)
                picker.save_auto()
                yield lambda: read(get_auto_continue(db))
                app.queue_runner.session_confirmed = True
                picker.auto_var.set(False)
                picker.save_auto()
                yield lambda: not read(get_auto_continue(db))
                assert app.queue_runner.session_confirmed is False  # switching it off forgets the earlier «Да»
                app._on_close()

            steps = scenario()
            waiting, attempts = None, 0

            def advance():
                nonlocal waiting, attempts
                try:
                    if waiting and not waiting():
                        attempts += 1
                        assert attempts < 200, f"UI callback timeout at line {steps.gi_frame.f_lineno}"
                    else:
                        waiting, attempts = next(steps), 0
                    if not app._closing:
                        app.after(50, advance)
                except StopIteration:
                    pass
                except Exception:
                    errors.append(traceback.format_exc())
                    app._on_close()

            app.report_callback_exception = lambda kind, exc, trace: errors.append(f"{kind.__name__}: {exc}")
            app.after(2000, advance)
            app.mainloop()
        thread.join(timeout=5)
        if not loop.is_running():
            loop.close()
    (folder / "gui.json").write_text(
        json.dumps({"errors": errors, "live_telegram_calls": False}, indent=2), encoding="utf-8"
    )
    assert not errors, errors
    print("theme GUI smoke passed")


if __name__ == "__main__":
    main()
