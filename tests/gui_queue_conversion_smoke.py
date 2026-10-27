"""Offline GUI checks for the dashboard «Конверсия» section and the campaign queue («После завершения запустить»)."""

import asyncio
import json
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import patch

from core import database
from core.campaign_queue import get_link
from core.database import Database, init_db
from ui.app import App


def main():
    folder = Path(".audit/queue-conversion")
    folder.mkdir(parents=True, exist_ok=True)
    errors, started = [], []
    with (
        tempfile.TemporaryDirectory(dir=folder) as directory,
        patch.object(database, "DB_PATH", str(Path(directory) / "test.db")),
    ):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(init_db())
        db = Database()
        first = loop.run_until_complete(db.create_campaign("Первая", "invite"))
        second = loop.run_until_complete(db.create_campaign("Вторая", "invite"))
        account_id = loop.run_until_complete(
            db.add_account({"phone": "+380500001234", "api_id": 1, "api_hash": "a" * 32})
        )
        scoped, follower = db.for_campaign(first), db.for_campaign(second)
        loop.run_until_complete(scoped.bulk_insert_targets(["alice_one", "bobby_two", "carol_three"]))
        for _ in range(2):
            target = loop.run_until_complete(scoped.claim_next_pending_target())
            loop.run_until_complete(scoped.record_success(target["id"], account_id))
        target = loop.run_until_complete(scoped.claim_next_pending_target())
        loop.run_until_complete(scoped.mark_target(target["id"], "private", None, "closed"))
        loop.run_until_complete(follower.bulk_insert_targets(["dave_four", "erin_five"]))

        def runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        with (
            patch("core.account_manager.TelegramClient", side_effect=AssertionError("Live Telegram blocked")),
            patch("tkinter.messagebox.showerror", side_effect=lambda *args, **kw: errors.append(str(args))),
            patch("tkinter.messagebox.askyesno", return_value=True),
        ):
            app = App(loop)
            app.geometry("1100x800")
            task, panel = app.task_tab, app.dashboard_tab.conversion

            def read(coro):
                return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=3)

            def scenario():
                yield lambda: not task._loading_campaign
                app.select_campaign(first)
                yield lambda: not task._loading_campaign and app.db.campaign_id == first
                # -- conversion: the open campaign, 2 added + 1 private
                app._switch_tab("dashboard")
                panel.period_var.set("Всё время")
                panel.reload()
                yield lambda: panel.invite_tiles["success"].cget("text").startswith("2 ·")
                assert panel.invite_tiles["skipped"].cget("text").startswith("1 ·"), panel.invite_tiles["skipped"].cget(
                    "text"
                )
                assert "66.7%" in panel.invite_tiles["success"].cget("text")
                assert any(
                    "+380500001234" in child.cget("text")
                    for child in panel.table.winfo_children()
                    if hasattr(child, "cget") and child.cget("text")
                )
                panel.campaign_menu.set("Все кампании")
                panel._campaign_chosen("Все кампании")
                yield lambda: "Все кампании" in panel.note.cget("text")
                # -- queue picker: save the follower
                app._switch_tab("task")
                picker = next(bar for bar in app.campaign_bars if hasattr(bar, "trigger_menu"))
                yield lambda: any(name.endswith("Вторая") for name in picker.choices)
                picker.value.set(next(name for name in picker.choices if name.endswith("Вторая")))
                picker.save()
                yield lambda: read(get_link(scoped))["next_id"] == second
                picker.trigger.set("в любом случае (кроме остановки)")
                picker.save()
                yield lambda: read(get_link(scoped))["on"] == "always"
                # -- queue runner: «completed» offers the follower, the user agrees, the task is started
                with patch.object(task, "start_task", side_effect=lambda: started.append(app.db.campaign_id)):
                    app.queue_runner.offer(scoped, {"outcome": "completed"})
                    yield lambda: started
                assert started == [second], started
                assert app.db.campaign_id == second
                assert app.queue_runner.chain in ([first], [])
                # a stopped run never continues
                before = list(started)
                with patch.object(task, "start_task", side_effect=lambda: started.append(-1)):
                    app.queue_runner.offer(scoped, {"outcome": "stopped"})
                    for _ in range(3):
                        yield lambda: True
                assert started == before
                app._on_close()

            steps = scenario()
            waiting = None
            attempts = 0

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
    print("queue/conversion GUI smoke passed")


if __name__ == "__main__":
    main()
