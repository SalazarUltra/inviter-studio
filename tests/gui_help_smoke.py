"""Verify searchable/contextual help, navigation, tooltips, and campaign log switching offline."""

import asyncio
import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import ImageGrab

from core import database
from core.database import Database, init_db
from ui.app import App
from ui.help_content import GUIDES


def main():
    folder = Path(".audit/help")
    folder.mkdir(parents=True, exist_ok=True)
    errors = []
    with (
        tempfile.TemporaryDirectory(dir=folder) as directory,
        patch.object(database, "DB_PATH", str(Path(directory) / "ui.db")),
    ):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(init_db())
        db = Database()
        loop.run_until_complete(db.add_log(None, "alice", "MESSAGE_SENT", "First campaign event"))
        other_id = loop.run_until_complete(db.create_campaign("Other campaign", "messages"))
        other = db.for_campaign(other_id)
        loop.run_until_complete(other.add_log(None, "bobby", "MESSAGE_SENT", "Second campaign event"))

        def runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        with (
            patch("core.account_manager.TelegramClient", side_effect=AssertionError("Live Telegram blocked")),
            patch("tkinter.messagebox.showerror", side_effect=lambda *args, **kwargs: errors.append(str(args))),
            patch("tkinter.messagebox.showinfo"),
            patch("tkinter.messagebox.askyesno", return_value=True),
        ):
            app = App(loop, show_startup_support=False)
            app.geometry("900x600+20+20")
            app.attributes("-topmost", True)
            dialog = None

            def failed(kind, exc, trace):
                errors.append(f"{kind.__name__}: {exc}")
                app._on_close()

            app.report_callback_exception = failed

            def capture(name, window):
                window.update_idletasks()
                ImageGrab.grab(
                    bbox=(
                        window.winfo_rootx(),
                        window.winfo_rooty(),
                        window.winfo_rootx() + window.winfo_width(),
                        window.winfo_rooty() + window.winfo_height(),
                    )
                ).save(folder / name)

            def within(widget, window):
                assert widget.winfo_viewable()
                assert widget.winfo_rootx() >= window.winfo_rootx()
                assert widget.winfo_rooty() >= window.winfo_rooty()
                assert widget.winfo_rootx() + widget.winfo_width() <= window.winfo_rootx() + window.winfo_width()
                assert widget.winfo_rooty() + widget.winfo_height() <= window.winfo_rooty() + window.winfo_height()

            def begin():
                nonlocal dialog
                app._switch_tab("task")
                app.focus_force()
                app.event_generate("<F1>")
                dialog = app._help_dialog
                assert dialog and dialog.topic == "task"
                assert app.show_help("start") is dialog
                dialog.attributes("-topmost", True)
                dialog.geometry("600x420+80+80")
                app.after(800, search)

            def search():
                for widget in (dialog.menu, dialog.search, *dialog.action_buttons.values()):
                    within(widget, dialog)
                capture("help-minimum.png", dialog)
                field = dialog.search._entry
                field.focus_force()
                field.clipboard_get = Mock(return_value="личка")
                field.event_generate("<KeyPress>", keycode=86, state=4)
                assert dialog.search_variable.get() == "личка"
                assert dialog.rendered_blocks
                dialog.search_variable.set("неизвестноеслово12345")
                assert not dialog.rendered_blocks
                assert "Ничего не найдено" in dialog.summary.cget("text")
                dialog.search_variable.set("")
                assert dialog.rendered_blocks
                topics = iter(GUIDES)

                def next_topic():
                    topic = next(topics, None)
                    if topic is None:
                        dialog.show_topic("start")
                        dialog.action_buttons["targets"].invoke()
                        assert app.current_tab_key == "targets" and not dialog.winfo_exists()
                        app.targets_tab.status_help_button.invoke()
                        assert app._help_dialog.topic == "statuses"
                        app.after(300, tooltip)
                        return
                    dialog.show_topic(topic)
                    assert dialog.rendered_blocks
                    app.after(120, next_topic)

                next_topic()

            def tooltip():
                app._help_dialog.destroy()
                app._switch_tab("task")
                app.task_tab.mode_var.set("Личные сообщения")
                app.task_tab.on_mode_change()
                assert "личную переписку" in app.task_tab.mode_hint.cget("text")
                app.task_tab.audience_button.invoke()
                assert app.current_tab_key == "targets"
                app.focus_force()
                focus = app.focus_get()
                hint = app.help_button._help_tip
                hint.show()
                assert hint.window and hint.window.winfo_exists()
                assert app.focus_get() is focus
                hint.hide()
                assert hint.window is None
                app.after(200, logs)

            def logs():
                app._switch_tab("logs")
                app.after(300, first_log)

            def first_log():
                text = app.logs_tab.log_box.get("1.0", "end")
                assert "First campaign event" in text
                assert "Сообщение отправлено" in text
                assert "Second campaign event" not in text
                app.select_campaign(other_id)
                app.after(350, other_log)

            def other_log():
                text = app.logs_tab.log_box.get("1.0", "end")
                assert "Second campaign event" in text and "First campaign event" not in text
                app._switch_tab("accounts")
                app.accounts_tab.show_add_form()
                app.after(200, close_form)

            def close_form():
                tip = app.accounts_tab.new_api_hash_entry._help_tip
                tip.schedule()
                app.accounts_tab.hide_form()
                app.after(650, finish)

            def finish():
                app._switch_tab("dashboard")
                app.lift()
                app.focus_force()
                app.after(800, final_capture)

            def final_capture():
                capture("getting-started.png", app)
                app.show_help("data")
                app._on_close()

            app.after(500, begin)
            app.after(18000, lambda: failed(RuntimeError, RuntimeError("Help smoke timed out"), None))
            app.mainloop()
        thread.join(timeout=5)
        assert not thread.is_alive()
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
    (folder / "gui-result.json").write_text(
        json.dumps(
            {
                "errors": errors,
                "topics": len(GUIDES),
                "context_f1": True,
                "search_paste_and_empty_results": True,
                "navigation": True,
                "tooltips_keep_focus": True,
                "campaign_logs_switch": True,
                "live_telegram_calls": False,
            }
        ),
        encoding="utf-8",
    )
    assert not errors, errors
    print("Help GUI passed: all topics, F1, search, navigation, tooltip focus, and campaign logs.")


if __name__ == "__main__":
    main()
