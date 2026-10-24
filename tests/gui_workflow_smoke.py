"""Wizard navigation and notification preferences; isolated DB, no network calls."""

import asyncio
import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from PIL import ImageGrab

from core import database
from core.database import Database, init_db
from tests.support import account_payload
from ui.app import App
from ui.notifications import NotifyIconData


def main():
    folder = Path(".audit/usability-20261022")
    folder.mkdir(parents=True, exist_ok=True)
    errors = []
    with (
        tempfile.TemporaryDirectory(dir=folder) as directory,
        patch.object(database, "DB_PATH", str(Path(directory) / "ui.db")),
    ):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(init_db())
        db = Database()
        loop.run_until_complete(db.add_account(account_payload()))
        loop.run_until_complete(db.bulk_insert_targets(["alice", "bobby"]))
        loop.run_until_complete(
            db.save_campaign_settings(
                {
                    "operation_mode": "messages",
                    "message_text": "Здравствуйте! Напоминаю о нашей встрече завтра.",
                    "selected_accounts": "[1]",
                }
            )
        )
        loop.run_until_complete(db.set_setting("desktop_notifications", "0"))
        thread = threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()), daemon=True)
        thread.start()
        with (
            patch("core.account_manager.TelegramClient", side_effect=AssertionError("Real Telegram blocked")),
            patch("tkinter.messagebox.showinfo"),
            patch("tkinter.messagebox.showerror") as error_dialog,
        ):
            app = App(loop, show_startup_support=False)
            task = app.task_tab
            app.attributes("-topmost", True)
            native = Mock(return_value=True)

            def prepare():
                app.notifications._data = NotifyIconData()
                app.notifications._shell = native

            app.notifications._prepare = prepare

            def read(coro):
                return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=3)

            def scenario():
                app._switch_tab("task")
                yield lambda: not task._loading_campaign and not task.recipients.loading and bool(task.account_vars)
                yield lambda: not app.notifications_var.get()
                assert not app.notifications.enabled
                for geometry, label in (("1280x800+10+10", "wide"), ("900x600+10+10", "minimum")):
                    app.geometry(geometry)
                    yield lambda: True
                    yield lambda: True
                    yield lambda: True
                    for step in range(4):
                        task._section_buttons[step].invoke()
                        yield lambda: True
                        assert task._step == step and task.back_btn.cget("state") == (
                            "disabled" if step == 0 else "normal"
                        )
                        assert not app.scheduler.is_running()
                        assert task.start_btn.winfo_viewable()
                        assert (
                            task.start_btn.winfo_rooty() + task.start_btn.winfo_height()
                            <= app.winfo_rooty() + app.winfo_height()
                        )
                        assert (
                            task._section_buttons[-1].winfo_rootx() + task._section_buttons[-1].winfo_width()
                            <= app.winfo_rootx() + app.winfo_width()
                        )
                        ImageGrab.grab(
                            bbox=(
                                app.winfo_rootx(),
                                app.winfo_rooty(),
                                app.winfo_rootx() + app.winfo_width(),
                                app.winfo_rooty() + app.winfo_height(),
                            )
                        ).save(folder / f"wizard-{label}-{step + 1}.png")
                assert "Здравствуйте" in task.review_box.get("1.0", "end-1c")
                assert account_payload()["phone"] in task.review_box.get("1.0", "end-1c")
                task.recipients.textbox.insert("end", "\n@charlie")
                task.refresh_review()
                assert "несохранённые" in task.review_box.get("1.0", "end-1c")
                with patch("tkinter.messagebox.showwarning") as warning:
                    task.start_btn.invoke()
                assert warning.called and task._step == 0 and not app.scheduler.is_running()
                task.recipients.textbox.delete("1.0", "end")
                task.recipients.textbox.insert("1.0", task.recipients.original)
                app.notify("Проверка", "Не должно появиться")
                native.assert_not_called()
                app.notifications_switch.toggle()
                yield lambda: app._notification_save_future.done()
                assert read(db.for_campaign(99).get_setting("desktop_notifications")) == "1"
                app.on_complete({"outcome": "completed"})
                yield lambda: native.call_count > 0
                assert [call.args[0] for call in native.call_args_list] == [1]
                assert app.notifications._data.szInfoTitle == "Задача завершена"
                app.notifications_switch.toggle()
                yield lambda: app._notification_save_future.done()
                assert native.call_args.args[0] == 2 and app.notifications._data is None
                assert read(db.get_setting("desktop_notifications")) == "0"
                app._notification_preference_changed = False
                app.notifications_var.set(True)
                app._load_notification_setting()
                yield lambda: not app.notifications_var.get()
                with patch.object(task.db, "get_campaign_kind", new=AsyncMock(side_effect=OSError("Test failure"))):
                    task.load_campaign_settings()
                    assert task.start_btn.cget("state") == "disabled"
                    yield lambda: task._load_error
                    assert task.start_btn.cget("text") == "Повторить загрузку"
                    assert not app.scheduler.is_running()
                task.start_btn.invoke()
                yield lambda: not task._loading_campaign
                assert task.message_box.get("1.0", "end-1c").startswith("Здравствуйте")
                error_dialog.assert_not_called()
                app._on_close()

            steps = scenario()
            waiting = None
            attempts = 0

            def advance():
                nonlocal waiting, attempts
                try:
                    if waiting and not waiting():
                        attempts += 1
                        assert attempts < 120, f"Timeout near {steps.gi_frame.f_lineno}"
                    else:
                        waiting, attempts = next(steps), 0
                except StopIteration:
                    return
                except Exception as exc:
                    errors.append(repr(exc))
                    task.recipients.textbox.delete("1.0", "end")
                    task.recipients.textbox.insert("1.0", task.recipients.original)
                    app._on_close()
                    return
                app.after(200, advance)

            app.report_callback_exception = lambda kind, exc, trace: errors.append(f"{kind.__name__}: {exc}")
            app.after(600, advance)
            app.mainloop()
        thread.join(timeout=5)
        assert not thread.is_alive()
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
    (folder / "workflow.json").write_text(
        json.dumps({"errors": errors, "live_telegram": False, "native_notifications": "mocked"}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert not errors, errors
    print(
        "Workflow GUI passed: four steps, sizes, unsaved recipient guard, notification preference, callback, cleanup."
    )


if __name__ == "__main__":
    main()
