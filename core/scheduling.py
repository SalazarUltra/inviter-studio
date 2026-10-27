"""Schedule helpers: weekday gate and the time zone used by the smart interval.

Pure functions only (no database, no UI) so the engines, the task tab and the tests share one rule.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo

SMART_TZ_LOCAL = "local"
SMART_TZ_MOSCOW = "Europe/Moscow"
SMART_TZ_SETTING = "smart_interval_tz"
SCHEDULE_DAYS_SETTING = "schedule_days"

# Label shown in the UI -> value stored in the campaign settings.
SMART_TZ_CHOICES = {
    "Время этого компьютера": SMART_TZ_LOCAL,
    "Москва (Europe/Moscow)": SMART_TZ_MOSCOW,
}
WEEKDAY_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
ALL_DAYS = frozenset(range(7))
DEFAULT_SCHEDULE_DAYS = "0,1,2,3,4,5,6"

# Moscow has had no daylight saving time since 2014: a fixed UTC+3 is exact when tzdata is unavailable
# (Windows ships no IANA database; the frozen build may not include the tzdata package).
_MOSCOW_FALLBACK = timezone(timedelta(hours=3), "MSK")


def normalize_smart_tz(value) -> str:
    """Known time zone key; anything unknown or empty means the computer's local time."""
    text = str(value or "").strip()
    if text.lower() in {"", SMART_TZ_LOCAL}:
        return SMART_TZ_LOCAL
    if text == SMART_TZ_MOSCOW:
        return SMART_TZ_MOSCOW
    return SMART_TZ_LOCAL


def validate_smart_tz(value) -> str:
    """Strict variant for user input: unknown values are an error instead of a silent fallback."""
    text = str(value or "").strip() or SMART_TZ_LOCAL
    if text.lower() == SMART_TZ_LOCAL:
        return SMART_TZ_LOCAL
    if text == SMART_TZ_MOSCOW:
        return SMART_TZ_MOSCOW
    raise ValueError("Часовой пояс умного интервала: «Время компьютера» или «Москва»")


def smart_tz_label(value) -> str:
    key = normalize_smart_tz(value)
    return next((label for label, stored in SMART_TZ_CHOICES.items() if stored == key), "Время этого компьютера")


def _zone(key: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(key)
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError / missing tzdata / bad key
        return _MOSCOW_FALLBACK


def smart_now(tz_setting, now: datetime | None = None) -> datetime:
    """Current moment as a wall clock in the chosen zone (naive local time for «local»)."""
    key = normalize_smart_tz(tz_setting)
    moment = now if now is not None else datetime.now()
    if key == SMART_TZ_LOCAL:
        return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment
    if moment.tzinfo is None:
        moment = moment.astimezone()  # naive values are the computer's local time
    return moment.astimezone(_zone(key))


def smart_hour(tz_setting, now: datetime | None = None) -> int:
    """Hour of day (0-23) used to pick the smart-interval multiplier."""
    return smart_now(tz_setting, now).hour


def parse_schedule_days(value) -> frozenset[int]:
    """Weekdays 0 (Monday) .. 6 (Sunday) from «0,1,4», a list/set, or a bitmask written as «mask:31».

    Empty/missing means every day. Raises ValueError for unreadable values and for «no day selected».
    """
    if value is None:
        return ALL_DAYS
    if isinstance(value, (set, frozenset, list, tuple)):
        days = {int(item) for item in value}
    else:
        text = str(value).strip()
        if not text:
            return ALL_DAYS
        try:
            if text.lower().startswith("mask:"):
                mask = int(text[5:].strip(), 0)
                if not 0 < mask < 128:
                    raise ValueError
                days = {day for day in range(7) if mask & (1 << day)}
            else:
                days = {int(part) for part in text.replace(";", ",").split(",") if part.strip()}
        except ValueError:
            raise ValueError("Дни недели: числа 0–6 через запятую (0 — понедельник)") from None
    if not days:
        raise ValueError("Выберите хотя бы один день недели")
    if not days <= ALL_DAYS:
        raise ValueError("Дни недели: числа 0–6 через запятую (0 — понедельник)")
    return frozenset(days)


def format_schedule_days(days) -> str:
    """Canonical stored value: sorted CSV of weekday numbers."""
    return ",".join(str(day) for day in sorted(parse_schedule_days(days)))


def describe_schedule_days(value) -> str:
    """Short Russian summary: «каждый день» or «Пн, Ср, Пт»."""
    days = parse_schedule_days(value)
    if days == ALL_DAYS:
        return "каждый день"
    return ", ".join(WEEKDAY_SHORT[day] for day in sorted(days))


def weekday_allowed(value, now: datetime | None = None) -> bool:
    """True when `now` (computer time) falls on one of the selected weekdays."""
    moment = now if now is not None else datetime.now()
    return moment.weekday() in parse_schedule_days(value)
