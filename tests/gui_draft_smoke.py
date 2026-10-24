"""Paste, autosave, switch campaigns, close, and reopen in separate offline processes."""

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from core.database import Database, init_db
from ui.app import App

FINAL = "Черновик перед выходом\n" + "Длинный текст 😀\n" * 300
OTHER = "Текст второй кампании\nСохранён автоматически"


def phase(name):
    loop = asyncio.new_event_loop()
    loop.run_until_complete(init_db())
    db = Database()
    if name == "edit":
        first = loop.run_until_complete(db.create_campaign("Первая", "messages"))
        second = loop.run_until_complete(db.create_campaign("Вторая", "messages"))
        assert (first, second) == (2, 3)
        loop.run_until_complete(db.set_setting("last_campaign_id", "2"))

    def runner():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    errors = []
    failure_patch = None
    with (
        patch("core.account_manager.TelegramClient", side_effect=AssertionError("Live Telegram blocked")),
        patch("tkinter.messagebox.showerror") as show_error,
        patch("tkinter.messagebox.showinfo"),
    ):
        app = App(loop)
        app.attributes("-alpha", 0)

        def failed(kind, exc, trace):
            errors.append(f"{kind.__name__}: {exc}")
            if failure_patch:
                failure_patch.stop()
            app._on_close()

        app.report_callback_exception = failed

        def paste(text):
            app._switch_tab("task")
            app.task_tab._section_buttons[1].invoke()
            field = app.task_tab.message_box._textbox
            field.focus_force()
            field.clipboard_get = Mock(return_value=text)
            field.event_generate("<KeyPress>", keycode=65, state=4)
            field.event_generate("<KeyPress>", keycode=86, state=4)
            assert app.task_tab.message_box.get("1.0", "end-1c") == text

        def loaded():
            if app.db.campaign_id != 2 or app.task_tab._loading_campaign:
                app.after(50, loaded)
                return
            if name == "check":
                assert app.task_tab.message_box.get("1.0", "end-1c") == FINAL
                app._on_close()
                return
            paste("Первый черновик\nСтрока 2")
            app.after(100, switch_other)

        def switch_other():
            app.select_campaign(3)
            app.after(400, write_other)

        def write_other():
            assert not app.task_tab._loading_campaign
            paste(OTHER)
            app.after(1200, check_autosave)

        def check_autosave():
            future = asyncio.run_coroutine_threadsafe(app.db.get_setting("message_text"), loop)
            assert future.result(timeout=2) == OTHER
            app.select_campaign(2)
            app.after(400, write_final)

        def write_final():
            nonlocal failure_patch
            assert app.task_tab.message_box.get("1.0", "end-1c") == "Первый черновик\nСтрока 2"
            paste(FINAL)
            failure_patch = patch.object(
                app.task_tab, "save_drafts", new=AsyncMock(side_effect=OSError("Тест сохранения"))
            )
            failure_patch.start()
            app._on_close()
            app.after(250, check_failed_close)

        def check_failed_close():
            nonlocal failure_patch
            if app._closing:
                app.after(50, check_failed_close)
                return
            assert app.winfo_exists()
            assert str(app.task_tab.message_box._textbox.cget("state")) == "normal"
            assert app.task_tab.message_box.get("1.0", "end-1c") == FINAL
            assert show_error.call_count == 1
            failure_patch.stop()
            failure_patch = None
            # Exit before another debounce: the final, long draft must still survive.
            app._on_close()

        app.after(500, loaded)
        app.after(12000, lambda: failed(RuntimeError, RuntimeError("Draft smoke timed out"), None))
        app.mainloop()
    thread.join(timeout=5)
    assert not thread.is_alive()
    loop.run_until_complete(loop.shutdown_asyncgens())
    loop.run_until_complete(loop.shutdown_default_executor())
    loop.close()
    assert not errors, errors
    print(f"Draft phase {name}: passed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("edit", "check"))
    args = parser.parse_args()
    if args.phase:
        phase(args.phase)
        return
    folder = Path(".audit/clipboard")
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder) as directory:
        env = {
            **os.environ,
            "INVITER_HOME": str(Path(directory).resolve()),
            "SSLKEYLOGFILE": "",
            "PYTHONIOENCODING": "utf-8",
        }
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        for name in ("edit", "check"):
            result = subprocess.run(
                [sys.executable, "-m", "tests.gui_draft_smoke", "--phase", name],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=flags,
            )
            if result.returncode:
                raise RuntimeError(f"Draft phase {name} failed:\n{result.stdout}\n{result.stderr}")
        with closing(sqlite3.connect(Path(directory) / "data/inviter.db")) as conn:
            values = dict(conn.execute("SELECT key,value FROM settings"))
        assert values["campaign:2:message_text"] == FINAL
        assert values["campaign:3:message_text"] == OTHER
        assert values["last_campaign_id"] == "2"
    (folder / "draft-result.json").write_text(
        json.dumps(
            {
                "autosave": True,
                "switch_before_debounce": True,
                "close_before_debounce": True,
                "restart_restores_text_and_campaign": True,
                "failed_save_keeps_window_open": True,
                "live_telegram_calls": False,
            }
        ),
        encoding="utf-8",
    )
    print("Draft smoke passed: paste, autosave, switch, failed save, close, and restart.")


if __name__ == "__main__":
    main()
