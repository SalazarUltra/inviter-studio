"""Shared validation for UI, imports and persistence."""

import re
from urllib.parse import urlparse


def validate_message(value: str) -> str:
    if not value.strip():
        raise ValueError("Введите текст сообщения")
    if len(value.encode("utf-16-le")) // 2 > 4096:
        raise ValueError("Сообщение не должно превышать 4096 символов (эмодзи могут занимать два)")
    return value


# Direct messages to strangers are the riskiest action (PeerFlood): keep the gap soft.
MESSAGES_MIN_INTERVAL = 45
MESSAGES_DEFAULT_INTERVAL = (60, 120)
INVITE_MAX_INTERVAL = 300
MESSAGES_MAX_INTERVAL = 86400


def validate_interval(mode: str, interval_min: int, interval_max: int) -> None:
    """Raise a Russian ValueError when the interval is unsafe or inconsistent for the mode."""
    maximum = MESSAGES_MAX_INTERVAL if mode == "messages" else INVITE_MAX_INTERVAL
    if mode == "messages" and interval_min < MESSAGES_MIN_INTERVAL and interval_min <= interval_max:
        raise ValueError(_soft_interval_text())
    if not 10 <= interval_min <= interval_max <= maximum:
        raise ValueError(f"Укажите интервал от 10 до {maximum} секунд. Минимум не должен быть больше максимума.")


def _soft_interval_text() -> str:
    return (
        f"Для личных сообщений минимальный интервал — {MESSAGES_MIN_INTERVAL} секунд "
        f"(рекомендуем {MESSAGES_DEFAULT_INTERVAL[0]}–{MESSAGES_DEFAULT_INTERVAL[1]}): "
        "частая отправка незнакомым людям быстро приводит к ограничению аккаунта (PeerFlood)."
    )


def normalize_username(value: str) -> str:
    value = value.strip().lstrip("\ufeff")
    if value.startswith(("https://", "http://", "t.me/", "telegram.me/")):
        url = urlparse(value if "://" in value else f"https://{value}")
        if url.hostname not in {"t.me", "telegram.me"}:
            raise ValueError("Ожидается ссылка t.me или Telegram username")
        value = url.path.strip("/")
    value = value.lstrip("@").lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{3,31}", value):
        raise ValueError("Никнейм: 4–32 символа, латинские буквы, цифры и _")
    return value


# Recipients known only by Telegram id are stored under the key «id:<number>» in the username column
# (a real username can never start with a digit or contain «:», so the forms cannot collide).
ID_PREFIX = "id:"
MAX_USER_ID = 2**52


def parse_user_id(value) -> int | None:
    """Telegram user id from «123456», «id:123456» or «ID 123456»; None when the text is not an id."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value if 0 < value <= MAX_USER_ID else None
    text = str(value or "").strip().lstrip("\ufeff")
    lowered = text.lower()
    if lowered.startswith("id:"):
        text = text[3:].strip()
    elif lowered.startswith("id "):
        text = text[3:].strip()
    if not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    return number if 0 < number <= MAX_USER_ID else None


def id_recipient(user_id: int) -> str:
    return f"{ID_PREFIX}{int(user_id)}"


def is_id_recipient(value) -> bool:
    return str(value or "").startswith(ID_PREFIX)


def recipient_user_id(value) -> int | None:
    """User id carried by a stored «id:<number>» recipient key (None for ordinary usernames)."""
    return parse_user_id(value) if is_id_recipient(value) else None


def recipient_label(value) -> str:
    """Human label for logs and tables: «@name» or «ID 123456»."""
    number = recipient_user_id(value)
    return f"ID {number}" if number is not None else f"@{value}"


def recipient_line(value) -> str:
    """Text form for editors/exports that parses back to the same recipient: «@name» or «id:123»."""
    return str(value) if is_id_recipient(value) else f"@{value}"


def normalize_recipient(value) -> str:
    """Username, t.me link, bare numeric Telegram id or «id:123» -> stored recipient key."""
    text = str(value if value is not None else "").strip().lstrip("\ufeff")
    number = parse_user_id(text)
    if number is not None:
        return id_recipient(number)
    if text.lower().startswith("id:"):
        raise ValueError("ID пользователя: положительное число, например id:123456789")
    return normalize_username(text)


def parse_usernames(text: str) -> list[str]:
    """Validate the whole list before mutating a campaign; preserve first occurrence order.

    Lines may be @usernames, t.me links, bare Telegram ids (123456789) or «id:123456789».
    """
    usernames = {}
    for line_number, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            usernames[normalize_recipient(line)] = None
        except ValueError as exc:
            raise ValueError(f"Строка {line_number}: {exc}") from exc
    return list(usernames)


def validate_account(payload: dict) -> dict:
    result = dict(payload)
    phone = str(result["phone"]).strip()
    if not re.fullmatch(r"\+?[1-9][0-9]{6,14}", phone):
        raise ValueError("Укажите телефон в международном формате")
    result["phone"] = "+" + phone.lstrip("+")
    if int(result["api_id"]) <= 0:
        raise ValueError("API ID должен быть положительным числом")
    if not re.fullmatch(r"[a-fA-F0-9]{32}", str(result["api_hash"]).strip()):
        raise ValueError("API Hash должен содержать 32 шестнадцатеричных символа")
    result["api_hash"] = str(result["api_hash"]).strip()
    limit = int(result.get("daily_limit", 40))
    if not 0 <= limit <= 10000:
        raise ValueError("Дневной лимит должен быть от 0 до 10000")
    result["daily_limit"] = limit
    if result.get("account_age_days") not in (None, ""):
        age = int(result["account_age_days"])
        if not 0 <= age <= 36500:
            raise ValueError("Возраст аккаунта должен быть от 0 до 36500 дней")
        result["account_age_days"] = age
    validate_proxy(result)
    return result


def validate_proxy(payload: dict) -> None:
    kind = str(payload.get("proxy_type") or "").upper()
    if not kind:
        return
    if kind not in {"SOCKS5", "HTTP"}:
        raise ValueError("Поддерживаются прокси SOCKS5 и HTTP")
    if not str(payload.get("proxy_host") or "").strip():
        raise ValueError("Укажите хост прокси")
    if not 1 <= int(payload.get("proxy_port") or 0) <= 65535:
        raise ValueError("Порт прокси должен быть от 1 до 65535")


def time_minutes(value: str) -> int:
    if not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", value):
        raise ValueError("Время должно быть в формате HH:MM")
    hour, minute = map(int, value.split(":"))
    if hour > 23:
        raise ValueError("Час должен быть от 00 до 23")
    return hour * 60 + minute


def csv_cell(value):
    """Prevent spreadsheet formula execution when opening exported data."""
    text = str(value) if value is not None else ""
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text
