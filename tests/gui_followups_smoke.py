"""Offline GUI checks for the two-message form and persisted campaign drafts."""

import asyncio
import json
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import Mock, patch

from core import database
from core.database import Database, init_db
from ui.app import App


def main():
    folder = Path(".audit/reply-followup")
    folder.mkdir(parents=True, exist_ok=True)
    errors = []
    with (
        tempfile.TemporaryDirectory(dir=folder) as directory,
        patch.object(database, "DB_PATH", str(Path(directory) / "test.db")),
    ):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(init_db())
        db = Database()
        campaign = loop.run_until_complete(db.create_campaign("Two messages", "messages"))
        other = loop.run_until_complete(db.create_campaign("Other", "messages"))
        account_id = loop.run_until_complete(
            db.add_account({"phone": "+380500001234", "api_id": 1, "api_hash": "a" * 32})
        )
        scoped = db.for_campaign(campaign)
        loop.run_until_complete(scoped.bulk_insert_targets(["alice"]))
        target = loop.run_until_complete(scoped.claim_next_pending_target())
        loop.run_until_complete(
            scoped.reserve_delivery(
                target["id"], account_id, 123, followup_text="Original queued second", access_hash=456
            )
        )
        loop.run_until_complete(scoped.finish_delivery(target["id"], "sent", 10))

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
            app.geometry("1000x760")
            task = app.task_tab

            def read(coro):
                return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=3)

            def paste(box, text):
                native = box._textbox
                native.clipboard_get = Mock(return_value=text)
                native.tag_add("sel", "1.0", "end-1c")
                button = task.paste_message_btn if box is task.message_box else task.paste_followup_btn
                button.invoke()
                assert box.get("1.0", "end-1c") == text

            def scenario():
                yield lambda: not task._loading_campaign
                app.select_campaign(campaign)
                app._switch_tab("task")
                yield lambda: not task._loading_campaign and not task.recipients.loading
                assert not task.followup_enabled.get() and not task.followup_frame.winfo_manager()
                paste(task.message_box, "First message")
                task.followup_switch.toggle()
                assert task.followup_frame.winfo_manager() == "pack"
                try:
                    task.task_values()
                    raise AssertionError("Empty second accepted")
                except ValueError as exc:
                    assert "Второе сообщение" in str(exc)
                task._scroll_section(1)
                yield lambda: bool(task.followup_box.winfo_viewable())
                paste(task.followup_box, "Second message\nWith another line 🚀")
                expected_length = len("Second message\nWith another line 🚀".encode("utf-16-le")) // 2
                yield lambda: task.followup_counter.cget("text") == f"{expected_length} / 4096"
                task.flush_draft()
                yield lambda: read(scoped.get_setting("followup_text", "")).startswith("Second message")
                assert read(scoped.get_setting("followup_enabled")) == "1"
                rows = read(scoped.get_followups())
                assert rows[0]["text"] == "Original queued second"
                app.select_campaign(other)
                yield lambda: not task._loading_campaign
                assert not task.followup_enabled.get() and task.followup_box.get("1.0", "end-1c") == ""
                app.select_campaign(campaign)
                yield lambda: not task._loading_campaign
                assert task.message_box.get("1.0", "end-1c") == "First message"
                assert task.followup_enabled.get() and task.followup_box.get("1.0", "end-1c").endswith("🚀")
                with patch.object(app.scheduler, "is_running", return_value=True):
                    task._update_buttons_state()
                    assert task.start_btn.cget("text") == "Задача запущена"
                    assert task.followup_switch.cget("state") == "disabled"
                    assert task.followup_box._textbox.cget("state") == "disabled"
                task._update_buttons_state()
                assert task.start_btn.cget("text") == "Далее: аккаунты и темп"
                task.cancel_followups_btn.invoke()
                yield lambda: read(scoped.get_followup_stats()).get("cancelled") == 1
                assert read(scoped.delivery_exists("alice")) == "sent"
                task.followup_switch.toggle()
                yield lambda: read(scoped.get_setting("followup_enabled")) == "0"
                assert task.followup_box.get("1.0", "end-1c").endswith("🚀")
                app._on_close()

            steps = scenario()
            waiting = None
            attempts = 0

            def advance():
                nonlocal waiting, attempts
                try:
                    if waiting and not waiting():
                        attempts += 1
                        assert attempts < 200, (
                            f"UI callback timeout at line {steps.gi_frame.f_lineno}; counter={task.followup_counter.cget('text')}; second={task.followup_box.get('1.0', 'end-1c')!r}"
                        )
                    else:
                        waiting, attempts = next(steps), 0
                    if not app._closing:
                        app.after(50, advance)
                except StopIteration:
                    pass
                except Exception:
                    errors.append(traceback.format_exc())
                    (folder / "gui-failure.json").write_text(json.dumps(errors, indent=2), encoding="utf-8")
                    app._on_close()

            app.report_callback_exception = lambda kind, exc, trace: errors.append(f"{kind.__name__}: {exc}")
            app.after(2000, advance)
            app.mainloop()
        thread.join(timeout=5)
        if not loop.is_running():
            if not errors:
                assert loop.run_until_complete(scoped.get_setting("followup_text")).endswith("🚀")
            loop.close()
    (folder / "gui.json").write_text(
        json.dumps({"errors": errors, "live_telegram_calls": False}, indent=2), encoding="utf-8"
    )
    if errors:
        raise AssertionError(errors)
    print(
        "Two-message GUI passed: paste, validation, draft persistence, campaign scope, frozen queued text, cancel and restart."
    )


if __name__ == "__main__":
    main()
