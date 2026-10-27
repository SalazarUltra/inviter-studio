"""Safety pacing: presets, aggressive-settings warnings, warmup gate and daily campaign cap.

Pure helpers shared by the Task tab and the engines. Nothing here talks to Telegram.
"""

from __future__ import annotations

from typing import Any, Iterable

PACE_KEYS = ("safe", "normal", "fast")
PACE_LABELS = {"safe": "Безопасный", "normal": "Обычный", "fast": "Быстрый"}
CUSTOM_PACE_KEY = "custom"
CUSTOM_PACE_LABEL = "Свой"

# interval_min / interval_max (сек), protection_consecutive (действий), break min/max (минут).
_PRESETS: dict[str, dict[str, tuple[int, int, int, int, int]]] = {
    "messages": {
        "safe": (90, 180, 3, 10, 25),
        "normal": (60, 120, 5, 5, 15),
        "fast": (45, 90, 7, 3, 8),
    },
    "invite": {
        "safe": (40, 90, 3, 10, 25),
        "normal": (20, 45, 5, 5, 15),
        "fast": (15, 30, 8, 3, 8),
    },
}
PACE_FIELDS = ("interval_min", "interval_max", "protection_consecutive", "protection_break_min", "protection_break_max")

# Below these values the pace is flagged as aggressive (risk of PeerFlood / limits).
MESSAGES_SAFE_INTERVAL_MIN = 60
INVITE_SAFE_INTERVAL_MIN = 20
MAX_SAFE_CONSECUTIVE = 8
MAX_DAILY_CAP = 100000


def _mode(mode: str) -> str:
    return "messages" if mode == "messages" else "invite"


def preset_values(mode: str, key: str) -> dict[str, str]:
    """Settings (as strings, ready for entries / save_campaign_settings) for a preset."""
    if key not in PACE_KEYS:
        raise ValueError("Неизвестный темп")
    return dict(zip(PACE_FIELDS, map(str, _PRESETS[_mode(mode)][key]), strict=True))


def detect_preset(mode: str, values: dict[str, Any]) -> str:
    """Return the preset key matching the numbers, or CUSTOM_PACE_KEY («Свой»)."""
    try:
        numbers = tuple(int(str(values[field]).strip()) for field in PACE_FIELDS)
    except (KeyError, ValueError):
        return CUSTOM_PACE_KEY
    for key, preset in _PRESETS[_mode(mode)].items():
        if numbers == preset:
            return key
    return CUSTOM_PACE_KEY


def pace_label(key: str) -> str:
    return PACE_LABELS.get(key, CUSTOM_PACE_LABEL)


def aggressive_warnings(mode: str, interval_min: int, consecutive: int) -> list[str]:
    """Russian warnings for risky pace settings; an empty list means the pace is reasonable."""
    warnings = []
    if _mode(mode) == "messages":
        if interval_min < MESSAGES_SAFE_INTERVAL_MIN:
            warnings.append(
                f"Интервал между личными сообщениями {interval_min} сек — меньше {MESSAGES_SAFE_INTERVAL_MIN}. "
                "Частая отправка незнакомым людям ведёт к PeerFlood и ограничению аккаунта."
            )
    elif interval_min < INVITE_SAFE_INTERVAL_MIN:
        warnings.append(
            f"Интервал между приглашениями {interval_min} сек — меньше {INVITE_SAFE_INTERVAL_MIN}. "
            "Частые приглашения повышают риск ограничений Telegram."
        )
    if consecutive > MAX_SAFE_CONSECUTIVE:
        warnings.append(
            f"Перерыв только после {consecutive} действий подряд — больше {MAX_SAFE_CONSECUTIVE}. "
            "Длинные серии без перерывов повышают риск ограничений."
        )
    return warnings


def format_warning_block(warnings: Iterable[str]) -> str:
    items = list(warnings)
    if not items:
        return ""
    return "ВНИМАНИЕ: агрессивные настройки темпа\n" + "\n".join("• " + item for item in items)


def is_warmup_ready(account: dict) -> bool:
    return str(account.get("warmup_status") or "cold").lower() == "ready"


WARMUP_STATUS_NAMES = {
    "cold": "не прогревался",
    "warming": "прогревается",
    "paused": "прогрев на паузе",
    "ready": "прогрет",
    "error": "ошибка прогрева",
}


def split_warmup_ready(accounts: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Split accounts into (ready, skipped) for the «only warmed-up accounts» gate."""
    ready, skipped = [], []
    for account in accounts:
        (ready if is_warmup_ready(account) else skipped).append(account)
    return ready, skipped


def gate_accounts(accounts: Iterable[dict], require_ready: bool) -> tuple[list[dict], list[dict]]:
    """Apply the optional warmup gate: (accounts allowed to start, accounts skipped by the gate)."""
    items = list(accounts)
    return split_warmup_ready(items) if require_ready else (items, [])


def warmup_gate_error(skipped: Iterable[dict]) -> str:
    return (
        "Включено «Запускать только с прогретыми аккаунтами», но среди выбранных нет прогретых. "
        "Пропущены: " + describe_skipped_warmup(skipped) + ". Завершите прогрев или отключите это ограничение."
    )


def describe_skipped_warmup(skipped: Iterable[dict]) -> str:
    return ", ".join(
        f"{item.get('phone') or item.get('id')} ({WARMUP_STATUS_NAMES.get(str(item.get('warmup_status') or 'cold'), 'не прогрет')})"
        for item in skipped
    )


def parse_daily_cap(value: Any) -> int:
    """Validate the campaign daily cap field (0 = off)."""
    text = str(value if value is not None else "").strip() or "0"
    if not text.isdigit() or int(text) > MAX_DAILY_CAP:
        raise ValueError(f"Дневной лимит кампании: целое число от 0 до {MAX_DAILY_CAP} (0 — без лимита)")
    return int(text)


def daily_cap_status(done: int, cap: int, mode: str = "invite") -> str:
    """Short Russian status; empty when the cap is off."""
    if cap <= 0:
        return ""
    noun = "сообщений" if _mode(mode) == "messages" else "приглашений"
    if done >= cap:
        return f"Дневной лимит кампании достигнут: {done}/{cap} {noun}. Новые действия — завтра"
    return f"Сегодня по кампании: {done}/{cap} {noun}"


def daily_cap_reached(done: int, cap: int) -> bool:
    return cap > 0 and done >= cap
