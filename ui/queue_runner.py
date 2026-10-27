"""Campaign queue on the UI side: after a run ends, offer (with a confirmation) to start the next campaign."""

from tkinter import messagebox

from core.campaign_queue import get_auto_continue, needs_confirmation, plan_next

SWITCH_WAIT_STEPS = 40  # x 250 ms: the finished task releases its sessions shortly after the callback
LOAD_WAIT_STEPS = 60


class QueueRunner:
    def __init__(self, app):
        self.app = app
        self.chain: list[int] = []  # campaigns already run in the current queue (loop guard)
        self.session_confirmed = False  # the user answered «Да» once in this session: do not ask again

    def offer(self, finished_db, summary) -> None:
        """UI thread. `finished_db` is the campaign scope of the run that just ended."""
        app = self.app
        if finished_db is None or app._closing:
            return
        chain = [*self.chain, finished_db.campaign_id]

        async def decide():
            return await plan_next(finished_db, summary, chain), await get_auto_continue(finished_db)

        future = app.run_coro(decide())

        def done(result):
            def render():
                if app._closing:
                    return
                if result.cancelled() or result.exception():
                    self.chain = []
                    return
                plan, auto = result.result()
                self.handle(plan, chain, auto_continue=auto)

            app._safe_after(render)

        future.add_done_callback(done)

    def handle(self, plan, chain, auto_continue=False) -> None:
        app = self.app
        if plan["action"] == "none":
            self.chain = []
            return
        if plan["action"] == "blocked":
            self.chain = []
            app.set_status(plan["message"])
            app.notify("Очередь кампаний", plan["message"], warning=True)
            return
        if needs_confirmation(auto_continue, self.session_confirmed):
            if not messagebox.askyesno(
                "Очередь кампаний",
                plan["message"] + "\n\nОткрыть эту кампанию и запустить её сейчас?\n"
                "«Да» — дальше в этой сессии следующие кампании запускаются без вопроса "
                "(постоянно: флажок «Автозапуск следующей»).\n"
                "«Нет» — очередь остановится, кампания останется на месте.",
                parent=app,
            ):
                self.chain = []
                return
            self.session_confirmed = True
        else:
            text = f"Автозапуск очереди: следующая кампания «{plan['name']}»."
            app.set_status(text)
            app.notify("Очередь кампаний", text)
        self.chain = chain
        self.start(plan["next_id"], 0)

    def start(self, campaign_id, attempt) -> None:
        app = self.app
        if app._closing:
            return
        if app.scheduler.is_running() or app.task_tab._starting:
            if attempt < SWITCH_WAIT_STEPS:
                app.after(250, lambda: self.start(campaign_id, attempt + 1))
            else:
                self.fail("Предыдущая задача ещё не освободила аккаунты. Запустите следующую кампанию вручную.")
            return
        if app.db.campaign_id != campaign_id:
            app.switch_tab("task")
            app.select_campaign(campaign_id)
            if app.db.campaign_id != campaign_id:
                self.fail("Не удалось открыть следующую кампанию. Откройте её и запустите вручную.")
                return
        self.await_loaded(campaign_id, 0)

    def await_loaded(self, campaign_id, attempt) -> None:
        app, task = self.app, self.app.task_tab
        if app._closing:
            return
        if app.db.campaign_id != campaign_id:
            self.fail("Кампания была переключена вручную: очередь остановлена.")
            return
        if task._loading_campaign and attempt < LOAD_WAIT_STEPS:
            app.after(250, lambda: self.await_loaded(campaign_id, attempt + 1))
            return
        if task._loading_campaign or getattr(task, "_load_error", False):
            self.fail("Настройки следующей кампании не загрузились. Запустите её вручную.")
            return
        task.start_task()
        self.watch_start(0)

    def watch_start(self, attempt) -> None:
        """Forget the chain when the start was refused (an error dialog was already shown by start_task)."""
        app = self.app
        if app._closing:
            return
        if app.task_tab._starting and attempt < 240:
            app.after(500, lambda: self.watch_start(attempt + 1))
        elif not app.scheduler.is_running():
            self.chain = []

    def fail(self, text) -> None:
        self.chain = []
        self.app.set_status(text)
        self.app.notify("Очередь кампаний", text, warning=True)
