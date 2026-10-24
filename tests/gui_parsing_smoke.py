"""Real Tk controls and database with a strictly read-only fake Telegram API."""

import asyncio
import json
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import ImageGrab
from telethon.tl.types import User, UserProfilePhoto, UserStatusRecently

from core import database
from core.database import Database, init_db
from core.export import write_parsed
from tests.support import account_payload
from tests.test_chat_authors import FakeHistoryClient, message
from tests.test_parsing import FakeClient
from ui.app import App
from ui.tab_parsing import EXPORT_FORMATS


def main():
    folder = Path(".audit/parsing")
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
        loop.run_until_complete(db.bulk_insert_targets(["person_1"]))
        users = [User(id=i, username=f"person_{i}", first_name=f"Участник {i}") for i in range(1, 251)]
        users[2].username = None
        users[3].bot = True
        users[4].deleted = True
        users[5].premium = True
        users[5].photo = UserProfilePhoto(photo_id=1, dc_id=2)
        users[5].status = UserStatusRecently()
        client = FakeClient(users=users)

        def manager_factory(*args):
            manager = AsyncMock(client=client)
            manager.get_joined_dialogs.return_value = [
                {"id": -1000000000101, "title": "Тестовая группа", "username": "group_one", "type": "group"}
            ]
            return manager

        def runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        with (
            patch("ui.notifications.WindowsNotifications.show") as notification,
            patch("core.account_manager.TelegramClient", side_effect=AssertionError("Live Telegram blocked")),
            patch("core.parser.AccountManager", side_effect=manager_factory),
            patch("core.chat_authors.AccountManager", side_effect=manager_factory),
            patch("ui.tab_parsing.AccountManager", side_effect=manager_factory),
            patch("tkinter.messagebox.showerror", side_effect=lambda *args, **kwargs: errors.append(str(args))),
            patch("tkinter.messagebox.showinfo"),
            patch("tkinter.messagebox.showwarning") as warning,
            patch("tkinter.messagebox.askyesno", return_value=True) as confirm,
        ):
            app = App(loop, show_startup_support=False)
            app.report_callback_exception = lambda kind, exc, trace: errors.append(f"{kind.__name__}: {exc}")
            app.geometry("1100x800+20+20")
            app.attributes("-topmost", True)

            def pump(predicate=lambda: False, timeout=1, required=False):
                until = time.monotonic() + timeout
                satisfied = False

                def tick():
                    nonlocal satisfied
                    satisfied = predicate()
                    if satisfied or time.monotonic() >= until:
                        app.quit()
                        return
                    app.after(20, tick)

                app.after(0, tick)
                app.mainloop()
                if required and not satisfied:
                    raise AssertionError(
                        f"GUI condition timed out: errors={errors}; status={app.parsing_tab.status.cget('text')}"
                    )

            def read(coroutine):
                future = asyncio.run_coroutine_threadsafe(coroutine, loop)
                pump(future.done, timeout=5, required=True)
                return future.result()

            def capture(name):
                app.update_idletasks()
                ImageGrab.grab(
                    bbox=(
                        app.winfo_rootx(),
                        app.winfo_rooty(),
                        app.winfo_rootx() + app.winfo_width(),
                        app.winfo_rooty() + app.winfo_height(),
                    )
                ).save(folder / name)

            pump(timeout=1)
            app.focus_force()
            app.event_generate("<Control-Key-8>")
            pump(lambda: app.current_tab_key == "parsing", required=True)
            tab = app.parsing_tab
            pump(lambda: bool(tab.accounts), required=True)
            assert tab.save_file_button.cget("state") == "disabled"
            tab.load_groups()
            pump(lambda: bool(tab.groups) and not tab.busy(), timeout=5, required=True)
            tab.add_group()
            assert tab.source_box.get("1.0", "end-1c") == "-1000000000101"
            tab.clear_button.invoke()
            with patch.object(tab.source_box._textbox, "clipboard_get", return_value="@group_one\n"):
                tab.paste_button.invoke()
            assert tab.sources() == ["group_one"]
            tab.check_access()
            pump(lambda: not tab.busy() and "список доступен" in tab.status.cget("text"), timeout=5, required=True)
            assert read(db.get_parse_runs()) == []
            tab.limit_entry.delete(0, "end")
            tab.limit_entry.insert(0, "150")
            tab.start()
            pump(lambda: not tab.busy() and len(tab.filtered) == 147, timeout=8, required=True)
            run_id = tab.selected_run
            assert len(tab.rows) == 150 and len(tab.table.get_children()) == 100
            assert tab.save_file_button.cget("state") == "normal"
            assert tab._view == "results" and tab.save_file_button.winfo_viewable()
            assert not tab.body.winfo_viewable() and tab.results_body.winfo_viewable()
            assert notification.call_count == 1
            assert "147" in tab.segment_label.cget("text")
            with patch("tkinter.filedialog.asksaveasfilename", return_value="") as cancelled_save:
                tab.save_file_button.invoke()
            assert cancelled_save.called and not tab._exporting and tab.last_export_path is None
            calls_before = len(client.requests)
            tab.filter_vars["premium"].set(True)
            tab.filter_vars["photo"].set(True)
            tab.apply_filters()
            assert [row["user_id"] for row in tab.filtered] == [6]
            tab.activity_menu.set("За 7 дней: точное время")
            tab.apply_filters()
            assert not tab.filtered
            tab.filter_vars["premium"].set(False)
            tab.filter_vars["photo"].set(False)
            tab.activity_menu.set("Все статусы")
            tab.grouping_menu.set("По группам")
            tab.apply_filters()
            assert len(tab.filtered) == 147 and len(client.requests) == calls_before
            capture("parser-sources.png")
            tab.results_body._parent_canvas.yview_moveto(1)
            pump(timeout=0.8)
            capture("parser-results.png")
            tab.change_page(1)
            assert len(tab.table.get_children()) == 47
            for format_name in ("csv", "json", "txt"):
                destination = folder / f"gui-export.{format_name}"
                with patch("tkinter.filedialog.asksaveasfilename", return_value=str(destination)):
                    tab.export_format.set(
                        next(label for label, value in EXPORT_FORMATS.items() if value == format_name)
                    )
                    tab.save_file_button.invoke()
                pump(
                    lambda path=destination: (
                        path.exists() and "Файл сохранён на ПК" in tab.status.cget("text") and not tab._exporting
                    ),
                    timeout=5,
                    required=True,
                )
            assert len(json.loads((folder / "gui-export.json").read_text(encoding="utf-8"))) == 147
            assert len((folder / "gui-export.txt").read_text(encoding="utf-8").splitlines()) == 147
            last_path = tab.last_export_path
            assert read(db.get_setting("parser_last_export_path")) == str(last_path)
            with patch("ui.tab_parsing.os.startfile") as open_folder:
                tab.open_folder_button.invoke()
                open_folder.assert_called_once_with(str(last_path.parent))
            tab.last_export_path = None
            tab.restore_export_path()
            pump(lambda: tab.last_export_path == last_path, timeout=5, required=True)
            with (
                patch("ui.tab_parsing.write_parsed", side_effect=OSError("Disk full")),
                patch("tkinter.filedialog.asksaveasfilename", return_value=str(folder / "failed.csv")),
                patch("tkinter.messagebox.showerror") as save_error,
            ):
                tab.export("csv")
                pump(lambda: not tab._exporting, timeout=5, required=True)
                assert save_error.called and "Файл не сохранён" in tab.status.cget("text")
            assert tab.last_export_path == last_path and len(tab.filtered) == 147
            confirm.return_value = False
            tab.to_audience()
            assert read(db.get_targets_stats())["total"] == 1
            confirm.return_value = True
            tab.to_audience()
            pump(lambda: "добавлено 146" in tab.status.cget("text"), timeout=5, required=True)
            tab.to_audience()
            pump(lambda: "добавлено 0" in tab.status.cget("text"), timeout=5, required=True)
            assert read(db.get_targets_stats())["total"] == 147
            assert not app.scheduler.is_running()
            tab.delete_run()
            pump(lambda: not tab.filtered, timeout=5, required=True)
            assert read(db.get_parse_results(run_id)) == ([], [])
            assert read(db.get_targets_stats())["total"] == 147
            client = FakeClient(users=users, block_offset=200)
            tab.limit_entry.delete(0, "end")
            tab.limit_entry.insert(0, "500")
            tab.start()
            pump(lambda: client.entered.is_set(), timeout=8, required=True)
            assert tab.start_button.cget("text") == "Получаем участников..."
            app.task_tab.start_task()
            assert warning.called and not app.task_tab._starting and not app.scheduler.is_running()
            tab.stop()
            pump(lambda: not tab.busy() and len(tab.rows) == 200, timeout=8, required=True)
            assert tab.start_button.cget("text") == "Получить участников"
            assert read(db.get_parse_runs())[0]["status"] == "stopped"
            app.geometry("900x600+20+20")
            tab.body._parent_canvas.yview_moveto(0)
            pump(timeout=0.8)
            for button in (
                tab.save_file_button,
                tab.open_folder_button,
            ):
                assert button.winfo_ismapped(), (
                    f"Button {button.cget('text')} unmapped; tab={app.current_tab_key}, root_state={app.state()}, page_mapped={tab.winfo_ismapped()}"
                )
                assert button.winfo_rooty() + button.winfo_height() <= app.winfo_rooty() + app.winfo_height()
            capture("parser-minimum.png")
            tab.show_view("setup")
            pump(timeout=0.3)
            assert tab.start_button.winfo_viewable() and tab.check_button.winfo_viewable()
            assert not tab.save_file_button.winfo_viewable()
            author_folder = Path(".audit/chat-authors")
            author_folder.mkdir(parents=True, exist_ok=True)
            author_users = [User(id=i, username=f"author_{i}") for i in range(1, 106)]
            now = datetime.now(timezone.utc)
            senders = [1] * 6 + list(range(2, 106))
            history = [
                message(1000 - index, sender, when=now - timedelta(minutes=index + 1))
                for index, sender in enumerate(senders)
            ]
            client = FakeHistoryClient(messages=history, users=author_users)
            tab.mode_menu.set("Авторы сообщений")
            tab.mode_changed()
            assert tab.limit_entry.get() == "2000"
            assert tab.start_button.cget("text") == "Получить авторов"
            tab.period_menu.set("Свои даты")
            tab.period_changed()
            tab.start()
            assert "Введите обе даты" in tab.status.cget("text")
            tab.date_from.insert(0, (now.astimezone() - timedelta(days=2)).strftime("%Y-%m-%d"))
            tab.date_to.insert(0, now.astimezone().strftime("%Y-%m-%d"))
            tab.check_access()
            pump(lambda: not tab.busy() and "история доступна" in tab.status.cget("text"), timeout=5, required=True)
            tab.start()
            assert tab.date_from.cget("state") == "disabled"
            pump(
                lambda: not tab.busy() and tab._result_mode == "authors" and len(tab.filtered) == 105,
                timeout=8,
                required=True,
            )
            author_run = tab.selected_run
            assert tab.role_menu.cget("state") == "disabled"
            assert tab.min_messages_menu.cget("state") == "normal"
            assert sum(person["message_count"] for person in tab.filtered) == 110
            tab.min_messages_menu.set("От 5 сообщений")
            tab.last_post_menu.set("Писал за последний день")
            tab.apply_filters()
            assert [(person["user_id"], person["message_count"]) for person in tab.filtered] == [(1, 6)]
            tab.min_messages_menu.set("Любое число")
            tab.last_post_menu.set("Любая дата сообщения")
            tab.grouping_menu.set("По числу сообщений")
            tab.apply_filters()
            assert tab.filtered[0]["message_count"] == 6
            tab.body._parent_canvas.yview_moveto(0)
            app.geometry("1100x800+20+20")
            pump(timeout=0.8)
            capture("authors-sources.png")
            tab.results_body._parent_canvas.yview_moveto(1)
            pump(timeout=0.8)
            capture("authors-results.png")
            tab.change_page(1)
            assert len(tab.table.get_children()) == 5
            for format_name in ("csv", "json", "txt"):
                destination = author_folder / f"gui-export.{format_name}"
                with patch("tkinter.filedialog.asksaveasfilename", return_value=str(destination)):
                    tab.export(format_name)
                pump(
                    lambda path=destination: (
                        path.exists() and "Файл сохранён на ПК" in tab.status.cget("text") and not tab._exporting
                    ),
                    timeout=5,
                    required=True,
                )
            exported = json.loads((author_folder / "gui-export.json").read_text(encoding="utf-8"))
            assert len(exported) == 105 and sum(person["message_count"] for person in exported) == 110
            assert "SECRET_BODY" not in str(exported)
            confirm.return_value = False
            tab.to_audience()
            assert read(db.get_targets_stats())["total"] == 147
            confirm.return_value = True
            tab.to_audience()
            pump(lambda: "добавлено 105" in tab.status.cget("text"), timeout=5, required=True)
            assert not app.scheduler.is_running()
            tab.mode_menu.set("Участники группы")
            tab.mode_changed()
            assert tab.limit_entry.get() == "500"
            assert not tab.author_options.winfo_ismapped()
            key = next(key for key, value in tab.runs.items() if value["mode"] == "members")
            tab.choose_run(key)
            pump(lambda: tab._result_mode == "members", timeout=5, required=True)
            assert tab.min_messages_menu.cget("state") == "disabled"
            assert tab.role_menu.cget("state") == "normal"
            tab.choose_run(next(key for key, value in tab.runs.items() if value["id"] == author_run))
            pump(lambda: tab._result_mode == "authors" and len(tab.filtered) == 105, timeout=5, required=True)
            tab.mode_menu.set("Авторы сообщений")
            tab.mode_changed()
            blocked_history = [
                message(1000 - index, 1 + index % 2, when=now - timedelta(minutes=index + 1)) for index in range(250)
            ]
            client = FakeHistoryClient(messages=blocked_history, users=author_users, block_offset=901)
            tab.start()
            pump(lambda: client.entered.is_set(), timeout=8, required=True)
            tab.stop()
            pump(lambda: not tab.busy() and len(tab.rows) == 2, timeout=8, required=True)
            assert sum(person["message_count"] for person in tab.rows) == 100
            assert read(db.get_parse_runs())[0]["status"] == "stopped"
            client = FakeHistoryClient(messages=blocked_history, users=author_users, block_offset=901)
            tab.start()
            pump(lambda: client.entered.is_set(), timeout=8, required=True)
            app.geometry("900x600+20+20")
            pump(timeout=0.8)
            assert tab.save_file_button.winfo_ismapped()
            save_entered, save_release = threading.Event(), threading.Event()
            final_export = author_folder / "saved-during-close.csv"

            def slow_write(path, rows, format_name):
                save_entered.set()
                assert save_release.wait(timeout=5)
                write_parsed(path, rows, format_name)

            with (
                patch("ui.tab_parsing.write_parsed", side_effect=slow_write),
                patch("tkinter.filedialog.asksaveasfilename", return_value=str(final_export)) as save_dialog,
            ):
                tab.export("csv")
                pump(save_entered.is_set, timeout=5, required=True)
                tab.export("json")
                assert save_dialog.call_count == 1
                app.after(200, save_release.set)
                app._on_close()
                pump(timeout=3)
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert loop.run_until_complete(db.get_parse_runs())[0]["status"] == "stopped"
            final_run = loop.run_until_complete(db.get_parse_runs())[0]["id"]
            loop.run_until_complete(init_db())
            final_rows, _ = loop.run_until_complete(db.get_parse_results(final_run))
            assert sum(person["message_count"] for person in final_rows) == 100
            assert final_export.is_file()
            assert loop.run_until_complete(db.get_setting("parser_last_export_path")) == str(final_export.resolve())
            loop.close()
    (folder / "gui-result.json").write_text(
        json.dumps(
            {
                "errors": errors,
                "screens": 8,
                "filters": True,
                "exports_all_pages": True,
                "paste_and_clear": True,
                "explicit_audience_import": True,
                "stop_and_close_preserve_results": True,
                "live_telegram_calls": False,
                "authors_mode": True,
                "period_and_message_filters": True,
                "authors_counters_survive_shutdown": True,
                "visible_save_and_folder": True,
                "export_cancel_failure_restore_and_shutdown": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    assert not errors, errors
    print(
        "Parsing GUI passed: access check, paste, filters, pagination, exports, audience, deletion, stop and active shutdown."
    )


if __name__ == "__main__":
    main()
