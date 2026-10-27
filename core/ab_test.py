"""A/B text variants for direct-message campaigns.

Every recipient is assigned to variant ``A`` or ``B`` by a hash, so the same person always lands in the
same group (re-queue after an uncertain send, restart of the app, repeated calls) and no state has to be
stored for the decision itself. The variant that was actually used is saved with the delivery record.
"""

from __future__ import annotations

import hashlib

VARIANTS = ("A", "B")
DEFAULT_SPLIT_A = 50
MIN_SPLIT_A = 1
MAX_SPLIT_A = 99
_BUCKETS = 10_000


def normalize_split(value) -> int:
    """Percent of recipients that receive variant A (1..99); empty means the default 50/50."""
    text = str(value if value is not None else "").strip()
    if not text:
        return DEFAULT_SPLIT_A
    if not text.isascii() or not text.isdigit() or not MIN_SPLIT_A <= int(text) <= MAX_SPLIT_A:
        raise ValueError(f"Доля варианта A: целое число процентов от {MIN_SPLIT_A} до {MAX_SPLIT_A}")
    return int(text)


def variant_key(username, user_id=None) -> str:
    """Stable identity of a recipient: the @username (lower-case) when known, otherwise the Telegram id.

    A username is known before the recipient is resolved, so the planned split can be previewed for the
    queue; the id is the fallback for recipients that have no username.
    """
    name = str(username or "").strip().lstrip("@").lower()
    if name:
        return name
    if user_id:
        return f"id:{int(user_id)}"
    raise ValueError("Для A/B-распределения нужен никнейм или Telegram ID получателя")


def assign_variant(key: str, split_a: int = DEFAULT_SPLIT_A, salt: str = "") -> str:
    """Deterministic ``"A"`` or ``"B"`` for ``key``; ``split_a`` percent of keys get ``"A"``.

    ``salt`` (the campaign id) keeps experiments independent: the same person is not always in group A.
    """
    split = normalize_split(split_a)
    digest = hashlib.sha256(f"{salt}\x1f{key}".encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") % _BUCKETS
    return "A" if bucket < split * (_BUCKETS // 100) else "B"


def planned_split(keys, split_a: int = DEFAULT_SPLIT_A, salt: str = "") -> dict[str, int]:
    """How many of ``keys`` would receive each variant (used for the review screen)."""
    counts = {"A": 0, "B": 0}
    for key in keys:
        counts[assign_variant(key, split_a, salt)] += 1
    return counts


def split_label(split_a: int) -> str:
    split = normalize_split(split_a)
    return f"A {split}% / B {100 - split}%"


class ABConfig:
    """Resolved A/B settings of one campaign run."""

    def __init__(self, text_a: str, text_b: str | None = None, split_a: int = DEFAULT_SPLIT_A, salt: str = ""):
        self.text_a = text_a
        self.text_b = text_b
        self.split_a = normalize_split(split_a)
        self.salt = str(salt)

    @property
    def enabled(self) -> bool:
        return self.text_b is not None

    def choose(self, key: str) -> tuple[str | None, str]:
        """``(variant, text)`` for a recipient; ``(None, text_a)`` when A/B is off."""
        if not self.enabled:
            return None, self.text_a
        variant = assign_variant(key, self.split_a, self.salt)
        return variant, self.text_b if variant == "B" else self.text_a


def _rate(replies: int, sent: int) -> str:
    return f"{round(100 * replies / sent)}%" if sent else "—"


def format_ab_stats(stats: dict) -> str:
    """Compact Russian summary for the audience/report screens; empty when no A/B message was sent."""
    a, b = stats["A"], stats["B"]
    if not (a["sent"] or b["sent"]):
        return ""
    text = f"A/B-тест · отправлено: A — {a['sent']}, B — {b['sent']}"
    if stats.get("replies_tracked"):
        text += (
            f" · ответов: A — {a['replies']} ({_rate(a['replies'], a['sent'])}), "
            f"B — {b['replies']} ({_rate(b['replies'], b['sent'])})"
        )
    else:
        text += " · ответы не отслеживаются (включите второе сообщение)"
    if stats.get("untested"):
        text += f" · без теста: {stats['untested']}"
    return text
