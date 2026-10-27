"""Exercise safety pacing in the Task tab (presets, warnings, warmup gate, daily cap) offline."""

import asyncio
import json
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import Mock, patch

from core import database
from core.database import Database, init_db
from tests.support import account_payload
from ui.app import App


def main():
    folder = Path(".audit/pacing")
    folder.mkdir(parents=True, exist_ok=True)
    errors, shown = [], []
    with (
        tempfile.TemporaryDirectory(dir=folder) as directory,
        patch.object(database, "DB_PATH", str(Path(directory) / "test.db")),
        patch("core.paths.SESSIONS_DIR", Path(directory) / "sessions"),
    ):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(init_db())
        db = Database()
        loop.run_until_complete(db.bulk_insert_targets(["alice", "bobby"]))
        loop.run_until_complete(db.add_account(account_payload(1)))

        thread = threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_forever()), daemon=True)
        thread.start()
        with (
            patch("core.account_manager.TelegramClient", side_effect=AssertionError("Live Telegram blocked")),
            patch("ui.tab_task.preflight", side_effect=AssertionError("Unexpected preflight")),
            patch("tkinter.messagebox.showerror", side_effect=lambda *a, **kw: shown.append(("error", a))),
            patch("tkinter.messagebox.showwarning", side_effect=lambda *a, **kw: shown.append(("warning", a))),
            patch("tkinter.messagebox.askyesno", return_value=False) as confirm,
        ):
            app = App(loop)
            app.geometry("900x700")
            tab = app.task_tab

            def read(coro):
                return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=3)

            def numbers():
                return tuple(
                    entry.get()
                    for entry in (
                        tab.interval_min_entry,
                        tab.interval_max_entry,
                        tab.consecutive_entry,
                        tab.break_min_entry,
                        tab.break_max_entry,
                    )
                )

            def scenario():
                yield lambda: not tab._loading_campaign and tab.account_vars
                app._switch_tab("task")
                # Invite presets.
                assert tab.operation_mode() == "invite"
                for key, expected in (
                    ("safe", ("40", "90", "3")),
                    ("normal", ("20", "45", "5")),
                    ("fast", ("15", "30", "8")),
                ):
                    tab.apply_pace_preset(key)
                    assert numbers()[:3] == expected, numbers()
                    assert tab.current_pace() == key
                    assert (
                        tab.pace_label.cget("text")
                        == "Выбрано: " + {"safe": "Безопасный", "normal": "Обычный", "fast": "Быстрый"}[key]
                    )
                assert tab.pace_warning.winfo_manager(), "fast invite pace must show a warning"
                tab.apply_pace_preset("safe")
                assert not tab.pace_warning.winfo_manager()
                tab.interval_min_entry.delete(0, "end")
                tab.interval_min_entry.insert(0, "41")
                tab._refresh_pace()
                assert tab.current_pace() == "custom" and tab.pace_label.cget("text") == "Выбрано: Свой"
                # Messages presets.
                tab.mode_var.set("Личные сообщения")
                tab.on_mode_change()
                for key, expected in (
                    ("safe", ("90", "180", "3")),
                    ("normal", ("60", "120", "5")),
                    ("fast", ("45", "90", "7")),
                ):
                    tab.apply_pace_preset(key)
                    assert numbers()[:3] == expected, numbers()
                    assert tab.current_pace() == key
                tab.mode_var.set("Приглашения")
                tab.on_mode_change()
                # Aggressive start asks for confirmation and declines cleanly.
                tab.chat_entry.delete(0, "end")
                tab.chat_entry.insert(0, "testchat")
                tab.apply_pace_preset("fast")
                yield lambda: tab.recipients.expected is not None and not tab.recipients.loading
                tab.refresh_review()
                assert "ВНИМАНИЕ" in tab.review_box.get("1.0", "end-1c")
                with patch.object(tab.scheduler, "start", Mock()) as start:
                    tab.start_task()
                    assert confirm.call_count == 1 and "Агрессивные" in confirm.call_args.args[0]
                    assert not start.called and not tab._starting
                    # Warmup gate: no warmed-up account -> clear Russian error, nothing started.
                    confirm.return_value = True
                    tab.require_warmup_var.set(True)
                    tab.daily_cap_entry.delete(0, "end")
                    tab.daily_cap_entry.insert(0, "5")
                    tab.start_task()
                    yield lambda: not tab._starting
                    yield lambda: any(kind == "error" for kind, _ in shown)
                    assert "прогретых" in str(shown[-1][1]), shown
                    assert not start.called
                    tab.refresh_review()
                    review = tab.review_box.get("1.0", "end-1c")
                    assert "Будут пропущены" in review and "Дневной лимит кампании: 5" in review
                # Settings persist and reload.
                tab.save_settings()
                yield lambda: read(db.get_setting("campaign_daily_cap")) == "5"
                assert read(db.get_setting("require_warmup_ready")) == "1"
                tab.daily_cap_entry.delete(0, "end")
                tab.daily_cap_entry.insert(0, "abc")
                try:
                    tab.task_values()
                except ValueError as exc:
                    assert "Дневной лимит" in str(exc)
                else:
                    raise AssertionError("invalid cap accepted")
                tab.daily_cap_entry.delete(0, "end")
                tab.daily_cap_entry.insert(0, "0")
                tab.require_warmup_var.set(False)
                tab.load_campaign_settings()
                yield lambda: not tab._loading_campaign
                assert tab.daily_cap_entry.get() == "5" and tab.require_warmup_var.get()
                confirm.return_value = True
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
                    app._on_close()

            app.report_callback_exception = lambda kind, exc, trace: errors.append(f"{kind.__name__}: {exc}")
            app.after(500, advance)
            app.mainloop()
        thread.join(timeout=5)
        loop.close()
    (folder / "gui.json").write_text(json.dumps({"errors": errors}, indent=2), encoding="utf-8")
    if errors:
        raise AssertionError(errors)
    print("Safety pacing passed: presets, custom detection, warnings, confirm, warmup gate, daily cap settings.")


if __name__ == "__main__":
    main()
