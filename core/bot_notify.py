"""User-owned Telegram bot notifications (Bot API over HTTPS).

Each user connects THEIR OWN bot (created in @BotFather): the token is stored DPAPI-protected like API keys,
the chat id is a plain number. Automatic summaries stay counters-only (see ``core.summary``). Separately, the
user can manually send a parsed TXT file (usernames / ids they already collected) via ``sendDocument``.

Safety rules:
* the token lives only in the request URL path; it is never logged and never put into an error message
  (errors are rebuilt from scratch, aiohttp exceptions are re-raised ``from None``);
* every request has a timeout, redirects are not followed;
* every failure becomes a short Russian ``BotNotifyError``.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import aiohttp

from core.secrets import protect, reveal

NOTIFY_TOKEN_KEY = "notify_bot_token"
NOTIFY_CHAT_ID_KEY = "notify_chat_id"
NOTIFY_CHANNEL_KEY = "notify_channel"

CHANNEL_OFF = "off"
CHANNEL_SAVED = "saved"
CHANNEL_BOT = "bot"
CHANNEL_BOTH = "both"
CHANNELS = (CHANNEL_OFF, CHANNEL_SAVED, CHANNEL_BOT, CHANNEL_BOTH)

API_BASE = "https://api.telegram.org"
REQUEST_TIMEOUT = 15
DOCUMENT_TIMEOUT = 60
MAX_TEXT = 4096
MAX_CAPTION = 1024
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024

_TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,64}$")
_CHAT_ID_RE = re.compile(r"^-?\d{1,15}$")
_FILENAME_RE = re.compile(r"[^\w.\-]+", re.UNICODE)


class BotNotifyError(RuntimeError):
    """A failure with a user-safe Russian message (never contains the token)."""


def normalize_channel(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in CHANNELS else CHANNEL_OFF


def uses_saved(channel: str) -> bool:
    return channel in (CHANNEL_SAVED, CHANNEL_BOTH)


def uses_bot(channel: str) -> bool:
    return channel in (CHANNEL_BOT, CHANNEL_BOTH)


def validate_token(value: Any) -> str:
    token = str(value or "").strip()
    if not token:
        raise BotNotifyError("Введите токен бота из @BotFather.")
    if not _TOKEN_RE.match(token):
        raise BotNotifyError("Токен выглядит неверно. Скопируйте его целиком из @BotFather: 123456789:AA…")
    return token


def validate_chat_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise BotNotifyError("Укажите chat_id или нажмите «Определить chat_id».")
    if not _CHAT_ID_RE.match(text) or int(text) == 0:
        raise BotNotifyError("chat_id — это число, например 123456789. Его можно определить кнопкой.")
    return str(int(text))


def scrub(text: Any, token: str | None) -> str:
    """Remove the token (and the ``bot<token>`` URL form) from any text that may reach a log or the UI."""
    result = str(text)
    if token:
        result = result.replace(token, "***")
    return result


def _description(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("description") or "").lower()
    return ""


def _error_for(status: int, data: Any, *, method: str) -> BotNotifyError:
    description = _description(data)
    if status in (401, 404):
        return BotNotifyError("Telegram не принял токен бота. Проверьте токен в @BotFather (команда /token).")
    if status == 403:
        return BotNotifyError(
            "Бот не может написать в этот чат. Откройте бота в Telegram, нажмите «Запустить» и не блокируйте его."
        )
    if status == 409:
        return BotNotifyError(
            "У бота включён webhook или им управляет другая программа, поэтому getUpdates недоступен. "
            "Введите chat_id вручную."
        )
    if status == 429:
        retry = 0
        if isinstance(data, dict):
            params = data.get("parameters")
            if isinstance(params, dict):
                try:
                    retry = max(0, int(params.get("retry_after") or 0))
                except (TypeError, ValueError):
                    retry = 0
        suffix = f" Повторите через {retry} сек." if retry else " Повторите позже."
        return BotNotifyError("Telegram временно ограничил запросы бота." + suffix)
    if status == 400:
        if "chat not found" in description:
            return BotNotifyError(
                "Чат не найден. Проверьте chat_id и убедитесь, что вы нажали «Запустить» в своём боте."
            )
        if "too long" in description:
            return BotNotifyError("Сообщение слишком длинное для Telegram.")
        if "file is too big" in description or "too big" in description:
            return BotNotifyError("Файл слишком большой для Telegram. Сохраните его на ПК кнопкой «Сохранить файл».")
        return BotNotifyError("Telegram отклонил запрос бота. Проверьте токен и chat_id.")
    if status >= 500:
        return BotNotifyError("Сервис Telegram временно недоступен. Повторите позже.")
    return BotNotifyError(f"Telegram ответил кодом {status} ({method}). Проверьте токен и chat_id.")


async def _post(
    token: str,
    method: str,
    *,
    json_payload: dict | None = None,
    form: aiohttp.FormData | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> Any:
    """POST JSON or multipart to a Bot API method and return ``result``; raises ``BotNotifyError`` only."""
    url = f"{API_BASE}/bot{token}/{method}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(
                url,
                json=json_payload,
                data=form,
                allow_redirects=False,
            ) as response:
                status = response.status
                try:
                    data = await response.json(content_type=None)
                except (ValueError, aiohttp.ClientError):
                    data = None
    except asyncio.TimeoutError:
        raise BotNotifyError(f"Telegram не ответил за {int(timeout)} сек. Проверьте интернет и повторите.") from None
    except aiohttp.ClientError:
        # The raw exception text can contain the URL (= token); never propagate it.
        raise BotNotifyError("Не удалось подключиться к api.telegram.org. Проверьте интернет и VPN.") from None
    if status != 200 or not isinstance(data, dict) or not data.get("ok"):
        raise _error_for(status, data, method=method)
    return data.get("result")


async def _call(token: str, method: str, payload: dict, timeout: float = REQUEST_TIMEOUT) -> Any:
    return await _post(token, method, json_payload=payload, timeout=timeout)


def safe_document_filename(name: Any) -> str:
    cleaned = _FILENAME_RE.sub("_", str(name or "").strip()).strip("._") or "parsed"
    if not cleaned.lower().endswith(".txt"):
        cleaned += ".txt"
    return cleaned[:120]


async def send_message(token: str, chat_id: str | int, text: str, *, timeout: float = REQUEST_TIMEOUT) -> None:
    token = validate_token(token)
    chat = validate_chat_id(chat_id)
    body = str(text or "").strip()
    if not body:
        raise BotNotifyError("Пустое сообщение не отправляется.")
    payload = {"chat_id": int(chat), "text": body[:MAX_TEXT], "disable_web_page_preview": True}
    await _call(token, "sendMessage", payload, timeout)


async def send_document(
    token: str,
    chat_id: str | int,
    filename: str,
    content: bytes | str,
    *,
    caption: str = "",
    timeout: float = DOCUMENT_TIMEOUT,
) -> None:
    """Send a small text file through Bot API ``sendDocument`` (multipart)."""
    token = validate_token(token)
    chat = validate_chat_id(chat_id)
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    if not data:
        raise BotNotifyError("Пустой файл не отправляется.")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise BotNotifyError(
            f"Файл слишком большой для отправки через бота (больше {MAX_DOCUMENT_BYTES // (1024 * 1024)} МБ). "
            "Сохраните его на ПК кнопкой «Сохранить файл»."
        )
    form = aiohttp.FormData()
    form.add_field("chat_id", str(int(chat)))
    caption_text = str(caption or "").strip()
    if caption_text:
        form.add_field("caption", caption_text[:MAX_CAPTION])
    form.add_field(
        "document",
        data,
        filename=safe_document_filename(filename),
        content_type="text/plain; charset=utf-8",
    )
    await _post(token, "sendDocument", form=form, timeout=timeout)


def _pick_chat(updates: Any) -> str | None:
    """Newest private chat from updates, preferring the one that wrote /start. Only the numeric id is kept."""
    start_id = None
    last_id = None
    if not isinstance(updates, list):
        return None
    for update in updates:
        message = update.get("message") if isinstance(update, dict) else None
        chat = message.get("chat") if isinstance(message, dict) else None
        if not isinstance(chat, dict) or chat.get("type") != "private":
            continue
        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id == 0:
            continue
        last_id = chat_id
        if str(message.get("text") or "").strip().lower().startswith("/start"):
            start_id = chat_id
    found = start_id if start_id is not None else last_id
    return None if found is None else str(found)


async def detect_chat_id(token: str, *, timeout: float = REQUEST_TIMEOUT) -> str:
    """Find the chat id of the user who wrote to the bot (getUpdates). Updates are read, not acknowledged."""
    token = validate_token(token)
    updates = await _call(token, "getUpdates", {"limit": 100, "timeout": 0, "allowed_updates": ["message"]}, timeout)
    chat_id = _pick_chat(updates)
    if chat_id is None:
        raise BotNotifyError(
            "Бот пока не получил сообщений. Откройте своего бота в Telegram, нажмите «Запустить» "
            "(или отправьте /start) и повторите."
        )
    return chat_id


# -- stored settings ------------------------------------------------------------------------
async def save_bot_settings(db, *, token: str | None = None, chat_id: str | None = None) -> None:
    """Validate and store. ``token=None`` keeps the saved one; the token is protected (DPAPI) before storing."""
    new_token = validate_token(token) if token is not None and str(token).strip() else None
    text = None if chat_id is None else str(chat_id).strip()
    new_chat = validate_chat_id(text) if text else ""
    # Everything is validated first: a bad chat_id must not leave a half-saved configuration.
    if new_token is not None:
        await db.set_setting(NOTIFY_TOKEN_KEY, protect(new_token))
    if text is not None:
        await db.set_setting(NOTIFY_CHAT_ID_KEY, new_chat)


async def clear_bot_token(db) -> None:
    await db.set_setting(NOTIFY_TOKEN_KEY, "")


async def saved_token(db) -> str:
    """The stored token, revealed. Raises ``BotNotifyError`` when none is saved or it cannot be decrypted."""
    stored = await db.get_setting(NOTIFY_TOKEN_KEY) or ""
    if not stored:
        raise BotNotifyError("Токен бота не задан. Введите его в настройках сводки.")
    try:
        return validate_token(reveal(stored))
    except RuntimeError as exc:
        if isinstance(exc, BotNotifyError):
            raise
        raise BotNotifyError(str(exc)) from None


async def load_bot_credentials(db) -> tuple[str, str]:
    """Return ``(token, chat_id)`` (token revealed). Raises ``BotNotifyError`` when incomplete."""
    token = await saved_token(db)
    return token, validate_chat_id(await db.get_setting(NOTIFY_CHAT_ID_KEY))


async def bot_is_configured(db) -> bool:
    values = await db.get_settings_map([NOTIFY_TOKEN_KEY, NOTIFY_CHAT_ID_KEY])
    chat = str(values.get(NOTIFY_CHAT_ID_KEY) or "").strip()
    return bool(values.get(NOTIFY_TOKEN_KEY)) and bool(_CHAT_ID_RE.match(chat))


async def send_to_bot(db, text: str) -> None:
    """Send ``text`` through the user's own bot using the stored token and chat id."""
    token, chat_id = await load_bot_credentials(db)
    try:
        await send_message(token, chat_id, text)
    except BotNotifyError:
        raise
    except Exception:  # noqa: BLE001 - unknown errors might quote the URL (= token); drop their text
        raise BotNotifyError("Не удалось отправить сообщение через бота. Проверьте интернет и настройки.") from None


async def send_document_to_bot(db, filename: str, content: bytes | str, *, caption: str = "") -> None:
    """Send a document through the user's own bot using the stored token and chat id."""
    token, chat_id = await load_bot_credentials(db)
    try:
        await send_document(token, chat_id, filename, content, caption=caption)
    except BotNotifyError:
        raise
    except Exception:  # noqa: BLE001 - unknown errors might quote the URL (= token); drop their text
        raise BotNotifyError("Не удалось отправить файл через бота. Проверьте интернет и настройки.") from None
