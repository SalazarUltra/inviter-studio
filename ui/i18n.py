"""Tiny dictionary-based UI translations (ru default, en optional).

Only the shell and the critical task/accounts strings live here; long help texts stay Russian.
``t(key)`` never raises: a missing key falls back to Russian, then to the key itself.
"""

from __future__ import annotations

from typing import Callable

LANG_SETTING_KEY = "ui_lang"
LANGS = ("ru", "en")
DEFAULT_LANG = "ru"
LANG_LABELS = {"ru": "Русский", "en": "English"}

STRINGS: dict[str, dict[str, str]] = {
    "ru": {
        # Navigation shell
        "nav.dashboard": "Обзор",
        "nav.accounts": "Аккаунты",
        "nav.targets": "Аудитория",
        "nav.task": "Задача",
        "nav.warmup": "Прогрев",
        "nav.ai": "AI-студия",
        "nav.logs": "Журнал",
        "nav.parsing": "Парсинг",
        "nav.heading": "ИНСТРУМЕНТЫ",
        "nav.workspace": "Рабочее пространство",
        "nav.help": "Помощь · F1",
        "nav.support": "Поддержать проект",
        "nav.support_tip": "Поддержать проект · адрес и сети для доната",
        "nav.new_task": "Настроить задачу",
        "nav.notifications": "Уведомления Windows",
        "nav.language_tip": "Язык интерфейса. Меняется сразу, без перезапуска.",
        "nav.help_tip": "Помощь по текущему разделу и пошаговый запуск. Можно открыть клавишей F1.",
        "nav.new_task_tip": "Открыть настройки выбранной кампании. Отправка начнётся только после «Запустить задачу».",
        "status.ready": "Готов к работе",
        "lang.save_failed": "Язык применён, но не сохранён. Повторите выбор.",
        # Common buttons
        "btn.start": "Запустить задачу",
        "btn.stop": "Остановить",
        "btn.pause": "Пауза",
        "btn.resume": "Продолжить",
        "btn.save": "Сохранить настройки",
        "btn.cancel": "Отмена",
        "btn.close": "Закрыть",
        "btn.back": "Назад",
        "btn.next": "Далее",
        "btn.skip": "Пропустить",
        "btn.done": "Готово",
        # Task tab
        "task.sections.recipients": "Получатели",
        "task.sections.message": "Сообщение / чат",
        "task.sections.accounts": "Аккаунты и темп",
        "task.sections.review": "Проверка",
        "task.preview": "Предпросмотр",
        "task.check": "Проверить подключение",
        "task.preview_tip": "Проверка настроек и очереди без подключения к Telegram. Ничего не отправляет.",
        "task.check_tip": "Проверяет сессии, прокси и для приглашений — права в чате. Ничего не отправляет.",
        "task.save_tip": "Сохранить параметры выбранной кампании для следующего запуска. "
        "Текст сообщения сохраняется автоматически.",
        "task.start_tip": "Начать реальные сообщения или приглашения выбранной кампании после проверки подключения.",
        "task.pause_tip": "Приостановить следующие действия. Для возобновления нажмите «Продолжить».",
        "task.stop_tip": "Завершить текущую задачу. Ожидающие остаются в очереди; "
        "уже отправленный запрос может завершиться.",
        "task.next_tip": "Перейти к следующему шагу. Сообщения и приглашения не отправляются.",
        "task.test_saved_btn": "Тест в Избранное",
        "task.next_message": "Далее: сообщение / чат",
        "task.next_accounts": "Далее: аккаунты и темп",
        "task.next_review": "Далее: проверка",
        "task.running": "Задача запущена",
        "task.paused": "Задача на паузе",
        "task.starting": "Проверяем перед запуском...",
        "task.stopping": "Останавливаем задачу...",
        "task.loading": "Загружаем настройки…",
        "task.retry_load": "Повторить загрузку",
        "task.pace": "Темп:",
        "task.pace_selected": "Выбрано: {name}",
        "task.pace_applied": "Темп «{name}» применён. Нажмите «Сохранить настройки», чтобы запомнить его.",
        "task.pace_safe_tip": "Редкие действия и частые перерывы: самый низкий риск ограничений. "
        "Заполняет интервал и перерывы.",
        "task.pace_normal_tip": "Рекомендуемый баланс скорости и безопасности. Заполняет интервал и перерывы.",
        "task.pace_fast_tip": "Быстрее, но выше риск PeerFlood и ограничений аккаунта. Заполняет интервал и перерывы.",
        "task.pace_label": "Темп: {name}",
        "pace.safe": "Безопасный",
        "pace.normal": "Обычный",
        "pace.fast": "Быстрый",
        "pace.custom": "Свой",
        "task.stop_confirm_title": "Остановить задачу?",
        "task.stop_confirm": "Остановить задачу? Ожидание вторых сообщений ({pending}) будет отменено.",
        "task.status_stopped": "Остановлено",
        "task.summary.title.completed": "Итог",
        "task.summary.title.stopped": "Остановлено",
        "task.summary.title.failed": "Ошибка задачи",
        "task.summary.added": "Добавлено: {n}",
        "task.summary.sent": "Первых сообщений: {n}",
        "task.summary.followup_sent": "Вторых сообщений после ответа: {n}",
        "task.summary.followup_cancelled": "Вторых сообщений отменено после завершения: {n}",
        "task.summary.skipped": "Пропущено: {n}",
        "task.summary.errors": "Ошибок: {n}",
        "task.summary.duration": "Время работы: {value}",
        "task.summary.by_account": "По аккаунтам:",
        "task.summary.account_line": "{phone}: добавлено {added}, пропущено {skipped}, ошибок {errors}",
        # Accounts tab
        "accounts.add": "+ Добавить аккаунт",
        "accounts.phone": "Телефон:",
        "accounts.use": "Использовать",
        "accounts.test": "Проверить",
        "accounts.edit": "Изменить",
        "accounts.delete": "Удалить",
        "accounts.new": "Новый аккаунт",
        "accounts.api_site": "Получить API ID и API Hash ↗",
        "accounts.api_steps": "Пошагово: где взять данные",
        "accounts.get_code": "Получить код",
        "accounts.sending": "Отправка...",
        "accounts.confirm": "Подтвердить",
        "accounts.confirm_2fa": "Подтвердить 2FA",
        "accounts.proxy": "Прокси (необязательно)",
        "accounts.step1": "Шаг 1: заполните данные и нажмите «Получить код»",
        "accounts.official_site": "Официальный сайт Telegram",
        "accounts.save_changes": "Сохранить изменения",
        "accounts.no_match": "Нет совпадений",
        "accounts.no_match_hint": "Измените телефон или выберите фильтр «Все».",
        "accounts.empty_title": "Подключите первый аккаунт",
        "accounts.empty_text": "Добавьте Telegram-аккаунт, чтобы начать работу.",
        "accounts.empty_add": "Добавить аккаунт",
        "accounts.delete_title": "Подтверждение",
        "accounts.delete_confirm": "Удалить аккаунт?",
        # Generic dialogs
        "dlg.error": "Ошибка",
        "dlg.fatal": "Критическая ошибка",
        "dlg.exit_title": "Выход",
        "dlg.exit_text": "Задача выполняется. Закрыть приложение?",
        "dlg.campaign_busy_title": "Кампания выполняется",
        "dlg.campaign_busy_text": "Остановите задачу или дождитесь проверки перед переключением",
        "dlg.task_running_title": "Задача выполняется",
        "dlg.task_running_text": "Остановите задачу: аккаунт нужен для тестовой отправки.",
        "dlg.test_saved": "Тест в Избранное",
        "dlg.test_saved_pick": "Отметьте аккаунт, через который отправить тест.",
        "dlg.complete.text": "Откройте Inviter Studio: итог доступен в задаче, подробности — в журнале.",
        "dlg.complete.completed": "Задача завершена",
        "dlg.complete.failed": "Задача требует внимания",
        "dlg.complete.stopped": "Задача остановлена",
        "dlg.complete.other": "Итог задачи",
        # First-run wizard
        "wizard.title": "Первый запуск · Inviter Studio",
        "wizard.progress": "Шаг {n} из {total}",
        "wizard.s1.title": "Получите API ID и API Hash",
        "wizard.s1.text": "Они нужны Telegram для входа. Откройте my.telegram.org/apps, войдите по номеру "
        "и создайте приложение. Не публикуйте API Hash.",
        "wizard.s1.button": "Открыть my.telegram.org ↗",
        "wizard.s2.title": "Добавьте аккаунт",
        "wizard.s2.text": "Раздел «Аккаунты» → «+ Добавить аккаунт»: API ID, API Hash, телефон, затем код из Telegram.",
        "wizard.s2.button": "Открыть «Аккаунты»",
        "wizard.s3.title": "Проверьте подключение (по желанию)",
        "wizard.s3.text": "На карточке аккаунта нажмите «Проверить». Ничего не отправляется — только проверка входа.",
        "wizard.s3.button": "Открыть «Аккаунты»",
        "wizard.s4.title": "Создайте кампанию",
        "wizard.s4.text": "Кампания хранит получателей, текст и настройки. Выберите или создайте её в «Аудитории» "
        "или вверху «Задачи», затем добавьте получателей.",
        "wizard.s4.button": "Открыть «Аудиторию»",
        "wizard.s5.title": "Откройте «Задачу»",
        "wizard.s5.text": "Выберите режим, чат или текст, аккаунты и темп. Начните с «Безопасного» темпа "
        "и «Предпросмотра» — ничего не отправится, пока вы не нажмёте «Запустить задачу».",
        "wizard.s5.button": "Открыть «Задачу»",
        "wizard.dont_show": "Больше не показывать",
        # Summary card (Saved Messages)
        "summary.card.title": "Сводка в «Избранное»",
        "summary.card.enable": "Отправлять краткую сводку в «Избранное» Telegram",
        "summary.card.hint": "Только счётчики: отправлено, добавлено, ошибки. Без списков получателей и номеров.",
        "summary.card.mode": "Когда:",
        "summary.card.mode_task": "По завершении задачи",
        "summary.card.mode_daily": "Раз в день",
        "summary.card.account": "Аккаунт:",
        "summary.card.no_accounts": "Нет аккаунтов",
        "summary.card.test": "Отправить пример",
        "summary.card.saved": "Настройка сохранена.",
        "summary.card.sent": "Пример отправлен в «Избранное».",
        "summary.card.failed": "Не удалось отправить. Проверьте аккаунт и подключение.",
        "summary.card.pick": "Выберите аккаунт для сводки.",
        "summary.header": "Inviter Studio · сводка",
        "summary.header_daily": "Inviter Studio · сводка за {date}",
        "summary.outcome.completed": "Задача завершена",
        "summary.outcome.stopped": "Задача остановлена",
        "summary.outcome.failed": "Задача завершилась с ошибкой",
        "summary.outcome.other": "Итог задачи",
        "summary.sent": "Отправлено: {n}",
        "summary.added": "Добавлено: {n}",
        "summary.errors": "Ошибок: {n}",
        "summary.skipped": "Пропущено: {n}",
        "summary.duration": "Время: {value}",
        "summary.tasks": "Задач: {n}",
        "summary.sample": "Это пример сводки.",
    },
    "en": {
        "nav.dashboard": "Overview",
        "nav.accounts": "Accounts",
        "nav.targets": "Audience",
        "nav.task": "Task",
        "nav.warmup": "Warm-up",
        "nav.ai": "AI Studio",
        "nav.logs": "Log",
        "nav.parsing": "Parsing",
        "nav.heading": "TOOLS",
        "nav.workspace": "Workspace",
        "nav.help": "Help · F1",
        "nav.support": "Support the project",
        "nav.support_tip": "Support the project · donation address and networks",
        "nav.new_task": "Set up task",
        "nav.notifications": "Windows notifications",
        "nav.language_tip": "Interface language. Applies immediately, no restart.",
        "nav.help_tip": "Help for the current section and a step-by-step start. Press F1 to open.",
        "nav.new_task_tip": "Open the selected campaign settings. Nothing is sent until you press “Start task”.",
        "status.ready": "Ready",
        "lang.save_failed": "Language applied but not saved. Please choose it again.",
        "btn.start": "Start task",
        "btn.stop": "Stop",
        "btn.pause": "Pause",
        "btn.resume": "Resume",
        "btn.save": "Save settings",
        "btn.cancel": "Cancel",
        "btn.close": "Close",
        "btn.back": "Back",
        "btn.next": "Next",
        "btn.skip": "Skip",
        "btn.done": "Done",
        "task.sections.recipients": "Recipients",
        "task.sections.message": "Message / chat",
        "task.sections.accounts": "Accounts & pace",
        "task.sections.review": "Review",
        "task.preview": "Preview",
        "task.check": "Check connection",
        "task.preview_tip": "Checks settings and the queue without connecting to Telegram. Sends nothing.",
        "task.check_tip": "Checks sessions, proxies and, for invites, chat rights. Sends nothing.",
        "task.save_tip": "Save the selected campaign settings for the next run. "
        "The message text is saved automatically.",
        "task.start_tip": "Start real messages or invites for the selected campaign after the connection check.",
        "task.pause_tip": "Pause upcoming actions. Press “Resume” to continue.",
        "task.stop_tip": "End the current task. Waiting recipients stay queued; a request already sent may still finish.",
        "task.next_tip": "Go to the next step. No messages or invites are sent.",
        "task.test_saved_btn": "Test to Saved Messages",
        "task.next_message": "Next: message / chat",
        "task.next_accounts": "Next: accounts & pace",
        "task.next_review": "Next: review",
        "task.running": "Task running",
        "task.paused": "Task paused",
        "task.starting": "Checking before start...",
        "task.stopping": "Stopping the task...",
        "task.loading": "Loading settings…",
        "task.retry_load": "Retry loading",
        "task.pace": "Pace:",
        "task.pace_selected": "Selected: {name}",
        "task.pace_applied": "Pace “{name}” applied. Press “Save settings” to keep it.",
        "task.pace_safe_tip": "Rare actions and frequent breaks: lowest risk of limits. Fills interval and breaks.",
        "task.pace_normal_tip": "Recommended balance of speed and safety. Fills interval and breaks.",
        "task.pace_fast_tip": "Faster, but a higher risk of PeerFlood and account limits. Fills interval and breaks.",
        "task.pace_label": "Pace: {name}",
        "pace.safe": "Safe",
        "pace.normal": "Normal",
        "pace.fast": "Fast",
        "pace.custom": "Custom",
        "task.stop_confirm_title": "Stop the task?",
        "task.stop_confirm": "Stop the task? Waiting second messages ({pending}) will be cancelled.",
        "task.status_stopped": "Stopped",
        "task.summary.title.completed": "Result",
        "task.summary.title.stopped": "Stopped",
        "task.summary.title.failed": "Task error",
        "task.summary.added": "Added: {n}",
        "task.summary.sent": "First messages: {n}",
        "task.summary.followup_sent": "Second messages after a reply: {n}",
        "task.summary.followup_cancelled": "Second messages cancelled at the end: {n}",
        "task.summary.skipped": "Skipped: {n}",
        "task.summary.errors": "Errors: {n}",
        "task.summary.duration": "Run time: {value}",
        "task.summary.by_account": "By account:",
        "task.summary.account_line": "{phone}: added {added}, skipped {skipped}, errors {errors}",
        "accounts.add": "+ Add account",
        "accounts.phone": "Phone:",
        "accounts.use": "Use",
        "accounts.test": "Check",
        "accounts.edit": "Edit",
        "accounts.delete": "Delete",
        "accounts.new": "New account",
        "accounts.api_site": "Get API ID and API Hash ↗",
        "accounts.api_steps": "Step by step: where to get them",
        "accounts.get_code": "Get code",
        "accounts.sending": "Sending...",
        "accounts.confirm": "Confirm",
        "accounts.confirm_2fa": "Confirm 2FA",
        "accounts.proxy": "Proxy (optional)",
        "accounts.step1": "Step 1: fill in the details and press “Get code”",
        "accounts.official_site": "Official Telegram site",
        "accounts.save_changes": "Save changes",
        "accounts.no_match": "No matches",
        "accounts.no_match_hint": "Change the phone or pick the “All” filter.",
        "accounts.empty_title": "Connect your first account",
        "accounts.empty_text": "Add a Telegram account to get started.",
        "accounts.empty_add": "Add account",
        "accounts.delete_title": "Confirm",
        "accounts.delete_confirm": "Delete the account?",
        "dlg.error": "Error",
        "dlg.fatal": "Critical error",
        "dlg.exit_title": "Exit",
        "dlg.exit_text": "A task is running. Close the application?",
        "dlg.campaign_busy_title": "Campaign is running",
        "dlg.campaign_busy_text": "Stop the task or wait for the check before switching",
        "dlg.task_running_title": "Task is running",
        "dlg.task_running_text": "Stop the task: the account is needed for the test send.",
        "dlg.test_saved": "Test to Saved Messages",
        "dlg.test_saved_pick": "Tick the account to send the test from.",
        "dlg.complete.text": "Open Inviter Studio: the result is in the task, details are in the log.",
        "dlg.complete.completed": "Task completed",
        "dlg.complete.failed": "Task needs attention",
        "dlg.complete.stopped": "Task stopped",
        "dlg.complete.other": "Task result",
        "wizard.title": "First run · Inviter Studio",
        "wizard.progress": "Step {n} of {total}",
        "wizard.s1.title": "Get your API ID and API Hash",
        "wizard.s1.text": "Telegram needs them to sign in. Open my.telegram.org/apps, log in with your phone "
        "and create an app. Never share the API Hash.",
        "wizard.s1.button": "Open my.telegram.org ↗",
        "wizard.s2.title": "Add an account",
        "wizard.s2.text": "Accounts → “+ Add account”: API ID, API Hash, phone, then the code from Telegram.",
        "wizard.s2.button": "Open Accounts",
        "wizard.s3.title": "Test the connection (optional)",
        "wizard.s3.text": "Press “Check” on the account card. Nothing is sent — it only verifies the login.",
        "wizard.s3.button": "Open Accounts",
        "wizard.s4.title": "Create a campaign",
        "wizard.s4.text": "A campaign holds recipients, text and settings. Pick or create one in Audience "
        "or at the top of Task, then add recipients.",
        "wizard.s4.button": "Open Audience",
        "wizard.s5.title": "Open Task",
        "wizard.s5.text": "Choose the mode, chat or text, accounts and pace. Start with the “Safe” pace "
        "and “Preview” — nothing is sent until you press “Start task”.",
        "wizard.s5.button": "Open Task",
        "wizard.dont_show": "Don't show again",
        "summary.card.title": "Summary to Saved Messages",
        "summary.card.enable": "Send a short summary to Telegram Saved Messages",
        "summary.card.hint": "Counters only: sent, added, errors. No recipient lists or phone numbers.",
        "summary.card.mode": "When:",
        "summary.card.mode_task": "When a task ends",
        "summary.card.mode_daily": "Once a day",
        "summary.card.account": "Account:",
        "summary.card.no_accounts": "No accounts",
        "summary.card.test": "Send sample",
        "summary.card.saved": "Setting saved.",
        "summary.card.sent": "Sample sent to Saved Messages.",
        "summary.card.failed": "Could not send. Check the account and connection.",
        "summary.card.pick": "Pick an account for the summary.",
        "summary.header": "Inviter Studio · summary",
        "summary.header_daily": "Inviter Studio · summary for {date}",
        "summary.outcome.completed": "Task completed",
        "summary.outcome.stopped": "Task stopped",
        "summary.outcome.failed": "Task finished with an error",
        "summary.outcome.other": "Task result",
        "summary.sent": "Sent: {n}",
        "summary.added": "Added: {n}",
        "summary.errors": "Errors: {n}",
        "summary.skipped": "Skipped: {n}",
        "summary.duration": "Time: {value}",
        "summary.tasks": "Tasks: {n}",
        "summary.sample": "This is a sample summary.",
    },
}

