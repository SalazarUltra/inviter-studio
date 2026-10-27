"""Optional summary sent to the user's own Telegram «Избранное» (Saved Messages) and/or their own bot.

Privacy: the text contains counters only (sent / added / errors / skipped), never recipient
usernames, phone numbers or chat names. Everything here is opt-in (``summary_enabled``).
"""

from __future__ import annotations

import json
from datetime import date as date_type
from typing import Any, Awaitable, Callable

from core.account_manager import AccountManager
from core.bot_notify import (
    CHANNEL_BOT,
    CHANNEL_OFF,
    CHANNEL_SAVED,
    NOTIFY_CHANNEL_KEY,
    NOTIFY_CHAT_ID_KEY,
    NOTIFY_TOKEN_KEY,
    BotNotifyError,
    normalize_channel,
    send_to_bot,
    uses_bot,
    uses_saved,
)
from core.user_feedback import explain_error
from core.validation import validate_message

SUMMARY_ENABLED_KEY = "summary_enabled"
SUMMARY_MODE_KEY = "summary_mode"
SUMMARY_ACCOUNT_KEY = "summary_account_id"
SUMMARY_PENDING_KEY = "summary_pending"
MODE_TASK = "task"
MODE_DAILY = "daily"
COUNT_FIELDS = ("sent", "added", "errors", "skipped")

Sender = Callable[[Any, int, str], Awaitable[Any]]
BotSender = Callable[[Any, str], Awaitable[Any]]
LogFn = Callable[[str, str], None]


