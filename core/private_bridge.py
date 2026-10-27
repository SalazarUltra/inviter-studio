"""Bridge: recipients with closed privacy ("private") -> direct messages with the group invite link.

Additive helpers only. The messages campaign reuses the normal message engine and the global
``message_deliveries`` ledger, so nobody receives a repeated first message.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from core.account_manager import AccountManager
from core.validation import normalize_username, validate_message

LINK_PLACEHOLDER = "{ссылка}"
DEFAULT_TEMPLATE = f"Здравствуйте! Приглашаю вас в нашу группу: {LINK_PLACEHOLDER}"
# Settings copied from the invite campaign so the user only reviews the text and presses start.
COPIED_SETTINGS = (
    "interval_min",
    "interval_max",
    "schedule_enabled",
    "schedule_from",
    "schedule_to",
    "protection_consecutive",
    "protection_break_min",
    "protection_break_max",
    "selected_accounts",
)
# Direct messages to strangers risk PeerFlood: the bridge never copies a faster pace than this.
CONSERVATIVE_INTERVAL_MIN = 60
CONSERVATIVE_INTERVAL_MAX = 120
CONSERVATIVE_MAX_CONSECUTIVE = 3
PEER_FLOOD_WARNING = (
    "Внимание: личные сообщения незнакомым людям — самое рискованное действие. "
    "Слишком быстрая отправка вызывает PeerFlood (Telegram блокирует рассылку на ~24 ч). "
    f"Поэтому интервал будет не меньше {CONSERVATIVE_INTERVAL_MIN} сек, а перерыв — после "
    f"{CONSERVATIVE_MAX_CONSECUTIVE} сообщений подряд или чаще."
)


def conservative_settings(settings: dict) -> dict:
    """Return copied invite settings tightened to a safe preset for direct messages."""

    def number(key, default):
        try:
            return int(str(settings.get(key, default)).strip())
        except ValueError:
            return default

    result = dict(settings)
    low = max(CONSERVATIVE_INTERVAL_MIN, number("interval_min", CONSERVATIVE_INTERVAL_MIN))
    high = max(CONSERVATIVE_INTERVAL_MAX, number("interval_max", CONSERVATIVE_INTERVAL_MAX), low)
    result["interval_min"], result["interval_max"] = str(low), str(high)
    result["protection_consecutive"] = str(
        max(1, min(CONSERVATIVE_MAX_CONSECUTIVE, number("protection_consecutive", 3)))
    )
    break_min = max(1, number("protection_break_min", 5))
    result["protection_break_min"] = str(break_min)
    result["protection_break_max"] = str(max(break_min, number("protection_break_max", 15)))
    return result


def chat_link(raw: str) -> str:
    """Return a shareable t.me link for the chat field value, or '' when it cannot be built.

    Public chats (``@name`` / ``t.me/name``) become ``https://t.me/name``; private invite links
    (``t.me/+hash`` / ``t.me/joinchat/hash``) are kept as they are.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith(("http://", "https://", "t.me/", "telegram.me/")):
        url = urlparse(raw if "://" in raw else f"https://{raw}")
        if url.hostname in {"t.me", "telegram.me"}:
            path = url.path.strip("/")
            if path.startswith(("+", "joinchat/")):
                return f"https://t.me/{path}"
    try:
        return f"https://t.me/{normalize_username(raw)}"
    except ValueError:
        return ""


def build_message(template: str, link: str) -> str:
    """Put the link into the message text (placeholder, otherwise appended) and validate it."""
    normalized = chat_link(link)
    if not normalized:
        raise ValueError("Укажите ссылку на группу: https://t.me/имя или invite-ссылку https://t.me/+…")
    template = template or ""
    if LINK_PLACEHOLDER in template:
        text = template.replace(LINK_PLACEHOLDER, normalized)
    elif normalized in template:
        text = template
    else:
        text = (template.rstrip() + "\n" + normalized).strip()
    return validate_message(text.strip())


async def create_private_dm_campaign(db, message: str, *, name: str | None = None) -> dict:
    """Create a messages campaign from the invite campaign's ``private`` targets.

    Returns ``campaign_id``, ``count`` (private targets taken), ``inserted`` and ``pending``
    (really queued; people already in the delivery ledger are not queued again).
    """
    if await db.get_campaign_kind() != "invite":
        raise ValueError("Кнопка работает в кампании приглашений")
    message = validate_message(message)
    rows = await db.get_targets("private", limit=None)
    usernames = [row["username"] for row in reversed(rows)]
    if not usernames:
        raise ValueError("Нет получателей со статусом «Закрытый»")
    title = (name or f"Закрытые → ЛС · {datetime.now():%d.%m %H:%M}").strip()[:80]
    campaign_id = await db.create_campaign(title, "messages")
    new_db = db.for_campaign(campaign_id)
    try:
        inserted = await new_db.bulk_insert_targets(usernames)
        settings = {"operation_mode": "messages", "message_text": message, "followup_enabled": "0"}
        for key in COPIED_SETTINGS:
            value = await db.get_setting(key)
            if value is not None:
                settings[key] = value
        await new_db.save_campaign_settings(conservative_settings(settings))
        pending = (await new_db.get_targets_stats())["pending"]
    except Exception:
        try:
            await new_db.delete_campaign()
        except Exception:  # noqa: BLE001
            pass
        raise
    return {"campaign_id": campaign_id, "count": len(usernames), "inserted": inserted, "pending": pending}


async def export_invite_link(db, account_ids: list[int], chat: str) -> str:
    """Create an invite link through the first account that can do it (needs a saved public chat)."""
    errors = []
    for account_id in account_ids:
        account = await db.get_account(account_id)
        if not account:
            continue
        if AccountManager.is_session_busy(account["phone"], account.get("session_name")):
            errors.append(f"{account['phone']}: аккаунт занят другой операцией")
            continue
        manager = AccountManager(account, db)
        try:
            await manager.connect()
            rights = await manager.check_admin_rights(chat)
            if not rights.get("ok"):
                errors.append(f"{account['phone']}: {rights.get('reason')}")
                continue
            return await manager.export_invite_link(chat)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{account['phone']}: {exc}")
        finally:
            await manager.disconnect()
    raise RuntimeError("Не удалось создать ссылку-приглашение. " + "; ".join(errors or ["нет доступных аккаунтов"]))
