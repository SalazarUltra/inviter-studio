import asyncio
import json
import os
import sys
import threading
from pathlib import Path
from tkinter import messagebox

from core.backup import apply_pending_restore, automatic_backup
from core.database import init_db
from core.instance_lock import InstanceLock

# PyInstaller's windowed bootloader sets stdout/stderr to None. Some dependencies
# write warnings to these streams during import; give them a harmless sink first.
if getattr(sys, "frozen", False):
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

from ui.app import App


def run_async_loop(loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(loop)
    loop.run_forever()


def main() -> None:
    loop = asyncio.new_event_loop()
    thread = None
    try:
        with InstanceLock():
            apply_pending_restore()
            automatic_backup()
            loop.run_until_complete(init_db())
            thread = threading.Thread(target=run_async_loop, args=(loop,), daemon=True)
            thread.start()
            try:
                app = App(async_loop=loop, show_startup_support="--smoke-test" not in sys.argv)
                errors = []
                if "--smoke-test" in sys.argv:
                    app.report_callback_exception = lambda kind, exc, trace: errors.append(str(exc))
                    app.after(2000, app._on_close)
                app.mainloop()
                if "--smoke-test" in sys.argv:
                    result_path = Path(sys.argv[sys.argv.index("--smoke-test") + 1])
                    result_path.write_text(
                        json.dumps(
                            {
                                "errors": errors,
                                "screens": len(app.page_frames),
                                "frozen": bool(getattr(sys, "frozen", False)),
                            }
                        ),
                        encoding="utf-8",
                    )
            finally:
                if loop.is_running():
                    loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=15)
    except Exception as exc:
        messagebox.showerror("Ошибка запуска TG Inviter", str(exc))
    finally:
        if not loop.is_running():
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()


if __name__ == "__main__":
    main()
