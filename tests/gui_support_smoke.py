"""First-launch donation UI on an isolated profile; clipboard and browser are mocked."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from unittest.mock import Mock, patch

from core import database
from core.database import Database, init_db
from core.support import SUPPORT_SEEN_KEY, WALLET_ADDRESS
from ui.app import App


def main():
    audit = Path(".audit/support-project")
    audit.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) == 1:
        with tempfile.TemporaryDirectory(dir=audit) as directory:
            for run in range(2):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tests.gui_support_smoke",
                        "--worker",
                        str(run),
                        str(Path(directory) / "test.db"),
                    ],
                    env={**os.environ, "SSLKEYLOGFILE": "", "PYTHONIOENCODING": "utf-8"},
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=45,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if result.returncode:
                    raise AssertionError(result.stdout + result.stderr)
        print(
            "Support UI passed: first launch only, exact wallet copy, no browser links, readonly address, reopen, profile scope and small window."
        )
        return
    errors = []
    with patch.object(database, "DB_PATH", sys.argv[3]):
        for run in [int(sys.argv[2])]:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(init_db())

            def runner(loop=loop):
                asyncio.set_event_loop(loop)
                loop.run_forever()

            thread = threading.Thread(target=runner, daemon=True)
            thread.start()
            with (
                patch("core.account_manager.TelegramClient", side_effect=AssertionError("Live Telegram blocked")),
                patch("webbrowser.open", return_value=True) as browser,
                patch("tkinter.messagebox.showerror", side_effect=lambda *args, **kw: errors.append(str(args))),
            ):
                app = App(loop)
                app.geometry("1280x800")

                def read(coro, loop=loop, app=app):
                    future = asyncio.run_coroutine_threadsafe(coro, loop)
                    deadline = time.monotonic() + 15
                    while not future.done():
                        app.update()
                        time.sleep(0.005)
                        assert time.monotonic() < deadline, "Temporary profile query timed out"
                    return future.result()

                def scenario(app=app, run=run, browser=browser):
                    if run == 0:
                        yield lambda: app._support_dialog is not None
                        dialog = app._support_dialog
                        assert app.grab_current() is None
                        assert (
                            dialog.address_entry.get() == WALLET_ADDRESS == "0x3EDdE055dED28684fEA81155af8891332f9B7B23"
                        )
                        assert dialog.address_entry._entry.cget("state") == "readonly"
                        assert app.show_support() is dialog
                        dialog.clipboard_clear = Mock()
                        dialog.clipboard_append = Mock()
                        dialog.copy_button.invoke()
                        dialog.clipboard_append.assert_called_once_with(WALLET_ADDRESS)
                        assert "скопирован" in dialog.status.cget("text")
                        dialog.clipboard_clear = Mock(side_effect=tk.TclError("Clipboard busy"))
                        dialog.copy_button.invoke()
                        assert "Не удалось" in dialog.status.cget("text")
                        assert dialog.clipboard_append.call_count == 1
                        assert not hasattr(dialog, "explorer_buttons")
                        browser.assert_not_called()
                        dialog.geometry("500x450")
                        deadline = time.monotonic() + 0.25
                        yield lambda: time.monotonic() > deadline
                        assert dialog.address_entry._entry.xview() == (0.0, 1.0)
                        for button in (dialog.copy_button, dialog.close_button):
                            assert (
                                button.winfo_rootx() + button.winfo_width()
                                <= dialog.winfo_rootx() + dialog.winfo_width()
                            )
                            assert (
                                button.winfo_rooty() + button.winfo_height()
                                <= dialog.winfo_rooty() + dialog.winfo_height()
                            )
                        dialog.close_button.invoke()
                        yield lambda: read(Database().get_setting(SUPPORT_SEEN_KEY)) == "1"
                        other = read(Database().create_campaign("Other", "messages"))
                        assert read(Database(other).get_setting(SUPPORT_SEEN_KEY)) == "1"
                        app._load_support_intro()
                        deadline = time.monotonic() + 0.25
                        yield lambda: time.monotonic() > deadline
                        assert not dialog.winfo_exists()
                    else:
                        assert app._support_dialog is None  # Same profile: no automatic repeat.
                    app.dashboard_tab.support_button.invoke()
                    dialog = app._support_dialog
                    assert dialog.winfo_exists()
                    app.support_button.invoke()
                    assert app._support_dialog is dialog
                    app.geometry("900x600")
                    yield lambda: app._rail_expanded is False
                    deadline = time.monotonic() + 0.3
                    yield lambda: time.monotonic() > deadline
                    assert (
                        app.support_button.winfo_rooty()
                        >= app.nav_buttons["parsing"].winfo_rooty() + app.nav_buttons["parsing"].winfo_height()
                    )
                    assert (
                        app.support_button.winfo_rooty() + app.support_button.winfo_height()
                        <= app.winfo_rooty() + app.winfo_height()
                    ), (
                        app.support_button.winfo_ismapped(),
                        app.support_button.winfo_rooty(),
                        app.support_button.winfo_height(),
                        app.winfo_rooty(),
                        app.winfo_height(),
                    )
                    assert app.dashboard_tab.support_button.winfo_width() > 100
                    dialog.close_button.invoke()
                    app._on_close()

                steps = scenario()
                waiting = None
                attempts = 0

                def advance(app=app, steps=steps):
                    nonlocal waiting, attempts
                    try:
                        if waiting and not waiting():
                            attempts += 1
                            assert attempts < 200, f"UI timeout at {steps.gi_frame.f_lineno}"
                        else:
                            waiting, attempts = next(steps), 0
                        if not app._closing:
                            app.after(50, advance)
                    except StopIteration:
                        pass
                    except Exception:
                        errors.append(traceback.format_exc())
                        app._on_close()

                def report_error(kind, exc, trace, app=app):
                    errors.append("".join(traceback.format_exception(kind, exc, trace)))
                    traceback.print_exception(kind, exc, trace)
                    if app._closing:
                        app.quit()

                app.report_callback_exception = report_error
                app.after(2600, advance)
                app.mainloop()
                assert app._support_seen_future is None or app._support_seen_future.done()
            thread.join(timeout=5)
            loop.close()
            if errors:
                break
    (audit / "gui.json").write_text(
        json.dumps(
            {
                "errors": errors,
                "profile_restarts_checked": run + 1,
                "clipboard_mocked": True,
                "browser_mocked": True,
                "live_telegram_calls": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if errors:
        raise AssertionError(errors)
    print(
        "Support UI passed: first launch only, exact wallet copy, no browser links, readonly address, reopen, profile scope and small window."
    )


if __name__ == "__main__":
    main()
