"""Campaign export/import as a JSON file that is safe to share.

Only an explicit allow-list of campaign settings is ever written. Accounts, Telegram sessions, ``api_id`` /
``api_hash``, proxies (including passwords), AI provider keys and the list of selected accounts are *not*
part of a campaign file, and anything unknown found in an imported file is ignored, never stored.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from core.ab_test import normalize_split
from core.database import INVITE_ORDERS
from core.followup_store import FOLLOWUP_WAIT_HOURS_RANGE
from core.pacing import parse_daily_cap
from core.scheduling import parse_schedule_days, validate_smart_tz
from core.validation import (
    is_id_recipient,
    normalize_recipient,
    normalize_username,
    parse_user_id,
    recipient_user_id,
    time_minutes,
    validate_interval,
    validate_message,
)

FORMAT_NAME = "botinviter.campaign"
FORMAT_VERSION = 1
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TARGETS = 500_000
MAX_NAME_LENGTH = 80
IMPORT_SUFFIX = " (импорт)"

# Settings that travel with a campaign. Everything else (accounts, run state, secrets) stays local.
EXPORT_SETTING_KEYS = (
    "target_chat",
    "message_text",
    "ab_enabled",
    "message_text_b",
    "ab_split_a",
    "followup_enabled",
    "followup_text",
    "followup_wait_hours",
    "interval_min",
    "interval_max",
    "schedule_enabled",
    "schedule_from",
    "schedule_to",
    "schedule_days",
    "smart_interval_tz",
    "protection_consecutive",
    "protection_break_min",
    "protection_break_max",
    "invite_order",
    "campaign_daily_cap",
    "require_warmup_ready",
)

# Names that must never appear anywhere in a campaign file (checked on export and on import).
FORBIDDEN_KEYS = frozenset(
    {
        "api_id",
        "api_hash",
        "apihash",
        "api_key",
        "apikey",
        "session",
        "sessions",
        "session_name",
        "session_string",
        "string_session",
        "auth_key",
        "phone",
        "proxy",
        "proxy_type",
        "proxy_host",
        "proxy_port",
        "proxy_login",
        "proxy_password",
        "password",
        "token",
        "secret",
        "ai_key",
        "ai_configs",
        "accounts",
        "selected_accounts",
    }
)

SECRETS_NOTICE = (
    "В файл попадают тексты, настройки и список получателей. Ключи API (api_id/api_hash), файлы сессий "
    "Telegram, прокси и пароли, ключи ИИ и выбранные аккаунты НЕ экспортируются. Список получателей — "
    "персональные данные: не публикуйте файл."
)


class ImportedCampaign(dict):
    """Validated import: ``name``, ``kind``, ``settings`` (strings), ``targets`` (dicts), ``ignored``."""


def find_forbidden_keys(value: Any, _path: str = "") -> list[str]:
    """Paths of every forbidden key anywhere in a JSON-like structure."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            where = f"{_path}.{key}" if _path else str(key)
            if str(key).strip().lower() in FORBIDDEN_KEYS:
                found.append(where)
            found.extend(find_forbidden_keys(item, where))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(find_forbidden_keys(item, f"{_path}[{index}]"))
    return found


def _target_entry(row: dict, include_statuses: bool) -> dict:
    username = str(row.get("username") or "")
    if is_id_recipient(username):
        entry = {"user_id": recipient_user_id(username)}
    else:
        entry = {"username": username}
        if row.get("user_id"):
            entry["user_id"] = int(row["user_id"])
    if include_statuses and row.get("status"):
        entry["status"] = str(row["status"])
    return entry


def build_campaign_export(data: dict, *, include_statuses: bool = False) -> dict:
    """JSON-ready dict from ``Database.get_campaign_export_data()``; never contains secrets."""
    settings = {key: str(value) for key, value in data.get("settings", {}).items() if key in EXPORT_SETTING_KEYS}
    if data.get("kind") != "messages":
        settings.pop("ab_enabled", None)  # A/B texts exist only in direct-message campaigns
    payload = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "includes_statuses": bool(include_statuses),
        "campaign": {"name": str(data["name"]), "kind": str(data["kind"])},
        "settings": settings,
        "targets": [_target_entry(row, include_statuses) for row in data.get("targets", [])],
    }
    leaked = find_forbidden_keys(payload)
    if leaked:  # defence in depth: the allow-list above makes this unreachable
        raise RuntimeError("Экспорт отменён: в данных найдены секретные поля " + ", ".join(leaked))
    return payload


