"""Offline GUI checks for the A/B text form, the audience A/B summary and campaign export/import."""

import asyncio
import json
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import patch

from core import database
from core.database import Database, init_db
from ui.app import App


def main():
    folder = Path(".audit/ab-transfer")
    folder.mkdir(parents=True, exist_ok=True)
    errors = []
    with (
        tempfile.TemporaryDirectory(dir=folder) as directory,
        patch.object(database, "DB_PATH", str(Path(directory) / "test.db")),
    ):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(init_db())
        db = Database()
        campaign = loop.run_until_complete(db.create_campaign("A/B", "messages"))
        account_id = loop.run_until_complete(
            db.add_account({"phone": "+380500001234", "api_id": 1, "api_hash": "a" * 32})
        )
        scoped = db.for_campaign(campaign)
        loop.run_until_complete(scoped.bulk_insert_targets(["alice", "bobby", "carol_x"]))
        for index in range(2):
            target = loop.run_until_complete(scoped.claim_next_pending_target())
            loop.run_until_complete(scoped.reserve_delivery(target["id"], account_id, 100 + index, variant="AB"[index]))
            loop.run_until_complete(scoped.finish_delivery(target["id"], "sent", 10 + index))
        export_path = str(Path(directory) / "campaign.json")

        def runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        with (
            patch("core.account_manager.TelegramClient", side_effect=AssertionError("Live Telegram blocked")),
            patch("tkinter.messagebox.showerror", side_effect=lambda *args, **kw: errors.append(str(args))),
            patch("tkinter.messagebox.askyesno", return_value=True),
            patch("tkinter.messagebox.askyesnocancel", return_value=True),
            patch("tkinter.messagebox.showinfo"),
        ):
            app = App(loop)
            app.geometry("1000x760")
            task = app.task_tab

            def read(coro):
                return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=5)

            def scenario():
                yield lambda: not task._loading_campaign
                app.select_campaign(campaign)
                app._switch_tab("task")
                yield lambda: not task._loading_campaign and not task.recipients.loading
                assert not task.ab_enabled.get() and not task.ab_frame.winfo_manager()
                task.message_box.insert("1.0", "Text A")
                task.ab_switch.toggle()
                assert task.ab_frame.winfo_manager() == "pack"
                try:
                    task.task_values()
                    raise AssertionError("Empty text B accepted")
                except ValueError as exc:
                    assert "Текст B" in str(exc), exc
                task.ab_box.insert("1.0", "Text B")
                task.ab_split_entry.delete(0, "end")
                task.ab_split_entry.insert(0, "100")
                try:
                    task.task_values()
                    raise AssertionError("Split 100 accepted")
                except ValueError as exc:
                    assert "Доля варианта A" in str(exc), exc
                task.ab_split_entry.delete(0, "end")
                task.ab_split_entry.insert(0, "40")
                task._ab_split_changed()
                assert "A 40%" in task.ab_split_label.cget("text")
                settings = task.task_values()[-1]
                assert (settings["ab_enabled"], settings["ab_split_a"]) == ("1", "40")
                assert settings["message_text_b"] == "Text B"
                review = "\n".join(task._ab_review_lines())
                assert "A 40% / B 60%" in review and "Text B" in review, review
                task.flush_draft()
                yield lambda: read(scoped.get_setting("ab_enabled")) == "1"
                assert read(scoped.get_setting("message_text_b")) == "Text B"
                task.save_settings()
                yield lambda: read(scoped.get_setting("ab_split_a")) == "40"
                # Audience tab shows the sent split.
                app._switch_tab("targets")
                app.targets_tab.refresh_ab_stats()
                yield lambda: "A — 1, B — 1" in app.targets_tab.ab_label.cget("text")
                assert app.targets_tab.ab_label.winfo_manager() == "pack"
                # Export, then import creates a new campaign without touching this one.
                bar = app.campaign_bars[0]
                with patch("tkinter.filedialog.asksaveasfilename", return_value=export_path):
                    bar.export_file()
                yield lambda: Path(export_path).exists() and Path(export_path).stat().st_size > 0
                data = json.loads(Path(export_path).read_text(encoding="utf-8"))
                assert data["settings"]["message_text_b"] == "Text B" and len(data["targets"]) == 3
                assert "api_hash" not in json.dumps(data)
                before = {row["id"] for row in read(db.get_campaigns())}
                with patch("tkinter.filedialog.askopenfilename", return_value=export_path):
                    bar.import_file()
                yield lambda: {row["id"] for row in read(db.get_campaigns())} != before
                new_id = max(row["id"] for row in read(db.get_campaigns()))
                imported = db.for_campaign(new_id)
                assert read(imported.get_setting("message_text_b")) == "Text B"
                assert read(imported.count_targets()) == 3
                yield lambda: not task._loading_campaign and app.db.campaign_id == new_id
                assert task.ab_enabled.get() and task.ab_box.get("1.0", "end-1c") == "Text B"
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
    if errors:
        raise AssertionError(errors)
    print("A/B GUI passed: form, validation, draft, review, audience summary, export and import.")


if __name__ == "__main__":
    main()
