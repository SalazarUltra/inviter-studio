"""Conversion report for one campaign (or all of them) over a period, read from the existing tables.

Invites come from `targets` (outcome per person) and `logs` (who skipped / failed), messages from
`message_deliveries` and `message_followups`, the chat journal from `invited_ledger`. Rates are shares of
people that reached a final outcome in the period; nothing is written.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from core import database
from core.database import chat_key

PERIODS = {"today": "Сегодня", "7d": "7 дней", "30d": "30 дней", "all": "Всё время"}
INVITE_BUCKETS = {
    "added": "success",
    "private": "skipped",
    "not_found": "skipped",
    "error": "errors",
    "uncertain": "errors",
    "already_in": "already_in",
}
# SKIP logs carry the raw result code as their text for «already in the chat» (see InviterEngine).
FLOOD_ACTIONS = ("FLOOD_WAIT", "PEER_FLOOD")


def period_bounds(key: str, now: datetime | None = None) -> tuple[str | None, str | None]:
    """ISO bounds (naive local time like the database stores it); None means unbounded."""
    now = now or datetime.now()
    if key == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds"), None
    days = {"7d": 7, "30d": 30}.get(key)
    if days:
        return (now - timedelta(days=days)).isoformat(timespec="seconds"), None
    if key == "all":
        return None, None
    raise ValueError("Неизвестный период")


def rate(part: int, whole: int) -> float | None:
    """Percentage with one decimal; None when there is nothing to divide by."""
    return round(part * 100.0 / whole, 1) if whole else None


def format_rate(value: float | None) -> str:
    return "—" if value is None else f"{value:g}%"


def _window(column: str, since, until) -> tuple[str, list]:
    clause, params = "", []
    if since:
        clause += f" AND {column} >= ?"
        params.append(since)
    if until:
        clause += f" AND {column} < ?"
        params.append(until)
    return clause, params


def _empty_invites() -> dict[str, int]:
    return {"success": 0, "skipped": 0, "errors": 0, "already_in": 0}


def _empty_messages() -> dict[str, int]:
    return {"sent": 0, "private": 0, "uncertain": 0, "replies": 0, "followup_ready": 0, "followup_sent": 0}


def _invite_summary(counts: dict[str, int]) -> dict[str, Any]:
    total = sum(counts.values())
    return {
        **counts,
        "total": total,
        "rates": {key: rate(value, total) for key, value in counts.items()},
    }


def _message_summary(counts: dict[str, int]) -> dict[str, Any]:
    attempts = counts["sent"] + counts["private"] + counts["uncertain"]
    return {
        **counts,
        "attempts": attempts,
        "rates": {
            "sent": rate(counts["sent"], attempts),
            "private": rate(counts["private"], attempts),
            "uncertain": rate(counts["uncertain"], attempts),
            "replies": rate(counts["replies"], counts["sent"]),
            "followup_sent": rate(counts["followup_sent"], counts["replies"]),
        },
    }


async def conversion_report(db, campaign_id: int | None = None, period: str = "all", now=None) -> dict[str, Any]:
    """Counts and rates for `campaign_id` (None — every campaign) over `period` (see PERIODS)."""
    since, until = period_bounds(period, now)
    scope = "" if campaign_id is None else " AND {alias}.campaign_id=?"
    scope_params = [] if campaign_id is None else [int(campaign_id)]
    invites, messages = _empty_invites(), _empty_messages()
    accounts: dict[str, dict[str, Any]] = {}

    def account_row(phone) -> dict[str, Any]:
        key = phone or "—"
        return accounts.setdefault(
            key,
            {"phone": key, "invites": {**_empty_invites(), "floods": 0, "ledger": 0}, "messages": _empty_messages()},
        )

    async with database._connect() as conn:
        name, kind = "Все кампании", None
        if campaign_id is not None:
            row = await (
                await conn.execute(
                    "SELECT name,kind FROM campaigns WHERE id=? AND deleted_at IS NULL", (int(campaign_id),)
                )
            ).fetchone()
            if row is None:
                raise ValueError("Кампания не найдена")
            name, kind = row[0], row[1]
        phones = {row[0]: row[1] for row in await (await conn.execute("SELECT id,phone FROM accounts")).fetchall()}

        # Invites: one row per person, by the time the outcome was recorded.
        window, window_params = _window("t.processed_at", since, until)
        for status, account_id, count in await (
            await conn.execute(
                "SELECT t.status,t.added_by_account_id,COUNT(*) FROM targets t "
                "JOIN campaigns c ON c.id=t.campaign_id AND c.kind='invite' AND c.deleted_at IS NULL "
                "WHERE t.status IN ('added','already_in','private','not_found','error','uncertain')"
                + scope.format(alias="t")
                + window
                + " GROUP BY t.status,t.added_by_account_id",
                [*scope_params, *window_params],
            )
        ).fetchall():
            bucket = INVITE_BUCKETS[status]
            invites[bucket] += count
            if status == "added":
                account_row(phones.get(account_id))["invites"]["success"] += count

        # Who skipped / failed comes from the journal (the target row keeps no account for those outcomes).
        window, window_params = _window("l.created_at", since, until)
        for phone, action, text, count in await (
            await conn.execute(
                "SELECT l.account_phone,l.action,l.message,COUNT(*) FROM logs l "
                "JOIN campaigns c ON c.id=l.campaign_id AND c.kind='invite' AND c.deleted_at IS NULL "
                "WHERE l.account_phone IS NOT NULL AND l.action IN ('SKIP','ADD_ERROR','FLOOD_WAIT','PEER_FLOOD')"
                + scope.format(alias="l")
                + window
                + " GROUP BY l.account_phone,l.action,l.message",
                [*scope_params, *window_params],
            )
        ).fetchall():
            row = account_row(phone)["invites"]
            if action == "ADD_ERROR":
                row["errors"] += count
            elif action in FLOOD_ACTIONS:
                row["floods"] += count
            elif str(text or "").strip() == "already_in":
                row["already_in"] += count
            else:
                row["skipped"] += count

        # Chat journal: people actually recorded as invited (two keys per person, id and @name).
        ledger_people = 0
        ledger_clause, ledger_params = None, []  # None: nothing to count (messages campaign or no chat set)
        if campaign_id is None:
            ledger_clause = ""
        elif kind == "invite":
            target = await (
                await conn.execute(
                    "SELECT value FROM settings WHERE key=?", (f"campaign:{int(campaign_id)}:target_chat",)
                )
            ).fetchone()
            key = chat_key(target[0]) if target and target[0] else ""
            if key:
                ledger_clause, ledger_params = " AND chat_key=?", [key]
        if ledger_clause is not None:
            window, window_params = _window("invited_at", since, until)
            for account_id, ids, names in await (
                await conn.execute(
                    "SELECT account_id,SUM(user_key LIKE 'id:%'),SUM(user_key LIKE 'u:%') FROM invited_ledger "
                    "WHERE 1=1" + ledger_clause + window + " GROUP BY account_id",
                    [*ledger_params, *window_params],
                )
            ).fetchall():
                people = max(int(ids or 0), int(names or 0))
                ledger_people += people
                account_row(phones.get(account_id))["invites"]["ledger"] += people
        # Messages: first messages by delivery state, replies and second messages from the follow-up queue.
        window, window_params = _window("d.created_at", since, until)
        states = {"sent": "sent", "blocked": "private", "uncertain": "uncertain"}
        for account_id, state, count in await (
            await conn.execute(
                "SELECT d.account_id,d.state,COUNT(*) FROM message_deliveries d "
                "JOIN campaigns c ON c.id=d.campaign_id AND c.kind='messages' AND c.deleted_at IS NULL "
                "WHERE d.state IN ('sent','blocked','uncertain')"
                + scope.format(alias="d")
                + window
                + " GROUP BY d.account_id,d.state",
                [*scope_params, *window_params],
            )
        ).fetchall():
            messages[states[state]] += count
            account_row(phones.get(account_id))["messages"][states[state]] += count

        window, window_params = _window("f.created_at", since, until)
        for account_id, state, replied, count in await (
            await conn.execute(
                "SELECT f.account_id,f.state,f.reply_id IS NOT NULL,COUNT(*) FROM message_followups f "
                "JOIN campaigns c ON c.id=f.campaign_id AND c.kind='messages' AND c.deleted_at IS NULL "
                "WHERE 1=1"
                + scope.format(alias="f")
                + window
                + " GROUP BY f.account_id,f.state,f.reply_id IS NOT NULL",
                [*scope_params, *window_params],
            )
        ).fetchall():
            row = account_row(phones.get(account_id))["messages"]
            if replied or state in ("ready", "sending", "sent"):
                messages["replies"] += count
                row["replies"] += count
            if state == "ready":
                messages["followup_ready"] += count
                row["followup_ready"] += count
            elif state == "sent":
                messages["followup_sent"] += count
                row["followup_sent"] += count

    rows = []
    for row in sorted(accounts.values(), key=lambda item: item["phone"]):
        row["invites"] = _invite_summary({key: row["invites"][key] for key in invites}) | {
            "floods": row["invites"]["floods"],
            "ledger": row["invites"]["ledger"],
        }
        row["messages"] = _message_summary(row["messages"])
        rows.append(row)
    return {
        "campaign_id": campaign_id,
        "campaign_name": name,
        "kind": kind,
        "period": period,
        "since": since,
        "until": until,
        "invites": _invite_summary(invites) | {"ledger": ledger_people},
        "messages": _message_summary(messages),
        "accounts": rows,
    }