def _flag(value, label):
    if str(value) not in {"0", "1"}:
        raise ValueError(f"«{label}»: допустимы только 0 или 1")
    return str(value)


def _bounded_int(value, label, low, high):
    text = str(value).strip()
    if not text.isascii() or not text.isdigit() or not low <= int(text) <= high:
        raise ValueError(f"«{label}»: целое число от {low} до {high}")
    return str(int(text))


def _clean_settings(raw: dict, kind: str) -> tuple[dict[str, str], list[str]]:
    settings: dict[str, str] = {}
    ignored = [str(key) for key in raw if key not in EXPORT_SETTING_KEYS]
    for key in EXPORT_SETTING_KEYS:
        if key not in raw or raw[key] is None:
            continue
        value = raw[key]
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ValueError(f"Настройка «{key}» имеет недопустимый тип")
        value = str(value)
        if key in {"message_text", "message_text_b", "followup_text"}:
            if value.strip():
                validate_message(value)
        elif key == "target_chat":
            value = normalize_username(value) if value.strip() else ""
        elif key == "ab_split_a":
            value = str(normalize_split(value))
        elif key in {"ab_enabled", "followup_enabled", "schedule_enabled", "require_warmup_ready"}:
            value = _flag(value, key)
        elif key == "followup_wait_hours":
            value = _bounded_int(value, key, *FOLLOWUP_WAIT_HOURS_RANGE)
        elif key in {"interval_min", "interval_max"}:
            value = _bounded_int(value, key, 10, 86400)
        elif key in {"protection_consecutive", "protection_break_min", "protection_break_max"}:
            value = _bounded_int(value, key, 1, 10000)
        elif key in {"schedule_from", "schedule_to"}:
            time_minutes(value)
        elif key == "schedule_days":
            parse_schedule_days(value)
        elif key == "smart_interval_tz":
            value = validate_smart_tz(value)
        elif key == "invite_order":
            if value not in INVITE_ORDERS:
                raise ValueError("Неизвестный порядок приглашений")
        elif key == "campaign_daily_cap":
            value = str(parse_daily_cap(value))
        settings[key] = value
    if "interval_min" in settings and "interval_max" in settings:
        validate_interval(kind, int(settings["interval_min"]), int(settings["interval_max"]))
    if settings.get("ab_enabled") == "1":
        if kind != "messages":
            raise ValueError("A/B-тест текста доступен только в кампаниях личных сообщений")
        if not settings.get("message_text_b", "").strip():
            raise ValueError("A/B-тест включён, но текст B пустой")
    if settings.get("followup_enabled") == "1" and not settings.get("followup_text", "").strip():
        raise ValueError("Второе сообщение включено, но его текст пустой")
    return settings, ignored


def _clean_targets(raw: list, kind: str) -> list[dict]:
    if len(raw) > MAX_TARGETS:
        raise ValueError(f"В файле больше {MAX_TARGETS} получателей")
    targets: dict[str, dict] = {}
    for index, item in enumerate(raw, 1):
        try:
            if isinstance(item, str):
                item = {"username": item}
            if not isinstance(item, dict):
                raise ValueError("ожидается строка или объект")
            username = str(item.get("username") or "").strip()
            user_id = item.get("user_id")
            if username:
                key = normalize_recipient(username)
            elif user_id not in (None, ""):
                key = normalize_recipient(user_id)
            else:
                raise ValueError("нужен username или user_id")
            clean = {"username": "" if is_id_recipient(key) else key}
            known_id = recipient_user_id(key)
            if known_id is None and user_id not in (None, ""):
                known_id = parse_user_id(user_id)
                if known_id is None:
                    raise ValueError("некорректный user_id")
            if known_id:
                clean["user_id"] = known_id
            if item.get("status") is not None:
                clean["status"] = str(item["status"])
        except ValueError as exc:
            raise ValueError(f"Получатель №{index}: {exc}") from None
        targets.setdefault(key, clean)
    return list(targets.values())


