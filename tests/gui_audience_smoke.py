"""Exercise the real audience editor offline, without reading the system clipboard."""

import asyncio
import json
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from PIL import ImageGrab

from core import database
from core.database import Database, init_db
from ui.app import App


def main():
    folder = Path(".audit/audience")
    folder.mkdir(parents=True, exist_ok=True)
    errors = []
    with (
        tempfile.TemporaryDirectory(dir=folder) as directory,
        patch.object(database, "DB_PATH", str(Path(directory) / "test.db")),
    ):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(init_db())
        db = Database()
        loop.run_until_complete(db.bulk_insert_targets(["alice", "bobby", "finished_user"]))
        rows = loop.run_until_complete(db.get_targets())
        finished_id = next(row["id"] for row in rows if row["username"] == "finished_user")
        loop.run_until_complete(db.mark_target(finished_id, "sent"))
        other_id = loop.run_until_complete(db.create_campaign("Other audience", "messages"))
        loop.run_until_complete(db.for_campaign(other_id).bulk_insert_targets(["other_user"]))

        def runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        with (
            patch("core.account_manager.TelegramClient", side_effect=AssertionError("Live Telegram blocked")),
            patch("tkinter.messagebox.showerror", side_effect=lambda *args, **kwargs: errors.append(str(args))),
            patch("tkinter.messagebox.showinfo"),
            patch("tkinter.messagebox.askyesno", return_value=True) as confirm,
        ):
            app = App(loop)
            app.geometry("1280x800+20+20")
            app.attributes("-topmost", True)
            editor = None
            failure_patch = None

            def failed(kind, exc, trace):
                nonlocal failure_patch
                errors.append(f"{kind.__name__}: {exc}")
                if trace:
                    errors.append("".join(traceback.format_exception(kind, exc, trace)).strip())
                if failure_patch:
                    failure_patch.stop()
                    failure_patch = None
                if editor and editor.winfo_exists():
                    editor.destroy()
                app._on_close()

            app.report_callback_exception = failed

            def read(coro):
                # A single-shot 3s limit occasionally loses to Windows stalling the
                # background loop thread (focus changes, DPI, AV scans); retry once.
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                try:
                    return future.result(timeout=10)
                except TimeoutError:
                    future.cancel()
                    for task in asyncio.all_tasks(loop):
                        errors.append(f"loop task {task.get_coro()}: {task.get_stack(limit=3)}")
                    raise

            def pending(scope=db):
                return [row["username"] for row in read(scope.get_pending_list())]

            def wait_for(predicate, callback, attempts=100):
                def diagnose():
                    if editor and editor.winfo_exists():
                        state = f"busy={editor.busy} status={editor.status.cget('text')!r}"
                    else:
                        state = "editor=closed"
                    return (
                        f"timeout state: {state} loop_running={loop.is_running()} "
                        f"queue={app.ui_dispatcher.queue.qsize()} closed={app.ui_dispatcher.closed}"
                    )

                if predicate():
                    callback()
                elif attempts:
                    app.after(50, lambda: wait_for(predicate, callback, attempts - 1))
                else:
                    errors.append(diagnose())
                    for task in asyncio.all_tasks(loop):
                        errors.append(f"loop task {task.get_coro()}: {task.get_stack(limit=4)}")
                    raise AssertionError(f"Timed out waiting before {callback.__name__}")

            def open_editor(callback):
                nonlocal editor
                editor = app.targets_tab.edit_list()
                assert editor is not None
                wait_for(lambda: editor.expected is not None, lambda: app.after(120, callback))

            def paste(text):
                field = editor.textbox._textbox
                field.focus_force()
                field.clipboard_get = Mock(return_value=text)
                field.event_generate("<KeyPress>", keycode=65, state=4)
                field.event_generate("<KeyPress>", keycode=86, state=4)
                actual = editor.textbox.get("1.0", "end-1c")
                expected = text.replace("\r\n", "\n").replace("\r", "\n")
                assert actual == expected, f"paste produced {actual!r}, expected {expected!r}"

            def capture(path, window):
                window.update_idletasks()
                ImageGrab.grab().crop(
                    (
                        window.winfo_rootx(),
                        window.winfo_rooty(),
                        window.winfo_rootx() + window.winfo_width(),
                        window.winfo_rooty() + window.winfo_height(),
                    )
                ).save(folder / path)

            def edit_and_save():
                assert editor.textbox.get("1.0", "end-1c") == "@alice\n@bobby"
                paste("@Alice\r\nhttps://t.me/Charlie\r\nalice")
                editor.geometry("560x420+100+100")
                app.after(800, invalid_input)

            def invalid_input():
                capture("editor-minimum.png", editor)
                for widget in (editor.paste_button, editor.clear_button, editor.save_button, editor.cancel_button):
                    assert widget.winfo_rootx() >= editor.winfo_rootx()
                    assert widget.winfo_rootx() + widget.winfo_width() <= editor.winfo_rootx() + editor.winfo_width()
                    assert widget.winfo_rooty() + widget.winfo_height() <= editor.winfo_rooty() + editor.winfo_height()
                paste("@alice\nbad name")
                editor.save_button.invoke()
                assert "Строка 2" in editor.status.cget("text")
                assert pending() == ["alice", "bobby"]
                paste("@Alice\nhttps://t.me/Charlie\nalice")
                nonlocal failure_patch
                failure_patch = patch.object(
                    editor.db, "replace_pending_targets", new=AsyncMock(side_effect=OSError("Тест ошибки записи"))
                )
                failure_patch.start()
                editor.save_button.invoke()
                # Wait for the error itself, not just for busy=False: the status
                # line is written in the same UI tick and polling it earlier races.
                # Extra attempts absorb rare multi-second stalls of the loop thread
                # under window-manager load; the global 25s timeout still bounds us.
                wait_for(
                    lambda: not editor.busy and "Тест ошибки записи" in editor.status.cget("text"),
                    failed_save,
                    attempts=400,
                )

            def failed_save():
                nonlocal failure_patch
                assert editor.winfo_exists()
                assert "Тест ошибки записи" in editor.status.cget("text")
                assert pending() == ["alice", "bobby"]
                assert "Charlie" in editor.textbox.get("1.0", "end-1c")
                failure_patch.stop()
                failure_patch = None
                editor.save_button.invoke()
                wait_for(lambda: not editor.winfo_exists(), saved_list)

            def saved_list():
                assert pending() == ["alice", "charlie"]
                assert next(row for row in read(db.get_targets()) if row["id"] == finished_id)["status"] == "sent"
                app.lift()
                app.focus_force()
                app.after(800, resize_main)

            def resize_main():
                capture("audience.png", app)
                app.geometry("900x600+20+20")
                app.after(1200, reopen)

            def reopen():
                capture("audience-minimum.png", app)
                button = app.targets_tab.edit_button
                assert button.winfo_viewable()
                assert app.targets_tab.table._parent_frame.winfo_height() >= 150
                open_editor(cancel_edit)

            def cancel_edit():
                assert editor.textbox.get("1.0", "end-1c") == "@alice\n@charlie"
                editor.clear_button.invoke()
                assert editor.textbox.get("1.0", "end-1c") == ""
                editor.textbox._textbox.clipboard_get = Mock(return_value="@unused_user")
                editor.paste_button.invoke()
                assert editor.textbox.get("1.0", "end-1c") == "@unused_user"
                editor.cancel_button.invoke()
                assert pending() == ["alice", "charlie"]
                open_editor(clear_saved)

            def clear_saved():
                editor.clear_button.invoke()
                confirm.return_value = False
                editor.save_button.invoke()
                assert pending() == ["alice", "charlie"]
                confirm.return_value = True
                editor.save_button.invoke()
                wait_for(lambda: not editor.winfo_exists(), switch_other)

            def switch_other():
                assert pending() == []
                assert read(db.count_targets()) == 1
                assert pending(db.for_campaign(other_id)) == ["other_user"]
                app.select_campaign(other_id)
                open_editor(check_other)

            def check_other():
                assert editor.textbox.get("1.0", "end-1c") == "@other_user"
                paste("@another_user")
                editor.save_button.invoke()
                wait_for(lambda: not editor.winfo_exists(), done)

            def done():
                assert pending(db.for_campaign(other_id)) == ["another_user"]
                assert read(db.count_targets()) == 1
                app._on_close()

            app.after(500, lambda: (app._switch_tab("targets"), open_editor(edit_and_save)))
            app.after(25000, lambda: failed(RuntimeError, RuntimeError("Audience smoke timed out"), None))
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
                "live_telegram_calls": False,
                "paste_edit_clear_save_cancel": True,
                "campaign_isolation": True,
                "invalid_input_and_failed_write_keep_queue": True,
            }
        ),
        encoding="utf-8",
    )
    assert not errors, errors
    print("Audience GUI passed: paste, edit, clear, save, cancel, error handling, and campaign isolation.")


if __name__ == "__main__":
    main()
