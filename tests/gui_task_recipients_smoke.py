"""Exercise the task's inline recipient list on an isolated, offline profile."""

import asyncio
import json
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from core import database
from core.database import Database, init_db
from ui.app import App


def main():
    folder = Path(".audit/task-recipients")
    folder.mkdir(parents=True, exist_ok=True)
    errors, warnings = [], []
    with (
        tempfile.TemporaryDirectory(dir=folder) as directory,
        patch.object(database, "DB_PATH", str(Path(directory) / "test.db")),
    ):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(init_db())
        db = Database()
        loop.run_until_complete(db.bulk_insert_targets(["alice", "bobby", "finished_user"]))
        rows = loop.run_until_complete(db.get_targets())
        loop.run_until_complete(
            db.mark_target(next(row["id"] for row in rows if row["username"] == "finished_user"), "sent")
        )
        other = loop.run_until_complete(db.create_campaign("Other", "messages"))
        loop.run_until_complete(db.for_campaign(other).bulk_insert_targets(["other_user"]))

        def runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        with (
            patch("core.account_manager.TelegramClient", side_effect=AssertionError("Live Telegram blocked")),
            patch("ui.tab_task.preflight", side_effect=AssertionError("Unexpected preflight")),
            patch("tkinter.messagebox.showerror", side_effect=lambda *args, **kw: errors.append(str(args))),
            patch("tkinter.messagebox.showwarning", side_effect=lambda *args, **kw: warnings.append(str(args))),
            patch("tkinter.messagebox.askyesno", return_value=True) as confirm,
        ):
            app = App(loop)
            app.geometry("900x600")
            panel = app.task_tab.recipients

            def read(coro):
                return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=3)

            def pending(scope=db):
                return [row["username"] for row in read(scope.get_pending_list())]

            def paste(text, shortcut=False):
                native = panel.textbox._textbox
                native.clipboard_get = Mock(return_value=text)
                native.focus_force()
                native.tag_add("sel", "1.0", "end-1c")
                if shortcut:
                    native.event_generate("<KeyPress>", keycode=86, state=4)
                else:
                    panel.paste_button.invoke()
                panel.changed()
                assert panel.textbox.get("1.0", "end-1c") == text.replace("\r\n", "\n")

            def scenario():
                yield lambda: panel.expected is not None and not app.task_tab._loading_campaign
                app._switch_tab("task")
                yield lambda: not panel.loading
                assert panel.textbox.get("1.0", "end-1c") == "@alice\n@bobby"
                yield lambda: panel.winfo_width() > 100
                for button in (panel.paste_button, panel.clear_button, panel.reload_button, panel.save_button):
                    assert button.winfo_x() + button.winfo_width() <= button.master.winfo_width()
                paste("@Alice\r\n@charlie\r\nalice\r\n@finished_user", shortcut=True)
                assert panel.dirty() and "3" in panel.status.cget("text")
                app.task_tab.start_task()
                app.task_tab.check_task()
                assert len(warnings) == 2 and pending() == ["alice", "bobby"]
                confirm.return_value = False
                app.select_campaign(other)
                assert app.db.campaign_id == 1
                app._on_close()
                assert not app._closing
                paste("@alice\nbad name")
                panel.save_button.invoke()
                assert "Строка 2" in panel.status.cget("text")
                assert pending() == ["alice", "bobby"]
                paste("@Alice\n@charlie\nalice\n@finished_user")
                panel.save_button.invoke()
                yield lambda: not panel.busy
                assert pending() == ["alice", "charlie"] and not panel.dirty()
                assert (read(Database().get_targets_stats()))["sent"] == 1
                assert pending(db.for_campaign(other)) == ["other_user"]
                assert any(row["action"] == "AUDIENCE_EDIT" for row in read(db.get_logs()))
                app._switch_tab("targets")
                yield lambda: len(app.targets_tab.table.winfo_children()) >= 4
                app._switch_tab("task")
                yield lambda: not panel.loading
                paste("@delta")
                with patch.object(
                    panel.db, "replace_pending_targets", new=AsyncMock(side_effect=OSError("Test write failure"))
                ):
                    panel.save_button.invoke()
                    yield lambda: not panel.busy
                assert panel.dirty() and "Test write failure" in panel.status.cget("text")
                assert pending() == ["alice", "charlie"]
                read(db.bulk_insert_targets(["external_user"]))
                panel.save_button.invoke()
                yield lambda: not panel.busy
                assert "Очередь изменилась" in panel.status.cget("text")
                assert set(pending()) == {"alice", "charlie", "external_user"}
                panel.reload_button.invoke()
                assert panel.dirty()  # Reject discarding the edited text.
                confirm.return_value = True
                panel.reload_button.invoke()
                yield lambda: not panel.loading
                assert not panel.dirty() and "@external_user" in panel.original
                app.select_campaign(other)
                yield lambda: not panel.loading and not app.task_tab._loading_campaign
                assert panel.original == "@other_user"
                app.select_campaign(1)
                yield lambda: not panel.loading and not app.task_tab._loading_campaign
                assert "@external_user" in panel.original and "@other_user" not in panel.original
                with patch.object(app.scheduler, "is_running", return_value=True):
                    app.task_tab._update_buttons_state()
                    assert panel.textbox._textbox.cget("state") == "disabled"
                    assert panel.save_button.cget("state") == "disabled"
                app.task_tab._update_buttons_state()
                panel.clear_button.invoke()
                confirm.return_value = False
                panel.save_button.invoke()
                assert len(pending()) == 3
                confirm.return_value = True
                panel.save_button.invoke()
                yield lambda: not panel.busy
                assert pending() == [] and not panel.dirty()
                assert read(db.get_targets())[0]["status"] == "sent"
                paste("@saved_user")

                async def delayed(names, expected):
                    await asyncio.sleep(0.3)
                    return await Database().replace_pending_targets(names, expected)

                with patch.object(panel.db, "replace_pending_targets", new=delayed):
                    panel.save_button.invoke()
                    app.select_campaign(other)
                    app._on_close()
                    assert app.db.campaign_id == 1 and not app._closing and panel.busy
                    yield lambda: not panel.busy
                assert pending() == ["saved_user"]
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
                    confirm.return_value = True
                    panel.busy = False
                    app._on_close()

            app.report_callback_exception = lambda kind, exc, trace: errors.append(f"{kind.__name__}: {exc}")
            app.after(500, advance)
            app.mainloop()
        thread.join(timeout=5)
        loop.close()
    (folder / "gui.json").write_text(
        json.dumps({"errors": errors, "warnings_checked": len(warnings), "live_telegram_calls": False}, indent=2),
        encoding="utf-8",
    )
    if errors:
        raise AssertionError(errors)
    print(
        "Inline recipients passed: paste, validation, save, deduplication, campaigns, conflicts, clear and exit guards."
    )


if __name__ == "__main__":
    main()