def parse_campaign_import(payload: Any, *, rename: bool = True) -> ImportedCampaign:
    """Validate a decoded campaign file; raises ``ValueError`` (Russian text) when it is unusable."""
    if not isinstance(payload, dict) or payload.get("format") != FORMAT_NAME:
        raise ValueError("Это не файл кампании BotInvityer (ожидается экспорт кампании в JSON)")
    version = payload.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("Не указана версия файла кампании")
    if version > FORMAT_VERSION:
        raise ValueError("Файл создан в более новой версии программы. Обновите BotInvityer и повторите импорт")
    campaign = payload.get("campaign")
    if not isinstance(campaign, dict):
        raise ValueError("В файле нет описания кампании")
    kind = campaign.get("kind")
    if kind not in {"invite", "messages"}:
        raise ValueError("Неизвестный режим кампании в файле")
    name = str(campaign.get("name") or "").strip() or "Импортированная кампания"
    if rename:
        name = name[: MAX_NAME_LENGTH - len(IMPORT_SUFFIX)].rstrip() + IMPORT_SUFFIX
    name = name[:MAX_NAME_LENGTH]
    settings_raw = payload.get("settings", {})
    targets_raw = payload.get("targets", [])
    if not isinstance(settings_raw, dict) or not isinstance(targets_raw, list):
        raise ValueError("Некорректная структура файла кампании")
    # Secrets are never read from a file: report them and drop them with every other unknown field.
    secret_paths = find_forbidden_keys({key: value for key, value in payload.items() if key != "targets"})
    settings, ignored = _clean_settings(settings_raw, kind)
    targets = _clean_targets(targets_raw, kind)
    return ImportedCampaign(
        name=name,
        kind=kind,
        settings=settings,
        targets=targets,
        ignored=sorted(set(ignored)),
        secrets_dropped=sorted(set(secret_paths)),
    )


def write_campaign_file(path: str, payload: dict) -> None:
    """Atomic write: a failure cannot truncate an existing file."""
    destination = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent, delete=False, suffix=".tmp"
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, destination)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def read_campaign_file(path: str) -> Any:
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise ValueError(f"Не удалось открыть файл: {exc.strerror or exc}") from None
    if size > MAX_FILE_BYTES:
        raise ValueError("Файл слишком большой для файла кампании (максимум 25 МБ)")
    try:
        return json.loads(source.read_text(encoding="utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Файл не читается: ожидается JSON в кодировке UTF-8") from None


async def export_campaign(db, path: str, *, include_statuses: bool = False) -> dict:
    """Write the campaign selected in ``db`` to ``path``; returns the exported payload."""
    data = await db.get_campaign_export_data(EXPORT_SETTING_KEYS)
    payload = build_campaign_export(data, include_statuses=include_statuses)
    await asyncio.to_thread(write_campaign_file, path, payload)
    return payload


async def import_campaign(db, parsed: ImportedCampaign) -> dict:
    """Create a NEW campaign from a validated import. Returns ``{"campaign_id", "stats"}``.

    Recipients always start as pending (statuses in the file are informational); people that are already in
    the shared delivery ledger keep the usual «no repeat» status. Accounts have to be chosen again.
    """
    campaign_id = await db.create_campaign_with_settings(parsed["name"], parsed["kind"], parsed["settings"])
    scoped = db.for_campaign(campaign_id)
    try:
        stats = await scoped.import_audience([dict(item) for item in parsed["targets"]], preserve_order=True)
    except Exception:
        try:
            await scoped.delete_campaign()
        except Exception:  # noqa: BLE001 - keep the original error
            pass
        raise
    return {"campaign_id": campaign_id, "stats": stats}
