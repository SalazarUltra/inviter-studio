"""Offline UI integration check with screenshots; uses a temporary empty DB."""

import asyncio
import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import AsyncMock, patch

import customtkinter as ctk
from PIL import ImageGrab
from telethon.tl.types import User

from core import database
from core.database import Database, init_db
from tests.support import account_payload
from ui.app import App


def main():
    errors = []
    captures = Path(".audit/redesign/ui")
    captures.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(dir=".audit/redesign") as directory,
        patch.object(database, "DB_PATH", str(Path(directory) / "ui.db")),
    ):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(init_db())
        db = Database()
        loop.run_until_complete(db.add_account(account_payload()))
        disabled_id = loop.run_until_complete(db.add_account(account_payload(2)))
        loop.run_until_complete(db.toggle_account_enabled(disabled_id, False))
        loop.run_until_complete(db.bulk_insert_targets(["alice", "bobby", "charlie"]))
        second_id = loop.run_until_complete(db.create_campaign("Тестовая кампания"))
        second = db.for_campaign(second_id)
        loop.run_until_complete(second.bulk_insert_targets(["second_user"]))
        loop.run_until_complete(
            second.save_campaign_settings({"target_chat": "second_chat", "interval_min": "30", "interval_max": "60"})
        )
        message_id = loop.run_until_complete(db.create_campaign("Тест сообщений", "messages"))
        message_db = db.for_campaign(message_id)
        loop.run_until_complete(message_db.bulk_insert_targets(["message_user"]))
        loop.run_until_complete(
            message_db.save_campaign_settings(
                {"operation_mode": "messages", "message_text": "Тестовый текст без реальной отправки"}
            )
        )

        def runner():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        with (
            patch("ui.notifications.WindowsNotifications.show"),
            patch(
                "core.account_manager.TelegramClient", side_effect=AssertionError("Live Telegram blocked in UI smoke")
            ),
            patch("core.inviter.AccountManager") as manager_factory,
            patch("core.messenger.AccountManager") as message_manager_factory,
            patch("core.diagnostics.AccountManager") as diagnostic_manager_factory,
            patch("tkinter.messagebox.showerror", side_effect=lambda *args, **kwargs: errors.append(str(args))),
            patch("tkinter.messagebox.showinfo"),
            patch("tkinter.messagebox.askyesno", return_value=True),
        ):
            app = App(loop, show_startup_support=False)
            manager = AsyncMock()
            manager.account = account_payload()
            manager.check_admin_rights.return_value = {"ok": True}
            manager.add_user_to_chat.return_value = {"success": True}
            manager_factory.return_value = manager
            message_manager = AsyncMock()
            message_manager.account = account_payload()
            message_manager.resolve_recipient.return_value = User(id=88001)
            message_manager.send_direct_message.return_value = {"success": True}
            message_manager_factory.return_value = message_manager
            diagnostic_manager_factory.return_value = AsyncMock()
            app.report_callback_exception = lambda kind, exc, trace: errors.append(f"{kind.__name__}: {exc}")
            app.geometry("1280x800+10+10")
            app.attributes("-topmost", True)
            pages = iter(app.page_frames)

            def capture_next():
                key = next(pages, None)
                if key is None:
                    app.geometry("900x600+50+50")
                    app._switch_tab("task")
                    app.after(1000, capture_minimum)
                    return
                app._switch_tab(key)
                app.after(600, lambda: capture(key))

            def check_campaigns():
                app.select_campaign(second_id)
                app.after(650, verify_campaign)

            def verify_campaign():
                if app.task_tab.chat_entry.get() != "second_chat":
                    errors.append("Campaign chat was not restored")
                if app.task_tab.interval_min_entry.get() != "30":
                    errors.append("Campaign interval was not restored")
                if "1" not in app.targets_tab.page_label.cget("text"):
                    errors.append("Campaign audience was not refreshed")
                app.select_campaign(1)
                app.after(650, check_preview)

            def check_preview():
                app.task_tab.chat_entry.insert(0, "test_group")
                app.task_tab.check_task(False)
                app.after(650, close_preview)

            def close_preview():
                if app.task_tab._starting:
                    errors.append("Offline diagnostics did not complete")
                for child in app.winfo_children():
                    if isinstance(child, ctk.CTkToplevel):
                        child.destroy()
                app.after(25, capture_next)

            def capture(key):
                app.lift()
                app.focus_force()
                app.update_idletasks()
                print(f"Capturing {key}, alpha={app.attributes('-alpha')}", flush=True)
                if key == "task" and disabled_id in app.task_tab.account_vars:
                    errors.append("Disabled account is selectable for a task")
                for name, frame in app.page_frames.items():
                    if bool(frame.winfo_ismapped()) != (name == key):
                        errors.append(f"Unexpected visible page: {name}, selected {key}")
                if key == "warmup":
                    assert_visible(app.warmup_tab.btn_start)
                    assert_visible(app.warmup_tab.btn_stop)
                    app.warmup_tab._set_warmup_controls(True)
                    if app.warmup_tab.btn_start.cget("text") != "Прогрев запущен":
                        errors.append("Warmup start label does not reflect running state")
                    app.warmup_tab._set_warmup_controls(False)
                box = (
                    app.winfo_rootx(),
                    app.winfo_rooty(),
                    app.winfo_rootx() + app.winfo_width(),
                    app.winfo_rooty() + app.winfo_height(),
                )
                ImageGrab.grab(bbox=box).save(captures / f"{key}.png")
                app.after(25, capture_next)

            def capture_minimum():
                assert_visible(app.task_tab.start_btn)
                assert not app.task_tab.stop_btn.winfo_manager()
                assert_visible(app.density_menu)
                assert_visible(app.profile_menu)
                capture_box = (
                    app.winfo_rootx(),
                    app.winfo_rooty(),
                    app.winfo_rootx() + app.winfo_width(),
                    app.winfo_rooty() + app.winfo_height(),
                )
                ImageGrab.grab(bbox=capture_box).save(captures / "task-minimum.png")
                app.select_campaign(message_id)
                app.after(650, capture_messages)

            def capture_messages():
                app.task_tab._section_buttons[0].invoke()
                app.update_idletasks()
                assert_visible(app.task_tab.start_btn)
                assert not app.task_tab.stop_btn.winfo_manager()
                assert_visible(app.profile_menu)
                assert_visible(app.density_menu)
                assert_visible(app.task_tab.mode_menu)
                if app.task_tab.chat_controls.winfo_ismapped():
                    errors.append("Message campaign incorrectly requires a chat")
                if app.task_tab.message_box.get("1.0", "end-1c") != "Тестовый текст без реальной отправки":
                    errors.append("Message text was not restored")
                box = (
                    app.winfo_rootx(),
                    app.winfo_rooty(),
                    app.winfo_rootx() + app.winfo_width(),
                    app.winfo_rooty() + app.winfo_height(),
                )
                ImageGrab.grab(bbox=box).save(captures / "messages-minimum.png")
                app.geometry("1280x800+10+10")
                app.after(650, capture_messages_wide)

            def capture_messages_wide():
                app.task_tab._section_buttons[1].invoke()
                app.update_idletasks()
                box = (
                    app.winfo_rootx(),
                    app.winfo_rooty(),
                    app.winfo_rootx() + app.winfo_width(),
                    app.winfo_rooty() + app.winfo_height(),
                )
                ImageGrab.grab(bbox=box).save(captures / "messages.png")
                app.task_tab.start_task()
                app.after(650, verify_messages)

            def verify_messages():
                if app.task_tab._starting or app.scheduler.is_running():
                    app.after(100, verify_messages)
                    return
                if message_manager.send_direct_message.await_count != 1:
                    errors.append("Message task did not send exactly once through the mock")
                if message_manager.check_admin_rights.await_count:
                    errors.append("Message task unnecessarily checked chat admin rights")
                app.select_campaign(1)
                app.after(650, lambda: (app.task_tab._section_buttons[-1].invoke(), app.after(600, capture_bottom)))

            def assert_visible(widget):
                x, y = widget.winfo_rootx(), widget.winfo_rooty()
                if (
                    not widget.winfo_ismapped()
                    or x < app.winfo_rootx()
                    or y < app.winfo_rooty()
                    or x + widget.winfo_width() > app.winfo_rootx() + app.winfo_width()
                    or y + widget.winfo_height() > app.winfo_rooty() + app.winfo_height()
                ):
                    errors.append(f"Clipped control: {widget}")

            def capture_bottom():
                capture_box = (
                    app.winfo_rootx(),
                    app.winfo_rooty(),
                    app.winfo_rootx() + app.winfo_width(),
                    app.winfo_rooty() + app.winfo_height(),
                )
                ImageGrab.grab(bbox=capture_box).save(captures / "task-settings-bottom.png")
                app._switch_tab("accounts")
                app.accounts_tab.show_add_form()
                app.after(600, capture_account_form)

            def capture_account_form():
                capture_box = (
                    app.winfo_rootx(),
                    app.winfo_rooty(),
                    app.winfo_rootx() + app.winfo_width(),
                    app.winfo_rooty() + app.winfo_height(),
                )
                ImageGrab.grab(bbox=capture_box).save(captures / "account-form.png")
                app.accounts_tab.hide_form()
                future = asyncio.run_coroutine_threadsafe(db.get_accounts(), loop)

                def open_edit():
                    app.accounts_tab.open_edit_modal(future.result()[0])
                    app.after(600, capture_edit)

                def capture_edit():
                    modal = next(
                        widget for widget in app.accounts_tab.winfo_children() if isinstance(widget, ctk.CTkToplevel)
                    )
                    modal.update_idletasks()
                    save = next(widget for widget in modal.winfo_children() if isinstance(widget, ctk.CTkButton))
                    if (
                        not save.winfo_ismapped()
                        or save.winfo_rooty() + save.winfo_height() > modal.winfo_rooty() + modal.winfo_height()
                    ):
                        errors.append("Account edit save button is clipped")
                    box = (
                        modal.winfo_rootx(),
                        modal.winfo_rooty(),
                        modal.winfo_rootx() + modal.winfo_width(),
                        modal.winfo_rooty() + modal.winfo_height(),
                    )
                    ImageGrab.grab(bbox=box).save(captures / "account-edit.png")
                    modal.destroy()
                    app.after(100, start_fake)

                def start_fake():
                    app.scheduler.start(future.result(), "test_group", 10, 10)
                    app.after(500, app._on_close)

                app.after(100, open_edit)

            app.after(600, check_campaigns)
            app.after(25000, app._on_close)
            app.mainloop()
            if manager.disconnect.await_count != 1:
                errors.append("Active worker did not disconnect exactly once")
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("Async loop failed to stop")
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
    (captures / "result.json").write_text(
        json.dumps({"errors": errors, "screenshots": len(list(captures.glob("*.png")))}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if errors:
        raise RuntimeError("; ".join(errors))
    print("UI smoke passed: eight tabs, minimum window, graceful shutdown, no live Telegram calls.")


if __name__ == "__main__":
    main()
