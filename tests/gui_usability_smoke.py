"""Exercise the new everyday controls offline, with disposable accounts and clipboard."""

import asyncio
import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import customtkinter as ctk
from PIL import ImageGrab

from core import database
from core.database import Database, init_db
from tests.support import account_payload
from ui.app import App


def main():
    folder = Path(".audit/usability")
    folder.mkdir(parents=True, exist_ok=True)
    errors = []
    with (
        tempfile.TemporaryDirectory(dir=folder) as directory,
        patch.object(database, "DB_PATH", str(Path(directory) / "ui.db")),
    ):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(init_db())
        db = Database()
        account_id = loop.run_until_complete(db.add_account(account_payload()))
        second_account = loop.run_until_complete(db.add_account(account_payload(2)))
        loop.run_until_complete(db.toggle_account_enabled(second_account, False))
        campaign_id = loop.run_until_complete(db.create_campaign("Тест интерфейса", "messages"))
        scoped = db.for_campaign(campaign_id)
        loop.run_until_complete(scoped.bulk_insert_targets(["alice", "bobby"]))
        loop.run_until_complete(scoped.set_setting("message_text", "Текущий текст"))
        loop.run_until_complete(scoped.add_log(None, "alice", "MESSAGE_PRIVATE", "Личка закрыта"))
        loop.run_until_complete(scoped.add_log(None, "bobby", "MESSAGE_SENT", "Сообщение отправлено"))

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
            app = App(loop, show_startup_support=False)
            app.geometry("900x600+20+20")
            app.attributes("-topmost", True)
            template = None
            management = None
            failure_patch = None
            copied_id = None

            def failed(kind, exc, trace):
                nonlocal failure_patch
                errors.append(f"{kind.__name__}: {exc}")
                if failure_patch:
                    failure_patch.stop()
                    failure_patch = None
                app._on_close()

            app.report_callback_exception = failed

            def wait_for(predicate, callback, attempts=100):
                if predicate():
                    callback()
                elif attempts:
                    app.after(50, lambda: wait_for(predicate, callback, attempts - 1))
                else:
                    raise AssertionError("Usability callback timed out")

            def read(coro):
                return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=3)

            def capture(name, window=app):
                window.update_idletasks()
                ImageGrab.grab(
                    bbox=(
                        window.winfo_rootx(),
                        window.winfo_rooty(),
                        window.winfo_rootx() + window.winfo_width(),
                        window.winfo_rooty() + window.winfo_height(),
                    )
                ).save(folder / name)

            def start():
                app.select_campaign(campaign_id)
                app._switch_tab("task")
                wait_for(lambda: not app.task_tab._loading_campaign and bool(app.task_tab.account_vars), task_controls)

            def task_controls():
                task = app.task_tab
                assert account_id in task.account_vars and second_account not in task.account_vars
                assert not task._section_cards[2].winfo_manager()
                assert task._step == 0 and task.start_btn.cget("text").startswith("Далее:")
                task.start_btn.invoke()
                assert task._step == 1 and task.details_card.winfo_manager() == "pack"
                assert not task._section_cards[0].winfo_manager()
                task.start_btn.invoke()
                assert task._step == 2 and task._section_cards[4].winfo_manager() == "pack"
                assert not app.scheduler.is_running()
                task.select_none_button.invoke()
                assert not any(var.get() for var in task.account_vars.values())
                assert "Отметьте аккаунты" in task.readiness_label.cget("text")
                task.select_all_button.invoke()
                assert all(var.get() for var in task.account_vars.values())
                task.advanced_var.set(True)
                task.toggle_advanced()
                assert task._section_cards[2].winfo_manager() == "pack"
                task.schedule_enabled.set(True)
                task.advanced_var.set(False)
                task.toggle_advanced()
                assert "Расписание" in task.readiness_label.cget("text")
                task.schedule_enabled.set(False)
                task.message_box.delete("1.0", "end")
                task.message_box.insert("1.0", "")
                task._last_stats = {"pending": 2}
                task.update_readiness()
                assert "Введите текст" in task.readiness_label.cget("text")
                task.message_box.insert("1.0", "Текущий текст")
                task.start_btn.invoke()
                assert task._step == 3 and task.start_btn.cget("text") == "Запустить задачу"
                assert "Текущий текст" in task.review_box.get("1.0", "end-1c")
                assert not app.scheduler.is_running()
                task.back_btn.invoke()
                assert task._step == 2
                task._scroll_section(1)
                task.flush_draft()
                app.after(700, open_template)

            def open_template():
                nonlocal template
                capture("task-simple.png")
                template = app.task_tab.open_templates()
                template.attributes("-topmost", True)
                template.geometry("580x450+80+80")
                template.name.insert(0, "Встреча")
                template.textbox.delete("1.0", "end")
                template.textbox.insert("1.0", "Текст шаблона 😀\nСсылка: https://t.me/example")
                template.save_button.invoke()
                wait_for(lambda: not template.busy and template.selected_id is not None, saved_template)

            def saved_template():
                wait_for(lambda: bool(template.templates), test_failed_template)

            def test_failed_template():
                nonlocal failure_patch
                failure_patch = patch.object(
                    template.db, "save_message_template", new=AsyncMock(side_effect=OSError("Тест ошибки записи"))
                )
                failure_patch.start()
                template.save_button.invoke()
                wait_for(lambda: not template.busy, retry_template)

            def retry_template():
                nonlocal failure_patch
                assert "Тест ошибки записи" in template.status.cget("text")
                assert "Текст шаблона" in template.textbox.get("1.0", "end-1c")
                failure_patch.stop()
                failure_patch = None
                app.after(300, use_template)

            def use_template():
                capture("template-minimum.png", template)
                confirm.return_value = False
                template.name.insert("end", " правка")
                app._on_close()
                assert not app._closing and template.winfo_exists()
                template.name.delete(0, "end")
                template.name.insert(0, "Встреча")
                template.use_button.invoke()
                assert app.task_tab.message_box.get("1.0", "end-1c") == "Текущий текст"
                confirm.return_value = True
                template.use_button.invoke()
                assert "Текст шаблона" in app.task_tab.message_box.get("1.0", "end-1c")
                assert not app.scheduler.is_running()
                app.task_tab.save_settings()
                wait_for(lambda: read(scoped.get_setting("message_text")).startswith("Текст шаблона"), campaigns)

            def campaigns():
                nonlocal management
                bar = next(bar for bar in app.campaign_bars if bar.winfo_viewable())
                management = bar.manage()
                management.attributes("-topmost", True)
                management.name_entry.delete(0, "end")
                management.name_entry.insert(0, "Копия для теста")
                management.copy_button.invoke()
                wait_for(lambda: not management.winfo_exists() and app.db.campaign_id != campaign_id, copied)

            def copied():
                nonlocal copied_id
                copied_id = app.db.campaign_id
                assert read(app.db.count_targets()) == 0
                assert read(app.db.get_setting("message_text")).startswith("Текст шаблона")
                app.select_campaign(campaign_id)
                app._switch_tab("accounts")
                app.accounts_tab.filter_var.set("Выключенные")

                def labels(parent):
                    return [
                        child.cget("text") for child in parent.winfo_children() if isinstance(child, ctk.CTkLabel)
                    ] + [value for child in parent.winfo_children() for value in labels(child)]

                texts = labels(app.accounts_tab.table)
                assert account_payload(2)["phone"] in texts and account_payload()["phone"] not in texts
                app.accounts_tab.search_var.set("no-match")
                assert "Нет совпадений" in labels(app.accounts_tab.table)
                app.accounts_tab.filter_var.set("Все")
                app.accounts_tab.search_var.set("")
                app._switch_tab("logs")
                app.logs_tab.filter_var.set("Пропуски")
                app.after(500, filtered_logs)

            def filtered_logs():
                value = app.logs_tab.log_box.get("1.0", "end")
                assert "@alice" in value and "@bobby" not in value
                assert str(app.logs_tab.log_box._textbox.cget("state")) == "disabled"
                app.logs_tab.search_var.set("bobby")
                app.logs_tab.filter_var.set("Все события")
                app.after(500, searched_logs)

            def searched_logs():
                value = app.logs_tab.log_box.get("1.0", "end")
                assert "@bobby" in value and "@alice" not in value
                capture("journal-filter.png")
                app._switch_tab("warmup")
                app.warmup_tab.ai_hourly_entry.delete(0, "end")
                app.warmup_tab.ai_hourly_entry.insert(0, "999")
                app.warmup_tab._save_ai_behavior_settings()
                assert read(db.get_setting("ai_hourly_limit", "6")) == "6"
                with patch("ui.tab_ai_settings.ctk.CTkInputDialog") as input_dialog:
                    app.ai_tab._preview_inflight = True
                    app.ai_tab._preview_config(None)
                    input_dialog.assert_not_called()
                    app.ai_tab._preview_inflight = False
                app._switch_tab("task")
                app.after(350, finish)

            def finish():
                assert read(scoped.get_setting("message_text")).startswith("Текст шаблона")
                assert read(db.for_campaign(copied_id).get_setting("task_status", "stopped")) == "stopped"
                app._on_close()

            app.after(600, start)
            app.after(20000, lambda: failed(RuntimeError, RuntimeError("Usability smoke timed out"), None))
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
                "templates_save_apply_failed_write": True,
                "copy_isolated_campaign": True,
                "simple_advanced_and_readiness": True,
                "log_search_filter_readonly": True,
                "live_telegram_calls": False,
            }
        ),
        encoding="utf-8",
    )
    assert not errors, errors
    print("Usability GUI passed: task controls, templates, campaign copy, account filters, and journal search.")


if __name__ == "__main__":
    main()
