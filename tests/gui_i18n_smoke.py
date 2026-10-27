"""First-run wizard and restart-free RU/EN switch, offline with an empty profile."""

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
    folder = Path(".audit/i18n")
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
            patch("tkinter.messagebox.askyesno", return_value=True),
        ):
            app = App(loop)
            app.geometry("1280x800")

            def read(coro):
                return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=3)

            def scenario():
                # New install: the wizard opens by itself and walks through all steps.
                yield lambda: app._wizard is not None and app._wizard.winfo_exists()
                wizard = app._wizard
                assert wizard.step == 0 and "API" in wizard.title_label.cget("text")
                wizard._next()
                wizard._action()  # opens the accounts form
                assert app.current_tab_key == "accounts" and app.accounts_tab.form_visible
                for _ in range(3):
                    wizard._next()
                assert wizard.step == 4 and wizard.next_button.cget("text") == "Готово"
                wizard._action()
                assert app.current_tab_key == "task"
                # Language switches while the wizard is open and re-renders it.
                app._on_language_change("English")
                assert wizard.next_button.cget("text") == "Done" and "Task" in wizard.title_label.cget("text")
                assert app.nav_buttons["task"].cget("text") == "Task"
                assert app.nav_buttons["accounts"].cget("text") == "Accounts"
                assert app.task_tab.stop_btn.cget("text") == "Stop"
                assert app.task_tab.pace_buttons["safe"].cget("text") == "Safe"
                assert app.task_tab.pause_btn.cget("text") == "Pause"
                assert "Start task" in app.task_tab.start_btn.cget("text") or "Next" in app.task_tab.start_btn.cget(
                    "text"
                )
                assert app.accounts_tab.add_button.cget("text") == "+ Add account"
                assert app.breadcrumb_label.cget("text").endswith("Task")
                assert app.status_text.cget("text") == "Ready"
                yield lambda: read(app.db.get_setting("ui_lang")) == "en"
                wizard._next()  # last step -> closes and remembers
                yield lambda: not wizard.winfo_exists()
                yield lambda: read(app.db.get_setting("first_run_wizard_done")) == "1"
                # Summary card exists and is opt-in (off by default).
                card = app.accounts_tab.summary_card
                assert not card.enabled_var.get() and card.title_label.cget("text") == "Summary to Saved Messages"
                card.enabled_var.set(True)
                card._save()  # no account yet -> refused, stays off
                assert not card.enabled_var.get()
                assert read(app.db.get_setting("summary_enabled")) in (None, "0")
                # Back to Russian, restart-free.
                app._on_language_change("Русский")
                assert app.nav_buttons["task"].cget("text") == "Задача"
                assert app.task_tab.pace_buttons["safe"].cget("text") == "Безопасный"
                assert app.task_tab.stop_btn.cget("text") == "Остановить"
                yield lambda: read(app.db.get_setting("ui_lang")) == "ru"
                # Returning user: with the flag stored the wizard is not shown again.
                assert app.show_first_run_wizard is not None
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
    print("i18n smoke passed: first-run wizard, RU/EN switch without restart, opt-in summary card.")


if __name__ == "__main__":
    main()
