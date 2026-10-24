"""Exercise AI connection setup against a temporary DB, with all live calls blocked."""

import asyncio
import faulthandler
import json
import tempfile
import threading
import time
import traceback
from pathlib import Path
from unittest.mock import AsyncMock, patch

import customtkinter as ctk
from PIL import ImageGrab

from core import database
from core.database import Database, init_db
from ui.app import App


def main():
    faulthandler.dump_traceback_later(25, repeat=True)
    audit = Path(".audit/ai-provider-audit")
    audit.mkdir(parents=True, exist_ok=True)
    errors, notices = [], []
    with (
        tempfile.TemporaryDirectory(dir=audit) as directory,
        patch.object(database, "DB_PATH", str(Path(directory) / "test.db")),
    ):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(init_db())
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        db = Database()

        def read(coro):
            try:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                deadline = time.monotonic() + 20
                while not future.done():
                    # Keep Tcl responsive if GC on a worker finalizes a Tk object.
                    app.update()
                    time.sleep(0.005)
                    assert time.monotonic() < deadline, "Read-only DB query timed out"
                return future.result()
            except Exception:
                faulthandler.dump_traceback()
                raise

        with (
            patch("core.account_manager.TelegramClient", side_effect=AssertionError("Live Telegram blocked")),
            patch("aiohttp.ClientSession", side_effect=AssertionError("Live AI calls blocked")),
            patch("ui.tab_ai_settings.webbrowser.open", return_value=True) as browser,
            patch("tkinter.messagebox.showwarning", side_effect=lambda *args, **kwargs: notices.append(args)),
            patch("tkinter.messagebox.showerror", side_effect=lambda *args, **kwargs: notices.append(args)),
            patch("tkinter.messagebox.showinfo"),
        ):
            app = App(loop, show_startup_support=False)
            app.geometry("1280x820+20+20")
            app.attributes("-topmost", True)
            app.lift()
            tab = app.ai_tab

            def capture(name):
                ImageGrab.grab(
                    bbox=(
                        app.winfo_rootx(),
                        app.winfo_rooty(),
                        app.winfo_rootx() + app.winfo_width(),
                        app.winfo_rooty() + app.winfo_height(),
                    )
                ).save(audit / name)

            def scenario():
                app._switch_tab("ai")
                yield lambda: tab.winfo_ismapped()
                assert tab.provider_menu.cget("values") == [info["name"] for info in tab.PROVIDERS.values()]
                assert tab.model_var.get() == "gpt-6-luna"
                assert not tab._key_visible
                tab._add_config()
                assert "название" in notices[-1][1]
                tab.name_entry.insert(0, "Test connection")
                with patch.object(tab.key_entry._entry, "clipboard_get", return_value="sk-test-fake"):
                    tab.paste_button.invoke()
                assert tab.key_entry.get() == "sk-test-fake"
                tab.show_key_button.invoke()
                assert tab.key_entry.cget("show") == ""
                tab._on_provider_change("Google Gemini")
                assert tab.key_entry.get() == "" and not tab._key_visible
                assert tab.model_var.get() == "gemini-3.5-flash-lite"
                tab._open_provider_page("keys_url")
                browser.assert_called_once_with("https://aistudio.google.com/apikey")
                tab.key_entry.insert(0, "AIza-test-fake")
                tab.model_var.set("models/gemini-3.5-flash-lite")
                tab.add_button.invoke()
                tab._add_config()  # A double click must not create two connections.
                yield lambda: not tab._save_inflight
                configs = read(db.get_ai_configs())
                assert len(configs) == 1 and configs[0]["model"] == "gemini-3.5-flash-lite"
                identifier = configs[0]["id"]
                assert read(db.get_active_ai_config()) is None
                assert tab.key_entry.get() == "" and "Активировать" in tab.status_label.cget("text")
                tab._activate_config(identifier)
                yield lambda: read(db.get_active_ai_config()) is not None
                read(db.update_ai_usage(identifier, 42, 0.0))
                config = read(db.get_active_ai_config())
                tab._edit_config(config)
                assert tab.key_entry.cget("show") == "*"
                tab.model_var.set("custom-model-id")
                tab.add_button.invoke()
                yield lambda: not tab._save_inflight
                assert read(db.get_active_ai_config())["total_tokens"] == 42
                assert read(db.get_active_ai_config())["model"] == "custom-model-id"
                assert tab._editing_id is None
                tab._edit_config(read(db.get_active_ai_config()))
                tab.cancel_edit_button.invoke()
                assert tab.key_entry.get() == "" and tab._editing_id is None
                tab.name_entry.insert(0, "Failed save")
                tab.key_entry.insert(0, "AIza-fake")
                with patch.object(tab.db, "add_ai_config", new=AsyncMock(side_effect=RuntimeError("private fake key"))):
                    tab.add_button.invoke()
                    yield lambda: not tab._save_inflight
                assert "Не удалось сохранить" in notices[-1][1]
                assert "private fake key" not in notices[-1][1]
                assert "private fake key" not in app.logs_tab.log_box.get("1.0", "end")
                assert tab.key_entry.get() == "AIza-fake"
                tab._reset_form()
                with (
                    patch("ui.tab_ai_settings.ctk.CTkInputDialog") as dialog,
                    patch(
                        "ui.tab_ai_settings.AIWriter.generate_message", new=AsyncMock(return_value="Тестовый ответ.")
                    ) as generate,
                    patch("ui.tab_ai_settings.show_report") as report,
                ):
                    dialog.return_value.get_input.return_value = "Тест"
                    tab._preview_config(read(db.get_active_ai_config()))
                    yield lambda: not tab._preview_inflight
                    generate.assert_awaited_once()
                    report.assert_called_once()
                tab._on_provider_change("OpenAI")
                tab.refresh_configs()
                deadline = time.monotonic() + 0.5
                yield lambda: time.monotonic() > deadline
                capture("ai-1280.png")
                app.geometry("900x600")
                deadline = time.monotonic() + 0.5
                yield lambda: time.monotonic() > deadline
                capture("ai-900.png")
                assert (
                    tab.add_button.winfo_rooty() + tab.add_button.winfo_height()
                    <= app.winfo_rooty() + app.winfo_height()
                )

                def buttons(widget):
                    for child in widget.winfo_children():
                        if isinstance(child, ctk.CTkButton):
                            yield child
                        else:
                            yield from buttons(child)

                for button in buttons(tab.cards_frame):
                    assert button.winfo_ismapped(), button.cget("text")
                    assert button.winfo_width() >= button._apply_widget_scaling(button._desired_width) - 2, (
                        button.cget("text"),
                        button.winfo_width(),
                        button._desired_width,
                    )
                app.geometry("1280x820")
                app._switch_tab("accounts")
                accounts = app.accounts_tab
                accounts.show_add_form()
                accounts.api_site_button.invoke()
                assert browser.call_args.args[0] == "https://my.telegram.org/apps"
                guide = accounts._show_api_instructions()
                textboxes = [widget for widget in guide.winfo_children() if isinstance(widget, ctk.CTkTextbox)]
                instructions = textboxes[0].get("1.0", "end")
                assert (
                    "API development tools" in instructions
                    and "Desktop" in instructions
                    and "App api_hash" in instructions
                )
                guide.destroy()
                accounts.hide_form()
                app._switch_tab("task")
                identifier = read(db.create_campaign("Delete GUI", "messages"))
                app.select_campaign(identifier)
                yield lambda: not app.task_tab._loading_campaign and not app.task_tab.recipients.loading
                bar = app.campaign_bars[0]
                yield lambda: "Delete GUI" in bar.value.get()
                with patch("ui.campaigns.messagebox.askyesno", return_value=False) as confirm:
                    bar.delete_button.invoke()
                    assert "Защита от повторной отправки" in confirm.call_args.args[1]
                assert len(read(db.get_campaigns())) == 2
                with (
                    patch.object(app.scheduler, "is_running", return_value=True),
                    patch("ui.campaigns.messagebox.askyesno") as confirm,
                ):
                    bar.delete_button.invoke()
                    confirm.assert_not_called()
                app.task_tab.message_box.delete("1.0", "end")
                app.task_tab.message_box.insert("1.0", "Discard unsaved draft")
                with patch("ui.campaigns.messagebox.askyesno", return_value=True):
                    bar.delete_button.invoke()
                    yield lambda: not app.task_tab._starting and app.db.campaign_id != identifier
                assert len(read(db.get_campaigns())) == 1
                assert identifier not in app.task_tab._drafts
                assert read(db.for_campaign(identifier).get_setting("message_text")) is None
                yield lambda: not app.task_tab._loading_campaign and not app.task_tab.recipients.loading
                with patch("ui.campaigns.messagebox.askyesno", return_value=True):
                    dialog = bar.manage()
                    dialog.delete_button.invoke()
                    yield lambda: not app.task_tab._starting and app.db.campaign_id != 1
                assert len(read(db.get_campaigns())) == 1
                assert not dialog.winfo_exists()
                yield lambda: not app.task_tab._loading_campaign and not app.task_tab.recipients.loading

                def check_commands(widget):
                    for command in widget._tclCommands or []:
                        if not widget.tk.call("info", "commands", command):
                            raise AssertionError(f"Missing Tcl command: {type(widget).__name__} {command}")
                    for child in widget.winfo_children():
                        check_commands(child)

                check_commands(app)
                app._on_close()

            steps = scenario()
            waiting, attempts = None, 0

            def advance():
                nonlocal waiting, attempts
                try:
                    if waiting and not waiting():
                        attempts += 1
                        assert attempts < 160, f"UI timeout at {steps.gi_frame.f_lineno}"
                    else:
                        waiting, attempts = next(steps), 0
                        print(f"GUI checkpoint {steps.gi_frame.f_lineno}", flush=True)
                    if not app._closing:
                        app.after(50, advance)
                except StopIteration:
                    pass
                except Exception:
                    errors.append(traceback.format_exc())
                    app._on_close()

            def report_error(kind, exc, trace):
                errors.append("".join(traceback.format_exception(kind, exc, trace)))
                traceback.print_exception(kind, exc, trace)
                if app._closing:
                    app.quit()

            app.report_callback_exception = report_error
            app.after(2200, advance)
            app.mainloop()
        thread.join(timeout=5)
        loop.close()
    (audit / "gui.json").write_text(
        json.dumps(
            {"errors": errors, "notices_checked": len(notices), "live_telegram_calls": False, "live_ai_calls": False},
            indent=2,
        ),
        encoding="utf-8",
    )
    if errors:
        raise AssertionError(errors)
    faulthandler.cancel_dump_traceback_later()
    print(
        "AI setup UI passed: provider links, key paste/masking, save/edit/activate/cancel, errors, preview and small window."
    )


if __name__ == "__main__":
    main()
