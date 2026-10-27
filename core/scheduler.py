import asyncio
from concurrent.futures import Future
from typing import Callable

from core.database import Database
from core.inviter import InviterEngine
from core.messenger import MessageEngine
from core.user_feedback import explain_error


class TaskScheduler:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        db: Database,
        log_callback: Callable[[str, str], None],
        progress_callback: Callable[[int, int], None] | None = None,
        status_callback: Callable[[str], None] | None = None,
        fatal_callback: Callable[[str], None] | None = None,
        complete_callback: Callable[[dict], None] | None = None,
        details_callback: Callable[[dict], None] | None = None,
    ):
        self.loop = loop
        self.db = db
        self.callbacks = dict(
            log_callback=log_callback,
            progress_callback=progress_callback,
            status_callback=status_callback,
            fatal_callback=fatal_callback,
            complete_callback=complete_callback,
            details_callback=details_callback,
        )
        self.engine = InviterEngine(db=db, **self.callbacks)
        self.future: Future | None = None

    def start(
        self,
        accounts: list[dict],
        chat: str,
        interval_min: int,
        interval_max: int,
        mode: str = "invite",
        message: str = "",
    ) -> None:
        if self.future and not self.future.done():
            raise RuntimeError("Task is already running.")
        # Freeze campaign scope for the lifetime of the task.
        if mode not in {"invite", "messages"}:
            raise ValueError("Неизвестный режим задачи")
        engine_type = MessageEngine if mode == "messages" else InviterEngine
        self.engine = engine_type(db=self.db.for_campaign(self.db.campaign_id), **self.callbacks)
        self.engine._stop_requested = False
        self.future = asyncio.run_coroutine_threadsafe(
            self.engine.start(accounts, message if mode == "messages" else chat, interval_min, interval_max),
            self.loop,
        )

        def observe(result: Future) -> None:
            # The engine handles its own errors; this surfaces framework bugs
            # that escape engine.start and would otherwise stay unread in the future.
            if not result.cancelled() and result.exception() is not None:
                self.callbacks["log_callback"]("ERROR", "Сбой задачи: " + explain_error(result.exception()))

        self.future.add_done_callback(observe)

    def pause(self) -> None:
        self.loop.call_soon_threadsafe(self.engine.pause)

    def resume(self) -> None:
        self.loop.call_soon_threadsafe(self.engine.resume)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.engine.stop)

    def is_running(self) -> bool:
        return bool(self.future and not self.future.done())
