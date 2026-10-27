"""English UI of the conversion dashboard, campaign queue picker, A/B controls, transfer buttons and help dialog."""

import asyncio
import json
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import patch

from core import database
from core.database import init_db
from ui import i18n
from ui.app import App


def main():
    folder = Path(".audit/i18n_en")
    folder.mkdir(parents=True, exist_ok=True)
    errors = []
    with (
        tempfile.TemporaryDirectory(dir=folder) as directory,
        patch.object(database, "DB_PATH", str(Path(directory) / "test.db")),
        patch("core.paths.SESSIONS_DIR", Path(directory) / "sessions"),
    ):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(init_db())
        thread = threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()), daemon=True)
        thread.start()
        with (
            patch("core.account_manager.TelegramClient", side_effect=AssertionError("Live Telegram blocked")),
            patch.object(App, "show_support"),
            patch.object(App, "show_first_run_wizard"),
        ):
            app = App(loop)
            app.geometry("1280x800")

            def scenario():
                panel = app.dashboard_tab.conversion
                yield lambda: panel._report is not None
                assert panel.title_label.cget("text") == "Конверсия"
                assert panel.period_var.get() == "7 дней"
                app._on_language_change("English")
                assert panel.title_label.cget("text") == "Conversion"
                assert panel.refresh_button.cget("text") == "Refresh"
                assert panel.period_var.get() == "7 days", panel.period_var.get()
                assert panel.invites_title.cget("text") == "Invites"
                assert panel.accounts_title.cget("text") == "By account"
                assert panel.tile_captions["conv.success"][0].cget("text") == "Success"
                yield lambda: "period: 7 days" in panel.note.cget("text")
                assert "All campaigns" in panel.campaign_menu.cget("values")
                assert panel._period_key() == "7d"
                picker = app.task_tab.queue_picker
                assert picker.caption.cget("text") == "When finished, start:"
                assert picker.auto_check.cget("text").startswith("Auto-start")
                yield lambda: picker.value.get() == "None"
                assert picker.trigger.get() == "only if finished successfully"
                task = app.task_tab
                assert task.ab_switch.cget("text").startswith("A/B test")
                assert task.paste_ab_btn.cget("text") == "Paste"
                assert task.ab_split_caption.cget("text") == "Share of variant A, %"
                task.ab_split_entry.delete(0, "end")
                task.ab_split_entry.insert(0, "0")
                task._ab_split_changed()
                assert task.ab_split_label.cget("text") == "Enter 1–99"
                task.ab_split_entry.delete(0, "end")
                task.ab_split_entry.insert(0, "40")
                task.ab_enabled.set(True)
                task.ab_box.insert("1.0", "variant b")
                assert any("A/B text test" in line for line in task._ab_review_lines())
                # Campaign transfer buttons live in the manage dialog.
                bar = next(bar for bar in app.campaign_bars if hasattr(bar, "manage_button"))
                dialog = bar.manage()
                assert dialog.export_button.cget("text") == "Export to JSON…"
                assert dialog.import_button.cget("text") == "Import from JSON…"
                dialog.destroy()
                # Help follows the language, including an open dialog.
                help_dialog = app.show_help("task")
                assert help_dialog.heading_label.cget("text") == "Help for your first run"
                assert help_dialog.menu.get() == "Task: sending and invites"
                assert help_dialog.action_buttons["accounts"].cget("text") == "1. Accounts"
                help_dialog.search_variable.set("interval")
                assert help_dialog.rendered_blocks and "Explanations found" in help_dialog.summary.cget("text")
                help_dialog.search_variable.set("")
                app._on_language_change("Русский")
                assert help_dialog.heading_label.cget("text") == "Помощь для первого запуска"
                assert help_dialog.menu.get() == "Задача: отправка и приглашения"
                assert help_dialog.action_buttons["accounts"].cget("text") == "1. Аккаунты"
                app._on_language_change("English")
                assert help_dialog.menu.get() == "Task: sending and invites"
                help_dialog.destroy()
                app._on_language_change("Русский")
                assert panel.title_label.cget("text") == "Конверсия"
                assert panel.period_var.get() == "7 дней"
                yield lambda: picker.value.get() == "Нет"
                assert task.ab_split_caption.cget("text") == "Доля варианта A, %"
                assert "Все кампании" in panel.campaign_menu.cget("values")
                app._on_close()

            steps = scenario()
            waiting, attempts = None, 0

            def advance():
                nonlocal waiting, attempts
                try:
                    if waiting and not waiting():
                        attempts += 1
                        assert attempts < 200, "UI callback timed out"
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
            app.after(500, advance)
            app.mainloop()
        thread.join(timeout=5)
        loop.close()
    i18n.set_language("ru")
    (folder / "gui.json").write_text(json.dumps({"errors": errors}, indent=2), encoding="utf-8")
    if errors:
        raise AssertionError(errors)
    print("English UI smoke passed: conversion, queue picker, A/B, transfer buttons, help.")


if __name__ == "__main__":
    main()