_current = DEFAULT_LANG
_listeners: list[Callable[[str], None]] = []


def normalize_lang(value: object) -> str:
    """Return a supported language code; anything unknown becomes the default (ru)."""
    text = str(value or "").strip().lower()
    return text if text in LANGS else DEFAULT_LANG


def get_language() -> str:
    return _current


def set_language(lang: object) -> str:
    """Switch the active language and notify listeners; returns the normalized code."""
    global _current
    _current = normalize_lang(lang)
    for listener in list(_listeners):
        try:
            listener(_current)
        except Exception:  # noqa: BLE001 - a broken view must not block the language switch
            pass
    return _current


def subscribe(listener: Callable[[str], None]) -> Callable[[], None]:
    """Register a callback fired after set_language; returns an unsubscribe function."""
    _listeners.append(listener)

    def unsubscribe() -> None:
        if listener in _listeners:
            _listeners.remove(listener)

    return unsubscribe


def t(key: str, lang: object = None, **params) -> str:
    """Translate ``key``: active/requested language -> Russian -> the key itself. Never raises."""
    code = normalize_lang(lang) if lang is not None else _current
    text = STRINGS.get(code, {}).get(key)
    if text is None:
        text = STRINGS[DEFAULT_LANG].get(key)
    if text is None:
        return key
    if params:
        try:
            return text.format(**params)
        except (KeyError, IndexError, ValueError):
            return text
    return text
