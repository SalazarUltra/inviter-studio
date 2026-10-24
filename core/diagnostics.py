"""Read-only preflight checks. Never invites users or sends Telegram messages."""

import asyncio
from datetime import datetime

from core.account_manager import AccountManager
from core.inviter import InviterEngine
from core.user_feedback import explain_error
from core.validation import normalize_username, validate_message


async def preflight(db, account_ids, chat, live=False, mode="invite", message="", followup_enabled=False):
    results = []

    def add(label, ok, detail, *, blocking=True):
        ok = bool(ok)
        results.append({"label": label, "ok": ok, "detail": detail, "blocking": bool(blocking)})

    if mode == "messages":
        try:
            validate_message(message)
        except ValueError as exc:
            add("Сообщение", False, str(exc))
        else:
            add("Сообщение", True, "Текст задан; общий журнал защищает от повторов")
    elif mode == "invite":
        try:
            chat = normalize_username(chat)
        except ValueError:
            add("Чат", False, "Укажите публичный @username чата")
        else:
            add("Чат", True, f"@{chat}")
    else:
        add("Режим", False, "Неизвестный режим")
    stats = await db.get_targets_stats()
    followup_rows = await db.get_followups(states=("waiting", "ready")) if mode == "messages" else []
    selected_account_ids = set(account_ids)
    selected_followup_rows = [row for row in followup_rows if row["account_id"] in selected_account_ids]
    followup_owner_ids = {row["account_id"] for row in followup_rows}
    followups = len(followup_rows) if followup_enabled else 0
    if followup_rows and not followup_enabled:
        add(
            "Вторые сообщения",
            False,
            f"Осталось {len(followup_rows)} сохранённых ожиданий. Включите вторые сообщения или отмените очередь",
        )
    pending = stats["pending"]
    add(
        "Очередь кампании",
        pending > 0 or bool(followups),
        f"Первых сообщений: {pending}; ожиданий второго: {followups}",
    )
    accounts = await db.get_accounts_by_ids(account_ids)
    add("Аккаунты", bool(accounts) and len(accounts) == len(set(account_ids)), f"Выбрано: {len(accounts)}")
    first_message_accounts = []
    followup_accounts = []
    invite_accounts = []
    live_connected = []
    live_invite_rights = []
    for account in accounts:
        label = account["phone"]
        usable = int(account.get("is_enabled", 1)) == 1 and account.get("status") not in {
            "restricted",
            "unauthorized",
            "inactive",
        }
        count_key = "sent_today" if mode == "messages" else "added_today"
        date_key = "last_message_reset_date" if mode == "messages" else "last_reset_date"
        used_today = (
            int(account.get(count_key) or 0) if account.get(date_key) == datetime.now().date().isoformat() else 0
        )
        remaining = max(0, InviterEngine.get_safe_daily_limit(account) - used_today)
        flood = account.get("flood_until")
        if flood:
            try:
                usable = usable and datetime.fromisoformat(flood) <= datetime.now()
            except ValueError:
                usable = False
        status = account.get("status")
        reason = {
            "restricted": (
                "Telegram отклонил действие с этим аккаунтом. Аккаунт сохранён; проверка входа не подтверждает "
                "возможность писать новым получателям. Проверьте состояние аккаунта в Telegram. "
                "Когда причина устранена, откройте «Аккаунты» → «Изменить» → «Готов»"
            ),
            "unauthorized": "Сессия отозвана; войдите в аккаунт заново",
            "inactive": "Аккаунт неактивен; проверьте подключение",
        }.get(status)
        if status == "flood" and flood and not usable:
            reason = f"Telegram требует ожидания до {flood}"
        status_label = {
            "active": "готов",
            "restricted": "требует проверки отправки",
            "unauthorized": "нужен повторный вход",
            "inactive": "неактивен",
            "flood": "пауза Telegram",
            "error": "ошибка подключения",
        }.get(status, "неизвестен")
        detail = f"Доступный лимит сегодня: {remaining}; состояние: {status_label}"
        if reason:
            detail += f". {reason}"
        # Individual account issues are shown to the user, but one restricted
        # account must not prevent the healthy selected accounts from running.
        # First-message quota does not apply to a reply-triggered follow-up.
        account_ready = usable and (
            (mode == "messages" and followup_enabled and bool(followups))
            or ((pending > 0 if mode == "messages" else True) and remaining > 0)
        )
        add(label, account_ready, detail, blocking=False)
        if usable:
            if mode == "messages":
                if remaining > 0:
                    first_message_accounts.append(account)
                if followup_enabled and account["id"] in followup_owner_ids:
                    followup_accounts.append(account)
            elif remaining > 0:
                invite_accounts.append(account)
        if live and usable:
            manager = AccountManager(account, db)
            try:
                await asyncio.wait_for(manager.connect(), 40)
                live_connected.append(account)
                add(f"{label} · подключение", True, "Сессия авторизована; соединение работает", blocking=False)
                if mode == "invite" and chat and not any(r["label"] == "Чат" and not r["ok"] for r in results):
                    rights = await asyncio.wait_for(manager.check_admin_rights(chat), 20)
                    if rights["ok"]:
                        live_invite_rights.append(account)
                    # Do not expose raw provider exceptions or connection URLs.
                    add(
                        f"{label} · права",
                        rights["ok"],
                        "Право приглашения подтверждено"
                        if rights["ok"]
                        else "Права не подтверждены: проверьте членство и роль администратора",
                        blocking=False,
                    )
            except Exception as exc:
                add(
                    f"{label} · подключение",
                    False,
                    "Проверка не пройдена: " + explain_error(exc),
                    blocking=False,
                )
            finally:
                await manager.disconnect()

    if accounts:
        if mode == "messages":
            # A campaign can contain both unfinished first messages and reply
            # follow-ups; each queue needs at least one account that can handle it.
            if pending:
                available_first = live_connected if live else first_message_accounts
                first_ready = [a for a in available_first if any(a["id"] == f["id"] for f in first_message_accounts)]
            else:
                first_ready = []
            available_followups = live_connected if live else followup_accounts
            available_followup_ids = {a["id"] for a in available_followups}
            followup_ready = followup_owner_ids & available_followup_ids
            unavailable_followup_ids = followup_owner_ids - followup_ready
            queue_ready = (not pending or bool(first_ready)) and not unavailable_followup_ids
            usable_labels = []
            if pending:
                usable_labels.append(f"первые сообщения: {len(first_ready)}")
            if followups:
                usable_labels.append(
                    f"ответы и вторые сообщения: {len(followup_ready)} из {len(followup_owner_ids)} аккаунтов-владельцев"
                )
            detail = "; ".join(usable_labels) if usable_labels else "Нет отправок, ожидающих обработки"
            if unavailable_followup_ids:
                unavailable_count = sum(row["account_id"] in unavailable_followup_ids for row in followup_rows)
                selected_count = len(selected_followup_rows)
                if selected_count < followups:
                    detail += (
                        f". Выберите исходные аккаунты для {followups - selected_count} ожиданий второго сообщения"
                    )
                else:
                    detail += f". {unavailable_count} ожиданий закреплены за отключённым или ограниченным аккаунтом"
            add("Доступность отправки", queue_ready, detail)
        else:
            available_invites = live_invite_rights if live else invite_accounts
            if live:
                queue_ready = bool(available_invites)
                detail = f"Аккаунтов с подтверждённым правом приглашения: {len(available_invites)}"
            else:
                queue_ready = bool(available_invites)
                detail = f"Включённых аккаунтов без известного ограничения: {len(available_invites)}"
            add("Доступность отправки", queue_ready, detail)

    return {
        "ok": all(not item["blocking"] or item["ok"] for item in results),
        "checks": results,
        "live": live,
    }


def format_report(report):
    title = "Проверка подключения и прав" if report["live"] else "Предварительный просмотр без подключения"
    return (
        title
        + "\n\n"
        + "\n".join(f"{'✓' if item['ok'] else '✕'} {item['label']}: {item['detail']}" for item in report["checks"])
    )