def _number(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def normalize_mode(value: Any) -> str:
    return MODE_DAILY if str(value or "").strip().lower() == MODE_DAILY else MODE_TASK


def counts_from_summary(summary: dict) -> dict[str, int]:
    """Reduce an engine summary to the four public counters (first + second messages count as sent)."""
    return {
        "sent": _number(summary.get("sent")) + _number(summary.get("followup_sent")),
        "added": _number(summary.get("added")),
        "errors": _number(summary.get("errors")),
        "skipped": _number(summary.get("skipped")),
    }


def build_summary_text(
    counts: dict,
    *,
    lang: str | None = None,
    outcome: str | None = None,
    duration: str | None = None,
    tasks: int | None = None,
    day: str | None = None,
) -> str:
    """Short human text. Only whitelisted counters are read, so extra keys (phones, lists) can never leak."""
    from ui.i18n import t

    lines = [t("summary.header_daily", lang, date=day) if day else t("summary.header", lang)]
    if outcome:
        known = outcome in {"completed", "stopped", "failed"}
        lines.append(t(f"summary.outcome.{outcome}" if known else "summary.outcome.other", lang))
    if tasks is not None:
        lines.append(t("summary.tasks", lang, n=_number(tasks)))
    for field in COUNT_FIELDS:
        lines.append(t(f"summary.{field}", lang, n=_number(counts.get(field))))
    if duration:
        lines.append(t("summary.duration", lang, value=str(duration)[:16]))
    return "\n".join(lines)


def _has_data(counts: dict) -> bool:
    return any(_number(counts.get(field)) for field in COUNT_FIELDS)


def parse_pending(raw: Any) -> dict | None:
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            return None
    if not isinstance(data, dict) or not isinstance(data.get("date"), str):
        return None
    result: dict[str, Any] = {field: _number(data.get(field)) for field in COUNT_FIELDS}
    result["date"] = data["date"]
    result["tasks"] = _number(data.get("tasks"))
    return result


def merge_pending(pending: Any, counts: dict, today: str, *, carry_over: bool = False) -> tuple[dict | None, dict]:
    """Add a finished task to the daily accumulator.

    Returns ``(to_flush, new_pending)``: ``to_flush`` is an older day's accumulator that is now due.
    With ``carry_over`` an older day is folded into today's totals instead (used when it could not be sent).
    """
    current = parse_pending(pending)
    to_flush = None
    fresh = {"date": today, "tasks": 0, **{field: 0 for field in COUNT_FIELDS}}
    if current is not None and current["date"] != today:
        if carry_over:
            fresh.update({key: current[key] for key in (*COUNT_FIELDS, "tasks")})
        elif _has_data(current):
            to_flush = current
        current = None
    if current is None:
        current = fresh
    for field in COUNT_FIELDS:
        current[field] += _number(counts.get(field))
    current["tasks"] += 1
    return to_flush, current


async def send_to_saved(db, account_id: int, text: str) -> None:
    """Send ``text`` to the account's own Saved Messages (``SendMessage`` to 'me')."""
    account = await db.get_account(account_id)
    if not account:
        raise ValueError("Аккаунт не найден")
    manager = AccountManager(account, db)
    try:
        await manager.connect()
        await manager.send_saved_message(validate_message(text))
    finally:
        await manager.disconnect()


async def _load_config(db) -> dict:
    values = await db.get_settings_map(
        [
            SUMMARY_ENABLED_KEY,
            SUMMARY_MODE_KEY,
            SUMMARY_ACCOUNT_KEY,
            SUMMARY_PENDING_KEY,
            NOTIFY_CHANNEL_KEY,
            NOTIFY_CHAT_ID_KEY,
            NOTIFY_TOKEN_KEY,
            "ui_lang",
        ]
    )
    try:
        account_id = int(values.get(SUMMARY_ACCOUNT_KEY) or 0)
    except (TypeError, ValueError):
        account_id = 0
    if values.get(NOTIFY_CHANNEL_KEY) is None:
        # Before bot notifications existed the opt-in flag meant «Saved Messages».
        channel = CHANNEL_SAVED if values.get(SUMMARY_ENABLED_KEY) == "1" else CHANNEL_OFF
    else:
        channel = normalize_channel(values.get(NOTIFY_CHANNEL_KEY))
    chat = str(values.get(NOTIFY_CHAT_ID_KEY) or "").strip()
    return {
        "enabled": channel != CHANNEL_OFF,
        "channel": channel,
        "mode": normalize_mode(values.get(SUMMARY_MODE_KEY)),
        "account_id": account_id,
        "bot_ready": bool(values.get(NOTIFY_TOKEN_KEY)) and bool(chat),
        "pending": values.get(SUMMARY_PENDING_KEY),
        "lang": values.get("ui_lang") or "ru",
    }


def _targets(config: dict) -> tuple[bool, bool]:
    """Which channels can actually be used right now: ``(saved, bot)``."""
    saved = uses_saved(config["channel"]) and bool(config["account_id"])
    bot = uses_bot(config["channel"]) and bool(config["bot_ready"])
    return saved, bot


def _missing_status(config: dict) -> str:
    """Status when the chosen channel(s) are not set up: bot-only reports ``no_bot``."""
    return "no_bot" if config["channel"] == CHANNEL_BOT else "no_account"


async def _deliver(
    config: dict,
    text: str,
    *,
    sender: Sender,
    bot_sender: BotSender,
    db,
    log: LogFn | None,
) -> bool:
    """Send via every usable channel. True when at least one delivered (so a retry never duplicates)."""
    saved, bot = _targets(config)
    delivered = False
    if saved:
        try:
            await sender(db, config["account_id"], text)
            delivered = True
        except Exception as exc:  # noqa: BLE001 - a summary must never break the app or a finished task
            if log:
                log("WARN", "Сводка в «Избранное» не отправлена: " + explain_error(exc))
    if bot:
        try:
            await bot_sender(db, text)
            delivered = True
        except Exception as exc:  # noqa: BLE001
            if log:
                # BotNotifyError texts are token-free; anything else is reduced to a generic explanation.
                reason = str(exc) if isinstance(exc, BotNotifyError) else explain_error(exc)
                log("WARN", "Сводка через бота не отправлена: " + reason)
    return delivered


async def flush_daily(
    db,
    *,
    sender: Sender = send_to_saved,
    bot_sender: BotSender = send_to_bot,
    today: str | None = None,
    log: LogFn | None = None,
) -> str:
    """Send a finished day's accumulator (if any). Safe to call often; returns a short status."""
    config = await _load_config(db)
    if not config["enabled"] or config["mode"] != MODE_DAILY:
        return "disabled"
    pending = parse_pending(config["pending"])
    today = today or date_type.today().isoformat()
    if pending is None or pending["date"] >= today or not _has_data(pending):
        if pending is not None and pending["date"] < today:
            await db.set_setting(SUMMARY_PENDING_KEY, "")
        return "nothing"
    if not any(_targets(config)):
        return _missing_status(config)
    text = build_summary_text(pending, lang=config["lang"], tasks=pending["tasks"], day=pending["date"])
    if not await _deliver(config, text, sender=sender, bot_sender=bot_sender, db=db, log=log):
        return "failed"  # keep the accumulator; the next check retries
    await db.set_setting(SUMMARY_PENDING_KEY, "")
    return "sent"


async def deliver_task_summary(
    db,
    summary: dict,
    *,
    sender: Sender = send_to_saved,
    bot_sender: BotSender = send_to_bot,
    today: str | None = None,
    log: LogFn | None = None,
) -> str:
    """Handle a finished task according to the opt-in settings. Returns a short status string."""
    config = await _load_config(db)
    if not config["enabled"]:
        return "disabled"
    counts = counts_from_summary(summary)
    outcome = summary.get("outcome", "completed")
    today = today or date_type.today().isoformat()
    if config["mode"] == MODE_DAILY:
        # Send an older finished day first; if that fails, its counters are carried into today's totals.
        status = await flush_daily(db, sender=sender, bot_sender=bot_sender, today=today, log=log)
        pending = (await _load_config(db))["pending"]
        _, merged = merge_pending(pending, counts, today, carry_over=True)
        await db.set_setting(SUMMARY_PENDING_KEY, json.dumps(merged))
        return "failed" if status == "failed" else "queued"
    if not _has_data(counts) and outcome != "failed":
        return "empty"
    if not any(_targets(config)):
        return _missing_status(config)
    text = build_summary_text(counts, lang=config["lang"], outcome=outcome, duration=summary.get("duration"))
    ok = await _deliver(config, text, sender=sender, bot_sender=bot_sender, db=db, log=log)
    return "sent" if ok else "failed"
